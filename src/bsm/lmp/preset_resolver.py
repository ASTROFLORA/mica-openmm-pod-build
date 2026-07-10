"""LMP Preset Resolver — Map UniProt IDs to preset XML files on disk.

Scans `output_all_presets/` directories to find the best matching LMP preset
for a given UniProt accession. Supports preset tier selection (full, nesy-core,
semantic) and state-specific queries.

Usage:
    from bsm.lmp.preset_resolver import LMPPresetResolver

    resolver = LMPPresetResolver()
    path = resolver.resolve("Q9H4A3")  # → full preset for WNK1
    path = resolver.resolve("Q9H4A3", preset="semantic")
    all_presets = resolver.list_presets()
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default base directory for presets (relative to src/bsm/lmp/)
_DEFAULT_PRESET_BASE = Path(__file__).parent / "output_all_presets"
_DEFAULT_LMP_OUTPUTS = Path(__file__).parent / "lmp_outputs"

# Preset tier priority (highest first)
PRESET_TIERS = ["full", "nesy-core", "semantic", "structural", "md-ifp", "llm-context"]


class LMPPresetResolver:
    """Resolve UniProt accessions to LMP v4 preset XML file paths.
    
    Scans the preset output directories (full/, nesy-core/, semantic/, etc.)
    and builds an index mapping accession → file path.
    """

    def __init__(self, preset_base: Optional[str | Path] = None):
        """Initialize resolver.
        
        Args:
            preset_base: Root directory containing preset subdirectories.
                         Defaults to src/bsm/lmp/output_all_presets/
        """
        self.preset_base = Path(preset_base) if preset_base else _DEFAULT_PRESET_BASE
        self._index: Dict[str, Dict[str, Path]] = {}  # accession -> {tier: path}
        self._pdb_index: Dict[str, Dict[str, Path]] = {}  # pdb_id -> {tier: path}
        self._built = False

    def _build_index(self) -> None:
        """Scan preset directories and build the accession → path index."""
        if self._built:
            return

        self._index.clear()

        if not self.preset_base.exists():
            logger.warning(f"Preset base directory not found: {self.preset_base}")
            self._built = True
            return

        # Pattern to extract UniProt accession from filename
        # e.g., Q9H4A3_WNK1_full_Phosphorylated_Active.xml → Q9H4A3
        accession_pattern = re.compile(
            r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5})_"
        )
        # Pattern for PDB-keyed files: e.g., 2V3S_OSR1_full_Active.xml → 2V3S
        pdb_pattern = re.compile(
            r"^([0-9][A-Za-z][A-Za-z0-9]{2})_"
        )

        for tier_dir in self.preset_base.iterdir():
            if not tier_dir.is_dir():
                continue
            tier_name = tier_dir.name  # "full", "nesy-core", etc.

            for xml_file in tier_dir.glob("*.xml"):
                match = accession_pattern.match(xml_file.name)
                if match:
                    accession = match.group(1)
                    if accession not in self._index:
                        self._index[accession] = {}
                    self._index[accession][tier_name] = xml_file
                else:
                    pdb_match = pdb_pattern.match(xml_file.name)
                    if pdb_match:
                        pdb_id = pdb_match.group(1).upper()
                        if pdb_id not in self._pdb_index:
                            self._pdb_index[pdb_id] = {}
                        self._pdb_index[pdb_id][tier_name] = xml_file

        # Also scan lmp_outputs/ for accession-keyed nested presets
        self._scan_lmp_outputs()

        total_prot = len(self._index)
        total_pdb = len(self._pdb_index)
        total_files = (sum(len(v) for v in self._index.values())
                       + sum(len(v) for v in self._pdb_index.values()))
        logger.info(
            f"LMP Preset Resolver: indexed {total_prot} UniProt + {total_pdb} PDB "
            f"proteins across {total_files} preset files"
        )
        self._built = True

    def _scan_lmp_outputs(self) -> None:
        """Scan lmp_outputs/ nested directory for additional presets.
        
        Structure: lmp_outputs/{accession}/{tier}/{accession}_{tier}_*.xml
        Only indexes accessions NOT already in _index (avoids duplicates).
        """
        lmp_dir = self.preset_base.parent / "lmp_outputs"
        if not lmp_dir.exists():
            return

        accession_pattern = re.compile(
            r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5})$"
        )

        for accession_dir in lmp_dir.iterdir():
            if not accession_dir.is_dir():
                continue
            accession = accession_dir.name.upper()
            if not accession_pattern.match(accession):
                continue
            # Skip if already indexed from output_all_presets
            if accession in self._index:
                continue

            for tier_dir in accession_dir.iterdir():
                if not tier_dir.is_dir():
                    continue
                tier_name = tier_dir.name
                for xml_file in tier_dir.glob("*.xml"):
                    if accession not in self._index:
                        self._index[accession] = {}
                    self._index[accession][tier_name] = xml_file

    def resolve(
        self,
        uniprot_id: str,
        preset: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Optional[Path]:
        """Resolve a UniProt accession to the best matching preset XML.
        
        Args:
            uniprot_id: UniProt accession (e.g., "Q9H4A3")
            preset: Preferred preset tier ("full", "semantic", "nesy-core", etc.)
                    Defaults to highest available tier.
            state: Preferred conformational state (e.g., "Phosphorylated_Active")
                   Filters matching files by state name if specified.
        
        Returns:
            Path to XML file, or None if not found.
        """
        self._build_index()

        accession = uniprot_id.strip().upper()
        tiers = self._index.get(accession)
        if not tiers:
            logger.debug(f"No LMP preset found for {accession}")
            return None

        # If specific preset requested, try it
        if preset and preset in tiers:
            path = tiers[preset]
            if state and state.lower() not in path.name.lower():
                # Try to find a state-matching file in same tier
                state_match = self._find_state_match(accession, preset, state)
                if state_match:
                    return state_match
            return path

        # Otherwise, use tier priority
        for tier in PRESET_TIERS:
            if tier in tiers:
                path = tiers[tier]
                if state and state.lower() not in path.name.lower():
                    state_match = self._find_state_match(accession, tier, state)
                    if state_match:
                        return state_match
                return path

        # Fallback: any available tier
        return next(iter(tiers.values()))

    def _find_state_match(
        self, accession: str, tier: str, state: str
    ) -> Optional[Path]:
        """Find a file matching a specific conformational state."""
        tier_dir = self.preset_base / tier
        if not tier_dir.exists():
            return None

        state_lower = state.lower()
        for xml_file in tier_dir.glob(f"{accession}_*.xml"):
            if state_lower in xml_file.name.lower():
                return xml_file
        return None

    def list_presets(self) -> Dict[str, Dict[str, str]]:
        """List all available presets.
        
        Returns:
            Dict mapping accession → {tier: filename}
        """
        self._build_index()
        result = {}
        for accession, tiers in self._index.items():
            result[accession] = {
                tier: path.name for tier, path in tiers.items()
            }
        return result

    def get_available_proteins(self) -> List[str]:
        """Get list of all UniProt accessions with available presets."""
        self._build_index()
        return sorted(self._index.keys())

    def get_protein_tiers(self, uniprot_id: str) -> List[str]:
        """Get available preset tiers for a protein."""
        self._build_index()
        tiers = self._index.get(uniprot_id.strip().upper(), {})
        return sorted(tiers.keys())

    def get_all_files_for_protein(self, uniprot_id: str) -> Dict[str, Path]:
        """Get all preset files for a protein."""
        self._build_index()
        return dict(self._index.get(uniprot_id.strip().upper(), {}))

    def resolve_by_gene_name(self, gene_name: str) -> Optional[Path]:
        """Try to resolve by gene name (searches filenames).
        
        Args:
            gene_name: Gene name (e.g., "WNK1", "TP53")
            
        Returns:
            Path to best matching preset, or None
        """
        self._build_index()
        gene_upper = gene_name.upper()

        # Search UniProt-keyed index first
        for accession, tiers in self._index.items():
            for tier in PRESET_TIERS:
                if tier in tiers:
                    path = tiers[tier]
                    # Check if gene name appears in filename
                    if f"_{gene_upper}_" in path.name.upper():
                        return path

        # Also search PDB-keyed index
        for pdb_id, tiers in self._pdb_index.items():
            for tier in PRESET_TIERS:
                if tier in tiers:
                    path = tiers[tier]
                    if f"_{gene_upper}_" in path.name.upper():
                        return path

        return None

    def resolve_by_pdb_id(
        self,
        pdb_id: str,
        preset: Optional[str] = None,
    ) -> Optional[Path]:
        """Resolve a PDB ID to a preset XML file.
        
        Args:
            pdb_id: 4-character PDB ID (e.g., "2V3S")
            preset: Preferred preset tier. Defaults to highest available.
            
        Returns:
            Path to XML file, or None if not found.
        """
        self._build_index()
        pdb_upper = pdb_id.strip().upper()
        tiers = self._pdb_index.get(pdb_upper)
        if not tiers:
            return None

        if preset and preset in tiers:
            return tiers[preset]

        for tier in PRESET_TIERS:
            if tier in tiers:
                return tiers[tier]

        return next(iter(tiers.values()), None)


# Singleton
_resolver: Optional[LMPPresetResolver] = None


def get_preset_resolver(preset_base: Optional[str | Path] = None) -> LMPPresetResolver:
    """Get singleton preset resolver."""
    global _resolver
    if _resolver is None or preset_base is not None:
        _resolver = LMPPresetResolver(preset_base)
    return _resolver


__all__ = [
    "LMPPresetResolver",
    "get_preset_resolver",
    "PRESET_TIERS",
]

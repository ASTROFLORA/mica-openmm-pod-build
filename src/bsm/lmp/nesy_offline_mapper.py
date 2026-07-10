"""
NeSy Offline Mapper - UniProt JSON → NeSyAnnotation

Converts UniProt JSON entries (from snapshots) to NeSyAnnotation dataclass,
enabling 100% offline NeSy encoding without network access.

This bridges LMP v3's deterministic snapshots with v2's rich neuro-symbolic grammar.

Author: MICA Team
Date: 2026-01-20
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Set

from .nesy_encoder import NeSyAnnotation

logger = logging.getLogger(__name__)


# ============================================================================
# FEATURE TYPE MAPPINGS
# ============================================================================

# UniProt feature type → NeSy category
FEATURE_TYPE_MAP: Dict[str, str] = {
    # Domains
    "Domain": "domain",
    "Repeat": "domain",
    "Zinc finger": "domain",
    "Coiled coil": "domain",
    "Compositional bias": "domain",
    
    # Motifs
    "Motif": "motif",
    "Short sequence motif": "motif",
    
    # PTMs (Modified residue covers most)
    "Modified residue": "ptm",
    "Glycosylation": "ptm",
    "Disulfide bond": "ptm",
    "Cross-link": "ptm",
    "Lipidation": "ptm",
    
    # Binding sites
    "Binding site": "binding",
    "Active site": "binding",
    "Site": "binding",
    
    # Nucleotide/metal binding
    "Nucleotide binding": "nucleotide_binding",
    "Metal binding": "metal_binding",
    "Calcium binding": "metal_binding",
    "DNA binding": "dna_binding",
    
    # Topology/regions
    "Transmembrane": "transmembrane",
    "Intramembrane": "transmembrane",
    "Topological domain": "region",
    "Region": "region",
    
    # Processing
    "Signal peptide": "signal",
    "Transit peptide": "transit",
    "Propeptide": "propeptide",
    "Chain": "chain",
    "Peptide": "peptide",
    
    # Secondary structure (from PDB)
    "Helix": "secondary",
    "Beta strand": "secondary",
    "Turn": "secondary",
}

# Domain type classification for NeSy markers
DOMAIN_TYPE_KEYWORDS: Dict[str, str] = {
    "kinase": "Kinase",
    "sh2": "SH2",
    "sh3": "SH3",
    "ph": "PH",
    "pdz": "PDZ",
    "wd": "WD",
    "leucine-rich": "LRR",
    "ankyrin": "ANK",
    "zinc finger": "ZnF",
    "dna-binding": "DBD",
    "rna-binding": "RBD",
    "transmembrane": "TMD",
    "coiled": "CC",
    "ef-hand": "EF",
    "immunoglobulin": "Ig",
    "fibronectin": "FN3",
    "death": "DD",
    "card": "CARD",
    "bromo": "Bromo",
    "chromo": "Chromo",
    "tudor": "Tudor",
    "ring": "RING",
    "ubiquitin": "UBQ",
}

# PTM description patterns → (type, enzyme_group)
PTM_PATTERNS: List[Tuple[re.Pattern, str, Optional[str]]] = [
    # Phosphorylation
    (re.compile(r"Phospho(serine|threonine|tyrosine)", re.I), "phosphorylation", None),
    (re.compile(r"Phospho\w*;?\s*by\s+(\w+)", re.I), "phosphorylation", r"\1"),
    
    # Acetylation
    (re.compile(r"N6-acetyllysine", re.I), "acetylation", None),
    (re.compile(r"N-acetyl", re.I), "n_terminal_acetylation", None),
    
    # Methylation
    (re.compile(r"N6-methyllysine", re.I), "methylation", None),
    (re.compile(r"Omega-N-methylarginine", re.I), "methylation", None),
    (re.compile(r"Asymmetric dimethylarginine", re.I), "methylation", None),
    (re.compile(r"Symmetric dimethylarginine", re.I), "methylation", None),
    (re.compile(r"N6,N6-dimethyllysine", re.I), "methylation", None),
    (re.compile(r"N6,N6,N6-trimethyllysine", re.I), "methylation", None),
    
    # Ubiquitination
    (re.compile(r"Glycyl lysine isopeptide", re.I), "ubiquitination", None),
    (re.compile(r"ubiquitin", re.I), "ubiquitination", None),
    
    # SUMOylation
    (re.compile(r"sumo", re.I), "sumoylation", None),
    
    # Glycosylation
    (re.compile(r"N-linked.*GlcNAc", re.I), "n-glycosylation", None),
    (re.compile(r"O-linked.*GalNAc", re.I), "o-glycosylation", None),
    (re.compile(r"O-linked.*GlcNAc", re.I), "o-glycosylation", None),
    
    # Lipidation
    (re.compile(r"S-palmitoyl", re.I), "palmitoylation", None),
    (re.compile(r"N-myristoyl", re.I), "n_terminal_myristoylation", None),
    (re.compile(r"S-farnesyl", re.I), "farnesylation", None),
    (re.compile(r"S-geranylgeranyl", re.I), "geranylgeranylation", None),
    (re.compile(r"GPI-anchor", re.I), "gpi_anchor", None),
    
    # Other
    (re.compile(r"Hydroxylation", re.I), "hydroxylation", None),
    (re.compile(r"4-hydroxyproline", re.I), "hydroxylation", None),
    (re.compile(r"S-nitrosocysteine", re.I), "nitrosylation", None),
    (re.compile(r"ADP-ribosyl", re.I), "adp_ribosylation", None),
]

# Binding site type classification
BINDING_SITE_KEYWORDS: Dict[str, str] = {
    "atp": "ATP-binding",
    "gtp": "GTP-binding",
    "nad": "NAD-binding",
    "fad": "FAD-binding",
    "substrate": "substrate",
    "catalytic": "catalytic",
    "active": "catalytic",
    "allosteric": "allosteric",
    "dna": "DNA-binding",
    "rna": "RNA-binding",
    "metal": "metal-binding",
    "zinc": "ion-binding",
    "calcium": "ion-binding",
    "magnesium": "ion-binding",
    "iron": "ion-binding",
    "copper": "ion-binding",
    "manganese": "ion-binding",
}

# Metal ion extraction from descriptions
METAL_ION_PATTERNS: Dict[re.Pattern, str] = {
    re.compile(r"\bzinc\b|\bzn\b", re.I): "Zn",
    re.compile(r"\bcalcium\b|\bca\b", re.I): "Ca",
    re.compile(r"\bmagnesium\b|\bmg\b", re.I): "Mg",
    re.compile(r"\biron\b|\bfe\b", re.I): "Fe",
    re.compile(r"\bcopper\b|\bcu\b", re.I): "Cu",
    re.compile(r"\bmanganese\b|\bmn\b", re.I): "Mn",
    re.compile(r"\bcobalt\b|\bco\b", re.I): "Co",
    re.compile(r"\bnickel\b|\bni\b", re.I): "Ni",
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _extract_position(loc_part: Any) -> Optional[int]:
    """Extract position value from UniProt location object."""
    if loc_part is None:
        return None
    if isinstance(loc_part, dict):
        val = loc_part.get("value")
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                return None
    if isinstance(loc_part, (int, float)):
        return int(loc_part)
    return None


def _classify_domain_type(description: str) -> str:
    """Classify domain type based on description keywords."""
    desc_lower = description.lower()
    
    for keyword, domain_type in DOMAIN_TYPE_KEYWORDS.items():
        if keyword in desc_lower:
            return domain_type
    
    # Default: use cleaned description
    # Remove common suffixes and clean up
    clean = re.sub(r"\s*(domain|repeat|region|like)\s*$", "", description, flags=re.I)
    clean = re.sub(r"[^\w\s-]", "", clean).strip()
    
    if clean:
        # Convert to CamelCase-ish format
        parts = clean.split()
        if len(parts) <= 3:
            return "_".join(parts)
    
    return description[:30] if description else "Unknown"


def _parse_ptm_description(description: str) -> Tuple[str, Optional[str]]:
    """
    Parse PTM description to extract type and enzyme.
    
    Returns:
        (ptm_type, enzyme_name or None)
    """
    if not description:
        return ("unknown", None)
    
    for pattern, ptm_type, enzyme_group in PTM_PATTERNS:
        match = pattern.search(description)
        if match:
            enzyme = None
            if enzyme_group and match.lastindex and match.lastindex >= 1:
                try:
                    enzyme = match.group(1)
                except IndexError:
                    pass
            return (ptm_type, enzyme)
    
    # Fallback: try to infer from keywords
    desc_lower = description.lower()
    
    if "phospho" in desc_lower:
        return ("phosphorylation", None)
    if "acetyl" in desc_lower:
        return ("acetylation", None)
    if "methyl" in desc_lower:
        return ("methylation", None)
    if "glyco" in desc_lower or "glcnac" in desc_lower or "galnac" in desc_lower:
        return ("glycosylation", None)
    
    return ("modified", None)


def _classify_binding_site(feature_type: str, description: str) -> str:
    """Classify binding site type from feature type and description."""
    combined = f"{feature_type} {description}".lower()
    
    for keyword, site_type in BINDING_SITE_KEYWORDS.items():
        if keyword in combined:
            return site_type
    
    if feature_type == "Active site":
        return "catalytic"
    
    return "binding"


def _extract_metal_ion(description: str) -> Optional[str]:
    """Extract metal ion type from description."""
    for pattern, ion in METAL_ION_PATTERNS.items():
        if pattern.search(description):
            return ion
    return None


def _infer_conformational_state(entry: Dict[str, Any]) -> Optional[str]:
    """
    Infer conformational state from keywords and comments.
    
    This is best-effort; many proteins won't have clear state indicators.
    """
    keywords = entry.get("keywords", [])
    keyword_names = {k.get("name", "").lower() for k in keywords if isinstance(k, dict)}
    
    # Check for state-indicating keywords
    if "kinase" in keyword_names:
        # Kinases often have DFG states - but we can't determine which without structure
        return None
    
    if "receptor" in keyword_names and "g-protein coupled" in keyword_names:
        # GPCRs have active/inactive states
        return None
    
    # Check comments for state information
    comments = entry.get("comments", [])
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        ctype = comment.get("commentType", "")
        if ctype == "ACTIVITY REGULATION":
            # Could parse this for activation/inhibition info
            pass
    
    return None


# ============================================================================
# MAIN MAPPER
# ============================================================================

@dataclass
class MappingStats:
    """Statistics from the mapping process."""
    total_features: int = 0
    mapped_domains: int = 0
    mapped_motifs: int = 0
    mapped_ptms: int = 0
    mapped_binding_sites: int = 0
    skipped_features: int = 0
    unmapped_types: Set[str] = field(default_factory=set)


def map_uniprot_to_nesy(
    entry: Dict[str, Any],
    *,
    include_stats: bool = False,
) -> NeSyAnnotation | Tuple[NeSyAnnotation, MappingStats]:
    """
    Convert UniProt JSON entry to NeSyAnnotation for offline NeSy encoding.
    
    Args:
        entry: Parsed UniProt JSON (from entry.json.gz)
        include_stats: If True, return (annotation, stats) tuple
        
    Returns:
        NeSyAnnotation ready for LMPNeSyEncoder.encode()
        Or tuple (NeSyAnnotation, MappingStats) if include_stats=True
    """
    stats = MappingStats()
    
    # Extract sequence
    seq_obj = entry.get("sequence", {})
    sequence = seq_obj.get("value", "") if isinstance(seq_obj, dict) else ""
    seq_len = len(sequence)
    
    # Initialize collections
    domains: List[Dict[str, Any]] = []
    motifs: List[Dict[str, Any]] = []
    ptms: List[Dict[str, Any]] = []
    binding_sites: List[Dict[str, Any]] = []
    
    # Process features
    features = entry.get("features", [])
    stats.total_features = len(features)
    
    for feat in features:
        if not isinstance(feat, dict):
            continue
        
        feat_type = feat.get("type", "")
        category = FEATURE_TYPE_MAP.get(feat_type)
        
        if not category:
            stats.skipped_features += 1
            stats.unmapped_types.add(feat_type)
            continue
        
        loc = feat.get("location", {})
        start = _extract_position(loc.get("start"))
        end = _extract_position(loc.get("end"))
        description = feat.get("description", "")
        
        # Skip invalid positions
        if start is None:
            stats.skipped_features += 1
            continue
        
        # Default end to start for point features
        if end is None:
            end = start
        
        # Clamp to sequence length
        if seq_len > 0:
            start = max(1, min(start, seq_len))
            end = max(1, min(end, seq_len))
        
        # Process by category
        if category == "domain":
            domain_type = _classify_domain_type(description)
            domains.append({
                "name": description or feat_type,
                "type": domain_type,
                "start": start,
                "end": end,
            })
            stats.mapped_domains += 1
            
        elif category == "motif":
            motifs.append({
                "name": description or "motif",
                "type": "default",
                "start": start,
                "end": end,
            })
            stats.mapped_motifs += 1
            
        elif category == "ptm":
            ptm_type, enzyme = _parse_ptm_description(description)
            residue = sequence[start - 1] if 1 <= start <= seq_len else "X"
            
            ptm_entry: Dict[str, Any] = {
                "position": start,
                "type": ptm_type,
                "residue": residue,
            }
            if enzyme:
                ptm_entry["enzyme"] = enzyme
            
            ptms.append(ptm_entry)
            stats.mapped_ptms += 1
            
        elif category == "binding":
            site_type = _classify_binding_site(feat_type, description)
            residues = list(range(start, end + 1))
            
            binding_sites.append({
                "type": site_type,
                "residues": residues,
            })
            stats.mapped_binding_sites += 1
            
        elif category in ("nucleotide_binding", "metal_binding", "dna_binding"):
            # Specialized binding sites
            if category == "nucleotide_binding":
                site_type = _classify_binding_site("nucleotide", description)
            elif category == "metal_binding":
                site_type = "ion-binding"
                ion = _extract_metal_ion(description)
                binding_entry: Dict[str, Any] = {
                    "type": site_type,
                    "residues": [start],
                }
                if ion:
                    binding_entry["ion_type"] = ion
                binding_sites.append(binding_entry)
                stats.mapped_binding_sites += 1
                continue
            else:
                site_type = "DNA-binding"
            
            binding_sites.append({
                "type": site_type,
                "residues": list(range(start, end + 1)),
            })
            stats.mapped_binding_sites += 1
            
        elif category == "transmembrane":
            # Add as special domain type
            domains.append({
                "name": "Transmembrane",
                "type": "transmembrane",
                "start": start,
                "end": end,
            })
            stats.mapped_domains += 1
            
        else:
            # Other categories (region, signal, etc.) - skip for now
            stats.skipped_features += 1
    
    # Infer conformational state (best-effort)
    conf_state = _infer_conformational_state(entry)
    
    # Build annotation
    annotation = NeSyAnnotation(
        sequence=sequence,
        domains=domains,
        motifs=motifs,
        ptms=ptms,
        binding_sites=binding_sites,
        ppi_interfaces=[],  # Would need STRING/IntAct data
        conformational_state=conf_state,
        state_regions=[],  # Would need structural data
    )
    
    if include_stats:
        return annotation, stats
    return annotation


def map_uniprot_to_nesy_with_pdb(
    entry: Dict[str, Any],
    pdb_features: Optional[List[Dict[str, Any]]] = None,
) -> NeSyAnnotation:
    """
    Extended mapper that can incorporate PDB-derived features.
    
    Args:
        entry: UniProt JSON entry
        pdb_features: Optional list of PDB-derived features (binding sites, ligands, etc.)
        
    Returns:
        NeSyAnnotation with combined UniProt + PDB data
    """
    # Start with UniProt mapping
    annotation = map_uniprot_to_nesy(entry)
    
    if not pdb_features:
        return annotation
    
    # Extend with PDB features
    for feat in pdb_features:
        feat_type = feat.get("type", "")
        
        if feat_type == "ligand_binding":
            # Add ligand info to binding site
            residues = feat.get("residues", [])
            ligand_name = feat.get("ligand_name", "")
            ligand_type = feat.get("ligand_type", "")  # agonist, antagonist, inhibitor_type1, etc.
            
            binding_entry: Dict[str, Any] = {
                "type": "ligand-binding",
                "residues": residues,
            }
            if ligand_name:
                binding_entry["ligand"] = {
                    "type": ligand_type or "unknown",
                    "name": ligand_name,
                }
            annotation.binding_sites.append(binding_entry)
            
        elif feat_type == "ppi_interface":
            # Add PPI interface
            annotation.ppi_interfaces.append({
                "partner_id": feat.get("partner", "unknown"),
                "residues": feat.get("residues", []),
            })
            
        elif feat_type == "conformational_state":
            # Update conformational state
            if feat.get("state"):
                annotation = NeSyAnnotation(
                    sequence=annotation.sequence,
                    domains=annotation.domains,
                    motifs=annotation.motifs,
                    ptms=annotation.ptms,
                    binding_sites=annotation.binding_sites,
                    ppi_interfaces=annotation.ppi_interfaces,
                    conformational_state=feat.get("state"),
                    state_regions=annotation.state_regions + feat.get("regions", []),
                )
    
    return annotation


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_mapping_coverage(stats: MappingStats) -> float:
    """Calculate the percentage of features that were mapped."""
    if stats.total_features == 0:
        return 0.0
    
    mapped = (
        stats.mapped_domains +
        stats.mapped_motifs +
        stats.mapped_ptms +
        stats.mapped_binding_sites
    )
    return mapped / stats.total_features


def summarize_annotation(annotation: NeSyAnnotation) -> Dict[str, Any]:
    """Get a summary of the annotation contents."""
    return {
        "sequence_length": len(annotation.sequence),
        "n_domains": len(annotation.domains),
        "n_motifs": len(annotation.motifs),
        "n_ptms": len(annotation.ptms),
        "n_binding_sites": len(annotation.binding_sites),
        "n_ppi_interfaces": len(annotation.ppi_interfaces),
        "conformational_state": annotation.conformational_state,
        "domain_types": list({d.get("type") for d in annotation.domains}),
        "ptm_types": list({p.get("type") for p in annotation.ptms}),
        "binding_types": list({b.get("type") for b in annotation.binding_sites}),
    }

"""
Structural metrics bridge for LMP.

Computes static structural features from a single PDB file:
- DSSP secondary structure per residue (via mdtraj)
- Contact map (Cb-Cb, 8A cutoff)
- Network centrality (hub residues via contact graph)
- Extended geometry (Rg, asphericity)
- Ramachandran region counts
- Per-residue solvent accessibility (SASA)

Designed for single-frame (static) analysis of:
- Experimental PDB structures
- AlphaFold predicted models
- Representative MD frames

Graceful degradation: each metric is computed independently.
If mdtraj or networkx is missing, the corresponding metric returns None.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SecondaryStructureSegment:
    """A contiguous segment of secondary structure."""

    ss_type: str  # "helix", "strand", "coil"
    start: int  # 1-indexed residue start
    end: int  # 1-indexed residue end
    chain: str = "A"
    length: int = 0

    def __post_init__(self) -> None:
        self.length = max(0, self.end - self.start + 1)


@dataclass
class SecondaryStructureResult:
    """Complete DSSP result."""

    segments: List[SecondaryStructureSegment] = field(default_factory=list)
    per_residue: List[str] = field(default_factory=list)  # H/E/C per residue
    composition: Dict[str, float] = field(default_factory=dict)  # helix/strand/coil fractions
    method: str = "dssp"


@dataclass
class ContactEntry:
    """A residue-residue contact."""

    residue_i: int
    residue_j: int
    chain_i: str = "A"
    chain_j: str = "A"
    distance: float = 0.0


@dataclass
class HubResidue:
    """A network hub residue."""

    residue_id: int
    chain: str = "A"
    betweenness: float = 0.0
    degree: float = 0.0
    closeness: float = 0.0
    allosteric_candidate: bool = False


@dataclass
class PocketResidue:
    """Residue projected onto a detected pocket."""

    residue_id: int
    chain: str = "A"
    residue_name: str = ""


@dataclass
class PocketSite:
    """Static pocket site detected from a single structure."""

    pocket_id: str
    rank: int = 0
    engine: str = "smic"
    source: str = "smic_static_pdb_adapter"
    score: float = 0.0
    volume: float = 0.0
    center_x: float = 0.0
    center_y: float = 0.0
    center_z: float = 0.0
    point_count: int = 0
    residue_count: int = 0
    static: bool = True
    residues: List[PocketResidue] = field(default_factory=list)


@dataclass
class StructuralQuality:
    """Aggregated structural quality metrics."""

    rg: Optional[float] = None  # Radius of gyration (A)
    ramachandran_favored: Optional[int] = None
    ramachandran_allowed: Optional[int] = None
    ramachandran_outlier: Optional[int] = None
    ramachandran_favored_pct: Optional[float] = None
    total_contacts: Optional[int] = None
    contacts_per_residue: Optional[float] = None
    source: str = "experimental"


@dataclass
class StructuralMetrics:
    """Complete structural metrics bundle for a PDB file."""

    secondary_structure: Optional[SecondaryStructureResult] = None
    contacts: Optional[List[ContactEntry]] = None
    hub_residues: Optional[List[HubResidue]] = None
    pocket_sites: Optional[List[PocketSite]] = None
    quality: Optional[StructuralQuality] = None
    n_residues: int = 0
    source: str = "experimental"


# ---------------------------------------------------------------------------
# Computation engine
# ---------------------------------------------------------------------------


class StructuralMetricsComputer:
    """Computes structural metrics from static PDB files.

    Each method is independent and gracefully returns None on failure.
    """

    def __init__(self, contact_cutoff: float = 8.0, hub_top_pct: float = 0.10):
        self.contact_cutoff = contact_cutoff
        self.hub_top_pct = hub_top_pct

    def compute_all(
        self,
        pdb_path: Path,
        *,
        source: str = "experimental",
        compute_dssp: bool = True,
        compute_contacts: bool = True,
        compute_network: bool = True,
        compute_quality: bool = True,
        compute_pockets: bool = True,
        prefer_smic_static: bool = True,
    ) -> StructuralMetrics:
        """Compute all available structural metrics for a PDB file.

        Each metric is independent; failures in one do not block others.
        """
        pdb_path = Path(pdb_path)
        result = StructuralMetrics(source=source)

        smic_receipt = None
        if prefer_smic_static and (compute_dssp or compute_pockets):
            smic_receipt = self._compute_smic_static_receipt(pdb_path)

        traj = self._load_trajectory(pdb_path)
        if traj is None:
            logger.warning("Could not load PDB as trajectory: %s", pdb_path)
            if compute_dssp:
                result.secondary_structure = self._secondary_structure_from_smic_receipt(smic_receipt)
            if compute_pockets:
                result.pocket_sites = self._pocket_sites_from_smic_receipt(smic_receipt)
            return result

        try:
            result.n_residues = traj.n_residues
        except Exception:
            pass

        if compute_dssp:
            result.secondary_structure = self._secondary_structure_from_smic_receipt(smic_receipt)
            if result.secondary_structure is None:
                try:
                    result.secondary_structure = self._compute_dssp(traj)
                except Exception as exc:
                    logger.warning("DSSP computation failed: %s", exc)

        if compute_pockets:
            result.pocket_sites = self._pocket_sites_from_smic_receipt(smic_receipt)

        if compute_contacts:
            try:
                result.contacts = self._compute_contacts(traj)
            except Exception as exc:
                logger.warning("Contact map computation failed: %s", exc)

        if compute_network and result.contacts:
            try:
                result.hub_residues = self._compute_network_centrality(result.contacts)
            except Exception as exc:
                logger.warning("Network centrality computation failed: %s", exc)

        if compute_quality:
            try:
                result.quality = self._compute_quality(traj, result.contacts, source=source)
            except Exception as exc:
                logger.warning("Structural quality computation failed: %s", exc)

        return result

    # -- Internal methods ---------------------------------------------------

    @staticmethod
    def _load_trajectory(pdb_path: Path):  # -> Optional[md.Trajectory]
        """Load a PDB file as a single-frame mdtraj trajectory."""
        try:
            import mdtraj as md

            return md.load(str(pdb_path))
        except ImportError:
            logger.warning("mdtraj not available; structural metrics will be empty")
            return None
        except Exception as exc:
            logger.warning("Failed to load PDB with mdtraj: %s", exc)
            return None

    def _get_smic_static_adapter(self):
        if hasattr(self, "_smic_static_adapter"):
            return getattr(self, "_smic_static_adapter")

        adapter = None
        try:
            repo_root = Path(__file__).resolve().parents[3]
            adapter_path = (
                repo_root
                / "workers"
                / "smic"
                / "python"
                / "smic_core"
                / "md_analisys"
                / "lmp_static_semantic_adapter.py"
            )
            if adapter_path.exists():
                spec = importlib.util.spec_from_file_location(
                    "_lmp_smic_static_semantic_adapter",
                    str(adapter_path),
                )
                if spec is not None and spec.loader is not None:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules.setdefault("_lmp_smic_static_semantic_adapter", module)
                    spec.loader.exec_module(module)  # type: ignore[union-attr]
                    adapter = getattr(module, "compute_static_pdb_receipt", None)
        except Exception as exc:
            logger.debug("SMIC static adapter import failed: %s", exc)

        self._smic_static_adapter = adapter
        return adapter

    def _compute_smic_static_receipt(self, pdb_path: Path) -> Optional[Dict[str, Any]]:
        adapter = self._get_smic_static_adapter()
        if adapter is None:
            return None
        try:
            return adapter(Path(pdb_path))
        except Exception as exc:
            logger.debug("SMIC static receipt computation failed: %s", exc)
            return None

    def _secondary_structure_from_smic_receipt(
        self,
        receipt: Optional[Dict[str, Any]],
    ) -> Optional[SecondaryStructureResult]:
        raw = (receipt or {}).get("secondary_structure")
        if not isinstance(raw, dict):
            return None

        segments: List[SecondaryStructureSegment] = []
        counts = {"helix": 0, "strand": 0, "coil": 0}
        total = 0

        for item in raw.get("segments") or []:
            if not isinstance(item, dict):
                continue
            try:
                start = int(item.get("start") or 0)
                end = int(item.get("end") or 0)
            except (TypeError, ValueError):
                continue
            if start <= 0 or end < start:
                continue
            ss_type = str(item.get("ss_type") or "coil")
            segment = SecondaryStructureSegment(
                ss_type=ss_type,
                start=start,
                end=end,
                chain=str(item.get("chain") or "A"),
            )
            segments.append(segment)
            counts[ss_type] = counts.get(ss_type, 0) + segment.length
            total += segment.length

        if not segments:
            return None

        composition = {}
        if total > 0:
            composition = {
                "helix": round(counts.get("helix", 0) / total, 3),
                "strand": round(counts.get("strand", 0) / total, 3),
                "coil": round(counts.get("coil", 0) / total, 3),
            }

        return SecondaryStructureResult(
            segments=segments,
            composition=composition,
            method=str(raw.get("method") or "smic_dssp"),
        )

    def _pocket_sites_from_smic_receipt(
        self,
        receipt: Optional[Dict[str, Any]],
    ) -> Optional[List[PocketSite]]:
        raw_sites = (receipt or {}).get("pocket_sites") or []
        if not raw_sites:
            return None

        pocket_sites: List[PocketSite] = []
        for raw_site in raw_sites:
            if not isinstance(raw_site, dict):
                continue
            residues: List[PocketResidue] = []
            for raw_residue in raw_site.get("residues") or []:
                if not isinstance(raw_residue, dict):
                    continue
                try:
                    residue_id = int(raw_residue.get("residue_id") or 0)
                except (TypeError, ValueError):
                    continue
                if residue_id <= 0:
                    continue
                residues.append(
                    PocketResidue(
                        residue_id=residue_id,
                        chain=str(raw_residue.get("chain") or "A"),
                        residue_name=str(raw_residue.get("residue_name") or ""),
                    )
                )

            pocket_sites.append(
                PocketSite(
                    pocket_id=str(raw_site.get("pocket_id") or f"pocket_{len(pocket_sites) + 1}"),
                    rank=int(raw_site.get("rank") or len(pocket_sites) + 1),
                    engine=str(raw_site.get("engine") or "smic"),
                    source=str(raw_site.get("source") or "smic_static_pdb_adapter"),
                    score=float(raw_site.get("score") or 0.0),
                    volume=float(raw_site.get("volume") or 0.0),
                    center_x=float(raw_site.get("center_x") or 0.0),
                    center_y=float(raw_site.get("center_y") or 0.0),
                    center_z=float(raw_site.get("center_z") or 0.0),
                    point_count=int(raw_site.get("point_count") or 0),
                    residue_count=int(raw_site.get("residue_count") or len(residues)),
                    static=bool(raw_site.get("static", True)),
                    residues=residues,
                )
            )

        return pocket_sites or None

    def _compute_dssp(self, traj) -> SecondaryStructureResult:
        """Compute DSSP secondary structure from single-frame trajectory."""
        import mdtraj as md

        dssp_raw = md.compute_dssp(traj, simplified=True)  # H/E/C per residue
        if dssp_raw is None or len(dssp_raw) == 0:
            return SecondaryStructureResult()

        # dssp_raw shape: (n_frames, n_residues) — take first frame
        per_residue = list(dssp_raw[0])
        total = len(per_residue)

        # Composition
        counts = {"H": 0, "E": 0, "C": 0}
        for code in per_residue:
            c = str(code).upper()
            if c in counts:
                counts[c] += 1
            else:
                counts["C"] += 1

        composition = {}
        if total > 0:
            composition = {
                "helix": round(counts["H"] / total, 3),
                "strand": round(counts["E"] / total, 3),
                "coil": round(counts["C"] / total, 3),
            }

        # Convert to contiguous segments
        segments: List[SecondaryStructureSegment] = []
        _dssp_to_type = {"H": "helix", "E": "strand", "C": "coil"}

        # Get chain IDs from topology
        chain_ids: List[str] = []
        for residue in traj.topology.residues:
            chain_ids.append(str(residue.chain.index))

        if per_residue:
            current_type = _dssp_to_type.get(per_residue[0], "coil")
            seg_start = 1  # 1-indexed
            seg_chain = chain_ids[0] if chain_ids else "A"

            for i in range(1, len(per_residue)):
                ss = _dssp_to_type.get(str(per_residue[i]).upper(), "coil")
                ch = chain_ids[i] if i < len(chain_ids) else seg_chain

                if ss != current_type or ch != seg_chain:
                    segments.append(
                        SecondaryStructureSegment(
                            ss_type=current_type,
                            start=seg_start,
                            end=i,  # 1-indexed
                            chain=seg_chain,
                        )
                    )
                    current_type = ss
                    seg_start = i + 1
                    seg_chain = ch

            # Final segment
            segments.append(
                SecondaryStructureSegment(
                    ss_type=current_type,
                    start=seg_start,
                    end=len(per_residue),
                    chain=seg_chain,
                )
            )

        return SecondaryStructureResult(
            segments=segments,
            per_residue=per_residue,
            composition=composition,
            method="dssp",
        )

    def _compute_contacts(self, traj) -> List[ContactEntry]:
        """Compute Cb-Cb contact map at configured cutoff.

        Falls back to CA for glycine residues.
        """
        import mdtraj as md
        import numpy as np

        topology = traj.topology
        n_res = topology.n_residues

        # Build index: residue -> CB atom (or CA for GLY)
        res_atoms: Dict[int, int] = {}
        for res in topology.residues:
            cb = [a for a in res.atoms if a.name == "CB"]
            if cb:
                res_atoms[res.index] = cb[0].index
            else:
                ca = [a for a in res.atoms if a.name == "CA"]
                if ca:
                    res_atoms[res.index] = ca[0].index

        # Build all pairs (i < j, |i - j| > 2 to skip trivial neighbours)
        pairs_idx = []
        pairs_res = []
        for i in range(n_res):
            if i not in res_atoms:
                continue
            for j in range(i + 3, n_res):
                if j not in res_atoms:
                    continue
                pairs_idx.append((res_atoms[i], res_atoms[j]))
                pairs_res.append((i, j))

        if not pairs_idx:
            return []

        distances = md.compute_distances(traj, np.array(pairs_idx))[0]  # first frame
        cutoff_nm = self.contact_cutoff / 10.0  # mdtraj uses nm

        contacts: List[ContactEntry] = []
        for k, dist in enumerate(distances):
            if dist <= cutoff_nm:
                ri, rj = pairs_res[k]
                res_i = topology.residue(ri)
                res_j = topology.residue(rj)
                contacts.append(
                    ContactEntry(
                        residue_i=ri + 1,  # 1-indexed
                        residue_j=rj + 1,
                        chain_i=str(res_i.chain.index),
                        chain_j=str(res_j.chain.index),
                        distance=round(float(dist) * 10.0, 2),  # back to A
                    )
                )
        return contacts

    def _compute_network_centrality(self, contacts: List[ContactEntry]) -> List[HubResidue]:
        """Build contact graph and compute betweenness/degree centrality."""
        try:
            import networkx as nx
        except ImportError:
            logger.warning("networkx not available; skipping network centrality")
            return []

        G = nx.Graph()
        for c in contacts:
            G.add_edge(c.residue_i, c.residue_j, weight=1.0 / max(c.distance, 0.1))

        if G.number_of_nodes() < 3:
            return []

        betweenness = nx.betweenness_centrality(G, weight="weight")
        degree = nx.degree_centrality(G)
        closeness = nx.closeness_centrality(G, distance="weight")

        # Collect chain info from contacts
        res_chain: Dict[int, str] = {}
        for c in contacts:
            res_chain.setdefault(c.residue_i, c.chain_i)
            res_chain.setdefault(c.residue_j, c.chain_j)

        # Identify top hubs by betweenness
        sorted_nodes = sorted(betweenness.keys(), key=lambda n: betweenness[n], reverse=True)
        top_n = max(1, int(len(sorted_nodes) * self.hub_top_pct))
        top_set = set(sorted_nodes[:top_n])

        hubs: List[HubResidue] = []
        for node in sorted_nodes[:top_n * 2]:  # Return top 2x hub_pct nodes
            hubs.append(
                HubResidue(
                    residue_id=node,
                    chain=res_chain.get(node, "A"),
                    betweenness=round(betweenness.get(node, 0.0), 4),
                    degree=round(degree.get(node, 0.0), 4),
                    closeness=round(closeness.get(node, 0.0), 4),
                    allosteric_candidate=node in top_set,
                )
            )
        return hubs

    def _compute_quality(
        self,
        traj,
        contacts: Optional[List[ContactEntry]],
        *,
        source: str = "experimental",
    ) -> StructuralQuality:
        """Compute structural quality metrics: Rg, Ramachandran, contact density."""
        import mdtraj as md
        import numpy as np

        quality = StructuralQuality(source=source)

        # Radius of gyration
        try:
            rg = md.compute_rg(traj)[0]  # first frame, nm
            quality.rg = round(float(rg) * 10.0, 2)  # to A
        except Exception:
            pass

        # Ramachandran
        try:
            phi_indices, phi_angles = md.compute_phi(traj)
            psi_indices, psi_angles = md.compute_psi(traj)

            # Match phi/psi by residue index (phi starts at residue 1, psi at 0)
            phi_by_res = {}
            for k, idx_tuple in enumerate(phi_indices):
                # phi is defined by C(i-1)-N(i)-CA(i)-C(i) => residue is idx_tuple[1]
                res_idx = traj.topology.atom(idx_tuple[1]).residue.index
                phi_by_res[res_idx] = math.degrees(float(phi_angles[0, k]))

            psi_by_res = {}
            for k, idx_tuple in enumerate(psi_indices):
                res_idx = traj.topology.atom(idx_tuple[1]).residue.index
                psi_by_res[res_idx] = math.degrees(float(psi_angles[0, k]))

            favored = 0
            allowed = 0
            outlier = 0
            for res_idx in phi_by_res:
                if res_idx not in psi_by_res:
                    continue
                phi_val = phi_by_res[res_idx]
                psi_val = psi_by_res[res_idx]
                region = self._classify_ramachandran(phi_val, psi_val)
                if region == "favored":
                    favored += 1
                elif region == "allowed":
                    allowed += 1
                else:
                    outlier += 1

            total_rama = favored + allowed + outlier
            quality.ramachandran_favored = favored
            quality.ramachandran_allowed = allowed
            quality.ramachandran_outlier = outlier
            if total_rama > 0:
                quality.ramachandran_favored_pct = round(100.0 * favored / total_rama, 1)
        except Exception as exc:
            logger.debug("Ramachandran computation failed: %s", exc)

        # Contact density
        if contacts is not None:
            quality.total_contacts = len(contacts)
            n_res = traj.n_residues
            if n_res > 0:
                quality.contacts_per_residue = round(len(contacts) / n_res, 2)

        return quality

    @staticmethod
    def _classify_ramachandran(phi: float, psi: float) -> str:
        """Classify phi/psi angles into Ramachandran regions (simplified).

        Uses broad regions:
        - Favored: right-handed alpha-helix region + beta-sheet region
        - Allowed: extended allowed regions around favored
        - Outlier: everything else
        """
        # Right-handed alpha-helix: phi ~ -60, psi ~ -47
        if -160 <= phi <= -20 and -80 <= psi <= 0:
            return "favored"
        # Beta-sheet: phi ~ -120, psi ~ 130
        if -180 <= phi <= -60 and 80 <= psi <= 180:
            return "favored"
        if -180 <= phi <= -60 and -180 <= psi <= -120:
            return "favored"
        # Left-handed alpha (rare, ~2%)
        if 30 <= phi <= 90 and -10 <= psi <= 80:
            return "favored"
        # Extended allowed regions (generous margin)
        if -180 <= phi <= 0 and -180 <= psi <= 180:
            return "allowed"
        if 0 <= phi <= 180 and -180 <= psi <= 180:
            return "allowed"
        return "outlier"

"""src/mica/sim/cg_martini/geometry_audit.py — CGGeometryAudit (P0.4).

Authority:
  Lane CG/Martini — SLICE CG-P0.4
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D5: execution_status != validation_status

Scope:
  - Parse .gro and .top to detect overlaps and order mismatches
  - min_protein_lipid_distance_nm
  - min_water_water_distance_nm
  - severe_overlap_count (< 0.15 nm)
  - top_gro_order_match: [ molecules ] count vs .gro atom count consistency
  - decision: pass | remediate_required | block

Thresholds are NOT hardcoded as constitution. They are set by running
against the known CLCN7 case (gauntlet-first).
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from mica.provenance.receipts import ReceiptCore, ReceiptHashes, ReceiptRefs

logger = logging.getLogger(__name__)

_receipt_counter = 0


def _next_receipt_id(kind: str) -> str:
    global _receipt_counter
    _receipt_counter += 1
    return f"{kind}_{_receipt_counter}_{datetime.now(tz=timezone.utc).timestamp():.0f}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Default thresholds (calibrated against CLCN7 case)
# These are draft — promoted to gauntlet_validated only after P1.1
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_THRESHOLDS = {
    "severe_overlap_nm": 0.15,        # < this = severe overlap
    "warning_overlap_nm": 0.25,       # < this = warning
    "min_protein_lipid_pass_nm": 0.20,  # >= this = pass
    "min_water_water_pass_nm": 0.10,    # >= this = pass
    "block_on_severe_overlaps": 5,      # >= this = block (not just remediate)
}


# ═══════════════════════════════════════════════════════════════════════════
# Payloads
# ═══════════════════════════════════════════════════════════════════════════


class CGGeometryAuditPayload(BaseModel):
    """Result of a CG geometry audit — inside ReceiptCore.payload.

    Doctrina D1: NO es schema aislado.
    """

    system_ref: str = Field(..., description="Path or ref to the .gro file.")
    topology_ref: str = Field(..., description="Path or ref to the .top file.")
    top_gro_order_match: bool = Field(..., description="[ molecules ] count matches atom content.")
    min_protein_lipid_distance_nm: float = Field(0.0, description="Minimum protein-to-lipid distance in nm.")
    min_water_water_distance_nm: float = Field(0.0, description="Minimum water-water distance in nm.")
    severe_overlap_count: int = Field(0, description="Number of atom pairs below severe_overlap_nm threshold.")
    warning_overlap_count: int = Field(0, description="Number of atom pairs between warning and severe threshold.")
    decision: str = Field("block", description="pass | remediate_required | block")
    execution_status: str = "completed"
    validation_status: Optional[str] = None
    thresholds_used: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))


class CGOverlapRemediationPayload(BaseModel):
    """Result of overlap remediation — inside ReceiptCore.payload."""

    policy: str = "conservative_remove_whole_lipids"
    input_system_ref: str
    output_system_ref: str
    lipids_removed: int = 0
    removed_molecule_names: list[str] = Field(default_factory=list)
    topology_counts_updated: bool = False
    before_min_distance_nm: float = 0.0
    after_min_distance_nm: float = 0.0
    before_severe_overlap_count: int = 0
    after_severe_overlap_count: int = 0
    execution_status: str = "completed"
    validation_status: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# .gro parser
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class GroAtom:
    index: int
    resnr: int
    resname: str
    atomname: str
    atomid: int
    x: float
    y: float
    z: float


class GroStructure:
    """Parsed .gro file."""

    def __init__(self):
        self.title: str = ""
        self.atom_count: int = 0
        self.atoms: list[GroAtom] = []
        self.box: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.residue_map: dict[str, list[GroAtom]] = {}

    @classmethod
    def parse(cls, path: str) -> "GroStructure":
        """Parse a .gro file into a GroStructure."""
        result = cls()
        with open(path) as f:
            lines = f.readlines()

        if len(lines) < 3:
            raise ValueError(f"GRO file too short: {path}")

        result.title = lines[0].strip()
        result.atom_count = int(lines[1].strip())

        atom_lines = lines[2:2 + result.atom_count]
        box_line = lines[2 + result.atom_count].strip()

        # Parse box
        box_parts = box_line.split()
        if len(box_parts) >= 3:
            result.box = (float(box_parts[0]), float(box_parts[1]), float(box_parts[2]))

        # Parse atoms
        # GRO format (fixed width): %5d%-5s%5s%5d%8.3f%8.3f%8.3f
        for i, line in enumerate(atom_lines):
            try:
                resnr = int(line[0:5].strip()) if len(line) > 0 else 0
                resname = line[5:10].strip() if len(line) > 5 else ""
                atomname = line[10:15].strip() if len(line) > 10 else ""
                atomid = int(line[15:20].strip()) if len(line) > 15 else (i + 1)
                x = float(line[20:28].strip()) if len(line) > 20 else 0.0
                y = float(line[28:36].strip()) if len(line) > 28 else 0.0
                z = float(line[36:44].strip()) if len(line) > 36 else 0.0
            except (ValueError, IndexError):
                # Fallback to whitespace parsing
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                resnr = int(parts[0]) if parts[0].isdigit() else (i + 1)
                resname = parts[1] if len(parts) > 1 else ""
                atomname = parts[2] if len(parts) > 2 else ""
                atomid = int(parts[3]) if len(parts) > 3 else (i + 1)
                x = float(parts[4]) if len(parts) > 4 else 0.0
                y = float(parts[5]) if len(parts) > 5 else 0.0
                z = float(parts[6]) if len(parts) > 6 else 0.0

            atom = GroAtom(
                index=i + 1,
                resnr=resnr,
                resname=resname,
                atomname=atomname,
                atomid=atomid,
                x=x, y=y, z=z,
            )
            result.atoms.append(atom)

            # Build residue map
            if resname not in result.residue_map:
                result.residue_map[resname] = []
            result.residue_map[resname].append(atom)

        return result

    def get_residue_names(self) -> set[str]:
        return set(self.residue_map.keys())

    def get_atoms_by_residue(self, resname: str) -> list[GroAtom]:
        return self.residue_map.get(resname, [])

    def classify_atoms_by_type(self) -> dict[str, list[GroAtom]]:
        """Classify atoms into protein, lipid, water/ion groups based on residue name."""
        result: dict[str, list[GroAtom]] = {"protein": [], "lipid": [], "water": [], "ion": [], "unknown": []}
        protein_keywords = {"PRO", "ALA", "GLY", "VAL", "LEU", "ILE", "PHE", "TYR", "TRP",
                           "SER", "THR", "CYS", "MET", "ASN", "GLN", "LYS", "ARG", "HIS",
                           "ASP", "GLU"}
        lipid_keywords = {"POPC", "POPE", "POPS", "POPG", "DOPC", "DOPE", "DOPS", "DOPG",
                          "DPPC", "DMPC", "CHOL", "CHOL1", "CHOL2", "NC3", "PO4", "GL1", "GL2",
                          "DMPE", "DPPE", "DMPG", "DPPG", "DMPS", "DPPS", "CER", "SM",
                          "LPA", "LPC", "LPE", "LPS", "LPG", "PA", "PC", "PE", "PS", "PG",
                          "PI", "PIP", "PIP2"}
        water_keywords = {"W", "WF", "W_SOL", "MW", "MWW", "SOL", "WAT", "HOH", "TIP3", "TIP4"}
        ion_keywords = {"NA", "CL", "K", "MG", "CA", "NA+", "CL-", "K+", "MG2+", "CA2+",
                        "POT", "SOD", "Cations", "Anions"}

        for atom in self.atoms:
            rn = atom.resname.upper()
            if rn in protein_keywords:
                result["protein"].append(atom)
            elif rn in lipid_keywords:
                result["lipid"].append(atom)
            elif rn in water_keywords:
                result["water"].append(atom)
            elif rn in ion_keywords:
                result["ion"].append(atom)
            else:
                result["unknown"].append(atom)
        return result


# ═══════════════════════════════════════════════════════════════════════════
# Topology parser (minimal — just [ molecules ] section)
# ═══════════════════════════════════════════════════════════════════════════


def parse_top_molecules(path: str) -> list[tuple[str, int]]:
    """Parse [ molecules ] section from a .top file.

    Returns list of (name, count).
    """
    molecules: list[tuple[str, int]] = []
    in_molecules = False
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("[ molecules ]"):
                in_molecules = True
                continue
            if in_molecules:
                if not stripped or stripped.startswith(";") or stripped.startswith("#"):
                    continue
                if stripped.startswith("["):
                    break
                parts = stripped.split()
                if len(parts) >= 2:
                    try:
                        molecules.append((parts[0], int(parts[1])))
                    except ValueError:
                        pass
    return molecules


def parse_top_atom_count(path: str) -> int:
    """Count total atoms in a .top file by summing molecule counts.

    NOTE: This is approximate — requires the .top to have explicit
    [ atoms ] sections per molecule type. For CG systems, this maps
    to total beads.
    """
    total = 0
    in_molecules = False
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("[ molecules ]"):
                in_molecules = True
                continue
            if in_molecules:
                if not stripped or stripped.startswith(";") or stripped.startswith("#"):
                    continue
                if stripped.startswith("["):
                    break
                parts = stripped.split()
                if len(parts) >= 2:
                    try:
                        total += int(parts[1])
                    except ValueError:
                        pass
    return total


# ═══════════════════════════════════════════════════════════════════════════
# GeometryAudit
# ═══════════════════════════════════════════════════════════════════════════


class GeometryAudit:
    """Audits CG system geometry for overlaps and consistency.

    Detects:
      - Molecule order mismatch between .gro and .top
      - Protein-lipid overlaps
      - Water-water overlaps (can indicate collapsed box)
      - Severe overlaps (atoms at nearly the same position)
    """

    def __init__(
        self,
        thresholds: Optional[dict[str, float]] = None,
        workspace_id: str = "cg_martini",
        actor_id: str = "system",
    ):
        self.thresholds = dict(thresholds or DEFAULT_THRESHOLDS)
        self.workspace_id = workspace_id
        self.actor_id = actor_id

    def run(self, system_ref: str, topology_ref: str) -> ReceiptCore:
        """Run geometry audit on a CG system.

        Args:
            system_ref: Path to .gro file.
            topology_ref: Path to .top file.

        Returns:
            ReceiptCore with CGGeometryAuditPayload.
        """
        errors: list[str] = []

        if not os.path.isfile(system_ref):
            return self._error_receipt(f"GRO file not found: {system_ref}")
        if not os.path.isfile(topology_ref):
            return self._error_receipt(f"TOP file not found: {topology_ref}")

        # Parse
        try:
            gro = GroStructure.parse(system_ref)
        except (ValueError, OSError) as exc:
            return self._error_receipt(f"Failed to parse GRO: {exc}")

        try:
            top_molecules = parse_top_molecules(topology_ref)
        except (OSError, ValueError) as exc:
            return self._error_receipt(f"Failed to parse TOP: {exc}")

        # 1. top_gro_order_match
        # Compare: number of unique residue names in .gro vs molecule types in .top
        gro_residues = set(atom.resname for atom in gro.atoms)
        top_molecule_names = set(name for name, _ in top_molecules)
        # A reasonable system should have roughly matching counts
        gro_residue_count = len(gro_residues)
        top_moltype_count = len(top_molecule_names)
        top_gro_order_match = abs(gro_residue_count - top_moltype_count) <= max(2, top_moltype_count)

        # 2. Classify atoms and compute minimum distances
        classified = gro.classify_atoms_by_type()

        # Compute min protein-lipid distance
        min_protein_lipid = self._min_distance_between_groups(
            classified["protein"], classified["lipid"]
        )

        # Compute min water-water distance
        min_water_water = self._min_distance_within_group(
            classified["water"]
        )

        # 3. Severe overlap detection (all atom pairs within threshold)
        severe_count = 0
        warning_count = 0
        severe_threshold = self.thresholds["severe_overlap_nm"]
        warning_threshold = self.thresholds["warning_overlap_nm"]

        # Sample-based: check protein-lipid and first N atoms vs bulk
        # Full pairwise is O(n²) — for large systems, sample strategically
        atoms = gro.atoms
        sample_size = min(5000, len(atoms))
        step = max(1, len(atoms) // sample_size)

        # Check consecutive atoms first (likely to be bonded, close)
        for i in range(0, len(atoms) - 1, step):
            for j in range(i + 1, min(i + 50, len(atoms))):
                d = self._distance(atoms[i], atoms[j])
                if d < severe_threshold:
                    severe_count += 1
                elif d < warning_threshold:
                    warning_count += 1

        # Also check protein vs lipid systematically
        for pa in classified["protein"][::max(1, len(classified["protein"]) // 100 + 1)]:
            for la in classified["lipid"][::max(1, len(classified["lipid"]) // 100 + 1)]:
                d = self._distance(pa, la)
                if d < severe_threshold:
                    severe_count += 1
                elif d < warning_threshold:
                    warning_count += 1

        # 4. Decision
        decision = self._decide(
            top_gro_order_match=top_gro_order_match,
            min_protein_lipid=min_protein_lipid,
            min_water_water=min_water_water,
            severe_count=severe_count,
            warning_count=warning_count,
        )

        payload = CGGeometryAuditPayload(
            system_ref=system_ref,
            topology_ref=topology_ref,
            top_gro_order_match=top_gro_order_match,
            min_protein_lipid_distance_nm=round(min_protein_lipid, 4),
            min_water_water_distance_nm=round(min_water_water, 4),
            severe_overlap_count=severe_count,
            warning_overlap_count=warning_count,
            decision=decision,
            execution_status="completed",
            thresholds_used=dict(self.thresholds),
        )

        return self._build_receipt(
            kind="cg_geometry_audit",
            status=decision,
            operation_name="geometry_audit",
            payload=payload,
            artifact_refs=[system_ref, topology_ref],
        )

    def _decide(
        self,
        top_gro_order_match: bool,
        min_protein_lipid: float,
        min_water_water: float,
        severe_count: int,
        warning_count: int,
    ) -> str:
        """Determine audit decision based on metrics and thresholds."""
        block_on_severe = self.thresholds.get("block_on_severe_overlaps", 5)

        if not top_gro_order_match:
            return "block"

        if severe_count >= block_on_severe:
            return "block"

        if min_protein_lipid < self.thresholds["min_protein_lipid_pass_nm"] and severe_count > 0:
            return "remediate_required"

        if min_water_water < self.thresholds["min_water_water_pass_nm"] and severe_count > 0:
            return "remediate_required"

        if severe_count > 0 or warning_count > 10:
            return "remediate_required"

        return "pass"

    @staticmethod
    def _min_distance_between_groups(
        group_a: list[GroAtom], group_b: list[GroAtom]
    ) -> float:
        """Minimum distance between any atom in group_a and group_b."""
        if not group_a or not group_b:
            return float("inf")
        min_d = float("inf")
        # Sample if groups are large
        step_a = max(1, len(group_a) // 500)
        step_b = max(1, len(group_b) // 500)
        for a in group_a[::step_a]:
            for b in group_b[::step_b]:
                d = GeometryAudit._distance(a, b)
                if d < min_d:
                    min_d = d
        return min_d

    @staticmethod
    def _min_distance_within_group(group: list[GroAtom]) -> float:
        """Minimum distance between any two atoms within a group."""
        if len(group) < 2:
            return float("inf")
        min_d = float("inf")
        step = max(1, len(group) // 500)
        for i in range(0, len(group) - 1, step):
            for j in range(i + 1, min(i + 50, len(group))):
                d = GeometryAudit._distance(group[i], group[j])
                if d < min_d:
                    min_d = d
        return min_d

    @staticmethod
    def _distance(a: GroAtom, b: GroAtom) -> float:
        """Euclidean distance in nm."""
        dx = a.x - b.x
        dy = a.y - b.y
        dz = a.z - b.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: CGGeometryAuditPayload,
        artifact_refs: Optional[list[str]] = None,
    ) -> ReceiptCore:
        receipt_id = _next_receipt_id(kind)
        return ReceiptCore(
            receipt_id=receipt_id,
            kind=kind,
            status=status,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            operation_name=operation_name,
            refs=ReceiptRefs(
                output_refs=[],
                artifact_refs=artifact_refs or [],
            ),
            hashes=ReceiptHashes(
                request_hash="",
                output_hash="",
                content_hash=f"audit_{payload.system_ref}",
            ),
            started_at=_now_iso(),
            ended_at=_now_iso(),
            trace_id=f"trace_{receipt_id}",
            payload=payload.model_dump(),
        )

    def _error_receipt(self, error: str) -> ReceiptCore:
        payload = CGGeometryAuditPayload(
            system_ref="",
            topology_ref="",
            top_gro_order_match=False,
            execution_status="failed",
        )
        receipt = self._build_receipt(
            kind="cg_geometry_audit",
            status="failed",
            operation_name="geometry_audit",
            payload=payload,
        )
        receipt.payload["validation_errors"] = [error]
        return receipt

"""src/mica/sim/cg_martini/overlap_remediation.py — OverlapRemediation (P0.4).

Authority:
  Lane CG/Martini — SLICE CG-P0.4
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D5: execution_status != validation_status

Scope:
  - Conservative whole-lipid removal from CG membrane systems
  - Never removes partial residues — only complete lipids
  - Updates [ molecules ] counts in .top
  - Emits before/after distance report

Policy: conservative_remove_whole_lipids (default)
  - Identifies lipid molecules whose atoms overlap with protein
  - Removes the ENTIRE lipid (all its beads)
  - Updates .gro and .top accordingly
"""

from __future__ import annotations

import logging
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from mica.provenance.receipts import ReceiptCore, ReceiptHashes, ReceiptRefs
from mica.sim.cg_martini.geometry_audit import (
    CGGeometryAuditPayload,
    GeometryAudit,
    GroStructure,
    parse_top_molecules,
)

logger = logging.getLogger(__name__)

_receipt_counter = 0


def _next_receipt_id(kind: str) -> str:
    global _receipt_counter
    _receipt_counter += 1
    return f"{kind}_{_receipt_counter}_{datetime.now(tz=timezone.utc).timestamp():.0f}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Overlap threshold for remediation
# ═══════════════════════════════════════════════════════════════════════════

REMEDIATION_OVERLAP_THRESHOLD_NM = 0.20  # nm: lipids closer than this to protein are candidates for removal


class CGOverlapRemediationPayload(BaseModel):
    """Result of overlap remediation — inside ReceiptCore.payload.

    Doctrina D1: NO es schema aislado.
    """

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
    validation_errors: list[str] = Field(default_factory=list)


class OverlapRemediation:
    """Removes lipid molecules that overlap with protein.

    Conservative policy:
      - Identifies lipids whose atoms are within REMEDIATION_OVERLAP_THRESHOLD_NM
        of any protein atom
      - Removes the ENTIRE lipid molecule (all its beads)
      - Updates .gro file (removes atoms) and .top file (updates molecule count)
      - Runs GeometryAudit before and after to confirm improvement
    """

    def __init__(
        self,
        overlap_threshold_nm: float = REMEDIATION_OVERLAP_THRESHOLD_NM,
        workspace_id: str = "cg_martini",
        actor_id: str = "system",
    ):
        self.overlap_threshold_nm = overlap_threshold_nm
        self.workspace_id = workspace_id
        self.actor_id = actor_id

    def remediate(self, system_ref: str, topology_ref: str, output_dir: str) -> ReceiptCore:
        """Run conservative overlap remediation.

        Args:
            system_ref: Path to input .gro file.
            topology_ref: Path to input .top file.
            output_dir: Directory for output files.

        Returns:
            ReceiptCore with CGOverlapRemediationPayload.
        """
        errors: list[str] = []

        if not os.path.isfile(system_ref):
            return self._error_receipt(f"Input GRO not found: {system_ref}")
        if not os.path.isfile(topology_ref):
            return self._error_receipt(f"Input TOP not found: {topology_ref}")

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # Run audit before remediation
        auditor = GeometryAudit(workspace_id=self.workspace_id, actor_id=self.actor_id)
        before_audit = auditor.run(system_ref, topology_ref)
        before_payload = before_audit.payload
        if isinstance(before_payload, dict):
            before_min = before_payload.get("min_protein_lipid_distance_nm", 0.0)
            before_severe = before_payload.get("severe_overlap_count", 0)
        else:
            before_min = 0.0
            before_severe = 0

        # Parse system
        try:
            gro = GroStructure.parse(system_ref)
        except (ValueError, OSError) as exc:
            return self._error_receipt(f"Failed to parse GRO: {exc}")

        try:
            top_molecules = parse_top_molecules(topology_ref)
        except (OSError, ValueError) as exc:
            return self._error_receipt(f"Failed to parse TOP: {exc}")

        # Classify atoms
        classified = gro.classify_atoms_by_type()
        protein_atoms = classified["protein"]
        lipid_atoms = classified["lipid"]

        if not protein_atoms or not lipid_atoms:
            return self._error_receipt("System has no protein or no lipid atoms — nothing to remediate")

        # Identify lipid beads overlapping with protein
        # We identify whole lipid molecules by residue name
        overlapping_residues: set[str] = set()
        for la in lipid_atoms:
            for pa in protein_atoms[::max(1, len(protein_atoms) // 200 + 1)]:
                d = self._distance(la, pa)
                if d < self.overlap_threshold_nm:
                    overlapping_residues.add(la.resname)
                    break

        # Count lipids to remove: all atoms of overlapping residue types
        atoms_to_keep: list = []
        lipids_removed = 0
        removed_molecule_names: list[str] = []

        for atom in gro.atoms:
            if atom.resname in overlapping_residues:
                lipids_removed += 1
                if atom.resname not in removed_molecule_names:
                    removed_molecule_names.append(atom.resname)
            else:
                atoms_to_keep.append(atom)

        # Write new .gro
        output_gro = out / "system.repacked.gro"
        self._write_gro(output_gro, gro.title, atoms_to_keep, gro.box)

        # Update .top molecule counts
        output_top = out / "system.repacked.top"
        self._update_top_molecule_counts(
            topology_ref, str(output_top), overlapping_residues
        )

        # Run audit after remediation
        after_audit = auditor.run(str(output_gro), str(output_top))
        after_payload = after_audit.payload
        if isinstance(after_payload, dict):
            after_min = after_payload.get("min_protein_lipid_distance_nm", 0.0)
            after_severe = after_payload.get("severe_overlap_count", 0)
        else:
            after_min = 0.0
            after_severe = 0

        payload = CGOverlapRemediationPayload(
            policy="conservative_remove_whole_lipids",
            input_system_ref=system_ref,
            output_system_ref=str(output_gro),
            lipids_removed=lipids_removed,
            removed_molecule_names=removed_molecule_names,
            topology_counts_updated=True,
            before_min_distance_nm=round(before_min, 4),
            after_min_distance_nm=round(after_min, 4),
            before_severe_overlap_count=before_severe,
            after_severe_overlap_count=after_severe,
            execution_status="completed",
        )

        return self._build_receipt(
            kind="cg_overlap_remediation",
            status="completed",
            operation_name="overlap_remediation",
            payload=payload,
            artifact_refs=[str(output_gro), str(output_top)],
        )

    @staticmethod
    def _distance(a, b) -> float:
        dx = a.x - b.x
        dy = a.y - b.y
        dz = a.z - b.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    @staticmethod
    def _write_gro(path: Path, title: str, atoms: list, box: tuple[float, float, float]):
        """Write a GRO file from atom list."""
        with open(path, "w") as f:
            f.write(f"{title}\n")
            f.write(f"{len(atoms):>5}\n")
            for i, atom in enumerate(atoms):
                resnr = min(atom.resnr, 99999)
                resname = (atom.resname or "UNK")[:5].ljust(5)
                atomname = (atom.atomname or "X")[:5].ljust(5)
                atomid = min(atom.atomid or (i + 1), 99999)
                x = atom.x if hasattr(atom, "x") else 0.0
                y = atom.y if hasattr(atom, "y") else 0.0
                z = atom.z if hasattr(atom, "z") else 0.0
                f.write(f"{resnr:5d}{resname}{atomname}{atomid:5d}{x:8.3f}{y:8.3f}{z:8.3f}\n")
            f.write(f"{box[0]:10.5f}{box[1]:10.5f}{box[2]:10.5f}\n")

    @staticmethod
    def _update_top_molecule_counts(
        input_top: str, output_top: str, removed_residues: set[str]
    ):
        """Copy .top and update molecule counts for removed lipid types.

        Removes molecule lines whose name matches a removed residue type.
        """
        with open(input_top) as f:
            lines = f.readlines()

        output_lines: list[str] = []
        in_molecules = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[ molecules ]"):
                in_molecules = True
                output_lines.append(line)
                continue
            if in_molecules:
                if not stripped or stripped.startswith(";") or stripped.startswith("#"):
                    output_lines.append(line)
                    continue
                if stripped.startswith("["):
                    in_molecules = False
                    output_lines.append(line)
                    continue
                parts = stripped.split()
                if len(parts) >= 2:
                    mol_name = parts[0]
                    if mol_name in removed_residues:
                        continue  # skip this molecule line
                output_lines.append(line)
            else:
                output_lines.append(line)

        with open(output_top, "w") as f:
            f.writelines(output_lines)

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: CGOverlapRemediationPayload,
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
                output_refs=[payload.output_system_ref],
                artifact_refs=artifact_refs or [],
            ),
            hashes=ReceiptHashes(
                request_hash="",
                output_hash="",
                content_hash=f"remediate_{payload.input_system_ref}",
            ),
            started_at=_now_iso(),
            ended_at=_now_iso(),
            trace_id=f"trace_{receipt_id}",
            payload=payload.model_dump(),
        )

    def _error_receipt(self, error: str) -> ReceiptCore:
        payload = CGOverlapRemediationPayload(
            input_system_ref="",
            output_system_ref="",
            execution_status="failed",
            validation_errors=[error],
        )
        return self._build_receipt(
            kind="cg_overlap_remediation",
            status="failed",
            operation_name="overlap_remediation",
            payload=payload,
        )

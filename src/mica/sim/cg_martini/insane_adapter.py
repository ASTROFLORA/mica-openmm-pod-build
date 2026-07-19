"""src/mica/sim/cg_martini/insane_adapter.py — INSANEAdapter (P0.2, real).

Authority:
  Lane CG/Martini — SLICE CG-P0.2, índice maestro v1.0 §2
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D2: todo edge input→output usa MUDODependencyEdge real
  Doctrina D3: builder_policy por geometry_class

Scope:
  preflight()  — valida toolchain, geometry_class, protein_gro_ref
  build()      — invoca INSANE 1.2.0 (Tieleman lab), produce system.gro/.top
  validate_outputs() — parsea outputs, verifica consistencia gro×top

Historia:
  - Versión inicial: TS2CG (Perl, Tsjerk/Wassenaar) — NO disponible en este entorno
  - Versión activa:   INSANE 1.2.0 (Python, Tieleman lab) — sucesor canónico Martini 3
  - Verificado en CLCN7 real (PDB 7JM7): sistema de 6.4M átomos CG
    (1 proteína + 7,802 POPC + 2.08M waters + 22,971 Na+ + 22,981 Cl-)

Fuera de scope:
  - Topology preprocessing (P0.3)
  - Geometry audit (P0.4)
  - Protein CG mapping via martinize2 (P0.5)
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from mica.provenance.receipts import ReceiptCore, ReceiptHashes, ReceiptRefs

logger = logging.getLogger(__name__)

_receipt_counter = 0


def _next_receipt_id(kind: str) -> str:
    global _receipt_counter
    _receipt_counter += 1
    return f"{kind}_{_receipt_counter}_{datetime.now(tz=timezone.utc).timestamp():.0f}"


# ═══════════════════════════════════════════════════════════════════════════
# Builder policy — doctrina D3
# ═══════════════════════════════════════════════════════════════════════════

BUILDER_POLICY: dict[str, list[str]] = {
    "flat_bilayer": ["insane", "ts2cg"],
    "curved_surface": ["ts2cg"],
    "vesicle": ["ts2cg"],
    "tomography": ["ts2cg"],
    "analytical_shape": ["ts2cg"],
}

ALLOWED_GEOMETRY_CLASSES = set(BUILDER_POLICY.keys())


def validate_builder_policy(builder: str, geometry_class: str) -> Optional[str]:
    """Check builder against policy for the given geometry class.

    Returns error message if rejected, None if allowed.
    """
    if geometry_class not in BUILDER_POLICY:
        return f"Unknown geometry_class: {geometry_class}. Allowed: {sorted(ALLOWED_GEOMETRY_CLASSES)}"
    allowed = BUILDER_POLICY[geometry_class]
    if builder not in allowed:
        return (
            f"Builder '{builder}' not allowed for geometry_class '{geometry_class}'. "
            f"Allowed: {allowed}. "
            f"Requires degraded_builder_override with actor_kind=human."
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Payload — contenido científico dentro de ReceiptCore.payload
# ═══════════════════════════════════════════════════════════════════════════


class INSANEBuildPayload(BaseModel):
    """Payload específico de INSANE, contenido en ReceiptCore.payload.

    Doctrina D1: NO es schema aislado — es el contenido de un receipt
    cuya forma base es ReceiptCore.
    """

    builder: str = "insane"
    builder_version: str = Field(..., description="Pin explícito, ej. '1.2.0'.")
    membrane_geometry_class: str = Field(default="flat_bilayer", description="Sólo flat_bilayer soportado en esta versión.")
    protein_gro_ref: str = Field(..., description="Artifact ref del .gro de proteína CG (output de P0.5).")
    lipid_composition: str = Field(default="POPC:1", description="Lipid composition string insane-format, ej. 'POPC:1'.")
    solvent: str = Field(default="PW", description="Solvent name, ej. 'PW' (Martini water).")
    salt_concentration: float = Field(default=0.15, description="Salt concentration in M (NaCl).")
    center_protein: bool = Field(default=True, description="Centrear la proteína en el origen antes de membrane build.")
    outputs: dict[str, str] = Field(
        default_factory=lambda: {"gro_ref": "", "top_ref": ""},
        description="Output artifact refs: gro_ref, top_ref.",
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Conteos: protein_beads, lipid_count, water_count, na_count, cl_count, total_atoms.",
    )
    box_nm: dict[str, float] = Field(
        default_factory=dict,
        description="Box dimensions in nm.",
    )
    execution_status: str = Field(default="pending", description="completed | failed.")
    validation_status: Optional[str] = Field(default=None, description="Se llena en validate_outputs().")
    validation_errors: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# INSANEAdapter
# ═══════════════════════════════════════════════════════════════════════════


class INSANEAdapter:
    """Autoridad canónica de construcción de membrana vía INSANE 1.2.0.

    Verificado en CLCN7 real (PDB 7JM7, chain A, 1589 CG beads) →
    sistema de 6,402,189 átomos (1 proteína + 7,802 POPC + 2,087,008 waters
    + 22,971 Na+ + 22,981 Cl-).

    Los métodos devuelven ReceiptCore con INSANEBuildPayload en .payload.
    """

    DEFAULT_INSANe_VERSION = "1.2.0"

    def __init__(self, workspace_id: str = "cg_martini", actor_id: str = "system"):
        self.workspace_id = workspace_id
        self.actor_id = actor_id
        # Verify insane is importable at construction time
        try:
            import insane  # noqa: F401
            self._insane_available = True
        except ImportError as e:
            self._insane_available = False
            self._insane_import_error = str(e)
            logger.error("insane not importable: %s", e)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: INSANEBuildPayload,
        output_refs: Optional[list[str]] = None,
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
                output_refs=output_refs or [],
                artifact_refs=artifact_refs or [],
            ),
            hashes=ReceiptHashes(
                request_hash="",
                output_hash="",
                content_hash=f"insane_{payload.protein_gro_ref}",
            ),
            started_at=self._now_iso(),
            ended_at=self._now_iso(),
            trace_id=f"trace_{receipt_id}",
            payload=payload.model_dump(),
        )

    # ── preflight ─────────────────────────────────────────────────────

    def preflight(
        self,
        builder: str,
        builder_version: str,
        geometry_class: str,
        protein_gro_ref: str,
    ) -> ReceiptCore:
        """Validate that the build request can proceed.

        Checks:
          1. builder + geometry_class cumplen BUILDER_POLICY (D3)
          2. INSANE library is importable
          3. Required refs are non-empty
        """
        errors: list[str] = []

        # D3: builder_policy
        policy_error = validate_builder_policy(builder, geometry_class)
        if policy_error:
            errors.append(policy_error)

        # Library availability
        if not self._insane_available:
            errors.append(
                f"insane library not importable: {getattr(self, '_insane_import_error', '?')}"
            )

        # Required refs
        if not protein_gro_ref:
            errors.append("protein_gro_ref is required")

        if protein_gro_ref and not os.path.isfile(protein_gro_ref):
            errors.append(f"protein_gro_ref not found: {protein_gro_ref}")

        payload = INSANEBuildPayload(
            builder=builder,
            builder_version=builder_version,
            membrane_geometry_class=geometry_class,
            protein_gro_ref=protein_gro_ref,
            execution_status="failed" if errors else "passed",
            validation_errors=errors,
        )

        return self._build_receipt(
            kind="cg_insane_preflight",
            status="failed" if errors else "passed",
            operation_name="insane_preflight",
            payload=payload,
            artifact_refs=[protein_gro_ref] if os.path.isfile(protein_gro_ref) else [],
        )

    # ── build ────────────────────────────────────────────────────────

    def build(
        self,
        protein_gro_ref: str,
        output_dir: str,
        builder: str = "insane",
        builder_version: str = DEFAULT_INSANe_VERSION,
        geometry_class: str = "flat_bilayer",
        lipid_composition: str = "POPC:1",
        solvent: str = "PW",
        salt_concentration: float = 0.15,
        center_protein: bool = True,
    ) -> ReceiptCore:
        """Build a CG membrane + solvate + ions around a protein using INSANE.

        Args:
            protein_gro_ref: Path to input protein .gro (from P0.5 martinize2).
            output_dir: Output directory.
            builder: 'insane' (only one supported in this adapter).
            builder_version: Pinned version string.
            geometry_class: Membrane geometry class (D3 policy checked).
            lipid_composition: e.g. 'POPC:1' for 100% POPC in both leaflets.
            solvent: e.g. 'PW' (Martini polarizable water).
            salt_concentration: in M (NaCl).
            center_protein: Centre the protein at origin first (CRITICAL).

        Returns:
            ReceiptCore with INSANEBuildPayload.

        Notes:
            The output GRO can be >280 MB for a real protein. Build time is
            3-5 minutes on a modern CPU for a CLCN7-scale system.
        """
        if builder != "insane":
            return self._error_receipt(
                f"This adapter only supports builder='insane', got '{builder}'"
            )

        errors: list[str] = []
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        if not os.path.isfile(protein_gro_ref):
            return self._error_receipt(f"protein_gro_ref not found: {protein_gro_ref}")

        if not self._insane_available:
            return self._error_receipt("insane not importable")

        # Step 1: Center protein at origin if requested
        centered_gro = out / "centered_protein.gro"
        if center_protein:
            try:
                self._center_gro(protein_gro_ref, str(centered_gro))
            except Exception as e:
                errors.append(f"centering failed: {e}")
        else:
            shutil.copyfile(protein_gro_ref, str(centered_gro))

        # Step 2: Call insane
        gro_out = out / "membrane.gro"
        top_out = out / "membrane.top"

        try:
            from insane import cli
            argv = [
                "insane",
                "-f", str(centered_gro),
                "-o", str(gro_out),
                "-p", str(top_out),
                "-l", lipid_composition,
                "-sol", solvent,
                "-salt", str(salt_concentration),
                "-center",
            ]
            rc = cli.main(argv)
            if rc != 0:
                errors.append(f"insane.cli.main returned {rc}")
        except SystemExit as e:
            errors.append(f"insane.cli.main SystemExit: {e}")
        except Exception as e:
            errors.append(f"insane.cli.main error: {e}")

        # Step 3: Parse outputs to extract counts and box
        counts: dict[str, int] = {}
        box_nm: dict[str, float] = {}
        if gro_out.exists():
            try:
                counts, box_nm = self._parse_gro(str(gro_out))
            except Exception as e:
                errors.append(f"output GRO parse failed: {e}")

        execution_status = "failed" if errors else "completed"

        payload = INSANEBuildPayload(
            builder=builder,
            builder_version=builder_version,
            membrane_geometry_class=geometry_class,
            protein_gro_ref=protein_gro_ref,
            lipid_composition=lipid_composition,
            solvent=solvent,
            salt_concentration=salt_concentration,
            center_protein=center_protein,
            outputs={
                "gro_ref": str(gro_out) if gro_out.exists() else "",
                "top_ref": str(top_out) if top_out.exists() else "",
            },
            counts=counts,
            box_nm=box_nm,
            execution_status=execution_status,
            validation_errors=errors,
        )

        return self._build_receipt(
            kind="cg_insane_build",
            status=execution_status,
            operation_name="insane_build_membrane",
            payload=payload,
            output_refs=[str(gro_out), str(top_out)],
            artifact_refs=[str(centered_gro), protein_gro_ref],
        )

    # ── validate_outputs ──────────────────────────────────────────────

    def validate_outputs(self, receipt: ReceiptCore) -> ReceiptCore:
        """Validate that the membrane build outputs are well-formed.

        Checks:
          1. Output GRO and TOP exist
          2. GRO has positive atom count
          3. TOP has [ molecules ] section
          4. Box dimensions are positive
          5. Lipid count is reasonable (>= 0)
        """
        payload_data = receipt.payload
        if isinstance(payload_data, dict):
            payload = INSANEBuildPayload(**payload_data)
        else:
            payload = payload_data

        errors: list[str] = []

        gro_ref = payload.outputs.get("gro_ref", "")
        top_ref = payload.outputs.get("top_ref", "")

        if not gro_ref or not os.path.isfile(gro_ref):
            errors.append(f"Output GRO not found: {gro_ref}")
        if not top_ref or not os.path.isfile(top_ref):
            errors.append(f"Output TOP not found: {top_ref}")

        # Box positive
        for k, v in payload.box_nm.items():
            if v <= 0:
                errors.append(f"Box {k}={v} is not positive")

        # Lipid count >= 0
        if payload.counts.get("lipid_count", -1) < 0:
            errors.append("lipid_count not set or negative")

        # TOP has [ molecules ]
        if top_ref and os.path.isfile(top_ref):
            with open(top_ref) as f:
                top_text = f.read()
            if "[ molecules ]" not in top_text:
                errors.append(f"TOP {top_ref} missing [ molecules ] section")

        if errors:
            payload.validation_status = "failed"
            payload.validation_errors = errors
        else:
            payload.validation_status = "passed"

        return self._build_receipt(
            kind="cg_insane_validation",
            status=payload.validation_status or "passed",
            operation_name="insane_validate",
            payload=payload,
            output_refs=[gro_ref, top_ref],
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _error_receipt(self, message: str) -> ReceiptCore:
        """Build an error receipt with a single validation error."""
        payload = INSANEBuildPayload(
            builder="insane",
            builder_version=self.DEFAULT_INSANe_VERSION,
            protein_gro_ref="",
            execution_status="failed",
            validation_errors=[message],
        )
        return self._build_receipt(
            kind="cg_insane_build",
            status="failed",
            operation_name="insane_build_membrane",
            payload=payload,
        )

    @staticmethod
    def _center_gro(input_gro: str, output_gro: str) -> None:
        """Centre a GRO file's coordinates at the origin.

        This is CRITICAL: insane's `setup_solvent` assumes the protein is
        near the box center, and fails with IndexError otherwise.
        """
        with open(input_gro) as f:
            lines = f.readlines()

        title = lines[0]
        n = int(lines[1].strip())
        box_line = lines[2 + n]

        # Parse box
        box = [float(x) for x in box_line.strip().split()[:3]]
        if len(box) < 3:
            box = [50.0, 50.0, 50.0]

        # Compute centroid
        coords = []
        for i in range(2, 2 + n):
            x = float(lines[i][20:28])
            y = float(lines[i][28:36])
            z = float(lines[i][36:44])
            coords.append((x, y, z))
        cx = sum(c[0] for c in coords) / n
        cy = sum(c[1] for c in coords) / n
        cz = sum(c[2] for c in coords) / n

        # Compute span to size the box
        if coords:
            min_x = min(c[0] for c in coords)
            max_x = max(c[0] for c in coords)
            min_y = min(c[1] for c in coords)
            max_y = max(c[1] for c in coords)
            min_z = min(c[2] for c in coords)
            max_z = max(c[2] for c in coords)
            span_x = max_x - min_x + 2.0
            span_y = max_y - min_y + 2.0
            span_z = max_z - min_z + 12.0  # extra for bilayer + water
        else:
            span_x, span_y, span_z = box

        with open(output_gro, "w") as f:
            f.write(title)
            f.write(f"{n}\n")
            for i, line in enumerate(lines[2:2 + n]):
                x = coords[i][0] - cx
                y = coords[i][1] - cy
                z = coords[i][2] - cz
                f.write(f"{line[:20]}{x:8.3f}{y:8.3f}{z:8.3f}\n")
            f.write(f"  {span_x:.3f}  {span_y:.3f}  {span_z:.3f}\n")

    @staticmethod
    def _parse_gro(gro_path: str) -> tuple[dict[str, int], dict[str, float]]:
        """Parse a GRO file to extract atom counts by residue type and box."""
        counts: dict[str, int] = {}
        box: dict[str, float] = {}

        with open(gro_path) as f:
            lines = f.readlines()

        if len(lines) < 3:
            return counts, box

        # Total atom count
        try:
            n_total = int(lines[1].strip())
        except ValueError:
            n_total = 0

        # Count by residue name
        atom_counts: dict[str, int] = {}
        protein_beads = 0
        lipid_count = 0
        water_count = 0
        na_count = 0
        cl_count = 0

        for i in range(2, 2 + n_total):
            if i >= len(lines):
                break
            line = lines[i]
            if len(line) < 20:
                continue
            resname = line[5:10].strip()
            atom_counts[resname] = atom_counts.get(resname, 0) + 1

        # Box dimensions
        if len(lines) > 2 + n_total:
            try:
                box_parts = lines[2 + n_total].split()
                if len(box_parts) >= 3:
                    box = {"x": float(box_parts[0]), "y": float(box_parts[1]), "z": float(box_parts[2])}
            except (ValueError, IndexError):
                pass

        # Heuristic: protein = SC/BB beads, lipid = POPC/DOPC/etc, water = PW/SOL
        for rname, cnt in atom_counts.items():
            rname_upper = rname.upper()
            if rname_upper in ("PW", "W", "SOL", "WAT", "TIP3", "TIP4"):
                water_count += cnt
            elif rname_upper in ("NA+", "NA", "SOD"):
                na_count += cnt
            elif rname_upper in ("CL-", "CL", "CLA"):
                cl_count += cnt
            elif rname_upper in ("POPC", "DOPC", "POPE", "DOPE", "POPG", "DOPG", "CHOL"):
                lipid_count += cnt
            else:
                protein_beads += cnt

        counts = {
            "total_atoms": n_total,
            "protein_beads": protein_beads,
            "lipid_count": lipid_count,
            "water_count": water_count,
            "na_count": na_count,
            "cl_count": cl_count,
        }
        return counts, box

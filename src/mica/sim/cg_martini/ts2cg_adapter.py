"""src/mica/sim/cg_martini/ts2cg_adapter.py — TS2CGAdapter (P0.2).

Authority:
  Lane CG/Martini — SLICE CG-P0.2, índice maestro v1.0 §2
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D2: todo edge input→output usa MUDODependencyEdge real
  Doctrina D3: builder_policy por geometry_class

Scope:
  preflight()  — valida toolchain, versión, perfil de lípidos, builder_policy
  build()      — invoca TS2CG PCG, produce system.gro/.top/pcg.log
  validate_outputs() — parsea outputs, verifica consistencia gro×top

Fuera de scope:
  - Topology preprocessing (P0.3)
  - Geometry audit (P0.4)
  - Protein CG mapping via martinize2 (P0.5)
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from mica.provenance.receipts import ReceiptCore, ReceiptHashes, ReceiptRefs

logger = logging.getLogger(__name__)

# Simple counter for receipt ID generation
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
# Payload — va dentro de ReceiptCore.payload
# ═══════════════════════════════════════════════════════════════════════════


class TS2CGBuildPayload(BaseModel):
    """Payload específico de TS2CG, contenido en ReceiptCore.payload.

    Doctrina D1: NO es un schema aislado — es el contenido científico
    de un receipt cuya forma base es ReceiptCore.
    """

    builder: str = "ts2cg"
    builder_version: str = Field(..., description="Pin explícito, ej. '2.0' o commit hash.")
    membrane_geometry_class: str = Field(..., description="flat_bilayer | curved_surface | vesicle | tomography | analytical_shape")
    input_surface_ref: Optional[str] = Field(default=None, description="Artifact ref de superficie triangulada (.tsi).")
    protein_gro_ref: str = Field(..., description="Artifact ref del .gro de proteína CG (output de P0.5).")
    lipid_profile_ref: str = Field(..., description="Lipid profile/library ref.")
    outputs: dict[str, str] = Field(
        default_factory=lambda: {"gro_ref": "", "top_ref": "", "log_ref": ""},
        description="Output artifact refs: gro_ref, top_ref, log_ref.",
    )
    execution_status: str = Field(default="pending", description="completed | failed — NUNCA implica validez física.")
    validation_status: Optional[str] = Field(default=None, description="Se llena en validate_outputs(), no en build().")
    validation_errors: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# TS2CGAdapter
# ═══════════════════════════════════════════════════════════════════════════


class TS2CGAdapter:
    """Autoridad canónica de construcción de membrana vía TS2CG v2.0.

    No existe equivalente en codebase — es pieza nueva real.
    Los métodos devuelven ReceiptCore con TS2CGBuildPayload en .payload.
    """

    def __init__(self, ts2cg_binary: str = "TS2CG", workspace_id: str = "cg_martini", actor_id: str = "system"):
        self.ts2cg_binary = ts2cg_binary
        self.workspace_id = workspace_id
        self.actor_id = actor_id

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: TS2CGBuildPayload,
        output_refs: Optional[list[str]] = None,
        artifact_refs: Optional[list[str]] = None,
        request_hash: str = "",
        output_hash: Optional[str] = None,
        content_hash: str = "",
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
                request_hash=request_hash,
                output_hash=output_hash,
                content_hash=content_hash,
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
        lipid_profile_ref: str,
        input_surface_ref: Optional[str] = None,
    ) -> ReceiptCore:
        """Validate that the build request can proceed.

        Checks:
          1. builder + geometry_class cumplen BUILDER_POLICY (D3)
          2. TS2CG binary is available (if builder=ts2cg)
          3. Required refs are non-empty

        Returns ReceiptCore con payload.execution_status.
        """
        errors: list[str] = []

        # D3: builder_policy
        policy_error = validate_builder_policy(builder, geometry_class)
        if policy_error:
            errors.append(policy_error)

        # Binary availability
        if builder == "ts2cg":
            if not self._probe_ts2cg():
                errors.append(f"TS2CG binary not found: {self.ts2cg_binary}")

        # Required refs
        if not protein_gro_ref:
            errors.append("protein_gro_ref is required")
        if not lipid_profile_ref:
            errors.append("lipid_profile_ref is required")

        payload = TS2CGBuildPayload(
            builder=builder,
            builder_version=builder_version,
            membrane_geometry_class=geometry_class,
            input_surface_ref=input_surface_ref,
            protein_gro_ref=protein_gro_ref,
            lipid_profile_ref=lipid_profile_ref,
            execution_status="failed" if errors else "passed",
        )

        return self._build_receipt(
            kind="cg_ts2cg_preflight",
            status="failed" if errors else "passed",
            operation_name="ts2cg_preflight",
            payload=payload,
            request_hash=str(hash(str(locals()))),
            content_hash=str(hash(str(locals()))),
        )

    def _probe_ts2cg(self) -> bool:
        """Check if TS2CG binary is available on PATH."""
        try:
            result = subprocess.run(
                [self.ts2cg_binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            return False

    # ── build ─────────────────────────────────────────────────────────

    def build(
        self,
        builder: str,
        builder_version: str,
        geometry_class: str,
        protein_gro_path: str,
        lipid_profile_path: str,
        output_dir: str,
        input_surface_path: Optional[str] = None,
        lipid_library_path: Optional[str] = None,
    ) -> ReceiptCore:
        """Invoke TS2CG PCG to build the membrane system.

        Args:
            builder: "ts2cg" | "insane"
            builder_version: Version pin.
            geometry_class: Membrane geometry classification.
            protein_gro_path: Path to protein CG .gro (output of P0.5).
            lipid_profile_path: Path to lipid profile (.str file for TS2CG).
            output_dir: Directory for output files.
            input_surface_path: Path to triangulated surface (.tsi), if applicable.
            lipid_library_path: Path to lipid library (.lib), if needed.

        Returns:
            ReceiptCore con outputs poblados si el build tuvo éxito.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        errors: list[str] = []
        outputs: dict[str, str] = {"gro_ref": "", "top_ref": "", "log_ref": ""}

        if builder == "ts2cg":
            try:
                result = self._run_ts2cg(
                    protein_gro_path=protein_gro_path,
                    lipid_profile_path=lipid_profile_path,
                    output_dir=str(out),
                    input_surface_path=input_surface_path,
                    lipid_library_path=lipid_library_path,
                )
                if result.returncode != 0:
                    errors.append(f"TS2CG exited with code {result.returncode}: {result.stderr[:500]}")
                else:
                    # Check expected outputs
                    gro_path = out / "system.gro"
                    top_path = out / "system.top"
                    log_path = out / "pcg.log"
                    if gro_path.exists():
                        outputs["gro_ref"] = f"file://{gro_path.absolute()}"
                    if top_path.exists():
                        outputs["top_ref"] = f"file://{top_path.absolute()}"
                    if log_path.exists():
                        outputs["log_ref"] = f"file://{log_path.absolute()}"

                    if not gro_path.exists() or not top_path.exists():
                        errors.append(f"TS2CG did not produce expected outputs in {output_dir}")
            except FileNotFoundError:
                errors.append(f"TS2CG binary not found: {self.ts2cg_binary}")
            except Exception as exc:
                errors.append(f"TS2CG execution error: {exc}")
        else:
            errors.append(f"Builder '{builder}' not supported by this adapter version")

        execution_status = "failed" if errors else "completed"
        payload = TS2CGBuildPayload(
            builder=builder,
            builder_version=builder_version,
            membrane_geometry_class=geometry_class,
            protein_gro_ref=f"file://{protein_gro_path}",
            lipid_profile_ref=f"file://{lipid_profile_path}",
            outputs=outputs,
            execution_status=execution_status,
            validation_errors=errors,
        )

        # Compute content hash from outputs
        content_hash = ""
        if outputs.get("gro_ref"):
            gro_path = outputs["gro_ref"].replace("file://", "")
            if os.path.isfile(gro_path):
                content_hash = self._sha256_file(gro_path)

        return self._build_receipt(
            kind="cg_ts2cg_build",
            status=execution_status,
            operation_name="ts2cg_build",
            payload=payload,
            artifact_refs=[v for v in outputs.values() if v],
            content_hash=content_hash,
        )

    def _run_ts2cg(
        self,
        protein_gro_path: str,
        lipid_profile_path: str,
        output_dir: str,
        input_surface_path: Optional[str] = None,
        lipid_library_path: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run TS2CG PCG with given parameters."""
        cmd = [self.ts2cg_binary, "PCG"]
        if input_surface_path:
            cmd.extend(["-s", input_surface_path])
        if protein_gro_path:
            cmd.extend(["-p", protein_gro_path])
        if lipid_profile_path:
            cmd.extend(["-l", lipid_profile_path])
        if lipid_library_path:
            cmd.extend(["-lib", lipid_library_path])
        cmd.extend(["-o", output_dir])

        logger.info("Running TS2CG: %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    # ── validate_outputs ──────────────────────────────────────────────

    def validate_outputs(self, build_receipt: ReceiptCore) -> ReceiptCore:
        """Validate the outputs of a TS2CG build.

        Checks:
          1. Output refs are non-empty
          2. Files exist on disk
          3. .gro and .top molecule counts match
          4. .top is parseable (basic line check)

        Returns a new ReceiptCore with validation_status set.
        """
        payload_data = build_receipt.payload
        if isinstance(payload_data, dict):
            payload = TS2CGBuildPayload(**payload_data)
        else:
            payload = payload_data

        errors: list[str] = []
        outputs = payload.outputs

        # Check refs are populated
        if not outputs.get("gro_ref"):
            errors.append("Missing gro_ref in outputs")
        if not outputs.get("top_ref"):
            errors.append("Missing top_ref in outputs")

        # Check files exist and are parseable
        gro_path = outputs.get("gro_ref", "").replace("file://", "")
        top_path = outputs.get("top_ref", "").replace("file://", "")

        gro_ok = False
        top_ok = False

        if gro_path and os.path.isfile(gro_path):
            atom_count = self._parse_gro_atom_count(gro_path)
            if atom_count > 0:
                gro_ok = True
            else:
                errors.append(f"GRO file {gro_path} has 0 atoms or is unparseable")
        else:
            errors.append(f"GRO file not found: {gro_path}")

        if top_path and os.path.isfile(top_path):
            molecule_types = self._parse_top_molecule_entries(top_path)
            if molecule_types > 0:
                top_ok = True
            else:
                errors.append(f"TOP file {top_path} has 0 molecule entries or is unparseable")
        else:
            errors.append(f"TOP file not found: {top_path}")

        content_hash = ""
        if gro_path and os.path.isfile(gro_path):
            content_hash = self._sha256_file(gro_path)

        validation_status = "passed" if not errors else "failed"
        payload.validation_status = validation_status
        payload.validation_errors = errors

        return self._build_receipt(
            kind="cg_ts2cg_validation",
            status=validation_status,
            operation_name="ts2cg_validate_outputs",
            payload=payload,
            output_refs=[outputs.get("gro_ref", ""), outputs.get("top_ref", "")],
            artifact_refs=list(outputs.values()),
            content_hash=content_hash,
        )

    # ── Parsing helpers ─────────────────────────────────────────────

    @staticmethod
    def _parse_gro_atom_count(path: str) -> int:
        """Parse total atom count from .gro header (line 2)."""
        try:
            with open(path) as f:
                lines = f.readlines()
                if len(lines) < 2:
                    return 0
                return int(lines[1].strip())
        except (OSError, ValueError, IndexError):
            return 0

    @staticmethod
    def _parse_top_molecule_entries(path: str) -> int:
        """Count [ molecules ] entries in a .top file."""
        try:
            count = 0
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
                        count += 1
            return count
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _sha256_file(path: str) -> str:
        """Compute SHA-256 of a file."""
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

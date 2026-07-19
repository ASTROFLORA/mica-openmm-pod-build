"""src/mica/sim/cg_martini/martinize2_adapter.py — Martinize2Adapter (P0.5).

Authority:
  Lane CG/Martini — SLICE CG-P0.5
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D5: execution_status != validation_status

Scope:
  - Invoke martinize2 with pinned parameters (version, martini version,
    secondary_structure_policy, elastic_network_policy)
  - Produce protein CG (.gro + .itp)
  - Validate: bead count vs residue count, secondary structure assigned

Fuera de scope:
  - Membrane building (P0.2) — this adapter produces input for it
  - Advanced EN/Gō-Martini policy — P2+
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
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


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Default pinned versions
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_MARTINIZE2_VERSION = "2023-12"  # placeholder — pinned after first run
DEFAULT_MARTINI_VERSION = "3001"  # martini3001 in martinize2 naming


# ═══════════════════════════════════════════════════════════════════════════
# Payload
# ═══════════════════════════════════════════════════════════════════════════


class Martinize2Payload(BaseModel):
    """Payload específico de martinize2, dentro de ReceiptCore.payload.

    Doctrina D1: NO es schema aislado.
    """

    martinize2_version: str = Field(default=DEFAULT_MARTINIZE2_VERSION, description="Pin explícito de martinize2.")
    martini_version: str = Field(default=DEFAULT_MARTINI_VERSION, description="Versión de Martini, ej. '3.0.0'.")
    input_structure_ref: str = Field(..., description="Path al .pdb/.gro de estructura AA de entrada.")
    secondary_structure_policy: str = Field(
        default="dssp",
        description="'dssp' (auto) | 'provided' (desde PDB) | 'custom'.",
    )
    elastic_network_policy: str = Field(
        default="elnedyn",
        description="'elnedyn' | 'go_martini' | 'none'. ElNeDyn es default recomendado.",
    )
    output_cg_gro_ref: str = Field(default="", description="Path al .gro CG de salida.")
    output_cg_itp_ref: str = Field(default="", description="Path al .itp de topología CG de salida.")
    residue_count_input: int = Field(0, description="N° de residuos en la estructura de entrada.")
    bead_count_output: int = Field(0, description="N° de beads CG en la estructura de salida.")
    execution_status: str = "pending"
    validation_status: Optional[str] = None
    validation_errors: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Martinize2Adapter
# ═══════════════════════════════════════════════════════════════════════════


class Martinize2Adapter:
    """Adapter canónico para mapeo de proteína AA→CG vía martinize2.

    Ya existe evidencia de uso real (CLCN7, OSR1) pero no como módulo
    de producto con receipt/contrato. Este slice formaliza eso.
    """

    def __init__(
        self,
        martinize2_binary: Optional[str] = None,
        martinize2_version: str = DEFAULT_MARTINIZE2_VERSION,
        martini_version: str = DEFAULT_MARTINI_VERSION,
        workspace_id: str = "cg_martini",
        actor_id: str = "system",
    ):
        # Resolution order for the martinize2 binary path:
        #   1. Explicit constructor argument
        #   2. MICA_MARTINIZE2_BINARY env var (Railway / container override)
        #   3. shutil.which("martinize2") (PATH lookup, picks up the script
        #      installed by the vermouth git+https wheel into /usr/local/bin)
        #   4. Fallback to the bare name "martinize2" (last resort; relies on
        #      subprocess PATH discovery at call time)
        if martinize2_binary:
            resolved = martinize2_binary
        else:
            env_override = os.environ.get("MICA_MARTINIZE2_BINARY")
            if env_override:
                resolved = env_override
            else:
                which_path = shutil.which("martinize2")
                resolved = which_path if which_path else "martinize2"
        # If the resolved path is relative, try to absolute-ify it for stability
        # on Windows where subprocess can be flaky with relative paths.
        if not os.path.isabs(resolved) and os.sep in resolved:
            abs_candidate = os.path.abspath(resolved)
            if os.path.isfile(abs_candidate):
                resolved = abs_candidate
        self.martinize2_binary = resolved
        self.martinize2_version = martinize2_version
        self.martini_version = martini_version
        self.workspace_id = workspace_id
        self.actor_id = actor_id

    # ── preflight ─────────────────────────────────────────────────────

    def preflight(
        self,
        input_structure_ref: str,
        ss_policy: str = "dssp",
        en_policy: str = "elnedyn",
    ) -> ReceiptCore:
        """Validate that martinize2 mapping can proceed.

        Checks:
          1. Input file exists
          2. martinize2 binary is available
          3. Secondary structure policy is known
          4. Elastic network policy is known

        Returns ReceiptCore with execution_status.
        """
        errors: list[str] = []

        if not os.path.isfile(input_structure_ref):
            errors.append(f"Input structure not found: {input_structure_ref}")

        if not self._probe_martinize2():
            errors.append(f"martinize2 binary not found: {self.martinize2_binary}")

        valid_ss = {"dssp", "provided", "custom"}
        if ss_policy not in valid_ss:
            errors.append(f"Unknown secondary_structure_policy: {ss_policy}. Valid: {valid_ss}")

        valid_en = {"elnedyn", "go_martini", "none"}
        if en_policy not in valid_en:
            errors.append(f"Unknown elastic_network_policy: {en_policy}. Valid: {valid_en}")

        payload = Martinize2Payload(
            martinize2_version=self.martinize2_version,
            martini_version=self.martini_version,
            input_structure_ref=input_structure_ref,
            secondary_structure_policy=ss_policy,
            elastic_network_policy=en_policy,
            execution_status="failed" if errors else "passed",
            validation_errors=errors,
        )

        return self._build_receipt(
            kind="cg_martinize2_preflight",
            status="failed" if errors else "passed",
            operation_name="martinize2_preflight",
            payload=payload,
            artifact_refs=[input_structure_ref] if os.path.isfile(input_structure_ref) else [],
        )

    def _probe_martinize2(self) -> bool:
        """Check if martinize2 binary is available.

        Resolves the binary through Python when the path points to a
        .py script (the canonical vermouth installation writes
        vermouth/bin/martinize2.py without a shebang on Windows, and
        subprocess cannot launch a .py file directly there).
        """
        binary = self.martinize2_binary
        if not os.path.isabs(binary):
            binary_abs = os.path.abspath(binary)
            if os.path.isfile(binary_abs):
                binary = binary_abs
        # If binary is a .py file (martinize2.py from vermouth), launch
        # it through the current Python interpreter.
        if binary.endswith(".py"):
            cmd = [sys.executable, binary, "--version"]
        else:
            cmd = [binary, "--version"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            return False

    # ── map_protein ───────────────────────────────────────────────────

    def map_protein(
        self,
        input_structure_ref: str,
        output_dir: str,
        ss_policy: str = "dssp",
        en_policy: str = "elnedyn",
        custom_ss_file: Optional[str] = None,
        maxwarn: int = 100,
    ) -> ReceiptCore:
        """Run martinize2 to map an AA structure to CG.

        Args:
            input_structure_ref: Path to input .pdb/.gro (AA structure).
            output_dir: Output directory for .gro and .itp files.
            ss_policy: Secondary structure policy.
            en_policy: Elastic network policy.
            custom_ss_file: Custom .ssd file (required if ss_policy='custom').

        Returns:
            ReceiptCore with Martinize2Payload.
        """
        errors: list[str] = []
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        if not os.path.isfile(input_structure_ref):
            return self._error_receipt(f"Input structure not found: {input_structure_ref}")

        # Count input residues (quick estimate from PDB ATOM lines)
        residue_count_input = self._count_residues(input_structure_ref)

        # Determine output paths
        # martinize2 -o writes TOPOLOGY (with #include statements)
        # martinize2 -x writes COORDINATES (ATOM records)
        # Extension is NOT used to determine content - martinize2 ignores it.
        # Per-molecule topology (molecule_0.itp, molecule_1.itp, ...) is
        # also emitted by martinize2; we discover those dynamically after
        # the run and expose them as output_cg_molecule_itp_refs.
        base = Path(input_structure_ref).stem
        output_top_file = out / f"{base}_cg.top"    # -o target → contains topology (#include martini.itp, molecule_X refs)
        output_coord_file = out / f"{base}_cg.pdb"  # -x target → contains coordinates (ATOM records)

        # Build command
        # martinize2 ignores file extension; -o writes topology (with
        # #include statements), -x writes coordinates (ATOM records).
        # DSSP: do not pass -dssp (None) → mdtraj DSSP is used automatically.
        # Do not pass -ss either; it forces a manual sequence.
        # If the binary is a .py script (the canonical vermouth install
        # writes vermouth/bin/martinize2.py without a Windows-friendly
        # shebang), launch it through the current Python interpreter.
        if self.martinize2_binary.endswith(".py"):
            cmd = [
                sys.executable, self.martinize2_binary,
                "-f", input_structure_ref,
                "-o", str(output_top_file),
                "-x", str(output_coord_file),
                "-ff", f"martini{self.martini_version}",
            ]
        else:
            cmd = [
                self.martinize2_binary,
                "-f", input_structure_ref,
                "-o", str(output_top_file),
                "-x", str(output_coord_file),
                "-ff", f"martini{self.martini_version}",
            ]

        if ss_policy == "custom" and custom_ss_file:
            cmd.extend(["-ss", custom_ss_file])

        if en_policy == "elnedyn":
            cmd.append("-elastic")
        elif en_policy == "go_martini":
            cmd.extend(["-elastic", "-go"])

        # Always emit per-molecule topology files. martinize2 emits
        # ``molecule_0.itp``, ``molecule_1.itp``, ... to its CWD
        # when ``-sep`` is given. We pass ``-sep`` so the downstream
        # ``cg_system_builder`` has per-chain .itp files to include in
        # ``[ molecules ]`` of the consolidated ``system.top``.
        cmd.append("-sep")

        # Bypass martinize2's internal warning cap so we capture the full run
        if maxwarn > 0:
            cmd.extend(["-maxwarn", str(maxwarn)])

        # Run with cwd=output_dir so the per-molecule .itp files land
        # in the output dir (martinize2 writes them to CWD), not in
        # the repo root.
        try:
            logger.info("Running martinize2: %s", " ".join(cmd))
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, cwd=str(out),
            )
            if result.returncode != 0:
                errors.append(f"martinize2 exited with code {result.returncode}: {result.stderr[:5000]}")
        except FileNotFoundError:
            errors.append(f"martinize2 binary not found: {self.martinize2_binary}")
        except subprocess.TimeoutExpired:
            errors.append("martinize2 timed out after 300s")
        except Exception as exc:
            errors.append(f"martinize2 execution error: {exc}")

        # Count output beads from the CG coordinate file (ATOMS).
        # martinize2 produces coordinates regardless of extension. We read
        # the GRO produced by the PDB-to-GRO conversion (which filters out
        # NaN/Inf atoms from martinize2's malformed trailing records).
        bead_count = 0
        output_gro = out / f"{base}_cg.gro"
        # martinize2 also emits per-molecule topology files named
        # ``molecule_0.itp``, ``molecule_1.itp``, ... These are the
        # **actual** per-molecule GROMACS .itp files that
        # ``martini_openmm.MartiniTopFile`` needs in [ molecules ].
        molecule_itp_paths: list[Path] = sorted(
            p for p in out.glob("molecule_*.itp") if p.is_file()
        )
        if output_coord_file.exists():
            self._pdb_to_gro(str(output_coord_file), str(output_gro), f"{base}_CG")
            if output_gro.exists():
                # Count atoms in the GRO header (line 2)
                try:
                    with open(output_gro) as f:
                        next(f)  # title
                        bead_count = int(next(f).strip())
                except (OSError, ValueError):
                    bead_count = self._count_pdb_atoms(str(output_coord_file))

        execution_status = "failed" if errors else "completed"

        payload = Martinize2Payload(
            martinize2_version=self.martinize2_version,
            martini_version=self.martini_version,
            input_structure_ref=input_structure_ref,
            secondary_structure_policy=ss_policy,
            elastic_network_policy=en_policy,
            output_cg_gro_ref=str(output_gro) if output_gro.exists() else "",
            # output_cg_itp_ref points to the per-molecule .itp list,
            # comma-joined (downstream consumers split). We also expose
            # the list as a separate field for callers that want a list
            # directly. The first .itp is the conventional single ref.
            output_cg_itp_ref=(
                ",".join(str(p) for p in molecule_itp_paths)
                if molecule_itp_paths
                else ""
            ),
            residue_count_input=residue_count_input,
            bead_count_output=bead_count,
            execution_status=execution_status,
            validation_errors=errors,
        )

        return self._build_receipt(
            kind="cg_martinize2_map",
            status=execution_status,
            operation_name="martinize2_map_protein",
            payload=payload,
            artifact_refs=(
                [str(output_gro), str(output_top_file)]
                + [str(p) for p in molecule_itp_paths]
            ),
        )

    # ── validate_outputs ──────────────────────────────────────────────

    def validate_outputs(self, receipt: ReceiptCore) -> ReceiptCore:
        """Validate martinize2 mapping outputs.

        Checks:
          1. Output .gro and .itp exist
          2. Bead count vs residue count ratio is in expected range
          3. .itp is parseable (has [ atoms ] section)

        Expected bead/residue ratio for Martini 3: roughly 1:1
        (each residue maps to 1-2 beads typically).
        If ratio is wildly off, flag it.
        """
        payload_data = receipt.payload
        if isinstance(payload_data, dict):
            payload = Martinize2Payload(**payload_data)
        else:
            payload = payload_data

        errors: list[str] = []

        # Check outputs exist
        gro_ref = payload.output_cg_gro_ref
        itp_ref = payload.output_cg_itp_ref

        if not gro_ref or not os.path.isfile(gro_ref):
            errors.append(f"Output GRO not found: {gro_ref}")
        if not itp_ref or not os.path.isfile(itp_ref):
            errors.append(f"Output ITP not found: {itp_ref}")

        # Bead vs residue ratio check
        if payload.residue_count_input > 0 and payload.bead_count_output > 0:
            ratio = payload.bead_count_output / payload.residue_count_input
            # Martini 3 typically maps 1-4 beads per residue
            # If ratio < 0.1 or > 10, something is wrong
            if ratio < 0.1 or ratio > 10.0:
                errors.append(
                    f"Bead/residue ratio {ratio:.2f} outside expected range 0.1–10.0 "
                    f"(beads={payload.bead_count_output}, residues={payload.residue_count_input})"
                )

        # Check ITP/coordinates has atom records (martinize2 -x output)
        if itp_ref and os.path.isfile(itp_ref):
            has_atoms = False
            try:
                with open(itp_ref) as f:
                    for line in f:
                        if line.startswith("ATOM"):
                            has_atoms = True
                            break
            except OSError:
                pass
            if not has_atoms:
                errors.append(f"Coordinate file {itp_ref} has no ATOM records")

        if errors:
            payload.validation_status = "failed"
            payload.validation_errors = errors
        else:
            payload.validation_status = "passed"

        return self._build_receipt(
            kind="cg_martinize2_validation",
            status=payload.validation_status or "passed",
            operation_name="martinize2_validate",
            payload=payload,
            artifact_refs=[gro_ref, itp_ref],
        )

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _pdb_to_gro(pdb_path: str, gro_path: str, title: str = "CG protein") -> str:
        """Convert martinize2 PDB-like output to GRO format.

        martinize2 -x flag produces CG coordinates in PDB-like format
        (ATOM records with Martini bead names). This converts to .gro.
        Filters out atoms with NaN/missing coordinates (martinize2 can
        emit malformed trailing atoms for incomplete sidechains).
        """
        import math
        try:
            atoms: list[str] = []
            skipped = 0
            with open(pdb_path) as f:
                for line in f:
                    if not line.startswith("ATOM"):
                        continue
                    if len(line) < 54:
                        continue
                    try:
                        resname = line[17:20].strip()
                        atomname = line[12:16].strip()
                        resnr = int(line[22:26].strip())
                        x = float(line[30:38].strip())
                        y = float(line[38:46].strip())
                        z = float(line[46:54].strip())
                        # Skip atoms with NaN/Inf coordinates (martinize2 artifact)
                        if math.isnan(x) or math.isnan(y) or math.isnan(z):
                            skipped += 1
                            continue
                        if math.isinf(x) or math.isinf(y) or math.isinf(z):
                            skipped += 1
                            continue
                        # GRO format: 5-digit resnr, 5-char resname, 5-char atomname, 5-digit atomid, 3x8.3f
                        atoms.append(f"{resnr % 100000:5d}{resname[:5]:5s}{atomname[:5]:5s}{len(atoms)+1:5d}{x:8.3f}{y:8.3f}{z:8.3f}\n")
                    except (ValueError, IndexError):
                        continue

            if not atoms:
                return ""

            box = Martinize2Adapter._estimate_box(pdb_path)
            with open(gro_path, "w") as f:
                f.write(f"{title}\n")
                f.write(f"{len(atoms):>5}\n")
                f.writelines(atoms)
                f.write(f"  {box[0]:.3f}  {box[1]:.3f}  {box[2]:.3f}\n")
            if skipped > 0:
                logger.warning("martinize2 PDB-to-GRO: skipped %d atoms with NaN/Inf coordinates", skipped)
            return gro_path
        except OSError:
            return ""

    @staticmethod
    def _estimate_box(pdb_path: str) -> tuple[float, float, float]:
        """Estimate box size from PDB coordinates."""
        try:
            min_x = min_y = min_z = float("inf")
            max_x = max_y = max_z = float("-inf")
            with open(pdb_path) as f:
                for line in f:
                    if not line.startswith("ATOM") or len(line) < 54:
                        continue
                    try:
                        x = float(line[30:38].strip())
                        y = float(line[38:46].strip())
                        z = float(line[46:54].strip())
                        min_x = min(min_x, x); max_x = max(max_x, x)
                        min_y = min(min_y, y); max_y = max(max_y, y)
                        min_z = min(min_z, z); max_z = max(max_z, z)
                    except ValueError:
                        continue
            if max_x == float("-inf"):
                return (5.0, 5.0, 5.0)
            padding = 2.0
            return (max_x - min_x + padding, max_y - min_y + padding, max_z - min_z + padding)
        except OSError:
            return (5.0, 5.0, 5.0)

    @staticmethod
    def _count_residues(pdb_path: str) -> int:
        """Count unique residue IDs from ATOM records in a PDB/gro file."""
        residues: set[str] = set()
        try:
            with open(pdb_path) as f:
                for line in f:
                    if line.startswith("ATOM") and len(line) >= 22:
                        res_id = line[21:26].strip()
                        residues.add(res_id)
        except OSError:
            pass
        return len(residues)

    @staticmethod
    def _count_residues_gro(gro_path: str) -> int:
        """Count unique residue numbers from a .gro file."""
        residues: set[int] = set()
        try:
            with open(gro_path) as f:
                lines = f.readlines()
            if len(lines) < 3:
                return 0
            atom_count = int(lines[1].strip())
            for i in range(atom_count):
                line_idx = 2 + i
                if line_idx >= len(lines):
                    break
                line = lines[line_idx]
                if len(line) >= 5:
                    try:
                        resnr = int(line[:5].strip())
                        residues.add(resnr)
                    except ValueError:
                        pass
            return len(residues)
        except (OSError, ValueError, IndexError):
            return 0

    @staticmethod
    def _count_pdb_atoms(path: str) -> int:
        """Count ATOM records in a PDB-like file (martinize2 -x output)."""
        count = 0
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("ATOM"):
                        count += 1
            return count
        except OSError:
            return 0

    @staticmethod
    def _count_gro_beads(path: str) -> int:
        """Count total atoms (beads) in a .gro file."""
        try:
            with open(path) as f:
                lines = f.readlines()
            if len(lines) < 2:
                return 0
            return int(lines[1].strip())
        except (OSError, ValueError):
            return 0

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: Martinize2Payload,
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
                output_refs=[payload.output_cg_gro_ref, payload.output_cg_itp_ref],
                artifact_refs=artifact_refs or [],
            ),
            hashes=ReceiptHashes(
                request_hash="",
                output_hash="",
                content_hash=f"martinize2_{payload.input_structure_ref}",
            ),
            started_at=_now_iso(),
            ended_at=_now_iso(),
            trace_id=f"trace_{receipt_id}",
            payload=payload.model_dump(),
        )

    def _error_receipt(self, error: str) -> ReceiptCore:
        payload = Martinize2Payload(
            martinize2_version=self.martinize2_version,
            martini_version=self.martini_version,
            input_structure_ref="",
            execution_status="failed",
            validation_errors=[error],
        )
        return self._build_receipt(
            kind="cg_martinize2_map",
            status="failed",
            operation_name="martinize2_map_protein",
            payload=payload,
        )

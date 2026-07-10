"""FlatProt CLI adapter for LMP structural visualization.

Upstream: https://github.com/t03i/FlatProt

Produces 2D SVG projections from protein structures (AlphaFold CIF or PDB+DSSP).
Designed for graceful degradation:
  - If `flatprot` binary is not on PATH, `is_available()` returns False and
    `render_svg()` returns None without raising.
  - If the run fails for any reason (timeout, non-zero exit, stderr), the
    module logs a warning and returns None.

The generator calls this inside the optional <Visuals> block and emits either
an inline-SVG URL (when the renderer succeeds and the file is uploaded to the
public LMP bucket) or a placeholder URL that a worker can fill later.

Environment variables:
  FLATPROT_BIN         : override binary path (default: shutil.which("flatprot"))
  FLATPROT_TIMEOUT_SEC : per-run timeout (default: 120)
  FLATPROT_CACHE_DIR   : local output cache (default: <cache_dir>/flatprot)
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FlatProtRenderResult:
    """Outcome of a FlatProt CLI invocation."""
    svg_path: Path
    command: str
    stderr_tail: str = ""
    returncode: int = 0


class FlatProtClient:
    """Thin wrapper around the `flatprot project` CLI.

    All IO is local — does NOT upload to GCS. The caller is responsible for
    hosting the SVG and emitting the public URL in the LMP XML.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        binary: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) / "flatprot"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._binary = binary or os.getenv("FLATPROT_BIN") or shutil.which("flatprot")
        try:
            self._timeout = int(timeout_sec if timeout_sec is not None else os.getenv("FLATPROT_TIMEOUT_SEC", "120"))
        except ValueError:
            self._timeout = 120

    # -- Discovery ----------------------------------------------------------

    def is_available(self) -> bool:
        """True iff the `flatprot` binary is resolvable."""
        return bool(self._binary) and Path(str(self._binary)).exists()

    @property
    def binary(self) -> Optional[str]:
        return self._binary

    # -- Rendering ----------------------------------------------------------

    @staticmethod
    def _preprocess_cif_for_sheets(cif_path: Path, cache_dir: Path) -> Path:
        """Inject ``_struct_sheet_range`` from STRN entries in ``_struct_conf``.

        AlphaFold CIF files store strand secondary-structure annotations as
        STRN rows inside the ``_struct_conf`` loop but do NOT emit a
        ``_struct_sheet_range`` table.  gemmi (used by FlatProt) only reads
        sheet information from ``_struct_sheet_range``, so without this
        preprocessing all beta-strands are rendered as coils.

        If the CIF already contains ``_struct_sheet_range`` or has no STRN
        entries the original path is returned unmodified.
        """
        try:
            text = cif_path.read_text(encoding="utf-8")
        except Exception:
            return cif_path

        # Quick guard: already has sheet_range → nothing to do
        if "_struct_sheet_range." in text:
            return cif_path

        # ---- Extract STRN rows from _struct_conf loop ----
        # Locate the _struct_conf loop block
        conf_match = re.search(
            r"(loop_\s*\n(?:_struct_conf\.\S+\s*\n)+)(.*?)(?=\n(?:loop_|#|_))",
            text,
            re.DOTALL,
        )
        if not conf_match:
            return cif_path

        header_block = conf_match.group(1)
        data_block = conf_match.group(2)

        # Parse column names from header
        columns = re.findall(r"_struct_conf\.(\S+)", header_block)
        if not columns:
            return cif_path

        # Build column-index lookup
        col_idx = {c: i for i, c in enumerate(columns)}

        needed = [
            "conf_type_id",
            "beg_auth_asym_id",
            "beg_auth_comp_id",
            "beg_auth_seq_id",
            "end_auth_asym_id",
            "end_auth_comp_id",
            "end_auth_seq_id",
        ]
        if not all(n in col_idx for n in needed):
            return cif_path

        # Tokenize data rows (respects single-quoted values)
        strn_rows: list[list[str]] = []
        for line in data_block.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("_"):
                continue
            tokens = re.findall(r"'[^']*'|\S+", line)
            if len(tokens) < len(columns):
                continue
            if "STRN" in tokens[col_idx["conf_type_id"]]:
                strn_rows.append(tokens)

        if not strn_rows:
            return cif_path

        # ---- Build _struct_sheet_range loop ----
        lines = [
            "#",
            "loop_",
            "_struct_sheet_range.sheet_id",
            "_struct_sheet_range.id",
            "_struct_sheet_range.beg_label_comp_id",
            "_struct_sheet_range.beg_label_asym_id",
            "_struct_sheet_range.beg_label_seq_id",
            "_struct_sheet_range.end_label_comp_id",
            "_struct_sheet_range.end_label_asym_id",
            "_struct_sheet_range.end_label_seq_id",
            "_struct_sheet_range.beg_auth_comp_id",
            "_struct_sheet_range.beg_auth_asym_id",
            "_struct_sheet_range.beg_auth_seq_id",
            "_struct_sheet_range.end_auth_comp_id",
            "_struct_sheet_range.end_auth_asym_id",
            "_struct_sheet_range.end_auth_seq_id",
        ]

        sheet_id = "A"
        for idx, row in enumerate(strn_rows, start=1):
            beg_chain = row[col_idx["beg_auth_asym_id"]]
            beg_comp = row[col_idx["beg_auth_comp_id"]]
            beg_seq = row[col_idx["beg_auth_seq_id"]]
            end_chain = row[col_idx["end_auth_asym_id"]]
            end_comp = row[col_idx["end_auth_comp_id"]]
            end_seq = row[col_idx["end_auth_seq_id"]]
            lines.append(
                f"{sheet_id} {idx} {beg_comp} {beg_chain} {beg_seq} "
                f"{end_comp} {end_chain} {end_seq} "
                f"{beg_comp} {beg_chain} {beg_seq} "
                f"{end_comp} {end_chain} {end_seq}"
            )
        lines.append("#")

        # Inject before the final data_ block or at end
        inject_text = "\n".join(lines) + "\n"

        # Find a good injection point — just before the last '#' line at the
        # end of the file, or append.
        last_hash = text.rfind("\n#\n")
        if last_hash != -1:
            patched = text[: last_hash + 1] + inject_text + text[last_hash + 1 :]
        else:
            patched = text + "\n" + inject_text

        patched_path = cache_dir / f"{cif_path.stem}_patched.cif"
        patched_path.write_text(patched, encoding="utf-8")
        logger.info(
            "FlatProt CIF preprocessed: injected %d STRN→sheet_range entries → %s",
            len(strn_rows),
            patched_path.name,
        )
        return patched_path

    def render_svg(
        self,
        structure_path: Path,
        *,
        output_name: Optional[str] = None,
        matrix_path: Optional[Path] = None,
        canvas_width: int = 800,
        canvas_height: int = 800,
    ) -> Optional[FlatProtRenderResult]:
        """Run `flatprot project <structure> --output <svg>`.

        Parameters
        ----------
        structure_path : Path
            Path to an AlphaFold CIF (no DSSP needed) or a DSSP-processed PDB/CIF.
        output_name : Optional[str]
            Basename of the output SVG. Defaults to the structure stem.
        matrix_path : Optional[Path]
            Optional alignment matrix (.npy) from `flatprot align`.
        canvas_width, canvas_height : int
            Output canvas size.

        Returns
        -------
        Optional[FlatProtRenderResult]
            None on any failure (missing binary, bad input, non-zero exit).
        """
        if not self.is_available():
            logger.debug("FlatProt binary not available; skipping SVG render")
            return None

        structure_path = Path(structure_path)
        if not structure_path.exists() or structure_path.stat().st_size == 0:
            logger.warning("FlatProt: structure path missing or empty: %s", structure_path)
            return None

        # Preprocess AlphaFold CIF: inject _struct_sheet_range from STRN
        # entries so gemmi (inside FlatProt) can see beta strands.
        if structure_path.suffix.lower() == ".cif":
            structure_path = self._preprocess_cif_for_sheets(
                structure_path, self.cache_dir
            )

        stem = output_name or structure_path.stem
        svg_path = self.cache_dir / f"{stem}.svg"

        cmd = [
            str(self._binary),
            "project",
            str(structure_path),
            "--output",
            str(svg_path),
            "--canvas-width",
            str(canvas_width),
            "--canvas-height",
            str(canvas_height),
        ]
        if matrix_path is not None:
            cmd.extend(["--matrix", str(matrix_path)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("FlatProt timed out after %ds for %s", self._timeout, structure_path.name)
            return None
        except FileNotFoundError:
            logger.warning("FlatProt binary not executable: %s", self._binary)
            return None
        except OSError as exc:
            logger.warning("FlatProt invocation OS error: %s", exc)
            return None

        stderr_tail = (proc.stderr or "").strip().splitlines()[-5:]
        stderr_tail_joined = "\n".join(stderr_tail)

        if proc.returncode != 0 or not svg_path.exists() or svg_path.stat().st_size == 0:
            logger.warning(
                "FlatProt render failed for %s (rc=%d): %s",
                structure_path.name,
                proc.returncode,
                stderr_tail_joined,
            )
            return None

        return FlatProtRenderResult(
            svg_path=svg_path,
            command=" ".join(cmd),
            stderr_tail=stderr_tail_joined,
            returncode=proc.returncode,
        )


__all__ = ["FlatProtClient", "FlatProtRenderResult"]

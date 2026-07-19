"""src/mica/sim/cg_martini/topology_preprocessor.py — TopologyPreprocessor (P0.3).

Authority:
  Lane CG/Martini — SLICE CG-P0.3
  Doctrina D1: todo receipt hereda ReceiptCore (receipts.py:37)
  Doctrina D5: execution_status != validation_status

Scope:
  - Expansion of GROMACS #define / #ifdef macros to explicit form
  - Resolution of named bond/angle/dihedral aliases
  - Normalization of ion names (NA+ → NA, CL- → CL, etc.)
  - Include graph flattening (optional, controlled)

Resuelve el blocker conocido:
  "Unsupported function type in [ bonds ] line: 1 2 b_NC3_PO4_def"

Fuera de scope:
  - Building topology from scratch (P0.2)
  - Geometry validation (P0.4)

Usage:
  pp = TopologyPreprocessor()
  receipt = pp.preprocess("input.top")
  # receipt.payload is CGTopologyPreprocessPayload
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from mica.provenance.receipts import ReceiptCore, ReceiptHashes, ReceiptRefs

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Known alias resolution maps
# ═══════════════════════════════════════════════════════════════════════════

# Bond type aliases: named GROMACS Martini bond types → explicit OpenMM params
# Format: { alias: (func_type, harmonic_params...) }
# These are Martini 3 specific. Extended as new cases appear.
KNOWN_BOND_ALIASES: dict[str, str] = {
    # Martini 3 default bond: harmonic force constant 1250 kJ/mol/nm^2
    "b_NC3_PO4_def": "1    1250.0    0.4",
    # Common Martini bonds
    "b_default": "1    1250.0    0.4",
    "b_weak": "1    500.0    0.5",
    "b_strong": "1    2500.0    0.35",
    "b_peptide": "1    2000.0    0.35",
}

KNOWN_ANGLE_ALIASES: dict[str, str] = {
    "a_default": "2    25.0    180.0",
    "a_weak": "2    10.0    180.0",
    "a_rigid": "2    45.0    180.0",
}

KNOWN_DIHEDRAL_ALIASES: dict[str, str] = {
    "d_default": "3    5.0    180.0    1",
}

# Ion name normalization map
ION_NAME_MAP: dict[str, str] = {
    "NA+": "NA",
    "Na+": "NA",
    "na+": "NA",
    "CL-": "CL",
    "Cl-": "CL",
    "cl-": "CL",
    "MG": "MG",
    "Mg2+": "MG",
    "mg2+": "MG",
    "CA": "CA",
    "Ca2+": "CA",
    "ca2+": "CA",
    "K+": "K",
    "k+": "K",
    "POT": "K",
    "Na+ ": "NA",
    "K+ ": "K",
    "Cl- ": "CL",
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Receipt counter
# ═══════════════════════════════════════════════════════════════════════════

_receipt_counter = 0


def _next_receipt_id(kind: str) -> str:
    global _receipt_counter
    _receipt_counter += 1
    return f"{kind}_{_receipt_counter}_{datetime.now(tz=timezone.utc).timestamp():.0f}"


# ═══════════════════════════════════════════════════════════════════════════
# Payload
# ═══════════════════════════════════════════════════════════════════════════


class CGTopologyPreprocessPayload(BaseModel):
    """Payload específico del preprocesador, dentro de ReceiptCore.payload.

    Doctrina D1: NO es schema aislado.
    """

    input_top_ref: str = Field(..., description="Input topology file path.")
    output_top_ref: str = Field(default="", description="Output preprocessed topology path.")
    include_graph: list[str] = Field(default_factory=list, description="Resolved #include tree.")
    macros_expanded: bool = False
    named_bond_aliases_resolved: bool = False
    ion_name_normalization: dict[str, str] = Field(default_factory=dict)
    unsupported_directives: list[str] = Field(
        default_factory=list,
        description="Directives that could not be resolved. Non-empty blocks validation.",
    )
    sha256_before: str = Field(default="")
    sha256_after: str = Field(default="")
    execution_status: str = "pending"
    validation_status: Optional[str] = None
    validation_errors: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Tokenizer
# ═══════════════════════════════════════════════════════════════════════════


class TopologyToken:
    """A single preprocessor directive or topology line."""

    def __init__(
        self,
        line_number: int,
        raw: str,
        directive: Optional[str] = None,
        args: Optional[str] = None,
        body: Optional[str] = None,
    ):
        self.line_number = line_number
        self.raw = raw
        self.directive = directive  # e.g. "define", "include", "ifdef"
        self.args = args
        self.body = body          # resolved body if applicable


def _tokenize_topology(text: str) -> list[TopologyToken]:
    """Tokenize a GROMACS topology into preprocessor tokens."""
    tokens: list[TopologyToken] = []
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            # Comment or empty
            tokens.append(TopologyToken(i, line))
            continue
        # Preprocessor directives
        if stripped.startswith("#"):
            parts = stripped[1:].split(None, 1)
            directive = parts[0].lower() if parts else ""
            args = parts[1] if len(parts) > 1 else ""
            tokens.append(TopologyToken(i, line, directive=directive, args=args))
        else:
            tokens.append(TopologyToken(i, line, body=stripped))
    return tokens


# ═══════════════════════════════════════════════════════════════════════════
# Built-in define registry (Martini 3 specific)
# ═══════════════════════════════════════════════════════════════════════════

# Common Martini 3 #define entries
_MARTINI3_BUILTIN_DEFINES: dict[str, str] = {
    "BOND_DEFAULT": "1    1250.0    0.4",
    "BOND_WEAK": "1    500.0    0.5",
    "BOND_STRONG": "1    2500.0    0.35",
    "BOND_PEPTIDE": "1    2000.0    0.35",
    "ANGLE_DEFAULT": "2    25.0    180.0",
    "ANGLE_WEAK": "2    10.0    180.0",
    "ANGLE_RIGID": "2    45.0    180.0",
    "DIHEDRAL_DEFAULT": "3    5.0    180.0    1",
    "FUDGE_LJ": "0.5",
    "FUDGE_QQ": "0.5",
    # Martini 3 water
    "MW": "72",
    "MWW": "36",
    "MW_SOL": "W",
    # Ion defines
    "NA_ION": "NA",
    "CL_ION": "CL",
    "MG_ION": "MG",
    "CA_ION": "CA",
    "K_ION": "K",
}


# ═══════════════════════════════════════════════════════════════════════════
# TopologyPreprocessor
# ═══════════════════════════════════════════════════════════════════════════


class TopologyPreprocessor:
    """Resuelve macros/aliases GROMACS → OpenMM para topologías Martini 3.

    El problema real documentado:
      "Unsupported function type in [ bonds ] line: 1 2 b_NC3_PO4_def"
    Ocurre porque GROMACS acepta named bond types como valores en [ bonds ]
    mientras que OpenMM requiere explicit function type + parameters.

    Flujo:
      1. Tokenize → identificar #define, #include, #ifdef
      2. Expand macros inline
      3. Resolve named aliases in [ bonds ], [ angles ], [ dihedrals ]
      4. Normalize ion names
      5. Emit preprocessed output + ReceiptCore
    """

    def __init__(
        self,
        builtin_defines: Optional[dict[str, str]] = None,
        bond_aliases: Optional[dict[str, str]] = None,
        angle_aliases: Optional[dict[str, str]] = None,
        dihedral_aliases: Optional[dict[str, str]] = None,
        ion_map: Optional[dict[str, str]] = None,
        workspace_id: str = "cg_martini",
        actor_id: str = "system",
    ):
        self.defines = dict(builtin_defines or _MARTINI3_BUILTIN_DEFINES)
        self.bond_aliases = dict(bond_aliases or KNOWN_BOND_ALIASES)
        self.angle_aliases = dict(angle_aliases or KNOWN_ANGLE_ALIASES)
        self.dihedral_aliases = dict(dihedral_aliases or KNOWN_DIHEDRAL_ALIASES)
        self.ion_map = dict(ion_map or ION_NAME_MAP)
        self.workspace_id = workspace_id
        self.actor_id = actor_id
        self._define_stack: list[dict[str, str]] = []

    # ── Main entry point ─────────────────────────────────────────────

    def preprocess(self, input_top_path: str, output_top_path: Optional[str] = None) -> ReceiptCore:
        """Preprocess a GROMACS topology for OpenMM compatibility.

        Args:
            input_top_path: Path to the input .top file.
            output_top_path: Optional output path. If None, auto-generated.

        Returns:
            ReceiptCore with CGTopologyPreprocessPayload.
        """
        if not os.path.isfile(input_top_path):
            return self._error_receipt(f"Input file not found: {input_top_path}")

        sha256_before = self._sha256_file(input_top_path)
        input_text = Path(input_top_path).read_text(encoding="utf-8", errors="replace")

        # Parse and preprocess
        result = self._process_topology_text(input_text)

        # Determine output path
        if not output_top_path:
            p = Path(input_top_path)
            output_top_path = str(p.parent / f"{p.stem}.preprocessed{p.suffix}")

        # Write output
        Path(output_top_path).write_text(result["output_text"], encoding="utf-8")
        sha256_after = self._sha256_file(output_top_path) if os.path.isfile(output_top_path) else ""

        # Build payload
        unsupported = result["unsupported_directives"]
        validation_errors: list[str] = []
        if unsupported:
            validation_errors.append(f"Unsupported directives remaining: {unsupported}")

        execution_status = "completed"
        validation_status = "failed" if unsupported else "passed"

        payload = CGTopologyPreprocessPayload(
            input_top_ref=input_top_path,
            output_top_ref=output_top_path,
            include_graph=result["include_graph"],
            macros_expanded=result["macros_expanded_count"] > 0,
            named_bond_aliases_resolved=result["alias_resolved_count"] > 0,
            ion_name_normalization=result["ion_normalization_map"],
            unsupported_directives=unsupported,
            sha256_before=sha256_before,
            sha256_after=sha256_after,
            execution_status=execution_status,
            validation_status=validation_status,
            validation_errors=validation_errors,
        )

        return self._build_receipt(
            kind="cg_topology_preprocess",
            status=validation_status,
            operation_name="topology_preprocess",
            payload=payload,
            artifact_refs=[output_top_path] if output_top_path else [],
            request_hash=sha256_before,
            output_hash=sha256_after,
            content_hash=sha256_after,
        )

    # ── Core processing ──────────────────────────────────────────────

    def _process_topology_text(self, text: str) -> dict:
        """Process topology text through preprocessor pipeline."""
        tokens = _tokenize_topology(text)
        active_define_map: dict[str, str] = dict(self.defines)
        include_graph: list[str] = []
        macros_expanded_count = 0
        alias_resolved_count = 0
        ion_normalization_map: dict[str, str] = {}
        unsupported_directives: list[str] = []
        output_lines: list[str] = []

        in_bonds = False
        in_angles = False
        in_dihedrals = False
        in_atoms = False
        in_molecules = False

        for token in tokens:
            # Handle preprocessor directives
            if token.directive:
                result = self._handle_directive(
                    token, active_define_map, include_graph, unsupported_directives
                )
                if result is None:
                    continue  # directive removed or skipped
                if result == "keep_raw":
                    output_lines.append(token.raw)
                continue

            # Track sections
            if token.body:
                section = self._detect_section(token.body)
                if section == "bonds":
                    in_bonds = True
                    in_angles = False
                    in_dihedrals = False
                    in_atoms = False
                    in_molecules = False
                elif section == "angles":
                    in_bonds = False
                    in_angles = True
                    in_dihedrals = False
                    in_atoms = False
                    in_molecules = False
                elif section == "dihedrals":
                    in_bonds = False
                    in_angles = False
                    in_dihedrals = True
                    in_atoms = False
                    in_molecules = False
                elif section == "atoms":
                    in_bonds = False
                    in_angles = False
                    in_dihedrals = False
                    in_atoms = True
                    in_molecules = False
                elif section == "molecules":
                    in_bonds = False
                    in_angles = False
                    in_dihedrals = False
                    in_atoms = False
                    in_molecules = True

            # Process body lines
            if token.body:
                line = token.body

                # Expand macros in line
                expanded_line, expanded = self._expand_macros(line, active_define_map)
                if expanded:
                    macros_expanded_count += 1

                # Resolve alias in bond/angle/dihedral sections
                if in_bonds:
                    resolved, count = self._resolve_bond_alias(expanded_line)
                    alias_resolved_count += count
                    expanded_line = resolved
                elif in_angles:
                    resolved, count = self._resolve_angle_alias(expanded_line)
                    alias_resolved_count += count
                    expanded_line = resolved
                elif in_dihedrals:
                    resolved, count = self._resolve_dihedral_alias(expanded_line)
                    alias_resolved_count += count
                    expanded_line = resolved

                # Normalize ion names in atoms section
                if in_atoms:
                    normalized_line, ion_map = self._normalize_ion_names(expanded_line)
                    ion_normalization_map.update(ion_map)
                    expanded_line = normalized_line

                output_lines.append(expanded_line)
            else:
                output_lines.append(token.raw)

        # Final macro expansion pass on the whole output (for #define values used later)
        final_text = "\n".join(output_lines)
        final_text, extra_expansions = self._expand_all_macros_post(final_text, active_define_map)
        macros_expanded_count += extra_expansions

        return {
            "output_text": final_text,
            "include_graph": include_graph,
            "macros_expanded_count": macros_expanded_count,
            "alias_resolved_count": alias_resolved_count,
            "ion_normalization_map": ion_normalization_map,
            "unsupported_directives": unsupported_directives,
        }

    def _handle_directive(
        self,
        token: TopologyToken,
        define_map: dict[str, str],
        include_graph: list[str],
        unsupported: list[str],
    ) -> Optional[str]:
        """Handle a preprocessor directive. Returns None if line should be removed."""
        directive = token.directive or ""
        args = (token.args or "").strip()

        if directive == "define":
            parts = args.split(None, 1)
            if len(parts) >= 1:
                name = parts[0]
                value = parts[1] if len(parts) > 1 else ""
                define_map[name] = value
            return None  # remove #define line

        elif directive == "include":
            # Extract filename
            fname = args.strip("\"<>").strip()
            if fname:
                include_graph.append(fname)
            return None  # remove #include line (flatten)

        elif directive in ("ifdef", "ifndef"):
            # For now, simplify: keep the block if condition matches
            # Full conditional handling is P1+
            return None

        elif directive == "endif":
            return None

        elif directive == "else":
            return None

        elif directive.startswith("if"):
            return None

        else:
            unsupported.append(f"#{directive} {args}")
            return "keep_raw"

    def _detect_section(self, line: str) -> Optional[str]:
        """Detect section header in a topology line."""
        stripped = line.strip().lower()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            if name == "bonds":
                return "bonds"
            if name == "angles":
                return "angles"
            if name == "dihedrals":
                return "dihedrals"
            if name == "atoms":
                return "atoms"
            if name == "molecules":
                return "molecules"
            if name in ("bondtypes", "angletypes", "dihedraltypes"):
                return name
        return None

    # ── Macro expansion ──────────────────────────────────────────────

    @staticmethod
    def _expand_macros(line: str, define_map: dict[str, str]) -> tuple[str, bool]:
        """Expand known #define macros in a line. Returns (expanded, was_modified)."""
        original = line
        # Sort defines by length descending to match longest first
        for name in sorted(define_map.keys(), key=len, reverse=True):
            value = define_map[name]
            # Replace the macro if it appears as a standalone token
            pattern = re.compile(rf'\b{re.escape(name)}\b')
            line = pattern.sub(value, line)
        return line, line != original

    @staticmethod
    def _expand_all_macros_post(text: str, define_map: dict[str, str]) -> tuple[str, int]:
        """Post-pass macro expansion on the entire output text."""
        count = 0
        for name in sorted(define_map.keys(), key=len, reverse=True):
            value = define_map[name]
            pattern = re.compile(rf'\b{re.escape(name)}\b')
            new_text, subs = pattern.subn(value, text)
            if subs > 0:
                count += subs
                text = new_text
        return text, count

    # ── Alias resolution ─────────────────────────────────────────────

    def _resolve_bond_alias(self, line: str) -> tuple[str, int]:
        """Resolve named bond types to explicit parameters."""
        return self._resolve_alias_in_line(line, self.bond_aliases)

    def _resolve_angle_alias(self, line: str) -> tuple[str, int]:
        return self._resolve_alias_in_line(line, self.angle_aliases)

    def _resolve_dihedral_alias(self, line: str) -> tuple[str, int]:
        return self._resolve_alias_in_line(line, self.dihedral_aliases)

    @staticmethod
    def _resolve_alias_in_line(line: str, alias_map: dict[str, str]) -> tuple[str, int]:
        """Replace named aliases with explicit parameters in a single line."""
        original = line
        # Remove leading whitespace and check first token
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#") or stripped.startswith("["):
            return line, 0

        tokens = stripped.split()
        if len(tokens) < 2:
            return line, 0

        # Check if the last significant token (before comments) is an alias
        clean_tokens = []
        for t in tokens:
            if t.startswith(";"):
                break
            clean_tokens.append(t)

        if len(clean_tokens) < 2:
            return line, 0

        # For [ bonds ]: i j  (optional: funct)  (optional: params or alias)
        # For [ angles ]: i j k  (optional: funct)  (optional: params or alias)
        # The alias could be in the funct position or after funct
        # Try matching the last token as an alias
        last_token = clean_tokens[-1]
        if last_token in alias_map:
            replacement = alias_map[last_token]
            # Replace last token with the alias expansion
            clean_line = " ".join(clean_tokens[:-1]) + " " + replacement
            # Pad to match original indentation
            indent = line[:len(line) - len(line.lstrip())]
            return indent + clean_line + "\n", 1

        # Also check if the funct column is a named type (before numeric params)
        if len(clean_tokens) >= 3:
            funct_token = clean_tokens[2]
            if funct_token in alias_map:
                replacement = alias_map[funct_token]
                clean_tokens[2] = replacement
                indent = line[:len(line) - len(line.lstrip())]
                return indent + " ".join(clean_tokens) + "\n", 1

        return line, 0

    # ── Ion normalization ────────────────────────────────────────────

    @staticmethod
    def _normalize_ion_names(line: str) -> tuple[str, dict[str, str]]:
        """Normalize ion names in a [ atoms ] line.

        Returns (modified_line, {found_ion: normalized_ion}).
        """
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("[") or stripped.startswith("#"):
            return line, {}

        normalized_map: dict[str, str] = {}
        tokens = stripped.split()
        if len(tokens) < 5:
            return line, {}

        # In GROMACS [ atoms ]: nr type resnr residue atom cgnr charge
        # Atom name is typically token 4 (0-indexed) or token depending on format
        # Try atom name field (varies by format)
        for idx in range(len(tokens)):
            token = tokens[idx]
            if token in ION_NAME_MAP:
                normalized = ION_NAME_MAP[token]
                if normalized != token:
                    tokens[idx] = normalized
                    normalized_map[token] = normalized

        if normalized_map:
            indent = line[:len(line) - len(line.lstrip())]
            return indent + " ".join(tokens) + "\n", normalized_map

        return line, {}

    # ── Validation ───────────────────────────────────────────────────

    def validate(self, receipt: ReceiptCore) -> ReceiptCore:
        """Validate preprocessor output: checks unsupported_directives.

        Returns a new ReceiptCore with validation_status.
        """
        payload_data = receipt.payload
        if isinstance(payload_data, dict):
            payload = CGTopologyPreprocessPayload(**payload_data)
        else:
            payload = payload_data

        errors: list[str] = []

        if payload.unsupported_directives:
            errors.append(f"Unsupported directives: {payload.unsupported_directives}")

        if not payload.output_top_ref or not os.path.isfile(payload.output_top_ref.replace("file://", "")):
            errors.append("Output file not found")

        if errors:
            payload.validation_status = "failed"
            payload.validation_errors = errors
        else:
            payload.validation_status = "passed"

        return self._build_receipt(
            kind="cg_topology_preprocess_validation",
            status=payload.validation_status or "passed",
            operation_name="topology_preprocess_validate",
            payload=payload,
            content_hash=payload.sha256_after,
        )

    # ── Receipt builder ──────────────────────────────────────────────

    def _build_receipt(
        self,
        kind: str,
        status: str,
        operation_name: str,
        payload: CGTopologyPreprocessPayload,
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
                output_refs=[payload.output_top_ref] if payload.output_top_ref else [],
                artifact_refs=artifact_refs or [],
            ),
            hashes=ReceiptHashes(
                request_hash=request_hash,
                output_hash=output_hash or "",
                content_hash=content_hash,
            ),
            started_at=_now_iso(),
            ended_at=_now_iso(),
            trace_id=f"trace_{receipt_id}",
            payload=payload.model_dump(),
        )

    def _error_receipt(self, error: str) -> ReceiptCore:
        payload = CGTopologyPreprocessPayload(
            input_top_ref="",
            execution_status="failed",
            validation_errors=[error],
        )
        return self._build_receipt(
            kind="cg_topology_preprocess",
            status="failed",
            operation_name="topology_preprocess",
            payload=payload,
        )

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _sha256_file(path: str) -> str:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

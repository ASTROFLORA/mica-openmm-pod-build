"""
safety_gate.py — P2-02 SafetyGate: Two-pass pre/post safety check.

Anti-rigidity rule R-10: Safety checks MUST NOT hard-block when data is marginal.
Only BLOCK for syntactically impossible inputs (empty SMILES, invalid SMILES chars).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ---------------------------------------------------------------------------
# SafetyLevel
# ---------------------------------------------------------------------------

class SafetyLevel(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"

    def is_blocking(self) -> bool:
        return self == SafetyLevel.BLOCK

    def severity_order(self) -> int:
        return {"PASS": 0, "WARN": 1, "BLOCK": 2}[self.value]


# ---------------------------------------------------------------------------
# SafetyCheckResult
# ---------------------------------------------------------------------------

@dataclass
class SafetyCheckResult:
    check_id: str
    level: SafetyLevel
    message: str
    evidence: dict = field(default_factory=dict)
    auto_corrected: bool = False

    def passed(self) -> bool:
        return self.level != SafetyLevel.BLOCK


# ---------------------------------------------------------------------------
# SafetyEnvelope
# ---------------------------------------------------------------------------

@dataclass
class SafetyEnvelope:
    tool_id: str
    pass_count: int
    warn_count: int
    block_count: int
    results: list[SafetyCheckResult]
    overall_level: SafetyLevel
    all_tags: list[str]
    execution_allowed: bool

    @property
    def is_blocked(self) -> bool:
        return self.block_count > 0

    @property
    def has_warnings(self) -> bool:
        return self.warn_count > 0

    def to_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "block_count": self.block_count,
            "overall_level": self.overall_level.value,
            "all_tags": self.all_tags,
            "execution_allowed": self.execution_allowed,
            "results": [
                {
                    "check_id": r.check_id,
                    "level": r.level.value,
                    "message": r.message,
                    "evidence": r.evidence,
                    "auto_corrected": r.auto_corrected,
                }
                for r in self.results
            ],
        }

    def summary(self) -> str:
        status = "BLOCKED" if self.is_blocked else "ALLOWED"
        return (
            f"SafetyEnvelope(tool={self.tool_id}, "
            f"PASS: {self.pass_count}, "
            f"WARN: {self.warn_count}, "
            f"BLOCK: {self.block_count} → {status})"
        )


def _build_envelope(tool_id: str, results: list[SafetyCheckResult]) -> SafetyEnvelope:
    pass_count = sum(1 for r in results if r.level == SafetyLevel.PASS)
    warn_count = sum(1 for r in results if r.level == SafetyLevel.WARN)
    block_count = sum(1 for r in results if r.level == SafetyLevel.BLOCK)

    if block_count > 0:
        overall = SafetyLevel.BLOCK
    elif warn_count > 0:
        overall = SafetyLevel.WARN
    else:
        overall = SafetyLevel.PASS

    all_tags = list(
        dict.fromkeys(r.check_id for r in results if r.level != SafetyLevel.PASS)
    )
    execution_allowed = block_count == 0

    return SafetyEnvelope(
        tool_id=tool_id,
        pass_count=pass_count,
        warn_count=warn_count,
        block_count=block_count,
        results=results,
        overall_level=overall,
        all_tags=all_tags,
        execution_allowed=execution_allowed,
    )


def _empty_envelope(tool_id: str) -> SafetyEnvelope:
    return SafetyEnvelope(
        tool_id=tool_id,
        pass_count=0,
        warn_count=0,
        block_count=0,
        results=[],
        overall_level=SafetyLevel.PASS,
        all_tags=[],
        execution_allowed=True,
    )


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

def check_smiles_syntax(smiles: str) -> SafetyCheckResult:
    """Basic SMILES validation (no RDKit required)."""
    if not smiles:
        return SafetyCheckResult(
            check_id="smiles_syntax",
            level=SafetyLevel.BLOCK,
            message="SMILES is empty — syntactically invalid input.",
            evidence={"smiles": smiles},
        )
    if " " in smiles:
        return SafetyCheckResult(
            check_id="smiles_syntax",
            level=SafetyLevel.BLOCK,
            message="SMILES contains spaces — syntactically invalid input.",
            evidence={"smiles": smiles, "reason": "smiles_has_space"},
        )
    if not re.search(r"[A-Za-z]", smiles):
        return SafetyCheckResult(
            check_id="smiles_syntax",
            level=SafetyLevel.BLOCK,
            message="SMILES contains no alphabetic characters — likely not a SMILES string.",
            evidence={"smiles": smiles, "reason": "not_smiles"},
        )
    return SafetyCheckResult(
        check_id="smiles_syntax",
        level=SafetyLevel.PASS,
        message="SMILES syntax check passed.",
        evidence={"smiles": smiles},
    )


def check_pdb_id_format(pdb_id: str) -> SafetyCheckResult:
    """
    PDB ID must be exactly 4 chars: 1 digit + 3 alphanumeric (case-insensitive).
    Invalid → WARN (could be local path). Valid → PASS.
    """
    pattern = re.compile(r"^[0-9][A-Z0-9]{3}$", re.IGNORECASE)
    if pattern.match(pdb_id):
        return SafetyCheckResult(
            check_id="pdb_id_format",
            level=SafetyLevel.PASS,
            message="PDB ID format is valid.",
            evidence={"pdb_id": pdb_id},
        )
    return SafetyCheckResult(
        check_id="pdb_id_format",
        level=SafetyLevel.WARN,
        message=(
            f"PDB ID '{pdb_id}' does not match expected format "
            "(1 digit + 3 alphanumeric). May be a local path — proceeding with warning."
        ),
        evidence={"pdb_id": pdb_id},
    )


def check_drug_likeness_range(mol_weight: float, logp: float) -> SafetyCheckResult:
    """
    Lipinski Ro5: MW ≤ 500 and logP ≤ 5.
    Violations produce WARN (R-10 — never BLOCK for borderline physicochemistry).
    """
    violations = 0
    if mol_weight > 500:
        violations += 1
    if logp > 5:
        violations += 1

    evidence = {
        "mol_weight": mol_weight,
        "logp": logp,
        "ro5_violations": violations,
    }

    if violations == 0:
        return SafetyCheckResult(
            check_id="drug_likeness_range",
            level=SafetyLevel.PASS,
            message="Compound satisfies Lipinski Ro5.",
            evidence=evidence,
        )
    if violations == 1:
        return SafetyCheckResult(
            check_id="drug_likeness_range",
            level=SafetyLevel.WARN,
            message="Minor Lipinski Ro5 violation (1 rule violated).",
            evidence={**evidence, "tag": "lipinski_minor_violation"},
        )
    return SafetyCheckResult(
        check_id="drug_likeness_range",
        level=SafetyLevel.WARN,
        message="Dual Lipinski Ro5 violation (MW and logP both exceeded).",
        evidence={**evidence, "tag": "lipinski_dual_violation"},
    )


_HALLUCINATION_PATTERNS = [
    "[BioDynamo]",
    "[PLACEHOLDER]",
    "Response to:",
    "[ERROR]",
    "{query}",
    "TODO",
]


def check_hallucination_markers(text: str) -> SafetyCheckResult:
    """Detect LLM template stubs that indicate hallucinated / unfilled responses."""
    matched = [p for p in _HALLUCINATION_PATTERNS if p in text]
    if matched:
        return SafetyCheckResult(
            check_id="hallucination_markers",
            level=SafetyLevel.WARN,
            message="Hallucination marker(s) detected in text.",
            evidence={"matched_patterns": matched},
        )
    return SafetyCheckResult(
        check_id="hallucination_markers",
        level=SafetyLevel.PASS,
        message="No hallucination markers detected.",
        evidence={},
    )


def check_output_completeness(
    output: dict, required_keys: list[str]
) -> SafetyCheckResult:
    """Check that a tool output dict contains all required keys."""
    missing = [k for k in required_keys if k not in output]
    if missing:
        return SafetyCheckResult(
            check_id="output_completeness",
            level=SafetyLevel.WARN,
            message=f"Tool output is missing required keys: {missing}",
            evidence={"missing_keys": missing},
        )
    return SafetyCheckResult(
        check_id="output_completeness",
        level=SafetyLevel.PASS,
        message="Tool output contains all required keys.",
        evidence={"required_keys": required_keys},
    )


def check_empty_result(output: dict) -> SafetyCheckResult:
    """Check whether the output looks like an empty or error result."""
    if not output:
        return SafetyCheckResult(
            check_id="empty_result",
            level=SafetyLevel.WARN,
            message="Tool output is an empty dict.",
            evidence={},
        )
    if output.get("status") == "error":
        return SafetyCheckResult(
            check_id="empty_result",
            level=SafetyLevel.WARN,
            message="Tool output has status='error'.",
            evidence={"status": output.get("status"), "tag": "error_status_in_result"},
        )
    return SafetyCheckResult(
        check_id="empty_result",
        level=SafetyLevel.PASS,
        message="Tool output is non-empty and not an error.",
        evidence={},
    )


# ---------------------------------------------------------------------------
# Gate registries
# ---------------------------------------------------------------------------

PRE_GATE_CHECKS: dict[str, Callable] = {
    "smiles_syntax": check_smiles_syntax,
    "pdb_id_format": check_pdb_id_format,
    "hallucination_markers": check_hallucination_markers,
}

POST_GATE_CHECKS: dict[str, Callable] = {
    "output_completeness": check_output_completeness,
    "empty_result": check_empty_result,
}


# ---------------------------------------------------------------------------
# SafetyGate
# ---------------------------------------------------------------------------

class SafetyGate:
    """
    Two-pass safety screening applied around tool calls.

    Pre-gate:  validate inputs BEFORE calling the tool.
    Post-gate: validate outputs AFTER the tool returns.
    """

    def __init__(self, tool_registry: Any | None = None) -> None:
        self._registry = tool_registry

    # ------------------------------------------------------------------
    # Pre-check
    # ------------------------------------------------------------------

    def pre_check(self, tool_id: str, arguments: dict) -> SafetyEnvelope:
        """Run pre-gate checks appropriate for the tool's argument types."""
        results: list[SafetyCheckResult] = []

        domain_check_run = False

        if "smiles" in arguments:
            results.append(check_smiles_syntax(str(arguments["smiles"])))
            domain_check_run = True

        if "pdb_id" in arguments:
            results.append(check_pdb_id_format(str(arguments["pdb_id"])))
            domain_check_run = True

        if "query" in arguments:
            results.append(check_hallucination_markers(str(arguments["query"])))
            domain_check_run = True

        if not domain_check_run:
            # Fall back: run hallucination check on serialized arguments
            dumped = json.dumps(arguments, default=str)
            results.append(check_hallucination_markers(dumped))

        return _build_envelope(tool_id, results)

    # ------------------------------------------------------------------
    # Post-check
    # ------------------------------------------------------------------

    def post_check(
        self,
        tool_id: str,
        result: dict,
        required_keys: list[str] | None = None,
    ) -> SafetyEnvelope:
        """Run post-gate checks on tool output."""
        results: list[SafetyCheckResult] = []

        results.append(check_empty_result(result))

        if required_keys:
            results.append(check_output_completeness(result, required_keys))

        # Check string values in result for hallucination markers
        result_text = str(result)
        if any(p in result_text for p in _HALLUCINATION_PATTERNS):
            results.append(check_hallucination_markers(result_text))

        return _build_envelope(tool_id, results)

    # ------------------------------------------------------------------
    # wrap_tool_call
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        tool_id: str,
        arguments: dict,
        executor_fn: Callable[[dict], dict],
        required_output_keys: list[str] | None = None,
    ) -> tuple[dict, SafetyEnvelope, SafetyEnvelope]:
        """
        Run pre_check → executor_fn → post_check.

        Returns (result, pre_envelope, post_envelope).
        If pre-gate blocks, executor_fn is NOT called.
        """
        pre_env = self.pre_check(tool_id, arguments)

        if pre_env.is_blocked:
            blocked_result: dict = {
                "status": "blocked",
                "blocked_by": pre_env.all_tags,
            }
            post_env = _empty_envelope(tool_id)
            return blocked_result, pre_env, post_env

        result = executor_fn(arguments)
        post_env = self.post_check(tool_id, result, required_output_keys)
        return result, pre_env, post_env

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def effective_level(
        self, pre: SafetyEnvelope, post: SafetyEnvelope
    ) -> SafetyLevel:
        """Return the maximum severity across both envelopes."""
        if pre.overall_level.severity_order() >= post.overall_level.severity_order():
            return pre.overall_level
        return post.overall_level

    def annotate_phase_event(
        self,
        event: Any,
        pre: SafetyEnvelope,
        post: SafetyEnvelope,
    ) -> None:
        """
        Add safety envelope summaries to event.quality_signals.
        Best-effort: silently ignores events that don't support attribute access.
        """
        try:
            qs = event.quality_signals
            qs["safety_pre"] = pre.summary()
            qs["safety_post"] = post.summary()
        except Exception:  # noqa: BLE001
            pass

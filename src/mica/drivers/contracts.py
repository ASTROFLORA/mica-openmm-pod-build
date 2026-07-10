"""
mica.drivers.contracts
======================
Typed contract layer shared by all specialist drivers (BioDynamo, Alchemist,
SMIC, AgenticDriver).

Anti-rigidity rules enforced here:
  R-03: Any driver that cannot produce a real answer MUST return DriverFailureEvent.
         Template stubs are forbidden.
  R-09: Evidence-Gated Claims — any synthesis referencing zero source_tool_call_id
         must be flagged UNVERIFIED.

Standard-library only — no third-party deps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass  # kept for future annotation-only imports


# ---------------------------------------------------------------------------
# FailureType constants
# ---------------------------------------------------------------------------

class FailureType:
    """Enumeration of driver failure categories."""
    LLM_TIMEOUT = "llm_timeout"
    LLM_ERROR = "llm_error"
    TOOL_CALL_FAILED = "tool_call_failed"
    NO_TOOLS_RESOLVED = "no_tools_resolved"
    TEMPLATE_STUB = "template_stub"          # driver returned "[AgentName] Response to:"
    MISSING_ARTIFACT = "missing_artifact"
    VALIDATION_FAILED = "validation_failed"
    PROTEIN_UNRESOLVABLE = "protein_unresolvable"

    @classmethod
    def all_values(cls) -> list[str]:
        return [
            cls.LLM_TIMEOUT,
            cls.LLM_ERROR,
            cls.TOOL_CALL_FAILED,
            cls.NO_TOOLS_RESOLVED,
            cls.TEMPLATE_STUB,
            cls.MISSING_ARTIFACT,
            cls.VALIDATION_FAILED,
            cls.PROTEIN_UNRESOLVABLE,
        ]


# ---------------------------------------------------------------------------
# DriverContractViolation exception
# ---------------------------------------------------------------------------

class DriverContractViolation(Exception):
    """Raised when a driver violates its typed output contract."""

    def __init__(self, driver_id: str, violation: str, detail: str = "") -> None:
        self.driver_id = driver_id
        self.violation = violation
        self.detail = detail
        msg = f"[{driver_id}] Contract violation: {violation}."
        if detail:
            msg += f" {detail}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# DriverFailureEvent dataclass
# ---------------------------------------------------------------------------

@dataclass
class DriverFailureEvent:
    """
    Typed failure signal returned by any driver that cannot produce a real answer.

    Contract: AgenticDriver MUST detect this and route to retry/soft-fail path.
    AgenticDriver MUST NEVER synthesize a DriverFailureEvent as if it were output.

    Anti-rigidity rule R-03: template stubs are forbidden.
    """

    driver_id: str
    failure_type: str          # See FailureType constants
    query: str
    attempted_steps: list[str]
    timestamp: datetime
    retryable: bool = True
    error_detail: str = ""
    context_snapshot: dict = field(default_factory=dict)  # sanitized (no secrets)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.driver_id:
            raise ValueError("DriverFailureEvent.driver_id must be non-empty.")
        if self.failure_type not in FailureType.all_values():
            raise ValueError(
                f"Unknown failure_type {self.failure_type!r}. "
                f"Use one of: {FailureType.all_values()}"
            )
        if not isinstance(self.attempted_steps, list):
            raise TypeError("attempted_steps must be a list.")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime instance.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_template_stub(self) -> bool:
        """True if this failure was caused by a template stub detection."""
        return self.failure_type == FailureType.TEMPLATE_STUB

    def to_safe_dict(self) -> dict:
        """
        Serializable, safe (no PII/secrets) representation.

        context_snapshot keys that look like secrets (password, token, key,
        secret, credential) are redacted.
        """
        _SECRET_KEYS = re.compile(
            r"(?i)(password|passwd|token|secret|credential|api_key|auth)"
        )

        safe_snapshot: dict = {}
        for k, v in self.context_snapshot.items():
            if _SECRET_KEYS.search(str(k)):
                safe_snapshot[k] = "***REDACTED***"
            else:
                safe_snapshot[k] = v

        return {
            "driver_id": self.driver_id,
            "failure_type": self.failure_type,
            "query": self.query,
            "attempted_steps": list(self.attempted_steps),
            "timestamp": self.timestamp.isoformat(),
            "retryable": self.retryable,
            "error_detail": self.error_detail,
            "context_snapshot": safe_snapshot,
        }


# ---------------------------------------------------------------------------
# TemplateStubDetector
# ---------------------------------------------------------------------------

class TemplateStubDetector:
    """
    Detects template stub responses in driver output.

    Patterns that constitute a template stub (R-03 violation):
    - "[AgentName] Response to: {query}"
    - "[BioDynamo] Response to:"
    - Any string matching the pattern r"\\[\\w\\s]+\\] Response to:"
    - Empty string
    - String containing "{query}" or "{context}" as literal text
    """

    _PATTERNS: list[re.Pattern] = [
        # e.g. "[BioDynamo] Response to: ..."
        re.compile(r"^\s*\[[\w\s]+\]\s+Response to:", re.IGNORECASE),
        # literal template placeholders left unformatted
        re.compile(r"\{query\}|\{context\}", re.IGNORECASE),
    ]

    @classmethod
    def is_stub(cls, text: str) -> bool:
        """Return True if *text* is a template stub (R-03 violation)."""
        if not text or not text.strip():
            return True
        for pattern in cls._PATTERNS:
            if pattern.search(text):
                return True
        return False

    @classmethod
    def extract_driver_id(cls, stub_text: str) -> str:
        """
        Extract driver name from a stub like '[BioDynamo] Response to: ...'.

        Returns empty string if pattern not found.
        """
        m = re.match(r"^\s*\[([\w\s]+)\]\s+Response to:", stub_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return ""


# ---------------------------------------------------------------------------
# HallucinationFirewall
# ---------------------------------------------------------------------------

@dataclass
class FirewallResult:
    """Result object returned by HallucinationFirewall.validate_synthesis."""
    synthesis_text: str
    verified_count: int
    unverified_count: int
    unverified_claims: list[str]
    overall_verification_rate: float  # 0.0–1.0
    categorical_unverified: list[str] = field(default_factory=list)

    def is_acceptable(self, min_rate: float = 0.5) -> bool:
        """Return True if verification rate meets *min_rate* threshold."""
        return self.overall_verification_rate >= min_rate


class HallucinationFirewall:
    """
    Validates synthesis output against saga log evidence.

    Anti-rigidity rule R-09: Any claim about a molecular property, simulation
    result, or literature finding must carry a source_tool_call_id in the saga
    log.  Claims without provenance are marked UNVERIFIED before delivery.

    Usage::

        firewall = HallucinationFirewall(saga_log_entries)
        result = firewall.validate_synthesis(synthesis_text)
        # result.unverified_claims: list of flagged claims
        # result.verified_count, result.unverified_count
    """

    # Regex: numeric value followed by an optional unit token.
    # Matches: -7.4 kcal/mol | 2.1 Å | IC50 = 50 nM | logP = 2.3 | MW = 342 Da
    _NUMERIC_CLAIM_RE = re.compile(
        r"""
        (?:                              # optional label prefix, e.g. "IC50 ="
            [A-Za-z_][A-Za-z0-9_\s]*    # label
            \s*[=:]\s*                   # = or :
        )?
        -?                               # optional negative sign
        \d+                              # integer part
        (?:\.\d+)?                       # optional decimal
        \s*                              # optional space
        (?:                              # optional unit
            kcal/mol|kJ/mol|nm|nM|µM|uM|
            mM|Da|kDa|Å|angstrom|kcal|
            ns|ps|fs|\%|Hz|K|°C
        )
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    # Pattern for inline citation markers like [tool_call_id: abc123] or (tcid:abc)
    _CITATION_RE = re.compile(
        r"\b(?:tool_call_id|tcid|source_id)\s*[=:]\s*([A-Za-z0-9_\-]+)",
        re.IGNORECASE,
    )

    # ── G1: Categorical biological claim detector ─────────────────
    # Matches entity-relationship sentences: "X phosphorylates Y",
    # "A inhibits B", "C activates D", "E binds F", etc.
    _BIO_RELATION_VERBS = (
        r"phosphorylat|dephosphorylat|ubiquitinat|acetylat|methylat|"
        r"SUMOylat|glycosylat|palmitoylat|myristoylat|nitrosylat|"
        r"activat|inhibit|suppress|repress|induc|stimulat|"
        r"regulat|modulat|upregulat|downregulat|"
        r"bind|interact|associat|complex|recruit|"
        r"transcrib|translat|degrad|stabiliz|destabiliz|"
        r"cleav|catalyz|translocat|secreti|signal"
    )
    _CATEGORICAL_CLAIM_RE = re.compile(
        r"(?P<subject>[A-Z][A-Z0-9]{1,15}(?:-[A-Z0-9]+)?)"    # Entity1: e.g. OSR1, TP53, WNK1-L
        r"\s+(?:\w+\s+){0,4}"                                    # up to 4 filler words
        r"(?P<verb>" + _BIO_RELATION_VERBS + r")\w*"             # relationship verb
        r"\s+(?:\w+\s+){0,4}"                                    # up to 4 filler words
        r"(?P<object>[A-Z][A-Z0-9]{1,15}(?:-[A-Z0-9]+)?)",      # Entity2
        re.IGNORECASE,
    )

    def __init__(
        self,
        saga_log_entries: list[dict],
        known_facts: list[dict] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        saga_log_entries:
            list of dicts, each with keys including ``tool_call_id``,
            ``tool_name``, ``outputs``, ``timestamp``.
        known_facts:
            Optional list of known entity-relationship facts from ATOM or
            external KB. Each dict has ``subject``, ``relation``, ``object``
            keys (all lowercased). Used to validate categorical claims.
        """
        self._tool_call_ids: set[str] = {
            e["tool_call_id"]
            for e in saga_log_entries
            if "tool_call_id" in e and e["tool_call_id"]
        }
        # Build a set of known (subject, relation_stem, object) triples
        self._known_facts: set[tuple[str, str, str]] = set()
        for fact in (known_facts or []):
            subj = str(fact.get("subject", "")).strip().upper()
            rel = str(fact.get("relation", "")).strip().lower()
            obj = str(fact.get("object", "")).strip().upper()
            if subj and rel and obj:
                self._known_facts.add((subj, rel, obj))

    def _split_sentences(self, text: str) -> list[str]:
        """Naive sentence splitter sufficient for scientific prose."""
        # Split on '. ', '! ', '? ' — keep sentence intact
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

    def _sentence_contains_numeric_claim(self, sentence: str) -> bool:
        return bool(self._NUMERIC_CLAIM_RE.search(sentence))

    def _sentence_has_valid_citation(self, sentence: str) -> bool:
        """Return True if sentence cites a tool_call_id present in the saga log."""
        if not self._tool_call_ids:
            return False
        for m in self._CITATION_RE.finditer(sentence):
            if m.group(1) in self._tool_call_ids:
                return True
        return False

    def _extract_categorical_claims(self, sentence: str) -> list[tuple[str, str, str]]:
        """Extract (subject, verb_stem, object) triples from a sentence."""
        claims: list[tuple[str, str, str]] = []
        for m in self._CATEGORICAL_CLAIM_RE.finditer(sentence):
            subj = m.group("subject").upper()
            verb = m.group("verb").lower().rstrip("es").rstrip("s")
            obj = m.group("object").upper()
            # Skip if subject == object (self-references are noise)
            if subj != obj:
                claims.append((subj, verb, obj))
        return claims

    def _categorical_claim_is_verified(
        self, subject: str, verb: str, obj: str
    ) -> bool | None:
        """Check a categorical claim against known_facts.

        Returns True if a matching fact is found, False if a contradicting
        fact is found, or None if no evidence either way.
        """
        if not self._known_facts:
            return None  # No KB to check against
        # Normalize verb to stem for matching
        for known_subj, known_rel, known_obj in self._known_facts:
            if known_subj == subject and known_obj == obj:
                # Same entity pair — check if relation aligns
                if verb in known_rel or known_rel in verb:
                    return True
                # Different relation on same pair → potential contradiction
                return False
            # Check reverse direction (B inhibits A ↔ A is inhibited by B)
            if known_subj == obj and known_obj == subject:
                if verb in known_rel or known_rel in verb:
                    return True
        return None  # No evidence

    def validate_synthesis(self, synthesis_text: str) -> FirewallResult:
        """
        Scan *synthesis_text* for claim-like statements and verify they cite
        tool call IDs present in the saga log.

        A "claim" is a sentence containing:
        - Numeric values with units (e.g., "binding affinity of -7.4 kcal/mol")
        - Statistical assertions ("RMSD of 2.1 Å", "IC50 = 50 nM")
        - Property predictions ("logP = 2.3", "MW = 342 Da")
        - Categorical biological claims ("OSR1 phosphorylates WNK1")

        Returns a :class:`FirewallResult`.  If saga_log is empty, ALL numeric
        claims are unverified.
        """
        sentences = self._split_sentences(synthesis_text)

        verified: int = 0
        unverified: int = 0
        unverified_claims: list[str] = []
        categorical_unverified: list[str] = []

        for sentence in sentences:
            is_numeric = self._sentence_contains_numeric_claim(sentence)
            categorical_triples = self._extract_categorical_claims(sentence)

            if not is_numeric and not categorical_triples:
                continue

            has_citation = self._sentence_has_valid_citation(sentence)

            # Check categorical claims against known facts
            cat_contradicted = False
            for subj, verb, obj in categorical_triples:
                verdict = self._categorical_claim_is_verified(subj, verb, obj)
                if verdict is False:
                    cat_contradicted = True
                    categorical_unverified.append(
                        f"CONTRADICTED: {subj} {verb}* {obj} — "
                        f"conflicts with known facts"
                    )
                elif verdict is None and not has_citation:
                    categorical_unverified.append(
                        f"UNVERIFIED: {subj} {verb}* {obj} — "
                        f"no citation and no KB evidence"
                    )

            if has_citation and not cat_contradicted:
                verified += 1
            else:
                unverified += 1
                unverified_claims.append(sentence)

        total = verified + unverified
        rate = (verified / total) if total > 0 else 1.0  # no claims → trivially verified

        return FirewallResult(
            synthesis_text=synthesis_text,
            verified_count=verified,
            unverified_count=unverified,
            unverified_claims=unverified_claims,
            overall_verification_rate=rate,
            categorical_unverified=categorical_unverified,
        )


# ---------------------------------------------------------------------------
# Phase name constants (used by all drivers)
# ---------------------------------------------------------------------------

class Phase:
    """Standard phase names for PhaseTransitionEvent.phase field."""
    # Input preparation
    STRUCTURE_PREPARED = "structure_prepared"
    LIGAND_RESOLVED = "ligand_resolved"
    ARTIFACTS_INVENTORIED = "artifacts_inventoried"

    # Simulation
    SIMULATION_QUEUED = "simulation_queued"
    SIMULATION_RUNNING = "simulation_running"
    SIMULATION_COMPLETE = "simulation_complete"

    # Analysis
    ANALYSIS_COMPLETE = "analysis_complete"
    DOCKING_COMPLETE = "docking_complete"

    # Failure/terminal
    FAILED = "failed"
    DEGRADED = "degraded"

    @classmethod
    def terminal_phases(cls) -> frozenset:
        return frozenset({
            cls.SIMULATION_COMPLETE, cls.ANALYSIS_COMPLETE,
            cls.DOCKING_COMPLETE, cls.FAILED, cls.DEGRADED,
        })

    @classmethod
    def all_phases(cls) -> list:
        return [
            cls.STRUCTURE_PREPARED, cls.LIGAND_RESOLVED, cls.ARTIFACTS_INVENTORIED,
            cls.SIMULATION_QUEUED, cls.SIMULATION_RUNNING, cls.SIMULATION_COMPLETE,
            cls.ANALYSIS_COMPLETE, cls.DOCKING_COMPLETE, cls.FAILED, cls.DEGRADED,
        ]


# ---------------------------------------------------------------------------
# PhaseTransitionEvent (P1-05 full)
# ---------------------------------------------------------------------------

@dataclass
class PhaseTransitionEvent:
    """
    Typed return from specialist drivers (P1-05).
    Drivers return this instead of raw dicts.
    """
    phase: str           # use Phase.* constants
    workflow_id: str
    driver_id: str
    artifacts: list = field(default_factory=list)
    quality_signals: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    degradation_notice: str = ""
    # P1-05 additions
    run_id: str = ""
    parent_event_id: str = ""
    next_phase: str = ""
    elapsed_seconds: float = 0.0
    error_detail: str = ""

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def is_terminal(self) -> bool:
        """Return True when phase is a terminal phase (no further transitions expected)."""
        return self.phase in Phase.terminal_phases()

    def is_failed(self) -> bool:
        """Return True when phase is FAILED or DEGRADED."""
        return self.phase in (Phase.FAILED, Phase.DEGRADED)

    def has_degradation(self) -> bool:
        """True if a degradation notice is present."""
        return bool(self.degradation_notice)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list:
        """
        Return a list of violation strings.  Empty list means the event is valid.
        """
        violations: list = []
        if not self.phase:
            violations.append("phase must be a non-empty string")
        if not self.workflow_id:
            violations.append("workflow_id must be non-empty")
        if not self.driver_id:
            violations.append("driver_id must be non-empty")
        if self.is_terminal() and not self.is_failed() and len(self.artifacts) == 0:
            violations.append("terminal phase has no artifacts")
        return violations

    # ------------------------------------------------------------------
    # Artifact helpers
    # ------------------------------------------------------------------

    def artifact_by_type(self, artifact_type: str):
        """Return the first artifact dict whose 'type' key matches *artifact_type*, or None."""
        for art in self.artifacts:
            if isinstance(art, dict) and art.get("type") == artifact_type:
                return art
        return None

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "workflow_id": self.workflow_id,
            "driver_id": self.driver_id,
            "artifacts": list(self.artifacts),
            "quality_signals": dict(self.quality_signals),
            "timestamp": self.timestamp.isoformat(),
            "degradation_notice": self.degradation_notice,
            "run_id": self.run_id,
            "parent_event_id": self.parent_event_id,
            "next_phase": self.next_phase,
            "elapsed_seconds": self.elapsed_seconds,
            "error_detail": self.error_detail,
        }

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_driver_result(
        cls,
        driver_id: str,
        workflow_id: str,
        phase: str,
        result: dict,
    ) -> "PhaseTransitionEvent":
        """Construct a PhaseTransitionEvent from a raw driver result dict."""
        return cls(
            phase=phase,
            workflow_id=workflow_id,
            driver_id=driver_id,
            artifacts=result.get("artifacts", []),
            quality_signals=result.get("quality_signals", result.get("metrics", {})),
            degradation_notice=result.get("degradation_notice", ""),
            run_id=result.get("run_id", result.get("job_id", "")),
            error_detail=result.get("error", result.get("error_detail", "")),
        )


# ---------------------------------------------------------------------------
# DockingHandoffPayload (P1-07)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ArtifactDescriptor (P2-04 dependency — added by Team Beta)
# ---------------------------------------------------------------------------

@dataclass
class ArtifactDescriptor:
    """Lightweight descriptor for a produced artifact, used by NRU gateway."""
    artifact_type: str
    value: Any = None
    path: str = ""
    source_tool_call_id: str = ""
    quality_score: float = 1.0

    def is_verified(self) -> bool:
        """Return True if quality_score >= 0.7 (matches NRUStatus.VERIFIED threshold)."""
        return self.quality_score >= 0.7

    def to_dict(self) -> dict:
        """Return a serializable dict representation."""
        return {
            "artifact_type": self.artifact_type,
            "value": self.value,
            "path": self.path,
            "source_tool_call_id": self.source_tool_call_id,
            "quality_score": self.quality_score,
        }


import warnings as _warnings  # noqa: E402  (inline to keep stdlib-only constraint visible)


@dataclass
class DockingHandoffPayload:
    """
    Typed schema for the Alchemist driver → BioDynamo driver handoff (P1-07).
    Replaces raw dict passing between specialists.
    """
    workflow_id: str
    source_driver_id: str
    target_driver_id: str
    protein_pdb_path: str
    docked_ligand_pdb_path: str
    ligand_smiles: str
    docking_score_kcal_mol: float = 0.0
    forcefield_hint: str = ""
    production_ns: float = 50.0
    provenance: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if not self.workflow_id:
            raise DriverContractViolation("DockingHandoffPayload", "workflow_id is required")
        if not self.protein_pdb_path:
            raise DriverContractViolation("DockingHandoffPayload", "protein_pdb_path is required")
        if not self.ligand_smiles:
            raise DriverContractViolation("DockingHandoffPayload", "ligand_smiles is required")
        if self.ligand_smiles and not self.docked_ligand_pdb_path:
            _warnings.warn(
                "DockingHandoffPayload: ligand_smiles is set but docked_ligand_pdb_path is empty "
                "(protein-only mode)",
                UserWarning,
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def is_complete(self) -> bool:
        """True when all three core fields (protein, ligand SMILES, docked pose) are non-empty."""
        return bool(self.protein_pdb_path and self.ligand_smiles and self.docked_ligand_pdb_path)

    def has_docked_pose(self) -> bool:
        """True when a docked-pose PDB path is present."""
        return bool(self.docked_ligand_pdb_path)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "source_driver_id": self.source_driver_id,
            "target_driver_id": self.target_driver_id,
            "protein_pdb_path": self.protein_pdb_path,
            "docked_ligand_pdb_path": self.docked_ligand_pdb_path,
            "ligand_smiles": self.ligand_smiles,
            "docking_score_kcal_mol": self.docking_score_kcal_mol,
            "forcefield_hint": self.forcefield_hint,
            "production_ns": self.production_ns,
            "provenance": dict(self.provenance),
            "timestamp": self.timestamp.isoformat(),
        }

    def to_biodynamo_context(self) -> dict:
        """Return the dict a BioDynamoDriver expects as its *context* argument."""
        return {
            "workflow_id": self.workflow_id,
            "ligand_smiles": self.ligand_smiles,
            "protein_pdb": self.protein_pdb_path,
            "docked_ligand_pdb": self.docked_ligand_pdb_path,
            "production_ns": self.production_ns,
            "forcefield": self.forcefield_hint or "charmm36m",
        }

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_alchemist_result(
        cls,
        workflow_id: str,
        result: dict,
    ) -> "DockingHandoffPayload":
        """
        Construct from a raw Alchemist driver result dict.

        Required keys: ``protein_pdb`` (or ``receptor_pdb``), ``smiles``.
        Optional:      ``docked_pose`` / ``docked_pdb``, ``score``, ``forcefield``.
        """
        protein = result.get("protein_pdb", result.get("receptor_pdb", ""))
        if not protein:
            raise DriverContractViolation(
                "AlchemistDriver",
                "missing protein_pdb in result",
            )
        docked = result.get("docked_pose", result.get("docked_pdb", ""))
        return cls(
            workflow_id=workflow_id,
            source_driver_id="AlchemistDriver",
            target_driver_id="BioDynamoDriver",
            protein_pdb_path=protein,
            docked_ligand_pdb_path=docked,
            ligand_smiles=result.get("smiles", ""),
            docking_score_kcal_mol=float(result.get("score", 0.0)),
            forcefield_hint=result.get("forcefield", ""),
        )

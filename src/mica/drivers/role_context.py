"""Role embodiment data types for Unified Cognitive Substrate (UCS).

Instead of ``_spawn_agent()`` creating isolated sub-loops that lose access to
the parent's cognitive infrastructure, embodied roles share the same nervous
system (EvidenceLedger, BioRouter, full tool bus) while maintaining independent
context windows per role.

Thermodynamic integration: roles have stratified negative memory (tombstone
visibility), rupture energy absorption from amputated branches, and
programmable heresy permits for exploratory roles.

See: MICAV4DOCS/specs/EMBODY_ROLE_TECHNICAL_SPEC_2026-03-16.md
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple


# ---------------------------------------------------------------------------
# RuptureBudget — energy released by violent branch amputation
# ---------------------------------------------------------------------------
@dataclass
class RuptureBudget:
    """Energy budget released when a previous branch was violently amputated.

    When the Critic destroys a reasoning branch, that destruction releases
    energy that the next embodied role can absorb as exploratory fuel:

    - ``temperature_bonus`` added to effective temperature (capped at 0.95)
    - ``extra_iterations`` added to max_iterations
    - ``appeal_priority`` raises this role's priority for heretical appeals
    """

    released_energy: float = 0.0
    temperature_bonus: float = 0.0
    extra_iterations: int = 0
    appeal_priority: int = 0


# ---------------------------------------------------------------------------
# InvariantCheck — output contract per role
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InvariantCheck:
    """A single output invariant that an embodied role must satisfy.

    Parameters
    ----------
    name : str
        Short identifier (e.g. ``"required_sections"``).
    check : callable
        ``(synthesis_text: str, context: RoleContext) -> bool``.
        Returns True when the invariant passes.
    severity : ``"warning"`` | ``"error"``
        ``"error"`` blocks acceptance; ``"warning"`` is logged.
    description : str
        Human-readable explanation.
    """

    name: str
    check: Callable[..., bool]
    severity: str = "warning"
    description: str = ""


# ---------------------------------------------------------------------------
# RoleSpec — immutable role blueprint
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoleModelProfile:
    """Explicit model-selection contract for an embodied role."""

    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    provider_family: Optional[str] = None
    inherit_parent: bool = False
    notes: str = ""


@dataclass(frozen=True)
class RoleSpec:
    """Immutable blueprint for an embodied role.

    Tools are NOT restricted here by default — all roles access the universal
    bus.  Specialization comes from *system_prompt* and *output_invariants*.
    """

    role_id: str
    system_prompt: str
    max_iterations: int = 8
    temperature: float = 0.3
    temperature_override: Optional[float] = None  # force T regardless of BioRouter
    output_invariants: Tuple[InvariantCheck, ...] = ()
    model_profile: Optional[RoleModelProfile] = None

    # --- Thermodynamic integration: stratified negative memory ---
    # "full"       → sees operational + archaeological + heretical tombstones
    # "semi_blind" → sees only archaeological (historical risk zones)
    # "blind"      → sees NO tombstones (Musa-class exploratory freedom)
    negative_memory_mode: str = "full"
    # Which tombstone classes this role can see (overrides mode-derived defaults)
    visible_tombstone_classes: FrozenSet[str] = frozenset({"operational"})
    # Whether the role can appeal heretical tombstones (re-explore the forbidden)
    allow_appeal_regime: bool = False


# ---------------------------------------------------------------------------
# RoleContext — mutable per-role state (session-scoped)
# ---------------------------------------------------------------------------
@dataclass
class RoleContext:
    """Mutable per-role state maintained across embodiments within a session.

    Each role gets its OWN context window — messages are NOT shared between
    roles.  The context persists: if bibliotecario is embodied twice in one
    session, the second embodiment can access the first's accumulated output.
    """

    role_id: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    accumulated_text: List[str] = field(default_factory=list)
    citations_log: List[Dict[str, Any]] = field(default_factory=list)
    gaps_log: List[Dict[str, Any]] = field(default_factory=list)
    review_issues: List[Dict[str, Any]] = field(default_factory=list)
    pending_ledger_entries: List[Dict[str, Any]] = field(default_factory=list)

    # --- Thermodynamic integration: tombstones + rupture ---
    # Tombstones inherited from parent (soft warnings injected into messages)
    inherited_tombstones: List[Dict[str, Any]] = field(default_factory=list)
    # Tombstones emitted by THIS role (via flag_tombstone tool)
    emitted_tombstones: List[Dict[str, Any]] = field(default_factory=list)
    # Whether this role is currently in appeal regime (can contest heretical tombstones)
    appeal_regime_active: bool = False
    # Rupture budget applied to this embodiment (absorbed from branch death)
    applied_rupture_budget: Optional[RuptureBudget] = None

    tool_calls_count: int = 0
    iterations_count: int = 0
    last_synthesis: str = ""
    embodiment_count: int = 0


def resolve_role_model_profile(
    profile: Optional[RoleModelProfile],
    *,
    parent_provider_id: str,
    parent_model_id: Optional[str],
    default_model_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve the effective provider/model tuple for a role profile."""

    resolved_provider = parent_provider_id
    resolved_model = parent_model_id

    if profile is None:
        return resolved_provider, resolved_model

    if profile.inherit_parent:
        resolved_provider = parent_provider_id
        resolved_model = parent_model_id

    if profile.provider_id:
        resolved_provider = profile.provider_id
    if profile.model_id:
        resolved_model = profile.model_id

    if not resolved_model and default_model_lookup is not None:
        resolved_model = default_model_lookup(resolved_provider)

    return resolved_provider, resolved_model


# ---------------------------------------------------------------------------
# Pre-defined output invariants
# ---------------------------------------------------------------------------

BIBLIOTECARIO_INVARIANTS: Tuple[InvariantCheck, ...] = (
    InvariantCheck(
        name="required_sections",
        check=lambda text, ctx: all(
            s in text for s in ["[KEY FINDINGS]", "[CONTRADICTIONS]", "[OPEN GAPS]"]
        ),
        severity="error",
        description="Synthesis must contain [KEY FINDINGS], [CONTRADICTIONS], [OPEN GAPS]",
    ),
    InvariantCheck(
        name="citation_density",
        check=lambda text, ctx: len(ctx.citations_log) >= 3,
        severity="warning",
        description="At least 3 citations expected from bibliotecario",
    ),
    InvariantCheck(
        name="max_tokens",
        check=lambda text, ctx: len(text.split()) <= 800,
        severity="warning",
        description="Synthesis should be <= ~600 tokens",
    ),
)

REVIEWER_INVARIANTS: Tuple[InvariantCheck, ...] = (
    InvariantCheck(
        name="verdict_present",
        check=lambda text, ctx: any(
            v in text.upper()
            for v in ["ACCEPT", "MAJOR_REVISION", "MINOR_REVISION", "REJECT"]
        ),
        severity="error",
        description="Review must end with explicit verdict",
    ),
    InvariantCheck(
        name="issues_flagged",
        check=lambda text, ctx: (
            len(ctx.review_issues) >= 1 or "ACCEPT" in text.upper()
        ),
        severity="warning",
        description="Non-ACCEPT verdict requires at least one flagged issue",
    ),
)

EXPERT_INVARIANTS: Tuple[InvariantCheck, ...] = (
    InvariantCheck(
        name="substantive_response",
        check=lambda text, ctx: len(text.split()) >= 30,
        severity="error",
        description="Expert response must be at least ~30 words (non-trivial)",
    ),
    InvariantCheck(
        name="max_tokens",
        check=lambda text, ctx: len(text.split()) <= 600,
        severity="warning",
        description="Expert response should be <= ~400 tokens (dense, no filler)",
    ),
)


# ---------------------------------------------------------------------------
# Tombstone warning formatting (for message injection)
# ---------------------------------------------------------------------------

def format_tombstone_warnings(
    tombstones: List[Dict[str, Any]],
    classes: FrozenSet[str],
) -> str:
    """Format tombstones visible to a role into a warning block.

    Only tombstones whose class is in *classes* are included.  Returns empty
    string if no visible tombstones exist.
    """
    if not tombstones or not classes:
        return ""
    # Lazy import — core.py may not always be available in test stubs
    try:
        from mica.agentic.core import normalize_tombstone_class
    except ImportError:
        def normalize_tombstone_class(t: Dict[str, Any]) -> str:
            return str(t.get("tombstone_class") or t.get("class") or "operational").strip().lower()
    visible = [t for t in tombstones if normalize_tombstone_class(t) in classes]
    if not visible:
        return ""
    lines: List[str] = []
    for t in visible:
        tc = normalize_tombstone_class(t)
        reason = str(t.get("reason") or t.get("critique_summary") or "no reason given")
        target = str(t.get("target_id") or t.get("claim_id") or "unknown")
        lines.append(f"- [{tc.upper()}] {target}: {reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

def is_embodiment_enabled(role_id: str) -> bool:
    """Check if embodiment is enabled for a given role via env var.

    Convention: ``MICA_EMBODY_{ROLE_ID_UPPER}=1`` enables embodiment.
    Example: ``MICA_EMBODY_BIBLIOTECARIO=1``
    """
    return os.environ.get(f"MICA_EMBODY_{role_id.upper()}", "0") == "1"

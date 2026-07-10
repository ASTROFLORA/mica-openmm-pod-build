"""Runtime skill resolution for AgenticDriver.

Resolves which MICA skills (context injection documents) should be active
for a given query.  This module is the canonical owner of ``resolve_runtime_skills``
which is imported by ``AgenticDriver`` at module load time.

The current implementation is a no-op stub that returns an empty plan so
that AgenticDriver loads and executes normally.  Full skill-routing logic
can be added here without touching the driver.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class RuntimeSkillPlan:
    """Represents the skill-injection plan for a single AgenticDriver turn.

    Attributes
    ----------
    skills:
        Mapping of ``skill_id -> skill_metadata`` for skills selected for
        this turn.  Empty mapping means no skill documents are injected.
    """

    def __init__(self, skills: Optional[Dict[str, Any]] = None) -> None:
        self.skills: Dict[str, Any] = skills or {}
        self.prompt_block: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation of the plan."""
        return {"skills": self.skills}

    def __bool__(self) -> bool:
        return bool(self.skills)

    def __repr__(self) -> str:  # pragma: no cover
        return f"RuntimeSkillPlan(skills={list(self.skills.keys())})"


def resolve_runtime_skills(
    *,
    query: str,
    visible_tool_names: List[str],
    explicit_skill_ids: Optional[List[str]] = None,
    include_tier_2: bool = False,
    disable_auto_skills: bool = False,
) -> RuntimeSkillPlan:
    """Resolve which MICA skills should be active for *query*.

    Parameters
    ----------
    query:
        The user's natural-language query.
    visible_tool_names:
        Tool names currently exposed to the agent on this turn.
    explicit_skill_ids:
        Skill IDs explicitly requested by the caller (override auto-selection).
    include_tier_2:
        When True, include lower-priority (tier-2) skills in the plan.
    disable_auto_skills:
        When True, suppress automatic skill selection even if explicit IDs are
        empty — returns an empty plan regardless of query content.

    Returns
    -------
    RuntimeSkillPlan
        The skill-injection plan.  An empty plan is always safe — the driver
        works without any injected skill documents.
    """
    # Stub: no auto-resolution yet.  Returns empty plan.
    return RuntimeSkillPlan()

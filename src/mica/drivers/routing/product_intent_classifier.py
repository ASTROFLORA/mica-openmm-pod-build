"""APV-11 Product intent classifier — product-before-scientific routing.

Authority: North Star V0.6 §8 / APV-11
Hard gate: interview benchmark obeys no-tool / no-mutation.

Consumes: EffectiveContext (optional enrichment)
Does not own: ToolKG scientific IntentClassifier, RouteCardService scientific lanes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ProductIntent = Literal[
    "navigation",
    "knowledge_discovery",
    "workspace_composition",
    "study_work",
    "governance",
    "compute",
    "artifact_management",
    "migration",
    "administration",
    "product_interview",
    "scientific_deferred",
]

ExecutionModeHint = Literal["narrative_only", "tool", "protocol", "gog"]


@dataclass(frozen=True)
class DriverConstraints:
    no_tool: bool = False
    no_mutation: bool = False
    read_only: bool = False
    max_tool_calls: int | None = None
    interview: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProductIntentDecision:
    product_intent: ProductIntent
    constraints: DriverConstraints
    confidence: float
    reasons: tuple[str, ...] = ()
    execution_mode_hint: ExecutionModeHint = "tool"
    product_lane_class: str = "product_general"

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_intent": self.product_intent,
            "constraints": self.constraints.to_dict(),
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "execution_mode_hint": self.execution_mode_hint,
            "product_lane_class": self.product_lane_class,
        }


def parse_driver_constraints(query: str) -> DriverConstraints:
    folded = str(query or "").casefold()
    no_tool = any(
        marker in folded
        for marker in (
            "no tool calls",
            "no tools",
            "without tools",
            "zero tool",
            "do not call tools",
            "don't call tools",
            "no_tool",
            "narrative only",
            "interview only",
        )
    )
    no_mutation = any(
        marker in folded
        for marker in (
            "no mutations",
            "no mutation",
            "without mutations",
            "read only",
            "read-only",
            "no_mutation",
            "do not mutate",
            "don't mutate",
        )
    )
    interview = any(
        marker in folded
        for marker in (
            "product operator interview",
            "product interview",
            "operator interview",
            "eight-scene journey",
            "eight scene journey",
        )
    )
    if interview:
        # Interview defaults to narrative obedience unless explicitly overridden.
        no_tool = True
        no_mutation = True
    read_only = no_mutation or ("read only" in folded or "read-only" in folded)
    max_tool_calls = 0 if no_tool else None
    return DriverConstraints(
        no_tool=no_tool,
        no_mutation=no_mutation,
        read_only=read_only,
        max_tool_calls=max_tool_calls,
        interview=interview,
    )


def classify_product_intent(
    query: str,
    *,
    effective_context: Any | None = None,
) -> ProductIntentDecision:
    """Classify product intent. Product matches win over scientific deferral."""
    folded = str(query or "").casefold().strip()
    constraints = parse_driver_constraints(folded)
    reasons: list[str] = []

    if constraints.interview or (
        "product operator" in folded and ("interview" in folded or "journey" in folded)
    ):
        reasons.append("product_interview_markers")
        if constraints.no_tool:
            reasons.append("explicit_no_tool")
        if constraints.no_mutation:
            reasons.append("explicit_no_mutation")
        return ProductIntentDecision(
            product_intent="product_interview",
            constraints=constraints,
            confidence=0.95,
            reasons=tuple(reasons),
            execution_mode_hint="narrative_only",
            product_lane_class="product_interview",
        )

    if constraints.no_tool and constraints.no_mutation:
        reasons.append("explicit_no_tool_and_no_mutation")
        return ProductIntentDecision(
            product_intent="navigation",
            constraints=constraints,
            confidence=0.85,
            reasons=tuple(reasons),
            execution_mode_hint="narrative_only",
            product_lane_class="product_constrained",
        )

    if any(
        m in folded
        for m in ("propose to lab", "governance", "approval pause", "governancecase", "personal-to-lab")
    ):
        reasons.append("governance_markers")
        return ProductIntentDecision(
            product_intent="governance",
            constraints=constraints,
            confidence=0.8,
            reasons=tuple(reasons),
            execution_mode_hint="tool" if not constraints.no_tool else "narrative_only",
            product_lane_class="product_governance",
        )

    if any(
        m in folded
        for m in ("working set", "workspace", "surface", "fullscreen", "semantic view", "gridstack")
    ):
        reasons.append("workspace_composition_markers")
        return ProductIntentDecision(
            product_intent="workspace_composition",
            constraints=constraints,
            confidence=0.75,
            reasons=tuple(reasons),
            execution_mode_hint="tool" if not constraints.no_tool else "narrative_only",
            product_lane_class="product_workspace",
        )

    if any(m in folded for m in ("artifact membership", "promote artifact", "signed url", "staging blob")):
        reasons.append("artifact_management_markers")
        return ProductIntentDecision(
            product_intent="artifact_management",
            constraints=constraints,
            confidence=0.75,
            reasons=tuple(reasons),
            execution_mode_hint="tool" if not constraints.no_tool else "narrative_only",
            product_lane_class="product_artifacts",
        )

    if any(m in folded for m in ("start study", "study closure", "investigation line", "study step")):
        reasons.append("study_work_markers")
        return ProductIntentDecision(
            product_intent="study_work",
            constraints=constraints,
            confidence=0.7,
            reasons=tuple(reasons),
            execution_mode_hint="protocol" if not constraints.no_tool else "narrative_only",
            product_lane_class="product_study",
        )

    if any(m in folded for m in ("bibliotecario", "seed paper", "knowledge discover", "deep research")):
        reasons.append("knowledge_discovery_markers")
        return ProductIntentDecision(
            product_intent="knowledge_discovery",
            constraints=constraints,
            confidence=0.7,
            reasons=tuple(reasons),
            execution_mode_hint="protocol" if not constraints.no_tool else "narrative_only",
            product_lane_class="product_knowledge",
        )

    if any(m in folded for m in ("serverless", "compute job", "runpod", "salad", "provider cost")):
        reasons.append("compute_markers")
        return ProductIntentDecision(
            product_intent="compute",
            constraints=constraints,
            confidence=0.7,
            reasons=tuple(reasons),
            execution_mode_hint="protocol" if not constraints.no_tool else "narrative_only",
            product_lane_class="product_compute",
        )

    _ = effective_context  # reserved for scope-aware enrichment
    reasons.append("scientific_deferred_default")
    return ProductIntentDecision(
        product_intent="scientific_deferred",
        constraints=constraints,
        confidence=0.4,
        reasons=tuple(reasons),
        execution_mode_hint="tool",
        product_lane_class="scientific_deferred",
    )


__all__ = [
    "DriverConstraints",
    "ExecutionModeHint",
    "ProductIntent",
    "ProductIntentDecision",
    "classify_product_intent",
    "parse_driver_constraints",
]

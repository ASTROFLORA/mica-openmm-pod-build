"""Proposal/reasoning adapter registry for PAU-10 governance.

This registry is deliberately separate from ``protocol_producer_registry``.
Chronoracle and MSRP may produce reasoning artifacts or protocol proposals, but
they are not compiler ingress points and cannot execute or mutate protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProposalAdapterStatus = Literal[
    "implemented",
    "partial",
    "registered_but_blocked",
    "design_only",
    "stale_missing",
]

ProposalKind = Literal[
    "hypothesis_generation",
    "scientific_reasoning_chain",
    "peer_review_feedback",
]

ProposalOutputContract = Literal[
    "reasoning_artifact",
    "proposal_artifact",
    "review_artifact",
]

PromotionBoundary = Literal[
    "proposal_artifact_review_gate_to_validated_protocol_jsonld_with_receipt",
    "not_promotable_without_companion_compiler",
]


@dataclass(frozen=True)
class ProtocolProposalAdapterDescriptor:
    adapter_id: str
    source_path: str
    module_path: str
    callable_name: str
    status: ProposalAdapterStatus
    proposal_kind: ProposalKind
    output_contract: ProposalOutputContract
    promotion_boundary: PromotionBoundary
    authority_class: str = "proposal_reasoning_adapter"
    executes_protocol: bool = False
    mutates_protocol_state: bool = False
    emits_executable_protocol: bool = False
    notes: str = ""


PROTOCOL_PROPOSAL_ADAPTERS: tuple[ProtocolProposalAdapterDescriptor, ...] = (
    ProtocolProposalAdapterDescriptor(
        adapter_id="chronoracle_reason_about_protein",
        source_path="src/bsm/mica_lineage/ai_reasoning/chronoracle_client.py",
        module_path="bsm.mica_lineage.ai_reasoning.chronoracle_client",
        callable_name="ChronoracleClient.reason_about_protein",
        status="partial",
        proposal_kind="hypothesis_generation",
        output_contract="reasoning_artifact",
        promotion_boundary=(
            "proposal_artifact_review_gate_to_validated_protocol_jsonld_with_receipt"
        ),
        notes=(
            "Returns ChronoracleResponse hypotheses, evidence, confidence, and next "
            "experiments. It is not a ProtocolJSONLD compiler or executor."
        ),
    ),
    ProtocolProposalAdapterDescriptor(
        adapter_id="msrp_reasoning_chain",
        source_path="src/mica/scientific/msrp_core.py",
        module_path="mica.scientific.msrp_core",
        callable_name="MSRPReasoningEngine.create_thinking_chain",
        status="implemented",
        proposal_kind="scientific_reasoning_chain",
        output_contract="reasoning_artifact",
        promotion_boundary=(
            "proposal_artifact_review_gate_to_validated_protocol_jsonld_with_receipt"
        ),
        notes=(
            "Creates MSRPThinkingChain reasoning artifacts. A companion compiler "
            "must validate and receipt any future protocol lowering."
        ),
    ),
    ProtocolProposalAdapterDescriptor(
        adapter_id="msrp_pressure_feedback",
        source_path="src/mica/scientific_workflow/peer_review.py",
        module_path="mica.scientific_workflow.peer_review",
        callable_name="MSRPPressureEngine.generate_peer_feedback",
        status="implemented",
        proposal_kind="peer_review_feedback",
        output_contract="review_artifact",
        promotion_boundary="not_promotable_without_companion_compiler",
        notes=(
            "Produces peer-review pressure feedback. Feedback can inform protocol "
            "changes but cannot itself become executable protocol state."
        ),
    ),
)


def list_protocol_proposal_adapters() -> tuple[ProtocolProposalAdapterDescriptor, ...]:
    return PROTOCOL_PROPOSAL_ADAPTERS


def get_protocol_proposal_adapter(
    adapter_id: str,
) -> ProtocolProposalAdapterDescriptor:
    for descriptor in PROTOCOL_PROPOSAL_ADAPTERS:
        if descriptor.adapter_id == adapter_id:
            return descriptor
    raise KeyError(f"Unknown protocol proposal adapter: {adapter_id}")


def assert_non_executing_proposal_adapter(
    descriptor: ProtocolProposalAdapterDescriptor,
) -> None:
    if descriptor.authority_class != "proposal_reasoning_adapter":
        raise ValueError(f"{descriptor.adapter_id} has invalid authority class")
    if descriptor.executes_protocol:
        raise ValueError(f"{descriptor.adapter_id} cannot execute protocols")
    if descriptor.mutates_protocol_state:
        raise ValueError(f"{descriptor.adapter_id} cannot mutate protocol state")
    if descriptor.emits_executable_protocol:
        raise ValueError(f"{descriptor.adapter_id} cannot emit executable protocols")


__all__ = [
    "PROTOCOL_PROPOSAL_ADAPTERS",
    "ProtocolProposalAdapterDescriptor",
    "assert_non_executing_proposal_adapter",
    "get_protocol_proposal_adapter",
    "list_protocol_proposal_adapters",
]

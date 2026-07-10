"""Governed proposal compiler and promotion gate for MSRP and Chronoracle proposals.

This module implements the promotion path:
proposal_artifact -> review/gate -> validated_protocol_jsonld -> receipt
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from bsm.mica_lineage.ai_reasoning.chronoracle_client import ChronoracleResponse
from mica.scientific.msrp_core import MSRPThinkingChain
from mica_q.protocol_jsonld_contract import (
    ProtocolApprovalMode,
    ProtocolApprovalPolicy,
    ProtocolBudgetPolicy,
    ProtocolEdge,
    ProtocolEdgeType,
    ProtocolJSONLDDocument,
    ProtocolLedgerMode,
    ProtocolLedgerPolicy,
    ProtocolNode,
    ProtocolNodePolicies,
    ProtocolReceiptSchema,
)
from mica_q.protocol_jsonld_validator import validate_protocol_jsonld


class ProtocolPromotionReceipt(BaseModel):
    receipt_id: str
    protocol_id: str
    source_proposal_id: str
    proposal_kind: str
    decision: str  # "approved" or "rejected"
    reason: str
    promoted_at: str
    promoted_by: str = "protocol_proposal_compiler"
    validation_debug: dict[str, Any] = Field(default_factory=dict)


class ProposalPromotionError(ValueError):
    def __init__(self, message: str, receipt: ProtocolPromotionReceipt):
        super().__init__(message)
        self.receipt = receipt


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def promote_proposal_to_protocol(
    proposal: Any,
    approver: str = "governance-gate",
    force_approve: bool = False,
    confidence_threshold: float = 0.7,
) -> tuple[ProtocolJSONLDDocument, ProtocolPromotionReceipt]:
    """Compile a Chronoracle or MSRP proposal into a validated ProtocolJSONLDDocument via a governed gate."""

    # 1. Detect proposal kind and extract IDs
    chain_id = _get_val(proposal, "chain_id")
    query_id = _get_val(proposal, "query_id")

    if chain_id is not None or _get_val(proposal, "phase_1_decomposition") is not None:
        proposal_kind = "scientific_reasoning_chain"
        source_proposal_id = str(chain_id or uuid.uuid4())
    elif query_id is not None or _get_val(proposal, "confidence_score") is not None:
        proposal_kind = "hypothesis_generation"
        source_proposal_id = str(query_id or uuid.uuid4())
    else:
        # Fallback detection
        proposal_kind = "unknown"
        source_proposal_id = str(uuid.uuid4())

    promoted_at = datetime.now(timezone.utc).isoformat()
    receipt_id = f"receipt-promotion-{uuid.uuid4()}"

    # Generate unique protocol ID
    protocol_id = f"protocol-from-{proposal_kind}-{source_proposal_id}"

    # 2. Evaluate review gate
    validation_errors: list[str] = []

    if proposal_kind == "scientific_reasoning_chain":
        # Check MSRP Thinking Chain
        is_complete = True
        for phase_name in [
            "phase_1_decomposition",
            "phase_2_hypothesis_generation",
            "phase_3_evidence_evaluation",
            "phase_4_alternative_consideration",
            "phase_5_uncertainty_quantification",
        ]:
            if _get_val(proposal, phase_name) is None:
                is_complete = False
                validation_errors.append(f"MSRP thinking chain phase {phase_name} is missing or incomplete.")

        # If it's an object with validate_chain, call it
        if hasattr(proposal, "validate_chain") and callable(proposal.validate_chain):
            ok, errors = proposal.validate_chain()
            if not ok:
                validation_errors.extend(errors)
        else:
            # Basic validation of research question
            rq = _get_val(proposal, "research_question")
            if not rq or not str(rq).strip():
                validation_errors.append("MSRP thinking chain has no research question.")

    elif proposal_kind == "hypothesis_generation":
        # Check Chronoracle
        conf = _get_val(proposal, "confidence_score")
        if conf is None:
            validation_errors.append("Chronoracle response is missing confidence_score.")
        elif float(conf) < confidence_threshold:
            validation_errors.append(
                f"Chronoracle confidence_score {conf} is below the threshold of {confidence_threshold}."
            )

        hyp = _get_val(proposal, "hypothesis")
        if not hyp or not str(hyp).strip():
            validation_errors.append("Chronoracle response has no hypothesis.")

        experiments = _get_val(proposal, "next_experiments")
        if not experiments or not isinstance(experiments, list):
            validation_errors.append("Chronoracle response is missing next_experiments list.")
    else:
        validation_errors.append("Unsupported proposal kind or layout.")

    # 3. Create receipt and decide
    decision = "approved"
    reason = "Proposal successfully passed the governed review gate."

    if validation_errors:
        if force_approve:
            reason = "Proposal failed validation but was force-approved by user."
        else:
            decision = "rejected"
            reason = f"Proposal failed governed review gate check with errors: {'; '.join(validation_errors)}"

    receipt = ProtocolPromotionReceipt(
        receipt_id=receipt_id,
        protocol_id=protocol_id,
        source_proposal_id=source_proposal_id,
        proposal_kind=proposal_kind,
        decision=decision,
        reason=reason,
        promoted_at=promoted_at,
        promoted_by=approver,
        validation_debug={"errors": validation_errors, "force_approved": force_approve},
    )

    if decision == "rejected":
        raise ProposalPromotionError(reason, receipt)

    # 4. Lower proposal to ProtocolJSONLDDocument
    nodes: list[ProtocolNode] = []
    experiment_names: list[str] = []

    if proposal_kind == "scientific_reasoning_chain":
        # Extract critical experiments from competing hypotheses and future work needed
        hyp_gen = _get_val(proposal, "phase_2_hypothesis_generation")
        if hyp_gen is not None:
            competing_hypotheses = _get_val(hyp_gen, "competing_hypotheses", [])
            for hyp in competing_hypotheses:
                critical_exps = _get_val(hyp, "critical_experiments", [])
                for ce in critical_exps:
                    if ce and isinstance(ce, str) and ce not in experiment_names:
                        experiment_names.append(ce)

        uq = _get_val(proposal, "phase_5_uncertainty_quantification")
        if uq is not None:
            future_work = _get_val(uq, "future_work_needed", [])
            for fw in future_work:
                if fw and isinstance(fw, str) and fw not in experiment_names:
                    experiment_names.append(fw)

        description = str(_get_val(proposal, "research_question", "MSRP Scientific Reasoning Protocol"))

    else:  # hypothesis_generation
        next_exps = _get_val(proposal, "next_experiments", [])
        for ne in next_exps:
            if ne and isinstance(ne, str) and ne not in experiment_names:
                experiment_names.append(ne)

        description = str(_get_val(proposal, "hypothesis", "Chronoracle Hypothesis Protocol"))

    # Generate sequential nodes
    if not experiment_names:
        experiment_names.append("default-verify-hypothesis")

    for index, exp_desc in enumerate(experiment_names):
        # Normalize node ID
        clean_name = "".join(c if c.isalnum() else "-" for c in exp_desc.lower())
        clean_name = "-".join(filter(None, clean_name.split("-")))
        node_id = f"node-{index + 1}-{clean_name[:30]}"

        node = ProtocolNode(
            node_id=node_id,
            node_kind="tool",
            executor_surface="sim_adapter",
            executor_id=f"{proposal_kind}_compiler",
            objective=exp_desc,
            dependencies=[nodes[-1].node_id] if nodes else [],
            inputs={"experiment_description": exp_desc},
            expected_outputs={
                "artifact_refs": [f"protocol://{node_id}/artifacts/output"],
                "receipt_ref": f"protocol://{node_id}/receipts/node",
            },
            evidence_requirements=["node_receipt"],
            policies=ProtocolNodePolicies(protected_surface=False, production_compute=False),
            failure_policy="halt",
            receipt_schema=ProtocolReceiptSchema(),
        )
        nodes.append(node)

    # Generate edges connecting nodes sequentially
    edges: list[ProtocolEdge] = []
    for i in range(len(nodes) - 1):
        edge = ProtocolEdge(
            source_node_id=nodes[i].node_id,
            target_node_id=nodes[i+1].node_id,
            edge_type=ProtocolEdgeType.CONTROL_DEPENDENCY,
            rationale=f"Sequential flow from {nodes[i].node_id} to {nodes[i+1].node_id}",
        )
        edges.append(edge)

    # Construct the JSON-LD document
    document = ProtocolJSONLDDocument(
        **{
            "@context": "https://mica.ai/protocol/v1",
            "@type": "MICAProtocol",
        },
        protocol_id=protocol_id,
        version="1.0.0",
        session_id=str(uuid.uuid4()),
        owner_lab="Scientific Protocol / L-07",
        execution_mode="development",
        risk_profile="medium",
        budgets=ProtocolBudgetPolicy(
            max_steps=len(nodes),
            max_wall_clock_s=3600,
            max_tool_calls=len(nodes) * 2,
        ),
        approval_policy=ProtocolApprovalPolicy(
            mode=ProtocolApprovalMode.AUTO,
            required_approvers=[],
            protected_surfaces=[],
        ),
        ledger_policy=ProtocolLedgerPolicy(
            mode=ProtocolLedgerMode.PROTOCOL_AND_NODE_RECEIPTS,
            receipt_schema="mica.receipts.node.v1",
            emit_events=True,
            require_node_receipts=True,
            require_durable_lineage=False,
        ),
        nodes=nodes,
        edges=edges,
        metadata={
            "compiler_id": "protocol_proposal_compiler",
            "compiler_version": "1.0.0",
            "source_proposal_id": source_proposal_id,
            "proposal_kind": proposal_kind,
            "promoted_at": promoted_at,
            "promoted_by": approver,
            "source_artifact_refs": [f"proposal://{source_proposal_id}"],
            "source_receipt_refs": [],
            "description": description,
        },
    )

    # Validate the generated document
    validated_doc = validate_protocol_jsonld(document)
    return validated_doc, receipt

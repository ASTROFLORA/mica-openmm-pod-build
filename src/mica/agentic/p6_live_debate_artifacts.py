from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, Field

from mica.agentic.debate_mudo_subscriber import DebateMUDOIngestResult, DebateMUDOSubscriber
from mica.agentic.events import MUDOReceiptReady
from mica.agentic.mudo_branch_contracts import MUDOBranchReceipt
from mica.agentic.p6_trigger_evaluator import P6TriggerEvaluationResult, evaluate_p6_triggers


P6_DEBATE_ARTIFACT_SCHEMA_ID = "mica.project_tolomeo.p6.live_debate_artifact.v1"
P6_DEBATE_RECEIPT_SCHEMA_ID = "mica.project_tolomeo.p6.live_debate_receipt.v1"
P6_DEBATE_PIPELINE_RESULT_SCHEMA_ID = "mica.project_tolomeo.p6.live_debate_pipeline_result.v1"

P6_DEBATE_ARTIFACT_KINDS = (
    "peer_review_report",
    "quality_assessment",
    "contradiction_surface",
    "evidence_gap_report",
)

RAW_PAYLOAD_KEYS = {
    "content",
    "raw_payload",
    "raw_output",
    "llm_transcript",
    "provider_payload",
    "raw_provider_payload",
}


class P6DebateBlocker(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class P6DebateOutputInput(BaseModel):
    artifact_kind: Literal[
        "peer_review_report",
        "quality_assessment",
        "contradiction_surface",
        "evidence_gap_report",
    ]
    source_artifact_ref: str
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_size_bytes: int = Field(ge=1)
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    episode_ref: str
    protocol_ref: str
    evidence_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    mudo_id: str | None = None
    claim_refs: tuple[str, ...] = ()
    claim_promotion_requested: bool = False
    graph_write_requested: bool = False
    direct_execution_requested: bool = False
    raw_payload_embedded: bool = False


class P6LiveDebateArtifact(BaseModel):
    schema_id: str = P6_DEBATE_ARTIFACT_SCHEMA_ID
    artifact_ref: str
    artifact_kind: Literal[
        "peer_review_report",
        "quality_assessment",
        "contradiction_surface",
        "evidence_gap_report",
    ]
    source_artifact_ref: str
    content_sha256: str
    content_size_bytes: int
    episode_ref: str
    protocol_ref: str
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    claim_refs: tuple[str, ...] = ()
    claim_status: Literal["unpromoted"] = "unpromoted"
    durability_status: Literal["source_materialized"] = "source_materialized"
    graph_projection_status: Literal["proposal_only"] = "proposal_only"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6LiveDebateReceipt(BaseModel):
    schema_id: str = P6_DEBATE_RECEIPT_SCHEMA_ID
    receipt_ref: str
    receipt_type: Literal["P6LiveDebateReceipt"] = "P6LiveDebateReceipt"
    p6_id: str
    artifact_kind: str
    decision: Literal["materialized", "blocked"]
    reason_codes: tuple[str, ...]
    artifact_ref: str | None = None
    source_artifact_ref: str
    source_receipt_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    episode_ref: str
    protocol_ref: str
    idempotency_key: str
    claim_promotion_performed: bool = False
    graph_write_performed: bool = False
    canonical_mudo_branch_mutated: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P6LiveDebatePipelineResult(BaseModel):
    schema_id: str = P6_DEBATE_PIPELINE_RESULT_SCHEMA_ID
    p6_id: str
    status: Literal["ready", "blocked", "partially_blocked", "duplicate_suppressed"]
    artifacts: tuple[P6LiveDebateArtifact, ...] = ()
    receipts: tuple[P6LiveDebateReceipt, ...] = ()
    mudo_branch_receipts: tuple[MUDOBranchReceipt, ...] = ()
    mudo_ingest_statuses: tuple[str, ...] = ()
    trigger_results: tuple[P6TriggerEvaluationResult, ...] = ()
    blockers: tuple[P6DebateBlocker, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    mudo_branch_receipt_refs: tuple[str, ...] = ()
    proposal_refs: tuple[str, ...] = ()
    materialized_count: int = 0
    blocked_count: int = 0
    duplicate_count: int = 0
    claim_promotions_performed: int = 0
    graph_writes_performed: int = 0
    canonical_mudo_branch_mutations: int = 0
    execution_started: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any, *, length: int = 24) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]


def _contains_raw_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in RAW_PAYLOAD_KEYS or _contains_raw_payload(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_raw_payload(item) for item in value)
    return False


def _as_inputs(raw: Any) -> tuple[tuple[P6DebateOutputInput, bool], ...]:
    values: Iterable[Any]
    if isinstance(raw, Mapping):
        values = (raw,)
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = ()

    parsed: list[tuple[P6DebateOutputInput, bool]] = []
    for value in values:
        if isinstance(value, P6DebateOutputInput):
            parsed.append((value, value.raw_payload_embedded))
        elif isinstance(value, Mapping):
            payload = dict(value)
            parsed.append((P6DebateOutputInput(**payload), _contains_raw_payload(payload)))
        else:
            raise ValueError("P6 live debate outputs must be objects")
    return tuple(parsed)


def _blockers(item: P6DebateOutputInput, *, raw_payload_detected: bool) -> tuple[P6DebateBlocker, ...]:
    blockers: list[P6DebateBlocker] = []
    if not item.source_artifact_ref.startswith("artifact://"):
        blockers.append(P6DebateBlocker(
            code="invalid_source_artifact_ref",
            message="Live debate output requires an already materialized artifact:// source ref.",
        ))
    if not item.source_receipt_refs or any(
        not ref.startswith("receipt://") for ref in item.source_receipt_refs
    ):
        blockers.append(P6DebateBlocker(
            code="invalid_source_receipt_refs",
            message="Live debate output requires durable receipt:// source refs.",
        ))
    if not item.target_refs:
        blockers.append(P6DebateBlocker(
            code="missing_target_refs",
            message="Live debate output requires at least one review target ref.",
        ))
    if not item.episode_ref.startswith("episode://"):
        blockers.append(P6DebateBlocker(
            code="invalid_episode_ref",
            message="Live debate output requires an episode:// ref from the existing episode runtime.",
        ))
    if not item.protocol_ref.startswith("protocol://"):
        blockers.append(P6DebateBlocker(
            code="invalid_protocol_ref",
            message="Live debate output requires a protocol:// lineage ref.",
        ))
    if item.claim_promotion_requested or item.graph_write_requested or item.direct_execution_requested:
        blockers.append(P6DebateBlocker(
            code="ungated_mutation_intent_blocked",
            message="P6 debate artifacts cannot promote claims, write GraphRAG, or execute directly.",
        ))
    if item.raw_payload_embedded or raw_payload_detected:
        blockers.append(P6DebateBlocker(
            code="raw_debate_payload_embedded",
            message="Debate receipts and projections are refs-only; raw cognition belongs in the source artifact.",
        ))
    return tuple(blockers)


def _identity(p6_id: str, item: P6DebateOutputInput) -> dict[str, Any]:
    return {
        "p6_id": p6_id,
        "artifact_kind": item.artifact_kind,
        "source_artifact_ref": item.source_artifact_ref,
        "content_sha256": item.content_sha256,
        "episode_ref": item.episode_ref,
        "protocol_ref": item.protocol_ref,
        "target_refs": sorted(item.target_refs),
    }


def _receipt(
    *,
    p6_id: str,
    item: P6DebateOutputInput,
    decision: Literal["materialized", "blocked"],
    reason_codes: tuple[str, ...],
    artifact_ref: str | None,
) -> P6LiveDebateReceipt:
    idempotency_key = hashlib.sha256(_stable_json(_identity(p6_id, item)).encode("utf-8")).hexdigest()
    receipt_ref = f"receipt://p6/live-debate/{_digest({'idempotency_key': idempotency_key, 'decision': decision, 'reason_codes': reason_codes})}"
    return P6LiveDebateReceipt(
        receipt_ref=receipt_ref,
        p6_id=p6_id,
        artifact_kind=item.artifact_kind,
        decision=decision,
        reason_codes=reason_codes,
        artifact_ref=artifact_ref,
        source_artifact_ref=item.source_artifact_ref,
        source_receipt_refs=item.source_receipt_refs,
        target_refs=item.target_refs,
        episode_ref=item.episode_ref,
        protocol_ref=item.protocol_ref,
        idempotency_key=idempotency_key,
    )


def _mudo_event(
    *,
    p6_id: str,
    item: P6DebateOutputInput,
    artifact: P6LiveDebateArtifact,
    receipt: P6LiveDebateReceipt,
    study_id: str,
    workspace_id: str,
) -> MUDOReceiptReady:
    event_ref = f"event://p6/live-debate/{_digest({'receipt_ref': receipt.receipt_ref})}"
    commit_hash = f"sha256:{hashlib.sha256(_stable_json({'artifact_ref': artifact.artifact_ref, 'receipt_ref': receipt.receipt_ref}).encode('utf-8')).hexdigest()}"
    return MUDOReceiptReady(
        run_id=item.episode_ref,
        program_id=p6_id,
        receipt_kind="P6LiveDebateReceipt",
        source_surface="protocol.p6.debate.artifacts",
        correlation_id=event_ref,
        protocol_ref=item.protocol_ref,
        study_id=study_id,
        workspace_id=workspace_id,
        input_refs=[item.source_artifact_ref, *item.source_receipt_refs],
        artifact_refs=[artifact.artifact_ref],
        evidence_refs=list(item.evidence_refs),
        receipt_payload={
            "mudo_id": item.mudo_id or "",
            "branch_type": "candidate",
            "commit_hash": commit_hash,
            "receipt_ref": receipt.receipt_ref,
            "reason_codes": list(receipt.reason_codes),
            "claim_status": "unpromoted",
            "graph_projection_status": "proposal_only",
        },
    )


def _contradiction_trigger(
    *,
    packet: Mapping[str, Any],
    item: P6DebateOutputInput,
    artifact: P6LiveDebateArtifact,
    receipt: P6LiveDebateReceipt,
) -> P6TriggerEvaluationResult:
    event_ref = f"event://p6/live-debate/{_digest({'receipt_ref': receipt.receipt_ref})}"
    return evaluate_p6_triggers(
        packet,
        trigger_payloads={
            "trigger_family": "contradiction_surface",
            "signal_ref": artifact.artifact_ref,
            "source_event_refs": (event_ref,),
            "source_receipt_refs": (receipt.receipt_ref,),
            "target_refs": item.target_refs,
            "reason_codes": item.reason_codes or ("contradiction_surface_requires_review",),
            "priority": "high",
            "confidence": item.confidence,
            "proposed_protocol_ref": "protocol://p6/review-contradiction-surface",
            "proposed_action_kind": "protocol_request",
        },
    )


def build_p6_live_debate_artifacts(
    packet: Mapping[str, Any],
    *,
    debate_outputs: Any,
    workspace_id: str,
    study_id: str,
    subscriber: DebateMUDOSubscriber | None = None,
) -> P6LiveDebatePipelineResult:
    p6_id = str(packet.get("p6_id") or "unknown-p6").strip()
    parsed = _as_inputs(debate_outputs)
    subscriber = subscriber or DebateMUDOSubscriber()
    artifacts: list[P6LiveDebateArtifact] = []
    receipts: list[P6LiveDebateReceipt] = []
    mudo_branch_receipts: list[MUDOBranchReceipt] = []
    mudo_ingest_statuses: list[str] = []
    trigger_results: list[P6TriggerEvaluationResult] = []
    blockers: list[P6DebateBlocker] = []
    duplicate_count = 0
    blocked_count = 0

    if not parsed:
        blockers.append(P6DebateBlocker(
            code="missing_debate_outputs",
            message="P6-5 requires at least one live debate output.",
        ))

    for item, raw_payload_detected in parsed:
        item_blockers = _blockers(item, raw_payload_detected=raw_payload_detected)
        identity = _identity(p6_id, item)
        artifact_ref = f"artifact://p6/live-debate/{item.artifact_kind}/{_digest(identity)}"
        if item_blockers:
            blocked_count += 1
            blockers.extend(item_blockers)
            receipts.append(_receipt(
                p6_id=p6_id,
                item=item,
                decision="blocked",
                reason_codes=tuple(sorted({blocker.code for blocker in item_blockers})),
                artifact_ref=None,
            ))
            continue

        artifact = P6LiveDebateArtifact(
            artifact_ref=artifact_ref,
            artifact_kind=item.artifact_kind,
            source_artifact_ref=item.source_artifact_ref,
            content_sha256=item.content_sha256,
            content_size_bytes=item.content_size_bytes,
            episode_ref=item.episode_ref,
            protocol_ref=item.protocol_ref,
            source_receipt_refs=item.source_receipt_refs,
            target_refs=item.target_refs,
            evidence_refs=item.evidence_refs,
            claim_refs=item.claim_refs,
        )
        receipt = _receipt(
            p6_id=p6_id,
            item=item,
            decision="materialized",
            reason_codes=item.reason_codes or ("live_debate_artifact_materialized",),
            artifact_ref=artifact.artifact_ref,
        )
        ingest: DebateMUDOIngestResult = subscriber.handle(_mudo_event(
            p6_id=p6_id,
            item=item,
            artifact=artifact,
            receipt=receipt,
            study_id=study_id,
            workspace_id=workspace_id,
        ))
        mudo_ingest_statuses.append(ingest.status)
        if ingest.status == "duplicate":
            duplicate_count += 1
            continue
        if ingest.status != "accepted" or ingest.branch_receipt is None:
            blocked_count += 1
            blockers.append(P6DebateBlocker(
                code=ingest.blocker_code or "mudo_branch_ingest_blocked",
                message=ingest.detail or "Debate MUDO branch receipt ingestion was blocked.",
            ))
            continue

        artifacts.append(artifact)
        receipts.append(receipt)
        mudo_branch_receipts.append(ingest.branch_receipt)
        if item.artifact_kind == "contradiction_surface":
            trigger_results.append(_contradiction_trigger(
                packet=packet,
                item=item,
                artifact=artifact,
                receipt=receipt,
            ))

    materialized_count = len(artifacts)
    if materialized_count and blocked_count:
        status: Literal["ready", "blocked", "partially_blocked", "duplicate_suppressed"] = "partially_blocked"
    elif materialized_count:
        status = "ready"
    elif duplicate_count and not blocked_count:
        status = "duplicate_suppressed"
    else:
        status = "blocked"

    return P6LiveDebatePipelineResult(
        p6_id=p6_id,
        status=status,
        artifacts=tuple(artifacts),
        receipts=tuple(receipts),
        mudo_branch_receipts=tuple(mudo_branch_receipts),
        mudo_ingest_statuses=tuple(mudo_ingest_statuses),
        trigger_results=tuple(trigger_results),
        blockers=tuple(blockers),
        artifact_refs=tuple(artifact.artifact_ref for artifact in artifacts),
        receipt_refs=tuple(receipt.receipt_ref for receipt in receipts),
        mudo_branch_receipt_refs=tuple(
            f"receipt://mudo/branch/{_digest({'idempotency_key': receipt.idempotency_key})}"
            for receipt in mudo_branch_receipts
        ),
        proposal_refs=tuple(
            proposal_ref
            for trigger_result in trigger_results
            for proposal_ref in trigger_result.proposal_refs
        ),
        materialized_count=materialized_count,
        blocked_count=blocked_count,
        duplicate_count=duplicate_count,
    )

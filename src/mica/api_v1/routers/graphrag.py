"""GraphRAG API router — product-scoped graph query, write, and traversal.

Prefix: /api/v1/graphrag
Tags:   graphrag

Endpoints
---------
POST /api/v1/graphrag/query                     hybrid search edges + facts
POST /api/v1/graphrag/hop1                     1-hop traversal from seed nodes
POST /api/v1/graphrag/claim                    write a scientific claim as fact + edges
POST /api/v1/graphrag/paper                    ingest a paper with authors + abstract
POST /api/v1/graphrag/lmp                      parse LMP v4 XML → persist into GraphRAG
POST /api/v1/graphrag/export-decision-subgraph export bounded subgraph for Decision Card
GET  /api/v1/graphrag/stats                    node/edge/fact counts
POST /api/v1/graphrag/promote-from-kb          extract entities/claims from KB → GraphRAG

Design:
- All endpoints require user authentication via user_dependency.
- All writes inject product scope (study_id, kb_id, working_set_id).
- All facts auto-compute claim_key for deduplication.
- LMP parsing reuses existing _parse_lmp_v4_graph from graph.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.kb.graph.commons import GraphCommonsRuntime
from mica.kb.graph.consensus import ConsensusMetaClaimBuilder, DebatePosition
from mica.kb.graph.durability.runtime import GraphDurabilityRuntime
from mica.kb.graph.economics import GraphKnowledgeEconomicsRuntime
from mica.kb.graph.health import (
    GraphHealthAuditInputs,
    GraphHealthReport,
    GraphKnowledgeDebtRuntime,
)
from mica.kb.graph.learning import GovernedLearningRuntime
from mica.kb.graph.ontology.predicate_governance import (
    GovernanceApproval,
    PredicateChangeKind,
    PredicateChangeRequest,
    PredicateDomainRange,
    PredicateExternalMapping,
    PredicateGovernanceProcess,
    PredicateImpactLevel,
    PredicateLifecycleState,
    PredicateMappingKind,
)
from mica.graphrag.answer_receipt_runtime import GraphAnswerReceiptRuntime
from mica.graphrag.cost_runtime import TenantBudgetEnforcer
from mica.graphrag.curation_runtime import GraphCurationRuntime, GraphCurationTask
from mica.graphrag.evidence_path_runtime import OutputValidator
from mica.graphrag.entity_identity_runtime import EntityIdentityRuntime
from mica.graphrag.golden_release_runtime import GoldenGraphRAGv3Runtime
from mica.graphrag.health_runtime import GraphHealthGateDecision
from mica.graphrag.inference_runtime import GraphInferenceProposalEngine
from mica.graphrag.node2vec_runtime import Node2VecABMetrics
from mica.graphrag.privacy_runtime import GraphAggregatePolicy, LeakageCanary
from mica.graphrag.projection_runtime import GraphProjectionGuardDecision
from mica.graphrag.traversal_runtime import (
    BudgetedTraversalEngine,
    TraversalDegradationEnvelope,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/graphrag", tags=["graphrag"])


# ---------------------------------------------------------------------------
# Lazy store access — follows same pattern as kb_service in knowledge_fabric
# ---------------------------------------------------------------------------

async def _recover_graphrag_runtime(request: Request) -> None:
    """Attempt bounded lazy recovery when GraphRAG booted degraded.

    This allows long-lived API processes to recover after Timescale resumes
    without requiring a full backend restart.
    """
    app_state = request.app.state
    if getattr(app_state, "graphrag_store", None) is not None:
        return

    lock = getattr(app_state, "_graphrag_recovery_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state._graphrag_recovery_lock = lock

    cooldown_sec = float(os.getenv("MICA_GRAPHRAG_RECOVERY_COOLDOWN_SEC", "15") or "15")
    last_attempt = float(getattr(app_state, "_graphrag_recovery_last_attempt", 0.0) or 0.0)
    now = time.monotonic()
    if now - last_attempt < cooldown_sec:
        return

    async with lock:
        if getattr(app_state, "graphrag_store", None) is not None:
            return
        last_attempt = float(getattr(app_state, "_graphrag_recovery_last_attempt", 0.0) or 0.0)
        now = time.monotonic()
        if now - last_attempt < cooldown_sec:
            return
        app_state._graphrag_recovery_last_attempt = now

        from mica.api_v1.startup_guard import await_nonfatal_startup_step
        from mica.infrastructure.persistence.graphrag_write_facade import GraphRAGWriteFacade
        from mica.infrastructure.persistence.timescale_graphrag_store import TimescaleGraphRAGStore

        timeout_sec = float(os.getenv("MICA_NONFATAL_STARTUP_TIMEOUT_SEC", "8") or "8")
        store = TimescaleGraphRAGStore()
        ready, _ = await await_nonfatal_startup_step(
            "GraphRAG lazy recovery init",
            store.initialize(),
            timeout_sec=timeout_sec,
        )
        if not ready:
            return

        app_state.graphrag_store = store
        app_state.graphrag_write_facade = GraphRAGWriteFacade(store)
        await await_nonfatal_startup_step(
            "GraphRAG lazy recovery indexes",
            store.ensure_global_indexes(),
            timeout_sec=timeout_sec,
        )
        logger.info("GraphRAG runtime recovered lazily after startup degradation")


async def _get_graphrag_store(request: Request):
    """Return the TimescaleGraphRAGStore from app.state, or raise 503."""
    store = getattr(request.app.state, "graphrag_store", None)
    if store is None:
        await _recover_graphrag_runtime(request)
        store = getattr(request.app.state, "graphrag_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="GraphRAG store not available")
    return store


async def _get_graphrag_facade(request: Request):
    """Return the GraphRAGWriteFacade from app.state, or raise 503."""
    facade = getattr(request.app.state, "graphrag_write_facade", None)
    if facade is None:
        await _recover_graphrag_runtime(request)
        facade = getattr(request.app.state, "graphrag_write_facade", None)
    if facade is None:
        raise HTTPException(status_code=503, detail="GraphRAG write facade not available")
    return facade


def _get_graphrag_projection_runtime(request: Request):
    """Return optional GraphRAG projection runtime from app.state."""
    return getattr(request.app.state, "graphrag_projection_runtime", None)


def _get_graphrag_health_runtime(request: Request):
    """Return optional GraphRAG health runtime from app.state."""
    return getattr(request.app.state, "graphrag_health_runtime", None)


def _get_graphrag_node2vec_runtime(request: Request):
    """Return optional GraphRAG node2vec shadow runtime from app.state."""
    return getattr(request.app.state, "graphrag_node2vec_runtime", None)


def _get_graphrag_answer_receipt_runtime(request: Request):
    """Return optional GraphRAG answer receipt runtime from app.state."""
    return getattr(request.app.state, "graphrag_answer_receipt_runtime", None)


def _get_graphrag_curation_runtime(request: Request) -> GraphCurationRuntime:
    runtime = getattr(request.app.state, "graphrag_curation_runtime", None)
    if runtime is None:
        return GraphCurationRuntime()
    if not isinstance(runtime, GraphCurationRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG curation runtime invalid")
    return runtime


def _get_graphrag_golden_release_runtime(request: Request) -> GoldenGraphRAGv3Runtime:
    runtime = getattr(request.app.state, "graphrag_golden_release_runtime", None)
    if runtime is None:
        return GoldenGraphRAGv3Runtime()
    if not isinstance(runtime, GoldenGraphRAGv3Runtime):
        raise HTTPException(status_code=500, detail="GraphRAG golden release runtime invalid")
    return runtime


def _get_graphrag_predicate_governance_runtime(request: Request) -> PredicateGovernanceProcess:
    runtime = getattr(request.app.state, "graphrag_predicate_governance_runtime", None)
    if runtime is None:
        return PredicateGovernanceProcess()
    if not isinstance(runtime, PredicateGovernanceProcess):
        raise HTTPException(status_code=500, detail="GraphRAG predicate governance runtime invalid")
    return runtime


def _get_graphrag_budget_enforcer(request: Request) -> TenantBudgetEnforcer:
    """Return GraphRAG tenant budget enforcer or a safe default."""
    enforcer = getattr(request.app.state, "graphrag_tenant_budget_enforcer", None)
    if enforcer is None:
        return TenantBudgetEnforcer()
    if not isinstance(enforcer, TenantBudgetEnforcer):
        raise HTTPException(status_code=500, detail="GraphRAG budget enforcer invalid")
    return enforcer


def _get_graphrag_knowledge_economics_runtime(request: Request) -> GraphKnowledgeEconomicsRuntime:
    runtime = getattr(request.app.state, "graphrag_knowledge_economics_runtime", None)
    if runtime is None:
        return GraphKnowledgeEconomicsRuntime()
    if not isinstance(runtime, GraphKnowledgeEconomicsRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG knowledge economics runtime invalid")
    return runtime


def _get_graphrag_commons_runtime(request: Request) -> GraphCommonsRuntime:
    runtime = getattr(request.app.state, "graphrag_commons_runtime", None)
    if runtime is None:
        return GraphCommonsRuntime()
    if not isinstance(runtime, GraphCommonsRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG commons runtime invalid")
    return runtime


def _get_graphrag_durability_runtime(request: Request) -> GraphDurabilityRuntime:
    runtime = getattr(request.app.state, "graphrag_durability_runtime", None)
    if runtime is None:
        return GraphDurabilityRuntime()
    if not isinstance(runtime, GraphDurabilityRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG durability runtime invalid")
    return runtime


def _get_graphrag_knowledge_debt_runtime(request: Request) -> GraphKnowledgeDebtRuntime:
    runtime = getattr(request.app.state, "graphrag_knowledge_debt_runtime", None)
    if runtime is None:
        return GraphKnowledgeDebtRuntime()
    if not isinstance(runtime, GraphKnowledgeDebtRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG knowledge debt runtime invalid")
    return runtime


def _get_graphrag_governed_learning_runtime(request: Request) -> GovernedLearningRuntime:
    runtime = getattr(request.app.state, "graphrag_governed_learning_runtime", None)
    if runtime is None:
        return GovernedLearningRuntime()
    if not isinstance(runtime, GovernedLearningRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG governed learning runtime invalid")
    return runtime


def _get_graphrag_entity_identity_runtime(request: Request) -> EntityIdentityRuntime:
    runtime = getattr(request.app.state, "graphrag_entity_identity_runtime", None)
    if runtime is None:
        return EntityIdentityRuntime()
    if not isinstance(runtime, EntityIdentityRuntime):
        raise HTTPException(status_code=500, detail="GraphRAG entity identity runtime invalid")
    return runtime


def _get_graphrag_debate_consensus_runtime(request: Request) -> ConsensusMetaClaimBuilder:
    runtime = getattr(request.app.state, "graphrag_debate_consensus_runtime", None)
    if runtime is None:
        return ConsensusMetaClaimBuilder()
    if not isinstance(runtime, ConsensusMetaClaimBuilder):
        raise HTTPException(status_code=500, detail="GraphRAG debate consensus runtime invalid")
    return runtime


def _build_graphrag_scope_ref(
    *,
    study_id: Optional[str],
    kb_id: Optional[str],
    working_set_id: Optional[str] = None,
    global_only: bool = False,
    user_id: Optional[str] = None,
) -> str:
    if global_only:
        return "scope://global"
    if study_id:
        return f"scope://study/{study_id}"
    if kb_id:
        return f"scope://kb/{kb_id}"
    if working_set_id:
        return f"scope://working-set/{working_set_id}"
    if user_id:
        return f"scope://user/{user_id}"
    return "scope://unscoped"


def _serialize_projection_guard(decision: GraphProjectionGuardDecision) -> Dict[str, Any]:
    return {
        "scope_ref": decision.scope_ref,
        "projection_ref": decision.projection_ref,
        "target": decision.target,
        "state": decision.state,
        "traversal_source": decision.traversal_source,
        "projected_traversal_allowed": decision.projected_traversal_allowed,
        "fallback_used": decision.fallback_used,
        "drift_signal_ref": decision.drift_signal_ref,
        "checkpoint_ref": decision.checkpoint_ref,
        "replay_backlog": decision.replay_backlog,
        "lag_seconds": decision.lag_seconds,
        "reconcile_action_ref": decision.reconcile_action_ref,
        "reason": decision.reason,
    }


def _serialize_traversal_degradation(degradation: TraversalDegradationEnvelope) -> Dict[str, Any]:
    return {
        "policy_ref": degradation.policy_ref,
        "authoritative_source": degradation.authoritative_source,
        "projection_state": degradation.projection_state,
        "partial_response": degradation.partial_response,
        "partial_reason": degradation.partial_reason,
        "fallback_used": degradation.fallback_used,
        "fallback_reason": degradation.fallback_reason,
        "budget_class": degradation.budget_class,
        "budget_status": degradation.budget_status,
        "budget_reason": degradation.budget_reason,
        "requested_limit": degradation.requested_limit,
        "effective_limit": degradation.effective_limit,
        "remaining_budget_ratio": degradation.remaining_budget_ratio,
    }


def _serialize_graph_health_gate(decision: GraphHealthGateDecision) -> Dict[str, Any]:
    return {
        "scope_ref": decision.scope_ref,
        "state": decision.state,
        "allow_answer": decision.allow_answer,
        "blocker_ref": decision.blocker_ref,
        "warning_ref": decision.warning_ref,
        "reason": decision.reason,
        "source": decision.source,
    }


def _serialize_graph_health_report(report: GraphHealthReport) -> Dict[str, Any]:
    return {
        "report_ref": report.report_ref,
        "scope_ref": report.scope_ref,
        "owner_ref": report.owner_ref,
        "generated_at": report.generated_at,
        "state": report.state,
        "public_gate_blocked": report.public_gate_blocked,
        "gate_reason_codes": list(report.gate_reason_codes),
        "metrics": {
            "active_edge_count": report.metrics.active_edge_count,
            "receipt_coverage_ratio": report.metrics.receipt_coverage_ratio,
            "stale_edge_ratio": report.metrics.stale_edge_ratio,
            "orphan_node_ratio": report.metrics.orphan_node_ratio,
            "orphan_edge_ratio": report.metrics.orphan_edge_ratio,
            "open_contradiction_ratio": report.metrics.open_contradiction_ratio,
            "hidden_critical_debt_ratio": report.metrics.hidden_critical_debt_ratio,
        },
        "domain_metrics": [
            {
                "domain_ref": item.domain_ref,
                "edge_count": item.edge_count,
                "receipt_coverage_ratio": item.receipt_coverage_ratio,
                "stale_edge_ratio": item.stale_edge_ratio,
                "open_contradiction_ratio": item.open_contradiction_ratio,
            }
            for item in report.domain_metrics
        ],
        "knowledge_debt_ledger": {
            "ledger_ref": report.knowledge_debt_ledger.ledger_ref,
            "scope_ref": report.knowledge_debt_ledger.scope_ref,
            "owner_ref": report.knowledge_debt_ledger.owner_ref,
            "hidden_critical_debt_count": report.knowledge_debt_ledger.hidden_critical_debt_count,
            "debt_entries": [
                {
                    "debt_ref": item.debt_ref,
                    "scope_ref": item.scope_ref,
                    "domain_ref": item.domain_ref,
                    "debt_kind": item.debt_kind,
                    "severity": item.severity,
                    "downstream_risk": item.downstream_risk,
                    "owner_ref": item.owner_ref,
                    "affected_count": item.affected_count,
                    "hidden": item.hidden,
                    "sample_strategy": item.sample_strategy,
                    "sampled_edge_refs": list(item.sampled_edge_refs),
                    "reason": item.reason,
                }
                for item in report.knowledge_debt_ledger.debt_entries
            ],
        },
    }


def _resolve_graph_health_gate(*, request: Request, scope_ref: str) -> GraphHealthGateDecision:
    runtime = _get_graphrag_health_runtime(request)
    if runtime is None:
        return GraphHealthGateDecision(
            scope_ref=scope_ref,
            state="green",
            allow_answer=True,
            blocker_ref=None,
            warning_ref=None,
            reason="graph_health_runtime_missing",
            source="graph_health_runtime",
        )
    return runtime.inspect_scope(scope_ref)


def _merge_graph_gate_decisions(
    primary: GraphHealthGateDecision,
    secondary: GraphHealthGateDecision,
) -> GraphHealthGateDecision:
    states = {"green": 0, "yellow": 1, "red": 2}
    if states.get(secondary.state, 0) > states.get(primary.state, 0):
        return secondary
    if secondary.state == primary.state:
        return GraphHealthGateDecision(
            scope_ref=primary.scope_ref,
            state=primary.state,
            allow_answer=primary.allow_answer and secondary.allow_answer,
            blocker_ref=primary.blocker_ref or secondary.blocker_ref,
            warning_ref=primary.warning_ref or secondary.warning_ref,
            reason=f"{primary.reason}|{secondary.reason}",
            source=f"{primary.source}|{secondary.source}",
        )
    return primary


def _resolve_graph_public_quality_gate(
    *,
    request: Request,
    scope_ref: str,
    public_surface: bool,
) -> GraphHealthGateDecision:
    health_gate = _resolve_graph_health_gate(
        request=request,
        scope_ref=scope_ref,
    )
    debt_runtime = _get_graphrag_knowledge_debt_runtime(request)
    debt_gate = debt_runtime.inspect_scope(scope_ref=scope_ref, public_surface=public_surface)
    quality_gate = GraphHealthGateDecision(
        scope_ref=scope_ref,
        state=debt_gate.state,
        allow_answer=debt_gate.allow_serve,
        blocker_ref=debt_gate.blocker_ref,
        warning_ref=debt_gate.warning_ref,
        reason="|".join(debt_gate.reason_codes),
        source="graph_knowledge_debt_runtime",
    )
    return _merge_graph_gate_decisions(health_gate, quality_gate)


async def _budgeted_hop1_with_projection_guard(
    *,
    request: Request,
    store: Any,
    seed_nodes: List[str],
    limit: int,
    user_id: str,
    workspace_id: Optional[str],
    global_only: bool,
    policy: str,
    budget_ref: Optional[str],
    scope_ref: str,
):
    projection_runtime = _get_graphrag_projection_runtime(request)
    engine = BudgetedTraversalEngine(
        store=store,
        projection_runtime=projection_runtime,
        budget_enforcer=_get_graphrag_budget_enforcer(request),
    )
    try:
        serve_decision = await engine.hop1(
            seed_nodes=seed_nodes,
            limit=limit,
            user_id=user_id,
            workspace_id=workspace_id,
            global_only=global_only,
            policy=policy,
            budget_ref=budget_ref,
            scope_ref=scope_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG traversal runtime invalid: {exc}")

    return (
        serve_decision.traversal,
        _serialize_projection_guard(serve_decision.projection_guard),
        _serialize_traversal_degradation(serve_decision.degradation),
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class GraphRAGQueryRequest(BaseModel):
    query_text: str = Field(..., min_length=1, max_length=2000)
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    limit: int = Field(10, ge=1, le=100)
    include_edges: bool = True
    include_facts: bool = True
    global_only: bool = False
    graph_snapshot_manifest_ref: Optional[str] = None
    vector_index_manifest_ref: Optional[str] = None


class GraphRAGHop1Request(BaseModel):
    seed_nodes: List[str] = Field(..., min_items=1, max_items=50)
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    limit: int = Field(50, ge=1, le=200)
    global_only: bool = False
    traversal_policy: str = Field("interactive", description="interactive, background, impact_frontier")
    budget_ref: Optional[str] = None


class GraphRAGWriteClaimRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    content: str = Field(..., min_length=1, max_length=5000)
    fact_type: str = Field("finding", description="finding, claim, observation, hypothesis")
    topic: Optional[str] = None
    entities: List[str] = Field(default_factory=list)
    source_doi: Optional[str] = None
    source_resource_uri: Optional[str] = None
    source_snippet_uri: Optional[str] = None
    created_by_receipt_ref: Optional[str] = None
    semantic_context_ref: Optional[str] = None
    policy_scope: str = Field("lab", description="global, org, lab, study")
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class GraphRAGWritePaperRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    paper_id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=500)
    authors: List[str] = Field(default_factory=list)
    abstract: str = Field("", max_length=10000)
    year: Optional[int] = None
    journal: Optional[str] = None
    doi: Optional[str] = None
    source_resource_uri: Optional[str] = None


class GraphRAGWriteLMPRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    lmp_file: str = Field(..., min_length=1, max_length=400, description="LMP v4 XML filename (from .tmp_lmp_v4 or GCS)")
    max_edges: int = Field(500, ge=1, le=5000)
    node_types_to_include: List[str] = Field(
        default_factory=lambda: ["protein", "domain", "pathway", "disease", "interaction", "pharmacology"]
    )


class GraphRAGExportDecisionSubgraphRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    query_focus: str = Field(..., min_length=1, max_length=1000)
    max_nodes: int = Field(100, ge=1, le=500)
    max_edges: int = Field(200, ge=1, le=1000)
    max_inference_proposals: int = Field(10, ge=0, le=50)
    include_provenance: bool = True
    traversal_policy: str = Field("interactive", description="interactive, background, impact_frontier")
    budget_ref: Optional[str] = None


class GraphRAGPromoteFromKBRequest(BaseModel):
    kb_id: str = Field(..., min_length=1)
    study_id: Optional[str] = None
    document_ids: Optional[List[str]] = None
    extraction_method: str = Field("dlm_entity", description="dlm_entity or llm_extraction")
    created_by_receipt_ref: Optional[str] = None
    semantic_context_ref: Optional[str] = None
    policy_scope: str = Field("lab", description="global, org, lab, study")
    max_claims: int = Field(50, ge=1, le=200)


class GraphRAGEdgeCurationRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    source_node: str = Field(..., min_length=1, max_length=500)
    source_type: str = Field(..., min_length=1, max_length=100)
    target_node: str = Field(..., min_length=1, max_length=500)
    target_type: str = Field(..., min_length=1, max_length=100)
    relationship: str = Field(..., min_length=1, max_length=120)
    corrected_edge_status: str = Field(..., description="review_required, deprecated, retracted, superseded, active")
    actor_ref: Optional[str] = None
    actor_role: str = Field(..., description="curator or senior_curator")
    created_by_receipt_ref: Optional[str] = None
    reason_codes: List[str] = Field(default_factory=list)
    details: Optional[str] = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    source_doi: Optional[str] = None
    source_sentence: Optional[str] = None
    extraction_method: str = Field("graph_curation")
    semantic_context_ref: Optional[str] = None
    policy_scope: str = Field("lab", description="global, org, lab, study")
    contradiction_claim_ref: Optional[str] = None
    contradiction_kind: Optional[str] = None
    supporting_edge_refs: List[str] = Field(default_factory=list)
    contradicting_edge_refs: List[str] = Field(default_factory=list)


class GraphRAGKnowledgeMaintenanceCandidateRequest(BaseModel):
    edge_ref: str = Field(..., min_length=1, max_length=500)
    maintenance_cost: int = Field(..., ge=1, le=1000)
    scientific_impact: float = Field(..., ge=0.0, le=1.0)
    downstream_usage: int = Field(0, ge=0, le=1_000_000)
    contradiction_pressure: float = Field(0.0, ge=0.0, le=1.0)
    public_visibility: float = Field(..., ge=0.0, le=1.0)
    retraction_risk: float = Field(..., ge=0.0, le=1.0)


class GraphRAGKnowledgeMaintenanceBudgetRequest(BaseModel):
    budget_ref: str = Field(..., min_length=1, max_length=300)
    subsidy_ref: str = Field(..., min_length=1, max_length=300)
    available_units: int = Field(..., ge=1, le=100_000)
    candidates: List[GraphRAGKnowledgeMaintenanceCandidateRequest] = Field(default_factory=list, min_length=1, max_length=200)


class GraphRAGCommonsPackageReviewRequest(BaseModel):
    package_ref: str = Field(..., min_length=1, max_length=300)
    source_instance: str = Field(..., min_length=1, max_length=300)
    trust_status: str = Field(..., min_length=1, max_length=50)
    signature_verified: bool = True
    federation_state: str = Field("external_asserted", description="must remain external_asserted until local promotion")
    publication_scope: str = Field(..., description="global or org")
    edge_refs: List[str] = Field(default_factory=list, min_length=1, max_length=500)
    receipt_refs: List[str] = Field(default_factory=list, min_length=1, max_length=500)
    prior_retractions: int = Field(0, ge=0, le=10_000)
    poisoning_incidents: int = Field(0, ge=0, le=10_000)
    local_promotions: int = Field(0, ge=0, le=10_000)
    curator_endorsements: int = Field(0, ge=0, le=10_000)


class GraphRAGCommonsSubgraphPublicationRequest(BaseModel):
    commons_package_ref: str = Field(..., min_length=1, max_length=300)
    package_ref: str = Field(..., min_length=1, max_length=300)
    source_instance: str = Field(..., min_length=1, max_length=300)
    federation_state: str = Field("external_asserted")
    publication_scope: str = Field(..., description="global or org")
    edge_refs: List[str] = Field(default_factory=list, min_length=1, max_length=500)
    receipt_refs: List[str] = Field(default_factory=list, min_length=1, max_length=500)
    edge_policy_scopes: Dict[str, str] = Field(default_factory=dict)


class GraphRAGLocalPromotionCandidateRequest(BaseModel):
    commons_package_ref: str = Field(..., min_length=1, max_length=300)
    source_publication_ref: str = Field(..., min_length=1, max_length=300)
    package_ref: str = Field(..., min_length=1, max_length=300)
    source_instance: str = Field(..., min_length=1, max_length=300)
    federation_state: str = Field("external_asserted")
    target_scope: str = Field(..., description="org, lab or study")
    candidate_edge_refs: List[str] = Field(default_factory=list, max_length=500)
    evidence_receipt_refs: List[str] = Field(default_factory=list, max_length=500)
    supporting_local_receipt_refs: List[str] = Field(default_factory=list, max_length=500)
    requested_by: str = Field(..., min_length=1, max_length=300)


class GraphRAGStatsResponse(BaseModel):
    schema: str
    node_count: int
    edge_count: int
    fact_count: int


class GraphRAGAggregateRequest(BaseModel):
    metric: str = Field(..., description="edge_count, degree, centrality")
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    global_only: bool = False
    node_name: Optional[str] = None
    top_k: int = Field(10, ge=1, le=100)


class GraphRAGGoldenReleaseGateRequest(BaseModel):
    query_outputs: List[Dict[str, Any]] = Field(default_factory=list)
    leakage_canaries: List[Dict[str, Any]] = Field(default_factory=list)
    replay_results: List[Dict[str, Any]] = Field(default_factory=list)
    traversal_latencies_ms: List[float] = Field(default_factory=list)
    hypothesis_labels: List[Dict[str, Any]] = Field(default_factory=list)
    fallback_metrics: Optional[Dict[str, Any]] = None
    evidence_refs: List[str] = Field(default_factory=list)


class GraphRAGPortableCanonExportRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    global_only: bool = False
    graph_snapshot_manifest_ref: str = Field(..., min_length=1, max_length=300)
    vector_index_manifest_ref: str = Field(..., min_length=1, max_length=300)
    predicate_registry_snapshot_ref: str = Field(..., min_length=1, max_length=300)
    semantic_contract_bundle_ref: str = Field(..., min_length=1, max_length=300)
    schema_version_ref: str = Field(..., min_length=1, max_length=300)
    doctrine_version_ref: str = Field(..., min_length=1, max_length=300)
    receipt_manifest_refs: List[str] = Field(default_factory=list, min_length=1, max_length=500)
    migration_manifest_refs: List[str] = Field(default_factory=list, max_length=500)
    as_of_ts: str = Field(..., min_length=1, max_length=80)
    transaction_as_of_ts: Optional[str] = Field(None, min_length=1, max_length=80)
    limit: int = Field(10000, ge=1, le=50000)


class GraphRAGReplayAsOfRequest(BaseModel):
    bundle: Dict[str, Any]
    requested_as_of_ts: str = Field(..., min_length=1, max_length=80)
    predicate_registry_snapshot_ref: str = Field(..., min_length=1, max_length=300)
    schema_version_ref: str = Field(..., min_length=1, max_length=300)
    doctrine_version_ref: str = Field(..., min_length=1, max_length=300)


class GraphRAGHealthReportRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    global_only: bool = False
    stale_edge_count: int = Field(0, ge=0)
    orphan_node_count: int = Field(0, ge=0)
    orphan_edge_count: int = Field(0, ge=0)
    open_contradiction_count: int = Field(0, ge=0)
    hidden_critical_debt_count: int = Field(0, ge=0)
    owner_ref: Optional[str] = None
    sample_size: int = Field(12, ge=1, le=200)
    domain_usage_weights: Dict[str, int] = Field(default_factory=dict)
    issuer_risk_weights: Dict[str, float] = Field(default_factory=dict)
    register_scope: bool = True


class GraphRAGUsageSignalRequest(BaseModel):
    study_id: Optional[str] = None
    kb_id: Optional[str] = None
    working_set_id: Optional[str] = None
    global_only: bool = False
    signal_kind: str = Field(..., min_length=1, max_length=80)
    path_ref: str = Field(..., min_length=1, max_length=300)
    edge_refs: List[str] = Field(default_factory=list, max_length=500)
    actor_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    receipt_ref: str = Field(..., min_length=1, max_length=300)
    proposed_effects: List[str] = Field(default_factory=list, max_length=20)


class GraphRAGABMetricsRequest(BaseModel):
    grounding_rate: float = Field(..., ge=0.0, le=1.0)
    p95_latency_ms: float = Field(..., ge=0.0, le=100000.0)
    permission_failure_rate: float = Field(..., ge=0.0, le=1.0)


class GraphRAGGovernedRetrainingRequest(BaseModel):
    frozen_graph_snapshot_ref: str = Field(..., min_length=1, max_length=300)
    candidate_model_ref: str = Field("mica_graph_node2vec_candidate", min_length=1, max_length=300)
    vector_snapshot_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    control_metrics: GraphRAGABMetricsRequest
    treatment_metrics: GraphRAGABMetricsRequest
    query_outputs: List[Dict[str, Any]] = Field(default_factory=list)
    leakage_canaries: List[Dict[str, Any]] = Field(default_factory=list)
    replay_results: List[Dict[str, Any]] = Field(default_factory=list)
    traversal_latencies_ms: List[float] = Field(default_factory=list)
    hypothesis_labels: List[Dict[str, Any]] = Field(default_factory=list)
    fallback_metrics: Optional[Dict[str, Any]] = None
    includes_unvalidated_inferred_edges: bool = False
    uses_usage_popularity_as_evidence: bool = False
    usage_signal_refs: List[str] = Field(default_factory=list, max_length=500)


class GraphRAGEntityIdentityDecisionRequest(BaseModel):
    local_entity_ref: str = Field(..., min_length=1, max_length=300)
    external_entity_refs: List[str] = Field(default_factory=list, max_length=500)
    relation_kind: str = Field(..., min_length=1, max_length=80)
    confidence: float = Field(..., ge=0.0, le=1.0)
    resolver_version_ref: str = Field(..., min_length=1, max_length=300)
    evidence_refs: List[str] = Field(default_factory=list, max_length=500)
    decision_receipt_ref: str = Field(..., min_length=1, max_length=300)
    valid_from: Optional[str] = Field(None, min_length=1, max_length=80)
    valid_to: Optional[str] = Field(None, min_length=1, max_length=80)
    disputed: bool = False


class GraphRAGIdentityConflictReviewRequest(BaseModel):
    local_entity_ref: str = Field(..., min_length=1, max_length=300)
    external_entity_refs: List[str] = Field(default_factory=list, max_length=500)
    local_relation_kind: str = Field(..., min_length=1, max_length=80)
    external_relation_kinds: List[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(..., ge=0.0, le=1.0)
    resolver_version_ref: str = Field(..., min_length=1, max_length=300)
    evidence_refs: List[str] = Field(default_factory=list, max_length=500)
    decision_receipt_ref: str = Field(..., min_length=1, max_length=300)
    external_decision_refs: List[str] = Field(default_factory=list, max_length=500)
    local_identity_gate_approved: bool = False


class GraphRAGEntitySplitRequest(BaseModel):
    canonical_entity_ref: str = Field(..., min_length=1, max_length=300)
    successor_entity_refs: List[str] = Field(default_factory=list, max_length=500)
    stale_path_refs: List[str] = Field(default_factory=list, max_length=500)
    impacted_edge_refs: List[str] = Field(default_factory=list, max_length=500)
    resolver_version_ref: str = Field(..., min_length=1, max_length=300)
    evidence_refs: List[str] = Field(default_factory=list, max_length=500)
    decision_receipt_ref: str = Field(..., min_length=1, max_length=300)


class GraphRAGResolverDriftPlanRequest(BaseModel):
    previous_resolver_version_ref: str = Field(..., min_length=1, max_length=300)
    resolver_version_ref: str = Field(..., min_length=1, max_length=300)
    impacted_entity_refs: List[str] = Field(default_factory=list, max_length=500)
    impacted_edge_refs: List[str] = Field(default_factory=list, max_length=500)
    stale_path_refs: List[str] = Field(default_factory=list, max_length=500)
    reason: str = Field("resolver_release_changed", min_length=1, max_length=300)


class GraphRAGDebatePositionRequest(BaseModel):
    position_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    claim_family_refs: List[str] = Field(default_factory=list, max_length=500)
    supporting_evidence_refs: List[str] = Field(default_factory=list, max_length=500)
    contradicting_evidence_refs: List[str] = Field(default_factory=list, max_length=500)
    institution_refs: List[str] = Field(default_factory=list, max_length=200)
    confidence: float = Field(0.5, ge=0.0, le=1.0)


class GraphRAGDebateFrontReviewRequest(BaseModel):
    topic_ref: str = Field(..., min_length=1, max_length=300)
    positions: List[GraphRAGDebatePositionRequest] = Field(default_factory=list, max_length=200)
    consensus_history_refs: List[str] = Field(default_factory=list, max_length=500)
    open_contradiction_refs: List[str] = Field(default_factory=list, max_length=500)
    resolved_contradiction_refs: List[str] = Field(default_factory=list, max_length=500)
    last_reviewed_at: Optional[str] = Field(None, min_length=1, max_length=80)
    consensus_status: Optional[str] = Field(None, min_length=1, max_length=80)


class GraphRAGConsensusClaimRequest(BaseModel):
    claim_family_ref: str = Field(..., min_length=1, max_length=300)
    subject_entity_ref: str = Field(..., min_length=1, max_length=300)
    subject_entity_type: str = Field("entity", min_length=1, max_length=80)
    predicate_id: str = Field(..., min_length=1, max_length=120)
    object_entity_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    object_entity_type: str = Field("entity", min_length=1, max_length=80)
    object_literal: Optional[str] = Field(None, min_length=1, max_length=300)
    organism: str = Field("taxon://9606", min_length=1, max_length=120)
    cell_type: Optional[str] = Field(None, min_length=1, max_length=120)
    tissue: Optional[str] = Field(None, min_length=1, max_length=120)
    condition: Optional[str] = Field(None, min_length=1, max_length=120)
    direction: str = Field("unknown", min_length=1, max_length=40)
    polarity: str = Field("neutral", min_length=1, max_length=40)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    institution_refs: List[str] = Field(default_factory=list, max_length=200)
    created_by_receipt_ref: Optional[str] = Field(None, min_length=1, max_length=300)


class GraphRAGConsensusEvidenceRequest(BaseModel):
    evidence_ref: str = Field(..., min_length=1, max_length=300)
    claim_family_ref: str = Field(..., min_length=1, max_length=300)
    source_work_ref: str = Field(..., min_length=1, max_length=300)
    source_version_ref: str = Field("v1", min_length=1, max_length=120)
    method_family: str = Field("literature", min_length=1, max_length=120)
    experimental_system: str = Field("mixed", min_length=1, max_length=120)
    evidence_kind: str = Field("literature", min_length=1, max_length=80)
    support_direction: str = Field("supports", min_length=1, max_length=80)
    strength: str = Field("moderate", min_length=1, max_length=80)
    artifact_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    source_doi: Optional[str] = Field(None, min_length=1, max_length=300)
    source_pmid: Optional[str] = Field(None, min_length=1, max_length=120)
    receipt_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    confidence: float = Field(0.5, ge=0.0, le=1.0)


class GraphRAGConsensusContradictionRequest(BaseModel):
    contradiction_ref: str = Field(..., min_length=1, max_length=300)
    claim_a_ref: str = Field(..., min_length=1, max_length=300)
    claim_b_ref: str = Field(..., min_length=1, max_length=300)
    contradiction_kind: str = Field("opposite_direction", min_length=1, max_length=80)
    status: str = Field("contradiction_open", min_length=1, max_length=80)
    explanation: Optional[str] = Field(None, min_length=1, max_length=300)


class GraphRAGConsensusMetaClaimRequest(BaseModel):
    topic_ref: str = Field(..., min_length=1, max_length=300)
    claims: List[GraphRAGConsensusClaimRequest] = Field(default_factory=list, max_length=200)
    evidence_items: List[GraphRAGConsensusEvidenceRequest] = Field(default_factory=list, max_length=1000)
    contradictions: List[GraphRAGConsensusContradictionRequest] = Field(default_factory=list, max_length=500)
    positions: List[GraphRAGDebatePositionRequest] = Field(default_factory=list, max_length=200)
    consensus_history_refs: List[str] = Field(default_factory=list, max_length=500)
    resolved_contradiction_refs: List[str] = Field(default_factory=list, max_length=500)
    excluded_claim_family_refs: List[str] = Field(default_factory=list, max_length=500)
    exclusion_reasons: Dict[str, str] = Field(default_factory=dict)
    last_reviewed_at: Optional[str] = Field(None, min_length=1, max_length=80)


class GraphRAGPredicateGovernanceApprovalRequest(BaseModel):
    actor_ref: str
    approver_class: str


class GraphRAGPredicateExternalMappingRequest(BaseModel):
    mapping_kind: str
    biolink_predicate_curie: Optional[str] = None
    ro_predicate_curie: Optional[str] = None


class GraphRAGPredicateGovernanceRequest(BaseModel):
    predicate_change_ref: str
    predicate_ref: str
    change_kind: str
    impact_level: str
    definition: str
    subject_category: str
    object_category: str
    external_mappings: List[GraphRAGPredicateExternalMappingRequest] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)
    counterexamples: List[str] = Field(default_factory=list)
    migration_plan_ref: Optional[str] = None
    conformance_tests_ref: Optional[str] = None
    current_lifecycle_state: Optional[str] = None
    approvals: List[GraphRAGPredicateGovernanceApprovalRequest] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/query")
async def graphrag_query(
    body: GraphRAGQueryRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Hybrid search across GraphRAG edges and facts.

    Returns combined results sorted by hybrid_score (0.6×FTS + 0.4×pgvector).
    Scoped to user_id by default; use study_id/kb_id for product filtering.
    """
    store = await _get_graphrag_store(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
        global_only=body.global_only,
        user_id=user_id,
    )
    health_gate = _resolve_graph_public_quality_gate(
        request=request,
        scope_ref=scope_ref,
        public_surface=body.global_only,
    )
    if not health_gate.allow_answer:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "blocked",
                "reason": "graph_health_red",
                "operation": "graphrag_query",
                "health_gate": _serialize_graph_health_gate(health_gate),
            },
        )
    retrieval_contract = await _resolve_query_asof_contract(request=request, body=body)
    try:
        hits = await store.search_graph_hybrid(
            query_text=body.query_text,
            query_embedding=None,  # FTS-only when no embedding provided
            limit=body.limit,
            user_id=user_id,
            workspace_id=body.study_id or body.kb_id,
            global_only=body.global_only,
            include_edges=body.include_edges,
            include_facts=body.include_facts,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG query failed: {exc}")

    output_validator = OutputValidator()
    results: List[Dict[str, Any]] = []
    for hit in hits:
        item: Dict[str, Any] = {
            "result_type": hit.result_type,
            "hybrid_score": round(hit.hybrid_score, 4),
        }
        if hit.edge:
            item["edge"] = {
                "source_node": hit.edge.source_node,
                "source_type": hit.edge.source_type,
                "relationship": hit.edge.relationship,
                "target_node": hit.edge.target_node,
                "target_type": hit.edge.target_type,
                "details": hit.edge.details,
                "confidence": hit.edge.confidence,
                "source_doi": hit.edge.source_doi,
                "source_sentence": hit.edge.source_sentence,
                "extraction_method": hit.edge.extraction_method,
                "metadata": _safe_metadata(hit.edge.metadata),
            }
        if hit.fact:
            item["fact"] = {
                "content": hit.fact.content[:500],
                "fact_type": hit.fact.fact_type,
                "topic": hit.fact.topic,
                "entities": hit.fact.entities,
                "source_doi": hit.fact.source_doi,
                "confidence": hit.fact.confidence,
                "metadata": _safe_metadata(hit.fact.metadata),
            }
        results.append(output_validator.validate_item(item))

    answer_receipt = None
    if retrieval_contract is not None:
        answer_receipt_runtime = _get_graphrag_answer_receipt_runtime(request)
        if answer_receipt_runtime is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "blocked",
                    "reason": "graph_answer_receipt_runtime_unavailable",
                    "operation": "graphrag_query",
                },
            )
        if not isinstance(answer_receipt_runtime, GraphAnswerReceiptRuntime):
            raise HTTPException(
                status_code=500,
                detail={
                    "status": "blocked",
                    "reason": "graph_answer_receipt_runtime_invalid",
                    "operation": "graphrag_query",
                },
            )
        answer_receipt = answer_receipt_runtime.build_receipt(
            query_text=body.query_text,
            scope_ref=scope_ref,
            retrieval_contract=retrieval_contract,
            hits=results,
        ).as_dict()

    return {
        "query": body.query_text,
        "total": len(results),
        "hits": results,
        "health_gate": _serialize_graph_health_gate(health_gate),
        "retrieval_contract": retrieval_contract,
        "answer_receipt": answer_receipt,
    }


@router.post("/hop1")
async def graphrag_hop1(
    body: GraphRAGHop1Request,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """1-hop graph traversal from seed node names.

    Returns all edges where seed_nodes appear as source or target.
    Useful for expanding a set of entities to find their graph neighbors.
    """
    store = await _get_graphrag_store(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        global_only=body.global_only,
        user_id=user_id,
    )
    try:
        traversal, projection_guard, traversal_degradation = await _budgeted_hop1_with_projection_guard(
            request=request,
            store=store,
            seed_nodes=body.seed_nodes,
            limit=body.limit,
            user_id=user_id,
            workspace_id=body.study_id or body.kb_id,
            global_only=body.global_only,
            policy=body.traversal_policy,
            budget_ref=body.budget_ref,
            scope_ref=scope_ref,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG hop1 failed: {exc}")

    neighbor_nodes: set[str] = set()
    edge_list: List[Dict[str, Any]] = []
    for e in traversal.edges:
        edge_list.append({
            "source_node": e.source_node,
            "source_type": e.source_type,
            "relationship": e.relationship,
            "target_node": e.target_node,
            "target_type": e.target_type,
            "confidence": e.confidence,
            "source_doi": e.source_doi,
            "source_sentence": e.source_sentence,
            "metadata": _safe_metadata(e.metadata),
        })
        if e.source_node not in body.seed_nodes:
            neighbor_nodes.add(e.source_node)
        if e.target_node not in body.seed_nodes:
            neighbor_nodes.add(e.target_node)

    return {
        "seed_nodes": body.seed_nodes,
        "neighbor_nodes": sorted(neighbor_nodes),
        "total_edges": len(edge_list),
        "edges": edge_list,
        "projection_guard": projection_guard,
        "traversal_contract": {
            "traversal_request_ref": traversal.traversal_request_ref,
            "budget_ref": traversal.budget_ref,
            "policy": traversal.policy,
            "status": traversal.status,
            "degradation": traversal_degradation,
            "cost_event": {
                "traversal_request_ref": traversal.cost_event.traversal_request_ref,
                "budget_ref": traversal.cost_event.budget_ref,
                "policy": traversal.cost_event.policy,
                "visited_nodes": traversal.cost_event.visited_nodes,
                "visited_edges": traversal.cost_event.visited_edges,
                "returned_paths": traversal.cost_event.returned_paths,
                "cost_units": traversal.cost_event.cost_units,
                "status": traversal.cost_event.status,
                "latency_budget_ms": traversal.cost_event.latency_budget_ms,
            },
        },
    }


@router.post("/claim", status_code=201)
async def graphrag_write_claim(
    body: GraphRAGWriteClaimRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Write a scientific claim as a fact node with provenance.

    Auto-computes claim_key = SHA256(content + fact_type + topic + entities + source_doi).
    Deduplicates by claim_key + user_id.

    Product scope: study_id, kb_id, or working_set_id must be provided.
    """
    if not body.study_id and not body.kb_id and not body.working_set_id:
        raise HTTPException(
            status_code=422,
            detail="At least one of study_id, kb_id, or working_set_id is required for product-scoped writes",
        )
    if not body.source_doi and not body.source_resource_uri and not body.source_snippet_uri:
        raise HTTPException(
            status_code=422,
            detail="At least one of source_doi, source_resource_uri, or source_snippet_uri is required for provenance",
        )
    created_by_receipt_ref = _require_created_by_receipt_ref(
        created_by_receipt_ref=body.created_by_receipt_ref,
        operation="graphrag_write_claim",
    )

    store = await _get_graphrag_store(request)

    # Compute provenance hash
    prov_payload = {
        "content": body.content,
        "fact_type": body.fact_type,
        "topic": body.topic,
        "entities": sorted(body.entities),
        "source_doi": body.source_doi,
    }
    provenance_hash = hashlib.sha256(
        json.dumps(prov_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    metadata: Dict[str, Any] = {
        "study_id": body.study_id,
        "kb_id": body.kb_id,
        "working_set_id": body.working_set_id,
        "resource_uri": body.source_resource_uri,
        "snippet_uri": body.source_snippet_uri,
        "provenance_hash": provenance_hash,
        "source_tool": "graphrag_write_claim_api",
        "created_by_receipt_ref": created_by_receipt_ref,
        "semantic_context_ref": body.semantic_context_ref,
        "policy_scope": body.policy_scope,
        "graph_write_mode": "receipt_gated_canonical_write",
    }
    # Remove None values
    metadata = {k: v for k, v in metadata.items() if v is not None}

    try:
        await store.upsert_fact(
            content=body.content,
            fact_type=body.fact_type,
            topic=body.topic,
            entities=body.entities,
            source_doi=body.source_doi,
            confidence=body.confidence,
            user_id=user_id,
            metadata=metadata,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write claim: {exc}")

    claim_key = store._claim_key(store._fact_claim_payload(
        content=body.content,
        fact_type=body.fact_type,
        topic=body.topic,
        entities=body.entities,
        source_doi=body.source_doi,
        scope={"user_id": user_id},
    ))

    logger.info(
        "GraphRAG claim written: claim_key=%s, user=%s, study=%s, kb=%s",
        claim_key[:16], user_id, body.study_id, body.kb_id,
    )

    return {
        "status": "written",
        "claim_key": claim_key,
        "provenance_hash": provenance_hash,
        "created_by_receipt_ref": created_by_receipt_ref,
        "semantic_context_ref": body.semantic_context_ref,
        "policy_scope": body.policy_scope,
        "study_id": body.study_id,
        "kb_id": body.kb_id,
    }


@router.post("/paper", status_code=201)
async def graphrag_write_paper(
    body: GraphRAGWritePaperRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Ingest a paper with authors, abstract, and provenance into GraphRAG.

    Creates paper + author nodes, AUTHORED_BY edges, and abstract as a fact.
    """
    facade = await _get_graphrag_facade(request)

    # Build a paper-like object for the facade
    class _Paper:
        pass

    paper = _Paper()
    paper.title = body.title
    paper.doi = body.doi or ""
    paper.paper_id = body.paper_id
    paper.abstract_snippet = body.abstract
    paper.authors = body.authors
    paper.year = body.year
    paper.journal = body.journal or ""

    try:
        count = await facade.write_paper_record(
            paper,
            run_id="",
            kb_id=body.kb_id or "",
            user_id=user_id,
            session_id="",
            ingest_facts=bool(body.abstract.strip()),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write paper graph: {exc}")

    return {
        "status": "written",
        "nodes_edges_facts_created": count,
        "paper_id": body.paper_id,
        "study_id": body.study_id,
        "kb_id": body.kb_id,
    }


@router.post("/lmp", status_code=201)
async def graphrag_write_lmp(
    body: GraphRAGWriteLMPRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Parse an LMP v4 XML file and persist its biological graph into GraphRAG.

    Reuses the existing LMP v4 parser from graph.py.
    Writes protein/structure/domain/pathway/disease/pharmacology nodes and edges.
    Links everything to the provided study_id.
    """
    # Import parser here to avoid circular import at module level
    from mica.api_v1.routers.graph import _parse_lmp_v4_graph, _resolve_xml_bytes, _safe_filename

    store = await _get_graphrag_store(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        global_only=False,
        user_id=user_id,
    )

    try:
        safe_name = _safe_filename(body.lmp_file)
        xml_bytes, source = _resolve_xml_bytes(safe_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to resolve LMP file: {exc}")

    try:
        graph = _parse_lmp_v4_graph(xml_bytes, filename=safe_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse LMP v4 XML: {exc}")

    # Filter node types
    allowed_types = set(body.node_types_to_include)
    nodes_to_persist = [n for n in graph["nodes"] if n.get("kind", "entity") in allowed_types]

    # Write nodes
    node_count = 0
    lmp_resource_uri = f"mica://lmp/v4/{safe_name}"
    metadata_base = {
        "study_id": body.study_id,
        "kb_id": body.kb_id,
        "working_set_id": body.working_set_id,
        "resource_uri": lmp_resource_uri,
        "source_tool": "graphrag_write_lmp_api",
        "lmp_file": safe_name,
        "budo_id": graph.get("meta", {}).get("budo_id"),
        "uniprot_id": graph.get("meta", {}).get("uniprot_id"),
    }
    metadata_base = {k: v for k, v in metadata_base.items() if v is not None}

    for node in nodes_to_persist:
        try:
            await store.upsert_node(
                canonical_name=node["id"],
                node_type=node.get("kind", "entity"),
                aliases=[node.get("label", "")] if node.get("label") != node["id"] else [],
                description=None,
                external_ids=node.get("props") or {},
                properties={"source": "lmp_v4"},
                source_doi=[],
            )
            node_count += 1
        except Exception as exc:
            logger.warning("Failed to upsert LMP node %s: %s", node.get("id"), exc)

    # Write edges
    edge_count = 0
    links = graph.get("links", [])[:body.max_edges]
    for link in links:
        src = link.get("source", "")
        tgt = link.get("target", "")
        if not src or not tgt:
            continue
        src_kind = _node_kind_from_id(src)
        tgt_kind = _node_kind_from_id(tgt)
        if src_kind not in allowed_types and tgt_kind not in allowed_types:
            continue

        rel = link.get("type") or "HAS_KNOWLEDGE"
        edge_metadata = dict(metadata_base)
        edge_metadata["lmp_edge_db"] = link.get("db")
        edge_metadata["lmp_edge_id"] = link.get("id")

        try:
            await store.insert_edge(
                source_node=src,
                source_type=src_kind,
                target_node=tgt,
                target_type=tgt_kind,
                relationship=rel,
                confidence=1.0,
                extraction_method="lmp_v4_parse",
                metadata=edge_metadata,
                user_id=user_id,
            )
            edge_count += 1
        except Exception as exc:
            logger.warning("Failed to insert LMP edge %s→%s: %s", src, tgt, exc)

    logger.info(
        "LMP graph persisted: file=%s, nodes=%d, edges=%d, study=%s",
        safe_name, node_count, edge_count, body.study_id,
    )

    return {
        "status": "written",
        "lmp_file": safe_name,
        "source": source,
        "budo_id": graph.get("meta", {}).get("budo_id"),
        "uniprot_id": graph.get("meta", {}).get("uniprot_id"),
        "total_nodes_in_file": len(graph["nodes"]),
        "total_edges_in_file": len(graph.get("links", [])),
        "nodes_persisted": node_count,
        "edges_persisted": edge_count,
        "study_id": body.study_id,
    }


@router.get("/stats", response_model=GraphRAGStatsResponse)
async def graphrag_stats(
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Get GraphRAG node, edge, and fact counts."""
    store = await _get_graphrag_store(request)
    try:
        await store.initialize()
        async with store._pool.acquire() as conn:
            node_row = await conn.fetchrow(
                f"SELECT COUNT(*) FROM {store.schema}.atom_graph_nodes"
            )
            edge_row = await conn.fetchrow(
                f"SELECT COUNT(*) FROM {store.schema}.atom_graph_edges WHERE user_id = $1", user_id
            )
            fact_row = await conn.fetchrow(
                f"SELECT COUNT(*) FROM {store.schema}.atom_facts WHERE user_id = $1", user_id
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {exc}")

    return GraphRAGStatsResponse(
        schema=store.schema,
        node_count=node_row["count"] if node_row else 0,
        edge_count=edge_row["count"] if edge_row else 0,
        fact_count=fact_row["count"] if fact_row else 0,
    )


@router.post("/aggregate")
async def graphrag_aggregate(
    body: GraphRAGAggregateRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Serve aggregate graph signals under a privacy-preserving policy."""
    if body.metric not in {"edge_count", "degree", "centrality"}:
        raise HTTPException(status_code=422, detail={"reason": "unsupported_aggregate_metric"})
    if body.metric == "degree" and not body.node_name:
        raise HTTPException(status_code=422, detail={"reason": "node_name_required_for_degree"})

    store = await _get_graphrag_store(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
        global_only=body.global_only,
        user_id=user_id,
    )
    policy = GraphAggregatePolicy()
    canary = LeakageCanary()
    workspace_id = body.study_id or body.kb_id or body.working_set_id

    if body.metric == "centrality":
        decision = policy.decide(
            metric="centrality",
            scope_ref=scope_ref,
            global_only=body.global_only,
        )
        if decision.blocked:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": decision.reason,
                    "policy": {
                        "metric": decision.metric,
                        "scope_ref": decision.scope_ref,
                        "private_scope": decision.private_scope,
                        "exposure": decision.exposure,
                        "threshold": decision.threshold,
                    },
                },
            )
        try:
            ranking = await store.rank_nodes_by_degree(
                top_k=body.top_k,
                user_id=user_id,
                workspace_id=workspace_id,
                global_only=body.global_only,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"GraphRAG aggregate failed: {exc}")
        canary_result = canary.inspect_current(decision=decision)
        return {
            "metric": body.metric,
            "scope_ref": scope_ref,
            "policy": {
                "metric": decision.metric,
                "private_scope": decision.private_scope,
                "blocked": decision.blocked,
                "exposure": decision.exposure,
                "threshold": decision.threshold,
                "exact_value_exposed": decision.exact_value_exposed,
                "reason": decision.reason,
            },
            "canary": {
                "status": canary_result.status,
                "leakage_detected": canary_result.leakage_detected,
                "exact_value_exposed": canary_result.exact_value_exposed,
                "reason": canary_result.reason,
            },
            "result": {
                "top_k": body.top_k,
                "ranking": ranking,
            },
        }

    try:
        if body.metric == "edge_count":
            exact_value = await store.aggregate_edge_count(
                user_id=user_id,
                workspace_id=workspace_id,
                global_only=body.global_only,
            )
        else:
            exact_value = await store.aggregate_node_degree(
                node_name=str(body.node_name),
                user_id=user_id,
                workspace_id=workspace_id,
                global_only=body.global_only,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG aggregate failed: {exc}")

    decision = policy.decide(
        metric=body.metric,
        scope_ref=scope_ref,
        global_only=body.global_only,
        exact_value=exact_value,
        exact_payload={body.metric: exact_value},
    )
    canary_result = canary.inspect_current(decision=decision)
    return {
        "metric": body.metric,
        "scope_ref": scope_ref,
        "policy": {
            "metric": decision.metric,
            "private_scope": decision.private_scope,
            "blocked": decision.blocked,
            "exposure": decision.exposure,
            "threshold": decision.threshold,
            "exact_value_exposed": decision.exact_value_exposed,
            "reason": decision.reason,
        },
        "canary": {
            "status": canary_result.status,
            "leakage_detected": canary_result.leakage_detected,
            "exact_value_exposed": canary_result.exact_value_exposed,
            "reason": canary_result.reason,
        },
        "result": decision.payload,
    }


@router.post("/golden-release-gate", status_code=200)
async def graphrag_golden_release_gate(
    body: GraphRAGGoldenReleaseGateRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Evaluate the hard production release gate for Golden GraphRAG v3."""
    del user_id  # authenticated operator surface; no per-user variance in the gate logic
    runtime = _get_graphrag_golden_release_runtime(request)
    metrics = runtime.derive_metrics(
        query_outputs=body.query_outputs,
        leakage_canaries=body.leakage_canaries,
        replay_results=body.replay_results,
        traversal_latencies_ms=body.traversal_latencies_ms,
        hypothesis_labels=body.hypothesis_labels,
        fallback_metrics=body.fallback_metrics,
    )
    verdict = runtime.evaluate(metrics=metrics, evidence_refs=body.evidence_refs)
    return verdict.as_dict()


@router.post("/predicate-governance/review", status_code=200)
async def graphrag_review_predicate_change(
    body: GraphRAGPredicateGovernanceRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Review a predicate lifecycle change before it can touch the registry."""
    del user_id
    runtime = _get_graphrag_predicate_governance_runtime(request)
    try:
        change = PredicateChangeRequest(
            predicate_change_ref=body.predicate_change_ref,
            predicate_ref=body.predicate_ref,
            change_kind=PredicateChangeKind(body.change_kind.strip().lower()),
            impact_level=PredicateImpactLevel(body.impact_level.strip().lower()),
            definition=body.definition,
            domain_range=PredicateDomainRange(
                subject_category=body.subject_category,
                object_category=body.object_category,
            ),
            external_mappings=tuple(
                PredicateExternalMapping(
                    mapping_kind=PredicateMappingKind(mapping.mapping_kind.strip().lower()),
                    biolink_predicate_curie=mapping.biolink_predicate_curie,
                    ro_predicate_curie=mapping.ro_predicate_curie,
                )
                for mapping in body.external_mappings
            ),
            examples=tuple(body.examples),
            counterexamples=tuple(body.counterexamples),
            migration_plan_ref=body.migration_plan_ref,
            conformance_tests_ref=body.conformance_tests_ref,
            current_lifecycle_state=(
                PredicateLifecycleState(body.current_lifecycle_state.strip().lower())
                if body.current_lifecycle_state
                else None
            ),
            approvals=tuple(
                GovernanceApproval(
                    actor_ref=approval.actor_ref,
                    approver_class=approval.approver_class,
                )
                for approval in body.approvals
            ),
        )
        receipt = runtime.review_change(change)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_review_predicate_change",
            },
        )

    return {
        "status": receipt.decision,
        "receipt": {
            "receipt_ref": receipt.receipt_ref,
            "predicate_change_ref": receipt.predicate_change_ref,
            "predicate_ref": receipt.predicate_ref,
            "change_kind": receipt.change_kind,
            "impact_level": receipt.impact_level,
            "decision": receipt.decision,
            "governance_policy_ref": receipt.governance_policy_ref,
            "registry_write_allowed": receipt.registry_write_allowed,
            "exact_mapping_allowed": receipt.exact_mapping_allowed,
            "required_approver_classes": list(receipt.required_approver_classes),
            "satisfied_approver_classes": list(receipt.satisfied_approver_classes),
            "reason_codes": list(receipt.reason_codes),
            "approval_receipt_ref": receipt.approval_receipt_ref,
            "registry_resolution": receipt.registry_resolution,
            "external_mapping_receipts": [
                {
                    "mapping_kind": mapping.mapping_kind,
                    "biolink_predicate_curie": mapping.biolink_predicate_curie,
                    "ro_predicate_curie": mapping.ro_predicate_curie,
                    "exact_traversal_allowed": mapping.exact_traversal_allowed,
                }
                for mapping in receipt.external_mapping_receipts
            ],
        },
    }


@router.post("/export-decision-subgraph")
async def graphrag_export_decision_subgraph(
    body: GraphRAGExportDecisionSubgraphRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Export a bounded subgraph suitable for a Scientific Decision Card.

    Combines semantic search + hop-1 traversal for the query focus.
    """
    store = await _get_graphrag_store(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        global_only=False,
        user_id=user_id,
    )

    try:
        # Step 1: Search for relevant facts
        facts = await store.search_facts_hybrid(
            query_text=body.query_focus,
            query_embedding=None,
            limit=body.max_nodes,
            user_id=user_id,
            workspace_id=body.study_id or body.kb_id,
        )

        # Step 2: Extract entity names from facts
        entity_names: set[str] = set()
        for f in facts:
            for entity in (f.entities or []):
                entity_names.add(str(entity).strip())

        # Step 3: Hop-1 from entities
        traversal, projection_guard, traversal_degradation = await _budgeted_hop1_with_projection_guard(
            request=request,
            store=store,
            seed_nodes=list(entity_names)[:50],
            limit=body.max_edges,
            user_id=user_id,
            workspace_id=body.study_id or body.kb_id,
            policy=body.traversal_policy,
            budget_ref=body.budget_ref,
            global_only=False,
            scope_ref=scope_ref,
        )

        # Step 4: Build subgraph
        nodes_set: set[str] = set()
        for e in traversal.edges:
            nodes_set.add(e.source_node)
            nodes_set.add(e.target_node)
        for f in facts:
            nodes_set.update(f.entities or [])

        subgraph_edges: List[Dict[str, Any]] = []
        for e in traversal.edges:
            subgraph_edges.append({
                "source_node": e.source_node,
                "source_type": e.source_type,
                "relationship": e.relationship,
                "target_node": e.target_node,
                "target_type": e.target_type,
                "confidence": e.confidence,
                "source_doi": e.source_doi,
                "source_sentence": e.source_sentence,
            })

        subgraph_facts: List[Dict[str, Any]] = []
        for f in facts:
            subgraph_facts.append({
                "content": f.content[:500],
                "fact_type": f.fact_type,
                "entities": f.entities,
                "source_doi": f.source_doi,
                "confidence": f.confidence,
                "claim_key": (f.metadata or {}).get("claim_key") if f.metadata else None,
            })

        inference_engine = GraphInferenceProposalEngine()
        inference_proposals = [
            proposal.as_dict()
            for proposal in inference_engine.propose_from_edges(
                edges=traversal.edges,
                max_proposals=body.max_inference_proposals,
            )
        ]
        node2vec_runtime = _get_graphrag_node2vec_runtime(request)
        node2vec_payload = None
        if node2vec_runtime is not None:
            node2vec_payload = node2vec_runtime.expand_candidates(
                seed_node_refs=[f"entity://{name}" for name in sorted(entity_names)],
                traversed_node_refs=[f"entity://{name}" for name in sorted(nodes_set)],
            ).as_dict()

        return {
            "query_focus": body.query_focus,
            "node_count": len(nodes_set),
            "edge_count": len(subgraph_edges),
            "fact_count": len(subgraph_facts),
            "nodes": sorted(nodes_set),
            "edges": subgraph_edges,
            "facts": subgraph_facts,
            "inference_contract": {
                "proposal_count": len(inference_proposals),
                "materialized_as_claims": 0,
                "max_status": "hypothesis",
                "materialization_policy": "never_active_edge_without_receipt",
            },
            "inference_proposals": inference_proposals,
            "node2vec_contract": node2vec_payload,
            "study_id": body.study_id,
            "kb_id": body.kb_id,
            "projection_guard": projection_guard,
            "traversal_contract": {
                "traversal_request_ref": traversal.traversal_request_ref,
                "budget_ref": traversal.budget_ref,
                "policy": traversal.policy,
                "status": traversal.status,
                "degradation": traversal_degradation,
                "cost_event": {
                    "traversal_request_ref": traversal.cost_event.traversal_request_ref,
                    "budget_ref": traversal.cost_event.budget_ref,
                    "policy": traversal.cost_event.policy,
                    "visited_nodes": traversal.cost_event.visited_nodes,
                    "visited_edges": traversal.cost_event.visited_edges,
                    "returned_paths": traversal.cost_event.returned_paths,
                    "cost_units": traversal.cost_event.cost_units,
                    "status": traversal.cost_event.status,
                    "latency_budget_ms": traversal.cost_event.latency_budget_ms,
                },
            },
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to export decision subgraph: {exc}")


@router.post("/promote-from-kb", status_code=202)
async def graphrag_promote_from_kb(
    body: GraphRAGPromoteFromKBRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Extract entities and claims from KB documents and persist into GraphRAG.

    This is an async operation — returns a job receipt immediately.
    The actual extraction runs in the background.
    """
    created_by_receipt_ref = _require_created_by_receipt_ref(
        created_by_receipt_ref=body.created_by_receipt_ref,
        operation="graphrag_promote_from_kb",
    )
    store = await _get_graphrag_store(request)

    # Resolve kb_service to load KB documents
    kb_service = getattr(request.app.state, "kb_service", None)
    if kb_service is None:
        raise HTTPException(status_code=503, detail="KB service not available")

    try:
        kb = await kb_service.get_kb(body.kb_id, owner_id=user_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="KB not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load KB: {exc}")

    # For now: extract entities from KB name + canonical_query + target_entities
    # Full document-level extraction would need KB document loading.
    entities = list(kb.target_entities or [])
    if kb.canonical_query:
        # Simple keyword extraction from canonical query
        import re
        words = re.findall(r'[A-Z][A-Za-z0-9]+|[A-Z]{2,}', kb.canonical_query)
        entities.extend(w for w in words if len(w) > 1)

    entity_count = 0
    for entity_name in set(entities[:body.max_claims]):
        try:
            await store.upsert_node(
                canonical_name=entity_name,
                node_type="entity",
                aliases=[],
                description=f"Extracted from KB: {kb.name}",
                external_ids={"kb_id": body.kb_id},
                properties={"source": "kb_promotion"},
                source_doi=[],
            )
            entity_count += 1
        except Exception as exc:
            logger.warning("Failed to upsert entity %s from KB: %s", entity_name, exc)

    # Write KB→entity edges
    edge_count = 0
    for entity_name in set(entities[:body.max_claims]):
        try:
            await store.insert_edge(
                source_node=body.kb_id,
                source_type="knowledge_base",
                target_node=entity_name,
                target_type="entity",
                relationship="CONTAINS_ENTITY",
                confidence=0.8,
                extraction_method=body.extraction_method,
                metadata={
                    "kb_id": body.kb_id,
                    "study_id": body.study_id,
                    "source_tool": "graphrag_promote_from_kb",
                    "created_by_receipt_ref": created_by_receipt_ref,
                    "semantic_context_ref": body.semantic_context_ref,
                    "policy_scope": body.policy_scope,
                    "edge_status": "active",
                    "graph_write_mode": "receipt_gated_canonical_write",
                },
                user_id=user_id,
            )
            edge_count += 1
        except Exception as exc:
            logger.warning("Failed to insert KB→entity edge: %s", exc)

    logger.info(
        "KB→GraphRAG promotion: kb=%s, entities=%d, edges=%d",
        body.kb_id, entity_count, edge_count,
    )

    return {
        "status": "promoted",
        "kb_id": body.kb_id,
        "kb_name": kb.name,
        "entities_extracted": len(set(entities[:body.max_claims])),
        "entities_persisted": entity_count,
        "edges_persisted": edge_count,
        "created_by_receipt_ref": created_by_receipt_ref,
        "semantic_context_ref": body.semantic_context_ref,
        "policy_scope": body.policy_scope,
        "study_id": body.study_id,
    }


@router.post("/curate-edge", status_code=200)
async def graphrag_curate_edge(
    body: GraphRAGEdgeCurationRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Curate an existing GraphRAG edge through the canonical store only.

    GP9 forbids direct projection mutation. The correction lands on the
    canonical edge and the projector is expected to rebuild from receipts.
    """
    source_receipt_ref = _require_created_by_receipt_ref(
        created_by_receipt_ref=body.created_by_receipt_ref,
        operation="graphrag_curate_edge",
    )
    runtime = _get_graphrag_curation_runtime(request)
    store = await _get_graphrag_store(request)
    task = GraphCurationTask(
        source_node=body.source_node,
        source_type=body.source_type,
        target_node=body.target_node,
        target_type=body.target_type,
        relationship=body.relationship,
        corrected_edge_status=body.corrected_edge_status.strip().lower(),
        actor_ref=(body.actor_ref or f"user:{user_id}").strip(),
        actor_role=body.actor_role,
        source_receipt_ref=source_receipt_ref,
        reason_codes=tuple(body.reason_codes),
        details=body.details,
        confidence=body.confidence,
        source_doi=body.source_doi,
        source_sentence=body.source_sentence,
        extraction_method=body.extraction_method,
        policy_scope=body.policy_scope,
        semantic_context_ref=body.semantic_context_ref,
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
        contradiction_claim_ref=body.contradiction_claim_ref,
        contradiction_kind=body.contradiction_kind,
        supporting_edge_refs=tuple(body.supporting_edge_refs),
        contradicting_edge_refs=tuple(body.contradicting_edge_refs),
    )
    try:
        result = await runtime.curate_edge(
            store=store,
            task=task,
            user_id=user_id,
            session_id=None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_curate_edge",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to curate edge: {exc}")

    return {
        "status": "curated",
        "receipt": {
            "receipt_ref": result.receipt.receipt_ref,
            "task_ref": result.receipt.task_ref,
            "edge_ref": result.receipt.edge_ref,
            "source_receipt_ref": result.receipt.source_receipt_ref,
            "corrected_edge_status": result.receipt.corrected_edge_status,
            "actor_ref": result.receipt.actor_ref,
            "actor_role": result.receipt.actor_role,
            "reason_codes": list(result.receipt.reason_codes),
            "neo4j_projection_action": result.receipt.neo4j_projection_action,
            "contradiction_ref": result.receipt.contradiction_ref,
        },
        "credit": {
            "score": result.credit.score,
            "impact_class": result.credit.impact_class,
            "weighted_reasons": result.credit.weighted_reasons,
        },
        "canonical_write": result.edge_metadata,
    }


@router.post("/knowledge-maintenance-budget", status_code=200)
async def graphrag_knowledge_maintenance_budget(
    body: GraphRAGKnowledgeMaintenanceBudgetRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Plan finite curation/maintenance budget over receipted graph edges."""
    del user_id  # authenticated operator surface; budget policy is lane-level, not per-user
    runtime = _get_graphrag_knowledge_economics_runtime(request)
    priorities = [
        runtime.derive_curation_priority(
            edge_ref=item.edge_ref,
            maintenance_cost=item.maintenance_cost,
            scientific_impact=item.scientific_impact,
            downstream_usage=item.downstream_usage,
            contradiction_pressure=item.contradiction_pressure,
            public_visibility=item.public_visibility,
            retraction_risk=item.retraction_risk,
        )
        for item in body.candidates
    ]
    budget = runtime.allocate_knowledge_maintenance_budget(
        budget_ref=body.budget_ref,
        subsidy_ref=body.subsidy_ref,
        available_units=body.available_units,
        priorities=priorities,
    )
    return {
        "status": "planned",
        "policy_ref": runtime.policy_ref,
        "budget": {
            "budget_ref": budget.budget_ref,
            "subsidy_ref": budget.subsidy_ref,
            "available_units": budget.available_units,
            "allocated_units": budget.allocated_units,
            "remaining_units": budget.remaining_units,
            "uncovered_edge_refs": list(budget.uncovered_edge_refs),
            "allocations": [
                {
                    "edge_ref": item.edge_ref,
                    "priority_score": item.priority_score,
                    "maintenance_cost": item.maintenance_cost,
                    "allocated_units": item.allocated_units,
                    "recommended_action": item.recommended_action,
                }
                for item in budget.allocations
            ],
        },
        "priorities": [
            {
                "edge_ref": item.edge_ref,
                "priority_ref": item.priority_ref,
                "priority_score": item.priority_score,
                "recommended_action": item.recommended_action,
                "scientific_impact": item.scientific_impact,
                "downstream_usage": item.downstream_usage,
                "contradiction_pressure": item.contradiction_pressure,
                "public_visibility": item.public_visibility,
                "retraction_risk": item.retraction_risk,
                "maintenance_cost": item.maintenance_cost,
            }
            for item in priorities
        ],
    }


@router.post("/commons-package/review", status_code=200)
async def graphrag_review_commons_package(
    body: GraphRAGCommonsPackageReviewRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Review a public commons package over the existing federated import seam.

    This does not import or promote truth. It only decides whether a signed
    `external_asserted` package is admissible as a public commons export.
    """
    del user_id  # operator identity is auth-gated; commons admission stays package/issuer scoped
    runtime = _get_graphrag_commons_runtime(request)
    try:
        decision = runtime.review_commons_package(
            package_ref=body.package_ref,
            source_instance=body.source_instance,
            trust_status=body.trust_status,
            signature_verified=body.signature_verified,
            federation_state=body.federation_state,
            publication_scope=body.publication_scope,
            edge_refs=body.edge_refs,
            receipt_refs=body.receipt_refs,
            prior_retractions=body.prior_retractions,
            poisoning_incidents=body.poisoning_incidents,
            local_promotions=body.local_promotions,
            curator_endorsements=body.curator_endorsements,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_review_commons_package",
            },
        )

    issuer_profile = decision.issuer_profile
    payload = {
        "status": decision.status,
        "decision": {
            "decision_ref": decision.decision_ref,
            "package_ref": decision.package_ref,
            "source_instance": decision.source_instance,
            "admission_policy_ref": decision.admission_policy_ref,
            "anti_poisoning_flags": list(decision.anti_poisoning_flags),
            "reason_codes": list(decision.reason_codes),
            "created_by_receipt_ref": decision.created_by_receipt_ref,
        },
        "issuer_profile": {
            "issuer_profile_ref": issuer_profile.issuer_profile_ref,
            "source_instance": issuer_profile.source_instance,
            "reputation_score": issuer_profile.reputation_score,
            "trust_status": issuer_profile.trust_status,
            "signature_verified": issuer_profile.signature_verified,
            "prior_retractions": issuer_profile.prior_retractions,
            "poisoning_incidents": issuer_profile.poisoning_incidents,
            "local_promotions": issuer_profile.local_promotions,
            "curator_endorsements": issuer_profile.curator_endorsements,
            "reputation_tier": issuer_profile.reputation_tier,
            "anti_poisoning_flags": list(issuer_profile.anti_poisoning_flags),
        },
    }
    if decision.commons_package is not None:
        payload["commons_package"] = {
            "commons_package_ref": decision.commons_package.commons_package_ref,
            "package_ref": decision.commons_package.package_ref,
            "source_instance": decision.commons_package.source_instance,
            "publication_scope": decision.commons_package.publication_scope,
            "edge_refs": list(decision.commons_package.edge_refs),
            "receipt_refs": list(decision.commons_package.receipt_refs),
            "issuer_profile_ref": decision.commons_package.issuer_profile_ref,
            "federation_state": decision.commons_package.federation_state,
            "provenance_policy_ref": decision.commons_package.provenance_policy_ref,
        }
    return payload


@router.post("/commons-package/publish-subgraph", status_code=200)
async def graphrag_publish_commons_subgraph(
    body: GraphRAGCommonsSubgraphPublicationRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Publish only the public/exportable subset of an admitted commons package."""
    del user_id
    runtime = _get_graphrag_commons_runtime(request)
    scope_ref = "scope://global" if str(body.publication_scope).strip().lower() == "global" else "scope://org"
    quality_gate = _resolve_graph_public_quality_gate(
        request=request,
        scope_ref=scope_ref,
        public_surface=True,
    )
    if not quality_gate.allow_answer:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "blocked",
                "reason": "graph_knowledge_debt_red",
                "operation": "graphrag_publish_commons_subgraph",
                "health_gate": _serialize_graph_health_gate(quality_gate),
            },
        )
    try:
        decision = runtime.publish_external_subgraph(
            commons_package_ref=body.commons_package_ref,
            package_ref=body.package_ref,
            source_instance=body.source_instance,
            federation_state=body.federation_state,
            publication_scope=body.publication_scope,
            edge_refs=body.edge_refs,
            receipt_refs=body.receipt_refs,
            edge_policy_scopes=body.edge_policy_scopes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_publish_commons_subgraph",
            },
        )

    payload = {
        "status": decision.status,
        "health_gate": _serialize_graph_health_gate(quality_gate),
        "decision": {
            "decision_ref": decision.decision_ref,
            "commons_package_ref": decision.commons_package_ref,
            "publication_policy_ref": decision.publication_policy_ref,
            "reason_codes": list(decision.reason_codes),
            "created_by_receipt_ref": decision.created_by_receipt_ref,
        },
    }
    if decision.publication is not None:
        payload["publication"] = {
            "publication_ref": decision.publication.publication_ref,
            "commons_package_ref": decision.publication.commons_package_ref,
            "package_ref": decision.publication.package_ref,
            "source_instance": decision.publication.source_instance,
            "publication_scope": decision.publication.publication_scope,
            "exported_edge_refs": list(decision.publication.exported_edge_refs),
            "exported_receipt_refs": list(decision.publication.exported_receipt_refs),
            "blocked_edge_refs": list(decision.publication.blocked_edge_refs),
            "federation_state": decision.publication.federation_state,
            "provenance_policy_ref": decision.publication.provenance_policy_ref,
        }
    return payload


@router.post("/commons-package/local-promotion-candidate", status_code=200)
async def graphrag_create_local_promotion_candidate(
    body: GraphRAGLocalPromotionCandidateRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Emit a local-promotion candidate without promoting external truth locally."""
    del user_id
    runtime = _get_graphrag_commons_runtime(request)
    try:
        decision = runtime.create_local_promotion_candidate(
            commons_package_ref=body.commons_package_ref,
            source_publication_ref=body.source_publication_ref,
            package_ref=body.package_ref,
            source_instance=body.source_instance,
            federation_state=body.federation_state,
            target_scope=body.target_scope,
            candidate_edge_refs=body.candidate_edge_refs,
            evidence_receipt_refs=body.evidence_receipt_refs,
            supporting_local_receipt_refs=body.supporting_local_receipt_refs,
            requested_by=body.requested_by,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_create_local_promotion_candidate",
            },
        )

    payload = {
        "status": decision.status,
        "decision": {
            "decision_ref": decision.decision_ref,
            "commons_package_ref": decision.commons_package_ref,
            "promotion_policy_ref": decision.promotion_policy_ref,
            "reason_codes": list(decision.reason_codes),
            "created_by_receipt_ref": decision.created_by_receipt_ref,
        },
    }
    if decision.candidate is not None:
        payload["candidate"] = {
            "candidate_ref": decision.candidate.candidate_ref,
            "commons_package_ref": decision.candidate.commons_package_ref,
            "source_publication_ref": decision.candidate.source_publication_ref,
            "package_ref": decision.candidate.package_ref,
            "source_instance": decision.candidate.source_instance,
            "target_scope": decision.candidate.target_scope,
            "candidate_edge_refs": list(decision.candidate.candidate_edge_refs),
            "evidence_receipt_refs": list(decision.candidate.evidence_receipt_refs),
            "supporting_local_receipt_refs": list(decision.candidate.supporting_local_receipt_refs),
            "requested_by": decision.candidate.requested_by,
            "candidate_status": decision.candidate.candidate_status,
            "provenance_policy_ref": decision.candidate.provenance_policy_ref,
        }
    return payload


@router.post("/durability/export-portable-canon", status_code=200)
async def graphrag_export_portable_canon(
    body: GraphRAGPortableCanonExportRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Export a portable graph canon bundle for decade-scale replay."""
    store = await _get_graphrag_store(request)
    runtime = _get_graphrag_durability_runtime(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
        global_only=body.global_only,
        user_id=user_id,
    )
    try:
        bundle = await runtime.export_bundle_from_store(
            store=store,
            scope_ref=scope_ref,
            as_of_ts=body.as_of_ts,
            transaction_as_of_ts=body.transaction_as_of_ts,
            graph_snapshot_manifest_ref=body.graph_snapshot_manifest_ref,
            vector_index_manifest_ref=body.vector_index_manifest_ref,
            predicate_registry_snapshot_ref=body.predicate_registry_snapshot_ref,
            predicate_registry_version=body.predicate_registry_snapshot_ref,
            semantic_contract_bundle_ref=body.semantic_contract_bundle_ref,
            schema_version_ref=body.schema_version_ref,
            doctrine_version_ref=body.doctrine_version_ref,
            receipt_manifest_refs=body.receipt_manifest_refs,
            migration_manifest_refs=body.migration_manifest_refs,
            user_id=user_id,
            workspace_id=body.study_id or body.kb_id,
            global_only=body.global_only,
            limit=body.limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_export_portable_canon",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG durable export failed: {exc}")
    return {
        "status": "exported",
        "bundle": bundle.as_dict(),
    }


@router.post("/durability/replay-as-of", status_code=200)
async def graphrag_replay_portable_canon_as_of(
    body: GraphRAGReplayAsOfRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Replay a portable graph canon bundle at a long-horizon as-of timestamp."""
    del user_id
    runtime = _get_graphrag_durability_runtime(request)
    try:
        replay = runtime.replay_as_of(
            bundle=body.bundle,
            requested_as_of_ts=body.requested_as_of_ts,
            predicate_registry_snapshot_ref=body.predicate_registry_snapshot_ref,
            schema_version_ref=body.schema_version_ref,
            doctrine_version_ref=body.doctrine_version_ref,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_replay_portable_canon_as_of",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG durable replay failed: {exc}")
    return {
        "status": "replayed",
        "replay": replay.as_dict(),
    }


@router.post("/health/report", status_code=200)
async def graphrag_build_health_report(
    body: GraphRAGHealthReportRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Build and optionally register a knowledge-debt health report by scope."""
    store = await _get_graphrag_store(request)
    runtime = _get_graphrag_knowledge_debt_runtime(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
        global_only=body.global_only,
        user_id=user_id,
    )
    audit_inputs = GraphHealthAuditInputs(
        stale_edge_count=body.stale_edge_count,
        orphan_node_count=body.orphan_node_count,
        orphan_edge_count=body.orphan_edge_count,
        open_contradiction_count=body.open_contradiction_count,
        hidden_critical_debt_count=body.hidden_critical_debt_count,
        owner_ref=body.owner_ref,
        sample_size=body.sample_size,
        domain_usage_weights=body.domain_usage_weights,
        issuer_risk_weights=body.issuer_risk_weights,
    )
    try:
        report = await runtime.build_report_from_store(
            store=store,
            scope_ref=scope_ref,
            user_id=user_id,
            workspace_id=body.study_id or body.kb_id,
            global_only=body.global_only,
            audit_inputs=audit_inputs,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_build_health_report",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GraphRAG health report failed: {exc}")

    if body.register_scope:
        runtime.register_report(report)

    return {
        "status": "reported",
        "registered": body.register_scope,
        "report": _serialize_graph_health_report(report),
    }


@router.post("/usage-signal", status_code=200)
async def graphrag_review_usage_signal(
    body: GraphRAGUsageSignalRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Review a GraphRAG usage signal under governed-learning constraints."""
    runtime = _get_graphrag_governed_learning_runtime(request)
    scope_ref = _build_graphrag_scope_ref(
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
        global_only=body.global_only,
        user_id=user_id,
    )
    try:
        decision = runtime.review_usage_signal(
            signal_kind=body.signal_kind,
            path_ref=body.path_ref,
            edge_refs=body.edge_refs,
            actor_ref=body.actor_ref or user_id,
            receipt_ref=body.receipt_ref,
            scope_ref=scope_ref,
            proposed_effects=body.proposed_effects,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_review_usage_signal",
            },
        )
    return decision.as_dict()


@router.post("/node2vec/governed-retraining-plan", status_code=200)
async def graphrag_plan_governed_retraining(
    body: GraphRAGGovernedRetrainingRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    """Plan a shadow-only node2vec retraining cycle under governed-learning rules."""
    del user_id
    runtime = _get_graphrag_governed_learning_runtime(request)
    plan = runtime.plan_node2vec_retraining(
        frozen_graph_snapshot_ref=body.frozen_graph_snapshot_ref,
        candidate_model_ref=body.candidate_model_ref,
        vector_snapshot_ref=body.vector_snapshot_ref,
        control_metrics=Node2VecABMetrics(**body.control_metrics.model_dump()),
        treatment_metrics=Node2VecABMetrics(**body.treatment_metrics.model_dump()),
        query_outputs=body.query_outputs,
        leakage_canaries=body.leakage_canaries,
        replay_results=body.replay_results,
        traversal_latencies_ms=body.traversal_latencies_ms,
        hypothesis_labels=body.hypothesis_labels,
        fallback_metrics=body.fallback_metrics,
        includes_unvalidated_inferred_edges=body.includes_unvalidated_inferred_edges,
        uses_usage_popularity_as_evidence=body.uses_usage_popularity_as_evidence,
        usage_signal_refs=body.usage_signal_refs,
    )
    return plan.as_dict()


@router.post("/entity-identity/decision", status_code=200)
async def graphrag_issue_entity_identity_decision(
    body: GraphRAGEntityIdentityDecisionRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    del user_id
    runtime = _get_graphrag_entity_identity_runtime(request)
    try:
        decision = runtime.issue_identity_decision(
            local_entity_ref=body.local_entity_ref,
            external_entity_refs=body.external_entity_refs,
            relation_kind=body.relation_kind,
            confidence=body.confidence,
            resolver_version_ref=body.resolver_version_ref,
            evidence_refs=body.evidence_refs,
            decision_receipt_ref=body.decision_receipt_ref,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            disputed=body.disputed,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_issue_entity_identity_decision",
            },
        )
    return decision.as_dict()


@router.post("/entity-identity/conflict-review", status_code=200)
async def graphrag_review_entity_identity_conflict(
    body: GraphRAGIdentityConflictReviewRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    del user_id
    runtime = _get_graphrag_entity_identity_runtime(request)
    try:
        decision = runtime.review_cross_instance_conflict(
            local_entity_ref=body.local_entity_ref,
            external_entity_refs=body.external_entity_refs,
            local_relation_kind=body.local_relation_kind,
            external_relation_kinds=body.external_relation_kinds,
            confidence=body.confidence,
            resolver_version_ref=body.resolver_version_ref,
            evidence_refs=body.evidence_refs,
            decision_receipt_ref=body.decision_receipt_ref,
            external_decision_refs=body.external_decision_refs,
            local_identity_gate_approved=body.local_identity_gate_approved,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_review_entity_identity_conflict",
            },
        )
    return decision.as_dict()


@router.post("/entity-identity/split", status_code=200)
async def graphrag_plan_entity_split(
    body: GraphRAGEntitySplitRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    del user_id
    runtime = _get_graphrag_entity_identity_runtime(request)
    try:
        plan = runtime.plan_split(
            canonical_entity_ref=body.canonical_entity_ref,
            successor_entity_refs=body.successor_entity_refs,
            stale_path_refs=body.stale_path_refs,
            impacted_edge_refs=body.impacted_edge_refs,
            resolver_version_ref=body.resolver_version_ref,
            evidence_refs=body.evidence_refs,
            decision_receipt_ref=body.decision_receipt_ref,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_plan_entity_split",
            },
        )
    return plan.as_dict()


@router.post("/entity-identity/resolver-drift-plan", status_code=200)
async def graphrag_plan_resolver_drift(
    body: GraphRAGResolverDriftPlanRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    del user_id
    runtime = _get_graphrag_entity_identity_runtime(request)
    try:
        plan = runtime.plan_resolver_drift(
            previous_resolver_version_ref=body.previous_resolver_version_ref,
            resolver_version_ref=body.resolver_version_ref,
            impacted_entity_refs=body.impacted_entity_refs,
            impacted_edge_refs=body.impacted_edge_refs,
            stale_path_refs=body.stale_path_refs,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_plan_resolver_drift",
            },
    )
    return plan.as_dict()


@router.post("/debate-front/review", status_code=200)
async def graphrag_debate_front_review(
    body: GraphRAGDebateFrontReviewRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    del user_id
    runtime = _get_graphrag_debate_consensus_runtime(request)
    try:
        positions = [
            DebatePosition(
                position_ref=item.position_ref
                or f"debate_position://{hashlib.sha256(json.dumps({'claim_family_refs': item.claim_family_refs, 'institution_refs': item.institution_refs, 'confidence': item.confidence}, sort_keys=True).encode('utf-8')).hexdigest()[:24]}",
                claim_family_refs=tuple(item.claim_family_refs),
                supporting_evidence_refs=tuple(item.supporting_evidence_refs),
                contradicting_evidence_refs=tuple(item.contradicting_evidence_refs),
                institution_refs=tuple(item.institution_refs),
                confidence=item.confidence,
            )
            for item in body.positions
        ]
        front = runtime.review_debate_front(
            topic_ref=body.topic_ref,
            positions=positions,
            consensus_history_refs=body.consensus_history_refs,
            open_contradiction_refs=body.open_contradiction_refs,
            resolved_contradiction_refs=body.resolved_contradiction_refs,
            last_reviewed_at=body.last_reviewed_at,
            consensus_status=body.consensus_status,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_debate_front_review",
            },
        )
    return front.as_dict()


@router.post("/debate-front/consensus-meta-claim", status_code=200)
async def graphrag_consensus_meta_claim(
    body: GraphRAGConsensusMetaClaimRequest,
    request: Request,
    user_id: str = Depends(user_dependency),
):
    del user_id
    runtime = _get_graphrag_debate_consensus_runtime(request)
    try:
        positions = [
            DebatePosition(
                position_ref=item.position_ref
                or f"debate_position://{hashlib.sha256(json.dumps({'claim_family_refs': item.claim_family_refs, 'institution_refs': item.institution_refs, 'confidence': item.confidence}, sort_keys=True).encode('utf-8')).hexdigest()[:24]}",
                claim_family_refs=tuple(item.claim_family_refs),
                supporting_evidence_refs=tuple(item.supporting_evidence_refs),
                contradicting_evidence_refs=tuple(item.contradicting_evidence_refs),
                institution_refs=tuple(item.institution_refs),
                confidence=item.confidence,
            )
            for item in body.positions
        ]
        result = runtime.review_consensus(
            topic_ref=body.topic_ref,
            claims=[item.model_dump() for item in body.claims],
            evidence_items=[item.model_dump() for item in body.evidence_items],
            contradictions=[item.model_dump() for item in body.contradictions],
            positions=positions,
            consensus_history_refs=body.consensus_history_refs,
            resolved_contradiction_refs=body.resolved_contradiction_refs,
            excluded_claim_family_refs=body.excluded_claim_family_refs,
            exclusion_reasons=body.exclusion_reasons,
            last_reviewed_at=body.last_reviewed_at,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": str(exc),
                "operation": "graphrag_consensus_meta_claim",
            },
        )
    return result.as_dict()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_kind_from_id(node_id: str) -> str:
    """Infer node kind from LMP-style node ID prefix."""
    if ":" in node_id:
        prefix = node_id.split(":", 1)[0].lower()
        kind_map = {
            "budo": "protein",
            "pdb": "structure", "alphafolddb": "structure",
            "interpro": "domain", "pfam": "domain", "smart": "domain", "prosite": "domain",
            "reactome": "pathway", "pathwaycommons": "pathway", "signalink": "pathway",
            "chembl": "pharmacology", "kegg": "pharmacology", "drugbank": "pharmacology",
            "disgenet": "disease", "malacards": "disease",
            "biogrid": "interaction", "intact": "interaction", "string": "interaction",
            "go": "go", "pan-go": "go",
            "hgnc": "gene", "genecards": "gene",
            "bgee": "expression", "hpa": "expression",
            "embl": "sequence", "refseq": "sequence",
        }
        return kind_map.get(prefix, "entity")
    return "entity"


def _safe_metadata(metadata: Any) -> Optional[Dict[str, Any]]:
    """Return metadata as a plain dict, stripping large or unneeded fields."""
    if metadata is None:
        return None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(metadata, dict):
        return None
    # Strip embedding vectors and overly large fields
    safe = {}
    for k, v in metadata.items():
        if k == "embedding":
            continue
        if isinstance(v, str) and len(v) > 500:
            safe[k] = v[:500] + "..."
        elif isinstance(v, (str, int, float, bool, list, dict)):
            safe[k] = v
    return safe or None


async def _resolve_query_asof_contract(
    *,
    request: Request,
    body: GraphRAGQueryRequest,
) -> Optional[Dict[str, Any]]:
    graph_snapshot_manifest_ref = (body.graph_snapshot_manifest_ref or "").strip()
    vector_index_manifest_ref = (body.vector_index_manifest_ref or "").strip()

    if not graph_snapshot_manifest_ref and not vector_index_manifest_ref:
        return None

    if not graph_snapshot_manifest_ref or not vector_index_manifest_ref:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": "as_of_manifest_pair_required",
                "operation": "graphrag_query",
            },
        )

    for manifest_ref, field_name in (
        (graph_snapshot_manifest_ref, "graph_snapshot_manifest_ref"),
        (vector_index_manifest_ref, "vector_index_manifest_ref"),
    ):
        if not manifest_ref.startswith("manifest://"):
            raise HTTPException(
                status_code=422,
                detail={
                    "status": "blocked",
                    "reason": "invalid_as_of_manifest_ref",
                    "operation": "graphrag_query",
                    "field": field_name,
                },
            )

    resolver = getattr(request.app.state, "graphrag_manifest_resolver", None)
    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "blocked",
                "reason": "as_of_manifest_resolver_unavailable",
                "operation": "graphrag_query",
            },
        )

    resolved = resolver(
        graph_snapshot_manifest_ref=graph_snapshot_manifest_ref,
        vector_index_manifest_ref=vector_index_manifest_ref,
        study_id=body.study_id,
        kb_id=body.kb_id,
        working_set_id=body.working_set_id,
    )
    if inspect.isawaitable(resolved):
        resolved = await resolved
    if not isinstance(resolved, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": "as_of_manifest_mismatch",
                "operation": "graphrag_query",
            },
        )

    resolved_graph = str(resolved.get("graph_snapshot_manifest_ref") or "").strip()
    resolved_vector = str(resolved.get("vector_index_manifest_ref") or "").strip()
    compatible = bool(resolved.get("compatible"))
    if (
        not compatible
        or resolved_graph != graph_snapshot_manifest_ref
        or resolved_vector != vector_index_manifest_ref
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": "as_of_manifest_mismatch",
                "operation": "graphrag_query",
            },
        )

    contract = {
        "mode": "manifest_bound_as_of",
        "graph_snapshot_manifest_ref": resolved_graph,
        "vector_index_manifest_ref": resolved_vector,
        "compatible": True,
    }
    as_of_ts = resolved.get("as_of_ts")
    if as_of_ts is not None:
        contract["as_of_ts"] = as_of_ts
    return contract


def _require_created_by_receipt_ref(*, created_by_receipt_ref: Optional[str], operation: str) -> str:
    receipt_ref = (created_by_receipt_ref or "").strip()
    if not receipt_ref:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": "missing_created_by_receipt_ref",
                "operation": operation,
            },
        )
    if not receipt_ref.startswith("receipt://"):
        raise HTTPException(
            status_code=422,
            detail={
                "status": "blocked",
                "reason": "invalid_created_by_receipt_ref",
                "operation": operation,
            },
        )
    return receipt_ref

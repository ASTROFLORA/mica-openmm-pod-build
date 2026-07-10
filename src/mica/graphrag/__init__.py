from .answer_receipt_runtime import (
    GraphAnswerReceipt,
    GraphAnswerReceiptRuntime,
    GraphAnswerReplayResult,
)
from .cost_runtime import (
    TenantBudgetDecision,
    TenantBudgetEnforcer,
)
from .curation_runtime import (
    CurationCredit,
    EdgeCorrectionReceipt,
    GraphCurationResult,
    GraphCurationRuntime,
    GraphCurationTask,
)
from mica.kb.graph.durability.runtime import (
    GraphDurabilityRuntime,
    GraphEdgeEventLogEntry,
    GraphLongHorizonReplayResult,
    GraphPortableCanonBundle,
)
from .edge_confidence_model import (
    EdgeConfidenceAssessment,
    EdgeConfidenceProfile,
    assess_edge_confidence,
    get_edge_confidence_profile,
)
from .entity_identity_runtime import (
    EntityIdentityDecision,
    EntityIdentityRuntime,
    GraphReprojectionPlan,
    ResolverDriftEvent,
)
from .evidence_path_runtime import (
    EvidencePathBundle,
    EvidencePathBundleComposer,
    OutputValidator,
)
from .federation_runtime import (
    CachedEvidencePathBundleStore,
    CachedEvidencePathEntry,
    ExternalRetractionDelta,
    FederatedGraphEdgeDelta,
    FederatedGraphImportResult,
    FederatedGraphImporter,
    PredicateReconcileDecision,
    PredicateReconciler,
    RetractionPropagationResult,
    RetractionPropagator,
)
from .golden_release_runtime import (
    GoldenGraphRAGv3Metrics,
    GoldenGraphRAGv3Runtime,
    GoldenGraphRAGv3Thresholds,
    GoldenGraphRAGv3Verdict,
)
from .health_runtime import (
    GraphHealthGateDecision,
    GraphHealthRuntime,
    GraphHealthScopeStatus,
    GraphHealthState,
)
from .node2vec_coverage_gate import (
    GraphNode2VecCoverageDecision,
    GraphNode2VecCoverageSnapshot,
    GraphNode2VecCoverageThresholds,
    assess_node2vec_coverage_gate,
    compute_graph_embedding_staleness,
)
from .node2vec_runtime import (
    GraphNode2VecCandidate,
    Node2VecABDecision,
    Node2VecABHarness,
    Node2VecABMetrics,
    Node2VecExpansionResult,
    Node2VecRuntime,
)
from .privacy_runtime import (
    GraphAggregatePolicy,
    GraphAggregatePolicyDecision,
    LeakageCanary,
    LeakageCanaryResult,
)
from .projection_runtime import (
    GraphProjectionDriftSignal,
    GraphProjectionGuardDecision,
    GraphProjectionLagSignal,
    GraphProjectionReconcileAction,
    GraphProjectionRuntime,
    GraphProjectionScopeStatus,
    GraphProjectionState,
)
from .rebuild_from_mudo_runtime import (
    GraphProjectionEdgeManifest,
    GraphProjectionRebuildEquivalence,
    GraphProjectionRebuildRuntime,
)
from .traversal_runtime import (
    BudgetedTraversalEngine,
    TraversalDegradationEnvelope,
    TraversalDegradePolicy,
    TraversalServeDecision,
    build_traversal_degrade_policy,
)

__all__ = [
    "EdgeConfidenceAssessment",
    "EdgeConfidenceProfile",
    "EvidencePathBundle",
    "EvidencePathBundleComposer",
    "ExternalRetractionDelta",
    "FederatedGraphEdgeDelta",
    "FederatedGraphImportResult",
    "FederatedGraphImporter",
    "GraphHealthGateDecision",
    "GraphHealthRuntime",
    "GraphHealthScopeStatus",
    "GraphHealthState",
    "GraphInferenceProposal",
    "GraphInferenceProposalEngine",
    "GraphAnswerReceipt",
    "GraphAnswerReceiptRuntime",
    "GraphAnswerReplayResult",
    "TenantBudgetDecision",
    "TenantBudgetEnforcer",
    "CurationCredit",
    "EntityIdentityDecision",
    "EntityIdentityRuntime",
    "EdgeCorrectionReceipt",
    "GraphCurationResult",
    "GraphCurationRuntime",
    "GraphCurationTask",
    "GraphDurabilityRuntime",
    "GraphEdgeEventLogEntry",
    "GraphReprojectionPlan",
    "GraphNode2VecCoverageDecision",
    "GraphNode2VecCoverageSnapshot",
    "GraphNode2VecCoverageThresholds",
    "GraphLongHorizonReplayResult",
    "GraphNode2VecCandidate",
    "GraphPortableCanonBundle",
    "GraphAggregatePolicy",
    "GraphAggregatePolicyDecision",
    "GoldenGraphRAGv3Metrics",
    "GoldenGraphRAGv3Runtime",
    "GoldenGraphRAGv3Thresholds",
    "GoldenGraphRAGv3Verdict",
    "GraphProjectionDriftSignal",
    "GraphProjectionEdgeManifest",
    "GraphProjectionGuardDecision",
    "GraphProjectionLagSignal",
    "GraphProjectionRebuildEquivalence",
    "GraphProjectionRebuildRuntime",
    "GraphProjectionReconcileAction",
    "GraphProjectionRuntime",
    "GraphProjectionScopeStatus",
    "GraphProjectionState",
    "CachedEvidencePathBundleStore",
    "CachedEvidencePathEntry",
    "LeakageCanary",
    "LeakageCanaryResult",
    "Node2VecABDecision",
    "Node2VecABHarness",
    "Node2VecABMetrics",
    "Node2VecExpansionResult",
    "Node2VecRuntime",
    "PredicateReconcileDecision",
    "PredicateReconciler",
    "BudgetedTraversalEngine",
    "RetractionPropagationResult",
    "RetractionPropagator",
    "ResolverDriftEvent",
    "TraversalDegradationEnvelope",
    "TraversalDegradePolicy",
    "TraversalServeDecision",
    "OutputValidator",
    "assess_edge_confidence",
    "assess_node2vec_coverage_gate",
    "build_traversal_degrade_policy",
    "compute_graph_embedding_staleness",
    "get_edge_confidence_profile",
]


def __getattr__(name):
    if name in {"GraphInferenceProposal", "GraphInferenceProposalEngine"}:
        from .inference_runtime import GraphInferenceProposal, GraphInferenceProposalEngine

        return {
            "GraphInferenceProposal": GraphInferenceProposal,
            "GraphInferenceProposalEngine": GraphInferenceProposalEngine,
        }[name]
    raise AttributeError(f"module 'mica.graphrag' has no attribute {name!r}")

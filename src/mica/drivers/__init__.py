"""
MICA Multi-Driver Architecture
================================

Hierarchical driver system following MPI-UOS framework:
- WorkerDriver: Specialist orchestration per domain (BioDynamo, Alchemist, SMIC)
- GeneralDriver: Multi-worker orchestration with MSRP enforcement

Based on:
- MPI-UOS: Model Scientific Reasoning Protocol and Proactive Induction Under Organized Systems
- MSRP: Sequential 5-phase scientific reasoning
- AI University Research Institute methodology
"""

from importlib import import_module
from typing import Any

_LAZY_EXPORT_GROUPS = {
    ".worker_driver": ("WorkerDriver",),
    ".biodynamo_driver": ("BioDynamoDriver",),
    ".alchemist_driver": ("AlchemistDriver",),
    ".smic_driver": ("SMICDriver",),
    ".general_driver": ("GeneralDriver",),
    ".config": ("AgenticDriverConfig", "AgenticSession"),
    ".system_config": (
        "ProductionBioConfig",
        "DevelopmentFullConfig",
        "load_driver_config",
        "DriverConfig",
    ),
    ".types": ("MICAState", "TaskType", "WorkflowState", "ToolExecutionHook"),
    ".utils": (
        "_emit_audit_event",
        "_truncate_text",
        "_redact_text",
        "_redact_obj",
        "_DriverTransportShim",
    ),
    ".evidence": (
        "official_link_from_identifiers",
        "build_source_record_from_paper",
        "format_bibliotecario_citation_entry",
        "extract_sources_from_text",
        "derive_claims_and_sources",
        "extract_native_evidence_from_side_data",
        "normalize_final_result_contract",
        "build_minimal_lab_report",
    ),
    ".persistence": (
        "saga_log_path",
        "append_saga_event",
        "append_saga_event_timescale",
        "get_timescale_store",
        "best_effort_saga_mcp_metrics",
        "snapshot_dir",
        "sha256_file",
        "save_session_snapshot",
        "restore_session_snapshot",
        "run_manifest_dir",
        "best_effort_git_info",
        "best_effort_versions",
        "write_run_manifest",
        "write_report_card",
        "conversation_log_path",
        "safe_result_for_log",
        "stringify_message_content",
        "append_conversation_log",
    ),
    ".atom_integration": (
        "record_atom_entry",
        "record_session_event_in_atom",
        "record_lab_report_to_atom",
        "record_quality_scores_to_atom",
        "query_atom_for_gap_signals",
        "maybe_run_proactive_gap_scan",
    ),
    ".identifiers": (
        "PDB_FALSE_POSITIVES",
        "extract_identifiers",
        "merge_identifiers",
        "best_protein_hint",
        "extract_candidate_gene_symbols",
        "extract_text_chunks_from_mcp",
        "extract_uniprot_accessions_from_mcp_result",
        "extract_pdb_ids_from_search_result",
    ),
    ".research_phase_context": (
        "ResearchPhase",
        "PhaseTransition",
        "PhaseGateResult",
        "ResearchPhaseContext",
    ),
    ".contracts": (
        "ArtifactDescriptor",
        "DockingHandoffPayload",
        "DriverContractViolation",
        "DriverFailureEvent",
        "FailureType",
        "FirewallResult",
        "HallucinationFirewall",
        "Phase",
        "PhaseTransitionEvent",
        "TemplateStubDetector",
    ),
    ".safety_gate": (
        "SafetyCheckResult",
        "SafetyEnvelope",
        "SafetyGate",
        "SafetyLevel",
    ),
    ".context_coherence": (
        "CoherenceDimension",
        "CoherenceScore",
        "CoherenceSignal",
        "ContextCoherenceScorer",
        "ReplanReason",
        "ReplanTrigger",
    ),
    ".driver_role_manifest": (
        "ArtifactBoundary",
        "CapabilityBoundary",
        "DriverRole",
        "DriverRoleManifest",
        "RoleCheckResult",
        "RoleEnforcer",
    ),
    ".workflow_dag": (
        "DAGResult",
        "DAGTask",
        "TaskStatus",
        "WorkflowDAG",
        "build_standard_workflow",
    ),
    ".mcp": (
        "format_tools_for_claude",
        "format_tools_for_openai",
        "normalize_mcp_call_tool_result",
        "get_tool_schema",
        "pick_tool_for_server",
        "build_tool_args",
        "build_tool_args_fallback",
        "normalize_call_args",
        "inject_attribution",
        "build_blocked_payload",
        "build_confirmation_payload",
        "build_success_payload",
        "build_error_payload",
        "build_saga_begin_event",
        "build_saga_abort_event",
        "build_saga_commit_event",
        "build_saga_retry_event",
        "run_security_gate",
        "run_governance_gate",
        "check_circuit_breaker",
        "circuit_breaker_on_success",
        "circuit_breaker_on_failure",
        "RetryConfig",
        "build_retry_config",
        "compute_backoff_sleep",
    ),
    ".structure": (
        "should_use_direct_structure_path",
        "rank_pdb_structures",
        "persist_structure_artifacts",
        "make_attachment",
    ),
    ".langgraph": (
        "node_route",
        "node_decompose",
        "node_analyze",
        "node_synthesize",
        "node_proactive_monitor",
        "router_quality_gate",
        "router_proactive_monitor",
        "node_initialize",
        "node_thermostat",
        "node_assign",
        "node_execute",
        "node_quality_gate",
    ),
}
_LAZY_EXPORTS = {
    name: module_name
    for module_name, names in _LAZY_EXPORT_GROUPS.items()
    for name in names
}
__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

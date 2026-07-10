"""
Execution services extracted from AgenticDriver.

Domain F — execution loop decomposition.
"""

from .loop_execution_context import LoopExecutionContext
from .loop_executor_bootstrap_service import LoopExecutorBootstrap, build_loop_executor_bootstrap
from .loop_front_dispatch_service import run_loop_front_branch
from .loop_gated_executor_service import build_loop_gated_executor
from .loop_primary_dispatch_service import run_loop_primary_branch
from .run_streaming_prompt_service import RunStreamingPromptPlan, build_run_streaming_prompt_plan
from .role_executor_service import build_role_executor
from .loop_tail_dispatch_service import run_loop_tail_branch
from .tool_payload_service import (
    tool_status_response,
    degraded_tool_response,
    unavailable_tool_response,
    coerce_seed_entities,
    transport_payload_or_degraded,
    normalize_tool_payload,
)
from .dependency_state_service import (
    provider_dependency_state,
    backend_dependency_state,
    sandbox_dependency_state,
    probe_host_reachability,
    network_dependency_state,
    dependency_state_for_tool,
    pre_dispatch_gate,
)
from .loop_dependency_gate_service import LoopDependencyGates, build_loop_dependency_gates
from .loop_literature_helper_service import LoopLiteratureHelpers, build_loop_literature_helpers
from .tool_execution_gate_service import (
    run_gated_tool_call,
    build_tool_heartbeat_body,
    publish_tool_heartbeat_cue,
    build_publish_heartbeat_adapter,
    DEFAULT_HEARTBEAT_SKIP_TOOLS,
    heartbeat_skip_tools_policy,
)
from .resource_cleanup_service import cleanup_execution_resources
from .tool_surface_service import (
    collect_named_tools,
    prepare_tool_surface,
    filter_tools_for_lane,
)
from .firewall_adapter_service import (
    FirewallVerdict,
    evaluate_pre_routing_firewall,
    attach_firewall_verdict_to_result,
    build_pre_routing_firewall_result,
)
from .bibliotecario_revision_service import run_bibliotecario_revision_cycle
from .atom_facts_service import run_atom_facts_branch
from .citation_verification_service import run_verify_citations_branch
from .dashboard_placeholder_service import run_dashboard_placeholder_branch
from .deep_research_service import run_deep_research_branch
from .driver_checkpoint_service import (
    run_driver_delegated_checkpoint_branch,
    run_driver_staging_deploy_checkpoint_branch,
)
from .driver_experiment_service import (
    run_driver_experiment_branch,
    replay_experiment_branch,
    get_experiment_quota_status_branch,
)
from .specialist_delegation_service import (
    run_consult_specialist_branch,
    build_specialist_response_adapter,
)
from .expert_consultation_service import run_consult_expert_branch
from .bibliotecario_consultation_service import (
    run_consult_bibliotecario_branch,
    build_bibliotecario_role_spec,
    build_last_bibliotecario_state_setter,
)
from .peer_review_service import (
    run_request_peer_review_branch,
    build_reviewer_role_spec,
    PeerReviewQualityAdapterService,
)
from .scientific_role_dispatch_service import run_scientific_role_dispatch_branch
from .hypothesis_service import (
    run_list_dlm_presets_branch,
    run_generate_hypotheses_branch,
)
from .fallback_routing_service import (
    BACKEND_ONLY_TYPED_TOOLS,
    is_backend_only_typed_tool,
    run_backend_only_degraded_branch,
    run_transport_fallback_branch,
)
from .transport_fallback_service import run_fallback_transport_execution
from .lmp_context_service import run_lmp_context_branch
from .literature_search_service import run_literature_search_branch
from .protein_lookup_service import run_protein_lookup_branch
from .report_orchestration_service import run_report_orchestration_branch
from .research_briefing_service import run_research_briefing_branch
from .sandbox_session_service import (
    run_execute_in_sandbox_branch,
    run_sandbox_session_status_branch,
    run_terminate_sandbox_session_branch,
)
from .uniprot_service import run_uniprot_search
from .vertical_report_service import run_vertical_report
from .worker_delegation_service import run_execute_worker_branch
from .feed_tools_service import FEED_TOOL_NAMES, run_feed_tool_branch
from .websearch_tool_service import run_web_search_branch
from .repo_ide_service import REPO_IDE_TOOL_NAMES, run_repo_ide_branch
from .hot_loop_reinjection_service import build_hot_loop_reinjection_packet
from .mcp_resource_injection_service import inject_mcp_resources_into_query
from .runtime_consumption_context_service import build_runtime_consumption_context
from .agentic_loop_result_service import execute_with_agentic_loop
from .direct_structure_service import execute_direct_structure_request
from .thermodynamic_routing_service import (
    build_thermodynamic_route_plan,
    build_thermodynamic_snapshot,
    emit_thermodynamic_routing_telemetry,
)
from .role_policy_service import filter_tools_for_role, run_output_invariants
from .mcp_gateway_service import (
    execute_mcp_retry_loop,
    run_mcp_governance_circuit_precheck,
)

__all__ = [
    "LoopExecutionContext",
    "LoopExecutorBootstrap",
    "build_loop_executor_bootstrap",
    "run_loop_front_branch",
    "build_loop_gated_executor",
    "run_loop_primary_branch",
    "RunStreamingPromptPlan",
    "build_run_streaming_prompt_plan",
    "build_role_executor",
    "run_loop_tail_branch",
    "tool_status_response",
    "degraded_tool_response",
    "unavailable_tool_response",
    "coerce_seed_entities",
    "transport_payload_or_degraded",
    "normalize_tool_payload",
    "provider_dependency_state",
    "backend_dependency_state",
    "sandbox_dependency_state",
    "probe_host_reachability",
    "network_dependency_state",
    "dependency_state_for_tool",
    "pre_dispatch_gate",
    "LoopDependencyGates",
    "build_loop_dependency_gates",
    "LoopLiteratureHelpers",
    "build_loop_literature_helpers",
    "run_gated_tool_call",
    "build_tool_heartbeat_body",
    "publish_tool_heartbeat_cue",
    "build_publish_heartbeat_adapter",
    "DEFAULT_HEARTBEAT_SKIP_TOOLS",
    "heartbeat_skip_tools_policy",
    "cleanup_execution_resources",
    "collect_named_tools",
    "prepare_tool_surface",
    "filter_tools_for_lane",
    "FirewallVerdict",
    "evaluate_pre_routing_firewall",
    "attach_firewall_verdict_to_result",
    "build_pre_routing_firewall_result",
    "run_atom_facts_branch",
    "run_bibliotecario_revision_cycle",
    "run_verify_citations_branch",
    "run_dashboard_placeholder_branch",
    "run_deep_research_branch",
    "run_driver_delegated_checkpoint_branch",
    "run_driver_staging_deploy_checkpoint_branch",
    "run_driver_experiment_branch",
    "replay_experiment_branch",
    "get_experiment_quota_status_branch",
    "run_consult_specialist_branch",
    "build_specialist_response_adapter",
    "run_consult_expert_branch",
    "run_consult_bibliotecario_branch",
    "build_bibliotecario_role_spec",
    "build_last_bibliotecario_state_setter",
    "run_request_peer_review_branch",
    "build_reviewer_role_spec",
    "PeerReviewQualityAdapterService",
    "run_scientific_role_dispatch_branch",
    "run_list_dlm_presets_branch",
    "run_generate_hypotheses_branch",
    "BACKEND_ONLY_TYPED_TOOLS",
    "is_backend_only_typed_tool",
    "run_backend_only_degraded_branch",
    "run_transport_fallback_branch",
    "run_fallback_transport_execution",
    "run_lmp_context_branch",
    "run_literature_search_branch",
    "run_protein_lookup_branch",
    "run_report_orchestration_branch",
    "run_research_briefing_branch",
    "run_execute_in_sandbox_branch",
    "run_sandbox_session_status_branch",
    "run_terminate_sandbox_session_branch",
    "run_uniprot_search",
    "run_vertical_report",
    "run_execute_worker_branch",
    "FEED_TOOL_NAMES",
    "run_feed_tool_branch",
    "run_web_search_branch",
    "REPO_IDE_TOOL_NAMES",
    "run_repo_ide_branch",
    "build_runtime_consumption_context",
    "execute_with_agentic_loop",
    "execute_direct_structure_request",
    "build_thermodynamic_route_plan",
    "build_thermodynamic_snapshot",
    "emit_thermodynamic_routing_telemetry",
    "filter_tools_for_role",
    "run_output_invariants",
    "execute_mcp_retry_loop",
    "run_mcp_governance_circuit_precheck",
]

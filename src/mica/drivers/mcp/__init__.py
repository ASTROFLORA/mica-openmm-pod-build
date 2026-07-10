"""MCP module — tool formatting, selection, invocation pipeline, and result normalization.

Phase 3+4a extraction from agentic_driver.py.
"""

from .format import (
    format_tools_for_claude,
    format_tools_for_openai,
    normalize_mcp_call_tool_result,
)
from .tool_selection import (
    get_tool_schema,
    pick_tool_for_server,
    build_tool_args,
    build_tool_args_fallback,
)
from .invocation import (
    normalize_call_args,
    inject_attribution,
    build_blocked_payload,
    build_confirmation_payload,
    build_success_payload,
    build_error_payload,
    build_saga_begin_event,
    build_saga_abort_event,
    build_saga_commit_event,
    build_saga_retry_event,
    run_security_gate,
    run_governance_gate,
    check_circuit_breaker,
    circuit_breaker_on_success,
    circuit_breaker_on_failure,
    RetryConfig,
    build_retry_config,
    compute_backoff_sleep,
)
from .policy_snapshot import (
    MCPServerSnapshot,
    MCPPolicySnapshot,
    capture_mcp_policy_snapshot,
)

__all__ = [
    # Phase 3: format + tool_selection
    "format_tools_for_claude",
    "format_tools_for_openai",
    "normalize_mcp_call_tool_result",
    "get_tool_schema",
    "pick_tool_for_server",
    "build_tool_args",
    "build_tool_args_fallback",
    # Phase 4a: invocation pipeline helpers
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
    # Phase 4a-S0.4: policy snapshot
    "MCPServerSnapshot",
    "MCPPolicySnapshot",
    "capture_mcp_policy_snapshot",
]

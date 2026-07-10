"""
MCP Tool Invocation Pipeline — helper functions.

Extracted from ``AgenticDriver.call_mcp_tool`` (Phase 4a).

Every function takes **explicit** dependencies via keyword arguments so it can
be unit-tested in isolation without a live driver instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ====================================================================
# Pure payload / event builders
# ====================================================================

def normalize_call_args(arguments: Any) -> Dict[str, Any]:
    """Coerce tool *arguments* to a ``dict``."""
    if isinstance(arguments, dict):
        return arguments
    return {}


def inject_attribution(
    call_args: Dict[str, Any],
    mcp_tools: Optional[List[Dict[str, Any]]],
    server_name: str,
    tool_name: str,
    session_id: str,
    *,
    user_id: Optional[str] = None,
    bucket: Optional[str] = None,
) -> None:
    """Best-effort attribution injection — mutates *call_args* in place.

    Only injects ``user_id``, ``bucket``, ``session_id`` when the tool schema
    declares them in ``input_schema.properties``.
    """
    try:
        expected_name = f"{server_name}_{tool_name}"
        schema: Dict[str, Any] = {}
        for t in mcp_tools or []:
            if t.get("server") == server_name and t.get("name") == expected_name:
                schema = t.get("input_schema") or {}
                break

        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        if isinstance(props, dict):
            if "user_id" in props and "user_id" not in call_args and user_id:
                call_args["user_id"] = user_id
            if "bucket" in props and "bucket" not in call_args and bucket:
                call_args["bucket"] = bucket
            if "session_id" in props and "session_id" not in call_args:
                call_args["session_id"] = session_id
    except Exception:
        pass


def build_blocked_payload(
    server: str,
    tool: str,
    *,
    error: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct a ``success=False, blocked=True`` result payload."""
    payload: Dict[str, Any] = {
        "success": False,
        "blocked": True,
        "error": error,
        "server": server,
        "tool": tool,
    }
    if extra:
        payload.update(extra)
    return payload


def build_confirmation_payload(
    server: str,
    tool: str,
    *,
    governance_decision: str,
    security_risk: Any = None,
) -> Dict[str, Any]:
    """Payload when governance requires human confirmation."""
    return {
        "success": False,
        "requires_confirmation": True,
        "error": "MCP tool call requires human confirmation",
        "server": server,
        "tool": tool,
        "governance_decision": governance_decision,
        "security_risk": security_risk,
    }


def build_success_payload(
    server: str,
    tool: str,
    result: Any,
    attempt: int,
    total_attempts: int,
) -> Dict[str, Any]:
    """Payload for a successful MCP tool call."""
    return {
        "success": True,
        "result": result,
        "server": server,
        "tool": tool,
        "attempt": attempt,
        "attempts": total_attempts,
    }


def build_error_payload(
    server: str,
    tool: str,
    error: str,
    error_type: Optional[str],
    attempt: int,
    total_attempts: int,
) -> Dict[str, Any]:
    """Payload for a final failed MCP tool call."""
    return {
        "success": False,
        "error": error,
        "error_type": error_type,
        "server": server,
        "tool": tool,
        "attempt": attempt,
        "attempts": total_attempts,
    }


# ── Saga event helpers ────────────────────────────────────────────

def build_saga_begin_event(
    event_id: str,
    server: str,
    tool: str,
    args_preview: str,
    started_at: datetime,
    *,
    run_id: str = "",
    execution_path: str = "",
) -> Dict[str, Any]:
    """Build the ``mcp_tool_begin`` saga event."""
    return {
        "event_id": event_id,
        "type": "mcp_tool_begin",
        "server": server,
        "tool": tool,
        "arguments_preview": args_preview,
        "started_at": started_at.isoformat(),
        "run_id": run_id,
        "execution_path": execution_path,
    }


def build_saga_abort_event(
    event_id: str,
    server: str,
    tool: str,
    started_at: datetime,
    *,
    status: str,
    extra: Optional[Dict[str, Any]] = None,
    run_id: str = "",
    execution_path: str = "",
) -> Dict[str, Any]:
    """Build a ``mcp_tool_abort`` saga event with timing."""
    finished_at = datetime.now(timezone.utc)
    evt: Dict[str, Any] = {
        "event_id": event_id,
        "type": "mcp_tool_abort",
        "server": server,
        "tool": tool,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "run_id": run_id,
        "execution_path": execution_path,
    }
    if extra:
        evt.update(extra)
    return evt


def build_saga_commit_event(
    event_id: str,
    server: str,
    tool: str,
    started_at: datetime,
    *,
    attempt: int,
    total_attempts: int,
    timeout_s: float,
    security_risk_dict: Any = None,
    run_id: str = "",
    execution_path: str = "",
) -> Dict[str, Any]:
    """Build a ``mcp_tool_commit`` saga event."""
    finished_at = datetime.now(timezone.utc)
    return {
        "event_id": event_id,
        "type": "mcp_tool_commit",
        "server": server,
        "tool": tool,
        "status": "success",
        "attempt": attempt,
        "attempts": total_attempts,
        "timeout_s": timeout_s,
        "security_risk": security_risk_dict,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "run_id": run_id,
        "execution_path": execution_path,
    }


def build_saga_retry_event(
    server: str,
    tool: str,
    *,
    attempt: int,
    total_attempts: int,
    error_type: Optional[str],
    error_str: Optional[str],
    sleep_s: float,
    timeout_s: float,
    attempt_started_at: datetime,
    run_id: str = "",
    execution_path: str = "",
) -> Dict[str, Any]:
    """Build a ``mcp_tool_retry`` saga event."""
    import uuid as _uuid

    return {
        "event_id": str(_uuid.uuid4()),
        "type": "mcp_tool_retry",
        "server": server,
        "tool": tool,
        "attempt": attempt,
        "attempts": total_attempts,
        "error_type": error_type,
        "error": error_str,
        "sleep_s": sleep_s,
        "timeout_s": timeout_s,
        "started_at": attempt_started_at.isoformat(),
        "run_id": run_id,
        "execution_path": execution_path,
    }


# ====================================================================
# Gate functions (near-pure, tested in isolation)
# ====================================================================

def run_security_gate(
    security_analyzer: Any,
    server_name: str,
    tool_name: str,
    call_args: Dict[str, Any],
    *,
    max_risk_fn: Callable[..., Any],
) -> Tuple[Any, bool]:
    """Run security analysis. Returns ``(risk, should_block)``."""
    if security_analyzer is None:
        return None, False
    args_json = json.dumps(call_args, sort_keys=True, ensure_ascii=False)
    risk_cmd = security_analyzer.analyze_command(
        f"mcp:{server_name}.{tool_name} {args_json}",
        source="system",
    )
    risk_evt = security_analyzer.analyze_event(
        {
            "type": "MCPToolCall",
            "server": server_name,
            "tool": tool_name,
            "arguments": call_args,
        },
        source="system",
    )
    risk = max_risk_fn(risk_cmd, risk_evt)
    blocked = risk is not None and security_analyzer.should_block(risk)
    return risk, blocked


def run_governance_gate(
    confirmation_policy: Any,
    server_name: str,
    tool_name: str,
    risk: Any,
    *,
    is_autonomous: bool,
    governance_decision_cls: Any,
    cost_estimate_cls: Any,
) -> Optional[str]:
    """Run governance policy evaluation.

    Returns:
        ``"REJECT"`` or ``"REQUIRE_CONFIRMATION"`` if the call must be
        blocked/held; ``None`` if approved.
    """
    if confirmation_policy is None or governance_decision_cls is None or cost_estimate_cls is None:
        return None

    estimate = cost_estimate_cls(
        computation_cost=0.0,
        api_cost=0.0,
        storage_cost=0.0,
        time_cost=0.0,
    )
    risk_level = getattr(getattr(risk, "level", None), "value", None) or "safe"
    decision = confirmation_policy.evaluate_operation(
        operation_type=f"mcp_tool_call:{server_name}.{tool_name}",
        cost_estimate=estimate,
        is_autonomous=is_autonomous,
        risk_level=str(risk_level),
    )

    if decision == governance_decision_cls.REJECT:
        return "REJECT"
    if decision in (governance_decision_cls.REQUIRE_CONFIRMATION, governance_decision_cls.DEFER):
        return "REQUIRE_CONFIRMATION"
    return None


# ====================================================================
# Circuit breaker (async, explicit state deps)
# ====================================================================

async def check_circuit_breaker(
    circuit_state: Dict[str, Any],
    circuit_lock: asyncio.Lock,
    key: str,
    *,
    threshold: int,
    reset_after_s: float,
) -> bool:
    """Pre-call circuit breaker check.

    Returns ``True`` if the call is **blocked** (circuit open and still
    within the reset window).
    """
    if threshold < 1:
        threshold = 1
    if reset_after_s < 0:
        reset_after_s = 0.0

    async with circuit_lock:
        st = circuit_state.get(key) or {
            "state": "closed",
            "consecutive_failures": 0,
            "opened_at_monotonic": None,
        }

        if st.get("state") == "open":
            opened_at = st.get("opened_at_monotonic")
            now = time.monotonic()
            if isinstance(opened_at, (int, float)) and (now - float(opened_at)) < reset_after_s:
                circuit_state[key] = st
                return True  # blocked

            # Reset window elapsed → close and allow trial
            st["state"] = "closed"
            st["consecutive_failures"] = 0
            st["opened_at_monotonic"] = None

        circuit_state[key] = st
    return False


async def circuit_breaker_on_success(
    circuit_state: Dict[str, Any],
    circuit_lock: asyncio.Lock,
    key: str,
) -> None:
    """Close circuit and reset failure counter on success."""
    async with circuit_lock:
        st = circuit_state.get(key) or {}
        st["state"] = "closed"
        st["consecutive_failures"] = 0
        st["opened_at_monotonic"] = None
        circuit_state[key] = st


async def circuit_breaker_on_failure(
    circuit_state: Dict[str, Any],
    circuit_lock: asyncio.Lock,
    key: str,
    *,
    threshold: int,
) -> None:
    """Increment failure count and possibly open the circuit."""
    if threshold < 1:
        threshold = 1
    async with circuit_lock:
        st = circuit_state.get(key) or {
            "state": "closed",
            "consecutive_failures": 0,
            "opened_at_monotonic": None,
        }
        st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
        if int(st["consecutive_failures"]) >= threshold:
            st["state"] = "open"
            st["opened_at_monotonic"] = time.monotonic()
        circuit_state[key] = st


# ====================================================================
# Retry configuration
# ====================================================================

@dataclass
class RetryConfig:
    """Retry-loop knobs extracted from AgenticDriverConfig."""

    timeout_s: float = 30.0
    max_retries: int = 1
    backoff_s: float = 0.5
    backoff_max_s: float = 8.0

    @property
    def total_attempts(self) -> int:
        return 1 + self.max_retries


def build_retry_config(config: Any) -> RetryConfig:
    """Build :class:`RetryConfig` from a driver config, clamping negatives."""
    timeout_s = float(getattr(config, "mcp_tool_timeout_s", 30.0) or 30.0)
    if timeout_s <= 0:
        timeout_s = 30.0

    max_retries = int(getattr(config, "mcp_tool_max_retries", 1) or 0)
    if max_retries < 0:
        max_retries = 0

    backoff_s = float(getattr(config, "mcp_tool_retry_backoff_s", 0.5) or 0.0)
    backoff_max_s = float(getattr(config, "mcp_tool_retry_backoff_max_s", 8.0) or 0.0)
    if backoff_s < 0:
        backoff_s = 0.0
    if backoff_max_s < 0:
        backoff_max_s = 0.0

    return RetryConfig(
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_s=backoff_s,
        backoff_max_s=backoff_max_s,
    )


def compute_backoff_sleep(attempt: int, cfg: RetryConfig) -> float:
    """Deterministic exponential backoff (no jitter)."""
    sleep_s = cfg.backoff_s * (2 ** (attempt - 1))
    if cfg.backoff_max_s > 0:
        sleep_s = min(sleep_s, cfg.backoff_max_s)
    return max(0.0, float(sleep_s))

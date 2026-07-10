"""MCP gateway orchestration helpers extracted from AgenticDriver."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional


async def run_mcp_governance_circuit_precheck(
    *,
    confirmation_policy: Any,
    config_obj: Any,
    server_name: str,
    tool_name: str,
    risk: Any,
    governance_decision_cls: Any,
    cost_estimate_cls: Any,
    run_governance_gate_fn: Callable[..., Optional[str]],
    security_risk_to_dict_fn: Callable[[Any], Optional[Dict[str, Any]]],
    check_circuit_breaker_fn: Callable[..., Awaitable[bool]],
    circuit_state: Dict[str, Any],
    circuit_lock: asyncio.Lock,
    build_blocked_payload_fn: Callable[..., Dict[str, Any]],
    build_confirmation_payload_fn: Callable[..., Dict[str, Any]],
    abort_fn: Callable[..., Awaitable[Dict[str, Any]]],
    logger_obj: Any,
) -> Optional[Dict[str, Any]]:
    risk_dict = security_risk_to_dict_fn(risk) if risk is not None else None

    try:
        gov_decision = run_governance_gate_fn(
            confirmation_policy,
            server_name,
            tool_name,
            risk,
            is_autonomous=bool(getattr(config_obj, "enable_autonomous_discovery", False)),
            governance_decision_cls=governance_decision_cls,
            cost_estimate_cls=cost_estimate_cls,
        )
        logger_obj.info(
            "Governance decision: %s for %s.%s (risk=%s)",
            gov_decision,
            server_name,
            tool_name,
            risk_dict.get("level") if risk_dict else "none",
        )
        if gov_decision == "REJECT":
            return await abort_fn(
                build_blocked_payload_fn(
                    server_name,
                    tool_name,
                    error="Blocked MCP tool call by governance policy",
                    extra={"governance_decision": gov_decision, "security_risk": risk_dict},
                ),
                saga_extra={"blocked_by": "governance", "governance_decision": gov_decision, "security_risk": risk_dict},
            )
        if gov_decision == "REQUIRE_CONFIRMATION":
            return await abort_fn(
                build_confirmation_payload_fn(
                    server_name,
                    tool_name,
                    governance_decision=gov_decision,
                    security_risk=risk_dict,
                ),
                status="requires_confirmation",
                saga_extra={"governance_decision": gov_decision, "security_risk": risk_dict},
            )
    except Exception:
        pass

    try:
        if bool(getattr(config_obj, "mcp_circuit_breaker_enabled", True)):
            cb_key = f"{server_name}.{tool_name}"
            cb_threshold = int(getattr(config_obj, "mcp_circuit_failure_threshold", 3) or 3)
            cb_reset = float(getattr(config_obj, "mcp_circuit_reset_after_s", 60.0) or 60.0)
            if await check_circuit_breaker_fn(
                circuit_state,
                circuit_lock,
                cb_key,
                threshold=cb_threshold,
                reset_after_s=cb_reset,
            ):
                return await abort_fn(
                    build_blocked_payload_fn(
                        server_name,
                        tool_name,
                        error="Blocked MCP tool call by circuit breaker (open)",
                        extra={"blocked_by": "circuit_breaker", "security_risk": risk_dict},
                    ),
                    saga_extra={"blocked_by": "circuit_breaker"},
                )
    except Exception:
        pass

    return None


async def execute_mcp_retry_loop(
    *,
    session_obj: Any,
    server_name: str,
    tool_name: str,
    call_args: Dict[str, Any],
    config_obj: Any,
    saga_session: str,
    saga_event_id: str,
    saga_started_at: datetime,
    risk: Any,
    normalize_mcp_call_tool_result_fn: Callable[[Any], Dict[str, Any]],
    build_success_payload_fn: Callable[..., Dict[str, Any]],
    build_error_payload_fn: Callable[..., Dict[str, Any]],
    build_saga_commit_event_fn: Callable[..., Dict[str, Any]],
    build_saga_retry_event_fn: Callable[..., Dict[str, Any]],
    security_risk_to_dict_fn: Callable[[Any], Optional[Dict[str, Any]]],
    truncate_text_fn: Callable[..., str],
    redact_text_fn: Callable[[str], str],
    append_saga_event_fn: Callable[..., Awaitable[None]],
    after_tool_success_fn: Callable[[Dict[str, Any]], Awaitable[None]],
    abort_fn: Callable[..., Awaitable[Dict[str, Any]]],
    build_retry_config_fn: Callable[[Any], Any],
    compute_backoff_sleep_fn: Callable[[int, Any], float],
    circuit_breaker_on_success_fn: Callable[..., Awaitable[None]],
    circuit_breaker_on_failure_fn: Callable[..., Awaitable[None]],
    circuit_state: Dict[str, Any],
    circuit_lock: asyncio.Lock,
    nru_gateway_obj: Any,
    logger_obj: Any,
) -> Dict[str, Any]:
    retry_config = build_retry_config_fn(config_obj)
    last_exc: Optional[BaseException] = None
    last_error_type: Optional[str] = None
    risk_dict_for_saga = security_risk_to_dict_fn(risk) if risk is not None else None
    circuit_breaker_enabled = bool(getattr(config_obj, "mcp_circuit_breaker_enabled", True))
    circuit_key = f"{server_name}.{tool_name}"
    circuit_threshold = int(getattr(config_obj, "mcp_circuit_failure_threshold", 3) or 3)

    for attempt in range(1, retry_config.total_attempts + 1):
        attempt_started_at = datetime.now(timezone.utc)
        try:
            raw_result = await asyncio.wait_for(
                session_obj.call_tool(tool_name, call_args),
                timeout=retry_config.timeout_s,
            )
            result = normalize_mcp_call_tool_result_fn(raw_result)
            payload = build_success_payload_fn(
                server_name,
                tool_name,
                result,
                attempt,
                retry_config.total_attempts,
            )
            try:
                if nru_gateway_obj is not None:
                    nru_payload = nru_gateway_obj.normalize(
                        tool_id=f"mcp_{server_name}.{tool_name}",
                        server_id=f"mcp_{server_name}",
                        raw_output=result,
                        run_id=saga_session,
                        workflow_id=saga_session,
                        tags=["mcp_tool"],
                    )
                    payload["nru"] = nru_payload.to_dict()
            except Exception:
                pass

            try:
                if circuit_breaker_enabled:
                    await circuit_breaker_on_success_fn(
                        circuit_state,
                        circuit_lock,
                        circuit_key,
                    )
            except Exception:
                pass

            try:
                await append_saga_event_fn(
                    session_id=saga_session,
                    event=build_saga_commit_event_fn(
                        saga_event_id,
                        server_name,
                        tool_name,
                        saga_started_at,
                        attempt=attempt,
                        total_attempts=retry_config.total_attempts,
                        timeout_s=retry_config.timeout_s,
                        security_risk_dict=risk_dict_for_saga,
                    ),
                )
            except Exception:
                pass

            await after_tool_success_fn(payload)
            return payload

        except asyncio.TimeoutError as exc:
            last_exc = exc
            last_error_type = "timeout"
        except Exception as exc:
            last_exc = exc
            last_error_type = "error"

        if attempt < retry_config.total_attempts:
            sleep_s = compute_backoff_sleep_fn(attempt, retry_config)
            try:
                await append_saga_event_fn(
                    session_id=saga_session,
                    event=build_saga_retry_event_fn(
                        server_name,
                        tool_name,
                        attempt=attempt,
                        total_attempts=retry_config.total_attempts,
                        error_type=last_error_type,
                        error_str=truncate_text_fn(redact_text_fn(str(last_exc)), max_len=500) if last_exc else None,
                        sleep_s=sleep_s,
                        timeout_s=retry_config.timeout_s,
                        attempt_started_at=attempt_started_at,
                    ),
                )
            except Exception:
                pass
            if retry_config.backoff_s > 0:
                await asyncio.sleep(sleep_s)
            continue

        err_str = truncate_text_fn(redact_text_fn(str(last_exc)), max_len=4000) if last_exc else "Unknown error"
        logger_obj.error(
            "MCP tool call failed: %s.%s - %s",
            server_name,
            tool_name,
            truncate_text_fn(redact_text_fn(str(last_exc)), max_len=2000) if last_exc else "Unknown",
        )

        try:
            if circuit_breaker_enabled:
                await circuit_breaker_on_failure_fn(
                    circuit_state,
                    circuit_lock,
                    circuit_key,
                    threshold=circuit_threshold,
                )
        except Exception:
            pass

        payload = build_error_payload_fn(
            server_name,
            tool_name,
            err_str,
            last_error_type,
            attempt,
            retry_config.total_attempts,
        )
        return await abort_fn(
            payload,
            status="error",
            saga_extra={
                "error_type": last_error_type,
                "error": truncate_text_fn(redact_text_fn(str(last_exc)), max_len=1000) if last_exc else "Unknown error",
                "attempt": attempt,
                "attempts": retry_config.total_attempts,
                "timeout_s": retry_config.timeout_s,
                "security_risk": risk_dict_for_saga,
            },
        )

    return await abort_fn(
        build_error_payload_fn(server_name, tool_name, "Unknown error", "error", 0, 0),
        status="error",
        saga_extra={"error_type": "error", "error": "Unknown error"},
    )
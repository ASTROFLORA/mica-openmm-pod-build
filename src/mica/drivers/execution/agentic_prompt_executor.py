#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agentic prompt processor — extracted logic from AgenticDriver.process_agentic_prompt.

Handles: session initialization, firewall evaluation, route planning, execution dispatch,
result normalization, cognitive critique, and persistence.
"""

import asyncio
import json
import logging
import os
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


async def execute_agentic_prompt(
    driver_self: Any,
    user_query: str,
    mode: str = "production",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    bucket: Optional[str] = None,
    workspace_id: Optional[str] = None,
    provider_id: str = "anthropic",
    model_id: Optional[str] = None,
    depth_preset: Optional[str] = None,
    execution_path_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute agentic workflow with full orchestration.
    
    Returns: Dict with final_result, execution_path, provenance, etc.
    """
    from datetime import datetime, timezone
    from .helpers import (
        resolve_depth_preset, _truncate_text, _redact_text
    )
    from ..cold_evidence import EpistemicFirewall
    from ...agentic.decision_ledger import DecisionLedger
    from ...agentic.session_audit_bundle import SessionAuditBundleBuilder

    # ── Initialize session instruments ──
    driver_self._depth_preset = resolve_depth_preset(depth_preset)
    driver_self._decision_ledger = DecisionLedger()
    driver_self._active_cue_results = []
    driver_self._audit_builder = None

    session_id = session_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    original_user_query = user_query
    resource_fabric: Optional[Dict[str, Any]] = None

    token_user = None
    token_bucket = None
    _ws_token = None

    try:
        # Set context vars
        if user_id is not None:
            from ..agentic_driver import _current_user_id_var
            token_user = _current_user_id_var.set(user_id)
        if bucket is not None:
            from ..agentic_driver import _current_bucket_var
            token_bucket = _current_bucket_var.set(bucket)
        _ws_token = driver_self._workspace_id_var.set((workspace_id or "").strip())

        # Assign run_id
        driver_self._session_run_ids.setdefault(session_id, str(uuid.uuid4()))
        run_id = driver_self._session_run_ids.get(session_id, session_id)
        driver_self._audit_builder = SessionAuditBundleBuilder(
            session_id=session_id,
            run_id=run_id,
            depth_preset=driver_self._depth_preset.name,
        )
        driver_self._get_or_create_evidence_ledger(session_id, run_id)

        await driver_self._emit_runtime_status_telemetry(
            session_id=session_id,
            run_id=run_id,
            phase="process_agentic_prompt",
            status="started",
            details="Driver workflow execution started.",
            mode=mode,
            metrics={"query_length": len(original_user_query or "")},
        )

        await driver_self._persist_driver_session_start(
            session_id=session_id,
            user_query=original_user_query,
            mode=mode,
            user_id=user_id,
            bucket=bucket,
            workspace_id=workspace_id,
            run_id=run_id,
        )

        # ── MCP resource injection ──
        if getattr(driver_self.config, "mcp_resources_enabled", False) and getattr(driver_self.config, "mcp_enabled", False):
            user_query, resource_fabric = await driver_self.inject_mcp_resources_into_query(user_query)

        # ── Epistemic firewall ──
        firewall_verdict = EpistemicFirewall().evaluate_pre_routing(query=original_user_query)
        await driver_self._emit_runtime_status_telemetry(
            session_id=session_id,
            run_id=run_id,
            phase="epistemic_firewall",
            status=firewall_verdict.action,
            details=("; ".join(firewall_verdict.reasons) or "No explicit premise conflict detected before routing."),
            mode=mode,
            severity="warning" if firewall_verdict.action in {"challenge", "block"} else "info",
            metrics={
                "reason_count": float(len(firewall_verdict.reasons)),
                "challenged": 1.0 if firewall_verdict.action == "challenge" else 0.0,
                "blocked": 1.0 if firewall_verdict.action == "block" else 0.0,
            },
        )

        # ── Build runtime context ──
        direct_structure_query = driver_self._should_use_direct_structure_path(original_user_query)
        runtime_consumption_context = await driver_self._build_runtime_consumption_context(
            query=original_user_query,
            session_id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        prompt_block = str(runtime_consumption_context.get("prompt_block") or "").strip()
        if prompt_block and not direct_structure_query:
            user_query = f"{user_query}\n\n[Persisted runtime evidence]\n{prompt_block}"

        # ── Build thermodynamic route plan ──
        route_plan = driver_self._build_thermodynamic_route_plan(
            query=original_user_query,
            session_id=session_id,
            requested_execution_path=str(execution_path_override or os.environ.get("MICA_EXECUTION_PATH", "auto")).lower().strip(),
        )
        route_plan = driver_self._annotate_route_plan_with_runtime_consumption(
            route_plan=route_plan,
            runtime_context=runtime_consumption_context,
        )
        await driver_self._emit_thermodynamic_routing_telemetry(
            session_id=session_id,
            run_id=run_id,
            mode=mode,
            route_plan=route_plan,
        )

        # ── Execution-path dispatch ──
        _exec_path = str(execution_path_override or os.environ.get("MICA_EXECUTION_PATH", "auto")).lower().strip()
        _resolved_path = _exec_path
        _chaos_seeded_result = driver_self._load_chaos_initial_result(
            session_id=session_id,
            run_id=driver_self._session_run_ids.get(session_id, session_id),
            user_query=original_user_query,
        )

        if _chaos_seeded_result is not None:
            _resolved_path = "chaos_seeded_initial_result"
            result = _chaos_seeded_result
        elif firewall_verdict.action == "block":
            _resolved_path = "pre_routing_firewall"
            result = driver_self._build_pre_routing_firewall_result(
                session_id=session_id,
                run_id=run_id,
                user_query=original_user_query,
                verdict=firewall_verdict,
            )
        elif _exec_path == "direct" and direct_structure_query:
            _resolved_path = "direct"
            result = await driver_self._execute_direct_structure_request(
                user_query=user_query, session_id=session_id,
            )
        elif _exec_path == "langgraph" and driver_self.compiled_graph:
            _resolved_path = "langgraph"
            result = await driver_self._execute_with_langgraph(user_query, mode, session_id)
        elif _exec_path == "agentic_loop":
            _resolved_path = "agentic_loop"
            result = await driver_self._execute_with_agentic_loop(
                user_query,
                mode,
                session_id,
                provider_id=provider_id,
                model_id=model_id,
            )
        elif _exec_path == "auto":
            preferred_execution_path = str(route_plan.get("preferred_execution_path") or "auto")
            if preferred_execution_path == "langgraph" and driver_self.compiled_graph:
                _resolved_path = "langgraph"
                result = await driver_self._execute_with_langgraph(user_query, mode, session_id)
            elif preferred_execution_path == "agentic_loop":
                _resolved_path = "agentic_loop"
                result = await driver_self._execute_with_agentic_loop(
                    user_query,
                    mode,
                    session_id,
                    provider_id=provider_id,
                    model_id=model_id,
                )
            elif driver_self.compiled_graph:
                _resolved_path = "langgraph"
                result = await driver_self._execute_with_langgraph(user_query, mode, session_id)
            else:
                _resolved_path = "agentic_loop"
                result = await driver_self._execute_with_agentic_loop(
                    user_query,
                    mode,
                    session_id,
                    provider_id=provider_id,
                    model_id=model_id,
                )
        else:
            logger.warning(
                "Unknown MICA_EXECUTION_PATH=%r — falling back to auto",
                _exec_path,
            )
            if driver_self.compiled_graph:
                _resolved_path = "langgraph"
                result = await driver_self._execute_with_langgraph(user_query, mode, session_id)
            else:
                _resolved_path = "agentic_loop"
                result = await driver_self._execute_with_agentic_loop(
                    user_query,
                    mode,
                    session_id,
                    provider_id=provider_id,
                    model_id=model_id,
                )

        # ── Inject metadata ──
        _run_id = driver_self._session_run_ids.get(session_id, "")
        if isinstance(result, dict):
            driver_self._attach_epistemic_firewall_verdict(result=result, verdict=firewall_verdict)
            result.setdefault("execution_path", _resolved_path)
            result.setdefault("run_id", _run_id)
            driver_self._attach_thermodynamic_route(result=result, route_state=route_plan)
            driver_self._attach_runtime_consumption_context(result=result, runtime_context=runtime_consumption_context)

        if isinstance(result, dict):
            result = driver_self._normalize_final_result_contract(user_query=original_user_query, result=result)
            driver_self._attach_epistemic_firewall_verdict(result=result, verdict=firewall_verdict)
            driver_self._attach_thermodynamic_route(result=result, route_state=route_plan)
            driver_self._attach_runtime_consumption_context(result=result, runtime_context=runtime_consumption_context)

            # ── Cognitive layer evaluation ──
            cognitive_layer = driver_self._run_cognitive_layer(
                session_id=session_id,
                run_id=_run_id,
                user_query=original_user_query,
                result=result,
            )
            driver_self._attach_cognitive_layer_verdicts(
                result=result,
                ach_state=dict(cognitive_layer.get("hypothesis_competition") or {}),
                critic_verdict=dict(cognitive_layer.get("critic_pass") or {}),
            )
            driver_self._apply_negative_memory_guidance(result=result)

            # ── Maybe retry with cognitive critique ──
            result, _resolved_path, route_plan = await driver_self._maybe_retry_with_cognitive_critique(
                user_query=original_user_query,
                mode=mode,
                session_id=session_id,
                run_id=_run_id,
                result=result,
                current_execution_path=_resolved_path,
                firewall_verdict=firewall_verdict,
                route_plan=route_plan,
            )
            driver_self._apply_negative_memory_guidance(result=result)
            driver_self._attach_epistemic_firewall_verdict(result=result, verdict=firewall_verdict)
            driver_self._attach_thermodynamic_route(result=result, route_state=route_plan)
            driver_self._attach_runtime_consumption_context(result=result, runtime_context=runtime_consumption_context)
            if user_query != original_user_query:
                result["effective_user_query"] = user_query

        if isinstance(result, dict) and resource_fabric:
            result.setdefault("resource_fabric", resource_fabric)
            result["effective_user_query"] = user_query
        elif isinstance(result, dict) and user_query != original_user_query:
            result["effective_user_query"] = user_query

        # ── Evaluate promotion gate ──
        sid = (result or {}).get("session_id") or (session_id or "unknown")
        gate_verdict = None
        if isinstance(result, dict):
            gate_verdict = driver_self._evaluate_final_result_promotion(
                session_id=sid,
                run_id=driver_self._session_run_ids.get(sid, run_id),
                result=result,
            )

        # ── Persistence & telemetry ──
        await driver_self._persist_driver_session_success(
            session_id=sid,
            result=result if isinstance(result, dict) else {"final_result": result},
            bucket=bucket,
            run_id=driver_self._session_run_ids.get(sid, run_id),
        )
        await driver_self._append_conversation_log(
            session_id=sid,
            user_query=original_user_query,
            mode=mode,
            result=result,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            error=None,
        )

        try:
            driver_self._write_run_manifest(
                session_id=sid,
                mode=mode,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                result=result,
                error=None,
            )
        except Exception:
            pass

        try:
            driver_self._write_report_card(
                session_id=sid,
                mode=mode,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                result=result,
                error=None,
            )
        except Exception:
            pass

        try:
            artifact_path = driver_self._persist_final_result_artifact(session_id=sid, result=result)
            driver_self._persist_evidence_ledger(sid)
            if gate_verdict is None or gate_verdict.passed:
                await driver_self._publish_communication_artifact_announcement(
                    session_id=sid,
                    run_id=driver_self._session_run_ids.get(sid, sid),
                    result=result,
                    artifact_path=artifact_path,
                )
            else:
                await driver_self._emit_runtime_status_telemetry(
                    session_id=sid,
                    run_id=driver_self._session_run_ids.get(sid, sid),
                    phase="artifact_promotion",
                    status="blocked",
                    details=gate_verdict.reason,
                    mode=mode,
                    severity="warning",
                    metrics={
                        "promotion_gate_passed": 0.0,
                        "promotion_block_reason_count": float(len(gate_verdict.promotion_block_reasons)),
                    },
                    artifact_refs=[artifact_path] if artifact_path else [],
                    evidence_refs=[str(claim.get("claim_id") or "") for claim in ((result or {}).get("final_result", {}) or {}).get("claims", []) if isinstance(claim, dict) and str(claim.get("claim_id") or "")],
                    source_ids=[str(source.get("source_id") or "") for source in ((result or {}).get("final_result", {}) or {}).get("sources", []) if isinstance(source, dict) and str(source.get("source_id") or "")],
                )
        except Exception:
            pass

        try:
            cost_info: Dict[str, Any] = {}
            for k in ("cost_usd", "estimated_cost_usd", "total_cost_usd", "cost"):
                if isinstance((result or {}).get(k), (int, float)):
                    cost_info[k] = float(result[k])
            if cost_info:
                cost_info["duration_s"] = (datetime.now(timezone.utc) - started_at).total_seconds()
                await driver_self._append_saga_event_timescale(
                    session_id=sid,
                    event={
                        "type": "cost_summary",
                        "status": "success",
                        "run_id": driver_self._session_run_ids.get(sid),
                        "payload": cost_info,
                    },
                )
        except Exception:
            pass

        try:
            final_result = (result or {}).get("final_result") if isinstance(result, dict) else {}
            if not isinstance(final_result, dict):
                final_result = {}
            artifact_refs = [
                str(artifact.get("path") or "")
                for artifact in final_result.get("artifacts", [])
                if isinstance(artifact, dict) and str(artifact.get("path") or "")
            ]
            evidence_refs = [
                str(claim.get("claim_id") or "")
                for claim in final_result.get("claims", [])
                if isinstance(claim, dict) and str(claim.get("claim_id") or "")
            ]
            source_ids = [
                str(source.get("source_id") or "")
                for source in final_result.get("sources", [])
                if isinstance(source, dict) and str(source.get("source_id") or "")
            ]
            await driver_self._emit_runtime_status_telemetry(
                session_id=sid,
                run_id=driver_self._session_run_ids.get(sid, sid),
                phase="process_agentic_prompt",
                status="completed",
                details=f"Driver workflow completed via {_resolved_path}.",
                mode=mode,
                metrics={
                    "duration_s": (datetime.now(timezone.utc) - started_at).total_seconds(),
                    "artifact_count": len(artifact_refs),
                    "evidence_count": len(evidence_refs),
                },
                artifact_refs=artifact_refs,
                evidence_refs=evidence_refs,
                source_ids=source_ids,
            )
        except Exception:
            pass

        # ── Attach session audit bundle ──
        try:
            if driver_self._audit_builder is not None:
                driver_self._audit_builder.set_decision_ledger(
                    driver_self._decision_ledger.to_audit_bundle()
                )
                for cr in (driver_self._active_cue_results or []):
                    driver_self._audit_builder.add_cue_result(cr)
                audit_bundle = driver_self._audit_builder.build()
                result["_session_audit_bundle"] = audit_bundle.to_dict()
        except Exception:
            pass

        return result

    except Exception as exc:
        sid = session_id or "unknown"
        run_id = driver_self._session_run_ids.get(sid, sid)
        await driver_self._persist_driver_session_failure(
            session_id=sid,
            exc=exc,
            bucket=bucket,
            run_id=run_id,
        )
        await driver_self._append_conversation_log(
            session_id=sid,
            user_query=original_user_query,
            mode=mode,
            result=None,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            error=str(exc),
        )

        try:
            driver_self._write_run_manifest(
                session_id=sid,
                mode=mode,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                result=None,
                error=str(exc),
            )
        except Exception:
            pass

        try:
            driver_self._write_report_card(
                session_id=sid,
                mode=mode,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                result=None,
                error=str(exc),
            )
        except Exception:
            pass

        try:
            driver_self._persist_evidence_ledger(sid)
        except Exception:
            pass

        try:
            error_artifact = driver_self._persist_runtime_error_artifact(
                session_id=sid,
                run_id=run_id,
                phase="process_agentic_prompt",
                exc=exc,
                mode=mode,
                user_query=original_user_query,
            )
            await driver_self._emit_runtime_error_telemetry(
                session_id=sid,
                run_id=run_id,
                phase="process_agentic_prompt",
                error_type=type(exc).__name__,
                message=_truncate_text(_redact_text(str(exc)), max_len=2000),
                traceback_text=traceback.format_exc(),
                artifact_path=getattr(error_artifact, "path", None),
                rescue_suggestion=getattr(error_artifact, "rescue_hint", None),
                mode=mode,
                retryable=driver_self._is_retryable_runtime_exception(exc),
            )
        except Exception:
            pass
        raise

    finally:
        if token_bucket is not None:
            try:
                from ..agentic_driver import _current_bucket_var
                _current_bucket_var.reset(token_bucket)
            except Exception:
                pass
        if token_user is not None:
            try:
                from ..agentic_driver import _current_user_id_var
                _current_user_id_var.reset(token_user)
            except Exception:
                pass
        if _ws_token is not None:
            try:
                driver_self._workspace_id_var.reset(_ws_token)
            except Exception:
                pass

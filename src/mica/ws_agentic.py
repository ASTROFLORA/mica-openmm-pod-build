from __future__ import annotations

import asyncio
import json
import os
import uuid
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
import logging

from config.settings import settings
from mica.infrastructure.persistence.session_repository import NeonSessionRepository
from mica.services.session_persistence import (
    persist_frontend_session_state,
    restore_frontend_session_state,
)


logger = logging.getLogger(__name__)

_WS_FAST_PATH_DEADLINE = int(os.environ.get("MICA_WS_FAST_PATH_DEADLINE_SECONDS", "120"))
_WS_DRIVER_PATH_DEADLINE = int(os.environ.get("MICA_WS_DRIVER_PATH_DEADLINE_SECONDS", "300"))


def _is_production_env() -> bool:
    env = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "development"
    return str(env).lower() in ("prod", "production")


def _extract_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip() or None


def _allow_ws_user_fallback() -> bool:
    return (os.getenv("MICA_WS_ALLOW_USER_ID_FALLBACK") or "false").lower() == "true"


def _is_local_origin(origin: str) -> bool:
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _allow_local_cli_ws_fallback(*, origin: str, authorization: Optional[str], fallback_user: Optional[str]) -> bool:
    """Allow explicit local CLI WS auth without weakening browser auth defaults.

    Contract:
    - never in production
    - never when Clerk/JWKS auth is configured
    - only for loopback/no-origin local clients
    - only when the client explicitly supplied a fallback user id
    """

    if _is_production_env():
        return False
    if os.getenv("CLERK_JWKS_URL"):
        return False
    if authorization:
        return False
    if not _is_local_origin(origin):
        return False
    return bool(fallback_user and fallback_user.strip())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_like_scientific_workflow(text: str) -> bool:
    """Best-effort heuristic for whether a prompt should run the full AgenticDriver.

    The driver init spins up many MCP servers; for casual/simple chat, we use a fast
    direct LLM path so the UI stays responsive.
    """

    normalized = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("utf-8")
    normalized = normalized.lower()
    keywords = [
        "simulate",
        "dynamics",
        "trajectory",
        "md",
        "protein",
        "uniprot",
        "pdb",
        "alphafold",
        "drug",
        "ligand",
        "docking",
        "adme",
        "pubmed",
        "papers",
        "research",
        "arxiv",
        "chembl",
    ]
    return any(kw in normalized for kw in keywords)


def _safe_number_metrics(obj: Any) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        if isinstance(v, (int, float)):
            out[str(k)] = float(v)
    return out


def _initial_frontend_state(*, workflow_id: str, workflow_type: str = "reactive") -> Dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "workflow_type": workflow_type,
        "active_node": "idle",
        "iteration_count": 0,
        "msrp_current_phase": 0,
        "msrp_status": {
            "phase_1": "PENDING",
            "phase_2": "PENDING",
            "phase_3": "PENDING",
            "phase_4": "PENDING",
            "phase_5": "PENDING",
        },
        "quality_metrics": {},
        "event_log": [],
        "proactive_triggers": [],
        "auto_generated_tasks": [],
        "status": "idle",
        "message": "Online - AgenticDriver",
    }


_NODE_TO_MSRP_PHASE: Dict[str, int] = {
    "initialize": 1,
    "analyze": 1,
    "route": 2,
    "decompose": 2,
    "assign": 2,
    "execute": 3,
    "quality_gate": 4,
    "synthesize": 5,
    "proactive_monitor": 5,
    "finalization": 5,
}


def _msrp_key(phase: int) -> str:
    return f"phase_{phase}"


def _set_msrp_phase(state: Dict[str, Any], phase: int, status: str) -> None:
    if phase < 1 or phase > 5:
        return
    msrp = state.get("msrp_status")
    if not isinstance(msrp, dict):
        msrp = {}
    key = _msrp_key(phase)
    msrp[key] = status
    state["msrp_status"] = msrp
    state["msrp_current_phase"] = phase


def _running_msrp_phase(state: Dict[str, Any]) -> Optional[int]:
    msrp = state.get("msrp_status")
    if not isinstance(msrp, dict):
        return None
    for phase in range(1, 6):
        if str(msrp.get(_msrp_key(phase)) or "").upper() == "RUNNING":
            return phase
    return None


def _merge_frontend_state(
    *,
    workflow_id: str,
    workflow_type: str,
    user_id: str,
    workspace_id: str,
    restored_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    default_state = _initial_frontend_state(workflow_id=workflow_id, workflow_type=workflow_type)
    merged_state = dict(default_state)

    if isinstance(restored_state, dict):
        merged_state.update(restored_state)

    merged_state["msrp_status"] = {
        **default_state["msrp_status"],
        **(merged_state.get("msrp_status") if isinstance(merged_state.get("msrp_status"), dict) else {}),
    }

    for dict_field in (
        "quality_metrics",
        "protocol_runtime",
        "unified_protocol_runtime",
        "protocol_runtime_projection",
        "scientific_protocol",
        "prompt_protocol",
    ):
        if not isinstance(merged_state.get(dict_field), dict):
            merged_state[dict_field] = {}

    for list_field in (
        "event_log",
        "proactive_triggers",
        "auto_generated_tasks",
        "protocol_events",
    ):
        if not isinstance(merged_state.get(list_field), list):
            merged_state[list_field] = []

    merged_state["workflow_id"] = workflow_id
    merged_state["workflow_type"] = workflow_type
    merged_state["user_id"] = user_id
    merged_state["workspace_id"] = workspace_id

    if restored_state is not None:
        resume_phase = _running_msrp_phase(merged_state)
        merged_state["status"] = "idle"
        if resume_phase is not None:
            merged_state["resume_phase"] = resume_phase
            merged_state["message"] = (
                f"Recovered session state from MSRP phase {resume_phase}. Awaiting resume."
            )
        else:
            merged_state["message"] = "Recovered previous session state."

    return merged_state


_DriverGetter = Callable[[], Awaitable[Any]]


_driver_singleton: Any = None
_driver_lock = asyncio.Lock()


async def _get_agentic_driver() -> Any:
    global _driver_singleton
    async with _driver_lock:
        if _driver_singleton is not None:
            return _driver_singleton

        try:
            from mica.config.dotenv_loader import seed_env_from_dotenv

            seed_env_from_dotenv()
        except Exception:
            # Best-effort: env loading should not prevent WS from running.
            pass

        from mica.drivers.agentic_driver import AgenticDriver, AgenticDriverConfig

        cfg = AgenticDriverConfig.from_driver_config()
        drv = AgenticDriver(config=cfg)
        try:
            await drv.initialize_async()
        except Exception:
            # Best-effort: allow running without MCP connectivity.
            pass
        _driver_singleton = drv
        return drv


async def handle_mica_agentic_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint compatible with `Alejandria-Ultimate/types.ts`.

    Outbound messages:
    - { type: 'STATE_UPDATE', payload: MICAState }
    - { type: 'TEXT_MESSAGE', payload: { id, text, timestamp, artifact? } }

    Inbound messages (minimal):
    - { type: 'SEND_MESSAGE', payload: { text, sessionId?, mode?, activeWorkers? } }
    """

    origin = (websocket.headers.get("origin") or "").strip()
    allowed_origins_env = os.getenv("WS_ALLOW_ORIGINS") or os.getenv("CORS_ALLOW_ORIGINS") or ""
    allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    enforce_origin = (os.getenv("WS_ENFORCE_ORIGIN") or ("true" if _is_production_env() else "false")).lower() == "true"

    if enforce_origin and allowed_origins and "*" not in allowed_origins:
        if not origin or origin not in allowed_origins:
            logger.warning(
                "MICA WS auth rejected status=%s origin=%s reason=%s allowed_origins=%s",
                403,
                origin or "<missing>",
                "Invalid Origin",
                allowed_origins,
            )
            await websocket.close(code=1008, reason="Invalid Origin")
            return

    authorization = websocket.headers.get("authorization")
    token = _extract_bearer(authorization)
    if not token:
        qs_token = (websocket.query_params.get("token") or "").strip()
        qs_ticket = (websocket.query_params.get("ticket") or "").strip()
        if qs_token:
            # ?token= query param — browser WebSocket API cannot send headers,
            # so this is the only viable mechanism for browser clients.
            # Token is still verified via Clerk; transport is HTTPS-encrypted.
            logger.warning(
                "WebSocket auth via ?token= query param — "
                "Authorization header preferred when not a browser WebSocket client"
            )
            token = qs_token
            authorization = f"Bearer {qs_token}"
        elif qs_ticket:
            token = qs_ticket
            authorization = f"Bearer {qs_ticket}"

    fallback_user = (
        (websocket.query_params.get("userId") or "").strip()
        or (websocket.query_params.get("user_id") or "").strip()
        or (websocket.headers.get("x-user-id") or "").strip()
        or None
    )
    if fallback_user and not (_allow_ws_user_fallback() or _allow_local_cli_ws_fallback(
        origin=origin,
        authorization=authorization,
        fallback_user=fallback_user,
    )):
        fallback_user = None

    try:
        if _allow_local_cli_ws_fallback(
            origin=origin,
            authorization=authorization,
            fallback_user=fallback_user,
        ):
            user_id = str(fallback_user).strip()
            logger.warning(
                "MICA WS local CLI fallback accepted user=%s transport=ws origin=%s",
                user_id,
                origin or "<missing>",
            )
        else:
            # WebSocket endpoints cannot use FastAPI Depends(Request) the same way
            # HTTP endpoints do; resolve the user id directly from the websocket scope
            # using the same logic as the HTTP `user_dependency` helper, but skipping
            # the `request` arg that would normally be injected.
            from mica.api_v1.auth import resolve_user_id
            user_id = resolve_user_id(
                x_user_id=fallback_user,
                authorization=authorization,
                request=None,
                transport="ws",
            )
    except HTTPException as exc:
        reason = str(exc.detail) if exc.detail else "Authentication failed"
        logger.warning(
            "MICA WS auth rejected status=%s origin=%s reason=%s",
            exc.status_code,
            origin or "<missing>",
            reason,
        )
        await websocket.close(code=1008, reason=reason[:120])
        return

    await websocket.accept()

    workflow_id = websocket.query_params.get("sessionId") or f"session_{uuid.uuid4().hex}"
    # P2-4: Accept workspace_id from client (query param or header).
    workspace_id = (
        (websocket.query_params.get("workspaceId") or "").strip()
        or (websocket.query_params.get("workspace_id") or "").strip()
        or (websocket.headers.get("x-mica-workspace-id") or "").strip()
    )
    session_repo: Optional[NeonSessionRepository] = (
        NeonSessionRepository() if getattr(settings, "enable_session_persistence", False) else None
    )
    session_persistence_enabled = session_repo is not None

    async def build_frontend_state(
        *,
        target_workflow_id: str,
        workflow_type: str = "reactive",
        restore: bool = False,
    ) -> Dict[str, Any]:
        nonlocal session_persistence_enabled

        restored_state: Optional[Dict[str, Any]] = None
        if restore and session_persistence_enabled and session_repo is not None:
            try:
                restored_state = await restore_frontend_session_state(
                    session_id=target_workflow_id,
                    user_id=user_id,
                    repo=session_repo,
                )
            except Exception as exc:
                session_persistence_enabled = False
                logger.warning(
                    "WS session restore disabled for %s after failure: %s",
                    target_workflow_id,
                    exc,
                )

        return _merge_frontend_state(
            workflow_id=target_workflow_id,
            workflow_type=workflow_type,
            user_id=user_id,
            workspace_id=workspace_id,
            restored_state=restored_state,
        )

    state: Dict[str, Any] = await build_frontend_state(
        target_workflow_id=workflow_id,
        restore=True,
    )

    event_log: List[Dict[str, Any]] = list(state.get("event_log") or [])

    async def send_state_update() -> None:
        nonlocal session_persistence_enabled

        if session_persistence_enabled and session_repo is not None:
            try:
                await persist_frontend_session_state(
                    session_id=str(state.get("workflow_id") or workflow_id),
                    user_id=user_id,
                    state=state,
                    repo=session_repo,
                )
            except Exception as exc:
                session_persistence_enabled = False
                logger.warning(
                    "WS session persistence disabled for %s after failure: %s",
                    state.get("workflow_id") or workflow_id,
                    exc,
                )
        try:
            await websocket.send_json({"type": "STATE_UPDATE", "payload": state})
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError can be raised when trying to send after close.
            raise WebSocketDisconnect
        except Exception:
            raise WebSocketDisconnect
        # APV-10: fail-soft mirror onto ProductEventOutbox (/ws/product authority).
        try:
            from mica.experience.product_event_bridge import try_bridge_agentic_frame

            try_bridge_agentic_frame(
                actor_user_id=str(user_id),
                session_id=str(state.get("workflow_id") or workflow_id),
                frame_type="STATE_UPDATE",
                payload={"workflow_id": state.get("workflow_id"), "status": state.get("status")},
            )
        except Exception:
            pass

    async def send_text_message(text: str, artifact: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "text": str(text),
            "timestamp": _utc_now_iso(),
        }
        if artifact is not None:
            payload["artifact"] = artifact
        try:
            await websocket.send_json({"type": "TEXT_MESSAGE", "payload": payload})
        except (WebSocketDisconnect, RuntimeError):
            raise WebSocketDisconnect
        except Exception:
            raise WebSocketDisconnect
        try:
            from mica.experience.product_event_bridge import try_bridge_agentic_frame

            try_bridge_agentic_frame(
                actor_user_id=str(user_id),
                session_id=str(state.get("workflow_id") or workflow_id),
                frame_type="TEXT_MESSAGE",
                payload=payload,
            )
        except Exception:
            pass

    async def stream_llm_response(
        stream_id: str,
        backend: Any,
        prompt: str,
        system_prompt: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Stream LLM tokens to the frontend as ``STREAM_TOKEN`` messages.

        Sends:
        - ``{ type: 'STREAM_START', payload: { id } }``  — once at the beginning
        - ``{ type: 'STREAM_TOKEN', payload: { id, token } }``  — per chunk
        - ``{ type: 'STREAM_END',   payload: { id } }``  — once at the end

        Returns the full assembled response string.
        """
        full_text = ""
        try:
            await websocket.send_json({"type": "STREAM_START", "payload": {"id": stream_id}})
            async for chunk in backend.invoke_stream(
                prompt=prompt,
                system_prompt=system_prompt,
                messages=messages,
            ):
                full_text += chunk
                token_payload = {"id": stream_id, "token": chunk}
                await websocket.send_json(
                    {"type": "STREAM_TOKEN", "payload": token_payload}
                )
                try:
                    from mica.experience.product_event_bridge import try_bridge_agentic_frame

                    try_bridge_agentic_frame(
                        actor_user_id=str(user_id),
                        session_id=str(state.get("workflow_id") or workflow_id),
                        frame_type="STREAM_TOKEN",
                        payload=token_payload,
                    )
                except Exception:
                    pass
        except (WebSocketDisconnect, RuntimeError):
            raise WebSocketDisconnect
        except Exception as exc:
            logger.warning("LLM stream error: %s", exc)
        finally:
            try:
                await websocket.send_json({"type": "STREAM_END", "payload": {"id": stream_id}})
            except Exception:
                pass
        return full_text

    def append_event(event_type: str, node_id: str, data: Any) -> None:
        nonlocal event_log
        ev = {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "node_id": node_id,
            "workflow_id": state["workflow_id"],
            "timestamp": _utc_now_iso(),
            "data": data,
        }
        event_log = (event_log + [ev])[-50:]
        state["event_log"] = event_log

    async def event_sink(event: Dict[str, Any]) -> None:
        """Receives events emitted by the driver."""
        try:
            event_type = str(event.get("event_type") or "")
            node_id = str(event.get("node_id") or "")
            data = event.get("data")

            if event_type == "NodeExecutionStarted" and node_id:
                state["active_node"] = node_id
                state["status"] = "executing"
                state["message"] = f"Running node: {node_id}"

                phase = _NODE_TO_MSRP_PHASE.get(node_id)
                if phase is not None:
                    _set_msrp_phase(state, phase, "RUNNING")

            if event_type == "NodeExecutionCompleted" and node_id:
                phase = _NODE_TO_MSRP_PHASE.get(node_id)
                if phase is not None:
                    # Don't override a later phase that might already be running.
                    _set_msrp_phase(state, phase, "PASSED")

            if event_type == "QualityAssessment":
                q = event.get("data") or {}
                if isinstance(q, dict):
                    score = q.get("score")
                    if isinstance(score, (int, float)):
                        state["quality_metrics"]["quality_score"] = float(score)

            if event_type == "ToolCallStarted":
                state["status"] = "executing"
                if isinstance(data, dict):
                    sub = data.get("subtask_id")
                    worker = data.get("worker")
                    state["message"] = f"Tool call: {worker} ({sub})"

            if event_type == "ToolCallCompleted":
                state["status"] = "executing"

            if event_type == "WorkflowStarted":
                state["status"] = "thinking"
                state["message"] = "Initializing workflow..."

            if event_type == "RemoteMDProgress":
                progress = data if isinstance(data, dict) else {}
                phase = str(progress.get("vast_phase") or progress.get("phase") or "").strip()
                detail = str(progress.get("last_event_message") or progress.get("message") or "").strip()
                progress_status = str(progress.get("status") or "running").strip().lower()

                state["status"] = "executing"
                state["message"] = (
                    f"Remote MD {phase.replace('_', ' ')}: {detail}".strip(": ")
                    if phase or detail
                    else "Remote MD running..."
                )

                protocol_runtime = dict(state.get("protocol_runtime") or {})
                previous_remote_md = (
                    dict(protocol_runtime.get("remote_md") or {})
                    if isinstance(protocol_runtime.get("remote_md"), dict)
                    else {}
                )
                protocol_runtime["remote_md"] = {
                    **previous_remote_md,
                    **progress,
                    "status": progress_status or previous_remote_md.get("status") or "running",
                    "updated_at": event.get("timestamp") or _utc_now_iso(),
                }
                state["protocol_runtime"] = protocol_runtime

            if event_type == "WorkflowCompleted":
                state["status"] = "replying"
                state["message"] = "Workflow completed."

            append_event(event_type, node_id or state.get("active_node", "unknown"), data)
            await send_state_update()
        except Exception:
            # Never crash the websocket loop on sink errors.
            return

    await send_state_update()

    async def ws_keepalive() -> None:
        # Starlette doesn't proactively ping; keep the connection active during
        # long workflows by emitting a lightweight heartbeat the UI can ignore.
        while True:
            try:
                await asyncio.sleep(30)
                # Use an existing message type expected by the frontend.
                # This also nudges the proxy/UI to avoid idle timeouts.
                await websocket.send_json({"type": "STATE_UPDATE", "payload": state})
            except asyncio.CancelledError:
                return
            except WebSocketDisconnect:
                return
            except RuntimeError:
                return
            except Exception:
                return

    keepalive_task = asyncio.create_task(ws_keepalive())

    busy = False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await send_text_message("Invalid message (expected JSON).")
                continue

            msg_type = msg.get("type")
            payload = msg.get("payload") or {}

            if msg_type == "PING":
                await websocket.send_json({"type": "PONG", "payload": {"ts": _utc_now_iso()}})
                continue

            if msg_type != "SEND_MESSAGE":
                await send_text_message(f"Unsupported message type: {msg_type}")
                continue

            if busy:
                await send_text_message("Agent busy; wait for completion.")
                continue

            text = str(payload.get("text") or "").strip()
            if not text:
                await send_text_message("Empty message.")
                continue

            requested_provider_id = payload.get("provider_id")
            requested_model_id = payload.get("model_id")
            provider_id = str(requested_provider_id).strip() if requested_provider_id is not None else None
            model_id = str(requested_model_id).strip() if requested_model_id is not None else None
            if provider_id == "":
                provider_id = None
            if model_id == "":
                model_id = None

            requested_session = payload.get("sessionId")
            if isinstance(requested_session, str) and requested_session:
                workflow_id = requested_session

            workflow_type = "reactive"
            mode = payload.get("mode")
            if isinstance(mode, str) and mode.lower() in {"proactive_board", "swarm"}:
                workflow_type = "reactive"

            # Reset UI state for new workflow.
            state = await build_frontend_state(
                target_workflow_id=workflow_id,
                workflow_type=workflow_type,
                restore=isinstance(requested_session, str) and bool(requested_session),
            )
            state["status"] = "thinking"
            state["message"] = "Initializing workflow..."
            state["user_id"] = user_id
            state["workspace_id"] = workspace_id
            event_log = list(state.get("event_log") or []) if state.get("resume_phase") else []
            state["event_log"] = event_log
            await send_state_update()

            busy = True
            try:
                # ── AgenticLoop path (replaces keyword-heuristic routing) ──
                # All queries go through AgenticLoop with tools. The model
                # decides whether to use tools or respond directly.
                if not _looks_like_scientific_workflow(text):
                    append_event("WorkflowStarted", "initialize", {"preview": text[:160], "fast_path": True})
                    await send_state_update()
                    try:
                        from mica.agentic.ws_bridge import stream_agentic_loop

                        result = await asyncio.wait_for(
                            stream_agentic_loop(
                                websocket,
                                user_text=text,
                                session_id=workflow_id,
                                provider_id=provider_id,
                                model_id=model_id,
                            ),
                            timeout=_WS_FAST_PATH_DEADLINE,
                        )
                        if isinstance(result, dict):
                            if isinstance(result.get("protocol_runtime"), dict):
                                state["protocol_runtime"] = dict(result.get("protocol_runtime") or {})
                            if isinstance(result.get("unified_protocol_runtime"), dict):
                                state["unified_protocol_runtime"] = dict(result.get("unified_protocol_runtime") or {})
                            if isinstance(result.get("protocol_runtime_projection"), dict):
                                state["protocol_runtime_projection"] = dict(result.get("protocol_runtime_projection") or {})
                            if isinstance(result.get("protocol_events"), list):
                                state["protocol_events"] = list(result.get("protocol_events") or [])
                            if isinstance(result.get("scientific_protocol"), dict):
                                state["scientific_protocol"] = dict(result.get("scientific_protocol") or {})
                            if isinstance(result.get("prompt_protocol"), dict):
                                state["prompt_protocol"] = dict(result.get("prompt_protocol") or {})
                        append_event(
                            "WorkflowCompleted",
                            "finalization",
                            {
                                "ok": True,
                                "fast_path": True,
                                "finish_reason": result.get("finish_reason", "unknown"),
                                "steps": result.get("total_steps", 0),
                            },
                        )
                    except asyncio.TimeoutError:
                        logger.error("WS fast-path timed out after %ds", _WS_FAST_PATH_DEADLINE)
                        await send_text_message(
                            f"Request timed out after {_WS_FAST_PATH_DEADLINE}s. Please try again or simplify your query."
                        )
                    except Exception as exc:
                        error_id = uuid.uuid4().hex
                        append_event(
                            "WorkflowFailed",
                            "finalization",
                            {"error_id": error_id, "error": str(exc), "fast_path": True},
                        )
                        await send_state_update()
                        logger.exception("WS agentic-loop failed (error_id=%s)", error_id)
                        await send_text_message(
                            f"Workflow failed (error_id={error_id}). Revisa backend logs para detalles."
                        )
                    finally:
                        state["status"] = "idle"
                        state["message"] = "Workflow Completed. Awaiting new command."
                        await send_state_update()
                    continue

                async def warmup_heartbeat() -> None:
                    # First-run init can be slow (MCP servers). Keep UI responsive.
                    while True:
                        try:
                            await asyncio.sleep(5)
                            if state.get("status") == "thinking":
                                state["message"] = "Warming tools (first run can take a bit)..."
                                await send_state_update()
                        except asyncio.CancelledError:
                            return
                        except WebSocketDisconnect:
                            return
                        except Exception:
                            # Don't crash the main workflow on heartbeat errors.
                            return

                warmup_task = asyncio.create_task(warmup_heartbeat())
                try:
                    driver = await _get_agentic_driver()
                finally:
                    warmup_task.cancel()
                    try:
                        await warmup_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass

                # Wire driver events to WS.
                token = None
                try:
                    token = driver._event_sink_var.set(event_sink)  # type: ignore[attr-defined]
                    append_event("WorkflowStarted", "initialize", {"preview": text[:160]})
                    await send_state_update()

                    result = await asyncio.wait_for(
                        driver.process_agentic_prompt(
                            user_query=text,
                            mode="production",
                            session_id=workflow_id,
                            workspace_id=state.get("workspace_id", ""),
                            output_contract=(
                                "tool_only_json"
                                if any(kw in text.lower() for kw in (
                                    "run_dlm_graph_repair_export",
                                    "graph repair export",
                                    "graph-repair export",
                                    "export the graph",
                                    "export graph",
                                    "dlm graph repair",
                                ))
                                else "default"
                            ),
                        ),
                        timeout=_WS_DRIVER_PATH_DEADLINE,
                    )
                finally:
                    if token is not None:
                        driver._event_sink_var.reset(token)  # type: ignore[attr-defined]

                final = (result or {}).get("final_result")
                if isinstance(final, dict):
                    summary = final.get("summary") or final.get("answer") or final.get("query")
                    await send_text_message(str(summary or "Workflow completed."))
                else:
                    await send_text_message(str(final or "Workflow completed."))

                state["status"] = "idle"
                state["message"] = "Workflow Completed. Awaiting new command."
                # Best-effort numeric metrics
                state["iteration_count"] = int((result or {}).get("provenance", {}).get("iterations", 0) or 0)
                qm = (result or {}).get("quality_metrics")
                state["quality_metrics"].update(_safe_number_metrics(qm))
                append_event("WorkflowCompleted", "finalization", {"ok": True})
                await send_state_update()

            except asyncio.TimeoutError:
                logger.error("WS driver-path timed out after %ds", _WS_DRIVER_PATH_DEADLINE)
                state["status"] = "idle"
                state["message"] = "Workflow timed out."
                await send_state_update()
                await send_text_message(
                    f"Scientific workflow timed out after {_WS_DRIVER_PATH_DEADLINE}s. "
                    "Please try again or narrow the scope of your query."
                )

            except Exception as exc:
                error_id = uuid.uuid4().hex
                append_event(
                    "WorkflowFailed",
                    state.get("active_node", "unknown"),
                    {"error_id": error_id, "error": str(exc)},
                )
                state["status"] = "idle"
                state["message"] = "Workflow failed."
                await send_state_update()
                logger.exception("WS workflow failed (error_id=%s)", error_id)
                await send_text_message(
                    f"Workflow failed (error_id={error_id}). Revisa backend logs para detalles."
                )
            finally:
                busy = False

    except WebSocketDisconnect:
        return
    finally:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        if session_repo is not None:
            try:
                await session_repo.close()
            except Exception:
                pass

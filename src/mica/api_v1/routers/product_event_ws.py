"""APV-09 Product WS — native event client endpoint.

Protocol: client.hello -> ProductEventEnvelope frames (urn:mica:ws:server:v1)
Legacy /ws/mica remains for agentic chat; this socket is the product event authority.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mica.experience.native_event_client import (
    ClientHello,
    NativeEventError,
    envelope_to_ws_message,
    get_native_event_runtime,
)
from mica.experience.product_events import ProductEventError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["product-event-ws"])


def _actor_from_websocket(websocket: WebSocket) -> str:
    user = (
        (websocket.query_params.get("userId") or "").strip()
        or (websocket.query_params.get("user_id") or "").strip()
        or (websocket.headers.get("x-user-id") or "").strip()
    )
    if not user:
        raise NativeEventError("X-User-Id or userId query required")
    return user


async def _send_events(websocket: WebSocket, events: list[Any]) -> None:
    for event in events:
        await websocket.send_json(envelope_to_ws_message(event))


@router.websocket("/ws/product")
async def product_event_websocket(websocket: WebSocket) -> None:
    """Native product event socket.

    Client must send client.hello first. Subsequent commands:
      - surface.mount
      - surface.lifecycle
      - workspace.manifest.mount
      - client.ack
      - legacy.adapt
    """
    await websocket.accept()
    runtime = get_native_event_runtime()
    session_id: str | None = None
    try:
        actor = _actor_from_websocket(websocket)
    except NativeEventError as exc:
        await websocket.close(code=1008, reason=str(exc)[:120])
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid_json"})
                continue
            if not isinstance(message, dict):
                await websocket.send_json({"type": "error", "detail": "message_must_be_object"})
                continue

            msg_type = str(message.get("type") or "")
            try:
                if msg_type == "client.hello":
                    hello = ClientHello.model_validate(message)
                    session, events = runtime.handle_hello(hello=hello, actor_user_id=actor)
                    session_id = session.session_id
                    await _send_events(websocket, events)
                    continue

                if session_id is None:
                    await websocket.send_json(
                        {"type": "error", "detail": "client.hello required first"}
                    )
                    continue

                if msg_type == "surface.mount":
                    surface, event, created = runtime.mount_surface(
                        session_id=session_id,
                        object_ref=str(message.get("object_ref") or ""),
                        surface_type=str(message.get("surface_type") or "generic"),
                        family=str(message.get("family") or "research"),
                        source_app=message.get("source_app"),
                        view_purpose=str(message.get("view_purpose") or "default"),
                        singleton_per_object=bool(message.get("singleton_per_object", True)),
                        lifecycle=message.get("lifecycle") or "preview",
                    )
                    await websocket.send_json(
                        {
                            **envelope_to_ws_message(event),
                            "created": created,
                            "surface_id": surface.surface_id,
                        }
                    )
                    continue

                if msg_type == "surface.lifecycle":
                    event = runtime.set_surface_lifecycle(
                        session_id=session_id,
                        surface_id=str(message.get("surface_id") or ""),
                        lifecycle=message.get("lifecycle") or "preview",
                    )
                    await _send_events(websocket, [event])
                    continue

                if msg_type == "workspace.manifest.mount":
                    event = runtime.publish_workspace_manifest_event(
                        session_id=session_id,
                        workspace_id=str(message.get("workspace_id") or ""),
                    )
                    await _send_events(websocket, [event])
                    continue

                if msg_type == "client.ack":
                    seq = runtime.acknowledge(
                        session_id=session_id,
                        replay_cursor=str(message.get("replay_cursor") or ""),
                    )
                    await websocket.send_json(
                        {"type": "client.ack.ok", "acknowledged_sequence": seq}
                    )
                    continue

                if msg_type == "legacy.adapt":
                    event = runtime.adapt_legacy(
                        session_id=session_id,
                        legacy_type=str(message.get("legacy_type") or "STATE_UPDATE"),
                        payload=message.get("payload") or {},
                    )
                    await _send_events(websocket, [event])
                    continue

                await websocket.send_json(
                    {"type": "error", "detail": f"unknown_message_type:{msg_type}"}
                )
            except (NativeEventError, ProductEventError, ValueError) as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
    except WebSocketDisconnect:
        logger.info("product WS disconnected session_id=%s", session_id)
    except Exception:
        logger.exception("product WS failure session_id=%s", session_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass

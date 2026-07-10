# filepath: src/mica/agentic/commands/phy_commands.py
"""phy_commands.py — Command Kernel handler for `phy.execute` (manifest #80).

PhY-S3: closes the LLM-as-judge / tool-calling path. The manifest entry for
`phy.execute` is already declared in backend_command_manifest.py with
binding_surface="phy_dispatcher" and backend_authority pointing to
`src/mica/phy/dispatcher.py:Dispatcher.dispatch`. This module wires the
Command Kernel to that dispatcher so a tool call from the agentic driver
becomes a real MQTT publish to the physical device.

Doctrina:
- D1: LLM never emits coordinates; only whitelisted payloads by kind.
- D2: Receipt class = "gate" (the manifest marks requires_gate=True).
- D3: route_authority = "command_kernel" (the kernel is the only path that
       can call dispatcher.dispatch when invoked via this surface).

Args (via envelope.arguments):
    kind:           "home" | "move" | "dispense" | "capture" | "stop" | "set_led"
    target:         optional device id (defaults to PHY_DEFAULT_DEVICE_ID)
    payload:        dict, must be whitelisted per kind
    requested_by:   "llm" | "operator" | "system" (default "llm" via tool call)
    correlation_id: optional trace id
    action_id:      optional pre-allocated id (else generated)

Returns:
    {
      "summary": "ok" | "rejected" | "error",
      "result": {
        "action_id": str,
        "status": "ok" | "rejected" | "error",
        "message": str,
        "data": dict,
        "receipt_urn": str | None,
        "transport_connected": bool,
        "transport_kind": str,
      },
      "route_authority": "command_kernel",
      "route_backed": True,
    }
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _envelope_to_dict(result: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a sub-result dict into the kernel envelope shape.

    Mirrors cg_martini_commands._envelope_to_dict so behavior is consistent
    across Command Kernel handlers.
    """
    return {
        "summary": str(result.get("status", "completed")),
        "result": dict(result),
        "route_authority": "command_kernel",
        "route_backed": True,
    }


async def phy_execute(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Dispatch a structured PhY action.

    Validates kind (must be whitelisted), forwards to Dispatcher.dispatch
    (which in turn runs the Executor + publishes via Transport if connected),
    and wraps the resulting PhyResult in the kernel envelope.

    The dispatcher already enforces per-kind payload whitelisting via its
    internal `_safety_guard` (defense-in-depth: the LLM cannot emit
    coordinates even if it tries).
    """
    err = _validate_required(args, "kind")
    if err:
        raise RuntimeError(f"phy.execute: {err}")

    kind = args["kind"]
    if kind not in {"home", "move", "dispense", "capture", "stop", "set_led"}:
        raise RuntimeError(
            f"phy.execute: unsupported kind={kind!r}; "
            "must be one of home|move|dispense|capture|stop|set_led"
        )

    # Lazy import: the lane PhY is intentionally decoupled from agentic
    # boot path. Only loaded when this command is actually executed.
    from mica.phy.dispatcher import Dispatcher

    payload = dict(args.get("payload") or {})
    target = args.get("target")
    requested_by = args.get("requested_by", "llm")
    correlation_id = args.get("correlation_id")
    action_id = args.get("action_id")

    # Reuse one Dispatcher per kernel instance (lazy singleton).
    # Stored on kernel._phy_dispatcher so tests can poke it.
    dispatcher: Optional[Dispatcher] = getattr(kernel, "_phy_dispatcher", None)
    if dispatcher is None:
        dispatcher = Dispatcher()
        try:
            kernel._phy_dispatcher = dispatcher  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            pass

    result = dispatcher.dispatch(
        kind=kind,
        target=target,
        payload=payload,
        requested_by=requested_by,
        correlation_id=correlation_id,
        action_id=action_id,
    )

    transport = dispatcher.transport
    transport_connected = bool(transport and transport.is_connected())
    transport_kind = type(transport).__name__ if transport else "None"

    # Map PhyResult.status to a kernel-friendly summary:
    # "ok" -> "ok"; "rejected" -> "rejected"; anything else -> "error".
    sub = {
        "action_id": result.action_id,
        "status": result.status,
        "message": result.message,
        "data": dict(result.data or {}),
        "receipt_urn": result.receipt_urn,
        "transport_connected": transport_connected,
        "transport_kind": transport_kind,
    }
    logger.info(
        "phy.execute kind=%s action_id=%s status=%s transport=%s",
        kind, sub["action_id"], sub["status"], transport_kind,
    )
    return _envelope_to_dict(sub)


def _validate_required(args: Dict[str, Any], *keys: str) -> Optional[str]:
    """Return None if all required keys are present, else an error message.

    Mirrors cg_martini_commands._validate_required so kernel error shapes
    stay aligned.
    """
    missing = [k for k in keys if not args.get(k)]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}"
    return None
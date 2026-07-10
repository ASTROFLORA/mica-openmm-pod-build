"""Bounded shared-kernel bridge helpers for MO-00.

This slice does not reopen protocol dispatch or projection authority. It only
packages the already-live driver-owned runtime surfaces into one additive
context object that worker drivers can consume without private reach-through.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from mica.agentic.event_bus import get_event_bus


@dataclass(frozen=True)
class ProtocolRuntimeKernelContext:
    """Bounded carrier for shared protocol-runtime kernel state."""

    atom_memory: Any = None
    cue_evaluator: Any = None
    event_bus: Any = None
    event_store: Any = None
    communication_protocol: Any = None
    message_bus: Any = None
    # Packet 7D: optional driver-level event emitter so the bridge can project
    # SnapshotPersisted onto the WS event_sink without creating a circular dep.
    emit_event_fn: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def build_protocol_runtime_kernel_context(
    *,
    atom_memory: Any = None,
    cue_evaluator: Any = None,
    event_bus: Any = None,
    event_store: Any = None,
    communication_protocol: Any = None,
    message_bus: Any = None,
    emit_event_fn: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ProtocolRuntimeKernelContext:
    """Build the bounded kernel context and attach the cue evaluator to the bus.

    The existing governance-side cue contract already speaks a generic
    ``subscribe`` bus. This helper keeps that contract intact and simply binds
    it to the process event bus that later bridge slices can project onto the
    BSM backbone.
    """

    active_event_bus = event_bus or get_event_bus()
    if cue_evaluator is not None:
        bind_fn = getattr(cue_evaluator, "bind_event_bus", None)
        if callable(bind_fn):
            bind_fn(active_event_bus)

    return ProtocolRuntimeKernelContext(
        atom_memory=atom_memory,
        cue_evaluator=cue_evaluator,
        event_bus=active_event_bus,
        event_store=event_store,
        communication_protocol=communication_protocol,
        message_bus=message_bus,
        emit_event_fn=emit_event_fn,
        metadata=dict(metadata or {}),
    )


def bind_worker_driver_protocol_runtime_kernel_context(
    worker_driver: Any,
    kernel_context: ProtocolRuntimeKernelContext,
) -> ProtocolRuntimeKernelContext:
    """Attach the bounded kernel context to a worker driver instance."""

    bind_fn = getattr(worker_driver, "bind_protocol_runtime_kernel_context", None)
    if callable(bind_fn):
        bind_fn(kernel_context)
    else:
        setattr(worker_driver, "protocol_runtime_kernel_context", kernel_context)
    return kernel_context


class ProtocolRuntimeEventBridge:
    """Project internal snapshot events onto the compatibility MessageBus."""

    def __init__(
        self,
        *,
        kernel_context: ProtocolRuntimeKernelContext,
        session_id: str,
        agent_name: str = "protocol_runtime_kernel_bridge",
    ) -> None:
        self.kernel_context = kernel_context
        self.session_id = session_id
        self.agent_name = agent_name

    def bind_event_bus(self, event_bus: Any | None = None) -> None:
        active_event_bus = event_bus or self.kernel_context.event_bus or get_event_bus()
        try:
            from mica.agentic.events import SnapshotPersisted
        except ImportError:
            return
        active_event_bus.subscribe(SnapshotPersisted, self._on_snapshot_persisted)

    def _on_snapshot_persisted(self, event: Any) -> None:
        payload = {
            "snapshot_id": str(getattr(event, "snapshot_id", "")),
            "user_id": str(getattr(event, "user_id", "default")),
            "mu": float(getattr(event, "mu", 0.0)),
            "sigma": float(getattr(event, "sigma", 0.0)),
            "contributing_quintuples": int(getattr(event, "contributing_quintuples", 0)),
            "entity_count": int(getattr(event, "entity_count", 0)),
            "relation_count": int(getattr(event, "relation_count", 0)),
            "empty_fallback": bool(getattr(event, "empty_fallback", False)),
        }
        run_id = str(getattr(event, "run_id", "") or payload["snapshot_id"] or "protocol-runtime-kernel")

        # ── BSM backbone projection (existing) ───────────────────────────────
        protocol = self.kernel_context.communication_protocol
        publish_fn = getattr(protocol, "publish_shared_kernel_snapshot", None)
        if callable(publish_fn):
            coroutine = publish_fn(
                session_id=self.session_id,
                run_id=run_id,
                agent_name=self.agent_name,
                snapshot=payload,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(coroutine)
            else:
                loop.create_task(coroutine)

        # ── Packet 7D: WS event_sink projection ──────────────────────────────
        # Emit a lightweight KernelSnapshotProjected event through the driver
        # event emitter so the frontend state can track memory kernel changes.
        emit_fn = self.kernel_context.emit_event_fn
        if callable(emit_fn):
            try:
                emit_fn(
                    event_type="KernelSnapshotProjected",
                    node_id="protocol_runtime_kernel_bridge",
                    data={"run_id": run_id, **payload},
                )
            except Exception:
                pass


def install_protocol_runtime_event_bridge(
    *,
    kernel_context: ProtocolRuntimeKernelContext,
    session_id: str,
    agent_name: str = "protocol_runtime_kernel_bridge",
) -> ProtocolRuntimeEventBridge:
    """Install the internal-event to compatibility-bus bridge.

    Packet 7D: The bridge will project SnapshotPersisted onto both the BSM
    backbone (via communication_protocol) and the WS event_sink (via the
    emit_event_fn stored in kernel_context).
    """

    bridge = ProtocolRuntimeEventBridge(
        kernel_context=kernel_context,
        session_id=session_id,
        agent_name=agent_name,
    )
    bridge.bind_event_bus(kernel_context.event_bus)
    return bridge
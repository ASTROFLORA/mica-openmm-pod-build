"""LoopEvent dataclasses for the MICA agentic core.

Every event emitted by :class:`AgenticLoop` is a frozen dataclass that is
trivially JSON-serialisable so it can be pushed over a WebSocket to the
frontend without extra marshalling.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Union


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopEvent:
    """Base class for all agentic loop events."""

    kind: str = field(init=False)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    run_id: str = ""  # S0.3: canonical run identifier — propagated by AgenticLoop
    program_id: str = ""  # S2: ThunderAgent program envelope identifier

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain ``dict`` suitable for ``json.dumps``.

        Includes ``"type"`` as an alias of ``"kind"`` for frontend compat.
        """
        d = asdict(self)
        d["type"] = d["kind"]
        return d


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamStart(LoopEvent):
    """Emitted when a new LLM inference step begins."""

    kind: str = field(init=False, default="stream_start")
    step: int = 0


@dataclass(frozen=True)
class TextDelta(LoopEvent):
    """Incremental text token from the model."""

    kind: str = field(init=False, default="text_delta")
    text: str = ""


@dataclass(frozen=True)
class ToolCallStart(LoopEvent):
    """Emitted when the model begins a tool call."""

    kind: str = field(init=False, default="tool_call_start")
    call_id: str = ""
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallEnd(LoopEvent):
    """Emitted when a tool call finishes executing."""

    kind: str = field(init=False, default="tool_call_end")
    call_id: str = ""
    name: str = ""
    result: str = ""
    duration_ms: int = 0
    was_truncated: bool = False


# ---------------------------------------------------------------------------
# Step / loop lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepFinish(LoopEvent):
    """Emitted at the end of each inference step (before tool execution)."""

    kind: str = field(init=False, default="step_finish")
    step: int = 0
    usage: Dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass(frozen=True)
class LoopFinish(LoopEvent):
    """Emitted when the agentic loop terminates normally."""

    kind: str = field(init=False, default="loop_finish")
    total_steps: int = 0
    total_cost_usd: float = 0.0
    finish_reason: str = "end_turn"
    remediation_hint: str = ""
    cumulative_tokens: int = 0


# ---------------------------------------------------------------------------
# Retry / error events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryWait(LoopEvent):
    """Emitted when a retryable error triggers back-off."""

    kind: str = field(init=False, default="retry_wait")
    attempt: int = 0
    delay_ms: int = 0
    error_message: str = ""


@dataclass(frozen=True)
class Error(LoopEvent):
    """Emitted on unrecoverable or informational errors."""

    kind: str = field(init=False, default="error")
    message: str = ""
    retryable: bool = False


# ---------------------------------------------------------------------------
# Context overflow
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextOverflow(LoopEvent):
    """Emitted when the context window is exhausted."""

    kind: str = field(init=False, default="context_overflow")
    prompt_tokens: int = 0
    limit_tokens: int = 0


@dataclass(frozen=True)
class ContextCompacted(LoopEvent):
    """Emitted when old messages are compacted to free context space."""

    kind: str = field(init=False, default="context_compacted")
    messages_before: int = 0
    messages_after: int = 0
    summary_chars: int = 0


@dataclass(frozen=True)
class ResourceInjected(LoopEvent):
    """Emitted when MCP resources are auto-injected into the query."""

    kind: str = field(init=False, default="resource_injected")
    count: int = 0
    total_chars: int = 0
    servers: str = ""


# ---------------------------------------------------------------------------
# Multi-agent dialogue events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentTurn(LoopEvent):
    """Emitted by a spawned sub-agent during multi-agent dialogue.

    Streams the sub-agent's thinking, tool use, and synthesis as it happens.
    The driver's LLM context only receives the final synthesis text; all
    intermediate AgentTurn events are for user visibility only.

    Roles:
        ``thinking`` – sub-loop step start (agent is processing).
        ``speaking`` – incremental text delta from the sub-agent.
        ``tool``     – the sub-agent made a tool call.
        ``done``     – sub-loop finished; text is the final synthesis.
    """

    kind: str = field(init=False, default="agent_turn")
    agent: str = ""       # 'bibliotecario' | 'biodynamo' | 'alchemist' | 'msrp_reviewer'
    role: str = ""        # 'thinking' | 'speaking' | 'tool' | 'done'
    text: str = ""
    session_id: str = ""  # ephemeral sub-loop ID


@dataclass(frozen=True)
class SideData(LoopEvent):
    """Emitted for artifacts that go to the UI side panel — NEVER into LLM context.

    Examples: 87 papers with scannable index, PDB files, DCD trajectories, PDFs.
    The UI (Alejandria) consumes these over the WS channel and renders them as
    side panels that the user can navigate independently of the conversation.
    """

    kind: str = field(init=False, default="side_data")
    channel: str = ""     # 'research' | 'structure' | 'trajectory' | 'pdf'
    agent: str = ""       # which agent produced it
    payload: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# R25.5 — Cross-community cognitive bus events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotPersisted(LoopEvent):
    """Emitted when an ATOM snapshot is committed to durable storage.

    Non-driver publisher: ``mica.memory.atom.persistence_timescale``.
    Non-driver subscribers: ``mica.agentic.cue_evaluator`` (governance side
    of the bicameral split). Driver is a permitted downstream subscriber
    but MUST NOT be the only subscriber — that would collapse the cognitive
    bus back to a driver-local channel.

    Fields mirror the R23.5 confidence pipeline contract so governance can
    react to distribution shape (mu, sigma, contributing quintuples) without
    rehydrating the snapshot.
    """

    kind: str = field(init=False, default="snapshot_persisted")
    snapshot_id: str = ""
    user_id: str = "default"
    mu: float = 0.0
    sigma: float = 0.0
    contributing_quintuples: int = 0
    entity_count: int = 0
    relation_count: int = 0
    empty_fallback: bool = False


@dataclass(frozen=True)
class MUDOReceiptReady(LoopEvent):
    """Typed runtime receipt ready for MUDO persistence.

    This event does not claim that provenance landed durably by itself. It only
    signals that a runtime surface produced enough receipt material for the
    dedicated MUDO subscriber to attempt a durable write.
    """

    kind: str = field(init=False, default="mudo_receipt_ready")
    receipt_kind: str = ""
    source_surface: str = ""
    correlation_id: str = ""
    protocol_ref: str = ""
    study_id: str = ""
    workspace_id: str = ""
    owner_user_id: str = ""
    input_refs: List[str] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    receipt_payload: Dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""


# ---------------------------------------------------------------------------
# Pipeline lifecycle events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PipelineCompleted(LoopEvent):
    """Emitted when a report pipeline (LRR, timeline, overview, SOTA) finishes.

    Downstream subscribers (e.g. :class:`KBAutoIngestListener`) use this to
    auto-create a report-derived KB so the user's next conversation can query
    the synthesised knowledge without re-running the pipeline.
    """

    kind: str = field(init=False, default="pipeline_completed")
    pipeline_kind: str = ""          # 'lrr' | 'timeline' | 'overview' | 'sota'
    report_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    entities: List[str] = field(default_factory=list)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    paper_refs: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Convenience union type
# ---------------------------------------------------------------------------

AnyLoopEvent = Union[
    StreamStart,
    TextDelta,
    ToolCallStart,
    ToolCallEnd,
    StepFinish,
    LoopFinish,
    RetryWait,
    Error,
    ContextOverflow,
    ContextCompacted,
    ResourceInjected,
    AgentTurn,
    SideData,
    SnapshotPersisted,
    MUDOReceiptReady,
    PipelineCompleted,
]

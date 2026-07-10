"""
GogChildExecutionBridge
=======================

Phases 2, 3, 5 — native child execution, artifact threading, EvidenceGate adapter.

This bridge is a ``ProtocolNodeDispatchAdapter`` factory.  It wraps a base adapter
(typically mock_dispatch or registry dispatch) and intercepts nodes that declare:

    inputs["_child_execution_mode"] == "native_child"

For such nodes it:

  1. Resolves ``node.child_graph_id`` from the ``ChildGraphRegistry``.
  2. Validates the child document (ChildGraphInvalid on failure).
  3. Derives the child execution frontier and builds a request.
  4. Executes the child graph via ``execute_protocol_executor_request``.
  5. Collects child run receipt + node receipts.
  6. Promotes child artifact_refs → parent ``state_after["artifact_refs"]``.
  7. Encodes child summary into ``state_after`` for recursive projection.
  8. Maps ``EvidenceGate``-blocked child nodes → parent blocked result.

For nodes with ``_child_execution_mode != "native_child"`` (or unset), the
request falls through to the wrapped base adapter unchanged.

**Compatibility guarantee**
   Nodes without ``child_graph_id`` or with ``_child_execution_mode=metadata_only``
   are never touched by this bridge.

Usage::

    from mica.drivers.execution.gog_child_execution_bridge import (
        GogChildExecutionBridge,
    )
    from mica.drivers.execution.child_graph_registry import ChildGraphRegistry

    registry = ChildGraphRegistry.from_dir(Path("tests/fixtures"))
    bridge = GogChildExecutionBridge(
        registry=registry,
        base_dispatch=my_adapter,          # optional: fallback for non-child nodes
        child_checkpoint_dir=tmp_path,     # optional: dir for child executor checkpoints
    )

    outcome = asyncio.run(execute_protocol_executor_request(
        request,
        dispatch_node=bridge.dispatch,
        ...
    ))
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from mica.drivers.execution.child_graph_registry import (
    ChildGraphExecFailed,
    ChildGraphInvalid,
    ChildGraphNotFound,
    ChildGraphRegistry,
)
from mica.drivers.execution.protocol_executor import (
    ProtocolExecutionOutcome,
    ProtocolNodeDispatchResult,
    execute_protocol_executor_request,
)
from mica.protocol_drafts import build_protocol_executor_request
from mica_q.protocol_jsonld_contract import (
    ProtocolNode,
    ProtocolNodeReceipt,
)
from mica_q.protocol_jsonld_validator import (
    derive_protocol_execution_frontier,
    validate_protocol_jsonld,
)


# ---------------------------------------------------------------------------
# Child execution mode constants
# ---------------------------------------------------------------------------

CHILD_EXEC_NATIVE = "native_child"
CHILD_EXEC_METADATA = "metadata_only"

_INPUT_KEY_EXEC_MODE = "_child_execution_mode"
_INPUT_KEY_MOCK_BLOCK = "_mock_block"


# ---------------------------------------------------------------------------
# Child execution summary (encoded into parent state_after)
# ---------------------------------------------------------------------------

@dataclass
class ChildExecutionSummary:
    """Summary of a child protocol execution — encoded into parent state_after."""

    child_graph_id: str
    child_run_id: str
    child_status: str                          # "completed" | "failed"
    child_completed_count: int
    child_blocked_count: int
    child_failed_count: int
    child_artifact_refs: List[str] = field(default_factory=list)
    child_evidence_refs: List[str] = field(default_factory=list)
    child_failure_message: Optional[str] = None
    evidencegate_verdict: Optional[Dict[str, Any]] = None
    evidencegate_source_node: Optional[str] = None

    def to_state_after_dict(self) -> Dict[str, Any]:
        """Encode summary as state_after dict for parent node receipt."""
        d: Dict[str, Any] = {
            "status": "completed" if self.child_status == "completed" else "blocked",
            "child_run_receipt_ref": f"run_receipt:{self.child_run_id}",
            "child_graph_id": self.child_graph_id,
            "child_status": self.child_status,
            "child_completed_count": self.child_completed_count,
            "child_blocked_count": self.child_blocked_count,
            "child_failed_count": self.child_failed_count,
            "native_child_execution": True,
            # Promoted artifacts — available to sibling template resolution
            "artifact_refs": list(self.child_artifact_refs),
            "evidence_refs": list(self.child_evidence_refs),
        }
        if self.child_failure_message:
            d["child_failure_message"] = self.child_failure_message
        if self.evidencegate_verdict is not None:
            d["blocked_by_cue"] = "evidence_gate_contradiction"
            d["evidencegate_verdict"] = self.evidencegate_verdict
            if self.evidencegate_source_node:
                d["evidencegate_source_node"] = self.evidencegate_source_node
        return d


# ---------------------------------------------------------------------------
# EvidenceGate verdict extraction
# ---------------------------------------------------------------------------

def _extract_evidencegate_verdict(
    node_receipts: List[ProtocolNodeReceipt],
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Scan child node receipts for EvidenceGate contradiction.

    Returns (verdict_dict, source_node_id) or (None, None) if not found.
    """
    for receipt in node_receipts:
        sa = dict(receipt.state_after or {})
        blocked_by = sa.get("blocked_by_cue") or sa.get("evidence_gate_verdict")
        if blocked_by or sa.get("status") == "blocked":
            verdict = sa.get("evidence_gate_verdict") or sa.get("evidencegate_verdict")
            if isinstance(verdict, dict):
                return verdict, receipt.node_id
            # Minimal synthesized verdict if no dict present
            if sa.get("blocked_by_cue"):
                return {
                    "passed": False,
                    "reason": str(sa.get("blocked_by_cue", "")),
                    "source_node": receipt.node_id,
                }, receipt.node_id
    return None, None


# ---------------------------------------------------------------------------
# Child graph execution
# ---------------------------------------------------------------------------

async def _execute_child_graph(
    *,
    node: ProtocolNode,
    registry: ChildGraphRegistry,
    child_checkpoint_dir: Optional[Union[str, Path]],
    base_dispatch: Optional[Any],
    child_registry: Optional["ChildGraphRegistry"],
) -> ChildExecutionSummary:
    """Execute child graph for *node* and return a summary.

    Raises:
        ChildGraphNotFound: child_graph_id not in registry
        ChildGraphInvalid: child document fails validation
        ChildGraphExecFailed: child executor raised an exception
    """
    child_graph_id = node.child_graph_id
    assert child_graph_id, f"Node {node.node_id} has no child_graph_id"

    # ── 1. Resolve child document ─────────────────────────────────────────
    try:
        child_doc_raw = registry.get(child_graph_id)
    except ChildGraphNotFound:
        raise  # propagate typed blocker

    # ── 2. Validate child document ────────────────────────────────────────
    try:
        child_doc = validate_protocol_jsonld(child_doc_raw)
    except Exception as exc:
        raise ChildGraphInvalid(child_graph_id, exc) from exc

    # ── 3. Build child executor request ───────────────────────────────────
    child_frontier = derive_protocol_execution_frontier(child_doc)
    child_request = build_protocol_executor_request(child_doc, child_frontier)

    # ── 4. Execute child graph ────────────────────────────────────────────
    ckpt = (
        str(child_checkpoint_dir)
        if child_checkpoint_dir
        else tempfile.mkdtemp(prefix=f"mica_child_{child_graph_id[:16]}_")
    )

    # Build a nested bridge if child graphs also need native execution
    dispatch_adapter = None
    if child_registry is not None and child_registry:
        nested_bridge = GogChildExecutionBridge(
            registry=child_registry,
            base_dispatch=base_dispatch,
            child_checkpoint_dir=ckpt,
        )
        dispatch_adapter = nested_bridge.dispatch
    elif base_dispatch is not None:
        dispatch_adapter = base_dispatch

    try:
        outcome: ProtocolExecutionOutcome = await execute_protocol_executor_request(
            child_request,
            checkpoint_dir=ckpt,
            dispatch_node=dispatch_adapter,
        )
    except Exception as exc:
        raise ChildGraphExecFailed(child_graph_id, "error", str(exc)) from exc

    # ── 5. Build summary ──────────────────────────────────────────────────
    child_rr = outcome.run_receipt
    child_nrs = outcome.node_receipts

    completed_count = len(child_rr.frontier_after.completed_node_ids)
    blocked_count = len(child_rr.frontier_after.blocked_node_ids)
    failed_count = sum(
        1 for nr in child_nrs if nr.event_type == "node.failed"
    )

    # ── 6. EvidenceGate verdict scan ─────────────────────────────────────
    verdict, verdict_node = _extract_evidencegate_verdict(child_nrs)

    return ChildExecutionSummary(
        child_graph_id=child_graph_id,
        child_run_id=child_rr.run_id,
        child_status=child_rr.status,
        child_completed_count=completed_count,
        child_blocked_count=blocked_count,
        child_failed_count=failed_count,
        child_artifact_refs=list(child_rr.artifact_refs),
        child_evidence_refs=list(child_rr.evidence_refs),
        child_failure_message=outcome.failure_message,
        evidencegate_verdict=verdict,
        evidencegate_source_node=verdict_node,
    )


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class GogChildExecutionBridge:
    """Dispatch adapter that intercepts GoG nodes and runs child graphs natively.

    Constructor args:
        registry: ChildGraphRegistry that maps child_graph_id → document.
        base_dispatch: Fallback adapter for nodes NOT in native_child mode.
        child_checkpoint_dir: Directory for child executor checkpoint files.
        child_registry: Optional nested registry for grandchild graphs.
    """

    def __init__(
        self,
        *,
        registry: ChildGraphRegistry,
        base_dispatch: Optional[Any] = None,
        child_checkpoint_dir: Optional[Union[str, Path]] = None,
        child_registry: Optional["ChildGraphRegistry"] = None,
    ) -> None:
        self._registry = registry
        self._base = base_dispatch
        self._ckpt = child_checkpoint_dir
        self._child_registry = child_registry
        self.dispatched_parent_inputs: Dict[str, Dict[str, Any]] = {}

    async def dispatch(self, node: ProtocolNode, **_kw: Any) -> ProtocolNodeDispatchResult:
        """Route node dispatch — native child execution or fallback."""
        inputs = dict(node.inputs or {})
        self.dispatched_parent_inputs[node.node_id] = inputs
        exec_mode = str(inputs.get(_INPUT_KEY_EXEC_MODE) or CHILD_EXEC_METADATA)

        # ── Not a native child execution ──────────────────────────────────
        if exec_mode != CHILD_EXEC_NATIVE or not node.child_graph_id:
            return await self._fallback(node)

        # ── Native child execution path ───────────────────────────────────
        try:
            summary = await _execute_child_graph(
                node=node,
                registry=self._registry,
                child_checkpoint_dir=self._ckpt,
                base_dispatch=self._base,
                child_registry=self._child_registry,
            )
        except ChildGraphNotFound as exc:
            return ProtocolNodeDispatchResult(
                summary=str(exc),
                status="blocked",
                event_type="node.blocked",
                state_after={
                    "status": "blocked",
                    "blocked_by_cue": "child_graph_not_found",
                    "child_graph_id": node.child_graph_id,
                    "error": str(exc),
                },
                failure_code="child_graph_not_found",
            )
        except ChildGraphInvalid as exc:
            return ProtocolNodeDispatchResult(
                summary=str(exc),
                status="blocked",
                event_type="node.blocked",
                state_after={
                    "status": "blocked",
                    "blocked_by_cue": "child_graph_invalid",
                    "child_graph_id": node.child_graph_id,
                    "error": str(exc.cause),
                },
                failure_code="child_graph_invalid",
            )
        except ChildGraphExecFailed as exc:
            return ProtocolNodeDispatchResult(
                summary=str(exc),
                status="blocked",
                event_type="node.blocked",
                state_after={
                    "status": "blocked",
                    "blocked_by_cue": "child_graph_execution_failed",
                    "child_graph_id": node.child_graph_id,
                    "child_status": exc.child_status,
                    "error": str(exc),
                },
                failure_code="child_graph_execution_failed",
            )

        # ── Build parent node dispatch result from summary ─────────────────
        sa = summary.to_state_after_dict()
        event_type = "node.completed" if summary.child_status == "completed" else "node.blocked"
        dispatch_status = "completed" if summary.child_status == "completed" else "blocked"

        # Failure detail
        if dispatch_status == "blocked":
            failure_code = (
                "evidence_gate_blocked"
                if summary.evidencegate_verdict is not None
                else "child_graph_execution_failed"
            )
        else:
            failure_code = None

        return ProtocolNodeDispatchResult(
            summary=(
                f"Child graph {node.child_graph_id!r} {summary.child_status}: "
                f"completed={summary.child_completed_count} "
                f"blocked={summary.child_blocked_count}"
                + (f" — {summary.child_failure_message}" if summary.child_failure_message else "")
            ),
            status=dispatch_status,
            event_type=event_type,
            state_after=sa,
            artifact_refs=list(summary.child_artifact_refs),
            evidence_refs=list(summary.child_evidence_refs),
            cost_snapshot={"usd": 0.0, "tool_calls": 1, "child_run": True},
            failure_code=failure_code,
        )

    async def _fallback(self, node: ProtocolNode) -> ProtocolNodeDispatchResult:
        """Delegate to base adapter or return metadata-only completion."""
        from inspect import isawaitable

        if self._base is not None:
            result = self._base(node)
            if isawaitable(result):
                result = await result
            if isinstance(result, ProtocolNodeDispatchResult):
                return result

        # No base adapter → metadata-only stub
        inputs = dict(node.inputs or {})
        if inputs.get(_INPUT_KEY_MOCK_BLOCK):
            return ProtocolNodeDispatchResult(
                summary=f"{node.node_id} BLOCKED by mock (metadata_only mode)",
                status="blocked",
                event_type="node.blocked",
                state_after={
                    "status": "blocked",
                    "blocked_by_cue": "mock_evidence_gate_contradiction",
                    "mock_dispatch": True,
                },
                failure_code="evidence_gate_blocked",
            )

        return ProtocolNodeDispatchResult(
            summary=f"{node.node_id} completed (metadata_only, no child execution)",
            status="completed",
            event_type="node.completed",
            state_after={
                "status": "completed",
                "child_graph_id": node.child_graph_id,
                "metadata_only": True,
            },
        )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def make_native_child_bridge(
    *,
    registry: ChildGraphRegistry,
    base_dispatch: Optional[Any] = None,
    child_checkpoint_dir: Optional[Union[str, Path]] = None,
) -> GogChildExecutionBridge:
    """Create a GogChildExecutionBridge — convenience wrapper."""
    return GogChildExecutionBridge(
        registry=registry,
        base_dispatch=base_dispatch,
        child_checkpoint_dir=child_checkpoint_dir,
    )


__all__ = [
    "GogChildExecutionBridge",
    "ChildExecutionSummary",
    "CHILD_EXEC_NATIVE",
    "CHILD_EXEC_METADATA",
    "make_native_child_bridge",
]

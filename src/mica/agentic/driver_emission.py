"""Slice-7 §9 — driver_thought / driver_decision emitters.

Thin wrappers over publish_cue that enforce:
- thought bodies are always scrubbed before emit
- decision bodies carry selected_tool + rationale_hash
- OTel span events are added on the active span

Never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from mica.observability.bootstrap import get_tracer
from mica.observability.redaction_patterns import scrub
from mica.observability.metrics_catalog import (
    mica_driver_decisions_total,
    mica_driver_thoughts_total,
    mica_driver_hypothesis_total,
)

_LOG = logging.getLogger("mica.driver.emission")


def _rationale_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


async def emit_driver_thought(
    agent_id: str,
    reasoning: str,
    *,
    turn: Optional[int] = None,
    session_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    scrubbed = scrub(reasoning)[:1024]
    try:
        tracer = get_tracer("mica.driver")
        from opentelemetry import trace
        span = trace.get_current_span()
        if span is not None:
            try:
                span.add_event("driver.thought", {"turn": turn or -1,
                                                  "hash": _rationale_hash(scrubbed)})
            except Exception:
                pass
    except Exception:
        pass
    try:
        mica_driver_thoughts_total.add(1, {"agent_id": agent_id})
    except Exception:
        pass
    try:
        from mica.agentic.tools.agent_feed import publish_cue
        return await publish_cue(
            agent_id=agent_id,
            post_type="driver_thought",
            topic="general",
            title=f"thought:{_rationale_hash(scrubbed)}",
            body=scrubbed,
            parent_id=parent_id,
            session_id=session_id,
            metadata={"turn": turn, "rationale_hash": _rationale_hash(scrubbed)},
        )
    except Exception as exc:
        _LOG.debug("driver_thought publish skipped: %s", exc)
        return None


async def emit_driver_decision(
    agent_id: str,
    selected_tool: str,
    rationale: str,
    *,
    rejected_options: Optional[List[str]] = None,
    verdict: str = "pending",
    hypothesis_id: Optional[str] = None,
    session_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    r_scrubbed = scrub(rationale)[:1024]
    r_hash = _rationale_hash(r_scrubbed)
    body = json.dumps({
        "selected_tool": selected_tool,
        "rationale_hash": r_hash,
        "rejected_options": list(rejected_options or []),
        "verdict": verdict,
        "hypothesis_id": hypothesis_id,
    })
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span is not None:
            span.add_event("driver.decision", {
                "selected_tool": selected_tool,
                "rationale_hash": r_hash,
                "verdict": verdict,
            })
    except Exception:
        pass
    try:
        mica_driver_decisions_total.add(1, {"tool": selected_tool, "verdict": verdict})
    except Exception:
        pass
    try:
        from mica.agentic.tools.agent_feed import publish_cue
        return await publish_cue(
            agent_id=agent_id,
            post_type="driver_decision",
            topic="general",
            title=f"decision:{selected_tool}",
            body=body,
            parent_id=parent_id,
            session_id=session_id,
            metadata={"selected_tool": selected_tool, "rationale_hash": r_hash,
                      "verdict": verdict, "hypothesis_id": hypothesis_id},
        )
    except Exception as exc:
        _LOG.debug("driver_decision publish skipped: %s", exc)
        return None


async def emit_hypothesis(
    agent_id: str,
    statement: str,
    *,
    session_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    try:
        mica_driver_hypothesis_total.add(1, {"agent_id": agent_id})
    except Exception:
        pass
    try:
        from mica.agentic.tools.agent_feed import publish_cue
        return await publish_cue(
            agent_id=agent_id,
            post_type="hypothesis",
            topic="general",
            title=scrub(statement)[:200],
            body=scrub(statement)[:4096],
            parent_id=parent_id,
            session_id=session_id,
            metadata=metadata or {},
        )
    except Exception as exc:
        _LOG.debug("hypothesis publish skipped: %s", exc)
        return None

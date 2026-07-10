"""R24 §5 / Phase 2.2 — Compaction of orphan tool messages.

Problem (quadratic attention tax):
    LLM chat histories that contain {"role": "tool", "tool_call_id": X} messages
    where NO preceding assistant turn has a matching tool_calls[*].id == X are
    "orphan tool messages". The attention mechanism is O(N^2) in N (history
    length), so keeping dead turns forces the model to pay attention cost on
    ruido that never resolves. Some providers also hard-error on orphans
    (e.g. OpenAI/Fireworks "tool message without tool_calls").

Contract:
    compact_orphan_tool_messages(messages) -> (clean_messages, report)

    A message is an ORPHAN if:
        - role == "tool", AND
        - its tool_call_id is not emitted by ANY preceding assistant message's
          tool_calls[*].id.

    We walk left-to-right, tracking the set of live tool_call_ids seen so far.
    Orphans are dropped. Non-tool messages are always kept.
"""
from __future__ import annotations
from typing import Any, Dict, Iterable, List, Tuple


def _extract_tool_call_ids(assistant_msg: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    tcs = assistant_msg.get("tool_calls")
    if not isinstance(tcs, list):
        return ids
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        tc_id = tc.get("id")
        if isinstance(tc_id, str) and tc_id:
            ids.append(tc_id)
    return ids


def compact_orphan_tool_messages(
    messages: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Drop tool messages whose tool_call_id was never emitted by an assistant turn.

    Returns (clean_messages, report).
    report keys: input_count, output_count, dropped_count, dropped_ids.
    """
    msgs = list(messages or [])
    live_ids: set[str] = set()
    kept: List[Dict[str, Any]] = []
    dropped_ids: List[str] = []

    for msg in msgs:
        if not isinstance(msg, dict):
            kept.append(msg)  # pass through unknown shapes
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc_id in _extract_tool_call_ids(msg):
                live_ids.add(tc_id)
            kept.append(msg)
            continue
        if role == "tool":
            tc_id = msg.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id in live_ids:
                kept.append(msg)
            else:
                dropped_ids.append(str(tc_id or "<missing>"))
            continue
        # user / system / any other role → always keep
        kept.append(msg)

    report = {
        "input_count": len(msgs),
        "output_count": len(kept),
        "dropped_count": len(dropped_ids),
        "dropped_ids": dropped_ids,
    }
    return kept, report


__all__ = ["compact_orphan_tool_messages"]

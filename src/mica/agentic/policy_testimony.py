"""Slice-6 §4 — Policy testimony chain.

Given a post_id (typically a closure/insight), walk backwards through the
parent_id chain and classify each hop by post_type. Produces a minimal
decision → hypothesis → experiment_id → insight trail usable as testimony
for a verdict emitted by the driver.

Exposed via:

- ``build_testimony(post_id) -> list[TestimonyHop]`` (in-process)
- ``GET /api/v1/testimony/{post_id}`` (public, rate-limited) added by the
  API router at slice-6 integration time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class TestimonyHop:
    post_id: str
    post_type: str
    topic: Optional[str]
    ts: Optional[str]
    actor: Optional[str]
    parent_id: Optional[str]
    summary_first_line: Optional[str]


def _iter_feed_posts() -> Iterable[dict]:
    # Import late so env overrides apply.
    from mica.agentic.feed_core import _feed_file  # type: ignore

    feed = _feed_file()
    if not feed.exists():
        return []
    out: list[dict] = []
    for line in feed.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _index_posts() -> dict[str, dict]:
    return {p.get("id"): p for p in _iter_feed_posts() if p.get("id")}


def _first_line(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    for line in str(s).splitlines():
        line = line.strip()
        if line:
            return line[:240]
    return None


def build_testimony(post_id: str, max_hops: int = 32) -> List[TestimonyHop]:
    """Walk parent_id chain from `post_id` until root or cycle/limit hit."""
    idx = _index_posts()
    hops: list[TestimonyHop] = []
    seen: set[str] = set()
    cur = post_id
    while cur and cur not in seen and len(hops) < max_hops:
        seen.add(cur)
        p = idx.get(cur)
        if not p:
            hops.append(TestimonyHop(
                post_id=cur, post_type="missing", topic=None, ts=None,
                actor=None, parent_id=None, summary_first_line=None,
            ))
            break
        hops.append(TestimonyHop(
            post_id=cur,
            post_type=p.get("post_type") or "unknown",
            topic=p.get("topic"),
            ts=p.get("ts") or p.get("created_at"),
            actor=p.get("actor") or p.get("agent_id"),
            parent_id=p.get("parent_id") or p.get("parent_post_id"),
            summary_first_line=_first_line(
                p.get("title")
                or p.get("body")
                or p.get("content")
                or p.get("summary")
            ),
        ))
        cur = p.get("parent_id") or p.get("parent_post_id")
    return hops


def testimony_as_dict(post_id: str, max_hops: int = 32) -> dict:
    hops = build_testimony(post_id, max_hops=max_hops)
    types = [h.post_type for h in hops]
    return {
        "root_post_id": post_id,
        "hop_count": len(hops),
        "type_chain": types,
        "has_insight": "insight" in types,
        "has_decision": "decision" in types,
        "has_hypothesis": "hypothesis" in types,
        "has_artifact": "artifact" in types,
        "hops": [asdict(h) for h in hops],
    }

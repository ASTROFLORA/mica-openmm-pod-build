"""instance_attribution.py

Utilities to attribute infrastructure usage (instances, runtime, cost) to:
- user_id
- session_id
- bucket

Data source: Timescale `events` table via `TimescaleEventStore.search_events()`.

Design goals:
- Schema tolerant (events may store event-specific data in `data` or legacy `payload`)
- Best-effort attribution (prefer dedicated columns, fallback to metadata)
- Deterministic and dependency-free (pure python aggregation)

Author: Team 2 (Infra)
Date: 2026-01-21
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Accept both Z and offset forms.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _pick_event_data(row: Dict[str, Any]) -> Dict[str, Any]:
    data = row.get("data")
    if isinstance(data, dict):
        return data
    payload = row.get("payload")
    if isinstance(payload, dict):
        return payload
    return {}


def _pick_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    md = row.get("metadata")
    return md if isinstance(md, dict) else {}


def _resolve_attribution(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    md = _pick_metadata(row)
    user_id = row.get("user_id") or md.get("user_id")
    session_id = row.get("session_id") or md.get("session_id")
    bucket = row.get("bucket") or md.get("bucket")
    return (
        str(user_id) if user_id else None,
        str(session_id) if session_id else None,
        str(bucket) if bucket else None,
    )


@dataclass
class InstanceUsage:
    instance_id: str
    provider: Optional[str]
    user_id: Optional[str]
    session_id: Optional[str]
    bucket: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    uptime_seconds: Optional[float]
    price_per_hour: Optional[float]
    estimated_cost_usd: Optional[float]


def summarize_instance_usage_from_events(
    events: Iterable[Dict[str, Any]],
    *,
    require_user_id: Optional[str] = None,
    require_session_id: Optional[str] = None,
) -> List[InstanceUsage]:
    """Aggregate instance runtime/cost from Timescale event rows.

    Expected event types (best-effort):
    - provisioning_succeeded: provides price_per_hour, gpu info
    - instance_terminated: provides uptime_seconds and estimated_cost_usd in metadata

    Filtering:
    - If require_user_id/session_id are provided, events are filtered by resolved
      attribution (columns OR metadata fallback).
    """

    per_instance: Dict[str, Dict[str, Any]] = {}

    for row in events:
        if not isinstance(row, dict):
            continue

        event_type = str(row.get("event_type") or "")
        instance_id = row.get("instance_id")
        if not instance_id:
            continue
        instance_id = str(instance_id)

        user_id, session_id, bucket = _resolve_attribution(row)
        if require_user_id is not None and user_id != require_user_id:
            continue
        if require_session_id is not None and session_id != require_session_id:
            continue

        ts = _parse_ts(row.get("timestamp"))
        provider = row.get("provider")
        provider_s = str(provider) if provider else None

        entry = per_instance.setdefault(
            instance_id,
            {
                "instance_id": instance_id,
                "provider": provider_s,
                "user_id": user_id,
                "session_id": session_id,
                "bucket": bucket,
                "started_at": None,
                "ended_at": None,
                "price_per_hour": None,
                "uptime_seconds": None,
                "estimated_cost_usd": None,
            },
        )

        # Keep latest attribution seen.
        entry["user_id"] = entry.get("user_id") or user_id
        entry["session_id"] = entry.get("session_id") or session_id
        entry["bucket"] = entry.get("bucket") or bucket
        entry["provider"] = entry.get("provider") or provider_s

        data = _pick_event_data(row)
        md = _pick_metadata(row)

        if event_type == "provisioning_succeeded":
            # Start time = earliest provisioning_succeeded.
            if ts is not None:
                cur = entry.get("started_at")
                if cur is None or (isinstance(cur, datetime) and ts < cur):
                    entry["started_at"] = ts
            pph = _safe_float(data.get("price_per_hour"))
            if pph is None:
                pph = _safe_float(md.get("price_per_hour"))
            entry["price_per_hour"] = entry.get("price_per_hour") or pph

        elif event_type == "instance_terminated":
            # End time = latest termination timestamp.
            if ts is not None:
                cur = entry.get("ended_at")
                if cur is None or (isinstance(cur, datetime) and ts > cur):
                    entry["ended_at"] = ts

            uptime = _safe_float(md.get("uptime_seconds"))
            if uptime is None:
                uptime = _safe_float(data.get("runtime_seconds"))
            entry["uptime_seconds"] = entry.get("uptime_seconds") or uptime

            cost = _safe_float(md.get("estimated_cost_usd"))
            if cost is None:
                cost = _safe_float(data.get("final_cost"))
            entry["estimated_cost_usd"] = entry.get("estimated_cost_usd") or cost

        elif event_type == "cost_incurred":
            # Optional: accumulate cost events.
            inc = _safe_float(data.get("cost_usd"))
            if inc is None:
                inc = _safe_float(md.get("cost_usd"))
            if inc is not None:
                prev = _safe_float(entry.get("estimated_cost_usd")) or 0.0
                entry["estimated_cost_usd"] = prev + inc

    out: List[InstanceUsage] = []
    for instance_id, e in per_instance.items():
        started_at = e.get("started_at")
        ended_at = e.get("ended_at")
        pph = _safe_float(e.get("price_per_hour"))
        uptime = _safe_float(e.get("uptime_seconds"))
        cost = _safe_float(e.get("estimated_cost_usd"))

        # Best-effort compute uptime/cost if missing.
        if uptime is None and isinstance(started_at, datetime) and isinstance(ended_at, datetime):
            uptime = max(0.0, (ended_at - started_at).total_seconds())

        if cost is None and uptime is not None and pph is not None:
            cost = (pph / 3600.0) * uptime

        out.append(
            InstanceUsage(
                instance_id=instance_id,
                provider=e.get("provider"),
                user_id=e.get("user_id"),
                session_id=e.get("session_id"),
                bucket=e.get("bucket"),
                started_at=started_at if isinstance(started_at, datetime) else None,
                ended_at=ended_at if isinstance(ended_at, datetime) else None,
                uptime_seconds=uptime,
                price_per_hour=pph,
                estimated_cost_usd=cost,
            )
        )

    # Stable ordering: earliest start first
    out.sort(key=lambda x: (x.started_at or datetime.min, x.instance_id))
    return out


def summarize_usage_totals(usages: Iterable[InstanceUsage]) -> Dict[str, float]:
    total_uptime = 0.0
    total_cost = 0.0
    count = 0

    for u in usages:
        count += 1
        if u.uptime_seconds is not None:
            total_uptime += float(u.uptime_seconds)
        if u.estimated_cost_usd is not None:
            total_cost += float(u.estimated_cost_usd)

    return {
        "total_uptime_seconds": total_uptime,
        "total_cost_usd": total_cost,
        "instance_count": float(count),
    }

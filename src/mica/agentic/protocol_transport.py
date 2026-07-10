from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


PROTOCOL_METADATA = "PROTOCOL_METADATA"
PROTOCOL_PHASE = "PROTOCOL_PHASE"
PROTOCOL_CUE = "PROTOCOL_CUE"
PROTOCOL_CUE_RESULT = "PROTOCOL_CUE_RESULT"
PROTOCOL_CUE_TELEMETRY = "PROTOCOL_CUE_TELEMETRY"
PLAN_PROGRESS = "PLAN_PROGRESS"
PROMOTION_GATE_RESULT = "PROMOTION_GATE_RESULT"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(payload: Any) -> str:
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    except Exception:
        encoded = str(payload).encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]}"


def transport_event(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": event_type, "payload": payload}


def telemetry_event(
    *,
    event_type: str,
    node_id: str,
    data: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "event_id": stable_hash({"event_type": event_type, "node_id": node_id, "data": data, "metadata": metadata or {}}),
        "event_type": event_type,
        "node_id": node_id,
        "timestamp": utcnow_iso(),
        "data": data,
        "metadata": metadata or {},
        "aggregate_version": 1,
    }

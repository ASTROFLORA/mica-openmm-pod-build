from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional


REMOTE_MD_EPHEMERAL_TIER = "tier_0_ephemeral"
REMOTE_MD_RECOVERABLE_TIER = "tier_2_recoverable"

_REDACTED_VALUE = "***REDACTED***"
_SENSITIVE_KEY_RE = re.compile(
    r"password|secret|token|api[_-]?key|credential|private[_-]?key|access[_-]?key|storage_env",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key or "")))


def redact_sensitive_payload(value: Any) -> Any:
    """Recursively redact secret-bearing keys from a nested payload."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if _is_sensitive_key(key):
                out[key] = _REDACTED_VALUE
            else:
                out[key] = redact_sensitive_payload(v)
        return out
    if isinstance(value, list):
        return [redact_sensitive_payload(item) for item in value]
    return value


def ensure_md_session_id(explicit: str = "") -> str:
    candidate = str(explicit or "").strip()
    return candidate or f"mdsess_{uuid.uuid4().hex[:12]}"


def classify_md_durability_tier(storage_backend: str = "none") -> str:
    backend = str(storage_backend or "none").strip().lower()
    if backend in {"rclone", "gcs"}:
        return REMOTE_MD_RECOVERABLE_TIER
    return REMOTE_MD_EPHEMERAL_TIER


def default_md_session_sidecar_path(session_id: str, output_json: str = "") -> str:
    if output_json:
        output_path = Path(output_json).expanduser().resolve()
        return str(output_path.parent / f"md_session_{session_id}.json")
    return str((Path.cwd() / f"md_session_{session_id}.json").resolve())


def build_md_session_payload(
    *,
    session_id: str,
    user_id: str,
    query: str,
    context: Dict[str, Any],
    status: str,
    started_at: str,
    output_json: str = "",
    result: Optional[Dict[str, Any]] = None,
    error: str = "",
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "entrypoint": "tools/mica_agent.py",
        "mode": "biodynamo_md_direct_session",
        "session_id": session_id,
        "user_id": user_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "status": status,
        "started_at": started_at,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "query": query,
        "context": context,
        "output_json": output_json,
        "result": result,
        "error": error,
    }


def write_md_session_sidecar(path: str, payload: Dict[str, Any]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = redact_sensitive_payload(payload)
    target.write_text(json.dumps(safe_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(target)

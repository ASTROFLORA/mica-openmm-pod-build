#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MICA Driver Utilities
======================

Extracted from ``agentic_driver.py`` (Phase 1 — Blueprint v3 §4.1).

Pure functions and lightweight helpers:
- Audit event emission (``_emit_audit_event``)
- Text truncation / secret redaction (``_truncate_text``, ``_redact_text``, …)
- Security-risk dict serialisation helpers
- Request-attribution context vars
- ``_DriverTransportShim`` (minimal ``TransportLayer`` stand-in)

All symbols are **re-exported** from ``agentic_driver.py`` (Rule 7).
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .agentic_driver import AgenticDriver  # forward ref for type hints

# Secret registry — best-effort import
try:
    from ..security.secrets import get_secret_registry
except Exception:  # pragma: no cover
    get_secret_registry = None  # type: ignore

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("mica.audit")


# ============================================================================
# AUDIT
# ============================================================================

def _emit_audit_event(event: str, **fields: Any) -> None:
    """Write a structured JSON line to the ``mica.audit`` logger."""
    try:
        payload = {
            "component": "agentic_driver",
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        audit_logger.info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        pass


# ============================================================================
# REQUEST ATTRIBUTION (context vars)
# ============================================================================

_current_user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mica_user_id", default=None
)
_current_bucket_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mica_bucket", default=None
)


# ============================================================================
# REDACTION / TRUNCATION
# ============================================================================

_SECRET_PATTERNS: List[re.Pattern] = [
    # key=value or key: value
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?key|secret|password|passwd|pwd|token|refresh[_-]?token)\b\s*[:=]\s*([^\s'\"`]{6,})"
    ),
    # Authorization: Bearer …
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*(bearer\s+[^\s'\"`]{10,})"),
    # Common token prefixes (best-effort)
    re.compile(r"\b(sk-[A-Za-z0-9]{16,})\b"),
    re.compile(r"\b(hf_[A-Za-z0-9]{16,})\b"),
]


def _truncate_text(text: str, max_len: int = 4000) -> str:
    """Return *text* shortened to *max_len* chars with an ellipsis marker."""
    if not isinstance(text, str):
        return ""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 12] + "…(truncated)"


def _redact_text(text: str) -> str:
    """Best-effort masking of secrets / tokens inside *text*."""
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    try:
        if get_secret_registry is not None:
            redacted = get_secret_registry().mask_text(redacted)
    except Exception:
        pass
    return redacted


def _security_risk_to_dict(risk: Any) -> Dict[str, Any]:
    """Serialise a ``SecurityRisk`` instance to a plain dict."""
    try:
        return {
            "level": getattr(getattr(risk, "level", None), "value", None) or str(getattr(risk, "level", "")),
            "category": getattr(risk, "category", None),
            "description": getattr(risk, "description", None),
            "evidence": _truncate_text(_redact_text(str(getattr(risk, "evidence", ""))), max_len=500),
            "mitigation": getattr(risk, "mitigation", None),
        }
    except Exception:
        return {"level": "unknown", "category": "unknown", "description": "unknown"}


def _risk_level_rank(level: Any) -> int:
    """Map a risk level to an integer rank (higher → worse)."""
    value = getattr(level, "value", None) or str(level or "")
    value = str(value).lower()
    return {
        "safe": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
        "critical": 4,
    }.get(value, 2)


def _max_risk(*risks: Any) -> Any:
    """Return the highest-severity risk among *risks*."""
    best = None
    best_rank = -1
    for r in risks:
        if r is None:
            continue
        rank = _risk_level_rank(getattr(r, "level", None))
        if rank > best_rank:
            best_rank = rank
            best = r
    return best


def _redact_obj(obj: Any) -> Any:
    """Recursively redact secret values in a nested structure."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return _redact_text(str(obj))


# ============================================================================
# TRANSPORT SHIM
# ============================================================================

class _DriverTransportShim:
    """Minimal ``TransportLayer``-compatible shim.

    Ensures callers that expect ``driver.transport.execute_worker(…)`` keep working
    even when the real TransportLayer cannot be imported / initialised.
    """

    def __init__(self, driver: "AgenticDriver") -> None:
        self._driver = driver

    async def execute_worker(self, worker: str, prompt: str, session_id: str = "default") -> Dict[str, Any]:
        return await self._driver._fallback_transport_execution(worker, prompt)

    async def execute(self, worker: str, prompt: str, session_id: str) -> Dict[str, Any]:
        return await self.execute_worker(worker, prompt, session_id)

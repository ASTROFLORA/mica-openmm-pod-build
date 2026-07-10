"""Anthropic Claude client wrapper with tool-use support."""
from __future__ import annotations
import json, logging, os, time
from typing import Any, Dict, List, Optional
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger(__name__)


def call_claude(
    prompt: str,
    *,
    system: str = "You are a concise scientific assistant.",
    max_tokens: int = 400,
    tools: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call Anthropic Messages API with optional tool-use.

    Args:
        prompt: User message content.
        system: System prompt.
        max_tokens: Maximum tokens.
        tools: List of tool definitions in Claude format
               (``[{"name": "…", "description": "…", "input_schema": {…}}]``).
        model: Override model (falls back to env CLAUDE_MODEL).

    Returns:
        Dict with keys:
            ok (bool), answer (str|None), provider, latency_s,
            tool_calls (list|None), stop_reason (str), model (str), usage (dict).
    """
    key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    resolved_model = model or os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20240620")
    if not key or requests is None:
        return {"ok": False, "error": "missing_credentials", "provider": "claude"}

    url = "https://api.anthropic.com/v1/messages"
    payload: Dict[str, Any] = {
        "model": resolved_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }

    if tools:
        payload["tools"] = tools

    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        t0 = time.time()
        r = requests.post(url, json=payload, headers=headers, timeout=120)
        lat = round(time.time() - t0, 3)

        if r.status_code >= 400:
            error_body = r.text[:500]
            logger.error("Claude HTTP %s: %s", r.status_code, error_body)
            return {"ok": False, "error": f"http_{r.status_code}", "detail": error_body, "provider": "claude", "latency_s": lat}

        data = r.json()
        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "unknown")

        # Extract text and tool_use blocks
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": block.get("input", {}),
                })

        result: Dict[str, Any] = {
            "ok": True,
            "answer": "\n".join(text_parts) if text_parts else None,
            "provider": "claude",
            "latency_s": lat,
            "stop_reason": stop_reason,
            "model": data.get("model", resolved_model),
            "usage": data.get("usage", {}),
        }

        if tool_calls:
            result["tool_calls"] = tool_calls
            logger.info("Claude returned %d tool_use blocks (stop_reason=%s)", len(tool_calls), stop_reason)

        return result

    except Exception as e:  # pragma: no cover
        logger.exception("Claude call failed")
        return {"ok": False, "error": str(e), "provider": "claude"}

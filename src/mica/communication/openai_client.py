"""OpenAI client wrapper (chat completions) with function-calling support."""
from __future__ import annotations
import json, logging, os, time
from typing import Any, Dict, List, Optional
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger(__name__)


def call_openai(
    prompt: str,
    *,
    system: str = "You are a concise scientific assistant.",
    max_tokens: int = 400,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: str = "auto",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call OpenAI Chat Completions API with optional function-calling (tools).

    Args:
        prompt: User message content.
        system: System prompt.
        max_tokens: Maximum tokens in response.
        tools: List of tool definitions in OpenAI format
               (``[{"type": "function", "function": {…}}]``).
               Pass ``None`` or ``[]`` to disable function calling.
        tool_choice: ``"auto"`` | ``"none"`` | ``"required"`` |
                     ``{"type": "function", "function": {"name": "…"}}``.
        model: Override model (falls back to env MICA_OPENAI_MODEL → OPENAI_MODEL → gpt-4o-mini).

    Returns:
        Dict with keys:
            ok (bool), answer (str|None), provider, latency_s,
            tool_calls (list|None) – present only when the model chose to call tools,
            finish_reason (str), model (str), usage (dict).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    resolved_model = model or os.getenv("MICA_OPENAI_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key or requests is None:
        return {"ok": False, "error": "missing_credentials", "provider": "openai"}

    url = f"{base.rstrip('/')}/chat/completions"
    payload: Dict[str, Any] = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # Newer OpenAI models (o-series, gpt-5+) only accept max_completion_tokens
        "max_completion_tokens": max_tokens,
    }

    # Attach tools only when a non-empty list is provided
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        t0 = time.time()
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        lat = round(time.time() - t0, 3)

        if r.status_code >= 400:
            error_body = r.text[:500]
            logger.error("OpenAI HTTP %s: %s", r.status_code, error_body)
            return {"ok": False, "error": f"http_{r.status_code}", "detail": error_body, "provider": "openai", "latency_s": lat}

        data = r.json()
        choices = data.get("choices", [])
        if not choices:
            return {"ok": False, "error": "empty_choices", "provider": "openai", "latency_s": lat}

        message = choices[0].get("message", {})
        finish_reason = choices[0].get("finish_reason", "unknown")
        text_content = message.get("content")

        result: Dict[str, Any] = {
            "ok": True,
            "answer": text_content,
            "provider": "openai",
            "latency_s": lat,
            "finish_reason": finish_reason,
            "model": data.get("model", resolved_model),
            "usage": data.get("usage", {}),
        }

        # ── Function-calling: extract tool_calls if present ──
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            parsed_calls = []
            for tc in raw_tool_calls:
                fn = tc.get("function", {})
                args_raw = fn.get("arguments", "{}")
                try:
                    args_parsed = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args_parsed = {"_raw": args_raw}
                parsed_calls.append({
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": args_parsed,
                })
            result["tool_calls"] = parsed_calls
            logger.info("OpenAI returned %d tool_calls (finish_reason=%s)", len(parsed_calls), finish_reason)

        return result

    except Exception as e:  # pragma: no cover
        logger.exception("OpenAI call failed")
        return {"ok": False, "error": str(e), "provider": "openai"}

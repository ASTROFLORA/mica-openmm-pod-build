"""Llama 3.3 Instruct client (generic OpenAI-compatible pattern).

Env vars:
  LLAMA_API_URL     (ej. https://api.llama.meta/v1/chat/completions o endpoint proxy)
  LLAMA_API_KEY     (Bearer token)
  LLAMA_MODEL       (default: llama-3.3-70b-instruct)

Returns dict {ok, provider='llama', answer|error, latency_s}.
Offline fallback si faltan credenciales.
"""
from __future__ import annotations
import os, time
from typing import Dict, Any

try:  # optional dep
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def call_llama(prompt: str, *, system: str = "You are a helpful scientific assistant.") -> Dict[str, Any]:
    url = os.getenv("LLAMA_API_URL")
    key = os.getenv("LLAMA_API_KEY")
    model = os.getenv("LLAMA_MODEL", "llama-3.3-70b-instruct")
    if not url or not key or requests is None:
        t0 = time.time()
        return {"ok": True, "answer": f"[offline-llama:{model}] {prompt[:160]}", "provider": "llama", "offline": True, "latency_s": round(time.time()-t0,3)}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 400
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        t0 = time.time()
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        lat = round(time.time() - t0, 3)
        if r.status_code >= 400:
            return {"ok": False, "error": f"http_{r.status_code}", "provider": "llama", "latency_s": lat}
        data = r.json(); ans=None
        if isinstance(data, dict):
            ch = data.get("choices")
            if isinstance(ch, list) and ch:
                ans = ch[0].get("message", {}).get("content")
        return {"ok": True, "answer": ans, "provider": "llama", "latency_s": lat}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "provider": "llama"}

__all__ = ["call_llama"]

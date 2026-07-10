"""Lightweight Nemotron (NVIDIA) chat client wrapper.

Environment variables expected:
  NEMOTRON_API_URL   e.g. https://integrate.api.nvidia.com/v1/chat/completions
  NEMOTRON_API_KEY   Bearer token
  NEMOTRON_MODEL     Model name (default: nemotron-4-340b-instruct)

Function returns unified dict with keys: ok, answer, status_code, latency_s, raw.
"""
from __future__ import annotations
from typing import Any, Dict
import os, time

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def call_nemotron(prompt: str, *, temperature: float = 0.2, max_tokens: int = 200) -> Dict[str, Any]:
    url = os.getenv("NEMOTRON_API_URL")
    key = os.getenv("NEMOTRON_API_KEY")
    model = os.getenv("NEMOTRON_MODEL", "nemotron-4-340b-instruct")
    if not url or not key:
        return {"ok": False, "error": "missing_credentials", "answer": None}
    if requests is None:
        return {"ok": False, "error": "requests_not_available", "answer": None}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a concise scientific assistant."},
            {"role": "user", "content": prompt}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    t0 = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        lat = time.time() - t0
        txt = None
        if resp.headers.get("content-type", "").lower().startswith("application/json"):
            try:
                data = resp.json()
            except Exception:
                data = {}
            if isinstance(data, dict):
                ch = data.get("choices")
                if isinstance(ch, list) and ch:
                    txt = ch[0].get("message", {}).get("content")
        else:
            txt = resp.text[:4000]
        return {
            "ok": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "answer": txt,
            "latency_s": round(lat, 3),
            "raw": None if txt else resp.text[:1000]
        }
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "answer": None}

__all__ = ["call_nemotron"]

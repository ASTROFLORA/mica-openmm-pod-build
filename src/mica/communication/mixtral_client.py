"""Mixtral (mistralai/mixtral-8x7b-instruct-v0.1) client via NVIDIA integrate (OpenAI-compatible).

Env:
  MIXTRAL_API_KEY (o reutiliza nvapi)
  MIXTRAL_API_URL (default integrate chat completions)
  MIXTRAL_MODEL (default mistralai/mixtral-8x7b-instruct-v0.1)
"""
from __future__ import annotations
import os, time
from typing import Dict, Any
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def call_mixtral(prompt: str, *, system: str = "You are a concise scientific assistant.") -> Dict[str, Any]:
    url = os.getenv("MIXTRAL_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
    key = os.getenv("MIXTRAL_API_KEY") or os.getenv("LLAMA_API_KEY") or os.getenv("NEMOTRON_API_KEY")
    model = os.getenv("MIXTRAL_MODEL", "mistralai/mixtral-8x7b-instruct-v0.1")
    if not key or requests is None:
        return {"ok": False, "error": "missing_credentials", "provider": "mixtral"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": float(os.getenv("MIXTRAL_TEMPERATURE", "0.5")),
        "max_tokens": int(os.getenv("MIXTRAL_MAX_TOKENS", "512"))
    }
    try:
        t0=time.time(); r=requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=60); lat=round(time.time()-t0,3)
        if r.status_code>=400:
            return {"ok": False, "error": f"http_{r.status_code}", "provider": "mixtral", "latency_s": lat}
        data=r.json(); ans=None
        if isinstance(data, dict):
            ch=data.get('choices');
            if isinstance(ch,list) and ch:
                ans = ch[0].get('message',{}).get('content')
        return {"ok": True, "answer": ans, "provider": "mixtral", "latency_s": lat, "model": model}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "provider": "mixtral"}

__all__ = ["call_mixtral"]

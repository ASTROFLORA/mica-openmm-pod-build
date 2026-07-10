"""Phi-4 Multimodal Instruct client (OpenAI-compatible chat endpoint).

Env:
  PHI_API_KEY
  PHI_API_URL (default integrate chat completions)
  PHI_MODEL (default phi-4-multimodal-instruct)
Supports text-only in this minimal wrapper.
"""
from __future__ import annotations
import os, time
from typing import Dict, Any
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def call_phi(prompt: str, *, system: str = "You are a helpful multimodal assistant (text-only path).") -> Dict[str, Any]:
    url = os.getenv("PHI_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
    key = os.getenv("PHI_API_KEY") or os.getenv("NEMOTRON_API_KEY")
    primary_model = os.getenv("PHI_MODEL", "phi-4-multimodal-instruct")
    if not key or requests is None:
        return {"ok": False, "error": "missing_credentials", "provider": "phi4"}
    candidates = [primary_model, "phi-4-multimodal", "phi-4", "phi-4-instruct"]
    temperature = float(os.getenv("PHI_TEMPERATURE", "0.5"))
    max_tokens = int(os.getenv("PHI_MAX_TOKENS", "512"))
    last_err = None
    for model in candidates:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        try:
            t0=time.time(); r=requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=60); lat=round(time.time()-t0,3)
            if r.status_code == 404:
                last_err = f"http_404_{model}"; continue  # prueba siguiente alias
            if r.status_code>=400:
                return {"ok": False, "error": f"http_{r.status_code}", "provider": "phi4", "latency_s": lat, "model": model}
            data=r.json(); ans=None
            if isinstance(data, dict):
                ch=data.get('choices');
                if isinstance(ch,list) and ch:
                    ans = ch[0].get('message',{}).get('content')
            return {"ok": True, "answer": ans, "provider": "phi4", "latency_s": lat, "model": model}
        except Exception as e:  # pragma: no cover
            last_err = str(e)
            break
    return {"ok": False, "error": last_err or "model_not_found", "provider": "phi4"}

__all__ = ["call_phi"]

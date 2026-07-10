"""DeepSeek vía NVIDIA Integrate API.

El usuario indicó que DeepSeek se invoca usando el cliente estilo OpenAI con:
  base_url = https://integrate.api.nvidia.com/v1
  model    = deepseek-ai/deepseek-r1

Aquí hacemos una llamada REST directa (evitamos dependencia estricta del SDK OpenAI).
Variables de entorno soportadas:
  DEEPSEEK_API_KEY (requerida)
  DEEPSEEK_MODEL (default: deepseek-ai/deepseek-r1)
  DEEPSEEK_API_URL (default: https://integrate.api.nvidia.com/v1/chat/completions)
  DEEPSEEK_TEMPERATURE (float, default 0.6)
  DEEPSEEK_TOP_P (float, default 0.7)
  DEEPSEEK_MAX_TOKENS (int, default 4096)
"""
from __future__ import annotations
import os, time
from typing import Dict, Any
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def call_deepseek(prompt: str, *, system: str = "You are a concise scientific assistant.") -> Dict[str, Any]:
    url = os.getenv("DEEPSEEK_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("NEMOTRON_API_KEY")  # fallback por si comparte key NVIDIA
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-ai/deepseek-r1")
    if not key or requests is None:
        return {"ok": False, "error": "missing_credentials", "provider": "deepseek"}
    temperature = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.6"))
    top_p = float(os.getenv("DEEPSEEK_TOP_P", "0.7"))
    max_tokens = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        t0 = time.time()
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=80,
        )
        lat = round(time.time() - t0, 3)
        if r.status_code >= 400:
            return {"ok": False, "error": f"http_{r.status_code}", "provider": "deepseek", "latency_s": lat}
        data = r.json(); txt = None
        if isinstance(data, dict):
            ch = data.get("choices")
            if isinstance(ch, list) and ch:
                txt = ch[0].get("message", {}).get("content")
        return {"ok": True, "answer": txt, "provider": "deepseek", "latency_s": lat, "model": model}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "provider": "deepseek"}

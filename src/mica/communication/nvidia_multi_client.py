"""Unified NVIDIA services (Nemotron + future BioNeMo) client."""
from __future__ import annotations
import os, time
from typing import Dict, Any
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

def call_nemotron(prompt: str, *, system: str = "You are a concise scientific assistant.") -> Dict[str, Any]:
    url = os.getenv("NEMOTRON_API_URL"); key = os.getenv("NEMOTRON_API_KEY"); model = os.getenv("NEMOTRON_MODEL", "nemotron-4-340b-instruct")
    if not url or not key or requests is None:
        return {"ok": False, "error": "missing_credentials", "provider": "nemotron"}
    payload = {"model": model, "messages": [{"role":"system","content":system},{"role":"user","content":prompt}], "temperature":0.2, "max_tokens":400}
    try:
        t0=time.time(); r=requests.post(url, headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=payload, timeout=60); lat=round(time.time()-t0,3)
        if r.status_code>=400:
            return {"ok": False, "error": f"http_{r.status_code}", "provider": "nemotron", "latency_s": lat}
        data = r.json(); txt=None
        if isinstance(data, dict):
            ch=data.get('choices');
            if isinstance(ch,list) and ch:
                txt=ch[0].get('message',{}).get('content')
        return {"ok": True, "answer": txt, "provider": "nemotron", "latency_s": lat}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "provider": "nemotron"}

def detect_services() -> Dict[str, Any]:
    services = {}
    if os.getenv("NEMOTRON_API_KEY") and os.getenv("NEMOTRON_API_URL"):
        services['nemotron'] = True
    if os.getenv("BIONEMO_PROTEIN_API_URL") and (os.getenv("BIONEMO_API_KEY") or os.getenv("NEMOTRON_API_KEY")):
        services['bionemo_protein'] = True
    return services

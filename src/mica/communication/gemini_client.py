"""Google Gemini API wrapper (google-generativeai)."""
from __future__ import annotations
import os, time
from typing import Dict, Any
try:
    import google.generativeai as genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None  # type: ignore

def call_gemini(prompt: str, *, model: str | None = None, system: str | None = None) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GENAI_API_KEY")
    if genai is None or not api_key:
        return {"ok": False, "error": "missing_credentials", "provider": "gemini"}
    genai.configure(api_key=api_key)
    model_name = model or os.getenv("GEMINI_MODEL", "gemini-1.5-pro-latest")
    try:
        t0 = time.time(); m = genai.GenerativeModel(model_name)
        parts = []
        if system: parts.append(system)
        parts.append(prompt)
        resp = m.generate_content("\n".join(parts)); lat = round(time.time()-t0,3)
        txt = getattr(resp, 'text', None)
        if not txt and getattr(resp, 'candidates', None):
            try:
                txt = resp.candidates[0].content.parts[0].text  # type: ignore
            except Exception:
                pass
        return {"ok": True, "answer": txt, "provider": "gemini", "latency_s": lat}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "provider": "gemini"}

"""Real Swarm communication client (Gold Protocol adapter).

Responsible for:
  - Managing payload variants (flat, flat+mode, nested, prompt)
  - Extracting a normalized answer field
  - Heuristically scoring if response resembles REAL SWARM
  - Returning structured diagnostics for debugging

Empirical findings (2025-08-09): flat root-level 'scientific_question'
payload succeeds; nested 'input.scientific_question' currently rejected
with {"detail": "scientific_question requerida"} though kept for
forward compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import os, time, json

try:  # optional dependency
    import requests  # type: ignore
except Exception:  # pragma: no cover - fallback environment
    requests = None  # type: ignore

REAL_HEURISTICS = [
    "INFORME EJECUTIVO: SWARM ASTROFLORA",
    "Workers Online:",
    "CPU Bootstrap:",
    "GPU Utilization:",
    "El Swarm AstroFlora sigue avanzando",
]


@dataclass
class SwarmInvocationResult:
    ok: bool
    status_code: int
    answer: Optional[str]
    raw: Any
    payload_used: Dict[str, Any]
    variant: str
    real_swarm_likelihood: float
    diagnostics: Dict[str, Any]

    def is_real(self) -> bool:
        return self.real_swarm_likelihood >= 0.5 and self.ok


class RealSwarmClient:
    def __init__(self, base_url: Optional[str] = None, *, timeout: int = 60, invoke_path: str = "/api/v1/invoke", session: Optional[Any] = None) -> None:
        self.base_url = (base_url or os.getenv("SWARM_API_BASE", "http://3.85.5.222:8001")).rstrip("/")
        self.timeout = timeout
        self.invoke_url = f"{self.base_url}{invoke_path}"
        self.session = session or (requests.Session() if requests else None)
        # circuit breaker state
        self._fail_count = 0
        self._opened_at = None  # type: Optional[float]
        self._open_threshold = 5  # consecutive failures
        self._reset_after = 60.0  # seconds

    # --- internal helpers ---
    def _post_json(self, payload: Dict[str, Any]) -> tuple[int, Any, Dict[str, str]]:
        if not self.session:  # urllib fallback
            import urllib.request, urllib.error  # type: ignore
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(self.invoke_url, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec - controlled
                    body = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                    parsed = json.loads(body.decode("utf-8")) if "json" in ctype.lower() else body.decode("utf-8")
                    return resp.status, parsed, {"content-type": ctype}
            except urllib.error.HTTPError as e:  # pragma: no cover
                try:
                    b = e.read().decode("utf-8")
                    parsed = json.loads(b)
                except Exception:
                    parsed = b
                return e.code, parsed, {"error": "http"}
            except Exception as e:  # pragma: no cover
                return 599, {"error": str(e)}, {"error": "exception"}
        try:  # requests path
            resp = self.session.post(self.invoke_url, json=payload, timeout=self.timeout)  # type: ignore
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype.lower():
                return resp.status_code, resp.json(), {"content-type": ctype}
            return resp.status_code, resp.text, {"content-type": ctype}
        except Exception as e:  # pragma: no cover
            return 599, {"error": str(e)}, {"error": "exception"}

    def _extract_answer(self, data: Any) -> Optional[str]:
        if isinstance(data, dict):
            for k in ("scientific_response", "answer", "response", "result", "output"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        if isinstance(data, str) and data.strip():
            return data
        return None

    def _score_real(self, text: Optional[str]) -> float:
        if not text:
            return 0.0
        hits = sum(1 for h in REAL_HEURISTICS if h in text)
        return 0.0 if hits == 0 else min(1.0, hits / len(REAL_HEURISTICS) + 0.2)

    # --- public API ---
    def ask(self, question: str, *, agent: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> SwarmInvocationResult:
        ctx = context or {}
        variants: List[tuple[str, Dict[str, Any]]] = [
            ("flat", {"scientific_question": question, "context": ctx, "agent": agent}),
            ("flat+mode", {"scientific_question": question, "mode": "HIGH_PRIORITY", "context": ctx, "agent": agent}),
            ("nested", {"input": {"scientific_question": question, "context": ctx}, "agent": agent, "stream": False}),
            ("prompt", {"prompt": question, "agent": agent}),
        ]
        attempts: List[Dict[str, Any]] = []
        import time as _time

        # circuit breaker pre-check
        now = _time.time()
        if self._opened_at and (now - self._opened_at) < self._reset_after:
            return SwarmInvocationResult(
                ok=False,
                status_code=0,
                answer=None,
                raw={"error": "circuit_open", "retry_after_s": self._reset_after - (now - self._opened_at)},
                payload_used={},
                variant="",
                real_swarm_likelihood=0.0,
                diagnostics={"attempts": []},
            )
        if self._opened_at and (now - self._opened_at) >= self._reset_after:
            # half-open
            self._fail_count = 0
            self._opened_at = None
        for name, payload in variants:
            start = time.time()
            status, raw, hdrs = self._post_json(payload)
            elapsed = time.time() - start
            answer = self._extract_answer(raw)
            attempts.append({
                "variant": name,
                "status": status,
                "elapsed_s": round(elapsed, 3),
                "answer_preview": (answer[:120] + "…") if answer and len(answer) > 123 else answer,
            })
            if 200 <= status < 300:
                # success resets breaker
                self._fail_count = 0
                self._opened_at = None
                score = self._score_real(answer)
                return SwarmInvocationResult(
                    ok=True,
                    status_code=status,
                    answer=answer,
                    raw=raw,
                    payload_used=payload,
                    variant=name,
                    real_swarm_likelihood=score,
                    diagnostics={"attempts": attempts, "headers": hdrs},
                )
            if isinstance(raw, dict) and raw.get("detail") == "scientific_question requerida" and name != "flat":
                continue
            # failure path
            self._fail_count += 1
            if self._fail_count >= self._open_threshold:
                self._opened_at = _time.time()
                break
        # Retry once after short backoff if breaker not open
        if self._fail_count and (self._opened_at is None):
            _time.sleep(1.0)
            return self.ask(question, agent=agent, context=ctx) if self._fail_count < self._open_threshold else SwarmInvocationResult(
                ok=False,
                status_code=attempts[-1]["status"] if attempts else 0,
                answer=None,
                raw={"error": "All payload variants failed (breaker opened)", "attempts": attempts},
                payload_used={},
                variant="",
                real_swarm_likelihood=0.0,
                diagnostics={"attempts": attempts},
            )
        return SwarmInvocationResult(
            ok=False,
            status_code=attempts[-1]["status"] if attempts else 0,
            answer=None,
            raw={"error": "All payload variants failed", "attempts": attempts},
            payload_used={},
            variant="",
            real_swarm_likelihood=0.0,
            diagnostics={"attempts": attempts},
        )


__all__ = ["RealSwarmClient", "SwarmInvocationResult"]

"""health_probes.py — Live DB reachability probes for `/health`.

Slice-4 §1. Complements the router-import state check with real `SELECT 1`
against Neon and Timescale using existing DSN resolution helpers. Each probe
is fully isolated: timeouts, exceptions, and missing DSNs degrade to a
structured result; the handler never raises.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 1.5
_SLOW_THRESHOLD_S = 3.0


@dataclass(frozen=True)
class Probe:
    ok: bool
    latency_ms: int
    error: Optional[str]
    kind: str  # "neon" | "timescale"
    configured: bool

    def to_public(self) -> Dict[str, Any]:
        # Public shape — never leak DSN pieces, only ok/latency/kind/status.
        return {
            "kind": self.kind,
            "configured": self.configured,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "status": _status_of(self),
        }


def _status_of(p: Probe) -> str:
    if not p.configured:
        return "not_configured"
    if p.ok and p.latency_ms <= int(_SLOW_THRESHOLD_S * 1000):
        return "ok"
    if p.ok:
        return "slow"
    return "error"


async def _probe_dsn(kind: str, dsn: Optional[str], timeout: float) -> Probe:
    if not dsn:
        return Probe(ok=False, latency_ms=0, error="no_dsn",
                     kind=kind, configured=False)
    try:
        from mica.infrastructure.persistence.pg_async import (
            connect_asyncpg_for_database_url,
        )
    except Exception as exc:  # noqa: BLE001
        return Probe(ok=False, latency_ms=0, error=f"import:{exc!s}",
                     kind=kind, configured=True)

    start = time.perf_counter()
    try:
        async def _go() -> None:
            conn = await connect_asyncpg_for_database_url(dsn)
            try:
                await conn.execute("SELECT 1")
            finally:
                await conn.close()
        await asyncio.wait_for(_go(), timeout=timeout)
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - start) * 1000)
        return Probe(ok=False, latency_ms=elapsed,
                     error=f"timeout>{timeout}s", kind=kind, configured=True)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - start) * 1000)
        # Redact DSN-like fragments from error text just in case.
        msg = type(exc).__name__
        detail = str(exc)[:120].replace(dsn, "«dsn»") if dsn else str(exc)[:120]
        return Probe(ok=False, latency_ms=elapsed,
                     error=f"{msg}:{detail}", kind=kind, configured=True)

    elapsed = int((time.perf_counter() - start) * 1000)
    return Probe(ok=True, latency_ms=elapsed, error=None,
                 kind=kind, configured=True)


async def probe_neon(timeout: float = _DEFAULT_TIMEOUT_S) -> Probe:
    """Probe the Neon DSN if configured."""
    try:
        from mica.infrastructure.persistence.pg_async import (
            choose_neon_database_url,
        )
        dsn = choose_neon_database_url()
    except Exception as exc:  # noqa: BLE001
        return Probe(ok=False, latency_ms=0, error=f"resolve:{exc!s}",
                     kind="neon", configured=False)
    return await _probe_dsn("neon", dsn, timeout)


async def probe_timescale(timeout: float = _DEFAULT_TIMEOUT_S) -> Probe:
    """Probe the TimescaleDB DSN if configured."""
    try:
        from mica.infrastructure.persistence.pg_async import (
            choose_timescale_database_url,
        )
        dsn = choose_timescale_database_url()
    except Exception as exc:  # noqa: BLE001
        return Probe(ok=False, latency_ms=0, error=f"resolve:{exc!s}",
                     kind="timescale", configured=False)
    return await _probe_dsn("timescale", dsn, timeout)


async def probe_all(timeout: float = _DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Fan out both probes concurrently; never raise."""
    try:
        neon, ts = await asyncio.gather(
            probe_neon(timeout), probe_timescale(timeout),
            return_exceptions=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("health_probes.probe_all failed: %s", exc)
        return {
            "neon": Probe(False, 0, f"unexpected:{exc!s}", "neon", False).to_public(),
            "timescale": Probe(False, 0, f"unexpected:{exc!s}", "timescale", False).to_public(),
        }
    return {"neon": neon.to_public(), "timescale": ts.to_public()}


def summarize(probes: Dict[str, Any]) -> str:
    """Return overall contribution for the /health status field.

    Returns ``"degraded"`` if any configured DB probe is error or slow,
    else ``"ok"``. Missing DSNs are treated as ``"ok"`` (local/dev mode).
    """
    for p in probes.values():
        if p.get("configured") and p.get("status") in {"error", "slow"}:
            return "degraded"
    return "ok"

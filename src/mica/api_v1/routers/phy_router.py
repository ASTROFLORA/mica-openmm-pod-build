# filepath: src/mica/api_v1/routers/phy_router.py
"""phy_router.py — REST surface para Lane PhY.

Endpoints:
- POST /api/v1/phy/cmd         → dispatch a PhY (vía Dispatcher + transport)
- GET  /api/v1/phy/state       → estado del ejecutor (mock hoy, real mañana)
- GET  /api/v1/phy/telemetry/{device_id} → últimas N filas de phy.telemetry
- GET  /api/v1/phy/health      → health check del lane

Nota: el ingester MQTT corre como thread daemon, no expone endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from mica.phy.dispatcher import Dispatcher
from mica.phy.config import load_config

log = logging.getLogger("mica.phy.api")

router = APIRouter(prefix="/api/v1/phy", tags=["phy"])


# ── Request/Response schemas ───────────────────────────────────────────────


class PhyCommandRequest(BaseModel):
    kind: str = Field(..., description="home|move|dispense|capture|stop|set_led")
    target: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    requested_by: str = "operator"
    correlation_id: Optional[str] = None
    action_id: Optional[str] = None


class PhyCommandResponse(BaseModel):
    action_id: str
    status: str
    message: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    receipt_urn: Optional[str] = None


class TelemetryRow(BaseModel):
    time: datetime
    device_id: str
    metric: str
    value: float


# ── State holder (singleton lazy; sobrevive entre requests) ─────────────────


_dispatcher: Optional[Dispatcher] = None


def _get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
    return _dispatcher


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/cmd", response_model=PhyCommandResponse)
def post_cmd(req: PhyCommandRequest) -> PhyCommandResponse:
    """Dispatch a PhY action. Determinista, auditado, sin coords del LLM."""
    result = _get_dispatcher().dispatch(
        kind=req.kind,
        target=req.target,
        payload=req.payload,
        requested_by=req.requested_by,
        correlation_id=req.correlation_id,
        action_id=req.action_id,
    )
    return PhyCommandResponse(
        action_id=result.action_id,
        status=result.status,
        message=result.message,
        data=result.data,
        receipt_urn=result.receipt_urn,
    )


@router.get("/state")
def get_state() -> Dict[str, Any]:
    """Estado del ejecutor físico (mock hoy)."""
    return _get_dispatcher().executor.state()


@router.get("/telemetry/{device_id}", response_model=List[TelemetryRow])
def get_telemetry(
    device_id: str,
    limit: int = Query(default=50, ge=1, le=500),
) -> List[TelemetryRow]:
    """Lee últimas N filas de phy.telemetry desde Neon."""
    cfg = load_config()
    if not cfg.db_url:
        raise HTTPException(status_code=503, detail="PHY_DB_URL no configurada")
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"psycopg2 no instalado: {e}") from e

    try:
        with psycopg2.connect(cfg.db_url, connect_timeout=5) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT time, device_id, metric, value FROM {cfg.db_schema}.telemetry "
                f"WHERE device_id = %s ORDER BY time DESC LIMIT %s",
                (device_id, limit),
            )
            rows = cur.fetchall()
    except Exception as e:
        log.exception("Error leyendo telemetry")
        raise HTTPException(status_code=500, detail=f"db error: {e}") from e

    return [
        TelemetryRow(
            time=r["time"] or datetime.now(timezone.utc),
            device_id=r["device_id"],
            metric=r["metric"],
            value=float(r["value"]),
        )
        for r in rows
    ]


@router.get("/health")
def health() -> Dict[str, Any]:
    cfg = load_config()
    d = _get_dispatcher()
    return {
        "status": "ok",
        "lane": "phy",
        "executor": cfg.executor_kind,
        "transport": cfg.transport_kind,
        "db_backend": cfg.db_backend,
        "db_schema": cfg.db_schema,
        "executor_state": d.executor.state(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
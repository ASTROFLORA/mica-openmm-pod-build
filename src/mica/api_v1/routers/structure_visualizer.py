"""
structure_visualizer.py — BinaryCIF proxy and structure preview endpoints.

GET /api/v1/structure/bcif?pdb_id={id}
    Proxies BinaryCIF binary from RCSB (models.rcsb.org) and caches in Redis.
    Returns raw bytes; Content-Type: application/octet-stream.
    No auth required (RCSB is public).

GET /api/v1/structure/health
    Smoke check: verifies upstream RCSB reachability. Returns JSON.
"""
from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from mica.infrastructure.redis_client import get_redis_if_configured

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/structure", tags=["structure-visualizer"])

# ── Constants ─────────────────────────────────────────────────────────────────
_RCSB_BCIF_URL = "https://models.rcsb.org/v1/{pdb_id}/full"
_REDIS_TTL_SECONDS = 86_400          # 24 h
_VALID_PDB_ID = re.compile(r"^[A-Za-z0-9]{4}$")
_HTTPX_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _validate_pdb_id(pdb_id: str) -> str:
    """Strict 4-char alphanumeric validation — prevents SSRF via path injection."""
    pdb_id = pdb_id.strip().upper()
    if not _VALID_PDB_ID.match(pdb_id):
        raise HTTPException(
            status_code=422,
            detail="pdb_id must be exactly 4 alphanumeric characters",
        )
    return pdb_id


@router.get(
    "/bcif",
    summary="BinaryCIF proxy",
    description=(
        "Returns the full BinaryCIF binary for a PDB entry, proxied from RCSB "
        "models.rcsb.org and cached in Redis for 24 h. "
        "Molstar must call this with `isBinary: true`."
    ),
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "BinaryCIF binary payload",
        },
        422: {"description": "Invalid pdb_id format"},
        502: {"description": "RCSB upstream unreachable or returned non-200"},
    },
)
async def get_bcif(
    pdb_id: str = Query(..., description="4-character PDB ID (e.g. 1ABC)"),
) -> Response:
    pdb_id = _validate_pdb_id(pdb_id)
    cache_key = f"structure_bcif:{pdb_id}"

    # ── Cache hit ──────────────────────────────────────────────────────────
    try:
        redis = await get_redis_if_configured(
            decode_responses=False,
            verify_connection=True,
        )
        if redis is not None:
            cached = await redis.get(cache_key)
            if cached:
                logger.debug("BinaryCIF cache HIT: %s", pdb_id)
                return Response(
                    content=cached,
                    media_type="application/octet-stream",
                    headers={
                        "X-Cache": "HIT",
                        "X-MICA-Cache": "HIT",
                        "X-PDB-ID": pdb_id,
                        "Access-Control-Allow-Origin": "*",
                    },
                )
    except Exception as exc:
        logger.warning(
            "Redis read failed for %s: %s — proceeding to upstream", cache_key, exc
        )

    # ── RCSB upstream fetch ────────────────────────────────────────────────
    url = _RCSB_BCIF_URL.format(pdb_id=pdb_id.lower())
    try:
        async with httpx.AsyncClient(
            timeout=_HTTPX_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        logger.error("RCSB fetch failed for %s: %s", pdb_id, exc)
        raise HTTPException(
            status_code=502, detail=f"RCSB upstream unreachable: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"RCSB returned {resp.status_code} for {pdb_id}",
        )

    payload = resp.content

    # ── Cache store (best-effort — do not fail request if Redis is down) ──
    try:
        redis = await get_redis_if_configured(
            decode_responses=False,
            verify_connection=True,
        )
        if redis is not None:
            await redis.set(cache_key, payload, ex=_REDIS_TTL_SECONDS)
            logger.debug(
                "BinaryCIF cache STORE: %s (%d bytes)", pdb_id, len(payload)
            )
    except Exception as exc:
        logger.warning("Redis write failed for %s: %s", cache_key, exc)

    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "X-Cache": "MISS",
            "X-MICA-Cache": "MISS",
            "X-PDB-ID": pdb_id,
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get(
    "/health",
    summary="Structure visualizer health",
    responses={
        200: {"description": "OK"},
        502: {"description": "RCSB unreachable"},
    },
)
async def structure_health() -> dict:
    """Smoke-check RCSB reachability by testing a well-known small structure (1CRN)."""
    url = _RCSB_BCIF_URL.format(pdb_id="1crn")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0)
        ) as client:
            resp = await client.head(url)
        ok = resp.status_code == 200
    except Exception as exc:
        logger.warning("RCSB health check failed: %s", exc)
        ok = False

    if not ok:
        raise HTTPException(
            status_code=502, detail="RCSB models.rcsb.org unreachable"
        )
    return {
        "status": "ok",
        "upstream": "rcsb",
        "endpoint": "/api/v1/structure/bcif",
    }

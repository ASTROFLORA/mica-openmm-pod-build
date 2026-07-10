"""compute_commands.py — CK4: Route-backed compute.* read adapters.

Read-only adapters over /api/v1/compute/* endpoints.
No side effects. Route authority backed by real HTTP.
"""

from __future__ import annotations

from typing import Any, Dict

from mica.sdk.command_contracts import BackendCommandEnvelope


async def compute_status(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Inspect compute job status — route-backed read adapter.

    GET /api/v1/compute/jobs/{job_id}
    """
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("compute.status requires a non-empty job_id.")

    try:
        import httpx
        from mica.api_v1.auth import user_dependency

        base = _resolve_api_base()
        headers = {"Authorization": f"Bearer {_resolve_token()}"} if _resolve_token() else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base}/api/v1/compute/jobs/{job_id}", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {
            "summary": f"compute.status route unavailable for job {job_id}",
            "result": {"job_id": job_id, "status": "route_unavailable", "error": str(exc)},
            "route_authority": "backend_api",
            "route_backed": False,
        }

    return {
        "summary": f"Compute job {job_id} status: {data.get('state', 'unknown')}",
        "result": dict(data),
        "route_authority": "backend_api",
        "route_backed": True,
    }


async def compute_artifacts(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """List compute job artifacts — route-backed read adapter.

    GET /api/v1/compute/jobs/{job_id}/ledger
    """
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("compute.artifacts requires a non-empty job_id.")

    try:
        import httpx

        base = _resolve_api_base()
        headers = {"Authorization": f"Bearer {_resolve_token()}"} if _resolve_token() else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base}/api/v1/compute/jobs/{job_id}/ledger", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {
            "summary": f"compute.artifacts route unavailable for job {job_id}",
            "result": {"job_id": job_id, "artifacts": [], "error": str(exc)},
            "route_authority": "backend_api",
            "route_backed": False,
        }

    return {
        "summary": f"Compute job {job_id}: {len(data) if isinstance(data, list) else 0} artifacts",
        "result": {"job_id": job_id, "artifacts": data if isinstance(data, list) else data.get("artifacts", [])},
        "route_authority": "backend_api",
        "route_backed": True,
    }


# ── Helpers ────────────────────────────────────────────────────────────────

_MICA_API_BASE: str | None = None


def _resolve_api_base() -> str:
    global _MICA_API_BASE
    if _MICA_API_BASE:
        return _MICA_API_BASE
    import os
    _MICA_API_BASE = (
        os.getenv("VITE_MICA_API_URL")
        or os.getenv("MICA_API_BASE")
        or "https://mica-api-production.up.railway.app"
    )
    return _MICA_API_BASE


def _resolve_token() -> str | None:
    import os
    return os.getenv("MICA_AUTH_TOKEN") or None

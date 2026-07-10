from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException


WsTicketScope = Literal["mica", "md", "preview"]

_VALID_SCOPES = {"mica", "md", "preview"}
_PROCESS_START_TIME = int(time.time())
_NONCE_COUNTER = itertools.count(1)


def is_production() -> bool:
    """Return True when running in a production-like deployment surface."""
    env = os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "development"
    return str(env).lower() in {"prod", "production"}


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _ticket_secret() -> bytes:
    # F-005 — the WS ticket signing secret must be its own dedicated value. We no
    # longer fall back to MICA_INTERNAL_TOKEN because that token is used as an
    # inter-service authorization header and reusing it as an HMAC key conflates
    # two different trust boundaries.
    secret = os.getenv("MICA_WS_TICKET_SECRET")
    if not secret:
        if is_production():
            raise HTTPException(status_code=503, detail="WebSocket ticket authority is not configured")
        secret = "mica-dev-ws-ticket-secret"
    return str(secret).encode("utf-8")


def _deployment_fingerprint() -> dict[str, str]:
    runtime_surface = (
        os.getenv("MICA_RUNTIME_SURFACE")
        or os.getenv("MICA_SERVICE_ROLE")
        or os.getenv("MICA_DEPLOYMENT_SURFACE")
        or "api"
    )
    buildversion = os.getenv("MICA_BUILDVERSION", "").strip()
    if not buildversion:
        for candidate in (Path("/app/.buildversion"), Path("/tmp/.buildversion")):
            try:
                if candidate.exists():
                    buildversion = candidate.read_text(encoding="utf-8", errors="replace").strip()
                    if buildversion:
                        break
            except Exception:
                continue
    code_ref = f"{Path(__file__).name}:{Path(__file__).stat().st_mtime_ns}"
    code_path_fingerprint = hashlib.sha256(code_ref.encode("utf-8")).hexdigest()[:16]
    build_time = os.getenv("MICA_BUILD_TIME", "").strip()
    deployment_id = os.getenv("RAILWAY_DEPLOYMENT_ID", "").strip()
    deployment_env = (
        os.getenv("ENVIRONMENT")
        or os.getenv("RAILWAY_ENVIRONMENT_NAME")
        or os.getenv("MICA_ENV")
        or ""
    ).strip()
    service_name = (
        os.getenv("RAILWAY_SERVICE_NAME")
        or os.getenv("MICA_COMPONENT")
        or ""
    ).strip()

    return {
        "app_version": buildversion or "dev",
        "git_sha": os.getenv("GITHUB_SHA") or os.getenv("MICA_GIT_SHA") or "",
        "deployment_surface": str(runtime_surface).strip().lower() or "api",
        "deployment_environment": deployment_env,
        "railway_service_name": service_name,
        "railway_deployment_id": deployment_id,
        "build_time": build_time,
        "process_start_time": str(_PROCESS_START_TIME),
        "code_path_fingerprint": code_path_fingerprint,
    }


def ws_ticket_authority_status() -> dict[str, Any]:
    """Return a redacted readiness snapshot for WS ticket authority.

    This intentionally never returns the raw secret.
    """
    secret = os.getenv("MICA_WS_TICKET_SECRET")
    secret_available = bool(secret)
    secret_length = len(str(secret or ""))
    if not secret_available:
        length_category = "missing"
    elif secret_length < 16:
        length_category = "short"
    elif secret_length < 32:
        length_category = "medium"
    else:
        length_category = "long"

    ready = secret_available or (not is_production())
    classification = "ready" if ready else "missing_ticket_secret"
    warning = ""
    if not ready:
        warning = "Set MICA_WS_TICKET_SECRET in production environment variables"

    return {
        "secret_env_name": "MICA_WS_TICKET_SECRET",
        "secret_available": secret_available,
        "secret_length_category": length_category,
        "raw_secret_logged": False,
        "production_env": is_production(),
        "ticket_authority_ready": ready,
        "classification": classification,
        "warning": warning,
        "route_loaded": True,
        "router_module_loaded": True,
        "compute_router_loaded": True,
        "ws_ticket_router_loaded": True,
        "expected_routes": [
            "GET /api/v1/compute/ws-ticket/authority",
            "POST /api/v1/compute/ws-ticket",
        ],
        **_deployment_fingerprint(),
    }


def _sign(encoded_payload: str) -> str:
    digest = hmac.new(_ticket_secret(), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _nonce_seed(*, user_id: str, scope: str, job_id: str, session_id: str, now: int) -> str:
    # Keep nonce generation fully in-process to avoid relying on OS entropy.
    serial = next(_NONCE_COUNTER)
    raw = "|".join(
        [
            str(_PROCESS_START_TIME),
            str(now),
            str(serial),
            str(user_id),
            str(scope),
            str(job_id),
            str(session_id),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def issue_ws_ticket(
    *,
    user_id: str,
    scope: WsTicketScope,
    job_id: str = "",
    run_id: str = "",
    workspace_id: str = "",
    session_id: str = "",
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    resolved_scope = str(scope or "").strip().lower()
    if resolved_scope not in _VALID_SCOPES:
        raise HTTPException(status_code=422, detail="Unsupported WebSocket ticket scope")
    if resolved_scope == "md" and not str(job_id or "").strip():
        raise HTTPException(status_code=422, detail="MD WebSocket tickets require job_id")
    if resolved_scope == "preview" and not str(run_id or "").strip():
        raise HTTPException(status_code=422, detail="Preview WebSocket tickets require run_id")

    ttl = int(ttl_seconds or os.getenv("MICA_WS_TICKET_TTL_SECONDS") or "75")
    ttl = max(15, min(ttl, 300))
    now = int(time.time())
    payload: dict[str, Any] = {
        "v": 1,
        "sub": str(user_id),
        "scope": resolved_scope,
        "job_id": str(job_id or ""),
        "run_id": str(run_id or ""),
        "workspace_id": str(workspace_id or ""),
        "session_id": str(session_id or ""),
        "iat": now,
        "exp": now + ttl,
        "nonce": _nonce_seed(
            user_id=str(user_id),
            scope=resolved_scope,
            job_id=str(job_id or ""),
            session_id=str(session_id or ""),
            now=now,
        ),
    }
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(encoded_payload)
    return {
        "ticket": f"{encoded_payload}.{signature}",
        "expires_at": payload["exp"],
        "ttl_seconds": ttl,
        "scope": resolved_scope,
    }


def verify_ws_ticket(
    ticket: str,
    *,
    scope: WsTicketScope,
    job_id: str = "",
    run_id: str = "",
    workspace_id: str = "",
    session_id: str = "",
) -> str:
    raw_ticket = str(ticket or "").strip()
    if not raw_ticket or "." not in raw_ticket:
        raise HTTPException(status_code=401, detail="Invalid WebSocket ticket")
    encoded_payload, signature = raw_ticket.rsplit(".", 1)
    expected = _sign(encoded_payload)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid WebSocket ticket signature")
    try:
        payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Malformed WebSocket ticket") from exc

    if int(payload.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="Expired WebSocket ticket")
    if str(payload.get("scope") or "") != str(scope):
        raise HTTPException(status_code=401, detail="WebSocket ticket scope mismatch")
    if job_id and str(payload.get("job_id") or "") != str(job_id):
        raise HTTPException(status_code=401, detail="WebSocket ticket job mismatch")
    if run_id and str(payload.get("run_id") or "") != str(run_id):
        raise HTTPException(status_code=401, detail="WebSocket ticket run mismatch")
    if workspace_id and str(payload.get("workspace_id") or "") not in {"", str(workspace_id)}:
        raise HTTPException(status_code=401, detail="WebSocket ticket workspace mismatch")
    if session_id and str(payload.get("session_id") or "") not in {"", str(session_id)}:
        raise HTTPException(status_code=401, detail="WebSocket ticket session mismatch")

    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="WebSocket ticket missing subject")
    return user_id

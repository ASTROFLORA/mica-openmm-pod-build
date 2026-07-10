"""Auth helpers for API v1.

Centralizes user identity extraction so routers don't ship with placeholder auth.

- If Clerk JWKS is configured, validates `Authorization: Bearer <jwt>` and returns `sub`.
- Otherwise falls back to `X-User-Id` header (dev-mode only).

NOTE: For production, set CLERK_JWKS_URL + CLERK_ISSUER (+ optionally CLERK_AUDIENCE)
      and disable X-User-Id fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import hmac
from functools import lru_cache
from typing import Any, Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request
from pydantic import ValidationError

from mica.identity.request_identity import MembershipRole, PlanTier, RequestIdentity

logger = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    return val if val not in (None, "") else default


def _is_production_env() -> bool:
    env = (
        _env("MICA_ENV")
        or _env("ENVIRONMENT")
        or _env("APP_ENV")
        or "development"
    )
    return str(env).lower() in ("prod", "production")


def _env_flag(name: str) -> bool:
    return (_env(name) or "false").lower() == "true"


def _clerk_auth_configured() -> bool:
    return bool(_env("CLERK_JWKS_URL"))


def allow_user_id_fallback(*, transport: str = "http") -> bool:
    """Return whether local/dev user-id fallback should be accepted.

    Contract:
    - production never auto-enables fallback
    - explicit env flags can enable fallback for local probes
    - WebSocket auth never auto-enables fallback; it must be opt-in.
    - HTTP keeps the legacy non-production fallback when Clerk is absent so
      repo-wide REST/dev slices do not break mid-migration.
    """

    allow_http_fallback = _env_flag("MICA_ALLOW_X_USER_ID_FALLBACK")
    allow_ws_fallback = _env_flag("MICA_WS_ALLOW_USER_ID_FALLBACK")
    transport_name = str(transport or "").lower()
    if allow_http_fallback:
        return True
    if transport_name == "ws":
        return allow_ws_fallback
    return (not _is_production_env()) and (not _clerk_auth_configured())


def _coerce_membership_role(value: Any) -> MembershipRole | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().lower()
    for role in MembershipRole:
        if role.value == raw:
            return role
    logger.warning("Unrecognized membership role claim: %s", value)
    return None


def _coerce_plan_tier(value: Any) -> PlanTier | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().lower()
    for tier in PlanTier:
        if tier.value == raw:
            return tier
    logger.warning("Unrecognized plan tier claim: %s", value)
    return None


def _datetime_from_claim(value: Any, *, claim_name: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token {claim_name}") from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise HTTPException(status_code=401, detail=f"Token missing {claim_name}")


def _build_request_identity_from_claims(claims: dict[str, Any]) -> RequestIdentity:
    org_metadata = claims.get("org_metadata")
    if not isinstance(org_metadata, dict):
        org_metadata = {}

    custom_claims = org_metadata.get("custom")
    if not isinstance(custom_claims, dict):
        custom_claims = {}

    try:
        issued_at = _datetime_from_claim(claims.get("iat"), claim_name="iat")
        expires_at = _datetime_from_claim(claims.get("exp"), claim_name="exp")
        return RequestIdentity(
            user_id=str(claims.get("sub") or ""),
            org_id=str(claims.get("org_id") or "").strip() or None,
            lab_id=str(org_metadata.get("lab_id") or claims.get("lab_id") or "").strip() or None,
            membership_role=_coerce_membership_role(claims.get("org_role") or claims.get("role")),
            issuer=str(claims.get("iss") or _env("CLERK_ISSUER") or "unknown-issuer"),
            issued_at=issued_at,
            expires_at=expires_at,
            plan_tier=_coerce_plan_tier(org_metadata.get("plan_tier") or claims.get("plan_tier")),
            lab_display_name=str(org_metadata.get("lab_display_name") or ""),
            custom_claims=custom_claims,
            session_id=str(claims.get("sid") or "").strip() or None,
            authenticated_at=issued_at,
            is_dev_fallback=False,
        )
    except ValidationError as exc:
        logger.warning("JWT identity envelope validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid identity claims") from exc


def _build_dev_fallback_identity(user_id: str) -> RequestIdentity | None:
    now = datetime.now(timezone.utc)
    try:
        return RequestIdentity(
            user_id=user_id,
            org_id=None,
            lab_id=None,
            membership_role=MembershipRole.MEMBER,
            issuer=_env("CLERK_ISSUER") or "dev-fallback://local",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            is_dev_fallback=True,
        )
    except ValidationError:
        logger.debug("Skipping canonical fallback identity for legacy user id %r", user_id)
        return None


def _resolve_user_id_and_identity(
    *,
    x_user_id: Optional[str],
    authorization: Optional[str],
    x_internal_token: Optional[str] = None,
    request: Request = None,
    transport: str = "http",
) -> tuple[str, RequestIdentity | None]:
    jwks_url = _env("CLERK_JWKS_URL")
    issuer = _env("CLERK_ISSUER")
    audience = _env("CLERK_AUDIENCE")
    require_token = (_env("CLERK_REQUIRE_TOKEN", "true") or "true").lower() == "true"

    is_prod = _is_production_env()
    allow_unsafe_prod = _env_flag("MICA_ALLOW_UNSAFE_X_USER_ID_PRODUCTION")

    # ── Internal Token path (service auth) ────────────────────────
    if x_internal_token:
        expected = _env("MICA_INTERNAL_TOKEN")
        if not expected:
            if is_prod:
                logger.error("MICA_INTERNAL_TOKEN is required in production but missing")
                raise HTTPException(status_code=401, detail="service_token_rejected")
            else:
                logger.warning("MICA_INTERNAL_TOKEN is empty — internal endpoint unauthenticated bypass")
        else:
            provided = x_internal_token.strip()
            if not hmac.compare_digest(provided, expected.strip()):
                raise HTTPException(status_code=401, detail="service_token_rejected")

        # Verify route allowlist
        is_allowlisted = False
        path = request.url.path if request else ""
        for prefix in [
            "/api/v1/artifacts",
            "/api/v1/jobs",
            "/api/v1/studies",
            "/api/v1/working-sets",
            "/api/v1/compute",
            "/api/v1/labs",
            "/api/v1/knowledge-spaces",
            "/api/v1/research-lines",
        ]:
            if path == prefix or path.startswith(prefix + "/"):
                is_allowlisted = True
                break

        if not is_allowlisted:
            raise HTTPException(status_code=403, detail="clerk_only_route")

        user_id = x_user_id.strip() if (x_user_id and x_user_id.strip()) else "agent_service"
        if len(user_id) < 10:
            user_id = f"user_agent_{user_id}"

        now = datetime.now(timezone.utc)
        identity = RequestIdentity(
            user_id=user_id,
            org_id=None,
            lab_id=None,
            membership_role=MembershipRole.ADMIN,
            issuer="internal-agent-service",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            is_dev_fallback=False,
            custom_claims={"agent_service_auth": True}
        )
        return user_id, identity

    # ── Clerk JWT path (preferred, secure) ────────────────────────
    token = _extract_bearer(authorization)
    if jwks_url and token:
        if is_prod and not issuer:
            raise HTTPException(status_code=500, detail="Auth misconfigured")

        client = _jwks_client()
        if client is None:
            raise HTTPException(status_code=500, detail="Auth not configured")

        try:
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=audience,
                issuer=issuer,
                options={
                    "verify_aud": bool(audience),
                    "verify_iss": bool(issuer),
                    "leeway": 10,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except jwt.InvalidTokenError as exc:
            logger.warning("JWT validation failed: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid token") from exc

        request_identity = _build_request_identity_from_claims(claims)
        return request_identity.user_id, request_identity

    # ── X-User-Id fallback path (staging/dev only) ────────────────
    allow_fallback = allow_user_id_fallback(transport=transport)

    if x_user_id and x_user_id.strip():
        user_id = x_user_id.strip()

        # Production: REJECT X-User-Id unless explicit unsafe override
        if is_prod:
            if not allow_unsafe_prod:
                logger.error(
                    "X-User-Id fallback REJECTED in production for user %r "
                    "(set MICA_ALLOW_UNSAFE_X_USER_ID_PRODUCTION=true to override dangerously)",
                    user_id,
                )
                raise HTTPException(
                    status_code=401,
                    detail="X-User-Id fallback is forbidden in production",
                )
            # Unsafe production override — log CRITICAL
            logger.critical(
                "UNSAFE: X-User-Id fallback enabled in production for user %r "
                "(MICA_ALLOW_UNSAFE_X_USER_ID_PRODUCTION=true)",
                user_id,
            )
            return user_id, _build_dev_fallback_identity(user_id)

        # Staging / development: check fallback policy
        if allow_fallback:
            logger.info(
                "X-User-Id fallback accepted (staging/dev, is_prod=%s) for user %r",
                is_prod,
                user_id,
            )
            return user_id, _build_dev_fallback_identity(user_id)

        # Fallback disallowed
        raise HTTPException(
            status_code=401,
            detail="X-User-Id fallback not allowed in this environment",
        )

    # ── No credentials provided ───────────────────────────────────
    if jwks_url:
        if require_token:
            raise HTTPException(status_code=401, detail="Missing bearer token")
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not allow_fallback:
        raise HTTPException(status_code=500, detail="Auth misconfigured")

    raise HTTPException(status_code=401, detail="Missing X-User-Id header")


def resolve_user_id(
    *,
    x_user_id: Optional[str],
    authorization: Optional[str],
    x_internal_token: Optional[str] = None,
    request: Request = None,
    transport: str = "http",
) -> str:
    user_id, _ = _resolve_user_id_and_identity(
        x_user_id=x_user_id,
        authorization=authorization,
        x_internal_token=x_internal_token,
        request=request,
        transport=transport,
    )
    return user_id


def resolve_request_identity(
    *,
    x_user_id: Optional[str],
    authorization: Optional[str],
    x_internal_token: Optional[str] = None,
    request: Request = None,
    transport: str = "http",
) -> RequestIdentity | None:
    _, request_identity = _resolve_user_id_and_identity(
        x_user_id=x_user_id,
        authorization=authorization,
        x_internal_token=x_internal_token,
        request=request,
        transport=transport,
    )
    return request_identity


def _extract_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip() or None


@lru_cache(maxsize=1)
def _jwks_client():
    jwks_url = _env("CLERK_JWKS_URL")
    if not jwks_url:
        return None
    try:
        return jwt.PyJWKClient(jwks_url)
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Failed to initialize JWKS client: {exc}")


def user_dependency(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
) -> str:
    """FastAPI dependency returning canonical user_id."""
    return resolve_user_id(
        x_user_id=x_user_id,
        authorization=authorization,
        x_internal_token=x_internal_token,
        request=request,
        transport="http",
    )


def request_identity_dependency(
    request: Request,
    user_id: str = Depends(user_dependency),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
) -> RequestIdentity | str:
    """Return the canonical HTTP identity envelope when it can be materialized.

    The nested ``Depends(user_dependency)`` keeps existing dependency override
    behavior intact while HTTP routes migrate incrementally from raw user ids to
    ``RequestIdentity``.
    """

    try:
        request_identity = resolve_request_identity(
            x_user_id=x_user_id,
            authorization=authorization,
            x_internal_token=x_internal_token,
            request=request,
            transport="http",
        )
    except HTTPException as exc:
        # FastAPI dependency overrides often short-circuit ``user_dependency``
        # without providing HTTP headers. In that case keep the already-resolved
        # user id so staged router migrations remain testable and incremental.
        if exc.status_code == 401 and not x_user_id and not authorization and not x_internal_token:
            return user_id
        raise
    return request_identity or user_id


def _header_or_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def effective_context_dependency(
    request: Request,
    identity: RequestIdentity | str = Depends(request_identity_dependency),
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-Id"),
    x_active_scope_id: Optional[str] = Header(default=None, alias="X-Active-Scope-Id"),
    x_destination_scope_id: Optional[str] = Header(default=None, alias="X-Destination-Scope-Id"),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
    x_study_id: Optional[str] = Header(default=None, alias="X-Study-Id"),
    x_research_line_id: Optional[str] = Header(default=None, alias="X-Research-Line-Id"),
    x_lab_id: Optional[str] = Header(default=None, alias="X-Lab-Id"),
    x_policy_snapshot_id: Optional[str] = Header(default=None, alias="X-Policy-Snapshot-Id"),
):
    """Resolve APV-01 EffectiveContext for the current HTTP request.

    Fail-closed: missing actor raises 401. Invalid scope refs raise 400.
    """
    from mica.identity.effective_context import (
        EffectiveContextError,
        EffectiveContextHints,
        resolve_effective_context,
    )

    hints = EffectiveContextHints(
        session_id=_header_or_none(x_session_id) or _header_or_none(request.query_params.get("session_id")),
        active_scope_id=_header_or_none(x_active_scope_id)
        or _header_or_none(request.query_params.get("active_scope_id")),
        destination_scope_id=_header_or_none(x_destination_scope_id)
        or _header_or_none(request.query_params.get("destination_scope_id")),
        workspace_id=_header_or_none(x_workspace_id)
        or _header_or_none(request.query_params.get("workspace_id")),
        study_id=_header_or_none(x_study_id) or _header_or_none(request.query_params.get("study_id")),
        research_line_id=_header_or_none(x_research_line_id)
        or _header_or_none(request.query_params.get("research_line_id")),
        lab_id=_header_or_none(x_lab_id) or _header_or_none(request.query_params.get("lab_id")),
        policy_snapshot_id=_header_or_none(x_policy_snapshot_id),
    )
    try:
        ctx = resolve_effective_context(identity=identity, hints=hints)
    except EffectiveContextError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_effective_context: {exc}") from exc

    # Stash for downstream handlers that still use Request.state.
    try:
        request.state.effective_context = ctx
    except Exception:
        pass
    return ctx

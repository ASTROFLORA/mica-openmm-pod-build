from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mica.identity.request_identity import RequestIdentity

from .control_plane import default_tenant_id_for_user

_VALID_SCOPES = {"user", "workspace", "team", "global"}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_cache_scope(value: Any) -> str:
    text = _safe_text(value).lower()
    if text in {"", "default"}:
        return ""
    if text in _VALID_SCOPES:
        return text
    if text in {"public", "shared", "all"}:
        return "global"
    if text in {"org", "organization", "tenant"}:
        return "team"
    if text in {"lab"}:
        return "workspace"
    return ""


def _normalize_lab_tenant(value: str) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("lab:") or lowered.startswith("workspace:"):
        return text
    return f"lab:{text}"


def _infer_scope_from_tenant_id(tenant_id: str, default_user_tenant_id: str) -> str:
    lowered = _safe_text(tenant_id).lower()
    if not lowered:
        return ""
    if lowered in {"global", "public"}:
        return "global"
    if lowered.startswith("lab:") or lowered.startswith("workspace:"):
        return "workspace"
    if lowered in {"default", default_user_tenant_id.lower()}:
        return "user"
    return "team"


@dataclass(frozen=True)
class ResolvedLiteratureScopeAuthority:
    tenant_id: str
    cache_write_scope: str
    authority_source: str
    default_user_tenant_id: str
    request_identity_bound: bool
    is_dev_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "cache_write_scope": self.cache_write_scope,
            "authority_source": self.authority_source,
            "default_user_tenant_id": self.default_user_tenant_id,
            "request_identity_bound": self.request_identity_bound,
            "is_dev_fallback": self.is_dev_fallback,
        }


def resolve_literature_scope_authority(
    *,
    user_id: str,
    request_identity: RequestIdentity | str | None,
    requested_tenant_id: str | None = None,
    requested_cache_write_scope: str | None = None,
) -> ResolvedLiteratureScopeAuthority:
    normalized_user_id = _safe_text(user_id)
    default_user_tenant_id = default_tenant_id_for_user(normalized_user_id)
    canonical_identity = request_identity if isinstance(request_identity, RequestIdentity) else None
    explicit_tenant_id = _safe_text(requested_tenant_id)
    resolved_scope = normalize_cache_scope(requested_cache_write_scope)
    authority_source = ""

    if resolved_scope == "global" and explicit_tenant_id.lower() not in {"global", "public"}:
        raise ValueError("cache_write_scope='global' requires explicit tenant_id 'global' or 'public'")

    if resolved_scope == "workspace" and not explicit_tenant_id:
        if canonical_identity is None or not _safe_text(canonical_identity.lab_id):
            raise ValueError("cache_write_scope='workspace' requires authenticated lab identity or explicit tenant_id")
        explicit_tenant_id = _normalize_lab_tenant(str(canonical_identity.lab_id or ""))
        authority_source = "request_identity.lab_id"
    elif resolved_scope == "team" and not explicit_tenant_id:
        if canonical_identity is None or not _safe_text(canonical_identity.org_id):
            raise ValueError("cache_write_scope='team' requires authenticated org identity or explicit tenant_id")
        explicit_tenant_id = _safe_text(canonical_identity.org_id)
        authority_source = "request_identity.org_id"
    elif resolved_scope == "user" and not explicit_tenant_id:
        explicit_tenant_id = default_user_tenant_id
        authority_source = "default_user_tenant"

    if not resolved_scope:
        resolved_scope = _infer_scope_from_tenant_id(explicit_tenant_id, default_user_tenant_id)

    if resolved_scope == "user" and not explicit_tenant_id:
        explicit_tenant_id = default_user_tenant_id

    if not authority_source:
        if explicit_tenant_id and requested_tenant_id:
            authority_source = "explicit_tenant_id"
        elif resolved_scope:
            authority_source = "inferred_runtime_scope"
        else:
            authority_source = "caller_default_runtime"

    return ResolvedLiteratureScopeAuthority(
        tenant_id=explicit_tenant_id,
        cache_write_scope=resolved_scope,
        authority_source=authority_source,
        default_user_tenant_id=default_user_tenant_id,
        request_identity_bound=canonical_identity is not None,
        is_dev_fallback=bool(canonical_identity and canonical_identity.is_dev_fallback),
    )

"""EffectiveContext — canonical product request scope resolver (APV-01).

Authority: MICA_ASTROFLORA_UNIFIED_PRODUCT_NORTH_STAR_V0_6.md §5.1
Plane: Identity and Scope

Consumes:
  - RequestIdentity (actor)
  - tenancy.models.build_scope_ref / parse_scope_ref (scope string format)

Does not own:
  - EffectivePermission decisions (APV-02)
  - PEP enforcement (APV-03)
  - Prompt ContextEnvelope (Context Steward)
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from mica.tenancy.models import ScopeType, build_scope_ref, parse_scope_ref

_SCOPE_REF_RE = re.compile(r"^[a-z_]+:.+$")
_UNRESOLVED_POLICY_SNAPSHOT = "policy_snapshot:unresolved"


class EffectiveContextError(ValueError):
    """Fail-closed EffectiveContext resolution error."""


class EffectiveContext(BaseModel):
    """Canonical effective product context for every request/session."""

    actor_user_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    active_scope_id: str = Field(..., min_length=1)
    home_scope_id: str = Field(..., min_length=1)
    destination_scope_id: str | None = None
    lab_id: str | None = None
    study_id: str | None = None
    research_line_id: str | None = None
    workspace_id: str | None = None
    permission_fingerprint: str = Field(..., min_length=1)
    policy_snapshot_id: str = Field(default=_UNRESOLVED_POLICY_SNAPSHOT)

    @field_validator("active_scope_id", "home_scope_id", "destination_scope_id")
    @classmethod
    def _validate_scope_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("scope ref must be non-empty")
        if not _SCOPE_REF_RE.match(normalized):
            raise ValueError(f"invalid scope_ref format: {normalized}")
        # Ensure parseable against tenancy ScopeType vocabulary.
        parse_scope_ref(normalized)
        return normalized

    @model_validator(mode="after")
    def _destination_distinct_when_set(self) -> "EffectiveContext":
        if self.destination_scope_id and self.destination_scope_id == self.active_scope_id:
            # Same destination as active is allowed but meaningless; keep explicit.
            return self
        return self

    def cache_key_material(self) -> str:
        """Stable material for cache keys (scope + permission fingerprint + actor)."""
        return ":".join(
            [
                self.actor_user_id,
                self.active_scope_id,
                self.permission_fingerprint,
                self.policy_snapshot_id,
            ]
        )


class EffectiveContextHints(BaseModel):
    """Optional request/session hints used to resolve EffectiveContext."""

    session_id: str | None = None
    active_scope_id: str | None = None
    destination_scope_id: str | None = None
    lab_id: str | None = None
    study_id: str | None = None
    research_line_id: str | None = None
    workspace_id: str | None = None
    policy_snapshot_id: str | None = None


def personal_home_scope_id(actor_user_id: str) -> str:
    return build_scope_ref(ScopeType.USER, actor_user_id)


def lab_home_scope_id(lab_id: str) -> str:
    return build_scope_ref(ScopeType.LAB, lab_id)


def study_scope_id(study_id: str) -> str:
    return build_scope_ref(ScopeType.STUDY, study_id)


def compute_permission_fingerprint(
    *,
    subject_claims_hash: str,
    active_scope_id: str,
    policy_snapshot_id: str = _UNRESOLVED_POLICY_SNAPSHOT,
) -> str:
    """APV-01 stub fingerprint until APV-02 durable EffectivePermission authority.

    Combines identity binding + active scope + policy snapshot id so caches
    invalidate on scope switch even before full PDP unification.
    """
    raw = f"{subject_claims_hash}|{active_scope_id}|{policy_snapshot_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _actor_from_identity(identity: Any) -> tuple[str, str | None, str | None, str]:
    """Extract actor fields from RequestIdentity or plain user_id string."""
    if identity is None:
        raise EffectiveContextError("actor identity is required")
    if isinstance(identity, str):
        user_id = identity.strip()
        if not user_id:
            raise EffectiveContextError("actor_user_id is required")
        claims_hash = hashlib.sha256(f"{user_id}:::".encode("utf-8")).hexdigest()
        return user_id, None, None, claims_hash

    user_id = str(getattr(identity, "user_id", "") or "").strip()
    if not user_id:
        raise EffectiveContextError("actor_user_id is required")
    lab_id = getattr(identity, "lab_id", None)
    session_id = getattr(identity, "session_id", None)
    claims_hash = str(getattr(identity, "subject_claims_hash", "") or "").strip()
    if not claims_hash:
        org_id = getattr(identity, "org_id", None)
        role = getattr(identity, "membership_role", None)
        role_value = role.value if hasattr(role, "value") else (str(role) if role else "")
        claims_hash = hashlib.sha256(
            f"{user_id}:{org_id or ''}:{lab_id or ''}:{role_value}".encode("utf-8")
        ).hexdigest()
    return user_id, lab_id, session_id, claims_hash


def resolve_effective_context(
    *,
    identity: Any,
    hints: EffectiveContextHints | dict[str, Any] | None = None,
) -> EffectiveContext:
    """Resolve the canonical EffectiveContext for a request.

    Fail-closed: missing actor raises EffectiveContextError.
    Home scope prefers lab membership when present, else personal user scope.
    Active scope defaults to home unless an explicit active/study hint is provided.
    """
    hint_model = (
        hints
        if isinstance(hints, EffectiveContextHints)
        else EffectiveContextHints.model_validate(hints or {})
    )
    actor_user_id, identity_lab_id, identity_session_id, claims_hash = _actor_from_identity(identity)

    lab_id = (hint_model.lab_id or identity_lab_id or None)
    if lab_id is not None:
        lab_id = str(lab_id).strip() or None

    study_id = (hint_model.study_id or None)
    if study_id is not None:
        study_id = str(study_id).strip() or None

    research_line_id = (hint_model.research_line_id or None)
    if research_line_id is not None:
        research_line_id = str(research_line_id).strip() or None

    workspace_id = (hint_model.workspace_id or None)
    if workspace_id is not None:
        workspace_id = str(workspace_id).strip() or None

    session_id = (
        (hint_model.session_id or identity_session_id or "").strip()
        or f"ses_actor_{hashlib.sha256(actor_user_id.encode('utf-8')).hexdigest()[:16]}"
    )

    if lab_id:
        home_scope_id = lab_home_scope_id(lab_id)
    else:
        home_scope_id = personal_home_scope_id(actor_user_id)

    if hint_model.active_scope_id:
        active_scope_id = hint_model.active_scope_id.strip()
    elif study_id:
        active_scope_id = study_scope_id(study_id)
    else:
        active_scope_id = home_scope_id

    destination_scope_id = (
        hint_model.destination_scope_id.strip() if hint_model.destination_scope_id else None
    )
    policy_snapshot_id = (
        hint_model.policy_snapshot_id.strip()
        if hint_model.policy_snapshot_id
        else _UNRESOLVED_POLICY_SNAPSHOT
    )

    permission_fingerprint = compute_permission_fingerprint(
        subject_claims_hash=claims_hash,
        active_scope_id=active_scope_id,
        policy_snapshot_id=policy_snapshot_id,
    )

    return EffectiveContext(
        actor_user_id=actor_user_id,
        session_id=session_id,
        active_scope_id=active_scope_id,
        home_scope_id=home_scope_id,
        destination_scope_id=destination_scope_id,
        lab_id=lab_id,
        study_id=study_id,
        research_line_id=research_line_id,
        workspace_id=workspace_id,
        permission_fingerprint=permission_fingerprint,
        policy_snapshot_id=policy_snapshot_id,
    )


def effective_context_to_request_identity_payload(ctx: EffectiveContext) -> dict[str, Any]:
    """Embed EffectiveContext into BackendCommandEnvelope.request_identity without a second envelope."""
    return {
        "user_id": ctx.actor_user_id,
        "effective_context": ctx.model_dump(mode="json"),
        "active_scope_id": ctx.active_scope_id,
        "home_scope_id": ctx.home_scope_id,
        "destination_scope_id": ctx.destination_scope_id,
        "permission_fingerprint": ctx.permission_fingerprint,
        "policy_snapshot_id": ctx.policy_snapshot_id,
    }


__all__ = [
    "EffectiveContext",
    "EffectiveContextError",
    "EffectiveContextHints",
    "compute_permission_fingerprint",
    "effective_context_to_request_identity_payload",
    "lab_home_scope_id",
    "personal_home_scope_id",
    "resolve_effective_context",
    "study_scope_id",
]

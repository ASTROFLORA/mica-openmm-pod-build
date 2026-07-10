"""
RequestIdentity — Canonical tenant identity envelope for MICA.

This module defines the single authoritative Pydantic model that all MICA
request handlers, middleware, and agentic bridges consume to represent an
authenticated, tenant-scoped request identity.

Schema authority: ``docs/identity/CANONICAL_IDENTITY_CLAIMS_SCHEMA.md`` (G-A1).
Compliance refs:
  - OWASP ASVS 4.1 §2.7 – session management and credential binding
  - RFC 7519 – JSON Web Tokens (JWT)
  - OpenID Connect Core 1.0 – standard claims
  - NIST 800-63-3 §5.2 – subject binding, affiliation, role-based access

DO NOT add runtime extraction logic here.  This module is type-definitions and
docstrings only.  Extraction logic lives in ``src/mica/api_v1/auth.py``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MembershipRole(str, Enum):
    """Org-scoped membership roles.

    Source: ``org_role`` claim in Clerk JWT.
    Ref: NIST 800-63-3 §5.2.3 (role-based access).
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class PlanTier(str, Enum):
    """Billing/quota tier for the organization.

    Source: ``org_metadata.plan_tier`` claim in Clerk JWT.
    Drives compute-access gates downstream.
    """

    FREE = "free"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


# ---------------------------------------------------------------------------
# Canonical role mapping  (Tenancy T1.1 bridge)
# ---------------------------------------------------------------------------

# Lazy import to avoid circular dependency at module level
_TENANCY_ROLES_IMPORTED = False
_CanonicalRole = None

def _get_canonical_role_enum():
    global _CanonicalRole, _TENANCY_ROLES_IMPORTED
    if not _TENANCY_ROLES_IMPORTED:
        try:
            from mica.tenancy.models import CanonicalRole as _CR
            _CanonicalRole = _CR
            _TENANCY_ROLES_IMPORTED = True
        except ImportError:
            pass
    return _CanonicalRole

# Clerk MembershipRole → T1.1 CanonicalRole mapping
# More specific roles (admin/owner) include the less specific ones.
_MEMBERSHIP_TO_CANONICAL: dict[MembershipRole, list[str]] = {
    MembershipRole.OWNER:   ["org_admin", "lab_admin", "pi", "curator", "lab_member", "member"],
    MembershipRole.ADMIN:   ["lab_admin", "pi", "curator", "lab_member", "member"],
    MembershipRole.MEMBER:  ["lab_member", "member"],
    MembershipRole.VIEWER:  ["external_reviewer"],
}

def membership_to_canonical_roles(membership_role: MembershipRole | None) -> list[str]:
    """Map Clerk membership role to T1.1 canonical role names.

    Doc T1.1: RBAC grants possibility. ABAC/policy decides permission.
    """
    if membership_role is None:
        return ["external_reviewer"]
    return _MEMBERSHIP_TO_CANONICAL.get(membership_role, ["member"])


# ---------------------------------------------------------------------------
# RequestIdentity model
# ---------------------------------------------------------------------------


class RequestIdentity(BaseModel):
    """Canonical tenant identity extracted from a Clerk JWT.

    Every field maps to a JWT claim as defined in
    ``docs/identity/CANONICAL_IDENTITY_CLAIMS_SCHEMA.md``.  Derived fields
    (``authenticated_at``, ``subject_claims_hash``, ``is_dev_fallback``) are
    computed at construction time and never stored in the JWT.

    Usage::

        identity = RequestIdentity(
            user_id="user_2abc...",
            org_id="org_xyz...",
            lab_id="lab-alpha",
            membership_role=MembershipRole.MEMBER,
            issuer="https://clerk.mica.ai",
            issued_at=datetime.now(tz=timezone.utc),
            expires_at=datetime.now(tz=timezone.utc),
        )

    Raises:
        pydantic.ValidationError: if any required field is missing or violates
            validation rules defined in the canonical claims schema.
    """

    # ------------------------------------------------------------------
    # Primary claims — required for all request types
    # ------------------------------------------------------------------

    user_id: str = Field(
        ...,
        description=(
            "Clerk user identifier.  Globally unique within the issuer. "
            "Source: JWT ``sub`` claim.  "
            "Ref: NIST 800-63-3 §5.2.2; RFC 7519 §4.1.2."
        ),
        min_length=10,
        pattern=r"^[a-zA-Z0-9_-]{10,}$",
    )

    org_id: str | None = Field(
        default=None,
        description=(
            "Clerk organization identifier.  Maps to MICA tenant.  "
            "Source: JWT ``org_id`` claim.  "
            "Ref: NIST 800-63-3 §5.2.4 (affiliation).  "
            "None only in dev-fallback mode."
        ),
    )

    lab_id: str | None = Field(
        default=None,
        description=(
            "MICA lab within the org.  Maps to RLS row scope.  "
            "Source: JWT ``org_metadata.lab_id`` claim.  "
            "Ref: NIST 800-63-3 §5.2.4.  "
            "None only in dev-fallback mode."
        ),
    )

    membership_role: MembershipRole | None = Field(
        default=None,
        description=(
            "Org-scoped membership role.  Drives authorization policy.  "
            "Source: JWT ``org_role`` claim.  "
            "Ref: NIST 800-63-3 §5.2.3."
        ),
    )

    issuer: str = Field(
        ...,
        description=(
            "JWT issuer.  Must equal the configured issuer URL for the environment.  "
            "Source: JWT ``iss`` claim.  "
            "Ref: RFC 7519 §4.1.1."
        ),
    )

    issued_at: datetime = Field(
        ...,
        description=(
            "Token issuance time (UTC).  Guards against token replay.  "
            "Source: JWT ``iat`` claim.  "
            "Ref: RFC 7519 §4.1.6; NIST 800-63B §7.3."
        ),
    )

    expires_at: datetime = Field(
        ...,
        description=(
            "Token expiry time (UTC).  Hard-fail if expired.  "
            "Source: JWT ``exp`` claim.  "
            "Ref: RFC 7519 §4.1.4; NIST 800-63B §7.1."
        ),
    )

    # ------------------------------------------------------------------
    # Organizational claims — required for Tier-R compute
    # ------------------------------------------------------------------

    plan_tier: PlanTier | None = Field(
        default=None,
        description=(
            "Billing/quota tier.  Drives compute access gates.  "
            "Source: JWT ``org_metadata.plan_tier`` claim."
        ),
    )

    # ------------------------------------------------------------------
    # Optional claims
    # ------------------------------------------------------------------

    lab_display_name: str = Field(
        default="",
        description=(
            "Human-readable lab name.  UI display only, not used for auth.  "
            "Source: JWT ``org_metadata.lab_display_name`` claim."
        ),
    )

    custom_claims: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Extensibility bag for enterprise orgs.  Must be opaque to the auth layer.  "
            "Source: JWT ``org_metadata.custom`` claim."
        ),
    )

    session_id: str | None = Field(
        default=None,
        description=(
            "Clerk session ID.  Used for audit log correlation.  "
            "Source: JWT ``sid`` claim."
        ),
    )

    # ------------------------------------------------------------------
    # Derived claims — computed, not from JWT
    # ------------------------------------------------------------------

    authenticated_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
        description=(
            "Canonical auth timestamp set to ``issued_at`` at extraction time.  "
            "Used in audit logs.  Not stored in JWT."
        ),
    )

    subject_claims_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of ``user_id + org_id + lab_id + membership_role``.  "
            "Immutable binding for audit log integrity and non-repudiation.  "
            "Ref: NIST 800-63-3 §8.3.  Not stored in JWT."
        ),
    )

    is_dev_fallback: bool = Field(
        default=False,
        description=(
            "True if this identity was created from the ``X-User-Id`` dev bypass header.  "
            "MUST always be False in production.  "
            "Ref: ``docs/identity/PRODUCTION_X_USER_ID_HARD_GATE_POLICY.md``."
        ),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("org_id")
    @classmethod
    def org_id_must_have_prefix(cls, v: str | None) -> str | None:
        """Clerk org IDs always start with ``org_``."""
        if v is not None and not v.startswith("org_"):
            raise ValueError("org_id must start with 'org_' (Clerk convention)")
        return v

    @model_validator(mode="after")
    def compute_derived_fields(self) -> "RequestIdentity":
        """Compute ``subject_claims_hash`` and set ``authenticated_at``."""
        # Hash binding: user_id + org_id + lab_id + membership_role
        raw = ":".join([
            self.user_id,
            self.org_id or "",
            self.lab_id or "",
            self.membership_role.value if self.membership_role else "",
        ])
        self.subject_claims_hash = hashlib.sha256(raw.encode()).hexdigest()
        # authenticated_at defaults to now(); callers may override with iat value.
        return self

    @model_validator(mode="after")
    def production_dev_fallback_guard(self) -> "RequestIdentity":
        """Raises if is_dev_fallback is True but org_id is set (inconsistent state).

        Full production guard (rejecting is_dev_fallback=True) is enforced by
        middleware reading MICA_ENV, not by this model.  The model only
        catches internal inconsistency.
        """
        if self.is_dev_fallback and self.org_id is not None:
            raise ValueError(
                "is_dev_fallback cannot be True when org_id is set; "
                "dev fallback only applies to unauthenticated dev sessions."
            )
        return self

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_expired(self) -> bool:
        """Return True if the token has passed its expiry time."""
        return datetime.now(tz=timezone.utc) >= self.expires_at

    def has_role(self, *roles: MembershipRole) -> bool:
        """Return True if membership_role is one of the given roles."""
        return self.membership_role in roles

    def is_tier_r_eligible(self) -> bool:
        """Return True if this identity meets Tier-R compute requirements.

        Tier-R requires: org_id, lab_id, membership_role, plan_tier, and
        is_dev_fallback must be False.
        Ref: ``docs/compliance/IMAGE_TIER_AND_WORKLOAD_CLASSIFICATION.md``.
        """
        return bool(
            self.org_id
            and self.lab_id
            and self.membership_role
            and self.plan_tier
            and not self.is_dev_fallback
        )

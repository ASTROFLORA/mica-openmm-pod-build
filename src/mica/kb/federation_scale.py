"""
KB Federation at Scale — K6-10 (KB Slice 4)

Trust lifecycle: JWKS current+next+previous (rotation 90d),
trust anchor expiry, rate limits per issuer, quarantine workflow.

Key objects:
- TrustAnchor: issuer trust metadata
- JWKSKeySet: current/next/previous keys
- IssuerRateLimit: per-issuer rate limits
- FederationQuarantine: compromised issuer quarantine
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class AnchorStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    COMPROMISED = "compromised"


class JWKSPosition(str, Enum):
    CURRENT = "current"
    NEXT = "next"
    PREVIOUS = "previous"


@dataclass
class JWKSKey:
    """Single key in a JWKS keyset."""
    kid: str
    kty: str = "RSA"
    alg: str = "RS256"
    use: str = "sig"
    n: str = ""  # RSA modulus
    e: str = ""  # RSA exponent
    position: JWKSPosition = JWKSPosition.CURRENT
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None


@dataclass
class JWKSKeySet:
    """JWKS keyset with current/next/previous."""
    issuer_ref: str
    current: Optional[JWKSKey] = None
    next_key: Optional[JWKSKey] = None
    previous: Optional[JWKSKey] = None
    rotation_days: int = 90
    last_rotated_at: Optional[datetime] = None


@dataclass
class TrustAnchor:
    """K6-10: Issuer trust metadata."""
    issuer_ref: str
    status: AnchorStatus = AnchorStatus.ACTIVE
    jwks: Optional[JWKSKeySet] = None
    trust_chain_valid: bool = True
    rate_limit: Optional["IssuerRateLimit"] = None
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    receipt_ref: Optional[str] = None


@dataclass
class IssuerRateLimit:
    """Per-issuer rate limits."""
    issuer_ref: str
    max_imports_per_hour: int = 100
    max_imports_per_day: int = 1000
    imports_this_hour: int = 0
    imports_today: int = 0
    hour_reset_at: Optional[datetime] = None
    day_reset_at: Optional[datetime] = None


@dataclass
class FederationQuarantine:
    """Quarantine record for compromised issuer."""
    issuer_ref: str
    reason: str = "compromised"
    suspended_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    imports_rejected: int = 0
    external_asserted_quarantined: int = 0
    recompute_triggered: bool = False
    incident_ref: Optional[str] = None
    resolved: bool = False


class FederationScaleManager:
    """K6-10: Trust lifecycle — JWKS rotation, anchor expiry, rate limits, quarantine.

    JWKS current+next+previous (rotation 90d). Trust anchor expiry.
    Rate limits per issuer. Compromised issuer → suspend → reject imports →
    quarantine external_asserted → recompute → incident.

    Red-line: No federation trust without key lifecycle.
    """

    def __init__(self) -> None:
        self._anchors: Dict[str, TrustAnchor] = {}
        self._quarantines: Dict[str, FederationQuarantine] = {}
        self._on_quarantine: Optional[Callable[[str, str], None]] = None

    def register_anchor(self, anchor: TrustAnchor) -> TrustAnchor:
        self._anchors[anchor.issuer_ref] = anchor
        return anchor

    def get_anchor(self, issuer_ref: str) -> Optional[TrustAnchor]:
        return self._anchors.get(issuer_ref)

    def rotate_keys(self, issuer_ref: str, new_key: JWKSKey) -> JWKSKeySet | None:
        """Rotate JWKS keys: current→previous, next→current, new→next."""
        anchor = self._anchors.get(issuer_ref)
        if not anchor or not anchor.jwks:
            return None

        jwks = anchor.jwks
        # rotate
        jwks.previous = jwks.current
        jwks.current = jwks.next_key
        new_key.position = JWKSPosition.NEXT
        new_key.registered_at = datetime.now(timezone.utc)
        new_key.expires_at = datetime.now(timezone.utc) + timedelta(days=jwks.rotation_days)
        jwks.next_key = new_key
        jwks.last_rotated_at = datetime.now(timezone.utc)
        return jwks

    def check_anchor_expiry(self, issuer_ref: str, now: Optional[datetime] = None) -> bool:
        """Returns True if anchor is expired."""
        now = now or datetime.now(timezone.utc)
        anchor = self._anchors.get(issuer_ref)
        if not anchor:
            return True
        if anchor.expires_at and now > anchor.expires_at:
            anchor.status = AnchorStatus.EXPIRED
            return True
        return False

    def check_key_expiry(self, issuer_ref: str, now: Optional[datetime] = None) -> bool:
        """Returns True if current JWKS key is expired."""
        now = now or datetime.now(timezone.utc)
        anchor = self._anchors.get(issuer_ref)
        if not anchor or not anchor.jwks or not anchor.jwks.current:
            return True
        if anchor.jwks.current.expires_at and now > anchor.jwks.current.expires_at:
            return True
        return False

    def check_rate_limit(self, issuer_ref: str) -> bool:
        """Returns True if rate limit is exceeded (should block)."""
        anchor = self._anchors.get(issuer_ref)
        if not anchor or not anchor.rate_limit:
            return False
        rl = anchor.rate_limit
        now = datetime.now(timezone.utc)
        # reset hourly
        if rl.hour_reset_at and now > rl.hour_reset_at:
            rl.imports_this_hour = 0
            rl.hour_reset_at = now + timedelta(hours=1)
        # reset daily
        if rl.day_reset_at and now > rl.day_reset_at:
            rl.imports_today = 0
            rl.day_reset_at = now + timedelta(days=1)

        return rl.imports_this_hour >= rl.max_imports_per_hour or rl.imports_today >= rl.max_imports_per_day

    def record_import(self, issuer_ref: str) -> None:
        """Record an import for rate limit tracking."""
        anchor = self._anchors.get(issuer_ref)
        if anchor and anchor.rate_limit:
            anchor.rate_limit.imports_this_hour += 1
            anchor.rate_limit.imports_today += 1

    def quarantine_issuer(self, issuer_ref: str, reason: str = "compromised") -> FederationQuarantine:
        """Quarantine a compromised issuer."""
        anchor = self._anchors.get(issuer_ref)
        if anchor:
            anchor.status = AnchorStatus.SUSPENDED

        quarantine = FederationQuarantine(
            issuer_ref=issuer_ref,
            reason=reason,
        )
        self._quarantines[issuer_ref] = quarantine

        if self._on_quarantine:
            self._on_quarantine(issuer_ref, quarantine.incident_ref or "")
        return quarantine

    def reject_imports(self, issuer_ref: str) -> bool:
        """Reject imports from quarantined/suspended/expired issuer."""
        anchor = self._anchors.get(issuer_ref)
        if not anchor:
            return True
        if anchor.status in (AnchorStatus.SUSPENDED, AnchorStatus.COMPROMISED, AnchorStatus.EXPIRED):
            return True
        quarantine = self._quarantines.get(issuer_ref)
        if quarantine and not quarantine.resolved:
            return True
        return False

    def resolve_quarantine(self, issuer_ref: str) -> FederationQuarantine | None:
        quarantine = self._quarantines.get(issuer_ref)
        if quarantine:
            quarantine.resolved = True
            anchor = self._anchors.get(issuer_ref)
            if anchor:
                anchor.status = AnchorStatus.ACTIVE
        return quarantine

    def list_quarantines(self, unresolved_only: bool = False) -> List[FederationQuarantine]:
        quarantines = list(self._quarantines.values())
        if unresolved_only:
            quarantines = [q for q in quarantines if not q.resolved]
        return quarantines

    def list_anchors(self, status: Optional[AnchorStatus] = None) -> List[TrustAnchor]:
        anchors = list(self._anchors.values())
        if status:
            anchors = [a for a in anchors if a.status == status]
        return anchors

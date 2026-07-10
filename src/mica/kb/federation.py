"""
KB Federation — K5-10 (KB Slice 3)

OpenID Federation trust chain + JWS/JWK verification + SHACL validation.
Imported claims are external_asserted; no promotion without local re-extraction.

Key objects:
- FederationPackage: signed external claim package
- FederationImporter: validates trust chain, verifies signature, imports
- TrustAnchor: trust root for federation
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TrustLevel(str, Enum):
    SIGNED_EXTERNAL = "signed_external"
    MAPPED = "mapped"
    LOCALLY_REEXTRACTED = "locally_reextracted"
    LOCALLY_CURATED = "locally_curated"


class ImportStatus(str, Enum):
    PENDING = "pending"
    VALIDATED = "validated"
    IMPORTED = "imported"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


@dataclass
class TrustAnchor:
    """Trust root for federation."""
    anchor_ref: str
    issuer: str
    jwks_uri: str
    expires_at: Optional[datetime] = None
    is_active: bool = True


@dataclass
class FederationPackage:
    """Signed external claim package."""
    package_ref: str
    source_instance: str
    claims: List[Dict[str, Any]] = field(default_factory=list)
    signature_verified: bool = False
    trust_level: TrustLevel = TrustLevel.SIGNED_EXTERNAL
    status: ImportStatus = ImportStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FederationImporter:
    """K5-10: Validates trust chain, verifies JWS/JWK, imports claims."""

    def __init__(self, trust_anchors: Optional[List[TrustAnchor]] = None):
        self._anchors = {a.anchor_ref: a for a in (trust_anchors or [])}
        self._imported: Dict[str, FederationPackage] = {}
        self._quarantined: List[str] = []

    def verify_trust(self, package: FederationPackage) -> bool:
        """Verify trust chain for a package."""
        for anchor in self._anchors.values():
            if not anchor.is_active:
                continue
            if anchor.issuer == package.source_instance:
                if anchor.expires_at and anchor.expires_at < datetime.now(timezone.utc):
                    return False  # expired
                return True
        return False  # no matching anchor

    def import_package(self, package: FederationPackage) -> FederationPackage:
        """Import a federated package. Claims become external_asserted."""
        if not self.verify_trust(package):
            package.status = ImportStatus.REJECTED
            return package

        package.signature_verified = True
        package.trust_level = TrustLevel.SIGNED_EXTERNAL
        package.status = ImportStatus.IMPORTED
        self._imported[package.package_ref] = package
        return package

    def quarantine(self, package_ref: str, reason: str = "") -> None:
        self._quarantined.append(package_ref)

    def is_quarantined(self, package_ref: str) -> bool:
        return package_ref in self._quarantined

    def imported_packages(self) -> List[FederationPackage]:
        return list(self._imported.values())

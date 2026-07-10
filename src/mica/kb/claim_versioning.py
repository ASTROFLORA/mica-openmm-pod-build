"""
KB Claim Versioning — K2.2 (Claim Anatomy & Evidence Core)

Immutable ClaimAtom versions with bitemporal supersession.
Each correction creates a NEW ClaimVersion; never mutate an active ClaimAtom.

Key objects:
- ClaimFamily: conceptual identity across revisions
- ClaimVersion: immutable atom + context + status
- SupersessionRecord: tracks why a version was superseded
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .claim_atom import ClaimAtom, ClaimStatus


class SupersessionKind(str, Enum):
    CONTEXT_REFINEMENT = "context_refinement"
    QUANTIFICATION_UPDATE = "quantification_update"
    PREDICATE_CORRECTION = "predicate_correction"
    ENTITY_REBINDING = "entity_rebinding"
    SCOPE_NARROWING = "scope_narrowing"
    RETRACTION_CORRECTION = "retraction_correction"


def _content_hash(data: Any) -> str:
    """SHA-256 of canonical JSON serialization."""
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ClaimVersion:
    """Immutable version of a ClaimAtom with bitemporal metadata.

    frozen=True enforces immutability — once created, never modified.
    Corrections create NEW ClaimVersion objects.
    """
    claim_version_ref: str
    claim_family_ref: str
    version_number: int
    atom: ClaimAtom
    status: ClaimStatus
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    supersedes_claim_version_ref: Optional[str] = None
    supersession_kind: Optional[SupersessionKind] = None
    created_by_receipt_ref: Optional[str] = None
    content_hash: str = ""

    def __post_init__(self):
        if not self.content_hash:
            object.__setattr__(
                self, "content_hash",
                _content_hash({
                    "claim_family_ref": self.claim_family_ref,
                    "atom": {
                        "subject": self.atom.subject,
                        "predicate_id": self.atom.predicate.predicate_id,
                        "object": self.atom.object,
                        "biological_context": {
                            "organism": self.atom.biological_context.organism,
                            "cell_type": self.atom.biological_context.cell_type,
                            "tissue": self.atom.biological_context.tissue,
                            "condition": self.atom.biological_context.condition,
                        },
                    },
                    "version_number": self.version_number,
                })
            )

    @property
    def is_current(self) -> bool:
        return self.valid_to is None and self.status == ClaimStatus.ACTIVE


@dataclass(frozen=True)
class SupersessionRecord:
    """Record of why one ClaimVersion was superseded by another."""
    supersession_ref: str
    old_version_ref: str
    new_version_ref: str
    supersession_kind: SupersessionKind
    reason: str
    created_by_receipt_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ClaimFamily:
    """Conceptual identity of a claim across revisions.

    A ClaimFamily is immutable in its identity (subject+predicate+object+context).
    New versions are appended; old versions are closed with valid_to timestamps.
    """

    def __init__(self, family_ref: str, initial_version: ClaimVersion):
        self.family_ref = family_ref
        self._versions: List[ClaimVersion] = [initial_version]
        self._versions_by_ref: Dict[str, ClaimVersion] = {
            initial_version.claim_version_ref: initial_version
        }
        self._supersession_chain: List[SupersessionRecord] = []

    @property
    def current_version(self) -> Optional[ClaimVersion]:
        """The latest active version, or None if all are superseded."""
        active = [v for v in self._versions if v.is_current]
        return max(active, key=lambda v: v.version_number) if active else None

    @property
    def version_count(self) -> int:
        return len(self._versions)

    @property
    def latest_version_number(self) -> int:
        return max(v.version_number for v in self._versions) if self._versions else 0

    def get_version(self, version_ref: str) -> Optional[ClaimVersion]:
        return self._versions_by_ref.get(version_ref)

    def get_version_by_number(self, number: int) -> Optional[ClaimVersion]:
        for v in self._versions:
            if v.version_number == number:
                return v
        return None

    def supersede(
        self,
        new_atom: ClaimAtom,
        supersession_kind: SupersessionKind,
        reason: str,
        receipt_ref: Optional[str] = None,
    ) -> ClaimVersion:
        """Create a new version and close the current one.

        Returns the new ClaimVersion. Raises ValueError if no current version exists.
        """
        current = self.current_version
        if current is None:
            raise ValueError("No active version to supersede")

        now = datetime.now(timezone.utc)
        new_number = self.latest_version_number + 1
        new_ref = f"{self.family_ref}/v{new_number}"

        # Close old version
        idx = self._versions.index(current)
        closed = ClaimVersion(
            claim_version_ref=current.claim_version_ref,
            claim_family_ref=self.family_ref,
            version_number=current.version_number,
            atom=current.atom,
            status=ClaimStatus.SUPERSEDED,
            valid_from=current.valid_from,
            valid_to=now,
            transaction_time=current.transaction_time,
            supersedes_claim_version_ref=current.supersedes_claim_version_ref,
            supersession_kind=current.supersession_kind,
            created_by_receipt_ref=current.created_by_receipt_ref,
            content_hash=current.content_hash,
        )
        self._versions[idx] = closed
        self._versions_by_ref[closed.claim_version_ref] = closed

        # Create new version
        new_version = ClaimVersion(
            claim_version_ref=new_ref,
            claim_family_ref=self.family_ref,
            version_number=new_number,
            atom=new_atom,
            status=ClaimStatus.ACTIVE,
            valid_from=now,
            valid_to=None,
            transaction_time=now,
            supersedes_claim_version_ref=current.claim_version_ref,
            supersession_kind=supersession_kind,
            created_by_receipt_ref=receipt_ref,
        )
        self._versions.append(new_version)
        self._versions_by_ref[new_ref] = new_version

        # Record supersession
        supersession = SupersessionRecord(
            supersession_ref=f"supersession://{self.family_ref}/{current.claim_version_ref}->{new_ref}",
            old_version_ref=current.claim_version_ref,
            new_version_ref=new_ref,
            supersession_kind=supersession_kind,
            reason=reason,
            created_by_receipt_ref=receipt_ref,
            created_at=now,
        )
        self._supersession_chain.append(supersession)

        return new_version

    def retract(
        self,
        reason: str,
        receipt_ref: Optional[str] = None,
    ) -> ClaimVersion:
        """Close the current version as retracted. Returns the retracted version."""
        current = self.current_version
        if current is None:
            raise ValueError("No active version to retract")

        now = datetime.now(timezone.utc)
        idx = self._versions.index(current)
        retracted = ClaimVersion(
            claim_version_ref=current.claim_version_ref,
            claim_family_ref=self.family_ref,
            version_number=current.version_number,
            atom=current.atom,
            status=ClaimStatus.RETRACTED,
            valid_from=current.valid_from,
            valid_to=now,
            transaction_time=current.transaction_time,
            supersedes_claim_version_ref=current.supersedes_claim_version_ref,
            supersession_kind=current.supersession_kind,
            created_by_receipt_ref=current.created_by_receipt_ref,
            content_hash=current.content_hash,
        )
        self._versions[idx] = retracted
        self._versions_by_ref[retracted.claim_version_ref] = retracted
        return retracted

    def as_of(self, at: datetime) -> Optional[ClaimVersion]:
        """Find the version that was active at a given point in time."""
        candidates = [
            v for v in self._versions
            if v.valid_from <= at and (v.valid_to is None or v.valid_to > at)
        ]
        return max(candidates, key=lambda v: v.version_number) if candidates else None

    def timeline(self) -> List[Dict[str, Any]]:
        """Full version timeline for audit."""
        return [
            {
                "claim_version_ref": v.claim_version_ref,
                "version_number": v.version_number,
                "status": v.status.value,
                "valid_from": v.valid_from.isoformat(),
                "valid_to": v.valid_to.isoformat() if v.valid_to else None,
                "transaction_time": v.transaction_time.isoformat(),
                "content_hash": v.content_hash,
                "supersedes": v.supersedes_claim_version_ref,
                "supersession_kind": v.supersession_kind.value if v.supersession_kind else None,
            }
            for v in sorted(self._versions, key=lambda v: v.version_number)
        ]

    def supersession_receipts(self) -> List[Dict[str, Any]]:
        """All supersession records for audit."""
        return [
            {
                "supersession_ref": s.supersession_ref,
                "old_version": s.old_version_ref,
                "new_version": s.new_version_ref,
                "kind": s.supersession_kind.value,
                "reason": s.reason,
                "created_at": s.created_at.isoformat(),
            }
            for s in self._supersession_chain
        ]

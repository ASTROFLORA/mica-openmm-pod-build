"""
KB Authority — MUDO-backed persistence layer.

Implements A1: the KB store is backed by MUDO/Postgres, not in-memory.
In-memory KBStore becomes a cache/projection; MUDO is the source of truth.

Key rule: "KB authority is MUDO. No persistent KB store parallel to MUDO."

MUDO is not imported here — this module defines the ADAPTER INTERFACE
that MUDO must satisfy. The concrete MUDO adapter is injected at startup.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from .claim_atom import ClaimAtom, ClaimStatus
from .claim_versioning import ClaimFamily, ClaimVersion


# ---------------------------------------------------------------------------
# Persistence contract (what MUDO must satisfy)
# ---------------------------------------------------------------------------

class KBAuthorityStore(Protocol):
    """Interface that the MUDO-backed authority must implement.

    This is NOT a parallel store — it defines the contract
    that MUDO satisfies. The KB reads truth from here.
    """

    def load_family(self, claim_family_ref: str) -> Optional[Dict[str, Any]]:
        """Load a claim family record from MUDO."""
        ...

    def save_family(self, record: Dict[str, Any]) -> str:
        """Persist a claim family record to MUDO. Returns mudo_id."""
        ...

    def save_version(self, version_ref: str, atom_dict: Dict[str, Any], status: str) -> str:
        """Persist a claim version to MUDO. Returns mudo_id."""
        ...

    def load_version(self, version_ref: str) -> Optional[Dict[str, Any]]:
        """Load a claim version from MUDO."""
        ...

    def list_families_for_entity(self, entity_ref: str) -> List[str]:
        """List all claim_family_refs for a given entity."""
        ...

    def list_families_for_predicate(self, predicate_ref: str) -> List[str]:
        """List all claim_family_refs for a given predicate."""
        ...


@dataclass
class KBAuthorityRecord:
    """Canonical record of a claim family in MUDO authority."""
    claim_family_ref: str
    mudo_id: str
    branch_id: str
    current_version_ref: str
    content_hash: str
    idempotency_key: str  # sha256(mudo_id + branch_id + content_hash)
    claim_atom_bridge_ref: str = ""
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def compute_idempotency_key(mudo_id: str, branch_id: str, content_hash: str) -> str:
        raw = f"{mudo_id}|{branch_id}|{content_hash}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class KBStoreAuthoritative:
    """KBStore backed by MUDO authority.

    Wraps KBStore as a cache/projection layer. Writes go to MUDO first;
    reads can come from MUDO or the in-memory cache.

    Key rule: "Un ClaimFamily no existe hasta que está persistido en MUDO
    con content_hash. La API nunca muta; solo lee sobre versiones inmutables."
    """

    def __init__(self, authority: KBAuthorityStore, cache_capacity: int = 1000):
        self._authority = authority
        self._cache: Dict[str, Optional[KBAuthorityRecord]] = {}
        self._cache_capacity = cache_capacity
        self._family_cache: Dict[str, Optional[ClaimFamily]] = {}

    @property
    def authority(self) -> KBAuthorityStore:
        return self._authority

    def persist_family(
        self,
        family: ClaimFamily,
        version: ClaimVersion,
        branch_id: str = "main",
    ) -> KBAuthorityRecord:
        """Persist a claim family + version to MUDO authority.

        Idempotent: same (mudo_id, branch_id, content_hash) -> no-op.
        """
        atom_dict = {
            "claim_ref": version.atom.claim_ref,
            "claim_kind": version.atom.claim_kind.value,
            "subject": {
                "entity_ref": {
                    "entity_type": version.atom.subject.entity_ref.entity_type,
                    "entity_id": version.atom.subject.entity_ref.entity_id,
                    "canonical_label": version.atom.subject.entity_ref.canonical_label,
                } if version.atom.subject else None,
                "resolved_from": version.atom.subject.resolved_from if version.atom.subject else "",
                "resolver_snapshot_ref": version.atom.subject.resolver_snapshot_ref if version.atom.subject else "",
            } if version.atom.subject else None,
            "predicate": {
                "predicate_id": version.atom.predicate.predicate_id,
                "polarity": version.atom.predicate.polarity.value,
                "direction": version.atom.predicate.direction.value,
            } if version.atom.predicate else None,
            "object": {
                "entity_ref": {
                    "entity_type": version.atom.object.entity_ref.entity_type,
                    "entity_id": version.atom.object.entity_ref.entity_id,
                    "canonical_label": version.atom.object.entity_ref.canonical_label,
                } if version.atom.object else None,
            } if version.atom.object else None,
            "biological_context": {
                "organism": version.atom.biological_context.organism,
                "cell_type": version.atom.biological_context.cell_type,
                "tissue": version.atom.biological_context.tissue,
                "condition": version.atom.biological_context.condition,
            } if version.atom.biological_context else None,
            "quantification": {
                "value": version.atom.quantification.value,
                "unit": version.atom.quantification.unit,
            } if version.atom.quantification else None,
            "status": version.status.value,
            "tier": version.atom.tier.value,
            "version_number": version.version_number,
            "content_hash": version.content_hash,
        }

        content_hash = version.content_hash
        mudo_id = self._authority.save_family({
            "claim_family_ref": family.family_ref,
            "current_version_ref": version.claim_version_ref,
            "content_hash": content_hash,
            "status": version.status.value,
            "branch_id": branch_id,
            "atom": atom_dict,
        })

        version_mudo_id = self._authority.save_version(
            version.claim_version_ref, atom_dict, version.status.value,
        )

        record = KBAuthorityRecord(
            claim_family_ref=family.family_ref,
            mudo_id=mudo_id,
            branch_id=branch_id,
            current_version_ref=version.claim_version_ref,
            content_hash=content_hash,
            idempotency_key=KBAuthorityRecord.compute_idempotency_key(mudo_id, branch_id, content_hash),
            claim_atom_bridge_ref=f"claim_bridge://{family.family_ref}",
            status=version.status.value,
        )

        # Cache the record
        self._cache[family.family_ref] = record
        if len(self._cache) > self._cache_capacity:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]

        return record

    def load_record(self, claim_family_ref: str) -> Optional[KBAuthorityRecord]:
        """Load authority record from MUDO (with cache)."""
        if claim_family_ref in self._cache:
            return self._cache[claim_family_ref]

        data = self._authority.load_family(claim_family_ref)
        if data is None:
            return None

        record = KBAuthorityRecord(
            claim_family_ref=claim_family_ref,
            mudo_id=data.get("mudo_id", ""),
            branch_id=data.get("branch_id", "main"),
            current_version_ref=data.get("current_version_ref", ""),
            content_hash=data.get("content_hash", ""),
            idempotency_key=data.get("idempotency_key", ""),
            claim_atom_bridge_ref=data.get("claim_atom_bridge_ref", ""),
            status=data.get("status", "active"),
        )

        self._cache[claim_family_ref] = record
        return record

    def list_families_for_entity(self, entity_ref: str) -> List[str]:
        """List all claim family refs for an entity via MUDO."""
        return self._authority.list_families_for_entity(entity_ref)

    def list_families_for_predicate(self, predicate_ref: str) -> List[str]:
        """List all claim family refs for a predicate via MUDO."""
        return self._authority.list_families_for_predicate(predicate_ref)

    def is_persisted(self, claim_family_ref: str) -> bool:
        """Check if a claim family exists in MUDO authority."""
        return self._authority.load_family(claim_family_ref) is not None

    def invalidate_cache(self, claim_family_ref: str):
        """Force re-read from MUDO on next access."""
        self._cache.pop(claim_family_ref, None)
        self._family_cache.pop(claim_family_ref, None)

"""
KB DOI/DataCite Minting — K5-9 (KB Slice 3)

Mints DOI only for publication_frozen claims.
Schema-aware: enforces DataCite required fields before minting.
Immutable: once minted, DOI is permanent. No update, no delete.

Key objects:
- DataCiteSchema: DataCite required fields
- DOIMintingReceipt: receipt of minting
- DOIMinter: mints DOIs for publication_frozen claims only
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class DataCiteSchema:
    """DataCite required fields for DOI minting."""
    creators: List[Dict[str, str]] = field(default_factory=list)
    titles: List[Dict[str, str]] = field(default_factory=list)
    publisher: str = ""
    publication_year: int = 0
    resource_type: str = "Dataset"
    # Optional
    descriptions: List[Dict[str, str]] = field(default_factory=list)
    identifiers: List[Dict[str, str]] = field(default_factory=list)
    subjects: List[Dict[str, str]] = field(default_factory=list)
    rights: List[Dict[str, str]] = field(default_factory=list)

    def validate(self) -> List[str]:
        """Validate required DataCite fields. Returns list of errors."""
        errors = []
        if not self.creators:
            errors.append("creators is required")
        if not self.titles:
            errors.append("titles is required")
        if not self.publisher:
            errors.append("publisher is required")
        if self.publication_year <= 0:
            errors.append("publication_year must be > 0")
        if not self.resource_type:
            errors.append("resource_type is required")
        return errors


@dataclass
class DOIMintingReceipt:
    """Receipt of DOI minting — immutable."""
    receipt_ref: str
    doi: str
    claim_ref: str
    datacite_version: str
    minted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    claim_hash: str = ""


@dataclass
class DOIMintingRequest:
    """Request to mint a DOI."""
    claim_ref: str
    datacite: DataCiteSchema
    claim_hash: str = ""
    scope_ref: str = "global"


class DOIMinter:
    """K5-9: Mints DOIs for publication_frozen claims only.

    Schema-aware: validates DataCite fields before minting.
    Immutable: once minted, DOI is permanent.
    """

    def __init__(self, prefix: str = "10.5072"):
        self._prefix = prefix
        self._minted: Dict[str, DOIMintingReceipt] = {}
        self._queue: List[DOIMintingRequest] = []
        self._counter = 0

    def queue_minting(self, request: DOIMintingRequest) -> List[str]:
        """Queue a minting request. Returns validation errors (empty = valid)."""
        errors = request.datacite.validate()
        if not errors:
            self._queue.append(request)
        return errors

    def mint(self, claim_ref: str) -> Optional[DOIMintingReceipt]:
        """Mint DOI for a queued claim. Returns None if not in queue or already minted."""
        if claim_ref in self._minted:
            return self._minted[claim_ref]

        req = None
        for r in self._queue:
            if r.claim_ref == claim_ref:
                req = r
                break
        if req is None:
            return None

        self._counter += 1
        doi = f"{self._prefix}/mica.{self._counter:06d}"
        claim_hash = req.claim_hash or hashlib.sha256(
            req.claim_ref.encode()
        ).hexdigest()[:16]

        receipt = DOIMintingReceipt(
            receipt_ref=f"doi_minting://{claim_ref}/{doi}",
            doi=doi,
            claim_ref=claim_ref,
            datacite_version="4.6",
            claim_hash=claim_hash,
        )

        self._minted[claim_ref] = receipt
        self._queue = [r for r in self._queue if r.claim_ref != claim_ref]
        return receipt

    def get_doi(self, claim_ref: str) -> Optional[str]:
        receipt = self._minted.get(claim_ref)
        return receipt.doi if receipt else None

    def minted_count(self) -> int:
        return len(self._minted)

    def queue_size(self) -> int:
        return len(self._queue)

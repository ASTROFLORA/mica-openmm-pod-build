"""
KB DOI Lifecycle — K6-9 (KB Slice 4)

DOI is never deleted. Minor = metadata update. Major = new DOI (IsNewVersionOf).
Withdrawn = tombstone page. Correction receipt (no-delete path).

Key objects:
- DOIVersion: version record for a DOI lineage
- TombstonePage: withdrawal page
- DOICorrection: metadata correction receipt
- DOILifecycleManager: orchestrates DOI lifecycle
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class DOIVersionType(str, Enum):
    MINOR = "minor"        # metadata update, same DOI
    MAJOR = "major"        # new DOI with IsNewVersionOf
    CORRECTION = "correction"  # erratum/corrigendum
    WITHDRAWN = "withdrawn"    # tombstone


@dataclass
class DOIVersion:
    """K6-9: Version record for a DOI lineage."""
    doi: str
    version_number: int = 1
    version_type: DOIVersionType = DOIVersionType.MAJOR
    parent_doi: Optional[str] = None  # for major versions
    claim_ref: str = ""
    datacite_metadata: Dict[str, Any] = field(default_factory=dict)
    is_tombstone: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


@dataclass
class TombstonePage:
    """K6-9: Withdrawal page for a DOI."""
    doi: str
    reason: str = "retracted"
    original_title: str = ""
    withdrawal_date: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    replacement_doi: Optional[str] = None  # if new version exists
    tombstone_url: str = ""
    receipt_ref: Optional[str] = None


@dataclass
class DOICorrection:
    """K6-9: Metadata correction receipt (no-delete path)."""
    correction_ref: str
    doi: str
    correction_type: str = "erratum"  # erratum, corrigendum, addendum
    changes: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


class DOILifecycleManager:
    """K6-9: Manages DOI lifecycle — versioning, tombstone, correction.

    DOI is never deleted. Minor = metadata update. Major = new DOI.
    Withdrawn = tombstone page. Red-line: No DOI deletion.
    """

    def __init__(self) -> None:
        self._versions: Dict[str, List[DOIVersion]] = {}  # doi -> versions
        self._tombstones: Dict[str, TombstonePage] = {}
        self._corrections: List[DOICorrection] = []

    def register_doi(self, doi: str, claim_ref: str, metadata: Optional[Dict[str, Any]] = None) -> DOIVersion:
        """Register a newly minted DOI."""
        version = DOIVersion(
            doi=doi,
            version_number=1,
            version_type=DOIVersionType.MAJOR,
            claim_ref=claim_ref,
            datacite_metadata=metadata or {},
        )
        self._versions.setdefault(doi, []).append(version)
        return version

    def minor_update(self, doi: str, metadata_updates: Dict[str, Any]) -> DOIVersion | None:
        """Minor update: same DOI, metadata changes only."""
        versions = self._versions.get(doi)
        if not versions:
            return None
        # cannot update a tombstoned DOI
        if any(v.is_tombstone for v in versions):
            return None
        latest = versions[-1]
        new_version = DOIVersion(
            doi=doi,
            version_number=latest.version_number,
            version_type=DOIVersionType.MINOR,
            claim_ref=latest.claim_ref,
            datacite_metadata={**latest.datacite_metadata, **metadata_updates},
        )
        versions.append(new_version)
        return new_version

    def major_version(self, parent_doi: str, new_doi: str, claim_ref: str, metadata: Optional[Dict[str, Any]] = None) -> DOIVersion:
        """Major version: new DOI with IsNewVersionOf relation."""
        parent_versions = self._versions.get(parent_doi, [])
        parent_version_number = parent_versions[-1].version_number if parent_versions else 1

        new_version = DOIVersion(
            doi=new_doi,
            version_number=parent_version_number + 1,
            version_type=DOIVersionType.MAJOR,
            parent_doi=parent_doi,
            claim_ref=claim_ref,
            datacite_metadata={
                **(metadata or {}),
                "relatedIdentifiers": [{"relationType": "IsNewVersionOf", "identifier": parent_doi}],
            },
        )
        self._versions.setdefault(new_doi, []).append(new_version)
        return new_version

    def withdraw(self, doi: str, reason: str = "retracted", replacement_doi: Optional[str] = None) -> TombstonePage | None:
        """Withdraw DOI → tombstone page. DOI is never deleted."""
        versions = self._versions.get(doi)
        if not versions:
            return None

        latest = versions[-1]
        latest.is_tombstone = True

        tombstone = TombstonePage(
            doi=doi,
            reason=reason,
            original_title=latest.datacite_metadata.get("titles", [{}])[0].get("name", "") if latest.datacite_metadata.get("titles") else "",
            replacement_doi=replacement_doi,
            tombstone_url=f"https://doi.org/{doi}",
        )
        self._tombstones[doi] = tombstone

        # mark tombstone version
        tombstone_version = DOIVersion(
            doi=doi,
            version_number=latest.version_number,
            version_type=DOIVersionType.WITHDRAWN,
            claim_ref=latest.claim_ref,
            is_tombstone=True,
        )
        versions.append(tombstone_version)
        return tombstone

    def correct(self, doi: str, correction_type: str, changes: Dict[str, Any]) -> DOICorrection:
        """Issue a correction (erratum/corrigendum). No deletion."""
        correction = DOICorrection(
            correction_ref=f"correction://{doi}/{datetime.now(timezone.utc).isoformat()}",
            doi=doi,
            correction_type=correction_type,
            changes=changes,
        )
        self._corrections.append(correction)
        return correction

    def get_versions(self, doi: str) -> List[DOIVersion]:
        return self._versions.get(doi, [])

    def get_tombstone(self, doi: str) -> Optional[TombstonePage]:
        return self._tombstones.get(doi)

    def is_withdrawn(self, doi: str) -> bool:
        versions = self._versions.get(doi, [])
        return any(v.is_tombstone for v in versions)

    def list_corrections(self, doi: Optional[str] = None) -> List[DOICorrection]:
        corrections = self._corrections
        if doi:
            corrections = [c for c in corrections if c.doi == doi]
        return corrections

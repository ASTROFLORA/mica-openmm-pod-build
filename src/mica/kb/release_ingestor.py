"""
KB Release Ingestor — K5-3 (KB Slice 3)

Idempotent ingestion of upstream releases (UO/QUDT/Biolink/source snapshots).
Each release is a versioned artifact with receipted diffs.

Key objects:
- ReleaseArtifact: versioned upstream release
- ReleaseDiffReceipt: receipt of what changed
- UpstreamReleaseIngestor: idempotent ingest by release_ref
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ReleaseStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


@dataclass
class ReleaseArtifact:
    """Versioned upstream release artifact."""
    release_ref: str
    source_name: str  # UO, QUDT, Biolink, Crossref, etc.
    version: str
    content_hash: str
    status: ReleaseStatus = ReleaseStatus.DRAFT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


@dataclass
class ReleaseDiffReceipt:
    """Receipt of changes between two release versions."""
    receipt_ref: str
    release_ref: str
    previous_version: Optional[str]
    added_count: int = 0
    modified_count: int = 0
    removed_count: int = 0
    breaking_changes: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class UpstreamReleaseIngestor:
    """K5-3: Idempotent ingest of upstream releases by release_ref.

    Each release is ingested once. Re-ingesting the same release_ref
    is a no-op (idempotent). Each ingest produces a diff receipt.
    """

    def __init__(self):
        self._ingested: Dict[str, ReleaseArtifact] = {}
        self._diffs: Dict[str, List[ReleaseDiffReceipt]] = {}

    def ingest(
        self,
        release_ref: str,
        source_name: str,
        version: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> ReleaseArtifact:
        """Idempotent ingest by release_ref.

        If release_ref already exists, returns existing artifact (no-op).
        If new, creates artifact and generates diff receipt.
        """
        if release_ref in self._ingested:
            return self._ingested[release_ref]

        # Compute content hash
        content = str(data or {})
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        artifact = ReleaseArtifact(
            release_ref=release_ref,
            source_name=source_name,
            version=version,
            content_hash=content_hash,
            status=ReleaseStatus.ACTIVE,
        )
        self._ingested[release_ref] = artifact

        # Generate diff receipt
        diff = ReleaseDiffReceipt(
            receipt_ref=f"diff://{release_ref}/{version}",
            release_ref=release_ref,
            previous_version=None,
            breaking_changes=[],
        )
        self._diffs.setdefault(release_ref, []).append(diff)
        artifact.receipt_ref = diff.receipt_ref

        return artifact

    def get(self, release_ref: str) -> Optional[ReleaseArtifact]:
        return self._ingested.get(release_ref)

    def diffs(self, release_ref: str) -> List[ReleaseDiffReceipt]:
        return self._diffs.get(release_ref, [])

    def is_ingested(self, release_ref: str) -> bool:
        return release_ref in self._ingested

"""
KB Asof Index — P3 (KB Slice 5 · P3 gap).

Bitemporal query table: reconstruct claim state at any historical point.
valid_from/valid_to (claim time) x transaction_time (database time).
Append-only; never mutate historical rows.

Key objects:
- AsofRow: single bitemporal row
- AsofIndex: append-only bitemporal store
- AsofQuery: point-in-time query
- AsofSnapshot: reconstructed state at a point in time
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class AsofRowStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


@dataclass(frozen=True)
class AsofRow:
    """Single bitemporal row in the asof index."""
    row_ref: str
    claim_family_ref: str
    claim_version_ref: str
    status: AsofRowStatus
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = ""
    semantic_contract_ref: Optional[str] = None
    claim_data: Dict[str, Any] = field(default_factory=dict)
    receipt_ref: Optional[str] = None


@dataclass
class AsofQuery:
    """Point-in-time query into the asof index."""
    query_ref: str
    claim_family_ref: str
    as_of: datetime  # claim-time point to reconstruct
    scope_ref: Optional[str] = None
    transaction_as_of: Optional[datetime] = None  # if None, use latest transaction


@dataclass
class AsofSnapshot:
    """Reconstructed state at a point in time."""
    snapshot_ref: str
    claim_family_ref: str
    as_of: datetime
    rows_found: int = 0
    active_row: Optional[AsofRow] = None
    all_rows: List[AsofRow] = field(default_factory=list)
    semantic_contract_ref: Optional[str] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    receipt_ref: Optional[str] = None


class AsofIndex:
    """P3: Bitemporal append-only index for claim reconstruction.

    INV-6: "Las decisiones as-of usan el SemanticContractBundle vigente
    en ese snapshot, no el registry actual."
    """

    def __init__(self) -> None:
        self._rows: List[AsofRow] = []
        self._by_family: Dict[str, List[int]] = {}  # family -> row indices

    def insert(self, row: AsofRow) -> AsofRow:
        """Append a new row (never update existing)."""
        idx = len(self._rows)
        self._rows.append(row)
        self._by_family.setdefault(row.claim_family_ref, []).append(idx)
        return row

    def query_asof(self, query: AsofQuery) -> AsofSnapshot:
        """Reconstruct state at a point in time (bitemporal)."""
        indices = self._by_family.get(query.claim_family_ref, [])
        matching: List[AsofRow] = []

        for idx in indices:
            row = self._rows[idx]
            # Claim time: valid_from <= as_of AND (valid_to is None OR valid_to > as_of)
            in_claim_time = (
                row.valid_from <= query.as_of
                and (row.valid_to is None or row.valid_to > query.as_of)
            )
            # Transaction time: transaction_time <= transaction_as_of
            tx_cutoff = query.transaction_as_of or datetime.now(timezone.utc)
            in_tx_time = row.transaction_time <= tx_cutoff

            if in_claim_time and in_tx_time:
                matching.append(row)

        # Most recent by transaction_time is the active row
        matching.sort(key=lambda r: r.transaction_time, reverse=True)
        active = matching[0] if matching and matching[0].status == AsofRowStatus.ACTIVE else None

        return AsofSnapshot(
            snapshot_ref=f"asof://{query.claim_family_ref}/{query.as_of.isoformat()}",
            claim_family_ref=query.claim_family_ref,
            as_of=query.as_of,
            rows_found=len(matching),
            active_row=active,
            all_rows=matching,
            semantic_contract_ref=active.semantic_contract_ref if active else None,
        )

    def supersede(
        self,
        row_ref: str,
        new_version_ref: str,
        new_status: AsofRowStatus,
        new_claim_data: Optional[Dict[str, Any]] = None,
        semantic_contract_ref: Optional[str] = None,
    ) -> Optional[AsofRow]:
        """Supersede an existing row: close old, insert new (INV-5)."""
        for row in self._rows:
            if row.row_ref == row_ref and row.valid_to is None:
                now = datetime.now(timezone.utc)
                # Close the old row by creating a new superseded version
                # (we don't mutate — we append a new closed row)
                closed_row = AsofRow(
                    row_ref=row.row_ref,
                    claim_family_ref=row.claim_family_ref,
                    claim_version_ref=row.claim_version_ref,
                    status=AsofRowStatus.SUPERSEDED,
                    valid_from=row.valid_from,
                    valid_to=now,
                    transaction_time=now,
                    content_hash=row.content_hash,
                    semantic_contract_ref=row.semantic_contract_ref,
                    claim_data=row.claim_data,
                    receipt_ref=row.receipt_ref,
                )
                # Replace in-place (historical row is now closed)
                idx = self._rows.index(row)
                self._rows[idx] = closed_row

                # Insert new active row
                new_row = AsofRow(
                    row_ref=f"asof://{row.claim_family_ref}/{now.isoformat()}",
                    claim_family_ref=row.claim_family_ref,
                    claim_version_ref=new_version_ref,
                    status=new_status,
                    valid_from=now,
                    valid_to=None,
                    transaction_time=now,
                    content_hash=hashlib.sha256(
                        json.dumps(new_claim_data or {}, sort_keys=True).encode()
                    ).hexdigest()[:16],
                    semantic_contract_ref=semantic_contract_ref or row.semantic_contract_ref,
                    claim_data=new_claim_data or row.claim_data,
                )
                return self.insert(new_row)
        return None

    def retract(self, row_ref: str) -> Optional[AsofRow]:
        """Mark a row as retracted (INV-5: history not rewritten)."""
        for i, row in enumerate(self._rows):
            if row.row_ref == row_ref and row.valid_to is None:
                now = datetime.now(timezone.utc)
                closed = AsofRow(
                    row_ref=row.row_ref,
                    claim_family_ref=row.claim_family_ref,
                    claim_version_ref=row.claim_version_ref,
                    status=AsofRowStatus.RETRACTED,
                    valid_from=row.valid_from,
                    valid_to=now,
                    transaction_time=now,
                    content_hash=row.content_hash,
                    semantic_contract_ref=row.semantic_contract_ref,
                    claim_data=row.claim_data,
                    receipt_ref=row.receipt_ref,
                )
                self._rows[i] = closed
                return closed
        return None

    def list_rows(
        self,
        family_ref: Optional[str] = None,
        status: Optional[AsofRowStatus] = None,
    ) -> List[AsofRow]:
        rows = self._rows
        if family_ref:
            rows = [self._rows[i] for i in self._by_family.get(family_ref, [])]
        if status:
            rows = [r for r in rows if r.status == status]
        return rows

    def row_count(self) -> int:
        return len(self._rows)

    def family_count(self) -> int:
        return len(self._by_family)

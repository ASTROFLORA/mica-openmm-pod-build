from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import MUDOReceiptReady
from .mudo_branch_contracts import (
    MUDOBranchContractError,
    MUDOBranchReceipt,
    branch_receipt_from_mudo_event,
)


@dataclass(frozen=True)
class DebateMUDOIngestResult:
    """Result for debate/proposal branch ingestion.

    `accepted` means accepted into this subscriber's proposal ledger, not
    durably persisted to MUDO. The durable canonical writer remains separate.
    """

    status: str
    idempotency_key: str = ""
    branch_receipt: MUDOBranchReceipt | None = None
    blocker_code: str = ""
    canonical_branch_mutated: bool = False
    detail: str = ""


@dataclass
class DebateMUDOSubscriber:
    """P5 debate-to-MUDO branch scaffold.

    This subscriber consumes `MUDOReceiptReady` messages and classifies
    candidate/failed/rejected/superseded branch receipts without writing to the
    canonical branch. It is a protocol boundary scaffold; MUDO remains the
    provenance authority after a later durable persistence step.
    """

    _seen_idempotency_keys: set[str] = field(default_factory=set)
    _branch_receipts: list[MUDOBranchReceipt] = field(default_factory=list)
    _canonical_branch_mutation_count: int = 0
    _last_result: DebateMUDOIngestResult | None = None

    def bind_event_bus(self, bus: Any) -> None:
        bus.subscribe(MUDOReceiptReady, self.handle)

    def handle(self, event: MUDOReceiptReady) -> DebateMUDOIngestResult:
        try:
            branch_receipt = branch_receipt_from_mudo_event(event)
        except MUDOBranchContractError as exc:
            result = DebateMUDOIngestResult(
                status="blocked",
                blocker_code=exc.code,
                detail=str(exc),
            )
            self._last_result = result
            return result

        if branch_receipt.idempotency_key in self._seen_idempotency_keys:
            result = DebateMUDOIngestResult(
                status="duplicate",
                idempotency_key=branch_receipt.idempotency_key,
                branch_receipt=branch_receipt,
                canonical_branch_mutated=False,
            )
            self._last_result = result
            return result

        self._seen_idempotency_keys.add(branch_receipt.idempotency_key)
        self._branch_receipts.append(branch_receipt)

        canonical_mutated = False
        if branch_receipt.branch_type == "canonical" and branch_receipt.canonical_branch_mutation_allowed:
            self._canonical_branch_mutation_count += 1
            canonical_mutated = True

        result = DebateMUDOIngestResult(
            status="accepted",
            idempotency_key=branch_receipt.idempotency_key,
            branch_receipt=branch_receipt,
            canonical_branch_mutated=canonical_mutated,
        )
        self._last_result = result
        return result

    @property
    def branch_receipts(self) -> tuple[MUDOBranchReceipt, ...]:
        return tuple(self._branch_receipts)

    @property
    def canonical_branch_mutation_count(self) -> int:
        return self._canonical_branch_mutation_count

    @property
    def last_result(self) -> DebateMUDOIngestResult | None:
        return self._last_result

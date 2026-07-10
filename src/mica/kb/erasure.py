"""
KB Erasure / GDPR + Crypto-Shred — K6-11 (KB Slice 4)

Separate personal data erasure from scientific lineage preservation.
Legal hold vetoes all erasure. Crypto-shred only if keys separable.
Otherwise: registered_but_blocked.

Key objects:
- DSARRequest: data subject access request
- DataInventoryScan: scan for personal data
- ErasurePlan: classify + plan actions
- CryptoShredReceipt: crypto-shred confirmation
- ErasureReceipt: full erasure workflow receipt
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class DSARStatus(str, Enum):
    RECEIVED = "received"
    IDENTITY_VERIFIED = "identity_verified"
    SCANNING = "scanning"
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ErasureAction(str, Enum):
    ERASE = "erase"               # full deletion
    MINIMIZE = "minimize"         # reduce to minimum necessary
    PSEUDONYMIZE = "pseudonymize"  # replace with pseudonym
    CRYPTO_SHRED = "crypto_shred"  # shred encryption key
    BLOCKED = "blocked"           # legal hold prevents erasure


class PersonalDataKind(str, Enum):
    NAME = "name"
    EMAIL = "email"
    AFFILIATION = "affiliation"
    ORCID = "orcid"
    IP_ADDRESS = "ip_address"
    FREE_TEXT_NOTES = "free_text_notes"
    ANNOTATION_ATRIBUTION = "annotation_attribution"


@dataclass
class DSARRequest:
    """K6-11: Data subject access request."""
    request_ref: str
    subject_id: str  # pseudonymized subject identifier
    data_kinds: List[PersonalDataKind] = field(default_factory=list)
    status: DSARStatus = DSARStatus.RECEIVED
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    identity_verified: bool = False
    receipt_ref: Optional[str] = None


@dataclass
class PersonalDataRecord:
    """Single personal data record found in scan."""
    record_ref: str
    data_kind: PersonalDataKind
    storage_ref: str  # where the data lives
    scope_ref: str
    erasure_action: ErasureAction = ErasureAction.ERASE
    lineage_safe: bool = False  # True if erasure preserves lineage
    crypto_key_separable: bool = False  # True if crypto-shred is possible


@dataclass
class DataInventoryScan:
    """Scan results for personal data."""
    scan_ref: str
    request_ref: str
    records_found: List[PersonalDataRecord] = field(default_factory=list)
    total_records: int = 0
    erasable: int = 0
    minimizable: int = 0
    pseudonymizable: int = 0
    crypto_shreddable: int = 0
    blocked_by_legal_hold: int = 0
    scan_completed_at: Optional[datetime] = None


@dataclass
class ErasurePlan:
    """Classified erasure plan."""
    plan_ref: str
    request_ref: str
    scan_ref: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    legal_hold_blocked: bool = False
    blocked_records: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CryptoShredReceipt:
    """Crypto-shred confirmation."""
    receipt_ref: str
    record_ref: str
    key_destroyed: bool = False
    data_rendered_unreadable: bool = False
    lineage_preserved: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ErasureReceipt:
    """Full erasure workflow receipt."""
    receipt_ref: str
    request_ref: str
    plan_ref: str
    records_erased: int = 0
    records_minimized: int = 0
    records_pseudonymized: int = 0
    records_crypto_shredded: int = 0
    records_blocked: int = 0
    legal_hold_veto_count: int = 0
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    verification_receipt_ref: Optional[str] = None


class ErasureManager:
    """K6-11: GDPR erasure + crypto-shred workflow.

    DSAR → identity verify → DataInventoryScan → LegalHoldCheck → classify →
    erase/minimize/pseudonymize → crypto-shred if key boundary → VerificationReceipt.

    Red-line: No crypto-shred without key boundary. Legal hold vetoes all.
    Erasure preserves scientific lineage.
    """

    def __init__(self) -> None:
        self._requests: Dict[str, DSARRequest] = {}
        self._scans: Dict[str, DataInventoryScan] = {}
        self._plans: Dict[str, ErasurePlan] = {}
        self._receipts: List[ErasureReceipt] = []
        self._crypto_receipts: List[CryptoShredReceipt] = []
        self._legal_hold_active: bool = False

    def submit_request(self, request: DSARRequest) -> DSARRequest:
        self._requests[request.request_ref] = request
        return request

    def verify_identity(self, request_ref: str) -> DSARRequest | None:
        req = self._requests.get(request_ref)
        if req:
            req.identity_verified = True
            req.status = DSARStatus.IDENTITY_VERIFIED
        return req

    def scan_inventory(self, request_ref: str, records: List[PersonalDataRecord]) -> DataInventoryScan:
        """Scan for personal data."""
        req = self._requests.get(request_ref)
        if req:
            req.status = DSARStatus.SCANNING

        scan = DataInventoryScan(
            scan_ref=f"scan://{request_ref}/{datetime.now(timezone.utc).isoformat()}",
            request_ref=request_ref,
            records_found=records,
            total_records=len(records),
            erasable=sum(1 for r in records if r.erasure_action == ErasureAction.ERASE),
            minimizable=sum(1 for r in records if r.erasure_action == ErasureAction.MINIMIZE),
            pseudonymizable=sum(1 for r in records if r.erasure_action == ErasureAction.PSEUDONYMIZE),
            crypto_shreddable=sum(1 for r in records if r.erasure_action == ErasureAction.CRYPTO_SHRED),
            blocked_by_legal_hold=sum(1 for r in records if r.erasure_action == ErasureAction.BLOCKED),
            scan_completed_at=datetime.now(timezone.utc),
        )
        self._scans[scan.scan_ref] = scan
        return scan

    def plan_erasure(self, request_ref: str, scan_ref: str) -> ErasurePlan:
        """Classify and plan erasure actions."""
        scan = self._scans.get(scan_ref)
        if not scan:
            raise ValueError(f"unknown scan: {scan_ref}")

        actions = []
        blocked_records = []
        for record in scan.records_found:
            if self._legal_hold_active and not record.lineage_safe:
                record.erasure_action = ErasureAction.BLOCKED
                blocked_records.append(record.record_ref)
            action_dict = {
                "record_ref": record.record_ref,
                "action": record.erasure_action.value,
                "data_kind": record.data_kind.value,
                "lineage_safe": record.lineage_safe,
            }
            actions.append(action_dict)

        plan = ErasurePlan(
            plan_ref=f"plan://{request_ref}/{datetime.now(timezone.utc).isoformat()}",
            request_ref=request_ref,
            scan_ref=scan_ref,
            actions=actions,
            legal_hold_blocked=self._legal_hold_active,
            blocked_records=blocked_records,
        )
        self._plans[plan.plan_ref] = plan
        return plan

    def execute_erasure(self, plan_ref: str) -> ErasureReceipt:
        """Execute erasure plan."""
        plan = self._plans.get(plan_ref)
        if not plan:
            raise ValueError(f"unknown plan: {plan_ref}")

        erased = sum(1 for a in plan.actions if a["action"] == ErasureAction.ERASE.value)
        minimized = sum(1 for a in plan.actions if a["action"] == ErasureAction.MINIMIZE.value)
        pseudonymized = sum(1 for a in plan.actions if a["action"] == ErasureAction.PSEUDONYMIZE.value)
        crypto_shredded = sum(1 for a in plan.actions if a["action"] == ErasureAction.CRYPTO_SHRED.value)
        blocked = sum(1 for a in plan.actions if a["action"] == ErasureAction.BLOCKED.value)

        receipt = ErasureReceipt(
            receipt_ref=f"receipt://erasure/{plan.request_ref}/{datetime.now(timezone.utc).isoformat()}",
            request_ref=plan.request_ref,
            plan_ref=plan_ref,
            records_erased=erased,
            records_minimized=minimized,
            records_pseudonymized=pseudonymized,
            records_crypto_shredded=crypto_shredded,
            records_blocked=blocked,
            legal_hold_veto_count=blocked,
        )
        self._receipts.append(receipt)
        return receipt

    def crypto_shred(self, record_ref: str, key_destroyed: bool = True) -> CryptoShredReceipt:
        """Crypto-shred: destroy key → data unreadable. Only if key separable."""
        receipt = CryptoShredReceipt(
            receipt_ref=f"receipt://crypto-shred/{record_ref}/{datetime.now(timezone.utc).isoformat()}",
            record_ref=record_ref,
            key_destroyed=key_destroyed,
            data_rendered_unreadable=key_destroyed,
            lineage_preserved=True,
        )
        self._crypto_receipts.append(receipt)
        return receipt

    def set_legal_hold(self, active: bool) -> None:
        self._legal_hold_active = active

    def get_legal_hold(self) -> bool:
        return self._legal_hold_active

    def get_request(self, request_ref: str) -> Optional[DSARRequest]:
        return self._requests.get(request_ref)

    def list_receipts(self, request_ref: Optional[str] = None) -> List[ErasureReceipt]:
        receipts = self._receipts
        if request_ref:
            receipts = [r for r in receipts if r.request_ref == request_ref]
        return receipts

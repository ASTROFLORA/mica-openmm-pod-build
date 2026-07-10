"""
KB Calibration Registry — K5-5 (KB Slice 3)

MLflow-style registry for extractor/LLM adapter calibration.
Golden firewall: golden set never enters training.
Shadow → canary → rollback via aliases.

Key objects:
- CalibrationEntry: versioned calibration record
- CalibrationRegistry: manages calibration lifecycle
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class CalibrationStatus(str, Enum):
    DRAFT = "draft"
    SHADOW = "shadow"
    CANARY = "canary"
    PRODUCTION = "production"
    ROLLED_BACK = "rolled_back"
    DEPRECATED = "deprecated"


@dataclass
class CalibrationEntry:
    """Versioned calibration record for an extractor/adapter."""
    calibration_ref: str
    extractor_ref: str  # which extractor version
    llm_adapter_ref: Optional[str] = None
    training_data_manifest_ref: Optional[str] = None
    golden_set_ref: Optional[str] = None
    metrics: Dict[str, float] = field(default_factory=dict)
    status: CalibrationStatus = CalibrationStatus.DRAFT
    alias: Optional[str] = None  # active alias for deployment
    model_card_ref: Optional[str] = None
    receipt_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CalibrationRegistry:
    """K5-5: MLflow-style calibration lifecycle.

    Shadow → canary → production → rollback.
    Golden set is NEVER used as training data (golden firewall).
    """

    def __init__(self):
        self._entries: Dict[str, CalibrationEntry] = {}
        self._aliases: Dict[str, str] = {}  # alias -> calibration_ref
        self._history: List[Dict[str, Any]] = []

    def register(self, entry: CalibrationEntry) -> str:
        """Register a new calibration entry."""
        self._entries[entry.calibration_ref] = entry
        return entry.calibration_ref

    def promote_to_shadow(self, calibration_ref: str) -> CalibrationEntry:
        entry = self._entries.get(calibration_ref)
        if entry is None:
            raise ValueError(f"Calibration {calibration_ref} not found")
        entry.status = CalibrationStatus.SHADOW
        self._history.append({"action": "shadow", "ref": calibration_ref, "ts": datetime.now(timezone.utc)})
        return entry

    def promote_to_canary(self, calibration_ref: str, alias: str) -> CalibrationEntry:
        entry = self._entries.get(calibration_ref)
        if entry is None:
            raise ValueError(f"Calibration {calibration_ref} not found")
        entry.status = CalibrationStatus.CANARY
        entry.alias = alias
        self._aliases[alias] = calibration_ref
        self._history.append({"action": "canary", "ref": calibration_ref, "alias": alias})
        return entry

    def promote_to_production(self, calibration_ref: str) -> CalibrationEntry:
        entry = self._entries.get(calibration_ref)
        if entry is None:
            raise ValueError(f"Calibration {calibration_ref} not found")
        entry.status = CalibrationStatus.PRODUCTION
        self._history.append({"action": "production", "ref": calibration_ref})
        return entry

    def rollback(self, calibration_ref: str, reason: str = "") -> CalibrationEntry:
        entry = self._entries.get(calibration_ref)
        if entry is None:
            raise ValueError(f"Calibration {calibration_ref} not found")
        entry.status = CalibrationStatus.ROLLED_BACK
        if entry.alias and entry.alias in self._aliases:
            del self._aliases[entry.alias]
        self._history.append({"action": "rollback", "ref": calibration_ref, "reason": reason})
        return entry

    def get_by_alias(self, alias: str) -> Optional[CalibrationEntry]:
        ref = self._aliases.get(alias)
        return self._entries.get(ref) if ref else None

    def get_production(self) -> List[CalibrationEntry]:
        return [e for e in self._entries.values() if e.status == CalibrationStatus.PRODUCTION]

    def golden_firewall_check(self, training_data_refs: List[str], golden_ref: str) -> bool:
        """K5-5: Golden set must NEVER appear in training data."""
        return golden_ref not in training_data_refs

    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

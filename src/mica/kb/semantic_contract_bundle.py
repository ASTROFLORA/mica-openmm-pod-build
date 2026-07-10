"""
KB Semantic Contract Bundle — P3 (KB Slice 5 · P3 gap).

Versioned contract under which a claim was created.
INV-6: "Las decisiones as-of usan el SemanticContractBundle vigente
en ese snapshot, no el registry actual."

Key objects:
- SemanticContractBundle: versioned set of predicates + mappings + rules
- ContractVersion: snapshot of contract at a point in time
- ContractRegistry: manages contract versions
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ContractStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class PredicateMapping:
    """Maps a predicate between contract versions."""
    def __init__(
        self,
        source_predicate: str,
        target_predicate: str,
        mapping_kind: str = "direct",  # direct, incompatible, untranslatable
        receipt_ref: Optional[str] = None,
    ):
        self.source_predicate = source_predicate
        self.target_predicate = target_predicate
        self.mapping_kind = mapping_kind
        self.receipt_ref = receipt_ref


@dataclass
class ContractVersion:
    """Snapshot of semantic contract at a point in time."""
    contract_ref: str
    version: str
    status: ContractStatus = ContractStatus.DRAFT
    predicates: List[str] = field(default_factory=list)
    predicate_mappings: List[Dict[str, Any]] = field(default_factory=list)
    entity_categories: List[str] = field(default_factory=list)
    tier_rules: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    activated_at: Optional[datetime] = None
    deprecated_at: Optional[datetime] = None
    supersedes_ref: Optional[str] = None
    receipt_ref: Optional[str] = None


class SemanticContractRegistry:
    """P3: Manages versioned semantic contracts.

    INV-6: Claims reference the contract version at creation time,
    not the current active contract. This enables as-of reconstruction
    with the correct predicate semantics.
    """

    def __init__(self) -> None:
        self._versions: Dict[str, ContractVersion] = {}
        self._active: Optional[str] = None

    def create_version(
        self,
        contract_ref: str,
        version: str,
        predicates: Optional[List[str]] = None,
        entity_categories: Optional[List[str]] = None,
        tier_rules: Optional[Dict[str, Any]] = None,
    ) -> ContractVersion:
        """Create a new contract version."""
        cv = ContractVersion(
            contract_ref=contract_ref,
            version=version,
            predicates=predicates or [],
            entity_categories=entity_categories or [],
            tier_rules=tier_rules or {},
        )
        self._versions[f"{contract_ref}:{version}"] = cv
        return cv

    def activate(self, contract_ref: str, version: str) -> Optional[ContractVersion]:
        """Activate a contract version. Deactivates previous active."""
        key = f"{contract_ref}:{version}"
        cv = self._versions.get(key)
        if not cv:
            return None
        # Deactivate previous
        if self._active:
            prev = self._versions.get(self._active)
            if prev and prev.status == ContractStatus.ACTIVE:
                prev.status = ContractStatus.SUPERSEDED
                prev.deprecated_at = datetime.now(timezone.utc)
        cv.status = ContractStatus.ACTIVE
        cv.activated_at = datetime.now(timezone.utc)
        self._active = key
        return cv

    def get_version(self, contract_ref: str, version: str) -> Optional[ContractVersion]:
        return self._versions.get(f"{contract_ref}:{version}")

    def get_active(self) -> Optional[ContractVersion]:
        if self._active:
            return self._versions.get(self._active)
        return None

    def get_at_time(
        self,
        contract_ref: str,
        as_of: datetime,
    ) -> Optional[ContractVersion]:
        """Find the contract version active at a given point in time."""
        candidates = []
        for key, cv in self._versions.items():
            if not key.startswith(contract_ref + ":"):
                continue
            if cv.activated_at and cv.activated_at < as_of:
                candidates.append(cv)
        if not candidates:
            # fallback: find the very first version
            for key, cv in self._versions.items():
                if not key.startswith(contract_ref + ":"):
                    continue
                candidates.append(cv)
        if not candidates:
            return None
        candidates.sort(key=lambda c: c.activated_at or datetime.min.replace(tzinfo=timezone.utc))
        return candidates[-1]

    def add_predicate_mapping(
        self,
        contract_ref: str,
        version: str,
        source: str,
        target: str,
        mapping_kind: str = "direct",
    ) -> Optional[Dict[str, Any]]:
        """Add a predicate mapping (breaking change requires mapping receipt)."""
        cv = self._versions.get(f"{contract_ref}:{version}")
        if not cv:
            return None
        mapping = {
            "source": source,
            "target": target,
            "kind": mapping_kind,
        }
        cv.predicate_mappings.append(mapping)
        return mapping

    def list_versions(self, contract_ref: Optional[str] = None) -> List[ContractVersion]:
        versions = list(self._versions.values())
        if contract_ref:
            versions = [v for v in versions if v.contract_ref == contract_ref]
        return versions

    def version_count(self) -> int:
        return len(self._versions)

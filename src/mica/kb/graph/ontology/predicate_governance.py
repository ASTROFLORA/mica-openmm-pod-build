from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from mica.kb.predicate_registry import PredicateRegistry, get_default_predicate_registry
from mica.memory.dlm.biolink_schema_authority import (
    BiolinkSchemaAuthority,
    get_default_biolink_schema_authority,
)


class PredicateChangeKind(str, Enum):
    CREATE = "create"
    DEPRECATE = "deprecate"
    SPLIT = "split"
    MERGE = "merge"
    REMAP = "remap"
    RETIRE = "retire"


class PredicateImpactLevel(str, Enum):
    LOCAL = "local"
    TRANSVERSAL = "transversal"
    CONSTITUTIONAL = "constitutional"


class PredicateLifecycleState(str, Enum):
    DRAFT = "draft"
    EXPERIMENTAL = "experimental"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"
    SUPERSEDED = "superseded"


class PredicateMappingKind(str, Enum):
    NARROWER = "narrower"
    BROADER = "broader"
    EXACT = "exact"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class GovernanceApproval:
    actor_ref: str
    approver_class: str


@dataclass(frozen=True)
class PredicateExternalMapping:
    mapping_kind: PredicateMappingKind
    biolink_predicate_curie: str | None = None
    ro_predicate_curie: str | None = None


@dataclass(frozen=True)
class PredicateDomainRange:
    subject_category: str
    object_category: str


@dataclass(frozen=True)
class PredicateChangeRequest:
    predicate_change_ref: str
    predicate_ref: str
    change_kind: PredicateChangeKind
    impact_level: PredicateImpactLevel
    definition: str
    domain_range: PredicateDomainRange
    external_mappings: tuple[PredicateExternalMapping, ...] = ()
    examples: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()
    migration_plan_ref: str | None = None
    conformance_tests_ref: str | None = None
    current_lifecycle_state: PredicateLifecycleState | None = None
    approvals: tuple[GovernanceApproval, ...] = ()


@dataclass(frozen=True)
class PredicateMappingReceipt:
    mapping_kind: str
    biolink_predicate_curie: str | None
    ro_predicate_curie: str | None
    exact_traversal_allowed: bool


@dataclass(frozen=True)
class PredicateChangeReceipt:
    receipt_ref: str
    predicate_change_ref: str
    predicate_ref: str
    change_kind: str
    impact_level: str
    decision: str
    governance_policy_ref: str
    registry_write_allowed: bool
    exact_mapping_allowed: bool
    required_approver_classes: tuple[str, ...]
    satisfied_approver_classes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    approval_receipt_ref: str | None
    registry_resolution: dict[str, Any]
    external_mapping_receipts: tuple[PredicateMappingReceipt, ...] = field(default_factory=tuple)


class PredicateGovernanceProcess:
    """G4.1 governance gate over predicate lifecycle changes.

    The shared PredicateRegistry remains the mapping authority. This process
    decides whether a proposed change is allowed to become registry-active.
    """

    governance_policy_ref = "predicate_governance://g4p0/v1"

    def __init__(
        self,
        *,
        registry: PredicateRegistry | None = None,
        biolink_authority: BiolinkSchemaAuthority | None = None,
    ) -> None:
        self._registry = registry or get_default_predicate_registry()
        self._authority = biolink_authority or get_default_biolink_schema_authority()

    def review_change(self, change: PredicateChangeRequest) -> PredicateChangeReceipt:
        self._validate_shape(change)

        required_approver_classes = self._required_approver_classes(change.impact_level)
        satisfied_approver_classes = tuple(
            sorted(
                {
                    approval.approver_class.strip().lower()
                    for approval in change.approvals
                    if approval.approver_class.strip()
                }
            )
        )
        blocking_reasons: list[str] = []
        advisory_reasons: list[str] = []

        missing_classes = [role for role in required_approver_classes if role not in satisfied_approver_classes]
        if missing_classes:
            blocking_reasons.append("missing_required_approver_class")

        if (
            change.impact_level is PredicateImpactLevel.CONSTITUTIONAL
            and len(satisfied_approver_classes) < 4
        ):
            blocking_reasons.append("cross_class_quorum_required")

        if (
            change.change_kind is PredicateChangeKind.DEPRECATE
            and change.current_lifecycle_state is PredicateLifecycleState.ACTIVE
            and len(change.approvals) < 2
        ):
            blocking_reasons.append("active_predicate_cannot_be_unilaterally_deprecated")

        mapping_receipts = tuple(
            self._build_mapping_receipt(
                predicate_ref=change.predicate_ref,
                mapping=mapping,
                advisory_reasons=advisory_reasons,
            )
            for mapping in change.external_mappings
        )
        exact_mapping_allowed = all(receipt.exact_traversal_allowed for receipt in mapping_receipts) if mapping_receipts else False

        registry_resolution = self._resolve_registry(change.predicate_ref)
        if not mapping_receipts:
            blocking_reasons.append("external_mapping_review_missing")

        reason_codes = tuple(advisory_reasons + blocking_reasons)
        decision = "approved" if not blocking_reasons else "rejected"
        payload = {
            "predicate_change_ref": change.predicate_change_ref,
            "predicate_ref": change.predicate_ref,
            "change_kind": change.change_kind.value,
            "impact_level": change.impact_level.value,
            "required_approver_classes": list(required_approver_classes),
            "satisfied_approver_classes": list(satisfied_approver_classes),
            "reason_codes": list(reason_codes),
            "external_mapping_receipts": [asdict(receipt) for receipt in mapping_receipts],
            "registry_resolution": registry_resolution,
            "decision": decision,
        }
        receipt_ref = self._stable_ref("receipt://graphrag/predicate-governance/", payload)
        approval_receipt_ref = receipt_ref if decision == "approved" else None
        return PredicateChangeReceipt(
            receipt_ref=receipt_ref,
            predicate_change_ref=change.predicate_change_ref,
            predicate_ref=change.predicate_ref,
            change_kind=change.change_kind.value,
            impact_level=change.impact_level.value,
            decision=decision,
            governance_policy_ref=self.governance_policy_ref,
            registry_write_allowed=decision == "approved",
            exact_mapping_allowed=exact_mapping_allowed,
            required_approver_classes=required_approver_classes,
            satisfied_approver_classes=satisfied_approver_classes,
            reason_codes=reason_codes,
            approval_receipt_ref=approval_receipt_ref,
            registry_resolution=registry_resolution,
            external_mapping_receipts=mapping_receipts,
        )

    def _validate_shape(self, change: PredicateChangeRequest) -> None:
        if not change.predicate_change_ref.startswith("predicate_change://"):
            raise ValueError("invalid_predicate_change_ref")
        if not change.predicate_ref.startswith("predicate://"):
            raise ValueError("invalid_predicate_ref")
        if not change.definition.strip():
            raise ValueError("definition_required")
        if not change.conformance_tests_ref:
            raise ValueError("conformance_tests_ref_required")
        if change.change_kind in {
            PredicateChangeKind.DEPRECATE,
            PredicateChangeKind.SPLIT,
            PredicateChangeKind.MERGE,
            PredicateChangeKind.REMAP,
            PredicateChangeKind.RETIRE,
        } and not change.migration_plan_ref:
            raise ValueError("migration_plan_ref_required")

    def _build_mapping_receipt(
        self,
        *,
        predicate_ref: str,
        mapping: PredicateExternalMapping,
        advisory_reasons: list[str],
    ) -> PredicateMappingReceipt:
        if mapping.mapping_kind is PredicateMappingKind.EXACT and not (
            mapping.biolink_predicate_curie or mapping.ro_predicate_curie
        ):
            raise ValueError("exact_mapping_requires_external_curie")

        exact_traversal_allowed = mapping.mapping_kind is PredicateMappingKind.EXACT
        if mapping.mapping_kind is not PredicateMappingKind.EXACT:
            advisory_reasons.append("non_exact_mapping_preserved")

        if mapping.biolink_predicate_curie and mapping.mapping_kind is PredicateMappingKind.EXACT:
            local_key = predicate_ref.rsplit("/", 1)[-1]
            authority_receipt = self._authority.resolve_predicate(local_key)
            matched_curie = authority_receipt.get("predicate_curie")
            if matched_curie and matched_curie != mapping.biolink_predicate_curie:
                raise ValueError("biolink_exact_mapping_mismatch")

        return PredicateMappingReceipt(
            mapping_kind=mapping.mapping_kind.value,
            biolink_predicate_curie=mapping.biolink_predicate_curie,
            ro_predicate_curie=mapping.ro_predicate_curie,
            exact_traversal_allowed=exact_traversal_allowed,
        )

    def _resolve_registry(self, predicate_ref: str) -> dict[str, Any]:
        local_key = predicate_ref.rsplit("/", 1)[-1]
        entry = self._registry.resolve(local_key)
        return {
            "input_key": entry.input_key,
            "edge_kind": entry.edge_kind,
            "predicate_id": entry.predicate_id,
            "registry_version": entry.registry_version,
            "biolink_predicate_curie": entry.biolink_predicate_curie,
            "registry_status": entry.registry_status,
        }

    @staticmethod
    def _required_approver_classes(impact_level: PredicateImpactLevel) -> tuple[str, ...]:
        if impact_level is PredicateImpactLevel.LOCAL:
            return ("graph_steward", "domain_curator")
        if impact_level is PredicateImpactLevel.TRANSVERSAL:
            return ("graph_steward", "kb_steward", "schema_owner")
        return ("graph_steward", "kb_steward", "schema_owner")

    @staticmethod
    def _stable_ref(prefix: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}"

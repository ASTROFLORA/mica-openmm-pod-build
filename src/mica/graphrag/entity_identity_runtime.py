from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

_RELATION_KINDS = {
    "same_as",
    "exact_match",
    "close_match",
    "related_match",
    "not_same_as",
    "split_required",
    "merge_required",
}

_COLLAPSING_RELATION_KINDS = {"same_as", "exact_match"}


@dataclass(frozen=True)
class EntityIdentityDecision:
    decision_ref: str
    status: str
    local_entity_ref: str
    external_entity_refs: tuple[str, ...]
    relation_kind: str
    confidence: float
    resolver_version_ref: str
    evidence_refs: tuple[str, ...]
    decision_receipt_ref: str
    valid_from: str
    valid_to: str | None
    disputed: bool
    identity_conflict: bool
    collapse_allowed: bool
    requires_local_identity_gate: bool
    stale_path_refs: tuple[str, ...]
    review_required_path_refs: tuple[str, ...]
    external_decision_refs: tuple[str, ...]
    blocked_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_ref": self.decision_ref,
            "status": self.status,
            "local_entity_ref": self.local_entity_ref,
            "external_entity_refs": list(self.external_entity_refs),
            "relation_kind": self.relation_kind,
            "confidence": self.confidence,
            "resolver_version_ref": self.resolver_version_ref,
            "evidence_refs": list(self.evidence_refs),
            "decision_receipt_ref": self.decision_receipt_ref,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "disputed": self.disputed,
            "identity_conflict": self.identity_conflict,
            "collapse_allowed": self.collapse_allowed,
            "requires_local_identity_gate": self.requires_local_identity_gate,
            "stale_path_refs": list(self.stale_path_refs),
            "review_required_path_refs": list(self.review_required_path_refs),
            "external_decision_refs": list(self.external_decision_refs),
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class ResolverDriftEvent:
    resolver_drift_event_ref: str
    previous_resolver_version_ref: str
    resolver_version_ref: str
    impacted_entity_refs: tuple[str, ...]
    impacted_edge_refs: tuple[str, ...]
    detected_at: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolver_drift_event_ref": self.resolver_drift_event_ref,
            "previous_resolver_version_ref": self.previous_resolver_version_ref,
            "resolver_version_ref": self.resolver_version_ref,
            "impacted_entity_refs": list(self.impacted_entity_refs),
            "impacted_edge_refs": list(self.impacted_edge_refs),
            "detected_at": self.detected_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GraphReprojectionPlan:
    reprojection_plan_ref: str
    status: str
    action: str
    canonical_entity_ref: str
    successor_entity_refs: tuple[str, ...]
    stale_path_refs: tuple[str, ...]
    review_required_path_refs: tuple[str, ...]
    impacted_edge_refs: tuple[str, ...]
    alias_updates: tuple[str, ...]
    rewrite_policy: str
    resolver_drift_event: ResolverDriftEvent | None = None
    identity_decision: EntityIdentityDecision | None = None
    blocked_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "reprojection_plan_ref": self.reprojection_plan_ref,
            "status": self.status,
            "action": self.action,
            "canonical_entity_ref": self.canonical_entity_ref,
            "successor_entity_refs": list(self.successor_entity_refs),
            "stale_path_refs": list(self.stale_path_refs),
            "review_required_path_refs": list(self.review_required_path_refs),
            "impacted_edge_refs": list(self.impacted_edge_refs),
            "alias_updates": list(self.alias_updates),
            "rewrite_policy": self.rewrite_policy,
            "resolver_drift_event": self.resolver_drift_event.as_dict() if self.resolver_drift_event else None,
            "identity_decision": self.identity_decision.as_dict() if self.identity_decision else None,
            "blocked_reasons": list(self.blocked_reasons),
        }


class EntityIdentityRuntime:
    """G4.7 global entity identity authority over existing same_as semantics."""

    policy_ref = "graph_entity_identity://g4p7/v1"
    rewrite_policy_ref = "graph_reprojection://reproject_not_rewrite/v1"

    def issue_identity_decision(
        self,
        *,
        local_entity_ref: str,
        external_entity_refs: Sequence[str],
        relation_kind: str,
        confidence: float,
        resolver_version_ref: str,
        evidence_refs: Sequence[str],
        decision_receipt_ref: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        disputed: bool = False,
        stale_path_refs: Sequence[str] = (),
        review_required_path_refs: Sequence[str] = (),
        external_decision_refs: Sequence[str] = (),
        blocked_reasons: Sequence[str] = (),
        identity_conflict: bool = False,
        collapse_allowed: bool | None = None,
        requires_local_identity_gate: bool | None = None,
    ) -> EntityIdentityDecision:
        normalized_relation_kind = self._normalize_relation_kind(relation_kind)
        normalized_local_entity_ref = self._require_ref(local_entity_ref, "local_entity_ref")
        normalized_resolver_version_ref = self._require_ref(resolver_version_ref, "resolver_version_ref")
        normalized_receipt_ref = self._require_ref(decision_receipt_ref, "decision_receipt_ref")
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
        normalized_external_refs = self._normalize_refs(external_entity_refs)
        normalized_evidence_refs = self._normalize_refs(evidence_refs)
        normalized_stale_path_refs = self._normalize_refs(stale_path_refs)
        normalized_review_required_path_refs = self._normalize_refs(review_required_path_refs)
        normalized_external_decision_refs = self._normalize_refs(external_decision_refs)
        normalized_blocked_reasons = self._normalize_refs(blocked_reasons)
        effective_valid_from = valid_from or datetime.now(timezone.utc).isoformat()
        effective_valid_to = valid_to.strip() if isinstance(valid_to, str) and valid_to.strip() else None

        if collapse_allowed is None:
            collapse_allowed = normalized_relation_kind in _COLLAPSING_RELATION_KINDS and not identity_conflict
        if requires_local_identity_gate is None:
            requires_local_identity_gate = identity_conflict or normalized_relation_kind in {"split_required", "merge_required"}

        status = "accepted"
        if normalized_blocked_reasons:
            status = "blocked"
        elif requires_local_identity_gate or normalized_review_required_path_refs:
            status = "review_required"

        payload = {
            "local_entity_ref": normalized_local_entity_ref,
            "external_entity_refs": list(normalized_external_refs),
            "relation_kind": normalized_relation_kind,
            "resolver_version_ref": normalized_resolver_version_ref,
            "decision_receipt_ref": normalized_receipt_ref,
            "valid_from": effective_valid_from,
            "valid_to": effective_valid_to,
            "identity_conflict": identity_conflict,
        }
        return EntityIdentityDecision(
            decision_ref=self._stable_ref("entity_identity_decision://graphrag/", payload),
            status=status,
            local_entity_ref=normalized_local_entity_ref,
            external_entity_refs=normalized_external_refs,
            relation_kind=normalized_relation_kind,
            confidence=normalized_confidence,
            resolver_version_ref=normalized_resolver_version_ref,
            evidence_refs=normalized_evidence_refs,
            decision_receipt_ref=normalized_receipt_ref,
            valid_from=effective_valid_from,
            valid_to=effective_valid_to,
            disputed=bool(disputed),
            identity_conflict=bool(identity_conflict),
            collapse_allowed=bool(collapse_allowed),
            requires_local_identity_gate=bool(requires_local_identity_gate),
            stale_path_refs=normalized_stale_path_refs,
            review_required_path_refs=normalized_review_required_path_refs,
            external_decision_refs=normalized_external_decision_refs,
            blocked_reasons=normalized_blocked_reasons,
        )

    def review_cross_instance_conflict(
        self,
        *,
        local_entity_ref: str,
        external_entity_refs: Sequence[str],
        local_relation_kind: str,
        external_relation_kinds: Sequence[str],
        confidence: float,
        resolver_version_ref: str,
        evidence_refs: Sequence[str],
        decision_receipt_ref: str,
        external_decision_refs: Sequence[str] = (),
        local_identity_gate_approved: bool = False,
    ) -> EntityIdentityDecision:
        normalized_local_relation = self._normalize_relation_kind(local_relation_kind)
        normalized_external_relations = {
            self._normalize_relation_kind(kind)
            for kind in external_relation_kinds
            if str(kind or "").strip()
        }
        identity_conflict = any(kind != normalized_local_relation for kind in normalized_external_relations)
        collapse_requested = normalized_local_relation in _COLLAPSING_RELATION_KINDS
        collapse_allowed = collapse_requested and not identity_conflict
        blocked_reasons: list[str] = []
        if collapse_requested and identity_conflict and not local_identity_gate_approved:
            blocked_reasons.append("cross_instance_identity_conflict_blocks_same_as_collapse")
        return self.issue_identity_decision(
            local_entity_ref=local_entity_ref,
            external_entity_refs=external_entity_refs,
            relation_kind=normalized_local_relation,
            confidence=confidence,
            resolver_version_ref=resolver_version_ref,
            evidence_refs=evidence_refs,
            decision_receipt_ref=decision_receipt_ref,
            disputed=identity_conflict,
            external_decision_refs=external_decision_refs,
            blocked_reasons=blocked_reasons,
            identity_conflict=identity_conflict,
            collapse_allowed=collapse_allowed and local_identity_gate_approved if identity_conflict else collapse_allowed,
            requires_local_identity_gate=identity_conflict,
        )

    def plan_split(
        self,
        *,
        canonical_entity_ref: str,
        successor_entity_refs: Sequence[str],
        stale_path_refs: Sequence[str],
        impacted_edge_refs: Sequence[str],
        resolver_version_ref: str,
        evidence_refs: Sequence[str],
        decision_receipt_ref: str,
    ) -> GraphReprojectionPlan:
        normalized_canonical_entity_ref = self._require_ref(canonical_entity_ref, "canonical_entity_ref")
        normalized_successors = self._normalize_refs(successor_entity_refs)
        if len(normalized_successors) < 2:
            raise ValueError("entity_split_requires_two_successors")
        normalized_stale_paths = self._normalize_refs(stale_path_refs)
        normalized_impacted_edges = self._normalize_refs(impacted_edge_refs)
        decision = self.issue_identity_decision(
            local_entity_ref=normalized_canonical_entity_ref,
            external_entity_refs=normalized_successors,
            relation_kind="split_required",
            confidence=1.0,
            resolver_version_ref=resolver_version_ref,
            evidence_refs=evidence_refs,
            decision_receipt_ref=decision_receipt_ref,
            disputed=True,
            stale_path_refs=normalized_stale_paths,
            review_required_path_refs=normalized_stale_paths,
            requires_local_identity_gate=True,
            collapse_allowed=False,
        )
        alias_updates = tuple(f"alias://{normalized_canonical_entity_ref}->{successor}" for successor in normalized_successors)
        payload = {
            "action": "split",
            "canonical_entity_ref": normalized_canonical_entity_ref,
            "successor_entity_refs": list(normalized_successors),
            "stale_path_refs": list(normalized_stale_paths),
            "impacted_edge_refs": list(normalized_impacted_edges),
        }
        return GraphReprojectionPlan(
            reprojection_plan_ref=self._stable_ref("graph_reprojection_plan://graphrag/", payload),
            status="review_required",
            action="split",
            canonical_entity_ref=normalized_canonical_entity_ref,
            successor_entity_refs=normalized_successors,
            stale_path_refs=normalized_stale_paths,
            review_required_path_refs=normalized_stale_paths,
            impacted_edge_refs=normalized_impacted_edges,
            alias_updates=alias_updates,
            rewrite_policy=self.rewrite_policy_ref,
            identity_decision=decision,
        )

    def plan_resolver_drift(
        self,
        *,
        previous_resolver_version_ref: str,
        resolver_version_ref: str,
        impacted_entity_refs: Sequence[str],
        impacted_edge_refs: Sequence[str],
        stale_path_refs: Sequence[str] = (),
        reason: str = "resolver_release_changed",
    ) -> GraphReprojectionPlan:
        event = ResolverDriftEvent(
            resolver_drift_event_ref=self._stable_ref(
                "resolver_drift_event://graphrag/",
                {
                    "previous_resolver_version_ref": previous_resolver_version_ref,
                    "resolver_version_ref": resolver_version_ref,
                    "impacted_entity_refs": list(self._normalize_refs(impacted_entity_refs)),
                    "impacted_edge_refs": list(self._normalize_refs(impacted_edge_refs)),
                    "reason": reason,
                },
            ),
            previous_resolver_version_ref=self._require_ref(previous_resolver_version_ref, "previous_resolver_version_ref"),
            resolver_version_ref=self._require_ref(resolver_version_ref, "resolver_version_ref"),
            impacted_entity_refs=self._normalize_refs(impacted_entity_refs),
            impacted_edge_refs=self._normalize_refs(impacted_edge_refs),
            detected_at=datetime.now(timezone.utc).isoformat(),
            reason=str(reason or "resolver_release_changed").strip() or "resolver_release_changed",
        )
        normalized_stale_paths = self._normalize_refs(stale_path_refs)
        payload = {
            "action": "resolver_drift_reprojection",
            "resolver_drift_event_ref": event.resolver_drift_event_ref,
            "stale_path_refs": list(normalized_stale_paths),
        }
        return GraphReprojectionPlan(
            reprojection_plan_ref=self._stable_ref("graph_reprojection_plan://graphrag/", payload),
            status="review_required" if normalized_stale_paths else "planned",
            action="resolver_drift_reprojection",
            canonical_entity_ref="entity://graph/global",
            successor_entity_refs=(),
            stale_path_refs=normalized_stale_paths,
            review_required_path_refs=normalized_stale_paths,
            impacted_edge_refs=event.impacted_edge_refs,
            alias_updates=(),
            rewrite_policy=self.rewrite_policy_ref,
            resolver_drift_event=event,
        )

    @staticmethod
    def _normalize_relation_kind(relation_kind: str) -> str:
        normalized = str(relation_kind or "").strip().lower()
        if normalized not in _RELATION_KINDS:
            raise ValueError("unsupported_relation_kind")
        return normalized

    @staticmethod
    def _normalize_refs(values: Sequence[str]) -> tuple[str, ...]:
        return tuple(str(value).strip() for value in values if str(value).strip())

    @staticmethod
    def _require_ref(value: str, field_name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field_name}_required")
        return normalized

    @staticmethod
    def _stable_ref(prefix: str, payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}"


__all__ = [
    "EntityIdentityDecision",
    "EntityIdentityRuntime",
    "GraphReprojectionPlan",
    "ResolverDriftEvent",
]

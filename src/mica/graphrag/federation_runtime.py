from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from mica.graphrag.evidence_path_runtime import EvidencePathBundle
from mica.kb.federation import FederationImporter, FederationPackage
from mica.kb.predicate_registry import PredicateRegistry, get_default_predicate_registry


@dataclass(frozen=True)
class FederatedGraphEdgeDelta:
    source_instance: str
    package_ref: str
    source_node: str
    source_type: str
    target_node: str
    target_type: str
    relationship: str
    details: Optional[str] = None
    confidence: float = 1.0
    source_doi: Optional[str] = None
    source_sentence: Optional[str] = None
    signature_ref: Optional[str] = None
    policy_scope: str = "global"


@dataclass(frozen=True)
class PredicateReconcileDecision:
    local_predicate_id: str
    external_predicate_id: str
    status: str
    chosen_predicate_id: str
    reason: str


@dataclass(frozen=True)
class FederatedGraphImportResult:
    package_ref: str
    source_instance: str
    imported_count: int
    imported_edge_refs: tuple[str, ...]
    created_by_receipt_ref: str
    trust_status: str


@dataclass(frozen=True)
class ExternalRetractionDelta:
    source_instance: str
    retraction_ref: str
    retracted_edge_refs: tuple[str, ...] = ()
    retracted_receipt_refs: tuple[str, ...] = ()
    affected_scope_refs: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class CachedEvidencePathEntry:
    bundle: EvidencePathBundle
    scope_ref: str
    status: str = "active"


@dataclass(frozen=True)
class RetractionPropagationResult:
    retraction_ref: str
    invalidated_bundle_refs: tuple[str, ...]
    affected_scope_refs: tuple[str, ...]
    reevaluation_enqueued_refs: tuple[str, ...]
    created_by_receipt_ref: str


class PredicateReconciler:
    def __init__(self, registry: PredicateRegistry | None = None) -> None:
        self._registry = registry or get_default_predicate_registry()

    def reconcile(self, *, local_predicate: str, external_predicate: str) -> PredicateReconcileDecision:
        local_entry = self._registry.resolve(local_predicate)
        external_entry = self._registry.resolve(external_predicate)
        if local_entry.predicate_id == external_entry.predicate_id:
            return PredicateReconcileDecision(
                local_predicate_id=local_entry.predicate_id,
                external_predicate_id=external_entry.predicate_id,
                status="exact",
                chosen_predicate_id=local_entry.predicate_id,
                reason="predicate_ids_match",
            )
        if local_entry.predicate_id == "related_to":
            return PredicateReconcileDecision(
                local_predicate_id=local_entry.predicate_id,
                external_predicate_id=external_entry.predicate_id,
                status="broader",
                chosen_predicate_id=external_entry.predicate_id,
                reason="local_related_to_broader_than_external",
            )
        if external_entry.predicate_id == "related_to":
            return PredicateReconcileDecision(
                local_predicate_id=local_entry.predicate_id,
                external_predicate_id=external_entry.predicate_id,
                status="narrower",
                chosen_predicate_id=local_entry.predicate_id,
                reason="external_related_to_broader_than_local",
            )
        return PredicateReconcileDecision(
            local_predicate_id=local_entry.predicate_id,
            external_predicate_id=external_entry.predicate_id,
            status="incompatible",
            chosen_predicate_id=local_entry.predicate_id,
            reason="predicate_ids_diverge",
        )


class CachedEvidencePathBundleStore:
    def __init__(self) -> None:
        self._entries: dict[str, CachedEvidencePathEntry] = {}

    def register(self, *, bundle: EvidencePathBundle, scope_ref: str) -> CachedEvidencePathEntry:
        entry = CachedEvidencePathEntry(bundle=bundle, scope_ref=scope_ref, status="active")
        self._entries[bundle.bundle_ref] = entry
        return entry

    def get(self, bundle_ref: str) -> Optional[CachedEvidencePathEntry]:
        return self._entries.get(bundle_ref)

    def invalidate_matching(
        self,
        *,
        retracted_edge_refs: Iterable[str],
        retracted_receipt_refs: Iterable[str],
    ) -> tuple[str, ...]:
        edge_ref_set = {ref for ref in retracted_edge_refs if ref}
        receipt_ref_set = {ref for ref in retracted_receipt_refs if ref}
        invalidated: list[str] = []
        for bundle_ref, entry in list(self._entries.items()):
            if entry.status != "active":
                continue
            if edge_ref_set.intersection(entry.bundle.edge_refs) or receipt_ref_set.intersection(entry.bundle.receipt_refs):
                self._entries[bundle_ref] = CachedEvidencePathEntry(
                    bundle=entry.bundle,
                    scope_ref=entry.scope_ref,
                    status="invalidated",
                )
                invalidated.append(bundle_ref)
        return tuple(sorted(invalidated))

    def affected_scope_refs(self, bundle_refs: Iterable[str]) -> tuple[str, ...]:
        scopes = {
            self._entries[bundle_ref].scope_ref
            for bundle_ref in bundle_refs
            if bundle_ref in self._entries
        }
        return tuple(sorted(scopes))


class RetractionPropagator:
    def __init__(self, cache: CachedEvidencePathBundleStore) -> None:
        self._cache = cache

    @staticmethod
    def _stable_ref(prefix: str, payload: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}{digest}"

    def propagate(self, delta: ExternalRetractionDelta) -> RetractionPropagationResult:
        invalidated = self._cache.invalidate_matching(
            retracted_edge_refs=delta.retracted_edge_refs,
            retracted_receipt_refs=delta.retracted_receipt_refs,
        )
        scope_refs = tuple(sorted(set(delta.affected_scope_refs) | set(self._cache.affected_scope_refs(invalidated))))
        reevaluation_refs = tuple(
            self._stable_ref(
                "graph_reeval://",
                {"scope_ref": scope_ref, "retraction_ref": delta.retraction_ref},
            )
            for scope_ref in scope_refs
        )
        receipt_ref = self._stable_ref(
            "receipt://graphrag/external-retraction/",
            {
                "retraction_ref": delta.retraction_ref,
                "invalidated_bundle_refs": list(invalidated),
                "scope_refs": list(scope_refs),
            },
        )
        return RetractionPropagationResult(
            retraction_ref=delta.retraction_ref,
            invalidated_bundle_refs=invalidated,
            affected_scope_refs=scope_refs,
            reevaluation_enqueued_refs=reevaluation_refs,
            created_by_receipt_ref=receipt_ref,
        )


class FederatedGraphImporter:
    def __init__(
        self,
        *,
        store: Any,
        federation_importer: FederationImporter,
        predicate_reconciler: PredicateReconciler | None = None,
    ) -> None:
        self._store = store
        self._federation_importer = federation_importer
        self._predicate_reconciler = predicate_reconciler or PredicateReconciler()

    @staticmethod
    def _stable_ref(prefix: str, payload: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}{digest}"

    async def import_edge_deltas(
        self,
        *,
        package: FederationPackage,
        deltas: Iterable[FederatedGraphEdgeDelta],
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> FederatedGraphImportResult:
        imported_package = self._federation_importer.import_package(package)
        if str(imported_package.status.value) != "imported":
            raise ValueError("federated package rejected by trust importer")

        imported_edge_refs: list[str] = []
        for delta in deltas:
            reconcile = self._predicate_reconciler.reconcile(
                local_predicate=delta.relationship,
                external_predicate=delta.relationship,
            )
            claim_payload = {
                "package_ref": delta.package_ref,
                "source_instance": delta.source_instance,
                "source_node": delta.source_node,
                "target_node": delta.target_node,
                "relationship": reconcile.chosen_predicate_id,
            }
            edge_ref = self._stable_ref("federated_edge://", claim_payload)
            receipt_ref = self._stable_ref("receipt://graphrag/federated-import/", claim_payload)
            metadata = {
                "edge_ref": edge_ref,
                "created_by_receipt_ref": receipt_ref,
                "policy_scope": delta.policy_scope,
                "federation_state": "external_asserted",
                "source_instance": delta.source_instance,
                "package_ref": delta.package_ref,
                "signature_ref": delta.signature_ref,
                "signature_verified": bool(imported_package.signature_verified),
                "trust_level": str(imported_package.trust_level.value),
                "predicate_reconcile_status": reconcile.status,
                "predicate_reconcile_reason": reconcile.reason,
                "external_predicate_id": reconcile.external_predicate_id,
                "chosen_predicate_id": reconcile.chosen_predicate_id,
            }
            await self._store.upsert_edge(
                source_node=delta.source_node,
                source_type=delta.source_type,
                target_node=delta.target_node,
                target_type=delta.target_type,
                relationship=reconcile.chosen_predicate_id,
                details=delta.details,
                confidence=delta.confidence,
                source_doi=delta.source_doi,
                source_sentence=delta.source_sentence,
                extraction_method="federated_import",
                metadata=metadata,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            imported_edge_refs.append(edge_ref)

        result_receipt_ref = self._stable_ref(
            "receipt://graphrag/federated-import-batch/",
            {
                "package_ref": imported_package.package_ref,
                "source_instance": imported_package.source_instance,
                "imported_edge_refs": imported_edge_refs,
            },
        )
        return FederatedGraphImportResult(
            package_ref=imported_package.package_ref,
            source_instance=imported_package.source_instance,
            imported_count=len(imported_edge_refs),
            imported_edge_refs=tuple(imported_edge_refs),
            created_by_receipt_ref=result_receipt_ref,
            trust_status=str(imported_package.status.value),
        )


__all__ = [
    "CachedEvidencePathBundleStore",
    "CachedEvidencePathEntry",
    "ExternalRetractionDelta",
    "FederatedGraphEdgeDelta",
    "FederatedGraphImportResult",
    "FederatedGraphImporter",
    "PredicateReconcileDecision",
    "PredicateReconciler",
    "RetractionPropagationResult",
    "RetractionPropagator",
]

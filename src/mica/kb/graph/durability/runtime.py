from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


def _stable_json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_ts(value: str | datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    if not text:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if text.endswith("Z"):
        return text
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_merkle_root(hashes: list[str]) -> str:
    if not hashes:
        return _stable_json_hash({"empty": True})
    level = [str(item) for item in hashes]
    while len(level) > 1:
        next_level: list[str] = []
        for index in range(0, len(level), 2):
            left = level[index]
            right = level[index + 1] if index + 1 < len(level) else left
            next_level.append(_stable_json_hash({"left": left, "right": right}))
        level = next_level
    return level[0]


@dataclass(frozen=True)
class GraphEdgeEventLogEntry:
    event_ref: str
    edge_ref: str
    event_kind: str
    edge_status: str
    valid_from_ts: str
    valid_to_ts: Optional[str]
    transaction_time_ts: str
    created_by_receipt_ref: str
    predicate_id: Optional[str]
    predicate_registry_version: Optional[str]
    policy_scope: Optional[str]
    edge_record: dict[str, Any]
    event_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphPortableCanonBundle:
    bundle_ref: str
    scope_ref: str
    as_of_ts: str
    transaction_as_of_ts: str
    graph_snapshot_manifest_ref: str
    vector_index_manifest_ref: str
    predicate_registry_snapshot_ref: str
    predicate_registry_version: str
    semantic_contract_bundle_ref: str
    schema_version_ref: str
    doctrine_version_ref: str
    receipt_manifest_refs: list[str]
    migration_manifest_refs: list[str]
    checksum_root: str
    merkle_root: str
    edge_count: int
    event_count: int
    active_edge_manifest: list[dict[str, Any]]
    event_log: list[GraphEdgeEventLogEntry]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_log"] = [entry.as_dict() for entry in self.event_log]
        return payload


@dataclass(frozen=True)
class GraphLongHorizonReplayResult:
    replay_status: str
    matched: bool
    reason: str
    requested_as_of_ts: str
    checksum_verified: bool
    merkle_verified: bool
    receipt_refs_verified: bool
    compatibility_verified: bool
    active_edge_count: int
    reconstructed_active_edges: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GraphDurabilityRuntime:
    """Portable graph canon + long-horizon as-of replay.

    This runtime does not create a second graph authority. It packages the
    already canonical, receipted graph surface into a portable bundle that can
    be replayed decades later under explicit registry/schema/doctrine refs.
    """

    @staticmethod
    def build_event_log_entry(
        *,
        manifest_entry: Mapping[str, Any],
        valid_from_ts: str | datetime,
        valid_to_ts: str | datetime | None,
        transaction_time_ts: str | datetime,
        event_kind: str = "edge_asserted",
        edge_status: Optional[str] = None,
    ) -> GraphEdgeEventLogEntry:
        edge_ref = str(manifest_entry.get("edge_ref") or "").strip()
        receipt_ref = str(manifest_entry.get("created_by_receipt_ref") or "").strip()
        if not edge_ref:
            raise ValueError("graph durability event log requires edge_ref")
        if not receipt_ref:
            raise ValueError("graph durability event log requires created_by_receipt_ref")
        normalized_edge_status = str(edge_status or manifest_entry.get("edge_status") or "active").strip().lower()
        valid_from = _normalize_ts(valid_from_ts)
        valid_to = _normalize_ts(valid_to_ts) if valid_to_ts is not None else None
        transaction_time = _normalize_ts(transaction_time_ts)
        edge_record = dict(manifest_entry)
        event_payload = {
            "edge_ref": edge_ref,
            "event_kind": event_kind,
            "edge_status": normalized_edge_status,
            "valid_from_ts": valid_from,
            "valid_to_ts": valid_to,
            "transaction_time_ts": transaction_time,
            "created_by_receipt_ref": receipt_ref,
            "edge_record": edge_record,
        }
        event_hash = _stable_json_hash(event_payload)
        return GraphEdgeEventLogEntry(
            event_ref=f"event://graphrag/edge-log/{event_hash[:24]}",
            edge_ref=edge_ref,
            event_kind=event_kind,
            edge_status=normalized_edge_status,
            valid_from_ts=valid_from,
            valid_to_ts=valid_to,
            transaction_time_ts=transaction_time,
            created_by_receipt_ref=receipt_ref,
            predicate_id=edge_record.get("predicate_id"),
            predicate_registry_version=edge_record.get("predicate_registry_version"),
            policy_scope=edge_record.get("policy_scope"),
            edge_record=edge_record,
            event_hash=event_hash,
        )

    @staticmethod
    def _normalize_active_edge_manifest(
        active_edge_manifest: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        manifest = [dict(entry) for entry in active_edge_manifest]
        manifest.sort(
            key=lambda item: (
                str(item.get("edge_ref") or ""),
                str(item.get("source_node") or ""),
                str(item.get("relationship") or ""),
                str(item.get("target_node") or ""),
            )
        )
        return manifest

    @staticmethod
    def _normalize_event_log(event_log: list[GraphEdgeEventLogEntry | Mapping[str, Any]]) -> list[GraphEdgeEventLogEntry]:
        entries: list[GraphEdgeEventLogEntry] = []
        for raw in event_log:
            if isinstance(raw, GraphEdgeEventLogEntry):
                entry = raw
            else:
                payload = dict(raw)
                edge_record = dict(payload.get("edge_record") or {})
                entry = GraphEdgeEventLogEntry(
                    event_ref=str(payload.get("event_ref") or ""),
                    edge_ref=str(payload.get("edge_ref") or ""),
                    event_kind=str(payload.get("event_kind") or "edge_asserted"),
                    edge_status=str(payload.get("edge_status") or "active"),
                    valid_from_ts=_normalize_ts(payload.get("valid_from_ts")),
                    valid_to_ts=_normalize_ts(payload.get("valid_to_ts")) if payload.get("valid_to_ts") else None,
                    transaction_time_ts=_normalize_ts(payload.get("transaction_time_ts")),
                    created_by_receipt_ref=str(payload.get("created_by_receipt_ref") or ""),
                    predicate_id=payload.get("predicate_id"),
                    predicate_registry_version=payload.get("predicate_registry_version"),
                    policy_scope=payload.get("policy_scope"),
                    edge_record=edge_record,
                    event_hash=str(payload.get("event_hash") or ""),
                )
            if not entry.event_hash:
                rebuilt = GraphDurabilityRuntime.build_event_log_entry(
                    manifest_entry=entry.edge_record,
                    valid_from_ts=entry.valid_from_ts,
                    valid_to_ts=entry.valid_to_ts,
                    transaction_time_ts=entry.transaction_time_ts,
                    event_kind=entry.event_kind,
                    edge_status=entry.edge_status,
                )
                entry = GraphEdgeEventLogEntry(
                    event_ref=entry.event_ref or rebuilt.event_ref,
                    edge_ref=rebuilt.edge_ref,
                    event_kind=rebuilt.event_kind,
                    edge_status=rebuilt.edge_status,
                    valid_from_ts=rebuilt.valid_from_ts,
                    valid_to_ts=rebuilt.valid_to_ts,
                    transaction_time_ts=rebuilt.transaction_time_ts,
                    created_by_receipt_ref=rebuilt.created_by_receipt_ref,
                    predicate_id=entry.predicate_id or rebuilt.predicate_id,
                    predicate_registry_version=entry.predicate_registry_version or rebuilt.predicate_registry_version,
                    policy_scope=entry.policy_scope or rebuilt.policy_scope,
                    edge_record=rebuilt.edge_record,
                    event_hash=rebuilt.event_hash,
                )
            entries.append(entry)
        entries.sort(
            key=lambda item: (
                item.edge_ref,
                item.valid_from_ts,
                item.transaction_time_ts,
                item.event_hash,
            )
        )
        return entries

    @staticmethod
    def _compute_checksum_root(
        *,
        scope_ref: str,
        as_of_ts: str,
        transaction_as_of_ts: str,
        graph_snapshot_manifest_ref: str,
        vector_index_manifest_ref: str,
        predicate_registry_snapshot_ref: str,
        predicate_registry_version: str,
        semantic_contract_bundle_ref: str,
        schema_version_ref: str,
        doctrine_version_ref: str,
        receipt_manifest_refs: list[str],
        migration_manifest_refs: list[str],
        active_edge_manifest: list[dict[str, Any]],
        event_log: list[GraphEdgeEventLogEntry],
    ) -> str:
        return _stable_json_hash(
            {
                "scope_ref": scope_ref,
                "as_of_ts": as_of_ts,
                "transaction_as_of_ts": transaction_as_of_ts,
                "graph_snapshot_manifest_ref": graph_snapshot_manifest_ref,
                "vector_index_manifest_ref": vector_index_manifest_ref,
                "predicate_registry_snapshot_ref": predicate_registry_snapshot_ref,
                "predicate_registry_version": predicate_registry_version,
                "semantic_contract_bundle_ref": semantic_contract_bundle_ref,
                "schema_version_ref": schema_version_ref,
                "doctrine_version_ref": doctrine_version_ref,
                "receipt_manifest_refs": list(receipt_manifest_refs),
                "migration_manifest_refs": list(migration_manifest_refs),
                "active_edge_manifest": active_edge_manifest,
                "event_hashes": [item.event_hash for item in event_log],
            }
        )

    def build_portable_canon_bundle(
        self,
        *,
        scope_ref: str,
        as_of_ts: str | datetime,
        transaction_as_of_ts: str | datetime | None,
        graph_snapshot_manifest_ref: str,
        vector_index_manifest_ref: str,
        predicate_registry_snapshot_ref: str,
        predicate_registry_version: str,
        semantic_contract_bundle_ref: str,
        schema_version_ref: str,
        doctrine_version_ref: str,
        receipt_manifest_refs: list[str],
        migration_manifest_refs: list[str],
        active_edge_manifest: list[Mapping[str, Any]],
        event_log: Optional[list[GraphEdgeEventLogEntry | Mapping[str, Any]]] = None,
    ) -> GraphPortableCanonBundle:
        if not str(scope_ref).strip():
            raise ValueError("portable canon bundle requires scope_ref")
        if not str(graph_snapshot_manifest_ref).startswith("manifest://"):
            raise ValueError("portable canon bundle requires graph_snapshot_manifest_ref")
        if not str(vector_index_manifest_ref).startswith("manifest://"):
            raise ValueError("portable canon bundle requires vector_index_manifest_ref")
        if not str(predicate_registry_snapshot_ref).strip():
            raise ValueError("portable canon bundle requires predicate_registry_snapshot_ref")
        if not str(semantic_contract_bundle_ref).strip():
            raise ValueError("portable canon bundle requires semantic_contract_bundle_ref")
        if not str(schema_version_ref).strip():
            raise ValueError("portable canon bundle requires schema_version_ref")
        if not str(doctrine_version_ref).strip():
            raise ValueError("portable canon bundle requires doctrine_version_ref")
        if not receipt_manifest_refs:
            raise ValueError("portable canon bundle requires receipt_manifest_refs")

        normalized_as_of = _normalize_ts(as_of_ts)
        normalized_transaction_as_of = _normalize_ts(transaction_as_of_ts or normalized_as_of)
        normalized_manifest = self._normalize_active_edge_manifest(active_edge_manifest)
        if event_log is None:
            normalized_event_log = [
                self.build_event_log_entry(
                    manifest_entry=entry,
                    valid_from_ts=normalized_as_of,
                    valid_to_ts=None,
                    transaction_time_ts=normalized_transaction_as_of,
                    event_kind="edge_asserted",
                    edge_status=str(entry.get("edge_status") or "active"),
                )
                for entry in normalized_manifest
            ]
        else:
            normalized_event_log = self._normalize_event_log(event_log)

        merkle_root = _build_merkle_root([entry.event_hash for entry in normalized_event_log])
        checksum_root = self._compute_checksum_root(
            scope_ref=scope_ref,
            as_of_ts=normalized_as_of,
            transaction_as_of_ts=normalized_transaction_as_of,
            graph_snapshot_manifest_ref=str(graph_snapshot_manifest_ref),
            vector_index_manifest_ref=str(vector_index_manifest_ref),
            predicate_registry_snapshot_ref=str(predicate_registry_snapshot_ref),
            predicate_registry_version=str(predicate_registry_version),
            semantic_contract_bundle_ref=str(semantic_contract_bundle_ref),
            schema_version_ref=str(schema_version_ref),
            doctrine_version_ref=str(doctrine_version_ref),
            receipt_manifest_refs=list(receipt_manifest_refs),
            migration_manifest_refs=list(migration_manifest_refs),
            active_edge_manifest=normalized_manifest,
            event_log=normalized_event_log,
        )
        bundle_ref = f"bundle://graphrag/portable-canon/{checksum_root[:24]}"
        return GraphPortableCanonBundle(
            bundle_ref=bundle_ref,
            scope_ref=str(scope_ref),
            as_of_ts=normalized_as_of,
            transaction_as_of_ts=normalized_transaction_as_of,
            graph_snapshot_manifest_ref=str(graph_snapshot_manifest_ref),
            vector_index_manifest_ref=str(vector_index_manifest_ref),
            predicate_registry_snapshot_ref=str(predicate_registry_snapshot_ref),
            predicate_registry_version=str(predicate_registry_version),
            semantic_contract_bundle_ref=str(semantic_contract_bundle_ref),
            schema_version_ref=str(schema_version_ref),
            doctrine_version_ref=str(doctrine_version_ref),
            receipt_manifest_refs=list(receipt_manifest_refs),
            migration_manifest_refs=list(migration_manifest_refs),
            checksum_root=checksum_root,
            merkle_root=merkle_root,
            edge_count=len(normalized_manifest),
            event_count=len(normalized_event_log),
            active_edge_manifest=normalized_manifest,
            event_log=normalized_event_log,
        )

    async def export_bundle_from_store(
        self,
        *,
        store: Any,
        scope_ref: str,
        as_of_ts: str | datetime,
        transaction_as_of_ts: str | datetime | None,
        graph_snapshot_manifest_ref: str,
        vector_index_manifest_ref: str,
        predicate_registry_snapshot_ref: str,
        predicate_registry_version: str,
        semantic_contract_bundle_ref: str,
        schema_version_ref: str,
        doctrine_version_ref: str,
        receipt_manifest_refs: list[str],
        migration_manifest_refs: list[str],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        limit: int = 10000,
    ) -> GraphPortableCanonBundle:
        active_edge_manifest = await store.export_active_edge_manifest(
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            global_only=global_only,
            limit=limit,
        )
        return self.build_portable_canon_bundle(
            scope_ref=scope_ref,
            as_of_ts=as_of_ts,
            transaction_as_of_ts=transaction_as_of_ts,
            graph_snapshot_manifest_ref=graph_snapshot_manifest_ref,
            vector_index_manifest_ref=vector_index_manifest_ref,
            predicate_registry_snapshot_ref=predicate_registry_snapshot_ref,
            predicate_registry_version=predicate_registry_version,
            semantic_contract_bundle_ref=semantic_contract_bundle_ref,
            schema_version_ref=schema_version_ref,
            doctrine_version_ref=doctrine_version_ref,
            receipt_manifest_refs=receipt_manifest_refs,
            migration_manifest_refs=migration_manifest_refs,
            active_edge_manifest=active_edge_manifest,
        )

    def replay_as_of(
        self,
        *,
        bundle: GraphPortableCanonBundle | Mapping[str, Any],
        requested_as_of_ts: str | datetime,
        predicate_registry_snapshot_ref: str,
        schema_version_ref: str,
        doctrine_version_ref: str,
    ) -> GraphLongHorizonReplayResult:
        if isinstance(bundle, GraphPortableCanonBundle):
            bundle_obj = bundle
        else:
            bundle_obj = GraphPortableCanonBundle(
                bundle_ref=str(bundle.get("bundle_ref") or ""),
                scope_ref=str(bundle.get("scope_ref") or ""),
                as_of_ts=str(bundle.get("as_of_ts") or ""),
                transaction_as_of_ts=str(bundle.get("transaction_as_of_ts") or ""),
                graph_snapshot_manifest_ref=str(bundle.get("graph_snapshot_manifest_ref") or ""),
                vector_index_manifest_ref=str(bundle.get("vector_index_manifest_ref") or ""),
                predicate_registry_snapshot_ref=str(bundle.get("predicate_registry_snapshot_ref") or ""),
                predicate_registry_version=str(bundle.get("predicate_registry_version") or ""),
                semantic_contract_bundle_ref=str(bundle.get("semantic_contract_bundle_ref") or ""),
                schema_version_ref=str(bundle.get("schema_version_ref") or ""),
                doctrine_version_ref=str(bundle.get("doctrine_version_ref") or ""),
                receipt_manifest_refs=list(bundle.get("receipt_manifest_refs") or []),
                migration_manifest_refs=list(bundle.get("migration_manifest_refs") or []),
                checksum_root=str(bundle.get("checksum_root") or ""),
                merkle_root=str(bundle.get("merkle_root") or ""),
                edge_count=int(bundle.get("edge_count") or 0),
                event_count=int(bundle.get("event_count") or 0),
                active_edge_manifest=self._normalize_active_edge_manifest(list(bundle.get("active_edge_manifest") or [])),
                event_log=self._normalize_event_log(list(bundle.get("event_log") or [])),
            )

        compatibility_verified = (
            predicate_registry_snapshot_ref == bundle_obj.predicate_registry_snapshot_ref
            and schema_version_ref == bundle_obj.schema_version_ref
            and doctrine_version_ref == bundle_obj.doctrine_version_ref
        )
        if not compatibility_verified:
            return GraphLongHorizonReplayResult(
                replay_status="compatibility_mismatch",
                matched=False,
                reason="registry_or_schema_or_doctrine_mismatch",
                requested_as_of_ts=_normalize_ts(requested_as_of_ts),
                checksum_verified=False,
                merkle_verified=False,
                receipt_refs_verified=False,
                compatibility_verified=False,
                active_edge_count=0,
                reconstructed_active_edges=[],
            )

        checksum_verified = bundle_obj.checksum_root == self._compute_checksum_root(
            scope_ref=bundle_obj.scope_ref,
            as_of_ts=bundle_obj.as_of_ts,
            transaction_as_of_ts=bundle_obj.transaction_as_of_ts,
            graph_snapshot_manifest_ref=bundle_obj.graph_snapshot_manifest_ref,
            vector_index_manifest_ref=bundle_obj.vector_index_manifest_ref,
            predicate_registry_snapshot_ref=bundle_obj.predicate_registry_snapshot_ref,
            predicate_registry_version=bundle_obj.predicate_registry_version,
            semantic_contract_bundle_ref=bundle_obj.semantic_contract_bundle_ref,
            schema_version_ref=bundle_obj.schema_version_ref,
            doctrine_version_ref=bundle_obj.doctrine_version_ref,
            receipt_manifest_refs=bundle_obj.receipt_manifest_refs,
            migration_manifest_refs=bundle_obj.migration_manifest_refs,
            active_edge_manifest=bundle_obj.active_edge_manifest,
            event_log=bundle_obj.event_log,
        )
        merkle_verified = bundle_obj.merkle_root == _build_merkle_root([entry.event_hash for entry in bundle_obj.event_log])
        receipt_refs_verified = all(
            str(entry.created_by_receipt_ref).strip()
            for entry in bundle_obj.event_log
        ) and bool(bundle_obj.receipt_manifest_refs)
        if not checksum_verified or not merkle_verified:
            return GraphLongHorizonReplayResult(
                replay_status="bundle_integrity_failed",
                matched=False,
                reason="checksum_or_merkle_mismatch",
                requested_as_of_ts=_normalize_ts(requested_as_of_ts),
                checksum_verified=checksum_verified,
                merkle_verified=merkle_verified,
                receipt_refs_verified=receipt_refs_verified,
                compatibility_verified=True,
                active_edge_count=0,
                reconstructed_active_edges=[],
            )
        if not receipt_refs_verified:
            return GraphLongHorizonReplayResult(
                replay_status="receipt_verification_failed",
                matched=False,
                reason="missing_receipt_refs",
                requested_as_of_ts=_normalize_ts(requested_as_of_ts),
                checksum_verified=True,
                merkle_verified=True,
                receipt_refs_verified=False,
                compatibility_verified=True,
                active_edge_count=0,
                reconstructed_active_edges=[],
            )

        requested_dt = _parse_ts(_normalize_ts(requested_as_of_ts))
        tx_cutoff = _parse_ts(bundle_obj.transaction_as_of_ts)
        active_by_edge_ref: dict[str, GraphEdgeEventLogEntry] = {}
        for entry in bundle_obj.event_log:
            valid_from = _parse_ts(entry.valid_from_ts)
            valid_to = _parse_ts(entry.valid_to_ts) if entry.valid_to_ts else None
            tx_time = _parse_ts(entry.transaction_time_ts)
            if valid_from > requested_dt:
                continue
            if valid_to is not None and requested_dt >= valid_to:
                continue
            if tx_time > tx_cutoff:
                continue
            current = active_by_edge_ref.get(entry.edge_ref)
            if current is None or (entry.valid_from_ts, entry.transaction_time_ts, entry.event_hash) > (
                current.valid_from_ts,
                current.transaction_time_ts,
                current.event_hash,
            ):
                active_by_edge_ref[entry.edge_ref] = entry

        reconstructed_active_edges = [
            dict(entry.edge_record)
            for entry in active_by_edge_ref.values()
            if str(entry.edge_status).strip().lower() == "active"
        ]
        reconstructed_active_edges = self._normalize_active_edge_manifest(reconstructed_active_edges)
        baseline_match = True
        replay_status = "reconstructed"
        if _normalize_ts(requested_as_of_ts) == bundle_obj.as_of_ts:
            baseline_match = (
                [item["edge_ref"] for item in reconstructed_active_edges]
                == [item["edge_ref"] for item in bundle_obj.active_edge_manifest]
            )
            replay_status = "match" if baseline_match else "baseline_mismatch"
        return GraphLongHorizonReplayResult(
            replay_status=replay_status,
            matched=baseline_match,
            reason="replay_match" if baseline_match else "active_edge_set_mismatch",
            requested_as_of_ts=_normalize_ts(requested_as_of_ts),
            checksum_verified=True,
            merkle_verified=True,
            receipt_refs_verified=True,
            compatibility_verified=True,
            active_edge_count=len(reconstructed_active_edges),
            reconstructed_active_edges=reconstructed_active_edges,
        )


__all__ = [
    "GraphDurabilityRuntime",
    "GraphEdgeEventLogEntry",
    "GraphLongHorizonReplayResult",
    "GraphPortableCanonBundle",
]

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from mica.graphrag.projection_runtime import (
    GraphProjectionDriftSignal,
    GraphProjectionRuntime,
)


def _stable_json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GraphProjectionEdgeManifest:
    scope_ref: str
    authority_source: str
    graph_snapshot_manifest_ref: str
    vector_index_manifest_ref: str
    checkpoint_ref: str
    manifest_hash: str
    edge_count: int
    entries: list[dict[str, Any]]
    projection_ref: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphProjectionRebuildEquivalence:
    rebuild_status: str
    matched: bool
    reason: str
    scope_ref: str
    projection_ref: Optional[str]
    canonical_manifest_hash: str
    rebuilt_manifest_hash: str
    checkpoint_ref: str
    authority_source: str
    reconcile_action: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GraphProjectionRebuildRuntime:
    """Freeze/export/equivalence runtime for rebuildable graph projections.

    Canonical GraphRAG truth stays in Postgres/MUDO. This runtime only exports a
    deterministic manifest from that truth and decides whether a rebuilt
    projection is equivalent enough to be restored.
    """

    @staticmethod
    def build_manifest(
        *,
        scope_ref: str,
        authority_source: str,
        graph_snapshot_manifest_ref: str,
        vector_index_manifest_ref: str,
        checkpoint_ref: str,
        entries: list[dict[str, Any]],
        projection_ref: Optional[str] = None,
    ) -> GraphProjectionEdgeManifest:
        normalized_entries = [dict(entry) for entry in entries]
        normalized_entries.sort(
            key=lambda item: (
                str(item.get("edge_ref") or ""),
                str(item.get("source_node") or ""),
                str(item.get("relationship") or ""),
                str(item.get("target_node") or ""),
            )
        )
        manifest_hash = _stable_json_hash(
            {
                "scope_ref": scope_ref,
                "graph_snapshot_manifest_ref": graph_snapshot_manifest_ref,
                "vector_index_manifest_ref": vector_index_manifest_ref,
                "checkpoint_ref": checkpoint_ref,
                "entries": normalized_entries,
            }
        )
        return GraphProjectionEdgeManifest(
            scope_ref=scope_ref,
            authority_source=authority_source,
            graph_snapshot_manifest_ref=graph_snapshot_manifest_ref,
            vector_index_manifest_ref=vector_index_manifest_ref,
            checkpoint_ref=checkpoint_ref,
            manifest_hash=manifest_hash,
            edge_count=len(normalized_entries),
            entries=normalized_entries,
            projection_ref=projection_ref,
        )

    async def export_canonical_manifest(
        self,
        *,
        store: Any,
        scope_ref: str,
        graph_snapshot_manifest_ref: str,
        vector_index_manifest_ref: str,
        checkpoint_ref: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        global_only: bool = False,
        limit: int = 10000,
    ) -> GraphProjectionEdgeManifest:
        entries = await store.export_active_edge_manifest(
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            global_only=global_only,
            limit=limit,
        )
        return self.build_manifest(
            scope_ref=scope_ref,
            authority_source="canonical_postgres",
            graph_snapshot_manifest_ref=graph_snapshot_manifest_ref,
            vector_index_manifest_ref=vector_index_manifest_ref,
            checkpoint_ref=checkpoint_ref,
            entries=entries,
            projection_ref=None,
        )

    @staticmethod
    def compare_manifests(
        *,
        canonical_manifest: GraphProjectionEdgeManifest,
        rebuilt_manifest: GraphProjectionEdgeManifest,
    ) -> GraphProjectionRebuildEquivalence:
        if rebuilt_manifest.scope_ref != canonical_manifest.scope_ref:
            return GraphProjectionRebuildEquivalence(
                rebuild_status="scope_mismatch",
                matched=False,
                reason="scope_ref_mismatch",
                scope_ref=canonical_manifest.scope_ref,
                projection_ref=rebuilt_manifest.projection_ref,
                canonical_manifest_hash=canonical_manifest.manifest_hash,
                rebuilt_manifest_hash=rebuilt_manifest.manifest_hash,
                checkpoint_ref=canonical_manifest.checkpoint_ref,
                authority_source=rebuilt_manifest.authority_source,
                reconcile_action="quarantine",
            )
        if (
            rebuilt_manifest.graph_snapshot_manifest_ref != canonical_manifest.graph_snapshot_manifest_ref
            or rebuilt_manifest.vector_index_manifest_ref != canonical_manifest.vector_index_manifest_ref
            or rebuilt_manifest.checkpoint_ref != canonical_manifest.checkpoint_ref
        ):
            return GraphProjectionRebuildEquivalence(
                rebuild_status="manifest_binding_mismatch",
                matched=False,
                reason="manifest_binding_mismatch",
                scope_ref=canonical_manifest.scope_ref,
                projection_ref=rebuilt_manifest.projection_ref,
                canonical_manifest_hash=canonical_manifest.manifest_hash,
                rebuilt_manifest_hash=rebuilt_manifest.manifest_hash,
                checkpoint_ref=canonical_manifest.checkpoint_ref,
                authority_source=rebuilt_manifest.authority_source,
                reconcile_action="quarantine",
            )
        matched = rebuilt_manifest.manifest_hash == canonical_manifest.manifest_hash
        return GraphProjectionRebuildEquivalence(
            rebuild_status="equivalent" if matched else "edge_manifest_mismatch",
            matched=matched,
            reason="manifest_match" if matched else "edge_manifest_hash_mismatch",
            scope_ref=canonical_manifest.scope_ref,
            projection_ref=rebuilt_manifest.projection_ref,
            canonical_manifest_hash=canonical_manifest.manifest_hash,
            rebuilt_manifest_hash=rebuilt_manifest.manifest_hash,
            checkpoint_ref=canonical_manifest.checkpoint_ref,
            authority_source=rebuilt_manifest.authority_source,
            reconcile_action="restore" if matched else "quarantine",
        )

    def finalize_rebuild(
        self,
        *,
        projection_runtime: GraphProjectionRuntime,
        canonical_manifest: GraphProjectionEdgeManifest,
        rebuilt_manifest: GraphProjectionEdgeManifest,
    ) -> GraphProjectionRebuildEquivalence:
        comparison = self.compare_manifests(
            canonical_manifest=canonical_manifest,
            rebuilt_manifest=rebuilt_manifest,
        )
        projection_ref = rebuilt_manifest.projection_ref or "projection://unknown"
        if comparison.matched:
            projection_runtime.complete_sync(
                scope_ref=canonical_manifest.scope_ref,
                projection_ref=projection_ref,
                checkpoint_ref=canonical_manifest.checkpoint_ref,
                lag_seconds=0.0,
                replay_backlog=0,
            )
            return comparison

        projection_runtime.report_drift(
            GraphProjectionDriftSignal(
                signal_ref=f"signal://graphrag/rebuild-drift/{canonical_manifest.manifest_hash[:24]}",
                scope_ref=canonical_manifest.scope_ref,
                projection_ref=projection_ref,
                reason=comparison.reason,
            )
        )
        return comparison


__all__ = [
    "GraphProjectionEdgeManifest",
    "GraphProjectionRebuildEquivalence",
    "GraphProjectionRebuildRuntime",
]

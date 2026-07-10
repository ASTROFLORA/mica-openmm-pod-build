from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from mica.infrastructure.unified_backend.database import (
    get_database_connection,
    release_database_connection,
)
from mica.mudo_foundation.contracts import (
    MUDOAssetCreateRequest,
    MUDOCommitCreateRequest,
    MUDODependencyEdgeCreateRequest,
    MUDOFoundationCreateRequest,
    StudyMUDOLinkCreateRequest,
)
from mica.mudo_foundation.service import MUDOFoundationService

logger = logging.getLogger(__name__)


def _artifact_kind_for_ref(artifact_ref: str) -> str:
    ref = str(artifact_ref or "").strip().lower()
    if ref.startswith("cea://") or ref.startswith("budo:"):
        return "identity"
    if ref.endswith((".dcd", ".xtc", ".trr")):
        return "trajectory"
    if ref.endswith((".pdb", ".cif", ".mmcif")):
        return "structure"
    if ref.endswith((".csv", ".tsv")):
        return "table"
    if ref.endswith(".json"):
        return "json"
    if ref.endswith((".png", ".jpg", ".jpeg", ".svg")):
        return "figure"
    if ref.endswith((".md", ".txt", ".log")):
        return "report"
    if ref.startswith("kb://"):
        return "knowledge_base"
    return "artifact"


class MUDOEventSubscriber:
    """Durable consumer for provenance-ready runtime receipts.

    This subscriber is intentionally narrow: it writes study-scoped runtime
    receipts into the canonical MUDO branch as one commit plus one asset per
    artifact reference. Missing durable scope or missing DB connectivity are
    treated as honest blocked outcomes, not as reasons to fall back to the
    in-memory repository.

    P0-D adds only bounded same-batch asset linkage. This is not the full
    `input_refs -> output_refs` ProvenanceWriter contract from the roadmap;
    it just keeps co-produced runtime assets connected until the canonical
    receipt algebra lands.
    """

    def __init__(
        self,
        *,
        get_db_connection_fn: Callable[[], Awaitable[Any | None]] = get_database_connection,
        release_db_connection_fn: Callable[[Any], Awaitable[None]] = release_database_connection,
        service_factory: Callable[[Any], MUDOFoundationService] = MUDOFoundationService.for_db_connection,
    ) -> None:
        self._get_db_connection = get_db_connection_fn
        self._release_db_connection = release_db_connection_fn
        self._service_factory = service_factory
        self._tasks: set[asyncio.Task[Any]] = set()
        self._outcomes: dict[str, dict[str, Any]] = {}

    def bind_event_bus(self, bus: Any) -> None:
        try:
            from .events import MUDOReceiptReady
        except Exception as exc:  # noqa: BLE001
            logger.debug("[MUDO_EVENT_SUBSCRIBER] bind skipped: %s", exc)
            return
        bus.subscribe(MUDOReceiptReady, self._on_mudo_receipt_ready)

    def _on_mudo_receipt_ready(self, event: Any) -> None:
        correlation_id = str(getattr(event, "correlation_id", "") or "").strip()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            outcome = asyncio.run(self._persist_event(event))
            if correlation_id:
                self._outcomes[correlation_id] = outcome
            return

        task = loop.create_task(self._persist_event(event))
        self._tasks.add(task)
        task.add_done_callback(
            lambda completed: self._record_task_outcome(
                correlation_id=correlation_id,
                task=completed,
            )
        )

    def get_outcome(self, correlation_id: str) -> dict[str, Any] | None:
        return self._outcomes.get(str(correlation_id or "").strip())

    async def drain_tasks(self) -> list[Any]:
        """Wait for all currently scheduled persistence tasks.

        Publishers that need durable completion before they return should call
        this instead of reaching into ``_tasks`` directly.
        """
        tasks = list(self._tasks)
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    def _record_task_outcome(
        self,
        *,
        correlation_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        self._tasks.discard(task)
        if not correlation_id:
            return
        try:
            self._outcomes[correlation_id] = dict(task.result() or {})
        except Exception as exc:  # noqa: BLE001
            self._outcomes[correlation_id] = {
                "state": "failed",
                "reason": f"{type(exc).__name__}:{exc}",
            }

    async def _persist_event(self, event: Any) -> dict[str, Any]:
        study_id = str(getattr(event, "study_id", "") or "").strip()
        workspace_id = str(getattr(event, "workspace_id", "") or "").strip()
        owner_user_id = str(getattr(event, "owner_user_id", "") or "").strip()
        protocol_ref = str(getattr(event, "protocol_ref", "") or "").strip()
        input_refs = [str(ref).strip() for ref in list(getattr(event, "input_refs", []) or []) if str(ref).strip()]
        artifact_refs = [str(ref).strip() for ref in list(getattr(event, "artifact_refs", []) or []) if str(ref).strip()]
        evidence_refs = [str(ref).strip() for ref in list(getattr(event, "evidence_refs", []) or []) if str(ref).strip()]
        receipt_kind = str(getattr(event, "receipt_kind", "") or "").strip()
        source_surface = str(getattr(event, "source_surface", "") or "").strip()
        run_id = str(getattr(event, "run_id", "") or "").strip()
        receipt_payload = dict(getattr(event, "receipt_payload", {}) or {})

        if not workspace_id or not owner_user_id:
            logger.warning(
                "[MUDO_EVENT_SUBSCRIBER] blocked %s due to missing scope: study_id=%r workspace_id=%r owner_user_id=%r",
                receipt_kind,
                study_id,
                workspace_id,
                owner_user_id,
            )
            return {"state": "blocked", "reason": "missing_durable_scope"}
        if not artifact_refs:
            logger.warning("[MUDO_EVENT_SUBSCRIBER] blocked %s due to missing artifact_refs", receipt_kind)
            return {"state": "blocked", "reason": "missing_artifact_refs"}

        db_conn = await self._get_db_connection()
        if db_conn is None:
            logger.warning(
                "[MUDO_EVENT_SUBSCRIBER] blocked %s for study %s because no DB connection is available",
                receipt_kind,
                study_id,
            )
            return {"state": "blocked", "reason": "database_unavailable"}

        try:
            service = self._service_factory(db_conn)
            mudo = await self._resolve_target_mudo(
                service=service,
                study_id=study_id,
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                receipt_kind=receipt_kind,
                source_surface=source_surface,
            )

            if mudo is None:
                logger.warning("[MUDO_EVENT_SUBSCRIBER] blocked %s because MUDO resolution failed", receipt_kind)
                return {"state": "blocked", "reason": "mudo_resolution_failed"}

            commit = await service.create_commit(
                mudo.mudo_id,
                MUDOCommitCreateRequest(
                    workspace_id=workspace_id,
                    branch_id=mudo.canonical_branch_id,
                    intent=f"{receipt_kind}:{source_surface or 'runtime'}",
                    artifact_refs=artifact_refs,
                    protocol_ref=protocol_ref or None,
                    metadata={
                        "created_by_slice": "PROYECTO_TOLOMEO/P0-A",
                        "study_id": study_id,
                        "receipt_kind": receipt_kind,
                        "source_surface": source_surface,
                        "run_id": run_id,
                        "evidence_refs": evidence_refs,
                        "receipt_payload": receipt_payload,
                    },
                ),
                owner_user_id=owner_user_id,
            )

            created_assets = []
            for artifact_ref in artifact_refs:
                created_assets.append(
                    await service.attach_asset(
                        mudo.mudo_id,
                        MUDOAssetCreateRequest(
                            workspace_id=workspace_id,
                            branch_id=mudo.canonical_branch_id,
                            commit_id=commit.commit_id,
                            artifact_ref=artifact_ref,
                            artifact_kind=_artifact_kind_for_ref(artifact_ref),
                            metadata={
                                "created_by_slice": "PROYECTO_TOLOMEO/P0-D",
                                "study_id": study_id,
                                "protocol_ref": protocol_ref,
                                "source_surface": source_surface,
                                "run_id": run_id,
                                "evidence_refs": evidence_refs,
                            },
                        ),
                        owner_user_id=owner_user_id,
                    )
                )

            await self._synthesize_same_batch_edges(
                service=service,
                mudo_id=mudo.mudo_id,
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                assets=created_assets,
            )
            await self._synthesize_input_output_edges(
                service=service,
                mudo_id=mudo.mudo_id,
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                input_refs=input_refs,
                output_assets=created_assets,
            )
            return {
                "state": "persisted",
                "mudo_id": mudo.mudo_id,
                "commit_id": commit.commit_id,
                "asset_ids": [asset.asset_id for asset in created_assets],
            }
        finally:
            await self._release_db_connection(db_conn)

    async def _synthesize_same_batch_edges(
        self,
        *,
        service: MUDOFoundationService,
        mudo_id: str,
        workspace_id: str,
        owner_user_id: str,
        assets: list[Any],
    ) -> None:
        if len(assets) < 2:
            return

        primary_asset = assets[0]
        for asset in assets[1:]:
            await service.add_dependency_edge(
                mudo_id,
                MUDODependencyEdgeCreateRequest(
                    workspace_id=workspace_id,
                    from_asset_id=asset.asset_id,
                    to_asset_id=primary_asset.asset_id,
                    relation_type="co_generated_with",
                    stale_propagates=False,
                ),
                owner_user_id=owner_user_id,
            )

    async def _synthesize_input_output_edges(
        self,
        *,
        service: MUDOFoundationService,
        mudo_id: str,
        workspace_id: str,
        owner_user_id: str,
        input_refs: list[str],
        output_assets: list[Any],
    ) -> None:
        if not input_refs or not output_assets:
            return

        existing_assets = await service.list_assets(mudo_id, owner_user_id=owner_user_id)
        created_output_ids = {str(asset.asset_id) for asset in output_assets}
        input_assets_by_ref: dict[str, Any] = {}
        for asset in existing_assets:
            if str(asset.asset_id) in created_output_ids:
                continue
            ref = str(asset.artifact_ref or "").strip()
            if ref and ref not in input_assets_by_ref:
                input_assets_by_ref[ref] = asset

        emitted_pairs: set[tuple[str, str]] = set()
        for output_asset in output_assets:
            output_ref = str(output_asset.artifact_ref or "").strip()
            for input_ref in input_refs:
                input_asset = input_assets_by_ref.get(str(input_ref or "").strip())
                if input_asset is None:
                    continue
                if str(input_asset.asset_id) == str(output_asset.asset_id):
                    continue
                if str(input_asset.artifact_ref or "").strip() == output_ref:
                    continue
                pair = (str(output_asset.asset_id), str(input_asset.asset_id))
                if pair in emitted_pairs:
                    continue
                emitted_pairs.add(pair)
                await service.add_dependency_edge(
                    mudo_id,
                    MUDODependencyEdgeCreateRequest(
                        workspace_id=workspace_id,
                        from_asset_id=output_asset.asset_id,
                        to_asset_id=input_asset.asset_id,
                        relation_type="derived_from",
                        stale_propagates=True,
                    ),
                    owner_user_id=owner_user_id,
                )

    async def _resolve_target_mudo(
        self,
        *,
        service: MUDOFoundationService,
        study_id: str,
        workspace_id: str,
        owner_user_id: str,
        receipt_kind: str,
        source_surface: str,
    ) -> Any | None:
        if study_id:
            links = await service.list_study_mudos(
                study_id,
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
            )
            if links:
                return await service.get_mudo(links[0].mudo_id, owner_user_id=owner_user_id)

            created = await service.create_mudo(
                MUDOFoundationCreateRequest(
                    workspace_id=workspace_id,
                    name=f"Study {study_id} runtime provenance",
                    description="Tolomeo P0 runtime provenance bridge",
                    metadata={
                        "created_by_slice": "PROYECTO_TOLOMEO/P0-C",
                        "runtime_provenance_scope": "study",
                        "source_surface": source_surface,
                        "study_id": study_id,
                    },
                ),
                owner_user_id=owner_user_id,
            )
            await service.link_mudo_to_study(
                study_id,
                StudyMUDOLinkCreateRequest(
                    workspace_id=workspace_id,
                    mudo_id=created.mudo_id,
                    role="primary_object",
                    metadata={"created_by_slice": "PROYECTO_TOLOMEO/P0-C"},
                ),
                owner_user_id=owner_user_id,
            )
            return created

        workspace_mudos = await service.list_workspace_mudos(
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
        )
        for workspace_mudo in workspace_mudos:
            metadata = dict(workspace_mudo.metadata or {})
            if metadata.get("runtime_provenance_scope") == "workspace":
                return workspace_mudo

        return await service.create_mudo(
            MUDOFoundationCreateRequest(
                workspace_id=workspace_id,
                name=f"Workspace {workspace_id} runtime provenance",
                description="Tolomeo P0 workspace runtime provenance bridge",
                metadata={
                    "created_by_slice": "PROYECTO_TOLOMEO/P0-C",
                    "runtime_provenance_scope": "workspace",
                    "source_surface": source_surface,
                    "receipt_kind": receipt_kind,
                },
            ),
            owner_user_id=owner_user_id,
        )

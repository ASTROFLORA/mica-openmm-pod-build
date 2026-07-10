from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Callable, Dict, Optional, Sequence, Set

logger = logging.getLogger(__name__)


class DriverMemoryBackendFacade:
    """Facade for lazy memory backend resolution and session persistence."""

    def __init__(self, *, atom_memory: Any = None, agent_summary_store: Any = None) -> None:
        self._atom_memory = atom_memory
        self._agent_summary_store = agent_summary_store
        self._cache: Dict[str, Any] = {
            "session_repository": None,
            "user_rag_store": None,
            "milvus_user_rag_store": None,
            "graph_store": None,
            "mica_q_multisurface_service": None,
        }
        self._owned_memory_backends: Set[str] = set()

    def _resolve_memory_backend(
        self,
        *,
        cache_attr: str,
        candidate_attrs: Sequence[str],
        factory: Optional[Callable[[], Any]],
        label: str,
    ) -> Any:
        current = self._cache.get(cache_attr)
        if current is not None:
            return current

        for attr in candidate_attrs:
            value = self._cache.get(attr)
            if value is not None:
                self._cache[cache_attr] = value
                return value

        if factory is None:
            return None

        try:
            value = factory()
        except Exception as exc:
            logger.debug("%s unavailable; continuing without that retrieval backend: %s", label, exc)
            return None

        self._cache[cache_attr] = value
        self._owned_memory_backends.add(cache_attr)
        return value

    def resolve_session_repository(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="session_repository",
            candidate_attrs=("_session_repository", "neon_session_repository", "session_repo"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.session_repository",
                fromlist=["NeonSessionRepository"],
            ).NeonSessionRepository(),
            label="NeonSessionRepository",
        )

    def resolve_user_rag_store(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="user_rag_store",
            candidate_attrs=("_user_rag_store", "timescale_user_rag_store"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.timescale_user_rag_store",
                fromlist=["TimescaleUserRAGStore"],
            ).TimescaleUserRAGStore(),
            label="TimescaleUserRAGStore",
        )

    def resolve_milvus_user_rag_store(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="milvus_user_rag_store",
            candidate_attrs=("_milvus_user_rag_store", "milvus_store"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.milvus_user_rag_store",
                fromlist=["MilvusUserRAGStore"],
            ).MilvusUserRAGStore(),
            label="MilvusUserRAGStore",
        )

    def resolve_graph_store(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="graph_store",
            candidate_attrs=("_graph_store", "graphrag_store", "timescale_graphrag_store"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.timescale_graphrag_store",
                fromlist=["TimescaleGraphRAGStore"],
            ).TimescaleGraphRAGStore(),
            label="TimescaleGraphRAGStore",
        )

    def resolve_mica_q_multisurface_service(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="mica_q_multisurface_service",
            candidate_attrs=("_mica_q_multisurface_service", "mica_q_service"),
            factory=lambda: __import__(
                "mica.memory.mica_q_multisurface",
                fromlist=["MICAQMultisurfaceService"],
            ).MICAQMultisurfaceService(graph_store=self.resolve_graph_store()),
            label="MICAQMultisurfaceService",
        )

    def build_memory_retrieval_planner(self, *, agent_memory: Any):
        from mica.infrastructure.persistence.retrieval_planner import RetrievalPlanner

        return RetrievalPlanner(
            session_repository=self.resolve_session_repository(),
            agent_summary_store=self._agent_summary_store,
            agent_memory=agent_memory,
            user_rag_store=self.resolve_user_rag_store(),
            milvus_user_rag_store=self.resolve_milvus_user_rag_store(),
            graph_store=self.resolve_graph_store(),
            atom_memory=self._atom_memory,
            mica_q_service=self.resolve_mica_q_multisurface_service(),
        )

    async def persist_driver_session_start(
        self,
        *,
        session_id: str,
        user_query: str,
        mode: str,
        user_id: Optional[str],
        bucket: Optional[str],
        workspace_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        repo = self.resolve_session_repository()
        if repo is None:
            return

        metadata: Dict[str, Any] = {
            "source": "agentic_driver_direct",
            "status": "running",
        }
        if bucket:
            metadata["bucket"] = bucket
        if workspace_id:
            metadata["workspace_id"] = workspace_id
        if run_id:
            metadata["run_id"] = run_id

        message_metadata = dict(metadata)
        message_metadata["phase"] = "input"

        try:
            await repo.save_session(
                session_id=session_id,
                user_id=(user_id or "direct_driver").strip() or "direct_driver",
                conversation_history=[],
                mode=mode,
                metadata=metadata,
            )
            await repo.append_message(
                session_id=session_id,
                role="user",
                content=user_query,
                metadata=message_metadata,
            )
        except Exception as exc:
            logger.warning("Driver session start persistence failed for %s: %s", session_id, exc)

    async def persist_driver_session_success(
        self,
        *,
        session_id: str,
        result: Dict[str, Any],
        bucket: Optional[str],
        run_id: Optional[str],
    ) -> None:
        repo = self.resolve_session_repository()
        if repo is None:
            return

        final_result = result.get("final_result") if isinstance(result, dict) else result
        try:
            content = (
                final_result
                if isinstance(final_result, str)
                else json.dumps(final_result, ensure_ascii=False, default=str)
            )
        except Exception:
            content = str(final_result)

        metadata: Dict[str, Any] = {
            "source": "agentic_driver_direct",
            "status": "completed",
            "phase": "output",
        }
        if bucket:
            metadata["bucket"] = bucket
        if run_id:
            metadata["run_id"] = run_id

        try:
            await repo.append_message(
                session_id=session_id,
                role="assistant",
                content=content,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("Driver session success persistence failed for %s: %s", session_id, exc)

        protocol_payload: Optional[Dict[str, Any]] = None
        if isinstance(result, dict) and result.get("protocol_run_receipt") and result.get("node_receipts"):
            protocol_payload = result
        elif isinstance(final_result, dict) and final_result.get("protocol_run_receipt") and final_result.get("node_receipts"):
            protocol_payload = final_result

        if protocol_payload:
            try:
                from mica.services.session_persistence import persist_protocol_execution_session_audit

                session = await repo.load_session(session_id)
                session_user_id = str((session or {}).get("user_id") or "").strip() or "direct_driver"
                session_mode = str((session or {}).get("mode") or "production").strip() or "production"
                audit_metadata: Dict[str, Any] = {"status": "completed"}
                if bucket:
                    audit_metadata["bucket"] = bucket
                if run_id:
                    audit_metadata["driver_run_id"] = run_id

                await persist_protocol_execution_session_audit(
                    session_id=session_id,
                    user_id=session_user_id,
                    protocol_id=str(protocol_payload.get("protocol_id") or "").strip(),
                    run_receipt=protocol_payload.get("protocol_run_receipt"),
                    node_receipts=protocol_payload.get("node_receipts") or [],
                    projection_message_ids=protocol_payload.get("projection_message_ids") or [],
                    source="agentic_driver_direct",
                    repo=repo,
                    mode=session_mode,
                    metadata=audit_metadata,
                )
            except Exception as exc:
                logger.warning("Driver protocol audit persistence failed for %s: %s", session_id, exc)

    async def persist_driver_session_failure(
        self,
        *,
        session_id: str,
        exc: Exception,
        bucket: Optional[str],
        run_id: Optional[str],
    ) -> None:
        repo = self.resolve_session_repository()
        if repo is None:
            return

        metadata: Dict[str, Any] = {
            "source": "agentic_driver_direct",
            "status": "failed",
            "phase": "error",
            "error_type": type(exc).__name__,
        }
        if bucket:
            metadata["bucket"] = bucket
        if run_id:
            metadata["run_id"] = run_id

        try:
            await repo.append_message(
                session_id=session_id,
                role="assistant",
                content=f"ERROR: {exc}",
                metadata=metadata,
            )
        except Exception as repo_exc:
            logger.warning("Driver session failure persistence failed for %s: %s", session_id, repo_exc)

    async def close(self) -> None:
        # Close lazily created retrieval backends (best effort)
        for attr in sorted(self._owned_memory_backends):
            backend = self._cache.get(attr)
            if backend is None:
                continue
            close_fn = getattr(backend, "close", None)
            if callable(close_fn):
                try:
                    result = close_fn()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass
            self._cache[attr] = None
        self._owned_memory_backends.clear()

    def get_cache_snapshot(self) -> Dict[str, Any]:
        return {
            "session_repository": self._cache.get("session_repository"),
            "user_rag_store": self._cache.get("user_rag_store"),
            "milvus_user_rag_store": self._cache.get("milvus_user_rag_store"),
            "graph_store": self._cache.get("graph_store"),
        }

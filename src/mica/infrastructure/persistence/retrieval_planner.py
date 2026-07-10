from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect
import logging
import time
from typing import Any, Dict, List, Optional

from mica.memory.contracts import RetrievalMode
from .retrieval_modes import looks_like_mica_q_query, mica_q_default_enabled, select_retrieval_mode


logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
except Exception:  # pragma: no cover - optional dependency
    _otel_trace = None


@contextmanager
def _start_mica_q_span():
    if _otel_trace is None:
        yield None
        return
    tracer = _otel_trace.get_tracer(__name__)
    with tracer.start_as_current_span("mica.retrieval.mica_q_multisurface") as span:
        yield span


@dataclass(frozen=True)
class RetrievalRequest:
    mode: RetrievalMode
    query_text: str
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    agent_name: Optional[str] = None
    collection: str = "default"
    limit: int = 10
    query_embedding: Optional[Any] = None


@dataclass
class RetrievalResponse:
    mode: RetrievalMode
    payload: Dict[str, Any] = field(default_factory=dict)


class RetrievalPlanner:
    """Thin retrieval-mode planner over current MICA stores.

    This is intentionally additive and delegates to existing stable stores.
    """

    def __init__(
        self,
        *,
        session_repository: Any = None,
        agent_summary_store: Any = None,
        agent_memory: Any = None,
        user_rag_store: Any = None,
        milvus_user_rag_store: Any = None,
        graph_store: Any = None,
        atom_memory: Any = None,
        mica_q_service: Any = None,
        enable_graph_fact_augmentation: bool = True,
    ) -> None:
        self.session_repository = session_repository
        self.agent_summary_store = agent_summary_store
        self.agent_memory = agent_memory
        self.user_rag_store = user_rag_store
        self.milvus_user_rag_store = milvus_user_rag_store
        self.graph_store = graph_store
        self.atom_memory = atom_memory
        self.mica_q_service = mica_q_service
        self.enable_graph_fact_augmentation = enable_graph_fact_augmentation

    @staticmethod
    def _split_graph_hits(items: List[Any]) -> tuple[List[Any], List[Any]]:
        edge_hits: List[Any] = []
        fact_hits: List[Any] = []
        for item in items or []:
            if hasattr(item, "result_type"):
                if getattr(item, "result_type", None) == "edge" and getattr(item, "edge", None) is not None:
                    edge_hits.append(getattr(item, "edge"))
                    continue
                if getattr(item, "result_type", None) == "fact" and getattr(item, "fact", None) is not None:
                    fact_hits.append(getattr(item, "fact"))
                    continue
            if isinstance(item, dict):
                if item.get("result_type") == "edge":
                    edge_hits.append(item.get("edge", item))
                    continue
                if item.get("result_type") == "fact":
                    fact_hits.append(item.get("fact", item))
                    continue
                if "source_node" in item:
                    edge_hits.append(item)
                    continue
                if "content" in item:
                    fact_hits.append(item)
                    continue
            if hasattr(item, "source_node"):
                edge_hits.append(item)
            elif hasattr(item, "content"):
                fact_hits.append(item)
        return edge_hits, fact_hits

    @staticmethod
    def _mica_q_default_enabled(explicit: Optional[bool] = None) -> bool:
        return mica_q_default_enabled(explicit)

    @staticmethod
    def _looks_like_mica_q_query(query_text: str) -> bool:
        return looks_like_mica_q_query(str(query_text or "").strip().lower())

    @staticmethod
    def _annotate_mica_q_span(span: Any, *, surface_roots: Dict[str, Any], degraded: List[str], elapsed_ms: float) -> None:
        if span is None or not hasattr(span, "set_attribute"):
            return
        span.set_attribute("mica.retrieval.mode", RetrievalMode.MICA_Q_MULTISURFACE.value)
        span.set_attribute("mica.retrieval.elapsed_ms", float(elapsed_ms))
        span.set_attribute("mica.retrieval.degraded_count", len(list(degraded or [])))
        for key, value in dict(surface_roots or {}).items():
            span.set_attribute(f"mica.retrieval.surface_roots.{key}", str(value))

    @staticmethod
    def _log_mica_q_event(*, surface_roots: Dict[str, Any], degraded: List[str], elapsed_ms: float) -> None:
        logger.info(
            "mica.retrieval.mica_q_multisurface",
            extra={
                "mica_event": {
                    "event": "mica.retrieval.mica_q_multisurface",
                    "mode": RetrievalMode.MICA_Q_MULTISURFACE.value,
                    "surface_roots": dict(surface_roots or {}),
                    "degraded": list(degraded or []),
                    "elapsed_ms": round(float(elapsed_ms), 3),
                }
            },
        )

    def select_mode(
        self,
        query_text: str,
        *,
        prefer_graph: bool = False,
        prefer_temporal: bool = False,
        prefer_session: bool = False,
        prefer_agent_reuse: bool = False,
        prefer_mica_q: bool = False,
        enable_mica_q_by_default: Optional[bool] = None,
        has_workspace_scope: bool = False,
    ) -> RetrievalMode:
        return select_retrieval_mode(
            query_text,
            prefer_graph=prefer_graph,
            prefer_temporal=prefer_temporal,
            prefer_session=prefer_session,
            prefer_agent_reuse=prefer_agent_reuse,
            prefer_mica_q=prefer_mica_q,
            enable_mica_q_by_default=enable_mica_q_by_default,
            has_workspace_scope=has_workspace_scope,
        )

    async def retrieve_auto(
        self,
        *,
        query_text: str,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        collection: str = "default",
        limit: int = 10,
        query_embedding: Optional[Any] = None,
        prefer_graph: bool = False,
        prefer_temporal: bool = False,
        prefer_session: bool = False,
        prefer_agent_reuse: bool = False,
        prefer_mica_q: bool = False,
        has_workspace_scope: bool = False,
        enable_mica_q_by_default: Optional[bool] = None,
    ) -> RetrievalResponse:
        explicit_mode_requested = any((prefer_graph, prefer_temporal, prefer_session, prefer_agent_reuse))
        bias_toward_mica_q = self.mica_q_service is not None and (
            prefer_mica_q
            or (
                self._mica_q_default_enabled(enable_mica_q_by_default)
                and self._looks_like_mica_q_query(query_text)
            )
        )

        if explicit_mode_requested:
            mode = self.select_mode(
                query_text,
                prefer_graph=prefer_graph,
                prefer_temporal=prefer_temporal,
                prefer_session=prefer_session,
                prefer_agent_reuse=prefer_agent_reuse,
                enable_mica_q_by_default=False,
                has_workspace_scope=has_workspace_scope,
            )
        elif bias_toward_mica_q:
            mode = RetrievalMode.MICA_Q_MULTISURFACE
        else:
            mode = self.select_mode(
                query_text,
                prefer_graph=prefer_graph,
                prefer_temporal=prefer_temporal,
                prefer_session=prefer_session,
                prefer_agent_reuse=prefer_agent_reuse,
                enable_mica_q_by_default=False,
                has_workspace_scope=has_workspace_scope,
            )

        return await self.retrieve(
            RetrievalRequest(
                mode=mode,
                query_text=query_text,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                agent_name=agent_name,
                collection=collection,
                limit=limit,
                query_embedding=query_embedding,
            )
        )

    @staticmethod
    def _query_overlap(a: str, b: str) -> float:
        wa = set((a or "").lower().split())
        wb = set((b or "").lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / max(len(wa), len(wb))

    @staticmethod
    def _require_personal_scope(request: RetrievalRequest) -> None:
        if not request.user_id:
            raise ValueError("user_id required for personal retrieval modes")
        if not request.workspace_id:
            raise ValueError("workspace_id required for personal retrieval modes")

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        if request.mode == RetrievalMode.SESSION_CONTINUITY:
            if not request.session_id:
                raise ValueError("session_id required for SESSION_CONTINUITY")
            session = None
            degraded: List[str] = []
            if self.session_repository is not None:
                try:
                    session = await self.session_repository.load_session(request.session_id)
                except Exception:
                    degraded.append("session_repository_unavailable")
            payload = {"session": session}
            if degraded:
                payload["degraded"] = degraded
            return RetrievalResponse(mode=request.mode, payload=payload)

        if request.mode == RetrievalMode.AGENT_REUSE:
            self._require_personal_scope(request)
            summaries = []
            degraded: List[str] = []
            if self.agent_summary_store is not None:
                try:
                    if hasattr(self.agent_summary_store, "list_active_scoped"):
                        summaries = self.agent_summary_store.list_active_scoped(
                            user_id=request.user_id,
                            workspace_id=request.workspace_id,
                            agent_type=request.agent_name,
                        )
                    else:
                        summaries = self.agent_summary_store.list_active(
                            workspace_id=request.workspace_id,
                            agent_type=request.agent_name,
                        )
                except Exception:
                    degraded.append("agent_summary_store_unavailable")
            recalled = []
            if self.agent_memory is not None and request.agent_name:
                try:
                    recalled = self.agent_memory.recall(request.agent_name, request.query_text, top_k=request.limit, min_similarity=0.0)
                except Exception:
                    degraded.append("agent_memory_unavailable")
            payload = {"summaries": summaries[: request.limit], "agent_memory": recalled[: request.limit]}
            if degraded:
                payload["degraded"] = degraded
            return RetrievalResponse(mode=request.mode, payload=payload)

        if request.mode == RetrievalMode.USER_WORKING_SET:
            self._require_personal_scope(request)
            timescale_hits = []
            milvus_hits = []
            degraded: List[str] = []
            if self.user_rag_store is not None:
                try:
                    timescale_hits = await self.user_rag_store.search_chunks_hybrid(
                        user_id=request.user_id,
                        query_text=request.query_text,
                        query_embedding=request.query_embedding,
                        collection=request.collection,
                        limit=request.limit,
                    )
                except Exception:
                    degraded.append("timescale_user_rag_unavailable")
            if self.milvus_user_rag_store is not None:
                try:
                    milvus_hits = await self.milvus_user_rag_store.search_chunks(
                        user_id=request.user_id,
                        query_text=request.query_text,
                        collection=request.collection,
                        session_id=request.session_id,
                        query_embedding=request.query_embedding,
                        limit=request.limit,
                    )
                except Exception:
                    degraded.append("milvus_user_rag_unavailable")
            payload = {"timescale_hits": timescale_hits, "milvus_hits": milvus_hits}
            if degraded:
                payload["degraded"] = degraded
            return RetrievalResponse(mode=request.mode, payload=payload)

        if request.mode == RetrievalMode.GLOBAL_SCIENCE:
            graph_hits = []
            edge_hits = []
            fact_hits = []
            degraded: List[str] = []
            if self.graph_store is not None:
                try:
                    if hasattr(self.graph_store, "search_graph_hybrid"):
                        graph_hits = await self.graph_store.search_graph_hybrid(
                            query_text=request.query_text,
                            query_embedding=request.query_embedding,
                            limit=request.limit,
                            global_only=True,
                        )
                        edge_hits, fact_hits = self._split_graph_hits(graph_hits)
                    else:
                        edge_hits = await self.graph_store.search_edges_hybrid(
                            query_text=request.query_text,
                            query_embedding=request.query_embedding,
                            limit=request.limit,
                            user_id=request.user_id,
                        )
                        graph_hits = edge_hits
                except Exception:
                    degraded.append("graph_store_unavailable")
            payload = {"graph_hits": graph_hits, "edge_hits": edge_hits, "fact_hits": fact_hits}
            if degraded:
                payload["degraded"] = degraded
            return RetrievalResponse(mode=request.mode, payload=payload)

        if request.mode == RetrievalMode.TEMPORAL_FACTS:
            facts = []
            graph_facts = []
            degraded: List[str] = []
            if self.atom_memory is not None:
                try:
                    facts = await self.atom_memory.query_temporal_facts(entity=request.query_text, limit=request.limit, beam_width=max(2, request.limit))
                except Exception:
                    degraded.append("atom_memory_unavailable")
            if self.enable_graph_fact_augmentation and self.graph_store is not None and hasattr(self.graph_store, "search_facts_hybrid"):
                try:
                    graph_facts = await self.graph_store.search_facts_hybrid(
                        query_text=request.query_text,
                        query_embedding=request.query_embedding,
                        limit=request.limit,
                        user_id=request.user_id,
                        session_id=request.session_id,
                        workspace_id=request.workspace_id,
                    )
                except Exception:
                    degraded.append("graph_fact_store_unavailable")
            payload = {"facts": facts, "graph_facts": graph_facts}
            if degraded:
                payload["degraded"] = degraded
            return RetrievalResponse(mode=request.mode, payload=payload)

        if request.mode == RetrievalMode.GRAPH_EXPLANATION:
            graph_hits = []
            edge_hits = []
            fact_hits = []
            hop_hits = []
            degraded: List[str] = []
            if self.graph_store is not None:
                try:
                    if hasattr(self.graph_store, "search_graph_hybrid"):
                        graph_hits = await self.graph_store.search_graph_hybrid(
                            query_text=request.query_text,
                            query_embedding=request.query_embedding,
                            limit=request.limit,
                            user_id=request.user_id,
                            session_id=request.session_id,
                            workspace_id=request.workspace_id,
                            global_only=not bool(request.user_id or request.session_id or request.workspace_id),
                        )
                        edge_hits, fact_hits = self._split_graph_hits(graph_hits)
                    else:
                        edge_hits = await self.graph_store.search_edges_hybrid(
                            query_text=request.query_text,
                            query_embedding=request.query_embedding,
                            limit=request.limit,
                            user_id=request.user_id,
                            session_id=request.session_id,
                        )
                        graph_hits = edge_hits
                    seed_nodes: List[str] = []
                    for hit in edge_hits:
                        seed_nodes.extend([getattr(hit, "source_node", ""), getattr(hit, "target_node", "")])
                    seed_nodes = [n for n in dict.fromkeys(seed_nodes) if n]
                    if seed_nodes:
                        hop_hits = await self.graph_store.hop1_edges(
                            seed_nodes=seed_nodes,
                            limit=request.limit,
                            user_id=request.user_id,
                            session_id=request.session_id,
                            workspace_id=request.workspace_id,
                            global_only=not bool(request.user_id or request.session_id or request.workspace_id),
                        )
                except Exception:
                    degraded.append("graph_store_unavailable")
            payload = {"graph_hits": graph_hits, "edge_hits": edge_hits, "fact_hits": fact_hits, "hop_hits": hop_hits}
            if degraded:
                payload["degraded"] = degraded
            return RetrievalResponse(mode=request.mode, payload=payload)

        if request.mode == RetrievalMode.MICA_Q_MULTISURFACE:
            degraded: List[str] = []
            payload: Dict[str, Any] = {}
            started = time.perf_counter()
            with _start_mica_q_span() as span:
                if self.mica_q_service is None:
                    degraded.append("mica_q_service_unavailable")
                else:
                    try:
                        payload = await self.mica_q_service.search(
                            query_text=request.query_text,
                            limit=request.limit,
                            query_embedding=request.query_embedding,
                            user_id=request.user_id,
                            workspace_id=request.workspace_id,
                            session_id=request.session_id,
                        )
                    except Exception:
                        degraded.append("mica_q_service_unavailable")
                        payload = {}
                payload = dict(payload or {})
                if degraded:
                    payload["degraded"] = list(dict.fromkeys(list(payload.get("degraded") or []) + degraded))
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                surface_roots = dict(payload.get("surface_roots") or {})
                self._annotate_mica_q_span(
                    span,
                    surface_roots=surface_roots,
                    degraded=list(payload.get("degraded") or []),
                    elapsed_ms=elapsed_ms,
                )
                self._log_mica_q_event(
                    surface_roots=surface_roots,
                    degraded=list(payload.get("degraded") or []),
                    elapsed_ms=elapsed_ms,
                )
                return RetrievalResponse(mode=request.mode, payload=payload)

        raise ValueError(f"Unsupported retrieval mode: {request.mode}")

    def build_agent_reuse_context(
        self,
        *,
        agent_name: str,
        query_text: str,
        user_id: str,
        workspace_id: str,
        max_chars: int = 1500,
    ) -> Optional[str]:
        summaries = []
        if self.agent_summary_store is not None:
            if hasattr(self.agent_summary_store, "list_active_scoped"):
                summaries = self.agent_summary_store.list_active_scoped(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    agent_type=agent_name,
                )
            else:
                summaries = self.agent_summary_store.list_active(workspace_id=workspace_id, agent_type=agent_name)

        scored = []
        for summary in summaries:
            sim = self._query_overlap(query_text, summary.query_summary)
            if sim > 0:
                scored.append((sim, summary))
        scored.sort(key=lambda x: x[0], reverse=True)

        parts: List[str] = []
        chars = 0
        if scored:
            parts.append("[PRIOR AGENT SUMMARY CONTEXT]")
            chars = len(parts[0])
            for _score, summary in scored[:3]:
                chunk = f"[Prior summary — {summary.query_summary}]\nSynthesis: {summary.synthesis[:400]}"
                if chars + len(chunk) > max_chars:
                    break
                parts.append(chunk)
                chars += len(chunk)

        fallback = None
        if self.agent_memory is not None:
            fallback = self.agent_memory.recall_context_injection(agent_name, query_text, max_chars=max_chars)

        text = "\n\n".join(parts).strip()
        if text and fallback:
            return text + "\n\n" + fallback
        return text or fallback

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _entry_synthesis(entry: Any) -> str:
        if hasattr(entry, "synthesis"):
            return str(getattr(entry, "synthesis") or "")
        if isinstance(entry, dict):
            return str(entry.get("synthesis") or "")
        return ""

    async def build_mode_context(
        self,
        *,
        query_text: str,
        user_id: str,
        workspace_id: str,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        max_chars: int = 1500,
    ) -> Optional[str]:
        mode = self.select_mode(
            query_text,
            prefer_agent_reuse=bool(agent_name),
            has_workspace_scope=True,
        )
        if mode != RetrievalMode.AGENT_REUSE:
            return None

        response = await self.retrieve(
            RetrievalRequest(
                mode=mode,
                query_text=query_text,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                agent_name=agent_name,
                limit=3,
            )
        )

        summaries = list(response.payload.get("summaries") or [])
        recalled = list(response.payload.get("agent_memory") or [])

        parts: List[str] = []
        chars = 0
        if summaries:
            parts.append("[PRIOR AGENT SUMMARY CONTEXT]")
            chars = len(parts[0])
            for summary in summaries[:3]:
                query_summary = getattr(summary, "query_summary", "") or (summary.get("query_summary", "") if isinstance(summary, dict) else "")
                synthesis = self._entry_synthesis(summary)
                chunk = f"[Prior summary — {query_summary}]\nSynthesis: {synthesis[:400]}"
                if chars + len(chunk) > max_chars:
                    break
                parts.append(chunk)
                chars += len(chunk)

        if recalled:
            if not parts:
                parts.append("[PRIOR AGENT MEMORY CONTEXT]")
                chars = len(parts[0])
            for entry in recalled[:3]:
                synthesis = self._entry_synthesis(entry)
                if not synthesis:
                    continue
                chunk = f"[Prior memory]\nSynthesis: {synthesis[:350]}"
                if chars + len(chunk) > max_chars:
                    break
                parts.append(chunk)
                chars += len(chunk)

        text = "\n\n".join(parts).strip()
        if text:
            return text
        if self.agent_memory is not None and agent_name:
            fallback = self.agent_memory.recall_context_injection(agent_name, query_text, max_chars=max_chars)
            return await self._maybe_await(fallback)
        return None


__all__ = ["RetrievalPlanner", "RetrievalRequest", "RetrievalResponse", "select_retrieval_mode"]
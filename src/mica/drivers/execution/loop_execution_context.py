"""
Loop Execution Context — extraction target for DD-CS-014 (Domain F preamble)

Captures the state initialization block of _build_loop_executor into a
single injectable context object.  Mutable attributes replace `nonlocal`
variables so nested closures can still read/write shared state.
"""

from __future__ import annotations

import asyncio
import re as _re
from typing import Any, Callable, Awaitable, Dict, List, Optional, Sequence


class LoopExecutionContext:
    """Mutable context bag for all nested closures inside _build_loop_executor."""

    __slots__ = (
        "user_id",
        "session_id",
        "provider_id",
        "model_id",
        "abort",
        "reinjection_packet",
        "literature_service",
        "dependency_probe_cache",
        "dependency_probe_ttl_s",
        "backend_native_tool_names",
        "backend_native_executor",
        "agent_memory",
        "summary_store",
        "workspace_id",
        "parent_run_id",
        "active_session_id",
        "retrieval_planner",
        "last_bibliotecario_state",
        "driver_literature_sources",
        "negative_memory_context",
        "latest_pipeline_outputs",
        "_driver_ref",        # weak back-reference to AgenticDriver for self.* calls
    )

    def __init__(
        self,
        *,
        user_id: str,
        session_id: Optional[str],
        provider_id: str,
        model_id: Optional[str],
        abort: Optional[asyncio.Event],
        reinjection_packet: Optional[Dict[str, Any]],
        backend_native_tool_names: Optional[set] = None,
        driver_literature_sources: Optional[List[str]] = None,
        dependency_probe_ttl_s: float = 120.0,
        driver_ref: Any = None,
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.provider_id = provider_id
        self.model_id = model_id
        self.abort = abort
        self.reinjection_packet = reinjection_packet

        # Mutable state — replaces `nonlocal` closures
        self.literature_service: Any = None
        self.dependency_probe_cache: Dict[str, Dict[str, Any]] = {}
        self.dependency_probe_ttl_s = dependency_probe_ttl_s
        self.backend_native_tool_names = backend_native_tool_names or {
            "search_institutional_knowledge",
            "read_workspace_file_content",
            "publish_operator_directive",
        }
        self.backend_native_executor: Optional[Callable[[str, str, Dict[str, Any]], Awaitable[str]]] = None

        # Agent memory
        self.agent_memory: Any = None
        self.summary_store: Any = None
        self.workspace_id: str = ""
        self.parent_run_id: str = ""
        self.active_session_id: str = str(session_id or user_id or "default")
        self.retrieval_planner: Any = None
        self.last_bibliotecario_state: Dict[str, Any] = {}
        self.driver_literature_sources = driver_literature_sources or ["semantic_scholar", "pubmed", "openalex"]
        self.negative_memory_context: Dict[str, Any] = {}
        self.latest_pipeline_outputs: Dict[str, Any] = {}

        self._driver_ref = driver_ref

    # ------------------------------------------------------------------
    # Shared helpers previously defined as nested closures
    # ------------------------------------------------------------------

    @staticmethod
    def shorten_query(q: str, max_words: int = 6) -> str:
        """Truncate a query string for display/logging (was _shorten_query)."""
        q = _re.sub(r'\([^)]{20,}\)', '', q)
        q = _re.sub(r'[,;]+', ' ', q)
        q = ' '.join(q.split())
        words = q.split()
        return ' '.join(words[:max_words]) if len(words) > max_words else q

    async def get_literature_service(self) -> Any:
        """Lazy-init LiteratureSearchService (was _get_literature_service)."""
        if self.literature_service is None:
            from mica.services.literature_search_service import LiteratureSearchService
            driver = self._driver_ref
            atom = getattr(driver, "atom_memory", None) if driver else None
            self.literature_service = LiteratureSearchService(
                atom_system=atom,
                enable_atom_persistence=atom is not None,
                enable_event_persistence=True,
                persistence_user_id=self.active_session_id,
            )
        return self.literature_service

    async def get_backend_native_executor(self) -> Callable[[str, str, Dict[str, Any]], Awaitable[str]]:
        """Lazy-init the native backend executor (was _get_backend_native_executor)."""
        if self.backend_native_executor is None and self._driver_ref:
            self.backend_native_executor = await self._driver_ref._create_backend_native_executor(
                user_id=self.user_id,
                session_id=self.session_id,
            )
        return self.backend_native_executor or (lambda a, b, c: "")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Factory — wires AgenticDriver state into the context
    # ------------------------------------------------------------------

    @classmethod
    def from_driver(
        cls,
        driver: Any,
        *,
        user_id: str = "agent",
        session_id: Optional[str] = None,
        provider_id: str = "anthropic",
        model_id: Optional[str] = None,
        abort: Optional[asyncio.Event] = None,
        reinjection_packet: Optional[Dict[str, Any]] = None,
    ) -> "LoopExecutionContext":
        """Create context pre-populated from the driver's current state."""
        ctx = cls(
            user_id=user_id,
            session_id=session_id,
            provider_id=provider_id,
            model_id=model_id,
            abort=abort,
            reinjection_packet=reinjection_packet,
            driver_ref=driver,
        )

        # Agent memory
        from mica.drivers.persistence.agent_memory import AgentMemory
        ctx.agent_memory = AgentMemory(session_id=user_id or "default")
        ctx.summary_store = getattr(driver, "agent_summary_store", None)
        ctx.workspace_id = driver._derive_summary_workspace_id(user_id) if hasattr(driver, "_derive_summary_workspace_id") else user_id

        session_run_ids = getattr(driver, "_session_run_ids", None) or {}
        ctx.parent_run_id = session_run_ids.get(session_id or "", "")
        if not ctx.parent_run_id and session_run_ids:
            ctx.parent_run_id = next(reversed(session_run_ids.values()), "")
        ctx.active_session_id = str(session_id or ctx.parent_run_id or user_id or "default")

        if hasattr(driver, "_get_or_create_evidence_ledger"):
            driver._get_or_create_evidence_ledger(ctx.active_session_id, ctx.parent_run_id or ctx.active_session_id)

        ctx.retrieval_planner = (
            driver._build_memory_retrieval_planner(agent_memory=ctx.agent_memory, workspace_id=ctx.workspace_id)
            if hasattr(driver, "_build_memory_retrieval_planner") else None
        )

        ctx.negative_memory_context = (
            driver._extract_negative_memory_context(reinjection_packet)
            if hasattr(driver, "_extract_negative_memory_context") else {}
        )

        latest = getattr(driver, "_latest_stream_pipeline_outputs", None)
        if latest is None:
            driver._latest_stream_pipeline_outputs = {}
            latest = driver._latest_stream_pipeline_outputs
        ctx.latest_pipeline_outputs = latest
        ctx.latest_pipeline_outputs[ctx.active_session_id] = None

        return ctx

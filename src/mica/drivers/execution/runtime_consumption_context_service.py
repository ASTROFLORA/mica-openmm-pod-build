"""Runtime consumption context assembly extracted from AgenticDriver."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


async def build_runtime_consumption_context(
    driver_self: Any,
    *,
    query: str,
    session_id: Optional[str],
    user_id: Optional[str],
    workspace_id: Optional[str],
    limit: int = 4,
    force: bool = False,
) -> Dict[str, Any]:
    if not force and not driver_self._should_build_runtime_consumption_context(query):
        return {
            "enabled": False,
            "state": "skipped",
            "reason": "query_profile_not_supported",
            "graph_hit_count": 0,
            "fact_hit_count": 0,
            "consumption_closure": {
                "graph_retrieval_state": "skipped",
                "graph_hit_count": 0,
                "fact_hit_count": 0,
                "lmp_closure": {"state": "skipped", "reason": "runtime_preconsumption_not_requested"},
            },
        }
    try:
        from mica.infrastructure.persistence.retrieval_planner import RetrievalRequest
        from mica.memory.contracts import RetrievalMode

        planner = driver_self._build_memory_retrieval_planner(
            agent_memory=getattr(driver_self, "agent_memory", None),
            workspace_id=str(workspace_id or session_id or "runtime"),
        )
        if driver_self.atom_memory is not None:
            try:
                await driver_self.atom_memory.load_persistent_state()
            except Exception:
                pass
        graph_response = await planner.retrieve(
            RetrievalRequest(
                mode=RetrievalMode.GRAPH_EXPLANATION,
                query_text=query,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                agent_name="agentic_driver",
                limit=max(1, int(limit)),
            )
        )
        temporal_response = await planner.retrieve(
            RetrievalRequest(
                mode=RetrievalMode.TEMPORAL_FACTS,
                query_text=query,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                agent_name="agentic_driver",
                limit=max(1, int(limit)),
            )
        )
        graph_payload = dict(graph_response.payload or {})
        temporal_payload = dict(temporal_response.payload or {})
        edge_hits = [driver_self._serialize_runtime_consumption_item(item) for item in list(graph_payload.get("edge_hits") or [])[:limit]]
        graph_facts = [driver_self._serialize_runtime_consumption_item(item) for item in list(temporal_payload.get("graph_facts") or [])[:limit]]
        atom_facts = [driver_self._serialize_runtime_consumption_item(item) for item in list(temporal_payload.get("facts") or [])[:limit]]
        hop_hits = [driver_self._serialize_runtime_consumption_item(item) for item in list(graph_payload.get("hop_hits") or [])[:limit]]
        degraded = list(dict.fromkeys(list(graph_payload.get("degraded") or []) + list(temporal_payload.get("degraded") or [])))
        graph_hit_count = int(len(list(graph_payload.get("graph_hits") or [])) or len(edge_hits))
        fact_hit_count = int(len(list(temporal_payload.get("facts") or [])) + len(list(temporal_payload.get("graph_facts") or [])))
        graph_state = "succeeded" if graph_hit_count > 0 else ("degraded" if degraded else "attempted")
        fact_state = "succeeded" if fact_hit_count > 0 else ("degraded" if degraded else "attempted")
        state = "succeeded" if graph_hit_count > 0 or fact_hit_count > 0 else ("degraded" if degraded else "attempted")
        runtime_context = {
            "enabled": True,
            "state": state,
            "query": query,
            "graph_hit_count": graph_hit_count,
            "fact_hit_count": fact_hit_count,
            "edge_hits": edge_hits,
            "graph_facts": graph_facts,
            "atom_facts": atom_facts,
            "hop_hits": hop_hits,
            "degraded": degraded,
            "consumption_closure": {
                "graph_retrieval_state": graph_state,
                "fact_retrieval_state": fact_state,
                "graph_hit_count": graph_hit_count,
                "fact_hit_count": fact_hit_count,
                "lmp_closure": {"state": "skipped", "reason": "runtime_preconsumption_only"},
            },
        }
        runtime_context["prompt_block"] = driver_self._build_runtime_consumption_prompt_block(runtime_context)
        return runtime_context
    except Exception as exc:
        logger.debug("Runtime pre-consumption unavailable for '%s': %s", query, exc)
        return {
            "enabled": True,
            "state": "degraded",
            "query": query,
            "graph_hit_count": 0,
            "fact_hit_count": 0,
            "edge_hits": [],
            "graph_facts": [],
            "atom_facts": [],
            "hop_hits": [],
            "degraded": [str(exc)],
            "consumption_closure": {
                "graph_retrieval_state": "degraded",
                "fact_retrieval_state": "degraded",
                "graph_hit_count": 0,
                "fact_hit_count": 0,
                "lmp_closure": {"state": "skipped", "reason": "runtime_preconsumption_failed"},
            },
        }
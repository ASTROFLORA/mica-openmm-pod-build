"""Bridge helpers exposing GraphRAG (Neo4j + Milvus) capabilities to the RAG orchestrator."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ..query_engine import (
    BSMQuery,
    BSMQueryEngine,
    QueryPriority,
    QueryType,
    create_bsm_query_engine,
)


class GraphRAGBridge:
    """Async helper that exposes the BSMQueryEngine as a GraphRAG provider."""

    def __init__(self, engine: Optional[BSMQueryEngine] = None) -> None:
        self._engine = engine
        self._engine_lock = asyncio.Lock()

    async def _get_engine(self) -> BSMQueryEngine:
        if self._engine is not None:
            return self._engine
        async with self._engine_lock:
            if self._engine is None:
                self._engine = await create_bsm_query_engine()
        assert self._engine is not None
        return self._engine

    async def run_query(
        self,
        query_text: str,
        *,
        query_type: Optional[QueryType] = None,
        priority: QueryPriority = QueryPriority.MEDIUM,
        parameters: Optional[Dict[str, Any]] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a hybrid GraphRAG query and return serialized results."""

        engine = await self._get_engine()
        if query_type is None:
            query = engine.interpreter.interpret_query(query_text)
            query.priority = priority
            if parameters:
                query.parameters.update(parameters)
            if filters:
                query.filters.update(filters)
        else:
            query = BSMQuery(
                query_text=query_text,
                query_type=query_type,
                priority=priority,
                parameters=parameters or {},
                filters=filters or {},
            )

        result = await engine.execute_query(query)
        return {
            "query_id": result.query_id,
            "query_type": result.query_type.value,
            "execution_time": result.execution_time,
            "confidence_score": result.confidence_score,
            "semantic_results": [r.to_dict() if hasattr(r, "to_dict") else r for r in result.semantic_results],
            "graph_results": result.graph_results,
            "combined_results": result.combined_results,
            "metadata": result.metadata,
        }


__all__ = ["GraphRAGBridge"]

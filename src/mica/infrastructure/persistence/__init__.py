"""persistence - Database Persistence Adapters.

This package intentionally lazy-loads public exports so importing one
submodule does not eagerly bootstrap every optional persistence backend.
"""

from __future__ import annotations

from importlib import import_module

_EXPORT_TO_MODULE = {
    "JobStoreABC": ".timescale_job_store",
    "TimescaleJobStore": ".timescale_job_store",
    "InMemoryJobStore": ".timescale_job_store",
    "TimescaleEventStore": ".timescale_event_store",
    "SessionRepositoryABC": ".session_repository",
    "NeonSessionRepository": ".session_repository",
    "InMemorySessionRepository": ".session_repository",
    "BudoGraphWriter": ".budo_graph_writer",
    "BudoGraphWriterFactory": ".budo_graph_writer",
    "upsert_budo_sync": ".budo_graph_writer",
    "BudoNeo4jWriter": ".budo_neo4j_writer",
    "BudoNeo4jWriterFactory": ".budo_neo4j_writer",
    "upsert_budo_neo4j_sync": ".budo_neo4j_writer",
    "DualWriteCoordinator": ".budo_neo4j_writer",
    "UnifiedQueryEngine": ".unified_query_engine",
    "UnifiedQueryEngineFactory": ".unified_query_engine",
    "ReciprocalRankFusion": ".unified_query_engine",
    "SearchResult": ".unified_query_engine",
    "QueryFilters": ".unified_query_engine",
    "RetrievalPlanner": ".retrieval_planner",
    "RetrievalRequest": ".retrieval_planner",
    "RetrievalResponse": ".retrieval_planner",
}

__all__ = list(_EXPORT_TO_MODULE)


def __getattr__(name: str):
    module_name = _EXPORT_TO_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

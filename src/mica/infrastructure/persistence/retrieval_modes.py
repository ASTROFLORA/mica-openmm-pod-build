from __future__ import annotations

import os

from mica.memory.contracts import RetrievalMode


_MICA_Q_PREFIXES = (
    "id:",
    "lmp:",
    "budo:",
    "lit:",
    "dlm:",
)
_MICA_Q_TERMS = (
    "uniprot",
    "pdb",
    "isoform",
    "lmp",
    "nesymol",
    "motif",
    "ptm",
    "phosphosite",
    "residue",
    "binding pocket",
    "active site",
    "conformation",
    "secondary structure",
    "domain architecture",
    "bibliotecario",
    "literature",
    "full text",
    "pubmed",
    "semantic scholar",
    "openalex",
    "imported structure",
    "graph repair",
    "graph export",
    "section index",
    "citation chase",
    "deep synthesis",
    "deep research",
    "dlm",
    "materialized kb",
    "kb materialization",
)


def mica_q_default_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return str(os.getenv("MICA_Q_DEFAULT_MULTISURFACE", "1") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def looks_like_mica_q_query(text: str) -> bool:
    return any(prefix in text for prefix in _MICA_Q_PREFIXES) or any(term in text for term in _MICA_Q_TERMS)


def select_retrieval_mode(
    query_text: str,
    *,
    prefer_graph: bool = False,
    prefer_temporal: bool = False,
    prefer_session: bool = False,
    prefer_agent_reuse: bool = False,
    prefer_mica_q: bool = False,
    enable_mica_q_by_default: bool | None = None,
    has_workspace_scope: bool = False,
) -> RetrievalMode:
    """Small heuristic selector for the additive retrieval-mode contract.

    This intentionally stays simple and testable. It does not replace higher-
    level routing; it provides a stable contract for memory-aware retrieval.
    """
    text = (query_text or "").lower()

    if prefer_session:
        return RetrievalMode.SESSION_CONTINUITY
    if prefer_agent_reuse:
        return RetrievalMode.AGENT_REUSE
    if prefer_temporal or any(k in text for k in ("timeline", "temporal", "recent facts", "history of")):
        return RetrievalMode.TEMPORAL_FACTS
    if has_workspace_scope or any(k in text for k in ("my papers", "my notes", "workspace", "uploaded", "project")):
        return RetrievalMode.USER_WORKING_SET
    if prefer_mica_q or (mica_q_default_enabled(enable_mica_q_by_default) and looks_like_mica_q_query(text)):
        return RetrievalMode.MICA_Q_MULTISURFACE
    if prefer_graph or any(k in text for k in ("pathway", "relationship", "graph", "neighbors", "mechanism")):
        return RetrievalMode.GRAPH_EXPLANATION
    return RetrievalMode.GLOBAL_SCIENCE


__all__ = ["mica_q_default_enabled", "looks_like_mica_q_query", "select_retrieval_mode"]
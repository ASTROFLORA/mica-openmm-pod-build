"""MCP tool selection and argument building.

Phase 3 extraction from agentic_driver.py.
Functions take explicit dependencies instead of ``self``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..utils import _emit_audit_event, _truncate_text, _redact_text

logger = logging.getLogger(__name__)


# ── Tool schema lookup ────────────────────────────────────────────────

def get_tool_schema(
    mcp_tools: List[Dict[str, Any]],
    server_name: str,
    tool_base: str,
) -> Dict[str, Any]:
    """Look up the input schema for an MCP tool.

    Args:
        mcp_tools: Full list of registered MCP tool dicts.
        server_name: Server name prefix (e.g. ``"uniprot"``).
        tool_base: Tool name without server prefix (e.g. ``"get_protein_info"``).

    Returns:
        The ``input_schema`` dict, or ``{}`` when not found.
    """
    full_name = f"{server_name}_{tool_base}"
    for tool in mcp_tools:
        if tool.get("name") == full_name:
            return tool.get("input_schema") or {}
    return {}


# ── Tool picking ──────────────────────────────────────────────────────

def pick_tool_for_server(
    mcp_tools: List[Dict[str, Any]],
    tool_context: Any,
    server_name: str,
    query: Optional[str] = None,
) -> Optional[str]:
    """Pick the best MCP tool for *server_name* given a task *query*.

    Uses TEA/ToolContext ranking when available, falling back to a simple
    heuristic score.

    Args:
        mcp_tools: Full list of registered MCP tool dicts.
        tool_context: The TEA ``ToolContext`` instance (needs
            ``.select_tools_for_task``).
        server_name: MCP server name.
        query: Optional user query for relevance ranking.

    Returns:
        Tool base name (without server prefix) or *None*.
    """
    candidates = [t for t in mcp_tools if t.get("server") == server_name]
    if not candidates:
        _emit_audit_event(
            "tool_pick",
            server=server_name,
            query_preview=_truncate_text(_redact_text(query or ""), max_len=300),
            selected_tool=None,
            reason="no_candidates",
        )
        return None

    # Prefer TEA/ToolContext ranking when we have a query.
    if query:
        try:
            ranked = tool_context.select_tools_for_task(query)
            prefix = f"{server_name}_"
            for tool in ranked or []:
                name = (tool or {}).get("name") or ""
                if isinstance(name, str) and name.startswith(prefix):
                    selected = name[len(prefix):]
                    _emit_audit_event(
                        "tool_pick",
                        server=server_name,
                        query_preview=_truncate_text(
                            _redact_text(query or ""), max_len=300
                        ),
                        selected_tool=selected,
                        reason="tool_context_ranked",
                        candidates=len(candidates),
                    )
                    return selected
        except Exception:
            pass

    # Fallback heuristic scoring.
    def _score(name: str, desc: str) -> int:
        hay = f"{name} {desc}".lower()
        s = 0
        for kw, w in [
            ("structure", 5),
            ("download", 4),
            ("pdb", 4),
            ("cif", 3),
            ("mmcif", 3),
            ("get", 1),
        ]:
            if kw in hay:
                s += w
        return s

    best = max(
        candidates,
        key=lambda t: _score(t.get("name", ""), t.get("description", "")),
    )
    full = best.get("name", "")
    prefix = f"{server_name}_"
    selected = full[len(prefix):] if full.startswith(prefix) else None
    _emit_audit_event(
        "tool_pick",
        server=server_name,
        query_preview=_truncate_text(_redact_text(query or ""), max_len=300),
        selected_tool=selected,
        reason="heuristic",
        candidates=len(candidates),
    )
    return selected


# ── Tool argument building ────────────────────────────────────────────

def build_tool_args(
    *,
    bridge: Any,
    bridge_available: bool,
    schema: Dict[str, Any],
    identifiers: Dict[str, List[str]],
    query: str,
    tool_type: str = "unknown",
    gene_symbols_fn: Callable[[str, Dict[str, List[str]]], List[str]],
    protein_hint_fn: Callable[[Dict[str, List[str]]], Optional[str]],
) -> Tuple[Dict[str, Any], Any]:
    """Build tool arguments using DLM-LMP Bridge or fallback regex.

    Args:
        bridge: DLM-LMP Bridge instance (or *None*).
        bridge_available: Whether the bridge module is importable.
        schema: Tool input schema dict.
        identifiers: Extracted identifiers (``uniprot``, ``pdb``).
        query: User query text.
        tool_type: Tool type hint for the bridge.
        gene_symbols_fn: Callable to extract candidate gene symbols.
        protein_hint_fn: Callable to get best protein hint.

    Returns:
        ``(args_dict, bridge_result_or_None)``
    """
    if bridge is not None and bridge_available:
        try:
            bridge_result = bridge.process_query(
                query, tool_type=tool_type, tool_schema=schema
            )

            if bridge_result.clarification_prompt:
                logger.warning(
                    "Bridge needs clarification: %s",
                    bridge_result.clarification_prompt,
                )
                return (
                    build_tool_args_fallback(
                        schema=schema,
                        identifiers=identifiers,
                        query=query,
                        gene_symbols_fn=gene_symbols_fn,
                        protein_hint_fn=protein_hint_fn,
                    ),
                    None,
                )

            if bridge_result.is_ready_for_execution():
                logger.info(
                    "Bridge args ready (confidence=%.2f): %s",
                    bridge_result.confidence,
                    bridge_result.args,
                )
                return bridge_result.args, bridge_result

            if bridge_result.needs_pre_search:
                logger.info(
                    "Bridge detected pre-search needed: %s",
                    bridge_result.search_query,
                )
                return bridge_result.args, bridge_result

            if bridge_result.args:
                return bridge_result.args, bridge_result

        except Exception as exc:
            logger.warning("Bridge processing failed: %s, falling back to regex", exc)

    return (
        build_tool_args_fallback(
            schema=schema,
            identifiers=identifiers,
            query=query,
            gene_symbols_fn=gene_symbols_fn,
            protein_hint_fn=protein_hint_fn,
        ),
        None,
    )


def build_tool_args_fallback(
    *,
    schema: Dict[str, Any],
    identifiers: Dict[str, List[str]],
    query: str,
    gene_symbols_fn: Callable[[str, Dict[str, List[str]]], List[str]],
    protein_hint_fn: Callable[[Dict[str, List[str]]], Optional[str]],
) -> Dict[str, Any]:
    """Legacy regex-based tool argument building (fallback).

    Args:
        schema: Tool input schema dict.
        identifiers: Extracted identifiers.
        query: User query text.
        gene_symbols_fn: Callable to extract candidate gene symbols.
        protein_hint_fn: Callable to get best protein hint.

    Returns:
        Constructed argument dict.
    """
    schema = schema or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []

    def _pick_value(field: str) -> Optional[str]:
        f = (field or "").lower()
        if f in {"pdb", "pdb_id", "pdbid", "entry", "entry_id", "id"} and identifiers.get("pdb"):
            return identifiers["pdb"][0]
        if f in {"accession", "uniprot", "uniprot_id", "uniprotid"} and identifiers.get("uniprot"):
            return identifiers["uniprot"][0]
        if f in {"gene", "gene_name", "genename", "symbol"}:
            gene_candidates = gene_symbols_fn(query, identifiers)
            if gene_candidates:
                return gene_candidates[0]
        if f in {"query", "identifier", "protein", "name"}:
            return protein_hint_fn(identifiers) or query
        return None

    args: Dict[str, Any] = {}
    for field in required:
        val = _pick_value(field)
        if val is not None:
            args[field] = val

    if not args:
        for field in props.keys():
            val = _pick_value(field)
            if val is not None:
                args[field] = val
                break

    return args

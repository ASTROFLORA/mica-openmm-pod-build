"""Fallback routing helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Dict, Iterable, Tuple


BACKEND_ONLY_TYPED_TOOLS: Tuple[str, ...] = (
    "resolve_pdb",
    "analyze_structure",
    "add_to_workspace",
    "visualize_molecule",
    "list_workspace_sessions",
    "list_workspace_assets",
    "read_workspace_document",
    "get_citations_and_references",
    "resolve_entity",
    "download_pdf_to_workspace",
    "milvus_hybrid_search",
    "enrich_protein_pharma",
)


def is_backend_only_typed_tool(name: str, backend_only_tools: Iterable[str] = BACKEND_ONLY_TYPED_TOOLS) -> bool:
    return str(name or "") in set(backend_only_tools)


def run_backend_only_degraded_branch(
    *,
    name: str,
    args: Dict[str, Any],
    degraded_tool_response_fn,
) -> str:
    return degraded_tool_response_fn(
        name,
        "This tool depends on the backend/API path and no local backend is available in this runtime.",
        args_payload=args,
    )


async def run_transport_fallback_branch(
    *,
    name: str,
    args: Dict[str, Any],
    fallback_transport_execution_fn,
) -> str:
    result = await fallback_transport_execution_fn(
        name,
        args.get("prompt", args.get("query", json.dumps(args))),
    )
    return json.dumps(result, ensure_ascii=False, default=str)

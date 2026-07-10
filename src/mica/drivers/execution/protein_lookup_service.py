"""Protein lookup branch helpers extracted from AgenticDriver loop executor."""

from typing import Any, Awaitable, Callable, Dict


async def run_protein_lookup_branch(
    *,
    args: Dict[str, Any],
    shorten_query_fn: Callable[[str], str],
    uniprot_search_fn: Callable[[str, int], Awaitable[str]],
) -> str:
    query = shorten_query_fn(str(args.get("query", "")))
    if args.get("is_kinase"):
        query = f"({query}) AND (reviewed:true) AND (keyword:Kinase)"

    max_results = int(args.get("limit", args.get("max_results", 5)))
    return await uniprot_search_fn(query, max_results)

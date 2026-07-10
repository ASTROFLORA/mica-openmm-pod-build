"""UniProt execution helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Awaitable, Callable


async def run_uniprot_search(
    *,
    query: str,
    max_results: int,
    shorten_query_fn: Callable[[str], str],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    short = shorten_query_fn(query)
    try:
        import aiohttp

        fields = "accession,id,protein_name,gene_names,organism_name,function,xref_pdb"
        url = (
            f"https://rest.uniprot.org/uniprotkb/search"
            f"?query={short}&format=json&fields={fields}&size={max_results}"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as response:
                if response.status == 200:
                    data = await response.json()
                    entries = []
                    for item in (data.get("results") or [])[:max_results]:
                        entries.append(
                            {
                                "accession": item.get("primaryAccession"),
                                "gene": ((item.get("genes") or [{}])[0].get("geneName", {}).get("value", "")),
                                "name": (
                                    (item.get("proteinDescription", {}))
                                    .get("recommendedName", {})
                                    .get("fullName", {})
                                    .get("value", "")
                                ),
                                "organism": item.get("organism", {}).get("scientificName", ""),
                                "function": next(
                                    (
                                        comment["texts"][0]["value"][:300]
                                        for comment in item.get("comments", [])
                                        if comment.get("commentType") == "FUNCTION"
                                    ),
                                    "",
                                ),
                                "pdb_ids": [
                                    xref["id"]
                                    for xref in item.get("uniProtKBCrossReferences", [])
                                    if xref.get("database") == "PDB"
                                ][:5],
                            }
                        )
                    return json.dumps(
                        {"count": len(entries), "entries": entries},
                        ensure_ascii=False,
                    )
                return degraded_tool_response_fn(
                    "search_protein",
                    f"UniProt search returned HTTP {response.status}.",
                    extra={"count": 0, "entries": []},
                )
    except Exception as exc:
        return degraded_tool_response_fn(
            "search_protein",
            "UniProt search degraded because the remote service is unreachable from this environment.",
            extra={"count": 0, "entries": [], "detail": str(exc)},
        )
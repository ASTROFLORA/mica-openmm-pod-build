"""DD-CS-016AC: web_search native branch via Firecrawl v2.

Driver-side direct call. No MCP round-trip. Anti-mock: if FIRECRAWL_API_KEY
is missing we return a structured error so the LLM can reason over it instead
of inventing URLs.

Extracted from _build_loop_executor (R28 Slice-1).
"""

from __future__ import annotations

import json
from typing import Any, Dict


async def run_web_search_branch(
    *,
    name: str,
    args: Dict[str, Any],
) -> str:
    """Execute web_search via FirecrawlSearchClient and return a JSON-encoded string."""
    try:
        from mica.drivers.websearch.firecrawl_client import (
            FirecrawlClientError,
            FirecrawlNotConfigured,
            FirecrawlSearchClient,
        )
    except Exception as imp_exc:
        return json.dumps(
            {"status": "error", "tool": name, "error": f"firecrawl_import_failed: {imp_exc!r}"},
            ensure_ascii=False,
            default=str,
        )
    try:
        client = FirecrawlSearchClient()
    except FirecrawlNotConfigured as nc_exc:
        return json.dumps(
            {"status": "error", "tool": name, "error": "not_configured", "detail": str(nc_exc)},
            ensure_ascii=False,
            default=str,
        )
    try:
        payload_result = await client.search(
            query=str(args.get("query") or ""),
            limit=int(args.get("limit", 10) or 10),
            sources=list(args.get("sources") or ["web"]),
            categories=list(args.get("categories") or []),
        )
    except FirecrawlClientError as fc_exc:
        return json.dumps(
            {"status": "error", "tool": name, "error": "firecrawl_client_error", "detail": str(fc_exc)},
            ensure_ascii=False,
            default=str,
        )
    return json.dumps(
        {"status": "ok", "tool": name, "result": payload_result},
        ensure_ascii=False,
        default=str,
    )

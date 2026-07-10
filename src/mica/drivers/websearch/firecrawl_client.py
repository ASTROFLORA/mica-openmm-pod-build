"""Minimal Firecrawl v2 client for MICA web_search.

Rationale
---------
R24 annex §9 requires a first-class ``web_search`` capability alongside the
literature lanes. Firecrawl v2 exposes ``POST /v2/search`` with Bearer auth,
returns ranked web/news/research results, and can optionally scrape each hit
to Markdown. We wrap it using stdlib ``urllib`` so the MCP container stays
dependency-light (no new wheels on the critical path) and the transport
matches the rest of the Slice-0+ SDK.

Behavior
--------
* Reads ``FIRECRAWL_API_KEY`` from the environment. If absent, the wrapper
  raises :class:`FirecrawlNotConfigured` (never returns stubs) so the driver
  can surface the outage explicitly per the anti-mock doctrine.
* Exposes a thin ``web_search`` coroutine that the MCP dispatcher and
  driver loop executor can both call with the same shape.
* Keeps secrets out of logs and never persists raw response bodies to disk.

References
----------
* Firecrawl v2 search API — validated live during Slice-1 planning
  (2026-04-21). Endpoint: ``https://api.firecrawl.dev/v2/search``.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

_FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_ALLOWED_SOURCES = {"web", "news", "images"}
_ALLOWED_CATEGORIES = {"github", "research", "pdf", "news"}


class FirecrawlClientError(RuntimeError):
    """Raised when Firecrawl returns a non-2xx payload we cannot parse."""


class FirecrawlNotConfigured(RuntimeError):
    """Raised when no API key is available — keeps the caller honest."""


@dataclass(frozen=True)
class FirecrawlResult:
    title: str
    url: str
    snippet: str
    source: str
    category: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "category": self.category,
            "metadata": dict(self.metadata),
        }


def _coerce_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


def _coerce_sources(sources: Any) -> List[str]:
    if not sources:
        return ["web"]
    if isinstance(sources, str):
        candidates = [sources]
    elif isinstance(sources, Iterable):
        candidates = [str(item) for item in sources]
    else:
        candidates = ["web"]
    normalized = [s.strip().lower() for s in candidates if str(s).strip()]
    filtered = [s for s in normalized if s in _ALLOWED_SOURCES]
    return filtered or ["web"]


def _coerce_categories(categories: Any) -> List[str]:
    if not categories:
        return []
    if isinstance(categories, str):
        candidates = [categories]
    elif isinstance(categories, Iterable):
        candidates = [str(item) for item in categories]
    else:
        return []
    normalized = [c.strip().lower() for c in candidates if str(c).strip()]
    return [c for c in normalized if c in _ALLOWED_CATEGORIES]


class FirecrawlSearchClient:
    """Stdlib-only Firecrawl v2 search client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        endpoint: str = _FIRECRAWL_SEARCH_URL,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        resolved = api_key or os.environ.get("FIRECRAWL_API_KEY")
        if not resolved:
            raise FirecrawlNotConfigured(
                "FIRECRAWL_API_KEY is not set. Set it in Railway Variables or "
                "your .env (not committed) to enable web_search."
            )
        self._api_key = resolved
        self._endpoint = endpoint
        self._timeout_s = float(timeout_s)

    def search_sync(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        sources: Optional[Sequence[str]] = None,
        categories: Optional[Sequence[str]] = None,
        tbs: Optional[str] = None,
        location: Optional[str] = None,
        scrape: bool = False,
    ) -> Dict[str, Any]:
        """Blocking search. Prefer :meth:`search` in async contexts."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        payload: Dict[str, Any] = {
            "query": query.strip(),
            "limit": _coerce_limit(limit),
            "sources": _coerce_sources(sources),
        }
        cats = _coerce_categories(categories)
        if cats:
            payload["categories"] = cats
        if tbs:
            payload["tbs"] = str(tbs)
        if location:
            payload["location"] = str(location)
        if scrape:
            payload["scrapeOptions"] = {"formats": ["markdown", "links"]}

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "mica-mcp/websearch-firecrawl",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise FirecrawlClientError(
                f"firecrawl HTTP {exc.code}: {detail[:400]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise FirecrawlClientError(f"firecrawl transport error: {exc.reason!r}") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FirecrawlClientError(
                f"firecrawl non-json payload (status={status}): {raw[:200]!r}"
            ) from exc

        results = _normalize_results(decoded)
        return {
            "query": payload["query"],
            "limit": payload["limit"],
            "sources": payload["sources"],
            "categories": cats,
            "status": status,
            "results": [r.to_dict() for r in results],
            "raw_keys": sorted(decoded.keys()) if isinstance(decoded, dict) else [],
        }

    async def search(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        sources: Optional[Sequence[str]] = None,
        categories: Optional[Sequence[str]] = None,
        tbs: Optional[str] = None,
        location: Optional[str] = None,
        scrape: bool = False,
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.search_sync(
                query,
                limit=limit,
                sources=sources,
                categories=categories,
                tbs=tbs,
                location=location,
                scrape=scrape,
            ),
        )


def _normalize_results(decoded: Any) -> List[FirecrawlResult]:
    """Firecrawl returns results under data.{web,news,images} (v2 current) or data (v1).

    We flatten into a single ordered list and tag the bucket as ``source``.
    """
    results: List[FirecrawlResult] = []
    if not isinstance(decoded, Mapping):
        return results
    data = decoded.get("data")
    if data is None:
        # Some responses put results at top level.
        data = decoded
    if isinstance(data, Mapping) and any(k in data for k in ("web", "news", "images")):
        for bucket in ("web", "news", "images"):
            items = data.get(bucket) or []
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                results.append(
                    FirecrawlResult(
                        title=str(item.get("title") or item.get("name") or "")[:400],
                        url=str(item.get("url") or item.get("link") or ""),
                        snippet=str(item.get("description") or item.get("snippet") or "")[:1500],
                        source=bucket,
                        category=item.get("category"),
                        metadata={
                            k: v
                            for k, v in item.items()
                            if k not in {"title", "name", "url", "link", "description", "snippet", "category"}
                        },
                    )
                )
        return results
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, Mapping):
                continue
            results.append(
                FirecrawlResult(
                    title=str(item.get("title") or "")[:400],
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("description") or item.get("snippet") or "")[:1500],
                    source="web",
                    category=item.get("category"),
                    metadata={},
                )
            )
    return results


async def web_search(
    query: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    sources: Optional[Sequence[str]] = None,
    categories: Optional[Sequence[str]] = None,
    tbs: Optional[str] = None,
    location: Optional[str] = None,
    scrape: bool = False,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Module-level convenience coroutine — one-off call without keeping a client."""

    client = FirecrawlSearchClient(api_key=api_key)
    return await client.search(
        query,
        limit=limit,
        sources=sources,
        categories=categories,
        tbs=tbs,
        location=location,
        scrape=scrape,
    )

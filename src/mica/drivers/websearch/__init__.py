"""Web-search driver clients.

Provides the canonical ``web_search`` capability for MICA research flows
(R24 annex §9). Currently wraps Firecrawl v2 via stdlib HTTP so the
dependency surface stays minimal and works inside lightweight API
containers.
"""

from .firecrawl_client import (
    FirecrawlClientError,
    FirecrawlNotConfigured,
    FirecrawlSearchClient,
    web_search,
)

__all__ = [
    "FirecrawlClientError",
    "FirecrawlNotConfigured",
    "FirecrawlSearchClient",
    "web_search",
]

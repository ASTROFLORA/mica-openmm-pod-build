"""HGNC Gene Symbol Validator with REST API integration.

This module provides validation of human gene symbols against the official
HUGO Gene Nomenclature Committee (HGNC) database. It uses the HGNC REST API
to verify gene symbols and implements an LRU cache to minimize API calls.

Author: Alex Rodriguez (AI Systems Architecture)
Phase: 1.006 - Gap 3 Implementation
Date: October 10, 2025

References:
- HGNC REST API: https://www.genenames.org/help/rest/
- HGNC Symbol Search: https://rest.genenames.org/fetch/symbol/{symbol}
"""

import asyncio
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class HGNCValidator:
    """Validates human gene symbols against the HGNC database.
    
    This validator uses the HGNC REST API to check if a gene symbol is valid
    and approved. It implements a two-layer caching strategy:
    
    1. **LRU Cache** (in-memory): 1000 entries, expires after 24 hours
    2. **HTTP Client** connection pooling for efficient API requests
    
    Examples
    --------
    >>> validator = HGNCValidator()
    >>> await validator.is_valid("TP53")
    True
    >>> await validator.is_valid("INVALID_GENE")
    False
    >>> await validator.is_valid("tp53")  # Case-insensitive
    True
    
    Parameters
    ----------
    base_url : str, optional
        HGNC REST API base URL (default: https://rest.genenames.org)
    cache_size : int, optional
        Maximum number of cached validation results (default: 1000)
    cache_ttl_hours : int, optional
        Time-to-live for cached entries in hours (default: 24)
    timeout_seconds : float, optional
        HTTP request timeout in seconds (default: 10.0)
    """

    def __init__(
        self,
        base_url: str = "https://rest.genenames.org",
        cache_size: int = 1000,
        cache_ttl_hours: int = 24,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.timeout = timeout_seconds
        
        # In-memory cache with timestamps for TTL validation
        self._cache: Dict[str, tuple[bool, datetime]] = {}
        self._cache_size = cache_size
        
        # Persistent HTTP client with connection pooling
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry (creates HTTP client)"""
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit (closes HTTP client)"""
        await self.close()

    async def _ensure_client(self) -> None:
        """Ensure HTTP client is initialized"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )

    async def close(self) -> None:
        """Close HTTP client and release resources"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def is_valid(self, gene_symbol: str) -> bool:
        """Check if a gene symbol is valid according to HGNC.
        
        This method validates a gene symbol against the HGNC database. It first
        checks the in-memory cache, then queries the HGNC REST API if needed.
        
        Parameters
        ----------
        gene_symbol : str
            Human gene symbol to validate (e.g., "TP53", "BRCA1"). Case-insensitive.
        
        Returns
        -------
        bool
            True if the gene symbol is approved by HGNC, False otherwise.
        
        Notes
        -----
        - Gene symbols are normalized to uppercase before validation
        - Empty or whitespace-only symbols return False immediately
        - Network errors are logged and return False (fail-safe)
        - HTTP 404 responses indicate invalid symbols
        - HTTP 200 responses with valid JSON indicate approved symbols
        
        Examples
        --------
        >>> validator = HGNCValidator()
        >>> await validator.is_valid("TP53")
        True
        >>> await validator.is_valid("FAKE_GENE_999")
        False
        """
        # Normalize gene symbol (uppercase, strip whitespace)
        symbol = gene_symbol.strip().upper()
        
        if not symbol:
            return False

        # Check cache first
        cached_result = self._get_cached_result(symbol)
        if cached_result is not None:
            logger.debug("Cache hit for gene symbol: %s (valid=%s)", symbol, cached_result)
            return cached_result

        # Query HGNC REST API
        is_valid = await self._query_hgnc(symbol)
        
        # Update cache
        self._cache_result(symbol, is_valid)
        
        return is_valid

    def _get_cached_result(self, symbol: str) -> Optional[bool]:
        """Retrieve cached validation result if still valid (within TTL)"""
        if symbol not in self._cache:
            return None
        
        is_valid, cached_at = self._cache[symbol]
        age = datetime.now() - cached_at
        
        if age > self.cache_ttl:
            # Cache entry expired
            del self._cache[symbol]
            logger.debug("Cache entry expired for gene symbol: %s (age=%s)", symbol, age)
            return None
        
        return is_valid

    def _cache_result(self, symbol: str, is_valid: bool) -> None:
        """Store validation result in cache with timestamp"""
        # Implement LRU eviction if cache is full
        if len(self._cache) >= self._cache_size and symbol not in self._cache:
            # Remove oldest entry (simple FIFO for now, can optimize to true LRU)
            oldest_symbol = next(iter(self._cache))
            del self._cache[oldest_symbol]
            logger.debug("Cache evicted (full): %s", oldest_symbol)
        
        self._cache[symbol] = (is_valid, datetime.now())
        logger.debug("Cached validation result: %s (valid=%s)", symbol, is_valid)

    async def _query_hgnc(self, symbol: str) -> bool:
        """Query HGNC REST API to validate gene symbol"""
        await self._ensure_client()
        
        url = f"{self.base_url}/fetch/symbol/{quote(symbol)}"
        
        try:
            response = await self._client.get(url)
            
            if response.status_code == 404:
                # Gene symbol not found
                logger.debug("HGNC query: symbol not found (404): %s", symbol)
                return False
            
            if response.status_code != 200:
                # Unexpected status code
                logger.warning(
                    "HGNC query failed for symbol '%s': HTTP %d",
                    symbol,
                    response.status_code,
                )
                return False
            
            # Parse JSON response
            data = response.json()
            
            # Check if response contains valid gene data
            if "response" not in data or "docs" not in data["response"]:
                logger.warning("HGNC query: invalid response structure for symbol '%s'", symbol)
                return False
            
            docs = data["response"]["docs"]
            
            if not docs:
                # No matching genes found
                logger.debug("HGNC query: no matching genes for symbol '%s'", symbol)
                return False
            
            # Symbol is valid if at least one approved gene is found
            logger.debug("HGNC query: symbol validated: %s", symbol)
            return True
        
        except httpx.TimeoutException:
            logger.error("HGNC query timeout for symbol '%s' (timeout=%s)", symbol, self.timeout)
            return False
        
        except httpx.RequestError as exc:
            logger.error("HGNC query network error for symbol '%s': %s", symbol, exc)
            return False
        
        except Exception as exc:
            logger.error("HGNC query unexpected error for symbol '%s': %s", symbol, exc)
            return False

    def clear_cache(self) -> None:
        """Clear all cached validation results (useful for testing)"""
        self._cache.clear()
        logger.debug("HGNC validator cache cleared")

    def get_cache_stats(self) -> Dict[str, int]:
        """Get cache statistics for monitoring/debugging"""
        return {
            "size": len(self._cache),
            "max_size": self._cache_size,
            "utilization_pct": int((len(self._cache) / self._cache_size) * 100),
        }

"""DLM Bibliotecario — Literature context enrichment via DLM pipeline.

Queries Semantic Scholar, PubMed, OpenAlex, and bioRxiv for relevant papers,
extracts entities and relations, and provides structured literature
context to driver specialists.

This is the primary source of "what papers exist about this protein" knowledge.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import DriverBibliotecario, EnrichmentResult

logger = logging.getLogger(__name__)


class DLMBibliotecario(DriverBibliotecario):
    """Enriches queries with literature context via DLM batch pipeline."""
    
    def __init__(self, max_papers: int = 10):
        """Initialize DLM literature enrichment.
        
        Args:
            max_papers: Maximum papers to return per query
        """
        self.max_papers = max_papers
        self._service = None
        self._encoder = None
        self._initialized = False
    
    def _ensure_initialized(self) -> bool:
        """Lazy init to avoid import-time failures."""
        if self._initialized:
            return self._service is not None
        
        self._initialized = True
        try:
            from mica.services.literature_search_service import LiteratureSearchService
            self._service = LiteratureSearchService()
            logger.info("✅ DLMBibliotecario: LiteratureSearchService initialized")
        except Exception as e:
            logger.warning(f"DLMBibliotecario: literature service unavailable: {e}")
            self._service = None
        
        try:
            from mica.memory.dlm.encoder import DLMEncoder
            self._encoder = DLMEncoder()
        except Exception as e:
            logger.debug(f"DLMBibliotecario: DLMEncoder unavailable: {e}")
            self._encoder = None
        
        return self._service is not None
    
    async def enrich_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> EnrichmentResult:
        """Search literature databases for papers related to query.
        
        Args:
            query: User or driver query
            context: Optional dict with 'gene_name', 'uniprot_id', 'keywords'
        
        Returns:
            EnrichmentResult with literature context
        """
        result = EnrichmentResult(original_query=query, source="dlm")
        
        if not self._ensure_initialized():
            result.errors.append("DLM fetcher not available")
            return result
        
        try:
            # Build search query
            search_query = self._build_search_query(query, context)
            
            # Fetch from the canonical DLM-backed service
            papers = await self._search_literature(search_query)
            
            if papers:
                result.literature_context = {
                    "query_used": search_query,
                    "total_papers": len(papers),
                    "papers": papers[:self.max_papers],
                    "source": "multi_provider",
                }
                result.confidence = min(0.8, 0.3 + 0.05 * len(papers))
                
                logger.info(
                    f"✅ DLMBibliotecario: found {len(papers)} papers for: {search_query[:80]}"
                )
            else:
                result.confidence = 0.0
                logger.debug("DLMBibliotecario: no papers found")
        
        except Exception as e:
            result.errors.append(f"Literature search failed: {e}")
            logger.error(f"DLMBibliotecario error: {e}", exc_info=True)
        
        return result
    
    def _build_search_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build an optimized search query from query text + context hints."""
        parts = []
        ctx = context or {}
        
        # Use gene name / protein name if provided
        gene = ctx.get("gene_name") or ctx.get("protein_name")
        if gene:
            parts.append(gene)
        
        # Add UniProt ID
        uid = ctx.get("uniprot_id")
        if uid:
            parts.append(uid)
        
        # Add explicit keywords
        keywords = ctx.get("keywords", [])
        if isinstance(keywords, list):
            parts.extend(keywords[:3])
        
        # If no hints, use the raw query (truncated)
        if not parts:
            parts.append(query[:200])
        
        return " ".join(parts)
    
    async def _search_literature(self, query: str) -> List[Dict[str, Any]]:
        """Search the canonical literature service using control-plane routing.

        Sources are resolved via ``normalize_literature_sources()`` instead of
        being hardcoded — WI-27 drift fix.
        """
        if not self._service:
            return []

        try:
            from mica.infrastructure.literature.control_plane import (
                normalize_literature_sources,
            )
            source_plan = normalize_literature_sources(
                sources=None,  # let the control plane decide
                lane_class="general",
                openalex_available=True,
            )
            effective_sources = list(
                source_plan.get("effective_sources")
                or ["semantic_scholar", "pubmed", "openalex"]
            )
        except Exception as exc:
            logger.warning(
                "DLMBibliotecario: control-plane unavailable, using default sources — %s",
                exc,
            )
            effective_sources = ["semantic_scholar", "pubmed", "openalex"]

        try:
            result = await self._service.search(
                query=query,
                max_papers=self.max_papers,
                sources=effective_sources,
            )
            return list(result.papers)

        except Exception as e:
            logger.error(f"Literature search failed: {e}")
            return []
    
    async def enrich_result(
        self,
        result: Dict[str, Any],
        enrichment: EnrichmentResult,
    ) -> Dict[str, Any]:
        """Add literature references to specialist result."""
        if enrichment.literature_context:
            result["dlm_literature_context"] = enrichment.literature_context
            result["literature_papers_found"] = enrichment.literature_context.get(
                "total_papers", 0
            )
        return result

    async def close(self) -> None:
        if self._service is not None:
            try:
                await self._service.close()
            except Exception as exc:
                logger.debug("DLMBibliotecario close skipped after error: %s", exc)
        self._service = None
    
    def name(self) -> str:
        return "dlm_bibliotecario"


__all__ = ["DLMBibliotecario"]

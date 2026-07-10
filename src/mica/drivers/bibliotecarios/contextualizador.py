"""Contextualizador — Combined DLM + LMP enrichment orchestrator.

Orchestrates both LMP (biological context) and DLM (literature context)
bibliotecarios into a single enrichment pipeline that can be injected
into any driver's specialist routing.

Usage:
    ctx = Contextualizador()
    enrichment = await ctx.enrich_query("Analyze WNK1 kinase domain")
    
    # Use in specialist prompt
    system_fragment = enrichment.system_prompt_fragment
    bio_ctx = enrichment.biological_context
    lit_ctx = enrichment.literature_context
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .base import DriverBibliotecario, EnrichmentResult
from .dlm_bibliotecario import DLMBibliotecario
from .lmp_bibliotecario import LMPBibliotecario

logger = logging.getLogger(__name__)


class Contextualizador(DriverBibliotecario):
    """Orchestrates LMP + DLM bibliotecarios for unified enrichment.
    
    Strategy:
    1. Run LMP first (fast, disk-based XML lookup)
    2. If LMP succeeds, feed its keywords into DLM for better literature search
    3. Merge both results into a single EnrichmentResult
    """
    
    def __init__(
        self,
        enable_lmp: bool = True,
        enable_dlm: bool = True,
        preset_base: Optional[str] = None,
        max_papers: int = 10,
    ):
        """Initialize both sub-bibliotecarios.
        
        Args:
            enable_lmp: Enable biological context from LMP presets
            enable_dlm: Enable literature context from DLM
            preset_base: Override path to LMP presets directory
            max_papers: Max literature papers to fetch
        """
        self.enable_lmp = enable_lmp
        self.enable_dlm = enable_dlm
        
        self._lmp = LMPBibliotecario(preset_base=preset_base) if enable_lmp else None
        self._dlm = DLMBibliotecario(max_papers=max_papers) if enable_dlm else None
    
    async def enrich_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> EnrichmentResult:
        """Run LMP + DLM enrichment pipeline.
        
        Strategy:
        1. LMP enrichment (biological context from XML presets)
        2. Use LMP results to improve DLM search query
        3. DLM enrichment (literature search)
        4. Merge both results
        
        Args:
            query: User or driver query
            context: Optional hints (uniprot_id, gene_name, keywords)
        
        Returns:
            Combined EnrichmentResult
        """
        base_result = EnrichmentResult(original_query=query, source="combined")
        
        # Step 1: LMP enrichment (fast, sync-ish)
        lmp_result = None
        if self._lmp:
            try:
                lmp_result = await self._lmp.enrich_query(query, context)
            except Exception as e:
                logger.warning(f"Contextualizador: LMP failed: {e}")
                lmp_result = EnrichmentResult(
                    original_query=query,
                    source="lmp",
                    errors=[str(e)],
                )
        
        # Step 2: Enrich DLM context with LMP keywords
        dlm_context = dict(context or {})
        if lmp_result and lmp_result.biological_context:
            bio = lmp_result.biological_context
            if isinstance(bio, dict):
                # Feed LMP data into DLM search
                if bio.get("gene_names"):
                    dlm_context.setdefault("gene_name", bio["gene_names"][0])
                if bio.get("keywords"):
                    dlm_context.setdefault("keywords", bio["keywords"][:5])
                if bio.get("protein_name"):
                    dlm_context.setdefault("protein_name", bio["protein_name"])
        
        # Step 3: DLM enrichment
        dlm_result = None
        if self._dlm:
            try:
                dlm_result = await self._dlm.enrich_query(query, dlm_context)
            except Exception as e:
                logger.warning(f"Contextualizador: DLM failed: {e}")
                dlm_result = EnrichmentResult(
                    original_query=query,
                    source="dlm",
                    errors=[str(e)],
                )
        
        # Step 4: Merge
        if lmp_result and lmp_result.has_context():
            base_result = base_result.merge(lmp_result)
        if dlm_result and dlm_result.has_context():
            base_result = base_result.merge(dlm_result)
        
        # Collect all errors
        if lmp_result:
            base_result.errors.extend(lmp_result.errors)
        if dlm_result:
            base_result.errors.extend(dlm_result.errors)
        
        logger.info(
            f"📚 Contextualizador: LMP={'✅' if lmp_result and lmp_result.has_context() else '❌'} "
            f"DLM={'✅' if dlm_result and dlm_result.has_context() else '❌'} "
            f"confidence={base_result.confidence:.2f}"
        )
        
        return base_result
    
    async def enrich_result(
        self,
        result: Dict[str, Any],
        enrichment: EnrichmentResult,
    ) -> Dict[str, Any]:
        """Post-process specialist result with both contexts."""
        if self._lmp and enrichment.biological_context:
            result = await self._lmp.enrich_result(result, enrichment)
        if self._dlm and enrichment.literature_context:
            result = await self._dlm.enrich_result(result, enrichment)
        return result

    async def close(self) -> None:
        if self._dlm is not None:
            try:
                await self._dlm.close()
            except Exception as exc:
                logger.debug("Contextualizador: DLM close skipped after error: %s", exc)
    
    def name(self) -> str:
        return "contextualizador"


__all__ = ["Contextualizador"]

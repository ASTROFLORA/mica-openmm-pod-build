"""LMP Bibliotecario — Biological context enrichment from LMP v4 presets.

Loads biological context (FUNCTION, PTM, SUBUNIT, DOMAIN comments, keywords,
NeSyGrammar markers, KG edges) from LMP XML presets and injects it into
driver queries and specialist prompts.

This is the primary source of "what does this protein DO" knowledge.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import DriverBibliotecario, EnrichmentResult

logger = logging.getLogger(__name__)


class LMPBibliotecario(DriverBibliotecario):
    """Enriches queries with biological context from LMP v4 XML presets."""
    
    def __init__(self, preset_base: Optional[str] = None):
        """Initialize LMP context resolver + extractor.
        
        Args:
            preset_base: Override path to output_all_presets/ directory
        """
        self._bridge = None
        self._preset_base = preset_base
        self._initialized = False
    
    def _ensure_initialized(self) -> bool:
        """Lazy init to avoid circular imports."""
        if self._initialized:
            return self._bridge is not None
        
        self._initialized = True
        try:
            from mica.drivers.dlm_lmp_bridge import get_bridge
            self._bridge = get_bridge()
            if not self._bridge.enable_lmp_context:
                logger.warning("LMPBibliotecario: bridge has LMP context disabled")
                return False
            return True
        except Exception as e:
            logger.warning(f"LMPBibliotecario: failed to init bridge: {e}")
            return False
    
    async def enrich_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> EnrichmentResult:
        """Load biological context for proteins mentioned in query.
        
        Args:
            query: User or driver query
            context: Optional dict with 'uniprot_id', 'gene_name', etc.
        
        Returns:
            EnrichmentResult with LMP biological context
        """
        result = EnrichmentResult(original_query=query, source="lmp")
        
        if not self._ensure_initialized():
            result.errors.append("LMP not available")
            return result
        
        try:
            # If caller provides explicit IDs, use them
            explicit_uid = (context or {}).get("uniprot_id")
            explicit_gene = (context or {}).get("gene_name")
            
            bio_ctx = None
            
            if explicit_uid:
                bio_ctx = self._bridge.get_biological_context(explicit_uid)
            
            if not bio_ctx and explicit_gene:
                resolver = self._bridge.preset_resolver
                if resolver:
                    path = resolver.resolve_by_gene_name(explicit_gene)
                    if path:
                        bio_ctx = self._bridge._load_context_from_path(path)
            
            if not bio_ctx:
                # Fall back to bridge entity extraction
                extracted = self._bridge._extract_entities(query)
                bio_ctx, prompt, tools = self._bridge._inject_biological_context(
                    extracted, query
                )
                if bio_ctx:
                    result.system_prompt_fragment = prompt
                    result.suggested_tools = tools
            
            if bio_ctx:
                result.biological_context = bio_ctx.to_compact_dict()
                if not result.system_prompt_fragment:
                    result.system_prompt_fragment = bio_ctx.to_system_prompt()
                if not result.suggested_tools:
                    result.suggested_tools = bio_ctx.suggest_tools()
                result.confidence = 0.9
                
                logger.info(
                    f"✅ LMPBibliotecario: loaded context for "
                    f"{bio_ctx.uniprot_id} ({bio_ctx.protein_name})"
                )
            else:
                result.confidence = 0.0
                logger.debug("LMPBibliotecario: no preset matched")
        
        except Exception as e:
            result.errors.append(f"LMP enrichment failed: {e}")
            logger.error(f"LMPBibliotecario error: {e}", exc_info=True)
        
        return result
    
    async def enrich_result(
        self,
        result: Dict[str, Any],
        enrichment: EnrichmentResult,
    ) -> Dict[str, Any]:
        """Add biological context summary to specialist result."""
        if enrichment.biological_context:
            result["lmp_biological_context"] = enrichment.biological_context
            result["lmp_context_loaded"] = True
        return result
    
    def name(self) -> str:
        return "lmp_bibliotecario"


__all__ = ["LMPBibliotecario"]

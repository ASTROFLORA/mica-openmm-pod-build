"""Base Bibliotecario — Abstract research sub-agent for driver enrichment.

A Bibliotecario ("librarian") is a lightweight sub-agent that enriches queries
and results with domain-specific context before/after specialist execution.

Unlike full agents, bibliotecarios are stateless helpers designed for
single-shot enrichment — no conversation memory, no planning.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    """Result of a bibliotecario enrichment step."""
    
    # Original input
    original_query: str
    
    # Enriched output
    enriched_query: Optional[str] = None  # Query rewritten with context
    system_prompt_fragment: Optional[str] = None  # Inject into system prompt
    
    # Structured context
    biological_context: Optional[Dict[str, Any]] = None  # From LMP
    literature_context: Optional[Dict[str, Any]] = None  # From DLM/S2/PubMed
    
    # Metadata
    suggested_tools: List[str] = field(default_factory=list)
    confidence: float = 0.0
    source: str = ""  # "lmp", "dlm", "combined"
    
    # Diagnostics
    errors: List[str] = field(default_factory=list)
    
    def has_context(self) -> bool:
        """Check if any enrichment was found."""
        return bool(self.biological_context or self.literature_context)
    
    def merge(self, other: "EnrichmentResult") -> "EnrichmentResult":
        """Merge another enrichment result into this one."""
        merged = EnrichmentResult(
            original_query=self.original_query,
            enriched_query=other.enriched_query or self.enriched_query,
            system_prompt_fragment=_merge_prompts(
                self.system_prompt_fragment, other.system_prompt_fragment
            ),
            biological_context=other.biological_context or self.biological_context,
            literature_context=other.literature_context or self.literature_context,
            suggested_tools=list(
                dict.fromkeys(self.suggested_tools + other.suggested_tools)
            ),
            confidence=max(self.confidence, other.confidence),
            source="combined",
            errors=self.errors + other.errors,
        )
        return merged


def _merge_prompts(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Merge two prompt fragments."""
    if a and b:
        return f"{a}\n\n{b}"
    return a or b


class DriverBibliotecario(ABC):
    """Abstract base for driver research sub-agents.
    
    Subclasses must implement:
    - enrich_query(): Add context before specialist execution
    - enrich_result(): Post-process specialist output (optional)
    """
    
    @abstractmethod
    async def enrich_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> EnrichmentResult:
        """Enrich a query with domain-specific context.
        
        Args:
            query: Original user/driver query
            context: Optional additional context (IDs, previous results, etc.)
        
        Returns:
            EnrichmentResult with context and optional rewritten query
        """
        ...
    
    async def enrich_result(
        self,
        result: Dict[str, Any],
        enrichment: EnrichmentResult,
    ) -> Dict[str, Any]:
        """Post-process a specialist response with context.
        
        Default implementation: no-op, override in subclasses.
        
        Args:
            result: Specialist response dict
            enrichment: The enrichment from enrich_query()
        
        Returns:
            Enriched result dict
        """
        return result

    async def close(self) -> None:
        """Release any optional runtime resources held by the bibliotecario."""
        return None
    
    @abstractmethod
    def name(self) -> str:
        """Bibliotecario identifier."""
        ...


__all__ = ["DriverBibliotecario", "EnrichmentResult"]

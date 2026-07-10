"""Bibliotecarios — Research sub-agents for MICA drivers.

Each driver can have one or more Bibliotecarios that enrich queries and results
with literature context (DLM), biological context (LMP), or both (Contextualizador).

Architecture:
    WorkerDriver
      ├── route_to_specialist()  ← pre-enrichment hook
      │     └── Contextualizador.enrich_query()
      │           ├── DLMBibliotecario.enrich_query()  → literature context
      │           └── LMPBibliotecario.enrich_query()  → biological context
      └── _execute_specialist_base()
            └── Contextualizador.enrich_result()  ← post-enrichment

Usage:
    from mica.drivers.bibliotecarios import Contextualizador
    
    ctx = Contextualizador()
    enriched_query = await ctx.enrich_query("Analyze WNK1 kinase domain")
    # enriched_query.biological_context → LMP preset data
    # enriched_query.literature_context → recent papers from S2/PubMed
"""

from .base import DriverBibliotecario, EnrichmentResult
from .dlm_bibliotecario import DLMBibliotecario
from .lmp_bibliotecario import LMPBibliotecario
from .contextualizador import Contextualizador

__all__ = [
    "DriverBibliotecario",
    "EnrichmentResult",
    "DLMBibliotecario",
    "LMPBibliotecario",
    "Contextualizador",
]

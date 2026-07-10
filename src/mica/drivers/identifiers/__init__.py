"""Identifier extraction and resolution utilities.

Phase 3 extraction from agentic_driver.py.
Pure functions for extracting and parsing UniProt accessions, PDB IDs,
and gene symbols from text and MCP results.
"""

from .extraction import (
    PDB_FALSE_POSITIVES,
    extract_identifiers,
    merge_identifiers,
    best_protein_hint,
    extract_candidate_gene_symbols,
)
from .resolution import (
    extract_text_chunks_from_mcp,
    extract_uniprot_accessions_from_mcp_result,
    extract_pdb_ids_from_search_result,
)

__all__ = [
    "PDB_FALSE_POSITIVES",
    "extract_identifiers",
    "merge_identifiers",
    "best_protein_hint",
    "extract_candidate_gene_symbols",
    "extract_text_chunks_from_mcp",
    "extract_uniprot_accessions_from_mcp_result",
    "extract_pdb_ids_from_search_result",
]

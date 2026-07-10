"""
MICA Evidence & Citation Extraction (Phase 2)
==============================================

Standalone functions for:
  - Scientific citation / identifier resolution
  - Source record construction from Semantic Scholar papers
  - Claim derivation with provenance tracking
  - Lab report construction (BSM schema)
  - Final result contract normalization

All functions are pure (no driver-instance state),
making them independently testable.
"""

from .citations import (
    official_link_from_identifiers,
    build_source_record_from_paper,
    format_bibliotecario_citation_entry,
    extract_sources_from_text,
    derive_claims_and_sources,
    extract_native_evidence_from_side_data,
)
from .contract import (
    normalize_final_result_contract,
    build_minimal_lab_report,
)

__all__ = [
    "official_link_from_identifiers",
    "build_source_record_from_paper",
    "format_bibliotecario_citation_entry",
    "extract_sources_from_text",
    "derive_claims_and_sources",
    "extract_native_evidence_from_side_data",
    "normalize_final_result_contract",
    "build_minimal_lab_report",
]

from __future__ import annotations

import asyncio
from datetime import datetime
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, model_validator

from bsm.lmp.scanner import LMPScanner
from mica.literature_consolidation.contracts.provider_quorum import (
    ProviderQuorumPolicy,
    ProviderQuorumRuntimeResult,
)
from mica.literature_consolidation.contracts.query_protocol import LiteratureQuerySpec
from mica.literature_consolidation.lmp_bibliotecario_handoff import (
    compile_lmp_bibliotecario_handoff,
)
from mica.literature_consolidation.services.provider_quorum_service import (
    ProviderQuorumService,
)
from mica.memory.atom.decomposer import ATOMAtomicDecomposer
from mica.memory.atom.jsonld import experience_trace_to_jsonld
from mica.memory.atom.models import AtomicExperienceTrace
from mica.memory.atom.quintuple_extractor import ATOMQuintupleExtractor
from mica.memory.dlm.encoder import DLMEncoder
from mica.memory.dlm.entity_mapper import EntityMapper
from mica.memory.dlm_lmp.metadata_service import LMPMetadataService, ProteinMetadata
from mica.storage.workspace_artifact_contract import ClaimBoundary, derived_sha256

from .model_contracts import utcnow_iso


GenesisTargetType = Literal["enzyme", "protein_family", "scaffold", "function", "motif", "custom"]
GenesisEvidenceGrade = Literal[
    "live_quorum",
    "live_degraded",
    "fixture_only",
    "blocked_missing_live_evidence",
]
GenesisEvidenceIntent = Literal[
    "dataset_support",
    "mechanism",
    "function",
    "structure",
    "review",
    "negative_control",
]
GenesisQueryClass = Literal[
    "same_target_strict",
    "function_with_target",
    "homolog_mechanism",
    "dataset_support",
    "negative_control",
    "exact_identifier_query",
    "function_query",
    "mechanism_query",
    "dataset_support_query",
    "negative_control_query",
]
GenesisEvidenceTier = Literal[
    "same_target_direct",
    "same_target_functional",
    "homolog_direct",
    "homolog_mechanistic",
    "family_background",
    "broad_context_only",
    "irrelevant",
    "ambiguous",
]
GenesisRelevanceLabel = Literal[
    "strong_target_match",
    "likely_target_match",
    "broad_context_only",
    "irrelevant",
    "ambiguous",
]
GenesisPrecisionStatus = Literal[
    "precision_same_target_passed",
    "precision_homolog_supported",
    "precision_partial_background_only",
    "precision_failed_no_target_evidence",
    "precision_failed_provider_quorum",
    "precision_failed_materialization",
    "live_quorum_satisfied_precision_passed",
    "live_quorum_satisfied_precision_partial",
    "live_quorum_satisfied_precision_failed",
    "provider_quorum_failed",
    "no_target_relevant_evidence_found",
    "citation_materialization_failed",
    "fixture_only",
    "blocked",
]

_DEFAULT_OUTPUT_POLICY = {
    "artifact_contract_ref": "mica.storage.workspace_artifact_contract.WorkspaceArtifactContract",
    "default_claim_boundary": "local_non_production",
}


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive
            error["value"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "target"


def _dedupe_strings(values: Iterable[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(value)
    return ordered


def _safe_optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _term_matches(text: str, terms: Sequence[str]) -> List[str]:
    haystack = re.sub(r"\s+", " ", str(text or "")).casefold()
    matches: List[str] = []
    for term in list(terms or []):
        normalized = re.sub(r"\s+", " ", str(term or "").strip())
        if not normalized:
            continue
        if normalized.casefold() in haystack:
            matches.append(normalized)
    return _dedupe_strings(matches)


def _exact_term_matches(text: str, terms: Sequence[str]) -> List[str]:
    haystack = re.sub(r"\s+", " ", str(text or ""))
    matches: List[str] = []
    for term in list(terms or []):
        normalized = re.sub(r"\s+", " ", str(term or "").strip())
        if not normalized:
            continue
        pattern_text = re.escape(normalized).replace(r"\ ", r"\s+")
        pattern = re.compile(rf"(?<![A-Za-z0-9]){pattern_text}(?![A-Za-z0-9])", re.IGNORECASE)
        if pattern.search(haystack):
            matches.append(normalized)
    return _dedupe_strings(matches)


def _casefolded_exact_matches(values: Sequence[str], terms: Sequence[str]) -> List[str]:
    normalized_terms = {
        str(term or "").strip().casefold(): str(term or "").strip()
        for term in list(terms or [])
        if str(term or "").strip()
    }
    matches: List[str] = []
    for raw in list(values or []):
        text = str(raw or "").strip()
        if not text:
            continue
        matched = normalized_terms.get(text.casefold())
        if matched:
            matches.append(text)
    return _dedupe_strings(matches)


def _text_haystack(*parts: Any) -> str:
    rendered: List[str] = []
    for part in parts:
        if isinstance(part, dict):
            rendered.append(" ".join(str(value or "") for value in part.values()))
        elif isinstance(part, (list, tuple, set)):
            rendered.append(" ".join(str(value or "") for value in part))
        else:
            rendered.append(str(part or ""))
    return "\n".join(rendered)


def _extract_species_names(text: str) -> List[str]:
    matches = re.findall(r"\b[A-Z][a-z]+ [a-z]{2,}\b", str(text or ""))
    return _dedupe_strings(matches)


def _extract_author_names(value: Any) -> List[str]:
    authors: List[str] = []
    for author in list(value or []):
        if isinstance(author, dict):
            name = str(author.get("name") or "").strip()
        else:
            name = str(author or "").strip()
        if name:
            authors.append(name)
    return _dedupe_strings(authors)


def _extract_publication_year(paper: Mapping[str, Any]) -> Optional[int]:
    for candidate in [
        paper.get("year"),
        (paper.get("metadata") or {}).get("year"),
        (paper.get("metadata") or {}).get("publication_year"),
    ]:
        try:
            if candidate is None or candidate == "":
                continue
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _extract_journal_source(paper: Mapping[str, Any]) -> str:
    metadata = dict(paper.get("metadata") or {})
    for candidate in [
        paper.get("journal"),
        paper.get("venue"),
        metadata.get("journal"),
        metadata.get("venue"),
        metadata.get("source_display_name"),
        paper.get("source"),
        paper.get("provider"),
    ]:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _extract_provider_record_url(paper: Mapping[str, Any]) -> str:
    metadata = dict(paper.get("metadata") or {})
    for candidate in [
        paper.get("source_url"),
        paper.get("official_url"),
        metadata.get("source_url"),
        metadata.get("official_url"),
        metadata.get("best_oa_url"),
        paper.get("pdf_url"),
    ]:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _normalized_identifier(value: Any) -> str:
    return str(value or "").strip()


def _extract_stable_identifier(candidate: Mapping[str, Any]) -> str:
    for key in ("pmid", "doi", "pmcid", "provider_record_url", "canonical_id"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    return ""


def _extract_ec_number(target_name: str) -> str:
    match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", str(target_name or ""))
    return str(match.group(1)) if match else ""


def _truncate_snippet(text: Any, *, max_chars: int = 900) -> str:
    rendered = str(text or "").strip()
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 3].rstrip() + "..."


def _organism_aliases(organism: str | None, taxon_id: str | None = None) -> List[str]:
    aliases: List[str] = []
    normalized = str(organism or "").strip()
    if normalized:
        aliases.append(normalized)
        lowered = normalized.casefold()
        if lowered == "homo sapiens":
            aliases.extend(["human"])
        elif lowered == "mus musculus":
            aliases.extend(["mouse", "murine"])
        elif lowered == "rattus norvegicus":
            aliases.extend(["rat"])
        elif lowered == "escherichia coli":
            aliases.extend(["e. coli"])
    if taxon_id:
        aliases.append(str(taxon_id))
    return _dedupe_strings(aliases)


def _preferred_organism_query_terms(organism: str | None, taxon_id: str | None = None) -> List[str]:
    aliases = _organism_aliases(organism, taxon_id)
    common_names = [
        alias
        for alias in aliases
        if alias.casefold() in {"human", "mouse", "murine", "rat", "e. coli"}
    ]
    return _dedupe_strings([*common_names, *aliases])


def _contains_review_signal(text: str) -> bool:
    haystack = str(text or "").casefold()
    return any(
        marker in haystack
        for marker in [
            "review",
            "overview",
            "survey",
            "background",
            "perspective",
            "roadmap",
            "meta-analysis",
        ]
    )


def _normalize_organism_for_semantic_query(organism_filter: str | None) -> str:
    organism = str(organism_filter or "").strip()
    if not organism:
        return ""
    if organism.isdigit():
        return f" organism {organism}"
    return f" {organism}"


def _normalize_entity_mapper_type(raw_type: str) -> str | None:
    normalized = str(raw_type or "").strip().lower()
    if normalized in {"protein", "proteins", "disease_proteins", "drug_targets"}:
        return "protein"
    if normalized in {"gene", "genes"}:
        return "gene"
    if normalized in {"disease", "diseases"}:
        return "disease"
    if normalized in {"drug", "drugs", "chemical", "chemicals"}:
        return "drug"
    return None


def _candidate_document_text(candidate: "GenesisEvidenceCandidate") -> str:
    return _truncate_snippet(
        " ".join(
            part
            for part in [
                str(candidate.title or "").strip(),
                str(candidate.abstract_snippet or "").strip(),
            ]
            if part
        ),
        max_chars=4000,
    )


def _default_atom_embedding(_text: str) -> List[float]:
    return [1.0]


def _normalize_provider_policy(policy: Mapping[str, Any] | None) -> ProviderQuorumPolicy:
    payload = dict(policy or {})
    return ProviderQuorumPolicy(
        min_attempted_providers=int(payload.get("min_attempted_providers") or 1),
        min_successful_providers=int(payload.get("min_successful_providers") or 1),
        allow_degraded_success=bool(payload.get("allow_degraded_success", True)),
        require_nonempty_papers=bool(payload.get("require_nonempty_papers", True)),
    )


class GenesisDatasetRequest(BaseModel):
    request_id: str = Field(..., min_length=1)
    target_type: GenesisTargetType
    target_name: str = Field(..., min_length=1)
    organism_filter: Optional[str] = None
    sequence_filters: Dict[str, Any] = Field(default_factory=dict)
    structure_filters: Dict[str, Any] = Field(default_factory=dict)
    function_filters: Dict[str, Any] = Field(default_factory=dict)
    literature_required: bool = True
    max_records: int = Field(25, ge=1, le=500)
    output_policy: Dict[str, Any] = Field(default_factory=lambda: dict(_DEFAULT_OUTPUT_POLICY))
    claim_boundary: ClaimBoundary = "local_non_production"

    @model_validator(mode="after")
    def _normalize_output_policy(self) -> "GenesisDatasetRequest":
        merged = dict(_DEFAULT_OUTPUT_POLICY)
        merged.update(dict(self.output_policy or {}))
        self.output_policy = merged
        return self


class GenesisDatasetRecord(BaseModel):
    target_id: str = Field(..., min_length=1)
    gene_name: Optional[str] = None
    protein_family: Optional[str] = None
    organism: Optional[str] = None
    sequence_length: Optional[int] = Field(None, ge=0)
    source: str = Field(..., min_length=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GenesisDatasetBlocker(BaseModel):
    code: str = Field(..., min_length=1)
    detail: str = Field(..., min_length=1)


class GenesisDatasetManifest(BaseModel):
    dataset_id: str = Field(..., min_length=1)
    request: GenesisDatasetRequest
    records: List[GenesisDatasetRecord] = Field(default_factory=list)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    filters_applied: Dict[str, Any] = Field(default_factory=dict)
    gcs_uri: Optional[str] = None
    local_path: Optional[str] = None
    sha256: Optional[str] = None
    blocker: Optional[GenesisDatasetBlocker] = None
    claim_boundary: ClaimBoundary
    status: Literal["completed", "partial", "blocked"] = "partial"
    record_count: int = Field(0, ge=0)
    artifact_policy: Dict[str, Any] = Field(default_factory=lambda: dict(_DEFAULT_OUTPUT_POLICY))
    backlog_items: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)

    @model_validator(mode="after")
    def _finalize(self) -> "GenesisDatasetManifest":
        self.record_count = len(self.records)
        if not self.sha256:
            target_ids = [record.target_id for record in self.records]
            self.sha256 = derived_sha256(self.dataset_id, self.request.request_id, *target_ids)
        return self


class GenesisDatasetBuilderReceipt(BaseModel):
    receipt_type: Literal["genesis_dataset_builder_receipt_v1"] = "genesis_dataset_builder_receipt_v1"
    request_id: str = Field(..., min_length=1)
    dataset_id: str = Field(..., min_length=1)
    status: Literal["completed", "partial", "blocked"]
    query_plan: Dict[str, Any] = Field(default_factory=dict)
    lmp_surfaces: Dict[str, Any] = Field(default_factory=dict)
    record_count: int = Field(0, ge=0)
    blocker: Optional[GenesisDatasetBlocker] = None
    backlog_items: List[str] = Field(default_factory=list)
    claim_boundary: ClaimBoundary
    created_at: str = Field(default_factory=utcnow_iso)


class GenesisEvidenceRequest(BaseModel):
    target_name: str = Field(..., min_length=1)
    target_type: GenesisTargetType
    gene_symbol: Optional[str] = None
    protein_name: Optional[str] = None
    organism: Optional[str] = None
    taxon_id: Optional[str] = None
    uniprot_accessions: List[str] = Field(default_factory=list)
    ec_numbers: List[str] = Field(default_factory=list)
    synonyms: List[str] = Field(default_factory=list)
    function_terms: List[str] = Field(default_factory=list)
    substrate_terms: List[str] = Field(default_factory=list)
    mechanism_terms: List[str] = Field(default_factory=list)
    structure_terms: List[str] = Field(default_factory=list)
    evidence_intent: GenesisEvidenceIntent = "dataset_support"
    must_include: List[str] = Field(default_factory=list)
    must_not_include: List[str] = Field(default_factory=list)
    source_dataset_context: Dict[str, Any] = Field(default_factory=dict)
    evidence_questions: List[str] = Field(default_factory=list)
    required_sources: List[str] = Field(default_factory=lambda: ["semantic_scholar", "pubmed", "openalex"])
    provider_policy: Dict[str, Any] = Field(default_factory=dict)
    output_policy: Dict[str, Any] = Field(default_factory=lambda: dict(_DEFAULT_OUTPUT_POLICY))

    @model_validator(mode="after")
    def _normalize_output_policy(self) -> "GenesisEvidenceRequest":
        merged = dict(_DEFAULT_OUTPUT_POLICY)
        merged.update(dict(self.output_policy or {}))
        self.output_policy = merged
        self.required_sources = _dedupe_strings(self.required_sources)
        self.uniprot_accessions = _dedupe_strings(self.uniprot_accessions)
        self.ec_numbers = _dedupe_strings(self.ec_numbers or [_extract_ec_number(self.target_name)])
        self.synonyms = _dedupe_strings(self.synonyms)
        self.function_terms = _dedupe_strings(self.function_terms)
        self.substrate_terms = _dedupe_strings(self.substrate_terms)
        self.mechanism_terms = _dedupe_strings(self.mechanism_terms)
        self.structure_terms = _dedupe_strings(self.structure_terms)
        self.must_include = _dedupe_strings(self.must_include)
        self.must_not_include = _dedupe_strings(self.must_not_include)
        return self


class GenesisTargetProfile(BaseModel):
    target_profile_id: str = Field(..., min_length=1)
    target_name: str = Field(..., min_length=1)
    gene_symbol: Optional[str] = None
    protein_name: Optional[str] = None
    organism: Optional[str] = None
    taxon_id: Optional[str] = None
    uniprot_accessions: List[str] = Field(default_factory=list)
    ec_numbers: List[str] = Field(default_factory=list)
    synonyms: List[str] = Field(default_factory=list)
    function_terms: List[str] = Field(default_factory=list)
    substrate_terms: List[str] = Field(default_factory=list)
    mechanism_terms: List[str] = Field(default_factory=list)
    structure_terms: List[str] = Field(default_factory=list)
    evidence_intent: GenesisEvidenceIntent = "dataset_support"
    must_include: List[str] = Field(default_factory=list)
    must_not_include: List[str] = Field(default_factory=list)
    source_dataset_context: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize(self) -> "GenesisTargetProfile":
        self.uniprot_accessions = _dedupe_strings(self.uniprot_accessions)
        self.ec_numbers = _dedupe_strings(self.ec_numbers)
        self.synonyms = _dedupe_strings(self.synonyms)
        self.function_terms = _dedupe_strings(self.function_terms)
        self.substrate_terms = _dedupe_strings(self.substrate_terms)
        self.mechanism_terms = _dedupe_strings(self.mechanism_terms)
        self.structure_terms = _dedupe_strings(self.structure_terms)
        self.must_include = _dedupe_strings(self.must_include)
        self.must_not_include = _dedupe_strings(self.must_not_include)
        return self


class GenesisTargetIdentityBundle(BaseModel):
    canonical_target_name: str = Field(..., min_length=1)
    canonical_gene_symbol: Optional[str] = None
    canonical_protein_name: Optional[str] = None
    organism: Optional[str] = None
    taxon_id: Optional[str] = None
    uniprot_accessions: List[str] = Field(default_factory=list)
    ec_numbers: List[str] = Field(default_factory=list)
    exact_aliases: List[str] = Field(default_factory=list)
    loose_aliases: List[str] = Field(default_factory=list)
    organism_aliases: List[str] = Field(default_factory=list)
    family_terms: List[str] = Field(default_factory=list)
    substrate_terms: List[str] = Field(default_factory=list)
    mechanism_terms: List[str] = Field(default_factory=list)
    forbidden_ambiguity_terms: List[str] = Field(default_factory=list)
    metadata_resolution_status: Literal["resolved", "metadata_resolution_partial"] = "resolved"
    evidence_basis: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self) -> "GenesisTargetIdentityBundle":
        self.uniprot_accessions = _dedupe_strings(self.uniprot_accessions)
        self.ec_numbers = _dedupe_strings(self.ec_numbers)
        self.exact_aliases = _dedupe_strings(self.exact_aliases)
        self.loose_aliases = _dedupe_strings(self.loose_aliases)
        self.organism_aliases = _dedupe_strings(self.organism_aliases)
        self.family_terms = _dedupe_strings(self.family_terms)
        self.substrate_terms = _dedupe_strings(self.substrate_terms)
        self.mechanism_terms = _dedupe_strings(self.mechanism_terms)
        self.forbidden_ambiguity_terms = _dedupe_strings(self.forbidden_ambiguity_terms)
        self.evidence_basis = _dedupe_strings(self.evidence_basis)
        return self


class GenesisEvidenceQuery(BaseModel):
    query_id: str = Field(..., min_length=1)
    query_text: str = Field(..., min_length=1)
    query_class: GenesisQueryClass
    fielded_query_text: Optional[str] = None
    target_fields_used: List[str] = Field(default_factory=list)
    expected_relevance: str = Field(..., min_length=1)
    expected_evidence_tier: GenesisEvidenceTier = "ambiguous"
    provider_constraints: Dict[str, Any] = Field(default_factory=dict)
    max_results: int = Field(5, ge=1, le=25)
    strictness_level: Literal["high", "medium", "broad"] = "medium"
    reason: str = ""
    generated_at: str = Field(default_factory=utcnow_iso)


class GenesisEvidenceCandidate(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    canonical_id: Optional[str] = None
    title: str = Field(..., min_length=1)
    authors: List[str] = Field(default_factory=list)
    journal_source: str = ""
    publication_year: Optional[int] = None
    pmid: str = ""
    doi: str = ""
    pmcid: str = ""
    provider: str = ""
    provider_record_url: str = ""
    abstract_snippet: str = ""
    retrieval_timestamp: str = Field(default_factory=utcnow_iso)
    query_ids: List[str] = Field(default_factory=list)
    query_classes: List[GenesisQueryClass] = Field(default_factory=list)
    provider_support: List[str] = Field(default_factory=list)
    target_profile_id: str = Field(..., min_length=1)
    matched_target_terms: List[str] = Field(default_factory=list)
    matched_organism_terms: List[str] = Field(default_factory=list)
    matched_function_terms: List[str] = Field(default_factory=list)
    matched_mechanism_terms: List[str] = Field(default_factory=list)
    identity_match_basis: List[str] = Field(default_factory=list)
    target_fields_matched: List[str] = Field(default_factory=list)
    score_breakdown: Dict[str, float] = Field(default_factory=dict)
    final_relevance_score: float = 0.0
    evidence_tier: GenesisEvidenceTier = "ambiguous"
    relevance_label: GenesisRelevanceLabel = "irrelevant"
    reason_for_acceptance: str = ""
    rejection_reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GenesisLiteratureEvidenceReceipt(BaseModel):
    receipt_type: Literal["genesis_literature_evidence_receipt_v1"] = "genesis_literature_evidence_receipt_v1"
    target_name: str = Field(..., min_length=1)
    target_type: GenesisTargetType
    status: Literal[
        "live_quorum_satisfied",
        "live_quorum_degraded",
        "fixture_only",
        "blocked",
        "live_quorum_satisfied_precision_passed",
        "live_quorum_satisfied_precision_partial",
        "live_quorum_satisfied_precision_failed",
        "provider_quorum_failed",
        "no_target_relevant_evidence_found",
        "citation_materialization_failed",
    ]
    queries: List[str] = Field(default_factory=list)
    providers_attempted: List[str] = Field(default_factory=list)
    provider_status: Dict[str, Any] = Field(default_factory=dict)
    citation_refs: List[Dict[str, Any]] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    evidence_grade: GenesisEvidenceGrade
    claim_boundary: ClaimBoundary
    paper_count: int = Field(0, ge=0)
    query_spec: Dict[str, Any] = Field(default_factory=dict)
    target_profile: Dict[str, Any] = Field(default_factory=dict)
    target_identity_bundle: Dict[str, Any] = Field(default_factory=dict)
    query_bundle: List[Dict[str, Any]] = Field(default_factory=list)
    provider_results: List[Dict[str, Any]] = Field(default_factory=list)
    deduplicated_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    ranked_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    authority_boundary: Dict[str, Any] = Field(default_factory=dict)
    literature_query_adapter: Dict[str, Any] = Field(default_factory=dict)
    dlm_evidence_receipt: Dict[str, Any] = Field(default_factory=dict)
    atom_evidence_graph_receipt: Dict[str, Any] = Field(default_factory=dict)
    evidence_decision: Dict[str, Any] = Field(default_factory=dict)
    accepted_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    accepted_same_target_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    accepted_homolog_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    background_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    rejected_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    quorum_status: str = ""
    precision_status: GenesisPrecisionStatus = "blocked"
    provider_quorum_satisfied: bool = False
    target_precision_satisfied: bool = False
    citation_materialization_satisfied: bool = False
    evidence_quality_satisfied: bool = False
    limitations: List[str] = Field(default_factory=list)
    artifact_policy: Dict[str, Any] = Field(default_factory=lambda: dict(_DEFAULT_OUTPUT_POLICY))
    created_at: str = Field(default_factory=utcnow_iso)


def build_genesis_target_profile(
    request: GenesisEvidenceRequest,
    *,
    dataset_manifest: GenesisDatasetManifest | None = None,
) -> GenesisTargetProfile:
    dataset_context = dict(request.source_dataset_context or {})
    dataset_accessions: List[str] = []
    dataset_gene_names: List[str] = []
    dataset_families: List[str] = []
    dataset_organisms: List[str] = []
    if dataset_manifest is not None:
        dataset_context.setdefault("dataset_id", dataset_manifest.dataset_id)
        dataset_context.setdefault("dataset_record_count", dataset_manifest.record_count)
        dataset_context.setdefault("dataset_filters", dict(dataset_manifest.filters_applied or {}))
        for record in list(dataset_manifest.records or [])[:25]:
            dataset_accessions.append(str(record.target_id or "").strip())
            if record.gene_name:
                dataset_gene_names.append(record.gene_name)
            if record.protein_family:
                dataset_families.append(record.protein_family)
            if record.organism:
                dataset_organisms.append(record.organism)

    organism = _safe_optional_text(request.organism) or (_dedupe_strings(dataset_organisms)[0] if dataset_organisms else None)
    gene_symbol = _safe_optional_text(request.gene_symbol) or (_dedupe_strings(dataset_gene_names)[0] if dataset_gene_names else None)
    protein_name = _safe_optional_text(request.protein_name)
    ec_numbers = _dedupe_strings(list(request.ec_numbers or []) or [_extract_ec_number(request.target_name)])
    synonyms = _dedupe_strings(
        [
            *list(request.synonyms or []),
            *list(dataset_families or []),
        ]
    )
    canonical_names = {
        str(request.target_name or "").casefold(),
        str(gene_symbol or "").casefold(),
        str(protein_name or "").casefold(),
    }
    synonyms = [value for value in synonyms if value.casefold() not in canonical_names]
    profile_id = f"genesis-target-{derived_sha256(request.target_name, gene_symbol or '', organism or '', *dataset_accessions[:5])[:16]}"
    return GenesisTargetProfile(
        target_profile_id=profile_id,
        target_name=request.target_name,
        gene_symbol=gene_symbol,
        protein_name=protein_name,
        organism=organism,
        taxon_id=_safe_optional_text(request.taxon_id),
        uniprot_accessions=_dedupe_strings([*list(request.uniprot_accessions or []), *dataset_accessions]),
        ec_numbers=ec_numbers,
        synonyms=synonyms,
        function_terms=_dedupe_strings(list(request.function_terms or []) or [request.target_name]),
        substrate_terms=_dedupe_strings(request.substrate_terms),
        mechanism_terms=_dedupe_strings(request.mechanism_terms),
        structure_terms=_dedupe_strings(request.structure_terms),
        evidence_intent=request.evidence_intent,
        must_include=_dedupe_strings(request.must_include),
        must_not_include=_dedupe_strings(request.must_not_include),
        source_dataset_context=dataset_context,
    )


def build_genesis_target_identity_bundle(target_profile: GenesisTargetProfile) -> GenesisTargetIdentityBundle:
    dataset_context = dict(target_profile.source_dataset_context or {})
    family_terms = _dedupe_strings(
        [
            *list(target_profile.synonyms or []),
            str(dataset_context.get("protein_family") or ""),
            *list(dataset_context.get("dataset_families") or []),
        ]
    )
    exact_aliases = _dedupe_strings(
        [
            target_profile.target_name,
            str(target_profile.protein_name or ""),
            str(target_profile.gene_symbol or ""),
            *list(target_profile.uniprot_accessions or []),
        ]
    )
    canonical_folded = {item.casefold() for item in exact_aliases}
    loose_aliases = [
        item
        for item in _dedupe_strings(
            [
                *list(target_profile.synonyms or []),
                *family_terms,
                *(["TIM", "TIM barrel"] if "triosephosphate isomerase" in target_profile.target_name.casefold() else []),
            ]
        )
        if item.casefold() not in canonical_folded
    ]
    metadata_resolution_status: Literal["resolved", "metadata_resolution_partial"] = "resolved"
    evidence_basis: List[str] = []
    if target_profile.uniprot_accessions:
        evidence_basis.append("uniprot_accessions")
    else:
        metadata_resolution_status = "metadata_resolution_partial"
    if target_profile.organism:
        evidence_basis.append("organism")
    else:
        metadata_resolution_status = "metadata_resolution_partial"
    if target_profile.gene_symbol or target_profile.protein_name:
        evidence_basis.append("target_identity_name")
    forbidden_ambiguity_terms = _dedupe_strings(
        [
            "biomarker",
            "fibrosis",
            "cancer",
            "review",
            "background",
            *list(target_profile.must_not_include or []),
        ]
    )
    return GenesisTargetIdentityBundle(
        canonical_target_name=target_profile.target_name,
        canonical_gene_symbol=_safe_optional_text(target_profile.gene_symbol),
        canonical_protein_name=_safe_optional_text(target_profile.protein_name or target_profile.target_name),
        organism=_safe_optional_text(target_profile.organism),
        taxon_id=_safe_optional_text(target_profile.taxon_id),
        uniprot_accessions=list(target_profile.uniprot_accessions or []),
        ec_numbers=list(target_profile.ec_numbers or []),
        exact_aliases=exact_aliases,
        loose_aliases=loose_aliases,
        organism_aliases=_organism_aliases(target_profile.organism, target_profile.taxon_id),
        family_terms=family_terms,
        substrate_terms=list(target_profile.substrate_terms or []),
        mechanism_terms=_dedupe_strings([*list(target_profile.function_terms or []), *list(target_profile.mechanism_terms or []), *list(target_profile.structure_terms or [])]),
        forbidden_ambiguity_terms=forbidden_ambiguity_terms,
        metadata_resolution_status=metadata_resolution_status,
        evidence_basis=evidence_basis,
    )


def build_genesis_query_bundle(
    target_profile: GenesisTargetProfile,
    *,
    required_sources: Sequence[str],
    target_identity_bundle: GenesisTargetIdentityBundle | None = None,
    evidence_questions: Sequence[str] | None = None,
) -> List[GenesisEvidenceQuery]:
    identity_bundle = target_identity_bundle or build_genesis_target_identity_bundle(target_profile)
    queries: List[GenesisEvidenceQuery] = []
    seen: set[str] = set()

    def _add_query(
        *,
        query_class: GenesisQueryClass,
        query_text: str,
        fielded_query_text: str | None,
        target_fields_used: Sequence[str],
        expected_relevance: str,
        expected_evidence_tier: GenesisEvidenceTier,
        strictness_level: Literal["high", "medium", "broad"],
        reason: str,
        max_results: int = 5,
    ) -> None:
        normalized = str(query_text or "").strip()
        if not normalized:
            return
        dedupe_key = f"{query_class}:{normalized.casefold()}"
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        query_id = f"{query_class}:{_safe_slug(normalized)}"
        queries.append(
            GenesisEvidenceQuery(
                query_id=query_id,
                query_text=normalized,
                query_class=query_class,
                fielded_query_text=_safe_optional_text(fielded_query_text),
                target_fields_used=_dedupe_strings(target_fields_used),
                expected_relevance=expected_relevance,
                expected_evidence_tier=expected_evidence_tier,
                provider_constraints={"sources": list(required_sources)},
                max_results=max_results,
                strictness_level=strictness_level,
                reason=reason,
            )
        )

    accession = identity_bundle.uniprot_accessions[0] if identity_bundle.uniprot_accessions else ""
    ec_number = identity_bundle.ec_numbers[0] if identity_bundle.ec_numbers else ""
    organism_query_terms = _preferred_organism_query_terms(identity_bundle.organism, identity_bundle.taxon_id)
    organism = str(organism_query_terms[0] if organism_query_terms else identity_bundle.organism or "").strip()
    protein_name = str(identity_bundle.canonical_protein_name or target_profile.target_name).strip()
    gene_symbol = str(identity_bundle.canonical_gene_symbol or "").strip()
    mechanism_terms = _dedupe_strings(list(identity_bundle.mechanism_terms or []))
    function_terms = _dedupe_strings([*list(target_profile.function_terms or []), *list(identity_bundle.substrate_terms or [])])
    family_terms = _dedupe_strings(list(identity_bundle.family_terms or []) or list(identity_bundle.loose_aliases or []))

    if accession and protein_name:
        _add_query(
            query_class="same_target_strict",
            query_text=f'{accession} "{protein_name}"',
            fielded_query_text=f'{accession}[All Fields] AND "{protein_name}"[Title/Abstract]',
            target_fields_used=["uniprot_accessions", "protein_name"],
            expected_relevance="Exact accession-conditioned same-target evidence.",
            expected_evidence_tier="same_target_direct",
            strictness_level="high",
            reason="Accession plus canonical protein name is the strongest same-target bounded query when provider search supports structured PubMed fields.",
        )
    if gene_symbol and organism and protein_name:
        _add_query(
            query_class="same_target_strict",
            query_text=f'{gene_symbol} "{organism}" "{protein_name}"',
            fielded_query_text=f'"{gene_symbol}"[Title/Abstract] AND "{organism}"[Title/Abstract] AND "{protein_name}"[Title/Abstract]',
            target_fields_used=["gene_symbol", "organism", "protein_name", "uniprot_accessions"],
            expected_relevance="Gene, organism, and canonical protein name conditioned same-target evidence.",
            expected_evidence_tier="same_target_direct",
            strictness_level="high",
            reason="Prefer gene plus common-organism alias plus canonical name; UniProt accessions stay in metadata instead of becoming brittle lexical seeds.",
        )
    if protein_name and organism:
        _add_query(
            query_class="same_target_strict",
            query_text=f'"{protein_name}" "{organism}"',
            fielded_query_text=f'"{protein_name}"[Title/Abstract] AND "{organism}"[Title/Abstract]',
            target_fields_used=["protein_name", "organism", "uniprot_accessions"],
            expected_relevance="Protein and organism conditioned same-target evidence.",
            expected_evidence_tier="same_target_direct",
            strictness_level="high",
            reason="Quoted protein name plus organism narrows PubMed toward exact-target mentions while keeping accessions as metadata only.",
        )
    if gene_symbol and protein_name:
        _add_query(
            query_class="same_target_strict",
            query_text=f'{gene_symbol} "{protein_name}"',
            fielded_query_text=f'"{gene_symbol}"[Title/Abstract] AND "{protein_name}"[Title/Abstract]',
            target_fields_used=["gene_symbol", "protein_name", "uniprot_accessions"],
            expected_relevance="Gene plus canonical protein name conditioned same-target evidence.",
            expected_evidence_tier="same_target_direct",
            strictness_level="high",
            reason="Gene plus canonical protein name provides an exact-target fallback when organism wording is sparse in the abstract.",
        )
    if gene_symbol and ec_number:
        _add_query(
            query_class="function_with_target",
            query_text=" ".join(_dedupe_strings([gene_symbol, organism, ec_number])).strip(),
            fielded_query_text=" AND ".join(
                [
                    f'"{gene_symbol}"[Title/Abstract]',
                    *([f'"{organism}"[Title/Abstract]'] if organism else []),
                    f'"{ec_number}"[Title/Abstract]',
                ]
            ),
            target_fields_used=["gene_symbol", "organism", "ec_numbers", "uniprot_accessions"],
            expected_relevance="Target identity linked to EC/function evidence.",
            expected_evidence_tier="same_target_functional",
            strictness_level="high",
            reason="Gene symbol plus organism alias plus EC number asks for target-linked catalytic literature rather than EC-only background.",
        )
    if protein_name and function_terms:
        _add_query(
            query_class="function_with_target",
            query_text=" ".join(_dedupe_strings([protein_name, ec_number, *function_terms[:2]])).strip(),
            fielded_query_text=" AND ".join(
                [
                    f'"{protein_name}"[Title/Abstract]',
                    *([f'"{ec_number}"[Title/Abstract]'] if ec_number else []),
                    *(f'"{term}"[Title/Abstract]' for term in function_terms[:2]),
                ]
            ),
            target_fields_used=["protein_name", "ec_numbers", "function_terms"],
            expected_relevance="Target-linked function, assay, or catalytic evidence.",
            expected_evidence_tier="same_target_functional",
            strictness_level="medium",
            reason="Canonical protein name plus EC/function terms should surface functional studies without drifting to EC-only background.",
        )
    mechanism_seed = mechanism_terms[:2] or ["mutagenesis", "kinetics"]
    if family_terms or protein_name or ec_number:
        _add_query(
            query_class="homolog_mechanism",
            query_text=" ".join(_dedupe_strings([(family_terms[:1] or [protein_name])[0], ec_number, *mechanism_seed])).strip(),
            fielded_query_text=" AND ".join(
                term for term in [
                    f'"{(family_terms[:1] or [protein_name])[0]}"[Title/Abstract]' if (family_terms[:1] or [protein_name])[0] else "",
                    f'"{ec_number}"[Title/Abstract]' if ec_number else "",
                    *(f'"{term}"[Title/Abstract]' for term in mechanism_seed),
                ] if term
            ),
            target_fields_used=["family_terms", "ec_numbers", "mechanism_terms", "structure_terms"],
            expected_relevance="Homolog or family mechanistic evidence, explicitly weaker than same-target evidence.",
            expected_evidence_tier="homolog_mechanistic",
            strictness_level="medium",
            reason="Family or homolog mechanism query is allowed for support, but it cannot prove same-target identity alone.",
        )
    _add_query(
        query_class="dataset_support",
        query_text=" ".join(_dedupe_strings([accession or gene_symbol or protein_name, (family_terms[:1] or ["homolog"])[0], "annotation"])).strip(),
        fielded_query_text=None,
        target_fields_used=["uniprot_accessions", "gene_symbol", "protein_name", "family_terms"],
        expected_relevance="Dataset inclusion, annotation, or homolog support evidence.",
        expected_evidence_tier="homolog_direct",
        strictness_level="medium",
        reason="Dataset support queries can justify inclusion or exclusion, but they should not create fake same-target confidence.",
    )
    negative_seed = _dedupe_strings([ec_number, *(family_terms[:1] or []), *list(function_terms[:1])])[:2] or ["enzyme"]
    _add_query(
        query_class="negative_control",
        query_text=" ".join(_dedupe_strings([*negative_seed, "review background"])).strip(),
        fielded_query_text=None,
        target_fields_used=["ec_numbers", "family_terms", "function_terms"],
        expected_relevance="Broad adjacent control query that must not pass as same-target evidence on its own.",
        expected_evidence_tier="broad_context_only",
        strictness_level="broad",
        reason="Negative control proves the scorer does not convert family/EC background into target-specific evidence.",
    )
    for question in list(evidence_questions or [])[:1]:
        _add_query(
            query_class="homolog_mechanism",
            query_text=question,
            fielded_query_text=None,
            target_fields_used=["evidence_questions"],
            expected_relevance="Operator-specified evidence question.",
            expected_evidence_tier="ambiguous",
            strictness_level="medium",
            reason="Operator-specified question is preserved as an auditable bounded query input.",
        )
    return queries


def materialize_genesis_citation_candidates(
    target_profile: GenesisTargetProfile,
    *,
    query_bundle: Sequence[GenesisEvidenceQuery],
    provider_results: Sequence[Mapping[str, Any]],
) -> tuple[List[GenesisEvidenceCandidate], List[Dict[str, Any]], bool]:
    query_map = {item.query_id: item for item in list(query_bundle or [])}
    candidates: List[GenesisEvidenceCandidate] = []
    rejected: List[Dict[str, Any]] = []
    saw_valid_materialization = False
    for result in list(provider_results or []):
        query_id = str(result.get("query_id") or "").strip()
        query = query_map.get(query_id)
        for paper in list(result.get("papers") or []):
            title = str(paper.get("title") or "").strip()
            if not title:
                rejected.append(
                    {
                        "query_id": query_id,
                        "provider": str(paper.get("provider") or paper.get("source") or ""),
                        "rejection_reason": "missing_title_for_final_citation",
                        "paper": dict(paper),
                    }
                )
                continue
            provider = str(paper.get("provider") or paper.get("source") or "").strip().lower()
            candidate = GenesisEvidenceCandidate(
                candidate_id=f"candidate-{derived_sha256(title, str(paper.get('canonical_id') or ''), query_id)[:16]}",
                canonical_id=_safe_optional_text(paper.get("canonical_id")),
                title=title,
                authors=_extract_author_names(paper.get("authors")),
                journal_source=_extract_journal_source(paper),
                publication_year=_extract_publication_year(paper),
                pmid=_normalized_identifier(paper.get("pmid") or (paper.get("externalIds") or {}).get("PubMed")),
                doi=_normalized_identifier(paper.get("doi")),
                pmcid=_normalized_identifier(paper.get("pmcid") or (paper.get("metadata") or {}).get("pmcid")),
                provider=provider,
                provider_record_url=_extract_provider_record_url(paper),
                abstract_snippet=_truncate_snippet(paper.get("abstract") or paper.get("summary") or ""),
                retrieval_timestamp=str(paper.get("fetch_timestamp") or utcnow_iso()),
                query_ids=[query_id] if query_id else [],
                query_classes=[query.query_class] if query is not None else [],
                provider_support=[provider] if provider else [],
                target_profile_id=target_profile.target_profile_id,
                evidence_tier=query.expected_evidence_tier if query is not None else "ambiguous",
                metadata={
                    "paper": dict(paper),
                    "query_text": str(result.get("query_text") or ""),
                    "query_class": str(result.get("query_class") or ""),
                    "provider_receipt": dict(result.get("provider_receipt") or {}),
                },
            )
            candidates.append(candidate)
            saw_valid_materialization = True
    deduped: Dict[str, GenesisEvidenceCandidate] = {}
    for candidate in candidates:
        dedupe_key = (
            str(candidate.pmid or "").strip()
            or str(candidate.doi or "").strip().lower()
            or str(candidate.pmcid or "").strip().upper()
            or _normalize_title_key(candidate.title)
        )
        existing = deduped.get(dedupe_key)
        if existing is None:
            deduped[dedupe_key] = candidate
            continue
        existing.query_ids = _dedupe_strings([*existing.query_ids, *candidate.query_ids])
        existing.provider_support = _dedupe_strings([*existing.provider_support, *candidate.provider_support])
        existing.query_classes = list(dict.fromkeys([*existing.query_classes, *candidate.query_classes]))
        existing.authors = _dedupe_strings([*existing.authors, *candidate.authors])
        if not existing.provider_record_url and candidate.provider_record_url:
            existing.provider_record_url = candidate.provider_record_url
        if not existing.abstract_snippet and candidate.abstract_snippet:
            existing.abstract_snippet = candidate.abstract_snippet
        existing.metadata.setdefault("merged_papers", []).append(candidate.metadata.get("paper"))
    return list(deduped.values()), rejected, saw_valid_materialization


def score_genesis_evidence_candidates(
    target_profile: GenesisTargetProfile,
    *,
    candidates: Sequence[GenesisEvidenceCandidate],
) -> List[GenesisEvidenceCandidate]:
    scored: List[GenesisEvidenceCandidate] = []
    identity_bundle = build_genesis_target_identity_bundle(target_profile)
    accession_terms = _dedupe_strings(list(identity_bundle.uniprot_accessions or []))
    exact_alias_terms = _dedupe_strings(list(identity_bundle.exact_aliases or []))
    loose_alias_terms = _dedupe_strings(list(identity_bundle.loose_aliases or []))
    ec_terms = _dedupe_strings(list(identity_bundle.ec_numbers or []))
    family_terms = _dedupe_strings(list(identity_bundle.family_terms or []))
    function_terms = _dedupe_strings([*list(target_profile.function_terms or []), *list(identity_bundle.substrate_terms or []), *ec_terms])
    mechanism_terms = _dedupe_strings(list(identity_bundle.mechanism_terms or []))
    organism_terms = _dedupe_strings(list(identity_bundle.organism_aliases or []))
    must_not_include = _dedupe_strings([*list(target_profile.must_not_include or []), *list(identity_bundle.forbidden_ambiguity_terms or [])])
    must_include = _dedupe_strings(list(target_profile.must_include or []))

    for candidate in list(candidates or []):
        raw_haystack = _text_haystack(
            candidate.title,
            candidate.abstract_snippet,
            candidate.journal_source,
            candidate.authors,
        )
        haystack = str(raw_haystack or "")
        accession_matches = _exact_term_matches(haystack, accession_terms)
        exact_alias_matches = _exact_term_matches(haystack, exact_alias_terms)
        loose_alias_matches = _term_matches(haystack, loose_alias_terms)
        ec_matches = _exact_term_matches(haystack, ec_terms)
        family_matches = _term_matches(haystack, family_terms)
        function_matches = _term_matches(haystack, function_terms)
        organism_matches = _exact_term_matches(haystack, organism_terms)
        mechanism_matches = _term_matches(haystack, mechanism_terms)
        must_include_matches = _term_matches(haystack, must_include)
        negative_matches = _term_matches(haystack, must_not_include)
        non_target_species = [
            species
            for species in _extract_species_names(haystack)
            if species.casefold() not in {item.casefold() for item in organism_terms}
        ]

        identity_score = min(
            4.0,
            (3.0 if accession_matches else 0.0)
            + min(2.0, 1.0 * len(exact_alias_matches))
            + min(1.0, 0.5 * len(loose_alias_matches)),
        )
        organism_score = min(1.5, 0.75 * len(organism_matches))
        function_score = min(2.0, (0.75 if ec_matches else 0.0) + (0.5 * len(function_matches)))
        mechanism_score = min(2.0, 0.5 * len(mechanism_matches))
        evidence_intent_score = 0.0
        if target_profile.evidence_intent == "mechanism" and mechanism_matches:
            evidence_intent_score = 1.0
        elif target_profile.evidence_intent == "structure" and mechanism_matches:
            evidence_intent_score = 1.0
        elif target_profile.evidence_intent == "dataset_support" and (function_matches or family_matches):
            evidence_intent_score = 0.75
        elif target_profile.evidence_intent == "review" and function_matches:
            evidence_intent_score = 0.5
        if must_include and must_include_matches:
            evidence_intent_score += 0.5
        provider_support_score = min(1.0, 0.25 * len(candidate.provider_support or []))
        ambiguity_penalty = 0.0
        broadness_penalty = 0.0
        if identity_bundle.metadata_resolution_status == "metadata_resolution_partial":
            ambiguity_penalty += 0.5
        if not accession_matches and not exact_alias_matches and loose_alias_matches:
            ambiguity_penalty += 0.5
        if organism_terms and non_target_species:
            ambiguity_penalty += 1.0
        if candidate.query_classes and all(item == "negative_control" for item in candidate.query_classes):
            broadness_penalty += 1.5
        if candidate.query_classes and all(item == "dataset_support" for item in candidate.query_classes):
            broadness_penalty += 0.5
        if _contains_review_signal(haystack):
            broadness_penalty += 1.25
        if ec_matches and not accession_matches and not exact_alias_matches:
            broadness_penalty += 0.75
        if family_matches and not accession_matches and not exact_alias_matches:
            broadness_penalty += 0.5
        negative_penalty = float(len(negative_matches)) * 1.0
        if organism_terms and non_target_species:
            negative_penalty += 0.75

        final_relevance_score = round(
            identity_score
            + organism_score
            + function_score
            + mechanism_score
            + evidence_intent_score
            + provider_support_score
            - ambiguity_penalty
            - broadness_penalty
            - negative_penalty,
            4,
        )
        same_target_identity = bool(
            accession_matches
            or (
                exact_alias_matches
                and organism_matches
                and not non_target_species
            )
        )
        exact_name_without_organism = bool(exact_alias_matches and not organism_matches and not accession_matches)
        homolog_identity = bool(
            (family_matches or loose_alias_matches or exact_name_without_organism)
            and not same_target_identity
        )
        review_signal = _contains_review_signal(haystack)
        if same_target_identity and (function_matches or mechanism_matches or ec_matches):
            evidence_tier: GenesisEvidenceTier = "same_target_functional"
        elif same_target_identity:
            evidence_tier = "same_target_direct"
        elif review_signal and (family_matches or loose_alias_matches or function_matches or mechanism_matches or ec_matches):
            evidence_tier = "broad_context_only"
        elif homolog_identity and (function_matches or mechanism_matches or ec_matches):
            evidence_tier = "homolog_mechanistic"
        elif homolog_identity:
            evidence_tier = "homolog_direct"
        elif family_matches or (ec_matches and not same_target_identity):
            evidence_tier = "family_background"
        elif review_signal or function_matches or mechanism_matches:
            evidence_tier = "broad_context_only"
        elif final_relevance_score > 0:
            evidence_tier = "ambiguous"
        else:
            evidence_tier = "irrelevant"

        if evidence_tier in {"same_target_direct", "same_target_functional"}:
            label: GenesisRelevanceLabel = "strong_target_match"
        elif evidence_tier in {"homolog_direct", "homolog_mechanistic"}:
            label = "likely_target_match"
        elif evidence_tier in {"family_background", "broad_context_only"}:
            label = "broad_context_only"
        elif evidence_tier == "ambiguous":
            label = "ambiguous"
        else:
            label = "irrelevant"

        identity_match_basis = _dedupe_strings(
            [
                *(["accession_match"] if accession_matches else []),
                *(["exact_alias_plus_organism"] if exact_alias_matches and organism_matches else []),
                *(["exact_alias_without_organism"] if exact_name_without_organism else []),
                *(["family_or_loose_alias"] if homolog_identity else []),
            ]
        )

        candidate.score_breakdown = {
            "identity_score": identity_score,
            "function_score": function_score,
            "organism_score": organism_score,
            "mechanism_score": mechanism_score,
            "evidence_intent_score": evidence_intent_score,
            "provider_support_score": provider_support_score,
            "ambiguity_penalty": ambiguity_penalty,
            "broadness_penalty": broadness_penalty,
            "negative_penalty": negative_penalty,
            "final_relevance_score": final_relevance_score,
        }
        candidate.final_relevance_score = final_relevance_score
        candidate.evidence_tier = evidence_tier
        candidate.relevance_label = label
        candidate.matched_target_terms = _dedupe_strings([*accession_matches, *exact_alias_matches, *loose_alias_matches, *family_matches])
        candidate.matched_organism_terms = _dedupe_strings(organism_matches)
        candidate.matched_function_terms = _dedupe_strings([*function_matches, *ec_matches])
        candidate.matched_mechanism_terms = _dedupe_strings(mechanism_matches)
        candidate.identity_match_basis = identity_match_basis
        candidate.target_fields_matched = _dedupe_strings(
            [
                *(["uniprot_accessions"] if accession_matches else []),
                *(["gene_symbol/protein_name/exact_aliases"] if exact_alias_matches else []),
                *(["family_terms/loose_aliases"] if (loose_alias_matches or family_matches) else []),
                *(["ec_numbers/function_terms/substrate_terms"] if (ec_matches or function_matches) else []),
                *(["function_terms/substrate_terms"] if function_matches else []),
                *(["organism/taxon_id"] if organism_matches else []),
                *(["mechanism_terms/structure_terms"] if mechanism_matches else []),
                *(["must_include"] if must_include_matches else []),
            ]
        )
        scored.append(candidate)

    return sorted(scored, key=lambda item: (item.final_relevance_score, item.publication_year or 0, item.title), reverse=True)


class GenesisDatasetBuilder:
    """Thin Genesis wrapper over real LMP/Nesymol search surfaces."""

    def __init__(
        self,
        *,
        scanner: Any | None = None,
        metadata_service: Any | None = None,
        cache_dir: str | Path = "lmp_cache",
        scanner_output_dir: str | Path = ".mica/programs/GENESIS_SUPERNOVA/tmp",
        allow_scanner_autoresolve: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.scanner_output_dir = Path(scanner_output_dir)
        self._scanner = scanner
        self._metadata_service = metadata_service
        self.allow_scanner_autoresolve = bool(allow_scanner_autoresolve)

    def build_dataset(self, request: GenesisDatasetRequest) -> tuple[GenesisDatasetManifest, GenesisDatasetBuilderReceipt]:
        dataset_id = f"genesis-{request.target_type}-{_safe_slug(request.target_name)}-{_safe_slug(request.request_id)}"
        metadata_service = self._resolve_metadata_service()
        scanner = self._resolve_scanner()
        query_plan = self._build_query_plan(request, metadata_service_available=metadata_service is not None)
        source_refs: List[Dict[str, Any]] = [
            {
                "surface": "LMPMetadataService",
                "available": metadata_service is not None,
                "cache_dir": str(self.cache_dir),
                "metadata_dir": str(self.cache_dir / "metadata"),
            },
            {
                "surface": "LMPScanner",
                "available": scanner is not None,
                "config_path": "src/bsm/lmp/lmp_config.yaml",
                "output_dir": str(self.scanner_output_dir),
            },
            {
                "surface": "workspace_artifact_contract",
                "contract_ref": _DEFAULT_OUTPUT_POLICY["artifact_contract_ref"],
            },
        ]
        backlog_items = self._base_backlog_items(metadata_service_available=metadata_service is not None)

        records: List[GenesisDatasetRecord] = []
        blocker: GenesisDatasetBlocker | None = None

        if query_plan["primary_surface"] == "metadata_search" and metadata_service is not None:
            records = self._run_metadata_search(metadata_service, request, query_plan)

        if not records and scanner is not None:
            scanned_records, scan_ref = self._run_scanner_query(scanner, metadata_service, request, query_plan)
            records = scanned_records
            source_refs.append(scan_ref)

        if not records and scanner is None and metadata_service is None:
            blocker = GenesisDatasetBlocker(
                code="lmp_runtime_unavailable",
                detail="Neither LMP metadata cache nor LMP scanner could be resolved for this request.",
            )
            backlog_items.append(
                "GENESIS-LMP-00: wire a guaranteed local LMP runtime seam for Genesis dataset requests when cache and scanner are both absent."
            )
        elif not records:
            blocker = GenesisDatasetBlocker(
                code="no_records_found",
                detail="The current LMP/Nesymol surfaces produced no dataset records for this request.",
            )

        claim_boundary = self._resolve_claim_boundary(request)
        filters_applied = {
            "sequence_filters": dict(request.sequence_filters),
            "structure_filters": dict(request.structure_filters),
            "function_filters": dict(request.function_filters),
            "organism_filter": request.organism_filter,
        }
        if claim_boundary == "blocked_missing_gcs_uri":
            blocker = blocker or GenesisDatasetBlocker(
                code="missing_gcs_uri",
                detail="Request asked for gcs_production output but no gcs_uri was supplied in output_policy.",
            )
        status: Literal["completed", "partial", "blocked"]
        if blocker and not records:
            status = "blocked"
        elif blocker:
            status = "partial"
        else:
            status = "completed"

        manifest = GenesisDatasetManifest(
            dataset_id=dataset_id,
            request=request,
            records=records[: request.max_records],
            source_refs=source_refs,
            filters_applied=filters_applied,
            gcs_uri=str(request.output_policy.get("gcs_uri") or "") or None,
            local_path=str(request.output_policy.get("local_path") or "") or None,
            blocker=blocker,
            claim_boundary=claim_boundary,
            status=status,
            artifact_policy=dict(request.output_policy),
            backlog_items=_dedupe_strings(backlog_items),
        )
        receipt = GenesisDatasetBuilderReceipt(
            request_id=request.request_id,
            dataset_id=manifest.dataset_id,
            status=manifest.status,
            query_plan=query_plan,
            lmp_surfaces={
                "metadata_service_available": metadata_service is not None,
                "scanner_available": scanner is not None,
            },
            record_count=manifest.record_count,
            blocker=manifest.blocker,
            backlog_items=list(manifest.backlog_items),
            claim_boundary=manifest.claim_boundary,
        )
        return manifest, receipt

    def _resolve_metadata_service(self) -> Any | None:
        if self._metadata_service is not None:
            return self._metadata_service
        metadata_dir = self.cache_dir / "metadata"
        if not metadata_dir.exists():
            return None
        self._metadata_service = LMPMetadataService(cache_dir=self.cache_dir)
        return self._metadata_service

    def _resolve_scanner(self) -> Any | None:
        if self._scanner is not None:
            return self._scanner
        if not self.allow_scanner_autoresolve:
            return None
        try:
            self._scanner = LMPScanner(output_dir=str(self.scanner_output_dir))
        except Exception:
            self._scanner = None
        return self._scanner

    def _build_query_plan(
        self,
        request: GenesisDatasetRequest,
        *,
        metadata_service_available: bool,
    ) -> Dict[str, Any]:
        organism = str(request.organism_filter or "").strip()
        semantic_query = f"{request.target_name}{_normalize_organism_for_semantic_query(organism)}".strip()
        ec_number = _extract_ec_number(request.target_name)
        if request.target_type == "protein_family":
            return {
                "primary_surface": "metadata_search" if metadata_service_available else "scan_semantic",
                "metadata_criteria": {
                    "protein_family": request.target_name,
                    **({"organism": organism} if organism else {}),
                },
                "scanner_mode": "scan_semantic",
                "scanner_query": semantic_query,
            }
        if request.target_type == "enzyme":
            if ec_number:
                uniprot_query = f"ec:{ec_number}"
            else:
                uniprot_query = f'protein_name:"{request.target_name}"'
            if organism:
                if organism.isdigit():
                    uniprot_query = f"{uniprot_query} AND organism_id:{organism}"
                else:
                    uniprot_query = f'{uniprot_query} AND organism_name:"{organism}"'
            return {
                "primary_surface": "metadata_search" if metadata_service_available else "scan_uniprot",
                "metadata_criteria": {
                    "query": request.target_name,
                    **({"organism": organism} if organism else {}),
                },
                "scanner_mode": "scan_uniprot",
                "scanner_query": uniprot_query,
                "ec_number": ec_number,
            }
        return {
            "primary_surface": "metadata_search" if metadata_service_available else "scan_semantic",
            "metadata_criteria": {
                "query": request.target_name,
                **({"organism": organism} if organism else {}),
            },
            "scanner_mode": "scan_semantic",
            "scanner_query": semantic_query,
        }

    def _run_metadata_search(
        self,
        metadata_service: Any,
        request: GenesisDatasetRequest,
        query_plan: Mapping[str, Any],
    ) -> List[GenesisDatasetRecord]:
        criteria = dict(query_plan.get("metadata_criteria") or {})
        results = list(metadata_service.search(**criteria))
        return [self._record_from_metadata(item) for item in results[: request.max_records]]

    def _run_scanner_query(
        self,
        scanner: Any,
        metadata_service: Any | None,
        request: GenesisDatasetRequest,
        query_plan: Mapping[str, Any],
    ) -> tuple[List[GenesisDatasetRecord], Dict[str, Any]]:
        scanner_mode = str(query_plan.get("scanner_mode") or "scan_semantic")
        scanner_query = str(query_plan.get("scanner_query") or request.target_name)
        post_filter_results: Dict[str, Any] = {}
        ids: List[str] = []
        if scanner_mode == "scan_uniprot":
            ids = list(scanner.scan_uniprot(scanner_query, limit=request.max_records))
        else:
            result = dict(scanner.scan_semantic(scanner_query, limit=request.max_records, apply_post_filters=True) or {})
            ids = list(result.get("ids") or [])
            post_filter_results = dict(result.get("post_filter_results") or {})
        records = self._records_from_ids(ids[: request.max_records], metadata_service)
        return records, {
            "surface": "LMPScanner",
            "scanner_mode": scanner_mode,
            "scanner_query": scanner_query,
            "post_filter_results": post_filter_results,
        }

    def _records_from_ids(self, ids: Sequence[str], metadata_service: Any | None) -> List[GenesisDatasetRecord]:
        metadata_map: Dict[str, ProteinMetadata | None] = {}
        if metadata_service is not None and hasattr(metadata_service, "get_many"):
            metadata_map = dict(metadata_service.get_many(list(ids)))
        records: List[GenesisDatasetRecord] = []
        for target_id in ids:
            metadata = metadata_map.get(target_id)
            if metadata is not None:
                records.append(self._record_from_metadata(metadata, source="lmp_scanner+metadata"))
            else:
                records.append(
                    GenesisDatasetRecord(
                        target_id=str(target_id),
                        source="lmp_scanner",
                        metadata={},
                    )
                )
        return records

    def _record_from_metadata(
        self,
        metadata: ProteinMetadata,
        *,
        source: str = "lmp_metadata",
    ) -> GenesisDatasetRecord:
        return GenesisDatasetRecord(
            target_id=metadata.uniprot_id,
            gene_name=metadata.gene_name,
            protein_family=metadata.protein_family,
            organism=metadata.organism,
            sequence_length=metadata.sequence_length,
            source=source,
            metadata={
                "num_ptms": metadata.num_ptms,
                "num_domains": metadata.num_domains,
                "num_binding_sites": metadata.num_binding_sites,
                "has_approved_drugs": metadata.has_approved_drugs,
            },
        )

    def _base_backlog_items(self, *, metadata_service_available: bool) -> List[str]:
        items = [
            "GENESIS-LMP-02: promote LMP scanner outputs into a canonical Genesis dataset manifest artifact instead of only dataset_manifest.jsonl side effects.",
            "GENESIS-LMP-03: add first-class artifact refs from LMP/Nesymol dataset builds so Genesis can bind datasets to durable workspace custody.",
        ]
        if not metadata_service_available:
            items.append(
                "GENESIS-LMP-01: populate lmp_cache/metadata so Genesis can resolve enzyme and protein-family context without falling back to network-only scanner queries."
            )
        return items

    def _resolve_claim_boundary(self, request: GenesisDatasetRequest) -> ClaimBoundary:
        if request.claim_boundary == "gcs_production" and not request.output_policy.get("gcs_uri"):
            return "blocked_missing_gcs_uri"
        return request.claim_boundary


class GenesisEvidenceAdapter:
    """Thin Genesis wrapper over Bibliotecario/provider quorum surfaces."""

    def __init__(
        self,
        *,
        quorum_service: ProviderQuorumService | None = None,
        allow_live_quorum: bool = False,
        fixture_citation_refs: Optional[List[Dict[str, Any]]] = None,
        dlm_encoder: DLMEncoder | None = None,
        entity_mapper: EntityMapper | None = None,
        atom_decomposer: ATOMAtomicDecomposer | None = None,
        atom_extractor: ATOMQuintupleExtractor | None = None,
        enable_semantic_pipeline: bool = True,
        max_semantic_candidates: int = 3,
    ) -> None:
        self._quorum_service = quorum_service
        self.allow_live_quorum = bool(allow_live_quorum)
        self.fixture_citation_refs = list(fixture_citation_refs or [])
        self._dlm_encoder = dlm_encoder
        self._entity_mapper = entity_mapper
        self._atom_decomposer = atom_decomposer
        self._atom_extractor = atom_extractor
        self.enable_semantic_pipeline = bool(enable_semantic_pipeline)
        self.max_semantic_candidates = max(1, int(max_semantic_candidates or 3))

    @staticmethod
    def _authority_boundary() -> Dict[str, Any]:
        return {
            "genesis_owned": [
                "target_profile",
                "evidence_intent",
                "evidence_decision",
                "dataset_decision",
                "model_decision_context",
            ],
            "literature_consolidation_owned": [
                "literature_query_spec",
                "lmp_bibliotecario_handoff",
                "provider_compiler",
                "provider_execution_plan",
                "provider_quorum_runtime",
            ],
            "dlm_owned": [
                "document_encoding",
                "semantic_entity_receipts",
                "entity_mapping",
                "extractor_core_relation_logic",
            ],
            "atom_owned": [
                "atomic_fact_projection",
                "temporal_quintuple_snapshot",
                "atom_jsonld_snapshot",
            ],
        }

    async def collect_evidence(
        self,
        request: GenesisEvidenceRequest,
        *,
        dataset_manifest: GenesisDatasetManifest | None = None,
        run_id: str = "",
        user_id: str = "",
        session_id: str = "",
    ) -> GenesisLiteratureEvidenceReceipt:
        target_profile = build_genesis_target_profile(request, dataset_manifest=dataset_manifest)
        target_identity_bundle = build_genesis_target_identity_bundle(target_profile)
        query_bundle = build_genesis_query_bundle(
            target_profile,
            required_sources=request.required_sources,
            target_identity_bundle=target_identity_bundle,
            evidence_questions=request.evidence_questions,
        )
        executed_queries = self._select_executed_queries(
            request=request,
            query_bundle=query_bundle,
        )
        query_spec_summary: Dict[str, Any] = {
            "mode": "query_bundle",
            "generated_query_count": len(query_bundle),
            "executed_query_count": len(executed_queries),
            "executed_query_ids": [item.query_id for item in executed_queries],
        }

        if not self.allow_live_quorum:
            status = "fixture_only" if self.fixture_citation_refs else "blocked"
            blockers = [] if self.fixture_citation_refs else ["live_provider_quorum_not_attempted"]
            evidence_grade: GenesisEvidenceGrade = "fixture_only" if self.fixture_citation_refs else "blocked_missing_live_evidence"
            return GenesisLiteratureEvidenceReceipt(
                target_name=request.target_name,
                target_type=request.target_type,
                status=status,
                queries=[item.query_text for item in executed_queries],
                providers_attempted=[],
                provider_status={
                    source: {"attempted": False, "status": "not_attempted"}
                    for source in request.required_sources
                },
                citation_refs=list(self.fixture_citation_refs),
                blockers=blockers,
                evidence_grade=evidence_grade,
                claim_boundary="local_non_production",
                paper_count=len(self.fixture_citation_refs),
                query_spec=query_spec_summary,
                target_profile=target_profile.model_dump(),
                target_identity_bundle=target_identity_bundle.model_dump(),
                query_bundle=[item.model_dump() for item in query_bundle],
                authority_boundary=self._authority_boundary(),
                precision_status="fixture_only",
                artifact_policy=dict(request.output_policy),
            )

        quorum_service = self._quorum_service or ProviderQuorumService()
        close_service = self._quorum_service is None
        try:
            provider_results: List[Dict[str, Any]] = []
            executed_specs: List[Dict[str, Any]] = []
            for query in executed_queries:
                spec, handoff = self._build_query_spec(
                    request=request,
                    query=query,
                    target_profile=target_profile,
                    dataset_manifest=dataset_manifest,
                    run_id=run_id,
                    user_id=user_id,
                    session_id=session_id,
                )
                executed_specs.append(
                    {
                        "query_id": query.query_id,
                        "query_class": query.query_class,
                        "spec": spec.model_dump(),
                        "handoff": handoff,
                    }
                )
                runtime = await quorum_service.run_quorum(
                    spec=spec,
                    lane_class="bibliotecario",
                    preset_name="genesis_dataset_evidence",
                    task_type="genesis_dataset_evidence",
                    policy=_normalize_provider_policy(request.provider_policy),
                    enable_unpaywall_enrichment=bool(request.provider_policy.get("enable_unpaywall_enrichment", False)),
                )
                provider_results.append(
                    self._provider_result_from_runtime(
                        query=query,
                        spec=spec,
                        runtime=runtime,
                    )
                )
            return await self._receipt_from_provider_results(
                request=request,
                target_profile=target_profile,
                target_identity_bundle=target_identity_bundle,
                query_bundle=query_bundle,
                executed_queries=executed_queries,
                provider_results=provider_results,
                query_spec_summary={**query_spec_summary, "executed_specs": executed_specs},
                executed_specs=executed_specs,
            )
        finally:
            if close_service:
                close = getattr(quorum_service, "close", None)
                if callable(close):
                    await close()

    def collect_evidence_sync(
        self,
        request: GenesisEvidenceRequest,
        *,
        dataset_manifest: GenesisDatasetManifest | None = None,
        run_id: str = "",
        user_id: str = "",
        session_id: str = "",
    ) -> GenesisLiteratureEvidenceReceipt:
        return _run_async(
            self.collect_evidence(
                request,
                dataset_manifest=dataset_manifest,
                run_id=run_id,
                user_id=user_id,
                session_id=session_id,
            )
        )

    def _handoff_entities(
        self,
        request: GenesisEvidenceRequest,
        dataset_manifest: GenesisDatasetManifest | None,
        *,
        target_profile: GenesisTargetProfile | None = None,
    ) -> List[str]:
        record_terms: List[str] = []
        if dataset_manifest is not None:
            for record in dataset_manifest.records[:5]:
                record_terms.extend(
                    value
                    for value in [record.target_id, record.gene_name or "", record.protein_family or ""]
                    if value
                )
        profile_terms: List[str] = []
        if target_profile is not None:
            profile_terms.extend(
                [
                    target_profile.target_name,
                    str(target_profile.protein_name or ""),
                    str(target_profile.gene_symbol or ""),
                    str(target_profile.organism or ""),
                    *list(target_profile.uniprot_accessions or []),
                    *list(target_profile.ec_numbers or []),
                    *list(target_profile.synonyms or []),
                ]
            )
        return _dedupe_strings([request.target_name, *request.evidence_questions, *record_terms, *profile_terms])

    def _select_executed_queries(
        self,
        *,
        request: GenesisEvidenceRequest,
        query_bundle: Sequence[GenesisEvidenceQuery],
    ) -> List[GenesisEvidenceQuery]:
        limit = max(1, min(3, int(request.provider_policy.get("max_queries") or 3)))
        include_negative = bool(request.provider_policy.get("include_negative_control_query", False))
        available = list(query_bundle or [])
        if request.evidence_intent == "negative_control":
            ordered = sorted(
                available,
                key=lambda item: 0 if item.query_class == "negative_control" else 1,
            )
            return ordered[:limit]
        priority = {
            "same_target_strict": 0,
            "function_with_target": 1,
            "homolog_mechanism": 2,
            "dataset_support": 3,
            "negative_control": 4,
            "exact_identifier_query": 5,
            "function_query": 6,
            "mechanism_query": 7,
            "dataset_support_query": 8,
            "negative_control_query": 9,
        }
        positive = [item for item in available if item.query_class != "negative_control"]
        selected: List[GenesisEvidenceQuery] = []
        preferred_classes = ("same_target_strict", "function_with_target")
        for query_class in preferred_classes:
            if len(selected) >= limit:
                break
            class_items = [item for item in positive if item.query_class == query_class]
            if class_items:
                selected.append(class_items[0])
        selected_ids = {item.query_id for item in selected}
        remaining = [
            item
            for item in sorted(positive, key=lambda entry: priority.get(entry.query_class, 100))
            if item.query_id not in selected_ids
        ]
        selected.extend(remaining[: max(0, limit - len(selected))])
        if include_negative and len(selected) < limit:
            selected.extend(item for item in available if item.query_class == "negative_control")
        return selected[:limit]

    def _build_query_spec(
        self,
        *,
        request: GenesisEvidenceRequest,
        query: GenesisEvidenceQuery,
        target_profile: GenesisTargetProfile,
        dataset_manifest: GenesisDatasetManifest | None,
        run_id: str,
        user_id: str,
        session_id: str,
    ) -> tuple[LiteratureQuerySpec, Dict[str, Any]]:
        dataset_records = [record.model_dump() for record in list(dataset_manifest.records)] if dataset_manifest is not None else []
        dataset_filters = dict(dataset_manifest.filters_applied) if dataset_manifest is not None else {}
        primary_query = query.query_text
        extra_queries: List[str] = []
        handoff_entities = self._handoff_entities(request, dataset_manifest, target_profile=target_profile)
        pubmed_only = len(list(request.required_sources or [])) == 1 and list(request.required_sources or [])[0] == "pubmed"
        precision_primary_only = pubmed_only and query.query_class == "same_target_strict"
        if pubmed_only:
            if query.fielded_query_text:
                primary_query = str(query.fielded_query_text)
                if not precision_primary_only:
                    extra_queries.append(query.query_text)
        if precision_primary_only:
            handoff_entities = []
        handoff = compile_lmp_bibliotecario_handoff(
            query=primary_query,
            entities=handoff_entities,
            extra_queries=extra_queries,
            lmp_handoff={
                "dataset_records": dataset_records,
                "dataset_filters": dataset_filters,
                "target_profile_id": target_profile.target_profile_id,
                "target_fields_used": list(query.target_fields_used),
            },
            require_full_text=bool(request.provider_policy.get("require_full_text", True)),
        )
        spec = LiteratureQuerySpec(
            query=handoff["primary_query"],
            entities=list(handoff.get("extra_queries") or []),
            max_papers=min(
                int(query.max_results),
                int(request.provider_policy.get("max_results_per_query") or query.max_results),
            ),
            sources=list(request.required_sources),
            lane="bibliotecario",
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            uniprot_id=target_profile.uniprot_accessions[0] if target_profile.uniprot_accessions else None,
            accessions=list(target_profile.uniprot_accessions or []) or None,
        )
        return spec, handoff

    def _provider_result_from_runtime(
        self,
        *,
        query: GenesisEvidenceQuery,
        spec: LiteratureQuerySpec,
        runtime: ProviderQuorumRuntimeResult,
    ) -> Dict[str, Any]:
        return {
            "query_id": query.query_id,
            "query_text": query.query_text,
            "query_class": query.query_class,
            "target_fields_used": list(query.target_fields_used),
            "spec": spec.model_dump(),
            "provider_receipt": runtime.receipt.model_dump(),
            "papers": [dict(item) for item in list(runtime.papers or [])],
            "paper_count": int(runtime.receipt.total_papers),
            "failure_records": list(runtime.receipt.failure_records or []),
        }

    def _build_literature_query_adapter_receipt(
        self,
        *,
        target_profile: GenesisTargetProfile,
        executed_queries: Sequence[GenesisEvidenceQuery],
        executed_specs: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "receipt_type": "targetprofile_to_literaturequeryspec_adapter_receipt_v1",
            "status": "completed" if executed_specs else "blocked",
            "target_profile_id": target_profile.target_profile_id,
            "query_count": len(executed_queries),
            "executed_query_ids": [item.query_id for item in executed_queries],
            "literature_query_specs": [
                {
                    "query_id": str(item.get("query_id") or ""),
                    "query_class": str(item.get("query_class") or ""),
                    "query_spec_hash": str((item.get("spec") or {}).get("query_spec_hash") or ""),
                    "protocol_version": str((item.get("spec") or {}).get("protocol_version") or ""),
                    "sources": list((item.get("spec") or {}).get("sources") or []),
                    "handoff_schema_version": str((item.get("handoff") or {}).get("schema_version") or ""),
                }
                for item in list(executed_specs or [])
            ],
            "authority_boundary": {
                "target_profile_owner": "genesis",
                "literature_query_spec_owner": "mica.literature_consolidation.contracts.query_protocol.LiteratureQuerySpec",
                "provider_routing_owner": "mica.literature_consolidation.provider_compiler.LiteratureProviderCompiler",
            },
            "created_at": utcnow_iso(),
        }

    def _semantic_candidates(
        self,
        *,
        accepted_same_target_candidates: Sequence[GenesisEvidenceCandidate],
        accepted_homolog_candidates: Sequence[GenesisEvidenceCandidate],
        ranked_candidates: Sequence[GenesisEvidenceCandidate],
    ) -> List[GenesisEvidenceCandidate]:
        primary = list(accepted_same_target_candidates or []) or list(accepted_homolog_candidates or []) or list(ranked_candidates or [])
        return list(primary)[: self.max_semantic_candidates]

    def _build_dlm_evidence_receipt(
        self,
        *,
        target_profile: GenesisTargetProfile,
        candidates: Sequence[GenesisEvidenceCandidate],
    ) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        if not self.enable_semantic_pipeline:
            return (
                {
                    "receipt_type": "dlm_evidence_extraction_receipt_v1",
                    "status": "unavailable",
                    "reason": "semantic_pipeline_disabled",
                    "documents_processed": 0,
                    "created_at": utcnow_iso(),
                },
                {},
            )
        if not candidates:
            return (
                {
                    "receipt_type": "dlm_evidence_extraction_receipt_v1",
                    "status": "unavailable",
                    "reason": "no_candidates_for_semantic_encoding",
                    "documents_processed": 0,
                    "created_at": utcnow_iso(),
                },
                {},
            )

        encoder = self._dlm_encoder or DLMEncoder()
        mapper = self._entity_mapper or EntityMapper(enable_api=False)
        bundle = build_genesis_target_identity_bundle(target_profile)
        candidate_summaries: List[Dict[str, Any]] = []
        candidate_index: Dict[str, Dict[str, Any]] = {}
        mapped_entities_total = 0

        for candidate in list(candidates or []):
            document_text = _candidate_document_text(candidate)
            encoded = encoder.encode(document_text)
            typed_mentions: List[tuple[str, str]] = []
            for entity in list(encoded.entities or []):
                mapper_type = _normalize_entity_mapper_type(str(entity.get("entity_type") or entity.get("type") or ""))
                mention_text = str(entity.get("text") or "").strip()
                if mapper_type and mention_text:
                    typed_mentions.append((mention_text, mapper_type))
            typed_mentions = list(dict.fromkeys(typed_mentions))
            mappings = mapper.map_batch(typed_mentions) if typed_mentions else []
            mapped_entities_total += sum(1 for item in mappings if getattr(item, "kb_id", None))

            semantic_entities = []
            for entity in list(encoded.entities or [])[:20]:
                semantic_entities.append(
                    {
                        "text": str(entity.get("text") or ""),
                        "entity_type": str(entity.get("entity_type") or entity.get("type") or ""),
                        "semantic_receipt": dict(entity.get("semantic_receipt") or {}),
                    }
                )
            matched_target_entities = _dedupe_strings(
                [
                    str(entity.get("text") or "")
                    for entity in list(encoded.entities or [])
                    if str(entity.get("text") or "").strip()
                ]
            )
            matched_target_entities = _casefolded_exact_matches(
                matched_target_entities,
                [*list(bundle.exact_aliases or []), *list(bundle.loose_aliases or [])],
            )
            matched_organism_entities = _dedupe_strings(
                [
                    str(entity.get("text") or "")
                    for entity in list(encoded.entities or [])
                    if str(entity.get("text") or "").strip()
                ]
            )
            matched_organism_entities = _casefolded_exact_matches(
                matched_organism_entities,
                bundle.organism_aliases,
            )
            text_haystack = _text_haystack(document_text)
            target_term_hits = _dedupe_strings(
                [
                    *_exact_term_matches(text_haystack, bundle.exact_aliases),
                    *_term_matches(text_haystack, bundle.loose_aliases),
                ]
            )
            organism_hits = _dedupe_strings(_term_matches(text_haystack, bundle.organism_aliases))
            summary = {
                "candidate_id": candidate.candidate_id,
                "document_id": f"genesis-dlm-{candidate.candidate_id}",
                "document_encoded": True,
                "dlm_metadata": encoder.to_metadata(encoded),
                "entity_count": len(list(encoded.entities or [])),
                "semantic_entities": semantic_entities,
                "target_term_hits": target_term_hits,
                "organism_term_hits": organism_hits,
                "matched_target_entities": matched_target_entities,
                "matched_organism_entities": matched_organism_entities,
                "mapped_entity_count": sum(1 for item in mappings if getattr(item, "kb_id", None)),
                "mapped_entities": [
                    {
                        "text": item.text,
                        "entity_type": item.entity_type,
                        "kb_id": item.kb_id,
                        "kb_source": item.kb_source,
                        "confidence": item.confidence,
                    }
                    for item in list(mappings or [])[:20]
                ],
            }
            candidate_summaries.append(summary)
            candidate_index[candidate.candidate_id] = summary

        receipt = {
            "receipt_type": "dlm_evidence_extraction_receipt_v1",
            "status": "completed",
            "documents_processed": len(candidate_summaries),
            "encoder": "mica.memory.dlm.encoder.DLMEncoder",
            "entity_mapper": "mica.memory.dlm.entity_mapper.EntityMapper",
            "mapped_entity_total": mapped_entities_total,
            "candidate_documents": candidate_summaries,
            "created_at": utcnow_iso(),
        }
        return receipt, candidate_index

    async def _build_atom_evidence_graph_receipt(
        self,
        *,
        candidates: Sequence[GenesisEvidenceCandidate],
    ) -> Dict[str, Any]:
        if not self.enable_semantic_pipeline:
            return {
                "receipt_type": "atom_evidence_graph_receipt_v1",
                "status": "unavailable",
                "reason": "semantic_pipeline_disabled",
                "documents_processed": 0,
                "created_at": utcnow_iso(),
            }
        if not candidates:
            return {
                "receipt_type": "atom_evidence_graph_receipt_v1",
                "status": "unavailable",
                "reason": "no_candidates_for_atom_projection",
                "documents_processed": 0,
                "created_at": utcnow_iso(),
            }

        encoder = self._dlm_encoder or DLMEncoder()
        decomposer = self._atom_decomposer or ATOMAtomicDecomposer(
            chunk_size=200,
            llm_call=None,
            max_concurrency=1,
            min_chunk_tokens=25,
            max_chunk_tokens=120,
            max_prompt_tokens=512,
            prompt_overhead_tokens=64,
            max_chunks_per_document=6,
            max_inflight_chunks=1,
            encoder=encoder,
            fallback_min_chars=10,
            fallback_min_alpha=0.2,
        )
        extractor = self._atom_extractor or ATOMQuintupleExtractor(
            batch_size=8,
            llm_call=None,
            embedding_fn=_default_atom_embedding,
        )

        document_summaries: List[Dict[str, Any]] = []
        total_facts = 0
        total_quintuples = 0
        for candidate in list(candidates or []):
            document_text = _candidate_document_text(candidate)
            observation_time = datetime.utcnow()
            facts = await decomposer.decompose_to_atomic_facts(
                document=document_text,
                observation_time=observation_time,
                metadata={
                    "document_id": candidate.candidate_id,
                    "source_blob": candidate.candidate_id,
                    "provider": candidate.provider,
                },
            )
            snapshots = await extractor.extract_quintuples_parallel(facts)
            quintuples = [quintuple for snapshot in snapshots for quintuple in list(snapshot.quintuples or [])]
            total_facts += len(facts)
            total_quintuples += len(quintuples)
            trace = AtomicExperienceTrace(
                observation_time=observation_time,
                document_metadata={
                    "document_id": candidate.candidate_id,
                    "source_blob": candidate.candidate_id,
                    "provider": candidate.provider,
                },
                chunks=[dict(item) for item in list(decomposer.last_chunks or [])],
                atomic_facts=list(facts),
                quintuples=list(quintuples),
                snapshot_metadata={
                    "candidate_id": candidate.candidate_id,
                    "snapshot_count": len(snapshots),
                },
            )
            jsonld = experience_trace_to_jsonld(trace)
            document_summaries.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "jsonld_id": str(jsonld.get("@id") or ""),
                    "chunk_count": len(list(decomposer.last_chunks or [])),
                    "atomic_fact_count": len(facts),
                    "quintuple_count": len(quintuples),
                    "quintuple_preview": list(jsonld.get("quintuples") or [])[:5],
                }
            )

        return {
            "receipt_type": "atom_evidence_graph_receipt_v1",
            "status": "completed",
            "documents_processed": len(document_summaries),
            "decomposer": "mica.memory.atom.decomposer.ATOMAtomicDecomposer",
            "extractor": "mica.memory.atom.quintuple_extractor.ATOMQuintupleExtractor",
            "atom_jsonld_export": "mica.memory.atom.jsonld.experience_trace_to_jsonld",
            "fact_count": total_facts,
            "quintuple_count": total_quintuples,
            "documents": document_summaries,
            "created_at": utcnow_iso(),
        }

    def _build_evidence_decision(
        self,
        *,
        target_profile: GenesisTargetProfile,
        accepted_same_target_candidates: Sequence[GenesisEvidenceCandidate],
        accepted_homolog_candidates: Sequence[GenesisEvidenceCandidate],
        background_candidates: Sequence[GenesisEvidenceCandidate],
        precision_status: GenesisPrecisionStatus,
        dlm_receipt: Mapping[str, Any],
        atom_receipt: Mapping[str, Any],
    ) -> Dict[str, Any]:
        same_target_ids = [item.candidate_id for item in list(accepted_same_target_candidates or [])]
        homolog_ids = [item.candidate_id for item in list(accepted_homolog_candidates or [])]
        background_ids = [item.candidate_id for item in list(background_candidates or [])]
        return {
            "decision_type": "genesis_evidence_decision_v1",
            "decision_owner": "genesis",
            "target_profile_id": target_profile.target_profile_id,
            "precision_status": precision_status,
            "same_target_candidate_ids": same_target_ids,
            "homolog_candidate_ids": homolog_ids,
            "background_candidate_ids": background_ids,
            "dlm_documents_processed": int(dlm_receipt.get("documents_processed") or 0),
            "atom_documents_processed": int(atom_receipt.get("documents_processed") or 0),
            "atom_quintuple_count": int(atom_receipt.get("quintuple_count") or 0),
            "rationale": (
                "Genesis kept target-profile construction and scientific acceptance decisions, "
                "while document encoding/entity receipts came from DLM and fact/quintuple snapshots came from ATOM."
            ),
            "created_at": utcnow_iso(),
        }

    async def _receipt_from_provider_results(
        self,
        *,
        request: GenesisEvidenceRequest,
        target_profile: GenesisTargetProfile,
        target_identity_bundle: GenesisTargetIdentityBundle,
        query_bundle: Sequence[GenesisEvidenceQuery],
        executed_queries: Sequence[GenesisEvidenceQuery],
        provider_results: Sequence[Mapping[str, Any]],
        query_spec_summary: Mapping[str, Any],
        executed_specs: Sequence[Mapping[str, Any]],
    ) -> GenesisLiteratureEvidenceReceipt:
        provider_status: Dict[str, Any] = {}
        providers_attempted: List[str] = []
        blockers: List[str] = []
        total_papers = 0
        quorum_statuses: List[str] = []
        for result in list(provider_results or []):
            receipt = dict(result.get("provider_receipt") or {})
            quorum_statuses.append(str(receipt.get("status") or "blocked"))
            total_papers += int(result.get("paper_count") or 0)
            blockers.extend(list(receipt.get("blocked_reasons") or []))
            for item in list(receipt.get("provider_receipts") or []):
                provider = str(item.get("provider") or "").strip()
                if not provider:
                    continue
                if item.get("attempted"):
                    providers_attempted.append(provider)
                existing = dict(provider_status.get(provider) or {})
                merged = {
                    "provider": provider,
                    "attempted": bool(existing.get("attempted")) or bool(item.get("attempted")),
                    "paper_count": int(existing.get("paper_count") or 0) + int(item.get("paper_count") or 0),
                    "failure_count": int(existing.get("failure_count") or 0) + int(item.get("failure_count") or 0),
                    "failure_reasons": _dedupe_strings([*list(existing.get("failure_reasons") or []), *list(item.get("failure_reasons") or [])]),
                    "http_statuses": list(dict.fromkeys([*list(existing.get("http_statuses") or []), *list(item.get("http_statuses") or [])])),
                    "degraded": bool(existing.get("degraded")) or bool(item.get("degraded")),
                    "query_ids": _dedupe_strings([*list(existing.get("query_ids") or []), str(result.get("query_id") or "")]),
                }
                candidate_statuses = [str(existing.get("status") or ""), str(item.get("status") or "")]
                if any(status in {"failed", "unavailable"} for status in candidate_statuses):
                    merged["status"] = "failed"
                elif any(status.startswith("degraded") or status == "degraded" for status in candidate_statuses):
                    merged["status"] = "degraded"
                elif any(status == "success" for status in candidate_statuses):
                    merged["status"] = "success"
                else:
                    merged["status"] = next((status for status in reversed(candidate_statuses) if status), "not_attempted")
                provider_status[provider] = merged

        quorum_status = "blocked"
        if any(status == "satisfied" for status in quorum_statuses):
            quorum_status = "satisfied"
        elif total_papers > 0 or any(status == "degraded" for status in quorum_statuses):
            quorum_status = "degraded"
        provider_quorum_satisfied = quorum_status != "blocked"
        evidence_grade: GenesisEvidenceGrade = (
            "live_quorum" if quorum_status == "satisfied"
            else "live_degraded" if quorum_status == "degraded"
            else "blocked_missing_live_evidence"
        )

        deduplicated_candidates, materialization_rejections, saw_valid_materialization = materialize_genesis_citation_candidates(
            target_profile,
            query_bundle=query_bundle,
            provider_results=provider_results,
        )
        ranked_candidates = score_genesis_evidence_candidates(target_profile, candidates=deduplicated_candidates)

        dlm_receipt, dlm_candidate_index = self._build_dlm_evidence_receipt(
            target_profile=target_profile,
            candidates=list(ranked_candidates or [])[: self.max_semantic_candidates],
        )
        for candidate in list(ranked_candidates or []):
            semantic_support = dict(dlm_candidate_index.get(candidate.candidate_id) or {})
            if semantic_support:
                candidate.metadata["semantic_support"] = semantic_support
                if semantic_support.get("matched_target_entities"):
                    candidate.identity_match_basis = _dedupe_strings(
                        [*list(candidate.identity_match_basis or []), "dlm_entity_target_match"]
                    )
                if semantic_support.get("matched_organism_entities"):
                    candidate.identity_match_basis = _dedupe_strings(
                        [*list(candidate.identity_match_basis or []), "dlm_entity_organism_match"]
                    )

        accepted_candidates: List[GenesisEvidenceCandidate] = []
        accepted_same_target_candidates: List[GenesisEvidenceCandidate] = []
        accepted_homolog_candidates: List[GenesisEvidenceCandidate] = []
        background_candidates: List[GenesisEvidenceCandidate] = []
        rejected_candidates: List[Dict[str, Any]] = list(materialization_rejections)
        for candidate in ranked_candidates:
            stable_ref = _extract_stable_identifier(candidate.model_dump())
            semantic_support = dict((candidate.metadata or {}).get("semantic_support") or {})
            strong_same_target_identity = bool(
                "accession_match" in list(candidate.identity_match_basis or [])
                or "exact_alias_plus_organism" in list(candidate.identity_match_basis or [])
                or (
                    semantic_support.get("matched_target_entities")
                    and (
                        semantic_support.get("matched_organism_entities")
                        or semantic_support.get("organism_term_hits")
                    )
                )
            )
            same_target_threshold = 1.0 if strong_same_target_identity else 2.0
            semantic_same_target_rescue = bool(
                strong_same_target_identity
                and candidate.final_relevance_score >= 2.0
                and (
                    semantic_support.get("matched_target_entities")
                    or "exact_alias_plus_organism" in list(candidate.identity_match_basis or [])
                )
            )
            if (
                (
                    (
                        candidate.evidence_tier in {"same_target_direct", "same_target_functional"}
                        and candidate.final_relevance_score >= same_target_threshold
                    )
                    or semantic_same_target_rescue
                )
                and stable_ref
                and candidate.identity_match_basis
            ):
                candidate.reason_for_acceptance = (
                    "Accepted same-target evidence because provider-backed fields matched "
                    + ", ".join(candidate.identity_match_basis)
                    + "."
                )
                accepted_candidates.append(candidate)
                accepted_same_target_candidates.append(candidate)
                continue
            if (
                candidate.evidence_tier in {"homolog_direct", "homolog_mechanistic"}
                and stable_ref
                and candidate.final_relevance_score >= 1.0
                and (candidate.matched_function_terms or candidate.matched_mechanism_terms or candidate.matched_organism_terms)
            ):
                accepted_homolog_candidates.append(candidate)
                continue
            if candidate.evidence_tier in {"family_background", "broad_context_only"}:
                background_candidates.append(candidate)
            rejection_reason = candidate.rejection_reason
            if not rejection_reason:
                if candidate.evidence_tier in {"family_background", "broad_context_only"}:
                    rejection_reason = "broad_context_only_not_counted_as_target_evidence"
                elif candidate.relevance_label == "irrelevant":
                    rejection_reason = "target_profile_not_supported"
                else:
                    rejection_reason = "insufficient_target_specific_support"
            rejected_candidates.append({**candidate.model_dump(), "rejection_reason": rejection_reason})

        homolog_query_count = len({query_id for item in accepted_homolog_candidates for query_id in item.query_ids})
        homolog_provider_count = len({provider for item in accepted_homolog_candidates for provider in item.provider_support})
        homolog_supported = len(accepted_homolog_candidates) >= 2 and (homolog_query_count >= 2 or homolog_provider_count >= 2)
        if homolog_supported:
            for candidate in accepted_homolog_candidates:
                candidate.reason_for_acceptance = "Accepted as homolog evidence only; same-target identity was not proven."
        else:
            for candidate in accepted_homolog_candidates:
                rejected_candidates.append(
                    {
                        **candidate.model_dump(),
                        "rejection_reason": "homolog_support_not_independent_enough",
                    }
                )
            accepted_homolog_candidates = []

        semantic_candidates = self._semantic_candidates(
            accepted_same_target_candidates=accepted_same_target_candidates,
            accepted_homolog_candidates=accepted_homolog_candidates,
            ranked_candidates=ranked_candidates,
        )
        atom_receipt = await self._build_atom_evidence_graph_receipt(candidates=semantic_candidates)

        target_precision_satisfied = bool(accepted_same_target_candidates)
        citation_materialization_satisfied = saw_valid_materialization if total_papers > 0 else False
        evidence_quality_satisfied = bool(
            (accepted_same_target_candidates or accepted_homolog_candidates)
            and all(item.reason_for_acceptance and item.score_breakdown for item in [*accepted_same_target_candidates, *accepted_homolog_candidates])
            and dlm_receipt.get("status") == "completed"
        )
        if not provider_quorum_satisfied:
            precision_status: GenesisPrecisionStatus = "precision_failed_provider_quorum"
        elif total_papers > 0 and not citation_materialization_satisfied:
            precision_status = "precision_failed_materialization"
        elif accepted_same_target_candidates:
            precision_status = "precision_same_target_passed"
        elif accepted_homolog_candidates:
            precision_status = "precision_homolog_supported"
        elif any(item.evidence_tier in {"homolog_direct", "homolog_mechanistic", "family_background", "broad_context_only", "ambiguous"} for item in ranked_candidates):
            precision_status = "precision_partial_background_only"
        elif ranked_candidates:
            precision_status = "precision_failed_no_target_evidence"
        else:
            precision_status = "precision_failed_no_target_evidence"

        status_map: Dict[str, str] = {
            "precision_same_target_passed": "live_quorum_satisfied_precision_passed",
            "precision_homolog_supported": "live_quorum_satisfied_precision_partial",
            "precision_partial_background_only": "live_quorum_satisfied_precision_partial",
            "precision_failed_no_target_evidence": "no_target_relevant_evidence_found",
            "precision_failed_provider_quorum": "provider_quorum_failed",
            "precision_failed_materialization": "citation_materialization_failed",
        }
        status = status_map[precision_status]

        limitations: List[str] = []
        if target_identity_bundle.metadata_resolution_status != "resolved":
            limitations.append("metadata_resolution_partial")
        if not target_profile.uniprot_accessions:
            limitations.append("target_profile_missing_accession_conditioning")
        if not target_profile.ec_numbers:
            limitations.append("target_profile_missing_ec_conditioning")
        if not accepted_same_target_candidates:
            limitations.append("no_accepted_target_specific_evidence")
        if total_papers == 0 and "no_live_papers_returned" not in blockers:
            blockers.append("no_live_papers_returned")
        if precision_status in {"precision_partial_background_only", "precision_failed_no_target_evidence"}:
            blockers.append("target_precision_not_satisfied")
        literature_query_adapter = self._build_literature_query_adapter_receipt(
            target_profile=target_profile,
            executed_queries=executed_queries,
            executed_specs=executed_specs,
        )
        evidence_decision = self._build_evidence_decision(
            target_profile=target_profile,
            accepted_same_target_candidates=accepted_same_target_candidates,
            accepted_homolog_candidates=accepted_homolog_candidates,
            background_candidates=background_candidates,
            precision_status=precision_status,
            dlm_receipt=dlm_receipt,
            atom_receipt=atom_receipt,
        )

        return GenesisLiteratureEvidenceReceipt(
            target_name=request.target_name,
            target_type=request.target_type,
            status=status,
            queries=[item.query_text for item in executed_queries],
            providers_attempted=_dedupe_strings(providers_attempted),
            provider_status=provider_status,
            citation_refs=self._citation_refs_from_candidates(accepted_same_target_candidates or accepted_homolog_candidates or ranked_candidates),
            blockers=_dedupe_strings(blockers),
            evidence_grade=evidence_grade,
            claim_boundary="local_non_production",
            paper_count=len(deduplicated_candidates),
            query_spec=dict(query_spec_summary),
            target_profile=target_profile.model_dump(),
            target_identity_bundle=target_identity_bundle.model_dump(),
            query_bundle=[item.model_dump() for item in query_bundle],
            provider_results=[dict(item) for item in provider_results],
            deduplicated_candidates=[item.model_dump() for item in deduplicated_candidates],
            ranked_evidence=[item.model_dump() for item in ranked_candidates],
            authority_boundary=self._authority_boundary(),
            literature_query_adapter=literature_query_adapter,
            dlm_evidence_receipt=dlm_receipt,
            atom_evidence_graph_receipt=atom_receipt,
            evidence_decision=evidence_decision,
            accepted_evidence=[item.model_dump() for item in [*accepted_same_target_candidates, *accepted_homolog_candidates]],
            accepted_same_target_evidence=[item.model_dump() for item in accepted_same_target_candidates],
            accepted_homolog_evidence=[item.model_dump() for item in accepted_homolog_candidates],
            background_evidence=[item.model_dump() for item in background_candidates],
            rejected_candidates=rejected_candidates,
            quorum_status=quorum_status,
            precision_status=precision_status,
            provider_quorum_satisfied=provider_quorum_satisfied,
            target_precision_satisfied=target_precision_satisfied,
            citation_materialization_satisfied=citation_materialization_satisfied,
            evidence_quality_satisfied=evidence_quality_satisfied,
            limitations=limitations,
            artifact_policy=dict(request.output_policy),
        )

    def _citation_refs_from_candidates(self, candidates: Sequence[GenesisEvidenceCandidate]) -> List[Dict[str, Any]]:
        citation_refs: List[Dict[str, Any]] = []
        for candidate in list(candidates or [])[:10]:
            citation_refs.append(
                {
                    "title": candidate.title,
                    "doi": candidate.doi,
                    "pmid": candidate.pmid,
                    "source": candidate.provider,
                    "canonical_id": candidate.canonical_id or "",
                    "provider_record_url": candidate.provider_record_url,
                    "query_ids": list(candidate.query_ids),
                    "target_fields_matched": list(candidate.target_fields_matched),
                    "matched_target_terms": list(candidate.matched_target_terms),
                    "matched_organism_terms": list(candidate.matched_organism_terms),
                    "matched_function_terms": list(candidate.matched_function_terms),
                    "matched_mechanism_terms": list(candidate.matched_mechanism_terms),
                    "identity_match_basis": list(candidate.identity_match_basis),
                    "evidence_tier": candidate.evidence_tier,
                    "score_breakdown": dict(candidate.score_breakdown),
                    "relevance_label": candidate.relevance_label,
                }
            )
        return citation_refs


def build_genesis_dataset_request_schema() -> Dict[str, Any]:
    return GenesisDatasetRequest.model_json_schema()


def build_genesis_dataset_manifest_schema() -> Dict[str, Any]:
    return GenesisDatasetManifest.model_json_schema()


def build_genesis_literature_evidence_receipt_schema() -> Dict[str, Any]:
    return GenesisLiteratureEvidenceReceipt.model_json_schema()


def build_genesis_dataset_literature_workflow_jsonld(
    *,
    dataset_request: GenesisDatasetRequest,
    dataset_manifest: GenesisDatasetManifest,
    dataset_receipt: GenesisDatasetBuilderReceipt,
    evidence_request: GenesisEvidenceRequest,
    evidence_receipt: GenesisLiteratureEvidenceReceipt,
) -> Dict[str, Any]:
    evidence_required = bool(dataset_request.literature_required)
    evidence_ok = evidence_receipt.precision_status in {
        "precision_same_target_passed",
    } or evidence_receipt.status in {"live_quorum_satisfied"}
    gate_status = "passed" if (not evidence_required or evidence_ok) and dataset_manifest.status != "blocked" else "blocked"
    gate_blockers: List[str] = []
    if dataset_manifest.blocker is not None:
        gate_blockers.append(dataset_manifest.blocker.code)
    if evidence_required and not evidence_ok:
        gate_blockers.extend(list(evidence_receipt.blockers or []) or [evidence_receipt.status])
    return {
        "@context": {
            "@vocab": "https://mica.local/genesis/protocol#",
            "node_id": "@id",
        },
        "@id": f"urn:genesis:workflow:{dataset_manifest.dataset_id}",
        "@type": "GenesisDatasetLiteratureWorkflow",
        "workflow_id": f"genesis_dataset_literature_workflow_v1:{dataset_manifest.dataset_id}",
        "parent_graph_id": dataset_request.request_id,
        "graph_level": "workflow",
        "campaign_id": dataset_request.request_id,
        "status": gate_status,
        "claim_boundary": dataset_manifest.claim_boundary,
        "artifacts": [
            {
                "kind": "dataset_manifest",
                "dataset_id": dataset_manifest.dataset_id,
                "sha256": dataset_manifest.sha256,
                "local_path": dataset_manifest.local_path,
                "gcs_uri": dataset_manifest.gcs_uri,
            },
            {
                "kind": "literature_evidence_receipt",
                "target_name": evidence_request.target_name,
                "paper_count": evidence_receipt.paper_count,
            },
        ],
        "receipts": {
            "dataset_builder": dataset_receipt.model_dump(),
            "literature_evidence": evidence_receipt.model_dump(),
        },
        "nodes": [
            {
                "node_id": "parse_design_intent",
                "node_type": "design_intent",
                "node_kind": "tool",
                "phase_id": "design_intent",
                "semantic_group": "decision",
                "collapsed_by_default": False,
                "intent_summary": "Normalize the Genesis target request before evidence routing.",
                "status": "completed",
                "outputs": {
                    "request_id": dataset_request.request_id,
                    "target_name": dataset_request.target_name,
                    "target_type": dataset_request.target_type,
                },
            },
            {
                "node_id": "lmp_dataset_plan",
                "node_type": "dataset_builder",
                "node_kind": "tool",
                "phase_id": "dataset",
                "semantic_group": "dataset",
                "collapsed_by_default": False,
                "intent_summary": "Resolve dataset context from canonical LMP/Nesymol surfaces.",
                "status": dataset_receipt.status,
                "outputs": {
                    "dataset_id": dataset_manifest.dataset_id,
                    "record_count": dataset_manifest.record_count,
                    "blocker": dataset_manifest.blocker.model_dump() if dataset_manifest.blocker else None,
                    "source_refs": dataset_manifest.source_refs,
                },
            },
            {
                "node_id": "bibliotecario_evidence_query",
                "node_type": "literature_evidence",
                "node_kind": "tool",
                "phase_id": "literature",
                "semantic_group": "literature",
                "collapsed_by_default": False,
                "intent_summary": "Route target-conditioned literature acquisition through Literature Consolidation.",
                "status": evidence_receipt.status,
                "outputs": {
                    "queries": list(evidence_receipt.queries),
                    "paper_count": evidence_receipt.paper_count,
                    "evidence_grade": evidence_receipt.evidence_grade,
                    "precision_status": evidence_receipt.precision_status,
                    "blockers": list(evidence_receipt.blockers),
                    "query_adapter": dict(evidence_receipt.literature_query_adapter or {}),
                },
            },
            {
                "node_id": "dlm_evidence_extraction",
                "node_type": "dlm_evidence",
                "node_kind": "tool",
                "phase_id": "dlm",
                "semantic_group": "dlm",
                "collapsed_by_default": True,
                "intent_summary": "Encode document/abstract evidence through DLM and preserve semantic entity receipts.",
                "status": str(evidence_receipt.dlm_evidence_receipt.get("status") or "unavailable"),
                "outputs": dict(evidence_receipt.dlm_evidence_receipt or {}),
            },
            {
                "node_id": "atom_evidence_graph",
                "node_type": "atom_evidence_graph",
                "node_kind": "tool",
                "phase_id": "atom",
                "semantic_group": "analysis",
                "collapsed_by_default": True,
                "intent_summary": "Project DLM-conditioned statements into ATOM fact and quintuple summaries.",
                "status": str(evidence_receipt.atom_evidence_graph_receipt.get("status") or "unavailable"),
                "outputs": dict(evidence_receipt.atom_evidence_graph_receipt or {}),
            },
            {
                "node_id": "model_decision_context",
                "node_type": "model_decision_context",
                "node_kind": "tool",
                "phase_id": "model",
                "semantic_group": "model",
                "collapsed_by_default": True,
                "intent_summary": "Reserve explicit model-decision context without moving generic model inference into this literature slice.",
                "status": "not_required",
                "outputs": {
                    "status": "not_required",
                    "reason": "GENESIS_DLM_LITERATURE_GOG_EVIDENCE_ALIGNMENT_V1 is evidence-authority-only.",
                },
            },
            {
                "node_id": "genesis_evidence_decision",
                "node_type": "evidence_decision",
                "node_kind": "tool",
                "phase_id": "decision",
                "semantic_group": "decision",
                "collapsed_by_default": False,
                "intent_summary": "Keep the final scientific acceptance decision in Genesis while citing Literature/DLM/ATOM receipts.",
                "status": evidence_receipt.status,
                "outputs": dict(evidence_receipt.evidence_decision or {}),
            },
            {
                "node_id": "dataset_manifest_emit",
                "node_type": "artifact_emit",
                "node_kind": "tool",
                "phase_id": "artifacts",
                "semantic_group": "dataset",
                "collapsed_by_default": True,
                "intent_summary": "Emit the dataset manifest artifact bound to the workflow receipt.",
                "status": "completed" if dataset_manifest.record_count > 0 else "blocked",
                "outputs": {
                    "sha256": dataset_manifest.sha256,
                    "claim_boundary": dataset_manifest.claim_boundary,
                },
            },
            {
                "node_id": "evidencegate_close",
                "node_type": "evidence_gate",
                "node_kind": "tool",
                "phase_id": "gate",
                "semantic_group": "decision",
                "collapsed_by_default": False,
                "intent_summary": "Fail closed when dataset or evidence authority requirements are not satisfied.",
                "status": gate_status,
                "outputs": {
                    "blockers": gate_blockers,
                    "literature_required": evidence_required,
                    "no_silent_success_check": True,
                },
            },
        ],
        "edges": [
            {"source_node_id": "parse_design_intent", "target_node_id": "lmp_dataset_plan", "edge_type": "control_dependency"},
            {"source_node_id": "parse_design_intent", "target_node_id": "bibliotecario_evidence_query", "edge_type": "control_dependency"},
            {"source_node_id": "bibliotecario_evidence_query", "target_node_id": "dlm_evidence_extraction", "edge_type": "control_dependency"},
            {"source_node_id": "dlm_evidence_extraction", "target_node_id": "atom_evidence_graph", "edge_type": "control_dependency"},
            {"source_node_id": "atom_evidence_graph", "target_node_id": "genesis_evidence_decision", "edge_type": "control_dependency"},
            {"source_node_id": "lmp_dataset_plan", "target_node_id": "genesis_evidence_decision", "edge_type": "control_dependency"},
            {"source_node_id": "model_decision_context", "target_node_id": "genesis_evidence_decision", "edge_type": "control_dependency"},
            {"source_node_id": "genesis_evidence_decision", "target_node_id": "evidencegate_close", "edge_type": "control_dependency"},
            {"source_node_id": "dataset_manifest_emit", "target_node_id": "evidencegate_close", "edge_type": "control_dependency"},
        ],
    }

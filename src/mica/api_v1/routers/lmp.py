"""
LMP v4 Presets & Generation API
--------------------------------
Exposes all 9 registered NeSyMol presets and a generation endpoint.

Endpoints:
  GET  /api/v1/lmp/presets               – list all presets + their block flags
  GET  /api/v1/lmp/preset/{name}         – detail for a single preset
  GET  /api/v1/lmp/consumer/{consumer}   – recommended preset for a consumer type
  POST /api/v1/lmp/generate              – trigger async LMP generation
  GET  /api/v1/lmp/generate/{job_id}     – poll generation job status
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import unquote, urlparse
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from mica.api_v1.auth import user_dependency

logger = logging.getLogger(__name__)

_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in {"prod", "production"}

# ---------------------------------------------------------------------------
# Preset imports (tolerant — API still boots without bsm.lmp installed)
# ---------------------------------------------------------------------------
_PRESETS_AVAILABLE = False
_PRESET_REGISTRY: Dict = {}
_get_preset = None
_list_presets = None
_preset_for_consumer = None

try:
    from bsm.lmp.presets import (  # type: ignore
        PRESET_REGISTRY as _PRESET_REGISTRY,
        get_preset as _get_preset,
        list_presets as _list_presets,
        preset_for_consumer as _preset_for_consumer,
        LMPPreset,
    )
    _PRESETS_AVAILABLE = True
except Exception as _e:
    _PRESETS_LOAD_ERROR = str(_e)
else:
    _PRESETS_LOAD_ERROR = None


# ---------------------------------------------------------------------------
# In-memory job store (for async generation jobs — fallback)
# ---------------------------------------------------------------------------
_JOBS: Dict[str, dict] = {}  # job_id -> {status, result, error, started_at, finished_at}

# ---------------------------------------------------------------------------
# RedisJobStore singleton (lazy — graceful fallback if Redis unavailable)
# ---------------------------------------------------------------------------

_job_store_instance = None


async def _get_job_store():
    """Return a shared RedisJobStore, or None if Redis is unreachable."""
    global _job_store_instance
    if _job_store_instance is not None:
        return _job_store_instance
    try:
        from mica.infrastructure.redis_client import get_redis
        from mica.worker.job_store import RedisJobStore

        redis_client = await get_redis()
        _job_store_instance = RedisJobStore(redis_client)
        logger.info("LMP RedisJobStore initialised")
        return _job_store_instance
    except Exception as exc:
        logger.warning("RedisJobStore unavailable: %s", exc)
        return None


def _production_queue_required(route_name: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"{route_name} requires Redis-backed worker execution in production; in-process fallback is disabled.",
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1/lmp", tags=["lmp"], dependencies=[Depends(user_dependency)])

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PresetInfo(BaseModel):
    name: str
    description: str
    include_identity: bool
    include_nesy_grammar: bool
    include_semantics: bool
    include_geometry: bool
    include_features: bool
    include_knowledge_graph: bool
    include_trajectory_ifp: bool
    include_provenance: bool
    embed_ground_truth: bool
    max_ifp_frames: int
    ifp_stride: int
    ifp_min_occupancy: float
    ifp_auto_ligand: bool
    ifp_auto_chain: bool
    ifp_detect_metals: bool
    blocks: List[str]   # human-readable list of enabled blocks
    consumer_aliases: List[str]  # shorthand aliases that resolve to this preset


# Hard-coded consumer alias map mirrors presets.py preset_for_consumer
_CONSUMER_MAP: Dict[str, str] = {
    "plm": "nesy-core",
    "esm2": "plm-esm2", "esm-2": "plm-esm2",
    "prott5": "plm-prott5", "prot-t5": "plm-prott5", "protbert": "nesy-core",
    "llm": "semantic", "gpt": "llm-context", "claude": "llm-context", "gemini": "llm-context",
    "md": "md-ifp", "trajectory": "md-ifp", "ifp": "md-ifp",
    "struct": "structural", "pdb": "structural", "structure": "structural",
    "full": "full", "archive": "full", "all": "full",
}

# Reverse map: preset_name -> [aliases...]
_REVERSE_CONSUMER: Dict[str, List[str]] = {}
for _alias, _pname in _CONSUMER_MAP.items():
    _REVERSE_CONSUMER.setdefault(_pname, []).append(_alias)


def _preset_to_info(name: str) -> PresetInfo:
    """Convert an LMPPreset to PresetInfo. Falls back to static data if bsm.lmp unavailable."""
    if _PRESETS_AVAILABLE and _get_preset:
        p = _get_preset(name)
        blocks = []
        if p.include_identity:           blocks.append("Identity")
        if p.include_nesy_grammar:       blocks.append("NeSyGrammar")
        if p.include_semantics:          blocks.append("Semantics")
        if p.include_geometry:           blocks.append("Geometry")
        if p.include_features:           blocks.append("Features")
        if p.include_knowledge_graph:    blocks.append("KnowledgeGraph")
        if p.include_trajectory_ifp:     blocks.append("TrajectoryIFP")
        if p.include_provenance:         blocks.append("Provenance")
        if p.embed_ground_truth:         blocks.append("GroundTruth")
        return PresetInfo(
            name=p.name,
            description=p.description,
            include_identity=p.include_identity,
            include_nesy_grammar=p.include_nesy_grammar,
            include_semantics=p.include_semantics,
            include_geometry=p.include_geometry,
            include_features=p.include_features,
            include_knowledge_graph=p.include_knowledge_graph,
            include_trajectory_ifp=p.include_trajectory_ifp,
            include_provenance=p.include_provenance,
            embed_ground_truth=p.embed_ground_truth,
            max_ifp_frames=p.max_ifp_frames,
            ifp_stride=p.ifp_stride,
            ifp_min_occupancy=p.ifp_min_occupancy,
            ifp_auto_ligand=p.ifp_auto_ligand,
            ifp_auto_chain=p.ifp_auto_chain,
            ifp_detect_metals=p.ifp_detect_metals,
            blocks=blocks,
            consumer_aliases=_REVERSE_CONSUMER.get(name, []),
        )
    # Fallback static data when bsm.lmp is not on sys.path
    return _STATIC_PRESETS.get(name) or PresetInfo(
        name=name, description="(preset data unavailable)",
        include_identity=True, include_nesy_grammar=False, include_semantics=False,
        include_geometry=False, include_features=False, include_knowledge_graph=False,
        include_trajectory_ifp=False, include_provenance=True, embed_ground_truth=False,
        max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1,
        ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False,
        blocks=[], consumer_aliases=_REVERSE_CONSUMER.get(name, []),
    )


# Static fallback preset definitions (used when bsm.lmp not on pytest path)
_STATIC_PRESETS: Dict[str, PresetInfo] = {
    "nesy-core":   PresetInfo(name="nesy-core",   description="Minimal: Identity + NeSy grammar for PLM tokenization",                    include_identity=True, include_nesy_grammar=True,  include_semantics=True,  include_geometry=False, include_features=False, include_knowledge_graph=False, include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","NeSyGrammar","Semantics","Provenance"], consumer_aliases=["plm","protbert"]),
    "semantic":    PresetInfo(name="semantic",     description="Semantic context for LLM injection (keywords, comments, xrefs)",           include_identity=True, include_nesy_grammar=False, include_semantics=True,  include_geometry=False, include_features=False, include_knowledge_graph=True,  include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","Semantics","KnowledgeGraph","Provenance"], consumer_aliases=["llm"]),
    "structural":  PresetInfo(name="structural",   description="Geometry + features for structural analysis (PDB-focused)",               include_identity=True, include_nesy_grammar=False, include_semantics=False, include_geometry=True,  include_features=True,  include_knowledge_graph=False, include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","Geometry","Features","Provenance"], consumer_aliases=["struct","pdb","structure"]),
    "v2-compat":   PresetInfo(name="v2-compat",    description="Back-compat preset approximating v2 outputs (no TrajectoryIFP)",          include_identity=True, include_nesy_grammar=True,  include_semantics=True,  include_geometry=True,  include_features=True,  include_knowledge_graph=False, include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","NeSyGrammar","Semantics","Geometry","Features","Provenance"], consumer_aliases=[]),
    "md-ifp":      PresetInfo(name="md-ifp",       description="MD trajectory IFP fingerprints for dynamics analysis",                    include_identity=True, include_nesy_grammar=False, include_semantics=False, include_geometry=True,  include_features=False, include_knowledge_graph=False, include_trajectory_ifp=True,  include_provenance=True,  embed_ground_truth=False, max_ifp_frames=1000, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","Geometry","TrajectoryIFP","Provenance"], consumer_aliases=["md","trajectory","ifp"]),
    "full":        PresetInfo(name="full",          description="Complete archive with all blocks (master format)",                        include_identity=True, include_nesy_grammar=True,  include_semantics=True,  include_geometry=True,  include_features=True,  include_knowledge_graph=True,  include_trajectory_ifp=True,  include_provenance=True,  embed_ground_truth=True,  max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","NeSyGrammar","Semantics","Geometry","Features","KnowledgeGraph","TrajectoryIFP","Provenance","GroundTruth"], consumer_aliases=["full","archive","all"]),
    "plm-esm2":    PresetInfo(name="plm-esm2",     description="Optimized for ESM-2 fine-tuning (sequence + per-residue labels)",         include_identity=True, include_nesy_grammar=True,  include_semantics=True,  include_geometry=False, include_features=True,  include_knowledge_graph=False, include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","NeSyGrammar","Semantics","Features","Provenance"], consumer_aliases=["esm2","esm-2"]),
    "plm-prott5":  PresetInfo(name="plm-prott5",   description="Optimized for ProtT5 fine-tuning",                                        include_identity=True, include_nesy_grammar=True,  include_semantics=True,  include_geometry=False, include_features=True,  include_knowledge_graph=False, include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","NeSyGrammar","Semantics","Features","Provenance"], consumer_aliases=["prott5","prot-t5"]),
    "llm-context": PresetInfo(name="llm-context",  description="Rich context for LLM prompts (no raw data)",                             include_identity=True, include_nesy_grammar=False, include_semantics=True,  include_geometry=False, include_features=False, include_knowledge_graph=True,  include_trajectory_ifp=False, include_provenance=True,  embed_ground_truth=False, max_ifp_frames=500, ifp_stride=1, ifp_min_occupancy=0.1, ifp_auto_ligand=True, ifp_auto_chain=True, ifp_detect_metals=False, blocks=["Identity","Semantics","KnowledgeGraph","Provenance"], consumer_aliases=["gpt","claude","gemini"]),
}

_ALL_PRESET_NAMES = list(_STATIC_PRESETS.keys())


# ---------------------------------------------------------------------------
# GET /api/v1/lmp/presets — list all 9 presets
# ---------------------------------------------------------------------------
@router.get("/presets")
def list_all_presets() -> dict:
    """Return all registered LMP presets with their block flags and consumer aliases."""
    presets = {}
    names = list(_PRESET_REGISTRY.keys()) if _PRESETS_AVAILABLE else _ALL_PRESET_NAMES
    for name in names:
        try:
            presets[name] = _preset_to_info(name).model_dump()
        except Exception as e:
            presets[name] = {"name": name, "error": str(e)}
    return {
        "ok": True,
        "count": len(presets),
        "presets": presets,
        "backend_available": _PRESETS_AVAILABLE,
        "load_error": _PRESETS_LOAD_ERROR,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/lmp/preset/{name} — single preset detail
# ---------------------------------------------------------------------------
@router.get("/preset/{name}")
def get_preset_detail(name: str) -> dict:
    """Return detailed information for a single LMP preset by name."""
    valid_names = list(_PRESET_REGISTRY.keys()) if _PRESETS_AVAILABLE else _ALL_PRESET_NAMES
    if name not in valid_names:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown preset '{name}'. Valid presets: {', '.join(sorted(valid_names))}",
        )
    try:
        info = _preset_to_info(name)
        return {"ok": True, "preset": info.model_dump()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load preset: {e}")


# ---------------------------------------------------------------------------
# GET /api/v1/lmp/consumer/{consumer} — resolve consumer alias → preset
# ---------------------------------------------------------------------------
@router.get("/consumer/{consumer}")
def resolve_consumer(consumer: str) -> dict:
    """Map a consumer shorthand (plm, llm, md, esm2, gpt, …) to the recommended preset."""
    key = consumer.lower().strip()
    # Try the static map first
    preset_name = _CONSUMER_MAP.get(key)
    if not preset_name:
        # Try preset_for_consumer from bsm.lmp if available
        if _PRESETS_AVAILABLE and _preset_for_consumer:
            try:
                p = _preset_for_consumer(key)
                preset_name = p.name
            except (ValueError, KeyError):
                pass
    if not preset_name:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown consumer '{consumer}'. Known aliases: {', '.join(sorted(_CONSUMER_MAP))}",
        )
    info = _preset_to_info(preset_name)
    return {
        "ok": True,
        "consumer": consumer,
        "resolved_preset": preset_name,
        "preset": info.model_dump(),
    }


# ---------------------------------------------------------------------------
# POST /api/v1/lmp/generate — trigger LMP XML generation
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    pdb_id: Optional[str] = Field(None, description="PDB ID (e.g. 2V3S). UniProt will be resolved automatically via RCSB.")
    uniprot: Optional[str] = Field(None, description="UniProt accession (e.g. P12931)")
    gene: Optional[str] = Field(None, description="Gene name (e.g. SRC)")
    preset: str = Field("nesy-core", description="Preset name (one of the 9 registered presets)")
    pdb_ids: List[str] = Field(default_factory=list, description="PDB IDs for structural context")
    states: List[str] = Field(default_factory=list, description="Biological states (e.g. Apo_Inactive)")
    out_dir: Optional[str] = Field(None, description="Output directory (default: .tmp_lmp_v4)")
    validate_xsd: bool = Field(True, description="Run XSD validation after generation")
    offline: bool = Field(False, description="Disable network calls (use cached data)")

    @model_validator(mode="after")
    def _require_pdb_or_uniprot(self) -> "GenerateRequest":
        if not self.pdb_id and not self.uniprot:
            raise ValueError("At least one of 'pdb_id' or 'uniprot' must be provided")
        return self


class ImportedStructureLiteraturePolicy(BaseModel):
    enabled: bool = Field(True, description="Compile deterministic literature handoff from the scan receipt")
    require_fulltext: bool = Field(True, description="Treat abstract-only acquisition as degraded")
    max_papers: int = Field(15, ge=1, le=200, description="Target paper budget for downstream Bibliotecario")
    execute_search: bool = Field(False, description="Reserved for the later async Bibliotecario execution slice")


class ImportedStructureDLMPolicy(BaseModel):
    materialize_kb: bool = Field(False, description="Reserved for downstream DocumentScanService materialization")
    promote_atom: bool = Field(False, description="Reserved for reviewed ATOM/GraphRAG promotion")


class ImportedStructureSMICPolicy(BaseModel):
    static_context: bool = Field(True, description="Include LMP zero-frame static contact context")
    execute_modules: List[str] = Field(default_factory=list, description="Requested downstream SMIC modules")
    require_feature_flag: bool = Field(True, description="Block module execution until the typed SMIC surface is enabled")


class ImportedStructureServerlessPolicy(BaseModel):
    generate_if_usable_structure_exists: bool = Field(False, description="Allow generation even when imported coordinates are usable")
    requires_approval_for_generation: bool = Field(True, description="Require an approval receipt before generation")
    approval_receipt_id: Optional[str] = Field(None, description="Approval receipt for downstream generation")


class ScanImportedStructureRequest(BaseModel):
    structure_uri: str = Field(..., description="Local/workspace/file URI for a PDB structure")
    asset_id: Optional[str] = Field(None, description="Optional stable structure asset id")
    workspace_id: Optional[str] = Field(None, description="Optional workspace scope")
    identity_policy: str = Field(
        "local_metadata",
        description="local_metadata | local_then_remote_sequence | local_then_remote_blast",
    )
    remote_identity_timeout_seconds: int = Field(30, ge=1, le=600)
    literature_policy: ImportedStructureLiteraturePolicy = Field(default_factory=ImportedStructureLiteraturePolicy)
    dlm_policy: ImportedStructureDLMPolicy = Field(default_factory=ImportedStructureDLMPolicy)
    smic_policy: ImportedStructureSMICPolicy = Field(default_factory=ImportedStructureSMICPolicy)
    serverless_policy: ImportedStructureServerlessPolicy = Field(default_factory=ImportedStructureServerlessPolicy)
    emit_lmp_xml: bool = Field(False, description="Reserved for downstream XML bundle generation/enqueue")
    validate_xsd: bool = Field(True, description="Required when XML generation is enabled")

    @model_validator(mode="after")
    def _validate_scan_request(self) -> "ScanImportedStructureRequest":
        allowed = {"local_metadata", "local_then_remote_sequence", "local_then_remote_blast"}
        if self.identity_policy not in allowed:
            raise ValueError(f"identity_policy must be one of: {', '.join(sorted(allowed))}")
        return self


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _resolve_imported_structure_path(structure_uri: str) -> Path:
    raw_uri = str(structure_uri or "").strip()
    if not raw_uri:
        raise HTTPException(status_code=400, detail="structure_uri is required")
    parsed = urlparse(raw_uri)
    if parsed.scheme in {"gs", "s3", "http", "https"}:
        raise HTTPException(
            status_code=501,
            detail="Remote imported-structure URIs require the async custody/download slice; provide a local or workspace path for this scanner surface.",
        )
    if parsed.scheme == "file":
        path_text = unquote(parsed.path or "")
        if parsed.netloc and not path_text.startswith("/"):
            path_text = f"//{parsed.netloc}/{path_text}"
        if re.match(r"^/[A-Za-z]:/", path_text):
            path_text = path_text[1:]
        path = Path(path_text)
    else:
        path = Path(raw_uri)
    if not path.is_absolute():
        path = _repo_root() / path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Imported structure not found: {path}") from exc
    if resolved.suffix.lower() not in {".pdb", ".ent"}:
        raise HTTPException(status_code=400, detail="scan_imported_structure currently accepts PDB/.ent coordinate files")
    return resolved


def _identity_policy_flags(identity_policy: str) -> tuple[bool, bool]:
    if identity_policy == "local_then_remote_blast":
        return True, True
    if identity_policy == "local_then_remote_sequence":
        return True, False
    return False, False


def _compact_features(features: Sequence[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    compacted: List[Dict[str, Any]] = []
    for feature in features[:limit]:
        compacted.append({
            "type": feature.get("type"),
            "name": feature.get("name"),
            "start": feature.get("start"),
            "end": feature.get("end"),
            "source": feature.get("source"),
        })
    return compacted


def _rfxx_motifs(sequence: str) -> List[Dict[str, Any]]:
    motifs: List[Dict[str, Any]] = []
    for match in re.finditer(r"RF[A-Z][A-Z]", str(sequence or "").upper()):
        motif = match.group(0)
        if motif[3] not in {"V", "I"}:
            continue
        motifs.append({
            "motif_family": "RFxV/RFIV",
            "motif_sequence": motif,
            "chain_local_start": match.start() + 1,
            "chain_local_end": match.end(),
        })
    return motifs


def _chain_identities(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    chains_by_id = {chain.get("chain_id"): chain for chain in context.get("chains", [])}
    identities: List[Dict[str, Any]] = []
    for resolution in context.get("identity_resolution", {}).get("chain_resolutions", []):
        chain_id = resolution.get("chain_id")
        chain = chains_by_id.get(chain_id, {})
        accepted = resolution.get("accepted_identity") or {}
        identities.append({
            "chain_id": chain_id,
            "status": resolution.get("status"),
            "sequence_length": resolution.get("sequence_length") or chain.get("sequence_length"),
            "sequence_sha256": resolution.get("sequence_sha256") or chain.get("sequence_sha256"),
            "residue_ranges": chain.get("residue_ranges", []),
            "uniprot_accession": accepted.get("uniprot_accession"),
            "entry_name": accepted.get("uniprot_entry_name"),
            "protein_name": accepted.get("protein_name"),
            "genes": accepted.get("genes", []),
            "organism": accepted.get("organism"),
            "protein_range": accepted.get("protein_range"),
            "identity_source": accepted.get("identity_source"),
            "confidence": accepted.get("confidence"),
            "motifs": _rfxx_motifs(chain.get("sequence", "")),
            "features": _compact_features((resolution.get("domain_context") or {}).get("features", [])),
        })
    return identities


def _identity_evidence(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for resolution in context.get("identity_resolution", {}).get("chain_resolutions", []):
        chain_id = resolution.get("chain_id")
        accepted = resolution.get("accepted_identity") or {}
        if accepted:
            evidence.append({
                "chain_id": chain_id,
                "evidence_kind": "accepted_identity",
                "source": accepted.get("identity_source"),
                "confidence": accepted.get("confidence"),
                "uniprot_accession": accepted.get("uniprot_accession"),
                "protein_range": accepted.get("protein_range"),
                "pdb_hit": accepted.get("pdb_hit"),
                "evidence_chain": accepted.get("evidence_chain", []),
            })
        for hint in resolution.get("metadata_hints", []):
            evidence.append({"chain_id": chain_id, "evidence_kind": "metadata_hint", **hint})
        remote = resolution.get("remote_sequence_search") or {}
        for candidate in remote.get("candidate_hits", [])[:3]:
            uniprot = candidate.get("uniprot") or {}
            evidence.append({
                "chain_id": chain_id,
                "evidence_kind": "rcsb_sifts_candidate",
                "identifier": candidate.get("identifier"),
                "entry_id": candidate.get("entry_id"),
                "score": candidate.get("score"),
                "uniprot_accession": uniprot.get("accession"),
                "title": candidate.get("title"),
            })
        blast = remote.get("ncbi_blast") or {}
        for hit in blast.get("hits", [])[:3]:
            evidence.append({
                "chain_id": chain_id,
                "evidence_kind": "ncbi_remote_blast_hit",
                "hit_id": hit.get("id") or hit.get("accession"),
                "description": hit.get("description"),
                "identity_percent": hit.get("identity_percent"),
                "e_value": hit.get("e_value"),
            })
    return evidence


def _identity_contradictions(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    contradictions: List[Dict[str, Any]] = []
    for resolution in context.get("identity_resolution", {}).get("chain_resolutions", []):
        accepted = resolution.get("accepted_identity") or {}
        accepted_accession = accepted.get("uniprot_accession")
        if not accepted_accession:
            continue
        for candidate in (resolution.get("remote_sequence_search") or {}).get("candidate_hits", [])[:5]:
            candidate_accession = (candidate.get("uniprot") or {}).get("accession")
            if candidate_accession and candidate_accession != accepted_accession:
                contradictions.append({
                    "chain_id": resolution.get("chain_id"),
                    "kind": "accepted_identity_differs_from_remote_candidate",
                    "accepted_uniprot_accession": accepted_accession,
                    "candidate_uniprot_accession": candidate_accession,
                    "candidate_identifier": candidate.get("identifier"),
                    "candidate_score": candidate.get("score"),
                    "resolution": "preserved_for_audit_not_overwritten",
                })
    return contradictions


def _bibliotecario_handoff_payload(context: Dict[str, Any], chain_identities: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    chains: List[Dict[str, Any]] = []
    for identity in chain_identities:
        chains.append({
            "chain_id": identity.get("chain_id"),
            "uniprot_accession": identity.get("uniprot_accession"),
            "entry_name": identity.get("entry_name"),
            "protein_name": identity.get("protein_name"),
            "genes": identity.get("genes", []),
            "organism": identity.get("organism"),
            "protein_range": identity.get("protein_range"),
            "domain_terms": [feature.get("name") for feature in identity.get("features", []) if feature.get("name")],
            "motif_terms": [motif.get("motif_sequence") for motif in identity.get("motifs", [])],
        })
    return {
        "source": "scan_imported_structure",
        "asset_id": context.get("asset", {}).get("asset_id"),
        "chains": chains,
        "physical_context_terms": [
            context.get("physical_context", {}).get("analysis_kind"),
            context.get("physical_context", {}).get("smic_compatibility"),
        ],
        "smic_handoff": context.get("smic_handoff", {}),
    }


def _primary_literature_query(chain_identities: Sequence[Dict[str, Any]]) -> str:
    terms: List[str] = []
    for identity in chain_identities:
        terms.extend(str(gene) for gene in identity.get("genes", []) if gene)
        for key in ("protein_name", "uniprot_accession"):
            value = identity.get(key)
            if value:
                terms.append(str(value))
        terms.extend(motif.get("motif_sequence", "") for motif in identity.get("motifs", []))
    cleaned: List[str] = []
    seen: set[str] = set()
    for term in terms:
        value = str(term or "").strip()
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        cleaned.append(value)
    return " ".join(cleaned[:8]) or "imported protein structure biological identity"


def _literature_context_for_scan(
    req: ScanImportedStructureRequest,
    context: Dict[str, Any],
    chain_identities: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    if not req.literature_policy.enabled:
        return {"enabled": False, "execution_state": "disabled_by_request", "degraded": []}
    from mica.literature_consolidation.lmp_bibliotecario_handoff import compile_lmp_bibliotecario_handoff

    handoff = _bibliotecario_handoff_payload(context, chain_identities)
    query = _primary_literature_query(chain_identities)
    strategy = compile_lmp_bibliotecario_handoff(
        query=query,
        entities=[item.get("uniprot_accession") for item in chain_identities if item.get("uniprot_accession")],
        pdb_ids=[],
        extra_queries=[],
        lmp_handoff=handoff,
        require_full_text=req.literature_policy.require_fulltext,
    )
    degraded = []
    if req.literature_policy.execute_search:
        degraded.append("bibliotecario_execution_requested_but_deferred_to_async_scan_slice")
    elif req.literature_policy.require_fulltext:
        degraded.append("fulltext_required_but_bibliotecario_execution_not_run_in_sync_scan")
    return {
        "enabled": True,
        "execution_state": "handoff_compiled" if not req.literature_policy.execute_search else "execution_deferred_to_async_bibliotecario",
        "query": query,
        "max_papers": req.literature_policy.max_papers,
        "lmp_bibliotecario_handoff": strategy,
        "handoff_payload": handoff,
        "degraded": degraded,
    }


def _serverless_decision(req: ScanImportedStructureRequest, context: Dict[str, Any]) -> Dict[str, Any]:
    usable_structure = bool(context.get("chains")) and context.get("physical_context", {}).get("status") in {
        "contacts_computed",
        "insufficient_chains",
    }
    policy = req.serverless_policy
    if usable_structure and not policy.generate_if_usable_structure_exists:
        state = "suppressed_existing_usable_structure"
        reason = "Imported coordinate asset parsed successfully; generation is downstream-only unless coverage/refinement/approval requires it."
    elif policy.requires_approval_for_generation and not policy.approval_receipt_id:
        state = "approval_required_generation_blocked"
        reason = "Generation was not launched because no approval receipt was supplied."
    else:
        state = "generation_not_launched_by_scanner_sync_surface"
        reason = "This sync scanner returns lineage and policy only; provider execution belongs to the serverless execution slice."
    return {
        "state": state,
        "reason": reason,
        "usable_imported_structure": usable_structure,
        "generation_receipt": None,
        "source_structure_asset_id": context.get("asset", {}).get("asset_id"),
        "approval_receipt_id": policy.approval_receipt_id,
    }


def _scanner_closure_state(degraded: Sequence[str], context: Dict[str, Any]) -> str:
    if not context.get("chains"):
        return "failed"
    if degraded:
        return "partial"
    if context.get("identity_resolution", {}).get("status") == "resolved_identity":
        return "closed_layer1"
    return "partial"


def _dedupe_reasons(values: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _bibliotecario_child_payload(
    req: ScanImportedStructureRequest,
    receipt: Dict[str, Any],
    *,
    user_id: str,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    literature_context = dict(receipt.get("literature_context") or {})
    query_strategy = dict(literature_context.get("lmp_bibliotecario_handoff") or {})
    handoff_payload = dict(literature_context.get("handoff_payload") or {})
    entities: List[str] = []
    for identity in receipt.get("chain_identities") or []:
        entities.extend(str(gene) for gene in identity.get("genes", []) if gene)
        for key in ("uniprot_accession", "protein_name", "entry_name"):
            value = identity.get(key)
            if value:
                entities.append(str(value))
    session_scope = req.workspace_id or receipt.get("workspace_id") or receipt.get("scan_id")
    return {
        "task_type": "bibliotecario_scan",
        "query": str(literature_context.get("query") or ""),
        "preset": "literature-review",
        "entities": _dedupe_reasons(entities),
        "extra_queries": list(query_strategy.get("extra_queries") or []),
        "pdb_ids": [],
        "lmp_handoff": handoff_payload,
        "max_papers": max(10, int(literature_context.get("max_papers") or req.literature_policy.max_papers)),
        "sources": ["semantic_scholar", "pubmed", "openalex"],
        "session_id": session_scope,
        "run_id": run_id or receipt.get("scan_id"),
        "user_id": user_id,
        "require_full_text": bool(req.literature_policy.require_fulltext),
        "acquisition_budget_usd": None,
        "upstream_scan_receipt": {
            "scan_id": receipt.get("scan_id"),
            "receipt_schema": receipt.get("receipt_schema"),
            "structure_asset_id": (receipt.get("structure_asset") or {}).get("asset_id"),
        },
    }


async def _maybe_enqueue_scan_bibliotecario_job(
    req: ScanImportedStructureRequest,
    receipt: Dict[str, Any],
    *,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    if not req.literature_policy.enabled or not req.literature_policy.execute_search:
        return None

    literature_context = dict(receipt.get("literature_context") or {})
    degraded = [
        item
        for item in literature_context.get("degraded", [])
        if item != "bibliotecario_execution_requested_but_deferred_to_async_scan_slice"
    ]

    store = await _get_job_store()
    if store is None:
        degraded.append("bibliotecario_execution_requested_but_queue_unavailable")
        literature_context["execution_state"] = "bibliotecario_queue_unavailable"
        literature_context["degraded"] = _dedupe_reasons(degraded)
        receipt["literature_context"] = literature_context
        receipt["degraded"] = _dedupe_reasons(
            [
                *(receipt.get("degraded") or []),
                "bibliotecario_execution_requested_but_queue_unavailable",
            ]
        )
        return None

    child_job_id = f"bib-{uuid.uuid4().hex[:10]}"
    child_payload = _bibliotecario_child_payload(
        req,
        receipt,
        user_id=user_id,
        run_id=child_job_id,
    )
    await store.enqueue(
        job_id=child_job_id,
        lane="research",
        payload=child_payload,
        user_id=user_id,
    )
    child_receipt = {
        "job_id": child_job_id,
        "task_type": "bibliotecario_scan",
        "lane": "research",
        "status": "queued",
        "poll_path": f"/api/v1/research/bibliotecario/scan/{child_job_id}",
        "status_path": f"/api/v1/research/bibliotecario/scan/{child_job_id}/status",
        "queued_from": "scan_imported_structure",
        "backend": "redis",
    }
    literature_context["execution_state"] = "bibliotecario_job_enqueued"
    literature_context["bibliotecario_job"] = child_receipt
    literature_context["degraded"] = _dedupe_reasons([*degraded, "bibliotecario_fulltext_job_pending"])
    receipt["literature_context"] = literature_context
    receipt["async_children"] = [
        *(receipt.get("async_children") or []),
        child_receipt,
    ]
    receipt["degraded"] = _dedupe_reasons(
        [
            *[
                item
                for item in (receipt.get("degraded") or [])
                if item != "bibliotecario_execution_requested_but_deferred_to_async_scan_slice"
            ],
            "bibliotecario_fulltext_job_pending",
        ]
    )
    return child_receipt


def _resolve_uniprot_from_pdb(pdb_id: str) -> tuple[str, str | None]:
    """Resolve UniProt accession and gene name from a PDB ID via the RCSB REST API.

    Returns (uniprot_accession, gene_name_or_None).
    Raises RuntimeError on failure.
    """
    url = f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id.upper()}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(
            f"Failed to fetch RCSB entry for PDB '{pdb_id}': {exc}"
        ) from exc

    # Navigate: the /core/entry/ endpoint does NOT embed polymer_entities inline.
    # We need to call /core/polymer_entity/{pdb_id}/1 for the first entity.
    # First try the entry-level identifiers for polymer entity IDs.
    poly_ids = []
    try:
        poly_ids = data.get("rcsb_entry_container_identifiers", {}).get("polymer_entity_ids", [])
    except (AttributeError, TypeError):
        pass

    uniprot_id = None
    gene_name: str | None = None

    # Try each polymer entity until we find a UniProt mapping
    for eid in (poly_ids or ["1", "2"]):
        ent_url = f"https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id.upper()}/{eid}"
        try:
            ent_req = urllib.request.Request(ent_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(ent_req, timeout=20) as ent_resp:
                ent_data = json.loads(ent_resp.read().decode())
            # uniprot_ids is at the top level of rcsb_polymer_entity_container_identifiers
            ent_ids = ent_data.get("rcsb_polymer_entity_container_identifiers", {})
            u_ids = ent_ids.get("uniprot_ids", [])
            if u_ids:
                uniprot_id = u_ids[0]
                # Try gene name from source organism
                try:
                    gene_name = ent_data["rcsb_entity_source_organism"][0].get("ncbi_scientific_name")
                except (KeyError, IndexError, TypeError):
                    pass
                # Also try gene_name from rcsb_polymer_entity
                if not gene_name:
                    try:
                        gene_name = ent_data.get("rcsb_polymer_entity", {}).get("pdbx_description")
                    except (AttributeError, TypeError):
                        pass
                break
        except Exception:
            continue

    if not uniprot_id:
        raise RuntimeError(
            f"Could not extract UniProt accession from RCSB data for PDB '{pdb_id}'. "
            f"The entry may lack a UniProt cross-reference."
        )
    try:
        gene_name = ent_data.get("rcsb_polymer_entity", {}).get("pdbx_description", gene_name)
    except (KeyError, TypeError, NameError):
        pass

    return uniprot_id, gene_name


def _lmp_bucket_key(uniprot: str, preset: str) -> str:
    """Canonical bucket key for LMP artifacts: lmp_v4/{uniprot}/{preset}.xml"""
    return f"lmp_v4/{uniprot}/{preset}.lmp_v4.xml"


def _persist_lmp_to_bucket(user_id: str, uniprot: str, preset: str, out_dir: str, output_files: list[str]) -> list[str]:
    """Upload generated LMP XMLs to the user's GCS bucket. Returns list of gs:// URIs."""
    uris: list[str] = []
    try:
        from mica.storage.gcs_user_storage import get_storage_manager
        storage = get_storage_manager()
    except Exception as exc:
        logger.warning("Bucket persistence skipped (storage unavailable): %s", exc)
        return uris

    for fname in output_files:
        local_path = Path(out_dir) / fname
        if not local_path.exists():
            continue
        object_path = f"lmp_v4/{uniprot}/{fname}"
        try:
            uri = storage.upload_file(
                user_id=user_id,
                object_path=object_path,
                local_path=local_path,
                content_type="application/xml",
            )
            uris.append(uri)
            logger.info("LMP artifact persisted → %s", uri)
        except Exception as exc:
            logger.warning("Failed to persist %s: %s", fname, exc)
    return uris


def _check_lmp_cache(user_id: str, uniprot: str, preset: str) -> Optional[str]:
    """Check if a cached LMP XML exists in the user bucket.

    Persisted filenames under ``lmp_v4/{uniprot}/`` can include state
    suffixes (e.g. ``{preset}_Inactive.lmp_v4.xml`` or ``{preset}_Apo.lmp_v4.xml``).
    A plain canonical-key lookup therefore produces silent cache misses.

    Resolution order:
      1. Exact canonical key ``lmp_v4/{uniprot}/{preset}.lmp_v4.xml``.
      2. Prefix-list under ``lmp_v4/{uniprot}/`` and pick the first object
         whose filename starts with ``{preset}`` and ends with ``.lmp_v4.xml``.

    Returns the text content of the resolved object, or ``None``.
    """
    try:
        from mica.storage.gcs_user_storage import get_storage_manager
        storage = get_storage_manager()
    except Exception:
        return None

    canonical_key = _lmp_bucket_key(uniprot, preset)
    # 1) Exact canonical path (legacy fast-path).
    try:
        result = storage.read_text_best_effort(
            user_id=user_id,
            object_path=canonical_key,
            max_chars=500_000,
        )
        text = result.get("text")
        if text:
            logger.info("LMP cache HIT (canonical): %s for user %s", canonical_key, user_id[:8])
            return text
    except Exception:
        pass

    # 2) Prefix list — cover state-suffixed filenames produced by generator_v4.
    try:
        prefix = f"lmp_v4/{uniprot}/"
        entries = storage.list_objects(
            user_id=user_id,
            prefix=prefix,
            max_results=50,
        )
    except Exception as exc:
        logger.debug("LMP cache prefix-list skipped: %s", exc)
        return None

    preset_prefix = f"{preset}"
    best_match: Optional[str] = None
    for entry in entries:
        name = entry.get("object_path") or entry.get("name") or ""
        if not name.endswith(".lmp_v4.xml"):
            continue
        tail = name[len(prefix):] if name.startswith(prefix) else name.rsplit("/", 1)[-1]
        if tail == f"{preset}.lmp_v4.xml":
            best_match = name
            break
        # Accept state-suffix variants: {preset}_*.lmp_v4.xml
        if tail.startswith(f"{preset_prefix}_") and tail.endswith(".lmp_v4.xml"):
            # Prefer shortest tail (closest to canonical) but take first if none earlier.
            if best_match is None:
                best_match = name

    if not best_match:
        return None

    try:
        result = storage.read_text_best_effort(
            user_id=user_id,
            object_path=best_match,
            max_chars=500_000,
        )
        text = result.get("text")
        if text:
            logger.info("LMP cache HIT (state-suffix): %s for user %s", best_match, user_id[:8])
            return text
    except Exception:
        pass
    return None


@router.post("/imported-structures/scan")
async def scan_imported_structure(
    req: ScanImportedStructureRequest,
    user_id: str = Depends(user_dependency),
) -> dict:
    """Scan a user/imported PDB into a traceable LMP structure receipt."""
    structure_path = _resolve_imported_structure_path(req.structure_uri)
    enable_remote_sequence_search, enable_remote_blast = _identity_policy_flags(req.identity_policy)
    try:
        from bsm.lmp.structure_asset_context import detect_pdb_structure_context

        context_obj = detect_pdb_structure_context(
            structure_path,
            asset_id=req.asset_id,
            privacy_decision="workspace_scanner_receipt",
            enable_remote_sequence_search=enable_remote_sequence_search,
            enable_remote_blast=enable_remote_blast,
            remote_identity_timeout_seconds=req.remote_identity_timeout_seconds,
        )
        context = context_obj.to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("scan_imported_structure failed for %s", structure_path)
        raise HTTPException(status_code=500, detail=f"Imported structure scan failed: {exc}") from exc

    chain_identities = _chain_identities(context)
    identity_evidence = _identity_evidence(context)
    contradictions = _identity_contradictions(context)
    literature_context = _literature_context_for_scan(req, context, chain_identities)
    serverless_decision = _serverless_decision(req, context)

    degraded: List[str] = []
    degraded.extend(context.get("warnings", []))
    degraded.extend(literature_context.get("degraded", []))
    if req.emit_lmp_xml:
        degraded.append("lmp_xml_generation_requested_but_not_enqueued_by_sync_scan_surface")
    if req.dlm_policy.materialize_kb:
        degraded.append("dlm_materialization_requested_but_not_run_by_sync_scan_surface")
    if req.smic_policy.execute_modules:
        degraded.append("smic_modules_requested_but_typed_executor_not_run_by_sync_scan_surface")
    if serverless_decision["state"] != "suppressed_existing_usable_structure":
        degraded.append(serverless_decision["state"])

    scan_id = f"scan_{uuid.uuid4().hex[:16]}"
    receipt = {
        "ok": True,
        "scan_id": scan_id,
        "receipt_schema": "mica.lmp.scan_imported_structure.v1",
        "user_id": user_id,
        "workspace_id": req.workspace_id,
        "identity_policy": req.identity_policy,
        "structure_asset": context.get("asset", {}),
        "chain_identities": chain_identities,
        "identity_evidence": identity_evidence,
        "contradictions": contradictions,
        "physical_context": context.get("physical_context", {}),
        "lmp_structure_context": context,
        "protocol_nodes": [
            {
                "node_kind": "SCAN_IMPORTED_STRUCTURE",
                "executor_surface": "lmp.scan_imported_structure",
                "inputs": {
                    "structure_uri": str(structure_path),
                    "asset_id": context.get("asset", {}).get("asset_id"),
                    "identity_policy": req.identity_policy,
                },
            },
            *context.get("lmp_attachment", {}).get("protocol_nodes", []),
        ],
        "lmp_xml_bundle": {
            "requested": req.emit_lmp_xml,
            "validate_xsd": req.validate_xsd,
            "execution_state": "not_requested" if not req.emit_lmp_xml else "pending_async_generation_slice",
            "validated": False,
            "artifacts": [],
        },
        "literature_context": literature_context,
        "kb_dlm_context": {
            "requested": req.dlm_policy.materialize_kb,
            "promote_atom": req.dlm_policy.promote_atom,
            "execution_state": "not_materialized_by_sync_scan_surface",
        },
        "mica_q_context": {
            "queryable": False,
            "surface_roots": ["lmp_structure_context"],
            "execution_state": "receipt_returned_inline_not_indexed",
        },
        "smic_receipts": [],
        "smic_handoff": context.get("smic_handoff", {}),
        "serverless_decision": serverless_decision,
        "degraded": _dedupe_reasons(degraded),
    }
    await _maybe_enqueue_scan_bibliotecario_job(req, receipt, user_id=user_id)
    receipt["closure_state"] = _scanner_closure_state(receipt["degraded"], context)
    return receipt


async def _run_scan_imported_structure_job(job_id: str, req: ScanImportedStructureRequest, user_id: str) -> None:
    _JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "request": req.model_dump(),
        "queued_at": _JOBS.get(job_id, {}).get("queued_at", time.time()),
        "started_at": time.time(),
        "finished_at": None,
        "error": None,
        "result": None,
    }
    try:
        result = await scan_imported_structure(req, user_id=user_id)
        _JOBS[job_id]["status"] = "done"
        _JOBS[job_id]["result"] = result
        _JOBS[job_id]["finished_at"] = time.time()
    except Exception as exc:
        _JOBS[job_id]["status"] = "error"
        _JOBS[job_id]["error"] = str(exc)
        _JOBS[job_id]["finished_at"] = time.time()


def _resolve_best_available_bibliotecario_text(paper: Dict[str, Any]) -> tuple[str, str]:
    full_text = str(paper.get("full_text") or "").strip()
    if full_text:
        return full_text, str(paper.get("content_type") or "full_text")

    metadata = dict(paper.get("metadata") or {})
    section_parts: List[str] = []
    for section in list(metadata.get("sections") or []):
        if not isinstance(section, dict):
            continue
        text = str(
            section.get("text")
            or section.get("content")
            or section.get("body")
            or section.get("full_text")
            or ""
        ).strip()
        if text:
            section_parts.append(text)
    if section_parts:
        return "\n\n".join(section_parts).strip(), str(metadata.get("acquisition_kind") or "sections")

    abstract = str(paper.get("abstract") or "").strip()
    if abstract:
        return abstract, "abstract_only"
    return "", "missing_text"


def _scan_scoped_kb_id(result: Dict[str, Any]) -> str:
    scan_id = str(result.get("scan_id") or "").strip() or uuid.uuid4().hex[:16]
    workspace_id = str(result.get("workspace_id") or "").strip()
    if workspace_id:
        return f"lmp_scan::{workspace_id}::{scan_id}"
    return f"lmp_scan::{scan_id}"


def _build_imported_structure_materialization_payload(
    *,
    parent_job_id: str,
    result: Dict[str, Any],
    child_result: Dict[str, Any],
) -> Dict[str, Any]:
    from mica.infrastructure.literature.literature_artifact_bundle import canonicalize_paper_record

    query = str(child_result.get("query") or result.get("literature_context", {}).get("query") or "imported structure literature bundle").strip()
    papers = [paper for paper in list(child_result.get("papers") or []) if isinstance(paper, dict)]
    canonical_papers: List[Dict[str, Any]] = []
    sections: List[str] = []
    degraded: List[str] = []
    content_kinds: List[str] = []

    for index, paper in enumerate(papers[:25], start=1):
        canonical = canonicalize_paper_record(paper)
        best_text, content_kind = _resolve_best_available_bibliotecario_text(paper)
        if not best_text:
            degraded.append("bibliotecario_child_paper_missing_text")
            continue
        content_kinds.append(content_kind)
        canonical["materialized_text_kind"] = content_kind
        canonical_papers.append(canonical)
        title = str(canonical.get("title") or canonical.get("canonical_id") or canonical.get("paper_id") or f"paper_{index}")
        sections.append(f"## {title}\n\n{best_text}")

    if not sections:
        return {
            "ok": False,
            "reason": "bibliotecario_child_missing_materializable_text",
            "query": query,
            "canonical_papers": canonical_papers,
            "degraded": _dedupe_reasons(degraded + ["bibliotecario_child_missing_materializable_text"]),
        }

    header = [
        f"Imported-structure literature bridge for query: {query}",
        f"Parent job: {parent_job_id}",
        f"Child job: {str(result.get('literature_context', {}).get('bibliotecario_job', {}).get('job_id') or '').strip()}",
        f"Total papers: {int(child_result.get('total_papers') or len(canonical_papers))}",
    ]
    text = "\n".join(header) + "\n\n" + "\n\n".join(sections)
    return {
        "ok": True,
        "query": query,
        "title": f"Imported structure literature bundle - {query[:120]}",
        "text": text,
        "canonical_papers": canonical_papers,
        "paper_count": len(canonical_papers),
        "content_kinds": sorted(set(content_kinds)),
        "degraded": _dedupe_reasons(degraded),
        "sources_used": list(child_result.get("sources_used") or []),
        "gcs_artifacts": dict(child_result.get("gcs_artifacts") or {}),
    }


def _resolve_document_scan_service_for_materialization(request: Optional[Request] = None) -> Any:
    if request is not None:
        app_state = getattr(getattr(request, "app", None), "state", None)
        service = getattr(app_state, "document_scan_service", None)
        if service is not None:
            return service

    from mica.pipelines.knowledge_fabric.document_scan_service import DocumentScanService

    return DocumentScanService()


async def _fetch_imported_structure_mica_q_context(
    *,
    query_text: str,
    user_id: str,
    session_id: str,
    workspace_id: str = "",
    limit: int = 6,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        from mica.api_v1.routers.bibliotecario import (
            _resolve_mica_q_multisurface_service_for_router,
            _resolve_router_graph_store,
        )
        from mica.infrastructure.persistence.retrieval_planner import RetrievalPlanner, RetrievalRequest
        from mica.memory.contracts import RetrievalMode
    except Exception as exc:
        return None, f"mica_q_imports_unavailable:{type(exc).__name__}"

    graph_store = _resolve_router_graph_store()
    service = _resolve_mica_q_multisurface_service_for_router(graph_store=graph_store)
    if service is None:
        return None, "mica_q_service_unavailable"

    planner = RetrievalPlanner(graph_store=graph_store, mica_q_service=service)
    try:
        response = await planner.retrieve(
            RetrievalRequest(
                mode=RetrievalMode.MICA_Q_MULTISURFACE,
                query_text=query_text,
                user_id=user_id or None,
                workspace_id=workspace_id or None,
                session_id=session_id or None,
                limit=max(1, int(limit)),
            )
        )
    except Exception as exc:
        return None, f"mica_q_retrieval_failed:{type(exc).__name__}"

    payload = dict(response.payload or {})
    if not payload:
        return None, "mica_q_context_empty"
    return payload, None


async def _load_job_record(*, job_id: str, store: Any) -> Optional[Dict[str, Any]]:
    if store is not None:
        record = await store.get(job_id)
        if record is not None:
            return record
    return _JOBS.get(job_id)


async def _persist_job_result(*, job_id: str, store: Any, result: Dict[str, Any]) -> None:
    if store is not None:
        await store.set_done(job_id, result=result)
    if job_id in _JOBS:
        _JOBS[job_id]["status"] = "done"
        _JOBS[job_id]["result"] = result
        _JOBS[job_id]["finished_at"] = _JOBS[job_id].get("finished_at") or time.time()


async def _maybe_materialize_imported_structure_result(
    *,
    parent_job_id: str,
    record: Dict[str, Any],
    store: Any,
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    result = dict(record.get("result") or {})
    if not result:
        return record

    kb_context = dict(result.get("kb_dlm_context") or {})
    mica_q_context = dict(result.get("mica_q_context") or {})
    if not kb_context.get("requested"):
        return record
    if str(kb_context.get("execution_state") or "").strip() not in {"", "not_materialized_by_sync_scan_surface", "awaiting_completed_bibliotecario_child"}:
        return record

    literature_context = dict(result.get("literature_context") or {})
    child_job = dict(literature_context.get("bibliotecario_job") or {})
    child_job_id = str(child_job.get("job_id") or "").strip()
    if not child_job_id:
        degraded = _dedupe_reasons(list(result.get("degraded") or []) + ["bibliotecario_child_job_missing_for_sp08_materialization"])
        kb_context["execution_state"] = "materialization_requested_but_bibliotecario_child_missing"
        mica_q_context["execution_state"] = "materialization_requested_but_bibliotecario_child_missing"
        result["kb_dlm_context"] = kb_context
        result["mica_q_context"] = mica_q_context
        result["degraded"] = degraded
        result["closure_state"] = _scanner_closure_state(degraded, result.get("lmp_structure_context", {}))
        record["result"] = result
        await _persist_job_result(job_id=parent_job_id, store=store, result=result)
        return record

    child_record = await _load_job_record(job_id=child_job_id, store=store)
    child_status = str((child_record or {}).get("status") or child_job.get("status") or "unknown").strip().lower()
    if child_status != "done":
        kb_context["execution_state"] = "awaiting_completed_bibliotecario_child"
        mica_q_context["execution_state"] = "awaiting_completed_bibliotecario_child"
        result["kb_dlm_context"] = kb_context
        result["mica_q_context"] = mica_q_context
        record["result"] = result
        await _persist_job_result(job_id=parent_job_id, store=store, result=result)
        return record

    child_result = dict((child_record or {}).get("result") or {})
    materialization = _build_imported_structure_materialization_payload(
        parent_job_id=parent_job_id,
        result=result,
        child_result=child_result,
    )
    local_degraded = list(materialization.get("degraded") or [])
    degraded = [
        reason
        for reason in list(result.get("degraded") or [])
        if reason not in {
            "dlm_materialization_requested_but_not_run_by_sync_scan_surface",
            "bibliotecario_fulltext_job_pending",
        }
    ]

    if not materialization.get("ok"):
        degraded = _dedupe_reasons(degraded + local_degraded)
        kb_context["execution_state"] = str(materialization.get("reason") or "materialization_failed")
        mica_q_context["execution_state"] = "materialization_failed"
        result["kb_dlm_context"] = kb_context
        result["mica_q_context"] = mica_q_context
        result["degraded"] = degraded
        result["closure_state"] = _scanner_closure_state(degraded, result.get("lmp_structure_context", {}))
        record["result"] = result
        await _persist_job_result(job_id=parent_job_id, store=store, result=result)
        return record

    from mica.pipelines.knowledge_fabric.document_envelope import DocumentKind, DocumentScanMode

    document_scan_service = _resolve_document_scan_service_for_materialization(request)
    kb_id = str(kb_context.get("kb_id") or _scan_scoped_kb_id(result)).strip()
    owner_id = str(result.get("user_id") or record.get("user_id") or "agent").strip() or "agent"
    workspace_id = str(result.get("workspace_id") or "")
    asset_id = str((result.get("structure_asset") or {}).get("asset_id") or result.get("asset_id") or "")
    canonical_paper_id = str(
        (list(materialization.get("canonical_papers") or [{}])[0] or {}).get("canonical_id")
        or (list(materialization.get("canonical_papers") or [{}])[0] or {}).get("paper_id")
        or ""
    ).strip()
    scan = await document_scan_service.create_scan(
        title=str(materialization.get("title") or "Imported structure literature bundle"),
        text=str(materialization.get("text") or ""),
        mode=DocumentScanMode.DLM_SECTIONS_AND_ATOM,
        document_kind=DocumentKind.KB_SOURCE,
        owner_id=owner_id,
        workspace_id=workspace_id,
        asset_id=asset_id,
        kb_id=kb_id,
        provider="bibliotecario",
        acquisition_type="bibliotecario_imported_structure_bundle",
        canonical_paper_id=canonical_paper_id,
        metadata={
            "modality": "literature",
            "parent_job_id": parent_job_id,
            "child_job_id": child_job_id,
            "query": materialization.get("query"),
            "canonical_papers": materialization.get("canonical_papers"),
            "sources_used": materialization.get("sources_used"),
            "gcs_artifacts": materialization.get("gcs_artifacts"),
            "content_kinds": materialization.get("content_kinds"),
        },
    )

    promotion_payload = None
    if kb_context.get("promote_atom"):
        promotion = await document_scan_service.promote_kb_scan(
            kb_id=kb_id,
            scan_id=scan.scan_id,
            minimum_evidentiality_score=0.1,
        )
        promotion_payload = promotion.to_dict() if hasattr(promotion, "to_dict") else dict(getattr(promotion, "__dict__", {}))

    mica_payload, mica_error = await _fetch_imported_structure_mica_q_context(
        query_text=str(materialization.get("query") or result.get("scan_id") or parent_job_id),
        user_id=owner_id,
        session_id=str(result.get("scan_id") or parent_job_id),
        workspace_id=workspace_id,
    )
    if mica_error:
        local_degraded.append(mica_error)
    degraded = _dedupe_reasons(degraded + local_degraded)

    kb_context.update(
        {
            "requested": True,
            "promote_atom": bool(kb_context.get("promote_atom")),
            "kb_id": kb_id,
            "materialized_scan_id": scan.scan_id,
            "paper_count": int(materialization.get("paper_count") or 0),
            "content_kinds": list(materialization.get("content_kinds") or []),
            "execution_state": "atom_promoted_from_bibliotecario_child" if promotion_payload else "scan_materialized_from_bibliotecario_child",
            "scan": scan.model_dump(mode="json"),
        }
    )
    if promotion_payload is not None:
        kb_context["atom_promotion"] = promotion_payload

    mica_q_context = {
        "queryable": True,
        "execution_state": "query_packet_materialized_from_bibliotecario_child",
        "surface_roots": ["lmp_structure_context", "kb_dlm_context", "bibliotecario_child_result"],
        "query_text": str(materialization.get("query") or ""),
        "kb_id": kb_id,
        "materialized_scan_id": scan.scan_id,
        "canonical_paper_ids": [
            str(paper.get("canonical_id") or paper.get("paper_id") or "")
            for paper in list(materialization.get("canonical_papers") or [])
            if str(paper.get("canonical_id") or paper.get("paper_id") or "").strip()
        ],
        "multisurface_context": mica_payload,
    }

    result["kb_dlm_context"] = kb_context
    result["mica_q_context"] = mica_q_context
    result["degraded"] = degraded
    result["closure_state"] = _scanner_closure_state(degraded, result.get("lmp_structure_context", {}))
    record["result"] = result
    await _persist_job_result(job_id=parent_job_id, store=store, result=result)
    return record


@router.post("/imported-structures/scan/async")
async def enqueue_scan_imported_structure(
    req: ScanImportedStructureRequest,
    user_id: str = Depends(user_dependency),
) -> dict:
    """Queue the imported-structure scanner on the LMP worker."""
    job_id = f"lmpscan-{uuid.uuid4().hex[:12]}"
    store = await _get_job_store()
    if store is not None:
        try:
            await store.enqueue(
                job_id=job_id,
                lane="lmp",
                payload={
                    "task_type": "lmp_scan_imported_structure",
                    "request": req.model_dump(),
                    "user_id": user_id,
                },
                user_id=user_id,
            )
            return {
                "ok": True,
                "job_id": job_id,
                "status": "queued",
                "backend": "redis",
                "poll_path": f"/api/v1/lmp/imported-structures/scan/{job_id}",
            }
        except Exception as exc:
            logger.warning("Redis enqueue failed for lmp_scan_imported_structure: %s", exc)
            if _PROD_ENV:
                raise _production_queue_required("imported-structure scan")

    if _PROD_ENV:
        raise _production_queue_required("imported-structure scan")

    _JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "request": req.model_dump(),
        "queued_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
    }
    asyncio.create_task(_run_scan_imported_structure_job(job_id, req, user_id))
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "backend": "memory",
        "poll_path": f"/api/v1/lmp/imported-structures/scan/{job_id}",
    }


async def _get_scan_imported_structure_status(job_id: str, request: Optional[Request] = None) -> dict:
    """Poll the status of an async imported-structure scan."""
    store = await _get_job_store()
    if store is not None:
        try:
            record = await store.get(job_id)
        except Exception:
            if _PROD_ENV:
                raise _production_queue_required("imported-structure scan polling")
        else:
            if record is not None:
                if str(record.get("status") or "").strip().lower() == "done":
                    record = await _maybe_materialize_imported_structure_result(
                        parent_job_id=job_id,
                        record=record,
                        store=store,
                        request=request,
                    )
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": record.get("status", "unknown"),
                    "job": record,
                }

    if _PROD_ENV:
        raise _production_queue_required("imported-structure scan polling")

    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if str(job.get("status") or "").strip().lower() == "done":
        job = await _maybe_materialize_imported_structure_result(
            parent_job_id=job_id,
            record=job,
            store=None,
            request=request,
        )
    return {
        "ok": True,
        "job_id": job_id,
        "status": job.get("status", "unknown"),
        "job": job,
    }


@router.get("/imported-structures/scan/{job_id}")
async def get_scan_imported_structure_status(job_id: str, request: Request) -> dict:
    return await _get_scan_imported_structure_status(job_id, request=request)


def _snapshot_lmp_outputs(out_dir: str) -> Dict[str, int]:
    """Return {filename: mtime_ns} for current LMP XML artifacts in *out_dir*."""
    base = Path(out_dir)
    return {
        path.name: path.stat().st_mtime_ns
        for path in base.glob("*.lmp_v4.xml")
        if path.is_file()
    }


def _collect_job_output_files(out_dir: str, before: Dict[str, int]) -> List[str]:
    """Return XML files created or updated during the current job."""
    after = _snapshot_lmp_outputs(out_dir)
    changed = [
        name
        for name, mtime_ns in after.items()
        if before.get(name) != mtime_ns
    ]
    return sorted(changed)


async def _run_generation(job_id: str, req: GenerateRequest, user_id: str = "agent") -> None:
    """Background task: run generator_v4.py as subprocess."""
    _JOBS[job_id]["status"] = "running"
    _JOBS[job_id]["started_at"] = time.time()

    # --- Resolve UniProt from PDB ID if needed ---
    resolved_pdb_id = req.pdb_id.upper() if req.pdb_id else None
    uniprot = req.uniprot
    gene = req.gene

    if not uniprot and resolved_pdb_id:
        try:
            uniprot, resolved_gene = _resolve_uniprot_from_pdb(resolved_pdb_id)
            if not gene:
                gene = resolved_gene or resolved_pdb_id
            logger.info("Resolved PDB %s → UniProt %s (gene=%s)", resolved_pdb_id, uniprot, gene)
        except RuntimeError as exc:
            _JOBS[job_id].update({
                "status": "error",
                "error": str(exc),
                "finished_at": time.time(),
            })
            return

    if not uniprot:
        _JOBS[job_id].update({
            "status": "error",
            "error": "Could not determine UniProt accession. Provide 'uniprot' or a valid 'pdb_id'.",
            "finished_at": time.time(),
        })
        return

    # Ensure gene has a fallback
    if not gene:
        gene = uniprot

    # Merge pdb_id into pdb_ids list if not already present
    pdb_ids = list(req.pdb_ids)
    if resolved_pdb_id and resolved_pdb_id not in [p.upper() for p in pdb_ids]:
        pdb_ids.insert(0, resolved_pdb_id)

    # Locate generator_v4.py relative to this file
    repo_root = Path(__file__).resolve().parents[4]
    generator = repo_root / "src" / "bsm" / "lmp" / "generator_v4.py"
    if not generator.exists():
        _JOBS[job_id].update({
            "status": "error",
            "error": f"generator_v4.py not found at {generator}",
            "finished_at": time.time(),
        })
        return

    out_dir = req.out_dir or str(repo_root / ".tmp_lmp_v4")
    os.makedirs(out_dir, exist_ok=True)
    before_outputs = _snapshot_lmp_outputs(out_dir)

    cmd = [
        sys.executable, "-m", "bsm.lmp.generator_v4",
        "--preset", req.preset,
        "--uniprot", uniprot,
        "--gene", gene,
        "--out-dir", out_dir,
    ]
    if pdb_ids:
        cmd += ["--pdb-ids"] + pdb_ids
    if req.states:
        cmd += ["--states"] + req.states
    if req.offline:
        cmd.append("--offline")

    env = os.environ.copy()
    src_dir = str(repo_root / "src")
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        rc = proc.returncode

        # Collect output files
        output_files = _collect_job_output_files(out_dir, before_outputs)

        _JOBS[job_id].update({
            "status": "completed" if rc == 0 else "error",
            "return_code": rc,
            "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-4000:],
            "out_dir": out_dir,
            "output_files": output_files,
            "files": output_files,
            "finished_at": time.time(),
            "error": None if rc == 0 else f"Generator exited with code {rc}",
        })

        # Persist generated LMP XMLs to user bucket
        if rc == 0 and output_files and uniprot:
            bucket_uris = _persist_lmp_to_bucket(
                user_id, uniprot, req.preset, out_dir,
                output_files,
            )
            _JOBS[job_id]["bucket_uris"] = bucket_uris
    except asyncio.TimeoutError:
        _JOBS[job_id].update({
            "status": "error",
            "error": "Generation timed out after 300 seconds",
            "finished_at": time.time(),
        })
    except Exception as exc:
        _JOBS[job_id].update({
            "status": "error",
            "error": str(exc),
            "finished_at": time.time(),
        })


@router.post("/generate")
async def generate_lmp(
    req: GenerateRequest,
    user_id: str = Depends(user_dependency),
) -> dict:
    """
    Trigger LMP v4 XML generation for a protein using the specified preset.
    Returns a job_id immediately; poll /api/v1/lmp/generate/{job_id} for status.

    If a cached result exists in the user's GCS bucket, returns it immediately.

    Attempts to enqueue via Redis for durable worker execution.
    Production traffic must never fall back to in-process execution.
    """
    valid_names = list(_PRESET_REGISTRY.keys()) if _PRESETS_AVAILABLE else _ALL_PRESET_NAMES
    if req.preset not in valid_names:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown preset '{req.preset}'. Valid: {', '.join(sorted(valid_names))}",
        )
    if req.uniprot and not req.uniprot.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid UniProt accession")
    if req.pdb_id and not req.pdb_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid PDB ID")

    # Check bucket cache before spawning expensive generation
    cache_key_uniprot = req.uniprot
    if not cache_key_uniprot and req.pdb_id:
        try:
            cache_key_uniprot, _ = _resolve_uniprot_from_pdb(req.pdb_id)
        except Exception:
            cache_key_uniprot = None
    if cache_key_uniprot:
        cached = _check_lmp_cache(user_id, cache_key_uniprot, req.preset)
        if cached:
            return {
                "ok": True,
                "cached": True,
                "uniprot": cache_key_uniprot,
                "preset": req.preset,
                "bucket_key": _lmp_bucket_key(cache_key_uniprot, req.preset),
                "xml_preview": cached[:2000],
            }

    job_id = str(uuid.uuid4())

    # ── Try durable Redis path ──────────────────────────
    store = await _get_job_store()
    if store is not None:
        try:
            await store.enqueue(
                job_id=job_id,
                lane="lmp",
                payload={
                    "task_type": "lmp_generate",
                    "request": req.model_dump(),
                    "user_id": user_id,
                },
                user_id=user_id,
            )
            logger.info("lmp_generate job %s enqueued to Redis", job_id[:8])
            return {"ok": True, "job_id": job_id, "status": "queued", "backend": "redis"}
        except Exception as exc:
            logger.warning("Redis enqueue failed for lmp_generate: %s", exc)
            if _PROD_ENV:
                raise _production_queue_required("lmp generation")

    if _PROD_ENV:
        raise _production_queue_required("lmp generation")

    _JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "request": req.model_dump(),
        "queued_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "output_files": [],
    }
    import asyncio
    asyncio.create_task(_run_generation(job_id, req, user_id))
    return {"ok": True, "job_id": job_id, "status": "queued", "backend": "memory"}


@router.get("/generate/{job_id}")
async def get_job_status(job_id: str) -> dict:
    """Poll the status of a generation job (checks Redis first, then in-memory)."""
    # Try Redis store
    store = await _get_job_store()
    if store is not None:
        try:
            record = await store.get(job_id)
            if record is not None:
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": record.get("status", "unknown"),
                    "job": record,
                }
        except Exception:
            if _PROD_ENV:
                raise _production_queue_required("lmp polling")
            pass  # fall through to in-memory

    if _PROD_ENV:
        raise _production_queue_required("lmp polling")

    # Fallback to in-memory dict
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"ok": True, "job_id": job_id, "status": job.get("status", "unknown"), "job": job}

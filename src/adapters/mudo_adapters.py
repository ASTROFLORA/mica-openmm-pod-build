"""M-UDO schema adapters.

Bridges external M-UDO dialects (ChronosFold scaffold + BSM packaging)
into the canonical API Pydantic models defined in ``src/models/mudo.py``.

Notes
-----
- We intentionally keep imports soft (NumPy / torch are optional) and rely on
  duck typing to avoid hard coupling to the scaffolds.
- Canonical schema: ``MUDOCreateRequest`` / ``MUDOUpdateRequest`` from
  ``src.models.mudo``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:  # Optional dependency
    import numpy as _np
except Exception:  # pragma: no cover - optional
    _np = None

try:  # Optional dependency
    import torch as _torch
except Exception:  # pragma: no cover - optional
    _torch = None

from src.models.mudo import MUDOCreateRequest, MUDOUpdateRequest


def _to_plain(value: Any) -> Any:
    """Convert numpy/torch containers to plain Python lists for JSON safety."""

    if _np is not None and isinstance(value, _np.ndarray):
        return value.tolist()
    if _torch is not None and isinstance(value, _torch.Tensor):
        return value.detach().cpu().tolist()
    if isinstance(value, (list, dict, str, int, float)) or value is None:
        return value
    # Fallback to string to avoid serialization failures
    return str(value)


# ---------------------------------------------------------------------------
# ChronosFold scaffold adapters
# ---------------------------------------------------------------------------

def chronos_to_create_request(
    chronos_mudo: Any,
    *,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> MUDOCreateRequest:
    """Map a ChronosFold ``ChronosMUDO`` (or similar dict) into create payload."""

    # Duck-typed access to fields
    entity_type = getattr(chronos_mudo, "entity_type", None) or "protein"
    name = getattr(chronos_mudo, "name", None) or getattr(chronos_mudo, "mudo_id", "chronos_mudo")

    raw_data: Dict[str, Any] = {
        "trajectory_data": _to_plain(getattr(chronos_mudo, "trajectory_data", None)),
        "physics_data": _to_plain(getattr(chronos_mudo, "physics_data", None)),
        "conformational_states": _to_plain(getattr(chronos_mudo, "conformational_states", None)),
        "diffusion_samples": _to_plain(getattr(chronos_mudo, "diffusion_samples", None)),
        "spectral_signatures": _to_plain(getattr(chronos_mudo, "spectral_signatures", None)),
        "unified_embedding": _to_plain(getattr(chronos_mudo, "unified_embedding", None)),
        "execution_plan": _to_plain(getattr(chronos_mudo, "execution_plan", None)),
        "causal_chain": _to_plain(getattr(chronos_mudo, "causal_chain", None)),
    }

    annotations: Dict[str, Any] = {
        "kan_validation": _to_plain(getattr(chronos_mudo, "kan_validation", None)),
        "gpt_predictions": _to_plain(getattr(chronos_mudo, "gpt_predictions", None)),
        "smic_sites": _to_plain(getattr(chronos_mudo, "smic_sites", None)),
        "chronoracle_decisions": _to_plain(getattr(chronos_mudo, "chronoracle_decisions", None)),
        "cognitive_state": _to_plain(getattr(chronos_mudo, "cognitive_state", None)),
    }

    # Remove empty entries to keep payload clean
    raw_data = {k: v for k, v in raw_data.items() if v is not None}
    annotations = {k: v for k, v in annotations.items() if v is not None}

    return MUDOCreateRequest(
        entity_type=entity_type,
        name=name,
        description=description,
        raw_data=raw_data,
        tags=tags or [],
    )


def chronos_to_update_request(
    chronos_mudo: Any,
    *,
    status: Optional[str] = None,
    suggested_actions: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> MUDOUpdateRequest:
    """Map an updated ChronosFold MUDO into an update payload."""

    raw_data: Dict[str, Any] = {
        "trajectory_data": _to_plain(getattr(chronos_mudo, "trajectory_data", None)),
        "physics_data": _to_plain(getattr(chronos_mudo, "physics_data", None)),
        "conformational_states": _to_plain(getattr(chronos_mudo, "conformational_states", None)),
        "diffusion_samples": _to_plain(getattr(chronos_mudo, "diffusion_samples", None)),
        "spectral_signatures": _to_plain(getattr(chronos_mudo, "spectral_signatures", None)),
        "unified_embedding": _to_plain(getattr(chronos_mudo, "unified_embedding", None)),
        "execution_plan": _to_plain(getattr(chronos_mudo, "execution_plan", None)),
        "causal_chain": _to_plain(getattr(chronos_mudo, "causal_chain", None)),
    }

    annotations: Dict[str, Any] = {
        "kan_validation": _to_plain(getattr(chronos_mudo, "kan_validation", None)),
        "gpt_predictions": _to_plain(getattr(chronos_mudo, "gpt_predictions", None)),
        "smic_sites": _to_plain(getattr(chronos_mudo, "smic_sites", None)),
        "chronoracle_decisions": _to_plain(getattr(chronos_mudo, "chronoracle_decisions", None)),
        "cognitive_state": _to_plain(getattr(chronos_mudo, "cognitive_state", None)),
    }

    raw_data = {k: v for k, v in raw_data.items() if v is not None}
    annotations = {k: v for k, v in annotations.items() if v is not None}

    return MUDOUpdateRequest(
        annotations=annotations or None,
        status=status,
        suggested_actions=suggested_actions,
        raw_data=raw_data or None,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# BSM packaging adapters
# ---------------------------------------------------------------------------

def bsm_mudo_to_create_request(
    bsm_mudo: Any,
    *,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> MUDOCreateRequest:
    """Map BSM packaging ``MUDO`` (mudo_packaging.py) into create payload."""

    sequence_id = getattr(bsm_mudo, "sequence_id", "bsm_mudo")
    name = getattr(bsm_mudo, "metadata", None)
    name = getattr(name, "protein_name", None) or sequence_id

    # Embeddings and analyses as annotations
    embeddings = {}
    for etype, emb in getattr(bsm_mudo, "embeddings", {}).items():
        embeddings[etype] = {
            "vector": _to_plain(getattr(emb, "embedding_vector", None)),
            "dimension": getattr(emb, "dimension", None),
            "confidence_score": getattr(emb, "confidence_score", None),
            "model_version": getattr(emb, "model_version", None),
            "magnitude": getattr(emb, "magnitude", None),
            "sparsity": getattr(emb, "sparsity", None),
            "entropy": getattr(emb, "entropy", None),
        }

    analyses = {}
    for aname, analysis in getattr(bsm_mudo, "analyses", {}).items():
        analyses[aname] = {
            "results": _to_plain(getattr(analysis, "results", None)),
            "confidence_level": getattr(analysis, "confidence_level", None),
            "statistical_significance": getattr(analysis, "statistical_significance", None),
            "method_used": getattr(analysis, "method_used", None),
        }

    annotations = {
        "embeddings": embeddings or None,
        "analyses": analyses or None,
        "validation_status": getattr(getattr(bsm_mudo, "metadata", None), "validation_status", None),
        "quality_score": getattr(getattr(bsm_mudo, "metadata", None), "quality_score", None),
    }
    annotations = {k: v for k, v in annotations.items() if v is not None}

    raw_data = {
        "sequence": getattr(bsm_mudo, "sequence", None),
        "data_references": getattr(bsm_mudo, "data_references", None),
        "source_modalities": getattr(getattr(bsm_mudo, "metadata", None), "source_modalities", None),
        "processing_pipeline": getattr(getattr(bsm_mudo, "metadata", None), "processing_pipeline", None),
        "parent_m_udos": getattr(getattr(bsm_mudo, "metadata", None), "parent_m_udos", None),
    }
    raw_data = {k: v for k, v in raw_data.items() if v is not None}

    return MUDOCreateRequest(
        entity_type="protein",
        name=name,
        description=description,
        raw_data=raw_data,
        tags=tags or [],
    )


def bsm_mudo_to_update_request(
    bsm_mudo: Any,
    *,
    status: Optional[str] = None,
    suggested_actions: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> MUDOUpdateRequest:
    """Map an updated BSM ``MUDO`` to update payload."""

    embeddings = {}
    for etype, emb in getattr(bsm_mudo, "embeddings", {}).items():
        embeddings[etype] = {
            "vector": _to_plain(getattr(emb, "embedding_vector", None)),
            "dimension": getattr(emb, "dimension", None),
            "confidence_score": getattr(emb, "confidence_score", None),
            "model_version": getattr(emb, "model_version", None),
            "magnitude": getattr(emb, "magnitude", None),
            "sparsity": getattr(emb, "sparsity", None),
            "entropy": getattr(emb, "entropy", None),
        }

    analyses = {}
    for aname, analysis in getattr(bsm_mudo, "analyses", {}).items():
        analyses[aname] = {
            "results": _to_plain(getattr(analysis, "results", None)),
            "confidence_level": getattr(analysis, "confidence_level", None),
            "statistical_significance": getattr(analysis, "statistical_significance", None),
            "method_used": getattr(analysis, "method_used", None),
        }

    annotations = {
        "embeddings": embeddings or None,
        "analyses": analyses or None,
        "validation_status": getattr(getattr(bsm_mudo, "metadata", None), "validation_status", None),
        "quality_score": getattr(getattr(bsm_mudo, "metadata", None), "quality_score", None),
    }
    annotations = {k: v for k, v in annotations.items() if v is not None}

    raw_data = {
        "sequence": getattr(bsm_mudo, "sequence", None),
        "data_references": getattr(bsm_mudo, "data_references", None),
        "source_modalities": getattr(getattr(bsm_mudo, "metadata", None), "source_modalities", None),
        "processing_pipeline": getattr(getattr(bsm_mudo, "metadata", None), "processing_pipeline", None),
        "parent_m_udos": getattr(getattr(bsm_mudo, "metadata", None), "parent_m_udos", None),
    }
    raw_data = {k: v for k, v in raw_data.items() if v is not None}

    return MUDOUpdateRequest(
        annotations=annotations or None,
        status=status,
        suggested_actions=suggested_actions,
        raw_data=raw_data or None,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# BUDO V3 → M-UDO adapter
# ---------------------------------------------------------------------------

def budo_v3_to_create_request(
    budo: Any,
    *,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> MUDOCreateRequest:
    """Map a BUDO V3 (``bsm.schemas.budo_v3.BudoV3``) into a MUDOCreateRequest.

    The adapter extracts identity, sequence, domains, ESE signatures,
    functional state, and cross-references from the typed BUDO V3 Pydantic
    model, producing a flat ``raw_data`` + ``annotations`` payload suitable
    for ``MUDOService.create_mudo``.

    Works with both a ``BudoV3`` Pydantic instance and plain ``dict``
    representations (``budo.model_dump()`` output).
    """

    _get = (
        (lambda k, d=None: budo.get(k, d))
        if isinstance(budo, dict)
        else (lambda k, d=None: getattr(budo, k, d))
    )

    budo_id: str = _get("budoId") or "unknown"
    canonical_name: str = _get("canonical_name") or _get("recommended_name") or budo_id
    sequence: str = _get("sequence") or ""

    # ---- raw_data: identity + sequence + structure refs ----
    raw_data: Dict[str, Any] = {
        "budo_ref": budo_id,
        "sequence": sequence,
        "sequence_length": _get("sequence_length") or len(sequence),
        "organism": _get("organism"),
        "taxonomy_id": _get("taxonomy_id"),
        "molecular_weight": _get("molecular_weight"),
        "isoelectric_point": _get("isoelectric_point"),
    }
    raw_data = {k: v for k, v in raw_data.items() if v is not None}

    # ---- annotations: domains ----
    domains_raw = _get("domains") or []
    domains_out: List[Dict[str, Any]] = []
    for d in domains_raw:
        _dget = (lambda k, df=None: d.get(k, df)) if isinstance(d, dict) else (lambda k, df=None: getattr(d, k, df))
        entry: Dict[str, Any] = {
            "domain_id": _dget("domain_id"),
            "domain_name": _dget("domain_name"),
            "domain_type": _dget("domain_type"),
            "start": _dget("start_position"),
            "end": _dget("end_position"),
            "cath_id": _dget("cath_id"),
            "pfam_id": _dget("pfam_id"),
            "interpro_id": _dget("interpro_id"),
        }
        entry = {k: v for k, v in entry.items() if v is not None}
        domains_out.append(entry)

    # ---- annotations: ESE signatures ----
    ese_raw = _get("ese_signatures") or []
    ese_out: List[Dict[str, Any]] = []
    for sig in ese_raw:
        _sget = (lambda k, df=None: sig.get(k, df)) if isinstance(sig, dict) else (lambda k, df=None: getattr(sig, k, df))
        ese_entry: Dict[str, Any] = {
            "trajectory_id": _sget("trajectory_id"),
            "rmsd_mean": _sget("rmsd_mean"),
            "rmsd_std": _sget("rmsd_std"),
            "radius_of_gyration": _sget("radius_of_gyration"),
            "ese_vector_dim": len(_sget("ese_vector") or []),
        }
        ese_entry = {k: v for k, v in ese_entry.items() if v is not None}
        ese_out.append(ese_entry)

    # ---- annotations: functional state ----
    fs = _get("functionalState")
    functional_state: Optional[Dict[str, Any]] = None
    if fs is not None:
        _fget = (lambda k, df=None: fs.get(k, df)) if isinstance(fs, dict) else (lambda k, df=None: getattr(fs, k, df))
        current = _fget("current")
        if hasattr(current, "value"):
            current = current.value
        functional_state = {
            "current": current,
            "predicted": _fget("predicted"),
            "prediction_confidence": _fget("prediction_confidence"),
        }

    # ---- annotations: biological annotations ----
    annotations: Dict[str, Any] = {
        "domains": domains_out or None,
        "ese_signatures": ese_out or None,
        "functional_state": functional_state,
        "go_terms": _get("go_terms") or None,
        "kegg_pathways": _get("kegg_pathways") or None,
        "reactome_pathways": _get("reactome_pathways") or None,
        "ec_numbers": _get("ec_numbers") or None,
        "variants_count": len(_get("variants") or []),
        "embeddings_count": len(_get("embeddings") or []),
        "interfaces_count": len(_get("interfaces") or []),
    }
    annotations = {k: v for k, v in annotations.items() if v is not None}

    # ---- determine status from functional state ----
    status_val = "new"
    if functional_state and functional_state.get("current") and functional_state["current"] != "unknown":
        status_val = functional_state["current"]

    auto_tags = ["budo_v3", "protein"]
    if _get("organism"):
        auto_tags.append(_get("organism").lower().replace(" ", "_"))

    return MUDOCreateRequest(
        entity_type="protein",
        name=canonical_name,
        description=description or f"M-UDO created from BUDO V3 {budo_id}",
        raw_data=raw_data,
        tags=list(set((tags or []) + auto_tags)),
    )


__all__ = [
    "chronos_to_create_request",
    "chronos_to_update_request",
    "bsm_mudo_to_create_request",
    "bsm_mudo_to_update_request",
    "budo_v3_to_create_request",
]
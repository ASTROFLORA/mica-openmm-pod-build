from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .alphafold_client import AlphaFoldClient
from .structural_metrics import StructuralMetricsComputer

logger = logging.getLogger(__name__)

_THREE_TO_ONE = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLN": "Q",
    "GLU": "E",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "PHE": "F",
    "PRO": "P",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
    "SEC": "U",
    "PYL": "O",
    "ASX": "B",
    "GLX": "Z",
    "XLE": "J",
    "UNK": "X",
}


def _resolve_cache_dir(cache_dir: Optional[Path] = None) -> Path:
    resolved = Path(cache_dir) if cache_dir is not None else Path("lmp_cache")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _serialize_pocket_sites(raw_pocket_sites: Any) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for pocket in raw_pocket_sites or []:
        residues = []
        for residue in getattr(pocket, "residues", []) or []:
            residues.append(
                {
                    "residue_id": int(getattr(residue, "residue_id", 0) or 0),
                    "chain": str(getattr(residue, "chain", "") or "") or None,
                    "residue_name": str(getattr(residue, "residue_name", "") or "") or None,
                }
            )
        serialized.append(
            {
                "id": str(getattr(pocket, "pocket_id", "pocket") or "pocket"),
                "rank": int(getattr(pocket, "rank", 0) or 0) or None,
                "engine": str(getattr(pocket, "engine", "") or "") or None,
                "source": str(getattr(pocket, "source", "") or "") or None,
                "score": float(getattr(pocket, "score", 0.0) or 0.0),
                "volume": float(getattr(pocket, "volume", 0.0) or 0.0),
                "center_x": float(getattr(pocket, "center_x", 0.0) or 0.0),
                "center_y": float(getattr(pocket, "center_y", 0.0) or 0.0),
                "center_z": float(getattr(pocket, "center_z", 0.0) or 0.0),
                "point_count": int(getattr(pocket, "point_count", 0) or 0),
                "residue_count": int(getattr(pocket, "residue_count", 0) or 0),
                "static": bool(getattr(pocket, "static", True)),
                "residues": residues,
                "artifact_refs": [],
            }
        )
    return serialized


def _structure_ref_for_accession(accession: str) -> str:
    normalized_accession = str(accession or "").strip().upper()
    return f"alphafold:{normalized_accession}" if normalized_accession else "alphafold"


def _display_name_for_entry(entry_id: Optional[str], accession: str) -> str:
    normalized_entry = str(entry_id or "").strip()
    if normalized_entry:
        return normalized_entry.rsplit("-F", 1)[0] if "-F" in normalized_entry else normalized_entry
    normalized_accession = str(accession or "").strip().upper()
    return f"AF-{normalized_accession}" if normalized_accession else "AlphaFold"


def _secondary_structure_lookup(metrics: Any) -> Dict[int, str]:
    lookup: Dict[int, str] = {}
    secondary_structure = getattr(metrics, "secondary_structure", None) if metrics is not None else None
    for segment in getattr(secondary_structure, "segments", []) or []:
        try:
            start = int(getattr(segment, "start", 0) or 0)
            end = int(getattr(segment, "end", 0) or 0)
        except (TypeError, ValueError):
            continue
        if start <= 0 or end <= 0 or end < start:
            continue
        ss_type = str(getattr(segment, "ss_type", "") or "")
        if not ss_type:
            continue
        for position in range(start, end + 1):
            lookup[position] = ss_type
    return lookup


def _hub_lookup(metrics: Any) -> Dict[int, Any]:
    lookup: Dict[int, Any] = {}
    for hub in getattr(metrics, "hub_residues", []) or []:
        try:
            lookup[int(getattr(hub, "residue_id", 0) or 0)] = hub
        except (TypeError, ValueError):
            continue
    return lookup


def _serialize_structure_catalog(structure: Any, accession: str) -> List[Dict[str, Any]]:
    meta = getattr(structure, "meta", None)
    residue_rows = list(getattr(structure, "plddt_per_residue", None) or [])
    structure_ref = _structure_ref_for_accession(accession)

    start = 1
    try:
        meta_start = int(getattr(meta, "uniprot_start", 0) or 0)
        if meta_start > 0:
            start = meta_start
        elif residue_rows:
            start = int(residue_rows[0][0])
    except (TypeError, ValueError, IndexError):
        start = 1

    end = start
    try:
        meta_end = int(getattr(meta, "uniprot_end", 0) or 0)
        if meta_end >= start:
            end = meta_end
        elif residue_rows:
            end = max(int(row[0]) for row in residue_rows)
    except (TypeError, ValueError, IndexError):
        end = start

    coverage_segments: List[Dict[str, Any]] = []
    if start > 0 and end >= start:
        coverage_segments.append(
            {
                "start": start,
                "end": end,
                "chain_id": "A",
                "auth_chain_id": "A",
                "label_chain_id": "A",
                "entity_type": "protein",
            }
        )

    return [
        {
            "structure_ref": structure_ref,
            "source_kind": "predicted_model",
            "provider": "alphafold",
            "provider_native_id": str(accession or "").strip().upper() or None,
            "coordinate_accession_ref": str(accession or "").strip().upper() or None,
            "artifact_uri": str(getattr(meta, "pdb_url", "") or "") or None,
            "format": "pdb",
            "display_name": _display_name_for_entry(getattr(meta, "entry_id", None), accession),
            "representative": True,
            "coverage_segments": coverage_segments,
        }
    ]


def _serialize_structure_set(accession: str) -> Dict[str, Any]:
    structure_ref = _structure_ref_for_accession(accession)
    normalized_accession = str(accession or "").strip().upper() or None
    return {
        "representative_structure_ref": structure_ref,
        "coordinate_accession_ref": normalized_accession,
        "members": [
            {
                "structure_ref": structure_ref,
                "role": "representative",
            }
        ],
    }


def _serialize_residue_statistics(structure: Any, metrics: Any, accession: str) -> List[Dict[str, Any]]:
    structure_ref = _structure_ref_for_accession(accession)
    residue_rows = list(getattr(structure, "plddt_per_residue", None) or [])
    secondary_structure_lookup = _secondary_structure_lookup(metrics)
    hub_lookup = _hub_lookup(metrics)

    serialized: List[Dict[str, Any]] = []
    for residue_row in residue_rows:
        try:
            position = int(residue_row[0])
            residue_name = str(residue_row[1] or "").strip().upper()
            confidence = float(residue_row[2])
        except (TypeError, ValueError, IndexError):
            continue
        hub = hub_lookup.get(position)
        residue_payload: Dict[str, Any] = {
            "position": position,
            "amino_acid": _THREE_TO_ONE.get(residue_name, "X"),
            "structure_ref": structure_ref,
            "chain": "A",
            "confidence": round(confidence, 2),
            "confidence_source": "afdb_plddt",
            "confidence_class": AlphaFoldClient.plddt_confidence_class(confidence),
        }
        secondary_structure = secondary_structure_lookup.get(position)
        if secondary_structure:
            residue_payload["secondary_structure"] = secondary_structure
        if hub is not None:
            betweenness = getattr(hub, "betweenness", None)
            degree = getattr(hub, "degree", None)
            if betweenness is not None:
                residue_payload["hub_score"] = round(float(betweenness), 4)
            if degree is not None:
                residue_payload["contact_degree"] = round(float(degree), 4)
        serialized.append(residue_payload)
    return serialized


def compute_afdb_first_structural_receipt(
    accession: str,
    *,
    cache_dir: Optional[Path] = None,
    prefer_smic_static: bool = True,
) -> Optional[Dict[str, Any]]:
    """Resolve an AlphaFold DB structure and project it onto bounded LMP structural contracts."""
    normalized_accession = str(accession or "").strip().upper()
    if not normalized_accession:
        return None

    client = AlphaFoldClient(_resolve_cache_dir(cache_dir))
    structure = client.get_structure_for_accession(
        normalized_accession,
        download_pdb=True,
        download_pae=False,
    )
    if structure is None or structure.pdb_path is None:
        return None

    try:
        metrics = StructuralMetricsComputer().compute_all(
            structure.pdb_path,
            source="alphafold",
            compute_dssp=True,
            compute_contacts=True,
            compute_network=True,
            compute_quality=True,
            compute_pockets=True,
            prefer_smic_static=prefer_smic_static,
        )
    except Exception as exc:
        logger.warning("AFDB-first structural receipt failed for %s: %s", normalized_accession, exc)
        return None

    return {
        "source_kind": "alphafold_db",
        "structure_origin": "afdb_live",
        "accession": normalized_accession,
        "entry_id": str(structure.meta.entry_id or "") or None,
        "structure_path": str(structure.pdb_path),
        "alphafold": {
            "entry_id": str(structure.meta.entry_id or "") or None,
            "avg_plddt": float(structure.meta.confidence_avg_plddt)
            if structure.meta.confidence_avg_plddt is not None
            else None,
            "model_date": str(structure.meta.model_created_date or "") or None,
        },
        "pocket_sites": _serialize_pocket_sites(getattr(metrics, "pocket_sites", None)),
        "structure_catalog": _serialize_structure_catalog(structure, normalized_accession),
        "structure_set": _serialize_structure_set(normalized_accession),
        "residue_statistics": _serialize_residue_statistics(structure, metrics, normalized_accession),
    }
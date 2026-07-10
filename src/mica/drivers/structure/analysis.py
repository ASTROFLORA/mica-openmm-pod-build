"""Structure analysis pure functions.

Phase 3 extraction from agentic_driver.py.
All functions are pure or accept explicit dependencies.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


# ── Direct-structure path detection ──────────────────────────────────

def should_use_direct_structure_path(user_query: str) -> bool:
    """Decide if the query should bypass normal routing and go straight
    to the structure-download fast-path.

    Args:
        user_query: Raw user query string.

    Returns:
        ``True`` when the query clearly asks for a structure download.
    """
    normalized = (
        unicodedata.normalize("NFKD", user_query or "")
        .encode("ascii", "ignore")
        .decode("utf-8")
        .lower()
    )
    if not normalized.strip():
        return False

    literature_terms = any(
        term in normalized
        for term in [
            "pubmed",
            "pmid",
            "pmcid",
            "doi",
            "paper",
            "papers",
            "literature",
            "citation",
            "citations",
            "reference",
            "references",
            "abstract",
            "evidence",
        ]
    )
    if literature_terms:
        return False

    structure_terms = any(
        term in normalized
        for term in ["alphafold", "pdb", "structure", "3d structure", "mmcif", "cif", "coordinates"]
    )
    action_terms = any(
        term in normalized
        for term in ["download", "fetch", "retrieve", "get", "show", "open"]
    )
    target_terms = any(
        term in normalized
        for term in [
            "protein", "tp53", "p53", "egfr", "wnk", "kinase", "receptor",
        ]
    )
    explicit_structure_identifier = bool(re.search(r"\b(AF-[A-Z0-9-]+|[0-9][A-Za-z][A-Za-z0-9]{2})\b", normalized))
    return (structure_terms or explicit_structure_identifier) and (action_terms or target_terms)


# ── PDB structure ranking ────────────────────────────────────────────

_METHOD_SCORES: Dict[str, float] = {
    "X-RAY DIFFRACTION": 1.0,
    "ELECTRON MICROSCOPY": 0.85,
    "CRYO-EM": 0.85,
    "SOLUTION NMR": 0.7,
    "NMR": 0.7,
    "ELECTRON CRYSTALLOGRAPHY": 0.6,
}


def rank_pdb_structures(
    structures: List[Dict[str, Any]],
    bridge_result: Any,
) -> List[Dict[str, Any]]:
    """Rank PDB structures by resolution, method, organism match, etc.

    Args:
        structures: PDB structure metadata dicts.
        bridge_result: Bridge result carrying ``extracted.organisms``.

    Returns:
        Copy of *structures* sorted best-first, each with ``rank_score``.
    """
    scored: List[Dict[str, Any]] = []
    for struct in structures:
        score = 0.0

        # Resolution (0-1, inverted — lower resolution → higher score)
        resolution = struct.get("resolution")
        if resolution and isinstance(resolution, (int, float)):
            resolution_score = max(0, min(1, 1.5 / (resolution + 0.1)))
            score += resolution_score * 0.4

        # Experimental method
        method = struct.get("method", "").upper()
        method_score = max(
            [v for k, v in _METHOD_SCORES.items() if k in method] or [0.5]
        )
        score += method_score * 0.3

        # Organism match
        organism = struct.get("organism", "").lower()
        try:
            query_organisms = [o.lower() for o in bridge_result.extracted.organisms]
        except (AttributeError, TypeError):
            query_organisms = []
        if query_organisms and any(qo in organism for qo in query_organisms):
            score += 0.2
        elif "homo sapiens" in organism or "human" in organism:
            score += 0.1

        # Quality indicators
        if struct.get("has_ligands"):
            score += 0.05
        if struct.get("r_factor") and struct["r_factor"] < 0.25:
            score += 0.05

        struct["rank_score"] = score
        scored.append(struct)

    return sorted(scored, key=lambda x: x.get("rank_score", 0), reverse=True)


# ── Structure artifact persistence ───────────────────────────────────

def persist_structure_artifacts(
    server_name: str,
    protein_hint: str,
    result_obj: Any,
    *,
    text_chunks_fn: Callable[[Any], List[str]],
    checkpoint_dir: str,
) -> List[str]:
    """Write downloaded structure chunks to disk.

    Args:
        server_name: MCP server name (e.g. ``"pdb"``).
        protein_hint: Short protein identifier for filenames.
        result_obj: Raw MCP result to extract text chunks from.
        text_chunks_fn: Callable to extract text chunks (e.g.
            ``extract_text_chunks_from_mcp``).
        checkpoint_dir: Root checkpoint directory path.

    Returns:
        List of written file paths.
    """
    chunks = text_chunks_fn(result_obj)
    if not chunks:
        return []

    out_dir = Path(checkpoint_dir).resolve() / "structures" / server_name
    out_dir.mkdir(parents=True, exist_ok=True)

    written: List[str] = []
    for idx, text in enumerate(chunks):
        if not isinstance(text, str) or len(text.strip()) < 20:
            continue
        t = text.lstrip()
        ext = "pdb"
        if t.startswith("data_") or "_atom_site" in t or t.startswith("loop_"):
            ext = "cif"
        elif t.startswith("ATOM") or t.startswith("HEADER"):
            ext = "pdb"

        path = out_dir / f"{protein_hint}_{idx}.{ext}"
        path.write_text(text, encoding="utf-8")
        written.append(str(path))

    return written


# ── Attachment factory ───────────────────────────────────────────────

def make_attachment(
    file_path: str,
    description: str,
    *,
    attachment_cls: Type[Any],
) -> Any:
    """Create an ``Attachment`` instance from a file path.

    Args:
        file_path: Path to the structure file.
        description: Human-readable description.
        attachment_cls: The ``Attachment`` dataclass/model to instantiate.

    Returns:
        An ``Attachment`` instance.
    """
    p = Path(file_path).resolve()
    suffix = p.suffix.lstrip(".") or "dat"
    size = p.stat().st_size if p.exists() else None
    return attachment_cls(
        file_path=str(p),
        file_type=suffix,
        description=description,
        size_bytes=size,
    )

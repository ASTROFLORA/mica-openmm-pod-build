"""Parse MCP result objects to extract identifiers.

Phase 3 extraction from agentic_driver.py.
All functions are pure — no driver state required.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

_UNIPROT_PATTERN = re.compile(
    r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5})\b",
    re.IGNORECASE,
)


def extract_text_chunks_from_mcp(result_obj: Any) -> List[str]:
    """Extract text content items from an MCP result object.

    Handles both dict-based and attribute-based result objects.

    Returns:
        List of text strings found in the result.
    """
    if result_obj is None:
        return []

    # Dict-style result
    if isinstance(result_obj, dict):
        content = result_obj.get("content")
        if isinstance(content, list):
            chunks: List[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
            return chunks

    # Attribute-style result (MCP SDK objects)
    content = getattr(result_obj, "content", None)
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                chunks.append(text)
        return chunks

    # Last resort: stringify
    try:
        s = str(result_obj)
        return [s] if s else []
    except Exception:
        return []


def extract_uniprot_accessions_from_mcp_result(result_obj: Any) -> List[str]:
    """Parse MCP result content to extract UniProt accession IDs.

    Handles both structured JSON payloads (with ``results`` / ``primaryAccession``
    keys) and raw text (regex fallback).

    Returns:
        Sorted, deduplicated list of uppercase UniProt accessions.
    """
    accessions: List[str] = []

    for chunk in extract_text_chunks_from_mcp(result_obj):
        if not isinstance(chunk, str) or not chunk.strip():
            continue

        parsed_payloads: List[Any] = []
        try:
            parsed_payloads.append(json.loads(chunk))
        except Exception:
            pass

        if not parsed_payloads:
            parsed_payloads.append(chunk)

        for payload in parsed_payloads:
            if isinstance(payload, dict):
                results = payload.get("results")
                if isinstance(results, list):
                    for item in results:
                        if not isinstance(item, dict):
                            continue
                        accession = str(
                            item.get("primaryAccession") or item.get("accession") or ""
                        ).strip().upper()
                        if _UNIPROT_PATTERN.fullmatch(accession):
                            accessions.append(accession)
                accession = str(
                    payload.get("primaryAccession") or payload.get("accession") or ""
                ).strip().upper()
                if _UNIPROT_PATTERN.fullmatch(accession):
                    accessions.append(accession)
            elif isinstance(payload, str):
                accessions.extend(
                    match.upper() for match in _UNIPROT_PATTERN.findall(payload)
                )

    return sorted(set(accessions))


def extract_pdb_ids_from_search_result(result_obj: Any) -> List[str]:
    """Parse MCP PDB search results to extract PDB IDs.

    Expects JSON content with ``result_set`` containing ``identifier`` fields.

    Returns:
        Sorted, deduplicated list of uppercase PDB IDs.
    """
    pdb_ids: List[str] = []
    _pdb_full = re.compile(r"[0-9][A-Z0-9]{3}")

    for chunk in extract_text_chunks_from_mcp(result_obj):
        if not isinstance(chunk, str) or not chunk.strip():
            continue
        try:
            payload = json.loads(chunk)
        except Exception:
            continue
        result_set = payload.get("result_set") if isinstance(payload, dict) else None
        if not isinstance(result_set, list):
            continue
        for item in result_set:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("identifier") or "").strip().upper()
            if _pdb_full.fullmatch(identifier):
                pdb_ids.append(identifier)

    return sorted(set(pdb_ids))

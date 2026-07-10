from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Optional


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000.0, 3)


def best_available_literature_text(paper: Dict[str, Any]) -> str:
    """Resolve the best text payload from full text -> sections -> abstract."""
    full_text = str(paper.get("full_text") or "").strip()
    if full_text:
        return full_text

    metadata = dict(paper.get("metadata") or {})
    parts: List[str] = []
    for section in list(metadata.get("sections") or []):
        if not isinstance(section, dict):
            continue
        text = str(section.get("text") or section.get("content") or section.get("body") or "").strip()
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)

    return f"{paper.get('title', '')}. {paper.get('abstract', '')}".strip(". ")


async def build_canonical_literature_bundle(
    *,
    query: str,
    preset: str,
    user_id: str,
    session_id: str,
    backend: str,
    papers: List[Dict[str, Any]],
    requested_sources: List[str],
    attempted_sources: List[str],
    failed_sources: List[str],
    source_counts: Dict[str, int],
    provider_health: Dict[str, Any],
    retrieval_policy: Dict[str, Any],
    acquisition_envelope: Dict[str, Any],
    generation_notes: Optional[List[str]] = None,
    synthesis_hint: str = "",
) -> Dict[str, Any]:
    """Build canonical artifact bundle + manifest + artifact list in one place."""
    from mica.infrastructure.literature.literature_artifact_bundle import (
        build_literature_artifact_manifest,
        build_rich_literature_artifact_bundle,
    )

    total_started = perf_counter()
    publication_assembly_started = perf_counter()
    bundle = await build_rich_literature_artifact_bundle(
        query=query,
        preset=preset,
        user_id=user_id,
        session_id=session_id,
        backend=backend,
        papers=list(papers or []),
        requested_sources=list(requested_sources or []),
        attempted_sources=list(attempted_sources or []),
        failed_sources=list(failed_sources or []),
        source_counts=dict(source_counts or {}),
        provider_health=dict(provider_health or {}),
        retrieval_policy=dict(retrieval_policy or {}),
        acquisition_envelope=dict(acquisition_envelope or {}),
        generation_notes=list(generation_notes or []),
        synthesis_hint=synthesis_hint,
    )
    publication_assembly_ms = _elapsed_ms(publication_assembly_started)
    artifact_bundle = bundle.model_dump()

    manifest_started = perf_counter()
    artifact_manifest = build_literature_artifact_manifest(artifact_bundle)
    manifest_generation_ms = _elapsed_ms(manifest_started)

    artifact_list_started = perf_counter()
    artifact_list = list(artifact_manifest.get("artifacts") or [])
    artifact_list_projection_ms = _elapsed_ms(artifact_list_started)
    runtime_profile = {
        "stages": {
            "publication_assembly_ms": publication_assembly_ms,
            "manifest_generation_ms": manifest_generation_ms,
            "artifact_list_projection_ms": artifact_list_projection_ms,
            "total_ms": _elapsed_ms(total_started),
        },
        "publication_assembly": dict(artifact_bundle.get("generation_profile") or {}),
        "artifact_count": len(artifact_list),
    }
    return {
        "artifact_bundle": artifact_bundle,
        "artifact_manifest": artifact_manifest,
        "artifact_list": artifact_list,
        "runtime_profile": runtime_profile,
    }

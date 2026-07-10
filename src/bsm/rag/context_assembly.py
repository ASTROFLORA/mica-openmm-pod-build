"""Context assembly helpers for BSM RAG orchestrator."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def assemble_context(
    snippets: List[str],
    embed_fn: Callable[[str], Any],
    drift_reference: Optional[Any] = None,
    *,
    max_fragments: int = 3,
) -> Dict[str, Any]:
    """Construct a lightweight context payload for downstream LLM calls."""

    selected = snippets[:max_fragments]
    embeddings = []
    for snippet in selected:
        try:
            embeddings.append(embed_fn(snippet))
        except Exception:
            embeddings.append(None)

    drift_score = None
    if drift_reference is not None and hasattr(drift_reference, "score"):
        try:
            drift_score = [drift_reference.score(vec) for vec in embeddings if isinstance(vec, list)]
        except Exception:
            drift_score = None

    return {
        "fragments": selected,
        "count": len(selected),
        "embeddings": embeddings,
        "drift_scores": drift_score,
    }


__all__ = ["assemble_context"]

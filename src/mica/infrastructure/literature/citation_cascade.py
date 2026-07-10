from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class CascadeSeed:
    canonical_id: str
    paper_id: str
    provider: str
    score: float
    openalex_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "paper_id": self.paper_id,
            "provider": self.provider,
            "score": self.score,
            "openalex_id": self.openalex_id,
        }


@dataclass(frozen=True)
class CascadeFrontier:
    semantic_scholar_ids: List[str]
    openalex_ids: List[str]

    def to_dict(self) -> Dict[str, List[str]]:
        return {
            "semantic_scholar_ids": list(self.semantic_scholar_ids),
            "openalex_ids": list(self.openalex_ids),
        }


@dataclass(frozen=True)
class CascadePolicy:
    seed_limit: int = 12
    max_frontier: int = 64
    max_depth: int = 1


def select_cascade_seeds(
    papers: Sequence[Dict[str, Any]],
    *,
    policy: Optional[CascadePolicy] = None,
) -> List[CascadeSeed]:
    active_policy = policy or CascadePolicy()
    ranked = sorted(
        [paper for paper in papers if isinstance(paper, dict)],
        key=lambda paper: (
            float(((paper.get("metadata") or {}).get("reranker_scores") or {}).get("light_first_pass") or 0.0),
            int(paper.get("citationCount") or 0),
        ),
        reverse=True,
    )
    seeds: List[CascadeSeed] = []
    for paper in ranked[: max(1, int(active_policy.seed_limit))]:
        metadata = dict(paper.get("metadata") or {})
        seed = CascadeSeed(
            canonical_id=str(paper.get("canonical_id") or "").strip(),
            paper_id=str(paper.get("paperId") or "").strip(),
            provider=str(paper.get("provider") or paper.get("source") or "").strip().lower(),
            score=float((metadata.get("reranker_scores") or {}).get("light_first_pass") or 0.0),
            openalex_id=str(metadata.get("openalex_id") or "").strip(),
        )
        if seed.canonical_id or seed.paper_id:
            seeds.append(seed)
    return seeds


def build_initial_frontier(
    seeds: Sequence[CascadeSeed],
    *,
    policy: Optional[CascadePolicy] = None,
) -> CascadeFrontier:
    active_policy = policy or CascadePolicy()
    semantic_scholar_ids: List[str] = []
    openalex_ids: List[str] = []
    seen_s2: set[str] = set()
    seen_openalex: set[str] = set()
    for seed in seeds:
        if seed.provider == "semantic_scholar" and seed.paper_id and seed.paper_id not in seen_s2:
            seen_s2.add(seed.paper_id)
            semantic_scholar_ids.append(seed.paper_id)
        if seed.provider == "openalex" and seed.openalex_id and seed.openalex_id not in seen_openalex:
            seen_openalex.add(seed.openalex_id)
            openalex_ids.append(seed.openalex_id)
    return CascadeFrontier(
        semantic_scholar_ids=semantic_scholar_ids[: active_policy.max_frontier],
        openalex_ids=openalex_ids[: active_policy.max_frontier],
    )


def extract_openalex_reference_ids(paper: Dict[str, Any]) -> List[str]:
    metadata = dict((paper or {}).get("metadata") or {})
    openalex_data = dict(metadata.get("openalex_data") or {})
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in openalex_data.get("referenced_works") or []:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value.rsplit("/", 1)[-1])
    return ordered


def summarize_cascade(
    *,
    seeds: Sequence[CascadeSeed],
    policy: CascadePolicy,
    depth_reached: int,
    citation_graph: Dict[str, List[str]],
) -> Dict[str, Any]:
    return {
        "seed_count": len(seeds),
        "seeds": [seed.to_dict() for seed in seeds],
        "policy": {
            "seed_limit": int(policy.seed_limit),
            "max_frontier": int(policy.max_frontier),
            "max_depth": int(policy.max_depth),
        },
        "depth_reached": int(depth_reached),
        "graph_edges": sum(len(list(values or [])) for values in citation_graph.values()),
    }
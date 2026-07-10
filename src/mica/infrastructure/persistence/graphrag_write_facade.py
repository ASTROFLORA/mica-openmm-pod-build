"""GraphRAG write facade — thin normalization adapter for pipelines.

Timeline, SOTA, artifact, and KB pipelines project their domain objects
onto the existing :class:`TimescaleGraphRAGStore` through this facade.

All graph writes are schema-normalised here so pipeline code never touches
raw store payloads directly.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Protocol


class GraphRAGStoreProtocol(Protocol):
    """Subset of TimescaleGraphRAGStore used by the facade."""

    async def upsert_node(self, **kw: Any) -> None: ...
    async def upsert_edge(self, payload: dict[str, Any], **kw: Any) -> None: ...
    async def upsert_fact(self, payload: dict[str, Any], **kw: Any) -> None: ...


class GraphRAGWriteError(RuntimeError):
    """Raised when a multi-step graph write partially succeeds before failing."""

    def __init__(
        self,
        message: str,
        *,
        failed_step: str,
        partial_writes: List[Dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.failed_step = failed_step
        self.partial_writes = list(partial_writes)


class GraphRAGWriteFacade:
    """Pipeline-friendly write surface on top of the raw store."""

    def __init__(self, store: GraphRAGStoreProtocol) -> None:
        self._store = store

    async def write_paper_record(
        self,
        paper: Any,
        *,
        run_id: str = "",
        kb_id: str = "",
        user_id: str = "",
        session_id: str = "",
        ingest_facts: bool = True,
    ) -> int:
        """Persist a paper and its provenance through the canonical facade."""
        title = str(getattr(paper, "title", "") or getattr(paper, "paper_id", "") or "unknown")[:200]
        doi = str(getattr(paper, "doi", "") or "")
        arxiv_id = str(getattr(paper, "arxiv_id", "") or "")
        paper_id = str(getattr(paper, "paper_id", "") or "")
        abstract = str(getattr(paper, "abstract_snippet", "") or getattr(paper, "abstract", "") or "")
        authors = [str(author).strip() for author in (getattr(paper, "authors", []) or []) if str(author).strip()]
        properties: Dict[str, Any] = {}
        if getattr(paper, "year", None):
            properties["year"] = getattr(paper, "year")
        if getattr(paper, "journal", None):
            properties["journal"] = getattr(paper, "journal")

        external_ids: Dict[str, Any] = {}
        if doi:
            external_ids["doi"] = doi
        if arxiv_id:
            external_ids["arxiv_id"] = arxiv_id
        if paper_id:
            external_ids["paper_id"] = paper_id

        await self._store.upsert_node(
            canonical_name=title,
            node_type="paper",
            aliases=[doi] if doi else [],
            description=abstract[:500] or None,
            embedding=None,
            external_ids=external_ids,
            properties=properties,
            source_doi=[doi] if doi else [],
        )
        count = 1

        writes: List[Dict[str, Any]] = []
        for index, raw_author in enumerate(authors[:20], start=1):
            author_key = " ".join(raw_author.lower().split())
            await self._store.upsert_node(
                canonical_name=author_key,
                node_type="author",
                aliases=[raw_author] if raw_author != author_key else [],
                description=None,
                embedding=None,
                external_ids={},
                properties={},
                source_doi=[],
            )
            count += 1
            writes.append(
                {
                    "kind": "edge",
                    "step": f"paper_authored_by_edge_{index}",
                    "payload": {
                        "source_node": title,
                        "source_type": "paper",
                        "target_node": author_key,
                        "target_type": "author",
                        "relationship": "AUTHORED_BY",
                        "confidence": 1.0,
                        "source_doi": doi or None,
                        "metadata": {
                            "run_id": run_id,
                            "kb_id": kb_id,
                            "scope": {"user_id": user_id, "session_id": session_id},
                        },
                    },
                }
            )

        if ingest_facts and abstract.strip():
            writes.append(
                {
                    "kind": "fact",
                    "step": "paper_abstract_fact",
                    "payload": {
                        "content": abstract.strip(),
                        "fact_type": "finding",
                        "topic": title,
                        "entities": authors[:5],
                        "confidence": 1.0,
                        "source_doi": doi or None,
                        "metadata": {
                            "paper_id": paper_id,
                            "run_id": run_id,
                            "kb_id": kb_id,
                            "scope": {"user_id": user_id, "session_id": session_id},
                        },
                    },
                }
            )

        await self._execute_write_plan(writes, user_id=user_id)
        return count + len(writes)

    async def write_papers(
        self,
        papers: List[Any],
        *,
        run_id: str = "",
        kb_id: str = "",
        user_id: str = "",
        session_id: str = "",
        ingest_facts: bool = True,
    ) -> int:
        total = 0
        for paper in papers:
            total += await self.write_paper_record(
                paper,
                run_id=run_id,
                kb_id=kb_id,
                user_id=user_id,
                session_id=session_id,
                ingest_facts=ingest_facts,
            )
        return total

    async def write_overview_artifact(
        self,
        overview: Dict[str, Any],
        *,
        artifact_id: str,
        run_id: str = "",
        kb_id: str = "",
        user_id: str = "",
        timeline_artifact_id: str = "",
        sota_artifact_id: str = "",
    ) -> int:
        summary = str(overview.get("summary") or overview.get("abstract_synthesis") or overview.get("topic") or "knowledge overview")
        supporting_papers = [str(pid) for pid in (overview.get("supporting_paper_ids") or []) if str(pid)]
        writes: List[Dict[str, Any]] = [
            {
                "kind": "fact",
                "step": "overview_fact",
                "payload": {
                    "content": summary,
                    "fact_type": "knowledge_overview",
                    "topic": str(overview.get("topic") or "knowledge overview"),
                    "entities": [str(ch) for ch in (overview.get("chapter_keys") or []) if str(ch)],
                    "confidence": float(overview.get("confidence", 0.8) or 0.8),
                    "source_doi": supporting_papers[0] if supporting_papers else None,
                    "metadata": {
                        "artifact_id": artifact_id,
                        "run_id": run_id,
                        "kb_id": kb_id,
                        "timeline_artifact_id": timeline_artifact_id,
                        "sota_artifact_id": sota_artifact_id,
                        "scope": {"user_id": user_id},
                    },
                },
            }
        ]
        if timeline_artifact_id:
            writes.append(
                {
                    "kind": "edge",
                    "step": "overview_informed_by_timeline_edge",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": timeline_artifact_id,
                        "target_type": "artifact",
                        "relationship": "informed_by_timeline",
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )
        if sota_artifact_id:
            writes.append(
                {
                    "kind": "edge",
                    "step": "overview_informed_by_sota_edge",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": sota_artifact_id,
                        "target_type": "artifact",
                        "relationship": "informed_by_sota",
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )
        for index, paper_id in enumerate(supporting_papers, start=1):
            writes.append(
                {
                    "kind": "edge",
                    "step": f"overview_supported_by_edge_{index}",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": paper_id,
                        "target_type": "paper",
                        "relationship": "supported_by",
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )
        await self._execute_write_plan(writes, user_id=user_id)
        return len(writes)

    # ── Timeline writes ──────────────────────────────────────────

    async def write_timeline_event(
        self,
        event: Dict[str, Any],
        *,
        run_id: str = "",
        kb_id: str = "",
        user_id: str = "",
    ) -> None:
        """Persist a timeline event as a graph fact + edges to entities."""
        event_id = event.get("event_id", "")
        entity_scope = event.get("entity_scope", "")
        entity_ids = list(event.get("entity_ids") or [])
        if entity_scope and entity_scope not in entity_ids:
            entity_ids.append(entity_scope)
        supporting_papers = list(event.get("supporting_papers") or [])

        writes = [
            {
                "kind": "fact",
                "step": "timeline_fact",
                "payload": {
                    "content": event.get("narrative", event.get("headline", "")),
                    "fact_type": "timeline_event",
                    "topic": event.get("headline", ""),
                    "entities": entity_ids,
                    "confidence": event.get("confidence", 0.8),
                    "source_doi": (supporting_papers or [None])[0],
                    "metadata": {
                        "event_id": event_id,
                        "event_year": event.get("event_year"),
                        "event_type": event.get("event_type"),
                        "supporting_papers": supporting_papers,
                        "run_id": run_id,
                        "kb_id": kb_id,
                        "scope": {"user_id": user_id},
                    },
                },
            }
        ]

        if entity_scope:
            writes.append(
                {
                    "kind": "edge",
                    "step": "timeline_entity_edge",
                    "payload": {
                        "source_node": event_id,
                        "source_type": "timeline_event",
                        "target_node": entity_scope,
                        "target_type": "entity",
                        "relationship": "concerns_entity",
                        "confidence": event.get("confidence", 0.8),
                        "details": event.get("headline", ""),
                        "metadata": {
                            "run_id": run_id,
                            "kb_id": kb_id,
                            "scope": {"user_id": user_id},
                        },
                    },
                }
            )

        for index, paper_id in enumerate(supporting_papers, start=1):
            writes.append(
                {
                    "kind": "edge",
                    "step": f"timeline_supported_by_edge_{index}",
                    "payload": {
                        "source_node": event_id,
                        "source_type": "timeline_event",
                        "target_node": paper_id,
                        "target_type": "paper",
                        "relationship": "supported_by",
                        "confidence": event.get("confidence", 0.8),
                        "metadata": {
                            "run_id": run_id,
                            "scope": {"user_id": user_id},
                        },
                    },
                }
            )

        await self._execute_write_plan(writes, user_id=user_id)

    # ── SOTA writes ──────────────────────────────────────────────

    async def write_sota_claim(
        self,
        claim: Dict[str, Any],
        *,
        run_id: str = "",
        kb_id: str = "",
        user_id: str = "",
    ) -> None:
        """Persist a SOTA frontier claim as a graph fact + edges."""
        claim_id = claim.get("claim_id", "")
        writes = [
            {
                "kind": "fact",
                "step": "sota_fact",
                "payload": {
                    "content": claim.get("claim_text", ""),
                    "fact_type": f"sota_{claim.get('claim_type', 'frontier')}",
                    "topic": claim.get("topic", ""),
                    "entities": claim.get("entity_ids", []),
                    "confidence": claim.get("confidence", 0.7),
                    "source_doi": (claim.get("supporting_papers") or [None])[0],
                    "metadata": {
                        "claim_id": claim_id,
                        "claim_type": claim.get("claim_type"),
                        "frontier_score": claim.get("frontier_score"),
                        "freshness_score": claim.get("freshness_score"),
                        "run_id": run_id,
                        "kb_id": kb_id,
                        "scope": {"user_id": user_id},
                    },
                },
            }
        ]

        for index, paper_id in enumerate(claim.get("supporting_papers", []), start=1):
            writes.append(
                {
                    "kind": "edge",
                    "step": f"sota_supported_by_edge_{index}",
                    "payload": {
                        "source_node": claim_id,
                        "source_type": "sota_claim",
                        "target_node": paper_id,
                        "target_type": "paper",
                        "relationship": "supported_by",
                        "metadata": {
                            "run_id": run_id,
                            "scope": {"user_id": user_id},
                        },
                    },
                }
            )

        for index, paper_id in enumerate(claim.get("contradicting_papers", []), start=1):
            writes.append(
                {
                    "kind": "edge",
                    "step": f"sota_contradicted_by_edge_{index}",
                    "payload": {
                        "source_node": claim_id,
                        "source_type": "sota_claim",
                        "target_node": paper_id,
                        "target_type": "paper",
                        "relationship": "contradicted_by",
                        "metadata": {
                            "run_id": run_id,
                            "scope": {"user_id": user_id},
                        },
                    },
                }
            )

        await self._execute_write_plan(writes, user_id=user_id)

    # ── Artifact lineage writes ──────────────────────────────────

    async def write_artifact_lineage(
        self,
        artifact_id: str,
        *,
        run_id: str = "",
        kb_id: str = "",
        kind: str = "",
        parent_ids: Optional[List[str]] = None,
        entity_ids: Optional[List[str]] = None,
        user_id: str = "",
    ) -> None:
        """Record artifact provenance in the graph."""
        writes: List[Dict[str, Any]] = []

        # Artifact → run
        if run_id:
            writes.append(
                {
                    "kind": "edge",
                    "step": "artifact_generated_in_run_edge",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": run_id,
                        "target_type": "run",
                        "relationship": "generated_in_run",
                        "details": kind,
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )

        # Artifact → KB
        if kb_id:
            writes.append(
                {
                    "kind": "edge",
                    "step": "artifact_belongs_to_kb_edge",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": kb_id,
                        "target_type": "knowledge_base",
                        "relationship": "belongs_to_kb",
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )

        # Lineage parents
        for index, pid in enumerate(parent_ids or [], start=1):
            writes.append(
                {
                    "kind": "edge",
                    "step": f"artifact_derived_from_edge_{index}",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": pid,
                        "target_type": "artifact",
                        "relationship": "derived_from",
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )

        # Entity links
        for index, eid in enumerate(entity_ids or [], start=1):
            writes.append(
                {
                    "kind": "edge",
                    "step": f"artifact_concerns_entity_edge_{index}",
                    "payload": {
                        "source_node": artifact_id,
                        "source_type": "artifact",
                        "target_node": eid,
                        "target_type": "entity",
                        "relationship": "concerns_entity",
                        "metadata": {"scope": {"user_id": user_id}},
                    },
                }
            )

        await self._execute_write_plan(writes, user_id=user_id)

    async def _execute_write_plan(
        self,
        writes: List[Dict[str, Any]],
        *,
        user_id: str = "",
    ) -> None:
        completed: List[Dict[str, Any]] = []
        for write in writes:
            try:
                if write["kind"] == "fact":
                    await self._store.upsert_fact(write["payload"], user_id=user_id)
                else:
                    await self._store.upsert_edge(write["payload"], user_id=user_id)
            except Exception as exc:
                raise GraphRAGWriteError(
                    f"GraphRAG fan-out failed at {write['step']}",
                    failed_step=write["step"],
                    partial_writes=completed,
                ) from exc
            completed.append(
                {
                    "kind": write["kind"],
                    "step": write["step"],
                    "payload": json.loads(json.dumps(write["payload"], default=str)),
                }
            )

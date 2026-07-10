"""Per-paper GraphRAG ingestion into TimescaleDB.

Converts ``PaperRecord`` objects (and optionally SOTA frontier claims) into the
MICA knowledge graph stored in ``atom_graph_nodes``, ``atom_graph_edges``, and
``atom_facts``.

Schema produced per paper
--------------------------
* **Node** — ``(paper_title, type=paper)``  with doi/arxiv in ``external_ids``
* **Node** — ``(author_name, type=author)`` per distinct author
* **Edge** — ``(paper) --AUTHORED_BY--> (author)``   for each author
* **Fact** — abstract snippet tagged ``fact_type="finding"``

Schema produced per SOTA claim
-------------------------------
* **Fact** — claim text, ``fact_type="sota_claim"``, ``topic=claim_type``
* **Node** — ``(claim_summary, type=claim)``
* **Edge** — ``(claim) --SUPPORTED_BY--> (paper)``  for each supporting paper

Usage::

    from mica.infrastructure.persistence.timescale_graphrag_store import (
        TimescaleGraphRAGStore,
    )
    from mica.infrastructure.persistence.paper_knowledge_base import (
        PaperKnowledgeBase,
    )

    store = TimescaleGraphRAGStore()
    kb = PaperKnowledgeBase(store)
    n = await kb.ingest_papers(paper_records, user_id="user-123", session_id="sess-1")
    print(f"Ingested {n} items")
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from .graphrag_write_facade import GraphRAGWriteFacade

log = logging.getLogger(__name__)


def _canon_author(name: str) -> str:
    """Normalise author name to a stable canonical form (lowercased, stripped)."""
    return re.sub(r"\s+", " ", name.strip()).lower()


def _paper_node_name(paper) -> str:
    """Return the canonical node name for a paper."""
    title = (getattr(paper, "title", "") or "").strip()
    return title[:200] if title else (getattr(paper, "paper_id", "") or "unknown")


def _paper_doi(paper) -> Optional[str]:
    return getattr(paper, "doi", None) or None


class PaperKnowledgeBase:
    """Ingest paper records into the TimescaleDB GraphRAG knowledge base.

    Parameters
    ----------
    store:
        An initialised (or uninitialised — we call ``initialize()`` lazily)
        ``TimescaleGraphRAGStore`` instance.
    """

    def __init__(self, store) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ingest_papers(
        self,
        paper_records: List,
        user_id: str,
        session_id: str = "",
        *,
        ingest_facts: bool = True,
    ) -> int:
        """Ingest a list of ``PaperRecord`` objects into the knowledge graph.

        Returns the total count of nodes + edges + facts inserted.
        """
        if not paper_records:
            return 0

        await self._store.initialize()
        facade = GraphRAGWriteFacade(self._store)
        total = await facade.write_papers(
            paper_records,
            user_id=user_id,
            session_id=session_id,
            ingest_facts=ingest_facts,
        )

        log.info("PaperKnowledgeBase: ingested %d items for user=%s", total, user_id)
        return total

    async def ingest_sota_claims(
        self,
        claims: List,
        paper_lookup: Dict[str, object],
        user_id: str,
        session_id: str = "",
    ) -> int:
        """Ingest SOTA frontier claims with supporting-paper edges.

        Parameters
        ----------
        claims:
            List of objects with ``.claim_text`` / ``.claim_type`` / ``.supporting_papers``
            attributes (``SOTAFrontierClaim`` or compatible duck type).
        paper_lookup:
            Mapping ``{paper_id: PaperRecord}`` for linking claims to papers.
        """
        if not claims:
            return 0

        await self._store.initialize()

        total = 0
        facade = GraphRAGWriteFacade(self._store)
        for claim in claims:
            try:
                supporting = [str(pid) for pid in list(getattr(claim, "supporting_papers", []) or [])]
                contradicting = [str(pid) for pid in list(getattr(claim, "contradicting_papers", []) or [])]
                await facade.write_sota_claim(
                    {
                        "claim_id": getattr(claim, "claim_id", "") or (getattr(claim, "claim_text", "") or "")[:80],
                        "claim_text": getattr(claim, "claim_text", "") or "",
                        "claim_type": getattr(claim, "claim_type", "unknown") or "unknown",
                        "topic": getattr(claim, "claim_type", "unknown") or "unknown",
                        "entity_ids": [],
                        "supporting_papers": supporting,
                        "contradicting_papers": contradicting,
                    },
                    user_id=user_id,
                    run_id=session_id,
                )
                total += 1 + len(supporting) + len(contradicting)
            except Exception as exc:
                log.warning("Failed to ingest SOTA claim: %s", exc)

        log.info("PaperKnowledgeBase: ingested %d SOTA claim items for user=%s", total, user_id)
        return total

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ingest_one_paper(
        self,
        paper,
        *,
        user_id: str,
        session_id: str,
        ingest_facts: bool,
        seen_authors: set[str],
    ) -> int:
        """Insert node + author edges + abstract fact for one paper."""
        count = 0
        node_name = _paper_node_name(paper)
        doi = _paper_doi(paper)
        arxiv_id = getattr(paper, "arxiv_id", None)
        year = getattr(paper, "year", None)
        journal = getattr(paper, "journal", None)

        external_ids: dict = {}
        if doi:
            external_ids["doi"] = doi
        if arxiv_id:
            external_ids["arxiv_id"] = arxiv_id
        paper_id = getattr(paper, "paper_id", "") or ""
        if paper_id:
            external_ids["paper_id"] = paper_id

        properties: dict = {}
        if year:
            properties["year"] = year
        if journal:
            properties["journal"] = journal

        source_dois = [doi] if doi else []

        # Paper node
        await self._store.upsert_node(
            canonical_name=node_name,
            node_type="paper",
            aliases=[doi] if doi else [],
            description=(getattr(paper, "abstract_snippet", "") or "")[:500],
            embedding=None,
            external_ids=external_ids,
            properties=properties,
            source_doi=source_dois,
        )
        count += 1

        # Author nodes + AUTHORED_BY edges
        authors: List[str] = getattr(paper, "authors", []) or []
        for raw_author in authors[:20]:  # cap to avoid runaway nodes
            if not raw_author:
                continue
            author_key = _canon_author(raw_author)
            if author_key not in seen_authors:
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
                seen_authors.add(author_key)
                count += 1

            await self._store.insert_edge(
                source_node=node_name,
                source_type="paper",
                target_node=author_key,
                target_type="author",
                relationship="AUTHORED_BY",
                details=None,
                confidence=1.0,
                source_doi=doi,
                user_id=user_id,
                session_id=session_id or None,
            )
            count += 1

        # Abstract fact
        if ingest_facts:
            snippet = (getattr(paper, "abstract_snippet", "") or "").strip()
            if snippet:
                entities = _author_entities(authors[:5])
                await self._store.insert_fact(
                    content=snippet,
                    fact_type="finding",
                    topic=None,
                    entities=entities,
                    source_doi=doi,
                    confidence=1.0,
                    embedding=None,
                    user_id=user_id,
                )
                count += 1

        return count

    async def _ingest_one_claim(
        self,
        claim,
        *,
        paper_lookup: Dict[str, object],
        user_id: str,
        session_id: str,
    ) -> int:
        """Insert claim fact + claim node + SUPPORTED_BY edges."""
        count = 0

        claim_text: str = (getattr(claim, "claim_text", "") or "").strip()
        claim_type: str = str(getattr(claim, "claim_type", "") or "unknown")
        supporting: List = list(getattr(claim, "supporting_papers", []) or [])

        if not claim_text:
            return 0

        # Claim fact
        await self._store.insert_fact(
            content=claim_text,
            fact_type="sota_claim",
            topic=claim_type,
            entities=[],
            source_doi=None,
            confidence=1.0,
            embedding=None,
            user_id=user_id,
        )
        count += 1

        # Claim node (first 160 chars as name)
        claim_node = claim_text[:160]
        await self._store.upsert_node(
            canonical_name=claim_node,
            node_type="claim",
            aliases=[],
            description=claim_text,
            embedding=None,
            external_ids={"claim_type": claim_type},
            properties={},
            source_doi=[],
        )
        count += 1

        # SUPPORTED_BY edges to paper nodes
        for pid in supporting[:10]:
            paper = paper_lookup.get(str(pid))
            if paper is None:
                continue
            paper_node = _paper_node_name(paper)
            doi = _paper_doi(paper)
            await self._store.insert_edge(
                source_node=claim_node,
                source_type="claim",
                target_node=paper_node,
                target_type="paper",
                relationship="SUPPORTED_BY",
                details=None,
                confidence=1.0,
                source_doi=doi,
                user_id=user_id,
                session_id=session_id or None,
            )
            count += 1

        return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _author_entities(authors: List[str]) -> List[str]:
    return [a.strip() for a in authors if a and a.strip()][:5]

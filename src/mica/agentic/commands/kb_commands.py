"""kb_commands.py — Hardened Command Kernel handlers for KB, GraphRAG, and Artifact actions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from mica.agentic.command_kernel import _KernelBlocked
from mica.api_v1.services.product_link_service import (
    ProductLinkNotFoundError,
    ProductLinkServiceError,
    attach_artifact_to_study_for_user,
    attach_artifact_to_working_set_for_user,
)
from mica.sdk.command_contracts import BackendCommandEnvelope
from mica.infrastructure.persistence.kb_postgres_store import KBPostgresStore
from mica.pipelines.knowledge_fabric.kb_service import KBService
from mica.pipelines.knowledge_fabric.contracts import KBType, OwnerScope

logger = logging.getLogger(__name__)


def _non_durable_capability_warning(reason: Optional[str], *, label: str) -> list[str]:
    if not reason:
        return [f"{label} is running without durable backing."]
    return [f"{label} is running without durable backing: {reason}"]


def _envelope_user_id(envelope: BackendCommandEnvelope) -> Optional[str]:
    identity = envelope.request_identity or {}
    for key in ("user_id", "sub", "subject"):
        value = str(identity.get(key) or "").strip()
        if value:
            return value
    return None


async def _get_kb_service_with_backing() -> tuple[KBService, str, str, str, Optional[str]]:
    """Initialize KBPostgresStore and return KBService along with dynamic backing status."""
    store = KBPostgresStore()
    try:
        await store.initialize()
        # Test connection
        async with store._pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return KBService(store=store), "durable", "durable", "active", None
    except Exception as e:
        logger.warning("KBPostgresStore initialization failed (falling back to in-memory): %s", e)
        return KBService(store=None), "in_memory", "non_durable", "degraded", f"Database connection failed: {e}"


async def _get_graphrag_store_with_backing():
    """Initialize TimescaleGraphRAGStore and return store along with dynamic backing status."""
    from mica.infrastructure.persistence.timescale_graphrag_store import TimescaleGraphRAGStore
    store = TimescaleGraphRAGStore()
    try:
        await store.initialize()
        if not store._pool:
            raise RuntimeError("Pool is None")
        async with store._pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return store, "durable", "durable", "active", None
    except Exception as e:
        logger.warning("TimescaleGraphRAGStore connection failed: %s", e)
        return store, "unavailable", "non_durable", "degraded", f"TimescaleDB connection failed: {e}"


async def _get_neon_status() -> tuple[str, str, str, Optional[str]]:
    """Check Neon database connection status."""
    from mica.infrastructure.persistence.pg_async import choose_neon_database_url
    import asyncpg
    dsn = choose_neon_database_url()
    if not dsn:
        return "unavailable", "non_durable", "degraded", "Neon database URL is not configured."
    try:
        conn = await asyncpg.connect(dsn, timeout=3.0)
        await conn.close()
        return "durable", "durable", "active", None
    except Exception as e:
        return "unavailable", "non_durable", "degraded", f"Neon connection failed: {e}"


def _serialize_kb(kb: Any) -> Dict[str, Any]:
    if hasattr(kb, "to_dict"):
        return dict(kb.to_dict())
    return dict(kb or {})


async def kb_list(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """List KBs through the durable KB service authority."""
    public_only = bool(args.get("public"))
    include_global = bool(args.get("include_global")) or public_only
    workspace_id = str(
        args.get("workspace_id")
        or envelope.workspace_id
        or ""
    ).strip()
    team_id = str(args.get("team_id") or "").strip()
    user_id = _envelope_user_id(envelope) or ""

    service, backing, durability, trust_state, reason = await _get_kb_service_with_backing()

    dedup: Dict[str, Any] = {}
    if public_only:
        visible = await service.list_global_kbs()
    else:
        owned = await service.list_kbs(
            owner_id=user_id,
            workspace_id=workspace_id,
            include_global=False,
        )
        for kb in owned:
            dedup[kb.kb_id] = kb

        if team_id:
            team_kbs = await service.list_kbs(
                owner_id=team_id,
                workspace_id=workspace_id,
                include_global=False,
            )
            for kb in team_kbs:
                dedup[kb.kb_id] = kb

        if workspace_id:
            workspace_kbs = await service.list_kbs(
                owner_id="",
                workspace_id=workspace_id,
                include_global=False,
            )
            for kb in workspace_kbs:
                dedup[kb.kb_id] = kb

        if include_global:
            for kb in await service.list_global_kbs():
                dedup[kb.kb_id] = kb

        visible = list(dedup.values())

    kb_payloads = [_serialize_kb(kb) for kb in visible]
    if backing == "durable":
        status_val = "completed"
        warnings: list[str] = []
    else:
        status_val = "completed_non_durable"
        warnings = _non_durable_capability_warning(reason, label="kb.list")

    visibility = "public" if public_only else "caller_scope"
    return {
        "summary": f"kb.list returned {len(kb_payloads)} knowledge base(s) from {visibility}.",
        "result": {
            "knowledge_bases": kb_payloads,
            "count": len(kb_payloads),
            "public": public_only,
            "include_global": include_global,
            "workspace_id": workspace_id or None,
            "team_id": team_id or None,
        },
        "artifact_refs": [f"kb://{payload.get('kb_id')}" for payload in kb_payloads if str(payload.get("kb_id") or "").strip()],
        "usd": 0.0,
        "tool_calls": 1,
        "status": status_val,
        "runtime_backing": backing,
        "durability": durability,
        "trust_state": trust_state,
        "degraded_reason": reason,
        "warnings": warnings,
    }


async def kb_create(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Create a new knowledge base."""
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("kb.create requires a non-empty name.")

    workspace_id = envelope.workspace_id or str(args.get("workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("kb.create requires a non-empty workspace_id.")

    kb_type_str = str(args.get("kb_type") or "query").upper()
    kb_type = KBType[kb_type_str] if kb_type_str in KBType.__members__ else KBType.QUERY

    owner_scope_str = str(args.get("owner_scope") or "workspace").upper()
    owner_scope = OwnerScope[owner_scope_str] if owner_scope_str in OwnerScope.__members__ else OwnerScope.WORKSPACE

    service, backing, durability, trust_state, reason = await _get_kb_service_with_backing()
    kb = await service.create_kb(
        name=name,
        kb_type=kb_type,
        owner_scope=owner_scope,
        owner_id=envelope.request_identity.get("user_id") if envelope.request_identity else "agent",
        workspace_id=workspace_id,
        canonical_query=str(args.get("canonical_query") or "").strip(),
        target_entities=list(args.get("target_entities") or []),
        target_topics=list(args.get("target_topics") or []),
    )

    status_val = "completed" if backing == "durable" else "degraded"
    if backing == "durable":
        warnings: list[str] = []
    else:
        status_val = "completed_non_durable"
        warnings = _non_durable_capability_warning(reason, label="kb.create")

    kb_dict = {
        "kb_id": kb.kb_id,
        "name": kb.name,
        "kb_type": kb.kb_type.value,
        "owner_scope": kb.owner_scope.value,
        "workspace_id": kb.workspace_id,
        "status": kb.status.value,
    }

    return {
        "summary": f"Created Knowledge Base '{name}' with ID {kb.kb_id}",
        "result": kb_dict,
        "artifact_refs": [f"kb://{kb.kb_id}"],
        "usd": 0.0,
        "tool_calls": 1,
        "status": status_val,
        "runtime_backing": backing,
        "durability": durability,
        "trust_state": trust_state,
        "degraded_reason": reason,
        "warnings": warnings,
    }


async def kb_ingest(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Ingest documents into a knowledge base.

    KB-UNBLOCK-004: wired to BiolinkBERT Modal (1024-dim L2-normed embeddings)
    and NeonKBStore (Tier-1 durable). No longer raises _KernelBlocked.

    Degradation modes:
    - biolinkbert_unavailable: Modal unreachable → degraded_reason, 0 ingested.
    - neon_unreachable: Neon pooler unreachable → degraded_reason, 0 ingested.
    - partial: per-document errors captured in `per_document_errors[]`.
    """
    kb_id = str(args.get("kb_id") or "").strip()
    if not kb_id:
        raise ValueError("kb.ingest requires a non-empty kb_id.")

    documents = args.get("documents") or []
    if not documents:
        raise ValueError("kb.ingest requires one or more documents.")

    owner_id = (
        envelope.request_identity.get("user_id") if envelope.request_identity else ""
    ) or "anonymous"

    # ── Step 1: embed all documents via BiolinkBERT Modal ─────────────────
    texts = [str((doc or {}).get("content") or "") for doc in documents]
    provenance_inputs = [
        {
            "source_url": (doc or {}).get("source_url"),
            "source_doi": (doc or {}).get("source_doi"),
            "content_hash": (doc or {}).get("content_hash")
            or f"sha256:auto-{i}-{uuid.uuid4().hex[:8]}",
        }
        for i, doc in enumerate(documents)
    ]
    try:
        from mica.kb.embeddings import embed_texts_modal, BiolinkBertUnavailable
        vectors, embed_meta = await asyncio.to_thread(embed_texts_modal, texts)
        embed_receipt_urn = f"urn:mica:embed:{uuid.uuid4().hex}"
        degraded_reason_embed: Optional[str] = None
    except Exception as exc:  # BiolinkBertUnavailable OR unexpected
        logger.warning("kb.ingest BiolinkBERT Modal failed: %s", str(exc)[:160])
        return {
            "summary": f"kb.ingest failed: biolinkbert_unavailable ({type(exc).__name__})",
            "result": {
                "kb_id": kb_id,
                "ingested_count": 0,
                "skipped_duplicates": 0,
                "provenance_receipts": [],
                "embed_receipts": [],
                "degraded_reason": "biolinkbert_unavailable",
            },
            "usd": 0.0,
            "tool_calls": 1,
            "status": "degraded",
            "runtime_backing": "modal",
            "durability": "durable",
            "trust_state": "degraded",
            "degraded_reason": f"biolinkbert_unavailable: {type(exc).__name__}: {str(exc)[:160]}",
            "warnings": [f"BiolinkBERT Modal unavailable: {str(exc)[:160]}"],
        }

    # ── Step 2: upsert each (content, embedding, provenance) into Neon ────
    try:
        from mica.kb.store import NeonKBStore, NeonUnreachable
        store = NeonKBStore()
    except Exception as exc:
        logger.warning("kb.ingest NeonKBStore init failed: %s", str(exc)[:160])
        return {
            "summary": "kb.ingest failed: neon_unreachable",
            "result": {
                "kb_id": kb_id,
                "ingested_count": 0,
                "skipped_duplicates": 0,
                "provenance_receipts": [],
                "embed_receipts": [embed_receipt_urn],
                "degraded_reason": "neon_unreachable",
            },
            "usd": 0.0,
            "tool_calls": 1,
            "status": "degraded",
            "runtime_backing": "neon",
            "durability": "durable",
            "trust_state": "degraded",
            "degraded_reason": f"neon_unreachable: {type(exc).__name__}: {str(exc)[:160]}",
            "warnings": [f"Neon unavailable: {str(exc)[:160]}"],
        }

    provenance_receipts: list[str] = []
    per_doc_receipts: list[dict] = []
    ingested_count = 0
    skipped_duplicates = 0
    per_doc_errors: list[dict] = []

    for i, (doc, vec, prov_in) in enumerate(zip(documents, vectors, provenance_inputs)):
        doc_prov_urn = f"urn:mica:provenance:{uuid.uuid4().hex}"
        mudo_id = (doc or {}).get("mudo_id") or str(uuid.uuid4())
        branch_id = (doc or {}).get("branch_id") or str(uuid.uuid4())
        try:
            receipt = store.upsert_document(
                kb_id=kb_id,
                mudo_id=mudo_id,
                branch_id=branch_id,
                content=texts[i],
                content_hash=prov_in["content_hash"],
                embedding=vec,
                model_id=embed_meta.get("model_id", "biolinkbert-large"),
                embed_receipt_urn=embed_receipt_urn,
                provenance_receipt_urn=doc_prov_urn,
                source_url=prov_in["source_url"],
                source_doi=prov_in["source_doi"],
                retrieval_method=(doc or {}).get("retrieval_method", "seed_source"),
            )
            provenance_receipts.append(doc_prov_urn)
            per_doc_receipts.append(receipt)
            if receipt.get("inserted"):
                ingested_count += 1
            else:
                skipped_duplicates += 1
        except NeonUnreachable as exc:
            logger.warning("kb.ingest Neon upsert failed for doc %d: %s", i, str(exc)[:160])
            per_doc_errors.append({"doc_index": i, "error": str(exc)[:200]})
            if not per_doc_errors:
                return {
                    "summary": "kb.ingest failed: neon_unreachable during upsert",
                    "result": {
                        "kb_id": kb_id,
                        "ingested_count": ingested_count,
                        "skipped_duplicates": skipped_duplicates,
                        "provenance_receipts": provenance_receipts,
                        "embed_receipts": [embed_receipt_urn],
                        "degraded_reason": "neon_unreachable",
                    },
                    "usd": 0.0,
                    "tool_calls": 1,
                    "status": "degraded",
                    "runtime_backing": "neon",
                    "durability": "durable",
                    "trust_state": "degraded",
                    "degraded_reason": f"neon_unreachable: {str(exc)[:160]}",
                    "warnings": [f"Neon failed during upsert: {str(exc)[:160]}"],
                }
        except Exception as exc:
            logger.warning("kb.ingest doc %d failed: %s", i, str(exc)[:160])
            per_doc_errors.append({"doc_index": i, "error": str(exc)[:200]})

    degraded_reason_final: Optional[str] = None
    status_final = "completed"
    trust_final = "active"
    if per_doc_errors and ingested_count == 0:
        degraded_reason_final = "neon_unreachable_during_upsert"
        status_final = "degraded"
        trust_final = "degraded"
    elif per_doc_errors:
        degraded_reason_final = "partial_ingest"
        status_final = "degraded"
        trust_final = "active"

    return {
        "summary": (
            f"kb.ingest ingested {ingested_count} doc(s), "
            f"{skipped_duplicates} duplicate(s), {len(per_doc_errors)} error(s)."
        ),
        "result": {
            "kb_id": kb_id,
            "ingested_count": ingested_count,
            "skipped_duplicates": skipped_duplicates,
            "provenance_receipts": provenance_receipts,
            "embed_receipts": [embed_receipt_urn],
            "per_document_errors": per_doc_errors,
            "embedding_dim": embed_meta.get("embedding_dim", 1024),
            "modal_function": embed_meta.get("modal_function", ""),
            "modal_app": embed_meta.get("modal_app", ""),
            "modal_latency_ms": embed_meta.get("latency_ms", 0.0),
            "degraded_reason": degraded_reason_final,
            "owner_id": owner_id,
        },
        "usd": 0.0,
        "tool_calls": 1,
        "status": status_final,
        "runtime_backing": "neon",
        "durability": "durable",
        "trust_state": trust_final,
        "degraded_reason": degraded_reason_final,
        "warnings": [f"partial ingest ({len(per_doc_errors)} errors)"] if per_doc_errors else [],
    }


async def kb_semantic_search(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Perform semantic search on a knowledge base.

    KB-UNBLOCK-005: wired to BiolinkBERT Modal (1024-dim) + NeonKBStore
    cosine top-k. No longer raises _KernelBlocked.
    """
    kb_id = str(args.get("kb_id") or "").strip()
    query = str(args.get("query") or "").strip()
    if not kb_id or not query:
        raise ValueError("kb.semantic_search requires non-empty kb_id and query.")

    top_k = int(args.get("top_k") or 10)
    min_similarity = float(args.get("min_similarity") or 0.0)

    # ── Step 1: embed the query via BiolinkBERT Modal ─────────────────────
    try:
        from mica.kb.embeddings import embed_texts_modal
        vectors, embed_meta = await asyncio.to_thread(embed_texts_modal, [query])
        query_embedding = vectors[0]
        embed_receipt_urn = f"urn:mica:embed:{uuid.uuid4().hex}"
    except Exception as exc:
        logger.warning("kb.semantic_search BiolinkBERT Modal failed: %s", str(exc)[:160])
        return {
            "summary": "kb.semantic_search failed: biolinkbert_unavailable",
            "result": {
                "kb_id": kb_id,
                "query": query,
                "hits": [],
                "degraded_reason": "biolinkbert_unavailable",
            },
            "usd": 0.0,
            "tool_calls": 1,
            "status": "degraded",
            "runtime_backing": "modal",
            "durability": "durable",
            "trust_state": "degraded",
            "degraded_reason": f"biolinkbert_unavailable: {type(exc).__name__}: {str(exc)[:160]}",
            "warnings": [f"BiolinkBERT Modal unavailable: {str(exc)[:160]}"],
        }

    # ── Step 2: cosine top-k over Neon KB embeddings ──────────────────────
    try:
        from mica.kb.store import NeonKBStore, NeonUnreachable
        store = NeonKBStore()
        hits = store.semantic_search(
            kb_id=kb_id,
            query_embedding=query_embedding,
            top_k=top_k,
            min_similarity=min_similarity,
        )
    except NeonUnreachable as exc:
        logger.warning("kb.semantic_search Neon unavailable: %s", str(exc)[:160])
        return {
            "summary": "kb.semantic_search failed: neon_unreachable",
            "result": {
                "kb_id": kb_id,
                "query": query,
                "hits": [],
                "degraded_reason": "neon_unreachable",
            },
            "usd": 0.0,
            "tool_calls": 1,
            "status": "degraded",
            "runtime_backing": "neon",
            "durability": "durable",
            "trust_state": "degraded",
            "degraded_reason": f"neon_unreachable: {type(exc).__name__}: {str(exc)[:160]}",
            "warnings": [f"Neon unavailable: {str(exc)[:160]}"],
        }
    except Exception as exc:
        logger.warning("kb.semantic_search unexpected: %s", str(exc)[:160])
        return {
            "summary": f"kb.semantic_search failed: {type(exc).__name__}",
            "result": {
                "kb_id": kb_id,
                "query": query,
                "hits": [],
                "degraded_reason": f"unexpected:{type(exc).__name__}",
            },
            "usd": 0.0,
            "tool_calls": 1,
            "status": "degraded",
            "runtime_backing": "neon",
            "durability": "durable",
            "trust_state": "degraded",
            "degraded_reason": str(exc)[:200],
            "warnings": [str(exc)[:160]],
        }

    formatted_hits = [
        {
            "doc_id": str(h.doc_id),
            "content_preview": h.content[:500],
            "similarity": float(h.similarity),
            "source_url": h.source_url,
            "source_doi": h.source_doi,
            "content_hash": h.content_hash,
            "provenance_receipt_urn": h.provenance_receipt_urn,
            "embed_receipt_urn": h.embed_receipt_urn,
        }
        for h in hits
    ]
    return {
        "summary": f"kb.semantic_search returned {len(formatted_hits)} hit(s) for query '{query[:60]}'.",
        "result": {
            "kb_id": kb_id,
            "query": query,
            "hits": formatted_hits,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "embed_receipt_urn": embed_receipt_urn,
            "embedding_dim": embed_meta.get("embedding_dim", 1024),
            "modal_function": embed_meta.get("modal_function", ""),
            "modal_app": embed_meta.get("modal_app", ""),
            "modal_latency_ms": embed_meta.get("latency_ms", 0.0),
            "degraded_reason": None,
        },
        "usd": 0.0,
        "tool_calls": 1,
        "status": "completed",
        "runtime_backing": "neon",
        "durability": "durable",
        "trust_state": "active",
        "degraded_reason": None,
        "warnings": [],
    }


async def graphrag_query(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Query GraphRAG with hybrid retrieval."""
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("graphrag.query requires a non-empty query.")

    store, backing, durability, trust_state, reason = await _get_graphrag_store_with_backing()
    results = []
    user_id = _envelope_user_id(envelope)
    workspace_id = envelope.workspace_id or str(args.get("workspace_id") or "").strip() or None
    session_id = envelope.session_id or str(args.get("session_id") or "").strip() or None
    global_only = bool(args.get("global_only", False))

    if backing == "durable":
        try:
            edges = await store.search_edges_hybrid(
                query_text=query,
                query_embedding=None,
                limit=10,
                user_id=user_id,
                session_id=session_id,
                workspace_id=workspace_id,
                global_only=global_only,
            )
            results = [
                {
                    "source": e.source_node,
                    "relationship": e.relationship,
                    "target": e.target_node,
                    "confidence": e.confidence,
                }
                for e in edges
            ]
        except Exception as e:
            backing = "unavailable"
            durability = "non_durable"
            trust_state = "degraded"
            reason = f"GraphRAG query execution failed: {e}"

    status_val = "completed" if backing == "durable" else "degraded"
    warnings = [f"GraphRAG query executed with degraded backing: {reason}"] if backing != "durable" else []

    return {
        "summary": f"GraphRAG query '{query}' matched {len(results)} edges.",
        "result": {"query": query, "edges": results},
        "usd": 0.0,
        "tool_calls": 1,
        "status": status_val,
        "runtime_backing": backing,
        "durability": durability,
        "trust_state": trust_state,
        "degraded_reason": reason,
        "warnings": warnings,
    }


async def graphrag_hop1(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Perform a hop-1 traversal from seed nodes in GraphRAG."""
    seed_nodes = list(args.get("seed_nodes") or [])
    if not seed_nodes:
        raise ValueError("graphrag.hop1 requires non-empty seed_nodes.")

    store, backing, durability, trust_state, reason = await _get_graphrag_store_with_backing()
    results = []

    if backing == "durable":
        try:
            edges = await store.hop1_edges(node_names=seed_nodes, limit=30)
            results = [
                {
                    "source": e.source_node,
                    "relationship": e.relationship,
                    "target": e.target_node,
                    "confidence": e.confidence,
                }
                for e in edges
            ]
        except Exception as e:
            backing = "unavailable"
            durability = "non_durable"
            trust_state = "degraded"
            reason = f"GraphRAG hop1 execution failed: {e}"

    status_val = "completed" if backing == "durable" else "degraded"
    warnings = [f"GraphRAG hop1 executed with degraded backing: {reason}"] if backing != "durable" else []

    return {
        "summary": f"Traversed hop-1 from {len(seed_nodes)} seed nodes, found {len(results)} edges.",
        "result": {"seed_nodes": seed_nodes, "edges": results},
        "usd": 0.0,
        "tool_calls": 1,
        "status": status_val,
        "runtime_backing": backing,
        "durability": durability,
        "trust_state": trust_state,
        "degraded_reason": reason,
        "warnings": warnings,
    }


async def graphrag_export_decision_subgraph(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Export a bounded decision subgraph with provenance."""
    store, backing, durability, trust_state, reason = await _get_graphrag_store_with_backing()
    raise _KernelBlocked(
        code="preview_only_command",
        message=(
            "graphrag.export_decision_subgraph does not have a canonical export implementation "
            "on the Command Kernel gateway yet."
        ),
        details={
            "command": "graphrag.export_decision_subgraph",
            "runtime_backing": backing,
            "durability": durability,
            "trust_state": "preview" if backing == "durable" else trust_state,
            "degraded_reason": reason,
        },
    )


async def artifact_attach_to_study(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Attach an artifact to a study."""
    artifact_id = str(args.get("artifact_id") or "").strip()
    study_id = envelope.study_id or str(args.get("study_id") or "").strip()
    if not artifact_id or not study_id:
        raise ValueError("artifact.attach_to_study requires non-empty artifact_id and study_id.")

    backing, durability, trust_state, reason = await _get_neon_status()
    if backing != "durable":
        raise _KernelBlocked(
            code="artifact_authority_degraded",
            message="artifact.attach_to_study requires durable product-surface authority.",
            details={
                "command": "artifact.attach_to_study",
                "study_id": study_id,
                "artifact_id": artifact_id,
                "runtime_backing": backing,
                "durability": durability,
                "trust_state": trust_state,
                "degraded_reason": reason,
            },
        )

    user_id = str(getattr(kernel, "_user_id", "") or getattr(kernel, "user_id", "") or _envelope_user_id(envelope) or "").strip()
    if not user_id:
        raise _KernelBlocked(
            code="missing_user_identity",
            message="artifact.attach_to_study requires a resolved user identity.",
            details={"command": "artifact.attach_to_study"},
        )

    try:
        result = await attach_artifact_to_study_for_user(
            user_id=user_id,
            study_id=study_id,
            artifact_id=artifact_id,
        )
    except ProductLinkNotFoundError as exc:
        raise _KernelBlocked(code=exc.code, message=exc.message, details=exc.details) from exc
    except ProductLinkServiceError as exc:
        raise _KernelBlocked(code=exc.code, message=exc.message, details=exc.details) from exc

    return {
        "summary": f"Attached artifact {artifact_id} to study {study_id}.",
        "result": result,
        "state_after": {
            "study_id": study_id,
            "artifact_id": artifact_id,
            "attachment_status": result["status"],
        },
        "status": "completed",
        "runtime_backing": backing,
        "durability": durability,
        "trust_state": trust_state,
        "degraded_reason": reason,
    }


async def artifact_attach_to_working_set(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Attach an artifact to a working set."""
    artifact_id = str(args.get("artifact_id") or "").strip()
    working_set_id = envelope.working_set_id or str(args.get("working_set_id") or "").strip()
    if not artifact_id or not working_set_id:
        raise ValueError("artifact.attach_to_working_set requires non-empty artifact_id and working_set_id.")

    backing, durability, trust_state, reason = await _get_neon_status()
    if backing != "durable":
        raise _KernelBlocked(
            code="artifact_authority_degraded",
            message="artifact.attach_to_working_set requires durable product-surface authority.",
            details={
                "command": "artifact.attach_to_working_set",
                "working_set_id": working_set_id,
                "artifact_id": artifact_id,
                "runtime_backing": backing,
                "durability": durability,
                "trust_state": trust_state,
                "degraded_reason": reason,
            },
        )

    user_id = str(getattr(kernel, "_user_id", "") or getattr(kernel, "user_id", "") or _envelope_user_id(envelope) or "").strip()
    if not user_id:
        raise _KernelBlocked(
            code="missing_user_identity",
            message="artifact.attach_to_working_set requires a resolved user identity.",
            details={"command": "artifact.attach_to_working_set"},
        )

    try:
        result = await attach_artifact_to_working_set_for_user(
            user_id=user_id,
            working_set_id=working_set_id,
            artifact_id=artifact_id,
            position=int(args.get("position") or 0),
            config=dict(args.get("config") or {}),
            artifact_ref_type=str(args.get("artifact_ref_type") or "").strip() or None,
        )
    except ProductLinkNotFoundError as exc:
        raise _KernelBlocked(code=exc.code, message=exc.message, details=exc.details) from exc
    except ProductLinkServiceError as exc:
        raise _KernelBlocked(code=exc.code, message=exc.message, details=exc.details) from exc

    return {
        "summary": f"Attached artifact {artifact_id} to working set {working_set_id}.",
        "result": result,
        "state_after": {
            "working_set_id": working_set_id,
            "artifact_id": artifact_id,
            "artifact_ref_type": result["artifact_ref_type"],
            "attachment_status": result["status"],
        },
        "status": "completed",
        "runtime_backing": backing,
        "durability": durability,
        "trust_state": trust_state,
        "degraded_reason": reason,
    }

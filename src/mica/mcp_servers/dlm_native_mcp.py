"""
📚 DLM Native MCP Server (In-Process / Zero Latency)

ARCHITECTURE:
- FastMCP In-Memory Server (NO subprocess)
- Exposes DLM pipeline modules as MCP tools
- Zero overhead — direct Python function calls
- Bridges gap between CLI-only dlm_tools.py and MCP ecosystem

TOOLS:
1. dlm_scan_protein_literature — Unified literature search for a protein
2. dlm_extract_entities — NER from text using DLMEncoder
3. dlm_map_entities — Map extracted entities to KB IDs (UniProt, PDB, HGNC)
4. dlm_check_api_status — Verify literature API connectivity
5. dlm_get_biological_context — Load LMP v4 XML context for a protein
6. dlm_search_semantic_scholar — Direct S2 search with rate limiting

USAGE:
    from mica.mcp_servers.dlm_native_mcp import dlm_native_server
    import fastmcp
    
    client = fastmcp.Client(dlm_native_server)
    result = await client.call_tool("dlm_scan_protein_literature", {
        "gene_name": "WNK1",
        "max_papers": 10,
    })
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

try:
    from fastmcp import FastMCP
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False

logger = logging.getLogger(__name__)

# Tool version for provenance
TOOL_VERSION = "1.0.0"

# ============================================================================
# CONDITIONAL IMPORTS
# ============================================================================

DLM_AVAILABLE = False
BRIDGE_AVAILABLE = False
FETCHER_AVAILABLE = False

try:
    from mica.memory.dlm.encoder import DLMEncoder
    DLM_AVAILABLE = True
except ImportError:
    logger.debug("DLMEncoder not available")

try:
    from mica.memory.dlm.entity_mapper import EntityMapper
    ENTITY_MAPPER_AVAILABLE = True
except ImportError:
    ENTITY_MAPPER_AVAILABLE = False

try:
    from mica.memory.dlm.batch_fetcher import BatchLiteratureFetcher
    FETCHER_AVAILABLE = True
except ImportError:
    logger.debug("BatchLiteratureFetcher not available")

try:
    from mica.drivers.dlm_lmp_bridge import DLMLMPBridge, get_bridge
    BRIDGE_AVAILABLE = True
except ImportError:
    logger.debug("DLMLMPBridge not available")


# ============================================================================
# INITIALIZE FASTMCP SERVER
# ============================================================================

if not FASTMCP_AVAILABLE:
    logger.error("fastmcp not installed. Run: pip install fastmcp")
    dlm_native_server = None
else:
    dlm_native_server = FastMCP("DLM-Literature-Native")

    # Singletons (lazy-initialized)
    _dlm_encoder = None
    _entity_mapper = None
    _batch_fetcher = None
    _bridge = None

    def _get_encoder():
        global _dlm_encoder
        if _dlm_encoder is None and DLM_AVAILABLE:
            _dlm_encoder = DLMEncoder()
        return _dlm_encoder

    def _get_mapper():
        global _entity_mapper
        if _entity_mapper is None and ENTITY_MAPPER_AVAILABLE:
            _entity_mapper = EntityMapper()
        return _entity_mapper

    def _get_fetcher():
        global _batch_fetcher
        if _batch_fetcher is None and FETCHER_AVAILABLE:
            _batch_fetcher = BatchLiteratureFetcher()
        return _batch_fetcher

    def _get_bridge():
        global _bridge
        if _bridge is None and BRIDGE_AVAILABLE:
            _bridge = get_bridge()
        return _bridge

    # ========================================================================
    # TOOL 1: Unified protein literature scan
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_scan_protein_literature(
        gene_name: Optional[str] = None,
        uniprot_id: Optional[str] = None,
        query: Optional[str] = None,
        max_papers: int = 10,
    ) -> Dict[str, Any]:
        """Search literature databases for papers about a protein.
        
        Queries Semantic Scholar with protein-specific keywords.
        At least one of gene_name, uniprot_id, or query must be provided.
        
        Args:
            gene_name: Gene name (e.g., "WNK1", "TP53")
            uniprot_id: UniProt accession (e.g., "Q9H4A3")
            query: Free-text search query
            max_papers: Maximum papers to return (default 10)
        
        Returns:
            Dict with papers found, total count, and metadata
        """
        t0 = time.time()
        
        # Build search query
        parts = []
        if gene_name:
            parts.append(gene_name)
        if uniprot_id:
            parts.append(uniprot_id)
        if query:
            parts.append(query)
        
        if not parts:
            return {
                "error": "Provide at least one of: gene_name, uniprot_id, query",
                "tool_version": TOOL_VERSION,
            }
        
        search_query = " ".join(parts)
        
        # Try enriching with LMP context
        bridge = _get_bridge()
        if bridge and bridge.enable_lmp_context:
            uid = uniprot_id
            if not uid and gene_name and bridge.preset_resolver:
                path = bridge.preset_resolver.resolve_by_gene_name(gene_name)
                if path:
                    ctx = bridge._load_context_from_path(path)
                    if ctx and ctx.keywords:
                        # Add top keywords to improve search
                        search_query += " " + " ".join(ctx.keywords[:3])
        
        # Search
        fetcher = _get_fetcher()
        if not fetcher:
            return {
                "error": "BatchLiteratureFetcher not available",
                "search_query": search_query,
                "tool_version": TOOL_VERSION,
            }
        
        try:
            papers = []
            if hasattr(fetcher, 'search_semantic_scholar_bulk'):
                raw = await fetcher.search_semantic_scholar_bulk(
                    query=search_query, max_papers=max_papers
                )
                for p in (raw or []):
                    if hasattr(p, '__dict__'):
                        papers.append({k: v for k, v in p.__dict__.items() if not k.startswith('_')})
                    elif isinstance(p, dict):
                        papers.append(p)
            
            return {
                "status": "SUCCESS",
                "search_query": search_query,
                "total_papers": len(papers),
                "papers": papers[:max_papers],
                "execution_time_ms": round((time.time() - t0) * 1000, 1),
                "tool_version": TOOL_VERSION,
            }
        except Exception as e:
            return {
                "status": "ERROR",
                "error": str(e),
                "search_query": search_query,
                "tool_version": TOOL_VERSION,
            }

    # ========================================================================
    # TOOL 2: Entity extraction from text
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_extract_entities(
        text: str,
        include_nesy_markers: bool = True,
        use_master_vocab: bool = False,
    ) -> Dict[str, Any]:
        """Extract biological entities (proteins, genes, PTMs, domains) from text.
        
        Uses DLMEncoder for NER with fallback to regex patterns.
        
        Args:
            text: Input text to analyze
            include_nesy_markers: Also extract NeSy grammar markers
            use_master_vocab: Use expanded master vocabulary (~360K terms from Pfam,
                              Reactome, GO) instead of base vocab (~1,050 terms).
                              Higher recall, more memory. (E1: V1 audit recommendation)
        
        Returns:
            Dict with extracted entities by category
        """
        t0 = time.time()
        
        bridge = _get_bridge()
        if bridge:
            try:
                # E1: support master vocab by creating a fresh encoder if requested
                if use_master_vocab and DLM_AVAILABLE:
                    from pathlib import Path as _Path
                    from mica.memory.dlm.config import DLMConfig
                    from mica.memory.dlm.encoder import DLMEncoder
                    master_path = _Path(__file__).parents[2] / "memory" / "dlm" / "dlm_config_master_v1.yaml"
                    if master_path.exists():
                        master_cfg = DLMConfig(config_path=master_path)
                        master_encoder = DLMEncoder(config=master_cfg)
                        encoded = master_encoder.encode(text)
                        entities = encoded.entities if hasattr(encoded, 'entities') else []
                        return {
                            "status": "SUCCESS",
                            "vocab": "master",
                            "entities": [
                                {"text": e.text if hasattr(e, 'text') else str(e),
                                 "type": e.entity_type if hasattr(e, 'entity_type') else "",
                                 "start": getattr(e, 'start', 0),
                                 "end": getattr(e, 'end', 0)}
                                for e in entities
                            ],
                            "total_entities": len(entities),
                            "execution_time_ms": round((time.time() - t0) * 1000, 1),
                            "tool_version": TOOL_VERSION,
                        }

                extracted = bridge._extract_entities(text)
                result = {
                    "status": "SUCCESS",
                    "vocab": "base",
                    "protein_names": extracted.protein_names,
                    "uniprot_ids": extracted.uniprot_ids,
                    "pdb_ids": extracted.pdb_ids,
                    "gene_names": extracted.gene_names,
                    "domains": extracted.domains,
                    "ptms": extracted.ptms,
                    "organisms": extracted.organisms,
                    "ligands": extracted.ligands,
                    "has_explicit_ids": extracted.has_explicit_ids(),
                }
                
                if include_nesy_markers:
                    result["nesy_markers"] = extracted.nesy_markers
                
                result["execution_time_ms"] = round((time.time() - t0) * 1000, 1)
                result["tool_version"] = TOOL_VERSION
                return result
            except Exception as e:
                return {"status": "ERROR", "error": str(e), "tool_version": TOOL_VERSION}
        
        return {
            "status": "ERROR",
            "error": "DLM-LMP Bridge not available",
            "tool_version": TOOL_VERSION,
        }

    # ========================================================================
    # TOOL 3: Entity mapping to knowledge bases
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_map_entities(
        entities: List[str],
        entity_type: str = "protein",
    ) -> Dict[str, Any]:
        """Map entity names to knowledge base identifiers.
        
        Maps protein/gene/disease/drug names to KB IDs:
        - Proteins → UniProt (e.g., TP53 → P04637)
        - Genes → HGNC (e.g., BRCA1 → HGNC:1100)
        - Diseases → MONDO via OLS4 API (E2: now implemented)
        - Drugs → DrugBank/ChEMBL via MyChemInfo (E2: now implemented)
        
        Args:
            entities: List of entity names to map
            entity_type: One of "protein", "gene", "disease", "drug"
        
        Returns:
            Dict with mappings and confidence scores
        """
        t0 = time.time()
        
        mapper = _get_mapper()
        if not mapper:
            return {
                "status": "ERROR",
                "error": "EntityMapper not available",
                "tool_version": TOOL_VERSION,
            }
        
        try:
            batch = [(e, entity_type) for e in entities]
            mappings = mapper.map_batch(batch)
            
            results = []
            for m in mappings:
                results.append({
                    "text": m.text,
                    "entity_type": m.entity_type,
                    "kb_id": m.kb_id,
                    "kb_source": m.kb_source,
                    "confidence": m.confidence,
                    "synonyms": m.synonyms if hasattr(m, 'synonyms') else [],
                    "mapped": m.is_mapped() if hasattr(m, 'is_mapped') else m.kb_id is not None,
                })
            
            return {
                "status": "SUCCESS",
                "mappings": results,
                "total": len(results),
                "mapped_count": sum(1 for r in results if r["mapped"]),
                "execution_time_ms": round((time.time() - t0) * 1000, 1),
                "tool_version": TOOL_VERSION,
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e), "tool_version": TOOL_VERSION}

    # ========================================================================
    # TOOL 4: API status check
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_check_api_status() -> Dict[str, Any]:
        """Check connectivity to literature APIs (Semantic Scholar, PubMed, bioRxiv).
        
        Returns:
            Dict with status of each API endpoint
        """
        import asyncio
        
        statuses = {
            "dlm_encoder": DLM_AVAILABLE,
            "entity_mapper": ENTITY_MAPPER_AVAILABLE,
            "batch_fetcher": FETCHER_AVAILABLE,
            "dlm_lmp_bridge": BRIDGE_AVAILABLE,
        }
        
        # Try to ping S2 (lightweight)
        fetcher = _get_fetcher()
        if fetcher:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        statuses["semantic_scholar_api"] = resp.status == 200
            except Exception:
                statuses["semantic_scholar_api"] = False
        
        # LMP context availability
        bridge = _get_bridge()
        if bridge and bridge.enable_lmp_context:
            proteins = bridge.get_available_preset_proteins()
            statuses["lmp_presets_available"] = len(proteins)
            statuses["lmp_preset_proteins"] = proteins[:10]
        
        statuses["tool_version"] = TOOL_VERSION
        return statuses

    # ========================================================================
    # TOOL 5: Get biological context from LMP preset
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_get_biological_context(
        uniprot_id: Optional[str] = None,
        gene_name: Optional[str] = None,
        include_full_comments: bool = True,
        max_tokens: int = 2000,
    ) -> Dict[str, Any]:
        """Load biological context from LMP v4 XML preset for a protein.
        
        Returns rich context including: function description, PTM info,
        subunit interactions, domain info, keywords, NeSy grammar markers,
        and suggested MCP tools.
        
        Args:
            uniprot_id: UniProt accession (e.g., "Q9H4A3")
            gene_name: Gene name (e.g., "WNK1")
            include_full_comments: Include all comment types (FUNCTION, PTM, etc.)
            max_tokens: Approximate token budget for system prompt generation
        
        Returns:
            Dict with biological context or error
        """
        t0 = time.time()
        
        bridge = _get_bridge()
        if not bridge or not bridge.enable_lmp_context:
            return {
                "status": "ERROR",
                "error": "LMP context not available",
                "tool_version": TOOL_VERSION,
            }
        
        ctx = None
        
        if uniprot_id:
            ctx = bridge.get_biological_context(uniprot_id)
        
        if not ctx and gene_name and bridge.preset_resolver:
            path = bridge.preset_resolver.resolve_by_gene_name(gene_name)
            if path:
                ctx = bridge._load_context_from_path(path)
        
        if not ctx:
            return {
                "status": "NOT_FOUND",
                "uniprot_id": uniprot_id,
                "gene_name": gene_name,
                "available_proteins": bridge.get_available_preset_proteins()[:20],
                "tool_version": TOOL_VERSION,
            }
        
        result = {
            "status": "SUCCESS",
            "uniprot_id": ctx.uniprot_id,
            "protein_name": ctx.protein_name,
            "gene_names": ctx.gene_names,
            "organism": ctx.organism,
            "keywords": ctx.keywords,
            "suggested_tools": ctx.suggest_tools(),
            "system_prompt": ctx.to_system_prompt(max_tokens=max_tokens),
        }
        
        if include_full_comments:
            result["comments"] = ctx.comments
        
        result["domains"] = [
            d.name if hasattr(d, 'name') else str(d)
            for d in getattr(ctx, 'domains', [])
        ]
        result["ptms"] = [
            p.description if hasattr(p, 'description') else str(p)
            for p in getattr(ctx, 'ptms', [])
        ]
        
        result["execution_time_ms"] = round((time.time() - t0) * 1000, 1)
        result["tool_version"] = TOOL_VERSION
        return result

    # ========================================================================
    # TOOL 6: Direct Semantic Scholar search
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_search_semantic_scholar(
        query: str,
        limit: int = 10,
        year_from: Optional[int] = None,
        fields_of_study: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Search Semantic Scholar for academic papers.
        
        Args:
            query: Search query text
            limit: Maximum results (default 10, max 100)
            year_from: Filter papers from this year onward
            fields_of_study: Filter by field (e.g., ["Biology", "Medicine"])
        
        Returns:
            Dict with papers, total count, and metadata
        """
        t0 = time.time()
        limit = min(limit, 100)
        
        fetcher = _get_fetcher()
        if not fetcher:
            return {
                "status": "ERROR",
                "error": "BatchLiteratureFetcher not available",
                "tool_version": TOOL_VERSION,
            }
        
        try:
            full_query = query
            if year_from:
                full_query += f" year:{year_from}-"
            
            papers = []
            if hasattr(fetcher, 'search_semantic_scholar_bulk'):
                raw = await fetcher.search_semantic_scholar_bulk(
                    query=full_query, max_papers=limit
                )
                for p in (raw or []):
                    if hasattr(p, '__dict__'):
                        papers.append({k: v for k, v in p.__dict__.items() if not k.startswith('_')})
                    elif isinstance(p, dict):
                        papers.append(p)
            
            return {
                "status": "SUCCESS",
                "query": query,
                "total_papers": len(papers),
                "papers": papers,
                "execution_time_ms": round((time.time() - t0) * 1000, 1),
                "tool_version": TOOL_VERSION,
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e), "tool_version": TOOL_VERSION}

    # ========================================================================
    # TOOL 7: Multi-source literature search (E4: fetch_papers_unified)
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_search_literature(
        query: str,
        sources: Optional[List[str]] = None,
        max_results: int = 20,
        extract_entities: bool = True,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Unified multi-source literature search via fetch_papers_unified().
        
        Searches Semantic Scholar, PubMed, and/or bioRxiv simultaneously
        using the unified parallel fetcher. Optionally runs DLM entity
        extraction on abstracts.
        
        This implements V1 audit recommendation E4:
        fetch_papers_unified() was implemented but never exposed as a tool.
        
        Args:
            query: Search query string
            sources: List of sources to search. Options: "semantic_scholar",
                     "pubmed", "biorxiv". Default: ["semantic_scholar"]
            max_results: Max results per source (default 20)
            extract_entities: Run DLM entity extraction on abstracts
            date_from: Start date for bioRxiv search (YYYY-MM-DD)
            date_to: End date for bioRxiv search (YYYY-MM-DD)
        
        Returns:
            Dict with papers by source, total count, and optional entity summary
        """
        t0 = time.time()
        if sources is None:
            sources = ["semantic_scholar"]
        
        fetcher = _get_fetcher()
        if not fetcher:
            return {
                "status": "ERROR",
                "error": "BatchLiteratureFetcher not available",
                "tool_version": TOOL_VERSION,
            }
        
        # Build fetch_papers_unified() queries dict
        queries: Dict[str, Dict[str, Any]] = {}
        if "semantic_scholar" in sources:
            queries["semantic_scholar"] = {"query": query, "max_papers": max_results}
        if "pubmed" in sources:
            queries["pubmed"] = {"query": query, "max_results": max_results}
        if "biorxiv" in sources or "medrxiv" in sources:
            biorxiv_params: Dict[str, Any] = {}
            if date_from:
                biorxiv_params["date_from"] = date_from
            if date_to:
                biorxiv_params["date_to"] = date_to
            if not biorxiv_params:
                from datetime import datetime, timedelta
                now = datetime.now()
                biorxiv_params["date_from"] = (now - timedelta(days=365)).strftime("%Y-%m-%d")
                biorxiv_params["date_to"] = now.strftime("%Y-%m-%d")
            queries["biorxiv"] = biorxiv_params
        
        try:
            results_by_source = await fetcher.fetch_papers_unified(queries)
            
            all_papers = []
            source_stats = {}
            for src, papers in results_by_source.items():
                src_papers = []
                for p in (papers or []):
                    if hasattr(p, '__dict__'):
                        src_papers.append({k: v for k, v in p.__dict__.items() if not k.startswith('_')})
                    elif isinstance(p, dict):
                        src_papers.append(p)
                source_stats[src] = len(src_papers)
                all_papers.extend(src_papers)
            
            result: Dict[str, Any] = {
                "status": "SUCCESS",
                "query": query,
                "sources_searched": sources,
                "total_papers": len(all_papers),
                "source_stats": source_stats,
                "papers": all_papers,
                "execution_time_ms": round((time.time() - t0) * 1000, 1),
                "tool_version": TOOL_VERSION,
            }
            
            # Optional entity extraction on abstracts
            if extract_entities and all_papers:
                bridge = _get_bridge()
                if bridge:
                    entity_summary: Dict[str, Any] = {
                        "proteins": set(), "genes": set(),
                        "diseases": set(), "drugs": set(),
                    }
                    for paper in all_papers[:10]:  # cap at 10 to avoid latency
                        abstract = paper.get("abstract", "")
                        if abstract:
                            try:
                                ext = bridge._extract_entities(abstract)
                                entity_summary["proteins"].update(ext.protein_names or [])
                                entity_summary["genes"].update(ext.gene_names or [])
                            except Exception:
                                pass
                    result["entity_summary"] = {
                        k: list(v)[:20] for k, v in entity_summary.items()
                    }
            
            return result
        except Exception as e:
            return {"status": "ERROR", "error": str(e), "tool_version": TOOL_VERSION}

    # ========================================================================
    # TOOL 8: Relation extraction (E3: surface polarity + certainty)
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_extract_relations(
        text: str,
        include_negated: bool = True,
        include_speculative: bool = True,
        resolve_entities: bool = False,
    ) -> Dict[str, Any]:
        """Extract biomedical relations from text with negation and speculation.
        
        Uses extractor_core's deterministic relation extractor to find
        subject-predicate-object triples. Surfaces polarity (positive/negative)
        and certainty (high/speculative) fields that were previously computed
        but discarded by all consumers.
        
        This implements V1 audit recommendation E3:
        negation/speculation data was computed but never surfaced.
        
        Supported predicates: binds, activates, inhibits, phosphorylates,
        ubiquitinates, acetylates, methylates, interacts_with, cleaves,
        regulates, upregulates, downregulates, localizes_to, and more.
        
        Args:
            text: Text to extract relations from (sentence or paragraph)
            include_negated: Include negated relations (polarity=negative)
            include_speculative: Include speculative relations (certainty=speculative)
            resolve_entities: Resolve entity names to UniProt/HGNC IDs
        
        Returns:
            Dict with relations list, polarity/certainty fields, statistics
        """
        t0 = time.time()
        
        try:
            from mica.memory.dlm.extractor_core import extract_relations as _extract_rels
        except ImportError:
            return {
                "status": "ERROR",
                "error": "extractor_core not available",
                "tool_version": TOOL_VERSION,
            }
        
        # Extract entities first (needed for relation extractor)
        bridge = _get_bridge()
        entities = []
        if bridge:
            try:
                ext = bridge._extract_entities(text)
                for name in (ext.protein_names or []):
                    entities.append({"text": name, "type": "protein", "start": 0, "end": len(name)})
                for name in (ext.gene_names or []):
                    entities.append({"text": name, "type": "gene", "start": 0, "end": len(name)})
            except Exception:
                pass
        
        # Run relation extraction per sentence
        import re as _re
        sentences = _re.split(r'(?<=[.!?])\s+', text.strip())
        
        all_relations = []
        total_sentences = 0
        offset = 0
        
        for sent in sentences:
            if len(sent) < 10:
                offset += len(sent) + 1
                continue
            total_sentences += 1
            try:
                rels = _extract_rels(sent, entities, offset=offset, accepted_plane_only=True)
                for r in rels:
                    if not include_negated and getattr(r, 'polarity', 'positive') == 'negative':
                        offset += len(sent) + 1
                        continue
                    if not include_speculative and getattr(r, 'certainty', 'high') in ('speculative', 'low'):
                        offset += len(sent) + 1
                        continue
                    all_relations.append({
                        "subject": r.subject_text,
                        "subject_type": r.subject_type,
                        "predicate": r.predicate_canonical,
                        "predicate_trigger": r.predicate_trigger,
                        "object": r.object_text,
                        "object_type": r.object_type,
                        "polarity": getattr(r, 'polarity', 'positive'),
                        "certainty": getattr(r, 'certainty', 'high'),
                        "sentence": sent,
                    })
            except Exception:
                pass
            offset += len(sent) + 1
        
        # Optional KB resolution
        if resolve_entities and all_relations:
            mapper = _get_mapper()
            if mapper:
                unique_ents = set()
                for rel in all_relations:
                    unique_ents.add((rel["subject"], rel["subject_type"]))
                    unique_ents.add((rel["object"], rel["object_type"]))
                mappings = {}
                for ent_text, ent_type in unique_ents:
                    try:
                        m = mapper.map_entity(ent_text, ent_type)
                        if m.is_mapped():
                            mappings[ent_text] = {"kb_id": m.kb_id, "source": m.kb_source}
                    except Exception:
                        pass
                for rel in all_relations:
                    rel["subject_kb"] = mappings.get(rel["subject"])
                    rel["object_kb"] = mappings.get(rel["object"])
        
        return {
            "status": "SUCCESS",
            "total_sentences": total_sentences,
            "total_relations": len(all_relations),
            "negated_count": sum(1 for r in all_relations if r.get("polarity") == "negative"),
            "speculative_count": sum(1 for r in all_relations if r.get("certainty") in ("speculative", "low")),
            "relations": all_relations,
            "execution_time_ms": round((time.time() - t0) * 1000, 1),
            "tool_version": TOOL_VERSION,
        }

    # ========================================================================
    # TOOL 9: Full NeSy document encoding (V1 C.3: dlm_encode_document)
    # ========================================================================

    @dlm_native_server.tool()
    async def dlm_encode_document(
        text: str,
        paper_id: str = "anonymous",
        include_encoded_text: bool = True,
        include_relations: bool = False,
    ) -> Dict[str, Any]:
        """Encode a scientific document into full NeSy (neuro-symbolic) markers.
        
        Runs the complete DLMEncoder pipeline: section detection, entity
        annotation, fact-type classification, and citation marking. Returns
        structured EncodedDocument with per-section analysis.
        
        Output includes: [SEC:*] section tags, {PROT:*} entity markers,
        (FIND:*) fact markers, +REF[] citation markers.
        
        This implements V1 audit tool C.3 (dlm_encode_document).
        
        Args:
            text: Full paper text (with section headers) or abstract
            paper_id: Paper identifier for provenance (DOI, PMID, etc.)
            include_encoded_text: Include NeSy-marked text in output
            include_relations: Also run relation extraction per section
        
        Returns:
            Dict with sections, entities, facts, encoded text, statistics
        """
        t0 = time.time()
        
        if not DLM_AVAILABLE:
            return {
                "status": "ERROR",
                "error": "DLMEncoder not available",
                "tool_version": TOOL_VERSION,
            }
        
        try:
            from mica.memory.dlm.encoder import DLMEncoder
            encoder = DLMEncoder()
            encoded = encoder.encode(text)
            
            sections = []
            for sec in getattr(encoded, 'sections', []):
                s = {
                    "type": getattr(sec, 'section_type', getattr(sec, 'type', '')),
                    "entity_count": len(getattr(sec, 'entities', [])),
                    "fact_types": getattr(sec, 'fact_types', []),
                }
                sections.append(s)
            
            entities = [
                {
                    "text": getattr(e, 'text', str(e)),
                    "type": getattr(e, 'entity_type', getattr(e, 'type', '')),
                    "start": getattr(e, 'start', 0),
                    "end": getattr(e, 'end', 0),
                }
                for e in getattr(encoded, 'entities', [])
            ]
            
            result: Dict[str, Any] = {
                "status": "SUCCESS",
                "paper_id": paper_id,
                "sections": sections,
                "entities": entities,
                "total_sections": len(sections),
                "total_entities": len(entities),
                "execution_time_ms": round((time.time() - t0) * 1000, 1),
                "tool_version": TOOL_VERSION,
            }
            
            if include_encoded_text and hasattr(encoded, 'annotated_text'):
                result["encoded_text"] = encoded.annotated_text
            
            if include_relations:
                from mica.memory.dlm.extractor_core import extract_relations as _extract_rels
                all_rels = []
                for sec in getattr(encoded, 'sections', []):
                    sec_text = getattr(sec, 'text', '')
                    sec_entities = [
                        {"text": getattr(e, 'text', str(e)),
                         "type": getattr(e, 'entity_type', ''),
                         "start": getattr(e, 'start', 0),
                         "end": getattr(e, 'end', 0)}
                        for e in getattr(sec, 'entities', [])
                    ]
                    if sec_text and sec_entities:
                        try:
                            rels = _extract_rels(sec_text, sec_entities)
                            for r in rels:
                                all_rels.append({
                                    "subject": r.subject_text,
                                    "predicate": r.predicate_canonical,
                                    "object": r.object_text,
                                    "polarity": getattr(r, 'polarity', 'positive'),
                                    "certainty": getattr(r, 'certainty', 'high'),
                                })
                        except Exception:
                            pass
                result["relations"] = all_rels
                result["total_relations"] = len(all_rels)
            
            return result
        except Exception as e:
            return {"status": "ERROR", "error": str(e), "tool_version": TOOL_VERSION}

# Public exports
__all__ = ["dlm_native_server"]
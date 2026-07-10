"""
UnifiedQueryEngine: Hybrid Query Fusion for BUDO GraphRAG
=========================================================

Combines Timescale (operational BM25/vector) + Neo4j (analytical graph)
using Reciprocal Rank Fusion (RRF) for best-of-both-worlds queries.

Architecture:
- Timescale: Fast BM25 + HNSW vector search (<10ms)
- Neo4j: Graph traversal + relationship-aware context
- RRF: Merge ranked lists with position-based scoring

Use Cases:
1. Hybrid search: "kinase domain" → BM25 + graph neighbors
2. Graph-enriched results: Add interacting proteins to search hits
3. Multi-modal fusion: Semantic + structural relevance

Author: Alex Rodriguez AI Lab
Date: January 21, 2026
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass
from collections import defaultdict

import asyncpg
from neo4j import AsyncGraphDatabase, AsyncDriver

from .budo_graph_writer import BudoGraphWriter
from .budo_neo4j_writer import BudoNeo4jWriter
from .pg_async import create_asyncpg_pool_for_database_url

logger = logging.getLogger(__name__)


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class SearchResult:
    """
    Unified search result combining Timescale + Neo4j data.
    
    Attributes:
        protein_id: Canonical protein name
        budo_id: BUDO V3 identifier
        score: Combined relevance score (0-1)
        rank: Final ranking position
        sources: Where result came from ('timescale', 'neo4j', 'both')
        timescale_score: Original BM25/vector score
        neo4j_score: Graph-based relevance score
        metadata: Additional context (domains, PTMs, neighbors)
    """
    protein_id: str
    budo_id: str
    score: float
    rank: int
    sources: Set[str]
    timescale_score: Optional[float] = None
    neo4j_score: Optional[float] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class QueryFilters:
    """
    Advanced query filters for hybrid search.
    
    Attributes:
        organism: Filter by organism (e.g., "Homo sapiens")
        has_ptm: Filter proteins with specific PTM type
        functional_state: Filter by state (active, inactive, unknown)
        min_sequence_length: Minimum sequence length
        max_sequence_length: Maximum sequence length
        has_domain: Filter proteins with specific domain type
        binds_ligand: Filter proteins binding specific ligand
        interacts_with: Filter proteins interacting with specific partner
    """
    organism: Optional[str] = None
    has_ptm: Optional[str] = None
    functional_state: Optional[str] = None
    min_sequence_length: Optional[int] = None
    max_sequence_length: Optional[int] = None
    has_domain: Optional[str] = None
    binds_ligand: Optional[str] = None
    interacts_with: Optional[str] = None


# ============================================================================
# RRF ALGORITHM
# ============================================================================

class ReciprocalRankFusion:
    """
    Reciprocal Rank Fusion (RRF) algorithm for merging ranked lists.
    
    Formula: score(d) = Σ 1 / (k + rank(d))
    where k=60 (default constant from Cormack et al.)
    
    References:
    - Cormack et al. (2009): "Reciprocal Rank Fusion outperforms 
      CombSUM and CombMNZ"
    - Used by Elasticsearch, Weaviate, Qdrant for hybrid search
    """
    
    def __init__(self, k: int = 60):
        """
        Initialize RRF with constant k.
        
        Args:
            k: RRF constant (default 60, standard value)
        """
        self.k = k
    
    def fuse(
        self, 
        timescale_results: List[Dict[str, Any]], 
        neo4j_results: List[Dict[str, Any]],
        timescale_weight: float = 1.0,
        neo4j_weight: float = 1.0
    ) -> List[SearchResult]:
        """
        Fuse two ranked lists using RRF with optional weighting.
        
        Args:
            timescale_results: Results from Timescale BM25/vector
            neo4j_results: Results from Neo4j graph traversal
            timescale_weight: Weight for Timescale scores (0-1)
            neo4j_weight: Weight for Neo4j scores (0-1)
        
        Returns:
            Merged and re-ranked list of SearchResult objects
        """
        # Build RRF scores
        rrf_scores: Dict[str, float] = defaultdict(float)
        sources: Dict[str, Set[str]] = defaultdict(set)
        original_scores: Dict[str, Dict[str, float]] = defaultdict(dict)
        metadata_map: Dict[str, Dict[str, Any]] = {}
        
        # Process Timescale results
        for rank, result in enumerate(timescale_results, start=1):
            protein_id = result['canonical_name']
            score = timescale_weight / (self.k + rank)
            rrf_scores[protein_id] += score
            sources[protein_id].add('timescale')
            original_scores[protein_id]['timescale'] = result.get('score', 0.0)
            metadata_map[protein_id] = {
                'budo_id': result.get('budo_id'),
                'organism': result.get('organism'),
                'sequence_length': result.get('sequence_length'),
            }
        
        # Process Neo4j results
        for rank, result in enumerate(neo4j_results, start=1):
            protein_id = result['canonical_name']
            score = neo4j_weight / (self.k + rank)
            rrf_scores[protein_id] += score
            sources[protein_id].add('neo4j')
            original_scores[protein_id]['neo4j'] = result.get('score', 0.0)
            
            # Merge metadata (Neo4j may have additional graph context)
            if protein_id not in metadata_map:
                metadata_map[protein_id] = {}
            metadata_map[protein_id].update({
                'neighbors': result.get('neighbors', []),
                'degree': result.get('degree', 0),
            })
        
        # Sort by RRF score
        sorted_proteins = sorted(
            rrf_scores.items(), 
            key=lambda x: x[1], 
            reverse=True
        )
        
        # Build SearchResult objects
        results = []
        for rank, (protein_id, rrf_score) in enumerate(sorted_proteins, start=1):
            result = SearchResult(
                protein_id=protein_id,
                budo_id=metadata_map[protein_id].get('budo_id', f"budo:{protein_id}_V1-S"),
                score=rrf_score,
                rank=rank,
                sources=sources[protein_id],
                timescale_score=original_scores[protein_id].get('timescale'),
                neo4j_score=original_scores[protein_id].get('neo4j'),
                metadata=metadata_map[protein_id]
            )
            results.append(result)
        
        return results


# ============================================================================
# UNIFIED QUERY ENGINE
# ============================================================================

class UnifiedQueryEngine:
    """
    Hybrid query engine combining Timescale + Neo4j with RRF fusion.
    
    Capabilities:
    - search_proteins(): Fast BM25/vector search on Timescale
    - traverse_neighborhood(): Graph exploration on Neo4j
    - hybrid_search(): RRF fusion of both stores
    - enrich_with_graph(): Add Neo4j context to Timescale results
    
    Example:
        engine = UnifiedQueryEngine(timescale_pool, neo4j_driver)
        results = await engine.hybrid_search(
            query="SH2 domain phosphorylation",
            limit=10,
            graph_enrichment=True
        )
    """
    
    def __init__(
        self, 
        timescale_pool: asyncpg.Pool, 
        neo4j_driver: AsyncDriver,
        rrf_k: int = 60
    ):
        """
        Initialize engine with both data stores.
        
        Args:
            timescale_pool: asyncpg connection pool for Timescale
            neo4j_driver: Neo4j async driver
            rrf_k: RRF constant (default 60)
        """
        self.ts_writer = BudoGraphWriter(timescale_pool)
        self.neo_writer = BudoNeo4jWriter(neo4j_driver)
        self.rrf = ReciprocalRankFusion(k=rrf_k)
        self.pool = timescale_pool
        self.driver = neo4j_driver
    
    async def search_proteins(
        self, 
        query: str, 
        limit: int = 20,
        filters: Optional[QueryFilters] = None,
        use_vector: bool = False,
        embedding: Optional[List[float]] = None
    ) -> List[Dict[str, Any]]:
        """
        Fast BM25 or vector search on Timescale.
        
        Args:
            query: Search query string
            limit: Maximum results
            filters: Optional QueryFilters object
            use_vector: Use vector search instead of BM25
            embedding: Query embedding (required if use_vector=True)
        
        Returns:
            List of protein records with scores
        """
        async with self.pool.acquire() as conn:
            if use_vector and embedding:
                # HNSW vector search
                sql = """
                    SELECT 
                        n.canonical_name,
                        n.properties->>'budo_id' as budo_id,
                        n.properties->>'organism' as organism,
                        (n.properties->>'sequence_length')::int as sequence_length,
                        1 - (n.embedding <=> $2::vector) as score
                    FROM atom_graph_nodes n
                    WHERE n.node_type = 'protein'
                """
                
                # Add filters
                if filters:
                    if filters.organism:
                        sql += f" AND n.properties->>'organism' = '{filters.organism}'"
                    if filters.min_sequence_length:
                        sql += f" AND (n.properties->>'sequence_length')::int >= {filters.min_sequence_length}"
                
                sql += " ORDER BY n.embedding <=> $2::vector LIMIT $1"
                
                results = await conn.fetch(sql, limit, embedding)
            else:
                # BM25 full-text search
                sql = """
                    SELECT 
                        n.canonical_name,
                        n.properties->>'budo_id' as budo_id,
                        n.properties->>'organism' as organism,
                        (n.properties->>'sequence_length')::int as sequence_length,
                        ts_rank(n.search_vector, plainto_tsquery('english', $2)) as score
                    FROM atom_graph_nodes n
                    WHERE n.node_type = 'protein'
                      AND n.search_vector @@ plainto_tsquery('english', $2)
                """
                
                # Add filters
                if filters:
                    if filters.organism:
                        sql += f" AND n.properties->>'organism' = '{filters.organism}'"
                    if filters.functional_state:
                        sql += f" AND n.properties->>'functional_state' = '{filters.functional_state}'"
                
                sql += " ORDER BY score DESC LIMIT $1"
                
                results = await conn.fetch(sql, limit, query)
            
            return [dict(r) for r in results]
    
    async def traverse_neighborhood(
        self, 
        protein_id: str, 
        max_hops: int = 2,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Graph traversal on Neo4j to find protein neighborhood.
        
        Args:
            protein_id: Starting protein canonical name
            max_hops: Maximum traversal depth (1-3 recommended)
            limit: Maximum neighbors to return
        
        Returns:
            List of neighbor nodes with relationship metadata
        """
        neighbors = await self.neo_writer.query_protein_neighborhood(
            protein_id, max_hops=max_hops
        )
        return neighbors[:limit]
    
    async def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        timescale_weight: float = 1.0,
        neo4j_weight: float = 0.5,
        graph_enrichment: bool = True,
        filters: Optional[QueryFilters] = None
    ) -> List[SearchResult]:
        """
        Hybrid search with RRF fusion of Timescale + Neo4j.
        
        Strategy:
        1. Run BM25 search on Timescale (fast, text-based)
        2. Run graph-aware search on Neo4j (structural context)
        3. Fuse results with RRF
        4. Optionally enrich with graph neighborhood
        
        Args:
            query: Search query string
            limit: Final result count
            timescale_weight: Weight for Timescale scores (0-1)
            neo4j_weight: Weight for Neo4j scores (0-1)
            graph_enrichment: Add neighbor context to results
            filters: Optional QueryFilters
        
        Returns:
            Merged and ranked SearchResult list
        """
        # Parallel execution of both searches
        timescale_task = self.search_proteins(
            query, limit=limit*2, filters=filters
        )
        neo4j_task = self._neo4j_search(query, limit=limit*2, filters=filters)
        
        timescale_results, neo4j_results = await asyncio.gather(
            timescale_task, neo4j_task
        )
        
        # RRF fusion
        fused_results = self.rrf.fuse(
            timescale_results,
            neo4j_results,
            timescale_weight=timescale_weight,
            neo4j_weight=neo4j_weight
        )
        
        # Graph enrichment (optional)
        if graph_enrichment:
            for result in fused_results[:limit]:
                try:
                    neighbors = await self.traverse_neighborhood(
                        result.protein_id, max_hops=1
                    )
                    result.metadata['neighbors'] = neighbors[:5]  # Top 5 neighbors
                except Exception as e:
                    logger.warning(f"Graph enrichment failed for {result.protein_id}: {e}")
        
        return fused_results[:limit]
    
    async def _neo4j_search(
        self, 
        query: str, 
        limit: int = 20,
        filters: Optional[QueryFilters] = None
    ) -> List[Dict[str, Any]]:
        """
        Full-text search on Neo4j (uses Lucene index if available).
        
        Fallback: Cypher CONTAINS query on recommended_name.
        
        Args:
            query: Search query
            limit: Maximum results
            filters: Optional filters
        
        Returns:
            List of protein records with pseudo-scores
        """
        cypher = """
            MATCH (p:Protein)
            WHERE toLower(p.recommended_name) CONTAINS toLower($query)
               OR toLower(p.canonical_name) CONTAINS toLower($query)
        """
        
        # Add filters
        if filters:
            if filters.organism:
                cypher += f" AND p.organism = '{filters.organism}'"
            if filters.functional_state:
                cypher += f" AND p.functional_state = '{filters.functional_state}'"
        
        cypher += """
            RETURN 
                p.canonical_name as canonical_name,
                p.budo_id as budo_id,
                p.organism as organism,
                p.sequence_length as sequence_length,
                size((p)-[]-()) as degree,
                0.5 as score
            ORDER BY degree DESC
            LIMIT $limit
        """
        
        async with self.driver.session() as session:
            result = await session.run(cypher, query=query, limit=limit)
            records = await result.values()
            
            return [
                {
                    'canonical_name': r[0],
                    'budo_id': r[1],
                    'organism': r[2],
                    'sequence_length': r[3],
                    'degree': r[4],
                    'score': r[5]
                }
                for r in records
            ]
    
    async def enrich_with_graph(
        self, 
        protein_ids: List[str],
        max_hops: int = 1
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Batch enrich proteins with graph neighborhood.
        
        Args:
            protein_ids: List of protein canonical names
            max_hops: Traversal depth
        
        Returns:
            Dict mapping protein_id → neighbors
        """
        enriched = {}
        
        # Parallel enrichment
        tasks = [
            self.traverse_neighborhood(pid, max_hops=max_hops)
            for pid in protein_ids
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for protein_id, neighbors in zip(protein_ids, results):
            if isinstance(neighbors, Exception):
                logger.warning(f"Enrichment failed for {protein_id}: {neighbors}")
                enriched[protein_id] = []
            else:
                enriched[protein_id] = neighbors
        
        return enriched
    
    async def find_similar_by_structure(
        self,
        protein_id: str,
        min_shared_domains: int = 1,
        limit: int = 20
    ) -> List[SearchResult]:
        """
        Find structurally similar proteins (shared domains/motifs).
        
        Strategy:
        1. Get domains of query protein from Neo4j
        2. Find other proteins with same domains
        3. Rank by number of shared domains
        
        Args:
            protein_id: Query protein
            min_shared_domains: Minimum shared domains
            limit: Maximum results
        
        Returns:
            List of similar proteins with similarity scores
        """
        cypher = """
            MATCH (p1:Protein {canonical_name: $protein_id})<-[:PART_OF]-(d1:Domain)
            MATCH (d2:Domain)-[:PART_OF]->(p2:Protein)
            WHERE d1.domain_type = d2.domain_type
              AND p1 <> p2
            WITH p2, count(DISTINCT d1.domain_type) as shared_domains
            WHERE shared_domains >= $min_shared
            RETURN 
                p2.canonical_name as canonical_name,
                p2.budo_id as budo_id,
                shared_domains,
                size((p2)<-[:PART_OF]-()) as total_domains
            ORDER BY shared_domains DESC
            LIMIT $limit
        """
        
        async with self.driver.session() as session:
            result = await session.run(
                cypher, 
                protein_id=protein_id, 
                min_shared=min_shared_domains,
                limit=limit
            )
            records = await result.values()
            
            results = []
            for rank, record in enumerate(records, start=1):
                canonical_name, budo_id, shared, total = record
                similarity_score = shared / max(total, 1)
                
                results.append(SearchResult(
                    protein_id=canonical_name,
                    budo_id=budo_id,
                    score=similarity_score,
                    rank=rank,
                    sources={'neo4j'},
                    neo4j_score=similarity_score,
                    metadata={
                        'shared_domains': shared,
                        'total_domains': total,
                        'similarity_type': 'structural'
                    }
                ))
            
            return results
    
    async def find_interaction_partners(
        self,
        protein_id: str,
        interaction_types: Optional[List[str]] = None,
        limit: int = 20
    ) -> List[SearchResult]:
        """
        Find proteins that interact with query protein.
        
        Args:
            protein_id: Query protein
            interaction_types: Filter by types (e.g., ['PHOSPHORYLATES', 'BINDS_LIGAND'])
            limit: Maximum results
        
        Returns:
            List of interaction partners
        """
        if interaction_types:
            rel_filter = f":{' | '.join(interaction_types)}"
        else:
            rel_filter = ""
        
        cypher = f"""
            MATCH (p1:Protein {{canonical_name: $protein_id}})-[r{rel_filter}]-(p2:Protein)
            RETURN DISTINCT
                p2.canonical_name as canonical_name,
                p2.budo_id as budo_id,
                type(r) as interaction_type,
                properties(r) as interaction_props
            LIMIT $limit
        """
        
        async with self.driver.session() as session:
            result = await session.run(cypher, protein_id=protein_id, limit=limit)
            records = await result.values()
            
            results = []
            for rank, record in enumerate(records, start=1):
                canonical_name, budo_id, int_type, int_props = record
                
                results.append(SearchResult(
                    protein_id=canonical_name,
                    budo_id=budo_id,
                    score=1.0,  # Binary relevance (interacts or not)
                    rank=rank,
                    sources={'neo4j'},
                    metadata={
                        'interaction_type': int_type,
                        'interaction_properties': int_props
                    }
                ))
            
            return results


# ============================================================================
# FACTORY & HELPERS
# ============================================================================

class UnifiedQueryEngineFactory:
    """
    Async context manager for UnifiedQueryEngine.
    
    Usage:
        async with UnifiedQueryEngineFactory(ts_dsn, neo4j_uri) as engine:
            results = await engine.hybrid_search("kinase")
    """
    
    def __init__(
        self,
        timescale_dsn: str,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str,
        pool_min_size: int = 5,
        pool_max_size: int = 20
    ):
        self.timescale_dsn = timescale_dsn
        self.neo4j_uri = neo4j_uri
        self.neo4j_user = neo4j_user
        self.neo4j_password = neo4j_password
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.pool = None
        self.driver = None
        self.engine = None
    
    async def __aenter__(self) -> UnifiedQueryEngine:
        # Create Timescale pool
        self.pool = await create_asyncpg_pool_for_database_url(
            self.timescale_dsn,
            min_size=self.pool_min_size,
            max_size=self.pool_max_size,
            command_timeout=30,
            timeout=20,
        )
        
        # Create Neo4j driver
        self.driver = AsyncGraphDatabase.driver(
            self.neo4j_uri,
            auth=(self.neo4j_user, self.neo4j_password)
        )
        
        # Create engine
        self.engine = UnifiedQueryEngine(self.pool, self.driver)
        
        return self.engine
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.pool:
            await self.pool.close()
        if self.driver:
            await self.driver.close()

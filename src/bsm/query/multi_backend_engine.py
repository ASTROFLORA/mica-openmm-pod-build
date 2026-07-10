"""
Multi-Backend Query Engine

Provides unified query interface combining:
1. Vector similarity search (Zilliz/Milvus)
2. Graph relationship queries (Neo4j)
3. Semantic reasoning (JSON-LD)

Created: October 8, 2025
Author: Alex Rodriguez
"""

from typing import List, Dict, Any, Optional
import logging
from dataclasses import dataclass

try:
    from pymilvus import Collection, connections
    MILVUS_AVAILABLE = True
except ImportError:
    MILVUS_AVAILABLE = False
    print("[WARNING] pymilvus not installed. Vector search will be unavailable.")

try:
    from bsm.budo.neo4j_service import BudoNeo4jService
except ImportError:
    BudoNeo4jService = None

logger = logging.getLogger(__name__)


@dataclass
class HybridSearchResult:
    """Result from hybrid search (vector + graph)"""
    budo_id: str
    gene_symbol: str
    similarity_score: float  # Milvus L2/cosine distance
    domains: List[Dict]
    biosites: List[Dict]
    interactions: List[Dict]
    functional_state: str
    organism: str


class MultiBackendQueryEngine:
    """
    Unified query engine for Neo4j + Milvus/Zilliz + JSON-LD
    
    Features:
    - Hybrid search: Vector similarity + graph context enrichment
    - Pathway search: Protein interaction network traversal
    - Semantic search: JSON-LD SPARQL queries (future)
    - Multi-modal filtering: Combine embeddings + metadata
    
    Example:
        >>> engine = MultiBackendQueryEngine(neo4j_service, milvus_collection)
        >>> results = engine.hybrid_search(query_embedding, top_k=10)
        >>> pathway = engine.pathway_search("budo:WNK1-S-001", max_depth=3)
    """
    
    def __init__(self, 
                 neo4j_service: 'BudoNeo4jService',
                 milvus_collection: Optional['Collection'] = None,
                 zilliz_uri: Optional[str] = None,
                 zilliz_token: Optional[str] = None):
        """
        Initialize multi-backend query engine
        
        Args:
            neo4j_service: BudoNeo4jService instance
            milvus_collection: Pymilvus Collection instance (optional)
            zilliz_uri: Zilliz Cloud URI (optional, if using Zilliz instead of local Milvus)
            zilliz_token: Zilliz Cloud token (optional)
        """
        self.neo4j = neo4j_service
        self.milvus_collection = milvus_collection
        
        # Connect to Zilliz Cloud if credentials provided
        if zilliz_uri and zilliz_token and MILVUS_AVAILABLE:
            connections.connect(
                alias="default",
                uri=zilliz_uri,
                token=zilliz_token
            )
            logger.info(f"Connected to Zilliz Cloud: {zilliz_uri}")
        
        if not MILVUS_AVAILABLE:
            logger.warning("Milvus/Zilliz unavailable. Vector search disabled.")
    
    def hybrid_search(self, 
                      query_embedding: List[float], 
                      top_k: int = 10,
                      filters: Optional[Dict[str, Any]] = None,
                      metric_type: str = "L2") -> List[HybridSearchResult]:
        """
        Hybrid search combining vector similarity (Milvus) and graph context (Neo4j)
        
        Workflow:
        1. Vector similarity search in Milvus (1280D embeddings)
        2. Retrieve top-k most similar BUDO IDs
        3. Enrich each result with Neo4j graph context
        4. Return fused results
        
        Args:
            query_embedding: 1280D embedding (768D PubMedBERT + 512D ESE)
            top_k: Number of results to return
            filters: Neo4j metadata filters (e.g., {"organism": "Homo sapiens"})
            metric_type: Distance metric ("L2", "IP", "COSINE")
        
        Returns:
            List of HybridSearchResult objects
        
        Example:
            >>> # Query: Find proteins similar to WNK1 embedding
            >>> query_emb = get_wnk1_embedding()  # 1280D vector
            >>> results = engine.hybrid_search(query_emb, top_k=5)
            >>> for r in results:
            >>>     print(f"{r.gene_symbol}: similarity={r.similarity_score:.3f}")
        """
        if not MILVUS_AVAILABLE or self.milvus_collection is None:
            raise RuntimeError("Milvus/Zilliz not available. Cannot perform vector search.")
        
        # Step 1: Vector similarity search in Milvus
        logger.info(f"[Hybrid Search] Querying Milvus for top-{top_k} similar embeddings...")
        
        search_params = {
            "metric_type": metric_type,
            "params": {"nprobe": 10}  # Number of clusters to search
        }
        
        try:
            milvus_results = self.milvus_collection.search(
                data=[query_embedding],
                anns_field="embedding",  # Embedding field name in Milvus
                param=search_params,
                limit=top_k,
                output_fields=["budo_id", "gene_symbol", "organism"]
            )[0]
        except Exception as e:
            logger.error(f"Milvus search failed: {e}")
            return []
        
        # Step 2: Enrich with Neo4j graph context
        logger.info(f"[Hybrid Search] Enriching {len(milvus_results)} results with Neo4j graph context...")
        
        hybrid_results = []
        for hit in milvus_results:
            budo_id = hit.entity.get("budo_id")
            
            # Fetch graph context from Neo4j
            try:
                graph_context = self._get_budo_graph_context(budo_id)
                
                result = HybridSearchResult(
                    budo_id=budo_id,
                    gene_symbol=hit.entity.get("gene_symbol", "Unknown"),
                    similarity_score=hit.distance,
                    domains=graph_context.get("domains", []),
                    biosites=graph_context.get("biosites", []),
                    interactions=graph_context.get("interactions", []),
                    functional_state=graph_context.get("functional_state", "Unknown"),
                    organism=hit.entity.get("organism", "Unknown")
                )
                
                hybrid_results.append(result)
            
            except Exception as e:
                logger.warning(f"Failed to enrich {budo_id} with graph context: {e}")
                continue
        
        # Step 3: Apply Neo4j filters (if provided)
        if filters:
            hybrid_results = self._apply_filters(hybrid_results, filters)
        
        logger.info(f"[Hybrid Search] Returning {len(hybrid_results)} enriched results")
        return hybrid_results
    
    def _get_budo_graph_context(self, budo_id: str) -> Dict:
        """
        Fetch BUDO graph context from Neo4j
        
        Returns:
            Dictionary with domains, biosites, interactions, functional_state
        """
        with self.neo4j.driver.session() as session:
            # Query BUDO with domains, biosites, interactions
            result = session.run("""
                MATCH (b:BUDO {budo_id: $budo_id})
                OPTIONAL MATCH (b)-[:HAS_DOMAIN]->(d:Domain)
                OPTIONAL MATCH (b)-[:HAS_BIOSITE]->(s:BioSite)
                OPTIONAL MATCH (b)-[i:INTERACTS_WITH]->(target:BUDO)
                RETURN 
                    b.functional_state AS functional_state,
                    collect(DISTINCT {domain_type: d.domain_type, start: d.start, end: d.end}) AS domains,
                    collect(DISTINCT {site_type: s.site_type, residues: s.residues}) AS biosites,
                    collect(DISTINCT {target_id: target.budo_id, target_gene: target.gene_symbol, interaction_type: type(i)}) AS interactions
            """, {"budo_id": budo_id})
            
            record = result.single()
            if record:
                return {
                    "functional_state": record["functional_state"],
                    "domains": [d for d in record["domains"] if d.get("domain_type")],
                    "biosites": [s for s in record["biosites"] if s.get("site_type")],
                    "interactions": [i for i in record["interactions"] if i.get("target_id")]
                }
            else:
                return {
                    "functional_state": "Unknown",
                    "domains": [],
                    "biosites": [],
                    "interactions": []
                }
    
    def _apply_filters(self, results: List[HybridSearchResult], filters: Dict[str, Any]) -> List[HybridSearchResult]:
        """Apply metadata filters to hybrid search results"""
        filtered = []
        
        for result in results:
            match = True
            
            if "organism" in filters and result.organism != filters["organism"]:
                match = False
            
            if "functional_state" in filters and result.functional_state != filters["functional_state"]:
                match = False
            
            if match:
                filtered.append(result)
        
        return filtered
    
    def pathway_search(self, 
                       start_budo_id: str, 
                       max_depth: int = 3,
                       interaction_types: Optional[List[str]] = None) -> Dict:
        """
        Search protein interaction pathways in Neo4j graph
        
        Traverses INTERACTS_WITH relationships to discover protein networks.
        
        Args:
            start_budo_id: Starting BUDO ID
            max_depth: Maximum relationship depth to traverse (1-5)
            interaction_types: Filter by interaction types (e.g., ["Phosphorylation", "Binding"])
        
        Returns:
            Pathway graph dictionary with nodes and edges
        
        Example:
            >>> pathway = engine.pathway_search("budo:WNK1-S-001", max_depth=2)
            >>> print(f"Found {len(pathway['nodes'])} proteins in pathway")
            >>> print(f"Found {len(pathway['edges'])} interactions")
        """
        logger.info(f"[Pathway Search] Traversing from {start_budo_id} (max depth: {max_depth})...")
        
        with self.neo4j.driver.session() as session:
            # Cypher query to traverse interaction network
            cypher_query = """
                MATCH path = (start:BUDO {budo_id: $start_id})-[:INTERACTS_WITH*1..$depth]-(target:BUDO)
                RETURN 
                    nodes(path) AS pathway_nodes,
                    relationships(path) AS pathway_edges
            """
            
            result = session.run(cypher_query, {
                "start_id": start_budo_id,
                "depth": max_depth
            })
            
            # Collect unique nodes and edges
            nodes = {}
            edges = []
            
            for record in result:
                # Process nodes
                for node in record["pathway_nodes"]:
                    budo_id = node["budo_id"]
                    if budo_id not in nodes:
                        nodes[budo_id] = {
                            "budo_id": budo_id,
                            "gene_symbol": node.get("gene_symbol", "Unknown"),
                            "functional_state": node.get("functional_state", "Unknown")
                        }
                
                # Process edges
                for edge in record["pathway_edges"]:
                    edge_data = {
                        "source": edge.start_node["budo_id"],
                        "target": edge.end_node["budo_id"],
                        "interaction_type": edge.get("interaction_type", "Unknown"),
                        "evidence": edge.get("evidence", "Unknown")
                    }
                    
                    # Filter by interaction type if specified
                    if interaction_types is None or edge_data["interaction_type"] in interaction_types:
                        edges.append(edge_data)
            
            pathway_graph = {
                "start_node": start_budo_id,
                "max_depth": max_depth,
                "nodes": list(nodes.values()),
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges)
            }
            
            logger.info(f"[Pathway Search] Found {len(nodes)} nodes, {len(edges)} edges")
            return pathway_graph
    
    def semantic_search(self, sparql_query: str) -> List[Dict]:
        """
        Semantic search using SPARQL over JSON-LD knowledge graph
        
        NOTE: This is a placeholder for future implementation.
        Requires RDFLib or Apache Jena integration.
        
        Args:
            sparql_query: SPARQL query string
        
        Returns:
            List of query results
        
        TODO: Implement JSON-LD → RDF conversion and SPARQL endpoint
        """
        raise NotImplementedError("Semantic SPARQL search not yet implemented. See Phase 2.3 roadmap.")
    
    def close(self):
        """Close all backend connections"""
        if self.neo4j:
            self.neo4j.close()
        
        if MILVUS_AVAILABLE:
            connections.disconnect("default")
        
        logger.info("Multi-backend query engine closed")


# Example usage
if __name__ == "__main__":
    import numpy as np
    
    # Mock setup (replace with real instances)
    print("[Example] Multi-Backend Query Engine")
    print("=" * 50)
    
    # Example: Hybrid search with random embedding
    query_embedding = np.random.randn(1280).tolist()  # 1280D random vector
    
    print(f"Query embedding: {len(query_embedding)}D vector")
    print("NOTE: This requires Neo4j and Milvus/Zilliz to be running.")
    print("\nSee integration tests for full examples.")

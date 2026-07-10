"""
FASE 5: End-to-End BUDO GraphRAG Pipeline
==========================================

Complete pipeline integrating all phases:
1. Parse LMP XML → BUDO V3
2. Dual-write to Timescale + Neo4j
3. Generate embeddings (placeholder)
4. Query with UnifiedQueryEngine (RRF fusion)

Author: Alex Rodriguez AI Lab
Date: January 21, 2026
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
import os

import asyncpg
from neo4j import AsyncGraphDatabase
from dotenv import load_dotenv
from mica.infrastructure.persistence.pg_async import create_asyncpg_pool_for_database_url

# BUDO Components
from bsm.lmp.budo_parser import parse_lmp_xml
from bsm.schemas.budo_v3 import BudoV3

# Persistence Layer
from mica.infrastructure.persistence import (
    DualWriteCoordinator,
    UnifiedQueryEngine,
    UnifiedQueryEngineFactory,
    SearchResult,
    QueryFilters,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class PipelineConfig:
    """Pipeline configuration from environment variables."""
    # Timescale (from .env)
    timescale_dsn: str
    
    # Neo4j (from .env)
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    
    # Pipeline settings
    batch_size: int = 10
    enable_embeddings: bool = False  # Placeholder for future
    enable_query_cache: bool = False
    
    @classmethod
    def from_env(cls) -> "PipelineConfig":
        """Load configuration from environment variables."""
        load_dotenv()
        
        # Timescale connection string
        timescale_dsn = os.getenv("DATABASE_URL")
        if not timescale_dsn:
            raise ValueError("DATABASE_URL not set in .env")
        
        # Convert postgres:// to postgresql://
        if timescale_dsn.startswith("postgres://"):
            timescale_dsn = timescale_dsn.replace("postgres://", "postgresql://")
        
        # Neo4j credentials
        neo4j_uri = os.getenv("NEO4J_URI")
        neo4j_user = os.getenv("NEO4J_USERNAME", "neo4j")
        neo4j_password = os.getenv("NEO5J_INSTANCE_PASSWORD")  # Note: NEO5J typo in .env
        
        if not neo4j_uri or not neo4j_password:
            raise ValueError("Neo4j credentials not set in .env")
        
        logger.info(f"Timescale DSN: {timescale_dsn[:50]}...")
        logger.info(f"Neo4j URI: {neo4j_uri}")
        
        return cls(
            timescale_dsn=timescale_dsn,
            neo4j_uri=neo4j_uri,
            neo4j_user=neo4j_user,
            neo4j_password=neo4j_password
        )


# ============================================================================
# PIPELINE STAGES
# ============================================================================

class BudoGraphRAGPipeline:
    """
    End-to-end pipeline for BUDO GraphRAG.
    
    Stages:
    1. Parse LMP XML → BUDO V3
    2. Dual-write to Timescale + Neo4j
    3. Generate embeddings (future)
    4. Make query-ready
    
    Usage:
        config = PipelineConfig.from_env()
        async with BudoGraphRAGPipeline(config) as pipeline:
            await pipeline.process_lmp_file("5DRB_complete.xml")
    """
    
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.timescale_pool: Optional[asyncpg.Pool] = None
        self.neo4j_driver = None
        self.coordinator: Optional[DualWriteCoordinator] = None
        self.query_engine: Optional[UnifiedQueryEngine] = None
        
        # Statistics
        self.stats = {
            'parsed': 0,
            'written': 0,
            'embedded': 0,
            'failed': 0,
            'start_time': None,
            'end_time': None
        }
    
    async def __aenter__(self):
        """Initialize connections."""
        logger.info("Initializing BUDO GraphRAG Pipeline...")
        
        self.stats['start_time'] = datetime.utcnow()
        
        # Create Timescale pool
        logger.info("Connecting to Timescale...")
        self.timescale_pool = await create_asyncpg_pool_for_database_url(
            self.config.timescale_dsn,
            min_size=5,
            max_size=20,
            command_timeout=60,
            timeout=20,
        )
        
        # Create Neo4j driver
        logger.info("Connecting to Neo4j...")
        self.neo4j_driver = AsyncGraphDatabase.driver(
            self.config.neo4j_uri,
            auth=(self.config.neo4j_user, self.config.neo4j_password)
        )
        
        # Verify Neo4j connectivity
        try:
            await self.neo4j_driver.verify_connectivity()
            logger.info("✅ Neo4j connection verified")
        except Exception as e:
            logger.error(f"❌ Neo4j connection failed: {e}")
            raise
        
        # Initialize schema
        await self._init_schema()
        
        # Create coordinator and query engine
        self.coordinator = DualWriteCoordinator(
            self.timescale_pool, 
            self.neo4j_driver
        )
        self.query_engine = UnifiedQueryEngine(
            self.timescale_pool,
            self.neo4j_driver
        )
        
        logger.info("✅ Pipeline initialized successfully")
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close connections and print statistics."""
        self.stats['end_time'] = datetime.utcnow()
        
        # Close connections
        if self.timescale_pool:
            await self.timescale_pool.close()
            logger.info("Timescale pool closed")
        
        if self.neo4j_driver:
            await self.neo4j_driver.close()
            logger.info("Neo4j driver closed")
        
        # Print statistics
        self._print_statistics()
    
    async def _init_schema(self):
        """Initialize database schema if not exists."""
        logger.info("Initializing Timescale schema...")
        
        async with self.timescale_pool.acquire() as conn:
            # Enable extensions
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            
            # Create atom_graph_nodes table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS atom_graph_nodes (
                    node_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    canonical_name TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    properties JSONB DEFAULT '{}',
                    embedding vector(768),
                    search_vector tsvector,
                    user_id TEXT DEFAULT 'system',
                    session_id TEXT DEFAULT 'default',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Create atom_graph_edges table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS atom_graph_edges (
                    edge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    source_node_id UUID NOT NULL,
                    target_node_id UUID NOT NULL,
                    edge_type TEXT NOT NULL,
                    properties JSONB DEFAULT '{}',
                    user_id TEXT DEFAULT 'system',
                    session_id TEXT DEFAULT 'default',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Create atom_facts table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS atom_facts (
                    fact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    node_id UUID NOT NULL,
                    fact_type TEXT NOT NULL,
                    fact_data JSONB NOT NULL,
                    confidence FLOAT DEFAULT 1.0,
                    user_id TEXT DEFAULT 'system',
                    session_id TEXT DEFAULT 'default',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Create indexes
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nodes_canonical 
                ON atom_graph_nodes(canonical_name)
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nodes_type 
                ON atom_graph_nodes(node_type)
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nodes_search 
                ON atom_graph_nodes USING GIN(search_vector)
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_edges_source 
                ON atom_graph_edges(source_node_id)
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_edges_target 
                ON atom_graph_edges(target_node_id)
            """)
        
        logger.info("✅ Timescale schema initialized")
    
    async def process_lmp_file(
        self, 
        xml_path: str, 
        user_id: str = "pipeline",
        session_id: str = "production"
    ) -> Dict[str, Any]:
        """
        Process single LMP XML file through complete pipeline.
        
        Args:
            xml_path: Path to LMP XML file
            user_id: User identifier for multi-tenancy
            session_id: Session identifier
        
        Returns:
            Dict with results (protein_id, status, timescale_node_id, neo4j_protein_id)
        """
        logger.info(f"Processing LMP file: {xml_path}")
        
        try:
            # STAGE 1: Parse LMP → BUDO V3
            logger.info("STAGE 1: Parsing LMP XML...")
            budo = await parse_lmp_xml(xml_path)
            self.stats['parsed'] += 1
            logger.info(f"✅ Parsed: {budo.canonical_name}")
            
            # STAGE 2: Dual-write to Timescale + Neo4j
            logger.info("STAGE 2: Dual-writing to Timescale + Neo4j...")
            result = await self.coordinator.upsert_budo(budo, user_id, session_id)
            
            if result.get('success'):
                self.stats['written'] += 1
                logger.info(f"✅ Written: Timescale={result['timescale_node_id']}, Neo4j={result['neo4j_protein_id']}")
            else:
                self.stats['failed'] += 1
                logger.error(f"❌ Write failed: {result.get('error')}")
                return result
            
            # STAGE 3: Generate embeddings (placeholder)
            if self.config.enable_embeddings:
                logger.info("STAGE 3: Generating embeddings...")
                await self._generate_embeddings(result['timescale_node_id'])
                self.stats['embedded'] += 1
            
            # STAGE 4: Verify query-ready
            logger.info("STAGE 4: Verifying query-ready...")
            search_results = await self.query_engine.search_proteins(
                budo.canonical_name, limit=1
            )
            
            if search_results:
                logger.info(f"✅ Query-ready: {budo.canonical_name} is searchable")
            else:
                logger.warning(f"⚠️  Not immediately searchable (indexing lag)")
            
            return {
                'status': 'success',
                'protein_id': budo.canonical_name,
                'budo_id': budo.budoId,
                **result
            }
        
        except Exception as e:
            self.stats['failed'] += 1
            logger.error(f"❌ Pipeline failed for {xml_path}: {e}", exc_info=True)
            return {
                'status': 'failed',
                'xml_path': xml_path,
                'error': str(e)
            }
    
    async def process_lmp_batch(
        self, 
        xml_paths: List[str],
        user_id: str = "pipeline",
        session_id: str = "production"
    ) -> List[Dict[str, Any]]:
        """
        Process batch of LMP files with parallel execution.
        
        Args:
            xml_paths: List of LMP XML file paths
            user_id: User identifier
            session_id: Session identifier
        
        Returns:
            List of results for each file
        """
        logger.info(f"Processing batch of {len(xml_paths)} LMP files...")
        
        # Process in batches to avoid overwhelming connections
        results = []
        for i in range(0, len(xml_paths), self.config.batch_size):
            batch = xml_paths[i:i + self.config.batch_size]
            logger.info(f"Batch {i//self.config.batch_size + 1}: {len(batch)} files")
            
            tasks = [
                self.process_lmp_file(xml_path, user_id, session_id)
                for xml_path in batch
            ]
            
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Handle exceptions
            for xml_path, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Exception for {xml_path}: {result}")
                    results.append({'status': 'failed', 'xml_path': xml_path, 'error': str(result)})
                else:
                    results.append(result)
        
        logger.info(f"✅ Batch processing complete: {len(results)} files processed")
        
        return results
    
    async def _generate_embeddings(self, node_id: str):
        """
        Generate embeddings for protein (placeholder).
        
        Future: Integrate ESM-3, E5, or other embedding models.
        """
        logger.info(f"Generating embeddings for node {node_id} (placeholder)")
        # TODO: Implement embedding generation
        pass
    
    def _print_statistics(self):
        """Print pipeline statistics."""
        if self.stats['start_time'] and self.stats['end_time']:
            elapsed = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        else:
            elapsed = 0
        
        logger.info("\n" + "="*60)
        logger.info("BUDO GRAPHRAG PIPELINE STATISTICS")
        logger.info("="*60)
        logger.info(f"Parsed:     {self.stats['parsed']}")
        logger.info(f"Written:    {self.stats['written']}")
        logger.info(f"Embedded:   {self.stats['embedded']}")
        logger.info(f"Failed:     {self.stats['failed']}")
        logger.info(f"Duration:   {elapsed:.2f}s")
        if self.stats['written'] > 0:
            logger.info(f"Throughput: {self.stats['written'] / elapsed:.2f} proteins/sec")
        logger.info("="*60 + "\n")


# ============================================================================
# QUERY INTERFACE
# ============================================================================

async def query_pipeline_results(
    config: PipelineConfig,
    query: str,
    limit: int = 10,
    use_hybrid: bool = True
) -> List[SearchResult]:
    """
    Query pipeline results using UnifiedQueryEngine.
    
    Args:
        config: Pipeline configuration
        query: Search query
        limit: Maximum results
        use_hybrid: Use hybrid RRF search (True) or Timescale only (False)
    
    Returns:
        List of SearchResult objects
    """
    async with UnifiedQueryEngineFactory(
        config.timescale_dsn,
        config.neo4j_uri,
        config.neo4j_user,
        config.neo4j_password
    ) as engine:
        if use_hybrid:
            logger.info(f"Hybrid search: '{query}'")
            results = await engine.hybrid_search(
                query=query,
                limit=limit,
                graph_enrichment=True
            )
        else:
            logger.info(f"Timescale search: '{query}'")
            results = await engine.search_proteins(query, limit=limit)
        
        # Print results
        logger.info(f"\n{'='*60}")
        logger.info(f"QUERY RESULTS: '{query}'")
        logger.info(f"{'='*60}")
        
        if use_hybrid:
            for result in results:
                logger.info(f"\n{result.rank}. {result.protein_id} (RRF score: {result.score:.4f})")
                logger.info(f"   BUDO ID: {result.budo_id}")
                logger.info(f"   Sources: {', '.join(result.sources)}")
                if result.timescale_score:
                    logger.info(f"   Timescale: {result.timescale_score:.4f}")
                if result.neo4j_score:
                    logger.info(f"   Neo4j: {result.neo4j_score:.4f}")
                if result.metadata.get('neighbors'):
                    logger.info(f"   Neighbors: {len(result.metadata['neighbors'])}")
        else:
            for i, result in enumerate(results, 1):
                logger.info(f"\n{i}. {result['canonical_name']}")
                logger.info(f"   BUDO ID: {result.get('budo_id')}")
                logger.info(f"   Score: {result.get('score', 0):.4f}")
        
        logger.info(f"\n{'='*60}\n")
        
        return results


# ============================================================================
# MAIN EXECUTION
# ============================================================================

async def main():
    """Main execution function."""
    logger.info("🚀 Starting BUDO GraphRAG Pipeline E2E Demo")
    
    # Load configuration
    config = PipelineConfig.from_env()
    
    # Initialize pipeline
    async with BudoGraphRAGPipeline(config) as pipeline:
        
        # Example 1: Process single file
        logger.info("\n=== EXAMPLE 1: Single File Processing ===")
        
        # Check if example XML exists
        xml_path = Path("5DRB_complete.xml")
        if xml_path.exists():
            result = await pipeline.process_lmp_file(str(xml_path))
            logger.info(f"Result: {result}")
        else:
            logger.warning(f"Example XML not found: {xml_path}")
            logger.info("Creating dummy BUDO for demo...")
            
            # Process with dummy BUDO
            from bsm.schemas.budo_v3 import (
                BudoDomain, BudoFunctionalState, BudoProvenance,
                FunctionalState, ConfidenceLevel
            )
            
            dummy_budo = BudoV3(
                budoId="budo:DEMO_PROTEIN_V1-S",
                canonical_name="DEMO_PROTEIN",
                recommended_name="Demo Protein for E2E Test",
                organism="Homo sapiens",
                taxonomy_id="9606",
                sequence="MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVAS",
                sequence_length=36,
                domains=[],
                interfaces=[],
                functionalState=BudoFunctionalState(
                    current=FunctionalState.ACTIVE,
                    predicted=None,
                    prediction_confidence=None,
                    history=[],
                    last_updated=datetime.utcnow(),
                    updated_by="demo"
                ),
                provenance=BudoProvenance(
                    created_by="pipeline_demo",
                    updated_by="pipeline_demo",
                    source="demo",
                    confidence=ConfidenceLevel.HIGH,
                    version=1
                ),
                embeddings=[],
                cross_references=[]
            )
            
            result = await pipeline.coordinator.upsert_budo(dummy_budo)
            logger.info(f"Dummy BUDO inserted: {result}")
        
        # Example 2: Query results
        logger.info("\n=== EXAMPLE 2: Query Pipeline Results ===")
        
        # Hybrid search
        await query_pipeline_results(
            config, 
            query="protein kinase",
            limit=5,
            use_hybrid=True
        )
        
        # Timescale-only search
        await query_pipeline_results(
            config,
            query="demo protein",
            limit=5,
            use_hybrid=False
        )
    
    logger.info("✅ Pipeline demo complete!")


if __name__ == "__main__":
    asyncio.run(main())

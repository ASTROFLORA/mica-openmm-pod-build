"""
Milvus ↔ Neo4j Bidirectional Sync Service
==========================================

Critical Gap Fix: Implements bidirectional synchronization between
Milvus vector database and Neo4j knowledge graph.

Gap Analysis Reference:
- 06_NEO4J_GRAPH_BRAIN_GAP_ANALYSIS.md Section 4.3
- Missing: budo_id field in Milvus, neo4j_node_id in Milvus
- Missing: Validation layer, update propagation

Author: AI Systems Architecture Lab
Date: October 26, 2025
Version: 1.0.0
"""

from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import logging

from pymilvus import Collection, connections
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


class MilvusNeo4jSyncService:
    """
    Bidirectional synchronization service between Milvus and Neo4j.
    
    Ensures:
    1. Every BUDO in Neo4j has corresponding embeddings in Milvus
    2. Every Milvus vector references valid Neo4j BUDO node
    3. Metadata consistency (protein_id, budo_id, names)
    4. Orphan detection and reconciliation
    """
    
    def __init__(
        self,
        milvus_host: str,
        milvus_port: int,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str
    ):
        # Milvus connection
        connections.connect(
            alias="default",
            host=milvus_host,
            port=milvus_port
        )
        
        # Neo4j connection
        self.neo4j_driver = GraphDatabase.driver(
            neo4j_uri,
            auth=(neo4j_user, neo4j_password)
        )
        
        logger.info(f"Initialized sync service: Milvus({milvus_host}:{milvus_port}) ↔ Neo4j({neo4j_uri})")
    
    def close(self):
        """Close connections"""
        connections.disconnect("default")
        self.neo4j_driver.close()
    
    def validate_budo_to_milvus_mapping(
        self,
        budo_id: str,
        collection_name: str
    ) -> Dict[str, Any]:
        """
        Validate that a BUDO node has corresponding Milvus vectors.
        
        Args:
            budo_id: BUDO ID in Neo4j
            collection_name: Milvus collection to check
            
        Returns:
            Validation report with status and missing embeddings
        """
        # Get embeddings from Neo4j
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})-[r:HAS_EMBEDDING]->(e:Embedding)
        WHERE e.milvus_collection = $collection_name
        RETURN e.milvus_vector_id as vector_id, e.modality as modality
        """
        
        with self.neo4j_driver.session() as session:
            result = session.run(query, budo_id=budo_id, collection_name=collection_name)
            neo4j_embeddings = [dict(record) for record in result]
        
        # Check Milvus existence
        collection = Collection(collection_name)
        collection.load()
        
        missing_vectors = []
        for embedding in neo4j_embeddings:
            vector_id = embedding["vector_id"]
            
            # Query Milvus by ID
            expr = f"id == '{vector_id}'"
            results = collection.query(expr=expr, output_fields=["id", "protein_id"])
            
            if not results:
                missing_vectors.append({
                    "vector_id": vector_id,
                    "modality": embedding["modality"],
                    "status": "MISSING_IN_MILVUS"
                })
        
        return {
            "budo_id": budo_id,
            "collection": collection_name,
            "neo4j_embeddings_count": len(neo4j_embeddings),
            "missing_in_milvus": missing_vectors,
            "is_valid": len(missing_vectors) == 0
        }
    
    def validate_milvus_to_neo4j_mapping(
        self,
        collection_name: str,
        sample_size: int = 100
    ) -> Dict[str, Any]:
        """
        Validate that Milvus vectors reference valid Neo4j BUDO nodes.
        
        Args:
            collection_name: Milvus collection to check
            sample_size: Number of random vectors to sample
            
        Returns:
            Validation report with orphaned vectors
        """
        collection = Collection(collection_name)
        collection.load()
        
        # Sample random vectors
        results = collection.query(
            expr="",
            output_fields=["id", "protein_id", "budo_id"],
            limit=sample_size
        )
        
        orphaned_vectors = []
        missing_budo_id_field = []
        
        for result in results:
            vector_id = result["id"]
            protein_id = result.get("protein_id")
            budo_id = result.get("budo_id")
            
            # Check if budo_id field exists (critical gap fix)
            if budo_id is None:
                missing_budo_id_field.append({
                    "vector_id": vector_id,
                    "protein_id": protein_id,
                    "issue": "MISSING_BUDO_ID_FIELD"
                })
                continue
            
            # Validate BUDO exists in Neo4j
            query = """
            MATCH (b:BUDO {budo_id: $budo_id})
            RETURN b.budo_id as budo_id
            """
            
            with self.neo4j_driver.session() as session:
                neo4j_result = session.run(query, budo_id=budo_id).single()
                
                if neo4j_result is None:
                    orphaned_vectors.append({
                        "vector_id": vector_id,
                        "budo_id": budo_id,
                        "protein_id": protein_id,
                        "issue": "NO_NEO4J_NODE"
                    })
        
        return {
            "collection": collection_name,
            "sample_size": len(results),
            "orphaned_vectors": orphaned_vectors,
            "missing_budo_id_field": missing_budo_id_field,
            "is_valid": len(orphaned_vectors) == 0 and len(missing_budo_id_field) == 0,
            "recommendations": [
                "Add 'budo_id' field to Milvus schema" if missing_budo_id_field else None,
                "Clean orphaned vectors or create Neo4j nodes" if orphaned_vectors else None
            ]
        }
    
    def sync_embedding_metadata(
        self,
        budo_id: str,
        collection_name: str,
        vector_id: str,
        metadata_updates: Dict[str, Any]
    ):
        """
        Update Milvus vector metadata when Neo4j BUDO changes.
        
        Critical Gap Fix: Implements update propagation.
        
        Args:
            budo_id: BUDO ID
            collection_name: Milvus collection
            vector_id: Milvus vector ID
            metadata_updates: Fields to update (e.g., {"functional_state": "active"})
        """
        collection = Collection(collection_name)
        
        # Milvus doesn't support in-place updates, must delete + reinsert
        # This is a simplified version - production needs batch operations
        
        # Get current vector data
        expr = f"id == '{vector_id}'"
        results = collection.query(
            expr=expr,
            output_fields=["*"]
        )
        
        if not results:
            logger.warning(f"Vector {vector_id} not found in {collection_name}")
            return
        
        current_data = results[0]
        
        # Merge updates
        updated_data = {**current_data, **metadata_updates}
        updated_data["updated_at"] = datetime.now().isoformat()
        updated_data["sync_source"] = "neo4j_budo_update"
        
        # Delete old vector
        collection.delete(expr=expr)
        
        # Insert updated vector
        collection.insert([updated_data])
        collection.flush()
        
        logger.info(f"Synced metadata: {budo_id} -> Milvus:{vector_id} ({len(metadata_updates)} fields)")
    
    def create_budo_id_field_migration_plan(
        self,
        collection_name: str
    ) -> Dict[str, Any]:
        """
        Generate migration plan to add 'budo_id' field to existing Milvus collection.
        
        Critical Gap Fix: Addresses missing budo_id field in Milvus.
        
        Returns:
            Migration plan with SQL-like operations and estimated impact
        """
        collection = Collection(collection_name)
        collection.load()
        
        # Sample collection to detect schema
        sample = collection.query(expr="", output_fields=["*"], limit=10)
        
        has_budo_id = "budo_id" in sample[0] if sample else False
        has_protein_id = "protein_id" in sample[0] if sample else False
        
        total_count = collection.num_entities
        
        plan = {
            "collection": collection_name,
            "current_schema": {
                "has_budo_id": has_budo_id,
                "has_protein_id": has_protein_id,
                "total_vectors": total_count
            },
            "migration_steps": [],
            "estimated_time_minutes": total_count / 1000  # ~1000 vectors/min
        }
        
        if has_budo_id:
            plan["status"] = "NO_MIGRATION_NEEDED"
            return plan
        
        plan["status"] = "MIGRATION_REQUIRED"
        plan["migration_steps"] = [
            {
                "step": 1,
                "action": "CREATE_TEMP_COLLECTION",
                "description": f"Create {collection_name}_migrated with budo_id field"
            },
            {
                "step": 2,
                "action": "LOOKUP_BUDO_IDS",
                "description": "Query Neo4j for protein_id -> budo_id mapping",
                "estimated_queries": total_count
            },
            {
                "step": 3,
                "action": "COPY_VECTORS",
                "description": "Copy vectors with added budo_id field",
                "batch_size": 1000
            },
            {
                "step": 4,
                "action": "SWAP_COLLECTIONS",
                "description": f"Rename {collection_name}_migrated -> {collection_name}"
            },
            {
                "step": 5,
                "action": "VALIDATE",
                "description": "Run validation checks (validate_milvus_to_neo4j_mapping)"
            }
        ]
        
        return plan
    
    def detect_orphans(
        self,
        collection_name: str,
        full_scan: bool = False
    ) -> Dict[str, Any]:
        """
        Detect orphaned records in both Milvus and Neo4j.
        
        Critical Gap Fix: Implements orphan detection.
        
        Args:
            collection_name: Milvus collection to scan
            full_scan: If True, scan all vectors (slow). If False, sample.
            
        Returns:
            Report with orphaned Neo4j embeddings and Milvus vectors
        """
        # Find Neo4j embeddings without Milvus vectors
        query = """
        MATCH (b:BUDO)-[r:HAS_EMBEDDING]->(e:Embedding)
        WHERE e.milvus_collection = $collection_name
        RETURN b.budo_id as budo_id, 
               e.milvus_vector_id as vector_id,
               e.modality as modality
        """
        
        with self.neo4j_driver.session() as session:
            result = session.run(query, collection_name=collection_name)
            neo4j_embeddings = [dict(record) for record in result]
        
        logger.info(f"Found {len(neo4j_embeddings)} embeddings in Neo4j")
        
        # Check Milvus existence
        collection = Collection(collection_name)
        collection.load()
        
        orphaned_neo4j = []
        for embedding in neo4j_embeddings:
            vector_id = embedding["vector_id"]
            expr = f"id == '{vector_id}'"
            results = collection.query(expr=expr, output_fields=["id"], limit=1)
            
            if not results:
                orphaned_neo4j.append(embedding)
        
        # Find Milvus vectors without Neo4j nodes (sample or full)
        limit = None if full_scan else 1000
        milvus_vectors = collection.query(
            expr="",
            output_fields=["id", "protein_id", "budo_id"],
            limit=limit
        )
        
        orphaned_milvus = []
        for vector in milvus_vectors:
            budo_id = vector.get("budo_id")
            if not budo_id:
                continue  # Skip vectors without budo_id
            
            query_check = """
            MATCH (b:BUDO {budo_id: $budo_id})
            RETURN b.budo_id as budo_id
            """
            
            with self.neo4j_driver.session() as session:
                result = session.run(query_check, budo_id=budo_id).single()
                if result is None:
                    orphaned_milvus.append({
                        "vector_id": vector["id"],
                        "budo_id": budo_id,
                        "protein_id": vector.get("protein_id")
                    })
        
        return {
            "collection": collection_name,
            "scan_type": "FULL" if full_scan else "SAMPLE",
            "orphaned_neo4j_embeddings": {
                "count": len(orphaned_neo4j),
                "examples": orphaned_neo4j[:10]
            },
            "orphaned_milvus_vectors": {
                "count": len(orphaned_milvus),
                "examples": orphaned_milvus[:10]
            },
            "health_score": 1.0 - (len(orphaned_neo4j) + len(orphaned_milvus)) / (len(neo4j_embeddings) + len(milvus_vectors)),
            "recommendations": [
                f"Delete {len(orphaned_neo4j)} orphaned Neo4j Embedding nodes" if orphaned_neo4j else None,
                f"Delete or create Neo4j nodes for {len(orphaned_milvus)} orphaned Milvus vectors" if orphaned_milvus else None
            ]
        }
    
    def cleanup_orphans(
        self,
        collection_name: str,
        delete_neo4j_orphans: bool = True,
        delete_milvus_orphans: bool = False
    ):
        """
        Clean up orphaned records.
        
        Args:
            collection_name: Milvus collection
            delete_neo4j_orphans: Delete Neo4j Embedding nodes without Milvus vectors
            delete_milvus_orphans: Delete Milvus vectors without Neo4j nodes (DANGEROUS)
        """
        orphan_report = self.detect_orphans(collection_name, full_scan=True)
        
        if delete_neo4j_orphans:
            orphaned = orphan_report["orphaned_neo4j_embeddings"]["examples"]
            
            for embedding in orphaned:
                query = """
                MATCH (e:Embedding {milvus_vector_id: $vector_id, milvus_collection: $collection})
                DETACH DELETE e
                """
                
                with self.neo4j_driver.session() as session:
                    session.run(
                        query,
                        vector_id=embedding["vector_id"],
                        collection=collection_name
                    )
            
            logger.info(f"Deleted {len(orphaned)} orphaned Neo4j Embedding nodes")
        
        if delete_milvus_orphans:
            orphaned = orphan_report["orphaned_milvus_vectors"]["examples"]
            collection = Collection(collection_name)
            
            for vector in orphaned:
                expr = f"id == '{vector['vector_id']}'"
                collection.delete(expr=expr)
            
            collection.flush()
            logger.warning(f"Deleted {len(orphaned)} orphaned Milvus vectors")


# Convenience function for quick validation
def validate_sync_health(
    milvus_host: str,
    milvus_port: int,
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    collection_name: str
) -> Dict[str, Any]:
    """
    Quick health check for Milvus ↔ Neo4j sync.
    
    Returns comprehensive validation report.
    """
    service = MilvusNeo4jSyncService(
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password
    )
    
    try:
        # Run all validation checks
        orphan_report = service.detect_orphans(collection_name, full_scan=False)
        migration_plan = service.create_budo_id_field_migration_plan(collection_name)
        
        return {
            "timestamp": datetime.now().isoformat(),
            "collection": collection_name,
            "orphan_detection": orphan_report,
            "migration_status": migration_plan,
            "overall_health": "HEALTHY" if orphan_report["health_score"] > 0.95 else "DEGRADED"
        }
    finally:
        service.close()

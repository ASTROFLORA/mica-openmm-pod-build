"""
BudoNeo4jWriter - Neo4j Graph Persistence Layer
================================================

Persists BUDO V3 objects to Neo4j analytical graph:
- Nodes: (:Protein), (:Domain), (:Compound)
- Relationships: [:CONTAINS_DOMAIN], [:PHOSPHORYLATES], [:BINDS_LIGAND], [:INTERACTS_WITH]

Author: Alex Rodriguez AI Lab
Phase: 0.002 - BUDO GraphRAG Integration
Date: January 21, 2026
Version: 3.0.0
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)
_PROD_ENV = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in ("prod", "production")

from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession
from neo4j.exceptions import Neo4jError

from bsm.schemas.budo_v3 import (
    BudoV3,
    BudoDomain,
    BudoPTM,
    BudoLigand,
    BudoInterface,
)


class BudoNeo4jWriter:
    """
    Writes BUDO V3 objects to Neo4j analytical graph.
    
    Architecture (Hemisferio Derecho):
    - Proteins → (:Protein) nodes
    - Domains → (:Domain)-[:PART_OF]->(:Protein)
    - PTMs → (:Protein)-[:PHOSPHORYLATES {position, residue}]->(:Protein)
    - Ligands → (:Protein)-[:BINDS_LIGAND {residues}]->(:Compound)
    - Interfaces → (:Protein)-[:INTERACTS_WITH {haddock_score}]->(:Protein)
    
    Visualization Focus:
    - Rich properties for Bloom/Browser exploration
    - Relationship weights for graph algorithms
    - Temporal tracking for evolution analysis
    """

    def __init__(self, driver: AsyncDriver):
        """
        Initialize writer with Neo4j async driver.
        
        Args:
            driver: neo4j.AsyncGraphDatabase.driver() instance
        """
        self.driver = driver

    async def upsert_budo_as_graph(
        self, 
        budo: BudoV3,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> str:
        """
        Persist complete BUDO V3 object as Neo4j graph.
        
        Pipeline:
        1. MERGE protein node
        2. MERGE domain nodes + [:PART_OF] relationships
        3. CREATE PTM relationships
        4. CREATE ligand relationships (with compound nodes)
        5. CREATE interface relationships
        6. CREATE motif properties on domains
        
        Args:
            budo: BudoV3 object from parser
            user_id: Optional user for tracking
            session_id: Optional session for tracking
            
        Returns:
            protein_id: Canonical name (used as Neo4j node ID)
            
        Raises:
            Neo4jError: On database errors
        """
        async with self.driver.session() as session:
            async with session.begin_transaction() as tx:
                # 1. MERGE protein node
                await self._merge_protein_node(tx, budo, user_id, session_id)
                
                # 2. MERGE domains + relationships
                await self._merge_domain_nodes(tx, budo)
                
                # 3. CREATE PTM relationships
                await self._create_ptm_relationships(tx, budo)
                
                # 4. CREATE ligand relationships
                await self._create_ligand_relationships(tx, budo)
                
                # 5. CREATE interface relationships
                await self._create_interface_relationships(tx, budo)
                
                # 6. Add motif properties
                await self._add_motif_properties(tx, budo)
                
                await tx.commit()
                
                return budo.canonical_name

    async def _merge_protein_node(
        self,
        tx: Any,  # AsyncTransaction
        budo: BudoV3,
        user_id: Optional[str],
        session_id: Optional[str]
    ) -> None:
        """
        MERGE protein node in Neo4j.
        
        Cypher:
            MERGE (p:Protein {canonical_name: $name})
            SET p += $properties
        """
        # Extract external IDs
        external_ids = {}
        for ref in budo.cross_references:
            db = ref.database.lower()
            if db == 'uniprot':
                external_ids['uniprot_id'] = ref.identifier
            elif db == 'pdb':
                if 'pdb_ids' not in external_ids:
                    external_ids['pdb_ids'] = []
                external_ids['pdb_ids'].append(ref.identifier)
            elif db == 'chembl':
                external_ids['chembl_id'] = ref.identifier
        
        # Properties for Neo4j node
        properties = {
            'canonical_name': budo.canonical_name,
            'budo_id': budo.budoId,
            'recommended_name': budo.recommended_name,
            'organism': budo.organism,
            'taxonomy_id': budo.taxonomy_id,
            'sequence': budo.sequence,
            'sequence_length': budo.sequence_length,
            'molecular_weight': budo.molecular_weight,
            'isoelectric_point': budo.isoelectric_point,
            'functional_state': budo.functionalState.current.value,
            'go_terms': [term.get('id') for term in budo.go_terms if 'id' in term],
            'kegg_pathways': budo.kegg_pathways,
            'reactome_pathways': budo.reactome_pathways,
            'ec_numbers': budo.ec_numbers,
            'created_at': budo.provenance.created_at.isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'source': budo.provenance.source,
            'confidence': budo.provenance.confidence.value,
        }
        
        # Merge external IDs
        properties.update(external_ids)
        
        # Add tracking if provided
        if user_id:
            properties['user_id'] = user_id
        if session_id:
            properties['session_id'] = session_id
        
        cypher = """
        MERGE (p:Protein {canonical_name: $canonical_name})
        SET p += $properties
        SET p.last_updated = datetime()
        """
        
        await tx.run(cypher, canonical_name=budo.canonical_name, properties=properties)

    async def _merge_domain_nodes(
        self,
        tx: Any,
        budo: BudoV3
    ) -> None:
        """
        MERGE domain nodes and create [:PART_OF] relationships.
        
        Cypher:
            MERGE (d:Domain {domain_id: $id})
            SET d += $properties
            
            MATCH (p:Protein {canonical_name: $protein})
            MERGE (d)-[:PART_OF]->(p)
        """
        for domain in budo.domains:
            # Domain properties
            properties = {
                'domain_id': f"{budo.canonical_name}:{domain.domain_id}",
                'domain_name': domain.domain_name,
                'domain_type': domain.domain_type,
                'start_position': domain.start_position,
                'end_position': domain.end_position,
                'cath_id': domain.cath_id,
                'pfam_id': domain.pfam_id,
                'interpro_id': domain.interpro_id,
                'num_ptms': len(domain.ptms),
                'num_ligands': len(domain.ligands),
                'num_conformations': len(domain.conformations),
            }
            
            cypher = """
            MERGE (d:Domain {domain_id: $domain_id})
            SET d += $properties
            SET d.last_updated = datetime()
            
            WITH d
            MATCH (p:Protein {canonical_name: $protein_name})
            MERGE (d)-[:PART_OF {
                start_position: $start,
                end_position: $end
            }]->(p)
            """
            
            await tx.run(
                cypher,
                domain_id=properties['domain_id'],
                properties=properties,
                protein_name=budo.canonical_name,
                start=domain.start_position,
                end=domain.end_position
            )

    async def _create_ptm_relationships(
        self,
        tx: Any,
        budo: BudoV3
    ) -> None:
        """
        CREATE PTM relationships: (:Protein)-[:PHOSPHORYLATES]->(:Protein).
        
        Relationship types by PTM:
        - Phosphorylation → [:PHOSPHORYLATES]
        - Acetylation → [:ACETYLATES]
        - Methylation → [:METHYLATES]
        - Ubiquitination → [:UBIQUITINATES]
        """
        ptm_type_map = {
            'Phosphorylation': 'PHOSPHORYLATES',
            'Acetylation': 'ACETYLATES',
            'Methylation': 'METHYLATES',
            'Ubiquitination': 'UBIQUITINATES',
        }
        
        for domain in budo.domains:
            for ptm in domain.ptms:
                rel_type = ptm_type_map.get(ptm.ptm_type, 'MODIFIES')
                
                cypher = f"""
                MATCH (p:Protein {{canonical_name: $protein_name}})
                CREATE (p)-[r:{rel_type} {{
                    position: $position,
                    residue: $residue,
                    domain_id: $domain_id,
                    domain_name: $domain_name,
                    enzyme: $enzyme,
                    timestamp: datetime()
                }}]->(p)
                """
                
                await tx.run(
                    cypher,
                    protein_name=budo.canonical_name,
                    position=ptm.position,
                    residue=ptm.residue,
                    domain_id=domain.domain_id,
                    domain_name=domain.domain_name,
                    enzyme=ptm.enzyme
                )

    async def _create_ligand_relationships(
        self,
        tx: Any,
        budo: BudoV3
    ) -> None:
        """
        CREATE ligand binding relationships with compound nodes.
        
        Cypher:
            MERGE (c:Compound {compound_id: $chembl_id})
            
            MATCH (p:Protein {canonical_name: $protein})
            CREATE (p)-[:BINDS_LIGAND {residues}]->(c)
        """
        for domain in budo.domains:
            for ligand in domain.ligands:
                # Prefer ChEMBL, fallback to PubChem
                compound_id = ligand.chembl_id or f"PUBCHEM:{ligand.pubchem_id}"
                compound_type = 'ChEMBL' if ligand.chembl_id else 'PubChem'
                
                cypher = """
                MERGE (c:Compound {compound_id: $compound_id})
                SET c.compound_type = $compound_type,
                    c.chembl_id = $chembl_id,
                    c.pubchem_id = $pubchem_id,
                    c.last_updated = datetime()
                
                WITH c
                MATCH (p:Protein {canonical_name: $protein_name})
                CREATE (p)-[:BINDS_LIGAND {
                    binding_residues: $residues,
                    domain_id: $domain_id,
                    domain_name: $domain_name,
                    timestamp: datetime()
                }]->(c)
                """
                
                await tx.run(
                    cypher,
                    compound_id=compound_id,
                    compound_type=compound_type,
                    chembl_id=ligand.chembl_id,
                    pubchem_id=ligand.pubchem_id,
                    protein_name=budo.canonical_name,
                    residues=ligand.binding_residues,
                    domain_id=domain.domain_id,
                    domain_name=domain.domain_name
                )

    async def _create_interface_relationships(
        self,
        tx: Any,
        budo: BudoV3
    ) -> None:
        """
        CREATE protein-protein interface relationships.
        
        Cypher:
            MATCH (p1:Protein {canonical_name: $source})
            MATCH (p2:Protein {canonical_name: $target})
            CREATE (p1)-[:INTERACTS_WITH {haddock_score}]->(p2)
        """
        for interface in budo.interfaces:
            rel_type = 'DOCKS_WITH' if interface.interface_type == 'docking' else 'INTERACTS_WITH'
            
            cypher = f"""
            MATCH (p1:Protein {{canonical_name: $source}})
            MERGE (p2:Protein {{canonical_name: $target}})
            CREATE (p1)-[r:{rel_type} {{
                interface_residues: $residues,
                partner_chain: $chain,
                interface_type: $type,
                haddock_score: $score,
                timestamp: datetime()
            }}]->(p2)
            """
            
            await tx.run(
                cypher,
                source=budo.canonical_name,
                target=interface.partner_protein_id,
                residues=interface.interface_residues,
                chain=interface.partner_chain,
                type=interface.interface_type,
                score=interface.haddock_score
            )

    async def _add_motif_properties(
        self,
        tx: Any,
        budo: BudoV3
    ) -> None:
        """
        Add motifs and catalytic residues as domain properties.
        
        Cypher:
            MATCH (d:Domain {domain_id: $id})
            SET d.motifs = $motifs,
                d.catalytic_residues = $catalytic
        """
        for domain in budo.domains:
            if not domain.motifs and not domain.catalytic_residues:
                continue
            
            # Convert motifs to simple format
            motifs = [
                f"{m.get('name', 'Unknown')}:{','.join(map(str, m.get('positions', [])))}"
                for m in domain.motifs
            ]
            
            cypher = """
            MATCH (d:Domain {domain_id: $domain_id})
            SET d.motifs = $motifs,
                d.catalytic_residues = $catalytic_residues
            """
            
            await tx.run(
                cypher,
                domain_id=f"{budo.canonical_name}:{domain.domain_id}",
                motifs=motifs,
                catalytic_residues=domain.catalytic_residues
            )

    async def query_protein_neighborhood(
        self,
        canonical_name: str,
        max_hops: int = 1,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query protein neighborhood (graph traversal).
        
        Args:
            canonical_name: Protein canonical name
            max_hops: Maximum relationship hops (1 or 2)
            user_id: Filter to nodes written by this user. Required in production.
            
        Returns:
            List of connected nodes with relationships
        """
        if user_id is None and _PROD_ENV:
            raise ValueError(
                "query_protein_neighborhood requires user_id in production"
            )
        user_filter = "AND p.user_id = $user_id" if user_id else ""
        async with self.driver.session() as session:
            if max_hops == 1:
                cypher = f"""
                MATCH (p:Protein {{canonical_name: $name}})-[r]-(connected)
                WHERE 1=1 {user_filter}
                RETURN p, type(r) as relationship, connected
                LIMIT 100
                """
            else:  # max_hops == 2
                cypher = f"""
                MATCH path = (p:Protein {{canonical_name: $name}})-[*1..2]-(connected)
                WHERE 1=1 {user_filter}
                RETURN path
                LIMIT 100
                """
            
            params: Dict[str, Any] = {"name": canonical_name}
            if user_id:
                params["user_id"] = user_id
            result = await session.run(cypher, **params)
            records = await result.data()
            return records

    async def query_phosphorylation_cascade(
        self,
        start_protein: str,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find phosphorylation cascade from starting protein.

        Args:
            start_protein: Protein canonical name
            user_id: Filter to proteins written by this user. Required in production.
        """
        if user_id is None and _PROD_ENV:
            raise ValueError(
                "query_phosphorylation_cascade requires user_id in production"
            )
        user_filter = "AND start.user_id = $user_id" if user_id else ""
        async with self.driver.session() as session:
            cypher = f"""
            MATCH path = (start:Protein {{canonical_name: $name}})-[:PHOSPHORYLATES*1..3]->(target:Protein)
            WHERE 1=1 {user_filter}
            RETURN path
            ORDER BY length(path) DESC
            LIMIT 50
            """
            
            params: Dict[str, Any] = {"name": start_protein}
            if user_id:
                params["user_id"] = user_id
            result = await session.run(cypher, **params)
            records = await result.data()
            return records

    async def query_shared_ligands(
        self,
        protein1: str,
        protein2: str,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find ligands shared by two proteins.

        Args:
            protein1: First protein canonical name
            protein2: Second protein canonical name
            user_id: Filter to proteins written by this user. Required in production.
        """
        if user_id is None and _PROD_ENV:
            raise ValueError(
                "query_shared_ligands requires user_id in production"
            )
        user_filter = "AND p1.user_id = $user_id" if user_id else ""
        async with self.driver.session() as session:
            cypher = f"""
            MATCH (p1:Protein {{canonical_name: $protein1}})-[:BINDS_LIGAND]->(c:Compound)<-[:BINDS_LIGAND]-(p2:Protein {{canonical_name: $protein2}})
            WHERE 1=1 {user_filter}
            RETURN c.compound_id as compound_id, c.compound_type as type
            """
            
            params: Dict[str, Any] = {"protein1": protein1, "protein2": protein2}
            if user_id:
                params["user_id"] = user_id
            result = await session.run(cypher, **params)
            records = await result.data()
            return records


# ============================================================================
# DUAL-WRITE COORDINATOR
# ============================================================================

class DualWriteCoordinator:
    """
    Coordinates dual writes to Timescale (operational) + Neo4j (analytical).
    
    Usage:
        coordinator = DualWriteCoordinator(timescale_pool, neo4j_driver)
        await coordinator.upsert_budo(budo)
    """
    
    def __init__(self, timescale_pool, neo4j_driver: AsyncDriver):
        from mica.infrastructure.persistence.budo_graph_writer import BudoGraphWriter
        
        self.timescale_writer = BudoGraphWriter(timescale_pool)
        self.neo4j_writer = BudoNeo4jWriter(neo4j_driver)
    
    async def upsert_budo(
        self,
        budo: BudoV3,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Write BUDO to both Timescale and Neo4j.
        
        Returns:
            dict: {'timescale_node_id': uuid, 'neo4j_protein_id': canonical_name}
        """
        # Write to Timescale (operational GraphRAG)
        timescale_node_id = await self.timescale_writer.upsert_budo_as_graph(
            budo, user_id, session_id
        )
        
        # Write to Neo4j (analytical graph)
        neo4j_protein_id = await self.neo4j_writer.upsert_budo_as_graph(
            budo, user_id, session_id
        )
        
        return {
            'timescale_node_id': timescale_node_id,
            'neo4j_protein_id': neo4j_protein_id
        }


# ============================================================================
# ASYNC CONTEXT MANAGER
# ============================================================================

class BudoNeo4jWriterFactory:
    """
    Factory for creating BudoNeo4jWriter instances.
    
    Usage:
        async with BudoNeo4jWriterFactory(uri, user, password) as writer:
            await writer.upsert_budo_as_graph(budo)
    """
    
    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.user = user
        self.password = password
        self.driver: Optional[AsyncDriver] = None
    
    async def __aenter__(self) -> BudoNeo4jWriter:
        self.driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password)
        )
        return BudoNeo4jWriter(self.driver)
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.driver:
            await self.driver.close()


# ============================================================================
# SYNCHRONOUS WRAPPER
# ============================================================================

def upsert_budo_neo4j_sync(uri: str, user: str, password: str, budo: BudoV3) -> str:
    """
    Synchronous wrapper for upsert_budo_as_graph.
    
    Args:
        uri: Neo4j URI (e.g., "bolt://localhost:7687")
        user: Neo4j username
        password: Neo4j password
        budo: BudoV3 object
        
    Returns:
        protein_id: Canonical name
    """
    async def _run():
        async with BudoNeo4jWriterFactory(uri, user, password) as writer:
            return await writer.upsert_budo_as_graph(budo)
    
    return asyncio.run(_run())

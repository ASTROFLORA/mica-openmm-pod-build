"""
BUDO V3 Neo4j Service Layer
============================

Service layer for BUDO V3 objects in Neo4j graph database.
Implements Phase 2 of BSM-BUDO-CEA roadmap.

Author: Alex Rodriguez
Date: October 8, 2025
Version: 1.0.0
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

from neo4j import GraphDatabase, Session
from ..schemas.budo_v3 import BudoV3, BudoDomain, BudoVariant
from ..schemas.biosite_v3 import BioSiteV3

logger = logging.getLogger(__name__)


class BudoNeo4jService:
    """
    Neo4j service for BUDO V3 objects.
    
    Implements:
    - BUDO node creation with full schema
    - Domain and variant relationships
    - BioSite linkage
    - Cross-reference mapping
    - Provenance tracking
    """
    
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info(f"Connected to Neo4j: {uri}")
    
    def close(self):
        """Close Neo4j connection"""
        self.driver.close()
    
    def create_constraints(self):
        """Create Neo4j constraints and indexes for BUDO schema"""
        
        constraints = [
            # BUDO node constraints
            "CREATE CONSTRAINT budo_id_unique IF NOT EXISTS FOR (b:BUDO) REQUIRE b.budo_id IS UNIQUE",
            "CREATE CONSTRAINT budo_canonical_id_unique IF NOT EXISTS FOR (b:BUDO) REQUIRE b.canonical_id IS UNIQUE",
            
            # BioSite constraints
            "CREATE CONSTRAINT biosite_id_unique IF NOT EXISTS FOR (s:BioSite) REQUIRE s.biosite_id IS UNIQUE",
            
            # Domain constraints
            "CREATE CONSTRAINT domain_id_unique IF NOT EXISTS FOR (d:Domain) REQUIRE d.domain_id IS UNIQUE",
            
            # Variant constraints
            "CREATE CONSTRAINT variant_id_unique IF NOT EXISTS FOR (v:Variant) REQUIRE v.variant_id IS UNIQUE",
        ]
        
        indexes = [
            # BUDO indexes
            "CREATE INDEX budo_gene_symbol IF NOT EXISTS FOR (b:BUDO) ON (b.gene_symbol)",
            "CREATE INDEX budo_organism IF NOT EXISTS FOR (b:BUDO) ON (b.organism)",
            "CREATE INDEX budo_functional_state IF NOT EXISTS FOR (b:BUDO) ON (b.functional_state)",
            
            # BioSite indexes
            "CREATE INDEX biosite_type IF NOT EXISTS FOR (s:BioSite) ON (s.site_type)",
            "CREATE INDEX biosite_conf_state IF NOT EXISTS FOR (s:BioSite) ON (s.conformational_state)",
            
            # Full-text search
            "CREATE FULLTEXT INDEX budo_fulltext IF NOT EXISTS FOR (b:BUDO) ON EACH [b.name, b.description, b.gene_symbol]",
        ]
        
        with self.driver.session() as session:
            for constraint in constraints:
                try:
                    session.run(constraint)
                    logger.info(f"Created constraint: {constraint[:50]}...")
                except Exception as e:
                    logger.warning(f"Constraint already exists or error: {e}")
            
            for index in indexes:
                try:
                    session.run(index)
                    logger.info(f"Created index: {index[:50]}...")
                except Exception as e:
                    logger.warning(f"Index already exists or error: {e}")
    
    def create_budo_node(self, budo: BudoV3) -> str:
        """
        Create BUDO node in Neo4j with full schema.
        
        Args:
            budo: BudoV3 object
            
        Returns:
            BUDO ID of created node
        """
        query = """
        CREATE (b:BUDO {
            budo_id: $budo_id,
            canonical_id: $canonical_id,
            name: $name,
            gene_symbol: $gene_symbol,
            organism: $organism,
            description: $description,
            sequence: $sequence,
            sequence_length: $sequence_length,
            molecular_weight: $molecular_weight,
            isoelectric_point: $isoelectric_point,
            functional_state: $functional_state,
            confidence_score: $confidence_score,
            version: $version,
            created_at: datetime($created_at),
            updated_at: datetime($updated_at),
            is_canonical: $is_canonical,
            is_reviewed: $is_reviewed
        })
        RETURN b.budo_id as budo_id
        """
        
        with self.driver.session() as session:
            result = session.run(query, **budo.dict())
            record = result.single()
            
            # Create domains
            for domain in budo.domains:
                self._create_domain(session, budo.budo_id, domain)
            
            # Create variants
            for variant in budo.variants:
                self._create_variant(session, budo.budo_id, variant)
            
            # Create cross-references
            for xref in budo.cross_references:
                self._create_cross_reference(session, budo.budo_id, xref)
            
            logger.info(f"Created BUDO node: {budo.budo_id}")
            return record["budo_id"]
    
    def _create_domain(self, session: Session, budo_id: str, domain: BudoDomain):
        """Create domain node and link to BUDO"""
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        CREATE (d:Domain {
            domain_id: $domain_id,
            name: $name,
            pfam_id: $pfam_id,
            interpro_id: $interpro_id,
            start_position: $start_position,
            end_position: $end_position,
            confidence_score: $confidence_score
        })
        CREATE (b)-[:HAS_DOMAIN]->(d)
        """
        
        session.run(query, budo_id=budo_id, **domain.dict())
    
    def _create_variant(self, session: Session, budo_id: str, variant: BudoVariant):
        """Create variant node and link to BUDO"""
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        CREATE (v:Variant {
            variant_id: $variant_id,
            position: $position,
            wild_type: $wild_type,
            mutant_type: $mutant_type,
            variant_type: $variant_type,
            clinical_significance: $clinical_significance,
            population_frequency: $population_frequency,
            dbsnp_id: $dbsnp_id,
            cosmic_id: $cosmic_id
        })
        CREATE (b)-[:HAS_VARIANT]->(v)
        """
        
        session.run(query, budo_id=budo_id, **variant.dict())
    
    def _create_cross_reference(self, session: Session, budo_id: str, xref: Dict[str, Any]):
        """Create cross-reference relationship"""
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        MERGE (db:Database {name: $database})
        CREATE (b)-[r:CROSS_REFERENCE {
            identifier: $identifier,
            confidence_score: $confidence_score,
            source: $source
        }]->(db)
        """
        
        session.run(query, budo_id=budo_id, **xref)
    
    def link_biosite(self, budo_id: str, biosite: BioSiteV3):
        """
        Link BioSite to BUDO object.
        
        Creates BioSite node and relationship to parent BUDO.
        """
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        CREATE (s:BioSite {
            biosite_id: $biosite_id,
            site_type: $site_type,
            name: $name,
            description: $description,
            conformational_state: $conformational_state,
            start_residue: $start_residue,
            end_residue: $end_residue,
            confidence_score: $confidence_score
        })
        CREATE (b)-[:HAS_BIOSITE]->(s)
        """
        
        with self.driver.session() as session:
            session.run(query, budo_id=budo_id, **biosite.dict())
            logger.info(f"Linked BioSite {biosite.biosite_id} to {budo_id}")
    
    def get_budo_by_id(self, budo_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve BUDO node by ID"""
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        OPTIONAL MATCH (b)-[:HAS_DOMAIN]->(d:Domain)
        OPTIONAL MATCH (b)-[:HAS_VARIANT]->(v:Variant)
        OPTIONAL MATCH (b)-[:HAS_BIOSITE]->(s:BioSite)
        RETURN b, collect(DISTINCT d) as domains, 
               collect(DISTINCT v) as variants,
               collect(DISTINCT s) as biosites
        """
        
        with self.driver.session() as session:
            result = session.run(query, budo_id=budo_id)
            record = result.single()
            
            if record is None:
                return None
            
            return {
                "budo": dict(record["b"]),
                "domains": [dict(d) for d in record["domains"] if d],
                "variants": [dict(v) for v in record["variants"] if v],
                "biosites": [dict(s) for s in record["biosites"] if s],
            }
    
    def search_budos(
        self,
        gene_symbol: Optional[str] = None,
        organism: Optional[str] = None,
        functional_state: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Search BUDO nodes by criteria"""
        
        conditions = []
        params = {"limit": limit}
        
        if gene_symbol:
            conditions.append("b.gene_symbol = $gene_symbol")
            params["gene_symbol"] = gene_symbol
        
        if organism:
            conditions.append("b.organism = $organism")
            params["organism"] = organism
        
        if functional_state:
            conditions.append("b.functional_state = $functional_state")
            params["functional_state"] = functional_state
        
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        
        query = f"""
        MATCH (b:BUDO)
        {where_clause}
        RETURN b
        LIMIT $limit
        """
        
        with self.driver.session() as session:
            result = session.run(query, **params)
            return [dict(record["b"]) for record in result]
    
    def update_functional_state(self, budo_id: str, new_state: str, reason: str):
        """
        Update BUDO functional state (sentient mutation).
        
        This represents the 'sentient' aspect of BUDO objects - they
        can change state based on experimental observations or AI inference.
        """
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        SET b.functional_state = $new_state,
            b.updated_at = datetime(),
            b.last_mutation_reason = $reason
        RETURN b.budo_id as budo_id, b.functional_state as state
        """
        
        with self.driver.session() as session:
            result = session.run(
                query,
                budo_id=budo_id,
                new_state=new_state,
                reason=reason
            )
            record = result.single()
            
            if record:
                logger.info(f"Updated {budo_id} functional state to {new_state}")
                return record
            else:
                logger.warning(f"BUDO {budo_id} not found")
                return None
    
    def create_variant_node(self, budo_id: str, variant: 'BudoVariant'):
        """
        Create Variant node and link to BUDO
        
        Args:
            budo_id: Parent BUDO ID
            variant: BudoVariant object with mutation details
        
        Returns:
            variant_id if created successfully
        
        Example:
            >>> variant = BudoVariant(
            ...     variant_id="var:WNK1-D368A-001",
            ...     position=368,
            ...     original_aa="D",
            ...     variant_aa="A",
            ...     mutation_type="Missense",
            ...     clinical_significance="Pathogenic"
            ... )
            >>> service.create_variant_node("budo:WNK1-S-001", variant)
        """
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        CREATE (v:Variant {
            variant_id: $variant_id,
            position: $position,
            original_aa: $original_aa,
            variant_aa: $variant_aa,
            mutation_type: $mutation_type,
            clinical_significance: $clinical_significance,
            disease_association: $disease_association,
            population_frequency: $population_frequency,
            dbsnp_id: $dbsnp_id,
            created_at: datetime()
        })
        CREATE (b)-[:HAS_VARIANT {impact: $impact}]->(v)
        RETURN v.variant_id AS variant_id
        """
        
        with self.driver.session() as session:
            result = session.run(
                query,
                budo_id=budo_id,
                variant_id=variant.variant_id,
                position=variant.position,
                original_aa=variant.original_aa,
                variant_aa=variant.variant_aa,
                mutation_type=getattr(variant, 'mutation_type', 'Unknown'),
                clinical_significance=getattr(variant, 'clinical_significance', 'Unknown'),
                disease_association=getattr(variant, 'disease_association', []),
                population_frequency=getattr(variant, 'population_frequency', None),
                dbsnp_id=getattr(variant, 'dbsnp_id', None),
                impact=getattr(variant, 'impact', 'Unknown')
            )
            
            record = result.single()
            if record:
                logger.info(f"Created variant {variant.variant_id} for {budo_id}")
                return record["variant_id"]
            else:
                logger.error(f"Failed to create variant for {budo_id}")
                return None
    
    def create_cross_reference(self, budo_id: str, database: str, db_id: str, url: Optional[str] = None):
        """
        Create cross-reference to external database
        
        Args:
            budo_id: Parent BUDO ID
            database: Database name (e.g., "UniProt", "PDB", "Ensembl")
            db_id: Database-specific ID (e.g., "P51617", "1Q3W")
            url: Full URL to resource (optional, auto-generated if not provided)
        
        Returns:
            True if created successfully
        
        Example:
            >>> service.create_cross_reference("budo:WNK1-S-001", "PDB", "1Q3W")
            >>> service.create_cross_reference("budo:WNK1-S-001", "Ensembl", "ENSG00000187555")
        """
        # Auto-generate URL if not provided
        if url is None:
            url_templates = {
                "UniProt": "http://purl.uniprot.org/uniprot/{db_id}",
                "PDB": "https://www.rcsb.org/structure/{db_id}",
                "Ensembl": "http://www.ensembl.org/id/{db_id}",
                "NCBI": "https://www.ncbi.nlm.nih.gov/gene/{db_id}",
                "Pfam": "https://www.ebi.ac.uk/interpro/entry/pfam/{db_id}",
                "InterPro": "https://www.ebi.ac.uk/interpro/entry/InterPro/{db_id}"
            }
            
            if database in url_templates:
                url = url_templates[database].format(db_id=db_id)
            else:
                url = f"https://example.org/{database}/{db_id}"  # Fallback
        
        query = """
        MATCH (b:BUDO {budo_id: $budo_id})
        MERGE (d:Database {db_name: $database, db_id: $db_id})
        ON CREATE SET d.url = $url, d.created_at = datetime()
        MERGE (b)-[:CROSS_REFERENCE {source: $database}]->(d)
        RETURN d.db_id AS db_id
        """
        
        with self.driver.session() as session:
            result = session.run(
                query,
                budo_id=budo_id,
                database=database,
                db_id=db_id,
                url=url
            )
            
            record = result.single()
            if record:
                logger.info(f"Created cross-reference {database}:{db_id} for {budo_id}")
                return True
            else:
                logger.error(f"Failed to create cross-reference for {budo_id}")
                return False
    
    def create_protein_interaction(self, 
                                    source_budo_id: str, 
                                    target_budo_id: str, 
                                    interaction_type: str,
                                    evidence: Optional[str] = None,
                                    confidence: Optional[float] = None):
        """
        Create protein-protein interaction relationship
        
        Args:
            source_budo_id: Source BUDO ID (e.g., "budo:WNK1-S-001")
            target_budo_id: Target BUDO ID (e.g., "budo:STK39-S-001")
            interaction_type: Type of interaction (e.g., "Phosphorylation", "Binding", "Activation")
            evidence: Evidence code (e.g., "Y2H", "Co-IP", "Computational prediction")
            confidence: Confidence score (0.0 - 1.0)
        
        Returns:
            True if created successfully
        
        Example:
            >>> # WNK1 phosphorylates STK39
            >>> service.create_protein_interaction(
            ...     "budo:WNK1-S-001",
            ...     "budo:STK39-S-001",
            ...     "Phosphorylation",
            ...     evidence="Kinase assay",
            ...     confidence=0.95
            ... )
        """
        query = """
        MATCH (source:BUDO {budo_id: $source_id})
        MATCH (target:BUDO {budo_id: $target_id})
        MERGE (source)-[i:INTERACTS_WITH {interaction_type: $interaction_type}]->(target)
        ON CREATE SET 
            i.evidence = $evidence,
            i.confidence = $confidence,
            i.created_at = datetime()
        RETURN i.interaction_type AS interaction_type
        """
        
        with self.driver.session() as session:
            result = session.run(
                query,
                source_id=source_budo_id,
                target_id=target_budo_id,
                interaction_type=interaction_type,
                evidence=evidence or "Unknown",
                confidence=confidence
            )
            
            record = result.single()
            if record:
                logger.info(f"Created interaction {source_budo_id} --[{interaction_type}]--> {target_budo_id}")
                return True
            else:
                logger.error(f"Failed to create interaction (check if both BUDOs exist)")
                return False

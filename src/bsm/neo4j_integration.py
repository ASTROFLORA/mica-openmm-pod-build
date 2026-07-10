#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🗂️ BSM NEO4J INTEGRATION
Integración con Neo4j para Biological Semantic Memory - Almacenamiento de datos explícitos
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass
from contextlib import asynccontextmanager
from urllib.parse import urlparse

# Neo4j imports
try:
    from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession
    from neo4j.exceptions import Neo4jError, ServiceUnavailable
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False
    AsyncGraphDatabase = None
    AsyncDriver = None
    AsyncSession = None
    Neo4jError = Exception
    ServiceUnavailable = Exception

from .config import BSMConfig, get_bsm_config, StorageBackend

logger = logging.getLogger(__name__)

# === MODELOS DE DATOS NEO4J ===

@dataclass
class ProteinNode:
    """Nodo de proteína en el grafo"""
    identifier: str
    name: str
    sequence: Optional[str] = None
    organism: Optional[str] = None
    function: Optional[str] = None
    keywords: List[str] = None
    properties: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []
        if self.properties is None:
            self.properties = {}

@dataclass
class BioSchemasNode:
    """Nodo BioSchemas en el grafo"""
    context: str
    type: str
    identifier: str
    data: Dict[str, Any]
    created_at: datetime
    updated_at: Optional[datetime] = None

@dataclass
class RelationshipData:
    """Datos de relación entre nodos"""
    source_id: str
    target_id: str
    relationship_type: str
    properties: Dict[str, Any] = None
    confidence: float = 1.0
    source: str = "BSM"
    
    def __post_init__(self):
        if self.properties is None:
            self.properties = {}

# === ESQUEMAS DE GRAFO BSM ===

class BSMGraphSchema:
    """Esquemas y constraints para el grafo BSM"""
    
    # Tipos de nodos
    NODE_TYPES = {
        "Protein": "Protein",
        "Gene": "Gene", 
        "Function": "Function",
        "Pathway": "Pathway",
        "Disease": "Disease",
        "Compound": "Compound",
        "Publication": "Publication",
        "BioSchema": "BioSchema",
        "Embedding": "Embedding"
    }
    
    # Tipos de relaciones
    RELATIONSHIP_TYPES = {
        "ENCODED_BY": "ENCODED_BY",         # Protein -> Gene
        "HAS_FUNCTION": "HAS_FUNCTION",     # Protein -> Function
        "PART_OF": "PART_OF",               # Function -> Pathway
        "ASSOCIATED_WITH": "ASSOCIATED_WITH", # Protein -> Disease
        "INTERACTS_WITH": "INTERACTS_WITH", # Protein -> Protein
        "BINDS_TO": "BINDS_TO",             # Protein -> Compound
        "CITED_IN": "CITED_IN",             # Protein -> Publication
        "REPRESENTS": "REPRESENTS",         # BioSchema -> Protein
        "SIMILAR_TO": "SIMILAR_TO",         # Protein -> Protein (via embeddings)
        "HAS_EMBEDDING": "HAS_EMBEDDING"    # Protein -> Embedding
    }
    
    @classmethod
    def get_constraints(cls) -> List[str]:
        """Obtiene constraints para el esquema"""
        return [
            # Constraints de unicidad
            "CREATE CONSTRAINT protein_id_unique IF NOT EXISTS FOR (p:Protein) REQUIRE p.identifier IS UNIQUE",
            "CREATE CONSTRAINT gene_id_unique IF NOT EXISTS FOR (g:Gene) REQUIRE g.identifier IS UNIQUE",
            "CREATE CONSTRAINT bioschema_id_unique IF NOT EXISTS FOR (b:BioSchema) REQUIRE b.identifier IS UNIQUE",
            "CREATE CONSTRAINT embedding_id_unique IF NOT EXISTS FOR (e:Embedding) REQUIRE e.identifier IS UNIQUE",
            
            # Índices para búsqueda rápida
            "CREATE INDEX protein_name_index IF NOT EXISTS FOR (p:Protein) ON (p.name)",
            "CREATE INDEX protein_organism_index IF NOT EXISTS FOR (p:Protein) ON (p.organism)",
            "CREATE INDEX bioschema_type_index IF NOT EXISTS FOR (b:BioSchema) ON (b.type)",
            "CREATE INDEX bioschema_created_index IF NOT EXISTS FOR (b:BioSchema) ON (b.created_at)",
            
            # Índices de texto completo
            "CREATE FULLTEXT INDEX protein_fulltext_index IF NOT EXISTS FOR (p:Protein) ON EACH [p.name, p.function, p.keywords]",
            "CREATE FULLTEXT INDEX publication_fulltext_index IF NOT EXISTS FOR (pub:Publication) ON EACH [pub.title, pub.abstract]"
        ]
    
    @classmethod
    def get_initial_data(cls) -> List[str]:
        """Obtiene datos iniciales para el grafo"""
        return [
            # Crear nodos de funciones básicas
            """
            MERGE (f1:Function {identifier: 'enzymatic_activity', name: 'Enzymatic Activity'})
            MERGE (f2:Function {identifier: 'binding', name: 'Binding'})
            MERGE (f3:Function {identifier: 'structural', name: 'Structural'})
            MERGE (f4:Function {identifier: 'transport', name: 'Transport'})
            MERGE (f5:Function {identifier: 'signaling', name: 'Signaling'})
            """,
            
            # Crear nodos de organismos modelo
            """
            MERGE (o1:Organism {identifier: 'homo_sapiens', name: 'Homo sapiens'})
            MERGE (o2:Organism {identifier: 'mus_musculus', name: 'Mus musculus'})
            MERGE (o3:Organism {identifier: 'escherichia_coli', name: 'Escherichia coli'})
            MERGE (o4:Organism {identifier: 'saccharomyces_cerevisiae', name: 'Saccharomyces cerevisiae'})
            """
        ]

# === INTEGRACIÓN NEO4J BSM ===

class BSMNeo4jIntegration:
    """Integración principal con Neo4j para BSM"""
    
    def __init__(self, config: Optional[BSMConfig] = None):
        """
        Inicializa integración Neo4j
        
        Args:
            config: Configuración BSM (opcional)
        """
        if not NEO4J_AVAILABLE:
            raise ImportError("Neo4j driver not available. Install with: pip install neo4j")
        
        self.config = config or get_bsm_config()
        self.driver: Optional[AsyncDriver] = None
        self.schema = BSMGraphSchema()
        self._connection_pool_initialized = False
        
    async def initialize(self):
        """Inicializa conexión y esquema"""
        await self.connect()
        await self.setup_schema()
        logger.info("🗂️ BSM Neo4j integration initialized successfully")
    
    async def connect(self):
        """Establece conexión con Neo4j"""
        try:
            neo4j_config = self.config.neo4j
            
            parsed_scheme = urlparse(neo4j_config.uri).scheme.lower()
            secure_schemes = {"neo4j+s", "neo4j+ssc", "bolt+s", "bolt+ssc"}

            driver_kwargs = {
                "auth": (neo4j_config.username, neo4j_config.password),
                "max_connection_lifetime": neo4j_config.max_connection_lifetime,
                "max_connection_pool_size": neo4j_config.max_connection_pool_size,
                "connection_timeout": neo4j_config.connection_timeout,
            }

            # Note: encrypted and trust parameters are deprecated in neo4j >= 5.0
            # Use scheme-based configuration (neo4j+s, bolt+s) instead
            # if parsed_scheme not in secure_schemes:
            #     driver_kwargs["encrypted"] = neo4j_config.encrypted

            self.driver = AsyncGraphDatabase.driver(
                neo4j_config.uri,
                **driver_kwargs,
            )
            
            # Verificar conexión
            await self.driver.verify_connectivity()
            logger.info(f"✅ Connected to Neo4j: {neo4j_config.uri}")
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to Neo4j: {e}")
            raise
    
    async def disconnect(self):
        """Cierra conexión con Neo4j"""
        if self.driver:
            await self.driver.close()
            logger.info("🔌 Neo4j connection closed")
    
    async def setup_schema(self):
        """Configura esquema y constraints"""
        try:
            async with self.driver.session(database=self.config.neo4j.database) as session:
                # Crear constraints
                for constraint in self.schema.get_constraints():
                    try:
                        await session.run(constraint)
                    except Neo4jError as e:
                        if "already exists" not in str(e).lower():
                            logger.warning(f"⚠️ Constraint error: {e}")
                
                # Insertar datos iniciales si no existen
                for initial_query in self.schema.get_initial_data():
                    await session.run(initial_query)
                
                logger.info("📋 Neo4j schema configured successfully")
                
        except Exception as e:
            logger.error(f"❌ Schema setup failed: {e}")
            raise
    
    @asynccontextmanager
    async def session(self, database: Optional[str] = None):
        """Context manager para sesiones Neo4j"""
        db = database or self.config.neo4j.database
        async with self.driver.session(database=db) as session:
            yield session
    
    # === OPERACIONES DE PROTEÍNAS ===
    
    async def create_protein_node(self, protein: ProteinNode) -> bool:
        """
        Crea nodo de proteína en el grafo
        
        Args:
            protein: Datos de la proteína
            
        Returns:
            bool: True si se creó exitosamente
        """
        try:
            query = """
            MERGE (p:Protein {identifier: $identifier})
            SET p.name = $name,
                p.sequence = $sequence,
                p.organism = $organism,
                p.function = $function,
                p.keywords = $keywords,
                p.created_at = datetime(),
                p.updated_at = datetime()
            SET p += $properties
            RETURN p.identifier as id
            """
            
            async with self.session() as session:
                result = await session.run(query, {
                    "identifier": protein.identifier,
                    "name": protein.name,
                    "sequence": protein.sequence,
                    "organism": protein.organism,
                    "function": protein.function,
                    "keywords": protein.keywords,
                    "properties": protein.properties
                })
                
                record = await result.single()
                if record:
                    logger.debug(f"✅ Created protein node: {record['id']}")
                    return True
                return False
                
        except Exception as e:
            logger.error(f"❌ Error creating protein node {protein.identifier}: {e}")
            return False
    
    async def get_protein_by_id(self, identifier: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene proteína por identificador
        
        Args:
            identifier: ID de la proteína
            
        Returns:
            Dict con datos de la proteína o None
        """
        try:
            query = """
            MATCH (p:Protein {identifier: $identifier})
            RETURN p
            """
            
            async with self.session() as session:
                result = await session.run(query, {"identifier": identifier})
                record = await result.single()
                
                if record:
                    return dict(record["p"])
                return None
                
        except Exception as e:
            logger.error(f"❌ Error getting protein {identifier}: {e}")
            return None
    
    async def search_proteins(self, 
                            query: str, 
                            limit: int = 50,
                            organism: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Búsqueda de proteínas por texto
        
        Args:
            query: Texto de búsqueda
            limit: Límite de resultados
            organism: Filtro por organismo
            
        Returns:
            Lista de proteínas encontradas
        """
        try:
            cypher_query = """
            CALL db.index.fulltext.queryNodes('protein_fulltext_index', $query)
            YIELD node, score
            """
            
            if organism:
                cypher_query += " WHERE node.organism = $organism"
            
            cypher_query += """
            RETURN node as protein, score
            ORDER BY score DESC
            LIMIT $limit
            """
            
            params = {"query": query, "limit": limit}
            if organism:
                params["organism"] = organism
            
            async with self.session() as session:
                result = await session.run(cypher_query, params)
                
                proteins = []
                async for record in result:
                    protein_data = dict(record["protein"])
                    protein_data["search_score"] = record["score"]
                    proteins.append(protein_data)
                
                logger.debug(f"🔍 Found {len(proteins)} proteins for query: {query}")
                return proteins
                
        except Exception as e:
            logger.error(f"❌ Error searching proteins: {e}")
            return []
    
    # === OPERACIONES DE BIOSCHEMAS ===
    
    async def create_bioschemas_node(self, bioschemas: BioSchemasNode) -> bool:
        """
        Crea nodo BioSchemas en el grafo
        
        Args:
            bioschemas: Datos BioSchemas
            
        Returns:
            bool: True si se creó exitosamente
        """
        try:
            query = """
            MERGE (b:BioSchema {identifier: $identifier})
            SET b.context = $context,
                b.type = $type,
                b.data = $data,
                b.created_at = $created_at,
                b.updated_at = datetime()
            RETURN b.identifier as id
            """
            
            async with self.session() as session:
                result = await session.run(query, {
                    "identifier": bioschemas.identifier,
                    "context": bioschemas.context,
                    "type": bioschemas.type,
                    "data": json.dumps(bioschemas.data),
                    "created_at": bioschemas.created_at.isoformat()
                })
                
                record = await result.single()
                if record:
                    logger.debug(f"✅ Created BioSchemas node: {record['id']}")
                    return True
                return False
                
        except Exception as e:
            logger.error(f"❌ Error creating BioSchemas node {bioschemas.identifier}: {e}")
            return False
    
    async def link_bioschemas_to_protein(self, 
                                       bioschemas_id: str, 
                                       protein_id: str,
                                       properties: Optional[Dict[str, Any]] = None) -> bool:
        """
        Vincula nodo BioSchemas con proteína
        
        Args:
            bioschemas_id: ID del nodo BioSchemas
            protein_id: ID de la proteína
            properties: Propiedades adicionales de la relación
            
        Returns:
            bool: True si se vinculó exitosamente
        """
        try:
            query = """
            MATCH (b:BioSchema {identifier: $bioschemas_id})
            MATCH (p:Protein {identifier: $protein_id})
            MERGE (b)-[r:REPRESENTS]->(p)
            SET r.created_at = datetime()
            """
            
            if properties:
                query += " SET r += $properties"
            
            query += " RETURN r"
            
            params = {
                "bioschemas_id": bioschemas_id,
                "protein_id": protein_id
            }
            if properties:
                params["properties"] = properties
            
            async with self.session() as session:
                result = await session.run(query, params)
                record = await result.single()
                
                if record:
                    logger.debug(f"✅ Linked BioSchemas {bioschemas_id} to protein {protein_id}")
                    return True
                return False
                
        except Exception as e:
            logger.error(f"❌ Error linking BioSchemas to protein: {e}")
            return False
    
    # === OPERACIONES DE RELACIONES ===
    
    async def create_relationship(self, relationship: RelationshipData) -> bool:
        """
        Crea relación entre nodos
        
        Args:
            relationship: Datos de la relación
            
        Returns:
            bool: True si se creó exitosamente
        """
        try:
            # Query dinámico basado en el tipo de relación
            query = f"""
            MATCH (source {{identifier: $source_id}})
            MATCH (target {{identifier: $target_id}})
            MERGE (source)-[r:{relationship.relationship_type}]->(target)
            SET r.confidence = $confidence,
                r.source = $source,
                r.created_at = datetime()
            SET r += $properties
            RETURN r
            """
            
            async with self.session() as session:
                result = await session.run(query, {
                    "source_id": relationship.source_id,
                    "target_id": relationship.target_id,
                    "confidence": relationship.confidence,
                    "source": relationship.source,
                    "properties": relationship.properties
                })
                
                record = await result.single()
                if record:
                    logger.debug(f"✅ Created relationship: {relationship.source_id} -{relationship.relationship_type}-> {relationship.target_id}")
                    return True
                return False
                
        except Exception as e:
            logger.error(f"❌ Error creating relationship: {e}")
            return False
    
    async def get_protein_relationships(self, 
                                      protein_id: str,
                                      relationship_types: Optional[List[str]] = None,
                                      limit: int = 100) -> List[Dict[str, Any]]:
        """
        Obtiene relaciones de una proteína
        
        Args:
            protein_id: ID de la proteína
            relationship_types: Tipos de relación a filtrar
            limit: Límite de resultados
            
        Returns:
            Lista de relaciones
        """
        try:
            query = """
            MATCH (p:Protein {identifier: $protein_id})-[r]->(target)
            """
            
            if relationship_types:
                types_filter = "|".join(relationship_types)
                query = f"""
                MATCH (p:Protein {{identifier: $protein_id}})-[r:{types_filter}]->(target)
                """
            
            query += """
            RETURN type(r) as relationship_type,
                   r as relationship,
                   target,
                   labels(target) as target_labels
            ORDER BY r.confidence DESC
            LIMIT $limit
            """
            
            async with self.session() as session:
                result = await session.run(query, {
                    "protein_id": protein_id,
                    "limit": limit
                })
                
                relationships = []
                async for record in result:
                    relationships.append({
                        "relationship_type": record["relationship_type"],
                        "relationship": dict(record["relationship"]),
                        "target": dict(record["target"]),
                        "target_labels": record["target_labels"]
                    })
                
                logger.debug(f"🔗 Found {len(relationships)} relationships for protein {protein_id}")
                return relationships
                
        except Exception as e:
            logger.error(f"❌ Error getting protein relationships: {e}")
            return []
    
    # === OPERACIONES DE BATCH ===
    
    async def batch_create_proteins(self, proteins: List[ProteinNode]) -> Dict[str, Any]:
        """
        Crea múltiples proteínas en lote
        
        Args:
            proteins: Lista de proteínas a crear
            
        Returns:
            Dict con estadísticas del procesamiento
        """
        success_count = 0
        error_count = 0
        errors = []
        
        try:
            async with self.session() as session:
                async with session.begin_transaction() as tx:
                    for protein in proteins:
                        try:
                            query = """
                            MERGE (p:Protein {identifier: $identifier})
                            SET p.name = $name,
                                p.sequence = $sequence,
                                p.organism = $organism,
                                p.function = $function,
                                p.keywords = $keywords,
                                p.created_at = datetime(),
                                p.updated_at = datetime()
                            SET p += $properties
                            """
                            
                            await tx.run(query, {
                                "identifier": protein.identifier,
                                "name": protein.name,
                                "sequence": protein.sequence,
                                "organism": protein.organism,
                                "function": protein.function,
                                "keywords": protein.keywords,
                                "properties": protein.properties
                            })
                            
                            success_count += 1
                            
                        except Exception as e:
                            error_count += 1
                            errors.append(f"Protein {protein.identifier}: {str(e)}")
                            logger.warning(f"⚠️ Error in batch protein creation: {e}")
                    
                    await tx.commit()
            
            logger.info(f"📦 Batch protein creation: {success_count} success, {error_count} errors")
            
            return {
                "success_count": success_count,
                "error_count": error_count,
                "total_processed": len(proteins),
                "errors": errors[:10]  # Limitar errores mostrados
            }
            
        except Exception as e:
            logger.error(f"❌ Batch protein creation failed: {e}")
            return {
                "success_count": 0,
                "error_count": len(proteins),
                "total_processed": len(proteins),
                "errors": [str(e)]
            }
    
    # === OPERACIONES DE ANÁLISIS ===
    
    async def get_graph_statistics(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas del grafo
        
        Returns:
            Dict con estadísticas
        """
        try:
            queries = {
                "total_nodes": "MATCH (n) RETURN count(n) as count",
                "total_relationships": "MATCH ()-[r]->() RETURN count(r) as count",
                "protein_count": "MATCH (p:Protein) RETURN count(p) as count",
                "bioschemas_count": "MATCH (b:BioSchema) RETURN count(b) as count",
                "function_count": "MATCH (f:Function) RETURN count(f) as count",
                "organisms": """
                    MATCH (p:Protein) 
                    WHERE p.organism IS NOT NULL 
                    RETURN p.organism as organism, count(p) as count 
                    ORDER BY count DESC LIMIT 10
                """,
                "relationship_types": """
                    MATCH ()-[r]->() 
                    RETURN type(r) as type, count(r) as count 
                    ORDER BY count DESC
                """
            }
            
            stats = {}
            
            async with self.session() as session:
                for stat_name, query in queries.items():
                    result = await session.run(query)
                    
                    if stat_name in ["organisms", "relationship_types"]:
                        stats[stat_name] = []
                        async for record in result:
                            stats[stat_name].append(dict(record))
                    else:
                        record = await result.single()
                        stats[stat_name] = record["count"] if record else 0
            
            logger.debug(f"📊 Graph statistics retrieved: {stats.get('total_nodes', 0)} nodes")
            return stats
            
        except Exception as e:
            logger.error(f"❌ Error getting graph statistics: {e}")
            return {}
    
    async def find_similar_proteins(self, 
                                  protein_id: str,
                                  similarity_threshold: float = 0.7,
                                  limit: int = 20) -> List[Dict[str, Any]]:
        """
        Encuentra proteínas similares por relaciones
        
        Args:
            protein_id: ID de la proteína de referencia
            similarity_threshold: Umbral de similitud
            limit: Límite de resultados
            
        Returns:
            Lista de proteínas similares
        """
        try:
            query = """
            MATCH (p1:Protein {identifier: $protein_id})
            MATCH (p1)-[r:SIMILAR_TO]-(p2:Protein)
            WHERE r.confidence >= $threshold
            RETURN p2 as protein, 
                   r.confidence as similarity,
                   r.method as method
            ORDER BY r.confidence DESC
            LIMIT $limit
            """
            
            async with self.session() as session:
                result = await session.run(query, {
                    "protein_id": protein_id,
                    "threshold": similarity_threshold,
                    "limit": limit
                })
                
                similar_proteins = []
                async for record in result:
                    similar_proteins.append({
                        "protein": dict(record["protein"]),
                        "similarity": record["similarity"],
                        "method": record["method"]
                    })
                
                logger.debug(f"🔍 Found {len(similar_proteins)} similar proteins for {protein_id}")
                return similar_proteins
                
        except Exception as e:
            logger.error(f"❌ Error finding similar proteins: {e}")
            return []

# === UTILIDADES ===

async def create_bsm_neo4j_integration(config: Optional[BSMConfig] = None) -> BSMNeo4jIntegration:
    """
    Crea e inicializa integración Neo4j para BSM
    
    Args:
        config: Configuración BSM
        
    Returns:
        BSMNeo4jIntegration inicializada
    """
    integration = BSMNeo4jIntegration(config)
    await integration.initialize()
    return integration

Neo4jClient = BSMNeo4jIntegration

# === EXPORTACIONES ===

__all__ = [
    "BSMNeo4jIntegration",
    "Neo4jClient",
    "ProteinNode", 
    "BioSchemasNode",
    "RelationshipData",
    "BSMGraphSchema",
    "create_bsm_neo4j_integration"
]
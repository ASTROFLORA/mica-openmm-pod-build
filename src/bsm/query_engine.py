#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔍 BSM QUERY ENGINE
Motor de consultas dual-core que combina Neo4j + Milvus para GraphRAG biológico
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass
from enum import Enum
import numpy as np

from .config import BSMConfig, get_bsm_config
from .neo4j_integration import BSMNeo4jIntegration, ProteinNode, RelationshipData
from .milvus_integration import BSMMilvusIntegration, ProteinEmbedding, SimilarityResult
from .identity_resolver import (
    ProteinIdentity,
    detect_id_type,
    resolve_identity_from_hit,
    merge_identities,
)

logger = logging.getLogger(__name__)

# === TIPOS DE CONSULTA ===

class QueryType(Enum):
    """Tipos de consulta BSM"""
    SEMANTIC_SEARCH = "semantic_search"          # Búsqueda por similaridad semántica
    GRAPH_TRAVERSAL = "graph_traversal"          # Navegación por grafo de conocimiento
    HYBRID_QUERY = "hybrid_query"                # Combinación de ambos enfoques
    PROTEIN_ANALYSIS = "protein_analysis"        # Análisis específico de proteínas
    PATHWAY_DISCOVERY = "pathway_discovery"      # Descubrimiento de pathways
    FUNCTION_PREDICTION = "function_prediction"   # Predicción de funciones

class QueryPriority(Enum):
    """Prioridad de consulta"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

# === MODELOS DE CONSULTA ===

@dataclass
class BSMQuery:
    """Consulta BSM estructurada"""
    query_text: str
    query_type: QueryType
    priority: QueryPriority = QueryPriority.MEDIUM
    parameters: Dict[str, Any] = None
    embedding: Optional[np.ndarray] = None
    cypher_query: Optional[str] = None
    filters: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}
        if self.filters is None:
            self.filters = {}

@dataclass
class BSMResult:
    """Resultado de consulta BSM"""
    query_id: str
    query_type: QueryType
    semantic_results: List[SimilarityResult] = None
    graph_results: List[Dict[str, Any]] = None
    combined_results: List[Dict[str, Any]] = None
    execution_time: float = 0.0
    confidence_score: float = 0.0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.semantic_results is None:
            self.semantic_results = []
        if self.graph_results is None:
            self.graph_results = []
        if self.combined_results is None:
            self.combined_results = []
        if self.metadata is None:
            self.metadata = {}

# === INTERPRETADOR DE CONSULTAS NATURALES ===

class NaturalLanguageInterpreter:
    """Interpreta consultas en lenguaje natural para BSM"""
    
    # Patrones de consulta
    PATTERNS = {
        QueryType.SEMANTIC_SEARCH: [
            r"find.*similar.*to\s+(.+)",
            r"search.*like\s+(.+)",
            r"proteins.*similar.*to\s+(.+)",
            r"what.*similar.*to\s+(.+)"
        ],
        QueryType.GRAPH_TRAVERSAL: [
            r"what.*interact.*with\s+(.+)",
            r"show.*relationships.*of\s+(.+)",
            r"connections.*to\s+(.+)",
            r"pathway.*involving\s+(.+)"
        ],
        QueryType.PROTEIN_ANALYSIS: [
            r"analyze.*protein\s+(.+)",
            r"information.*about\s+(.+)",
            r"details.*of\s+(.+)",
            r"tell.*me.*about\s+(.+)"
        ],
        QueryType.FUNCTION_PREDICTION: [
            r"function.*of\s+(.+)",
            r"what.*does\s+(.+).*do",
            r"predict.*function.*of\s+(.+)",
            r"role.*of\s+(.+)"
        ]
    }
    
    @classmethod
    def interpret_query(cls, query_text: str) -> BSMQuery:
        """
        Interpreta consulta en lenguaje natural
        
        Args:
            query_text: Texto de la consulta
            
        Returns:
            BSMQuery estructurada
        """
        query_text_lower = query_text.lower().strip()
        
        # Detectar tipo de consulta
        detected_type = QueryType.HYBRID_QUERY  # Default
        extracted_entity = None
        
        for query_type, patterns in cls.PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, query_text_lower)
                if match:
                    detected_type = query_type
                    extracted_entity = match.group(1).strip()
                    break
            if extracted_entity:
                break
        
        # Extraer parámetros adicionales
        parameters = {}
        if extracted_entity:
            parameters["target_entity"] = extracted_entity
        
        # Detectar filtros
        filters = {}
        if "organism" in query_text_lower:
            organism_match = re.search(r"organism[:\s]+([a-zA-Z\s]+)", query_text_lower)
            if organism_match:
                filters["organism"] = organism_match.group(1).strip()
        
        if "function" in query_text_lower:
            function_match = re.search(r"function[:\s]+([a-zA-Z\s]+)", query_text_lower)
            if function_match:
                filters["function"] = function_match.group(1).strip()
        
        # Detectar prioridad
        priority = QueryPriority.MEDIUM
        if any(word in query_text_lower for word in ["urgent", "critical", "important"]):
            priority = QueryPriority.HIGH
        elif any(word in query_text_lower for word in ["quick", "fast", "simple"]):
            priority = QueryPriority.LOW
        
        return BSMQuery(
            query_text=query_text,
            query_type=detected_type,
            priority=priority,
            parameters=parameters,
            filters=filters
        )

# === MOTOR DE CONSULTAS BSM ===

class BSMQueryEngine:
    """Motor de consultas dual-core BSM"""
    
    def __init__(self, 
                 neo4j_integration: Optional[BSMNeo4jIntegration] = None,
                 milvus_integration: Optional[BSMMilvusIntegration] = None,
                 config: Optional[BSMConfig] = None):
        """
        Inicializa motor de consultas BSM
        
        Args:
            neo4j_integration: Integración Neo4j (opcional)
            milvus_integration: Integración Milvus (opcional)
            config: Configuración BSM
        """
        self.config = config or get_bsm_config()
        self.neo4j = neo4j_integration
        self.milvus = milvus_integration
        self.interpreter = NaturalLanguageInterpreter()
        self._query_cache: Dict[str, BSMResult] = {}
        self._identity_cache: Dict[str, ProteinIdentity] = {}

    async def resolve_entity(self, entity: str) -> ProteinIdentity:
        """Resolve a protein identifier to a canonical ProteinIdentity.

        Queries available backends to populate UniProt, ENSP, and gene fields.
        Results are cached for the engine lifetime.
        """
        entity = entity.strip()
        if not entity:
            return ProteinIdentity(resolved_from=entity)

        if entity in self._identity_cache:
            return self._identity_cache[entity]

        identity = ProteinIdentity(resolved_from=entity)
        id_type = detect_id_type(entity)

        # Seed identity from the detected type
        if id_type == "uniprot":
            identity.uniprot = entity
        elif id_type == "ensp":
            identity.ensp = entity
        elif id_type == "gene":
            identity.gene = entity

        # Try to enrich via Milvus text search
        if self.milvus:
            try:
                hits = await self.milvus.search_by_text(entity, limit=1)
                if hits:
                    hit_identity = resolve_identity_from_hit(hits[0], entity)
                    identity = merge_identities(identity, hit_identity)
            except Exception as exc:
                logger.debug("Identity enrichment via Milvus failed: %s", exc)

        self._identity_cache[entity] = identity
        if identity.is_resolved:
            logger.info(
                "🔗 Resolved %r → uniprot=%s ensp=%s gene=%s",
                entity, identity.uniprot, identity.ensp, identity.gene,
            )
        return identity
        
    async def initialize(self):
        """Inicializa integraciones si no están proporcionadas"""
        if self.neo4j is None:
            from .neo4j_integration import create_bsm_neo4j_integration
            self.neo4j = await create_bsm_neo4j_integration(self.config)
            
        if self.milvus is None:
            from .milvus_integration import create_bsm_milvus_integration
            self.milvus = await create_bsm_milvus_integration(self.config)
            
        logger.info("🔍 BSM Query Engine initialized successfully")
    
    async def process_query(self, query: Union[str, BSMQuery]) -> BSMResult:
        """
        Procesa consulta BSM
        
        Args:
            query: Consulta como string o BSMQuery estructurada
            
        Returns:
            BSMResult con resultados combinados
        """
        start_time = datetime.now()
        
        # Convertir string a BSMQuery si es necesario
        if isinstance(query, str):
            query = self.interpreter.interpret_query(query)
        
        query_id = f"bsm_query_{int(start_time.timestamp())}"
        logger.info(f"🔍 Processing BSM query: {query.query_type.value}")
        
        try:
            # Stage 1: Identity Resolution — resolve target entity aliases
            target_entity = query.parameters.get("target_entity", "")
            if target_entity:
                identity = await self.resolve_entity(target_entity)
                query.parameters["_identity"] = identity.to_dict()
                # Enrich with resolved IDs so downstream methods can query any collection
                if identity.uniprot and target_entity != identity.uniprot:
                    query.parameters.setdefault("_alt_ids", []).append(identity.uniprot)
                if identity.ensp and target_entity != identity.ensp:
                    query.parameters.setdefault("_alt_ids", []).append(identity.ensp)
                if identity.gene and target_entity != identity.gene:
                    query.parameters.setdefault("_alt_ids", []).append(identity.gene)

            # Ejecutar consulta según tipo
            if query.query_type == QueryType.SEMANTIC_SEARCH:
                result = await self._execute_semantic_search(query, query_id)
            elif query.query_type == QueryType.GRAPH_TRAVERSAL:
                result = await self._execute_graph_traversal(query, query_id)
            elif query.query_type == QueryType.HYBRID_QUERY:
                result = await self._execute_hybrid_query(query, query_id)
            elif query.query_type == QueryType.PROTEIN_ANALYSIS:
                result = await self._execute_protein_analysis(query, query_id)
            elif query.query_type == QueryType.FUNCTION_PREDICTION:
                result = await self._execute_function_prediction(query, query_id)
            else:
                result = await self._execute_hybrid_query(query, query_id)
            
            # Calcular tiempo de ejecución
            execution_time = (datetime.now() - start_time).total_seconds()
            result.execution_time = execution_time
            
            # Cachear resultado si es útil
            if query.priority in [QueryPriority.HIGH, QueryPriority.CRITICAL]:
                self._query_cache[query.query_text] = result
            
            logger.info(f"✅ Query processed in {execution_time:.3f}s")
            return result
            
        except Exception as e:
            logger.error(f"❌ Query processing failed: {e}")
            return BSMResult(
                query_id=query_id,
                query_type=query.query_type,
                execution_time=(datetime.now() - start_time).total_seconds(),
                metadata={"error": str(e)}
            )
    
    async def _execute_semantic_search(self, query: BSMQuery, query_id: str) -> BSMResult:
        """Ejecuta búsqueda semántica en Milvus"""
        try:
            target_entity = query.parameters.get("target_entity", "")
            
            # Si hay entidad objetivo, buscar su embedding
            if target_entity:
                # Buscar proteína objetivo en Milvus
                target_protein = await self.milvus.search_by_text(target_entity, limit=1)
                
                if target_protein:
                    # Usar embedding de la proteína encontrada
                    target_embedding = np.array(target_protein[0].get("embedding", []))
                    if len(target_embedding) > 0:
                        similar_proteins = await self.milvus.search_similar_proteins(
                            target_embedding,
                            top_k=20,
                            similarity_threshold=0.6
                        )
                    else:
                        similar_proteins = []
                else:
                    # Búsqueda por texto si no se encuentra embedding
                    text_results = await self.milvus.search_by_text(target_entity, limit=20)
                    similar_proteins = [
                        SimilarityResult(
                            protein_id=r.get("protein_id", ""),
                            name=r.get("name", ""),
                            similarity_score=0.5,  # Score por defecto para búsqueda por texto
                            metadata=r
                        ) for r in text_results
                    ]
            else:
                similar_proteins = []
            
            return BSMResult(
                query_id=query_id,
                query_type=query.query_type,
                semantic_results=similar_proteins,
                confidence_score=0.8 if similar_proteins else 0.2,
                metadata={
                    "search_method": "semantic_similarity",
                    "target_entity": target_entity,
                    "results_count": len(similar_proteins)
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Semantic search failed: {e}")
            return BSMResult(query_id=query_id, query_type=query.query_type)
    
    async def _execute_graph_traversal(self, query: BSMQuery, query_id: str) -> BSMResult:
        """Ejecuta navegación por grafo en Neo4j"""
        try:
            target_entity = query.parameters.get("target_entity", "")
            
            if target_entity:
                # Buscar proteína en Neo4j
                protein_data = await self.neo4j.get_protein_by_id(target_entity)
                
                if not protein_data:
                    # Buscar por nombre
                    search_results = await self.neo4j.search_proteins(target_entity, limit=1)
                    if search_results:
                        protein_data = search_results[0]
                        target_entity = protein_data.get("identifier", target_entity)
                
                if protein_data:
                    # Obtener relaciones
                    relationships = await self.neo4j.get_protein_relationships(
                        target_entity,
                        limit=50
                    )
                    
                    graph_results = [{
                        "protein": protein_data,
                        "relationships": relationships
                    }]
                else:
                    graph_results = []
            else:
                graph_results = []
            
            return BSMResult(
                query_id=query_id,
                query_type=query.query_type,
                graph_results=graph_results,
                confidence_score=0.9 if graph_results else 0.1,
                metadata={
                    "search_method": "graph_traversal",
                    "target_entity": target_entity,
                    "relationships_found": sum(len(r.get("relationships", [])) for r in graph_results)
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Graph traversal failed: {e}")
            return BSMResult(query_id=query_id, query_type=query.query_type)
    
    async def _execute_hybrid_query(self, query: BSMQuery, query_id: str) -> BSMResult:
        """Ejecuta consulta híbrida combinando Neo4j y Milvus"""
        try:
            # Ejecutar ambas búsquedas en paralelo
            semantic_task = self._execute_semantic_search(query, f"{query_id}_semantic")
            graph_task = self._execute_graph_traversal(query, f"{query_id}_graph")
            
            semantic_result, graph_result = await asyncio.gather(
                semantic_task, graph_task, return_exceptions=True
            )
            
            # Combinar resultados
            combined_results = []
            
            # Agregar resultados semánticos
            if isinstance(semantic_result, BSMResult) and semantic_result.semantic_results:
                for sim_result in semantic_result.semantic_results:
                    combined_results.append({
                        "type": "semantic_match",
                        "protein_id": sim_result.protein_id,
                        "name": sim_result.name,
                        "similarity_score": sim_result.similarity_score,
                        "source": "milvus",
                        "metadata": sim_result.metadata
                    })
            
            # Agregar resultados de grafo
            if isinstance(graph_result, BSMResult) and graph_result.graph_results:
                for graph_data in graph_result.graph_results:
                    protein = graph_data.get("protein", {})
                    combined_results.append({
                        "type": "graph_match",
                        "protein_id": protein.get("identifier", ""),
                        "name": protein.get("name", ""),
                        "relationships_count": len(graph_data.get("relationships", [])),
                        "source": "neo4j",
                        "metadata": protein
                    })
            
            # Calcular score de confianza combinado
            confidence_scores = []
            if isinstance(semantic_result, BSMResult):
                confidence_scores.append(semantic_result.confidence_score)
            if isinstance(graph_result, BSMResult):
                confidence_scores.append(graph_result.confidence_score)
            
            avg_confidence = np.mean(confidence_scores) if confidence_scores else 0.0
            
            return BSMResult(
                query_id=query_id,
                query_type=query.query_type,
                semantic_results=semantic_result.semantic_results if isinstance(semantic_result, BSMResult) else [],
                graph_results=graph_result.graph_results if isinstance(graph_result, BSMResult) else [],
                combined_results=combined_results,
                confidence_score=avg_confidence,
                metadata={
                    "search_method": "hybrid",
                    "semantic_results": len(semantic_result.semantic_results) if isinstance(semantic_result, BSMResult) else 0,
                    "graph_results": len(graph_result.graph_results) if isinstance(graph_result, BSMResult) else 0,
                    "combined_results": len(combined_results)
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Hybrid query failed: {e}")
            return BSMResult(query_id=query_id, query_type=query.query_type)
    
    async def _execute_protein_analysis(self, query: BSMQuery, query_id: str) -> BSMResult:
        """Ejecuta análisis completo de proteína"""
        try:
            target_entity = query.parameters.get("target_entity", "")
            
            if not target_entity:
                return BSMResult(query_id=query_id, query_type=query.query_type)
            
            # Buscar en ambos sistemas
            milvus_data = await self.milvus.search_by_text(target_entity, limit=1)
            neo4j_data = await self.neo4j.search_proteins(target_entity, limit=1)
            
            combined_results = []
            
            if milvus_data:
                protein_milvus = milvus_data[0]
                
                # Buscar similares
                if "embedding" in protein_milvus:
                    embedding = np.array(protein_milvus["embedding"])
                    similar_proteins = await self.milvus.search_similar_proteins(
                        embedding, top_k=10, similarity_threshold=0.7
                    )
                else:
                    similar_proteins = []
                
                combined_results.append({
                    "type": "protein_analysis",
                    "source": "milvus",
                    "protein_data": protein_milvus,
                    "similar_proteins": [
                        {"id": s.protein_id, "name": s.name, "similarity": s.similarity_score}
                        for s in similar_proteins
                    ]
                })
            
            if neo4j_data:
                protein_neo4j = neo4j_data[0]
                protein_id = protein_neo4j.get("identifier", "")
                
                # Obtener relaciones
                relationships = await self.neo4j.get_protein_relationships(protein_id, limit=20)
                
                combined_results.append({
                    "type": "protein_analysis",
                    "source": "neo4j",
                    "protein_data": protein_neo4j,
                    "relationships": relationships
                })
            
            return BSMResult(
                query_id=query_id,
                query_type=query.query_type,
                combined_results=combined_results,
                confidence_score=0.9 if combined_results else 0.1,
                metadata={
                    "search_method": "protein_analysis",
                    "target_entity": target_entity,
                    "sources": ["milvus" if milvus_data else None, "neo4j" if neo4j_data else None]
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Protein analysis failed: {e}")
            return BSMResult(query_id=query_id, query_type=query.query_type)
    
    async def _execute_function_prediction(self, query: BSMQuery, query_id: str) -> BSMResult:
        """Ejecuta predicción de función basada en similitud"""
        try:
            target_entity = query.parameters.get("target_entity", "")
            
            if not target_entity:
                return BSMResult(query_id=query_id, query_type=query.query_type)
            
            # Buscar proteína objetivo
            target_proteins = await self.milvus.search_by_text(target_entity, limit=1)
            
            if not target_proteins:
                return BSMResult(query_id=query_id, query_type=query.query_type)
            
            target_protein = target_proteins[0]
            
            # Si ya tiene función, mostrarla
            if target_protein.get("function"):
                combined_results = [{
                    "type": "known_function",
                    "protein_id": target_protein["protein_id"],
                    "name": target_protein["name"],
                    "function": target_protein["function"],
                    "confidence": 1.0
                }]
            else:
                # Predecir función basada en proteínas similares
                if "embedding" in target_protein:
                    embedding = np.array(target_protein["embedding"])
                    similar_proteins = await self.milvus.search_similar_proteins(
                        embedding, top_k=20, similarity_threshold=0.8
                    )
                    
                    # Analizar funciones de proteínas similares
                    function_counts = {}
                    for similar in similar_proteins:
                        if similar.metadata and similar.metadata.get("function"):
                            func = similar.metadata["function"]
                            if func not in function_counts:
                                function_counts[func] = []
                            function_counts[func].append(similar.similarity_score)
                    
                    # Predecir función más probable
                    predicted_functions = []
                    for func, scores in function_counts.items():
                        avg_score = np.mean(scores)
                        predicted_functions.append({
                            "function": func,
                            "confidence": avg_score,
                            "supporting_proteins": len(scores)
                        })
                    
                    predicted_functions.sort(key=lambda x: x["confidence"], reverse=True)
                    
                    combined_results = [{
                        "type": "function_prediction",
                        "protein_id": target_protein["protein_id"],
                        "name": target_protein["name"],
                        "predicted_functions": predicted_functions[:5]  # Top 5
                    }]
                else:
                    combined_results = []
            
            return BSMResult(
                query_id=query_id,
                query_type=query.query_type,
                combined_results=combined_results,
                confidence_score=0.8 if combined_results else 0.2,
                metadata={
                    "search_method": "function_prediction",
                    "target_entity": target_entity
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Function prediction failed: {e}")
            return BSMResult(query_id=query_id, query_type=query.query_type)
    
    # === OPERACIONES DE ANÁLISIS AVANZADO ===
    
    async def discover_protein_pathways(self, protein_id: str, max_depth: int = 3) -> Dict[str, Any]:
        """
        Descubre pathways que involucran una proteína
        
        Args:
            protein_id: ID de la proteína
            max_depth: Profundidad máxima de búsqueda
            
        Returns:
            Pathways descubiertos
        """
        try:
            # Búsqueda en grafo para pathways
            pathways = []
            
            # Consulta Cypher para encontrar pathways
            cypher_query = f"""
            MATCH (p:Protein {{identifier: '{protein_id}'}})-[:HAS_FUNCTION]->(f:Function)-[:PART_OF]->(pathway:Pathway)
            RETURN pathway, f, p
            LIMIT 20
            """
            
            async with self.neo4j.session() as session:
                result = await session.run(cypher_query)
                async for record in result:
                    pathways.append({
                        "pathway": dict(record["pathway"]),
                        "function": dict(record["f"]),
                        "protein": dict(record["p"])
                    })
            
            return {
                "protein_id": protein_id,
                "pathways": pathways,
                "pathway_count": len(pathways)
            }
            
        except Exception as e:
            logger.error(f"❌ Pathway discovery failed: {e}")
            return {"protein_id": protein_id, "pathways": []}
    
    async def get_query_suggestions(self, partial_query: str) -> List[str]:
        """
        Sugiere completaciones de consulta
        
        Args:
            partial_query: Consulta parcial
            
        Returns:
            Lista de sugerencias
        """
        suggestions = []
        
        # Sugerencias basadas en patrones comunes
        if "similar" in partial_query.lower():
            suggestions.extend([
                "Find proteins similar to hemoglobin",
                "Search for proteins similar to insulin",
                "What proteins are similar to actin"
            ])
        
        if "function" in partial_query.lower():
            suggestions.extend([
                "What is the function of protein P53",
                "Predict function of unknown protein",
                "Functions related to metabolism"
            ])
        
        if "interact" in partial_query.lower():
            suggestions.extend([
                "What proteins interact with BRCA1",
                "Show interactions of insulin receptor",
                "Protein-protein interactions in apoptosis"
            ])
        
        return suggestions[:10]  # Limitar sugerencias

# === UTILIDADES ===

async def create_bsm_query_engine(config: Optional[BSMConfig] = None) -> BSMQueryEngine:
    """
    Crea e inicializa motor de consultas BSM
    
    Args:
        config: Configuración BSM
        
    Returns:
        BSMQueryEngine inicializado
    """
    engine = BSMQueryEngine(config=config)
    await engine.initialize()
    return engine

# === EXPORTACIONES ===

__all__ = [
    "BSMQueryEngine",
    "BSMQuery",
    "BSMResult", 
    "QueryType",
    "QueryPriority",
    "NaturalLanguageInterpreter",
    "create_bsm_query_engine"
]
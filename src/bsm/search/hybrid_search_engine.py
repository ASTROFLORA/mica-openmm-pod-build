"""
BSM Hybrid Search Engine
=========================

Motor de búsqueda híbrido que integra:
- 5 vectores Milvus (ProtT5, ESM-C, BioLinkBERT, SciBERT, node2vec)
- Neo4j GraphRAG
- BLAST alignment
- BM25 text search
- RRF fusion para combinar resultados

Alineado con:
- MULTIMODAL_RAG_BLAST_ARCHITECTURE_5VECTORS.md
- CITATION_DRIVEN_RAG_ARCHITECTURE.md
- DEEPRESEARCH Milvus 2.6 + Specialized Embeddings

Author: BSM Modernization Initiative
Version: 3.0.0
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set, Tuple, Union
from enum import Enum, auto
from datetime import datetime
import numpy as np
from abc import ABC, abstractmethod
import hashlib
import json

logger = logging.getLogger(__name__)


# ============================================================================
# SEARCH STRATEGY CONFIGURATION
# ============================================================================

class SearchStrategy(Enum):
    """Estrategias de búsqueda disponibles"""
    SEMANTIC_ONLY = "semantic_only"           # Solo vectores
    GRAPH_ONLY = "graph_only"                 # Solo Neo4j
    BLAST_ONLY = "blast_only"                 # Solo BLAST
    HYBRID_VECTOR_GRAPH = "hybrid_vg"         # Vectores + Graph
    HYBRID_VECTOR_BLAST = "hybrid_vb"         # Vectores + BLAST
    HYBRID_GRAPH_BLAST = "hybrid_gb"          # Graph + BLAST
    FULL_HYBRID = "full_hybrid"               # Todo combinado
    ADAPTIVE = "adaptive"                     # Selección automática


class QueryIntent(Enum):
    """Intención detectada del query"""
    PROTEIN_SIMILARITY = auto()      # Buscar proteínas similares
    FUNCTION_SEARCH = auto()         # Buscar por función
    PATHWAY_EXPLORATION = auto()     # Explorar pathways
    LITERATURE_SEARCH = auto()       # Buscar en literatura
    STRUCTURE_ANALYSIS = auto()      # Análisis estructural
    INTERACTION_NETWORK = auto()     # Red de interacciones
    DRUG_TARGET = auto()             # Búsqueda de targets
    GENERAL = auto()                 # General/no clasificado


@dataclass
class SearchConfig:
    """Configuración del motor de búsqueda híbrido"""
    
    # Vector search settings (Milvus)
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    collection_name: str = "bsm_proteins_multimodal_v3"
    
    # Neo4j settings
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    
    # BLAST settings
    blast_db_path: str = "./databases/blast"
    blast_evalue: float = 1e-5
    blast_use_remote: bool = False
    
    # RRF fusion settings
    rrf_k: int = 60
    
    # Search limits
    max_results_per_source: int = 50
    final_results_limit: int = 20
    
    # Timeout settings (seconds)
    vector_timeout: float = 5.0
    graph_timeout: float = 10.0
    blast_timeout: float = 30.0
    
    # Parallel execution
    enable_parallel: bool = True
    max_parallel_sources: int = 8
    
    # Caching
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    
    # Vector weights (used in RRF)
    vector_weights: Dict[str, float] = field(default_factory=lambda: {
        "prott5": 1.0,
        "esmc": 1.2,       # Bonus por info estructural
        "biolinkbert": 1.0,
        "scibert": 0.9,
        "node2vec": 0.8,
        "neo4j": 1.0,
        "blast": 1.5,      # Bonus por precisión
        "bm25": 0.7
    })


# ============================================================================
# SEARCH RESULT TYPES
# ============================================================================

@dataclass
class SourceResult:
    """Resultado de una fuente individual"""
    source_name: str
    document_id: str
    score: float
    rank: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    snippet: Optional[str] = None


@dataclass
class UnifiedResult:
    """Resultado unificado después de fusión"""
    document_id: str
    final_score: float
    final_rank: int
    sources: List[SourceResult]
    source_count: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    
    def __post_init__(self):
        self.source_count = len(self.sources)
        # Confianza basada en acuerdo entre fuentes
        if self.source_count >= 3:
            self.confidence = min(1.0, 0.5 + (self.source_count * 0.15))
        else:
            self.confidence = 0.3 + (self.source_count * 0.2)


@dataclass
class SearchResponse:
    """Respuesta completa de búsqueda"""
    query: str
    strategy: SearchStrategy
    detected_intent: QueryIntent
    results: List[UnifiedResult]
    total_results: int
    search_time_ms: float
    sources_used: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario"""
        return {
            "query": self.query,
            "strategy": self.strategy.value,
            "detected_intent": self.detected_intent.name,
            "results": [
                {
                    "document_id": r.document_id,
                    "score": r.final_score,
                    "rank": r.final_rank,
                    "confidence": r.confidence,
                    "source_count": r.source_count,
                    "sources": [s.source_name for s in r.sources],
                    "metadata": r.metadata
                }
                for r in self.results
            ],
            "total_results": self.total_results,
            "search_time_ms": self.search_time_ms,
            "sources_used": self.sources_used,
            "warnings": self.warnings
        }


# ============================================================================
# SOURCE INTERFACES
# ============================================================================

class SearchSource(ABC):
    """Interfaz base para fuentes de búsqueda"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre de la fuente"""
        pass
    
    @abstractmethod
    async def search(
        self,
        query: str,
        embedding: Optional[np.ndarray] = None,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[SourceResult]:
        """Ejecuta búsqueda"""
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """Verifica si la fuente está disponible"""
        pass


# ============================================================================
# MILVUS VECTOR SOURCE
# ============================================================================

class MilvusVectorSource(SearchSource):
    """Fuente de búsqueda vectorial con Milvus"""
    
    def __init__(
        self,
        vector_field: str,
        source_name: str,
        config: SearchConfig,
        embedding_router = None
    ):
        self._vector_field = vector_field
        self._source_name = source_name
        self._config = config
        self._embedding_router = embedding_router
        self._client = None
        
    @property
    def name(self) -> str:
        return self._source_name
    
    async def is_available(self) -> bool:
        """Verifica conexión a Milvus"""
        try:
            if self._client is None:
                await self._connect()
            return self._client is not None
        except Exception as e:
            logger.warning(f"Milvus source {self._source_name} unavailable: {e}")
            return False
    
    async def _connect(self):
        """Conecta a Milvus"""
        try:
            from pymilvus import connections, Collection
            
            connections.connect(
                alias=f"bsm_{self._source_name}",
                host=self._config.milvus_host,
                port=self._config.milvus_port
            )
            
            self._client = Collection(self._config.collection_name)
            self._client.load()
            
            logger.info(f"✅ Connected to Milvus: {self._source_name}")
        except Exception as e:
            logger.error(f"❌ Failed to connect Milvus: {e}")
            self._client = None
    
    async def search(
        self,
        query: str,
        embedding: Optional[np.ndarray] = None,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[SourceResult]:
        """Búsqueda vectorial en Milvus"""
        if self._client is None:
            await self._connect()
            if self._client is None:
                return []
        
        try:
            # Generar embedding si no se proporciona
            if embedding is None and self._embedding_router:
                embedding = await self._generate_embedding(query)
            
            if embedding is None:
                logger.warning(f"No embedding available for {self._source_name}")
                return []
            
            # Construir expresión de filtro
            expr = self._build_filter_expr(filters)
            
            # Ejecutar búsqueda
            search_params = {
                "metric_type": "COSINE",
                "params": {"nprobe": 10}
            }
            
            results = self._client.search(
                data=[embedding.tolist()],
                anns_field=self._vector_field,
                param=search_params,
                limit=limit,
                expr=expr if expr else None,
                output_fields=["uniprot_id", "protein_name", "organism", "function"]
            )
            
            # Convertir resultados
            source_results = []
            for i, hit in enumerate(results[0]):
                source_results.append(SourceResult(
                    source_name=self._source_name,
                    document_id=hit.entity.get("uniprot_id", str(hit.id)),
                    score=float(hit.score),
                    rank=i + 1,
                    metadata={
                        "protein_name": hit.entity.get("protein_name", ""),
                        "organism": hit.entity.get("organism", ""),
                        "function": hit.entity.get("function", ""),
                        "vector_field": self._vector_field
                    }
                ))
            
            return source_results
            
        except Exception as e:
            logger.error(f"Milvus search error ({self._source_name}): {e}")
            return []
    
    async def _generate_embedding(self, query: str) -> Optional[np.ndarray]:
        """Genera embedding usando el router"""
        if not self._embedding_router:
            return None
        
        # Mapear source_name a espacio de embedding
        space_mapping = {
            "vector_prott5": "SEQUENCE_PROTT5",
            "vector_esmc": "SEQUENCE_ESMC",
            "vector_biolinkbert": "METADATA_SEMANTIC",
            "vector_scibert": "PAPER_KNOWLEDGE"
        }
        
        space = space_mapping.get(self._source_name)
        if space:
            from ..embeddings.multi_model_router import EmbeddingSpace
            return await self._embedding_router.embed_for_space(
                EmbeddingSpace[space], query
            )
        return None
    
    def _build_filter_expr(self, filters: Optional[Dict[str, Any]]) -> Optional[str]:
        """Construye expresión de filtro para Milvus"""
        if not filters:
            return None
        
        conditions = []
        
        if "organism" in filters:
            conditions.append(f'organism == "{filters["organism"]}"')
        
        if "min_length" in filters:
            conditions.append(f'sequence_length >= {filters["min_length"]}')
        
        if "max_length" in filters:
            conditions.append(f'sequence_length <= {filters["max_length"]}')
        
        if "reviewed" in filters:
            conditions.append(f'is_reviewed == {str(filters["reviewed"]).lower()}')
        
        return " and ".join(conditions) if conditions else None


# ============================================================================
# NEO4J GRAPH SOURCE
# ============================================================================

class Neo4jGraphSource(SearchSource):
    """Fuente de búsqueda en grafo Neo4j"""
    
    def __init__(self, config: SearchConfig):
        self._config = config
        self._driver = None
    
    @property
    def name(self) -> str:
        return "neo4j_graph"
    
    async def is_available(self) -> bool:
        """Verifica conexión a Neo4j"""
        try:
            if self._driver is None:
                await self._connect()
            
            async with self._driver.session() as session:
                result = await session.run("RETURN 1")
                await result.consume()
            return True
        except Exception as e:
            logger.warning(f"Neo4j unavailable: {e}")
            return False
    
    async def _connect(self):
        """Conecta a Neo4j"""
        try:
            from neo4j import AsyncGraphDatabase
            
            self._driver = AsyncGraphDatabase.driver(
                self._config.neo4j_uri,
                auth=(self._config.neo4j_user, self._config.neo4j_password)
            )
            logger.info("✅ Connected to Neo4j")
        except Exception as e:
            logger.error(f"❌ Failed to connect Neo4j: {e}")
    
    async def search(
        self,
        query: str,
        embedding: Optional[np.ndarray] = None,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[SourceResult]:
        """Búsqueda en grafo Neo4j"""
        if self._driver is None:
            await self._connect()
            if self._driver is None:
                return []
        
        try:
            # Detectar tipo de query y seleccionar Cypher apropiado
            cypher_query = self._build_cypher_query(query, filters, limit)
            
            async with self._driver.session() as session:
                result = await session.run(cypher_query, {"query": query, "limit": limit})
                records = await result.data()
            
            # Convertir a SourceResults
            source_results = []
            for i, record in enumerate(records):
                source_results.append(SourceResult(
                    source_name="neo4j_graph",
                    document_id=record.get("id", str(i)),
                    score=record.get("score", 1.0 - (i * 0.01)),
                    rank=i + 1,
                    metadata={
                        "node_type": record.get("type", "unknown"),
                        "properties": record.get("properties", {}),
                        "relationships": record.get("relationships", [])
                    }
                ))
            
            return source_results
            
        except Exception as e:
            logger.error(f"Neo4j search error: {e}")
            return []
    
    def _build_cypher_query(
        self,
        query: str,
        filters: Optional[Dict[str, Any]],
        limit: int
    ) -> str:
        """Construye query Cypher basado en el query"""
        
        # Query básico de búsqueda full-text
        base_query = """
        CALL db.index.fulltext.queryNodes('protein_fulltext', $query) 
        YIELD node, score
        WITH node, score
        OPTIONAL MATCH (node)-[r]-(related)
        RETURN 
            node.uniprot_id as id,
            labels(node)[0] as type,
            properties(node) as properties,
            collect(DISTINCT type(r)) as relationships,
            score
        ORDER BY score DESC
        LIMIT $limit
        """
        
        return base_query


# ============================================================================
# BLAST ALIGNMENT SOURCE
# ============================================================================

class BlastAlignmentSource(SearchSource):
    """Fuente de búsqueda por alineamiento BLAST"""
    
    def __init__(self, config: SearchConfig, blast_integration = None):
        self._config = config
        self._blast = blast_integration
    
    @property
    def name(self) -> str:
        return "blast_alignment"
    
    async def is_available(self) -> bool:
        """Verifica disponibilidad de BLAST"""
        try:
            if self._blast:
                return await self._blast.is_available()
            
            # Verificar BLAST local
            import subprocess
            result = subprocess.run(
                ["blastp", "-version"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
    
    async def search(
        self,
        query: str,
        embedding: Optional[np.ndarray] = None,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[SourceResult]:
        """Búsqueda por BLAST alignment"""
        
        # Verificar si query es una secuencia de proteína
        if not self._is_protein_sequence(query):
            logger.debug("Query is not a protein sequence, skipping BLAST")
            return []
        
        try:
            if self._blast:
                # Usar integración BLAST existente
                results = await self._blast.run_blastp(
                    sequence=query,
                    database="swissprot",
                    evalue=self._config.blast_evalue,
                    max_hits=limit
                )
            else:
                # BLAST directo
                results = await self._run_blast_direct(query, limit)
            
            # Convertir a SourceResults
            source_results = []
            for i, hit in enumerate(results):
                # Calcular score normalizado basado en E-value
                evalue = hit.get("evalue", 1.0)
                score = self._evalue_to_score(evalue)
                
                source_results.append(SourceResult(
                    source_name="blast_alignment",
                    document_id=hit.get("accession", str(i)),
                    score=score,
                    rank=i + 1,
                    metadata={
                        "evalue": evalue,
                        "identity": hit.get("identity", 0),
                        "coverage": hit.get("coverage", 0),
                        "bit_score": hit.get("bit_score", 0),
                        "alignment_length": hit.get("alignment_length", 0)
                    },
                    snippet=hit.get("title", "")
                ))
            
            return source_results
            
        except Exception as e:
            logger.error(f"BLAST search error: {e}")
            return []
    
    def _is_protein_sequence(self, text: str) -> bool:
        """Verifica si el texto es una secuencia de proteína"""
        # Quitar espacios y newlines
        clean = text.upper().replace(" ", "").replace("\n", "")
        
        # Aminoácidos válidos
        valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
        
        # Debe tener al menos 10 caracteres y ser mayormente aminoácidos
        if len(clean) < 10:
            return False
        
        aa_count = sum(1 for c in clean if c in valid_aa)
        return (aa_count / len(clean)) > 0.9
    
    def _evalue_to_score(self, evalue: float) -> float:
        """Convierte E-value a score normalizado [0, 1]"""
        import math
        if evalue <= 0:
            return 1.0
        # Score = 1 / (1 + log10(evalue))
        # Para evalue=1e-50, score ≈ 0.98
        # Para evalue=1e-5, score ≈ 0.83
        # Para evalue=1, score ≈ 0.5
        return 1.0 / (1.0 + max(0, math.log10(evalue) + 5) / 5)
    
    async def _run_blast_direct(self, sequence: str, limit: int) -> List[Dict]:
        """Ejecuta BLAST/MMseqs2 utilizando BlastService"""
        try:
            from bsm.alignment import BlastConfig, BlastService
            config = BlastConfig(
                use_remote=True,
                max_target_seqs=limit,
                evalue_threshold=self._config.blast_evalue,
            )
            service = BlastService(config)
            result = await service.search(
                sequence=sequence,
                max_hits=limit,
                evalue=self._config.blast_evalue,
            )
            
            results = []
            for hit in result.hits[:limit]:
                subject_id = hit.subject_id
                subject_title = hit.subject_title or ""
                accession = subject_id
                import re
                for text in (subject_id, subject_title):
                    match = re.search(r"\b(?:sp|tr)\|([A-Z0-9]+)\|", text)
                    if match:
                        accession = match.group(1)
                        break
                else:
                    match = re.search(r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9])\b", subject_id + " " + subject_title)
                    if match:
                        accession = match.group(1)

                results.append({
                    "accession": accession,
                    "title": hit.subject_title or hit.subject_id,
                    "evalue": hit.e_value,
                    "bit_score": hit.bit_score,
                    "identity": hit.identity,
                    "coverage": (hit.alignment_length / len(sequence) * 100) if len(sequence) > 0 else 0.0,
                    "alignment_length": hit.alignment_length
                })
            
            return results
            
        except Exception as e:
            logger.warning(f"Error running BLAST/MMseqs2 alignment: {e}")
            return []


# ============================================================================
# BM25 TEXT SEARCH SOURCE
# ============================================================================

class BM25TextSource(SearchSource):
    """Fuente de búsqueda BM25 para texto"""
    
    def __init__(self, config: SearchConfig):
        self._config = config
        self._index = None
    
    @property
    def name(self) -> str:
        return "bm25_text"
    
    async def is_available(self) -> bool:
        return True  # BM25 siempre disponible in-memory
    
    async def search(
        self,
        query: str,
        embedding: Optional[np.ndarray] = None,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[SourceResult]:
        """Búsqueda BM25"""
        try:
            # Conectar a Milvus para sparse search (BM25)
            from pymilvus import connections, Collection
            
            connections.connect(
                alias="bsm_bm25",
                host=self._config.milvus_host,
                port=self._config.milvus_port
            )
            
            collection = Collection(self._config.collection_name)
            collection.load()
            
            # Milvus 2.6+ soporta sparse vectors para BM25
            # Aquí usamos full-text search si está disponible
            results = collection.query(
                expr=f'TEXT_MATCH(function_description, "{query}")',
                output_fields=["uniprot_id", "protein_name", "function_description"],
                limit=limit
            )
            
            source_results = []
            for i, doc in enumerate(results):
                source_results.append(SourceResult(
                    source_name="bm25_text",
                    document_id=doc.get("uniprot_id", str(i)),
                    score=1.0 - (i * 0.02),  # Score decreciente por posición
                    rank=i + 1,
                    metadata={
                        "protein_name": doc.get("protein_name", ""),
                        "match_type": "bm25_text"
                    },
                    snippet=doc.get("function_description", "")[:200]
                ))
            
            return source_results
            
        except Exception as e:
            logger.debug(f"BM25 search skipped: {e}")
            return []


# ============================================================================
# INTENT DETECTOR
# ============================================================================

class IntentDetector:
    """Detector de intención del query"""
    
    # Palabras clave por intención
    INTENT_KEYWORDS = {
        QueryIntent.PROTEIN_SIMILARITY: [
            "similar", "homolog", "paralog", "ortholog", "family",
            "related", "like", "sequence", "alignment"
        ],
        QueryIntent.FUNCTION_SEARCH: [
            "function", "catalyze", "enzyme", "activity", "role",
            "involved in", "participates", "mechanism"
        ],
        QueryIntent.PATHWAY_EXPLORATION: [
            "pathway", "signaling", "cascade", "network", "metabolic",
            "biosynthesis", "regulation", "downstream", "upstream"
        ],
        QueryIntent.LITERATURE_SEARCH: [
            "paper", "publication", "study", "research", "reported",
            "described", "published", "citation", "reference"
        ],
        QueryIntent.STRUCTURE_ANALYSIS: [
            "structure", "fold", "domain", "motif", "binding site",
            "active site", "3d", "conformation", "alpha helix", "beta sheet"
        ],
        QueryIntent.INTERACTION_NETWORK: [
            "interact", "bind", "complex", "partner", "associate",
            "ppi", "protein-protein", "receptor", "ligand"
        ],
        QueryIntent.DRUG_TARGET: [
            "drug", "target", "inhibitor", "therapeutic", "treatment",
            "pharmaceutical", "medicine", "compound", "small molecule"
        ]
    }
    
    def detect(self, query: str) -> QueryIntent:
        """Detecta la intención del query"""
        query_lower = query.lower()
        
        # Verificar si es una secuencia de proteína
        if self._is_sequence(query):
            return QueryIntent.PROTEIN_SIMILARITY
        
        # Contar matches por intención
        intent_scores = {}
        for intent, keywords in self.INTENT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in query_lower)
            if score > 0:
                intent_scores[intent] = score
        
        if intent_scores:
            return max(intent_scores, key=intent_scores.get)
        
        return QueryIntent.GENERAL
    
    def _is_sequence(self, text: str) -> bool:
        """Verifica si es secuencia de proteína"""
        clean = text.upper().replace(" ", "").replace("\n", "")
        valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
        if len(clean) < 10:
            return False
        aa_count = sum(1 for c in clean if c in valid_aa)
        return (aa_count / len(clean)) > 0.9


# ============================================================================
# HYBRID SEARCH ENGINE
# ============================================================================

class HybridSearchEngine:
    """
    Motor de búsqueda híbrido que combina múltiples fuentes
    usando Reciprocal Rank Fusion (RRF)
    """
    
    def __init__(
        self,
        config: Optional[SearchConfig] = None,
        embedding_router = None,
        blast_integration = None
    ):
        self.config = config or SearchConfig()
        self._embedding_router = embedding_router
        self._blast_integration = blast_integration
        self._intent_detector = IntentDetector()
        
        # Inicializar fuentes
        self._sources: Dict[str, SearchSource] = {}
        self._initialized = False
        
        # Cache
        self._cache: Dict[str, SearchResponse] = {}
    
    async def initialize(self) -> None:
        """Inicializa todas las fuentes de búsqueda"""
        if self._initialized:
            return
        
        logger.info("🔄 Initializing Hybrid Search Engine...")
        
        # Crear fuentes vectoriales (5 vectores Milvus)
        vector_fields = [
            ("embedding_sequence_space", "vector_prott5"),
            ("embedding_sequence_esmc", "vector_esmc"),
            ("embedding_metadata_semantic", "vector_biolinkbert"),
            ("embedding_paper_knowledge", "vector_scibert"),
            ("embedding_network_space", "vector_node2vec"),
        ]
        
        for field, name in vector_fields:
            self._sources[name] = MilvusVectorSource(
                vector_field=field,
                source_name=name,
                config=self.config,
                embedding_router=self._embedding_router
            )
        
        # Crear fuente Neo4j
        self._sources["neo4j_graph"] = Neo4jGraphSource(self.config)
        
        # Crear fuente BLAST
        self._sources["blast_alignment"] = BlastAlignmentSource(
            self.config,
            self._blast_integration
        )
        
        # Crear fuente BM25
        self._sources["bm25_text"] = BM25TextSource(self.config)
        
        # Verificar disponibilidad
        available = []
        for name, source in self._sources.items():
            try:
                if await source.is_available():
                    available.append(name)
            except Exception as e:
                logger.warning(f"Source {name} check failed: {e}")
        
        logger.info(f"✅ Hybrid Search Engine initialized. Sources: {available}")
        self._initialized = True
    
    async def search(
        self,
        query: str,
        strategy: SearchStrategy = SearchStrategy.ADAPTIVE,
        limit: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        use_cache: bool = True
    ) -> SearchResponse:
        """
        Ejecuta búsqueda híbrida.
        
        Args:
            query: Query de búsqueda
            strategy: Estrategia a usar
            limit: Límite de resultados
            filters: Filtros adicionales
            use_cache: Usar cache
            
        Returns:
            SearchResponse con resultados fusionados
        """
        start_time = datetime.now()
        
        # Verificar cache
        if use_cache and self.config.cache_enabled:
            cache_key = self._get_cache_key(query, strategy, filters)
            if cache_key in self._cache:
                cached = self._cache[cache_key]
                cached.metadata["from_cache"] = True
                return cached
        
        # Detectar intención
        intent = self._intent_detector.detect(query)
        
        # Seleccionar fuentes según estrategia
        if strategy == SearchStrategy.ADAPTIVE:
            strategy = self._select_adaptive_strategy(intent)
        
        sources_to_use = self._get_sources_for_strategy(strategy)
        
        # Ejecutar búsquedas en paralelo
        all_results = await self._execute_parallel_search(
            query, sources_to_use, filters
        )
        
        # Fusionar resultados con RRF
        fused_results = self._rrf_fusion(all_results, limit)
        
        # Calcular tiempo
        elapsed = (datetime.now() - start_time).total_seconds() * 1000
        
        # Crear respuesta
        response = SearchResponse(
            query=query,
            strategy=strategy,
            detected_intent=intent,
            results=fused_results,
            total_results=len(fused_results),
            search_time_ms=elapsed,
            sources_used=list(sources_to_use),
            metadata={
                "from_cache": False,
                "filters_applied": filters or {},
                "rrf_k": self.config.rrf_k
            }
        )
        
        # Guardar en cache
        if use_cache and self.config.cache_enabled:
            self._cache[cache_key] = response
        
        return response
    
    def _select_adaptive_strategy(self, intent: QueryIntent) -> SearchStrategy:
        """Selecciona estrategia basada en intención"""
        
        strategy_map = {
            QueryIntent.PROTEIN_SIMILARITY: SearchStrategy.HYBRID_VECTOR_BLAST,
            QueryIntent.FUNCTION_SEARCH: SearchStrategy.HYBRID_VECTOR_GRAPH,
            QueryIntent.PATHWAY_EXPLORATION: SearchStrategy.GRAPH_ONLY,
            QueryIntent.LITERATURE_SEARCH: SearchStrategy.SEMANTIC_ONLY,
            QueryIntent.STRUCTURE_ANALYSIS: SearchStrategy.HYBRID_VECTOR_BLAST,
            QueryIntent.INTERACTION_NETWORK: SearchStrategy.HYBRID_GRAPH_BLAST,
            QueryIntent.DRUG_TARGET: SearchStrategy.FULL_HYBRID,
            QueryIntent.GENERAL: SearchStrategy.FULL_HYBRID
        }
        
        return strategy_map.get(intent, SearchStrategy.FULL_HYBRID)
    
    def _get_sources_for_strategy(self, strategy: SearchStrategy) -> Set[str]:
        """Obtiene fuentes a usar según estrategia"""
        
        vector_sources = {
            "vector_prott5", "vector_esmc", "vector_biolinkbert",
            "vector_scibert", "vector_node2vec"
        }
        
        strategy_sources = {
            SearchStrategy.SEMANTIC_ONLY: vector_sources | {"bm25_text"},
            SearchStrategy.GRAPH_ONLY: {"neo4j_graph"},
            SearchStrategy.BLAST_ONLY: {"blast_alignment"},
            SearchStrategy.HYBRID_VECTOR_GRAPH: vector_sources | {"neo4j_graph"},
            SearchStrategy.HYBRID_VECTOR_BLAST: vector_sources | {"blast_alignment"},
            SearchStrategy.HYBRID_GRAPH_BLAST: {"neo4j_graph", "blast_alignment"},
            SearchStrategy.FULL_HYBRID: vector_sources | {"neo4j_graph", "blast_alignment", "bm25_text"}
        }
        
        return strategy_sources.get(strategy, vector_sources)
    
    async def _execute_parallel_search(
        self,
        query: str,
        sources_to_use: Set[str],
        filters: Optional[Dict[str, Any]]
    ) -> Dict[str, List[SourceResult]]:
        """Ejecuta búsquedas en paralelo"""
        
        async def search_source(name: str) -> Tuple[str, List[SourceResult]]:
            source = self._sources.get(name)
            if not source:
                return (name, [])
            try:
                results = await asyncio.wait_for(
                    source.search(query, limit=self.config.max_results_per_source, filters=filters),
                    timeout=self._get_timeout_for_source(name)
                )
                return (name, results)
            except asyncio.TimeoutError:
                logger.warning(f"Source {name} timed out")
                return (name, [])
            except Exception as e:
                logger.error(f"Source {name} error: {e}")
                return (name, [])
        
        # Ejecutar en paralelo
        tasks = [search_source(name) for name in sources_to_use]
        results = await asyncio.gather(*tasks)
        
        return dict(results)
    
    def _get_timeout_for_source(self, source_name: str) -> float:
        """Obtiene timeout para una fuente"""
        if source_name.startswith("vector"):
            return self.config.vector_timeout
        elif source_name == "neo4j_graph":
            return self.config.graph_timeout
        elif source_name == "blast_alignment":
            return self.config.blast_timeout
        return 5.0
    
    def _rrf_fusion(
        self,
        all_results: Dict[str, List[SourceResult]],
        limit: int
    ) -> List[UnifiedResult]:
        """
        Aplica Reciprocal Rank Fusion para combinar resultados.
        
        RRF Score = Σ (weight_r * 1 / (k + rank_r(d)))
        """
        k = self.config.rrf_k
        
        # Agrupar por document_id
        doc_scores: Dict[str, Tuple[float, List[SourceResult]]] = {}
        
        for source_name, results in all_results.items():
            weight = self.config.vector_weights.get(
                source_name.replace("vector_", ""),
                1.0
            )
            
            for result in results:
                doc_id = result.document_id
                rrf_score = weight * (1.0 / (k + result.rank))
                
                if doc_id in doc_scores:
                    current_score, current_sources = doc_scores[doc_id]
                    doc_scores[doc_id] = (
                        current_score + rrf_score,
                        current_sources + [result]
                    )
                else:
                    doc_scores[doc_id] = (rrf_score, [result])
        
        # Ordenar por score
        sorted_docs = sorted(
            doc_scores.items(),
            key=lambda x: x[1][0],
            reverse=True
        )
        
        # Crear resultados unificados
        unified_results = []
        for rank, (doc_id, (score, sources)) in enumerate(sorted_docs[:limit], 1):
            # Combinar metadata de todas las fuentes
            combined_metadata = {}
            for source in sources:
                combined_metadata.update(source.metadata)
            
            unified_results.append(UnifiedResult(
                document_id=doc_id,
                final_score=score,
                final_rank=rank,
                sources=sources,
                source_count=len(sources),
                metadata=combined_metadata
            ))
        
        return unified_results
    
    def _get_cache_key(
        self,
        query: str,
        strategy: SearchStrategy,
        filters: Optional[Dict[str, Any]]
    ) -> str:
        """Genera clave de cache determinística"""
        content = f"{query}:{strategy.value}:{json.dumps(filters, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]


# ============================================================================
# FACTORY FUNCTION
# ============================================================================

async def create_hybrid_search_engine(
    config: Optional[SearchConfig] = None,
    embedding_router = None,
    blast_integration = None,
    initialize: bool = True
) -> HybridSearchEngine:
    """
    Factory para crear el motor de búsqueda híbrido.
    
    Args:
        config: Configuración
        embedding_router: Router de embeddings multi-modelo
        blast_integration: Integración BLAST
        initialize: Si True, inicializa automáticamente
        
    Returns:
        HybridSearchEngine configurado
    """
    engine = HybridSearchEngine(
        config=config,
        embedding_router=embedding_router,
        blast_integration=blast_integration
    )
    
    if initialize:
        await engine.initialize()
    
    return engine


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "SearchStrategy",
    "QueryIntent",
    "SearchConfig",
    "SourceResult",
    "UnifiedResult",
    "SearchResponse",
    "SearchSource",
    "MilvusVectorSource",
    "Neo4jGraphSource",
    "BlastAlignmentSource",
    "BM25TextSource",
    "IntentDetector",
    "HybridSearchEngine",
    "create_hybrid_search_engine",
]

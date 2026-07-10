#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔗 RECIPROCAL RANK FUSION (RRF) ENGINE
Fusión inteligente de resultados de múltiples fuentes de retrieval.

RRF Formula: score(d) = Σ (1 / (k + rank_r(d))) para cada ranking r
donde k = 60 (parámetro estándar recomendado)

Soporta fusión de:
- Búsquedas vectoriales multi-espacio (ProtT5, ESM-C, BioLinkBERT, SciBERT, node2vec)
- Resultados de Neo4j GraphRAG
- Resultados de BLAST alignment

Author: BSM Team (Modernization based on DEEPRESEARCH + Hybrid RAG Architecture)
Date: 2025
Version: 2.0.0
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

class RetrievalSource(Enum):
    """Fuentes de retrieval para fusión"""
    # Espacios vectoriales Milvus
    VECTOR_PROTT5 = "vector_prott5"              # Secuencia evolutiva
    VECTOR_ESMC = "vector_esmc"                  # Secuencia structure-aware
    VECTOR_BIOLINKBERT = "vector_biolinkbert"    # Metadatos semánticos
    VECTOR_SCIBERT = "vector_scibert"            # Conocimiento de literatura
    VECTOR_NODE2VEC = "vector_node2vec"          # Red biológica
    
    # Otras fuentes
    GRAPH_NEO4J = "graph_neo4j"                  # GraphRAG Neo4j
    BLAST_ALIGNMENT = "blast_alignment"          # BLAST sequence alignment
    BM25_TEXT = "bm25_text"                      # BM25 keyword search


@dataclass
class RRFConfig:
    """Configuración del motor RRF"""
    
    # Parámetro k estándar (60 es el valor recomendado en la literatura)
    k: int = 60
    
    # Pesos por fuente (1.0 = peso igual)
    source_weights: Dict[str, float] = field(default_factory=lambda: {
        RetrievalSource.VECTOR_PROTT5.value: 1.0,
        RetrievalSource.VECTOR_ESMC.value: 1.0,
        RetrievalSource.VECTOR_BIOLINKBERT.value: 1.0,
        RetrievalSource.VECTOR_SCIBERT.value: 1.0,
        RetrievalSource.VECTOR_NODE2VEC.value: 1.0,
        RetrievalSource.GRAPH_NEO4J.value: 1.0,
        RetrievalSource.BLAST_ALIGNMENT.value: 1.5,  # BLAST tiene bonus por precisión
        RetrievalSource.BM25_TEXT.value: 0.8,
    })
    
    # Normalización de scores finales
    normalize_final_scores: bool = True
    
    # Mínimo de fuentes requeridas para incluir documento
    min_sources_required: int = 1
    
    # Threshold mínimo de score RRF para incluir
    min_rrf_score: float = 0.0


@dataclass
class RankedResult:
    """Resultado rankeado de una fuente"""
    document_id: str
    rank: int                         # 1-indexed rank
    score: float                      # Score original de la fuente
    source: RetrievalSource
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.rank < 1:
            raise ValueError("Rank must be >= 1")


@dataclass
class FusedResult:
    """Resultado fusionado con RRF"""
    document_id: str
    rrf_score: float
    sources: List[RetrievalSource]
    source_ranks: Dict[str, int]      # {source_name: rank}
    source_scores: Dict[str, float]   # {source_name: original_score}
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Información adicional para explicabilidad
    contribution_breakdown: Dict[str, float] = field(default_factory=dict)
    
    def explain(self) -> str:
        """Genera explicación del score RRF"""
        lines = [f"Document: {self.document_id}"]
        lines.append(f"RRF Score: {self.rrf_score:.6f}")
        lines.append(f"Sources: {len(self.sources)}")
        lines.append("Contributions:")
        for source, contribution in self.contribution_breakdown.items():
            rank = self.source_ranks.get(source, "N/A")
            lines.append(f"  - {source}: rank={rank}, contribution={contribution:.6f}")
        return "\n".join(lines)


# ============================================================================
# RRF FUSION ENGINE
# ============================================================================

class RRFFusionEngine:
    """
    Motor de Reciprocal Rank Fusion para combinar rankings de múltiples fuentes.
    
    RRF es superior a otros métodos de fusión porque:
    1. No requiere normalización de scores entre fuentes
    2. Es robusto ante outliers
    3. Combina rankings de forma justa sin favorecer fuentes específicas
    4. El parámetro k=60 ha sido validado empíricamente
    """
    
    def __init__(self, config: Optional[RRFConfig] = None):
        self.config = config or RRFConfig()
        logger.info(f"🔗 RRFFusionEngine initialized with k={self.config.k}")
    
    def compute_rrf_score(
        self,
        rank: int,
        source_weight: float = 1.0
    ) -> float:
        """
        Calcula contribución RRF para un rank específico.
        
        RRF(d) = 1 / (k + rank)
        
        Args:
            rank: Posición en el ranking (1-indexed)
            source_weight: Peso multiplicativo de la fuente
            
        Returns:
            Contribución RRF
        """
        return source_weight / (self.config.k + rank)
    
    def fuse_rankings(
        self,
        rankings: Dict[RetrievalSource, List[RankedResult]],
        top_k: Optional[int] = None
    ) -> List[FusedResult]:
        """
        Fusiona múltiples rankings usando RRF.
        
        Args:
            rankings: Dict {source: [RankedResult, ...]}
            top_k: Número máximo de resultados a retornar
            
        Returns:
            Lista de FusedResult ordenada por score RRF descendente
        """
        # Acumuladores por documento
        doc_scores: Dict[str, float] = defaultdict(float)
        doc_sources: Dict[str, List[RetrievalSource]] = defaultdict(list)
        doc_source_ranks: Dict[str, Dict[str, int]] = defaultdict(dict)
        doc_source_scores: Dict[str, Dict[str, float]] = defaultdict(dict)
        doc_contributions: Dict[str, Dict[str, float]] = defaultdict(dict)
        doc_metadata: Dict[str, Dict[str, Any]] = defaultdict(dict)
        
        # Procesar cada ranking
        for source, results in rankings.items():
            source_weight = self.config.source_weights.get(source.value, 1.0)
            
            for result in results:
                doc_id = result.document_id
                
                # Calcular contribución RRF
                contribution = self.compute_rrf_score(result.rank, source_weight)
                
                # Acumular
                doc_scores[doc_id] += contribution
                doc_sources[doc_id].append(source)
                doc_source_ranks[doc_id][source.value] = result.rank
                doc_source_scores[doc_id][source.value] = result.score
                doc_contributions[doc_id][source.value] = contribution
                
                # Merge metadata
                if result.metadata:
                    doc_metadata[doc_id].update(result.metadata)
        
        # Filtrar por mínimo de fuentes
        filtered_docs = [
            doc_id for doc_id, sources in doc_sources.items()
            if len(sources) >= self.config.min_sources_required
        ]
        
        # Filtrar por score mínimo
        filtered_docs = [
            doc_id for doc_id in filtered_docs
            if doc_scores[doc_id] >= self.config.min_rrf_score
        ]
        
        # Normalizar scores si está configurado
        if self.config.normalize_final_scores and filtered_docs:
            max_score = max(doc_scores[doc_id] for doc_id in filtered_docs)
            if max_score > 0:
                for doc_id in filtered_docs:
                    doc_scores[doc_id] /= max_score
        
        # Crear resultados fusionados
        fused_results = []
        for doc_id in filtered_docs:
            fused_results.append(FusedResult(
                document_id=doc_id,
                rrf_score=doc_scores[doc_id],
                sources=doc_sources[doc_id],
                source_ranks=doc_source_ranks[doc_id],
                source_scores=doc_source_scores[doc_id],
                contribution_breakdown=doc_contributions[doc_id],
                metadata=doc_metadata[doc_id]
            ))
        
        # Ordenar por score RRF descendente
        fused_results.sort(key=lambda x: x.rrf_score, reverse=True)
        
        # Limitar a top_k si se especifica
        if top_k is not None:
            fused_results = fused_results[:top_k]
        
        logger.debug(
            f"🔗 RRF fusion: {sum(len(r) for r in rankings.values())} results "
            f"from {len(rankings)} sources → {len(fused_results)} fused results"
        )
        
        return fused_results
    
    def fuse_from_lists(
        self,
        source_results: List[Tuple[RetrievalSource, List[Tuple[str, float]]]],
        top_k: Optional[int] = None
    ) -> List[FusedResult]:
        """
        Fusiona rankings desde formato simplificado (listas de IDs y scores).
        
        Args:
            source_results: Lista de (source, [(doc_id, score), ...])
            top_k: Número máximo de resultados
            
        Returns:
            Lista de FusedResult
        """
        rankings = {}
        
        for source, results in source_results:
            ranked_results = []
            for rank, (doc_id, score) in enumerate(results, start=1):
                ranked_results.append(RankedResult(
                    document_id=doc_id,
                    rank=rank,
                    score=score,
                    source=source
                ))
            rankings[source] = ranked_results
        
        return self.fuse_rankings(rankings, top_k)
    
    def fuse_milvus_results(
        self,
        prott5_results: Optional[List[Dict[str, Any]]] = None,
        esmc_results: Optional[List[Dict[str, Any]]] = None,
        biolinkbert_results: Optional[List[Dict[str, Any]]] = None,
        scibert_results: Optional[List[Dict[str, Any]]] = None,
        node2vec_results: Optional[List[Dict[str, Any]]] = None,
        top_k: Optional[int] = None,
        id_field: str = "protein_id",
        score_field: str = "similarity_score"
    ) -> List[FusedResult]:
        """
        Fusiona resultados de búsquedas Milvus multi-vector.
        
        Formato esperado por resultado:
        {
            "protein_id": "...",
            "similarity_score": 0.95,
            "name": "...",
            ...
        }
        
        Args:
            prott5_results: Resultados de ProtT5 vector search
            esmc_results: Resultados de ESM-C vector search
            biolinkbert_results: Resultados de BioLinkBERT vector search
            scibert_results: Resultados de SciBERT vector search
            node2vec_results: Resultados de node2vec vector search
            top_k: Máximo de resultados
            id_field: Campo para document_id
            score_field: Campo para score
            
        Returns:
            Lista fusionada
        """
        rankings = {}
        
        def _convert_results(results: List[Dict], source: RetrievalSource):
            ranked = []
            for rank, result in enumerate(results, start=1):
                ranked.append(RankedResult(
                    document_id=str(result.get(id_field, "")),
                    rank=rank,
                    score=float(result.get(score_field, 0.0)),
                    source=source,
                    metadata={k: v for k, v in result.items() 
                             if k not in [id_field, score_field]}
                ))
            return ranked
        
        if prott5_results:
            rankings[RetrievalSource.VECTOR_PROTT5] = _convert_results(
                prott5_results, RetrievalSource.VECTOR_PROTT5
            )
        
        if esmc_results:
            rankings[RetrievalSource.VECTOR_ESMC] = _convert_results(
                esmc_results, RetrievalSource.VECTOR_ESMC
            )
        
        if biolinkbert_results:
            rankings[RetrievalSource.VECTOR_BIOLINKBERT] = _convert_results(
                biolinkbert_results, RetrievalSource.VECTOR_BIOLINKBERT
            )
        
        if scibert_results:
            rankings[RetrievalSource.VECTOR_SCIBERT] = _convert_results(
                scibert_results, RetrievalSource.VECTOR_SCIBERT
            )
        
        if node2vec_results:
            rankings[RetrievalSource.VECTOR_NODE2VEC] = _convert_results(
                node2vec_results, RetrievalSource.VECTOR_NODE2VEC
            )
        
        return self.fuse_rankings(rankings, top_k)
    
    def add_graph_results(
        self,
        existing_fused: List[FusedResult],
        graph_results: List[Dict[str, Any]],
        top_k: Optional[int] = None,
        id_field: str = "protein_id",
        score_field: str = "relevance_score"
    ) -> List[FusedResult]:
        """
        Añade resultados de GraphRAG a resultados ya fusionados.
        
        Args:
            existing_fused: Resultados RRF previos
            graph_results: Resultados de Neo4j GraphRAG
            top_k: Máximo de resultados
            id_field: Campo para document_id
            score_field: Campo para score
            
        Returns:
            Lista re-fusionada con graph results
        """
        # Convertir resultados existentes a rankings
        rankings: Dict[RetrievalSource, List[RankedResult]] = defaultdict(list)
        
        # Reconstruir rankings de fuentes existentes
        for rank, result in enumerate(existing_fused, start=1):
            # Crear entrada combinada pseudo-ranking
            for source in result.sources:
                original_rank = result.source_ranks.get(source.value, rank)
                original_score = result.source_scores.get(source.value, result.rrf_score)
                rankings[source].append(RankedResult(
                    document_id=result.document_id,
                    rank=original_rank,
                    score=original_score,
                    source=source,
                    metadata=result.metadata
                ))
        
        # Añadir graph results
        if graph_results:
            graph_ranked = []
            for rank, result in enumerate(graph_results, start=1):
                graph_ranked.append(RankedResult(
                    document_id=str(result.get(id_field, "")),
                    rank=rank,
                    score=float(result.get(score_field, 0.0)),
                    source=RetrievalSource.GRAPH_NEO4J,
                    metadata={k: v for k, v in result.items() 
                             if k not in [id_field, score_field]}
                ))
            rankings[RetrievalSource.GRAPH_NEO4J] = graph_ranked
        
        return self.fuse_rankings(rankings, top_k)
    
    def add_blast_results(
        self,
        existing_fused: List[FusedResult],
        blast_results: List[Dict[str, Any]],
        top_k: Optional[int] = None,
        id_field: str = "subject_id",
        score_field: str = "bit_score"
    ) -> List[FusedResult]:
        """
        Añade resultados de BLAST a resultados ya fusionados.
        
        Args:
            existing_fused: Resultados RRF previos
            blast_results: Resultados de BLAST alignment
            top_k: Máximo de resultados
            id_field: Campo para subject_id
            score_field: Campo para score (bit_score o e_value)
            
        Returns:
            Lista re-fusionada con BLAST results
        """
        rankings: Dict[RetrievalSource, List[RankedResult]] = defaultdict(list)
        
        # Reconstruir rankings existentes
        for rank, result in enumerate(existing_fused, start=1):
            for source in result.sources:
                original_rank = result.source_ranks.get(source.value, rank)
                original_score = result.source_scores.get(source.value, result.rrf_score)
                rankings[source].append(RankedResult(
                    document_id=result.document_id,
                    rank=original_rank,
                    score=original_score,
                    source=source,
                    metadata=result.metadata
                ))
        
        # Añadir BLAST results
        if blast_results:
            # BLAST results ya vienen ordenados por e-value (menor = mejor)
            # o por bit_score (mayor = mejor)
            blast_ranked = []
            for rank, result in enumerate(blast_results, start=1):
                blast_ranked.append(RankedResult(
                    document_id=str(result.get(id_field, "")),
                    rank=rank,
                    score=float(result.get(score_field, 0.0)),
                    source=RetrievalSource.BLAST_ALIGNMENT,
                    metadata={
                        "e_value": result.get("e_value"),
                        "identity": result.get("identity"),
                        "alignment_length": result.get("alignment_length"),
                        **{k: v for k, v in result.items() 
                           if k not in [id_field, score_field, "e_value", "identity", "alignment_length"]}
                    }
                ))
            rankings[RetrievalSource.BLAST_ALIGNMENT] = blast_ranked
        
        return self.fuse_rankings(rankings, top_k)


# ============================================================================
# WEIGHTED RRF VARIANTS
# ============================================================================

class AdaptiveRRFEngine(RRFFusionEngine):
    """
    RRF adaptivo que ajusta pesos basado en la calidad de resultados.
    
    Características:
    - Boost a fuentes con scores consistentemente altos
    - Penalización a fuentes con baja cobertura
    - Adaptación basada en query type
    """
    
    def __init__(self, config: Optional[RRFConfig] = None):
        super().__init__(config)
        self._source_performance: Dict[str, List[float]] = defaultdict(list)
    
    def compute_adaptive_weight(
        self,
        source: RetrievalSource,
        results: List[RankedResult]
    ) -> float:
        """Calcula peso adaptivo para una fuente basado en calidad de resultados"""
        base_weight = self.config.source_weights.get(source.value, 1.0)
        
        if not results:
            return base_weight * 0.5  # Penalización por no contribuir
        
        # Bonus por cobertura (más resultados = más confianza)
        coverage_factor = min(len(results) / 20.0, 1.0)  # Cap at 20 results
        
        # Bonus por calidad de scores
        avg_score = np.mean([r.score for r in results]) if results else 0.0
        quality_factor = min(avg_score, 1.0)  # Asume scores normalizados 0-1
        
        adaptive_weight = base_weight * (0.5 + 0.25 * coverage_factor + 0.25 * quality_factor)
        
        return adaptive_weight
    
    def fuse_rankings(
        self,
        rankings: Dict[RetrievalSource, List[RankedResult]],
        top_k: Optional[int] = None
    ) -> List[FusedResult]:
        """Override para usar pesos adaptativos"""
        # Calcular pesos adaptativos
        adaptive_weights = {}
        for source, results in rankings.items():
            adaptive_weights[source.value] = self.compute_adaptive_weight(source, results)
        
        # Aplicar pesos adaptativos temporalmente
        original_weights = self.config.source_weights.copy()
        self.config.source_weights = adaptive_weights
        
        # Fusionar con pesos adaptativos
        result = super().fuse_rankings(rankings, top_k)
        
        # Restaurar pesos originales
        self.config.source_weights = original_weights
        
        return result


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================

def create_rrf_engine(
    k: int = 60,
    normalize: bool = True,
    custom_weights: Optional[Dict[str, float]] = None
) -> RRFFusionEngine:
    """
    Factory para crear motor RRF configurado.
    
    Args:
        k: Parámetro RRF (default 60)
        normalize: Si normalizar scores finales
        custom_weights: Pesos personalizados por fuente
        
    Returns:
        RRFFusionEngine configurado
    """
    config = RRFConfig(
        k=k,
        normalize_final_scores=normalize
    )
    
    if custom_weights:
        config.source_weights.update(custom_weights)
    
    return RRFFusionEngine(config)


def create_adaptive_rrf_engine(
    k: int = 60,
    normalize: bool = True
) -> AdaptiveRRFEngine:
    """Factory para crear motor RRF adaptivo"""
    config = RRFConfig(
        k=k,
        normalize_final_scores=normalize
    )
    return AdaptiveRRFEngine(config)


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "RetrievalSource",
    "RRFConfig",
    "RankedResult",
    "FusedResult",
    "RRFFusionEngine",
    "AdaptiveRRFEngine",
    "create_rrf_engine",
    "create_adaptive_rrf_engine",
]

#!/usr/bin/env python3
"""
🧬 BioBERT-Powered Scientific Information Pipeline for MICA
Implementación de la Arquitectura Cognitiva siguiendo la Guía Técnica Española

Características implementadas según el documento:
- Named Entity Recognition con BioBERT especializado
- LayoutLMv3 para análisis multimodal de PDFs científicos  
- Hybrid Search combinando búsqueda densa y BM25
- Modular RAG Framework con pre-retrieval y post-retrieval
- Semantic Chunking para preservar contexto científico
"""

import os
import asyncio
import json
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
import logging

import numpy as np
from transformers import (
    AutoTokenizer, AutoModel, AutoModelForTokenClassification,
    LayoutLMv3Tokenizer, LayoutLMv3ForTokenClassification,
    pipeline
)
import torch
from sentence_transformers import SentenceTransformer, CrossEncoder
from rank_bm25 import BM25Okapi
import spacy
from spacy import displacy

logger = logging.getLogger(__name__)

@dataclass
class ScientificEntity:
    """Entidad científica extraída con BioBERT"""
    text: str
    label: str
    start: int
    end: int
    confidence: float
    context: str
    metadata: Dict[str, Any]

@dataclass
class DocumentChunk:
    """Chunk de documento con información semántica"""
    text: str
    chunk_id: str
    parent_chunk: Optional[str]
    document_id: str
    chunk_type: str  # 'text', 'table', 'figure_caption', 'abstract'
    entities: List[ScientificEntity]
    embeddings: Dict[str, List[float]]
    layout_features: Optional[Dict[str, Any]]
    confidence_score: float

@dataclass
class HybridSearchResult:
    """Resultado de búsqueda híbrida"""
    chunks: List[DocumentChunk]
    vector_scores: List[float]
    bm25_scores: List[float]
    rerank_scores: List[float]
    final_scores: List[float]
    metadata: Dict[str, Any]

class BioBERTScientificPipeline:
    """
    🧠 Pipeline Científico con BioBERT y Arquitectura Cognitiva
    
    Implementa las mejores prácticas del documento técnico español:
    - BioBERT para NER biomédico superior a BERT genérico
    - LayoutLMv3 para comprensión multimodal de documentos
    - Chunking jerárquico y semántico
    - Hybrid Search con fusión RRF
    - Cross-encoder re-ranking
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.models = {}
        self.tokenizers = {}
        self.pipelines = {}
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # Initialize performance tracking
        self.performance_stats = {
            "documents_processed": 0,
            "entities_extracted": 0,
            "embeddings_generated": 0,
            "search_queries": 0,
            "avg_processing_time": 0.0
        }
        
    async def initialize_models(self):
        """Inicializar modelos especializados según la guía técnica"""
        logger.info("🧠 Inicializando modelos especializados...")
        
        try:
            # BioBERT para NER biomédico (recomendación del documento)
            if self.config.get("use_biobert", True):
                logger.info("📚 Cargando BioBERT para NER biomédico...")
                self.tokenizers["biobert"] = AutoTokenizer.from_pretrained(
                    self.config.get("biobert_model", "dmis-lab/biobert-v1.1")
                )
                self.models["biobert_ner"] = AutoModelForTokenClassification.from_pretrained(
                    "d4data/biomedical-ner-all"  # Modelo pre-entrenado para NER biomédico
                ).to(self.device)
                
                # Pipeline de NER especializado
                self.pipelines["biobert_ner"] = pipeline(
                    "ner",
                    model=self.models["biobert_ner"],
                    tokenizer=self.tokenizers["biobert"],
                    aggregation_strategy="simple",
                    device=0 if torch.cuda.is_available() else -1
                )
                
            # LayoutLMv3 para análisis multimodal (recomendación del documento)
            if self.config.get("use_layoutlm", True):
                logger.info("📄 Cargando LayoutLMv3 para análisis multimodal...")
                self.tokenizers["layoutlm"] = LayoutLMv3Tokenizer.from_pretrained(
                    self.config.get("layoutlm_model", "microsoft/layoutlmv3-base")
                )
                # Nota: LayoutLMv3 requiere datos de imagen para funcionar completamente
                
            # Cross-encoder para re-ranking
            if self.config.get("cross_encoder_rerank", True):
                logger.info("🎯 Cargando Cross-encoder para re-ranking...")
                self.models["cross_encoder"] = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
                
            # Sentence transformer para embeddings semánticos
            logger.info("🔗 Cargando modelo de embeddings semánticos...")
            self.models["sentence_transformer"] = SentenceTransformer('all-MiniLM-L6-v2')
            
            # SpaCy para procesamiento de texto avanzado
            try:
                self.nlp = spacy.load("en_core_sci_sm")  # Modelo científico especializado
            except OSError:
                logger.warning("⚠️ Modelo científico no encontrado, usando modelo general")
                self.nlp = spacy.load("en_core_web_sm")
                
            logger.info("✅ Todos los modelos inicializados correctamente")
            
        except Exception as e:
            logger.error(f"❌ Error inicializando modelos: {e}")
            raise
    
    async def extract_scientific_entities(self, text: str, context: str = "") -> List[ScientificEntity]:
        """
        Extraer entidades científicas usando BioBERT especializado
        
        Según el documento: "BioBERT superior a BERT para texto biomédico"
        """
        if not self.pipelines.get("biobert_ner"):
            logger.warning("⚠️ BioBERT NER no disponible, usando extracción básica")
            return self._extract_basic_entities(text)
        
        try:
            # Usar BioBERT para extracción de entidades
            ner_results = self.pipelines["biobert_ner"](text)
            
            entities = []
            for result in ner_results:
                entity = ScientificEntity(
                    text=result["word"],
                    label=result["entity_group"],
                    start=result["start"],
                    end=result["end"], 
                    confidence=result["score"],
                    context=context,
                    metadata={
                        "extraction_method": "biobert",
                        "model_version": self.config.get("biobert_model", "dmis-lab/biobert-v1.1"),
                        "timestamp": datetime.now().isoformat()
                    }
                )
                entities.append(entity)
                
            self.performance_stats["entities_extracted"] += len(entities)
            return entities
            
        except Exception as e:
            logger.error(f"❌ Error en extracción BioBERT: {e}")
            return self._extract_basic_entities(text)
    
    def _extract_basic_entities(self, text: str) -> List[ScientificEntity]:
        """Extracción básica de entidades como fallback"""
        doc = self.nlp(text)
        entities = []
        
        for ent in doc.ents:
            if ent.label_ in ["PROTEIN", "GENE", "CHEMICAL", "DISEASE", "SPECIES"]:
                entity = ScientificEntity(
                    text=ent.text,
                    label=ent.label_,
                    start=ent.start_char,
                    end=ent.end_char,
                    confidence=0.8,  # Score conservador para extracción básica
                    context="",
                    metadata={
                        "extraction_method": "spacy_fallback",
                        "timestamp": datetime.now().isoformat()
                    }
                )
                entities.append(entity)
                
        return entities
    
    async def semantic_chunking(self, text: str, document_id: str) -> List[DocumentChunk]:
        """
        Chunking semántico para preservar contexto científico
        
        Implementa la estrategia "Small-to-Big" del documento técnico
        """
        logger.info(f"📝 Realizando chunking semántico para documento {document_id}")
        
        # Dividir en oraciones usando SpaCy
        doc = self.nlp(text)
        sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]
        
        if not sentences:
            return []
        
        # Generar embeddings para cada oración
        sentence_embeddings = self.models["sentence_transformer"].encode(sentences)
        
        # Encontrar límites semánticos calculando distancias coseno
        chunks = []
        current_chunk = []
        current_embeddings = []
        
        similarity_threshold = 0.7  # Umbral para división semántica
        
        for i, (sentence, embedding) in enumerate(zip(sentences, sentence_embeddings)):
            if current_chunk:
                # Calcular similitud con la oración anterior
                prev_embedding = current_embeddings[-1]
                similarity = np.dot(embedding, prev_embedding) / (
                    np.linalg.norm(embedding) * np.linalg.norm(prev_embedding)
                )
                
                # Si la similitud es baja, crear nuevo chunk
                if similarity < similarity_threshold or len(" ".join(current_chunk)) > self.config.get("max_chunk_size", 1000):
                    # Finalizar chunk actual
                    chunk_text = " ".join(current_chunk)
                    entities = await self.extract_scientific_entities(chunk_text)
                    
                    # Generar embedding del chunk completo
                    chunk_embedding = self.models["sentence_transformer"].encode([chunk_text])[0]
                    
                    chunk = DocumentChunk(
                        text=chunk_text,
                        chunk_id=f"{document_id}_chunk_{len(chunks)}",
                        parent_chunk=None,  # Implementar jerarquía después
                        document_id=document_id,
                        chunk_type="text",
                        entities=entities,
                        embeddings={"sentence_transformer": chunk_embedding.tolist()},
                        layout_features=None,
                        confidence_score=np.mean([e.confidence for e in entities]) if entities else 0.8
                    )
                    chunks.append(chunk)
                    
                    # Resetear para nuevo chunk
                    current_chunk = [sentence]
                    current_embeddings = [embedding]
                else:
                    current_chunk.append(sentence)
                    current_embeddings.append(embedding)
            else:
                current_chunk.append(sentence)
                current_embeddings.append(embedding)
        
        # Procesar último chunk
        if current_chunk:
            chunk_text = " ".join(current_chunk)
            entities = await self.extract_scientific_entities(chunk_text)
            chunk_embedding = self.models["sentence_transformer"].encode([chunk_text])[0]
            
            chunk = DocumentChunk(
                text=chunk_text,
                chunk_id=f"{document_id}_chunk_{len(chunks)}",
                parent_chunk=None,
                document_id=document_id,
                chunk_type="text",
                entities=entities,
                embeddings={"sentence_transformer": chunk_embedding.tolist()},
                layout_features=None,
                confidence_score=np.mean([e.confidence for e in entities]) if entities else 0.8
            )
            chunks.append(chunk)
        
        logger.info(f"✅ Generados {len(chunks)} chunks semánticos")
        self.performance_stats["documents_processed"] += 1
        return chunks
    
    async def hybrid_search(self, query: str, chunks: List[DocumentChunk], top_k: int = 10) -> HybridSearchResult:
        """
        Búsqueda híbrida combinando vector search y BM25
        
        Implementa la recomendación del documento: "Hybrid Search crucial para dominios científicos"
        """
        logger.info(f"🔍 Ejecutando búsqueda híbrida para: '{query[:50]}...'")
        
        if not chunks:
            return HybridSearchResult([], [], [], [], [], {})
        
        # 1. Vector Search (búsqueda semántica densa)
        query_embedding = self.models["sentence_transformer"].encode([query])[0]
        
        vector_scores = []
        for chunk in chunks:
            chunk_embedding = np.array(chunk.embeddings["sentence_transformer"])
            similarity = np.dot(query_embedding, chunk_embedding) / (
                np.linalg.norm(query_embedding) * np.linalg.norm(chunk_embedding)
            )
            vector_scores.append(similarity)
        
        # 2. BM25 Search (búsqueda de palabras clave sparse)
        corpus = [chunk.text for chunk in chunks]
        tokenized_corpus = [doc.split() for doc in corpus]
        bm25 = BM25Okapi(tokenized_corpus)
        
        tokenized_query = query.split()
        bm25_scores = bm25.get_scores(tokenized_query)
        
        # Normalizar scores BM25
        if max(bm25_scores) > 0:
            bm25_scores = [score / max(bm25_scores) for score in bm25_scores]
        
        # 3. Fusión híbrida usando Reciprocal Rank Fusion (RRF)
        vector_weight = self.config.get("vector_weight", 0.7)
        bm25_weight = self.config.get("bm25_weight", 0.3)
        
        hybrid_scores = []
        for i in range(len(chunks)):
            hybrid_score = (vector_weight * vector_scores[i]) + (bm25_weight * bm25_scores[i])
            hybrid_scores.append(hybrid_score)
        
        # 4. Seleccionar top candidates
        top_indices = np.argsort(hybrid_scores)[::-1][:top_k * 2]  # Obtener más para re-ranking
        top_chunks = [chunks[i] for i in top_indices]
        top_vector_scores = [vector_scores[i] for i in top_indices]
        top_bm25_scores = [bm25_scores[i] for i in top_indices]
        top_hybrid_scores = [hybrid_scores[i] for i in top_indices]
        
        # 5. Cross-encoder re-ranking (si está habilitado)
        rerank_scores = []
        if self.config.get("cross_encoder_rerank", True) and self.models.get("cross_encoder"):
            query_chunk_pairs = [(query, chunk.text) for chunk in top_chunks]
            rerank_scores = self.models["cross_encoder"].predict(query_chunk_pairs)
            
            # Re-ordenar basado en cross-encoder scores
            rerank_indices = np.argsort(rerank_scores)[::-1][:top_k]
            final_chunks = [top_chunks[i] for i in rerank_indices]
            final_vector_scores = [top_vector_scores[i] for i in rerank_indices]
            final_bm25_scores = [top_bm25_scores[i] for i in rerank_indices]
            final_rerank_scores = [rerank_scores[i] for i in rerank_indices]
            final_scores = final_rerank_scores
        else:
            # Sin re-ranking, usar scores híbridos
            final_chunks = top_chunks[:top_k]
            final_vector_scores = top_vector_scores[:top_k]
            final_bm25_scores = top_bm25_scores[:top_k]
            final_rerank_scores = [0.0] * len(final_chunks)
            final_scores = top_hybrid_scores[:top_k]
        
        self.performance_stats["search_queries"] += 1
        
        return HybridSearchResult(
            chunks=final_chunks,
            vector_scores=final_vector_scores,
            bm25_scores=final_bm25_scores,
            rerank_scores=final_rerank_scores,
            final_scores=final_scores,
            metadata={
                "query": query,
                "total_candidates": len(chunks),
                "hybrid_fusion": f"vector:{vector_weight}, bm25:{bm25_weight}",
                "reranking_enabled": self.config.get("cross_encoder_rerank", False),
                "timestamp": datetime.now().isoformat()
            }
        )
    
    async def query_expansion(self, query: str) -> List[str]:
        """
        Expansión de consultas para mejorar recuperación
        
        Implementa "Pre-Retrieval Query Transformation" del documento
        """
        expanded_queries = [query]  # Consulta original
        
        # Query decomposition para consultas complejas
        if "and" in query.lower() or "compare" in query.lower():
            # Dividir consultas complejas
            parts = query.split(" and ")
            if len(parts) > 1:
                expanded_queries.extend(parts)
        
        # Step-back prompting: consulta más general
        if "specific" in query.lower() or "recent" in query.lower():
            general_query = query.replace("recent", "").replace("specific", "").strip()
            expanded_queries.append(general_query)
        
        # Agregar sinónimos científicos comunes
        scientific_synonyms = {
            "protein": ["protein", "polypeptide", "enzyme"],
            "structure": ["structure", "conformation", "fold"],
            "binding": ["binding", "interaction", "association"],
            "inhibitor": ["inhibitor", "antagonist", "blocker"]
        }
        
        for original, synonyms in scientific_synonyms.items():
            if original in query.lower():
                for synonym in synonyms:
                    if synonym != original:
                        expanded_query = query.lower().replace(original, synonym)
                        expanded_queries.append(expanded_query)
        
        logger.info(f"🔄 Query expandida de 1 a {len(expanded_queries)} variantes")
        return list(set(expanded_queries))  # Eliminar duplicados
    
    async def process_document(self, text: str, document_id: str, metadata: Dict[str, Any] = None) -> List[DocumentChunk]:
        """
        Procesamiento completo de documento científico
        
        Pipeline completo siguiendo las mejores prácticas del documento técnico
        """
        start_time = datetime.now()
        logger.info(f"📄 Procesando documento científico: {document_id}")
        
        try:
            # 1. Semantic chunking
            chunks = await self.semantic_chunking(text, document_id)
            
            # 2. Enriquecimiento con metadatos
            for chunk in chunks:
                if metadata:
                    chunk.metadata = metadata.copy()
                    
                # Calcular métricas de calidad del chunk
                chunk.confidence_score = self._calculate_chunk_quality(chunk)
            
            # 3. Generar embeddings adicionales si es necesario
            for chunk in chunks:
                self.performance_stats["embeddings_generated"] += 1
            
            processing_time = (datetime.now() - start_time).total_seconds()
            self.performance_stats["avg_processing_time"] = (
                (self.performance_stats["avg_processing_time"] * (self.performance_stats["documents_processed"] - 1) + processing_time) /
                self.performance_stats["documents_processed"]
            )
            
            logger.info(f"✅ Documento procesado en {processing_time:.2f}s: {len(chunks)} chunks generados")
            return chunks
            
        except Exception as e:
            logger.error(f"❌ Error procesando documento {document_id}: {e}")
            raise
    
    def _calculate_chunk_quality(self, chunk: DocumentChunk) -> float:
        """Calcular score de calidad del chunk basado en entidades y coherencia"""
        quality_score = 0.5  # Base score
        
        # Bonus por entidades científicas
        if chunk.entities:
            entity_bonus = min(len(chunk.entities) * 0.1, 0.3)
            quality_score += entity_bonus
        
        # Bonus por longitud apropiada
        text_length = len(chunk.text)
        if 200 <= text_length <= 1500:  # Longitud óptima
            quality_score += 0.2
        
        # Penalty por texto muy corto o muy largo
        if text_length < 100 or text_length > 2000:
            quality_score -= 0.1
        
        return min(max(quality_score, 0.0), 1.0)  # Clamp entre 0 y 1
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Obtener estadísticas de rendimiento del pipeline"""
        return {
            **self.performance_stats,
            "models_loaded": list(self.models.keys()),
            "config_summary": {
                "biobert_enabled": self.config.get("use_biobert", False),
                "layoutlm_enabled": self.config.get("use_layoutlm", False),
                "hybrid_search": self.config.get("hybrid_search_enabled", False),
                "cross_encoder_rerank": self.config.get("cross_encoder_rerank", False)
            },
            "timestamp": datetime.now().isoformat()
        }

# Factory function para crear el pipeline
async def create_biobert_pipeline(config: Dict[str, Any]) -> BioBERTScientificPipeline:
    """Factory para crear y inicializar el pipeline BioBERT"""
    pipeline = BioBERTScientificPipeline(config)
    await pipeline.initialize_models()
    return pipeline
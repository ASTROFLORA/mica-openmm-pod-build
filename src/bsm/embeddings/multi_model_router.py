#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM MULTI-MODEL EMBEDDING ROUTER
Router inteligente para embeddings multi-modelo: ProtT5, ESM-C, BioLinkBERT, SciBERT, node2vec

Arquitectura 5-Vectores:
- embedding_sequence_space (1024D) - ProtT5: Patrones evolutivos, dominios
- embedding_sequence_esmc (2560D) - ESM-C 6B: Secuencia structure-aware
- embedding_metadata_semantic (768D) - BioLinkBERT: Semántica NLP sobre anotaciones
- embedding_paper_knowledge (768D) - SciBERT: Conocimiento derivado de literatura
- embedding_network_space (512D) - node2vec: Procesos biológicos, pathways

Author: BSM Team (Modernization based on DEEPRESEARCH + 5-Vector Architecture)
Date: 2025
Version: 2.0.0
"""

import asyncio
import hashlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

class EmbeddingSpace(Enum):
    """Espacios de embedding disponibles en arquitectura 5-vectores (schema v3)"""
    SEQUENCE_PROTT5 = "embedding_sequence_space"       # 1024D - ProtT5
    SEQUENCE_ESM2 = "embedding_esm2"                   # 1280D - ESM2-650M
    SEQUENCE_ESMC = "embedding_esm2"                   # backward-compat alias → ESM2
    METADATA_SEMANTIC = "embedding_metadata_semantic"  # 768D - BioLinkBERT (deprecated, backward-compat)
    PAPER_KNOWLEDGE = "embedding_paper_knowledge"      # 768D - SciBERT (deprecated, backward-compat)
    NETWORK_SPACE = "embedding_network_space"          # 512D - node2vec
    STRUCTURE_AF2 = "embedding_structure_af2"          # 384D - DPEB AF2 (s3://deepdrug-dpeb/)
    STRUCTURE_DCT = "embedding_dct_domain"             # 480D - DCT domain fingerprint


@dataclass
class EmbeddingModelConfig:
    """Configuración para un modelo de embeddings específico"""
    name: str
    huggingface_id: str
    dimension: int
    max_length: int
    pooling_strategy: str = "mean"  # mean, cls, max, last
    normalize: bool = True
    batch_size: int = 8
    device: str = "auto"  # auto, cpu, cuda
    cache_dir: Optional[str] = None
    trust_remote_code: bool = False
    
    def __post_init__(self):
        if self.device == "auto":
            try:
                import torch
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                self.device = "cpu"


@dataclass
class MultiModelRouterConfig:
    """Configuración completa del router multi-modelo"""
    
    # Modelo ProtT5 para secuencias de proteínas (1024D)
    prott5: EmbeddingModelConfig = field(default_factory=lambda: EmbeddingModelConfig(
        name="ProtT5-XL-U50",
        huggingface_id="Rostlab/prot_t5_xl_uniref50",
        dimension=1024,
        max_length=1024,
        pooling_strategy="mean",
        trust_remote_code=False
    ))
    
    # Modelo ESM2-650M para secuencias ricas (1280D)
    esmc: EmbeddingModelConfig = field(default_factory=lambda: EmbeddingModelConfig(
        name="ESM2-650M",
        huggingface_id="facebook/esm2_t33_650M_UR50D",
        dimension=1280,
        max_length=1024,
        pooling_strategy="mean",
        trust_remote_code=False
    ))

    # Modelo BioLinkBERT para metadatos semánticos (768D)
    biolinkbert: EmbeddingModelConfig = field(default_factory=lambda: EmbeddingModelConfig(
        name="BioLinkBERT-base",
        huggingface_id="michiyasunaga/BioLinkBERT-base",
        dimension=768,
        max_length=512,
        pooling_strategy="mean"
    ))
    
    # Modelo SciBERT para conocimiento de literatura (768D)
    scibert: EmbeddingModelConfig = field(default_factory=lambda: EmbeddingModelConfig(
        name="SciBERT-uncased",
        huggingface_id="allenai/scibert_scivocab_uncased",
        dimension=768,
        max_length=512,
        pooling_strategy="mean"
    ))
    
    # Cache settings
    cache_enabled: bool = True
    cache_dir: str = "./cache/embeddings"
    
    # Fallback models (lighter alternatives when GPU memory is limited)
    use_fallback_models: bool = True
    
    # node2vec settings (trained separately on network data)
    node2vec_dimension: int = 512

    # af2 settings (pre-computed DPEB, no model loading)
    af2_dimension: int = 384


# ============================================================================
# BASE EMBEDDER INTERFACE
# ============================================================================

class BaseEmbedder(ABC):
    """Interfaz base para todos los embedders"""
    
    def __init__(self, config: EmbeddingModelConfig):
        self.config = config
        self._model = None
        self._tokenizer = None
        self._initialized = False
        self._cache: Dict[str, np.ndarray] = {}
        
    @abstractmethod
    async def initialize(self) -> None:
        """Inicializa el modelo (carga desde HuggingFace)"""
        pass
    
    @abstractmethod
    async def embed(self, text: str) -> np.ndarray:
        """Genera embedding para un texto"""
        pass
    
    @abstractmethod
    async def embed_batch(self, texts: List[str]) -> np.ndarray:
        """Genera embeddings para lote de textos"""
        pass
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    def _get_cache_key(self, text: str) -> str:
        """Genera clave de cache determinística"""
        return hashlib.sha256(f"{self.config.name}:{text}".encode()).hexdigest()[:16]
    
    def _normalize_embedding(self, embedding: np.ndarray) -> np.ndarray:
        """Normaliza embedding L2"""
        norm = np.linalg.norm(embedding)
        if norm > 0:
            return embedding / norm
        return embedding


# ============================================================================
# PROTEIN SEQUENCE EMBEDDERS
# ============================================================================

class ProtT5Embedder(BaseEmbedder):
    """
    ProtT5-XL-U50 Embedder para secuencias de proteínas
    
    Dimensión: 1024
    Especialidad: Patrones evolutivos, dominios funcionales, familias de proteínas
    """
    
    async def initialize(self) -> None:
        """Carga ProtT5 desde HuggingFace"""
        if self._initialized:
            return
            
        try:
            from transformers import T5Tokenizer, T5EncoderModel
            import torch
            
            logger.info(f"🔄 Loading ProtT5 from {self.config.huggingface_id}...")
            
            self._tokenizer = T5Tokenizer.from_pretrained(
                self.config.huggingface_id,
                do_lower_case=False,
                cache_dir=self.config.cache_dir
            )
            
            self._model = T5EncoderModel.from_pretrained(
                self.config.huggingface_id,
                cache_dir=self.config.cache_dir
            )
            
            self._model.to(self.config.device)
            self._model.eval()
            
            self._initialized = True
            logger.info(f"✅ ProtT5 loaded successfully on {self.config.device}")
            
        except Exception as e:
            logger.error(f"❌ Failed to load ProtT5: {e}")
            raise
    
    def _prepare_sequence(self, sequence: str) -> str:
        """Prepara secuencia de proteína para ProtT5 (espacios entre aminoácidos)"""
        # ProtT5 requiere espacios entre cada aminoácido
        return " ".join(list(sequence.upper().replace(" ", "")))
    
    async def embed(self, sequence: str) -> np.ndarray:
        """Genera embedding 1024D para secuencia de proteína"""
        if not self._initialized:
            await self.initialize()
        
        import torch
        
        # Check cache
        cache_key = self._get_cache_key(sequence)
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        prepared_seq = self._prepare_sequence(sequence)
        
        # Tokenize
        inputs = self._tokenizer(
            prepared_seq,
            max_length=self.config.max_length,
            truncation=True,
            padding=True,
            return_tensors="pt"
        ).to(self.config.device)
        
        # Forward pass
        with torch.no_grad():
            outputs = self._model(**inputs)
            
        # Pooling (mean over sequence length)
        hidden_states = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"]
        
        # Masked mean pooling
        mask_expanded = attention_mask.unsqueeze(-1).expand(hidden_states.size()).float()
        sum_embeddings = torch.sum(hidden_states * mask_expanded, 1)
        sum_mask = torch.clamp(mask_expanded.sum(1), min=1e-9)
        embedding = (sum_embeddings / sum_mask).squeeze().cpu().numpy()
        
        # Ensure correct dimension
        if embedding.shape[-1] != self.config.dimension:
            logger.warning(f"ProtT5 output dimension {embedding.shape[-1]} != expected {self.config.dimension}")
        
        # Normalize
        if self.config.normalize:
            embedding = self._normalize_embedding(embedding)
        
        # Cache result
        self._cache[cache_key] = embedding
        
        return embedding
    
    async def embed_batch(self, sequences: List[str]) -> np.ndarray:
        """Genera embeddings para lote de secuencias"""
        embeddings = []
        for seq in sequences:
            emb = await self.embed(seq)
            embeddings.append(emb)
        return np.array(embeddings)


class ESMCEmbedder(BaseEmbedder):
    """
    ESM-C 6B Embedder para secuencias structure-aware
    
    Dimensión: 2560
    Especialidad: Representaciones que capturan estructura 3D implícita
    
    Nota: ESM-C es el modelo más reciente y pesado. Se usa ESM-2 como fallback.
    """
    
    async def initialize(self) -> None:
        """Carga ESM-C o ESM-2 desde HuggingFace"""
        if self._initialized:
            return
            
        try:
            from transformers import AutoTokenizer, AutoModel
            import torch
            
            # Intentar ESM-C primero, fallback a ESM-2 si no está disponible
            model_id = self.config.huggingface_id
            fallback_id = "facebook/esm2_t33_650M_UR50D"  # ESM-2 650M como fallback
            
            try:
                logger.info(f"🔄 Loading ESM-C from {model_id}...")
                self._tokenizer = AutoTokenizer.from_pretrained(
                    model_id, 
                    trust_remote_code=self.config.trust_remote_code,
                    cache_dir=self.config.cache_dir
                )
                self._model = AutoModel.from_pretrained(
                    model_id,
                    trust_remote_code=self.config.trust_remote_code,
                    cache_dir=self.config.cache_dir
                )
            except Exception as e:
                logger.warning(f"⚠️ ESM-C not available ({e}), falling back to ESM-2")
                self._tokenizer = AutoTokenizer.from_pretrained(
                    fallback_id,
                    cache_dir=self.config.cache_dir
                )
                self._model = AutoModel.from_pretrained(
                    fallback_id,
                    cache_dir=self.config.cache_dir
                )
                # Update dimension for fallback model
                self.config.dimension = 1280  # ESM-2 650M dimension
            
            self._model.to(self.config.device)
            self._model.eval()
            
            self._initialized = True
            logger.info(f"✅ ESM model loaded successfully on {self.config.device}")
            
        except Exception as e:
            logger.error(f"❌ Failed to load ESM model: {e}")
            raise
    
    async def embed(self, sequence: str) -> np.ndarray:
        """Genera embedding structure-aware para secuencia de proteína"""
        if not self._initialized:
            await self.initialize()
        
        import torch
        
        # Check cache
        cache_key = self._get_cache_key(sequence)
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # ESM models don't need spaces
        clean_seq = sequence.upper().replace(" ", "")
        
        # Tokenize
        inputs = self._tokenizer(
            clean_seq,
            max_length=self.config.max_length,
            truncation=True,
            padding=True,
            return_tensors="pt"
        ).to(self.config.device)
        
        # Forward pass
        with torch.no_grad():
            outputs = self._model(**inputs)
        
        # Mean pooling (exclude special tokens)
        hidden_states = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"]
        
        mask_expanded = attention_mask.unsqueeze(-1).expand(hidden_states.size()).float()
        sum_embeddings = torch.sum(hidden_states * mask_expanded, 1)
        sum_mask = torch.clamp(mask_expanded.sum(1), min=1e-9)
        embedding = (sum_embeddings / sum_mask).squeeze().cpu().numpy()
        
        # Normalize
        if self.config.normalize:
            embedding = self._normalize_embedding(embedding)
        
        # Cache
        self._cache[cache_key] = embedding
        
        return embedding
    
    async def embed_batch(self, sequences: List[str]) -> np.ndarray:
        """Genera embeddings para lote de secuencias"""
        embeddings = []
        for seq in sequences:
            emb = await self.embed(seq)
            embeddings.append(emb)
        return np.array(embeddings)


# ============================================================================
# TEXT EMBEDDERS (Metadata & Literature)
# ============================================================================

class BioLinkBERTEmbedder(BaseEmbedder):
    """
    BioLinkBERT Embedder para metadatos semánticos
    
    Dimensión: 768
    Especialidad: Nombres de proteínas, funciones, GO terms, descripciones
    """
    
    async def initialize(self) -> None:
        """Carga BioLinkBERT desde HuggingFace"""
        if self._initialized:
            return
            
        try:
            from transformers import AutoTokenizer, AutoModel
            import torch
            
            logger.info(f"🔄 Loading BioLinkBERT from {self.config.huggingface_id}...")
            
            self._tokenizer = AutoTokenizer.from_pretrained(
                self.config.huggingface_id,
                cache_dir=self.config.cache_dir
            )
            
            self._model = AutoModel.from_pretrained(
                self.config.huggingface_id,
                cache_dir=self.config.cache_dir
            )
            
            self._model.to(self.config.device)
            self._model.eval()
            
            self._initialized = True
            logger.info(f"✅ BioLinkBERT loaded successfully on {self.config.device}")
            
        except Exception as e:
            logger.error(f"❌ Failed to load BioLinkBERT: {e}")
            raise
    
    async def embed(self, text: str) -> np.ndarray:
        """Genera embedding 768D para texto biomédico"""
        if not self._initialized:
            await self.initialize()
        
        import torch
        
        # Check cache
        cache_key = self._get_cache_key(text)
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # Tokenize
        inputs = self._tokenizer(
            text,
            max_length=self.config.max_length,
            truncation=True,
            padding=True,
            return_tensors="pt"
        ).to(self.config.device)
        
        # Forward pass
        with torch.no_grad():
            outputs = self._model(**inputs)
        
        # Mean pooling
        hidden_states = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"]
        
        mask_expanded = attention_mask.unsqueeze(-1).expand(hidden_states.size()).float()
        sum_embeddings = torch.sum(hidden_states * mask_expanded, 1)
        sum_mask = torch.clamp(mask_expanded.sum(1), min=1e-9)
        embedding = (sum_embeddings / sum_mask).squeeze().cpu().numpy()
        
        # Normalize
        if self.config.normalize:
            embedding = self._normalize_embedding(embedding)
        
        # Cache
        self._cache[cache_key] = embedding
        
        return embedding
    
    async def embed_batch(self, texts: List[str]) -> np.ndarray:
        """Genera embeddings para lote de textos"""
        if not self._initialized:
            await self.initialize()
        
        import torch
        
        embeddings = []
        
        # Process in batches
        for i in range(0, len(texts), self.config.batch_size):
            batch = texts[i:i + self.config.batch_size]
            
            inputs = self._tokenizer(
                batch,
                max_length=self.config.max_length,
                truncation=True,
                padding=True,
                return_tensors="pt"
            ).to(self.config.device)
            
            with torch.no_grad():
                outputs = self._model(**inputs)
            
            hidden_states = outputs.last_hidden_state
            attention_mask = inputs["attention_mask"]
            
            mask_expanded = attention_mask.unsqueeze(-1).expand(hidden_states.size()).float()
            sum_embeddings = torch.sum(hidden_states * mask_expanded, 1)
            sum_mask = torch.clamp(mask_expanded.sum(1), min=1e-9)
            batch_embeddings = (sum_embeddings / sum_mask).cpu().numpy()
            
            if self.config.normalize:
                norms = np.linalg.norm(batch_embeddings, axis=1, keepdims=True)
                batch_embeddings = batch_embeddings / np.maximum(norms, 1e-9)
            
            embeddings.append(batch_embeddings)
        
        return np.vstack(embeddings)


class SciBERTEmbedder(BaseEmbedder):
    """
    SciBERT Embedder para conocimiento de literatura científica
    
    Dimensión: 768
    Especialidad: Abstracts, papers, conocimiento científico estructurado
    """
    
    async def initialize(self) -> None:
        """Carga SciBERT desde HuggingFace"""
        if self._initialized:
            return
            
        try:
            from transformers import AutoTokenizer, AutoModel
            import torch
            
            logger.info(f"🔄 Loading SciBERT from {self.config.huggingface_id}...")
            
            self._tokenizer = AutoTokenizer.from_pretrained(
                self.config.huggingface_id,
                cache_dir=self.config.cache_dir
            )
            
            self._model = AutoModel.from_pretrained(
                self.config.huggingface_id,
                cache_dir=self.config.cache_dir
            )
            
            self._model.to(self.config.device)
            self._model.eval()
            
            self._initialized = True
            logger.info(f"✅ SciBERT loaded successfully on {self.config.device}")
            
        except Exception as e:
            logger.error(f"❌ Failed to load SciBERT: {e}")
            raise
    
    async def embed(self, text: str) -> np.ndarray:
        """Genera embedding 768D para literatura científica"""
        if not self._initialized:
            await self.initialize()
        
        import torch
        
        cache_key = self._get_cache_key(text)
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        inputs = self._tokenizer(
            text,
            max_length=self.config.max_length,
            truncation=True,
            padding=True,
            return_tensors="pt"
        ).to(self.config.device)
        
        with torch.no_grad():
            outputs = self._model(**inputs)
        
        hidden_states = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"]
        
        mask_expanded = attention_mask.unsqueeze(-1).expand(hidden_states.size()).float()
        sum_embeddings = torch.sum(hidden_states * mask_expanded, 1)
        sum_mask = torch.clamp(mask_expanded.sum(1), min=1e-9)
        embedding = (sum_embeddings / sum_mask).squeeze().cpu().numpy()
        
        if self.config.normalize:
            embedding = self._normalize_embedding(embedding)
        
        self._cache[cache_key] = embedding
        
        return embedding
    
    async def embed_batch(self, texts: List[str]) -> np.ndarray:
        """Genera embeddings para lote de textos"""
        embeddings = []
        for text in texts:
            emb = await self.embed(text)
            embeddings.append(emb)
        return np.array(embeddings)


# ============================================================================
# MULTI-MODEL EMBEDDING ROUTER
# ============================================================================

@dataclass
class MultiModalEmbedding:
    """Resultado de embedding multi-modal con todos los espacios vectoriales"""
    protein_id: str
    
    # Embeddings principales - schema v3 (5 dense protein-model vectors)
    embedding_sequence_space: Optional[np.ndarray] = None       # 1024D ProtT5
    embedding_esm2: Optional[np.ndarray] = None                 # 1280D ESM2-650M
    embedding_sequence_esmc: Optional[np.ndarray] = None        # backward-compat alias (maps to esm2)
    embedding_metadata_semantic: Optional[np.ndarray] = None    # 768D BioLinkBERT (deprecated — not in v3 schema)
    embedding_paper_knowledge: Optional[np.ndarray] = None      # 768D SciBERT (deprecated — not in v3 schema)
    embedding_network_space: Optional[np.ndarray] = None        # 512D node2vec
    embedding_structure_af2: Optional[np.ndarray] = None        # 384D DPEB AF2
    embedding_dct_domain: Optional[np.ndarray] = None           # 480D DCT domain fingerprint
    
    # Metadatos
    sequence: Optional[str] = None
    name: Optional[str] = None
    function: Optional[str] = None
    organism: Optional[str] = None
    paper_abstract: Optional[str] = None
    
    # Información de procesamiento
    processing_time_ms: float = 0.0
    models_used: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para inserción en Milvus"""
        result = {
            "protein_id": self.protein_id,
            "sequence": self.sequence or "",
            "name": self.name or "",
            "function": self.function or "",
            "organism": self.organism or "",
            "paper_abstract": self.paper_abstract or "",
            "processing_time_ms": self.processing_time_ms,
            "models_used": self.models_used,
        }
        
        if self.embedding_sequence_space is not None:
            result["embedding_sequence_space"] = self.embedding_sequence_space.tolist()
        # Canonical ESM2 field; backward-compat: also write under old key if set
        esm2_vec = self.embedding_esm2 if self.embedding_esm2 is not None else self.embedding_sequence_esmc
        if esm2_vec is not None:
            result["embedding_esm2"] = esm2_vec.tolist()
        if self.embedding_metadata_semantic is not None:
            result["embedding_metadata_semantic"] = self.embedding_metadata_semantic.tolist()
        if self.embedding_paper_knowledge is not None:
            result["embedding_paper_knowledge"] = self.embedding_paper_knowledge.tolist()
        if self.embedding_network_space is not None:
            result["embedding_network_space"] = self.embedding_network_space.tolist()
        if self.embedding_structure_af2 is not None:
            result["embedding_structure_af2"] = self.embedding_structure_af2.tolist()
        if self.embedding_dct_domain is not None:
            result["embedding_dct_domain"] = self.embedding_dct_domain.tolist()
        
        return result


class MultiModelEmbeddingRouter:
    """
    Router inteligente para generación de embeddings multi-modelo.
    
    Orquesta los 5 modelos de embedding para generar representaciones completas:
    1. ProtT5 (1024D) - Secuencia evolutiva
    2. ESM-C (2560D) - Secuencia structure-aware
    3. BioLinkBERT (768D) - Metadatos semánticos
    4. SciBERT (768D) - Conocimiento de literatura
    5. node2vec (512D) - Red biológica (pre-entrenado)
    """
    
    def __init__(self, config: Optional[MultiModelRouterConfig] = None):
        self.config = config or MultiModelRouterConfig()
        
        # Inicializar embedders
        self._prott5: Optional[ProtT5Embedder] = None
        self._esmc: Optional[ESMCEmbedder] = None
        self._biolinkbert: Optional[BioLinkBERTEmbedder] = None
        self._scibert: Optional[SciBERTEmbedder] = None
        
        # node2vec se carga desde archivos pre-entrenados
        self._node2vec_embeddings: Dict[str, np.ndarray] = {}
        
        self._initialized = False
        logger.info("🧬 MultiModelEmbeddingRouter initialized")
    
    async def initialize(self, 
                         load_sequence_models: bool = True,
                         load_text_models: bool = True) -> None:
        """
        Inicializa modelos seleccionados.
        
        Args:
            load_sequence_models: Cargar ProtT5 y ESM-C
            load_text_models: Cargar BioLinkBERT y SciBERT
        """
        if self._initialized:
            return
        
        initialization_tasks = []
        
        if load_sequence_models:
            self._prott5 = ProtT5Embedder(self.config.prott5)
            self._esmc = ESMCEmbedder(self.config.esmc)
            initialization_tasks.append(self._prott5.initialize())
            initialization_tasks.append(self._esmc.initialize())
        
        if load_text_models:
            self._biolinkbert = BioLinkBERTEmbedder(self.config.biolinkbert)
            self._scibert = SciBERTEmbedder(self.config.scibert)
            initialization_tasks.append(self._biolinkbert.initialize())
            initialization_tasks.append(self._scibert.initialize())
        
        # Inicializar en paralelo
        if initialization_tasks:
            await asyncio.gather(*initialization_tasks, return_exceptions=True)
        
        # Crear directorio de cache
        if self.config.cache_enabled:
            Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)
        
        self._initialized = True
        logger.info("✅ MultiModelEmbeddingRouter fully initialized")
    
    async def embed_protein(
        self,
        protein_id: str,
        sequence: Optional[str] = None,
        name: Optional[str] = None,
        function: Optional[str] = None,
        organism: Optional[str] = None,
        paper_abstract: Optional[str] = None,
        spaces: Optional[List[EmbeddingSpace]] = None
    ) -> MultiModalEmbedding:
        """
        Genera embeddings multi-modelo para una proteína.
        
        Args:
            protein_id: Identificador único
            sequence: Secuencia de aminoácidos
            name: Nombre de la proteína
            function: Descripción funcional
            organism: Organismo de origen
            paper_abstract: Abstract de paper relacionado
            spaces: Lista de espacios a generar (None = todos disponibles)
            
        Returns:
            MultiModalEmbedding con todos los vectores generados
        """
        import time
        start_time = time.time()
        
        result = MultiModalEmbedding(
            protein_id=protein_id,
            sequence=sequence,
            name=name,
            function=function,
            organism=organism,
            paper_abstract=paper_abstract
        )
        
        # Determinar qué espacios generar
        target_spaces = spaces or list(EmbeddingSpace)
        
        # Generar embeddings según los datos disponibles
        tasks = []
        
        # 1. Secuencia → ProtT5 (1024D)
        if sequence and EmbeddingSpace.SEQUENCE_PROTT5 in target_spaces:
            if self._prott5 and self._prott5.is_initialized:
                tasks.append(("prott5", self._prott5.embed(sequence)))
        
        # 2. Secuencia → ESM-C (2560D)
        if sequence and EmbeddingSpace.SEQUENCE_ESMC in target_spaces:
            if self._esmc and self._esmc.is_initialized:
                tasks.append(("esmc", self._esmc.embed(sequence)))
        
        # 3. Metadatos → BioLinkBERT (768D)
        if EmbeddingSpace.METADATA_SEMANTIC in target_spaces:
            metadata_text = self._build_metadata_text(name, function, organism)
            if metadata_text and self._biolinkbert and self._biolinkbert.is_initialized:
                tasks.append(("biolinkbert", self._biolinkbert.embed(metadata_text)))
        
        # 4. Literatura → SciBERT (768D)
        if paper_abstract and EmbeddingSpace.PAPER_KNOWLEDGE in target_spaces:
            if self._scibert and self._scibert.is_initialized:
                tasks.append(("scibert", self._scibert.embed(paper_abstract)))
        
        # 5. Network → node2vec (512D) - lookup desde pre-entrenado
        if EmbeddingSpace.NETWORK_SPACE in target_spaces:
            if protein_id in self._node2vec_embeddings:
                result.embedding_network_space = self._node2vec_embeddings[protein_id]
                result.models_used.append("node2vec")
        
        # Ejecutar tasks en paralelo
        if tasks:
            task_results = await asyncio.gather(
                *[t[1] for t in tasks],
                return_exceptions=True
            )
            
            for (task_name, _), task_result in zip(tasks, task_results):
                if isinstance(task_result, Exception):
                    result.errors.append(f"{task_name}: {str(task_result)}")
                    logger.warning(f"⚠️ {task_name} embedding failed: {task_result}")
                else:
                    if task_name == "prott5":
                        result.embedding_sequence_space = task_result
                    elif task_name == "esmc":
                        result.embedding_sequence_esmc = task_result
                    elif task_name == "biolinkbert":
                        result.embedding_metadata_semantic = task_result
                    elif task_name == "scibert":
                        result.embedding_paper_knowledge = task_result
                    
                    result.models_used.append(task_name)
        
        result.processing_time_ms = (time.time() - start_time) * 1000
        
        logger.debug(
            f"🧬 Embedded {protein_id}: {len(result.models_used)} models, "
            f"{result.processing_time_ms:.1f}ms"
        )
        
        return result
    
    async def embed_batch(
        self,
        proteins: List[Dict[str, Any]],
        spaces: Optional[List[EmbeddingSpace]] = None
    ) -> List[MultiModalEmbedding]:
        """
        Genera embeddings para lote de proteínas.
        
        Args:
            proteins: Lista de dicts con protein_id, sequence, name, function, organism, paper_abstract
            spaces: Espacios a generar
            
        Returns:
            Lista de MultiModalEmbedding
        """
        results = []
        for protein in proteins:
            result = await self.embed_protein(
                protein_id=protein.get("protein_id", "unknown"),
                sequence=protein.get("sequence"),
                name=protein.get("name"),
                function=protein.get("function"),
                organism=protein.get("organism"),
                paper_abstract=protein.get("paper_abstract"),
                spaces=spaces
            )
            results.append(result)
        return results
    
    def _build_metadata_text(
        self,
        name: Optional[str],
        function: Optional[str],
        organism: Optional[str]
    ) -> str:
        """Construye texto de metadatos para embedding"""
        parts = []
        if name:
            parts.append(f"Protein: {name}")
        if function:
            parts.append(f"Function: {function}")
        if organism:
            parts.append(f"Organism: {organism}")
        return ". ".join(parts)
    
    def load_node2vec_embeddings(self, embeddings_path: str) -> int:
        """
        Carga embeddings node2vec pre-entrenados desde archivo.
        
        Args:
            embeddings_path: Ruta a archivo .npy o .pkl con embeddings
            
        Returns:
            Número de embeddings cargados
        """
        try:
            if embeddings_path.endswith(".npy"):
                data = np.load(embeddings_path, allow_pickle=True).item()
            elif embeddings_path.endswith(".pkl"):
                import pickle
                with open(embeddings_path, "rb") as f:
                    data = pickle.load(f)
            else:
                raise ValueError(f"Unsupported format: {embeddings_path}")
            
            self._node2vec_embeddings = data
            logger.info(f"✅ Loaded {len(data)} node2vec embeddings from {embeddings_path}")
            return len(data)
            
        except Exception as e:
            logger.error(f"❌ Failed to load node2vec embeddings: {e}")
            return 0
    
    async def get_embedding_for_space(
        self,
        space: EmbeddingSpace,
        text: str
    ) -> np.ndarray:
        """
        Genera embedding específico para un espacio.
        
        Args:
            space: Espacio de embedding deseado
            text: Texto a embeder (secuencia o texto)
            
        Returns:
            Embedding numpy array
        """
        if space == EmbeddingSpace.SEQUENCE_PROTT5:
            if not self._prott5:
                raise ValueError("ProtT5 not initialized")
            return await self._prott5.embed(text)
        
        elif space == EmbeddingSpace.SEQUENCE_ESMC:
            if not self._esmc:
                raise ValueError("ESM-C not initialized")
            return await self._esmc.embed(text)
        
        elif space == EmbeddingSpace.METADATA_SEMANTIC:
            if not self._biolinkbert:
                raise ValueError("BioLinkBERT not initialized")
            return await self._biolinkbert.embed(text)
        
        elif space == EmbeddingSpace.PAPER_KNOWLEDGE:
            if not self._scibert:
                raise ValueError("SciBERT not initialized")
            return await self._scibert.embed(text)
        
        elif space == EmbeddingSpace.NETWORK_SPACE:
            raise ValueError("node2vec requires pre-trained embeddings lookup")
        
        else:
            raise ValueError(f"Unknown embedding space: {space}")
    
    @property
    def available_models(self) -> Dict[str, bool]:
        """Retorna estado de modelos disponibles"""
        return {
            "prott5": self._prott5 is not None and self._prott5.is_initialized,
            "esmc": self._esmc is not None and self._esmc.is_initialized,
            "biolinkbert": self._biolinkbert is not None and self._biolinkbert.is_initialized,
            "scibert": self._scibert is not None and self._scibert.is_initialized,
            "node2vec": len(self._node2vec_embeddings) > 0
        }
    
    def get_dimensions(self) -> Dict[str, int]:
        """Retorna dimensiones de cada espacio de embedding"""
        return {
            EmbeddingSpace.SEQUENCE_PROTT5.value: self.config.prott5.dimension,
            EmbeddingSpace.SEQUENCE_ESMC.value: self.config.esmc.dimension,
            EmbeddingSpace.METADATA_SEMANTIC.value: self.config.biolinkbert.dimension,
            EmbeddingSpace.PAPER_KNOWLEDGE.value: self.config.scibert.dimension,
            EmbeddingSpace.NETWORK_SPACE.value: self.config.node2vec_dimension
        }


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================

async def create_embedding_router(
    config: Optional[MultiModelRouterConfig] = None,
    load_all_models: bool = False
) -> MultiModelEmbeddingRouter:
    """
    Factory para crear router de embeddings.
    
    Args:
        config: Configuración del router
        load_all_models: Si True, carga todos los modelos al inicializar
        
    Returns:
        MultiModelEmbeddingRouter inicializado
    """
    router = MultiModelEmbeddingRouter(config)
    
    if load_all_models:
        await router.initialize(
            load_sequence_models=True,
            load_text_models=True
        )
    
    return router


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "EmbeddingSpace",
    "EmbeddingModelConfig",
    "MultiModelRouterConfig",
    "BaseEmbedder",
    "ProtT5Embedder",
    "ESMCEmbedder",
    "BioLinkBERTEmbedder",
    "SciBERTEmbedder",
    "MultiModalEmbedding",
    "MultiModelEmbeddingRouter",
    "create_embedding_router",
]

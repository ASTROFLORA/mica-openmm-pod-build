"""
PubMedBERT Fusion Engine - Dr. Yuan Chen
=======================================

Multi-modal fusion engine combining ESM-C, Evoformer, and ESE embeddings
with PubMedBERT for comprehensive protein understanding.

Phase 4 Implementation: PubMedBERT Integration (4 weeks)
Lead: Dr. Yuan Chen + Alex Rodriguez
"""

import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass
import numpy as np
import json
import time
from pathlib import Path
import pickle

import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.metrics.pairwise import cosine_similarity

# Transformers for PubMedBERT
try:
    from transformers import AutoTokenizer, AutoModel
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    logger.warning("Transformers not available - mock PubMedBERT will be used")

from bsm.config import get_bsm_config

logger = logging.getLogger(__name__)


@dataclass
class FusionConfig:
    """Configuration for multi-modal fusion"""
    esmc_dimension: int = 2560  # ESM-C embedding dimension
    evoformer_dimension: int = 512  # Evoformer embedding dimension
    ese_dimension: int = 416  # ESE embedding dimension
    pubmedbert_dimension: int = 768  # PubMedBERT embedding dimension
    
    # Fusion architecture
    fusion_hidden_dim: int = 1024  # Hidden layer dimension
    final_embedding_dim: int = 1280  # Final fused embedding (768D + 512D)
    num_attention_heads: int = 8  # Multi-head attention heads
    num_fusion_layers: int = 4  # Number of fusion transformer layers
    dropout_rate: float = 0.1  # Dropout probability
    
    # PubMedBERT settings
    pubmedbert_model: str = "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext"
    max_sequence_length: int = 512  # Maximum text sequence length
    
    device: str = "auto"  # Computing device
    cache_embeddings: bool = True  # Cache PubMedBERT embeddings


@dataclass
class ProteinContext:
    """Protein contextual information for PubMedBERT"""
    protein_name: str
    function_description: Optional[str] = None
    organism: Optional[str] = None
    cellular_location: Optional[str] = None
    pathway_involvement: Optional[List[str]] = None
    disease_associations: Optional[List[str]] = None
    literature_abstracts: Optional[List[str]] = None
    additional_context: Optional[Dict[str, Any]] = None


@dataclass
class MultiModalInput:
    """Input for multi-modal fusion"""
    sequence_id: str
    sequence: str
    
    # Embeddings from different modalities
    esmc_embedding: Optional[np.ndarray] = None
    evoformer_embedding: Optional[np.ndarray] = None
    ese_embedding: Optional[np.ndarray] = None
    
    # Contextual information
    protein_context: Optional[ProteinContext] = None
    
    # Quality indicators
    embedding_quality: Dict[str, float] = None


@dataclass
class FusionOutput:
    """Output from multi-modal fusion"""
    sequence_id: str
    
    # Final fused embeddings
    fused_embedding: np.ndarray  # 1280D (768D + 512D)
    structural_embedding: np.ndarray  # 512D structural component
    contextual_embedding: np.ndarray  # 768D contextual component
    
    # Individual modality embeddings (normalized)
    esmc_embedding: np.ndarray  # 2560D → normalized
    evoformer_embedding: np.ndarray  # 512D → normalized
    ese_embedding: np.ndarray  # 416D → normalized
    pubmedbert_embedding: np.ndarray  # 768D → normalized
    
    # Attention weights and fusion analysis
    attention_weights: Dict[str, np.ndarray]
    modality_importance: Dict[str, float]
    fusion_quality: Dict[str, float]
    
    # Processing metadata
    processing_time: float = 0.0
    confidence_score: float = 1.0
    fusion_metadata: Dict[str, Any] = None


class CrossModalAttention(nn.Module):
    """Cross-modal attention mechanism for embedding fusion"""
    
    def __init__(self, config: FusionConfig):
        super().__init__()
        self.config = config
        self.num_heads = config.num_attention_heads
        self.head_dim = config.fusion_hidden_dim // config.num_attention_heads
        
        # Query, Key, Value projections for each modality
        self.esmc_qkv = nn.Linear(config.esmc_dimension, 3 * config.fusion_hidden_dim)
        self.evoformer_qkv = nn.Linear(config.evoformer_dimension, 3 * config.fusion_hidden_dim)
        self.ese_qkv = nn.Linear(config.ese_dimension, 3 * config.fusion_hidden_dim)
        self.pubmedbert_qkv = nn.Linear(config.pubmedbert_dimension, 3 * config.fusion_hidden_dim)
        
        # Output projection
        self.output_proj = nn.Linear(config.fusion_hidden_dim, config.fusion_hidden_dim)
        
        # Layer normalization
        self.layer_norm = nn.LayerNorm(config.fusion_hidden_dim)
        
        # Dropout
        self.dropout = nn.Dropout(config.dropout_rate)
        
        self.scale = self.head_dim ** -0.5
    
    def forward(
        self, 
        esmc_emb: torch.Tensor,
        evoformer_emb: torch.Tensor,
        ese_emb: torch.Tensor,
        pubmedbert_emb: torch.Tensor,
        mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """
        Forward pass of cross-modal attention
        
        Args:
            esmc_emb: [batch, esmc_dim]
            evoformer_emb: [batch, evoformer_dim]
            ese_emb: [batch, ese_dim]
            pubmedbert_emb: [batch, pubmedbert_dim]
            mask: Optional attention mask
            
        Returns:
            Fused representation and attention weights
        """
        
        batch_size = esmc_emb.size(0)
        
        # Project each modality to Q, K, V
        esmc_qkv = self.esmc_qkv(esmc_emb)  # [batch, 3 * hidden_dim]
        evoformer_qkv = self.evoformer_qkv(evoformer_emb)
        ese_qkv = self.ese_qkv(ese_emb)
        pubmedbert_qkv = self.pubmedbert_qkv(pubmedbert_emb)
        
        # Split Q, K, V and reshape for multi-head attention
        def split_qkv(qkv_tensor):
            q, k, v = torch.chunk(qkv_tensor, 3, dim=-1)
            q = q.view(batch_size, self.num_heads, self.head_dim)
            k = k.view(batch_size, self.num_heads, self.head_dim)
            v = v.view(batch_size, self.num_heads, self.head_dim)
            return q, k, v
        
        esmc_q, esmc_k, esmc_v = split_qkv(esmc_qkv)
        evo_q, evo_k, evo_v = split_qkv(evoformer_qkv)
        ese_q, ese_k, ese_v = split_qkv(ese_qkv)
        bert_q, bert_k, bert_v = split_qkv(pubmedbert_qkv)
        
        # Stack all modalities
        all_q = torch.stack([esmc_q, evo_q, ese_q, bert_q], dim=1)  # [batch, 4, num_heads, head_dim]
        all_k = torch.stack([esmc_k, evo_k, ese_k, bert_k], dim=1)
        all_v = torch.stack([esmc_v, evo_v, ese_v, bert_v], dim=1)
        
        # Compute attention scores
        scores = torch.matmul(all_q, all_k.transpose(-2, -1)) * self.scale  # [batch, 4, num_heads, 4]
        
        # Apply mask if provided
        if mask is not None:
            scores.masked_fill_(mask == 0, float('-inf'))
        
        # Softmax to get attention weights
        attn_weights = F.softmax(scores, dim=-1)  # [batch, 4, num_heads, 4]
        attn_weights = self.dropout(attn_weights)
        
        # Apply attention to values
        attended = torch.matmul(attn_weights, all_v)  # [batch, 4, num_heads, head_dim]
        
        # Sum across modalities (cross-modal fusion)
        fused = torch.sum(attended, dim=1)  # [batch, num_heads, head_dim]
        
        # Reshape and project
        fused = fused.view(batch_size, -1)  # [batch, num_heads * head_dim]
        output = self.output_proj(fused)
        
        # Residual connection and layer norm
        # Note: For simplicity, using output as residual (in practice would use mean of inputs)
        output = self.layer_norm(output + fused)
        
        # Extract attention weights for analysis
        attention_dict = {
            'esmc_to_all': attn_weights[:, 0].mean(dim=1),  # Average across heads
            'evoformer_to_all': attn_weights[:, 1].mean(dim=1),
            'ese_to_all': attn_weights[:, 2].mean(dim=1),
            'pubmedbert_to_all': attn_weights[:, 3].mean(dim=1),
            'cross_modal_matrix': attn_weights.mean(dim=2)  # [batch, 4, 4]
        }
        
        return output, attention_dict


class FusionTransformer(nn.Module):
    """Multi-layer transformer for embedding fusion"""
    
    def __init__(self, config: FusionConfig):
        super().__init__()
        self.config = config
        
        # Input projections for each modality
        self.esmc_proj = nn.Linear(config.esmc_dimension, config.fusion_hidden_dim)
        self.evoformer_proj = nn.Linear(config.evoformer_dimension, config.fusion_hidden_dim)
        self.ese_proj = nn.Linear(config.ese_dimension, config.fusion_hidden_dim)
        self.pubmedbert_proj = nn.Linear(config.pubmedbert_dimension, config.fusion_hidden_dim)
        
        # Cross-modal attention layers
        self.attention_layers = nn.ModuleList([
            CrossModalAttention(config) for _ in range(config.num_fusion_layers)
        ])
        
        # Final projections to output dimensions
        self.structural_proj = nn.Sequential(
            nn.Linear(config.fusion_hidden_dim, config.fusion_hidden_dim // 2),
            nn.ReLU(),
            nn.Dropout(config.dropout_rate),
            nn.Linear(config.fusion_hidden_dim // 2, 512)  # 512D structural
        )
        
        self.contextual_proj = nn.Sequential(
            nn.Linear(config.fusion_hidden_dim, config.fusion_hidden_dim),
            nn.ReLU(),
            nn.Dropout(config.dropout_rate),
            nn.Linear(config.fusion_hidden_dim, 768)  # 768D contextual
        )
        
        # Modality importance predictor
        self.importance_predictor = nn.Sequential(
            nn.Linear(config.fusion_hidden_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 4),  # 4 modalities
            nn.Softmax(dim=-1)
        )
    
    def forward(
        self,
        esmc_emb: torch.Tensor,
        evoformer_emb: torch.Tensor,
        ese_emb: torch.Tensor,
        pubmedbert_emb: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, Dict[str, Any]]:
        """
        Forward pass through fusion transformer
        
        Returns:
            structural_embedding, contextual_embedding, analysis_dict
        """
        
        # Project each modality to common dimension
        esmc_proj = self.esmc_proj(esmc_emb)
        evo_proj = self.evoformer_proj(evoformer_emb)
        ese_proj = self.ese_proj(ese_emb)
        bert_proj = self.pubmedbert_proj(pubmedbert_emb)
        
        # Apply cross-modal attention layers
        current_esmc = esmc_proj
        current_evo = evo_proj
        current_ese = ese_proj
        current_bert = bert_proj
        
        all_attention_weights = []
        
        for attention_layer in self.attention_layers:
            fused_repr, attn_weights = attention_layer(
                current_esmc, current_evo, current_ese, current_bert
            )
            
            # Update representations (residual connections)
            current_esmc = current_esmc + fused_repr
            current_evo = current_evo + fused_repr  
            current_ese = current_ese + fused_repr
            current_bert = current_bert + fused_repr
            
            all_attention_weights.append(attn_weights)
        
        # Final fused representation
        final_fused = (current_esmc + current_evo + current_ese + current_bert) / 4
        
        # Generate structural and contextual embeddings
        structural_emb = self.structural_proj(final_fused)
        contextual_emb = self.contextual_proj(final_fused)
        
        # Predict modality importance
        modality_importance = self.importance_predictor(final_fused)
        
        # Analysis dictionary
        analysis = {
            'attention_weights': all_attention_weights,
            'modality_importance': modality_importance,
            'fusion_quality': {
                'representation_norm': torch.norm(final_fused, dim=-1),
                'structural_norm': torch.norm(structural_emb, dim=-1),
                'contextual_norm': torch.norm(contextual_emb, dim=-1)
            }
        }
        
        return structural_emb, contextual_emb, analysis


class PubMedBERTProcessor:
    """Processor for PubMedBERT embeddings from protein context"""
    
    def __init__(self, config: FusionConfig):
        self.config = config
        
        if TRANSFORMERS_AVAILABLE:
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(config.pubmedbert_model)
                self.model = AutoModel.from_pretrained(config.pubmedbert_model)
                self.model.eval()
                
                # Move to device
                device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                self.model.to(device)
                
                self.real_pubmedbert = True
                logger.info("Real PubMedBERT loaded successfully")
                
            except Exception as e:
                logger.warning(f"Failed to load PubMedBERT: {e}")
                self.real_pubmedbert = False
        else:
            self.real_pubmedbert = False
        
        # Cache for embeddings
        self.embedding_cache = {}
    
    def process_protein_context(self, protein_context: ProteinContext) -> np.ndarray:
        """
        Process protein context to generate PubMedBERT embeddings
        
        Args:
            protein_context: ProteinContext with textual information
            
        Returns:
            768D PubMedBERT embedding
        """
        
        # Create text representation
        context_text = self._create_context_text(protein_context)
        
        # Check cache
        cache_key = hash(context_text)
        if cache_key in self.embedding_cache:
            return self.embedding_cache[cache_key]
        
        # Generate embedding
        if self.real_pubmedbert:
            embedding = self._get_real_pubmedbert_embedding(context_text)
        else:
            embedding = self._get_mock_pubmedbert_embedding(context_text, protein_context)
        
        # Cache result
        if self.config.cache_embeddings:
            self.embedding_cache[cache_key] = embedding
        
        return embedding
    
    def _create_context_text(self, protein_context: ProteinContext) -> str:
        """Create structured text from protein context"""
        
        text_parts = []
        
        # Protein name
        if protein_context.protein_name:
            text_parts.append(f"Protein: {protein_context.protein_name}")
        
        # Function description
        if protein_context.function_description:
            text_parts.append(f"Function: {protein_context.function_description}")
        
        # Organism
        if protein_context.organism:
            text_parts.append(f"Organism: {protein_context.organism}")
        
        # Cellular location
        if protein_context.cellular_location:
            text_parts.append(f"Location: {protein_context.cellular_location}")
        
        # Pathway involvement
        if protein_context.pathway_involvement:
            pathways = ", ".join(protein_context.pathway_involvement)
            text_parts.append(f"Pathways: {pathways}")
        
        # Disease associations
        if protein_context.disease_associations:
            diseases = ", ".join(protein_context.disease_associations)
            text_parts.append(f"Diseases: {diseases}")
        
        # Literature abstracts
        if protein_context.literature_abstracts:
            abstracts = " ".join(protein_context.literature_abstracts[:3])  # Limit to 3 abstracts
            text_parts.append(f"Literature: {abstracts}")
        
        # Combine all parts
        context_text = " ".join(text_parts)
        
        # Truncate to maximum length
        if len(context_text) > self.config.max_sequence_length * 4:  # Rough token estimate
            context_text = context_text[:self.config.max_sequence_length * 4]
        
        return context_text
    
    def _get_real_pubmedbert_embedding(self, context_text: str) -> np.ndarray:
        """Get real PubMedBERT embedding"""
        
        # Tokenize
        inputs = self.tokenizer(
            context_text,
            max_length=self.config.max_sequence_length,
            padding=True,
            truncation=True,
            return_tensors="pt"
        )
        
        # Move to device
        device = next(self.model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        
        # Generate embedding
        with torch.no_grad():
            outputs = self.model(**inputs)
            
            # Use [CLS] token embedding
            cls_embedding = outputs.last_hidden_state[:, 0, :]  # [batch, 768]
            
            # Pool all tokens (alternative)
            # pooled_embedding = torch.mean(outputs.last_hidden_state, dim=1)
        
        return cls_embedding.cpu().numpy().flatten()
    
    def _get_mock_pubmedbert_embedding(
        self, 
        context_text: str, 
        protein_context: ProteinContext
    ) -> np.ndarray:
        """Generate mock PubMedBERT embedding"""
        
        # Use context information to create realistic embedding
        np.random.seed(hash(context_text) % (2**32))
        
        # Base embedding
        embedding = np.random.normal(0, 1, 768)
        
        # Modify based on protein characteristics
        if protein_context.function_description:
            # Add function-related patterns
            function_hash = hash(protein_context.function_description) % (2**32)
            np.random.seed(function_hash)
            function_pattern = np.random.normal(0, 0.5, 768)
            embedding += 0.3 * function_pattern
        
        if protein_context.organism:
            # Add organism-specific patterns
            organism_hash = hash(protein_context.organism) % (2**32)
            np.random.seed(organism_hash)
            organism_pattern = np.random.normal(0, 0.3, 768)
            embedding += 0.2 * organism_pattern
        
        if protein_context.disease_associations:
            # Add disease-related patterns
            disease_text = " ".join(protein_context.disease_associations)
            disease_hash = hash(disease_text) % (2**32)
            np.random.seed(disease_hash)
            disease_pattern = np.random.normal(0, 0.4, 768)
            embedding += 0.4 * disease_pattern
        
        # Normalize
        embedding = embedding / np.linalg.norm(embedding)
        
        return embedding


class PubMedBERTFusionEngine:
    """
    Main fusion engine for combining ESM-C, Evoformer, ESE, and PubMedBERT embeddings.
    
    Generates 1280D fused embeddings (768D contextual + 512D structural).
    """
    
    def __init__(self, config: Optional[FusionConfig] = None):
        self.config = config or FusionConfig()
        self.bsm_config = get_bsm_config()
        
        # Setup device
        if self.config.device == "auto":
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(self.config.device)
        
        # Initialize components
        self.pubmedbert_processor = PubMedBERTProcessor(self.config)
        self.fusion_transformer = FusionTransformer(self.config)
        
        # Move fusion model to device
        self.fusion_transformer.to(self.device)
        
        # Embedding normalizers
        self.esmc_scaler = StandardScaler()
        self.evoformer_scaler = StandardScaler()
        self.ese_scaler = StandardScaler()
        self.pubmedbert_scaler = StandardScaler()
        
        # Processing cache and statistics
        self.fusion_cache = {}
        self.processing_stats = {
            'fusions_processed': 0,
            'total_processing_time': 0.0,
            'average_processing_time': 0.0,
            'cache_hits': 0
        }
        
        logger.info("PubMedBERT Fusion Engine initialized - Dr. Yuan Chen implementation")
    
    async def fuse_embeddings(
        self,
        multi_modal_input: MultiModalInput
    ) -> FusionOutput:
        """
        Fuse multi-modal embeddings with PubMedBERT context.
        
        Args:
            multi_modal_input: MultiModalInput with embeddings and context
            
        Returns:
            FusionOutput with fused embeddings and analysis
        """
        
        start_time = time.time()
        
        # Check cache
        cache_key = self._generate_cache_key(multi_modal_input)
        if cache_key in self.fusion_cache:
            logger.debug(f"Using cached fusion result for {multi_modal_input.sequence_id}")
            self.processing_stats['cache_hits'] += 1
            return self.fusion_cache[cache_key]
        
        logger.info(f"Fusing embeddings for {multi_modal_input.sequence_id}")
        
        # Validate and prepare embeddings
        embeddings = self._prepare_embeddings(multi_modal_input)
        
        # Process PubMedBERT context
        if multi_modal_input.protein_context:
            pubmedbert_emb = self.pubmedbert_processor.process_protein_context(
                multi_modal_input.protein_context
            )
        else:
            # Use sequence-based context
            pubmedbert_emb = self._generate_sequence_context_embedding(
                multi_modal_input.sequence
            )
        
        embeddings['pubmedbert'] = pubmedbert_emb
        
        # Convert to tensors
        embeddings_tensor = self._prepare_tensors(embeddings)
        
        # Perform fusion
        with torch.no_grad():
            structural_emb, contextual_emb, fusion_analysis = self.fusion_transformer(
                embeddings_tensor['esmc'],
                embeddings_tensor['evoformer'],
                embeddings_tensor['ese'],
                embeddings_tensor['pubmedbert']
            )
        
        # Combine into final embedding
        fused_embedding = torch.cat([contextual_emb, structural_emb], dim=-1)  # 768 + 512 = 1280
        
        # Convert back to numpy
        fused_np = fused_embedding.cpu().numpy().flatten()
        structural_np = structural_emb.cpu().numpy().flatten()
        contextual_np = contextual_emb.cpu().numpy().flatten()
        
        # Process fusion analysis
        attention_weights = self._process_attention_weights(fusion_analysis['attention_weights'])
        modality_importance = self._process_modality_importance(fusion_analysis['modality_importance'])
        fusion_quality = self._calculate_fusion_quality(fusion_analysis, embeddings)
        
        # Calculate confidence score
        confidence_score = self._calculate_confidence_score(
            embeddings, fusion_analysis, multi_modal_input.embedding_quality
        )
        
        # Create output
        output = FusionOutput(
            sequence_id=multi_modal_input.sequence_id,
            fused_embedding=fused_np,
            structural_embedding=structural_np,
            contextual_embedding=contextual_np,
            esmc_embedding=embeddings['esmc'],
            evoformer_embedding=embeddings['evoformer'],
            ese_embedding=embeddings['ese'],
            pubmedbert_embedding=embeddings['pubmedbert'],
            attention_weights=attention_weights,
            modality_importance=modality_importance,
            fusion_quality=fusion_quality,
            processing_time=time.time() - start_time,
            confidence_score=confidence_score,
            fusion_metadata={
                'sequence_length': len(multi_modal_input.sequence),
                'has_context': multi_modal_input.protein_context is not None,
                'modalities_used': list(embeddings.keys()),
                'fusion_config': self.config.__dict__
            }
        )
        
        # Cache and update statistics
        self.fusion_cache[cache_key] = output
        self._update_processing_stats(output)
        
        logger.info(f"Fusion completed for {multi_modal_input.sequence_id} in {output.processing_time:.2f}s")
        return output
    
    def _generate_cache_key(self, multi_modal_input: MultiModalInput) -> str:
        """Generate cache key for fusion input"""
        
        key_components = [multi_modal_input.sequence_id]
        
        # Add embedding hashes
        if multi_modal_input.esmc_embedding is not None:
            key_components.append(f"esmc:{hash(multi_modal_input.esmc_embedding.tobytes())}")
        
        if multi_modal_input.evoformer_embedding is not None:
            key_components.append(f"evo:{hash(multi_modal_input.evoformer_embedding.tobytes())}")
        
        if multi_modal_input.ese_embedding is not None:
            key_components.append(f"ese:{hash(multi_modal_input.ese_embedding.tobytes())}")
        
        # Add context hash
        if multi_modal_input.protein_context:
            context_str = str(multi_modal_input.protein_context.__dict__)
            key_components.append(f"ctx:{hash(context_str)}")
        
        return "|".join(key_components)
    
    def _prepare_embeddings(self, multi_modal_input: MultiModalInput) -> Dict[str, np.ndarray]:
        """Prepare and validate input embeddings"""
        
        embeddings = {}
        
        # ESM-C embedding
        if multi_modal_input.esmc_embedding is not None:
            esmc_emb = multi_modal_input.esmc_embedding
            if len(esmc_emb) != self.config.esmc_dimension:
                logger.warning(f"ESM-C embedding dimension mismatch: {len(esmc_emb)} vs {self.config.esmc_dimension}")
                # Pad or truncate
                esmc_emb = self._resize_embedding(esmc_emb, self.config.esmc_dimension)
            embeddings['esmc'] = esmc_emb
        else:
            embeddings['esmc'] = np.zeros(self.config.esmc_dimension)
            logger.warning("No ESM-C embedding provided, using zeros")
        
        # Evoformer embedding
        if multi_modal_input.evoformer_embedding is not None:
            evo_emb = multi_modal_input.evoformer_embedding
            if len(evo_emb) != self.config.evoformer_dimension:
                logger.warning(f"Evoformer embedding dimension mismatch: {len(evo_emb)} vs {self.config.evoformer_dimension}")
                evo_emb = self._resize_embedding(evo_emb, self.config.evoformer_dimension)
            embeddings['evoformer'] = evo_emb
        else:
            embeddings['evoformer'] = np.zeros(self.config.evoformer_dimension)
            logger.warning("No Evoformer embedding provided, using zeros")
        
        # ESE embedding
        if multi_modal_input.ese_embedding is not None:
            ese_emb = multi_modal_input.ese_embedding
            if len(ese_emb) != self.config.ese_dimension:
                logger.warning(f"ESE embedding dimension mismatch: {len(ese_emb)} vs {self.config.ese_dimension}")
                ese_emb = self._resize_embedding(ese_emb, self.config.ese_dimension)
            embeddings['ese'] = ese_emb
        else:
            embeddings['ese'] = np.zeros(self.config.ese_dimension)
            logger.warning("No ESE embedding provided, using zeros")
        
        return embeddings
    
    def _resize_embedding(self, embedding: np.ndarray, target_size: int) -> np.ndarray:
        """Resize embedding to target size"""
        
        if len(embedding) > target_size:
            # Truncate
            return embedding[:target_size]
        elif len(embedding) < target_size:
            # Pad with zeros
            padding = target_size - len(embedding)
            return np.pad(embedding, (0, padding), mode='constant')
        else:
            return embedding
    
    def _generate_sequence_context_embedding(self, sequence: str) -> np.ndarray:
        """Generate context embedding from sequence alone"""
        
        # Create basic protein context from sequence
        basic_context = ProteinContext(
            protein_name=f"Protein_{hash(sequence) % 10000}",
            function_description="Unknown protein function",
            organism="Unknown organism"
        )
        
        return self.pubmedbert_processor.process_protein_context(basic_context)
    
    def _prepare_tensors(self, embeddings: Dict[str, np.ndarray]) -> Dict[str, torch.Tensor]:
        """Convert embeddings to tensors"""
        
        tensors = {}
        
        for modality, embedding in embeddings.items():
            # Normalize embedding
            if modality == 'esmc' and not hasattr(self.esmc_scaler, 'mean_'):
                # Fit scaler with synthetic data
                synthetic = np.random.normal(0, 1, (1000, len(embedding)))
                self.esmc_scaler.fit(synthetic)
            elif modality == 'evoformer' and not hasattr(self.evoformer_scaler, 'mean_'):
                synthetic = np.random.normal(0, 1, (1000, len(embedding)))
                self.evoformer_scaler.fit(synthetic)
            elif modality == 'ese' and not hasattr(self.ese_scaler, 'mean_'):
                synthetic = np.random.normal(0, 1, (1000, len(embedding)))
                self.ese_scaler.fit(synthetic)
            elif modality == 'pubmedbert' and not hasattr(self.pubmedbert_scaler, 'mean_'):
                synthetic = np.random.normal(0, 1, (1000, len(embedding)))
                self.pubmedbert_scaler.fit(synthetic)
            
            # Apply scaling
            if modality == 'esmc':
                scaled = self.esmc_scaler.transform(embedding.reshape(1, -1)).flatten()
            elif modality == 'evoformer':
                scaled = self.evoformer_scaler.transform(embedding.reshape(1, -1)).flatten()
            elif modality == 'ese':
                scaled = self.ese_scaler.transform(embedding.reshape(1, -1)).flatten()
            elif modality == 'pubmedbert':
                scaled = self.pubmedbert_scaler.transform(embedding.reshape(1, -1)).flatten()
            else:
                scaled = embedding
            
            # Convert to tensor
            tensor = torch.FloatTensor(scaled).unsqueeze(0).to(self.device)  # Add batch dimension
            tensors[modality] = tensor
        
        return tensors
    
    def _process_attention_weights(self, attention_weights_list: List[Dict]) -> Dict[str, np.ndarray]:
        """Process attention weights from fusion layers"""
        
        processed_weights = {}
        
        if attention_weights_list:
            # Average attention weights across layers
            num_layers = len(attention_weights_list)
            
            # Initialize accumulator
            for key in attention_weights_list[0].keys():
                if key != 'cross_modal_matrix':
                    processed_weights[key] = torch.zeros_like(attention_weights_list[0][key])
            
            # Accumulate across layers
            for layer_weights in attention_weights_list:
                for key, weights in layer_weights.items():
                    if key != 'cross_modal_matrix':
                        processed_weights[key] += weights
            
            # Average and convert to numpy
            for key in processed_weights:
                processed_weights[key] = (processed_weights[key] / num_layers).cpu().numpy().flatten()
            
            # Process cross-modal matrix (use last layer)
            if 'cross_modal_matrix' in attention_weights_list[-1]:
                processed_weights['cross_modal_matrix'] = attention_weights_list[-1]['cross_modal_matrix'].cpu().numpy()
        
        else:
            # Default weights
            processed_weights = {
                'esmc_to_all': np.array([0.25, 0.25, 0.25, 0.25]),
                'evoformer_to_all': np.array([0.25, 0.25, 0.25, 0.25]),
                'ese_to_all': np.array([0.25, 0.25, 0.25, 0.25]),
                'pubmedbert_to_all': np.array([0.25, 0.25, 0.25, 0.25]),
                'cross_modal_matrix': np.ones((1, 4, 4)) * 0.25
            }
        
        return processed_weights
    
    def _process_modality_importance(self, importance_tensor: torch.Tensor) -> Dict[str, float]:
        """Process modality importance scores"""
        
        importance_np = importance_tensor.cpu().numpy().flatten()
        
        modality_names = ['esmc', 'evoformer', 'ese', 'pubmedbert']
        
        importance_dict = {}
        for i, modality in enumerate(modality_names):
            importance_dict[modality] = float(importance_np[i]) if i < len(importance_np) else 0.25
        
        return importance_dict
    
    def _calculate_fusion_quality(
        self, 
        fusion_analysis: Dict, 
        embeddings: Dict[str, np.ndarray]
    ) -> Dict[str, float]:
        """Calculate fusion quality metrics"""
        
        quality_metrics = {}
        
        # Representation norms
        if 'fusion_quality' in fusion_analysis:
            for key, value in fusion_analysis['fusion_quality'].items():
                if isinstance(value, torch.Tensor):
                    quality_metrics[key] = float(value.mean().cpu())
        
        # Embedding coherence (cosine similarities between modalities)
        modalities = list(embeddings.keys())
        similarities = []
        
        for i in range(len(modalities)):
            for j in range(i + 1, len(modalities)):
                emb1 = embeddings[modalities[i]].reshape(1, -1)
                emb2 = embeddings[modalities[j]].reshape(1, -1)
                
                similarity = cosine_similarity(emb1, emb2)[0, 0]
                similarities.append(similarity)
        
        if similarities:
            quality_metrics['embedding_coherence'] = np.mean(similarities)
            quality_metrics['embedding_diversity'] = np.std(similarities)
        
        # Fusion stability (based on attention weight variance)
        if 'attention_weights' in fusion_analysis:
            attention_vars = []
            for layer_attn in fusion_analysis['attention_weights']:
                for key, weights in layer_attn.items():
                    if key != 'cross_modal_matrix':
                        attention_vars.append(float(torch.var(weights)))
            
            if attention_vars:
                quality_metrics['attention_stability'] = 1.0 / (1.0 + np.mean(attention_vars))
        
        return quality_metrics
    
    def _calculate_confidence_score(
        self,
        embeddings: Dict[str, np.ndarray],
        fusion_analysis: Dict,
        embedding_quality: Optional[Dict[str, float]]
    ) -> float:
        """Calculate confidence score for fusion result"""
        
        confidence_factors = []
        
        # Input quality factor
        if embedding_quality:
            input_quality = np.mean(list(embedding_quality.values()))
            confidence_factors.append(input_quality)
        else:
            confidence_factors.append(0.7)  # Default moderate confidence
        
        # Modality availability factor
        non_zero_modalities = sum(1 for emb in embeddings.values() if np.any(emb != 0))
        availability_score = non_zero_modalities / len(embeddings)
        confidence_factors.append(availability_score)
        
        # Fusion consistency factor
        if 'modality_importance' in fusion_analysis:
            importance_values = fusion_analysis['modality_importance'].cpu().numpy()
            # Good fusion has balanced importance (not too concentrated)
            importance_entropy = -np.sum(importance_values * np.log(importance_values + 1e-8))
            max_entropy = np.log(len(importance_values))
            consistency_score = importance_entropy / max_entropy
            confidence_factors.append(consistency_score)
        
        # Representation quality factor
        representation_magnitudes = [np.linalg.norm(emb) for emb in embeddings.values()]
        magnitude_variance = np.var(representation_magnitudes)
        magnitude_quality = 1.0 / (1.0 + magnitude_variance)  # Lower variance = higher quality
        confidence_factors.append(magnitude_quality)
        
        # Combined confidence
        overall_confidence = np.mean(confidence_factors)
        
        return float(np.clip(overall_confidence, 0.1, 0.99))
    
    def _update_processing_stats(self, output: FusionOutput):
        """Update processing statistics"""
        
        self.processing_stats['fusions_processed'] += 1
        self.processing_stats['total_processing_time'] += output.processing_time
        self.processing_stats['average_processing_time'] = (
            self.processing_stats['total_processing_time'] / 
            self.processing_stats['fusions_processed']
        )
    
    def save_fusion_output(self, output: FusionOutput, filepath: str):
        """Save fusion output to file"""
        
        # Prepare data for saving
        save_data = {
            'sequence_id': output.sequence_id,
            'fused_embedding': output.fused_embedding.tolist(),
            'structural_embedding': output.structural_embedding.tolist(),
            'contextual_embedding': output.contextual_embedding.tolist(),
            'esmc_embedding': output.esmc_embedding.tolist(),
            'evoformer_embedding': output.evoformer_embedding.tolist(),
            'ese_embedding': output.ese_embedding.tolist(),
            'pubmedbert_embedding': output.pubmedbert_embedding.tolist(),
            'attention_weights': {k: v.tolist() for k, v in output.attention_weights.items()},
            'modality_importance': output.modality_importance,
            'fusion_quality': output.fusion_quality,
            'processing_time': output.processing_time,
            'confidence_score': output.confidence_score,
            'fusion_metadata': output.fusion_metadata
        }
        
        # Save as JSON
        with open(filepath, 'w') as f:
            json.dump(save_data, f, indent=2)
        
        logger.info(f"Fusion output saved to {filepath}")
    
    def get_processing_statistics(self) -> Dict[str, Any]:
        """Get comprehensive processing statistics"""
        
        stats = self.processing_stats.copy()
        
        # Add configuration info
        stats['fusion_config'] = self.config.__dict__
        
        # Add cache statistics
        stats['cache_size'] = len(self.fusion_cache)
        stats['pubmedbert_cache_size'] = len(self.pubmedbert_processor.embedding_cache)
        
        # Add device info
        stats['device'] = str(self.device)
        stats['real_pubmedbert'] = self.pubmedbert_processor.real_pubmedbert
        
        return stats
    
    def clear_cache(self):
        """Clear processing caches"""
        
        fusion_cache_size = len(self.fusion_cache)
        bert_cache_size = len(self.pubmedbert_processor.embedding_cache)
        
        self.fusion_cache.clear()
        self.pubmedbert_processor.embedding_cache.clear()
        
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        
        logger.info(f"Cleared fusion caches (fusion: {fusion_cache_size}, PubMedBERT: {bert_cache_size})")
    
    def __del__(self):
        """Cleanup when fusion engine is destroyed"""
        
        if hasattr(self, 'device') and self.device.type == "cuda":
            torch.cuda.empty_cache()
        
        logger.info("PubMedBERT Fusion Engine cleaned up")
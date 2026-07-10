"""
ESM-C 6B Processor - Dr. Yuan Chen
==================================

ESM-C 6B protein language model processor for MICA-Lineage system.
Provides next-generation 6 billion parameter protein understanding.

Phase 4 Implementation: PubMedBERT Integration (4 weeks)
Lead: Dr. Yuan Chen + Alex Rodriguez  
"""

import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass
from pathlib import Path
import json
import time

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
import h5py
from scipy.spatial.distance import cosine
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

# Updated imports for compatibility with requirements
try:
    # Try fair-esm first (preferred for ESM-C 6B)
    import esm
    from esm import pretrained
    ESM_FAIR_AVAILABLE = True
    logger.info("Using fair-esm (Meta AI) for ESM-C 6B models")
except ImportError:
    ESM_FAIR_AVAILABLE = False
    logger.warning("fair-esm not available, falling back to transformers")

# Standard transformers as fallback
from transformers import EsmTokenizer, EsmForMaskedLM, EsmConfig, AutoTokenizer, AutoModel

# Performance optimizations
try:
    import numba
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    logger.warning("Numba not available, some operations will be slower")

# Fast JSON serialization
try:
    import orjson as json_lib
    JSON_FAST_AVAILABLE = True
except ImportError:
    import json as json_lib
    JSON_FAST_AVAILABLE = False

from bsm.config import get_bsm_config


@dataclass
class ESMCConfig:
    """Configuration for ESM-C 6B processing - Updated for requirements compatibility"""
    
    # Model selection (prioritized list)
    model_name: str = "esm2_t36_3B_UR50D"  # Will try ESM-C 6B first if available
    model_source: str = "auto"  # "fair-esm", "transformers", or "auto"
    
    # Processing parameters
    max_sequence_length: int = 1024
    batch_size: int = 4  # Small batch for large models
    device: str = "auto"
    precision: str = "fp16"  # Memory optimization
    
    # Embedding extraction
    layer_selection: str = "last_4_mean"  # Which layers to extract
    pooling_strategy: str = "mean_no_cls"  # How to pool sequence representations
    output_dimension: int = 2560  # Target output dimension (ESM-C 6B native)
    
    # Performance optimization
    use_gradient_checkpointing: bool = True
    enable_mixed_precision: bool = True
    use_torch_compile: bool = False  # Experimental PyTorch 2.0 feature
    
    # Caching
    cache_embeddings: bool = True
    cache_size_limit: int = 1000  # Maximum cached sequences
    
    # Memory management
    memory_fraction: float = 0.8  # GPU memory fraction to use
    cleanup_interval: int = 100  # Clean cache every N sequences


@dataclass
class ProteinInput:
    """Input structure for protein sequences"""
    sequence_id: str
    sequence: str


@dataclass  
class ESMCOutput:
    """Output from ESM-C processing"""
    sequence_id: str
    sequence: str
    embeddings: np.ndarray  # Primary sequence embedding
    attention_weights: Optional[np.ndarray] = None
    layer_embeddings: Optional[Dict[int, np.ndarray]] = None
    processing_time: float = 0.0
    confidence_score: float = 1.0
    metadata: Dict[str, Any] = None


class ProteinDataset(Dataset):
    """Dataset class for batch processing proteins"""
    
    def __init__(self, sequences: List[Tuple[str, str]], tokenizer, max_length: int):
        self.sequences = sequences  # List of (id, sequence) tuples
        self.tokenizer = tokenizer
        self.max_length = max_length
    
    def __len__(self):
        return len(self.sequences)
    
    def __getitem__(self, idx):
        seq_id, sequence = self.sequences[idx]
        
        # Truncate sequence if too long
        if len(sequence) > self.max_length - 2:  # Account for special tokens
            sequence = sequence[:self.max_length - 2]
        
        # Tokenize
        tokens = self.tokenizer(
            sequence,
            truncation=True,
            padding=False,
            max_length=self.max_length,
            return_tensors="pt"
        )
        
        return {
            'seq_id': seq_id,
            'sequence': sequence,
            'input_ids': tokens['input_ids'].squeeze(0),
            'attention_mask': tokens['attention_mask'].squeeze(0)
        }


class ESMCProcessor:
    """
    ESM-C 6B Protein Language Model Processor.
    
    Capabilities:
    - Next-generation 6B parameter protein understanding
    - Multi-layer representation extraction with attention analysis
    - Batch processing optimization for large-scale datasets
    - Memory-efficient processing with gradient checkpointing
    - Integration with MICA-Lineage multi-modal fusion
    """
    
    def __init__(self, config: Optional[ESMCConfig] = None):
        self.config = config or ESMCConfig()
        self.bsm_config = get_bsm_config()
        
        # Model components
        self.tokenizer = None
        self.model = None
        self.device = None
        
        # Processing state
        self.processing_cache = {}
        self.batch_processor = None
        
        # Performance tracking
        self.processing_stats = {
            'sequences_processed': 0,
            'total_processing_time': 0.0,
            'average_sequence_time': 0.0,
            'memory_usage_peak': 0.0
        }
        
        # Initialize processor
        self._setup_device()
        self._load_model()
        self._setup_batch_processing()
        
        logger.info("ESM-C 6B Processor initialized - Dr. Yuan Chen implementation")
    
    def _setup_device(self):
        """Setup compute device with optimal configuration"""
        
        if self.config.device == "auto":
            if torch.cuda.is_available():
                self.device = torch.device("cuda")
                # Enable optimizations for modern GPUs
                torch.backends.cudnn.benchmark = True
                logger.info(f"Using GPU: {torch.cuda.get_device_name()}")
            else:
                self.device = torch.device("cpu")
                logger.warning("CUDA not available, using CPU (will be slow for 6B model)")
        else:
            self.device = torch.device(self.config.device)
        
        # Memory optimization settings
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
            # Enable memory fraction for large models
            torch.cuda.set_per_process_memory_fraction(0.8)
    
    def _load_model(self):
        """Load ESM-C 6B model with optimizations - Updated for requirements compatibility"""
        
        model_loaded = False
        
        # Strategy 1: Try fair-esm for best ESM-C 6B support
        if ESM_FAIR_AVAILABLE and self.config.model_source in ["auto", "fair-esm"]:
            try:
                logger.info("Attempting to load model via fair-esm (Meta AI)")
                
                # Try ESM-C 6B models first
                for model_name in ["esmc_6b", "esmc_300m", "esm2_t36_3B_UR50D"]:
                    try:
                        logger.info(f"Loading fair-esm model: {model_name}")
                        self.model, self.alphabet = pretrained.load_model_and_alphabet(model_name)
                        self.tokenizer = None  # fair-esm uses alphabet instead
                        self.model_source = "fair-esm"
                        self.actual_model_name = model_name
                        
                        self.model.to(self.device)
                        self.model.eval()
                        
                        # Enable optimizations
                        if self.config.use_gradient_checkpointing and hasattr(self.model, 'set_grad_checkpointing'):
                            self.model.set_grad_checkpointing(True)
                        
                        total_params = sum(p.numel() for p in self.model.parameters())
                        logger.info(f"Successfully loaded {model_name} via fair-esm: {total_params:,} parameters")
                        model_loaded = True
                        break
                        
                    except Exception as e:
                        logger.debug(f"Failed to load {model_name} via fair-esm: {e}")
                        continue
                        
            except Exception as e:
                logger.warning(f"fair-esm loading failed: {e}")
        
        # Strategy 2: Fall back to transformers library
        if not model_loaded:
            try:
                logger.info("Loading model via transformers library")
                
                # Model priority list (try in order)
                model_candidates = [
                    "facebook/esm2_t36_3B_UR50D",
                    "facebook/esm2_t33_650M_UR50D", 
                    "facebook/esm2_t30_150M_UR50D"
                ]
                
                for model_name in model_candidates:
                    try:
                        logger.info(f"Loading transformers model: {model_name}")
                        
                        # Load tokenizer
                        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
                        
                        # Load model with optimizations
                        model_kwargs = {
                            'torch_dtype': torch.float16 if self.config.precision == "fp16" else torch.float32,
                        }
                        
                        # Add device_map for large models
                        if torch.cuda.is_available() and "3B" in model_name:
                            model_kwargs['device_map'] = "auto"
                        
                        self.model = AutoModel.from_pretrained(model_name, **model_kwargs)
                        
                        if 'device_map' not in model_kwargs:
                            self.model.to(self.device)
                        
                        self.model.eval()
                        self.model_source = "transformers"
                        self.actual_model_name = model_name
                        
                        # Enable optimizations
                        if self.config.use_gradient_checkpointing and hasattr(self.model, 'gradient_checkpointing_enable'):
                            self.model.gradient_checkpointing_enable()
                        
                        # PyTorch 2.0 compilation (experimental)
                        if self.config.use_torch_compile and hasattr(torch, 'compile'):
                            try:
                                self.model = torch.compile(self.model)
                                logger.info("Model compiled with torch.compile")
                            except Exception as e:
                                logger.warning(f"torch.compile failed: {e}")
                        
                        total_params = sum(p.numel() for p in self.model.parameters())
                        logger.info(f"Successfully loaded {model_name} via transformers: {total_params:,} parameters")
                        model_loaded = True
                        break
                        
                    except Exception as e:
                        logger.debug(f"Failed to load {model_name} via transformers: {e}")
                        continue
                        
            except Exception as e:
                logger.error(f"Transformers loading failed: {e}")
        
        if not model_loaded:
            raise RuntimeError(
                "Failed to load any ESM model. Please check:\n"
                "1. Internet connection for downloading models\n"
                "2. Available GPU memory (need 8GB+ for large models)\n"
                "3. Install fair-esm: pip install fair-esm\n"
                "4. Install transformers: pip install transformers>=4.30.0"
            )
        
        # Set up mixed precision if requested
        if self.config.enable_mixed_precision and self.device.type == "cuda":
            try:
                from torch.cuda.amp import autocast
                self.use_autocast = True
                logger.info("Mixed precision training enabled")
            except ImportError:
                self.use_autocast = False
                logger.warning("Mixed precision not available")
        else:
            self.use_autocast = False
        
        logger.info(f"ESM model initialization complete - Source: {self.model_source}")
        
        # Initialize embedding dimension based on actual model
        if hasattr(self.model, 'config') and hasattr(self.model.config, 'hidden_size'):
            self.embedding_dim = self.model.config.hidden_size
        elif hasattr(self.model, 'embed_dim'):
            self.embedding_dim = self.model.embed_dim
        else:
            self.embedding_dim = self.config.output_dimension  # Fallback
            
        logger.info(f"Model embedding dimension: {self.embedding_dim}")
    
    def _setup_batch_processing(self):
        """Setup batch processing optimizations"""
        
        # Configure batch processing parameters based on available memory
        if self.device.type == "cuda":
            gpu_memory = torch.cuda.get_device_properties(0).total_memory / 1024**3  # GB
            
            if gpu_memory < 8:
                self.config.batch_size = 1
                logger.warning("Low GPU memory detected, using batch size 1")
            elif gpu_memory < 16:
                self.config.batch_size = 2
            elif gpu_memory < 24:
                self.config.batch_size = 4
            else:
                self.config.batch_size = 8
        else:
            self.config.batch_size = 1  # Conservative for CPU
        
        logger.info(f"Configured batch size: {self.config.batch_size}")
    
    async def process_sequence(self, sequence_id: str, sequence: str) -> ESMCOutput:
        """
        Process single protein sequence with ESM-C 6B.
        
        Args:
            sequence_id: Unique identifier for the sequence
            sequence: Amino acid sequence string
            
        Returns:
            ESMCOutput with embeddings and analysis
        """
        
        start_time = time.time()
        
        # Check cache first
        cache_key = f"{sequence_id}_{hash(sequence)}"
        if cache_key in self.processing_cache:
            logger.debug(f"Using cached result for {sequence_id}")
            return self.processing_cache[cache_key]
        
        # Validate sequence
        if not self._validate_sequence(sequence):
            raise ValueError(f"Invalid protein sequence: {sequence_id}")
        
        # Process sequence
        try:
            # Tokenize
            tokens = self._tokenize_sequence(sequence)
            
            # Get model embeddings
            embeddings, attention_weights, layer_embeddings = await self._extract_embeddings(
                tokens, sequence_id
            )
            
            # Post-process embeddings
            processed_embeddings = self._post_process_embeddings(embeddings)
            
            # Calculate confidence
            confidence = self._calculate_confidence(embeddings, attention_weights)
            
            # Create output
            output = ESMCOutput(
                sequence_id=sequence_id,
                sequence=sequence,
                embeddings=processed_embeddings,
                attention_weights=attention_weights,
                layer_embeddings=layer_embeddings,
                processing_time=time.time() - start_time,
                confidence_score=confidence,
                metadata={
                    'model_name': self.config.model_name,
                    'layer_selection': self.config.layer_selection,
                    'pooling_strategy': self.config.pooling_strategy,
                    'sequence_length': len(sequence)
                }
            )
            
            # Cache result
            self.processing_cache[cache_key] = output
            
            # Update stats
            self._update_processing_stats(output)
            
            return output
            
        except Exception as e:
            logger.error(f"Failed to process sequence {sequence_id}: {e}")
            raise RuntimeError(f"ESM-C processing failed: {e}")
    
    def _validate_sequence(self, sequence: str) -> bool:
        """Validate protein sequence format"""
        
        if not sequence:
            return False
        
        # Check for valid amino acids
        valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
        sequence_aa = set(sequence.upper())
        
        # Allow some ambiguous amino acids
        valid_aa.update("XBZJ*-")
        
        if not sequence_aa.issubset(valid_aa):
            invalid_chars = sequence_aa - valid_aa
            logger.warning(f"Invalid amino acids found: {invalid_chars}")
            return False
        
        # Check reasonable length
        if len(sequence) < 10 or len(sequence) > 5000:
            logger.warning(f"Unusual sequence length: {len(sequence)}")
            return False
        
        return True
    
    def _tokenize_sequence(self, sequence: str) -> Dict[str, torch.Tensor]:
        """Tokenize protein sequence - Compatible with both fair-esm and transformers"""
        
        if self.model_source == "fair-esm":
            # fair-esm tokenization
            try:
                tokens = self.alphabet.encode(sequence)
                tokens = torch.tensor(tokens, dtype=torch.long).unsqueeze(0)  # Add batch dimension
                
                # Create attention mask (all tokens are valid)
                attention_mask = torch.ones_like(tokens)
                
                return {
                    'input_ids': tokens.to(self.device),
                    'attention_mask': attention_mask.to(self.device)
                }
                
            except Exception as e:
                logger.error(f"fair-esm tokenization failed: {e}")
                raise
        
        else:
            # transformers tokenization
            try:
                # Add spaces between amino acids for ESM tokenization
                spaced_sequence = " ".join(sequence)
                
                # Tokenize
                tokens = self.tokenizer(
                    spaced_sequence,
                    truncation=True,
                    padding=True,
                    max_length=self.config.max_sequence_length,
                    return_tensors="pt"
                )
                
                # Move to device
                tokens = {k: v.to(self.device) for k, v in tokens.items()}
                
                return tokens
                
            except Exception as e:
                logger.error(f"transformers tokenization failed: {e}")
                raise
    
    async def _extract_embeddings(
        self, 
        tokens: Dict[str, torch.Tensor],
        sequence_id: str
    ) -> Tuple[np.ndarray, np.ndarray, Dict[int, np.ndarray]]:
        """Extract embeddings from ESM model - Compatible with both fair-esm and transformers"""
        
        # Use autocast if available and enabled
        context_manager = torch.cuda.amp.autocast() if self.use_autocast else torch.no_grad()
        
        with context_manager:
            try:
                if self.model_source == "fair-esm":
                    # fair-esm extraction
                    results = self.model(
                        tokens['input_ids'],
                        repr_layers=list(range(self.model.num_layers + 1)),
                        return_contacts=False
                    )
                    
                    # Extract representations
                    representations = results["representations"]
                    
                    # Get last layer by default
                    layer_idx = max(representations.keys())
                    embeddings = representations[layer_idx]
                    
                    # Pool sequence embeddings (remove batch dimension)
                    pooled_embeddings = self._pool_sequence_embeddings_fair_esm(
                        embeddings.squeeze(0), tokens['input_ids'].squeeze(0)
                    )
                    
                    # Store layer-wise embeddings for analysis
                    layer_embeddings = {}
                    for layer_idx, layer_repr in representations.items():
                        if layer_idx > 0:  # Skip embedding layer
                            layer_emb = self._pool_sequence_embeddings_fair_esm(
                                layer_repr.squeeze(0), tokens['input_ids'].squeeze(0)
                            )
                            layer_embeddings[layer_idx] = layer_emb.cpu().numpy()
                    
                    # fair-esm doesn't return attention weights by default
                    attention_weights = None
                    
                else:
                    # transformers extraction
                    outputs = self.model(
                        input_ids=tokens['input_ids'],
                        attention_mask=tokens['attention_mask'],
                        output_hidden_states=True,
                        output_attentions=True,
                        return_dict=True
                    )
                    
                    hidden_states = outputs.hidden_states  # All layer outputs
                    attentions = outputs.attentions if hasattr(outputs, 'attentions') else None
                    
                    # Extract embeddings based on layer selection strategy
                    embeddings = self._select_layer_embeddings(hidden_states)
                    
                    # Pool sequence embeddings
                    pooled_embeddings = self._pool_sequence_embeddings(
                        embeddings, tokens['attention_mask']
                    )
                    
                    # Extract attention weights
                    attention_weights = self._extract_attention_weights(attentions)
                    
                    # Store layer-wise embeddings for analysis
                    layer_embeddings = {}
                    if len(hidden_states) > 4:  # Store last 4 layers
                        for i, layer_output in enumerate(hidden_states[-4:]):
                            layer_idx = len(hidden_states) - 4 + i
                            layer_emb = self._pool_sequence_embeddings(
                                layer_output, tokens['attention_mask']
                            )
                            layer_embeddings[layer_idx] = layer_emb.cpu().numpy()
                
                return (
                    pooled_embeddings.cpu().numpy(),
                    attention_weights.cpu().numpy() if attention_weights is not None else None,
                    layer_embeddings
                )
                
            except RuntimeError as e:
                if "out of memory" in str(e):
                    logger.error(f"GPU OOM processing {sequence_id}, try reducing batch size")
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                raise e
    
    def _select_layer_embeddings(self, hidden_states: Tuple[torch.Tensor]) -> torch.Tensor:
        """Select which layers to use for embeddings"""
        
        if self.config.layer_selection == "last":
            return hidden_states[-1]
        
        elif self.config.layer_selection == "last_4_mean":
            if len(hidden_states) >= 4:
                last_4_layers = torch.stack(hidden_states[-4:])
                return torch.mean(last_4_layers, dim=0)
            else:
                return hidden_states[-1]
        
        elif self.config.layer_selection == "last_4_concat":
            if len(hidden_states) >= 4:
                last_4_layers = hidden_states[-4:]
                return torch.cat(last_4_layers, dim=-1)
            else:
                return hidden_states[-1]
        
        elif self.config.layer_selection == "all_mean":
            all_layers = torch.stack(hidden_states[1:])  # Skip embedding layer
            return torch.mean(all_layers, dim=0)
        
        else:
            logger.warning(f"Unknown layer selection: {self.config.layer_selection}")
            return hidden_states[-1]
    
    def _pool_sequence_embeddings(
        self, 
        embeddings: torch.Tensor, 
        attention_mask: torch.Tensor
    ) -> torch.Tensor:
        """Pool sequence embeddings to fixed-size representation"""
        
        if self.config.pooling_strategy == "mean":
            # Standard mean pooling
            masked_embeddings = embeddings * attention_mask.unsqueeze(-1)
            pooled = torch.sum(masked_embeddings, dim=1) / torch.sum(attention_mask, dim=1, keepdim=True)
        
        elif self.config.pooling_strategy == "mean_no_cls":
            # Mean pooling excluding CLS token
            if embeddings.size(1) > 1:
                sequence_embeddings = embeddings[:, 1:, :]  # Skip CLS token
                sequence_mask = attention_mask[:, 1:]
                
                masked_embeddings = sequence_embeddings * sequence_mask.unsqueeze(-1)
                pooled = torch.sum(masked_embeddings, dim=1) / torch.sum(sequence_mask, dim=1, keepdim=True)
            else:
                pooled = embeddings[:, 0, :]  # Fallback to first token
        
        elif self.config.pooling_strategy == "cls_only":
            # Use CLS token only
            pooled = embeddings[:, 0, :]
        
        elif self.config.pooling_strategy == "max":
            # Max pooling
            masked_embeddings = embeddings * attention_mask.unsqueeze(-1)
            pooled = torch.max(masked_embeddings, dim=1)[0]
        
        else:
            logger.warning(f"Unknown pooling strategy: {self.config.pooling_strategy}")
            pooled = torch.mean(embeddings, dim=1)  # Fallback
        
        return pooled
    
    def _pool_sequence_embeddings_fair_esm(
        self, 
        embeddings: torch.Tensor,
        tokens: torch.Tensor
    ) -> torch.Tensor:
        """Pool sequence embeddings for fair-esm output (no batch dimension)"""
        
        # fair-esm tokens: 0=CLS, 1=sequence..., 2=EOS
        # We typically want to exclude special tokens
        
        if self.config.pooling_strategy == "mean":
            # Mean pooling over sequence tokens (excluding CLS and EOS)
            if len(tokens) > 2:
                sequence_embeddings = embeddings[1:-1]  # Skip CLS and EOS
                pooled = torch.mean(sequence_embeddings, dim=0)
            else:
                pooled = embeddings[0]  # Fallback to CLS if very short
        
        elif self.config.pooling_strategy == "mean_no_cls":
            # Same as mean for fair-esm
            if len(tokens) > 2:
                sequence_embeddings = embeddings[1:-1]  # Skip CLS and EOS
                pooled = torch.mean(sequence_embeddings, dim=0)
            else:
                pooled = embeddings[0]
        
        elif self.config.pooling_strategy == "cls_only":
            # Use CLS token (first token)
            pooled = embeddings[0]
        
        elif self.config.pooling_strategy == "max":
            # Max pooling over sequence tokens
            if len(tokens) > 2:
                sequence_embeddings = embeddings[1:-1]  # Skip CLS and EOS
                pooled = torch.max(sequence_embeddings, dim=0)[0]
            else:
                pooled = embeddings[0]
        
        else:
            logger.warning(f"Unknown pooling strategy: {self.config.pooling_strategy}")
            pooled = torch.mean(embeddings, dim=0)  # Fallback
        
        return pooled
    
    def _extract_attention_weights(self, attentions: Tuple[torch.Tensor]) -> Optional[torch.Tensor]:
        """Extract and process attention weights"""
        
        if not attentions:
            return None
        
        # Use last layer attention
        last_attention = attentions[-1]  # [batch, heads, seq_len, seq_len]
        
        # Average across heads
        avg_attention = torch.mean(last_attention, dim=1)  # [batch, seq_len, seq_len]
        
        return avg_attention
    
    def _post_process_embeddings(self, embeddings: np.ndarray) -> np.ndarray:
        """Post-process raw embeddings for downstream use"""
        
        # L2 normalization for better similarity computation
        norm = np.linalg.norm(embeddings, axis=-1, keepdims=True)
        normalized_embeddings = embeddings / (norm + 1e-8)
        
        return normalized_embeddings
    
    def _calculate_confidence(
        self, 
        embeddings: np.ndarray, 
        attention_weights: Optional[np.ndarray]
    ) -> float:
        """Calculate confidence score for the embeddings"""
        
        confidence_factors = []
        
        # Embedding magnitude (higher magnitude often indicates more confidence)
        embedding_magnitude = np.linalg.norm(embeddings)
        normalized_magnitude = min(embedding_magnitude / 10.0, 1.0)  # Normalize
        confidence_factors.append(normalized_magnitude)
        
        # Attention distribution (more focused attention = higher confidence)
        if attention_weights is not None:
            # Calculate attention entropy
            attention_flat = attention_weights.flatten()
            attention_probs = np.exp(attention_flat) / np.sum(np.exp(attention_flat))
            attention_entropy = -np.sum(attention_probs * np.log(attention_probs + 1e-8))
            
            # Lower entropy = more focused = higher confidence
            max_entropy = np.log(len(attention_probs))
            attention_confidence = 1.0 - (attention_entropy / max_entropy)
            confidence_factors.append(attention_confidence)
        
        # Embedding variance (appropriate variance indicates good representation)
        embedding_variance = np.var(embeddings)
        variance_confidence = min(embedding_variance * 10, 1.0)  # Scale appropriately
        confidence_factors.append(variance_confidence)
        
        # Combined confidence
        overall_confidence = np.mean(confidence_factors)
        
        return float(np.clip(overall_confidence, 0.1, 0.99))
    
    def _update_processing_stats(self, output: ESMCOutput):
        """Update processing statistics"""
        
        self.processing_stats['sequences_processed'] += 1
        self.processing_stats['total_processing_time'] += output.processing_time
        self.processing_stats['average_sequence_time'] = (
            self.processing_stats['total_processing_time'] / 
            self.processing_stats['sequences_processed']
        )
        
        # Track memory usage
        if self.device.type == "cuda":
            current_memory = torch.cuda.max_memory_allocated() / 1024**3  # GB
            self.processing_stats['memory_usage_peak'] = max(
                self.processing_stats['memory_usage_peak'], 
                current_memory
            )
    
    async def batch_process_sequences(
        self, 
        sequences: List[Tuple[str, str]]
    ) -> List[ESMCOutput]:
        """
        Process multiple sequences in batches for efficiency.
        
        Args:
            sequences: List of (sequence_id, sequence) tuples
            
        Returns:
            List of ESMCOutput objects
        """
        
        logger.info(f"Batch processing {len(sequences)} sequences")
        
        # Create dataset
        dataset = ProteinDataset(sequences, self.tokenizer, self.config.max_sequence_length)
        
        # Create dataloader with custom collate function
        dataloader = DataLoader(
            dataset,
            batch_size=self.config.batch_size,
            shuffle=False,
            collate_fn=self._collate_batch
        )
        
        all_outputs = []
        
        for batch_idx, batch in enumerate(dataloader):
            logger.debug(f"Processing batch {batch_idx + 1}/{len(dataloader)}")
            
            try:
                batch_outputs = await self._process_batch(batch)
                all_outputs.extend(batch_outputs)
                
                # Clear cache periodically
                if batch_idx % 10 == 0:
                    torch.cuda.empty_cache()
                    
            except Exception as e:
                logger.error(f"Failed to process batch {batch_idx}: {e}")
                # Create error outputs for failed batch
                error_outputs = []
                for seq_id in batch['seq_ids']:
                    error_output = ESMCOutput(
                        sequence_id=seq_id,
                        sequence="",
                        embeddings=np.zeros(1280),  # ESM-2 3B dimension
                        confidence_score=0.0,
                        metadata={'error': str(e)}
                    )
                    error_outputs.append(error_output)
                all_outputs.extend(error_outputs)
        
        logger.info(f"Completed batch processing: {len(all_outputs)} results")
        return all_outputs
    
    def _collate_batch(self, batch: List[Dict]) -> Dict[str, Any]:
        """Custom collate function for batching variable-length sequences"""
        
        seq_ids = [item['seq_id'] for item in batch]
        sequences = [item['sequence'] for item in batch]
        
        # Pad input_ids and attention_masks
        max_length = max(len(item['input_ids']) for item in batch)
        
        padded_input_ids = []
        padded_attention_masks = []
        
        for item in batch:
            input_ids = item['input_ids']
            attention_mask = item['attention_mask']
            
            # Pad to max_length
            pad_length = max_length - len(input_ids)
            
            if pad_length > 0:
                # Pad with tokenizer pad_token_id
                pad_token_id = self.tokenizer.pad_token_id or self.tokenizer.eos_token_id
                
                padded_input_ids.append(
                    torch.cat([input_ids, torch.full((pad_length,), pad_token_id, dtype=input_ids.dtype)])
                )
                padded_attention_masks.append(
                    torch.cat([attention_mask, torch.zeros(pad_length, dtype=attention_mask.dtype)])
                )
            else:
                padded_input_ids.append(input_ids)
                padded_attention_masks.append(attention_mask)
        
        return {
            'seq_ids': seq_ids,
            'sequences': sequences,
            'input_ids': torch.stack(padded_input_ids).to(self.device),
            'attention_mask': torch.stack(padded_attention_masks).to(self.device)
        }
    
    async def _process_batch(self, batch: Dict[str, Any]) -> List[ESMCOutput]:
        """Process a single batch of sequences"""
        
        start_time = time.time()
        batch_outputs = []
        
        with torch.no_grad():
            try:
                # Forward pass
                outputs = self.model(
                    input_ids=batch['input_ids'],
                    attention_mask=batch['attention_mask'],
                    output_hidden_states=True,
                    output_attentions=True,
                    return_dict=True
                )
                
                hidden_states = outputs.hidden_states
                attentions = outputs.attentions
                
                # Process each sequence in the batch
                for i, (seq_id, sequence) in enumerate(zip(batch['seq_ids'], batch['sequences'])):
                    
                    # Extract embeddings for this sequence
                    seq_hidden_states = tuple(hs[i:i+1] for hs in hidden_states)
                    seq_attentions = tuple(att[i:i+1] for att in attentions)
                    seq_attention_mask = batch['attention_mask'][i:i+1]
                    
                    # Process embeddings
                    embeddings = self._select_layer_embeddings(seq_hidden_states)
                    pooled_embeddings = self._pool_sequence_embeddings(embeddings, seq_attention_mask)
                    
                    # Extract attention
                    attention_weights = self._extract_attention_weights(seq_attentions)
                    
                    # Post-process
                    processed_embeddings = self._post_process_embeddings(pooled_embeddings.cpu().numpy())
                    confidence = self._calculate_confidence(
                        processed_embeddings, 
                        attention_weights.cpu().numpy() if attention_weights is not None else None
                    )
                    
                    # Create output
                    output = ESMCOutput(
                        sequence_id=seq_id,
                        sequence=sequence,
                        embeddings=processed_embeddings.flatten(),
                        attention_weights=attention_weights[0].cpu().numpy() if attention_weights is not None else None,
                        processing_time=(time.time() - start_time) / len(batch['seq_ids']),  # Average time per sequence
                        confidence_score=confidence,
                        metadata={
                            'model_name': self.config.model_name,
                            'batch_processing': True,
                            'sequence_length': len(sequence)
                        }
                    )
                    
                    batch_outputs.append(output)
                    self._update_processing_stats(output)
                
            except RuntimeError as e:
                if "out of memory" in str(e):
                    logger.error("GPU OOM in batch processing, reducing batch size")
                    torch.cuda.empty_cache()
                raise e
        
        return batch_outputs
    
    def save_embeddings(self, outputs: List[ESMCOutput], filepath: str):
        """Save embeddings to HDF5 file for efficient storage"""
        
        logger.info(f"Saving {len(outputs)} embeddings to {filepath}")
        
        with h5py.File(filepath, 'w') as f:
            # Create datasets
            sequences_group = f.create_group('sequences')
            embeddings_group = f.create_group('embeddings')
            metadata_group = f.create_group('metadata')
            
            for i, output in enumerate(outputs):
                # Store sequence info
                seq_group = sequences_group.create_group(f'seq_{i}')
                seq_group.attrs['sequence_id'] = output.sequence_id
                seq_group.attrs['sequence'] = output.sequence
                seq_group.attrs['sequence_length'] = len(output.sequence)
                
                # Store embeddings
                emb_group = embeddings_group.create_group(f'seq_{i}')
                emb_group.create_dataset('primary_embedding', data=output.embeddings)
                
                if output.attention_weights is not None:
                    emb_group.create_dataset('attention_weights', data=output.attention_weights)
                
                if output.layer_embeddings:
                    layer_group = emb_group.create_group('layer_embeddings')
                    for layer_idx, layer_emb in output.layer_embeddings.items():
                        layer_group.create_dataset(f'layer_{layer_idx}', data=layer_emb)
                
                # Store metadata
                meta_group = metadata_group.create_group(f'seq_{i}')
                meta_group.attrs['processing_time'] = output.processing_time
                meta_group.attrs['confidence_score'] = output.confidence_score
                
                if output.metadata:
                    for key, value in output.metadata.items():
                        if isinstance(value, (str, int, float, bool)):
                            meta_group.attrs[key] = value
                        else:
                            meta_group.attrs[key] = str(value)
            
            # Store global metadata
            f.attrs['model_name'] = self.config.model_name
            f.attrs['total_sequences'] = len(outputs)
            f.attrs['processing_stats'] = json.dumps(self.processing_stats)
        
        logger.info(f"Embeddings saved successfully to {filepath}")
    
    def load_embeddings(self, filepath: str) -> List[ESMCOutput]:
        """Load embeddings from HDF5 file"""
        
        logger.info(f"Loading embeddings from {filepath}")
        outputs = []
        
        with h5py.File(filepath, 'r') as f:
            total_sequences = f.attrs['total_sequences']
            
            for i in range(total_sequences):
                # Load sequence info
                seq_group = f['sequences'][f'seq_{i}']
                sequence_id = seq_group.attrs['sequence_id']
                sequence = seq_group.attrs['sequence']
                
                # Load embeddings
                emb_group = f['embeddings'][f'seq_{i}']
                embeddings = emb_group['primary_embedding'][:]
                
                attention_weights = None
                if 'attention_weights' in emb_group:
                    attention_weights = emb_group['attention_weights'][:]
                
                layer_embeddings = {}
                if 'layer_embeddings' in emb_group:
                    layer_group = emb_group['layer_embeddings']
                    for layer_name in layer_group.keys():
                        layer_idx = int(layer_name.split('_')[1])
                        layer_embeddings[layer_idx] = layer_group[layer_name][:]
                
                # Load metadata
                meta_group = f['metadata'][f'seq_{i}']
                processing_time = meta_group.attrs['processing_time']
                confidence_score = meta_group.attrs['confidence_score']
                
                metadata = {}
                for key, value in meta_group.attrs.items():
                    if key not in ['processing_time', 'confidence_score']:
                        metadata[key] = value
                
                # Create output object
                output = ESMCOutput(
                    sequence_id=sequence_id,
                    sequence=sequence,
                    embeddings=embeddings,
                    attention_weights=attention_weights,
                    layer_embeddings=layer_embeddings or None,
                    processing_time=processing_time,
                    confidence_score=confidence_score,
                    metadata=metadata
                )
                
                outputs.append(output)
        
        logger.info(f"Loaded {len(outputs)} embeddings from {filepath}")
        return outputs
    
    def get_processing_statistics(self) -> Dict[str, Any]:
        """Get comprehensive processing statistics"""
        
        stats = self.processing_stats.copy()
        
        # Add model information
        stats['model_info'] = {
            'model_name': self.config.model_name,
            'device': str(self.device),
            'precision': self.config.precision,
            'batch_size': self.config.batch_size,
            'max_sequence_length': self.config.max_sequence_length
        }
        
        # Add cache information
        stats['cache_info'] = {
            'cached_sequences': len(self.processing_cache),
            'cache_hit_rate': 0.0  # Would track in production
        }
        
        return stats
    
    def clear_cache(self):
        """Clear processing cache to free memory"""
        
        cache_size = len(self.processing_cache)
        self.processing_cache.clear()
        
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        
        logger.info(f"Cleared cache ({cache_size} entries) and GPU memory")
    
    def __del__(self):
        """Cleanup when processor is destroyed"""
        
        if hasattr(self, 'device') and self.device.type == "cuda":
            torch.cuda.empty_cache()
        
        logger.info("ESM-C Processor cleaned up")
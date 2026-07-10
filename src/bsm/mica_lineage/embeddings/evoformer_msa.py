"""
Evoformer MSA Processor - Dr. Yuan Chen
======================================

Evoformer architecture for Multiple Sequence Alignment processing.
Extracts co-evolutionary coupling signals for protein analysis.

Phase 3 Implementation: ESE Pipeline (6 weeks)
Lead: Dr. Yuan Chen + Sofia Petrov + Priya Sharma
"""

import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass
import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy.spatial.distance import pdist, squareform
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
try:
    import biotite.sequence.align as align
    import biotite.sequence as seq
    BIOTITE_AVAILABLE = True
except ImportError:
    BIOTITE_AVAILABLE = False
    align = None
    seq = None

from bsm.config import get_bsm_config

logger = logging.getLogger(__name__)


@dataclass
class EvoformerConfig:
    """Configuration for Evoformer MSA processing"""
    max_msa_depth: int = 512  # Maximum MSA sequences to process
    max_sequence_length: int = 1024  # Maximum sequence length
    num_blocks: int = 8  # Number of Evoformer blocks
    msa_channels: int = 256  # MSA representation channels
    pair_channels: int = 128  # Pairwise representation channels
    num_heads: int = 8  # Number of attention heads
    dropout_rate: float = 0.1  # Dropout probability
    device: str = "auto"  # Computing device


@dataclass
class MSAInput:
    """Input Multiple Sequence Alignment"""
    target_sequence: str
    aligned_sequences: List[str]
    sequence_weights: Optional[np.ndarray] = None
    species_info: Optional[List[str]] = None
    alignment_scores: Optional[np.ndarray] = None


@dataclass
class EvoformerOutput:
    """Output from Evoformer MSA processing"""
    sequence_id: str
    msa_representation: np.ndarray  # [msa_depth, seq_len, channels]
    pair_representation: np.ndarray  # [seq_len, seq_len, channels]
    coevolution_matrix: np.ndarray  # [seq_len, seq_len]
    coupling_scores: np.ndarray  # [seq_len, seq_len]
    conservation_profile: np.ndarray  # [seq_len]
    evoformer_embedding: np.ndarray  # 512D final embedding
    processing_time: float = 0.0
    confidence_score: float = 1.0
    metadata: Dict[str, Any] = None


class MSAAttention(nn.Module):
    """Multi-head attention for MSA processing"""
    
    def __init__(self, channels: int, num_heads: int, dropout: float = 0.1):
        super().__init__()
        self.channels = channels
        self.num_heads = num_heads
        self.head_dim = channels // num_heads
        
        assert channels % num_heads == 0, "channels must be divisible by num_heads"
        
        self.q_proj = nn.Linear(channels, channels)
        self.k_proj = nn.Linear(channels, channels)
        self.v_proj = nn.Linear(channels, channels)
        self.o_proj = nn.Linear(channels, channels)
        
        self.dropout = nn.Dropout(dropout)
        self.scale = self.head_dim ** -0.5
    
    def forward(self, msa: torch.Tensor, mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Forward pass of MSA attention
        
        Args:
            msa: [batch, msa_depth, seq_len, channels]
            mask: [batch, msa_depth, seq_len] or None
            
        Returns:
            Updated MSA representation
        """
        
        batch, msa_depth, seq_len, channels = msa.shape
        
        # Project to Q, K, V
        q = self.q_proj(msa)  # [batch, msa_depth, seq_len, channels]
        k = self.k_proj(msa)
        v = self.v_proj(msa)
        
        # Reshape for multi-head attention
        q = q.view(batch, msa_depth, seq_len, self.num_heads, self.head_dim)
        k = k.view(batch, msa_depth, seq_len, self.num_heads, self.head_dim)
        v = v.view(batch, msa_depth, seq_len, self.num_heads, self.head_dim)
        
        # Transpose for attention computation
        q = q.transpose(2, 3)  # [batch, msa_depth, num_heads, seq_len, head_dim]
        k = k.transpose(2, 3)
        v = v.transpose(2, 3)
        
        # Compute attention scores
        scores = torch.matmul(q, k.transpose(-2, -1)) * self.scale
        
        # Apply mask if provided
        if mask is not None:
            mask = mask.unsqueeze(2).unsqueeze(-1)  # Broadcast to attention dimensions
            scores.masked_fill_(mask == 0, float('-inf'))
        
        # Apply softmax
        attn_weights = F.softmax(scores, dim=-1)
        attn_weights = self.dropout(attn_weights)
        
        # Apply attention to values
        out = torch.matmul(attn_weights, v)
        
        # Reshape back
        out = out.transpose(2, 3).contiguous()  # [batch, msa_depth, seq_len, num_heads, head_dim]
        out = out.view(batch, msa_depth, seq_len, channels)
        
        # Final projection
        out = self.o_proj(out)
        
        return out


class PairAttention(nn.Module):
    """Attention for pairwise representations"""
    
    def __init__(self, pair_channels: int, msa_channels: int, num_heads: int, dropout: float = 0.1):
        super().__init__()
        self.pair_channels = pair_channels
        self.msa_channels = msa_channels
        self.num_heads = num_heads
        self.head_dim = pair_channels // num_heads
        
        # Projections
        self.q_proj = nn.Linear(pair_channels, pair_channels)
        self.k_proj = nn.Linear(pair_channels, pair_channels)
        self.v_proj = nn.Linear(pair_channels, pair_channels)
        self.o_proj = nn.Linear(pair_channels, pair_channels)
        
        # MSA to pair bias
        self.msa_to_pair_bias = nn.Linear(msa_channels, num_heads)
        
        self.dropout = nn.Dropout(dropout)
        self.scale = self.head_dim ** -0.5
    
    def forward(
        self, 
        pair: torch.Tensor, 
        msa: torch.Tensor,
        pair_mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass of pair attention
        
        Args:
            pair: [batch, seq_len, seq_len, pair_channels]
            msa: [batch, msa_depth, seq_len, msa_channels] 
            pair_mask: [batch, seq_len, seq_len] or None
            
        Returns:
            Updated pair representation
        """
        
        batch, seq_len, _, pair_channels = pair.shape
        
        # Project pair representation
        q = self.q_proj(pair)
        k = self.k_proj(pair) 
        v = self.v_proj(pair)
        
        # Reshape for attention
        q = q.view(batch, seq_len, seq_len, self.num_heads, self.head_dim)
        k = k.view(batch, seq_len, seq_len, self.num_heads, self.head_dim)
        v = v.view(batch, seq_len, seq_len, self.num_heads, self.head_dim)
        
        # Compute attention scores
        scores = torch.matmul(q, k.transpose(-2, -1)) * self.scale
        
        # Add MSA bias if provided
        if msa is not None:
            # Average MSA representation over depth
            msa_avg = torch.mean(msa, dim=1)  # [batch, seq_len, msa_channels]
            
            # Compute pairwise MSA bias
            msa_bias = self.msa_to_pair_bias(msa_avg)  # [batch, seq_len, num_heads]
            msa_bias = msa_bias.unsqueeze(2)  # [batch, seq_len, 1, num_heads]
            msa_bias = msa_bias.unsqueeze(-1)  # [batch, seq_len, 1, num_heads, 1]
            
            scores = scores + msa_bias
        
        # Apply mask if provided
        if pair_mask is not None:
            pair_mask = pair_mask.unsqueeze(-1).unsqueeze(-1)  # Broadcast
            scores.masked_fill_(pair_mask == 0, float('-inf'))
        
        # Softmax and dropout
        attn_weights = F.softmax(scores, dim=-2)  # Attention over sequence positions
        attn_weights = self.dropout(attn_weights)
        
        # Apply attention
        out = torch.matmul(attn_weights, v)
        out = out.view(batch, seq_len, seq_len, pair_channels)
        
        # Final projection
        out = self.o_proj(out)
        
        return out


class EvoformerBlock(nn.Module):
    """Single Evoformer block with MSA and pair updates"""
    
    def __init__(self, config: EvoformerConfig):
        super().__init__()
        self.config = config
        
        # MSA attention and transition
        self.msa_attention = MSAAttention(
            config.msa_channels, 
            config.num_heads, 
            config.dropout_rate
        )
        self.msa_transition = nn.Sequential(
            nn.Linear(config.msa_channels, 4 * config.msa_channels),
            nn.ReLU(),
            nn.Linear(4 * config.msa_channels, config.msa_channels),
            nn.Dropout(config.dropout_rate)
        )
        
        # Pair attention and transition
        self.pair_attention = PairAttention(
            config.pair_channels,
            config.msa_channels, 
            config.num_heads,
            config.dropout_rate
        )
        self.pair_transition = nn.Sequential(
            nn.Linear(config.pair_channels, 4 * config.pair_channels),
            nn.ReLU(), 
            nn.Linear(4 * config.pair_channels, config.pair_channels),
            nn.Dropout(config.dropout_rate)
        )
        
        # Layer normalizations
        self.msa_ln1 = nn.LayerNorm(config.msa_channels)
        self.msa_ln2 = nn.LayerNorm(config.msa_channels)
        self.pair_ln1 = nn.LayerNorm(config.pair_channels)
        self.pair_ln2 = nn.LayerNorm(config.pair_channels)
        
        # MSA to pair update
        self.msa_to_pair = nn.Linear(2 * config.msa_channels, config.pair_channels)
    
    def forward(
        self,
        msa: torch.Tensor,
        pair: torch.Tensor,
        msa_mask: Optional[torch.Tensor] = None,
        pair_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass through Evoformer block
        
        Args:
            msa: [batch, msa_depth, seq_len, msa_channels]
            pair: [batch, seq_len, seq_len, pair_channels]
            msa_mask: [batch, msa_depth, seq_len] or None
            pair_mask: [batch, seq_len, seq_len] or None
            
        Returns:
            Updated (msa, pair) representations
        """
        
        # MSA self-attention
        msa_residual = msa
        msa = self.msa_ln1(msa)
        msa = self.msa_attention(msa, msa_mask)
        msa = msa_residual + msa
        
        # MSA transition
        msa_residual = msa
        msa = self.msa_ln2(msa)
        msa = self.msa_transition(msa)
        msa = msa_residual + msa
        
        # Update pair representation from MSA
        batch, msa_depth, seq_len, msa_channels = msa.shape
        
        # Create outer product from first sequence (target)
        target_msa = msa[:, 0, :, :]  # [batch, seq_len, msa_channels]
        
        # Outer product for pair update
        target_i = target_msa.unsqueeze(2)  # [batch, seq_len, 1, msa_channels]
        target_j = target_msa.unsqueeze(1)  # [batch, 1, seq_len, msa_channels]
        
        pair_update = torch.cat([
            target_i.expand(-1, -1, seq_len, -1),
            target_j.expand(-1, seq_len, -1, -1)
        ], dim=-1)  # [batch, seq_len, seq_len, 2 * msa_channels]
        
        pair_update = self.msa_to_pair(pair_update)
        pair = pair + pair_update
        
        # Pair self-attention  
        pair_residual = pair
        pair = self.pair_ln1(pair)
        pair = self.pair_attention(pair, msa, pair_mask)
        pair = pair_residual + pair
        
        # Pair transition
        pair_residual = pair
        pair = self.pair_ln2(pair)
        pair = self.pair_transition(pair)
        pair = pair_residual + pair
        
        return msa, pair


class EvoformerMSAProcessor:
    """
    Evoformer architecture for MSA processing and co-evolution analysis.
    
    Capabilities:
    - Multiple Sequence Alignment processing with attention mechanisms
    - Co-evolutionary coupling extraction from MSA data
    - Pairwise residue interaction prediction
    - Conservation profile analysis
    - 512D Evoformer embeddings for downstream fusion
    """
    
    def __init__(self, config: Optional[EvoformerConfig] = None):
        self.config = config or EvoformerConfig()
        self.bsm_config = get_bsm_config()
        
        # Setup device
        if self.config.device == "auto":
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(self.config.device)
        
        # Build Evoformer model
        self.evoformer_model = self._build_evoformer_model()
        
        # Embedding projections
        self.msa_embedder = nn.Embedding(21, self.config.msa_channels)  # 20 amino acids + gap
        self.positional_embedder = nn.Embedding(self.config.max_sequence_length, self.config.msa_channels)
        
        # Final projection to 512D
        self.final_projection = nn.Sequential(
            nn.Linear(self.config.msa_channels + self.config.pair_channels, 512),
            nn.ReLU(),
            nn.Dropout(self.config.dropout_rate),
            nn.Linear(512, 512)
        )
        
        # Move to device
        self.evoformer_model.to(self.device)
        self.msa_embedder.to(self.device) 
        self.positional_embedder.to(self.device)
        self.final_projection.to(self.device)
        
        # Processing cache and stats
        self.processing_cache = {}
        self.processing_stats = {
            'msas_processed': 0,
            'total_processing_time': 0.0,
            'average_msa_time': 0.0
        }
        
        logger.info("Evoformer MSA Processor initialized - Dr. Yuan Chen implementation")
    
    def _build_evoformer_model(self) -> nn.ModuleList:
        """Build stack of Evoformer blocks"""
        
        blocks = nn.ModuleList([
            EvoformerBlock(self.config) 
            for _ in range(self.config.num_blocks)
        ])
        
        return blocks
    
    async def process_msa(self, sequence_id: str, msa_input: MSAInput) -> EvoformerOutput:
        """
        Process Multiple Sequence Alignment with Evoformer architecture.
        
        Args:
            sequence_id: Unique identifier for the MSA
            msa_input: MSAInput object with alignment data
            
        Returns:
            EvoformerOutput with co-evolution analysis
        """
        
        start_time = time.time()
        
        # Check cache
        cache_key = f"{sequence_id}_{hash(str(msa_input.aligned_sequences))}"
        if cache_key in self.processing_cache:
            logger.debug(f"Using cached Evoformer result for {sequence_id}")
            return self.processing_cache[cache_key]
        
        # Validate and preprocess MSA
        processed_msa = self._preprocess_msa(msa_input)
        
        # Convert to tokens
        msa_tokens, msa_mask = self._tokenize_msa(processed_msa)
        
        # Generate embeddings
        with torch.no_grad():
            msa_emb, pair_emb = await self._forward_evoformer(msa_tokens, msa_mask)
        
        # Compute co-evolution analysis
        coevolution_matrix = self._compute_coevolution_matrix(msa_emb, pair_emb)
        coupling_scores = self._compute_coupling_scores(pair_emb)
        conservation_profile = self._compute_conservation_profile(msa_emb)
        
        # Generate final 512D embedding
        evoformer_embedding = self._generate_final_embedding(msa_emb, pair_emb)
        
        # Calculate confidence
        confidence = self._calculate_confidence(msa_emb, pair_emb, msa_input)
        
        # Create output
        output = EvoformerOutput(
            sequence_id=sequence_id,
            msa_representation=msa_emb.cpu().numpy(),
            pair_representation=pair_emb.cpu().numpy(),
            coevolution_matrix=coevolution_matrix,
            coupling_scores=coupling_scores,
            conservation_profile=conservation_profile,
            evoformer_embedding=evoformer_embedding,
            processing_time=time.time() - start_time,
            confidence_score=confidence,
            metadata={
                'msa_depth': len(processed_msa['sequences']),
                'sequence_length': len(processed_msa['sequences'][0]),
                'num_evoformer_blocks': self.config.num_blocks,
                'model_config': self.config.__dict__
            }
        )
        
        # Cache and update stats
        self.processing_cache[cache_key] = output
        self._update_processing_stats(output)
        
        logger.info(f"Evoformer MSA processing completed for {sequence_id}")
        return output
    
    def _preprocess_msa(self, msa_input: MSAInput) -> Dict[str, Any]:
        """Preprocess MSA input for Evoformer"""
        
        sequences = [msa_input.target_sequence] + msa_input.aligned_sequences
        
        # Limit MSA depth
        if len(sequences) > self.config.max_msa_depth:
            # Keep target sequence and top aligned sequences
            sequences = sequences[:self.config.max_msa_depth]
            logger.info(f"Limited MSA depth to {self.config.max_msa_depth} sequences")
        
        # Limit sequence length
        max_len = self.config.max_sequence_length
        if any(len(seq) > max_len for seq in sequences):
            sequences = [seq[:max_len] for seq in sequences]
            logger.info(f"Truncated sequences to {max_len} residues")
        
        # Ensure all sequences have same length (pad with gaps if needed)
        target_length = len(sequences[0])
        for i in range(1, len(sequences)):
            if len(sequences[i]) < target_length:
                sequences[i] = sequences[i] + '-' * (target_length - len(sequences[i]))
            elif len(sequences[i]) > target_length:
                sequences[i] = sequences[i][:target_length]
        
        # Generate sequence weights if not provided
        weights = msa_input.sequence_weights
        if weights is None:
            weights = self._compute_sequence_weights(sequences)
        elif len(weights) > len(sequences):
            weights = weights[:len(sequences)]
        
        return {
            'sequences': sequences,
            'weights': weights,
            'target_length': target_length,
            'msa_depth': len(sequences)
        }
    
    def _compute_sequence_weights(self, sequences: List[str]) -> np.ndarray:
        """Compute sequence weights based on similarity"""
        
        # Simple weight computation based on uniqueness
        weights = np.ones(len(sequences))
        
        # Reduce weights for very similar sequences
        for i in range(len(sequences)):
            for j in range(i + 1, len(sequences)):
                similarity = self._sequence_similarity(sequences[i], sequences[j])
                if similarity > 0.9:  # Very similar sequences
                    weights[j] *= 0.5  # Reduce weight
        
        # Normalize weights
        weights = weights / np.sum(weights) * len(sequences)
        
        return weights
    
    def _sequence_similarity(self, seq1: str, seq2: str) -> float:
        """Calculate sequence similarity"""
        
        if len(seq1) != len(seq2):
            return 0.0
        
        matches = sum(1 for a, b in zip(seq1, seq2) if a == b and a != '-')
        total_positions = len(seq1) - seq1.count('-') - seq2.count('-')
        
        if total_positions == 0:
            return 0.0
        
        return matches / total_positions
    
    def _tokenize_msa(self, processed_msa: Dict[str, Any]) -> Tuple[torch.Tensor, torch.Tensor]:
        """Convert MSA sequences to tokens"""
        
        sequences = processed_msa['sequences']
        msa_depth = len(sequences)
        seq_length = processed_msa['target_length']
        
        # Amino acid to token mapping
        aa_to_token = {
            'A': 0, 'R': 1, 'N': 2, 'D': 3, 'C': 4, 'Q': 5, 'E': 6, 'G': 7,
            'H': 8, 'I': 9, 'L': 10, 'K': 11, 'M': 12, 'F': 13, 'P': 14,
            'S': 15, 'T': 16, 'W': 17, 'Y': 18, 'V': 19, '-': 20  # Gap token
        }
        
        # Convert sequences to tokens
        msa_tokens = torch.zeros((1, msa_depth, seq_length), dtype=torch.long, device=self.device)
        msa_mask = torch.ones((1, msa_depth, seq_length), dtype=torch.float, device=self.device)
        
        for i, sequence in enumerate(sequences):
            for j, aa in enumerate(sequence):
                token = aa_to_token.get(aa.upper(), 20)  # Default to gap
                msa_tokens[0, i, j] = token
                
                if aa == '-':
                    msa_mask[0, i, j] = 0.0  # Mask gaps
        
        return msa_tokens, msa_mask
    
    async def _forward_evoformer(
        self, 
        msa_tokens: torch.Tensor, 
        msa_mask: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Forward pass through Evoformer model"""
        
        batch, msa_depth, seq_length = msa_tokens.shape
        
        # Generate MSA embeddings
        msa_emb = self.msa_embedder(msa_tokens)  # [batch, msa_depth, seq_len, msa_channels]
        
        # Add positional embeddings
        positions = torch.arange(seq_length, device=self.device)
        pos_emb = self.positional_embedder(positions)  # [seq_len, msa_channels]
        pos_emb = pos_emb.unsqueeze(0).unsqueeze(0)  # [1, 1, seq_len, msa_channels]
        msa_emb = msa_emb + pos_emb
        
        # Initialize pair representation
        pair_emb = torch.zeros(
            (batch, seq_length, seq_length, self.config.pair_channels),
            device=self.device
        )
        
        # Create pair mask (symmetric, no self-pairs)
        pair_mask = torch.ones((batch, seq_length, seq_length), device=self.device)
        # Mask diagonal
        for i in range(seq_length):
            pair_mask[:, i, i] = 0.0
        
        # Forward through Evoformer blocks
        for block in self.evoformer_model:
            msa_emb, pair_emb = block(msa_emb, pair_emb, msa_mask, pair_mask)
        
        return msa_emb, pair_emb
    
    def _compute_coevolution_matrix(
        self, 
        msa_emb: torch.Tensor, 
        pair_emb: torch.Tensor
    ) -> np.ndarray:
        """Compute co-evolution matrix from representations"""
        
        batch, seq_length, _, pair_channels = pair_emb.shape
        
        # Extract pairwise interaction strengths
        # Use L2 norm of pair embeddings as interaction strength
        interaction_strength = torch.norm(pair_emb, dim=-1)  # [batch, seq_len, seq_len]
        
        # Symmetrize
        interaction_strength = (interaction_strength + interaction_strength.transpose(-2, -1)) / 2
        
        # Apply sigmoid to normalize to [0, 1]
        coevolution_matrix = torch.sigmoid(interaction_strength)
        
        return coevolution_matrix.squeeze(0).cpu().numpy()
    
    def _compute_coupling_scores(self, pair_emb: torch.Tensor) -> np.ndarray:
        """Compute coupling scores between residue pairs"""
        
        # Project pair embeddings to coupling scores
        coupling_proj = nn.Linear(self.config.pair_channels, 1).to(self.device)
        
        with torch.no_grad():
            coupling_scores = coupling_proj(pair_emb).squeeze(-1)  # [batch, seq_len, seq_len]
        
        # Apply tanh activation for bounded scores
        coupling_scores = torch.tanh(coupling_scores)
        
        return coupling_scores.squeeze(0).cpu().numpy()
    
    def _compute_conservation_profile(self, msa_emb: torch.Tensor) -> np.ndarray:
        """Compute conservation profile from MSA embeddings"""
        
        batch, msa_depth, seq_length, msa_channels = msa_emb.shape
        
        # Calculate variance across MSA depth for each position
        msa_variance = torch.var(msa_emb, dim=1)  # [batch, seq_len, msa_channels]
        
        # Average variance across channels
        position_variance = torch.mean(msa_variance, dim=-1)  # [batch, seq_len]
        
        # Conservation is inverse of variance
        conservation = 1.0 / (1.0 + position_variance)
        
        return conservation.squeeze(0).cpu().numpy()
    
    def _generate_final_embedding(
        self, 
        msa_emb: torch.Tensor, 
        pair_emb: torch.Tensor
    ) -> np.ndarray:
        """Generate final 512D embedding from MSA and pair representations"""
        
        batch, msa_depth, seq_length, msa_channels = msa_emb.shape
        
        # Pool MSA representation (use target sequence)
        target_msa = msa_emb[:, 0, :, :]  # [batch, seq_len, msa_channels]
        pooled_msa = torch.mean(target_msa, dim=1)  # [batch, msa_channels]
        
        # Pool pair representation  
        pair_diag = torch.diagonal(pair_emb, dim1=1, dim2=2)  # [batch, pair_channels, seq_len]
        pair_diag = pair_diag.transpose(1, 2)  # [batch, seq_len, pair_channels]
        pooled_pair = torch.mean(pair_diag, dim=1)  # [batch, pair_channels]
        
        # Concatenate and project to 512D
        combined = torch.cat([pooled_msa, pooled_pair], dim=-1)  # [batch, msa_channels + pair_channels]
        
        with torch.no_grad():
            final_embedding = self.final_projection(combined)  # [batch, 512]
        
        # L2 normalize
        final_embedding = F.normalize(final_embedding, dim=-1)
        
        return final_embedding.squeeze(0).cpu().numpy()
    
    def _calculate_confidence(
        self, 
        msa_emb: torch.Tensor, 
        pair_emb: torch.Tensor, 
        msa_input: MSAInput
    ) -> float:
        """Calculate confidence score for Evoformer processing"""
        
        confidence_factors = []
        
        # MSA depth factor (more sequences = higher confidence)
        msa_depth = msa_emb.shape[1]
        depth_confidence = min(msa_depth / 100.0, 1.0)  # Normalize to [0,1]
        confidence_factors.append(depth_confidence)
        
        # MSA quality factor (based on sequence diversity)
        sequences = [msa_input.target_sequence] + msa_input.aligned_sequences
        diversity_score = self._calculate_msa_diversity(sequences[:msa_depth])
        confidence_factors.append(diversity_score)
        
        # Representation quality (based on embedding magnitudes)
        msa_magnitude = torch.norm(msa_emb, dim=-1).mean()
        pair_magnitude = torch.norm(pair_emb, dim=-1).mean()
        
        magnitude_confidence = min((msa_magnitude + pair_magnitude) / 10.0, 1.0)
        confidence_factors.append(magnitude_confidence.item())
        
        # Attention pattern quality (based on pair representation structure)
        pair_entropy = self._calculate_pair_entropy(pair_emb)
        entropy_confidence = 1.0 - min(pair_entropy / 10.0, 1.0)  # Lower entropy = higher confidence
        confidence_factors.append(entropy_confidence)
        
        # Combined confidence
        overall_confidence = np.mean(confidence_factors)
        
        return float(np.clip(overall_confidence, 0.1, 0.99))
    
    def _calculate_msa_diversity(self, sequences: List[str]) -> float:
        """Calculate diversity score for MSA sequences"""
        
        if len(sequences) < 2:
            return 0.1
        
        # Calculate pairwise similarities
        similarities = []
        for i in range(len(sequences)):
            for j in range(i + 1, len(sequences)):
                sim = self._sequence_similarity(sequences[i], sequences[j])
                similarities.append(sim)
        
        if not similarities:
            return 0.1
        
        # Diversity is inverse of average similarity
        avg_similarity = np.mean(similarities)
        diversity = 1.0 - avg_similarity
        
        return max(diversity, 0.1)
    
    def _calculate_pair_entropy(self, pair_emb: torch.Tensor) -> float:
        """Calculate entropy of pair representation"""
        
        # Flatten pair embeddings
        pair_flat = pair_emb.flatten()
        
        # Compute histogram
        hist, _ = torch.histogram(pair_flat, bins=50)
        hist = hist.float()
        
        # Normalize to probabilities
        probs = hist / torch.sum(hist)
        
        # Calculate entropy
        entropy = -torch.sum(probs * torch.log(probs + 1e-8))
        
        return entropy.item()
    
    def _update_processing_stats(self, output: EvoformerOutput):
        """Update processing statistics"""
        
        self.processing_stats['msas_processed'] += 1
        self.processing_stats['total_processing_time'] += output.processing_time
        self.processing_stats['average_msa_time'] = (
            self.processing_stats['total_processing_time'] / 
            self.processing_stats['msas_processed']
        )
    
    async def create_msa_from_sequence(
        self, 
        target_sequence: str, 
        database_sequences: Optional[List[str]] = None
    ) -> MSAInput:
        """
        Create MSA from target sequence using sequence alignment.
        
        Args:
            target_sequence: Target protein sequence
            database_sequences: Optional list of sequences to align against
            
        Returns:
            MSAInput object with aligned sequences
        """
        
        if database_sequences is None:
            # Mock MSA generation - in practice would use HHblits, PSI-BLAST, etc.
            database_sequences = self._generate_mock_homologs(target_sequence)
        
        # Perform multiple sequence alignment
        aligned_sequences = []
        alignment_scores = []
        
        for db_seq in database_sequences:
            try:
                if not BIOTITE_AVAILABLE:
                    raise ImportError("biotite not available")
                # Simple pairwise alignment using biotite
                seq1 = seq.ProteinSequence(target_sequence)
                seq2 = seq.ProteinSequence(db_seq)
                
                # Perform alignment
                alignment = align.align_optimal(
                    seq1, seq2, 
                    align.SubstitutionMatrix.std_protein_matrix(),
                    gap_penalty=(-10, -1)
                )[0]
                
                # Extract aligned sequence
                aligned_seq = str(alignment[1])
                aligned_sequences.append(aligned_seq)
                
                # Calculate alignment score
                score = alignment.score / len(target_sequence)
                alignment_scores.append(score)
                
            except Exception as e:
                logger.warning(f"Failed to align sequence: {e}")
                continue
        
        # Sort by alignment score and take top sequences
        if aligned_sequences:
            sorted_indices = np.argsort(alignment_scores)[::-1]
            top_sequences = [aligned_sequences[i] for i in sorted_indices[:50]]  # Top 50
            top_scores = [alignment_scores[i] for i in sorted_indices[:50]]
        else:
            top_sequences = []
            top_scores = []
        
        return MSAInput(
            target_sequence=target_sequence,
            aligned_sequences=top_sequences,
            alignment_scores=np.array(top_scores)
        )
    
    def _generate_mock_homologs(self, target_sequence: str, num_homologs: int = 20) -> List[str]:
        """Generate mock homologous sequences for testing"""
        
        homologs = []
        
        for i in range(num_homologs):
            # Create sequence with some mutations
            mutated_seq = list(target_sequence)
            
            # Introduce random mutations
            mutation_rate = 0.1 + (i * 0.05)  # Increasing divergence
            num_mutations = int(len(target_sequence) * mutation_rate)
            
            amino_acids = 'ACDEFGHIKLMNPQRSTVWY'
            for _ in range(num_mutations):
                pos = np.random.randint(0, len(mutated_seq))
                mutated_seq[pos] = np.random.choice(list(amino_acids))
            
            homologs.append(''.join(mutated_seq))
        
        return homologs
    
    def get_processing_statistics(self) -> Dict[str, Any]:
        """Get comprehensive processing statistics"""
        
        stats = self.processing_stats.copy()
        
        # Add model configuration
        stats['model_config'] = {
            'num_blocks': self.config.num_blocks,
            'msa_channels': self.config.msa_channels,
            'pair_channels': self.config.pair_channels,
            'num_heads': self.config.num_heads,
            'device': str(self.device)
        }
        
        return stats
    
    def save_evoformer_output(self, output: EvoformerOutput, filepath: str):
        """Save Evoformer output to file"""
        
        # Prepare data for saving
        save_data = {
            'sequence_id': output.sequence_id,
            'msa_representation': output.msa_representation.tolist(),
            'pair_representation': output.pair_representation.tolist(),
            'coevolution_matrix': output.coevolution_matrix.tolist(),
            'coupling_scores': output.coupling_scores.tolist(),
            'conservation_profile': output.conservation_profile.tolist(),
            'evoformer_embedding': output.evoformer_embedding.tolist(),
            'processing_time': output.processing_time,
            'confidence_score': output.confidence_score,
            'metadata': output.metadata
        }
        
        # Save as JSON
        with open(filepath, 'w') as f:
            json.dump(save_data, f, indent=2)
        
        logger.info(f"Evoformer output saved to {filepath}")
    
    def clear_cache(self):
        """Clear processing cache"""
        
        cache_size = len(self.processing_cache)
        self.processing_cache.clear()
        
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        
        logger.info(f"Cleared Evoformer cache ({cache_size} entries)")
    
    def __del__(self):
        """Cleanup when processor is destroyed"""
        
        if hasattr(self, 'device') and self.device.type == "cuda":
            torch.cuda.empty_cache()
        
        logger.info("Evoformer MSA Processor cleaned up")
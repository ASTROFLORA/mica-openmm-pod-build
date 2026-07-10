"""
Tri-Modal Cross-Attention Fusion Model
Version: 1.0
Date: October 8, 2025
Author: Dr. Yuan Chen
"""
import torch
import torch.nn as nn

class TriModalCrossAttentionFuser(nn.Module):
    def __init__(self, seq_dim, struct_dim, dyn_dim, projection_dim, num_heads, mlp_dim):
        super().__init__()
        
        # Projection layers
        self.seq_proj = nn.Linear(seq_dim, projection_dim)
        self.struct_proj = nn.Linear(struct_dim, projection_dim)
        self.dyn_proj = nn .Linear(dyn_dim, projection_dim)
        
        # Cross-Attention blocks
        self.struct_attention = nn.MultiheadAttention(projection_dim, num_heads, batch_first=True)
        self.dyn_attention = nn.MultiheadAttention(projection_dim, num_heads, batch_first=True)
        
        # Layer Normalization
        self.norm1 = nn.LayerNorm(projection_dim)
        self.norm2 = nn.LayerNorm(projection_dim)
        self.norm3 = nn.LayerNorm(projection_dim)
        
        # Final MLP
        self.mlp = nn.Sequential(
            nn.Linear(projection_dim * 3, mlp_dim),
            nn.GELU(),
            nn.Linear(mlp_dim, projection_dim)
        )
        
        self.final_proj = nn.Linear(projection_dim, seq_dim + struct_dim + dyn_dim) # Or a different target dimension

    def forward(self, seq_emb, struct_emb, dyn_emb):
        # Project to common dimension
        seq_p = self.seq_proj(seq_emb)
        struct_p = self.struct_proj(struct_emb)
        dyn_p = self.dyn_proj(dyn_emb)
        
        # Ensure embeddings have a sequence length dimension (e.g., [batch, 1, dim])
        if len(seq_p.shape) == 2:
            seq_p = seq_p.unsqueeze(1)
            struct_p = struct_p.unsqueeze(1)
            dyn_p = dyn_p.unsqueeze(1)

        # 1. Sequence <-> Structure Attention
        struct_aware_seq, _ = self.struct_attention(query=seq_p, key=struct_p, value=struct_p)
        struct_aware_seq = self.norm1(seq_p + struct_aware_seq) # Add & Norm
        
        # 2. Result <-> Dynamics Attention
        dyn_aware_seq, _ = self.dyn_attention(query=struct_aware_seq, key=dyn_p, value=dyn_p)
        dyn_aware_seq = self.norm2(struct_aware_seq + dyn_aware_seq) # Add & Norm
        
        # 3. Final Fusion
        # Concatenate the original projected embeddings with the final context-aware embedding
        fused_emb = torch.cat([seq_p, dyn_aware_seq, struct_p], dim=-1)
        
        # Squeeze the sequence dimension if it was added
        if fused_emb.shape[1] == 1:
            fused_emb = fused_emb.squeeze(1)

        # Final MLP for deep fusion
        final_embedding = self.mlp(fused_emb)
        
        return final_embedding

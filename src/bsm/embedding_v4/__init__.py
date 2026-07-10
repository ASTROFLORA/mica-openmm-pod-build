"""
Advanced Unified Protein Embedding (A-UPE) Factory
Version: 1.0
Date: October 8, 2025
Author: Dr. Yuan Chen
"""
from .tri_modal_fusion import TriModalCrossAttentionFuser

def create_aupe_model(config):
    """
    Factory function to create the A-UPE model.
    """
    model = TriModalCrossAttentionFuser(
        seq_dim=config.get("seq_dim", 768),
        struct_dim=config.get("struct_dim", 512),
        dyn_dim=config.get("dyn_dim", 512),
        projection_dim=config.get("projection_dim", 512),
        num_heads=config.get("num_heads", 8),
        mlp_dim=config.get("mlp_dim", 2048)
    )
    return model

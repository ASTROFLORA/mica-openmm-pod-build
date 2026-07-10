"""
LMP Preset Definitions

Each preset controls which blocks are included in generated XML.
Presets are optimized for different consumers:
- PLMs: sequence + NeSy tokens (minimal)
- LLMs: semantic context (readable)
- MD pipelines: trajectory data (numerical)
- Archives: everything (complete)

Author: MICA Team
Date: 2026-01-20
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class LMPPreset:
    """Configuration for LMP generation."""
    
    name: str
    description: str
    
    # Block inclusion flags
    include_identity: bool = True
    include_nesy_grammar: bool = False
    include_semantics: bool = False
    include_geometry: bool = False
    include_features: bool = False
    include_knowledge_graph: bool = False
    include_trajectory_ifp: bool = False
    include_provenance: bool = True
    embed_ground_truth: bool = False
    
    # Size limits for IFP
    max_ifp_frames: int = 500
    ifp_stride: int = 1
    ifp_min_occupancy: float = 0.1

    # Structural enrichment flags (v4.1)
    include_alphafold: bool = False
    include_secondary_structure: bool = False
    include_structural_quality: bool = False
    include_network_annotation: bool = False
    include_structure_catalog: bool = False
    include_residue_statistics: bool = False
    include_pocket_sites: bool = False
    include_dynamic_statistics: bool = False
    include_membrane_topology: bool = False
    include_cell_context_hints: bool = False
    include_topology_hooks: bool = False
    include_contact_map: bool = False
    prefer_smic_static: bool = False
    alphafold_download_pdb: bool = False
    alphafold_download_pae: bool = False

    # Visual enrichment flags (v4.3) — AF2/PDB/FlatProt image URLs for FE protein card
    include_structural_visuals: bool = False
    flatprot_enabled: bool = False  # gated: requires flatprot CLI + DSSP in the environment

    # Multi-API enrichment flags (v4.2)
    include_string_interactions: bool = False
    include_opentargets: bool = False
    include_chembl_bioactivity: bool = False
    include_kegg_pathways: bool = False
    include_reactome_pathways: bool = False
    include_protein_atlas: bool = False
    include_go_enrichment: bool = False
    include_ensembl: bool = False
    include_hpo_phenotypes: bool = False
    include_gtex_expression: bool = False

    # SMIC / TrajectoryIFP options
    ifp_auto_ligand: bool = True
    ifp_auto_chain: bool = True
    ifp_detect_metals: bool = False
    ifp_receptor_sel: Optional[str] = None
    
    # Output options
    pretty_print: bool = True
    include_xml_declaration: bool = True


# ============================================================================
# PRESET REGISTRY
# ============================================================================

PRESET_REGISTRY: Dict[str, LMPPreset] = {
    
    "nesy-core": LMPPreset(
        name="nesy-core",
        description="Minimal: Identity + NeSy grammar for PLM tokenization",
        include_nesy_grammar=True,
        include_semantics=True,  # Needed as parent for NeSyGrammar
    ),
    
    "semantic": LMPPreset(
        name="semantic",
        description="Semantic context for LLM injection (keywords, comments, xrefs)",
        include_semantics=True,
        include_knowledge_graph=True,
    ),
    
    "structural": LMPPreset(
        name="structural",
        description="Full structural mode: AlphaFold + DSSP + quality metrics + network centrality + multi-API",
        include_geometry=True,
        include_features=True,
        include_knowledge_graph=True,
        include_alphafold=True,
        include_secondary_structure=True,
        include_structural_quality=True,
        include_network_annotation=True,
        include_structure_catalog=True,
        include_residue_statistics=True,
        include_pocket_sites=True,
        include_membrane_topology=True,
        include_cell_context_hints=True,
        include_topology_hooks=True,
        prefer_smic_static=True,
        include_string_interactions=True,
        include_opentargets=True,
        include_chembl_bioactivity=True,
        include_kegg_pathways=True,
        include_reactome_pathways=True,
        include_go_enrichment=True,
        alphafold_download_pdb=True,
        alphafold_download_pae=True,
        include_structural_visuals=True,
        flatprot_enabled=True,
    ),

    "v2-compat": LMPPreset(
        name="v2-compat",
        description="Back-compat preset approximating v2 outputs (no TrajectoryIFP)",
        include_nesy_grammar=True,
        include_semantics=True,
        include_geometry=True,
        include_features=True,
        include_knowledge_graph=False,
        include_trajectory_ifp=False,
        include_provenance=True,
        embed_ground_truth=False,
    ),
    
    "md-ifp": LMPPreset(
        name="md-ifp",
        description="MD trajectory IFP fingerprints for dynamics analysis",
        include_geometry=True,
        include_trajectory_ifp=True,
        include_dynamic_statistics=True,
        max_ifp_frames=1000,
        ifp_stride=1,
    ),
    
    "full": LMPPreset(
        name="full",
        description="Complete archive with all blocks including AlphaFold structural data and multi-API enrichment",
        include_nesy_grammar=True,
        include_semantics=True,
        include_geometry=True,
        include_features=True,
        include_knowledge_graph=True,
        include_trajectory_ifp=True,
        include_provenance=True,
        embed_ground_truth=True,
        include_alphafold=True,
        include_secondary_structure=True,
        include_structural_quality=True,
        include_network_annotation=True,
        include_structure_catalog=True,
        include_residue_statistics=True,
        include_pocket_sites=True,
        include_dynamic_statistics=True,
        include_membrane_topology=True,
        include_cell_context_hints=True,
        include_topology_hooks=True,
        prefer_smic_static=True,
        include_string_interactions=True,
        include_opentargets=True,
        include_chembl_bioactivity=True,
        include_kegg_pathways=True,
        include_reactome_pathways=True,
        include_protein_atlas=True,
        include_go_enrichment=True,
        include_ensembl=True,
        include_hpo_phenotypes=True,
        include_gtex_expression=True,
        alphafold_download_pdb=True,
        alphafold_download_pae=True,
        include_structural_visuals=True,
        flatprot_enabled=True,
        max_ifp_frames=500,
    ),
    
    # Specialized presets
    "plm-esm2": LMPPreset(
        name="plm-esm2",
        description="Optimized for ESM-2 fine-tuning (sequence + per-residue labels)",
        include_nesy_grammar=True,
        include_semantics=True,
        include_features=True,  # For per-residue labels
        embed_ground_truth=False,
    ),
    
    "plm-prott5": LMPPreset(
        name="plm-prott5",
        description="Optimized for ProtT5 fine-tuning",
        include_nesy_grammar=True,
        include_semantics=True,
        include_features=True,
    ),
    
    "llm-context": LMPPreset(
        name="llm-context",
        description="Rich context for LLM prompts (no raw data) + multi-API enrichment + visuals",
        include_semantics=True,
        include_knowledge_graph=True,
        include_provenance=True,
        include_string_interactions=True,
        include_opentargets=True,
        include_chembl_bioactivity=True,
        include_kegg_pathways=True,
        include_reactome_pathways=True,
        include_go_enrichment=True,
        include_structural_visuals=True,
        embed_ground_truth=False,
        pretty_print=True,
    ),
}


# ============================================================================
# ACCESSOR FUNCTIONS
# ============================================================================

def get_preset(name: str) -> LMPPreset:
    """
    Get preset by name.
    
    Args:
        name: Preset name (e.g., "nesy-core", "full")
        
    Returns:
        LMPPreset configuration
        
    Raises:
        ValueError: If preset name is unknown
    """
    if name not in PRESET_REGISTRY:
        valid = ", ".join(sorted(PRESET_REGISTRY.keys()))
        raise ValueError(f"Unknown preset '{name}'. Valid presets: {valid}")
    return PRESET_REGISTRY[name]


def list_presets() -> Dict[str, str]:
    """
    List all available presets with descriptions.
    
    Returns:
        Dict mapping preset name to description
    """
    return {name: preset.description for name, preset in PRESET_REGISTRY.items()}


def preset_for_consumer(consumer: str) -> LMPPreset:
    """
    Get recommended preset for a given consumer type.
    
    Args:
        consumer: Consumer type (e.g., "plm", "llm", "md", "esm2")
        
    Returns:
        Recommended LMPPreset
    """
    consumer_map = {
        # PLM variants
        "plm": "nesy-core",
        "esm2": "plm-esm2",
        "esm-2": "plm-esm2",
        "prott5": "plm-prott5",
        "prot-t5": "plm-prott5",
        "protbert": "nesy-core",
        
        # LLM variants
        "llm": "semantic",
        "gpt": "llm-context",
        "gpt-4": "llm-context",
        "claude": "llm-context",
        "gemini": "llm-context",
        "context": "llm-context",
        
        # MD/dynamics
        "md": "md-ifp",
        "dynamics": "md-ifp",
        "trajectory": "md-ifp",
        "ifp": "md-ifp",
        
        # Structural
        "structure": "structural",
        "pdb": "structural",
        "geometry": "structural",
        "alphafold": "structural",
        
        # Archive/complete
        "archive": "full",
        "complete": "full",
        "all": "full",
        "master": "full",
    }
    
    preset_name = consumer_map.get(consumer.lower(), "full")
    return PRESET_REGISTRY[preset_name]


def create_custom_preset(
    name: str,
    description: str,
    **kwargs,
) -> LMPPreset:
    """
    Create a custom preset with specified options.
    
    Args:
        name: Preset name
        description: Preset description
        **kwargs: Override any LMPPreset field
        
    Returns:
        New LMPPreset instance
    """
    return LMPPreset(name=name, description=description, **kwargs)

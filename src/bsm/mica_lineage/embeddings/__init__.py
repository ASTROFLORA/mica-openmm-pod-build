"""
Embeddings Module Init - Dr. Yuan Chen
=====================================

MICA-Lineage Embeddings Module Initialization.
Unified embeddings architecture for Protocolo Fénix Azteca.

Phase 3 Implementation: ESE Pipeline (6 weeks)
Phase 4 Implementation: PubMedBERT Integration (4 weeks)
Lead: Dr. Yuan Chen + Alex Rodriguez
"""

import logging
from typing import Dict, List, Optional, Union, Any, Tuple
import numpy as np

# Import all embedding components
from .esmc_processor import (
    ESMCProcessor,
    ESMCConfig,
    ESMCOutput,
    ProteinInput
)

from .evoformer_msa import (
    EvoformerMSAProcessor,
    EvoformerConfig,
    EvoformerOutput,
    MSAInput,
    MSAAttention,
    PairAttention,
    EvoformerBlock
)

from .ese_extractor import (
    ESEExtractor,
    ESEConfig,
    ESEOutput,
    StructuralInput,
    TrajectoryInput,
    StructuralFeatureExtractor,
    EvolutionarySignalExtractor,
    DynamicFeatureExtractor,
    SpectralAnalyzer
)

from .pubmedbert_fusion import (
    PubMedBERTFusionEngine,
    FusionConfig,
    FusionOutput,
    MultiModalInput,
    ProteinContext,
    CrossModalAttention,
    FusionTransformer,
    PubMedBERTProcessor
)

from .mudo_packaging import (
    MUDO,
    MUDOPackagingSystem,
    MUDOMetadata,
    MUDOEmbedding,
    MUDOAnalysis
)

logger = logging.getLogger(__name__)

# Module version
__version__ = "1.0.0"

# Export all classes and functions
__all__ = [
    # ESM-C Components
    'ESMCProcessor',
    'ESMCConfig', 
    'ESMCOutput',
    'ProteinInput',
    
    # Evoformer Components
    'EvoformerMSAProcessor',
    'EvoformerConfig',
    'EvoformerOutput', 
    'MSAInput',
    'MSAAttention',
    'PairAttention',
    'EvoformerBlock',
    
    # ESE Components
    'ESEExtractor',
    'ESEConfig',
    'ESEOutput',
    'StructuralInput',
    'TrajectoryInput',
    'StructuralFeatureExtractor',
    'EvolutionarySignalExtractor', 
    'DynamicFeatureExtractor',
    'SpectralAnalyzer',
    
    # PubMedBERT Fusion Components
    'PubMedBERTFusionEngine',
    'FusionConfig',
    'FusionOutput',
    'MultiModalInput',
    'ProteinContext',
    'CrossModalAttention',
    'FusionTransformer',
    'PubMedBERTProcessor',
    
    # M-UDO Components
    'MUDO',
    'MUDOPackagingSystem',
    'MUDOMetadata',
    'MUDOEmbedding',
    'MUDOAnalysis',
    
    # Unified Pipeline
    'UnifiedEmbeddingPipeline',
    'EmbeddingPipelineConfig',
    'PipelineOutput'
]


class EmbeddingPipelineConfig:
    """Configuration for unified embedding pipeline"""
    
    def __init__(
        self,
        # ESM-C Configuration
        esmc_model_name: str = "facebook/esm2_t36_3B_UR50D",
        esmc_batch_size: int = 1,
        esmc_max_sequence_length: int = 1024,
        
        # Evoformer Configuration
        evoformer_blocks: int = 8,
        evoformer_msa_depth: int = 512,
        evoformer_channels: int = 256,
        
        # ESE Configuration
        ese_embedding_dim: int = 416,
        ese_structural_features: int = 128,
        ese_evolutionary_features: int = 144,
        
        # PubMedBERT Configuration
        pubmedbert_model: str = "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext",
        fusion_layers: int = 4,
        
        # M-UDO Configuration
        mudo_format: str = "hdf5",
        mudo_compression: bool = True,
        
        # General Configuration
        device: str = "auto",
        cache_embeddings: bool = True,
        validate_outputs: bool = True
    ):
        
        # Store all configurations
        self.esmc_config = ESMCConfig(
            model_name=esmc_model_name,
            batch_size=esmc_batch_size,
            max_sequence_length=esmc_max_sequence_length,
            device=device
        )
        
        self.evoformer_config = EvoformerConfig(
            num_blocks=evoformer_blocks,
            max_msa_depth=evoformer_msa_depth,
            msa_channels=evoformer_channels,
            device=device
        )
        
        self.ese_config = ESEConfig(
            embedding_dimension=ese_embedding_dim,
            structural_features=ese_structural_features,
            evolutionary_features=ese_evolutionary_features,
            device=device
        )
        
        self.fusion_config = FusionConfig(
            pubmedbert_model=pubmedbert_model,
            num_fusion_layers=fusion_layers,
            device=device,
            cache_embeddings=cache_embeddings
        )
        
        # Pipeline settings
        self.mudo_format = mudo_format
        self.mudo_compression = mudo_compression
        self.cache_embeddings = cache_embeddings
        self.validate_outputs = validate_outputs


class PipelineOutput:
    """Output from unified embedding pipeline"""
    
    def __init__(
        self,
        sequence_id: str,
        m_udo: MUDO,
        processing_times: Dict[str, float],
        success_status: Dict[str, bool],
        error_messages: Dict[str, Optional[str]] = None
    ):
        self.sequence_id = sequence_id
        self.m_udo = m_udo
        self.processing_times = processing_times
        self.success_status = success_status
        self.error_messages = error_messages or {}
        
        # Calculate overall metrics
        self.total_processing_time = sum(processing_times.values())
        self.overall_success = all(success_status.values())
        self.success_rate = sum(success_status.values()) / len(success_status) if success_status else 0.0


class UnifiedEmbeddingPipeline:
    """
    Unified embedding pipeline for MICA-Lineage system.
    
    Orchestrates the complete embedding extraction workflow:
    1. ESM-C protein language model embeddings (2560D)
    2. Evoformer MSA co-evolution analysis (512D)
    3. ESE structural/evolutionary/dynamic features (416D)
    4. PubMedBERT contextual embeddings (768D)
    5. Multi-modal fusion (1280D = 768D + 512D)
    6. M-UDO packaging and storage
    
    Dr. Yuan Chen Implementation - Phase 3 & 4
    """
    
    def __init__(
        self, 
        config: Optional[EmbeddingPipelineConfig] = None,
        storage_path: Optional[str] = None
    ):
        
        self.config = config or EmbeddingPipelineConfig()
        
        # Initialize all processors
        logger.info("Initializing MICA-Lineage Unified Embedding Pipeline")
        
        try:
            self.esmc_processor = ESMCProcessor(self.config.esmc_config)
            logger.info("✓ ESM-C processor initialized")
        except Exception as e:
            logger.error(f"Failed to initialize ESM-C processor: {e}")
            self.esmc_processor = None
        
        try:
            self.evoformer_processor = EvoformerMSAProcessor(self.config.evoformer_config)
            logger.info("✓ Evoformer MSA processor initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Evoformer processor: {e}")
            self.evoformer_processor = None
        
        try:
            self.ese_extractor = ESEExtractor(self.config.ese_config)
            logger.info("✓ ESE extractor initialized")
        except Exception as e:
            logger.error(f"Failed to initialize ESE extractor: {e}")
            self.ese_extractor = None
        
        try:
            self.fusion_engine = PubMedBERTFusionEngine(self.config.fusion_config)
            logger.info("✓ PubMedBERT fusion engine initialized")
        except Exception as e:
            logger.error(f"Failed to initialize fusion engine: {e}")
            self.fusion_engine = None
        
        try:
            self.mudo_system = MUDOPackagingSystem(storage_path)
            logger.info("✓ M-UDO packaging system initialized")
        except Exception as e:
            logger.error(f"Failed to initialize M-UDO system: {e}")
            self.mudo_system = None
        
        # Pipeline statistics
        self.pipeline_stats = {
            'sequences_processed': 0,
            'total_processing_time': 0.0,
            'average_processing_time': 0.0,
            'success_rate': 0.0,
            'modality_success_rates': {
                'esmc': 0.0,
                'evoformer': 0.0,
                'ese': 0.0,
                'fusion': 0.0,
                'mudo': 0.0
            }
        }
        
        logger.info("MICA-Lineage Unified Embedding Pipeline ready - Dr. Yuan Chen implementation")
    
    async def process_protein(
        self,
        sequence_id: str,
        sequence: str,
        protein_context: Optional[ProteinContext] = None,
        structure_input: Optional[StructuralInput] = None,
        trajectory_input: Optional[TrajectoryInput] = None,
        msa_sequences: Optional[List[str]] = None,
        save_m_udo: bool = True
    ) -> PipelineOutput:
        """
        Process protein through complete embedding pipeline.
        
        Args:
            sequence_id: Unique protein identifier
            sequence: Protein amino acid sequence
            protein_context: Optional contextual information for PubMedBERT
            structure_input: Optional structural data for ESE
            trajectory_input: Optional trajectory data for ESE
            msa_sequences: Optional MSA sequences for Evoformer
            save_m_udo: Whether to save M-UDO to storage
            
        Returns:
            PipelineOutput with M-UDO and processing results
        """
        
        logger.info(f"Processing protein {sequence_id} through unified embedding pipeline")
        
        processing_times = {}
        success_status = {}
        error_messages = {}
        
        # Create M-UDO object
        m_udo = MUDO(sequence_id, sequence)
        
        # Add basic protein context to metadata
        if protein_context:
            m_udo.metadata.organism = protein_context.organism
            m_udo.metadata.protein_name = protein_context.protein_name
            m_udo.metadata.function_description = protein_context.function_description
        
        # 1. ESM-C Processing
        esmc_embedding = None
        if self.esmc_processor is not None:
            try:
                logger.info(f"[1/5] Processing ESM-C embeddings for {sequence_id}")
                start_time = time.time()
                
                protein_input = ProteinInput(sequence_id=sequence_id, sequence=sequence)
                esmc_output = await self.esmc_processor.process_protein(protein_input)
                esmc_embedding = esmc_output.final_embedding
                
                # Add to M-UDO
                m_udo.add_embedding(
                    'esmc',
                    esmc_embedding,
                    confidence_score=esmc_output.confidence_score,
                    processing_time=esmc_output.processing_time,
                    model_version=self.config.esmc_config.model_name
                )
                
                processing_times['esmc'] = time.time() - start_time
                success_status['esmc'] = True
                logger.info(f"✓ ESM-C processing completed ({processing_times['esmc']:.2f}s)")
                
            except Exception as e:
                error_msg = f"ESM-C processing failed: {e}"
                logger.error(error_msg)
                processing_times['esmc'] = 0.0
                success_status['esmc'] = False
                error_messages['esmc'] = error_msg
        else:
            logger.warning("ESM-C processor not available")
            processing_times['esmc'] = 0.0
            success_status['esmc'] = False
            error_messages['esmc'] = "ESM-C processor not initialized"
        
        # 2. Evoformer MSA Processing
        evoformer_embedding = None
        if self.evoformer_processor is not None:
            try:
                logger.info(f"[2/5] Processing Evoformer MSA for {sequence_id}")
                start_time = time.time()
                
                # Create or use provided MSA
                if msa_sequences:
                    msa_input = MSAInput(
                        target_sequence=sequence,
                        aligned_sequences=msa_sequences
                    )
                else:
                    # Generate MSA from sequence
                    msa_input = await self.evoformer_processor.create_msa_from_sequence(sequence)
                
                evoformer_output = await self.evoformer_processor.process_msa(sequence_id, msa_input)
                evoformer_embedding = evoformer_output.evoformer_embedding
                
                # Add to M-UDO
                m_udo.add_embedding(
                    'evoformer',
                    evoformer_embedding,
                    confidence_score=evoformer_output.confidence_score,
                    processing_time=evoformer_output.processing_time,
                    model_version=f"Evoformer-{self.config.evoformer_config.num_blocks}blocks"
                )
                
                # Add co-evolution analysis
                m_udo.add_analysis(
                    'coevolution',
                    {
                        'coevolution_matrix': evoformer_output.coevolution_matrix.tolist(),
                        'coupling_scores': evoformer_output.coupling_scores.tolist(),
                        'conservation_profile': evoformer_output.conservation_profile.tolist()
                    },
                    confidence_level=evoformer_output.confidence_score,
                    method_used="Evoformer MSA Attention"
                )
                
                processing_times['evoformer'] = time.time() - start_time
                success_status['evoformer'] = True
                logger.info(f"✓ Evoformer processing completed ({processing_times['evoformer']:.2f}s)")
                
            except Exception as e:
                error_msg = f"Evoformer processing failed: {e}"
                logger.error(error_msg)
                processing_times['evoformer'] = 0.0
                success_status['evoformer'] = False
                error_messages['evoformer'] = error_msg
        else:
            logger.warning("Evoformer processor not available")
            processing_times['evoformer'] = 0.0
            success_status['evoformer'] = False
            error_messages['evoformer'] = "Evoformer processor not initialized"
        
        # 3. ESE Extraction
        ese_embedding = None
        if self.ese_extractor is not None:
            try:
                logger.info(f"[3/5] Processing ESE features for {sequence_id}")
                start_time = time.time()
                
                # Prepare evolutionary data from Evoformer if available
                evolutionary_data = None
                if evoformer_embedding is not None and success_status.get('evoformer', False):
                    evoformer_output_for_ese = m_udo.get_analysis('coevolution')
                    if evoformer_output_for_ese:
                        evolutionary_data = {
                            'coevolution_matrix': np.array(evoformer_output_for_ese.results['coevolution_matrix']),
                            'conservation_profile': np.array(evoformer_output_for_ese.results['conservation_profile']),
                            'msa_depth': len(msa_sequences) if msa_sequences else 20
                        }
                
                ese_output = await self.ese_extractor.extract_ese_features(
                    sequence_id=sequence_id,
                    sequence=sequence,
                    structure_input=structure_input,
                    trajectory_input=trajectory_input,
                    evolutionary_data=evolutionary_data
                )
                ese_embedding = ese_output.ese_embedding
                
                # Add to M-UDO
                m_udo.add_embedding(
                    'ese',
                    ese_embedding,
                    confidence_score=ese_output.confidence_score,
                    processing_time=ese_output.processing_time,
                    model_version="ESE-v1.0"
                )
                
                # Add detailed ESE analyses
                m_udo.add_analysis(
                    'stability',
                    ese_output.stability_metrics,
                    confidence_level=ese_output.confidence_score,
                    method_used="ESE Stability Analysis"
                )
                
                m_udo.add_analysis(
                    'flexibility',
                    {'profile': ese_output.flexibility_profile.tolist()},
                    confidence_level=ese_output.confidence_score,
                    method_used="ESE Flexibility Profiling"
                )
                
                m_udo.add_analysis(
                    'interaction',
                    ese_output.interaction_network,
                    confidence_level=ese_output.confidence_score,
                    method_used="ESE Interaction Network Analysis"
                )
                
                m_udo.add_analysis(
                    'spectral',
                    ese_output.spectral_decomposition,
                    confidence_level=ese_output.confidence_score,
                    method_used="ESE Spectral Decomposition"
                )
                
                processing_times['ese'] = time.time() - start_time
                success_status['ese'] = True
                logger.info(f"✓ ESE processing completed ({processing_times['ese']:.2f}s)")
                
            except Exception as e:
                error_msg = f"ESE processing failed: {e}"
                logger.error(error_msg)
                processing_times['ese'] = 0.0
                success_status['ese'] = False
                error_messages['ese'] = error_msg
        else:
            logger.warning("ESE extractor not available")
            processing_times['ese'] = 0.0
            success_status['ese'] = False
            error_messages['ese'] = "ESE extractor not initialized"
        
        # 4. PubMedBERT Fusion
        fused_embedding = None
        if self.fusion_engine is not None:
            try:
                logger.info(f"[4/5] Processing PubMedBERT fusion for {sequence_id}")
                start_time = time.time()
                
                # Prepare multi-modal input
                embedding_quality = {}
                for modality in ['esmc', 'evoformer', 'ese']:
                    if success_status.get(modality, False):
                        embedding_obj = m_udo.get_embedding(modality)
                        embedding_quality[modality] = embedding_obj.confidence_score if embedding_obj else 0.5
                    else:
                        embedding_quality[modality] = 0.0
                
                multi_modal_input = MultiModalInput(
                    sequence_id=sequence_id,
                    sequence=sequence,
                    esmc_embedding=esmc_embedding,
                    evoformer_embedding=evoformer_embedding,
                    ese_embedding=ese_embedding,
                    protein_context=protein_context,
                    embedding_quality=embedding_quality
                )
                
                fusion_output = await self.fusion_engine.fuse_embeddings(multi_modal_input)
                
                # Add fused embeddings to M-UDO
                m_udo.add_embedding(
                    'fused',
                    fusion_output.fused_embedding,
                    confidence_score=fusion_output.confidence_score,
                    processing_time=fusion_output.processing_time,
                    model_version="Multi-Modal-Fusion-v1.0"
                )
                
                m_udo.add_embedding(
                    'structural_component',
                    fusion_output.structural_embedding,
                    confidence_score=fusion_output.confidence_score,
                    processing_time=0.0,
                    model_version="Fusion-Structural-Component"
                )
                
                m_udo.add_embedding(
                    'contextual_component',
                    fusion_output.contextual_embedding,
                    confidence_score=fusion_output.confidence_score,
                    processing_time=0.0,
                    model_version="Fusion-Contextual-Component"
                )
                
                # Add fusion analysis
                m_udo.add_analysis(
                    'fusion_attention',
                    {k: v.tolist() for k, v in fusion_output.attention_weights.items()},
                    confidence_level=fusion_output.confidence_score,
                    method_used="Cross-Modal Attention"
                )
                
                m_udo.add_analysis(
                    'modality_importance',
                    fusion_output.modality_importance,
                    confidence_level=fusion_output.confidence_score,
                    method_used="Modality Importance Prediction"
                )
                
                processing_times['fusion'] = time.time() - start_time
                success_status['fusion'] = True
                logger.info(f"✓ Fusion processing completed ({processing_times['fusion']:.2f}s)")
                
            except Exception as e:
                error_msg = f"Fusion processing failed: {e}"
                logger.error(error_msg)
                processing_times['fusion'] = 0.0
                success_status['fusion'] = False
                error_messages['fusion'] = error_msg
        else:
            logger.warning("Fusion engine not available")
            processing_times['fusion'] = 0.0
            success_status['fusion'] = False
            error_messages['fusion'] = "Fusion engine not initialized"
        
        # 5. M-UDO Packaging and Storage
        if save_m_udo and self.mudo_system is not None:
            try:
                logger.info(f"[5/5] Packaging and saving M-UDO for {sequence_id}")
                start_time = time.time()
                
                # Update M-UDO metadata
                m_udo.metadata.processing_pipeline = list(processing_times.keys())
                m_udo.metadata.fenix_azteca_processed = True
                
                # Calculate final quality score
                final_quality = m_udo.calculate_quality_score()
                
                # Save M-UDO
                mudo_path = self.mudo_system.save_m_udo(
                    m_udo,
                    format_type=self.config.mudo_format,
                    compress=self.config.mudo_compression
                )
                
                processing_times['mudo'] = time.time() - start_time
                success_status['mudo'] = True
                logger.info(f"✓ M-UDO saved to {mudo_path} ({processing_times['mudo']:.2f}s)")
                
            except Exception as e:
                error_msg = f"M-UDO packaging failed: {e}"
                logger.error(error_msg)
                processing_times['mudo'] = 0.0
                success_status['mudo'] = False
                error_messages['mudo'] = error_msg
        else:
            if not save_m_udo:
                logger.info("M-UDO saving skipped (save_m_udo=False)")
            else:
                logger.warning("M-UDO system not available")
            processing_times['mudo'] = 0.0
            success_status['mudo'] = True if not save_m_udo else False
            if save_m_udo and self.mudo_system is None:
                error_messages['mudo'] = "M-UDO system not initialized"
        
        # Update pipeline statistics
        self._update_pipeline_stats(processing_times, success_status)
        
        # Create pipeline output
        output = PipelineOutput(
            sequence_id=sequence_id,
            m_udo=m_udo,
            processing_times=processing_times,
            success_status=success_status,
            error_messages=error_messages
        )
        
        logger.info(
            f"Pipeline processing completed for {sequence_id}: "
            f"success_rate={output.success_rate:.1%}, "
            f"total_time={output.total_processing_time:.2f}s"
        )
        
        return output
    
    def _update_pipeline_stats(
        self, 
        processing_times: Dict[str, float], 
        success_status: Dict[str, bool]
    ):
        """Update pipeline statistics"""
        
        self.pipeline_stats['sequences_processed'] += 1
        
        total_time = sum(processing_times.values())
        self.pipeline_stats['total_processing_time'] += total_time
        self.pipeline_stats['average_processing_time'] = (
            self.pipeline_stats['total_processing_time'] / 
            self.pipeline_stats['sequences_processed']
        )
        
        # Update success rates
        total_success = sum(success_status.values())
        current_success_rate = total_success / len(success_status) if success_status else 0.0
        
        # Moving average of success rate
        alpha = 0.1  # Smoothing factor
        self.pipeline_stats['success_rate'] = (
            alpha * current_success_rate + 
            (1 - alpha) * self.pipeline_stats['success_rate']
        )
        
        # Update modality-specific success rates
        for modality in self.pipeline_stats['modality_success_rates']:
            if modality in success_status:
                current_modality_success = 1.0 if success_status[modality] else 0.0
                self.pipeline_stats['modality_success_rates'][modality] = (
                    alpha * current_modality_success + 
                    (1 - alpha) * self.pipeline_stats['modality_success_rates'][modality]
                )
    
    def get_pipeline_statistics(self) -> Dict[str, Any]:
        """Get comprehensive pipeline statistics"""
        
        stats = self.pipeline_stats.copy()
        
        # Add component statistics
        if self.esmc_processor:
            stats['esmc_stats'] = self.esmc_processor.get_processing_statistics()
        
        if self.evoformer_processor:
            stats['evoformer_stats'] = self.evoformer_processor.get_processing_statistics()
        
        if self.ese_extractor:
            stats['ese_stats'] = self.ese_extractor.get_extraction_statistics()
        
        if self.fusion_engine:
            stats['fusion_stats'] = self.fusion_engine.get_processing_statistics()
        
        if self.mudo_system:
            stats['mudo_stats'] = self.mudo_system.get_storage_statistics()
        
        # Add configuration
        stats['pipeline_config'] = {
            'esmc_model': self.config.esmc_config.model_name,
            'evoformer_blocks': self.config.evoformer_config.num_blocks,
            'ese_dimension': self.config.ese_config.embedding_dimension,
            'fusion_layers': self.config.fusion_config.num_fusion_layers,
            'mudo_format': self.config.mudo_format
        }
        
        return stats
    
    def clear_all_caches(self):
        """Clear all processor caches"""
        
        logger.info("Clearing all embedding processor caches")
        
        if self.esmc_processor:
            self.esmc_processor.clear_cache()
        
        if self.evoformer_processor:
            self.evoformer_processor.clear_cache()
        
        if self.ese_extractor:
            self.ese_extractor.clear_cache()
        
        if self.fusion_engine:
            self.fusion_engine.clear_cache()
        
        logger.info("All caches cleared")
    
    def __del__(self):
        """Cleanup when pipeline is destroyed"""
        
        logger.info("MICA-Lineage Unified Embedding Pipeline cleanup")


# Convenience functions for quick access
def create_pipeline(
    storage_path: Optional[str] = None,
    **config_kwargs
) -> UnifiedEmbeddingPipeline:
    """Create unified embedding pipeline with custom configuration"""
    
    config = EmbeddingPipelineConfig(**config_kwargs)
    return UnifiedEmbeddingPipeline(config, storage_path)


async def process_single_protein(
    sequence_id: str,
    sequence: str,
    pipeline: Optional[UnifiedEmbeddingPipeline] = None,
    **kwargs
) -> PipelineOutput:
    """Process a single protein through the pipeline"""
    
    if pipeline is None:
        pipeline = create_pipeline()
    
    return await pipeline.process_protein(sequence_id, sequence, **kwargs)


def load_m_udo_from_storage(
    sequence_id_or_path: str,
    storage_path: Optional[str] = None
) -> MUDO:
    """Load M-UDO from storage"""
    
    packaging_system = MUDOPackagingSystem(storage_path)
    return packaging_system.load_m_udo(sequence_id_or_path)


# Module initialization
logger.info("MICA-Lineage Embeddings Module initialized - Dr. Yuan Chen implementation")
logger.info(f"Available components: {len(__all__)} classes and functions")
logger.info("Protocolo Fénix Azteca - Phase 3 & 4 Implementation Ready")
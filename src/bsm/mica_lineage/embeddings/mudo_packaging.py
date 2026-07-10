"""
M-UDO Packaging System - Dr. Yuan Chen
=====================================

Multi-modal Unified Data Object packaging for MICA-Lineage embeddings.
Standardized format for embedding storage, retrieval, and integration.

Phase 3 Implementation: ESE Pipeline (6 weeks) / Phase 4: PubMedBERT Integration
Lead: Dr. Yuan Chen + Alex Rodriguez
"""

import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import numpy as np
import json
import h5py
import pickle
import gzip
import hashlib
from pathlib import Path
import uuid
import time

from bsm.config import get_bsm_config

logger = logging.getLogger(__name__)


@dataclass
class MUDOMetadata:
    """Metadata for M-UDO objects"""
    # Core identifiers
    m_udo_id: str  # Unique M-UDO identifier
    sequence_id: str  # Original sequence identifier
    budo_id: Optional[str] = None  # BUDO protocol identifier
    
    # Versioning and provenance
    version: str = "1.0.0"
    created_at: datetime = None
    updated_at: datetime = None
    created_by: str = "Dr. Yuan Chen - MICA-Lineage"
    
    # Data lineage
    source_modalities: List[str] = None  # ['esmc', 'evoformer', 'ese', 'pubmedbert']
    processing_pipeline: List[str] = None  # Processing steps
    parent_m_udos: List[str] = None  # Parent M-UDO IDs for derived objects
    
    # Quality and validation
    quality_score: float = 1.0
    validation_status: str = "pending"  # pending, validated, failed
    data_integrity_hash: str = ""
    
    # Biological context
    organism: Optional[str] = None
    protein_name: Optional[str] = None
    function_description: Optional[str] = None
    sequence_length: Optional[int] = None
    
    # Technical metadata
    compression_type: str = "gzip"  # gzip, lz4, none
    storage_format: str = "hdf5"  # hdf5, json, pickle
    size_bytes: int = 0
    checksum: str = ""
    
    # MICA-specific fields
    archaeoproteomics_compatible: bool = False
    degradation_simulation: bool = False
    fenix_azteca_processed: bool = False


@dataclass
class MUDOEmbedding:
    """Standardized embedding structure within M-UDO"""
    embedding_type: str  # 'esmc', 'evoformer', 'ese', 'pubmedbert', 'fused'
    embedding_vector: np.ndarray
    dimension: int
    
    # Embedding-specific metadata
    confidence_score: float = 1.0
    processing_time: float = 0.0
    model_version: str = ""
    normalization_applied: bool = False
    
    # Quality metrics
    magnitude: float = 0.0
    sparsity: float = 0.0  # Fraction of zero/near-zero elements
    entropy: float = 0.0  # Information entropy
    
    # Provenance
    created_at: datetime = None
    processing_parameters: Dict[str, Any] = None


@dataclass
class MUDOAnalysis:
    """Analysis results within M-UDO"""
    analysis_type: str  # 'stability', 'flexibility', 'interaction', 'spectral'
    results: Dict[str, Any]
    
    # Analysis metadata
    confidence_level: float = 1.0
    statistical_significance: Optional[float] = None
    method_used: str = ""
    parameters: Dict[str, Any] = None
    
    # Temporal information
    analysis_timestamp: datetime = None
    analysis_duration: float = 0.0


class MUDO:
    """
    Multi-modal Unified Data Object for MICA-Lineage embeddings.
    
    Standardized container for:
    - ESM-C embeddings (2560D)
    - Evoformer embeddings (512D) 
    - ESE embeddings (416D)
    - PubMedBERT embeddings (768D)
    - Fused embeddings (1280D)
    - Analysis results and metadata
    """
    
    def __init__(
        self,
        sequence_id: str,
        sequence: str,
        metadata: Optional[MUDOMetadata] = None
    ):
        # Generate unique M-UDO ID
        self.m_udo_id = self._generate_m_udo_id(sequence_id)
        
        # Core data
        self.sequence_id = sequence_id
        self.sequence = sequence
        
        # Initialize metadata
        if metadata is None:
            metadata = MUDOMetadata(
                m_udo_id=self.m_udo_id,
                sequence_id=sequence_id,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                sequence_length=len(sequence)
            )
        self.metadata = metadata
        
        # Embedding storage
        self.embeddings: Dict[str, MUDOEmbedding] = {}
        
        # Analysis storage
        self.analyses: Dict[str, MUDOAnalysis] = {}
        
        # Raw data references (for large trajectory/structure files)
        self.data_references: Dict[str, str] = {}
        
        # Validation tracking
        self._validation_results: Dict[str, Any] = {}
        self._is_validated: bool = False
        
        logger.debug(f"Created M-UDO {self.m_udo_id} for sequence {sequence_id}")
    
    def _generate_m_udo_id(self, sequence_id: str) -> str:
        """Generate unique M-UDO identifier"""
        
        # Combine sequence ID with timestamp and random component
        timestamp = datetime.now(timezone.utc).isoformat()
        unique_component = str(uuid.uuid4())[:8]
        
        # Create hash-based ID
        id_string = f"{sequence_id}_{timestamp}_{unique_component}"
        id_hash = hashlib.sha256(id_string.encode()).hexdigest()[:16]
        
        # Format as M-UDO ID
        m_udo_id = f"MUDO_{id_hash.upper()}"
        
        return m_udo_id
    
    def add_embedding(
        self,
        embedding_type: str,
        embedding_vector: np.ndarray,
        **kwargs
    ) -> str:
        """
        Add embedding to M-UDO.
        
        Args:
            embedding_type: Type of embedding ('esmc', 'evoformer', 'ese', 'pubmedbert', 'fused')
            embedding_vector: Embedding vector as numpy array
            **kwargs: Additional embedding metadata
            
        Returns:
            Embedding ID within M-UDO
        """
        
        # Validate embedding
        self._validate_embedding(embedding_type, embedding_vector)
        
        # Calculate embedding metrics
        magnitude = float(np.linalg.norm(embedding_vector))
        sparsity = float(np.sum(np.abs(embedding_vector) < 1e-8) / len(embedding_vector))
        
        # Calculate entropy
        # Normalize to probabilities for entropy calculation
        abs_values = np.abs(embedding_vector)
        probabilities = abs_values / (np.sum(abs_values) + 1e-10)
        entropy = float(-np.sum(probabilities * np.log(probabilities + 1e-10)))
        
        # Create embedding object
        embedding = MUDOEmbedding(
            embedding_type=embedding_type,
            embedding_vector=embedding_vector.copy(),
            dimension=len(embedding_vector),
            magnitude=magnitude,
            sparsity=sparsity,
            entropy=entropy,
            created_at=datetime.now(timezone.utc),
            **kwargs
        )
        
        # Store embedding
        self.embeddings[embedding_type] = embedding
        
        # Update metadata
        if self.metadata.source_modalities is None:
            self.metadata.source_modalities = []
        
        if embedding_type not in self.metadata.source_modalities:
            self.metadata.source_modalities.append(embedding_type)
        
        self.metadata.updated_at = datetime.now(timezone.utc)
        
        # Update data integrity
        self._update_data_integrity()
        
        logger.info(f"Added {embedding_type} embedding ({len(embedding_vector)}D) to M-UDO {self.m_udo_id}")
        
        return f"{self.m_udo_id}_{embedding_type}"
    
    def _validate_embedding(self, embedding_type: str, embedding_vector: np.ndarray):
        """Validate embedding data"""
        
        # Check embedding type
        valid_types = ['esmc', 'evoformer', 'ese', 'pubmedbert', 'fused']
        if embedding_type not in valid_types:
            raise ValueError(f"Invalid embedding type: {embedding_type}. Must be one of {valid_types}")
        
        # Check vector properties
        if not isinstance(embedding_vector, np.ndarray):
            raise TypeError("Embedding vector must be numpy array")
        
        if embedding_vector.ndim != 1:
            raise ValueError("Embedding vector must be 1-dimensional")
        
        if len(embedding_vector) == 0:
            raise ValueError("Embedding vector cannot be empty")
        
        # Check for invalid values
        if np.any(np.isnan(embedding_vector)):
            raise ValueError("Embedding vector contains NaN values")
        
        if np.any(np.isinf(embedding_vector)):
            raise ValueError("Embedding vector contains infinite values")
        
        # Dimension validation
        expected_dims = {
            'esmc': 2560,
            'evoformer': 512,
            'ese': 416,
            'pubmedbert': 768,
            'fused': 1280
        }
        
        expected_dim = expected_dims.get(embedding_type)
        if expected_dim and len(embedding_vector) != expected_dim:
            logger.warning(
                f"Embedding dimension mismatch for {embedding_type}: "
                f"expected {expected_dim}, got {len(embedding_vector)}"
            )
    
    def add_analysis(
        self,
        analysis_type: str,
        results: Dict[str, Any],
        **kwargs
    ) -> str:
        """
        Add analysis results to M-UDO.
        
        Args:
            analysis_type: Type of analysis ('stability', 'flexibility', 'interaction', etc.)
            results: Analysis results dictionary
            **kwargs: Additional analysis metadata
            
        Returns:
            Analysis ID within M-UDO
        """
        
        # Create analysis object
        analysis = MUDOAnalysis(
            analysis_type=analysis_type,
            results=results.copy(),
            analysis_timestamp=datetime.now(timezone.utc),
            **kwargs
        )
        
        # Store analysis
        analysis_key = f"{analysis_type}_{len(self.analyses)}"
        self.analyses[analysis_key] = analysis
        
        # Update metadata
        self.metadata.updated_at = datetime.now(timezone.utc)
        
        logger.info(f"Added {analysis_type} analysis to M-UDO {self.m_udo_id}")
        
        return f"{self.m_udo_id}_{analysis_key}"
    
    def get_embedding(self, embedding_type: str) -> Optional[MUDOEmbedding]:
        """Get embedding by type"""
        return self.embeddings.get(embedding_type)
    
    def get_embedding_vector(self, embedding_type: str) -> Optional[np.ndarray]:
        """Get embedding vector by type"""
        embedding = self.embeddings.get(embedding_type)
        return embedding.embedding_vector if embedding else None
    
    def get_analysis(self, analysis_type: str) -> Optional[MUDOAnalysis]:
        """Get analysis by type"""
        
        # Look for exact match first
        for key, analysis in self.analyses.items():
            if analysis.analysis_type == analysis_type:
                return analysis
        
        # Look for partial match
        for key, analysis in self.analyses.items():
            if analysis_type in key:
                return analysis
        
        return None
    
    def list_embeddings(self) -> List[str]:
        """List available embedding types"""
        return list(self.embeddings.keys())
    
    def list_analyses(self) -> List[str]:
        """List available analysis types"""
        return [analysis.analysis_type for analysis in self.analyses.values()]
    
    def _update_data_integrity(self):
        """Update data integrity hash"""
        
        # Collect all embedding vectors
        all_vectors = []
        for embedding in self.embeddings.values():
            all_vectors.append(embedding.embedding_vector.tobytes())
        
        # Create combined hash
        combined_data = b"".join(all_vectors)
        integrity_hash = hashlib.sha256(combined_data).hexdigest()
        
        self.metadata.data_integrity_hash = integrity_hash
    
    def validate_integrity(self) -> bool:
        """Validate data integrity"""
        
        # Recalculate hash
        original_hash = self.metadata.data_integrity_hash
        self._update_data_integrity()
        current_hash = self.metadata.data_integrity_hash
        
        # Restore original hash
        self.metadata.data_integrity_hash = original_hash
        
        # Check if hashes match
        integrity_valid = (original_hash == current_hash)
        
        if integrity_valid:
            logger.debug(f"M-UDO {self.m_udo_id} integrity validation passed")
        else:
            logger.error(f"M-UDO {self.m_udo_id} integrity validation failed")
        
        return integrity_valid
    
    def calculate_quality_score(self) -> float:
        """Calculate overall quality score for M-UDO"""
        
        quality_factors = []
        
        # Data completeness (number of embeddings)
        max_embeddings = 5  # esmc, evoformer, ese, pubmedbert, fused
        completeness_score = len(self.embeddings) / max_embeddings
        quality_factors.append(completeness_score)
        
        # Embedding quality (average confidence scores)
        if self.embeddings:
            confidence_scores = [emb.confidence_score for emb in self.embeddings.values()]
            avg_confidence = np.mean(confidence_scores)
            quality_factors.append(avg_confidence)
        
        # Data integrity
        integrity_score = 1.0 if self.validate_integrity() else 0.0
        quality_factors.append(integrity_score)
        
        # Analysis richness
        analysis_score = min(len(self.analyses) / 4.0, 1.0)  # Up to 4 analysis types
        quality_factors.append(analysis_score)
        
        # Overall quality
        overall_quality = np.mean(quality_factors)
        
        # Update metadata
        self.metadata.quality_score = overall_quality
        
        return overall_quality
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert M-UDO to dictionary representation"""
        
        # Convert embeddings
        embeddings_dict = {}
        for embedding_type, embedding in self.embeddings.items():
            embeddings_dict[embedding_type] = {
                'embedding_type': embedding.embedding_type,
                'embedding_vector': embedding.embedding_vector.tolist(),
                'dimension': embedding.dimension,
                'confidence_score': embedding.confidence_score,
                'processing_time': embedding.processing_time,
                'model_version': embedding.model_version,
                'normalization_applied': embedding.normalization_applied,
                'magnitude': embedding.magnitude,
                'sparsity': embedding.sparsity,
                'entropy': embedding.entropy,
                'created_at': embedding.created_at.isoformat() if embedding.created_at else None,
                'processing_parameters': embedding.processing_parameters
            }
        
        # Convert analyses
        analyses_dict = {}
        for analysis_key, analysis in self.analyses.items():
            analyses_dict[analysis_key] = {
                'analysis_type': analysis.analysis_type,
                'results': analysis.results,
                'confidence_level': analysis.confidence_level,
                'statistical_significance': analysis.statistical_significance,
                'method_used': analysis.method_used,
                'parameters': analysis.parameters,
                'analysis_timestamp': analysis.analysis_timestamp.isoformat() if analysis.analysis_timestamp else None,
                'analysis_duration': analysis.analysis_duration
            }
        
        # Convert metadata
        metadata_dict = asdict(self.metadata)
        if metadata_dict.get('created_at'):
            metadata_dict['created_at'] = self.metadata.created_at.isoformat()
        if metadata_dict.get('updated_at'):
            metadata_dict['updated_at'] = self.metadata.updated_at.isoformat()
        
        return {
            'm_udo_id': self.m_udo_id,
            'sequence_id': self.sequence_id,
            'sequence': self.sequence,
            'metadata': metadata_dict,
            'embeddings': embeddings_dict,
            'analyses': analyses_dict,
            'data_references': self.data_references,
            'validation_results': self._validation_results,
            'is_validated': self._is_validated
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MUDO':
        """Create M-UDO from dictionary representation"""
        
        # Create basic M-UDO
        m_udo = cls(
            sequence_id=data['sequence_id'],
            sequence=data['sequence']
        )
        
        # Restore M-UDO ID
        m_udo.m_udo_id = data['m_udo_id']
        
        # Restore metadata
        metadata_dict = data['metadata'].copy()
        if metadata_dict.get('created_at'):
            metadata_dict['created_at'] = datetime.fromisoformat(metadata_dict['created_at'])
        if metadata_dict.get('updated_at'):
            metadata_dict['updated_at'] = datetime.fromisoformat(metadata_dict['updated_at'])
        
        m_udo.metadata = MUDOMetadata(**metadata_dict)
        
        # Restore embeddings
        for embedding_type, embedding_data in data['embeddings'].items():
            embedding = MUDOEmbedding(
                embedding_type=embedding_data['embedding_type'],
                embedding_vector=np.array(embedding_data['embedding_vector']),
                dimension=embedding_data['dimension'],
                confidence_score=embedding_data.get('confidence_score', 1.0),
                processing_time=embedding_data.get('processing_time', 0.0),
                model_version=embedding_data.get('model_version', ''),
                normalization_applied=embedding_data.get('normalization_applied', False),
                magnitude=embedding_data.get('magnitude', 0.0),
                sparsity=embedding_data.get('sparsity', 0.0),
                entropy=embedding_data.get('entropy', 0.0),
                created_at=datetime.fromisoformat(embedding_data['created_at']) if embedding_data.get('created_at') else None,
                processing_parameters=embedding_data.get('processing_parameters')
            )
            m_udo.embeddings[embedding_type] = embedding
        
        # Restore analyses
        for analysis_key, analysis_data in data['analyses'].items():
            analysis = MUDOAnalysis(
                analysis_type=analysis_data['analysis_type'],
                results=analysis_data['results'],
                confidence_level=analysis_data.get('confidence_level', 1.0),
                statistical_significance=analysis_data.get('statistical_significance'),
                method_used=analysis_data.get('method_used', ''),
                parameters=analysis_data.get('parameters'),
                analysis_timestamp=datetime.fromisoformat(analysis_data['analysis_timestamp']) if analysis_data.get('analysis_timestamp') else None,
                analysis_duration=analysis_data.get('analysis_duration', 0.0)
            )
            m_udo.analyses[analysis_key] = analysis
        
        # Restore other attributes
        m_udo.data_references = data.get('data_references', {})
        m_udo._validation_results = data.get('validation_results', {})
        m_udo._is_validated = data.get('is_validated', False)
        
        return m_udo


class MUDOPackagingSystem:
    """
    Packaging system for M-UDO objects with multiple storage formats.
    
    Supports:
    - JSON format for metadata and small embeddings
    - HDF5 format for large numerical data
    - Compression for storage efficiency
    - Validation and integrity checking
    """
    
    def __init__(self, base_storage_path: Optional[str] = None):
        self.bsm_config = get_bsm_config()
        
        # Set base storage path
        if base_storage_path:
            self.base_path = Path(base_storage_path)
        else:
            # Use BSM configuration or default
            default_path = Path("data") / "m_udos"
            self.base_path = Path(self.bsm_config.get('m_udo_storage_path', default_path))
        
        # Ensure storage directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)
        
        # Storage statistics
        self.storage_stats = {
            'objects_stored': 0,
            'objects_loaded': 0,
            'total_size_bytes': 0,
            'compression_ratio': 0.0
        }
        
        logger.info(f"M-UDO Packaging System initialized - storage path: {self.base_path}")
    
    def save_m_udo(
        self,
        m_udo: MUDO,
        format_type: str = "hdf5",
        compress: bool = True,
        custom_path: Optional[str] = None
    ) -> str:
        """
        Save M-UDO to storage.
        
        Args:
            m_udo: M-UDO object to save
            format_type: Storage format ('hdf5', 'json', 'pickle')
            compress: Whether to apply compression
            custom_path: Custom storage path
            
        Returns:
            Path to saved file
        """
        
        start_time = time.time()
        
        # Determine file path
        if custom_path:
            filepath = Path(custom_path)
        else:
            filename = f"{m_udo.m_udo_id}.{format_type}"
            if compress:
                filename += ".gz"
            filepath = self.base_path / filename
        
        # Ensure parent directory exists
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # Update metadata before saving
        m_udo.metadata.storage_format = format_type
        m_udo.metadata.compression_type = "gzip" if compress else "none"
        m_udo.calculate_quality_score()  # Update quality score
        
        # Save based on format type
        try:
            if format_type == "hdf5":
                self._save_hdf5(m_udo, filepath, compress)
            elif format_type == "json":
                self._save_json(m_udo, filepath, compress)
            elif format_type == "pickle":
                self._save_pickle(m_udo, filepath, compress)
            else:
                raise ValueError(f"Unsupported format type: {format_type}")
            
            # Calculate file size
            file_size = filepath.stat().st_size
            
            # Update M-UDO metadata
            m_udo.metadata.size_bytes = file_size
            m_udo.metadata.checksum = self._calculate_file_checksum(filepath)
            
            # Update statistics
            self._update_storage_stats(file_size, time.time() - start_time)
            
            logger.info(f"Saved M-UDO {m_udo.m_udo_id} to {filepath} ({file_size} bytes)")
            
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Failed to save M-UDO {m_udo.m_udo_id}: {e}")
            raise
    
    def load_m_udo(
        self,
        filepath_or_id: str,
        validate_integrity: bool = True
    ) -> MUDO:
        """
        Load M-UDO from storage.
        
        Args:
            filepath_or_id: File path or M-UDO ID
            validate_integrity: Whether to validate data integrity
            
        Returns:
            Loaded M-UDO object
        """
        
        start_time = time.time()
        
        # Determine file path
        filepath = self._resolve_filepath(filepath_or_id)
        
        if not filepath.exists():
            raise FileNotFoundError(f"M-UDO file not found: {filepath}")
        
        # Determine format from filename
        format_type = self._detect_format(filepath)
        
        try:
            # Load based on format type
            if format_type == "hdf5":
                m_udo = self._load_hdf5(filepath)
            elif format_type == "json":
                m_udo = self._load_json(filepath)
            elif format_type == "pickle":
                m_udo = self._load_pickle(filepath)
            else:
                raise ValueError(f"Unsupported format type: {format_type}")
            
            # Validate integrity if requested
            if validate_integrity:
                if not m_udo.validate_integrity():
                    logger.warning(f"Integrity validation failed for M-UDO {m_udo.m_udo_id}")
                else:
                    m_udo._is_validated = True
            
            # Update statistics
            self.storage_stats['objects_loaded'] += 1
            
            logger.info(f"Loaded M-UDO {m_udo.m_udo_id} from {filepath} in {time.time() - start_time:.3f}s")
            
            return m_udo
            
        except Exception as e:
            logger.error(f"Failed to load M-UDO from {filepath}: {e}")
            raise
    
    def _resolve_filepath(self, filepath_or_id: str) -> Path:
        """Resolve filepath from ID or path string"""
        
        path = Path(filepath_or_id)
        
        # If it's already a complete path and exists, use it
        if path.is_absolute() and path.exists():
            return path
        
        # If it's relative and exists, use it
        if path.exists():
            return path
        
        # Treat as M-UDO ID and search for files
        if not path.suffix:  # No extension, treat as ID
            # Search for files with this ID
            patterns = [
                f"{filepath_or_id}.hdf5*",
                f"{filepath_or_id}.json*", 
                f"{filepath_or_id}.pickle*"
            ]
            
            for pattern in patterns:
                matches = list(self.base_path.glob(pattern))
                if matches:
                    return matches[0]  # Return first match
        
        # Default to base path + filename
        return self.base_path / filepath_or_id
    
    def _detect_format(self, filepath: Path) -> str:
        """Detect format from filename"""
        
        filename = filepath.name.lower()
        
        if '.hdf5' in filename or '.h5' in filename:
            return 'hdf5'
        elif '.json' in filename:
            return 'json'
        elif '.pickle' in filename or '.pkl' in filename:
            return 'pickle'
        else:
            # Default to HDF5
            return 'hdf5'
    
    def _save_hdf5(self, m_udo: MUDO, filepath: Path, compress: bool):
        """Save M-UDO in HDF5 format"""
        
        def write_hdf5_data(file_handle):
            # Save metadata
            metadata_dict = m_udo.to_dict()['metadata']
            metadata_json = json.dumps(metadata_dict, default=str)
            file_handle.attrs['metadata'] = metadata_json
            
            # Save basic information
            file_handle.attrs['m_udo_id'] = m_udo.m_udo_id
            file_handle.attrs['sequence_id'] = m_udo.sequence_id
            file_handle.attrs['sequence'] = m_udo.sequence
            
            # Save embeddings
            embeddings_group = file_handle.create_group('embeddings')
            for embedding_type, embedding in m_udo.embeddings.items():
                emb_group = embeddings_group.create_group(embedding_type)
                emb_group.create_dataset('vector', data=embedding.embedding_vector)
                emb_group.attrs['type'] = embedding.embedding_type
                emb_group.attrs['dimension'] = embedding.dimension
                emb_group.attrs['confidence_score'] = embedding.confidence_score
                emb_group.attrs['processing_time'] = embedding.processing_time
                emb_group.attrs['magnitude'] = embedding.magnitude
                emb_group.attrs['sparsity'] = embedding.sparsity
                emb_group.attrs['entropy'] = embedding.entropy
                emb_group.attrs['model_version'] = embedding.model_version or ''
                emb_group.attrs['normalization_applied'] = embedding.normalization_applied
                if embedding.created_at:
                    emb_group.attrs['created_at'] = embedding.created_at.isoformat()
            
            # Save analyses
            analyses_group = file_handle.create_group('analyses')
            for analysis_key, analysis in m_udo.analyses.items():
                ana_group = analyses_group.create_group(analysis_key)
                ana_group.attrs['analysis_type'] = analysis.analysis_type
                ana_group.attrs['confidence_level'] = analysis.confidence_level
                ana_group.attrs['method_used'] = analysis.method_used or ''
                ana_group.attrs['analysis_duration'] = analysis.analysis_duration
                if analysis.analysis_timestamp:
                    ana_group.attrs['analysis_timestamp'] = analysis.analysis_timestamp.isoformat()
                
                # Store results as JSON
                results_json = json.dumps(analysis.results, default=str)
                ana_group.attrs['results'] = results_json
        
        # Write to file (with or without compression)
        if compress:
            # Use gzip compression for HDF5
            with h5py.File(filepath, 'w', compression='gzip') as f:
                write_hdf5_data(f)
        else:
            with h5py.File(filepath, 'w') as f:
                write_hdf5_data(f)
    
    def _load_hdf5(self, filepath: Path) -> MUDO:
        """Load M-UDO from HDF5 format"""
        
        with h5py.File(filepath, 'r') as f:
            # Load basic information
            m_udo_id = f.attrs['m_udo_id']
            sequence_id = f.attrs['sequence_id']
            sequence = f.attrs['sequence']
            
            # Create M-UDO object
            m_udo = MUDO(sequence_id, sequence)
            m_udo.m_udo_id = m_udo_id
            
            # Load metadata
            metadata_json = f.attrs['metadata']
            metadata_dict = json.loads(metadata_json)
            if metadata_dict.get('created_at'):
                metadata_dict['created_at'] = datetime.fromisoformat(metadata_dict['created_at'])
            if metadata_dict.get('updated_at'):
                metadata_dict['updated_at'] = datetime.fromisoformat(metadata_dict['updated_at'])
            m_udo.metadata = MUDOMetadata(**metadata_dict)
            
            # Load embeddings
            if 'embeddings' in f:
                embeddings_group = f['embeddings']
                for embedding_type in embeddings_group.keys():
                    emb_group = embeddings_group[embedding_type]
                    
                    embedding = MUDOEmbedding(
                        embedding_type=emb_group.attrs['type'],
                        embedding_vector=emb_group['vector'][:],
                        dimension=int(emb_group.attrs['dimension']),
                        confidence_score=float(emb_group.attrs.get('confidence_score', 1.0)),
                        processing_time=float(emb_group.attrs.get('processing_time', 0.0)),
                        magnitude=float(emb_group.attrs.get('magnitude', 0.0)),
                        sparsity=float(emb_group.attrs.get('sparsity', 0.0)),
                        entropy=float(emb_group.attrs.get('entropy', 0.0)),
                        model_version=emb_group.attrs.get('model_version', ''),
                        normalization_applied=bool(emb_group.attrs.get('normalization_applied', False)),
                        created_at=datetime.fromisoformat(emb_group.attrs['created_at']) if emb_group.attrs.get('created_at') else None
                    )
                    
                    m_udo.embeddings[embedding_type] = embedding
            
            # Load analyses
            if 'analyses' in f:
                analyses_group = f['analyses']
                for analysis_key in analyses_group.keys():
                    ana_group = analyses_group[analysis_key]
                    
                    # Parse results from JSON
                    results_json = ana_group.attrs['results']
                    results = json.loads(results_json)
                    
                    analysis = MUDOAnalysis(
                        analysis_type=ana_group.attrs['analysis_type'],
                        results=results,
                        confidence_level=float(ana_group.attrs.get('confidence_level', 1.0)),
                        method_used=ana_group.attrs.get('method_used', ''),
                        analysis_duration=float(ana_group.attrs.get('analysis_duration', 0.0)),
                        analysis_timestamp=datetime.fromisoformat(ana_group.attrs['analysis_timestamp']) if ana_group.attrs.get('analysis_timestamp') else None
                    )
                    
                    m_udo.analyses[analysis_key] = analysis
        
        return m_udo
    
    def _save_json(self, m_udo: MUDO, filepath: Path, compress: bool):
        """Save M-UDO in JSON format"""
        
        # Convert to dictionary
        m_udo_dict = m_udo.to_dict()
        
        # Serialize to JSON
        json_data = json.dumps(m_udo_dict, indent=2, default=str)
        
        # Write to file
        if compress:
            with gzip.open(filepath, 'wt', encoding='utf-8') as f:
                f.write(json_data)
        else:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(json_data)
    
    def _load_json(self, filepath: Path) -> MUDO:
        """Load M-UDO from JSON format"""
        
        # Determine if compressed
        is_compressed = filepath.suffix.lower() == '.gz'
        
        # Read JSON data
        if is_compressed:
            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                json_data = f.read()
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                json_data = f.read()
        
        # Parse JSON
        m_udo_dict = json.loads(json_data)
        
        # Create M-UDO from dictionary
        return MUDO.from_dict(m_udo_dict)
    
    def _save_pickle(self, m_udo: MUDO, filepath: Path, compress: bool):
        """Save M-UDO in pickle format"""
        
        # Serialize with pickle
        pickle_data = pickle.dumps(m_udo)
        
        # Write to file
        if compress:
            with gzip.open(filepath, 'wb') as f:
                f.write(pickle_data)
        else:
            with open(filepath, 'wb') as f:
                f.write(pickle_data)
    
    def _load_pickle(self, filepath: Path) -> MUDO:
        """Load M-UDO from pickle format"""
        
        # Determine if compressed
        is_compressed = filepath.suffix.lower() == '.gz'
        
        # Read pickle data
        if is_compressed:
            with gzip.open(filepath, 'rb') as f:
                pickle_data = f.read()
        else:
            with open(filepath, 'rb') as f:
                pickle_data = f.read()
        
        # Deserialize
        return pickle.loads(pickle_data)
    
    def _calculate_file_checksum(self, filepath: Path) -> str:
        """Calculate SHA-256 checksum of file"""
        
        sha256_hash = hashlib.sha256()
        
        with open(filepath, 'rb') as f:
            # Read file in chunks to handle large files
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        
        return sha256_hash.hexdigest()
    
    def _update_storage_stats(self, file_size: int, processing_time: float):
        """Update storage statistics"""
        
        self.storage_stats['objects_stored'] += 1
        self.storage_stats['total_size_bytes'] += file_size
    
    def list_m_udos(self) -> List[Dict[str, Any]]:
        """List all M-UDOs in storage"""
        
        m_udos = []
        
        # Scan for M-UDO files
        patterns = ['*.hdf5*', '*.json*', '*.pickle*']
        
        for pattern in patterns:
            for filepath in self.base_path.glob(pattern):
                try:
                    # Extract basic information without full loading
                    m_udo_info = self._get_m_udo_info(filepath)
                    m_udos.append(m_udo_info)
                except Exception as e:
                    logger.warning(f"Failed to read M-UDO info from {filepath}: {e}")
        
        return m_udos
    
    def _get_m_udo_info(self, filepath: Path) -> Dict[str, Any]:
        """Get basic M-UDO information without full loading"""
        
        format_type = self._detect_format(filepath)
        
        try:
            if format_type == 'hdf5':
                with h5py.File(filepath, 'r') as f:
                    info = {
                        'filepath': str(filepath),
                        'format': format_type,
                        'm_udo_id': f.attrs.get('m_udo_id', ''),
                        'sequence_id': f.attrs.get('sequence_id', ''),
                        'file_size': filepath.stat().st_size,
                        'modified_time': filepath.stat().st_mtime,
                        'embeddings': list(f['embeddings'].keys()) if 'embeddings' in f else [],
                        'analyses': list(f['analyses'].keys()) if 'analyses' in f else []
                    }
            else:
                # For JSON and pickle, we need to load the file (less efficient)
                m_udo = self.load_m_udo(str(filepath), validate_integrity=False)
                info = {
                    'filepath': str(filepath),
                    'format': format_type,
                    'm_udo_id': m_udo.m_udo_id,
                    'sequence_id': m_udo.sequence_id,
                    'file_size': filepath.stat().st_size,
                    'modified_time': filepath.stat().st_mtime,
                    'embeddings': list(m_udo.embeddings.keys()),
                    'analyses': list(m_udo.analyses.keys())
                }
            
            return info
            
        except Exception as e:
            logger.error(f"Failed to extract M-UDO info from {filepath}: {e}")
            return {
                'filepath': str(filepath),
                'format': format_type,
                'error': str(e)
            }
    
    def delete_m_udo(self, filepath_or_id: str) -> bool:
        """Delete M-UDO from storage"""
        
        filepath = self._resolve_filepath(filepath_or_id)
        
        try:
            if filepath.exists():
                filepath.unlink()
                logger.info(f"Deleted M-UDO file: {filepath}")
                return True
            else:
                logger.warning(f"M-UDO file not found: {filepath}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to delete M-UDO {filepath}: {e}")
            return False
    
    def get_storage_statistics(self) -> Dict[str, Any]:
        """Get storage system statistics"""
        
        stats = self.storage_stats.copy()
        
        # Add directory information
        stats['storage_path'] = str(self.base_path)
        stats['storage_exists'] = self.base_path.exists()
        
        # Count files in storage
        total_files = 0
        total_size = 0
        
        patterns = ['*.hdf5*', '*.json*', '*.pickle*']
        for pattern in patterns:
            for filepath in self.base_path.glob(pattern):
                total_files += 1
                total_size += filepath.stat().st_size
        
        stats['files_in_storage'] = total_files
        stats['actual_storage_size'] = total_size
        
        return stats
    
    def cleanup_storage(self, max_age_days: int = 30) -> int:
        """Clean up old M-UDO files"""
        
        cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)
        cleaned_count = 0
        
        patterns = ['*.hdf5*', '*.json*', '*.pickle*']
        
        for pattern in patterns:
            for filepath in self.base_path.glob(pattern):
                try:
                    if filepath.stat().st_mtime < cutoff_time:
                        filepath.unlink()
                        cleaned_count += 1
                        logger.info(f"Cleaned up old M-UDO file: {filepath}")
                except Exception as e:
                    logger.warning(f"Failed to clean up {filepath}: {e}")
        
        logger.info(f"Cleaned up {cleaned_count} old M-UDO files")
        return cleaned_count
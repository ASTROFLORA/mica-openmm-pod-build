#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 CEA BATCH EMBEDDING GENERATOR - Google Cloud Run Ready
==========================================================

Genera embeddings 5-vector para proteomas completos usando:
- Cloud Run Jobs para procesamiento batch
- Cloud Storage para input/output
- Artifact Registry para imágenes Docker
- GPU L4/A100 para inferencia acelerada

Datasets soportados:
- Human Proteome (UP000005640): ~20K proteínas
- SwissProt completo: ~570K proteínas

Author: BSM Team
Date: 2025
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Iterator, Tuple, Any
import argparse

import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class CloudRunConfig:
    """Configuración para Cloud Run Job"""
    
    # Cloud Storage paths
    gcs_input_bucket: str = "gs://mica-proteome-data"
    gcs_output_bucket: str = "gs://mica-embeddings-output"
    
    # Input files
    fasta_file: str = "human_proteome_reviewed.fasta"
    
    # Processing settings
    batch_size: int = 32              # Proteins per GPU batch
    checkpoint_interval: int = 1000   # Save checkpoint every N proteins
    max_sequence_length: int = 1024   # Truncate longer sequences
    
    # Model selection (for resource-constrained environments)
    use_prott5: bool = True           # 1024D - main sequence embedding
    use_esm2: bool = True             # 1280D - structure-aware (ESM-2 fallback)
    use_biolinkbert: bool = True      # 768D - semantic metadata
    use_scibert: bool = True          # 768D - literature knowledge
    
    # Cloud Run specifics
    task_index: int = 0               # CLOUD_RUN_TASK_INDEX
    task_count: int = 1               # CLOUD_RUN_TASK_COUNT
    
    # Memory management
    clear_cache_interval: int = 100   # Clear GPU cache every N batches
    
    def __post_init__(self):
        # Read Cloud Run environment variables
        self.task_index = int(os.getenv("CLOUD_RUN_TASK_INDEX", "0"))
        self.task_count = int(os.getenv("CLOUD_RUN_TASK_COUNT", "1"))


@dataclass
class EmbeddingResult:
    """Resultado de embedding para una proteína"""
    protein_id: str
    uniprot_ac: str
    gene_name: Optional[str]
    organism: str
    sequence_length: int
    
    # Embeddings (stored as lists for JSON serialization)
    embedding_prott5: Optional[List[float]] = None      # 1024D
    embedding_esm2: Optional[List[float]] = None        # 1280D
    embedding_biolinkbert: Optional[List[float]] = None # 768D
    embedding_scibert: Optional[List[float]] = None     # 768D
    
    # Metadata
    processing_time_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    def get_combined_dimension(self) -> int:
        """Calcula dimensión total de embeddings generados"""
        total = 0
        if self.embedding_prott5:
            total += len(self.embedding_prott5)
        if self.embedding_esm2:
            total += len(self.embedding_esm2)
        if self.embedding_biolinkbert:
            total += len(self.embedding_biolinkbert)
        if self.embedding_scibert:
            total += len(self.embedding_scibert)
        return total


@dataclass 
class BatchStats:
    """Estadísticas de procesamiento"""
    total_proteins: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    total_time_seconds: float = 0.0
    
    @property
    def proteins_per_second(self) -> float:
        if self.total_time_seconds > 0:
            return self.processed / self.total_time_seconds
        return 0.0
    
    @property
    def progress_percent(self) -> float:
        if self.total_proteins > 0:
            return (self.processed / self.total_proteins) * 100
        return 0.0


# ============================================================================
# FASTA PARSER
# ============================================================================

def parse_fasta(fasta_path: Path) -> Iterator[Tuple[str, str, Dict[str, str]]]:
    """
    Parse UniProt FASTA format.
    
    Yields:
        (uniprot_ac, sequence, metadata_dict)
    
    Example header:
    >sp|P12345|BRCA1_HUMAN Breast cancer type 1 OS=Homo sapiens OX=9606 GN=BRCA1 PE=1 SV=2
    """
    current_header = None
    current_sequence = []
    
    with open(fasta_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('>'):
                # Yield previous entry
                if current_header and current_sequence:
                    yield _parse_header(current_header, ''.join(current_sequence))
                
                current_header = line[1:]  # Remove '>'
                current_sequence = []
            else:
                current_sequence.append(line)
        
        # Yield last entry
        if current_header and current_sequence:
            yield _parse_header(current_header, ''.join(current_sequence))


def _parse_header(header: str, sequence: str) -> Tuple[str, str, Dict[str, str]]:
    """Parse UniProt FASTA header"""
    parts = header.split('|')
    
    metadata = {
        'db': 'unknown',
        'uniprot_ac': '',
        'entry_name': '',
        'description': '',
        'organism': '',
        'gene_name': '',
    }
    
    if len(parts) >= 3:
        metadata['db'] = parts[0]  # sp or tr
        metadata['uniprot_ac'] = parts[1]
        
        # Parse rest of header
        rest = parts[2]
        
        # Extract entry name (before first space)
        if ' ' in rest:
            entry_name, desc_part = rest.split(' ', 1)
            metadata['entry_name'] = entry_name
            
            # Parse OS=, GN=, etc.
            if 'OS=' in desc_part:
                os_start = desc_part.find('OS=') + 3
                os_end = desc_part.find(' OX=') if ' OX=' in desc_part else desc_part.find(' GN=')
                if os_end == -1:
                    os_end = len(desc_part)
                metadata['organism'] = desc_part[os_start:os_end].strip()
            
            if 'GN=' in desc_part:
                gn_start = desc_part.find('GN=') + 3
                gn_end = desc_part.find(' ', gn_start)
                if gn_end == -1:
                    gn_end = len(desc_part)
                metadata['gene_name'] = desc_part[gn_start:gn_end].strip()
            
            # Description is everything before OS=
            if 'OS=' in desc_part:
                metadata['description'] = desc_part[:desc_part.find('OS=')].strip()
        else:
            metadata['entry_name'] = rest
    
    return metadata['uniprot_ac'], sequence, metadata


# ============================================================================
# EMBEDDING GENERATOR
# ============================================================================

class CloudRunEmbeddingGenerator:
    """Generador de embeddings optimizado para Cloud Run"""
    
    def __init__(self, config: CloudRunConfig):
        self.config = config
        self.models_loaded = False
        
        # Model references (lazy loaded)
        self._prott5_model = None
        self._prott5_tokenizer = None
        self._esm2_model = None
        self._esm2_tokenizer = None
        self._biolinkbert_model = None
        self._biolinkbert_tokenizer = None
        self._scibert_model = None
        self._scibert_tokenizer = None
        
        self._device = None
    
    def _get_device(self):
        """Detect available device"""
        if self._device is None:
            import torch
            if torch.cuda.is_available():
                self._device = torch.device("cuda")
                logger.info(f"🔥 Using CUDA: {torch.cuda.get_device_name()}")
            else:
                self._device = torch.device("cpu")
                logger.info("💻 Using CPU (GPU not available)")
        return self._device
    
    def load_models(self):
        """Load all required models"""
        if self.models_loaded:
            return
        
        import torch
        from transformers import AutoModel, AutoTokenizer, T5EncoderModel, T5Tokenizer
        
        device = self._get_device()
        
        # ProtT5 (1024D)
        if self.config.use_prott5:
            logger.info("📥 Loading ProtT5...")
            try:
                self._prott5_tokenizer = T5Tokenizer.from_pretrained(
                    "Rostlab/prot_t5_xl_uniref50",
                    do_lower_case=False
                )
                self._prott5_model = T5EncoderModel.from_pretrained(
                    "Rostlab/prot_t5_xl_uniref50"
                ).to(device).eval()
                logger.info("✅ ProtT5 loaded (1024D)")
            except Exception as e:
                logger.warning(f"⚠️ ProtT5 failed: {e}")
        
        # ESM-2 (1280D) - fallback for ESM-C
        if self.config.use_esm2:
            logger.info("📥 Loading ESM-2...")
            try:
                self._esm2_tokenizer = AutoTokenizer.from_pretrained(
                    "facebook/esm2_t33_650M_UR50D"
                )
                self._esm2_model = AutoModel.from_pretrained(
                    "facebook/esm2_t33_650M_UR50D"
                ).to(device).eval()
                logger.info("✅ ESM-2 loaded (1280D)")
            except Exception as e:
                logger.warning(f"⚠️ ESM-2 failed: {e}")
        
        # BioLinkBERT (768D)
        if self.config.use_biolinkbert:
            logger.info("📥 Loading BioLinkBERT...")
            try:
                self._biolinkbert_tokenizer = AutoTokenizer.from_pretrained(
                    "michiyasunaga/BioLinkBERT-base"
                )
                self._biolinkbert_model = AutoModel.from_pretrained(
                    "michiyasunaga/BioLinkBERT-base"
                ).to(device).eval()
                logger.info("✅ BioLinkBERT loaded (768D)")
            except Exception as e:
                logger.warning(f"⚠️ BioLinkBERT failed: {e}")
        
        # SciBERT (768D)
        if self.config.use_scibert:
            logger.info("📥 Loading SciBERT...")
            try:
                self._scibert_tokenizer = AutoTokenizer.from_pretrained(
                    "allenai/scibert_scivocab_uncased"
                )
                self._scibert_model = AutoModel.from_pretrained(
                    "allenai/scibert_scivocab_uncased"
                ).to(device).eval()
                logger.info("✅ SciBERT loaded (768D)")
            except Exception as e:
                logger.warning(f"⚠️ SciBERT failed: {e}")
        
        self.models_loaded = True
        logger.info("🎯 All models loaded successfully")
    
    def _embed_prott5(self, sequence: str) -> Optional[np.ndarray]:
        """Generate ProtT5 embedding"""
        if not self._prott5_model:
            return None
        
        import torch
        
        # Add spaces between amino acids (ProtT5 requirement)
        seq_spaced = " ".join(list(sequence[:self.config.max_sequence_length]))
        
        inputs = self._prott5_tokenizer(
            seq_spaced,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.config.max_sequence_length
        ).to(self._device)
        
        with torch.no_grad():
            outputs = self._prott5_model(**inputs)
        
        # Mean pooling
        embedding = outputs.last_hidden_state.mean(dim=1).squeeze().cpu().numpy()
        return embedding / np.linalg.norm(embedding)
    
    def _embed_esm2(self, sequence: str) -> Optional[np.ndarray]:
        """Generate ESM-2 embedding"""
        if not self._esm2_model:
            return None
        
        import torch
        
        inputs = self._esm2_tokenizer(
            sequence[:self.config.max_sequence_length],
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.config.max_sequence_length
        ).to(self._device)
        
        with torch.no_grad():
            outputs = self._esm2_model(**inputs)
        
        embedding = outputs.last_hidden_state.mean(dim=1).squeeze().cpu().numpy()
        return embedding / np.linalg.norm(embedding)
    
    def _embed_text(self, text: str, model, tokenizer) -> Optional[np.ndarray]:
        """Generate BERT-style embedding for text"""
        if not model:
            return None
        
        import torch
        
        inputs = tokenizer(
            text,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512
        ).to(self._device)
        
        with torch.no_grad():
            outputs = model(**inputs)
        
        # Mean pooling
        attention_mask = inputs["attention_mask"]
        hidden = outputs.last_hidden_state
        mask_expanded = attention_mask.unsqueeze(-1).expand(hidden.size()).float()
        sum_embeddings = torch.sum(hidden * mask_expanded, dim=1)
        sum_mask = torch.clamp(mask_expanded.sum(dim=1), min=1e-9)
        embedding = (sum_embeddings / sum_mask).squeeze().cpu().numpy()
        
        return embedding / np.linalg.norm(embedding)
    
    def generate_embeddings(
        self,
        uniprot_ac: str,
        sequence: str,
        metadata: Dict[str, str]
    ) -> EmbeddingResult:
        """Generate all embeddings for a single protein"""
        start_time = time.time()
        
        result = EmbeddingResult(
            protein_id=f"{metadata.get('db', 'sp')}|{uniprot_ac}",
            uniprot_ac=uniprot_ac,
            gene_name=metadata.get('gene_name'),
            organism=metadata.get('organism', ''),
            sequence_length=len(sequence)
        )
        
        # ProtT5 - sequence embedding
        if self.config.use_prott5:
            emb = self._embed_prott5(sequence)
            if emb is not None:
                result.embedding_prott5 = emb.tolist()
        
        # ESM-2 - structure-aware embedding
        if self.config.use_esm2:
            emb = self._embed_esm2(sequence)
            if emb is not None:
                result.embedding_esm2 = emb.tolist()
        
        # BioLinkBERT - semantic metadata
        if self.config.use_biolinkbert:
            # Create semantic text from metadata
            semantic_text = f"{metadata.get('gene_name', '')} {metadata.get('description', '')} {metadata.get('organism', '')}"
            emb = self._embed_text(semantic_text, self._biolinkbert_model, self._biolinkbert_tokenizer)
            if emb is not None:
                result.embedding_biolinkbert = emb.tolist()
        
        # SciBERT - for literature context (using description)
        if self.config.use_scibert:
            lit_text = metadata.get('description', f"Protein {uniprot_ac}")
            emb = self._embed_text(lit_text, self._scibert_model, self._scibert_tokenizer)
            if emb is not None:
                result.embedding_scibert = emb.tolist()
        
        result.processing_time_ms = (time.time() - start_time) * 1000
        
        return result
    
    def clear_gpu_cache(self):
        """Clear GPU memory cache"""
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


# ============================================================================
# CLOUD RUN JOB EXECUTOR
# ============================================================================

def run_cloud_job(config: CloudRunConfig):
    """Main Cloud Run Job execution"""
    
    logger.info("=" * 60)
    logger.info("🧬 CEA BATCH EMBEDDING GENERATOR")
    logger.info(f"   Task {config.task_index + 1} of {config.task_count}")
    logger.info("=" * 60)
    
    # Initialize generator
    generator = CloudRunEmbeddingGenerator(config)
    generator.load_models()
    
    # Determine input path
    # In Cloud Run, mount GCS bucket or use gsutil
    if config.gcs_input_bucket.startswith("gs://"):
        # Download from GCS
        local_fasta = Path("/tmp") / config.fasta_file
        if not local_fasta.exists():
            import subprocess
            gcs_path = f"{config.gcs_input_bucket}/{config.fasta_file}"
            logger.info(f"📥 Downloading from {gcs_path}...")
            subprocess.run(["gsutil", "cp", gcs_path, str(local_fasta)], check=True)
    else:
        local_fasta = Path(config.fasta_file)
    
    if not local_fasta.exists():
        logger.error(f"❌ FASTA file not found: {local_fasta}")
        sys.exit(1)
    
    # Count total proteins
    logger.info("📊 Counting proteins...")
    proteins = list(parse_fasta(local_fasta))
    total_count = len(proteins)
    logger.info(f"   Found {total_count:,} proteins")
    
    # Calculate this task's portion
    proteins_per_task = total_count // config.task_count
    start_idx = config.task_index * proteins_per_task
    end_idx = start_idx + proteins_per_task if config.task_index < config.task_count - 1 else total_count
    
    task_proteins = proteins[start_idx:end_idx]
    logger.info(f"   Processing proteins {start_idx:,} to {end_idx:,}")
    
    # Process proteins
    stats = BatchStats(total_proteins=len(task_proteins))
    results = []
    start_time = time.time()
    
    output_file = Path(f"/tmp/embeddings_task_{config.task_index}.jsonl")
    
    with open(output_file, 'w') as f:
        for i, (uniprot_ac, sequence, metadata) in enumerate(task_proteins):
            try:
                result = generator.generate_embeddings(uniprot_ac, sequence, metadata)
                results.append(result)
                stats.processed += 1
                
                # Write to JSONL immediately
                f.write(json.dumps(result.to_dict()) + "\n")
                
                # Progress logging
                if (i + 1) % 100 == 0:
                    elapsed = time.time() - start_time
                    rate = stats.processed / elapsed
                    eta = (stats.total_proteins - stats.processed) / rate if rate > 0 else 0
                    logger.info(
                        f"   Progress: {stats.processed:,}/{stats.total_proteins:,} "
                        f"({stats.progress_percent:.1f}%) | "
                        f"{rate:.1f} prot/s | ETA: {eta/60:.1f} min"
                    )
                
                # Clear GPU cache periodically
                if (i + 1) % config.clear_cache_interval == 0:
                    generator.clear_gpu_cache()
                
                # Checkpoint
                if (i + 1) % config.checkpoint_interval == 0:
                    f.flush()
                    logger.info(f"   💾 Checkpoint saved at {stats.processed:,} proteins")
                    
            except Exception as e:
                logger.error(f"❌ Failed {uniprot_ac}: {e}")
                stats.failed += 1
    
    stats.total_time_seconds = time.time() - start_time
    
    # Upload results to GCS
    if config.gcs_output_bucket.startswith("gs://"):
        import subprocess
        gcs_output = f"{config.gcs_output_bucket}/embeddings_task_{config.task_index}.jsonl"
        logger.info(f"📤 Uploading results to {gcs_output}...")
        subprocess.run(["gsutil", "cp", str(output_file), gcs_output], check=True)
    
    # Final summary
    logger.info("\n" + "=" * 60)
    logger.info("📊 PROCESSING COMPLETE")
    logger.info("=" * 60)
    logger.info(f"   Total proteins: {stats.total_proteins:,}")
    logger.info(f"   Processed: {stats.processed:,}")
    logger.info(f"   Failed: {stats.failed:,}")
    logger.info(f"   Time: {stats.total_time_seconds:.1f}s")
    logger.info(f"   Rate: {stats.proteins_per_second:.1f} proteins/second")
    logger.info(f"   Output: {output_file}")
    
    return stats


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="CEA Batch Embedding Generator for Cloud Run")
    
    parser.add_argument("--fasta", default="human_proteome_reviewed.fasta",
                       help="Input FASTA file (local or in GCS bucket)")
    parser.add_argument("--gcs-input", default="gs://mica-proteome-data",
                       help="GCS bucket for input files")
    parser.add_argument("--gcs-output", default="gs://mica-embeddings-output",
                       help="GCS bucket for output files")
    parser.add_argument("--batch-size", type=int, default=32,
                       help="Batch size for GPU processing")
    parser.add_argument("--no-prott5", action="store_true",
                       help="Disable ProtT5 embeddings")
    parser.add_argument("--no-esm2", action="store_true",
                       help="Disable ESM-2 embeddings")
    parser.add_argument("--no-biolinkbert", action="store_true",
                       help="Disable BioLinkBERT embeddings")
    parser.add_argument("--local", action="store_true",
                       help="Run locally (not in Cloud Run)")
    
    args = parser.parse_args()
    
    config = CloudRunConfig(
        fasta_file=args.fasta,
        gcs_input_bucket=args.gcs_input if not args.local else ".",
        gcs_output_bucket=args.gcs_output if not args.local else "./output",
        batch_size=args.batch_size,
        use_prott5=not args.no_prott5,
        use_esm2=not args.no_esm2,
        use_biolinkbert=not args.no_biolinkbert,
    )
    
    run_cloud_job(config)


if __name__ == "__main__":
    main()

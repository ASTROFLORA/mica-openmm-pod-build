"""
HPC Batch Processor for Kinase Dataset
=======================================

Parallel processing infrastructure for ingesting all human kinases into CEA/BUDO.
Supports SLURM job submission and distributed processing.

Author: Alex Rodriguez (Chief Data Architect)
Lab: Alex Rodriguez AI Systems Architecture Lab
Phase: 1.004 - UniProt Bootstrap Scale-Up
Date: October 8, 2025
Version: 1.0.0

HPC Features:
- SLURM job array support
- Chunk-based parallel processing
- Automatic retry mechanisms
- Progress tracking and checkpointing
- Result aggregation
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd

# Optional heavyweight imports (not required for BatchProcessor unit tests)
try:  # pragma: no cover - fallback for lightweight test contexts
    from bsm.schemas.budo_v3 import BudoV3
    from bsm.schemas.cea import CanonicalEntity
    from bsm.cea.cea_service import CEAService
    from bsm.cea.ingestion import ProteinIngestionPipeline
except ImportError:  # pragma: no cover - fallback types for tests
    BudoV3 = CanonicalEntity = CEAService = ProteinIngestionPipeline = Any  # type: ignore
from .validation import BatchValidator, IngestionReport

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class BatchConfig:
    """Configuration for the asynchronous batch processor."""

    batch_size: int = 50
    max_concurrent: int = 5
    checkpoint_interval: int = 50
    output_dir: Path = field(default_factory=lambda: Path("data/hpc_checkpoints"))
    dry_run: bool = True

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        if self.checkpoint_interval <= 0:
            raise ValueError("checkpoint_interval must be positive")


BatchProcessorCallable = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class BatchProcessor:
    """High-level asynchronous batch processor with checkpointing support."""

    def __init__(
        self,
        config: BatchConfig,
        *,
        validator: Optional[BatchValidator] = None,
        ingestion_handler: Optional["BatchProcessorCallable"] = None,
    ) -> None:
        self.config = config
        self.validator = validator or BatchValidator()
        self._ingestion_handler = ingestion_handler or self._default_ingestion_handler
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    async def process_batch(self, csv_path: Path, resume: bool = False) -> IngestionReport:
        """Process an input CSV and return an ingestion report."""

        start_time = perf_counter()
        df = pd.read_csv(csv_path)
        validation_errors = self.validator.validate_input_csv(df)
        if validation_errors:
            raise ValueError(
                "Invalid batch input: " + "; ".join(sorted(validation_errors))
            )

        records = df.to_dict("records")
        total = len(records)
        successful = 0
        failed = 0
        skipped = 0
        errors: List[Dict[str, Any]] = []

        chunk_size = min(self.config.batch_size, self.config.checkpoint_interval)

        for chunk_id, chunk in enumerate(self._chunk(records, chunk_size)):
            checkpoint_path = self._checkpoint_path(chunk_id)

            if resume and checkpoint_path.exists():
                stored = self._load_checkpoint(checkpoint_path)
                for item in stored:
                    if item.get("status") == "success":
                        successful += 1
                    else:
                        failed += 1
                        errors.append({"uniprot_id": item.get("uniprot_id"), "error": item.get("error")})
                    skipped += 1
                continue

            chunk_results = await self._process_chunk(chunk)
            self._save_checkpoint(checkpoint_path, chunk_results)

            for item in chunk_results:
                if item.get("status") == "success":
                    successful += 1
                else:
                    failed += 1
                    errors.append({"uniprot_id": item.get("uniprot_id"), "error": item.get("error")})

        duration = perf_counter() - start_time
        throughput = successful / duration if duration > 0 else 0.0

        report = IngestionReport(
            total_proteins=total,
            successful=successful,
            failed=failed,
            skipped=skipped,
            duration_seconds=duration,
            throughput=throughput,
            errors=errors,
            timestamp=datetime.utcnow(),
        )

        return report

    async def _process_chunk(self, chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.config.max_concurrent)

        async def _worker(record: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                try:
                    return await self._ingestion_handler(record)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Failed to process protein %s", record.get("uniprot_id"))
                    return {
                        **record,
                        "status": "failed",
                        "error": str(exc),
                        "budo_id": None,
                    }

        tasks = [asyncio.create_task(_worker(record)) for record in chunk]
        results: List[Dict[str, Any]] = []
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
        return results

    async def _default_ingestion_handler(self, record: Dict[str, Any]) -> Dict[str, Any]:
        # Placeholder ingestion logic until Phase 1.004 pipeline is wired
        await asyncio.sleep(0)
        budo_id = f"budo:{record['uniprot_id']}-S-001"
        return {
            **record,
            "status": "success",
            "budo_id": budo_id,
            "error": None,
        }

    def _checkpoint_path(self, chunk_id: int) -> Path:
        return self.config.output_dir / f"checkpoint_{chunk_id:04d}.json"

    def _save_checkpoint(self, path: Path, results: List[Dict[str, Any]]) -> None:
        payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "results": results,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Saved checkpoint to %s", path)

    def _load_checkpoint(self, path: Path) -> List[Dict[str, Any]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("results", [])

    @staticmethod
    def _chunk(records: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
        return [records[i : i + size] for i in range(0, len(records), size)]


class HPCBatchProcessor:
    """
    HPC-optimized batch processor for kinase ingestion.
    
    Designed for SLURM job arrays with automatic chunking and checkpointing.
    """
    
    def __init__(
        self,
        chunk_size: int = 50,
        checkpoint_dir: Optional[Path] = None,
        max_retries: int = 3,
        dry_run: bool = False
    ):
        """
        Initialize HPC batch processor.
        
        Args:
            chunk_size: Number of proteins per chunk (SLURM task)
            checkpoint_dir: Directory for checkpoint files
            max_retries: Maximum retry attempts per protein
            dry_run: If True, validate without writing to database
        """
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.dry_run = dry_run
        
        # Setup checkpoint directory
        if checkpoint_dir is None:
            checkpoint_dir = Path("data/hpc_checkpoints")
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize services (lazy loaded to avoid connection overhead)
        self._cea_service = None
        self._ingestion_pipeline = None
        
        logger.info(f"Initialized HPC Batch Processor (chunk_size={chunk_size}, dry_run={dry_run})")
    
    @property
    def cea_service(self) -> CEAService:
        """Lazy-load CEA service"""
        if self._cea_service is None:
            self._cea_service = CEAService()
        return self._cea_service
    
    @property
    def ingestion_pipeline(self) -> ProteinIngestionPipeline:
        """Lazy-load ingestion pipeline"""
        if self._ingestion_pipeline is None:
            self._ingestion_pipeline = ProteinIngestionPipeline(
                cea_service=self.cea_service,
                dry_run=self.dry_run
            )
        return self._ingestion_pipeline
    
    def load_kinase_catalog(self, catalog_path: Path) -> List[Dict]:
        """
        Load kinase catalog from JSON file.
        
        Args:
            catalog_path: Path to kinase catalog JSON
            
        Returns:
            List of kinase metadata dictionaries
        """
        logger.info(f"Loading kinase catalog from {catalog_path}")
        
        with open(catalog_path, 'r') as f:
            catalog = json.load(f)
        
        kinases = catalog.get('kinases', [])
        logger.info(f"Loaded {len(kinases)} kinases from catalog")
        
        return kinases
    
    def create_chunks(self, kinases: List[Dict]) -> List[List[Dict]]:
        """
        Split kinase list into chunks for parallel processing.
        
        Args:
            kinases: List of kinase metadata
            
        Returns:
            List of kinase chunks
        """
        chunks = []
        for i in range(0, len(kinases), self.chunk_size):
            chunk = kinases[i:i + self.chunk_size]
            chunks.append(chunk)
        
        logger.info(f"Created {len(chunks)} chunks (size={self.chunk_size})")
        return chunks
    
    def save_checkpoint(self, chunk_id: int, results: Dict) -> None:
        """
        Save checkpoint for chunk processing.
        
        Args:
            chunk_id: Chunk identifier
            results: Processing results for chunk
        """
        checkpoint_file = self.checkpoint_dir / f"chunk_{chunk_id:04d}.json"
        
        checkpoint_data = {
            'chunk_id': chunk_id,
            'timestamp': datetime.utcnow().isoformat(),
            'results': results
        }
        
        with open(checkpoint_file, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)
        
        logger.info(f"Saved checkpoint for chunk {chunk_id} to {checkpoint_file}")
    
    def load_checkpoint(self, chunk_id: int) -> Optional[Dict]:
        """
        Load checkpoint for chunk if exists.
        
        Args:
            chunk_id: Chunk identifier
            
        Returns:
            Checkpoint data or None if not found
        """
        checkpoint_file = self.checkpoint_dir / f"chunk_{chunk_id:04d}.json"
        
        if not checkpoint_file.exists():
            return None
        
        with open(checkpoint_file, 'r') as f:
            checkpoint_data = json.load(f)
        
        logger.info(f"Loaded checkpoint for chunk {chunk_id}")
        return checkpoint_data
    
    def process_kinase(self, kinase: Dict) -> Dict:
        """
        Process single kinase through ingestion pipeline.
        
        Args:
            kinase: Kinase metadata dictionary
            
        Returns:
            Processing result with status and details
        """
        uniprot_id = kinase.get('uniprot_id')
        gene_symbol = kinase.get('gene_symbol', 'UNKNOWN')
        
        result = {
            'uniprot_id': uniprot_id,
            'gene_symbol': gene_symbol,
            'status': 'pending',
            'budo_id': None,
            'error': None
        }
        
        try:
            logger.info(f"Processing {gene_symbol} ({uniprot_id})")
            
            # Ingest through pipeline
            budo_entity = self.ingestion_pipeline.ingest_protein(
                uniprot_id=uniprot_id,
                gene_symbol=gene_symbol,
                additional_metadata=kinase
            )
            
            result['status'] = 'success'
            result['budo_id'] = budo_entity.budoId
            
            logger.info(f"✓ Successfully processed {gene_symbol}: {budo_entity.budoId}")
            
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            logger.error(f"✗ Failed to process {gene_symbol}: {e}")
        
        return result
    
    def process_chunk(self, chunk_id: int, chunk: List[Dict]) -> Dict:
        """
        Process a chunk of kinases.
        
        Args:
            chunk_id: Chunk identifier
            chunk: List of kinase metadata
            
        Returns:
            Chunk processing summary
        """
        logger.info(f"Processing chunk {chunk_id} ({len(chunk)} kinases)")
        
        # Check for existing checkpoint
        checkpoint = self.load_checkpoint(chunk_id)
        if checkpoint is not None:
            logger.info(f"Chunk {chunk_id} already processed (checkpoint found)")
            return checkpoint['results']
        
        results = {
            'chunk_id': chunk_id,
            'total': len(chunk),
            'success': 0,
            'failed': 0,
            'kinases': []
        }
        
        for kinase in chunk:
            result = self.process_kinase(kinase)
            results['kinases'].append(result)
            
            if result['status'] == 'success':
                results['success'] += 1
            else:
                results['failed'] += 1
        
        # Save checkpoint
        self.save_checkpoint(chunk_id, results)
        
        logger.info(
            f"Chunk {chunk_id} complete: "
            f"{results['success']} success, {results['failed']} failed"
        )
        
        return results
    
    def aggregate_results(self, num_chunks: int) -> Dict:
        """
        Aggregate results from all chunks.
        
        Args:
            num_chunks: Total number of chunks
            
        Returns:
            Aggregated results summary
        """
        logger.info(f"Aggregating results from {num_chunks} chunks")
        
        aggregate = {
            'total_chunks': num_chunks,
            'total_kinases': 0,
            'total_success': 0,
            'total_failed': 0,
            'chunks': [],
            'timestamp': datetime.utcnow().isoformat()
        }
        
        for chunk_id in range(num_chunks):
            checkpoint = self.load_checkpoint(chunk_id)
            
            if checkpoint is None:
                logger.warning(f"Chunk {chunk_id} not found")
                continue
            
            results = checkpoint['results']
            aggregate['total_kinases'] += results['total']
            aggregate['total_success'] += results['success']
            aggregate['total_failed'] += results['failed']
            aggregate['chunks'].append({
                'chunk_id': chunk_id,
                'success': results['success'],
                'failed': results['failed']
            })
        
        # Save aggregate report
        report_file = self.checkpoint_dir / "aggregate_report.json"
        with open(report_file, 'w') as f:
            json.dump(aggregate, f, indent=2)
        
        logger.info(
            f"Aggregation complete: {aggregate['total_success']}/{aggregate['total_kinases']} "
            f"kinases processed successfully"
        )
        
        return aggregate


def main():
    """Main entry point for HPC batch processing"""
    parser = argparse.ArgumentParser(description='HPC Batch Processor for Kinase Dataset')
    
    parser.add_argument(
        '--catalog',
        type=Path,
        required=True,
        help='Path to kinase catalog JSON file'
    )
    
    parser.add_argument(
        '--chunk-id',
        type=int,
        help='Process specific chunk (SLURM_ARRAY_TASK_ID)'
    )
    
    parser.add_argument(
        '--chunk-size',
        type=int,
        default=50,
        help='Number of kinases per chunk (default: 50)'
    )
    
    parser.add_argument(
        '--checkpoint-dir',
        type=Path,
        default=Path('data/hpc_checkpoints'),
        help='Directory for checkpoint files'
    )
    
    parser.add_argument(
        '--aggregate',
        action='store_true',
        help='Aggregate results from all chunks'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Validate without writing to database'
    )
    
    args = parser.parse_args()
    
    # Initialize processor
    processor = HPCBatchProcessor(
        chunk_size=args.chunk_size,
        checkpoint_dir=args.checkpoint_dir,
        dry_run=args.dry_run
    )
    
    # Load kinase catalog
    kinases = processor.load_kinase_catalog(args.catalog)
    chunks = processor.create_chunks(kinases)
    
    if args.aggregate:
        # Aggregate mode: collect results from all chunks
        results = processor.aggregate_results(len(chunks))
        print(json.dumps(results, indent=2))
        
    elif args.chunk_id is not None:
        # Chunk mode: process specific chunk (SLURM job array)
        if args.chunk_id >= len(chunks):
            logger.error(f"Chunk ID {args.chunk_id} out of range (max: {len(chunks)-1})")
            sys.exit(1)
        
        chunk = chunks[args.chunk_id]
        results = processor.process_chunk(args.chunk_id, chunk)
        print(json.dumps(results, indent=2))
        
    else:
        # Sequential mode: process all chunks (local testing)
        logger.info(f"Sequential processing of {len(chunks)} chunks")
        for chunk_id, chunk in enumerate(chunks):
            processor.process_chunk(chunk_id, chunk)
        
        # Aggregate results
        results = processor.aggregate_results(len(chunks))
        print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()

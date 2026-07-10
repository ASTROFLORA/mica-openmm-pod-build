"""
HPC Batch Processing Module
============================

Infrastructure for parallel batch processing of protein datasets on HPC systems.

Author: Alex Rodriguez (Chief Data Architect)
Date: October 8, 2025
Version: 1.0.0

Components:
- batch_processor.py: Core batch processing engine with checkpointing
- kinase_catalog.py: UniProt kinase catalog generator
- slurm_batch_kinase.sh: SLURM job array script
- orchestrate_kinase_batch.sh: End-to-end orchestrator
"""

from .batch_processor import HPCBatchProcessor
from .kinase_catalog import KinaseCatalogGenerator

__all__ = [
    "HPCBatchProcessor",
    "KinaseCatalogGenerator",
]

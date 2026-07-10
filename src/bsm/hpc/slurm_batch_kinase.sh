#!/bin/bash
#SBATCH --job-name=bsm_kinase_batch
#SBATCH --output=logs/slurm_%A_%a.out
#SBATCH --error=logs/slurm_%A_%a.err
#SBATCH --array=0-19
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --partition=standard

################################################################################
# BSM Kinase Batch Processing - SLURM Job Array
################################################################################
#
# Author: Alex Rodriguez (Chief Data Architect)
# Date: October 8, 2025
# Version: 1.0.0
#
# Description:
#   SLURM job array for parallel processing of human kinase dataset.
#   Each task processes one chunk of kinases through CEA/BUDO ingestion.
#
# Usage:
#   sbatch slurm_batch_kinase.sh
#
# Job Array Configuration:
#   - SBATCH --array=0-19: Process 20 chunks (adjust based on catalog size)
#   - Each chunk processes 50 kinases (configurable in batch_processor.py)
#   - Total capacity: 20 chunks × 50 kinases = 1000 kinases
#
# Environment:
#   Assumes BSM conda environment is activated in ~/.bashrc or similar
#   Requires access to Neo4j and Zilliz Cloud (credentials in .env)
#
################################################################################

echo "=================================================="
echo "BSM Kinase Batch Processing - Task ${SLURM_ARRAY_TASK_ID}"
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: $(hostname)"
echo "Date: $(date)"
echo "=================================================="

# Exit on error
set -e

# Load environment modules (adjust for your HPC environment)
# module load python/3.11
# module load gcc/11.2.0

# Activate conda environment
source ~/.bashrc
conda activate bsm

# Set working directory (adjust to your project path)
cd /path/to/BSM-BUDO-CEA

# Create logs directory
mkdir -p logs

# Set environment variables
export PYTHONPATH="${PWD}/src:${PYTHONPATH}"
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}

# Log environment info
echo "Python: $(which python)"
echo "Python version: $(python --version)"
echo "PYTHONPATH: ${PYTHONPATH}"
echo "CPUs: ${SLURM_CPUS_PER_TASK}"
echo "Memory: ${SLURM_MEM_PER_NODE}"
echo ""

# Define paths
CATALOG_PATH="data/kinase_catalog_human.json"
CHECKPOINT_DIR="data/hpc_checkpoints"
CHUNK_SIZE=50

# Check if catalog exists
if [ ! -f "${CATALOG_PATH}" ]; then
    echo "ERROR: Kinase catalog not found at ${CATALOG_PATH}"
    echo "Please run: python src/bsm/hpc/kinase_catalog.py --output ${CATALOG_PATH}"
    exit 1
fi

echo "Processing chunk ${SLURM_ARRAY_TASK_ID}..."
echo "Catalog: ${CATALOG_PATH}"
echo "Checkpoint dir: ${CHECKPOINT_DIR}"
echo "Chunk size: ${CHUNK_SIZE}"
echo ""

# Run batch processor for this chunk
python src/bsm/hpc/batch_processor.py \
    --catalog "${CATALOG_PATH}" \
    --chunk-id "${SLURM_ARRAY_TASK_ID}" \
    --chunk-size "${CHUNK_SIZE}" \
    --checkpoint-dir "${CHECKPOINT_DIR}"

EXIT_CODE=$?

if [ ${EXIT_CODE} -eq 0 ]; then
    echo ""
    echo "✓ Chunk ${SLURM_ARRAY_TASK_ID} completed successfully"
else
    echo ""
    echo "✗ Chunk ${SLURM_ARRAY_TASK_ID} failed with exit code ${EXIT_CODE}"
fi

echo "=================================================="
echo "Finished at: $(date)"
echo "=================================================="

exit ${EXIT_CODE}

#!/bin/bash
################################################################################
# BSM Kinase Batch Orchestrator
################################################################################
#
# Author: Alex Rodriguez (Chief Data Architect)
# Date: October 8, 2025
# Version: 1.0.0
#
# Description:
#   Orchestrator script for end-to-end kinase batch processing on HPC.
#   Handles catalog generation, job submission, monitoring, and aggregation.
#
# Usage:
#   ./orchestrate_kinase_batch.sh [--dry-run] [--chunk-size N]
#
################################################################################

set -e

# Default configuration
DRY_RUN=false
CHUNK_SIZE=50
CATALOG_PATH="data/kinase_catalog_human.json"
CHECKPOINT_DIR="data/hpc_checkpoints"
SLURM_SCRIPT="src/bsm/hpc/slurm_batch_kinase.sh"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --chunk-size)
            CHUNK_SIZE="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [--dry-run] [--chunk-size N]"
            echo ""
            echo "Options:"
            echo "  --dry-run        Validate without writing to database"
            echo "  --chunk-size N   Number of kinases per chunk (default: 50)"
            echo "  --help           Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "=================================================="
echo "BSM Kinase Batch Orchestrator"
echo "=================================================="
echo "Configuration:"
echo "  Dry run: ${DRY_RUN}"
echo "  Chunk size: ${CHUNK_SIZE}"
echo "  Catalog: ${CATALOG_PATH}"
echo "  Checkpoint dir: ${CHECKPOINT_DIR}"
echo "=================================================="
echo ""

# Step 1: Generate kinase catalog (if not exists)
if [ ! -f "${CATALOG_PATH}" ]; then
    echo "[1/5] Generating kinase catalog from UniProt..."
    python src/bsm/hpc/kinase_catalog.py --output "${CATALOG_PATH}"
    echo "✓ Catalog generated: ${CATALOG_PATH}"
else
    echo "[1/5] Kinase catalog already exists: ${CATALOG_PATH}"
fi
echo ""

# Step 2: Calculate number of chunks needed
TOTAL_KINASES=$(python -c "import json; print(json.load(open('${CATALOG_PATH}'))['metadata']['total_kinases'])")
NUM_CHUNKS=$(python -c "import math; print(math.ceil(${TOTAL_KINASES} / ${CHUNK_SIZE}))")

echo "[2/5] Dataset overview:"
echo "  Total kinases: ${TOTAL_KINASES}"
echo "  Chunk size: ${CHUNK_SIZE}"
echo "  Number of chunks: ${NUM_CHUNKS}"
echo ""

# Step 3: Update SLURM script with correct array size
echo "[3/5] Configuring SLURM job array..."

# Create temporary SLURM script with correct array size
TEMP_SLURM_SCRIPT="${SLURM_SCRIPT}.tmp"
sed "s/#SBATCH --array=0-[0-9]*/#SBATCH --array=0-$((NUM_CHUNKS - 1))/" "${SLURM_SCRIPT}" > "${TEMP_SLURM_SCRIPT}"

echo "  Array size: 0-$((NUM_CHUNKS - 1))"
echo "  SLURM script: ${TEMP_SLURM_SCRIPT}"
echo ""

# Step 4: Submit SLURM job array
if [ "${DRY_RUN}" = true ]; then
    echo "[4/5] Dry run mode - skipping job submission"
    echo "  Would submit: sbatch ${TEMP_SLURM_SCRIPT}"
else
    echo "[4/5] Submitting SLURM job array..."
    
    JOB_ID=$(sbatch --parsable "${TEMP_SLURM_SCRIPT}")
    
    echo "✓ Job submitted: ${JOB_ID}"
    echo "  Monitor with: squeue -j ${JOB_ID}"
    echo "  Cancel with: scancel ${JOB_ID}"
    echo ""
    
    # Wait for jobs to complete
    echo "Waiting for jobs to complete..."
    echo "  (This may take several hours depending on dataset size)"
    
    while true; do
        # Check job status
        JOB_STATUS=$(squeue -j ${JOB_ID} -h -o "%T" | head -1)
        
        if [ -z "${JOB_STATUS}" ]; then
            echo "✓ All jobs completed"
            break
        fi
        
        # Count running/pending tasks
        RUNNING=$(squeue -j ${JOB_ID} -h -o "%T" | grep -c "RUNNING" || true)
        PENDING=$(squeue -j ${JOB_ID} -h -o "%T" | grep -c "PENDING" || true)
        
        echo "  Status: ${RUNNING} running, ${PENDING} pending ($(date +%H:%M:%S))"
        
        sleep 60
    done
    echo ""
fi

# Step 5: Aggregate results
echo "[5/5] Aggregating results..."

python src/bsm/hpc/batch_processor.py \
    --catalog "${CATALOG_PATH}" \
    --checkpoint-dir "${CHECKPOINT_DIR}" \
    --aggregate

echo ""
echo "=================================================="
echo "Batch processing complete!"
echo "=================================================="
echo ""
echo "Summary report: ${CHECKPOINT_DIR}/aggregate_report.json"
echo ""
echo "Next steps:"
echo "  1. Review aggregate report for failures"
echo "  2. Retry failed chunks if necessary"
echo "  3. Proceed to Phase 4: PubMedBERT embedding generation"
echo ""

# Cleanup temporary files
rm -f "${TEMP_SLURM_SCRIPT}"

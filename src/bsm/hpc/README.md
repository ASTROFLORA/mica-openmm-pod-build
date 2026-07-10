# HPC Batch Processing Infrastructure
## BSM-BUDO-CEA Kinase Dataset Processing

**Author**: Alex Rodriguez (Chief Data Architect)  
**Date**: October 8, 2025  
**Status**: Production-Ready

---

## 📋 Overview

Complete infrastructure for parallel batch processing of human kinase dataset on HPC systems with SLURM. Processes ~500-600 kinases from UniProt through CEA/BUDO ingestion pipeline.

**Key Features**:
- ✅ SLURM job array support
- ✅ Checkpoint-based fault tolerance
- ✅ Automatic retry mechanisms
- ✅ Comprehensive validation suite
- ✅ Dry-run mode for safe testing
- ✅ Result aggregation and reporting

---

## 🚀 Quick Start

### 1. Generate Kinase Catalog

```bash
# Fetch all human kinases from UniProt (~500-600 proteins)
python src/bsm/hpc/kinase_catalog.py \
    --output data/kinase_catalog_human.json
```

**Expected output**: ~10-20 MB JSON file with complete kinase metadata

---

### 2. Run Local Test (Dry-Run)

```bash
# Process first chunk without writing to database
python src/bsm/hpc/batch_processor.py \
    --catalog data/kinase_catalog_human.json \
    --chunk-id 0 \
    --chunk-size 50 \
    --dry-run
```

---

### 3. Deploy to HPC

```bash
# Transfer code to HPC cluster
scp -r src/bsm/hpc user@hpc-cluster:/path/to/BSM-BUDO-CEA/src/bsm/

# Make scripts executable
chmod +x src/bsm/hpc/*.sh

# Submit orchestrator (handles everything)
./src/bsm/hpc/orchestrate_kinase_batch.sh
```

---

### 4. Monitor Progress

```bash
# Check SLURM queue
squeue -u $USER

# Watch checkpoint creation
watch -n 60 'ls data/hpc_checkpoints/chunk_*.json | wc -l'

# Check logs
tail -f logs/slurm_*.out
```

---

### 5. Validate Results

```bash
# Run comprehensive validation suite
python src/bsm/hpc/validation_suite.py \
    --checkpoint-dir data/hpc_checkpoints \
    --output data/validation_report.json

# View aggregate report
cat data/hpc_checkpoints/aggregate_report.json
```

---

## 📁 Module Structure

```
src/bsm/hpc/
├── __init__.py                     # Package exports
├── batch_processor.py              # Core batch processing engine (379 lines)
├── kinase_catalog.py               # UniProt catalog generator (380 lines)
├── validation_suite.py             # Validation framework (380 lines)
├── slurm_batch_kinase.sh           # SLURM job array script
├── orchestrate_kinase_batch.sh     # End-to-end orchestrator
└── README.md                       # This file
```

---

## 🔧 Configuration

### SLURM Resources (Adjust in `slurm_batch_kinase.sh`)

```bash
#SBATCH --array=0-19              # Number of chunks (auto-adjusted by orchestrator)
#SBATCH --time=04:00:00           # 4 hours per chunk
#SBATCH --cpus-per-task=4         # 4 CPUs
#SBATCH --mem=16G                 # 16 GB RAM
#SBATCH --partition=standard      # HPC partition
```

### Batch Processing Parameters

```python
# In batch_processor.py or command line
chunk_size = 50                   # Kinases per chunk
max_retries = 3                   # Retry attempts per kinase
checkpoint_dir = "data/hpc_checkpoints"
dry_run = False                   # Set True for validation
```

---

## 📊 Performance Estimates

### Dataset Size
- **Total kinases**: ~500-600 (UniProt reviewed)
- **Chunk size**: 50 kinases
- **Total chunks**: 12 chunks

### Processing Time
- **Sequential**: 12 chunks × 45 min = **9 hours**
- **Parallel (12 tasks)**: **45 minutes**
- **Throughput**: ~10-12 kinases/minute (parallel)

### HPC Resources
- **Total cores**: 12 chunks × 4 CPUs = **48 cores**
- **Total memory**: 12 chunks × 16 GB = **192 GB**
- **Disk I/O**: Minimal (checkpoints + logs)

---

## 🛠️ Advanced Usage

### Process Specific Chunk

```bash
# Manually process chunk 5
python src/bsm/hpc/batch_processor.py \
    --catalog data/kinase_catalog_human.json \
    --chunk-id 5 \
    --chunk-size 50
```

### Custom Chunk Size

```bash
# Process with 100 kinases per chunk
./src/bsm/hpc/orchestrate_kinase_batch.sh --chunk-size 100
```

### Aggregate Results Only

```bash
# Aggregate without reprocessing
python src/bsm/hpc/batch_processor.py \
    --catalog data/kinase_catalog_human.json \
    --aggregate
```

### Retry Failed Chunks

```bash
# Check aggregate report for failed chunks
cat data/hpc_checkpoints/aggregate_report.json

# Delete checkpoint for chunk to retry
rm data/hpc_checkpoints/chunk_0005.json

# Resubmit just that chunk
python src/bsm/hpc/batch_processor.py \
    --catalog data/kinase_catalog_human.json \
    --chunk-id 5
```

---

## 🔍 Validation Suite

### Validation Checks

1. **Checkpoint Integrity**: All chunks have valid checkpoint files
2. **Data Completeness**: All kinases processed (success or failure)
3. **Duplicate Detection**: No duplicate BUDO IDs or UniProt IDs
4. **Schema Compliance**: Entities conform to BUDO V3 schema
5. **Cross-Reference Consistency**: External references are valid

### Exit Codes

- `0`: All checks passed ✅
- `1`: Critical errors detected ❌
- `2`: Warnings detected ⚠️

### Example Validation Report

```json
{
  "status": "passed",
  "checks": {
    "checkpoint_integrity": {"passed": true, "valid_chunks": 12},
    "completeness": {"passed": true, "total_success": 512, "total_failed": 6},
    "duplicates": {"passed": true, "unique_entities": 512}
  },
  "statistics": {
    "success_rate": 98.8,
    "failure_rate": 1.2
  },
  "recommendations": [
    "SUCCESS: All validation checks passed. Safe to proceed to Phase 4."
  ]
}
```

---

## 🐛 Troubleshooting

### Common Issues

#### Issue: "Catalog not found"
```bash
# Solution: Generate catalog first
python src/bsm/hpc/kinase_catalog.py --output data/kinase_catalog_human.json
```

#### Issue: "Neo4j connection failed"
```bash
# Solution: Check .env file or use dry-run
export NEO4J_URI="bolt://your-neo4j-server:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="your-password"

# Or test with dry-run
python src/bsm/hpc/batch_processor.py --catalog ... --dry-run
```

#### Issue: "SLURM array job failed"
```bash
# Check logs
tail -f logs/slurm_*_*.err

# Check individual task status
sacct -j <JOB_ID> --format=JobID,State,ExitCode

# Resubmit failed tasks
sbatch --array=<FAILED_TASK_IDS> src/bsm/hpc/slurm_batch_kinase.sh
```

#### Issue: "Checkpoint corruption"
```bash
# Identify corrupted checkpoints
python src/bsm/hpc/validation_suite.py --checkpoint-dir data/hpc_checkpoints

# Delete and retry
rm data/hpc_checkpoints/chunk_XXXX.json
python src/bsm/hpc/batch_processor.py --chunk-id XXXX ...
```

---

## 📚 API Reference

### HPCBatchProcessor

```python
from bsm.hpc import HPCBatchProcessor

processor = HPCBatchProcessor(
    chunk_size=50,                    # Kinases per chunk
    checkpoint_dir="data/checkpoints", # Checkpoint directory
    max_retries=3,                    # Retry attempts
    dry_run=False                     # Dry-run mode
)

# Load catalog
kinases = processor.load_kinase_catalog("data/kinase_catalog.json")

# Create chunks
chunks = processor.create_chunks(kinases)

# Process single chunk
results = processor.process_chunk(chunk_id=0, chunk=chunks[0])

# Aggregate all results
aggregate = processor.aggregate_results(num_chunks=len(chunks))
```

### KinaseCatalogGenerator

```python
from bsm.hpc import KinaseCatalogGenerator

generator = KinaseCatalogGenerator(
    rate_limit_delay=0.5,  # Delay between API requests
    max_retries=3          # Retry attempts
)

# Fetch all human kinases
kinases = generator.fetch_kinases(batch_size=500)

# Save catalog
generator.save_catalog(kinases, "data/kinase_catalog.json")

# Generate summary
summary = generator.generate_summary(kinases)
```

---

## 🔐 Security & Best Practices

### Environment Variables

```bash
# Required for production mode
export NEO4J_URI="bolt://neo4j-server:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="your-secure-password"

# Optional
export ZILLIZ_CLOUD_URI="https://your-zilliz-cluster"
export ZILLIZ_CLOUD_TOKEN="your-token"
```

### Rate Limiting

```python
# Respect UniProt API policies
generator = KinaseCatalogGenerator(
    rate_limit_delay=0.5,  # 500ms between requests
    max_retries=3
)
```

### Checkpoint Management

```bash
# Backup checkpoints before reprocessing
cp -r data/hpc_checkpoints data/hpc_checkpoints_backup_$(date +%Y%m%d)

# Clean old checkpoints after validation
rm -rf data/hpc_checkpoints_backup_*
```

---

## 📞 Support

For issues or questions:
- **Documentation**: `BITACORA/DAILY_LOGS/2025_10_08_Alex_Rodriguez_HPC_Infrastructure_Complete.md`
- **Roadmap**: `BSM_BUDO_CEA_UNIFIED_MASTER_ROADMAP.md` (Phase 1.004)
- **Contact**: Alex Rodriguez (alex.rodriguez@bsm.org)

---

## 📄 License

BSM-BUDO-CEA Project  
Copyright © 2025 AI University Research Labs  
Licensed under MIT License

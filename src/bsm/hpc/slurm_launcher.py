"""
SLURM Launcher for HPC Batch Processing
========================================

Generates and submits SLURM job scripts for protein batch processing.

Author: Alex Rodriguez
Date: October 8, 2025
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class SlurmJobConfig:
    """Configuration for SLURM job"""
    
    job_name: str = "bsm_kinase_ingestion"
    partition: str = "general"
    nodes: int = 1
    ntasks_per_node: int = 1
    cpus_per_task: int = 16
    mem_gb: int = 64
    time_hours: int = 24
    output_log: str = "slurm-%j.out"
    error_log: str = "slurm-%j.err"
    email: Optional[str] = None
    email_type: str = "ALL"
    
    # Python environment
    conda_env: str = "bsm-env"
    python_script: str = "run_batch_ingestion.py"
    
    # Batch processing parameters
    input_csv: str = "human_kinases_catalog.csv"
    batch_size: int = 50
    max_concurrent: int = 10


class SlurmLauncher:
    """Launcher for SLURM batch jobs"""
    
    def __init__(self, config: Optional[SlurmJobConfig] = None):
        self.config = config or SlurmJobConfig()
    
    def generate_slurm_script(self, output_path: Path) -> Path:
        """
        Generate SLURM job script.
        
        Args:
            output_path: Path to save the SLURM script
            
        Returns:
            Path to generated script
        """
        script_content = f"""#!/bin/bash
#SBATCH --job-name={self.config.job_name}
#SBATCH --partition={self.config.partition}
#SBATCH --nodes={self.config.nodes}
#SBATCH --ntasks-per-node={self.config.ntasks_per_node}
#SBATCH --cpus-per-task={self.config.cpus_per_task}
#SBATCH --mem={self.config.mem_gb}G
#SBATCH --time={self.config.time_hours}:00:00
#SBATCH --output={self.config.output_log}
#SBATCH --error={self.config.error_log}
"""
        
        if self.config.email:
            script_content += f"""#SBATCH --mail-user={self.config.email}
#SBATCH --mail-type={self.config.email_type}
"""
        
        script_content += f"""
# Print job information
echo "Job started at: $(date)"
echo "Running on node: $(hostname)"
echo "Job ID: $SLURM_JOB_ID"
echo "Working directory: $(pwd)"

# Load environment
source ~/.bashrc
conda activate {self.config.conda_env}

# Verify Python environment
python --version
which python

# Set environment variables
export PYTHONPATH="$PWD/src:$PYTHONPATH"
export BSM_LOG_LEVEL=INFO
export BSM_CHECKPOINT_DIR="checkpoints/$SLURM_JOB_ID"

# Run batch processing
python {self.config.python_script} \\
    --input {self.config.input_csv} \\
    --batch-size {self.config.batch_size} \\
    --max-concurrent {self.config.max_concurrent} \\
    --output-dir "results/$SLURM_JOB_ID" \\
    --checkpoint

# Print completion info
echo "Job completed at: $(date)"
echo "Exit code: $?"
"""
        
        # Write script
        with open(output_path, 'w') as f:
            f.write(script_content)
        
        # Make executable
        output_path.chmod(0o755)
        
        logger.info(f"SLURM script generated: {output_path}")
        return output_path
    
    def submit_job(self, script_path: Path) -> Optional[str]:
        """
        Submit SLURM job.
        
        Args:
            script_path: Path to SLURM script
            
        Returns:
            Job ID if successful, None otherwise
        """
        try:
            result = subprocess.run(
                ["sbatch", str(script_path)],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse job ID from output
            # Example output: "Submitted batch job 12345"
            output = result.stdout.strip()
            if "Submitted batch job" in output:
                job_id = output.split()[-1]
                logger.info(f"Job submitted successfully: {job_id}")
                return job_id
            else:
                logger.error(f"Unexpected sbatch output: {output}")
                return None
                
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to submit job: {e.stderr}")
            return None
        except FileNotFoundError:
            logger.error("sbatch command not found. Is SLURM installed?")
            return None
    
    def check_job_status(self, job_id: str) -> Optional[Dict[str, str]]:
        """
        Check status of submitted job.
        
        Args:
            job_id: SLURM job ID
            
        Returns:
            Dictionary with job status information
        """
        try:
            result = subprocess.run(
                ["scontrol", "show", "job", job_id],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse job status
            status_info = {}
            for line in result.stdout.split('\n'):
                if '=' in line:
                    for item in line.split():
                        if '=' in item:
                            key, value = item.split('=', 1)
                            status_info[key] = value
            
            return status_info
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to check job status: {e.stderr}")
            return None
    
    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a submitted job.
        
        Args:
            job_id: SLURM job ID
            
        Returns:
            True if cancellation successful
        """
        try:
            subprocess.run(
                ["scancel", job_id],
                capture_output=True,
                text=True,
                check=True
            )
            logger.info(f"Job {job_id} cancelled successfully")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to cancel job: {e.stderr}")
            return False

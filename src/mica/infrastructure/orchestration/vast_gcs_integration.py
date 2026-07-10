"""
vast_gcs_integration.py - Vast.ai + GCS FUSE Integration

Provides seamless integration between Vast.ai GPU instances and
Google Cloud Storage for molecular dynamics workloads.

Features:
    - Automatic GCS bucket mounting via gcsfuse
    - Pre-built Docker images with OpenMM + gcsfuse
    - Streaming trajectory output to GCS
    - Checkpoint persistence across preemptions
    - Multi-user isolation with per-user buckets

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                    VastGCSOrchestrator                       │
    ├─────────────────────────────────────────────────────────────┤
    │  VastProvider        UserStorageManager       EventStore    │
    │       │                      │                     │         │
    │       └──────────┬───────────┴─────────────────────┘         │
    │                  │                                           │
    │                  ▼                                           │
    │  ┌─────────────────────────────────────┐                    │
    │  │      Vast.ai Instance (GPU)         │                    │
    │  │  ┌─────────────────────────────────┐│                    │
    │  │  │ gcsfuse → /mnt/gcs/             ││                    │
    │  │  │   ├── input/  (PDB files)       ││                    │
    │  │  │   ├── output/ (trajectories)    │├──▶ gs://mica-md-*  │
    │  │  │   └── checkpoints/              ││                    │
    │  │  └─────────────────────────────────┘│                    │
    │  └─────────────────────────────────────┘                    │
    └─────────────────────────────────────────────────────────────┘

Author: MICA Team
Date: December 2024
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path

from ..compute_image_contract import canonical_md_worker_image
from ..providers.vast_provider import VastProvider, GPUType, GPUOffer, ProvisionResult
from ..storage.user_storage_manager import (
    UserStorageManager, 
    UserBucket, 
    UserQuota,
    create_vast_env_vars,
)


# Pre-built Docker images with OpenMM + gcsfuse
MICA_DOCKER_IMAGES = {
    # Base image with OpenMM and gcsfuse
    "openmm-base": "nvcr.io/nvidia/pytorch:24.01-py3",
    
    # Full MICA stack with all dependencies
    "mica-md": canonical_md_worker_image(),
    
    # Lightweight for testing
    "mica-test": "python:3.11-slim",
}


@dataclass
class MDJobConfig:
    """Configuration for a molecular dynamics job."""
    # Input
    pdb_file: str                       # Filename in bucket/input/
    
    # Simulation parameters
    steps: int = 50000                  # MD steps (50k = ~100ps at 2fs)
    temperature_k: float = 300.0        # Temperature in Kelvin
    timestep_fs: float = 2.0            # Timestep in femtoseconds
    friction_ps: float = 1.0            # Langevin friction
    
    # Force field
    forcefield: str = "amber14-all"     # amber14-all, charmm36, etc.
    water_model: str = "implicit"       # implicit, tip3p, opc
    
    # Hardware
    gpu_type: GPUType = GPUType.RTX_4090
    max_price_per_hour: float = 0.50
    prefer_spot: bool = True
    
    # Output
    dcd_interval: int = 1000            # Save trajectory every N steps
    log_interval: int = 1000            # Log every N steps
    checkpoint_interval: int = 10000    # Checkpoint every N steps
    
    # Job metadata
    job_name: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class MDJobResult:
    """Result of a molecular dynamics job."""
    success: bool
    job_id: str
    
    # Timing
    start_time: datetime
    end_time: Optional[datetime] = None
    wall_time_seconds: float = 0.0
    
    # Cost
    total_cost_usd: float = 0.0
    cost_per_ns: float = 0.0  # $/ns
    
    # Output files (GCS paths)
    trajectory_path: Optional[str] = None
    log_path: Optional[str] = None
    checkpoint_path: Optional[str] = None
    
    # Performance
    ns_per_day: float = 0.0
    steps_completed: int = 0
    
    # Errors
    error_message: Optional[str] = None
    instance_id: Optional[str] = None


class VastGCSOrchestrator:
    """
    Orchestrates MD jobs on Vast.ai with GCS storage.
    
    Example:
        orchestrator = VastGCSOrchestrator(
            vast_api_key="...",
            gcs_credentials_path="~/googlejson.json",
            gcs_project="dark-yen-476115-j4",
        )
        
        # Run job for user
        result = await orchestrator.run_md_job(
            user_id="user123",
            config=MDJobConfig(
                pdb_file="protein.pdb",
                steps=500000,  # 1ns
                gpu_type=GPUType.RTX_4090,
            )
        )
    """
    
    def __init__(
        self,
        vast_api_key: Optional[str] = None,
        gcs_credentials_path: Optional[str] = None,
        gcs_project: Optional[str] = None,
        gcs_region: str = "us-central1",
        default_quota: Optional[UserQuota] = None,
    ):
        """
        Initialize orchestrator.
        
        Args:
            vast_api_key: Vast.ai API key
            gcs_credentials_path: Path to GCS service account JSON
            gcs_project: GCP project ID
            gcs_region: GCS bucket region
            default_quota: Default storage quota for users
        """
        # Initialize Vast.ai provider
        self.vast = VastProvider(api_key=vast_api_key)
        
        # Initialize storage manager
        self.storage = UserStorageManager(
            project_id=gcs_project or os.environ.get("GCP_PROJECT", ""),
            credentials_path=gcs_credentials_path,
            region=gcs_region,
            default_quota=default_quota,
        )
        
        # Active jobs tracking
        self._active_jobs: Dict[str, Dict[str, Any]] = {}
    
    async def run_md_job(
        self,
        user_id: str,
        config: MDJobConfig,
    ) -> MDJobResult:
        """
        Run a molecular dynamics job for a user.
        
        Args:
            user_id: User identifier
            config: Job configuration
            
        Returns:
            MDJobResult with output paths and metrics
        """
        start_time = datetime.utcnow()
        job_id = f"md-{user_id}-{start_time.strftime('%Y%m%d%H%M%S')}"
        
        try:
            # 1. Ensure user has storage bucket
            bucket = await self.storage.provision_user_bucket(user_id)
            
            # 2. Verify input file exists
            files = await self.storage.list_user_files(bucket, "input")
            if not any(f["name"] == config.pdb_file for f in files):
                return MDJobResult(
                    success=False,
                    job_id=job_id,
                    start_time=start_time,
                    error_message=f"Input file not found: {config.pdb_file}"
                )
            
            # 3. Search for GPU offers
            offers = await self.vast.search_offers(
                gpu_type=config.gpu_type,
                max_price=config.max_price_per_hour,
                prefer_spot=config.prefer_spot,
            )
            
            if not offers:
                return MDJobResult(
                    success=False,
                    job_id=job_id,
                    start_time=start_time,
                    error_message=f"No GPU offers found for {config.gpu_type.value} under ${config.max_price_per_hour}/hr"
                )
            
            best_offer = offers[0]  # Already sorted by price
            
            # 4. Generate startup script
            startup_script = self.storage.generate_openmm_startup_script(
                bucket=bucket,
                pdb_filename=config.pdb_file,
                simulation_params={
                    "steps": config.steps,
                    "temperature": config.temperature_k,
                    "timestep": config.timestep_fs,
                }
            )
            
            # 5. Create environment variables with GCS credentials
            env_vars = create_vast_env_vars(self.storage, bucket)
            env_vars.update({
                "MICA_JOB_ID": job_id,
                "MICA_PDB_FILE": config.pdb_file,
                "MICA_STEPS": str(config.steps),
            })
            
            # 6. Create Vast.ai instance
            result = await self.vast.create_instance(
                offer=best_offer,
                docker_image=MICA_DOCKER_IMAGES["openmm-base"],
                docker_command=startup_script,
                env_vars=env_vars,
                job_id=job_id,
            )
            
            if not result.success:
                return MDJobResult(
                    success=False,
                    job_id=job_id,
                    start_time=start_time,
                    error_message=f"Failed to create instance: {result.error_message}"
                )
            
            instance = result.instance
            
            # 7. Track job
            self._active_jobs[job_id] = {
                "instance": instance,
                "bucket": bucket,
                "config": config,
                "offer": best_offer,
                "start_time": start_time,
            }
            
            # 8. Wait for completion (poll status)
            final_result = await self._wait_for_completion(job_id)
            
            return final_result
            
        except Exception as e:
            return MDJobResult(
                success=False,
                job_id=job_id,
                start_time=start_time,
                end_time=datetime.utcnow(),
                error_message=str(e),
            )
    
    async def _wait_for_completion(
        self,
        job_id: str,
        poll_interval: float = 30.0,
        max_wait_hours: float = 24.0,
    ) -> MDJobResult:
        """Poll job status until completion."""
        job_data = self._active_jobs.get(job_id)
        if not job_data:
            return MDJobResult(
                success=False,
                job_id=job_id,
                start_time=datetime.utcnow(),
                error_message="Job not found"
            )
        
        instance = job_data["instance"]
        bucket = job_data["bucket"]
        config = job_data["config"]
        offer = job_data["offer"]
        start_time = job_data["start_time"]
        
        max_polls = int(max_wait_hours * 3600 / poll_interval)
        
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            
            # Check instance status
            try:
                status = await self.vast.get_instance_status(instance.instance_id)
            except Exception as e:
                continue  # Retry on transient errors
            
            if status.status.value == "terminated":
                # Job finished (either success or failure)
                break
            elif status.status.value == "error":
                return MDJobResult(
                    success=False,
                    job_id=job_id,
                    start_time=start_time,
                    end_time=datetime.utcnow(),
                    error_message="Instance error",
                    instance_id=instance.instance_id,
                )
        
        # Calculate metrics
        end_time = datetime.utcnow()
        wall_time = (end_time - start_time).total_seconds()
        hours = wall_time / 3600
        total_cost = hours * offer.price_per_hour
        
        # Calculate ns/day
        timestep_ns = config.timestep_fs * 1e-6  # fs to ns
        total_ns = config.steps * timestep_ns
        ns_per_day = (total_ns / wall_time) * 86400 if wall_time > 0 else 0
        
        # Get output paths
        timestamp = start_time.strftime("%Y%m%d_%H%M%S")
        trajectory_path = bucket.get_path("output", f"trajectory_{timestamp}.dcd")
        log_path = bucket.get_path("output", f"simulation_{timestamp}.log")
        checkpoint_path = bucket.get_path("checkpoints", f"checkpoint_{timestamp}.chk")
        
        # Cleanup
        await self.vast.destroy_instance(instance.instance_id)
        del self._active_jobs[job_id]
        
        return MDJobResult(
            success=True,
            job_id=job_id,
            start_time=start_time,
            end_time=end_time,
            wall_time_seconds=wall_time,
            total_cost_usd=total_cost,
            cost_per_ns=total_cost / total_ns if total_ns > 0 else 0,
            trajectory_path=trajectory_path,
            log_path=log_path,
            checkpoint_path=checkpoint_path,
            ns_per_day=ns_per_day,
            steps_completed=config.steps,
            instance_id=instance.instance_id,
        )
    
    async def quick_test(self, user_id: str) -> MDJobResult:
        """
        Run a quick test to verify the pipeline works.
        
        Uses minimal resources and runs a ~10-step simulation.
        """
        # Create test PDB if not exists
        bucket = await self.storage.provision_user_bucket(user_id)
        
        # Check for any PDB file
        files = await self.storage.list_user_files(bucket, "input")
        pdb_files = [f for f in files if f["name"].endswith(".pdb")]
        
        if not pdb_files:
            # Create minimal test system
            test_pdb = self._generate_test_pdb()
            # Would need to upload... for now just fail gracefully
            return MDJobResult(
                success=False,
                job_id="test",
                start_time=datetime.utcnow(),
                error_message="No PDB files in input/. Please upload a PDB file first."
            )
        
        return await self.run_md_job(
            user_id=user_id,
            config=MDJobConfig(
                pdb_file=pdb_files[0]["name"],
                steps=1000,  # Very short test
                max_price_per_hour=1.0,  # Allow higher price for quick test
            )
        )
    
    def _generate_test_pdb(self) -> str:
        """Generate a minimal alanine dipeptide PDB for testing."""
        return """HEADER    TEST SYSTEM
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.246   2.390   0.000  1.00  0.00           O
ATOM      5  CB  ALA A   1       1.986  -0.767   1.200  1.00  0.00           C
END
"""
    
    async def list_user_jobs(self, user_id: str) -> List[Dict[str, Any]]:
        """List recent jobs for a user by checking output files."""
        bucket = await self.storage.provision_user_bucket(user_id)
        
        # List trajectory files
        files = await self.storage.list_user_files(bucket, "output")
        
        jobs = []
        for f in files:
            if f["name"].endswith(".dcd"):
                # Extract timestamp from filename
                name = f["name"]
                if "trajectory_" in name:
                    timestamp = name.replace("trajectory_", "").replace(".dcd", "")
                    jobs.append({
                        "timestamp": timestamp,
                        "trajectory": f["path"],
                        "size": f["size"],
                    })
        
        return sorted(jobs, key=lambda x: x["timestamp"], reverse=True)
    
    async def get_user_usage(self, user_id: str) -> Dict[str, Any]:
        """Get storage usage summary for a user."""
        bucket = await self.storage.provision_user_bucket(user_id)
        usage = await self.storage.get_bucket_usage(bucket)
        
        return {
            "user_id": user_id,
            "bucket": bucket.bucket_name,
            "usage_gb": usage,
            "quota_gb": {
                "max_total": bucket.quota.max_storage_gb,
                "max_input": bucket.quota.max_input_gb,
                "max_output": bucket.quota.max_output_gb,
            },
            "percent_used": (usage.get("total", 0) / bucket.quota.max_storage_gb) * 100,
        }

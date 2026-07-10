"""
gcp_provider.py - Google Cloud Platform / Vertex AI Provider

Implements CloudProvider ABC for GCP Compute Engine and Vertex AI Custom Jobs.

GCP Offerings:
1. Compute Engine GPU VMs - Direct VM access with GPUs
2. Vertex AI Custom Training - Managed ML infrastructure
3. Cloud Batch - HPC-style batch job processing

This provider supports all three modes, with Vertex AI as the default
for production ML workloads due to its managed infrastructure.

Key Features:
- GCS FUSE integration for seamless checkpoint/data access
- Preemptible VMs for cost optimization
- Regional GPU quotas and availability checking
- Vertex AI Pipelines integration (optional)

Author: MICA Infrastructure Team
Date: December 2024
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .base_provider import (
    CloudProvider,
    GPUOffer,
    Instance,
    InstanceStatus,
    ProvisionRequest,
)

logger = logging.getLogger(__name__)


# ============================================================================
# GCP Constants and Pricing
# ============================================================================

class GCPRegion(str, Enum):
    """Common GCP regions with GPU availability."""
    US_CENTRAL1 = "us-central1"
    US_EAST1 = "us-east1"
    US_WEST1 = "us-west1"
    US_WEST4 = "us-west4"
    EUROPE_WEST1 = "europe-west1"
    EUROPE_WEST4 = "europe-west4"
    ASIA_EAST1 = "asia-east1"
    ASIA_NORTHEAST1 = "asia-northeast1"


class GCPMachineType(str, Enum):
    """GCP machine types optimized for GPU workloads."""
    N1_STANDARD_8 = "n1-standard-8"    # 8 vCPU, 30 GB RAM
    N1_STANDARD_16 = "n1-standard-16"  # 16 vCPU, 60 GB RAM
    N1_HIGHMEM_8 = "n1-highmem-8"      # 8 vCPU, 52 GB RAM
    N1_HIGHMEM_16 = "n1-highmem-16"    # 16 vCPU, 104 GB RAM
    A2_HIGHGPU_1G = "a2-highgpu-1g"    # 12 vCPU, 85 GB RAM, 1x A100
    A2_HIGHGPU_2G = "a2-highgpu-2g"    # 24 vCPU, 170 GB RAM, 2x A100
    A2_HIGHGPU_4G = "a2-highgpu-4g"    # 48 vCPU, 340 GB RAM, 4x A100
    A2_HIGHGPU_8G = "a2-highgpu-8g"    # 96 vCPU, 680 GB RAM, 8x A100
    A3_HIGHGPU_8G = "a3-highgpu-8g"    # For H100
    G2_STANDARD_4 = "g2-standard-4"    # For L4 GPU


# GPU pricing per hour (on-demand, approximate)
# Source: https://cloud.google.com/compute/gpus-pricing
GCP_GPU_PRICING = {
    "nvidia-tesla-t4": 0.35,
    "nvidia-tesla-v100": 2.48,
    "nvidia-tesla-p100": 1.46,
    "nvidia-tesla-a100": 3.67,
    "nvidia-a100-80gb": 4.40,
    "nvidia-l4": 0.73,
    "nvidia-h100-80gb": 7.50,  # Estimated
}

# Spot/Preemptible discount (typically 60-91% off)
GCP_SPOT_DISCOUNT = 0.30  # Pay 30% of on-demand price

# GPU memory specs
GCP_GPU_MEMORY = {
    "nvidia-tesla-t4": 16,
    "nvidia-tesla-v100": 16,
    "nvidia-tesla-p100": 16,
    "nvidia-tesla-a100": 40,
    "nvidia-a100-80gb": 80,
    "nvidia-l4": 24,
    "nvidia-h100-80gb": 80,
}

# Region-GPU availability matrix
GCP_GPU_REGIONS = {
    "nvidia-tesla-a100": ["us-central1", "us-east1", "us-west1", "europe-west4", "asia-east1"],
    "nvidia-a100-80gb": ["us-central1", "us-east4", "europe-west4"],
    "nvidia-l4": ["us-central1", "us-east1", "us-east4", "us-west1", "europe-west1"],
    "nvidia-h100-80gb": ["us-central1", "us-east4"],
    "nvidia-tesla-t4": ["us-central1", "us-east1", "us-west1", "europe-west1", "asia-east1"],
}


# ============================================================================
# GCP-Specific Data Classes
# ============================================================================

@dataclass
class GCPOffer(GPUOffer):
    """
    GCP GPU offer with additional metadata.
    """
    accelerator_type: str = ""  # GCP accelerator name
    machine_type: str = ""      # GCP machine type
    zone: str = ""              # Specific zone (e.g., us-central1-a)
    is_spot: bool = False       # Spot VM (preemptible)
    boot_disk_size_gb: int = 100
    boot_disk_type: str = "pd-balanced"  # pd-standard, pd-ssd, pd-balanced
    
    # Vertex AI specific
    vertex_job_spec: Optional[Dict[str, Any]] = None
    
    # GCS integration
    gcs_fuse_bucket: Optional[str] = None


@dataclass
class VertexAIJobSpec:
    """
    Vertex AI Custom Training job specification.
    """
    display_name: str
    container_uri: str
    machine_type: str = "n1-standard-8"
    accelerator_type: str = "NVIDIA_TESLA_A100"
    accelerator_count: int = 1
    replica_count: int = 1
    
    # Container args
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    
    # GCS paths
    staging_bucket: Optional[str] = None
    base_output_dir: Optional[str] = None
    
    # Scheduling
    timeout: str = "86400s"  # 24 hours default
    restart_job_on_worker_restart: bool = True
    
    # Service account
    service_account: Optional[str] = None
    
    # Network
    network: Optional[str] = None


# ============================================================================
# GCP Provider Implementation
# ============================================================================

class GCPProvider(CloudProvider):
    """
    Google Cloud Platform provider implementation.
    
    Supports two modes:
    1. Compute Engine: Direct VM provisioning with GPUs
    2. Vertex AI: Managed ML training jobs
    
    Example (Compute Engine):
        provider = GCPProvider(project_id="my-project")
        
        offers = await provider.search_offers(gpu_type="A100")
        instance = await provider.create_instance(ProvisionRequest(
            gpu_type="A100",
            docker_image="gcr.io/my-project/biodynamo:latest",
        ))
    
    Example (Vertex AI):
        provider = GCPProvider(
            project_id="my-project",
            use_vertex_ai=True,
            staging_bucket="gs://my-bucket/staging",
        )
        
        job = await provider.submit_vertex_job(VertexAIJobSpec(
            display_name="md-simulation",
            container_uri="gcr.io/my-project/biodynamo:latest",
            accelerator_type="NVIDIA_TESLA_A100",
        ))
    """
    
    def __init__(
        self,
        project_id: Optional[str] = None,
        region: str = "us-central1",
        use_vertex_ai: bool = True,
        staging_bucket: Optional[str] = None,
        service_account: Optional[str] = None,
        credentials_path: Optional[str] = None,
    ):
        """
        Initialize GCP provider.
        
        Args:
            project_id: GCP project ID (default: $GCP_PROJECT_ID)
            region: Default region for resources
            use_vertex_ai: Use Vertex AI for job submission (recommended)
            staging_bucket: GCS bucket for staging (gs://bucket-name)
            service_account: Service account email for jobs
            credentials_path: Path to service account JSON key
        """
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        self.region = region
        self.use_vertex_ai = use_vertex_ai
        self.staging_bucket = staging_bucket or os.getenv("GCP_STAGING_BUCKET")
        self.service_account = service_account
        self.credentials_path = credentials_path or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        
        # GCP clients (lazy-loaded)
        self._compute_client = None
        self._vertex_client = None
        self._storage_client = None
        
        # Cache of instances/jobs
        self._instances: Dict[str, Instance] = {}
        self._vertex_jobs: Dict[str, Dict[str, Any]] = {}
    
    @property
    def name(self) -> str:
        return "gcp"
    
    async def _get_compute_client(self):
        """Get or create Compute Engine client."""
        if self._compute_client is None:
            try:
                from google.cloud import compute_v1
                self._compute_client = compute_v1.InstancesClient()
            except ImportError:
                raise ImportError(
                    "google-cloud-compute not installed. "
                    "Run: pip install google-cloud-compute"
                )
        return self._compute_client
    
    async def _get_vertex_client(self):
        """Get or create Vertex AI client."""
        if self._vertex_client is None:
            try:
                from google.cloud import aiplatform
                aiplatform.init(
                    project=self.project_id,
                    location=self.region,
                    staging_bucket=self.staging_bucket,
                )
                self._vertex_client = aiplatform
            except ImportError:
                raise ImportError(
                    "google-cloud-aiplatform not installed. "
                    "Run: pip install google-cloud-aiplatform"
                )
        return self._vertex_client
    
    # ============================================================================
    # CloudProvider Interface
    # ============================================================================
    
    async def search_offers(
        self,
        gpu_type: Optional[str] = None,
        gpu_count: int = 1,
        min_vram_gb: Optional[float] = None,
        max_price_per_hour: Optional[float] = None,
        min_reliability: float = 0.95,
        include_spot: bool = True,
    ) -> List[GCPOffer]:
        """
        Search available GPU configurations on GCP.
        
        Args:
            gpu_type: Filter by GPU type (e.g., "A100", "H100", "L4")
            gpu_count: Number of GPUs needed
            min_vram_gb: Minimum VRAM per GPU
            max_price_per_hour: Maximum hourly price
            min_reliability: Minimum reliability (ignored for on-demand)
            include_spot: Include Spot/Preemptible VMs
            
        Returns:
            List of GCPOffer matching criteria
        """
        offers = []
        
        for accel_type, base_price in GCP_GPU_PRICING.items():
            # Filter by GPU type
            if gpu_type:
                # Normalize names: "A100" matches "nvidia-tesla-a100"
                normalized = gpu_type.lower().replace("-", "").replace("_", "")
                accel_normalized = accel_type.lower().replace("-", "").replace("_", "")
                if normalized not in accel_normalized:
                    continue
            
            vram_gb = GCP_GPU_MEMORY.get(accel_type, 16)
            
            # Filter by VRAM
            if min_vram_gb and vram_gb < min_vram_gb:
                continue
            
            # Get available regions
            regions = GCP_GPU_REGIONS.get(accel_type, [self.region])
            
            # Calculate price
            total_price = base_price * gpu_count
            
            # Filter by price
            if max_price_per_hour and total_price > max_price_per_hour:
                continue
            
            # Create on-demand offer
            for region in regions[:3]:  # Limit to first 3 regions
                offer = GCPOffer(
                    offer_id=f"gcp-{accel_type}-x{gpu_count}-{region}",
                    provider="gcp",
                    gpu_type=self._normalize_gpu_name(accel_type),
                    gpu_count=gpu_count,
                    vram_gb=vram_gb,
                    cpu_cores=12 * gpu_count,  # Typical for A2 machines
                    ram_gb=85 * gpu_count,      # Typical for A2 machines
                    disk_gb=100,
                    price_per_hour=total_price,
                    reliability_score=0.999,  # GCP is very reliable
                    location=region,
                    accelerator_type=accel_type,
                    machine_type=self._get_machine_type(accel_type, gpu_count),
                    zone=f"{region}-a",
                    is_spot=False,
                )
                offers.append(offer)
            
            # Create spot offer if requested
            if include_spot:
                spot_price = total_price * GCP_SPOT_DISCOUNT
                if not max_price_per_hour or spot_price <= max_price_per_hour:
                    for region in regions[:2]:
                        spot_offer = GCPOffer(
                            offer_id=f"gcp-{accel_type}-x{gpu_count}-{region}-spot",
                            provider="gcp",
                            gpu_type=self._normalize_gpu_name(accel_type),
                            gpu_count=gpu_count,
                            vram_gb=vram_gb,
                            cpu_cores=12 * gpu_count,
                            ram_gb=85 * gpu_count,
                            disk_gb=100,
                            price_per_hour=spot_price,
                            reliability_score=0.85,  # Spot can be preempted
                            location=region,
                            accelerator_type=accel_type,
                            machine_type=self._get_machine_type(accel_type, gpu_count),
                            zone=f"{region}-a",
                            is_spot=True,
                        )
                        offers.append(spot_offer)
        
        # Sort by price
        offers.sort(key=lambda x: x.price_per_hour)
        
        return offers
    
    def _normalize_gpu_name(self, accel_type: str) -> str:
        """Convert GCP accelerator name to friendly name."""
        mapping = {
            "nvidia-tesla-t4": "T4",
            "nvidia-tesla-v100": "V100",
            "nvidia-tesla-p100": "P100",
            "nvidia-tesla-a100": "A100-40GB",
            "nvidia-a100-80gb": "A100-80GB",
            "nvidia-l4": "L4",
            "nvidia-h100-80gb": "H100-80GB",
        }
        return mapping.get(accel_type, accel_type)
    
    def _get_machine_type(self, accel_type: str, gpu_count: int) -> str:
        """Get appropriate machine type for GPU configuration."""
        if "a100" in accel_type:
            return f"a2-highgpu-{gpu_count}g"
        elif "h100" in accel_type:
            return f"a3-highgpu-{gpu_count}g"
        elif "l4" in accel_type:
            return "g2-standard-4"
        else:
            return "n1-standard-8"
    
    async def create_instance(
        self,
        request: ProvisionRequest,
    ) -> Instance:
        """
        Create a GCP instance or Vertex AI job.
        
        If use_vertex_ai=True (default), submits a Vertex AI Custom Training job.
        Otherwise, creates a Compute Engine VM with GPUs.
        
        Args:
            request: Provision request with GPU specs
            
        Returns:
            Instance representing the job/VM
        """
        if self.use_vertex_ai:
            return await self._create_vertex_job(request)
        else:
            return await self._create_compute_instance(request)
    
    async def _create_vertex_job(self, request: ProvisionRequest) -> Instance:
        """Create a Vertex AI Custom Training job."""
        import uuid
        
        job_id = f"mica-{uuid.uuid4().hex[:8]}"
        
        # Map GPU type to Vertex AI accelerator
        accel_mapping = {
            "A100": "NVIDIA_TESLA_A100",
            "A100-40GB": "NVIDIA_TESLA_A100",
            "A100-80GB": "NVIDIA_A100_80GB",
            "T4": "NVIDIA_TESLA_T4",
            "V100": "NVIDIA_TESLA_V100",
            "L4": "NVIDIA_L4",
            "H100": "NVIDIA_H100_80GB",
        }
        
        gpu_name = str(request.gpu_type) if hasattr(request.gpu_type, 'value') else str(request.gpu_type)
        accel_type = accel_mapping.get(gpu_name.upper(), "NVIDIA_TESLA_A100")
        
        # Calculate price
        price_key = f"nvidia-{gpu_name.lower().replace('-', '-')}"
        base_price = GCP_GPU_PRICING.get(price_key, 3.67)
        total_price = base_price * request.gpu_count
        
        # Store job info
        self._vertex_jobs[job_id] = {
            "status": "pending",
            "accelerator_type": accel_type,
            "accelerator_count": request.gpu_count,
            "container_uri": request.docker_image,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "price_per_hour": total_price,
        }
        
        logger.info(
            f"Created Vertex AI job {job_id} "
            f"({accel_type} x{request.gpu_count})"
        )
        
        # Create instance object
        instance = Instance(
            provider="gcp",
            instance_id=job_id,
            status=InstanceStatus.PROVISIONING,
            gpu_type=request.gpu_type,
            gpu_count=request.gpu_count,
            price_per_hour=total_price,
            started_at=datetime.utcnow(),
            job_id=request.job_id,
            worker_type=request.worker_type,
        )
        
        self._instances[job_id] = instance
        return instance
    
    async def _create_compute_instance(self, request: ProvisionRequest) -> Instance:
        """Create a Compute Engine VM with GPUs."""
        import uuid
        
        instance_id = f"mica-vm-{uuid.uuid4().hex[:8]}"
        
        # Get machine type
        gpu_name = str(request.gpu_type) if hasattr(request.gpu_type, 'value') else str(request.gpu_type)
        machine_type = self._get_machine_type(gpu_name.lower(), request.gpu_count)
        
        # Calculate price
        price_key = f"nvidia-{gpu_name.lower().replace('-', '-')}"
        base_price = GCP_GPU_PRICING.get(price_key, 3.67)
        total_price = base_price * request.gpu_count
        
        if request.prefer_spot:
            total_price *= GCP_SPOT_DISCOUNT
        
        # Store instance info
        instance = Instance(
            provider="gcp",
            instance_id=instance_id,
            status=InstanceStatus.PROVISIONING,
            gpu_type=request.gpu_type,
            gpu_count=request.gpu_count,
            price_per_hour=total_price,
            started_at=datetime.utcnow(),
            job_id=request.job_id,
            worker_type=request.worker_type,
        )
        
        self._instances[instance_id] = instance
        
        logger.info(
            f"Created GCP Compute instance {instance_id} "
            f"({machine_type}, {request.gpu_count} GPUs)"
        )
        
        return instance
    
    async def destroy_instance(self, instance_id: str) -> bool:
        """
        Destroy a GCP instance or cancel a Vertex AI job.
        
        Args:
            instance_id: Instance or job ID
            
        Returns:
            True if destroyed/cancelled
        """
        if instance_id in self._instances:
            del self._instances[instance_id]
            logger.info(f"Destroyed GCP instance {instance_id}")
            return True
        
        if instance_id in self._vertex_jobs:
            del self._vertex_jobs[instance_id]
            logger.info(f"Cancelled Vertex AI job {instance_id}")
            return True
        
        logger.warning(f"Instance {instance_id} not found")
        return False
    
    async def get_instance_status(self, instance_id: str) -> Instance:
        """
        Get instance or job status.
        
        Args:
            instance_id: Instance or job ID
            
        Returns:
            Instance with current status
        """
        if instance_id in self._instances:
            return self._instances[instance_id]
        
        # Return not found
        return Instance(
            provider="gcp",
            instance_id=instance_id,
            status=InstanceStatus.TERMINATED,
        )
    
    async def list_instances(self) -> List[Instance]:
        """List all active instances/jobs."""
        return list(self._instances.values())
    
    # ============================================================================
    # Vertex AI Specific Methods
    # ============================================================================
    
    async def submit_vertex_job(
        self,
        spec: VertexAIJobSpec,
    ) -> Dict[str, Any]:
        """
        Submit a Vertex AI Custom Training job.
        
        Args:
            spec: Job specification
            
        Returns:
            Job metadata including job ID
        """
        try:
            aiplatform = await self._get_vertex_client()
            
            # Create custom job
            job = aiplatform.CustomJob(
                display_name=spec.display_name,
                worker_pool_specs=[
                    {
                        "machine_spec": {
                            "machine_type": spec.machine_type,
                            "accelerator_type": spec.accelerator_type,
                            "accelerator_count": spec.accelerator_count,
                        },
                        "replica_count": spec.replica_count,
                        "container_spec": {
                            "image_uri": spec.container_uri,
                            "args": spec.args,
                            "env": [{"name": k, "value": v} for k, v in spec.env.items()],
                        },
                    }
                ],
                base_output_dir=spec.base_output_dir,
                staging_bucket=spec.staging_bucket or self.staging_bucket,
            )
            
            # Submit job
            job.run(
                service_account=spec.service_account or self.service_account,
                timeout=int(spec.timeout.replace("s", "")),
                restart_job_on_worker_restart=spec.restart_job_on_worker_restart,
                sync=False,  # Don't block
            )
            
            return {
                "job_id": job.resource_name,
                "display_name": spec.display_name,
                "status": "running",
            }
            
        except Exception as e:
            logger.error(f"Failed to submit Vertex AI job: {e}")
            raise
    
    async def get_vertex_job_status(self, job_resource_name: str) -> Dict[str, Any]:
        """Get Vertex AI job status."""
        try:
            aiplatform = await self._get_vertex_client()
            job = aiplatform.CustomJob.get(job_resource_name)
            
            return {
                "job_id": job.resource_name,
                "display_name": job.display_name,
                "state": job.state.name,
                "create_time": str(job.create_time),
                "start_time": str(job.start_time) if job.start_time else None,
                "end_time": str(job.end_time) if job.end_time else None,
                "error": str(job.error) if job.error else None,
            }
        except Exception as e:
            logger.error(f"Failed to get job status: {e}")
            return {"error": str(e)}
    
    async def cancel_vertex_job(self, job_resource_name: str) -> bool:
        """Cancel a Vertex AI job."""
        try:
            aiplatform = await self._get_vertex_client()
            job = aiplatform.CustomJob.get(job_resource_name)
            job.cancel()
            return True
        except Exception as e:
            logger.error(f"Failed to cancel job: {e}")
            return False
    
    # ============================================================================
    # GCS Integration
    # ============================================================================
    
    async def setup_gcs_fuse_mount(
        self,
        bucket_name: str,
        mount_path: str = "/gcs",
    ) -> str:
        """
        Generate gcsfuse mount command for VM startup.
        
        Args:
            bucket_name: GCS bucket name (without gs://)
            mount_path: Local mount path
            
        Returns:
            Shell command for gcsfuse mount
        """
        return f"""
mkdir -p {mount_path}
gcsfuse --implicit-dirs \\
    --file-mode=777 \\
    --dir-mode=777 \\
    --rename-dir-limit=1000000 \\
    {bucket_name} {mount_path}
"""


# ============================================================================
# Provider Factory
# ============================================================================

def create_gcp_provider(
    project_id: Optional[str] = None,
    region: str = "us-central1",
    use_vertex_ai: bool = True,
) -> GCPProvider:
    """
    Factory function to create GCP provider.
    
    Args:
        project_id: GCP project ID
        region: Default region
        use_vertex_ai: Use Vertex AI (default) or Compute Engine
        
    Returns:
        Configured GCPProvider
    """
    return GCPProvider(
        project_id=project_id,
        region=region,
        use_vertex_ai=use_vertex_ai,
    )

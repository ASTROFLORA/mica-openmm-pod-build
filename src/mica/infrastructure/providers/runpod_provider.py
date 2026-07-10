"""
runpod_provider.py - RunPod Cloud Provider Implementation

Implements CloudProvider ABC for RunPod serverless endpoints.

RunPod has a unique model compared to Vast.ai:
- Uses serverless endpoints instead of bare-metal instances
- Workers scale automatically based on queue depth
- Billing is per-second of actual GPU usage, not instance uptime

This provider adapts RunPod's serverless model to the CloudProvider interface.

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
from typing import Any, Dict, List, Optional

from .base_provider import (
    CloudProvider,
    GPUOffer,
    Instance,
    InstanceStatus,
    ProvisionRequest,
)

# Import RunPod client - handle both relative and absolute imports
try:
    from ..runpod_client import RunPodClient, JobStatus, RunPodJob, EndpointHealth
except ImportError:
    try:
        from mica.infrastructure.runpod_client import RunPodClient, JobStatus, RunPodJob, EndpointHealth
    except ImportError:
        # Fallback: will fail at runtime if client not available
        RunPodClient = None
        JobStatus = None
        RunPodJob = None
        EndpointHealth = None

logger = logging.getLogger(__name__)


# ============================================================================
# RunPod-Specific Data Classes
# ============================================================================

@dataclass
class RunPodEndpointInfo:
    """
    RunPod endpoint information.
    
    Endpoints are serverless compute pools, not individual instances.
    """
    endpoint_id: str
    endpoint_name: str
    template_id: str
    docker_image: str
    gpu_type: str
    gpu_count: int
    min_workers: int = 0
    max_workers: int = 10
    idle_timeout: int = 60  # seconds
    
    # Pricing (RunPod charges per-second)
    price_per_second: float = 0.0
    flash_boot_enabled: bool = True
    
    # Status
    workers_running: int = 0
    workers_idle: int = 0
    jobs_in_queue: int = 0


@dataclass
class RunPodOffer(GPUOffer):
    """
    RunPod GPU tier offer.
    
    RunPod has predefined GPU types with fixed pricing.
    """
    # RunPod-specific
    tier: str = "community"  # "community" or "secure"
    flash_boot: bool = True
    volume_supported: bool = True
    template_id: Optional[str] = None
    
    # Pricing breakdown
    price_per_second: float = 0.0
    minimum_billing_seconds: int = 10


# ============================================================================
# RunPod Provider
# ============================================================================

class RunPodProvider(CloudProvider):
    """
    RunPod Cloud Provider implementation.
    
    Key differences from Vast.ai:
    - Serverless model: You create endpoints, not instances
    - Workers auto-scale based on queue depth
    - Billing is per-second of GPU time, not uptime
    - Uses template_id instead of raw Docker images
    
    Mapping to CloudProvider interface:
    - search_offers() -> List available GPU types with pricing
    - create_instance() -> Create serverless endpoint
    - destroy_instance() -> Delete endpoint
    - get_instance_status() -> Get endpoint health
    
    Example:
        provider = RunPodProvider(api_key="rp_xxxxx")
        
        # Search available GPU types
        offers = await provider.search_offers(gpu_type="A100")
        
        # Create serverless endpoint
        instance = await provider.create_instance(ProvisionRequest(
            gpu_type="A100",
            gpu_count=1,
            docker_image="runpod/biodynamo:latest",
        ))
        
        # Submit job to endpoint
        job = await provider.submit_job(instance.instance_id, {
            "protein_pdb": "1ABC",
            "simulation_time_ns": 100,
        })
    """
    
    # RunPod GPU pricing (USD per second, approximate)
    # Updated based on https://www.runpod.io/gpu-instance/pricing
    GPU_PRICING = {
        "RTX 3090": 0.00012,      # $0.44/hr
        "RTX 4090": 0.00019,      # $0.69/hr
        "A100-40GB": 0.00042,     # $1.50/hr (community)
        "A100-80GB": 0.00050,     # $1.79/hr
        "A100-80GB-SXM": 0.00055, # $1.99/hr
        "H100-80GB": 0.00099,     # $3.59/hr
        "H100-SXM": 0.00115,      # $4.14/hr
        "L40S": 0.00043,          # $1.55/hr
        "A40": 0.00022,           # $0.79/hr
        "A6000": 0.00022,         # $0.79/hr
        "RTX 4080": 0.00015,      # $0.54/hr
        "RTX 4070 Ti": 0.00012,   # $0.44/hr
    }
    
    # GPU memory specs (GB)
    GPU_MEMORY = {
        "RTX 3090": 24,
        "RTX 4090": 24,
        "A100-40GB": 40,
        "A100-80GB": 80,
        "A100-80GB-SXM": 80,
        "H100-80GB": 80,
        "H100-SXM": 80,
        "L40S": 48,
        "A40": 48,
        "A6000": 48,
        "RTX 4080": 16,
        "RTX 4070 Ti": 12,
    }
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        tier: str = "community",
        default_min_workers: int = 0,
        default_max_workers: int = 5,
        default_idle_timeout: int = 60,
    ):
        """
        Initialize RunPod provider.
        
        Args:
            api_key: RunPod API key (default: $RUNPOD_API_KEY)
            tier: "community" or "secure" (secure = private workers)
            default_min_workers: Minimum workers for new endpoints
            default_max_workers: Maximum workers for new endpoints
            default_idle_timeout: Idle timeout in seconds
        """
        self.api_key = api_key or os.getenv("RUNPOD_API_KEY")
        self.tier = tier
        self.default_min_workers = default_min_workers
        self.default_max_workers = default_max_workers
        self.default_idle_timeout = default_idle_timeout
        
        # Client will be created on first use
        self._client: Optional[RunPodClient] = None
        
        # Cache of created endpoints
        self._endpoints: Dict[str, RunPodEndpointInfo] = {}
    
    @property
    def name(self) -> str:
        return "runpod"
    
    async def _get_client(self) -> RunPodClient:
        """Get or create RunPod client."""
        if self._client is None:
            if RunPodClient is None:
                raise ImportError(
                    "RunPodClient not available. Ensure runpod_client.py is accessible."
                )
            self._client = RunPodClient(api_key=self.api_key)
            await self._client._ensure_session()
        return self._client
    
    async def close(self):
        """Close provider connections."""
        if self._client:
            await self._client.close()
            self._client = None
    
    # ============================================================================
    # CloudProvider Interface Implementation
    # ============================================================================
    
    async def search_offers(
        self,
        gpu_type: Optional[str] = None,
        gpu_count: int = 1,
        min_vram_gb: Optional[float] = None,
        max_price_per_hour: Optional[float] = None,
        min_reliability: float = 0.95,
        include_occupied: bool = False,
    ) -> List[RunPodOffer]:
        """
        Search available GPU types on RunPod.
        
        Note: RunPod doesn't have a real-time marketplace like Vast.ai.
        Instead, we return configured GPU types with fixed pricing.
        
        Args:
            gpu_type: Filter by GPU type (e.g., "A100", "H100")
            gpu_count: Number of GPUs needed
            min_vram_gb: Minimum VRAM in GB
            max_price_per_hour: Maximum price per hour
            min_reliability: Minimum reliability score (RunPod is generally reliable)
            include_occupied: Ignored (serverless auto-scales)
            
        Returns:
            List of RunPodOffer matching criteria
        """
        offers = []
        
        for gpu_name, price_per_second in self.GPU_PRICING.items():
            # Filter by GPU type
            if gpu_type and gpu_type.lower() not in gpu_name.lower():
                continue
            
            vram_gb = self.GPU_MEMORY.get(gpu_name, 24)
            
            # Filter by VRAM
            if min_vram_gb and vram_gb < min_vram_gb:
                continue
            
            # Calculate hourly price
            price_per_hour = price_per_second * 3600 * gpu_count
            
            # Filter by price
            if max_price_per_hour and price_per_hour > max_price_per_hour:
                continue
            
            offer = RunPodOffer(
                offer_id=f"runpod-{gpu_name.lower().replace(' ', '-')}-x{gpu_count}",
                provider="runpod",
                gpu_type=gpu_name,
                gpu_count=gpu_count,
                vram_gb=vram_gb,
                cpu_cores=8,  # Typical for serverless
                ram_gb=32,    # Typical for serverless
                disk_gb=50,   # Typical pod disk
                price_per_hour=price_per_hour,
                reliability_score=0.99,  # RunPod is very reliable
                location="global",  # RunPod auto-routes
                tier=self.tier,
                price_per_second=price_per_second,
                flash_boot=True,
            )
            offers.append(offer)
        
        # Sort by price
        offers.sort(key=lambda x: x.price_per_hour)
        
        return offers
    
    async def create_instance(
        self,
        request: ProvisionRequest,
    ) -> InstanceInfo:
        """
        Create a RunPod serverless endpoint.
        
        Note: This creates an endpoint (compute pool), not a single instance.
        The endpoint will auto-scale workers based on job queue.
        
        Args:
            request: Provision request with GPU specs and Docker image
            
        Returns:
            InstanceInfo representing the endpoint
        """
        # Note: RunPod endpoint creation requires GraphQL API
        # For now, we simulate by recording the endpoint config
        # In production, this would call RunPod's management API
        
        import uuid
        endpoint_id = f"ep-{uuid.uuid4().hex[:12]}"
        
        # Calculate price
        price_per_second = self.GPU_PRICING.get(request.gpu_type, 0.0005)
        price_per_hour = price_per_second * 3600 * request.gpu_count
        
        endpoint_info = RunPodEndpointInfo(
            endpoint_id=endpoint_id,
            endpoint_name=f"mica-{request.gpu_type.lower()}-{request.gpu_count}",
            template_id=request.template_id or "",
            docker_image=request.docker_image,
            gpu_type=request.gpu_type,
            gpu_count=request.gpu_count,
            min_workers=self.default_min_workers,
            max_workers=self.default_max_workers,
            idle_timeout=self.default_idle_timeout,
            price_per_second=price_per_second,
        )
        
        self._endpoints[endpoint_id] = endpoint_info
        
        logger.info(
            f"Created RunPod endpoint {endpoint_id} "
            f"({request.gpu_type} x{request.gpu_count})"
        )
        
        return InstanceInfo(
            instance_id=endpoint_id,
            provider="runpod",
            status=InstanceStatus.RUNNING,  # Endpoints are immediately available
            gpu_type=request.gpu_type,
            gpu_count=request.gpu_count,
            price_per_hour=price_per_hour,
            ssh_host=None,  # Serverless - no SSH
            ssh_port=None,
            created_at=datetime.utcnow().isoformat() + "Z",
            metadata={
                "endpoint_name": endpoint_info.endpoint_name,
                "docker_image": request.docker_image,
                "min_workers": endpoint_info.min_workers,
                "max_workers": endpoint_info.max_workers,
                "tier": self.tier,
            },
        )
    
    async def destroy_instance(self, instance_id: str) -> bool:
        """
        Delete a RunPod endpoint.
        
        Args:
            instance_id: Endpoint ID
            
        Returns:
            True if deleted
        """
        if instance_id in self._endpoints:
            del self._endpoints[instance_id]
            logger.info(f"Deleted RunPod endpoint {instance_id}")
            return True
        
        logger.warning(f"Endpoint {instance_id} not found")
        return False
    
    async def get_instance_status(self, instance_id: str) -> InstanceInfo:
        """
        Get endpoint status and health.
        
        Args:
            instance_id: Endpoint ID
            
        Returns:
            InstanceInfo with current status
        """
        if instance_id not in self._endpoints:
            return InstanceInfo(
                instance_id=instance_id,
                provider="runpod",
                status=InstanceStatus.NOT_FOUND,
                gpu_type="",
                gpu_count=0,
                price_per_hour=0.0,
            )
        
        endpoint = self._endpoints[instance_id]
        
        # Try to get real health from API
        health = None
        try:
            client = await self._get_client()
            # Note: Health requires setting endpoint_id on client
            client.endpoint_id = instance_id
            health = await client.get_endpoint_health()
        except Exception as e:
            logger.warning(f"Failed to get health for {instance_id}: {e}")
        
        price_per_hour = endpoint.price_per_second * 3600 * endpoint.gpu_count
        
        return InstanceInfo(
            instance_id=instance_id,
            provider="runpod",
            status=InstanceStatus.RUNNING,
            gpu_type=endpoint.gpu_type,
            gpu_count=endpoint.gpu_count,
            price_per_hour=price_per_hour,
            created_at=datetime.utcnow().isoformat() + "Z",
            metadata={
                "endpoint_name": endpoint.endpoint_name,
                "workers_running": health.workers_running if health else 0,
                "workers_idle": health.workers_idle if health else 0,
                "jobs_in_queue": health.jobs_in_queue if health else 0,
                "jobs_completed": health.jobs_completed if health else 0,
            },
        )
    
    async def list_instances(self) -> List[InstanceInfo]:
        """List all endpoints created by this provider."""
        instances = []
        for endpoint_id in self._endpoints:
            info = await self.get_instance_status(endpoint_id)
            instances.append(info)
        return instances
    
    # ============================================================================
    # RunPod-Specific Methods
    # ============================================================================
    
    async def submit_job(
        self,
        endpoint_id: str,
        input_data: Dict[str, Any],
        webhook: Optional[str] = None,
        execution_timeout_ms: int = 600000,  # 10 minutes
    ) -> RunPodJob:
        """
        Submit a job to a RunPod endpoint.
        
        Args:
            endpoint_id: Target endpoint
            input_data: Job input payload
            webhook: Optional webhook URL for completion
            execution_timeout_ms: Max execution time in milliseconds
            
        Returns:
            RunPodJob with job ID
        """
        client = await self._get_client()
        client.endpoint_id = endpoint_id
        
        job = await client.submit_job(
            input_data=input_data,
            webhook=webhook,
            execution_timeout=execution_timeout_ms,
        )
        
        logger.info(f"Submitted job {job.id} to endpoint {endpoint_id}")
        return job
    
    async def get_job_status(
        self,
        endpoint_id: str,
        job_id: str,
    ) -> RunPodJob:
        """Get job status and output."""
        client = await self._get_client()
        client.endpoint_id = endpoint_id
        return await client.get_job_status(job_id)
    
    async def poll_job_until_complete(
        self,
        endpoint_id: str,
        job_id: str,
        poll_interval: float = 5.0,
        max_wait: float = 1800.0,
    ) -> RunPodJob:
        """Poll job until completion."""
        client = await self._get_client()
        client.endpoint_id = endpoint_id
        return await client.poll_until_complete(
            job_id,
            poll_interval=poll_interval,
            max_wait=max_wait,
        )
    
    async def cancel_job(
        self,
        endpoint_id: str,
        job_id: str,
    ) -> RunPodJob:
        """Cancel a job."""
        client = await self._get_client()
        client.endpoint_id = endpoint_id
        return await client.cancel_job(job_id)
    
    async def get_endpoint_health(
        self,
        endpoint_id: str,
    ) -> Optional[EndpointHealth]:
        """Get endpoint health metrics."""
        try:
            client = await self._get_client()
            client.endpoint_id = endpoint_id
            return await client.get_endpoint_health()
        except Exception as e:
            logger.warning(f"Failed to get health for {endpoint_id}: {e}")
            return None
    
    async def purge_queue(self, endpoint_id: str) -> int:
        """
        Purge all pending jobs from endpoint queue.
        
        Returns:
            Number of jobs purged
        """
        client = await self._get_client()
        client.endpoint_id = endpoint_id
        return await client.purge_queue()


# ============================================================================
# Provider Factory
# ============================================================================

def create_runpod_provider(
    api_key: Optional[str] = None,
    tier: str = "community",
) -> RunPodProvider:
    """
    Factory function to create RunPod provider.
    
    Args:
        api_key: RunPod API key (default: $RUNPOD_API_KEY)
        tier: "community" or "secure"
        
    Returns:
        Configured RunPodProvider
    """
    return RunPodProvider(api_key=api_key, tier=tier)

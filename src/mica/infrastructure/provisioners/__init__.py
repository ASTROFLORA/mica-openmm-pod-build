"""
provisioners - Cloud GPU Instance Provisioners

Abstraction layer for creating GPU instances across multiple cloud providers.

Architecture:
    HTTP-based communication (not SSH) for scalability
    Docker pre-configured with FastAPI worker server
    Auto-checkpoint to GCS
    Multi-provider support (Vast.ai, RunPod, Lambda)

Usage:
    provisioner = RunPodProvisioner()
    instance = await provisioner.create_instance(ProvisionRequest(...))
    
    # Start job via HTTP (not SSH)
    response = await http_client.post(
        f"{instance.endpoint_url}/jobs/start",
        json=job_spec
    )

Author: Team 2
Date: 2025-01-21
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum
import asyncio


class ProviderType(Enum):
    """Cloud provider types."""
    VAST_AI = "vast"
    RUNPOD = "runpod"
    LAMBDA = "lambda"
    PAPERSPACE = "paperspace"


class InstanceStatus(Enum):
    """Instance lifecycle status."""
    CREATING = "creating"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    TERMINATED = "terminated"
    FAILED = "failed"


@dataclass
class ProvisionRequest:
    """Request to create a GPU instance."""
    # Resource requirements
    gpu_type: str = "A4000"  # "A4000" | "5080" | "L40S" | "A100"
    gpu_count: int = 1
    min_ram_gb: float = 32.0
    min_disk_gb: float = 100.0
    
    # Cost constraints
    max_price_per_hour: Optional[float] = None
    prefer_spot: bool = True  # Use interruptible instances
    
    # Docker configuration
    docker_image: str = "mica/gpu-worker:latest"
    docker_command: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    
    # Job metadata
    job_id: str = ""
    user_id: str = ""
    
    # Provider preference
    provider_preference: Optional[ProviderType] = None


@dataclass
class Instance:
    """Represents a running GPU instance."""
    instance_id: str
    provider: ProviderType
    status: InstanceStatus
    
    # Connection info (HTTP-based, not SSH primary)
    endpoint_url: str  # http://instance:8000
    ssh_host: Optional[str] = None  # Only for debugging
    ssh_port: int = 22
    ssh_key_path: Optional[str] = None
    
    # Resource info
    gpu_type: str = ""
    gpu_count: int = 1
    ram_gb: float = 0.0
    disk_gb: float = 0.0
    
    # Cost tracking
    price_per_hour: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    # Metadata
    job_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProvisionerABC(ABC):
    """
    Abstract base class for cloud provisioners.
    
    Each provider (Vast, RunPod, Lambda) implements this interface.
    """
    
    @abstractmethod
    async def create_instance(self, request: ProvisionRequest) -> Instance:
        """
        Create a new GPU instance.
        
        Returns Instance with endpoint_url for HTTP communication.
        """
        pass
    
    @abstractmethod
    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate/destroy an instance."""
        pass
    
    @abstractmethod
    async def get_instance_status(self, instance_id: str) -> InstanceStatus:
        """Get current instance status."""
        pass
    
    @abstractmethod
    async def list_instances(self) -> List[Instance]:
        """List all instances."""
        pass
    
    @abstractmethod
    async def search_offers(
        self,
        gpu_type: str,
        gpu_count: int = 1,
        max_price: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for available GPU offers that match criteria.
        
        Returns list of offers sorted by price (cheapest first).
        """
        pass
    
    async def wait_for_ready(
        self,
        instance_id: str,
        timeout_seconds: int = 300
    ) -> bool:
        """
        Wait for instance to be ready (HTTP endpoint accessible).
        
        Polls instance status and endpoint health check.
        """
        import aiohttp
        
        start_time = datetime.utcnow()
        
        while (datetime.utcnow() - start_time).total_seconds() < timeout_seconds:
            # Check instance status
            status = await self.get_instance_status(instance_id)
            
            if status == InstanceStatus.RUNNING:
                # Try health check on HTTP endpoint
                instance = await self._get_instance(instance_id)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{instance.endpoint_url}/health",
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            if resp.status == 200:
                                return True
                except:
                    pass
            
            elif status in [InstanceStatus.FAILED, InstanceStatus.TERMINATED]:
                return False
            
            await asyncio.sleep(5)
        
        return False
    
    @abstractmethod
    async def _get_instance(self, instance_id: str) -> Instance:
        """Get full instance details (internal helper)."""
        pass


__all__ = [
    "ProvisionerABC",
    "ProvisionRequest",
    "Instance",
    "InstanceStatus",
    "ProviderType",
]

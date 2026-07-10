"""
base_provider.py - Abstract Cloud Provider Interface

This module defines the abstract base class for all cloud GPU providers.
Each provider (Vast.ai, RunPod, GCP, Lambda Labs) implements this interface.

Design Principles:
- Provider-agnostic: Same API regardless of backend
- Async-first: All I/O operations are async
- Cost-aware: Every operation tracks $/hr and efficiency metrics
- Fault-tolerant: Built-in retry and graceful degradation

Author: MICA Team
Date: December 2024
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import asyncio


class InstanceStatus(Enum):
    """Unified instance status across all providers."""
    PENDING = "pending"          # Request submitted, waiting for allocation
    PROVISIONING = "provisioning"  # Hardware allocated, OS/Docker starting
    RUNNING = "running"          # Ready to accept jobs
    STOPPING = "stopping"        # Graceful shutdown in progress
    STOPPED = "stopped"          # Instance stopped but not destroyed
    TERMINATED = "terminated"    # Instance destroyed, resources released
    ERROR = "error"              # Failed state, check error_message
    PREEMPTED = "preempted"      # Spot instance reclaimed by provider


class GPUType(Enum):
    """Common GPU types across providers."""
    # Consumer/Prosumer
    RTX_3080 = "RTX_3080"
    RTX_3090 = "RTX_3090"
    RTX_4080 = "RTX_4080"
    RTX_4090 = "RTX_4090"
    RTX_5070Ti = "RTX_5070Ti"
    RTX_5080 = "RTX_5080"
    RTX_5090 = "RTX_5090"
    
    # Data Center - Ampere
    A100_40GB = "A100_40GB"
    A100_80GB = "A100_80GB"
    A40 = "A40"
    A10 = "A10"
    
    # Data Center - Ada Lovelace
    L40S = "L40S"
    L40 = "L40"
    
    # Data Center - Hopper
    H100_80GB = "H100_80GB"
    H100_SXM = "H100_SXM"
    
    # Legacy
    V100_16GB = "V100_16GB"
    V100_32GB = "V100_32GB"
    T4 = "T4"


@dataclass
class GPUOffer:
    """
    Represents an available GPU instance offer from a provider.
    
    This is returned by search_offers() and used to make provisioning decisions.
    """
    provider: str                   # "vast", "runpod", "gcp", "lambda"
    offer_id: str                   # Provider-specific offer ID
    gpu_type: GPUType
    gpu_count: int
    gpu_memory_gb: float            # Per GPU
    cpu_cores: int
    ram_gb: float
    disk_gb: float
    disk_type: str                  # "nvme", "ssd", "hdd"
    
    # Pricing
    price_per_hour: float           # USD/hr
    is_spot: bool                   # Spot/preemptible instance
    spot_interruption_rate: Optional[float] = None  # Historical interruption %
    
    # Network
    upload_mbps: Optional[float] = None
    download_mbps: Optional[float] = None
    
    # Location
    region: Optional[str] = None
    datacenter: Optional[str] = None
    
    # Provider-specific metadata
    raw_data: Dict[str, Any] = field(default_factory=dict)
    
    # Computed metrics (set by scorer)
    dcem_score: Optional[float] = None  # $/ns for MD workloads
    
    def __post_init__(self):
        """Validate offer data."""
        if self.price_per_hour <= 0:
            raise ValueError(f"Invalid price: {self.price_per_hour}")
        if self.gpu_count <= 0:
            raise ValueError(f"Invalid GPU count: {self.gpu_count}")


@dataclass
class ProvisionRequest:
    """
    Request to provision a cloud GPU instance.
    
    This is passed to provision() to create a new instance.
    """
    # Hardware requirements
    gpu_type: GPUType
    gpu_count: int = 1
    min_gpu_memory_gb: float = 16.0
    min_ram_gb: float = 32.0
    min_disk_gb: float = 100.0
    
    # Docker configuration
    docker_image: str = "nvcr.io/nvidia/pytorch:24.01-py3"
    docker_command: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    
    # Cost constraints
    max_price_per_hour: Optional[float] = None
    prefer_spot: bool = True
    max_spot_interruption_rate: float = 0.30  # Max 30% historical interruptions
    
    # Time constraints
    expected_duration_hours: Optional[float] = None
    
    # Network requirements
    min_upload_mbps: Optional[float] = None
    min_download_mbps: Optional[float] = None
    
    # Region preferences
    preferred_regions: List[str] = field(default_factory=list)
    excluded_regions: List[str] = field(default_factory=list)
    
    # Labels for tracking
    job_id: Optional[str] = None
    worker_type: Optional[str] = None  # "dynamo", "chronos", "spectra"
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class Instance:
    """
    Represents a provisioned cloud GPU instance.
    
    This is returned by provision() and used to track/destroy instances.
    """
    provider: str
    instance_id: str
    status: InstanceStatus
    
    # Connection info
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_key_path: Optional[str] = None
    
    # Jupyter/API access (if applicable)
    jupyter_url: Optional[str] = None
    api_endpoint: Optional[str] = None
    
    # Hardware info (from offer)
    gpu_type: Optional[GPUType] = None
    gpu_count: int = 1
    
    # Billing
    price_per_hour: float = 0.0
    started_at: Optional[datetime] = None
    total_cost_usd: float = 0.0
    
    # Job tracking
    job_id: Optional[str] = None
    worker_type: Optional[str] = None
    
    # Error info
    error_message: Optional[str] = None
    
    # Provider-specific
    raw_data: Dict[str, Any] = field(default_factory=dict)
    
    def compute_current_cost(self) -> float:
        """Calculate cost incurred so far."""
        if self.started_at is None:
            return 0.0
        elapsed = datetime.utcnow() - self.started_at
        hours = elapsed.total_seconds() / 3600
        return hours * self.price_per_hour


@dataclass
class ProvisionResult:
    """
    Result of a provision() call.
    """
    success: bool
    instance: Optional[Instance] = None
    error_message: Optional[str] = None
    offer_used: Optional[GPUOffer] = None
    
    # Timing
    provision_time_seconds: float = 0.0


class CloudProvider(ABC):
    """
    Abstract base class for cloud GPU providers.
    
    All providers (Vast.ai, RunPod, GCP, Lambda) must implement this interface.
    
    Example:
        provider = VastProvider(api_key="...")
        offers = await provider.search_offers(GPUType.L40S, max_price=0.50)
        result = await provider.create_instance(offers[0], docker_image="...")
        ...
        await provider.destroy_instance(result.instance.instance_id)
    """
    
    # Provider identifier
    PROVIDER_NAME: str = "base"
    
    def __init__(self, api_key: Optional[str] = None, **kwargs):
        """
        Initialize provider with credentials.
        
        Args:
            api_key: API key for the provider (some use env vars)
            **kwargs: Provider-specific configuration
        """
        self.api_key = api_key
        self.config = kwargs
        self._instances: Dict[str, Instance] = {}
    
    @abstractmethod
    async def search_offers(
        self,
        gpu_type: GPUType,
        max_price: Optional[float] = None,
        min_gpu_count: int = 1,
        prefer_spot: bool = True,
        region: Optional[str] = None,
    ) -> List[GPUOffer]:
        """
        Search available GPU offers from this provider.
        
        Args:
            gpu_type: Desired GPU type
            max_price: Maximum $/hr (None = no limit)
            min_gpu_count: Minimum number of GPUs
            prefer_spot: Prefer spot/preemptible instances
            region: Filter by region
            
        Returns:
            List of GPUOffer sorted by price (cheapest first)
        """
        pass
    
    @abstractmethod
    async def create_instance(
        self,
        offer: GPUOffer,
        docker_image: str,
        docker_command: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        job_id: Optional[str] = None,
    ) -> ProvisionResult:
        """
        Create a new instance from an offer.
        
        Args:
            offer: GPUOffer from search_offers()
            docker_image: Docker image to run
            docker_command: Command to run (None = image default)
            env_vars: Environment variables
            job_id: Optional job ID for tracking
            
        Returns:
            ProvisionResult with instance details or error
        """
        pass
    
    @abstractmethod
    async def destroy_instance(self, instance_id: str) -> bool:
        """
        Terminate and destroy an instance.
        
        Args:
            instance_id: Instance ID from create_instance()
            
        Returns:
            True if destroyed successfully, False otherwise
        """
        pass
    
    @abstractmethod
    async def get_instance_status(self, instance_id: str) -> Instance:
        """
        Get current status of an instance.
        
        Args:
            instance_id: Instance ID to check
            
        Returns:
            Instance with current status and metadata
        """
        pass
    
    async def wait_for_ready(
        self,
        instance_id: str,
        timeout_seconds: float = 300,
        poll_interval: float = 5.0,
    ) -> Instance:
        """
        Wait for instance to reach RUNNING status.
        
        Args:
            instance_id: Instance to wait for
            timeout_seconds: Maximum wait time
            poll_interval: Seconds between status checks
            
        Returns:
            Instance in RUNNING status
            
        Raises:
            TimeoutError: If instance doesn't start in time
            RuntimeError: If instance enters ERROR state
        """
        start_time = asyncio.get_event_loop().time()
        
        while True:
            instance = await self.get_instance_status(instance_id)
            
            if instance.status == InstanceStatus.RUNNING:
                return instance
            
            if instance.status in (InstanceStatus.ERROR, InstanceStatus.TERMINATED):
                raise RuntimeError(
                    f"Instance {instance_id} failed: {instance.error_message}"
                )
            
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_seconds:
                raise TimeoutError(
                    f"Instance {instance_id} did not start within {timeout_seconds}s"
                )
            
            await asyncio.sleep(poll_interval)
    
    async def execute_command(
        self,
        instance_id: str,
        command: str,
        timeout_seconds: float = 60,
    ) -> tuple[int, str, str]:
        """
        Execute a command on the instance via SSH.
        
        Default implementation uses asyncssh. Providers can override.
        
        Args:
            instance_id: Target instance
            command: Shell command to run
            timeout_seconds: Command timeout
            
        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        # Default implementation - providers can override
        instance = await self.get_instance_status(instance_id)
        
        if instance.status != InstanceStatus.RUNNING:
            raise RuntimeError(f"Instance not running: {instance.status}")
        
        try:
            import asyncssh
            
            async with asyncssh.connect(
                instance.ssh_host,
                port=instance.ssh_port,
                username=instance.ssh_user,
                client_keys=[instance.ssh_key_path] if instance.ssh_key_path else None,
                known_hosts=None,  # For ephemeral instances
            ) as conn:
                result = await asyncio.wait_for(
                    conn.run(command),
                    timeout=timeout_seconds
                )
                return result.exit_status, result.stdout, result.stderr
                
        except ImportError:
            raise RuntimeError("asyncssh not installed. Run: pip install asyncssh")
    
    async def upload_file(
        self,
        instance_id: str,
        local_path: str,
        remote_path: str,
    ) -> bool:
        """
        Upload a file to the instance via SCP.
        
        Args:
            instance_id: Target instance
            local_path: Local file path
            remote_path: Destination path on instance
            
        Returns:
            True if successful
        """
        instance = await self.get_instance_status(instance_id)
        
        try:
            import asyncssh
            
            async with asyncssh.connect(
                instance.ssh_host,
                port=instance.ssh_port,
                username=instance.ssh_user,
                client_keys=[instance.ssh_key_path] if instance.ssh_key_path else None,
                known_hosts=None,
            ) as conn:
                await asyncssh.scp(local_path, (conn, remote_path))
                return True
                
        except Exception as e:
            print(f"Upload failed: {e}")
            return False
    
    async def download_file(
        self,
        instance_id: str,
        remote_path: str,
        local_path: str,
    ) -> bool:
        """
        Download a file from the instance via SCP.
        
        Args:
            instance_id: Source instance
            remote_path: File path on instance
            local_path: Local destination path
            
        Returns:
            True if successful
        """
        instance = await self.get_instance_status(instance_id)
        
        try:
            import asyncssh
            
            async with asyncssh.connect(
                instance.ssh_host,
                port=instance.ssh_port,
                username=instance.ssh_user,
                client_keys=[instance.ssh_key_path] if instance.ssh_key_path else None,
                known_hosts=None,
            ) as conn:
                await asyncssh.scp((conn, remote_path), local_path)
                return True
                
        except Exception as e:
            print(f"Download failed: {e}")
            return False
    
    def list_instances(self) -> List[Instance]:
        """
        List all instances created by this provider instance.
        
        Note: This only returns locally tracked instances, not all
        instances in the cloud account.
        """
        return list(self._instances.values())
    
    async def health_check(self) -> bool:
        """
        Check if provider API is reachable and credentials are valid.
        
        Returns:
            True if healthy
        """
        try:
            # Try a minimal search to verify API access
            await self.search_offers(GPUType.T4, max_price=10.0)
            return True
        except Exception:
            return False
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} provider={self.PROVIDER_NAME}>"

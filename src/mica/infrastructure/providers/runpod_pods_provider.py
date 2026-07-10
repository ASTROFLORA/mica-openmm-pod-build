"""
runpod_pods_provider.py - RunPod Pods (Dedicated Instances) Provider

Implements CloudProvider for RunPod on-demand/spot GPU pods (not serverless).
Pods are long-running dedicated instances ideal for MD simulations with
large outputs (20+ GB trajectories).

Key Differences from Serverless:
- Persistent disk storage (network volumes)
- Direct SSH/Jupyter access
- Billing by uptime ($/hr), not per-second GPU usage
- Support for GCS FUSE mounting via startup script
- Better for long-running MD workloads (hours/days)

RunPod Pods API Endpoints:
- GET /pods - List pods
- POST /pods - Create pod
- DELETE /pods/{id} - Terminate pod
- GET /pods/{id} - Get pod status

Author: MICA Infrastructure Team
Date: December 2024
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

from ..compute_image_contract import ghcr_basic_auth, is_ghcr_image
from .base_provider import (
    CloudProvider,
    GPUOffer,
    GPUType,
    Instance,
    InstanceStatus,
    ProvisionRequest,
    ProvisionResult,
)

logger = logging.getLogger(__name__)


# RunPod GPU pricing for on-demand pods (approximate USD/hr)
RUNPOD_POD_PRICING = {
    "NVIDIA RTX 3090": 0.44,
    "NVIDIA RTX 4090": 0.69,
    "NVIDIA A100 40GB": 1.50,
    "NVIDIA A100 80GB": 1.79,
    "NVIDIA A100-SXM4-80GB": 1.99,
    "NVIDIA H100 80GB": 3.59,
    "NVIDIA H100 SXM5": 4.14,
    "NVIDIA L40S": 1.55,
    "NVIDIA A40": 0.79,
    "NVIDIA A6000": 0.79,
}

# GPU VRAM mapping
GPU_MEMORY = {
    "NVIDIA RTX 3090": 24,
    "NVIDIA RTX 4090": 24,
    "NVIDIA A100 40GB": 40,
    "NVIDIA A100 80GB": 80,
    "NVIDIA A100-SXM4-80GB": 80,
    "NVIDIA H100 80GB": 80,
    "NVIDIA H100 SXM5": 80,
    "NVIDIA L40S": 48,
    "NVIDIA A40": 48,
    "NVIDIA A6000": 48,
}


@dataclass
class RunPodPodOffer(GPUOffer):
    """RunPod pod offer (extends base GPUOffer)."""
    pod_type: str = "on-demand"  # "on-demand" or "spot"
    network_volume_id: Optional[str] = None
    datacenter_id: Optional[str] = None


class RunPodPodsProvider(CloudProvider):
    """
    RunPod Pods (dedicated instances) provider.
    
    Uses RunPod GraphQL/REST API to create on-demand or spot GPU pods.
    Pods are long-running instances with persistent storage, ideal for MD.
    
    Example:
        provider = RunPodPodsProvider(api_key="rp_xxxxx")
        
        # Search available GPU types
        offers = await provider.search_offers(
            gpu_type=GPUType.L40S,
            max_price=2.0,
        )
        
        # Create pod with GCS mount script
        request = ProvisionRequest(
            gpu_type=GPUType.L40S,
            docker_image="nvcr.io/nvidia/pytorch:24.01-py3",
            docker_command="bash /workspace/mount_gcs.sh && python run_md.py",
            env_vars={"GCS_BUCKET": "gs://mica-md-user123"},
        )
        result = await provider.provision(request)
        
        # SSH access
        print(f"ssh -p {result.instance.ssh_port} {result.instance.ssh_host}")
    """
    
    PROVIDER_NAME = "runpod_pods"
    
    # RunPod GraphQL endpoint
    GRAPHQL_URL = "https://api.runpod.io/graphql"
    REST_API_URL = "https://rest.runpod.io/v1"
    
    # Community cloud datacenter IDs (examples, may vary)
    COMMUNITY_DATACENTERS = {
        "us-west": "US-CA-1",
        "us-east": "US-NY-1",
        "eu-central": "EU-RO-1",
    }
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        prefer_spot: bool = True,
        default_disk_gb: int = 100,
        **kwargs
    ):
        """
        Initialize RunPod Pods provider.
        
        Args:
            api_key: RunPod API key (default: $RUNPOD_API_KEY)
            prefer_spot: Prefer spot instances when available
            default_disk_gb: Default pod disk size
        """
        super().__init__(api_key, **kwargs)
        self.api_key = api_key or os.getenv("RUNPOD_API_KEY")
        if not self.api_key:
            raise ValueError("RunPod API key required (set RUNPOD_API_KEY or pass api_key)")
        
        self.prefer_spot = prefer_spot
        self.default_disk_gb = default_disk_gb
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._pods_cache: Dict[str, Instance] = {}
        self._container_registry_auth_id: Optional[str] = None
    
    async def _ensure_session(self):
        """Ensure aiohttp session exists."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self.api_key,
                },
                timeout=aiohttp.ClientTimeout(total=120),
            )
    
    async def close(self):
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def _graphql(self, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute GraphQL query against RunPod API."""
        await self._ensure_session()
        
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        
        async with self._session.post(self.GRAPHQL_URL, json=payload) as resp:
            data = await resp.json()
            
            # Check for GraphQL errors in response body (even if HTTP status is 200)
            if "errors" in data:
                error_msg = data["errors"][0].get("message", "Unknown GraphQL error")
                logger.error(f"GraphQL error: {error_msg}")
                logger.error(f"Full error response: {data}")
                raise RuntimeError(f"GraphQL error: {error_msg}")
            
            # Also check HTTP status
            if resp.status >= 400:
                logger.error(f"HTTP {resp.status}: {await resp.text()}")
            resp.raise_for_status()
            
            if "errors" in data:
                raise RuntimeError(f"GraphQL error: {data['errors']}")
            
            return data.get("data", {})

    def _rest_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    async def _rest_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        await self._ensure_session()
        assert self._session is not None
        async with self._session.request(
            method,
            f"{self.REST_API_URL}{path}",
            json=payload,
            headers=self._rest_headers(),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"RunPod REST {resp.status}: {text}")
            if not text:
                return None
            return await resp.json(content_type=None)

    async def _ensure_container_registry_auth(self, docker_image: str) -> Optional[str]:
        if not is_ghcr_image(docker_image):
            return None
        auth = ghcr_basic_auth()
        if auth is None:
            return None
        if self._container_registry_auth_id:
            return self._container_registry_auth_id

        auth_name = f"mica-ghcr-{auth['username']}".lower()
        existing = await self._rest_json("GET", "/containerregistryauth")
        if isinstance(existing, list):
            for item in existing:
                if item.get("name") == auth_name and item.get("id"):
                    self._container_registry_auth_id = str(item["id"])
                    return self._container_registry_auth_id

        created = await self._rest_json(
            "POST",
            "/containerregistryauth",
            {
                "name": auth_name,
                "username": auth["username"],
                "password": auth["password"],
            },
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError("RunPod registry auth creation did not return an id")
        self._container_registry_auth_id = str(created["id"])
        return self._container_registry_auth_id
    
    async def search_offers(
        self,
        gpu_type: GPUType,
        max_price: Optional[float] = None,
        min_gpu_count: int = 1,
        prefer_spot: bool = True,
        region: Optional[str] = None,
    ) -> List[GPUOffer]:
        """
        Search available GPU pod types.
        
        Note: RunPod doesn't have a real-time marketplace API like Vast.
        We return static pricing for known GPU types.
        
        Args:
            gpu_type: Desired GPU type
            max_price: Maximum $/hr
            min_gpu_count: Minimum GPUs (pods support 1-8 typically)
            prefer_spot: Prefer spot pricing (20-50% cheaper)
            region: Preferred region
            
        Returns:
            List of available pod configurations
        """
        offers = []
        
        # Map GPUType to RunPod GPU names
        gpu_name_map = {
            GPUType.RTX_3090: "NVIDIA RTX 3090",
            GPUType.RTX_4090: "NVIDIA RTX 4090",
            GPUType.A100_40GB: "NVIDIA A100 40GB",
            GPUType.A100_80GB: "NVIDIA A100 80GB",
            GPUType.L40S: "NVIDIA L40S",
            GPUType.A40: "NVIDIA A40",
            GPUType.H100_80GB: "NVIDIA H100 80GB",
        }
        
        gpu_name = gpu_name_map.get(gpu_type)
        if not gpu_name:
            logger.warning(f"GPU type {gpu_type} not supported on RunPod")
            return []
        
        # Get base price
        base_price = RUNPOD_POD_PRICING.get(gpu_name, 1.0)
        vram = GPU_MEMORY.get(gpu_name, 24)
        
        # Spot discount (approximate)
        spot_multiplier = 0.6 if prefer_spot else 1.0
        
        for gpu_count in range(min_gpu_count, min(min_gpu_count + 3, 9)):
            price_per_hour = base_price * gpu_count * spot_multiplier
            
            if max_price and price_per_hour > max_price:
                continue
            
            offer = RunPodPodOffer(
                provider=self.PROVIDER_NAME,
                offer_id=f"runpod-pod-{gpu_name.lower().replace(' ', '-')}-x{gpu_count}",
                gpu_type=gpu_type,
                gpu_count=gpu_count,
                gpu_memory_gb=vram,
                cpu_cores=8 * gpu_count,
                ram_gb=32 * gpu_count,
                disk_gb=self.default_disk_gb,
                disk_type="nvme",
                price_per_hour=price_per_hour,
                is_spot=prefer_spot,
                region=region or "us-west",
                datacenter="community" if prefer_spot else "secure",
                pod_type="spot" if prefer_spot else "on-demand",
            )
            offers.append(offer)
        
        offers.sort(key=lambda x: x.price_per_hour)
        return offers
    
    async def create_instance(
        self,
        offer: GPUOffer,
        docker_image: str,
        docker_command: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        job_id: Optional[str] = None,
    ) -> ProvisionResult:
        """
        Create RunPod pod from offer (implements abstract method).
        
        Args:
            offer: GPUOffer from search_offers()
            docker_image: Docker image to run
            docker_command: Command to run
            env_vars: Environment variables
            job_id: Optional job ID
            
        Returns:
            ProvisionResult with instance
        """
        # Convert offer to ProvisionRequest and delegate to provision()
        request = ProvisionRequest(
            gpu_type=offer.gpu_type,
            gpu_count=offer.gpu_count,
            min_ram_gb=offer.ram_gb,
            min_disk_gb=offer.disk_gb,
            docker_image=docker_image,
            docker_command=docker_command,
            env_vars=env_vars or {},
            prefer_spot=getattr(offer, "pod_type", "on-demand") == "spot",
            max_price_per_hour=offer.price_per_hour,
        )
        
        return await self.provision(request)
    
    async def provision(
        self,
        request: ProvisionRequest,
        strategy: Optional[str] = None,
        preferred_provider: Optional[str] = None,
    ) -> ProvisionResult:
        """
        Provision a RunPod pod.
        
        Args:
            request: Provision request with GPU specs and Docker config
            strategy: Ignored (for interface compatibility)
            preferred_provider: Ignored
            
        Returns:
            ProvisionResult with pod instance
        """
        start_time = datetime.utcnow()
        
        try:
            # Map GPUType to RunPod GPU ID (use query to get live GPU IDs)
            gpu_type_id = await self._get_gpu_type_id(request.gpu_type)
            container_registry_auth_id = await self._ensure_container_registry_auth(
                request.docker_image
            )
            registry_auth_line = (
                f'\n                    containerRegistryAuthId: "{container_registry_auth_id}"'
                if container_registry_auth_id
                else ""
            )
            
            # Build pod creation mutation based on spot vs on-demand
            # Use podRentInterruptable for spot (COMMUNITY cloud) or podFindAndDeployOnDemand for on-demand
            if request.prefer_spot:
                mutation = """
                mutation {
                  podRentInterruptable(input: {
                    bidPerGpu: 0.5
                    cloudType: COMMUNITY
                    gpuCount: %d
                    volumeInGb: %d
                    containerDiskInGb: %d
                    minVcpuCount: %d
                    minMemoryInGb: %d
                    gpuTypeId: "%s"
                    name: "%s"
                    imageName: "%s"%s
                    dockerArgs: "%s"
                    ports: "22/tcp,8888/http"
                    volumeMountPath: "/workspace"
                    env: %s
                  }) {
                    id
                    imageName
                    env
                    machineId
                    machine {
                      podHostId
                    }
                  }
                }
                """ % (
                    request.gpu_count,
                    int(request.min_disk_gb),
                    self.default_disk_gb,
                    4,
                    int(request.min_ram_gb),
                    gpu_type_id,
                    f"mica-compute-{uuid.uuid4().hex[:8]}",
                    request.docker_image,
                    registry_auth_line,
                    request.docker_command or "",
                    str([{"key": k, "value": v} for k, v in request.env_vars.items()]).replace("'", "\\\"")
                )
                result = await self._graphql(mutation)
                pod_data = result.get("podRentInterruptable", {})
            else:
                mutation = """
                mutation {
                  podFindAndDeployOnDemand(input: {
                    cloudType: ALL
                    gpuCount: %d
                    volumeInGb: %d
                    containerDiskInGb: %d
                    minVcpuCount: %d
                    minMemoryInGb: %d
                    gpuTypeId: "%s"
                    name: "%s"
                    imageName: "%s"%s
                    dockerArgs: "%s"
                    ports: "22/tcp,8888/http"
                    volumeMountPath: "/workspace"
                    env: %s
                  }) {
                    id
                    imageName
                    env
                    machineId
                    machine {
                      podHostId
                    }
                  }
                }
                """ % (
                    request.gpu_count,
                    int(request.min_disk_gb),
                    self.default_disk_gb,
                    4,
                    int(request.min_ram_gb),
                    gpu_type_id,
                    f"mica-compute-{uuid.uuid4().hex[:8]}",
                    request.docker_image,
                    registry_auth_line,
                    request.docker_command or "",
                    str([{"key": k, "value": v} for k, v in request.env_vars.items()]).replace("'", "\\\"")
                )
                result = await self._graphql(mutation)
                pod_data = result.get("podFindAndDeployOnDemand", {})
            
            if not pod_data or "id" not in pod_data:
                raise RuntimeError(f"Pod creation failed: {result}")
            
            pod_id = pod_data["id"]
            
            # Wait for pod to start and get connection info
            instance = await self._wait_for_pod_ready(pod_id, timeout=300)
            
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            
            return ProvisionResult(
                success=True,
                instance=instance,
                provision_time_seconds=elapsed,
            )
            
        except Exception as e:
            logger.error(f"Pod provision failed: {e}", exc_info=True)
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            return ProvisionResult(
                success=False,
                error_message=str(e),
                provision_time_seconds=elapsed,
            )
    
    async def _get_gpu_type_id(self, gpu_type: GPUType) -> str:
        """Get RunPod GPU type ID for a GPUType enum."""
        # Query available GPUs
        query = """
        {
            gpuTypes {
                id
                displayName
                memoryInGb
            }
        }
        """
        
        result = await self._graphql(query)
        gpu_types = result.get("gpuTypes", [])
        
        # Map GPUType to display name
        target_name_map = {
            GPUType.RTX_3090: "RTX 3090",
            GPUType.RTX_4090: "RTX 4090",
            GPUType.A100_40GB: "A100",
            GPUType.A100_80GB: "A100",
            GPUType.L40S: "L40S",
            GPUType.A40: "A40",
            GPUType.H100_80GB: "H100",
        }
        
        target = target_name_map.get(gpu_type, "")
        
        for gt in gpu_types:
            if target.lower() in gt.get("displayName", "").lower():
                return gt["id"]
        
        raise ValueError(f"GPU type {gpu_type} not found on RunPod")
    
    async def _wait_for_pod_ready(
        self,
        pod_id: str,
        timeout: float = 300,
    ) -> Instance:
        """Wait for pod to reach RUNNING status and return instance info."""
        start = datetime.utcnow()
        
        query = """
        query GetPod($podId: String!) {
            pod(input: {podId: $podId}) {
                id
                name
                desiredStatus
                runtime {
                    ports {
                        privatePort
                        publicPort
                        type
                    }
                    uptimeInSeconds
                }
                machine {
                    gpuDisplayName
                    gpuCount
                    podHostId
                }
                costPerHr
            }
        }
        """
        
        while (datetime.utcnow() - start).total_seconds() < timeout:
            result = await self._graphql(query, {"podId": pod_id})
            pod = result.get("pod", {})
            
            if not pod:
                raise RuntimeError(f"Pod {pod_id} not found")
            
            status_str = pod.get("desiredStatus", "").upper()
            
            if status_str == "RUNNING" and pod.get("runtime"):
                # Extract connection info
                runtime = pod["runtime"]
                ports = runtime.get("ports", [])
                
                ssh_port = None
                for port in ports:
                    if port.get("privatePort") == 22:
                        ssh_port = port.get("publicPort")
                        break
                
                machine = pod.get("machine", {})
                host_id = machine.get("podHostId", "unknown")
                
                instance = Instance(
                    provider=self.PROVIDER_NAME,
                    instance_id=pod_id,
                    status=InstanceStatus.RUNNING,
                    ssh_host=f"{host_id}.runpod.io",
                    ssh_port=ssh_port or 22,
                    ssh_user="root",
                    gpu_type=GPUType.L40S,  # TODO: parse from displayName
                    gpu_count=machine.get("gpuCount", 1),
                    price_per_hour=float(pod.get("costPerHr", 0)),
                    started_at=datetime.utcnow(),
                    job_id=None,
                    raw_data=pod,
                )
                
                self._pods_cache[pod_id] = instance
                return instance
            
            await asyncio.sleep(5)
        
        raise TimeoutError(f"Pod {pod_id} did not start within {timeout}s")
    
    async def destroy_instance(self, instance_id: str) -> bool:
        """Terminate a pod."""
        mutation = """
        mutation TerminatePod($podId: String!) {
            podTerminate(input: {podId: $podId})
        }
        """
        
        try:
            await self._graphql(mutation, {"podId": instance_id})
            if instance_id in self._pods_cache:
                del self._pods_cache[instance_id]
            return True
        except Exception as e:
            logger.error(f"Failed to terminate pod {instance_id}: {e}")
            return False
    
    async def get_instance_status(self, instance_id: str) -> Instance:
        """Get current pod status."""
        query = """
        query GetPod($podId: String!) {
            pod(input: {podId: $podId}) {
                id
                name
                desiredStatus
                runtime {
                    uptimeInSeconds
                }
                machine {
                    gpuCount
                }
                costPerHr
            }
        }
        """
        
        result = await self._graphql(query, {"podId": instance_id})
        pod = result.get("pod", {})
        
        if not pod:
            return Instance(
                provider=self.PROVIDER_NAME,
                instance_id=instance_id,
                status=InstanceStatus.TERMINATED,
                gpu_type=GPUType.L40S,
                gpu_count=0,
                price_per_hour=0.0,
            )
        
        status_map = {
            "RUNNING": InstanceStatus.RUNNING,
            "EXITED": InstanceStatus.STOPPED,
            "FAILED": InstanceStatus.ERROR,
        }
        
        status = status_map.get(pod.get("desiredStatus", ""), InstanceStatus.PENDING)
        
        if instance_id in self._pods_cache:
            instance = self._pods_cache[instance_id]
            instance.status = status
            return instance
        
        # Create minimal instance
        return Instance(
            provider=self.PROVIDER_NAME,
            instance_id=instance_id,
            status=status,
            gpu_type=GPUType.L40S,
            gpu_count=pod.get("machine", {}).get("gpuCount", 1),
            price_per_hour=float(pod.get("costPerHr", 0)),
        )
    
    async def health_check(self) -> bool:
        """Check if RunPod API is accessible."""
        try:
            query = "{ myself { id } }"
            await self._graphql(query)
            return True
        except Exception:
            return False

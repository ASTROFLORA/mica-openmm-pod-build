"""
vast_provider.py - Vast.ai Cloud Provider Implementation

Implements the CloudProvider interface for Vast.ai marketplace.
Vast.ai offers the best $/GPU ratio for spot instances, typically 60-80% cheaper
than RunPod or GCP for equivalent hardware.

Key Features:
- CLI-based API (vastai CLI must be installed)
- Spot instances with ~$0.20-0.40/hr for RTX 4090
- Direct SSH access to instances
- Docker support with custom images

Installation:
    pip install vastai
    vastai set api-key YOUR_API_KEY

Pricing Reference (Dec 2024):
    - RTX 4090: $0.25-0.40/hr (spot)
    - L40S: $0.50-0.80/hr (spot)  
    - A100 40GB: $0.80-1.20/hr (spot)
    - H100 80GB: $2.00-3.00/hr (spot)

Author: MICA Team
Date: December 2024
"""

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
import shutil

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


# GPU name mapping: Vast.ai names -> GPUType enum
VAST_GPU_MAPPING = {
    "RTX 3090": GPUType.RTX_3090,
    "GeForce RTX 3090": GPUType.RTX_3090,
    "RTX 4090": GPUType.RTX_4090,
    "GeForce RTX 4090": GPUType.RTX_4090,
    "A100-SXM4-40GB": GPUType.A100_40GB,
    "A100-PCIE-40GB": GPUType.A100_40GB,
    "A100 40GB": GPUType.A100_40GB,
    "A100-SXM4-80GB": GPUType.A100_80GB,
    "A100-PCIE-80GB": GPUType.A100_80GB,
    "A100 80GB": GPUType.A100_80GB,
    "A40": GPUType.A40,
    "A10": GPUType.A10,
    "L40S": GPUType.L40S,
    "L40": GPUType.L40,
    "H100-SXM5-80GB": GPUType.H100_80GB,
    "H100-PCIE-80GB": GPUType.H100_80GB,
    "H100 80GB": GPUType.H100_80GB,
    "H100 SXM": GPUType.H100_SXM,
    "Tesla V100-SXM2-16GB": GPUType.V100_16GB,
    "Tesla V100-SXM2-32GB": GPUType.V100_32GB,
    "Tesla T4": GPUType.T4,
    "RTX 5080": GPUType.RTX_5080,
    "GeForce RTX 5080": GPUType.RTX_5080,
    "NVIDIA GeForce RTX 5080": GPUType.RTX_5080,
    "RTX 5090": GPUType.RTX_5090,
    "GeForce RTX 5090": GPUType.RTX_5090,
    "NVIDIA GeForce RTX 5090": GPUType.RTX_5090,
}

# Reverse mapping: GPUType -> Vast.ai search query
# NOTE: Vast.ai CLI query language requires underscores instead of spaces
# in gpu_name values.  e.g. gpu_name=RTX_5080  (NOT gpu_name=RTX 5080)
GPUYPE_TO_VAST_QUERY = {
    GPUType.RTX_3090: "RTX_3090",
    GPUType.RTX_4090: "RTX_4090",
    GPUType.A100_40GB: "A100",
    GPUType.A100_80GB: "A100",
    GPUType.A40: "A40",
    GPUType.A10: "A10",
    GPUType.L40S: "L40S",
    GPUType.L40: "L40",
    GPUType.H100_80GB: "H100",
    GPUType.H100_SXM: "H100",
    GPUType.V100_16GB: "V100",
    GPUType.V100_32GB: "V100",
    GPUType.T4: "T4",
    GPUType.RTX_5080: "RTX_5080",
    GPUType.RTX_5090: "RTX_5090",
}


def _looks_like_fatal_startup_status(status_message: str) -> bool:
    """Return True when Vast's startup message indicates a terminal boot/pull failure."""
    normalized = str(status_message or "").strip().lower()
    if not normalized:
        return False
    fatal_markers = (
        "certificate is not yet valid",
        "curl failed to verify",
        "failed to verify the legitimacy",
        "x509:",
        "tls:",
        "ssl:",
        "pull access denied",
        "requested access to the resource is denied",
        "no basic auth credentials",
        "authentication required",
        "insufficient_scope",
        "unauthorized",
        "denied:",
        "manifest unknown",
        "failed to resolve reference",
        "repository does not exist",
        "name unknown",
        "error response from daemon",
        "failed to copy",
        "oci runtime",
        "runc create failed",
        "failed to start container",
        "exec format error",
        "permission denied",
        "no space left on device",
        "no such host",
        "temporary failure in name resolution",
        "connection refused",
        "context deadline exceeded",
        "i/o timeout",
        "problem:",
    )
    return any(marker in normalized for marker in fatal_markers)


class VastProvider(CloudProvider):
    """
    Vast.ai marketplace provider.
    
    Uses the vastai CLI for all operations. The CLI must be installed
    and configured with an API key.
    
    Example:
        provider = VastProvider(api_key="your_key")
        offers = await provider.search_offers(GPUType.RTX_4090, max_price=0.50)
        result = await provider.create_instance(
            offers[0],
            docker_image="pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        )
    """
    
    PROVIDER_NAME = "vast"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        ssh_key_path: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize Vast.ai provider.
        
        Args:
            api_key: Vast.ai API key (or set VAST_API_KEY env var)
            ssh_key_path: Path to SSH private key for instance access
        """
        super().__init__(api_key, **kwargs)
        
        # Get API key from env if not provided
        self.api_key = api_key or os.environ.get("VAST_API_KEY")
        
        # SSH key for instance access (prefer vast_key, fallback to env var)
        self.ssh_key_path = (
            ssh_key_path
            or os.environ.get("VAST_SSH_KEY_PATH")
            or os.path.expanduser("~/.ssh/vast_key")
        )
        
        # Verify vastai CLI is installed
        if not shutil.which("vastai"):
            raise RuntimeError(
                "vastai CLI not found. Install with: pip install vastai"
            )
        
        # Set API key if provided
        if self.api_key:
            self._run_cli(["set", "api-key", self.api_key])
    
    def _run_cli(
        self,
        args: List[str],
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """
        Run vastai CLI command and return parsed JSON output.
        
        Args:
            args: CLI arguments (without 'vastai' prefix)
            timeout: Command timeout in seconds
            
        Returns:
            Parsed JSON response or {"raw": stdout} for non-JSON output
        """
        cmd = ["vastai"] + args
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            
            if result.returncode != 0:
                raise RuntimeError(f"vastai CLI error: {result.stderr}")
            
            # Try to parse as JSON
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"raw": result.stdout.strip()}
                
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"vastai command timed out: {' '.join(cmd)}")
    
    async def _run_cli_async(
        self,
        args: List[str],
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Async wrapper for CLI commands."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._run_cli(args, timeout)
        )
    
    def _parse_gpu_type(self, gpu_name: str) -> Optional[GPUType]:
        """Parse Vast.ai GPU name to GPUType enum."""
        # Exact match
        if gpu_name in VAST_GPU_MAPPING:
            return VAST_GPU_MAPPING[gpu_name]
        
        # Partial match
        gpu_name_lower = gpu_name.lower()
        for vast_name, gpu_type in VAST_GPU_MAPPING.items():
            if vast_name.lower() in gpu_name_lower:
                return gpu_type
        
        return None
    
    def _parse_offer(self, data: Dict[str, Any]) -> Optional[GPUOffer]:
        """Parse Vast.ai offer JSON to GPUOffer dataclass."""
        try:
            gpu_name = data.get("gpu_name", "")
            gpu_type = self._parse_gpu_type(gpu_name)
            
            if gpu_type is None:
                return None  # Unknown GPU type
            
            return GPUOffer(
                provider=self.PROVIDER_NAME,
                offer_id=str(data.get("id", "")),
                gpu_type=gpu_type,
                gpu_count=data.get("num_gpus", 1),
                gpu_memory_gb=data.get("gpu_ram", 0) / 1024,  # MB to GB
                cpu_cores=data.get("cpu_cores", 0),
                ram_gb=data.get("cpu_ram", 0) / 1024,  # MB to GB
                disk_gb=data.get("disk_space", 0),
                disk_type="nvme" if data.get("disk_name", "").lower().find("nvme") >= 0 else "ssd",
                price_per_hour=data.get("dph_total", 0),  # dollars per hour
                is_spot=True,  # Vast.ai is primarily spot
                upload_mbps=data.get("inet_up", 0),
                download_mbps=data.get("inet_down", 0),
                region=data.get("geolocation", ""),
                datacenter=data.get("hosting_type", ""),
                raw_data=data,
            )
        except Exception as e:
            print(f"Failed to parse offer: {e}")
            return None
    
    async def search_offers(
        self,
        gpu_type: GPUType,
        max_price: Optional[float] = None,
        min_gpu_count: int = 1,
        prefer_spot: bool = True,
        region: Optional[str] = None,
        min_reliability: float = 0.97,
        min_disk_gb: Optional[float] = None,
    ) -> List[GPUOffer]:
        """
        Search Vast.ai marketplace for available GPU offers.
        
        Args:
            gpu_type: Desired GPU type
            max_price: Maximum $/hr
            min_gpu_count: Minimum GPUs per instance
            prefer_spot: Always True for Vast.ai (spot-only)
            region: Filter by region (e.g., "US", "EU")
            
        Returns:
            List of GPUOffer sorted by price
        """
        # Build search query
        gpu_query = GPUYPE_TO_VAST_QUERY.get(gpu_type, "")
        
        # Vast.ai search uses a query language
        # Format: vastai search offers 'query'
        query_parts = [f"gpu_name={gpu_query}"]
        
        if min_gpu_count > 1:
            query_parts.append(f"num_gpus>={min_gpu_count}")
        
        if max_price is not None:
            query_parts.append(f"dph_total<={max_price}")
        
        # Ensure GPU has sufficient compute capability (CUDA support)
        # Note: cuda_vers / cuda_max_good are NOT valid search filters.
        # compute_cap is the correct field (610=sm_61, 890=sm_89, 1200=sm_120)
        query_parts.append("compute_cap>=610")
        
        # Require reasonable reliability
        if min_reliability > 0:
            query_parts.append(f"reliability>={min_reliability:.2f}")
        
        query = " ".join(query_parts)
        
        # Run search
        args = ["search", "offers", query, "--raw"]
        result = await self._run_cli_async(args)
        
        # Parse results
        offers = []
        if isinstance(result, list):
            for item in result:
                offer = self._parse_offer(item)
                if offer is not None:
                    # Filter by GPU type (double-check)
                    if offer.gpu_type == gpu_type:
                        if min_disk_gb is None or float(offer.disk_gb) >= float(min_disk_gb):
                            offers.append(offer)
        
        # Sort by price
        offers.sort(key=lambda x: (float(x.price_per_hour), -float(x.disk_gb)))
        
        return offers
    
    async def create_instance(
        self,
        offer: GPUOffer,
        docker_image: str,
        docker_command: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        job_id: Optional[str] = None,
        disk_gb: Optional[float] = None,
    ) -> ProvisionResult:
        """
        Create a Vast.ai instance from an offer.
        
        Args:
            offer: GPUOffer from search_offers()
            docker_image: Docker image to run
            docker_command: Entrypoint command (optional)
            env_vars: Environment variables
            job_id: Job ID for tracking
            
        Returns:
            ProvisionResult with instance details
        """
        start_time = datetime.utcnow()
        
        try:
            # Build create command
            # vastai create instance OFFER_ID --image IMAGE [--onstart-cmd CMD] [--env VAR=VAL]
            args = [
                "create", "instance",
                offer.offer_id,
                "--image", docker_image,
            ]

            ghcr_auth = ghcr_basic_auth() if is_ghcr_image(docker_image) else None
            if ghcr_auth:
                args.extend(
                    [
                        "--login",
                        f"-u {ghcr_auth['username']} -p {ghcr_auth['password']} ghcr.io",
                    ]
                )
            
            # Use --onstart-cmd for direct commands (not file path)
            if docker_command:
                args.extend(["--onstart-cmd", docker_command])
            
            if env_vars:
                for key, value in env_vars.items():
                    args.extend(["--env", f"{key}={value}"])
            
            # Add disk allocation
            requested_disk_gb = float(disk_gb) if disk_gb is not None else float(offer.disk_gb)
            requested_disk_gb = max(1.0, min(requested_disk_gb, float(offer.disk_gb)))
            args.extend(["--disk", str(int(requested_disk_gb))])
            
            # Create instance
            result = await self._run_cli_async(args, timeout=120)
            
            # Parse response - should contain instance ID
            # Response format: {"success": true, "new_contract": 12345}
            if isinstance(result, dict):
                if result.get("success"):
                    instance_id = str(result.get("new_contract", ""))
                elif "raw" in result:
                    # Try to extract ID from raw output
                    import re
                    match = re.search(r"(\d+)", result["raw"])
                    instance_id = match.group(1) if match else ""
                else:
                    raise RuntimeError(f"Failed to create instance: {result}")
            else:
                raise RuntimeError(f"Unexpected response: {result}")
            
            if not instance_id:
                raise RuntimeError("No instance ID returned")
            
            # Create Instance object
            instance = Instance(
                provider=self.PROVIDER_NAME,
                instance_id=instance_id,
                status=InstanceStatus.PROVISIONING,
                gpu_type=offer.gpu_type,
                gpu_count=offer.gpu_count,
                price_per_hour=offer.price_per_hour,
                started_at=datetime.utcnow(),
                job_id=job_id,
                raw_data={"offer": offer.raw_data},
            )
            
            # Track instance
            self._instances[instance_id] = instance
            
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            
            return ProvisionResult(
                success=True,
                instance=instance,
                offer_used=offer,
                provision_time_seconds=elapsed,
            )
            
        except Exception as e:
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            return ProvisionResult(
                success=False,
                error_message=str(e),
                provision_time_seconds=elapsed,
            )
    
    async def destroy_instance(self, instance_id: str) -> bool:
        """
        Terminate a Vast.ai instance.
        
        Args:
            instance_id: Instance/contract ID
            
        Returns:
            True if destroyed successfully
        """
        try:
            args = ["destroy", "instance", instance_id]
            await self._run_cli_async(args)

            deadline = asyncio.get_event_loop().time() + 60.0
            while True:
                listing = await self._run_cli_async(["show", "instances", "--raw"])
                present = False
                if isinstance(listing, list):
                    for item in listing:
                        candidate_id = str(
                            item.get("id")
                            or item.get("contract_id")
                            or item.get("instance_id")
                            or ""
                        )
                        if candidate_id == str(instance_id):
                            present = True
                            break
                if not present:
                    if instance_id in self._instances:
                        self._instances[instance_id].status = InstanceStatus.TERMINATED
                    return True
                if asyncio.get_event_loop().time() > deadline:
                    print(f"Destroy command completed but instance {instance_id} still appears in Vast listings after 60s")
                    return False
                await asyncio.sleep(2.0)
            
        except Exception as e:
            print(f"Failed to destroy instance {instance_id}: {e}")
            return False
    
    async def get_instance_status(self, instance_id: str) -> Instance:
        """
        Get current status of a Vast.ai instance.
        
        Args:
            instance_id: Instance/contract ID
            
        Returns:
            Instance with current status
        """
        # Vast's singular `show instance` path can crash when the provider emits
        # a null `start_date`. The bulk listing path is materially more stable,
        # so resolve the row from `show instances --raw` and only fall back to
        # the cached instance if the freshly-created contract is not visible yet.
        args = ["show", "instances", "--raw"]
        listing = await self._run_cli_async(args)

        result: Optional[Dict[str, Any]] = None
        if isinstance(listing, list):
            for item in listing:
                candidate_id = str(
                    item.get("id")
                    or item.get("contract_id")
                    or item.get("instance_id")
                    or ""
                )
                if candidate_id == str(instance_id):
                    result = item
                    break

        if result is None:
            cached = self._instances.get(instance_id)
            if cached is not None:
                return cached
            raise RuntimeError(f"Failed to get instance status for {instance_id}: not found in Vast listing")
        
        # Parse status
        status_str = result.get("actual_status") or result.get("status", "")
        if status_str:
            status_str = str(status_str).lower()
        else:
            status_str = "pending"

        status_map = {
            "running": InstanceStatus.RUNNING,
            "loading": InstanceStatus.PROVISIONING,
            "created": InstanceStatus.PENDING,
            "exited": InstanceStatus.STOPPED,
            "offline": InstanceStatus.TERMINATED,
            "error": InstanceStatus.ERROR,
        }

        status = status_map.get(status_str, InstanceStatus.PENDING)
        status_message = str(result.get("status_msg") or "").strip() or None
        if status in {InstanceStatus.PENDING, InstanceStatus.PROVISIONING} and status_message:
            if _looks_like_fatal_startup_status(status_message):
                status = InstanceStatus.ERROR

        # Get SSH info
        ssh_host = result.get("ssh_host", "")
        ssh_port = result.get("ssh_port", 22)

        # Get Jupyter URL if available
        jupyter_port = result.get("jupyter_port")
        jupyter_url = f"http://{ssh_host}:{jupyter_port}" if jupyter_port else None

        # Update cached instance or create new
        if instance_id in self._instances:
            instance = self._instances[instance_id]
            instance.status = status
            instance.ssh_host = ssh_host
            instance.ssh_port = ssh_port
            instance.ssh_user = instance.ssh_user or "root"
            instance.ssh_key_path = instance.ssh_key_path or self.ssh_key_path
            instance.jupyter_url = jupyter_url
            instance.error_message = status_message if status == InstanceStatus.ERROR else None
            instance.raw_data.update(result)
        else:
            instance = Instance(
                provider=self.PROVIDER_NAME,
                instance_id=instance_id,
                status=status,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user="root",
                ssh_key_path=self.ssh_key_path,
                jupyter_url=jupyter_url,
                price_per_hour=result.get("dph_total", 0),
                error_message=status_message if status == InstanceStatus.ERROR else None,
                raw_data=result,
            )
            self._instances[instance_id] = instance

        return instance
    
    async def list_my_instances(self) -> List[Instance]:
        """
        List all instances in the Vast.ai account.
        
        Returns:
            List of all instances (not just locally tracked)
        """
        args = ["show", "instances", "--raw"]
        result = await self._run_cli_async(args)
        
        instances = []
        if isinstance(result, list):
            for item in result:
                instance_id = str(item.get("id", ""))
                instance = await self.get_instance_status(instance_id)
                instances.append(instance)
        
        return instances
    
    async def get_account_balance(self) -> float:
        """
        Get current Vast.ai account balance.
        
        Returns:
            Balance in USD
        """
        args = ["show", "user", "--raw"]
        result = await self._run_cli_async(args)
        
        if isinstance(result, dict):
            return float(result.get("credit", 0))
        return 0.0
    
    async def health_check(self) -> bool:
        """Check if Vast.ai API is accessible."""
        try:
            await self.get_account_balance()
            return True
        except Exception:
            return False

 """
vast_provisioning_server.py - MCP Server for Vast.ai GPU Provisioning + SSH Execution

Provides MCP tools for:
- Searching GPU offers
- Creating instances
- Executing SSH commands remotely
- Destroying instances
- Monitoring status

This allows the AgenticDriver to provision GPUs on-demand without Docker complexity.

Author: Team 2
Date: 2026-01-21
"""

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mica.infrastructure.providers.vast_provider import VastProvider
from mica.infrastructure.providers.base_provider import GPUType
from mica.infrastructure.persistence import TimescaleEventStore, TimescaleJobStore
from mica.infrastructure.ssh_resilience import (
    get_ssh_executor,
    CommandProtocol,
    SSHFailureReason
)

# Try fastmcp import
try:
    from fastmcp import FastMCP
except ImportError:
    print("ERROR: fastmcp not installed. Run: pip install fastmcp")
    sys.exit(1)


# Initialize MCP server
mcp = FastMCP("Vast.ai GPU Provisioning & SSH")

# Global state (in-memory for now, could persist to Timescale)
_active_instances: Dict[str, Dict[str, Any]] = {}
_provider: Optional[VastProvider] = None
_event_store: Optional[TimescaleEventStore] = None


async def get_provider() -> VastProvider:
    """Get or initialize Vast provider."""
    global _provider
    if _provider is None:
        _provider = VastProvider()
    return _provider


async def get_event_store() -> TimescaleEventStore:
    """Get or initialize event store."""
    global _event_store
    if _event_store is None:
        _event_store = TimescaleEventStore()
        await _event_store.initialize()
    return _event_store


@mcp.tool()
async def vast_search_offers(
    gpu_type: str = "RTX_4090",
    max_price: float = 0.50,
    min_gpu_count: int = 1,
    limit: int = 10
) -> str:
    """
    Search Vast.ai for available GPU offers.
    
    Args:
        gpu_type: GPU type (RTX_3090, RTX_4090, A100_40GB, L40S, H100_80GB)
        max_price: Maximum price per hour in USD
        min_gpu_count: Minimum number of GPUs
        limit: Maximum offers to return
        
    Returns:
        JSON string with list of offers sorted by price
    """
    try:
        provider = await get_provider()
        
        # Convert string to GPUType enum
        gpu_enum = GPUType[gpu_type]
        
        # Search offers
        offers = await provider.search_offers(
            gpu_type=gpu_enum,
            max_price=max_price,
            min_gpu_count=min_gpu_count
        )
        
        # Limit results
        offers = offers[:limit]
        
        # Format results
        results = []
        for offer in offers:
            results.append({
                "offer_id": offer.offer_id,
                "gpu_type": offer.gpu_type.value,
                "gpu_count": offer.gpu_count,
                "gpu_memory_gb": offer.gpu_memory_gb,
                "cpu_cores": offer.cpu_cores,
                "ram_gb": offer.ram_gb,
                "disk_gb": offer.disk_gb,
                "price_per_hour": offer.price_per_hour,
                "region": offer.region,
                "datacenter": offer.datacenter,
            })
        
        return json.dumps({
            "success": True,
            "count": len(results),
            "offers": results
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
async def vast_create_instance(
    offer_id: str,
    docker_image: str = "pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime",
    docker_command: str = "sleep infinity",
    job_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    bucket: Optional[str] = None,
) -> str:
    """
    Create a Vast.ai instance from an offer.
    
    Args:
        offer_id: Offer ID from vast_search_offers
        docker_image: Docker image to run
        docker_command: Command to run in container
        job_id: Optional job ID for tracking
        
    Returns:
        JSON with instance details including SSH connection info
    """
    try:
        provider = await get_provider()
        
        # Search for the specific offer to get full details
        # We need to get the offer object first
        offers = await provider.search_offers(
            gpu_type=GPUType.RTX_4090,  # Will be filtered by offer_id
            max_price=999.0  # High limit to find any offer
        )
        
        offer = None
        for o in offers:
            if o.offer_id == offer_id:
                offer = o
                break
        
        if offer is None:
            return json.dumps({
                "success": False,
                "error": f"Offer {offer_id} not found or no longer available"
            }, indent=2)
        
        # Create instance
        result = await provider.create_instance(
            offer=offer,
            docker_image=docker_image,
            docker_command=docker_command,
            job_id=job_id
        )
        
        if not result.success:
            return json.dumps({
                "success": False,
                "error": result.error_message
            }, indent=2)
        
        instance = result.instance
        
        # Store in active instances
        _active_instances[instance.instance_id] = {
            "instance_id": instance.instance_id,
            "offer_id": offer_id,
            "gpu_type": instance.gpu_type.value,
            "gpu_count": instance.gpu_count,
            "price_per_hour": instance.price_per_hour,
            "status": instance.status.value,
            "created_at": instance.started_at.isoformat() if instance.started_at else None,
            "job_id": job_id,
            "user_id": user_id,
            "session_id": session_id,
            "bucket": bucket,
        }
        
        # Log event
        event_store = await get_event_store()
        from mica.infrastructure.event_store import ProvisioningSucceededEvent
        await event_store.append(ProvisioningSucceededEvent(
            job_id=job_id or instance.instance_id,
            instance_id=instance.instance_id,
            provider="vast",
            gpu_type=instance.gpu_type.value,
            gpu_count=instance.gpu_count,
            price_per_hour=instance.price_per_hour,
            user_id=user_id,
            session_id=session_id,
            bucket=bucket,
            metadata={
                "user_id": user_id,
                "session_id": session_id,
                "bucket": bucket,
            },
        ))
        
        return json.dumps({
            "success": True,
            "instance_id": instance.instance_id,
            "status": instance.status.value,
            "gpu_type": instance.gpu_type.value,
            "gpu_count": instance.gpu_count,
            "price_per_hour": instance.price_per_hour,
            "ssh_host": instance.ssh_host,
            "ssh_port": instance.ssh_port,
            "ssh_command": f"ssh -p {instance.ssh_port} root@{instance.ssh_host}",
            "message": "Instance created. Wait ~30-60s for it to be RUNNING."
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
async def vast_ssh_exec(
    instance_id: str,
    command: str,
    timeout: int = 300,
    protocol: str = "retry_3x",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    bucket: Optional[str] = None,
) -> str:
    """
    Execute a command on a Vast.ai instance via SSH with resilient retry logic.
    
    Args:
        instance_id: Instance ID from vast_create_instance
        command: Shell command to execute
        timeout: Timeout in seconds (default 300)
        protocol: Execution protocol - 'fail_fast', 'retry_3x', 'retry_5x', 'fallback'
        
    Returns:
        JSON with command output (stdout, stderr, exit_code, attempts, failure_reason)
        
    Protocols:
        - fail_fast: Single attempt, immediate failure (for quick checks)
        - retry_3x: Retry up to 3 times with exponential backoff (default, recommended)
        - retry_5x: Retry up to 5 times (for critical long operations)
        - fallback: Try alternatives if primary fails
    """
    try:
        # Get instance details
        if instance_id not in _active_instances:
            return json.dumps({
                "success": False,
                "error": f"Instance {instance_id} not found in active instances"
            }, indent=2)
        
        provider = await get_provider()
        instance = await provider.get_instance_status(instance_id)
        
        if not instance.ssh_host or not instance.ssh_port:
            return json.dumps({
                "success": False,
                "error": "SSH info not available yet. Wait for instance to be RUNNING."
            }, indent=2)
        
        # Map protocol string to enum
        protocol_map = {
            "fail_fast": CommandProtocol.FAIL_FAST,
            "retry_3x": CommandProtocol.RETRY_3X,
            "retry_5x": CommandProtocol.RETRY_5X,
            "fallback": CommandProtocol.FALLBACK,
        }
        exec_protocol = protocol_map.get(protocol.lower(), CommandProtocol.RETRY_3X)
        
        # Use resilient SSH executor
        executor = get_ssh_executor()
        result = await executor.execute_with_protocol(
            host=instance.ssh_host,
            port=instance.ssh_port,
            command=command,
            protocol=exec_protocol,
            timeout=timeout,
            key_path=provider.ssh_key_path,
        )
        
        return json.dumps({
            "success": result.success,
            "instance_id": instance_id,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "execution_time": result.duration,
            "attempts": result.attempts,
            "protocol": protocol,
            "failure_reason": result.failure_reason.value if result.failure_reason else None,
            "user_id": user_id,
            "session_id": session_id,
            "bucket": bucket,
            "message": "Command executed successfully" if result.success else f"Command failed after {result.attempts} attempts"
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
async def vast_get_instance_status(instance_id: str) -> str:
    """
    Get current status of a Vast.ai instance.
    
    Args:
        instance_id: Instance ID
        
    Returns:
        JSON with instance status and details
    """
    try:
        provider = await get_provider()
        instance = await provider.get_instance_status(instance_id)
        
        # Update local cache
        if instance_id in _active_instances:
            _active_instances[instance_id]["status"] = instance.status.value
        
        return json.dumps({
            "success": True,
            "instance_id": instance.instance_id,
            "status": instance.status.value,
            "gpu_type": instance.gpu_type.value,
            "gpu_count": instance.gpu_count,
            "price_per_hour": instance.price_per_hour,
            "ssh_host": instance.ssh_host,
            "ssh_port": instance.ssh_port,
            "uptime_seconds": (datetime.utcnow() - instance.started_at).total_seconds() if instance.started_at else 0,
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
async def vast_destroy_instance(
    instance_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    bucket: Optional[str] = None,
) -> str:
    """
    Destroy a Vast.ai instance.
    
    Args:
        instance_id: Instance ID to destroy
        
    Returns:
        JSON with destruction status
    """
    try:
        provider = await get_provider()
        
        # Get final status before destruction
        instance = await provider.get_instance_status(instance_id)
        uptime = (datetime.utcnow() - instance.started_at).total_seconds() if instance.started_at else 0
        estimated_cost = (instance.price_per_hour / 3600) * uptime
        
        # Capture attribution before we mutate local cache.
        cached = _active_instances.get(instance_id) or {}
        resolved_user_id = user_id or cached.get("user_id")
        resolved_session_id = session_id or cached.get("session_id")
        resolved_bucket = bucket or cached.get("bucket")

        # Destroy instance
        success = await provider.destroy_instance(instance_id)
        
        if success:
            # Remove from active instances
            _active_instances.pop(instance_id, None)
            
            # Log event
            event_store = await get_event_store()
            from mica.infrastructure.event_store import InstanceTerminatedEvent
            await event_store.append(InstanceTerminatedEvent(
                instance_id=instance_id,
                provider="vast",
                user_id=resolved_user_id,
                session_id=resolved_session_id,
                bucket=resolved_bucket,
                metadata={
                    "uptime_seconds": uptime,
                    "estimated_cost_usd": estimated_cost,
                    "user_id": resolved_user_id,
                    "session_id": resolved_session_id,
                    "bucket": resolved_bucket,
                }
            ))
            
            return json.dumps({
                "success": True,
                "instance_id": instance_id,
                "uptime_seconds": uptime,
                "estimated_cost_usd": round(estimated_cost, 4),
                "message": "Instance destroyed successfully"
            }, indent=2)
        else:
            return json.dumps({
                "success": False,
                "error": "Failed to destroy instance"
            }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
async def vast_list_active_instances() -> str:
    """
    List all active instances created by this MCP server.
    
    Returns:
        JSON with list of active instances
    """
    try:
        instances = []
        for instance_id, data in _active_instances.items():
            # Get fresh status
            provider = await get_provider()
            instance = await provider.get_instance_status(instance_id)
            
            instances.append({
                "instance_id": instance_id,
                "status": instance.status.value,
                "gpu_type": data["gpu_type"],
                "gpu_count": data["gpu_count"],
                "price_per_hour": data["price_per_hour"],
                "created_at": data["created_at"],
                "job_id": data.get("job_id"),
            })
        
        return json.dumps({
            "success": True,
            "count": len(instances),
            "instances": instances
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


# Server metadata
@mcp.resource("vast://status")
async def get_server_status() -> str:
    """Get MCP server status and statistics."""
    return json.dumps({
        "server": "Vast.ai GPU Provisioning & SSH",
        "version": "1.0.0",
        "active_instances": len(_active_instances),
        "capabilities": [
            "GPU offer search",
            "Instance provisioning",
            "SSH command execution",
            "Status monitoring",
            "Instance cleanup"
        ]
    }, indent=2)


if __name__ == "__main__":
    # Run MCP server
    mcp.run()

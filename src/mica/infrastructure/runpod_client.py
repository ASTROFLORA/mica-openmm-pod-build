"""
RunPod Serverless Infrastructure Client

Gestiona endpoints, jobs y health monitoring para BioDynamo en RunPod.

Basado en: https://docs.runpod.io/serverless/endpoints/send-requests

Autor: MICA Infrastructure Team
Fecha: 12 de Noviembre, 2025
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


def _make_connector() -> aiohttp.TCPConnector:
    """Use the OS resolver to avoid aiodns failures on some Windows setups."""
    return aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())


class JobStatus(str, Enum):
    """RunPod job statuses"""
    IN_QUEUE = "IN_QUEUE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


@dataclass
class RunPodJob:
    """RunPod job representation"""
    id: str
    status: JobStatus
    delay_time: Optional[int] = None  # Queue delay in ms
    execution_time: Optional[int] = None  # Processing time in ms
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class EndpointHealth:
    """RunPod endpoint health status"""
    jobs_completed: int
    jobs_failed: int
    jobs_in_progress: int
    jobs_in_queue: int
    jobs_retried: int
    workers_idle: int
    workers_running: int


class RunPodClient:
    """
    RunPod Serverless API Client
    
    Gestiona endpoints, jobs y monitoring para BioDynamo en RunPod.
    
    Args:
        api_key (str): RunPod API key (default: $RUNPOD_API_KEY env var)
        endpoint_id (str, optional): Default endpoint ID (default: $RUNPOD_ENDPOINT_ID)
        base_url (str): RunPod API base URL
    
    Example:
        >>> client = RunPodClient(
        ...     api_key="YOUR_API_KEY",
        ...     endpoint_id="YOUR_ENDPOINT_ID"
        ... )
        >>> 
        >>> # Submit async job
        >>> job = await client.submit_job({
        ...     "input": {"protein_pdb": "1ABC", "simulation_time_ns": 100}
        ... })
        >>> 
        >>> # Poll status
        >>> result = await client.get_job_status(job.id)
        >>> 
        >>> # Health check
        >>> health = await client.get_endpoint_health()
    
    Raises:
        ValueError: If API key missing or invalid endpoint_id
        aiohttp.ClientResponseError: For HTTP errors (401, 404, 429, 500)
    
    See Also:
        - https://docs.runpod.io/serverless/endpoints/send-requests
        - runpod/rp_dynamo_handler.py (serverless handler)
        - docker/Dockerfile.runpod-dynamo (container image)
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        base_url: str = "https://api.runpod.ai/v2",
        timeout: int = 300,  # 5 minutes default
    ):
        self.api_key = api_key or os.getenv("RUNPOD_API_KEY")
        if not self.api_key:
            raise ValueError(
                "RunPod API key required. Set RUNPOD_API_KEY env var or pass api_key parameter."
            )
        
        self.endpoint_id = endpoint_id or os.getenv("RUNPOD_ENDPOINT_ID")
        self.base_url = base_url
        self.timeout = timeout
        
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        """Context manager entry"""
        await self._ensure_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self.close()
    
    async def _ensure_session(self):
        """Ensure aiohttp session exists"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=_make_connector(),
                headers={
                    "Authorization": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
    
    async def close(self):
        """Close aiohttp session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    def _get_endpoint_url(self, endpoint_id: Optional[str] = None) -> str:
        """Get base URL for endpoint"""
        eid = endpoint_id or self.endpoint_id
        if not eid:
            raise ValueError("endpoint_id required. Set RUNPOD_ENDPOINT_ID or pass parameter.")
        return f"{self.base_url}/{eid}"
    
    # ===========================
    # JOB SUBMISSION
    # ===========================
    
    async def submit_job(
        self,
        input_data: Dict[str, Any],
        endpoint_id: Optional[str] = None,
        webhook: Optional[str] = None,
        execution_timeout: Optional[int] = None,  # milliseconds
        low_priority: bool = False,
        ttl: Optional[int] = None,  # milliseconds
        s3_config: Optional[Dict[str, str]] = None,
    ) -> RunPodJob:
        """
        Submit asynchronous job (/run endpoint)
        
        Args:
            input_data: Job input (passed to handler's event['input'])
            endpoint_id: Override default endpoint
            webhook: Webhook URL for completion notification
            execution_timeout: Max runtime in ms (default: 600000 = 10 min)
            low_priority: Don't trigger worker scaling
            ttl: Max job lifetime in ms (default: 86400000 = 24h)
            s3_config: S3 storage config (accessId, accessSecret, bucketName, endpointUrl)
        
        Returns:
            RunPodJob with id and status IN_QUEUE
        
        Raises:
            aiohttp.ClientResponseError: HTTP errors (401, 404, 429, 500)
        
        Example:
            >>> job = await client.submit_job({
            ...     "protein_pdb": "1ABC",
            ...     "simulation_time_ns": 100,
            ...     "temperature_k": 310,
            ... })
            >>> print(job.id)  # "eaebd6e7-6a92-4bb8-a911-f996ac5ea99d"
        """
        await self._ensure_session()
        
        payload: Dict[str, Any] = {"input": input_data}
        
        # Advanced options
        if webhook:
            payload["webhook"] = webhook
        
        policy: Dict[str, Any] = {}
        if execution_timeout:
            policy["executionTimeout"] = execution_timeout
        if low_priority:
            policy["lowPriority"] = True
        if ttl:
            policy["ttl"] = ttl
        if policy:
            payload["policy"] = policy
        
        if s3_config:
            payload["s3Config"] = s3_config
        
        url = f"{self._get_endpoint_url(endpoint_id)}/run"
        
        async with self._session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        
        return RunPodJob(
            id=data["id"],
            status=JobStatus(data["status"]),
        )
    
    async def submit_sync_job(
        self,
        input_data: Dict[str, Any],
        endpoint_id: Optional[str] = None,
        wait: int = 60000,  # milliseconds (default 1 min, max 5 min)
    ) -> RunPodJob:
        """
        Submit synchronous job (/runsync endpoint)
        
        Waits for completion and returns result in single response.
        
        Args:
            input_data: Job input
            endpoint_id: Override default endpoint
            wait: Result TTL in ms (1000-300000, default 60000 = 1 min)
        
        Returns:
            RunPodJob with status COMPLETED and output
        
        Example:
            >>> job = await client.submit_sync_job(
            ...     {"protein_pdb": "1ABC"},
            ...     wait=120000  # 2 minutes
            ... )
            >>> print(job.output)
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/runsync?wait={wait}"
        payload = {"input": input_data}
        
        async with self._session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        
        return RunPodJob(
            id=data["id"],
            status=JobStatus(data["status"]),
            delay_time=data.get("delayTime"),
            execution_time=data.get("executionTime"),
            output=data.get("output"),
        )
    
    # ===========================
    # JOB MONITORING
    # ===========================
    
    async def get_job_status(
        self,
        job_id: str,
        endpoint_id: Optional[str] = None,
        ttl: Optional[int] = None,  # milliseconds
    ) -> RunPodJob:
        """
        Get job status and results (/status endpoint)
        
        Args:
            job_id: Job ID from submit_job()
            endpoint_id: Override default endpoint
            ttl: Custom TTL in ms (optional)
        
        Returns:
            RunPodJob with current status and output (if completed)
        
        Example:
            >>> job = await client.get_job_status("eaebd6e7-...")
            >>> if job.status == JobStatus.COMPLETED:
            ...     print(job.output)
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/status/{job_id}"
        if ttl:
            url += f"?ttl={ttl}"
        
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        
        return RunPodJob(
            id=data["id"],
            status=JobStatus(data["status"]),
            delay_time=data.get("delayTime"),
            execution_time=data.get("executionTime"),
            output=data.get("output"),
        )
    
    async def poll_until_complete(
        self,
        job_id: str,
        endpoint_id: Optional[str] = None,
        poll_interval: float = 5.0,  # seconds
        max_wait: float = 1800.0,  # 30 minutes
    ) -> RunPodJob:
        """
        Poll job status until completion
        
        Args:
            job_id: Job ID
            endpoint_id: Override default endpoint
            poll_interval: Seconds between status checks
            max_wait: Max total wait time in seconds
        
        Returns:
            RunPodJob with final status
        
        Raises:
            TimeoutError: If max_wait exceeded
        
        Example:
            >>> job = await client.submit_job({"protein_pdb": "1ABC"})
            >>> result = await client.poll_until_complete(job.id)
            >>> print(result.output)
        """
        start_time = time.time()
        
        while True:
            job = await self.get_job_status(job_id, endpoint_id)
            
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}:
                return job
            
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                raise TimeoutError(
                    f"Job {job_id} did not complete within {max_wait}s (status: {job.status})"
                )
            
            await asyncio.sleep(poll_interval)
    
    # ===========================
    # JOB MANAGEMENT
    # ===========================
    
    async def cancel_job(
        self,
        job_id: str,
        endpoint_id: Optional[str] = None,
    ) -> RunPodJob:
        """
        Cancel in-progress or queued job (/cancel endpoint)
        
        Args:
            job_id: Job ID
            endpoint_id: Override default endpoint
        
        Returns:
            RunPodJob with status CANCELLED
        
        Example:
            >>> await client.cancel_job("eaebd6e7-...")
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/cancel/{job_id}"
        
        async with self._session.post(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        
        return RunPodJob(
            id=data["id"],
            status=JobStatus(data["status"]),
        )
    
    async def retry_job(
        self,
        job_id: str,
        endpoint_id: Optional[str] = None,
    ) -> RunPodJob:
        """
        Retry failed/timed out job (/retry endpoint)
        
        Args:
            job_id: Job ID (must be FAILED or TIMED_OUT)
            endpoint_id: Override default endpoint
        
        Returns:
            RunPodJob with status IN_QUEUE
        
        Example:
            >>> await client.retry_job("failed-job-id")
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/retry/{job_id}"
        
        async with self._session.post(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        
        return RunPodJob(
            id=data["id"],
            status=JobStatus(data["status"]),
        )
    
    async def purge_queue(
        self,
        endpoint_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Remove all pending jobs from queue (/purge-queue endpoint)
        
        WARNING: Only affects queued jobs, not in-progress jobs.
        
        Args:
            endpoint_id: Override default endpoint
        
        Returns:
            Dict with 'removed' count and 'status'
        
        Example:
            >>> result = await client.purge_queue()
            >>> print(f"Removed {result['removed']} jobs")
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/purge-queue"
        
        async with self._session.post(url) as resp:
            resp.raise_for_status()
            return await resp.json()
    
    # ===========================
    # ENDPOINT MONITORING
    # ===========================
    
    async def get_endpoint_health(
        self,
        endpoint_id: Optional[str] = None,
    ) -> EndpointHealth:
        """
        Get endpoint health status (/health endpoint)
        
        Args:
            endpoint_id: Override default endpoint
        
        Returns:
            EndpointHealth with job and worker statistics
        
        Example:
            >>> health = await client.get_endpoint_health()
            >>> print(f"Queue: {health.jobs_in_queue}, Running: {health.jobs_in_progress}")
            >>> print(f"Workers: {health.workers_running} running, {health.workers_idle} idle")
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/health"
        
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        
        return EndpointHealth(
            jobs_completed=data["jobs"]["completed"],
            jobs_failed=data["jobs"]["failed"],
            jobs_in_progress=data["jobs"]["inProgress"],
            jobs_in_queue=data["jobs"]["inQueue"],
            jobs_retried=data["jobs"]["retried"],
            workers_idle=data["workers"]["idle"],
            workers_running=data["workers"]["running"],
        )
    
    # ===========================
    # STREAMING (ADVANCED)
    # ===========================
    
    async def stream_job_output(
        self,
        job_id: str,
        endpoint_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Stream incremental job output (/stream endpoint)
        
        Requires handler with return_aggregate_stream=True.
        
        Args:
            job_id: Job ID
            endpoint_id: Override default endpoint
        
        Returns:
            List of stream chunks with metrics and output
        
        Example:
            >>> chunks = await client.stream_job_output("job-id")
            >>> for chunk in chunks:
            ...     print(chunk["output"]["text"])
        """
        await self._ensure_session()
        
        url = f"{self._get_endpoint_url(endpoint_id)}/stream/{job_id}"
        
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()


# ===========================
# CONVENIENCE FUNCTIONS
# ===========================

async def submit_biodynamo_job(
    protein_pdb: str,
    simulation_time_ns: int = 100,
    temperature_k: float = 310.0,
    api_key: Optional[str] = None,
    endpoint_id: Optional[str] = None,
    wait_for_completion: bool = True,
) -> RunPodJob:
    """
    Submit BioDynamo simulation job to RunPod
    
    Convenience wrapper for BioDynamo-specific jobs.
    
    Args:
        protein_pdb: PDB ID or path
        simulation_time_ns: Simulation time in nanoseconds
        temperature_k: Temperature in Kelvin
        api_key: RunPod API key (default: $RUNPOD_API_KEY)
        endpoint_id: RunPod endpoint ID (default: $RUNPOD_ENDPOINT_ID)
        wait_for_completion: Poll until complete
    
    Returns:
        RunPodJob with results
    
    Example:
        >>> job = await submit_biodynamo_job(
        ...     protein_pdb="1ABC",
        ...     simulation_time_ns=100,
        ...     wait_for_completion=True
        ... )
        >>> print(job.output)
    """
    async with RunPodClient(api_key=api_key, endpoint_id=endpoint_id) as client:
        job = await client.submit_job({
            "protein_pdb": protein_pdb,
            "simulation_time_ns": simulation_time_ns,
            "temperature_k": temperature_k,
        })
        
        logger.info(f"Submitted BioDynamo job {job.id} for protein {protein_pdb}")
        
        if wait_for_completion:
            job = await client.poll_until_complete(job.id)
            logger.info(f"Job {job.id} completed with status {job.status}")
        
        return job


async def check_endpoint_status(
    api_key: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Quick health check for RunPod endpoint
    
    Args:
        api_key: RunPod API key
        endpoint_id: RunPod endpoint ID
    
    Returns:
        Dict with health status and recommendations
    
    Example:
        >>> status = await check_endpoint_status()
        >>> print(status["health_status"])
        >>> print(status["recommendations"])
    """
    async with RunPodClient(api_key=api_key, endpoint_id=endpoint_id) as client:
        health = await client.get_endpoint_health()
        
        # Calculate metrics
        total_jobs = health.jobs_completed + health.jobs_failed
        failure_rate = health.jobs_failed / total_jobs if total_jobs > 0 else 0.0
        queue_backlog = health.jobs_in_queue > health.workers_running * 10
        
        # Generate recommendations
        recommendations = []
        if failure_rate > 0.1:
            recommendations.append(f"High failure rate ({failure_rate:.1%}). Check logs.")
        if queue_backlog:
            recommendations.append("Queue backlog detected. Consider increasing max workers.")
        if health.workers_running == 0 and health.jobs_in_queue > 0:
            recommendations.append("No workers running but jobs queued. Check endpoint config.")
        
        return {
            "endpoint_id": client.endpoint_id,
            "health_status": "HEALTHY" if not recommendations else "NEEDS_ATTENTION",
            "jobs": {
                "completed": health.jobs_completed,
                "failed": health.jobs_failed,
                "in_progress": health.jobs_in_progress,
                "in_queue": health.jobs_in_queue,
                "failure_rate": f"{failure_rate:.1%}",
            },
            "workers": {
                "running": health.workers_running,
                "idle": health.workers_idle,
            },
            "recommendations": recommendations if recommendations else ["Endpoint healthy. No action needed."],
        }

"""
timescale_job_store.py - Timescale Job Persistence Layer

Persists JobManager jobs to Timescale 'jobs' table for durability.
Jobs survive process restarts and can be queried for analytics.

Schema (from migrate_timescale_direct.py):
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        worker_type TEXT NOT NULL,
        status TEXT NOT NULL,
        gpu_type TEXT,
        gpu_count INTEGER DEFAULT 1,
        provider TEXT,
        instance_id TEXT,
        docker_image TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        total_cost_usd DOUBLE PRECISION DEFAULT 0.0,
        error_message TEXT,
        checkpoint_path TEXT,
        metadata JSONB DEFAULT '{}'
    );

Author: Team 2 (Infra)
Date: 2025-01-20
"""

import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING, TypeAlias
from abc import ABC, abstractmethod

from .pg_async import choose_timescale_database_url, create_asyncpg_pool_for_database_url
from mica.serverless_models.execution_records import project_timescale_job_to_execution_record

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

if TYPE_CHECKING:
    import asyncpg as _asyncpg

    Pool: TypeAlias = _asyncpg.Pool
else:
    Pool: TypeAlias = Any


class JobStoreABC(ABC):
    """Abstract base for job persistence backends."""
    
    @abstractmethod
    async def save_job(self, job: Any) -> None:
        """Persist or update a job."""
        pass
    
    @abstractmethod
    async def load_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Load a job by ID."""
        pass
    
    @abstractmethod
    async def load_all_jobs(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load all jobs, optionally filtered by user."""
        pass
    
    @abstractmethod
    async def delete_job(self, job_id: str) -> bool:
        """Delete a job."""
        pass


class TimescaleJobStore(JobStoreABC):
    """
    Timescale-backed job persistence.
    
    Usage:
        store = TimescaleJobStore()
        await store.initialize()
        
        await store.save_job(job)
        job_data = await store.load_job("job-123")
    """
    
    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize with database URL.
        
        Args:
            database_url: Timescale connection string.
                         Defaults to TIMESCALE_SERVICE_URL / TIMESCALE_DSN / TIMESCALE_URL.
        """
        self.database_url = choose_timescale_database_url(database_url)
        self._pool: Optional[Pool] = None
    
    async def initialize(self) -> None:
        """Create connection pool."""
        if asyncpg is None:
            raise RuntimeError("asyncpg not installed - run: pip install asyncpg")
        
        if not self.database_url:
            raise RuntimeError("No Timescale database URL configured")
        
        self._pool = await create_asyncpg_pool_for_database_url(
            self.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
            timeout=20,
        )
    
    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
    
    async def save_job(self, job: Any) -> None:
        """
        Persist or update a job to Timescale.
        
        Uses upsert (INSERT ... ON CONFLICT UPDATE).
        """
        if not self._pool:
            await self.initialize()
        
        # Extract fields from Job dataclass
        spec = job.spec
        env_vars = spec.env_vars if isinstance(spec.env_vars, dict) else {}
        metadata = {
            "priority": spec.priority.value if hasattr(spec.priority, 'value') else spec.priority,
            "max_price_per_hour": spec.max_price_per_hour,
            "max_total_cost_usd": spec.max_total_cost_usd,
            "prefer_spot": spec.prefer_spot,
            "checkpoint_interval_minutes": spec.checkpoint_interval_minutes,
            "env_var_names": sorted(str(key) for key in env_vars.keys()),
            "env_vars_redacted": bool(env_vars),
            "input_data_path": spec.input_data_path,
            "output_data_path": spec.output_data_path,
        }
        
        async with self._pool.acquire() as conn:
            # Use checkpoint_gcs_path instead of checkpoint_path (table has checkpoint_gcs_path)
            checkpoint_path = spec.checkpoint_gcs_path if hasattr(spec, 'checkpoint_gcs_path') else None
            
            await conn.execute(
                """
                INSERT INTO jobs (
                    job_id, user_id, worker_type, status,
                    gpu_type, gpu_count, provider, instance_id,
                    docker_image, created_at, started_at, completed_at,
                    total_cost_usd, error_message, checkpoint_gcs_path, metadata
                ) VALUES (
                    $1::VARCHAR, $2, $3, $4,
                    $5, $6, $7, $8,
                    $9, $10, $11, $12,
                    $13, $14, $15, $16
                )
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    provider = EXCLUDED.provider,
                    instance_id = EXCLUDED.instance_id,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    total_cost_usd = EXCLUDED.total_cost_usd,
                    error_message = EXCLUDED.error_message,
                    checkpoint_gcs_path = EXCLUDED.checkpoint_gcs_path,
                    metadata = EXCLUDED.metadata
                """,
                spec.job_id,  # Use job_id as application_name (unique identifier)
                (spec.user_id or "").strip() or (spec.metadata.get("user_id") if isinstance(spec.metadata, dict) else None) or "system",
                spec.worker_type,
                job.status.value if hasattr(job.status, 'value') else str(job.status),
                spec.gpu_type,
                spec.gpu_count,
                job.provider,
                job.instance_id,
                spec.docker_image,
                job.created_at,
                job.started_at,
                job.completed_at,
                job.total_cost_usd,
                job.error_message,
                checkpoint_path,
                json.dumps(metadata),
            )
    
    async def load_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Load a job by ID."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM jobs WHERE job_id = $1::VARCHAR",
                job_id
            )
            
            if row is None:
                return None
            
            return dict(row)
    
    async def load_all_jobs(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load all jobs, optionally filtered by user."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            if user_id:
                rows = await conn.fetch(
                    "SELECT * FROM jobs WHERE user_id = $1 ORDER BY created_at DESC",
                    user_id
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1000"
                )
            
            return [dict(row) for row in rows]
    
    async def load_active_jobs(self) -> List[Dict[str, Any]]:
        """Load jobs that are not in terminal state (for recovery)."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM jobs 
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                ORDER BY created_at ASC
                """
            )
            
            return [dict(row) for row in rows]
    
    async def delete_job(self, job_id: str) -> bool:
        """Delete a job."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM jobs WHERE job_id = $1::VARCHAR",
                job_id
            )
            return "DELETE 1" in result

    def project_job_row_to_execution_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return project_timescale_job_to_execution_record(row)

    async def load_job_execution_record(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = await self.load_job(job_id)
        if row is None:
            return None
        return self.project_job_row_to_execution_record(row)
    
    async def get_cost_summary(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get cost summary for analytics."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            if user_id:
                row = await conn.fetchrow(
                    """
                    SELECT 
                        COUNT(*) as total_jobs,
                        SUM(total_cost_usd) as total_cost,
                        COUNT(*) FILTER (WHERE status = 'completed') as completed,
                        COUNT(*) FILTER (WHERE status = 'failed') as failed
                    FROM jobs WHERE user_id = $1
                    """,
                    user_id
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT 
                        COUNT(*) as total_jobs,
                        SUM(total_cost_usd) as total_cost,
                        COUNT(*) FILTER (WHERE status = 'completed') as completed,
                        COUNT(*) FILTER (WHERE status = 'failed') as failed
                    FROM jobs
                    """
                )
            
            return dict(row) if row else {}


class InMemoryJobStore(JobStoreABC):
    """In-memory job store for testing."""
    
    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
    
    async def save_job(self, job: Any) -> None:
        self._jobs[job.spec.job_id] = job.to_dict()
    
    async def load_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._jobs.get(job_id)
    
    async def load_all_jobs(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        jobs = list(self._jobs.values())
        if user_id:
            jobs = [j for j in jobs if j.get("user_id") == user_id]
        return jobs
    
    async def delete_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            return True
        return False

    def project_job_row_to_execution_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return project_timescale_job_to_execution_record(row)

"""
timescale_event_store.py - Timescale Event Persistence Layer

Implements EventStoreABC using Timescale 'events' hypertable for:
- Durable event sourcing
- Time-series queries on infrastructure events
- Audit trail with automatic retention

Schema (from migrate_timescale_direct.py):
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        job_id TEXT,
        instance_id TEXT,
        provider TEXT,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        sequence_id BIGINT,
        payload JSONB DEFAULT '{}',
        metadata JSONB DEFAULT '{}'
    );
    SELECT create_hypertable('events', 'timestamp', if_not_exists => TRUE);

Author: Team 2 (Infra)
Date: 2025-01-20
"""

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from .pg_async import choose_timescale_database_url, create_asyncpg_pool_for_database_url

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

if TYPE_CHECKING:
    from .event_store import (
        EventStoreABC,
        EventFilter,
        InfrastructureEvent,
        InfrastructureState,
        EventType,
    )


class TimescaleEventStore:
    """
    Timescale-backed event store implementing EventStoreABC pattern.
    
    Stores infrastructure events in Timescale hypertable for:
    - Time-series analysis
    - Audit compliance  
    - State reconstruction
    
    Usage:
        store = TimescaleEventStore()
        await store.initialize()
        
        seq_id = await store.append(JobRequestedEvent(...))
        events = await store.search_events(EventFilter(job_id="abc"))
    """
    
    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize with database URL.
        
        Args:
            database_url: Timescale connection string.
                         Defaults to TIMESCALE_SERVICE_URL / TIMESCALE_DSN / TIMESCALE_URL.
        """
        self.database_url = choose_timescale_database_url(database_url)
        self._pool: Optional["asyncpg.Pool"] = None
        self._sequence_id = 0
        self._events_columns: Optional[set[str]] = None
    
    async def initialize(self) -> None:
        """Create connection pool and cache schema info."""
        if asyncpg is None:
            raise RuntimeError("asyncpg not installed - run: pip install asyncpg")
        
        if not self.database_url:
            raise RuntimeError("No Timescale database URL configured")
        
        pool_kwargs: Dict[str, Any] = {
            "min_size": 1,
            "max_size": 5,
            "command_timeout": 30,
            "timeout": 20,
        }
        self._pool = await create_asyncpg_pool_for_database_url(self.database_url, **pool_kwargs)

        async with self._pool.acquire() as conn:
            cols = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'events'
                """
            )
            self._events_columns = {r["column_name"] for r in cols}

            if "sequence_id" in self._events_columns:
                row = await conn.fetchrow("SELECT COALESCE(MAX(sequence_id), 0) as max_seq FROM events")
                self._sequence_id = (row["max_seq"] or 0) + 1
            else:
                self._sequence_id = 1
    
    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
    
    async def append(self, event: "InfrastructureEvent") -> int:
        """
        Append an event to Timescale.
        
        Args:
            event: Event to persist
            
        Returns:
            sequence_id assigned
        """
        if not self._pool:
            await self.initialize()
        
        # Assign sequence_id if available
        if not self._events_columns:
            # Defensive: initialize() should populate this.
            await self.initialize()

        if self._events_columns and "sequence_id" in self._events_columns:
            event.sequence_id = self._sequence_id
            self._sequence_id += 1
        else:
            event.sequence_id = 0
        
        # Build data (all event-specific fields)
        data = event.to_dict()
        # Remove fields that have dedicated columns (best-effort; schema differs across migrations)
        for field in [
            "event_id",
            "event_type",
            "job_id",
            "instance_id",
            "provider",
            "timestamp",
            "sequence_id",
            "metadata",
            "user_id",
            "session_id",
            "bucket",
        ]:
            data.pop(field, None)

        user_id = getattr(event, "user_id", None) or (event.metadata or {}).get("user_id")
        session_id = getattr(event, "session_id", None) or (event.metadata or {}).get("session_id")
        bucket = getattr(event, "bucket", None) or (event.metadata or {}).get("bucket")
        if bucket is not None and "bucket" not in (event.metadata or {}):
            # Keep bucket discoverable even if there is no dedicated column.
            event.metadata["bucket"] = bucket
        if user_id is not None and "user_id" not in (event.metadata or {}):
            event.metadata["user_id"] = user_id
        if session_id is not None and "session_id" not in (event.metadata or {}):
            event.metadata["session_id"] = session_id
        
        # Build an INSERT compatible with the detected schema.
        # Prefer "data" (newer schema) and fall back to "payload" (older schema).
        cols_to_values: Dict[str, Any] = {
            "event_id": event.event_id,
            "event_type": event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
            "timestamp": datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if isinstance(event.timestamp, str)
            else event.timestamp,
            "job_id": event.job_id,
            "instance_id": event.instance_id,
            "provider": event.provider,
            "sequence_id": event.sequence_id,
            "metadata": json.dumps(event.metadata),
            "node_id": "mica_api",
            "user_id": user_id,
            "session_id": session_id,
        }

        if self._events_columns and "data" in self._events_columns:
            cols_to_values["data"] = json.dumps(data)
        elif self._events_columns and "payload" in self._events_columns:
            cols_to_values["payload"] = json.dumps(data)

        insert_cols = [c for c in cols_to_values.keys() if self._events_columns and c in self._events_columns]
        insert_vals = [cols_to_values[c] for c in insert_cols]
        placeholders = ", ".join(f"${i}" for i in range(1, len(insert_cols) + 1))
        cols_sql = ", ".join(insert_cols)

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO events ({cols_sql}) VALUES ({placeholders})",
                *insert_vals,
            )
        
        return event.sequence_id
    
    async def get_event(self, sequence_id: int) -> Optional[Dict[str, Any]]:
        """Get event by sequence_id."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM events WHERE sequence_id = $1",
                sequence_id
            )
            
            if row is None:
                return None
            
            return self._row_to_dict(row)
    
    async def search_events(
        self,
        filter: Optional["EventFilter"] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Search events with filter.
        
        Args:
            filter: EventFilter with query criteria
            limit: Max events to return
            
        Returns:
            List of matching events
        """
        if not self._pool:
            await self.initialize()
        
        # Build WHERE clause
        conditions = []
        params = []
        param_idx = 1
        
        if filter:
            if filter.event_types:
                type_values = [et.value if hasattr(et, 'value') else str(et) for et in filter.event_types]
                conditions.append(f"event_type = ANY(${param_idx}::text[])")
                params.append(type_values)
                param_idx += 1
            
            if filter.job_id:
                conditions.append(f"job_id = ${param_idx}")
                params.append(filter.job_id)
                param_idx += 1
            
            if filter.instance_id:
                conditions.append(f"instance_id = ${param_idx}")
                params.append(filter.instance_id)
                param_idx += 1
            
            if filter.provider:
                conditions.append(f"provider = ${param_idx}")
                params.append(filter.provider)
                param_idx += 1

            if getattr(filter, "user_id", None) and self._events_columns and "user_id" in self._events_columns:
                conditions.append(f"user_id = ${param_idx}")
                params.append(getattr(filter, "user_id"))
                param_idx += 1

            if getattr(filter, "session_id", None) and self._events_columns and "session_id" in self._events_columns:
                conditions.append(f"session_id = ${param_idx}")
                params.append(getattr(filter, "session_id"))
                param_idx += 1
            
            if filter.since:
                conditions.append(f"timestamp >= ${param_idx}")
                params.append(filter.since)
                param_idx += 1
            
            if filter.until:
                conditions.append(f"timestamp <= ${param_idx}")
                params.append(filter.until)
                param_idx += 1
            
            if filter.min_sequence_id is not None:
                conditions.append(f"sequence_id >= ${param_idx}")
                params.append(filter.min_sequence_id)
                param_idx += 1
            
            if filter.max_sequence_id is not None:
                conditions.append(f"sequence_id <= ${param_idx}")
                params.append(filter.max_sequence_id)
                param_idx += 1
        
        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        
        order_by = "sequence_id ASC" if (self._events_columns and "sequence_id" in self._events_columns) else "timestamp ASC"

        query = f"""
            SELECT * FROM events
            WHERE {where_clause}
            ORDER BY {order_by}
            LIMIT {limit}
        """
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]
    
    async def get_latest_sequence_id(self) -> int:
        """Get the latest sequence_id."""
        if not self._pool:
            await self.initialize()

        if not self._events_columns or "sequence_id" not in self._events_columns:
            return 0
        
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COALESCE(MAX(sequence_id), 0) as max_seq FROM events")
            return row["max_seq"] or 0
    
    async def get_events_for_job(self, job_id: str) -> List[Dict[str, Any]]:
        """Get all events for a job."""
        if not self._pool:
            await self.initialize()

        order_by = "sequence_id ASC" if (self._events_columns and "sequence_id" in self._events_columns) else "timestamp ASC"
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM events WHERE job_id = $1 ORDER BY {order_by}",
                job_id
            )
            return [self._row_to_dict(row) for row in rows]
    
    async def get_events_for_instance(self, instance_id: str) -> List[Dict[str, Any]]:
        """Get all events for an instance."""
        if not self._pool:
            await self.initialize()

        order_by = "sequence_id ASC" if (self._events_columns and "sequence_id" in self._events_columns) else "timestamp ASC"
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM events WHERE instance_id = $1 ORDER BY {order_by}",
                instance_id
            )
            return [self._row_to_dict(row) for row in rows]
    
    async def get_cost_events(
        self,
        job_id: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get cost-related events for analytics."""
        if not self._pool:
            await self.initialize()
        
        conditions = ["event_type = 'cost_incurred'"]
        params = []
        param_idx = 1
        
        if job_id:
            conditions.append(f"job_id = ${param_idx}")
            params.append(job_id)
            param_idx += 1
        
        if since:
            conditions.append(f"timestamp >= ${param_idx}")
            params.append(since)
            param_idx += 1
        
        query = f"""
            SELECT * FROM events
            WHERE {" AND ".join(conditions)}
            ORDER BY timestamp ASC
        """
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]
    
    async def get_summary(self, since: Optional[datetime] = None) -> Dict[str, Any]:
        """Get event summary for dashboard."""
        if not self._pool:
            await self.initialize()
        
        since_clause = ""
        params = []
        if since:
            since_clause = "WHERE timestamp >= $1"
            params.append(since)
        
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT 
                    COUNT(*) as total_events,
                    COUNT(DISTINCT job_id) as unique_jobs,
                    COUNT(DISTINCT instance_id) as unique_instances,
                    MIN(timestamp) as first_event,
                    MAX(timestamp) as last_event
                FROM events
                {since_clause}
                """,
                *params
            )
            
            # Count by event type
            type_counts = await conn.fetch(
                f"""
                SELECT event_type, COUNT(*) as count
                FROM events
                {since_clause}
                GROUP BY event_type
                ORDER BY count DESC
                """,
                *params
            )
            
            return {
                "total_events": row["total_events"],
                "unique_jobs": row["unique_jobs"],
                "unique_instances": row["unique_instances"],
                "first_event": row["first_event"].isoformat() if row["first_event"] else None,
                "last_event": row["last_event"].isoformat() if row["last_event"] else None,
                "by_type": {r["event_type"]: r["count"] for r in type_counts},
            }

    async def summarize_instance_usage(
        self,
        *,
        user_id: str,
        session_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Compute per-instance runtime and cost summaries for a user/session.

        This is built on top of the Timescale `events` table and expects that
        provisioning/termination events are being written with user/session attribution.
        """

        from mica.infrastructure.event_store import EventFilter, EventType
        from mica.infrastructure.analytics.instance_attribution import (
            summarize_instance_usage_from_events,
            summarize_usage_totals,
        )

        filt = EventFilter(
            event_types=[
                EventType.PROVISIONING_SUCCEEDED,
                EventType.INSTANCE_TERMINATED,
                EventType.COST_INCURRED,
            ],
            user_id=user_id,
            session_id=session_id,
            since=since,
            until=until,
        )

        rows = await self.search_events(filt, limit=limit)

        # Double-filter on resolved attribution (metadata fallback), then aggregate.
        usages = summarize_instance_usage_from_events(
            rows,
            require_user_id=user_id,
            require_session_id=session_id,
        )

        def _ser_dt(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if isinstance(dt, datetime) else None

        instances = [
            {
                "instance_id": u.instance_id,
                "provider": u.provider,
                "user_id": u.user_id,
                "session_id": u.session_id,
                "bucket": u.bucket,
                "started_at": _ser_dt(u.started_at),
                "ended_at": _ser_dt(u.ended_at),
                "uptime_seconds": u.uptime_seconds,
                "price_per_hour": u.price_per_hour,
                "estimated_cost_usd": u.estimated_cost_usd,
            }
            for u in usages
        ]

        totals = summarize_usage_totals(usages)
        return {
            "user_id": user_id,
            "session_id": session_id,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "totals": totals,
            "instances": instances,
        }
    
    def _row_to_dict(self, row: "asyncpg.Record") -> Dict[str, Any]:
        """Convert database row to dict."""
        result = dict(row)
        
        # Parse JSONB fields (data vs payload depends on migration)
        if "data" in result and isinstance(result["data"], str):
            result["data"] = json.loads(result["data"])
        if "payload" in result and isinstance(result["payload"], str):
            result["payload"] = json.loads(result["payload"])
        if "metadata" in result and isinstance(result["metadata"], str):
            result["metadata"] = json.loads(result["metadata"])
        
        # Convert timestamp to ISO string
        if "timestamp" in result and result["timestamp"]:
            result["timestamp"] = result["timestamp"].isoformat()
        
        return result

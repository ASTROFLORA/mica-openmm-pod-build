"""
session_repository.py - Session Persistence Layer

Persists AgenticDriver conversation sessions to Neon for:
- Conversation history survival across restarts
- Session resumption with context
- Multi-user session management
- Analytics and audit trail

Schema:
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        mode TEXT DEFAULT 'production',
        conversation_history JSONB DEFAULT '[]',
        metadata JSONB DEFAULT '{}',
        is_active BOOLEAN DEFAULT TRUE
    );

Author: Team 2 (Infra)
Date: 2025-01-21
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

from .pg_async import asyncpg_connection_kwargs_for_database_url, choose_neon_database_url, connect_asyncpg_for_database_url

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore


logger = logging.getLogger(__name__)

_REQUIRED_SESSION_COLUMNS = {
    "session_id",
    "user_id",
    "created_at",
    "updated_at",
    "mode",
    "conversation_history",
    "metadata",
    "is_active",
}
_LEGACY_SESSION_COLUMNS = {
    "id",
    "workspace_id",
    "session_token",
    "started_at",
    "last_activity_at",
    "ended_at",
    "context",
}


async def _inspect_sessions_table_contract(conn: "asyncpg.Connection") -> Dict[str, Any]:
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'sessions'
        )
        """
    )
    if not exists:
        return {
            "status": "missing_table",
            "table_exists": False,
            "missing_columns": sorted(_REQUIRED_SESSION_COLUMNS),
            "legacy_columns": [],
            "session_id_unique": False,
        }

    cols = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'sessions'
        ORDER BY ordinal_position
        """
    )
    column_names = {row["column_name"] for row in cols}
    idx = await conn.fetch(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'sessions'
        ORDER BY indexname
        """
    )
    session_id_unique = any("(session_id)" in row["indexdef"] and "UNIQUE INDEX" in row["indexdef"] for row in idx)
    missing_columns = sorted(_REQUIRED_SESSION_COLUMNS - column_names)
    legacy_columns = sorted(column_names & _LEGACY_SESSION_COLUMNS)
    if missing_columns or not session_id_unique:
        status = "legacy_or_incomplete"
    else:
        status = "ok"
    return {
        "status": status,
        "table_exists": True,
        "missing_columns": missing_columns,
        "legacy_columns": legacy_columns,
        "session_id_unique": session_id_unique,
    }


async def inspect_neon_sessions_table_contract(database_url: Optional[str] = None) -> Dict[str, Any]:
    resolved = choose_neon_database_url(database_url)
    if not resolved:
        return {"status": "not_configured", "table_exists": False}
    if asyncpg is None:
        return {"status": "asyncpg_missing", "table_exists": False}

    conn = None
    try:
        conn = await connect_asyncpg_for_database_url(resolved, timeout=10)
        result = await _inspect_sessions_table_contract(conn)
        result["database_url_configured"] = True
        return result
    finally:
        if conn is not None:
            await conn.close()


class SessionRepositoryABC(ABC):
    """Abstract base for session persistence backends."""
    
    @abstractmethod
    async def save_session(
        self,
        session_id: str,
        user_id: str,
        conversation_history: List[Dict[str, Any]],
        mode: str = "production",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Save or update a session."""
        pass
    
    @abstractmethod
    async def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load a session by ID."""
        pass
    
    @abstractmethod
    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a message to session history."""
        pass
    
    @abstractmethod
    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by user."""
        pass
    
    @abstractmethod
    async def close_session(self, session_id: str) -> bool:
        """Mark a session as inactive."""
        pass


class NeonSessionRepository(SessionRepositoryABC):
    """
    Neon-backed session persistence.
    
    Usage:
        repo = NeonSessionRepository()
        await repo.initialize()
        
        await repo.save_session(
            session_id="sess-123",
            user_id="user_abc",
            conversation_history=[{"role": "user", "content": "Hello"}],
        )
        
        session = await repo.load_session("sess-123")
    """
    
    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize with database URL.
        
        Args:
            database_url: Neon connection string.
                         Defaults to NEON_DATABASE_URL.
        """
        self.database_url = choose_neon_database_url(database_url)
        self._pool: Optional["asyncpg.Pool"] = None
        self._schema_status: Optional[Dict[str, Any]] = None
    
    async def initialize(self) -> None:
        """Create connection pool and ensure table exists."""
        if asyncpg is None:
            raise RuntimeError("asyncpg not installed - run: pip install asyncpg")
        
        if not self.database_url:
            raise RuntimeError("No Neon database URL configured")

        pool_kwargs: Dict[str, Any] = {
            "min_size": 1,
            "max_size": 5,
            "command_timeout": 30,
            "timeout": 20,
        }
        pool_kwargs.update(asyncpg_connection_kwargs_for_database_url(self.database_url))

        self._pool = await asyncpg.create_pool(**pool_kwargs)
        
        # Ensure table exists
        async with self._pool.acquire() as conn:
            before = await _inspect_sessions_table_contract(conn)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    mode TEXT DEFAULT 'production',
                    conversation_history JSONB DEFAULT '[]',
                    metadata JSONB DEFAULT '{}',
                    is_active BOOLEAN DEFAULT TRUE
                )
            """)

            # Repair legacy deployments where `sessions` existed before the
            # current runtime columns were added.
            await conn.execute("""
                ALTER TABLE sessions
                    ADD COLUMN IF NOT EXISTS session_id TEXT,
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW(),
                    ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'production',
                    ADD COLUMN IF NOT EXISTS conversation_history JSONB DEFAULT '[]',
                    ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}',
                    ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE
            """)

            repair_sources = ["session_id"]
            legacy_columns = set(before.get("legacy_columns") or [])
            if "session_token" in legacy_columns:
                repair_sources.append("session_token")
            if "id" in legacy_columns:
                repair_sources.append("id::text")
            if len(repair_sources) > 1:
                await conn.execute(
                    f"""
                    UPDATE sessions
                    SET session_id = COALESCE({', '.join(repair_sources)})
                    WHERE session_id IS NULL
                    """
                )
            
            # Create indexes
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_active);
                CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
            """)
            after = await _inspect_sessions_table_contract(conn)
            self._schema_status = after
            if before.get("status") != "ok":
                logger.warning(
                    "Repaired legacy/incomplete Neon sessions schema: before=%s after=%s",
                    before,
                    after,
                )
            elif after.get("legacy_columns"):
                logger.warning(
                    "Neon sessions table still contains legacy columns after contract validation: %s",
                    after.get("legacy_columns"),
                )

    async def inspect_schema_status(self) -> Dict[str, Any]:
        if self._schema_status is not None:
            return dict(self._schema_status)
        return await inspect_neon_sessions_table_contract(self.database_url)
    
    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
    
    async def save_session(
        self,
        session_id: str,
        user_id: str,
        conversation_history: List[Dict[str, Any]],
        mode: str = "production",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Save or update a session."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (
                    session_id, user_id, mode, conversation_history, metadata, updated_at
                ) VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    conversation_history = EXCLUDED.conversation_history,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                session_id,
                user_id,
                mode,
                json.dumps(conversation_history),
                json.dumps(metadata or {}),
            )

    async def upsert_session_metadata(
        self,
        *,
        session_id: str,
        user_id: str,
        metadata: Optional[Dict[str, Any]],
        mode: str = "production",
    ) -> None:
        """Persist session metadata without clobbering conversation history."""
        if not self._pool:
            await self.initialize()

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO sessions (
                    session_id, user_id, mode, conversation_history, metadata, updated_at
                ) VALUES ($1, $2, $3, '[]'::jsonb, $4, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    mode = EXCLUDED.mode,
                    metadata = COALESCE(sessions.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                    updated_at = NOW()
                WHERE sessions.user_id = EXCLUDED.user_id
                """,
                session_id,
                user_id,
                mode,
                json.dumps(metadata or {}, default=str),
            )
            if result == "INSERT 0 0":
                raise PermissionError(
                    f"Session {session_id} belongs to a different user and cannot be updated"
                )
    
    async def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load a session by ID."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE session_id = $1",
                session_id
            )
            
            if row is None:
                return None
            
            result = dict(row)
            # Parse JSONB fields
            if isinstance(result.get("conversation_history"), str):
                result["conversation_history"] = json.loads(result["conversation_history"])
            if isinstance(result.get("metadata"), str):
                result["metadata"] = json.loads(result["metadata"])
            
            return result
    
    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a message to session history."""
        if not self._pool:
            await self.initialize()
        
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if metadata:
            message["metadata"] = metadata
        
        async with self._pool.acquire() as conn:
            # Append to JSONB array
            await conn.execute(
                """
                UPDATE sessions 
                SET conversation_history = conversation_history || $2::jsonb,
                    updated_at = NOW()
                WHERE session_id = $1
                """,
                session_id,
                json.dumps([message]),
            )
    
    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by user."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            conditions = []
            params = []
            param_idx = 1
            
            if user_id:
                conditions.append(f"user_id = ${param_idx}")
                params.append(user_id)
                param_idx += 1
            
            if active_only:
                conditions.append("is_active = TRUE")
            
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            
            query = f"""
                SELECT session_id, user_id, created_at, updated_at, mode, is_active,
                       jsonb_array_length(conversation_history) as message_count
                FROM sessions
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT {limit}
            """
            
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    async def close_session(self, session_id: str) -> bool:
        """Mark a session as inactive."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE sessions SET is_active = FALSE, updated_at = NOW() WHERE session_id = $1",
                session_id
            )
            return "UPDATE 1" in result
    
    async def get_conversation_summary(self, session_id: str) -> Dict[str, Any]:
        """Get summary stats for a session."""
        if not self._pool:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 
                    session_id,
                    jsonb_array_length(conversation_history) as message_count,
                    created_at,
                    updated_at,
                    EXTRACT(EPOCH FROM (updated_at - created_at)) as duration_seconds
                FROM sessions
                WHERE session_id = $1
                """,
                session_id
            )
            
            if row is None:
                return {}
            
            return dict(row)


class InMemorySessionRepository(SessionRepositoryABC):
    """In-memory session repository for testing."""
    
    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}
    
    async def save_session(
        self,
        session_id: str,
        user_id: str,
        conversation_history: List[Dict[str, Any]],
        mode: str = "production",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._sessions[session_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "conversation_history": conversation_history,
            "mode": mode,
            "metadata": metadata or {},
            "is_active": True,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

    async def upsert_session_metadata(
        self,
        *,
        session_id: str,
        user_id: str,
        metadata: Optional[Dict[str, Any]],
        mode: str = "production",
    ) -> None:
        existing = self._sessions.get(session_id)
        if existing is not None and existing.get("user_id") != user_id:
            raise PermissionError(
                f"Session {session_id} belongs to a different user and cannot be updated"
            )

        payload = existing or {
            "session_id": session_id,
            "user_id": user_id,
            "conversation_history": [],
            "created_at": datetime.utcnow(),
            "is_active": True,
        }
        payload["user_id"] = user_id
        payload["mode"] = mode
        payload["metadata"] = {**(payload.get("metadata") or {}), **(metadata or {})}
        payload["updated_at"] = datetime.utcnow()
        self._sessions[session_id] = payload
    
    async def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)
    
    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if session_id in self._sessions:
            message = {
                "role": role,
                "content": content,
                "timestamp": datetime.utcnow().isoformat(),
            }
            if metadata:
                message["metadata"] = metadata
            self._sessions[session_id]["conversation_history"].append(message)
            self._sessions[session_id]["updated_at"] = datetime.utcnow()
    
    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sessions = list(self._sessions.values())
        if user_id:
            sessions = [s for s in sessions if s["user_id"] == user_id]
        if active_only:
            sessions = [s for s in sessions if s["is_active"]]
        return sessions[:limit]
    
    async def close_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            self._sessions[session_id]["is_active"] = False
            return True
        return False

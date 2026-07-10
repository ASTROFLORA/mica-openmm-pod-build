"""
mcp_session_manager.py — Pre-flight MCP session registry.

Anti-rigidity rule R-07: No implicit session init.
Every MCP server connection is explicitly registered, tracked with TTL,
and must be closed via close_all() at agent teardown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionStatus(str, Enum):
    PENDING = "PENDING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    CLOSED = "CLOSED"


@dataclass
class MCPSession:
    server_id: str
    status: SessionStatus = SessionStatus.PENDING
    opened_at: Optional[datetime] = None
    last_active: Optional[datetime] = None
    ttl_seconds: int = 300
    error_detail: str = ""
    metadata: dict = field(default_factory=dict)

    def is_expired(self) -> bool:
        """Return True if last_active + ttl_seconds is in the past."""
        if self.last_active is None:
            return False
        now = datetime.now(timezone.utc)
        last = self.last_active
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        return elapsed > self.ttl_seconds

    def touch(self) -> None:
        """Update last_active to now (UTC)."""
        self.last_active = datetime.now(timezone.utc)

    def mark_connected(self) -> None:
        """Set status CONNECTED, record opened_at, touch."""
        self.status = SessionStatus.CONNECTED
        self.opened_at = datetime.now(timezone.utc)
        self.touch()

    def mark_degraded(self, reason: str) -> None:
        """Set status DEGRADED, store reason."""
        self.status = SessionStatus.DEGRADED
        self.error_detail = reason

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "status": self.status.value,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "last_active": self.last_active.isoformat() if self.last_active else None,
            "ttl_seconds": self.ttl_seconds,
            "error_detail": self.error_detail,
            "metadata": self.metadata,
        }


class MCPSessionManager:
    """Registry for all MCP server sessions in a single agent run."""

    def __init__(self, default_ttl: int = 300) -> None:
        self._sessions: Dict[str, MCPSession] = {}
        self._default_ttl: int = default_ttl

    def register(self, server_id: str, ttl_seconds: Optional[int] = None) -> MCPSession:
        """Create and store a PENDING session, or return the existing one if not CLOSED."""
        existing = self._sessions.get(server_id)
        if existing is not None and existing.status != SessionStatus.CLOSED:
            return existing
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        session = MCPSession(server_id=server_id, ttl_seconds=ttl)
        self._sessions[server_id] = session
        logger.debug("MCPSessionManager: registered session for %s", server_id)
        return session

    def get(self, server_id: str) -> Optional[MCPSession]:
        """Return session by ID or None."""
        return self._sessions.get(server_id)

    def mark_connected(self, server_id: str) -> MCPSession:
        """Mark session as CONNECTED. Raises KeyError if not registered."""
        session = self._sessions.get(server_id)
        if session is None:
            raise KeyError(f"Server '{server_id}' is not registered.")
        session.mark_connected()
        logger.info("MCPSessionManager: %s CONNECTED", server_id)
        return session

    def mark_degraded(self, server_id: str, reason: str) -> MCPSession:
        """Mark session as DEGRADED with a reason."""
        session = self._sessions.get(server_id)
        if session is None:
            raise KeyError(f"Server '{server_id}' is not registered.")
        session.mark_degraded(reason)
        logger.warning("MCPSessionManager: %s DEGRADED — %s", server_id, reason)
        return session

    def touch(self, server_id: str) -> None:
        """Update last_active for a session."""
        session = self._sessions.get(server_id)
        if session is not None:
            session.touch()

    def expire_stale(self) -> List[str]:
        """Close all expired sessions and return their server IDs."""
        expired_ids: List[str] = []
        for sid, session in self._sessions.items():
            if session.status not in (SessionStatus.CLOSED,) and session.is_expired():
                session.status = SessionStatus.CLOSED
                expired_ids.append(sid)
                logger.info("MCPSessionManager: expired stale session %s", sid)
        return expired_ids

    def close(self, server_id: str) -> None:
        """Set session status to CLOSED."""
        session = self._sessions.get(server_id)
        if session is not None:
            session.status = SessionStatus.CLOSED
            logger.info("MCPSessionManager: closed session %s", server_id)

    def close_all(self) -> int:
        """Close all non-CLOSED sessions. Returns count of sessions closed."""
        count = 0
        for session in self._sessions.values():
            if session.status != SessionStatus.CLOSED:
                session.status = SessionStatus.CLOSED
                count += 1
        logger.info("MCPSessionManager: close_all closed %d session(s)", count)
        return count

    def connected_count(self) -> int:
        """Return number of CONNECTED sessions."""
        return sum(
            1 for s in self._sessions.values() if s.status == SessionStatus.CONNECTED
        )

    def degraded_servers(self) -> List[str]:
        """Return list of server_ids with DEGRADED status."""
        return [
            sid
            for sid, s in self._sessions.items()
            if s.status == SessionStatus.DEGRADED
        ]

    def all_sessions(self) -> Dict[str, MCPSession]:
        """Return a shallow copy of the sessions dict."""
        return dict(self._sessions)


class SessionPreflightError(Exception):
    """Raised by preflight_check when required servers are not available."""

    def __init__(
        self,
        failed_servers: List[str],
        degraded_servers: List[str],
    ) -> None:
        self.failed_servers = failed_servers
        self.degraded_servers = degraded_servers
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = []
        if self.failed_servers:
            parts.append(f"Missing/closed servers: {self.failed_servers}")
        if self.degraded_servers:
            parts.append(f"Degraded servers (warning): {self.degraded_servers}")
        return "SessionPreflightError — " + "; ".join(parts)


def preflight_check(
    manager: MCPSessionManager,
    required_servers: List[str],
) -> None:
    """
    Validate that all required servers are registered and not CLOSED/expired.

    - Expired sessions are closed first via expire_stale().
    - DEGRADED servers emit warnings but do NOT raise.
    - Absent or CLOSED servers raise SessionPreflightError.
    """
    manager.expire_stale()

    failed: List[str] = []
    degraded: List[str] = []

    for server_id in required_servers:
        session = manager.get(server_id)
        if session is None or session.status == SessionStatus.CLOSED:
            failed.append(server_id)
        elif session.status == SessionStatus.DEGRADED:
            degraded.append(server_id)
            logger.warning("preflight_check: server %s is DEGRADED", server_id)

    if failed:
        raise SessionPreflightError(
            failed_servers=failed,
            degraded_servers=degraded,
        )

"""MICA Infrastructure — event sourcing, session management, orchestration."""
from .event_log import (
    CrashResumeMarker,
    EventLog,
    EventLogEntry,
    EventLogStats,
    RunPlan,
)
from .mcp_session_manager import (
    MCPSession,
    MCPSessionManager,
    SessionPreflightError,
    SessionStatus,
)

__all__ = [
    # event log (P1-03)
    "CrashResumeMarker",
    "EventLog",
    "EventLogEntry",
    "EventLogStats",
    "RunPlan",
    # MCP session manager (P0-02)
    "MCPSession",
    "MCPSessionManager",
    "SessionPreflightError",
    "SessionStatus",
]

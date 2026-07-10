"""
MCP Policy Snapshot — S0.4

Captures the full MCP policy state at the start of each run so that
post-mortem auditing can answer: "which servers were reachable, which
tools were available / blocked, and what were the circuit-breaker states
when this run started?"

The snapshot is **read-only after creation** — purely observational.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# -------------------------------------------------------------------
# Per-server snapshot
# -------------------------------------------------------------------

@dataclass(frozen=True)
class MCPServerSnapshot:
    """Point-in-time state of a single MCP server."""

    server_name: str
    status: str = "unknown"  # "connected" | "disconnected" | "error" | "unknown"
    tools_available: tuple = ()  # tuple for frozen compat
    tools_blocked: tuple = ()
    block_reasons: Dict[str, str] = field(default_factory=dict)
    circuit_state: str = "closed"  # "closed" | "open"
    consecutive_failures: int = 0
    timeout_s: float = 30.0
    capabilities: Dict[str, Any] = field(default_factory=dict)

    # Convenience ----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert tuples back to lists for JSON serialisation
        d["tools_available"] = list(self.tools_available)
        d["tools_blocked"] = list(self.tools_blocked)
        return d


# -------------------------------------------------------------------
# Full run-scoped snapshot
# -------------------------------------------------------------------

@dataclass(frozen=True)
class MCPPolicySnapshot:
    """Complete MCP policy state captured once per run."""

    run_id: str
    created_at: str = ""
    servers: tuple = ()  # Tuple[MCPServerSnapshot, ...]
    total_servers: int = 0
    connected_servers: int = 0
    total_tools_available: int = 0
    total_tools_blocked: int = 0
    retry_config: Dict[str, Any] = field(default_factory=dict)
    circuit_breaker_enabled: bool = True
    circuit_failure_threshold: int = 5
    circuit_reset_after_s: float = 60.0

    # Serialisation --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "servers": [s.to_dict() for s in self.servers],
            "total_servers": self.total_servers,
            "connected_servers": self.connected_servers,
            "total_tools_available": self.total_tools_available,
            "total_tools_blocked": self.total_tools_blocked,
            "retry_config": dict(self.retry_config),
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "circuit_reset_after_s": self.circuit_reset_after_s,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MCPPolicySnapshot":
        servers = tuple(
            MCPServerSnapshot(
                server_name=s["server_name"],
                status=s.get("status", "unknown"),
                tools_available=tuple(s.get("tools_available", ())),
                tools_blocked=tuple(s.get("tools_blocked", ())),
                block_reasons=s.get("block_reasons", {}),
                circuit_state=s.get("circuit_state", "closed"),
                consecutive_failures=s.get("consecutive_failures", 0),
                timeout_s=s.get("timeout_s", 30.0),
                capabilities=s.get("capabilities", {}),
            )
            for s in d.get("servers", [])
        )
        return cls(
            run_id=d["run_id"],
            created_at=d.get("created_at", ""),
            servers=servers,
            total_servers=d.get("total_servers", len(servers)),
            connected_servers=d.get("connected_servers", 0),
            total_tools_available=d.get("total_tools_available", 0),
            total_tools_blocked=d.get("total_tools_blocked", 0),
            retry_config=d.get("retry_config", {}),
            circuit_breaker_enabled=d.get("circuit_breaker_enabled", True),
            circuit_failure_threshold=d.get("circuit_failure_threshold", 5),
            circuit_reset_after_s=d.get("circuit_reset_after_s", 60.0),
        )


# -------------------------------------------------------------------
# Factory
# -------------------------------------------------------------------

def capture_mcp_policy_snapshot(
    *,
    run_id: str,
    mcp_tools: Optional[List[Dict[str, Any]]] = None,
    circuit_state: Optional[Dict[str, Any]] = None,
    config: Any = None,
    blocked_tools: Optional[Dict[str, str]] = None,
) -> MCPPolicySnapshot:
    """Build an :class:`MCPPolicySnapshot` from live runtime objects.

    Parameters
    ----------
    run_id:
        Current run identifier.
    mcp_tools:
        ``self.mcp_tools`` flat list — each item must carry at least
        ``{"server_name": str, "name": str}``.
    circuit_state:
        ``self._circuit_state`` dict mapping ``"server__tool"`` →
        ``{"failures": int, "opened_at_monotonic": float | None}``.
    config:
        ``AgenticDriverConfig`` (or any object with the retry attrs).
    blocked_tools:
        Map of ``"server__tool"`` → reason code for tools that are
        currently blocked by security / governance policy.
    """
    mcp_tools = mcp_tools or []
    circuit_state = circuit_state or {}
    blocked_tools = blocked_tools or {}

    # ── group tools by server ────────────────────────────────────────
    server_tools: Dict[str, List[str]] = {}
    for t in mcp_tools:
        srv = t.get("server_name", "unknown")
        server_tools.setdefault(srv, []).append(t.get("name", ""))

    # ── build per-server snapshots ───────────────────────────────────
    srv_snapshots: List[MCPServerSnapshot] = []
    total_avail = 0
    total_block = 0

    for srv_name, tools in sorted(server_tools.items()):
        avail: List[str] = []
        blocked: List[str] = []
        reasons: Dict[str, str] = {}

        for tool_name in tools:
            key = f"{srv_name}__{tool_name}"
            if key in blocked_tools:
                blocked.append(tool_name)
                reasons[tool_name] = blocked_tools[key]
            else:
                avail.append(tool_name)

        # circuit breaker for this server (use first matching key)
        cb_failures = 0
        cb_state = "closed"
        for cb_key, cb_val in circuit_state.items():
            if cb_key.startswith(f"{srv_name}__"):
                fails = cb_val.get("failures", 0) if isinstance(cb_val, dict) else 0
                if fails > cb_failures:
                    cb_failures = fails
                opened = (
                    cb_val.get("opened_at_monotonic")
                    if isinstance(cb_val, dict)
                    else None
                )
                if opened is not None:
                    cb_state = "open"

        total_avail += len(avail)
        total_block += len(blocked)

        srv_snapshots.append(
            MCPServerSnapshot(
                server_name=srv_name,
                status="connected",  # reachable if tools were registered
                tools_available=tuple(sorted(avail)),
                tools_blocked=tuple(sorted(blocked)),
                block_reasons=reasons,
                circuit_state=cb_state,
                consecutive_failures=cb_failures,
            )
        )

    # ── retry config ─────────────────────────────────────────────────
    retry_cfg: Dict[str, Any] = {}
    if config is not None:
        retry_cfg = {
            "timeout_s": float(getattr(config, "mcp_tool_timeout_s", 30.0) or 30.0),
            "max_retries": int(getattr(config, "mcp_tool_max_retries", 1) or 0),
            "backoff_s": float(
                getattr(config, "mcp_tool_retry_backoff_s", 0.5) or 0.0
            ),
            "backoff_max_s": float(
                getattr(config, "mcp_tool_retry_backoff_max_s", 8.0) or 0.0
            ),
        }

    cb_threshold = int(getattr(config, "circuit_failure_threshold", 5) or 5) if config else 5
    cb_reset = float(getattr(config, "circuit_reset_after_s", 60.0) or 60.0) if config else 60.0

    return MCPPolicySnapshot(
        run_id=run_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        servers=tuple(srv_snapshots),
        total_servers=len(srv_snapshots),
        connected_servers=len(srv_snapshots),  # all reachable by definition
        total_tools_available=total_avail,
        total_tools_blocked=total_block,
        retry_config=retry_cfg,
        circuit_breaker_enabled=True,
        circuit_failure_threshold=cb_threshold,
        circuit_reset_after_s=cb_reset,
    )

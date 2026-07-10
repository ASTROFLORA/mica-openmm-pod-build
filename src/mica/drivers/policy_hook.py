"""
S1.8 — Policy hooks (CrewAI-style).

A concrete ``ToolExecutionHook`` that enforces configurable tool-call
policies: allow/deny lists, per-tool call limits, server restrictions.

Usage::

    from mica.drivers.policy_hook import PolicyToolHook

    hook = PolicyToolHook(
        allowed_tools={"search_literature", "search_protein"},
        max_calls_per_tool=20,
    )
    driver.add_tool_hook(hook)

    # After execution:
    print(hook.violations)          # list of policy violation dicts
    print(hook.call_counts)         # {"search_literature": 5, ...}
    print(hook.total_calls)         # 5

Design:
    - Best-effort: violations are *recorded* but don't block execution
      (because ``_run_tool_hooks`` swallows exceptions).
    - Fully synchronous — compatible with both sync and async call paths.
    - The ``on_violation`` callback allows custom reactions (logging,
      telemetry, future hard-blocking when ``_run_tool_hooks`` supports it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# PolicyViolation — recorded when a rule is triggered
# ────────────────────────────────────────────────────────────────────

@dataclass
class PolicyViolation:
    """Record of a single policy rule being triggered."""
    rule: str
    server: str
    tool: str
    detail: str
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "server": self.server,
            "tool": self.tool,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }


# ────────────────────────────────────────────────────────────────────
# PolicyToolHook — implements ToolExecutionHook protocol
# ────────────────────────────────────────────────────────────────────

class PolicyToolHook:
    """
    Concrete ``ToolExecutionHook`` implementation with configurable rules.

    Parameters
    ----------
    allowed_tools:
        If non-empty, only these tool names are permitted.
        (Allowlist mode — anything not listed triggers a violation.)
    denied_tools:
        These tool names are always forbidden.
        Checked after ``allowed_tools``.
    denied_servers:
        Calls to these server names trigger a violation.
    max_calls_per_tool:
        Maximum number of calls allowed per individual tool.
        0 = unlimited.
    max_total_calls:
        Maximum total tool calls across all tools.
        0 = unlimited.
    on_violation:
        Optional callback ``(PolicyViolation) → None`` for custom reactions.
    """

    def __init__(
        self,
        *,
        allowed_tools: Optional[Set[str]] = None,
        denied_tools: Optional[Set[str]] = None,
        denied_servers: Optional[Set[str]] = None,
        max_calls_per_tool: int = 0,
        max_total_calls: int = 0,
        on_violation: Optional[Callable[[PolicyViolation], None]] = None,
    ) -> None:
        self.allowed_tools: Set[str] = set(allowed_tools or set())
        self.denied_tools: Set[str] = set(denied_tools or set())
        self.denied_servers: Set[str] = set(denied_servers or set())
        self.max_calls_per_tool: int = max_calls_per_tool
        self.max_total_calls: int = max_total_calls
        self.on_violation = on_violation

        # Tracking state
        self.call_counts: Dict[str, int] = {}
        self.total_calls: int = 0
        self.violations: List[PolicyViolation] = []

    # ── ToolExecutionHook protocol ─────────────────────────────────

    def before_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: Dict[str, Any],
        session_id: str,
    ) -> None:
        """Check policies before tool execution."""
        # Track call counts
        self.call_counts[tool] = self.call_counts.get(tool, 0) + 1
        self.total_calls += 1

        # Rule: denied server
        if self.denied_servers and server in self.denied_servers:
            self._record_violation(
                "denied_server", server, tool,
                f"Server '{server}' is on the deny list",
            )

        # Rule: allowed tools (allowlist mode)
        if self.allowed_tools and tool not in self.allowed_tools:
            self._record_violation(
                "not_in_allowlist", server, tool,
                f"Tool '{tool}' is not in the allowed set",
            )

        # Rule: denied tools
        if self.denied_tools and tool in self.denied_tools:
            self._record_violation(
                "denied_tool", server, tool,
                f"Tool '{tool}' is on the deny list",
            )

        # Rule: per-tool call limit
        if self.max_calls_per_tool > 0 and self.call_counts[tool] > self.max_calls_per_tool:
            self._record_violation(
                "max_calls_per_tool_exceeded", server, tool,
                f"Tool '{tool}' called {self.call_counts[tool]} times "
                f"(limit: {self.max_calls_per_tool})",
            )

        # Rule: total call limit
        if self.max_total_calls > 0 and self.total_calls > self.max_total_calls:
            self._record_violation(
                "max_total_calls_exceeded", server, tool,
                f"Total calls: {self.total_calls} (limit: {self.max_total_calls})",
            )

    def after_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: Dict[str, Any],
        session_id: str,
        result: Dict[str, Any],
    ) -> None:
        """Post-execution hook — currently a no-op; can be extended."""
        pass

    # ── Introspection ──────────────────────────────────────────────

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0

    def violations_by_rule(self, rule: str) -> List[PolicyViolation]:
        return [v for v in self.violations if v.rule == rule]

    def reset(self) -> None:
        """Clear all tracking state."""
        self.call_counts.clear()
        self.total_calls = 0
        self.violations.clear()

    # ── Private ────────────────────────────────────────────────────

    def _record_violation(
        self, rule: str, server: str, tool: str, detail: str,
    ) -> None:
        v = PolicyViolation(rule=rule, server=server, tool=tool, detail=detail)
        self.violations.append(v)
        logger.warning("PolicyToolHook violation: %s — %s", rule, detail)
        if self.on_violation:
            try:
                self.on_violation(v)
            except Exception:
                pass  # best-effort callback

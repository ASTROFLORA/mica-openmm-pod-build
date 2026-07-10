"""
SimpleAgentHub — Minimal cross-driver routing hub.
====================================================

Provides bidirectional routing so specialist drivers can call each other
without knowing concrete class types (GAP-4 fix).

Usage (injected by AgenticDriver._initialize_specialist_drivers):
    hub = SimpleAgentHub(drivers=self.specialist_drivers)
    for driver in self.specialist_drivers.values():
        driver.agent_hub = hub

Any specialist can then do:
    result = await self.agent_hub.route("alchemist", query="...", context={})
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SimpleAgentHub:
    """
    Lightweight routing hub that forwards calls between registered specialist drivers.

    Thread-safe for async concurrent access (no mutable state after construction).
    """

    def __init__(self, drivers: Dict[str, Any]) -> None:
        # Shallow copy so mutations to the original dict don't affect us.
        self._drivers: Dict[str, Any] = dict(drivers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, name: str, driver: Any) -> None:
        """Register or replace a driver at runtime."""
        self._drivers[name] = driver
        logger.debug("AgentHub: registered driver '%s'", name)

    def unregister(self, name: str) -> None:
        self._drivers.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._drivers

    def list_drivers(self):
        return list(self._drivers.keys())

    async def route(
        self,
        target: str,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        method: str = "execute",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Route a query to the named specialist driver.

        Args:
            target: Driver name (e.g. "biodynamo", "alchemist", "smic").
            query:  Natural-language query.
            context: Optional context dict forwarded to the driver.
            method:  Driver method to call (default: "execute").
                     Use "plan_and_execute" for the Alchemist planner path.

        Returns:
            Driver response dict (always a dict, never raises).
        """
        if target not in self._drivers:
            logger.warning("AgentHub: no driver registered for target '%s'", target)
            return {
                "error": f"AgentHub: no driver for target '{target}'",
                "available": list(self._drivers.keys()),
                "confidence": 0.0,
            }

        driver = self._drivers[target]

        # Resolve the requested method
        fn = getattr(driver, method, None)
        if not callable(fn):
            # Fallback to execute() if the requested method doesn't exist
            logger.warning(
                "AgentHub: driver '%s' has no %s() method — falling back to execute()",
                target, method,
            )
            fn = getattr(driver, "execute", None)
            if not callable(fn):
                return {
                    "error": f"AgentHub: driver '{target}' has no callable method",
                    "confidence": 0.0,
                }

        try:
            # plan_and_execute has a different signature
            if method == "plan_and_execute":
                mudo_data = (context or {}).get("mudo_data", context or {})
                result = await fn(
                    mudo_data=mudo_data,
                    task_description=query,
                    context=context,
                )
            else:
                result = await fn(query=query, context=context or {}, **kwargs)
            if not isinstance(result, dict):
                result = {"answer": str(result), "confidence": 0.8}
            return result
        except Exception as exc:
            logger.error("AgentHub: driver '%s'.%s() raised: %s", target, method, exc)
            return {
                "error": f"AgentHub: driver '{target}' error: {exc}",
                "confidence": 0.0,
            }

    async def route_streaming(
        self,
        target: str,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """Streaming version of :meth:`route` — yields ``AgentTurn`` events.

        Emits three events: a *thinking* marker before delegation, a *speaking*
        event with the full answer text, and a *done* marker.  This mirrors the
        ``_spawn_agent`` contract so the outer ``run_streaming`` loop can treat
        hub results and sub-loops identically.

        Args:
            target:  Driver name (e.g. ``"biodynamo"``, ``"alchemist"``).
            query:   Natural-language query forwarded to the driver.
            context: Optional context dict.

        Yields:
            :class:`mica.agentic.events.AgentTurn` instances.
        """
        import uuid as _uuid_hub
        _hub_sid = str(_uuid_hub.uuid4())[:8]
        from mica.agentic.events import AgentTurn

        yield AgentTurn(agent=target, role="thinking", text="[iniciando]", session_id=_hub_sid)

        result = await self.route(target, query=query, context=context, **kwargs)
        answer = str(result.get("answer", result.get("response", "")))

        if answer:
            yield AgentTurn(agent=target, role="speaking", text=answer, session_id=_hub_sid)

        yield AgentTurn(
            agent=target, role="done",
            text=result.get("error", ""), session_id=_hub_sid,
        )
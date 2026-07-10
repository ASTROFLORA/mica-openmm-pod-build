from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Awaitable, Callable, Dict, Optional


CallMcpTool = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass(frozen=True)
class ToolCall:
    server: str
    tool: str
    arguments: Dict[str, Any]
    session_id: Optional[str] = None


class DeterministicToolRunner:
    """Deterministic wrapper for MCP tool execution (no AI).

    Goals:
    - Single place for timeout/retry/caching behavior
    - Works both in-process (AgenticDriver) and behind an API endpoint
    - Keeps outputs deterministic when upstream is deterministic

    Note: this does NOT make a non-deterministic tool deterministic; it only
    provides a consistent execution envelope.
    """

    def __init__(
        self,
        call_mcp_tool: CallMcpTool,
        *,
        timeout_s: float = 30.0,
        max_retries: int = 0,
        enable_cache: bool = True,
    ) -> None:
        self._call_mcp_tool = call_mcp_tool
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._enable_cache = bool(enable_cache)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _cache_key(self, call: ToolCall) -> str:
        # Stable, JSON-based hashing. Best-effort stringification for non-JSON types.
        try:
            args_json = json.dumps(call.arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            args_json = str(call.arguments)

        raw = f"{call.server}\n{call.tool}\n{args_json}".encode("utf-8", errors="replace")
        return sha256(raw).hexdigest()

    async def run(self, call: ToolCall, *, use_cache: bool = True) -> Dict[str, Any]:
        """Run a single tool call with deterministic envelope.

        Returns the original payload from `call_mcp_tool`, plus a `runner` meta block.
        """

        started = time.time()
        cache_key = self._cache_key(call)

        if self._enable_cache and use_cache:
            cached = self._cache.get(cache_key)
            if isinstance(cached, dict):
                out = dict(cached)
                out["runner"] = {
                    "from_cache": True,
                    "attempts": 0,
                    "timeout_s": self._timeout_s,
                    "duration_ms": int((time.time() - started) * 1000),
                }
                return out

        last_error: Optional[str] = None
        attempts = 0
        for attempt in range(max(self._max_retries, 0) + 1):
            attempts = attempt + 1
            try:
                coro = self._call_mcp_tool(call.server, call.tool, call.arguments)
                payload = await asyncio.wait_for(coro, timeout=self._timeout_s)

                out = dict(payload or {})
                out["runner"] = {
                    "from_cache": False,
                    "attempts": attempts,
                    "timeout_s": self._timeout_s,
                    "duration_ms": int((time.time() - started) * 1000),
                }

                # Cache only successes.
                if self._enable_cache and bool(out.get("success")):
                    # Store without runner meta so it doesn't lie later.
                    to_store = dict(out)
                    to_store.pop("runner", None)
                    self._cache[cache_key] = to_store

                return out

            except asyncio.TimeoutError:
                last_error = f"Timeout after {self._timeout_s}s"
            except Exception as exc:
                last_error = str(exc)

        return {
            "success": False,
            "error": last_error or "Tool call failed",
            "server": call.server,
            "tool": call.tool,
            "runner": {
                "from_cache": False,
                "attempts": attempts,
                "timeout_s": self._timeout_s,
                "duration_ms": int((time.time() - started) * 1000),
            },
        }

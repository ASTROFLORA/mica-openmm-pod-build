"""Slice-7 §8 — @observe_tool decorator.

Wraps any public tool. On every invocation:
- opens an OTel span with gen-purpose attributes
- increments metrics
- publishes a tool_invocation feed post (best effort, never raises)

Usage:
    from mica.agentic.tools._observability import observe_tool

    @observe_tool("my_tool")
    async def my_tool(...): ...
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

from mica.observability.bootstrap import get_tracer
from mica.observability.metrics_catalog import (
    mica_tool_calls_total,
    mica_tool_latency_seconds,
)
from mica.observability.redaction_patterns import scrub, scrub_dict

_LOG = logging.getLogger("mica.observability.observe_tool")
F = TypeVar("F", bound=Callable[..., Any])


def _hash_args(args: tuple, kwargs: dict) -> str:
    try:
        payload = json.dumps(
            {"args": scrub_dict(list(args)), "kwargs": scrub_dict(kwargs)},
            default=str, sort_keys=True,
        )[:8192]
    except Exception:
        payload = scrub(repr((args, kwargs)))[:8192]
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_result(result: Any) -> str:
    try:
        payload = json.dumps(scrub_dict(result), default=str)[:8192]
    except Exception:
        payload = scrub(repr(result))[:8192]
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


async def _publish_tool_invocation(
    tool_name: str,
    args_hash: str,
    result_hash: str | None,
    wall_s: float,
    verdict: str,
    error: str | None,
    agent_id: str,
) -> None:
    try:
        from mica.agentic.tools.agent_feed import publish_cue
        await publish_cue(
            agent_id=agent_id,
            post_type="tool_invocation",
            topic="general",
            title=f"tool:{tool_name}:{verdict}",
            body=json.dumps({
                "tool": tool_name,
                "args_hash": args_hash,
                "result_hash": result_hash,
                "wall_s": round(wall_s, 4),
                "verdict": verdict,
                "error": error,
            }),
            metadata={"tool": tool_name, "verdict": verdict, "wall_s": round(wall_s, 4)},
        )
    except Exception as exc:
        _LOG.debug("tool_invocation feed write skipped: %s", exc)


def observe_tool(
    name: str,
    *,
    agent_id: str = "mica-tool",
    emit_feed_post: bool = True,
) -> Callable[[F], F]:
    """Instrument any (async or sync) tool callable."""
    tracer = get_tracer("mica.tools")

    def deco(fn: F) -> F:
        is_async = asyncio.iscoroutinefunction(fn)

        async def _run_async(*args, **kwargs):
            t0 = time.monotonic()
            args_hash = _hash_args(args, kwargs)
            verdict = "ok"
            error: str | None = None
            result = None
            with tracer.start_as_current_span(f"tool.{name}") as span:
                span.set_attribute("mica.tool.name", name)
                span.set_attribute("mica.tool.args_hash", args_hash)
                try:
                    result = await fn(*args, **kwargs)
                    return result
                except Exception as exc:
                    verdict = "error"
                    error = scrub(repr(exc))[:200]
                    try:
                        span.record_exception(exc)
                    except Exception:
                        pass
                    raise
                finally:
                    wall = time.monotonic() - t0
                    try:
                        mica_tool_calls_total.add(1, {"tool": name, "verdict": verdict})
                        mica_tool_latency_seconds.record(wall, {"tool": name})
                    except Exception:
                        pass
                    if emit_feed_post:
                        r_hash = _hash_result(result) if verdict == "ok" else None
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(_publish_tool_invocation(
                                name, args_hash, r_hash, wall, verdict, error, agent_id
                            ))
                        except Exception:
                            pass

        def _run_sync(*args, **kwargs):
            t0 = time.monotonic()
            args_hash = _hash_args(args, kwargs)
            verdict = "ok"
            error: str | None = None
            result = None
            with tracer.start_as_current_span(f"tool.{name}") as span:
                span.set_attribute("mica.tool.name", name)
                span.set_attribute("mica.tool.args_hash", args_hash)
                try:
                    result = fn(*args, **kwargs)
                    return result
                except Exception as exc:
                    verdict = "error"
                    error = scrub(repr(exc))[:200]
                    try:
                        span.record_exception(exc)
                    except Exception:
                        pass
                    raise
                finally:
                    wall = time.monotonic() - t0
                    try:
                        mica_tool_calls_total.add(1, {"tool": name, "verdict": verdict})
                        mica_tool_latency_seconds.record(wall, {"tool": name})
                    except Exception:
                        pass
                    if emit_feed_post:
                        r_hash = _hash_result(result) if verdict == "ok" else None
                        try:
                            # Best-effort fire-and-forget (no loop in sync context).
                            asyncio.run(_publish_tool_invocation(
                                name, args_hash, r_hash, wall, verdict, error, agent_id
                            ))
                        except RuntimeError:
                            # Already in a loop — skip.
                            pass
                        except Exception:
                            pass

        if is_async:
            return functools.wraps(fn)(_run_async)  # type: ignore
        return functools.wraps(fn)(_run_sync)  # type: ignore

    return deco

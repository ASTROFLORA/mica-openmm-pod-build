from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from mica.agentic.feed_core import register_post_append_listener, unregister_post_append_listener
from mica.infrastructure.persistence.pg_async import choose_timescale_database_url, create_asyncpg_pool_for_database_url

logger = logging.getLogger(__name__)


class FeedTimescaleProjector:
    """Best-effort projection of canonical feed posts into Timescale.

    FeedCore remains the only write authority. This projector only mirrors
    already-appended posts into `public.feed_events` for analytics/driver-view.
    """

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = choose_timescale_database_url(database_url)
        self._pool = None
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=2048)
        self._task: Optional[asyncio.Task[None]] = None
        self._started = False

    async def start(self) -> bool:
        if self._started:
            return True
        if not self.database_url:
            return False
        self._pool = await create_asyncpg_pool_for_database_url(
            self.database_url,
            min_size=1,
            max_size=3,
            command_timeout=30,
            timeout=20,
        )
        await self._ensure_schema()
        register_post_append_listener(self.enqueue_post)
        self._task = asyncio.create_task(self._run(), name="feed-timescale-projector")
        self._started = True
        return True

    async def stop(self) -> None:
        unregister_post_append_listener(self.enqueue_post)
        self._started = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def enqueue_post(self, post: Dict[str, Any]) -> None:
        if not self._started:
            return
        try:
            self._queue.put_nowait(dict(post))
        except asyncio.QueueFull:
            logger.warning("feed_timescale_projector queue full; dropping post %s", post.get("id"))

    async def _ensure_schema(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS public.feed_events (
                    ts TIMESTAMPTZ NOT NULL,
                    post_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    post_type TEXT,
                    topic TEXT,
                    title TEXT,
                    body TEXT,
                    session_id TEXT,
                    metadata JSONB DEFAULT '{}'::jsonb
                )
                """
            )
            try:
                await conn.execute(
                    "SELECT create_hypertable('public.feed_events', 'ts', if_not_exists => TRUE)"
                )
            except Exception:
                logger.debug("feed_events hypertable ensure skipped", exc_info=True)

    async def _run(self) -> None:
        while True:
            post = await self._queue.get()
            try:
                await self._project(post)
            except Exception:
                logger.warning("feed_timescale_projector failed for post %s", post.get("id"), exc_info=True)
            finally:
                self._queue.task_done()

    async def _project(self, post: Dict[str, Any]) -> None:
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.feed_events (
                    ts, post_id, agent_id, post_type, topic, title, body, session_id, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT (post_id) DO NOTHING
                """,
                post.get("timestamp") or post.get("ts"),
                post.get("id"),
                post.get("agent_id"),
                post.get("post_type"),
                post.get("topic"),
                post.get("title") or post.get("intent"),
                post.get("body") or post.get("content"),
                post.get("session_id"),
                json.dumps(post.get("metadata") or {}),
            )


_PROJECTOR: Optional[FeedTimescaleProjector] = None


def get_feed_timescale_projector(database_url: Optional[str] = None) -> FeedTimescaleProjector:
    global _PROJECTOR
    if _PROJECTOR is None:
        _PROJECTOR = FeedTimescaleProjector(database_url=database_url)
    return _PROJECTOR

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional


logger = logging.getLogger(__name__)


async def await_nonfatal_startup_step(
    step_name: str,
    awaitable: Any,
    *,
    timeout_sec: float,
) -> tuple[bool, Any]:
    """Bound startup latency for degradable services."""

    started_at = time.perf_counter()
    try:
        result = await asyncio.wait_for(awaitable, timeout=timeout_sec)
        logger.info("%s ready in %.2fs", step_name, time.perf_counter() - started_at)
        return True, result
    except Exception as exc:
        logger.warning(
            "%s unavailable after %.2fs (non-fatal): %s",
            step_name,
            time.perf_counter() - started_at,
            exc,
        )
        return False, None


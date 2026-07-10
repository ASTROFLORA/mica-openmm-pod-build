"""Retry policy for transient LLM API failures.

Inspired by OpenCode's ``retry.ts``.  Supports exponential back-off with
optional ``Retry-After`` header respect.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 529})


@dataclass
class RetryPolicy:
    """Configurable exponential back-off retry policy."""

    initial_delay_ms: int = 2000
    backoff_factor: float = 2.0
    max_delay_ms: int = 30_000
    max_attempts: int = 5

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def delay_ms(
        self,
        attempt: int,
        response_headers: Optional[Dict[str, str]] = None,
    ) -> int:
        """Return the delay (in ms) before the next retry.

        If the response contains a ``Retry-After`` header (seconds), that
        value takes precedence over the calculated exponential back-off,
        clamped to *max_delay_ms*.
        """
        if response_headers:
            retry_after = response_headers.get("retry-after") or response_headers.get(
                "Retry-After"
            )
            if retry_after is not None:
                try:
                    header_ms = int(float(retry_after) * 1000)
                    return min(max(header_ms, 0), self._safe_int(self.max_delay_ms, default=30_000, minimum=0))
                except (ValueError, TypeError):
                    pass

        initial_delay_ms = self._safe_int(self.initial_delay_ms, default=2000, minimum=0)
        backoff_factor = self._safe_float(self.backoff_factor, default=2.0, minimum=1.0)
        max_delay_ms = self._safe_int(self.max_delay_ms, default=30_000, minimum=0)
        computed = int(initial_delay_ms * (backoff_factor ** max(attempt - 1, 0)))
        return min(max(computed, 0), max_delay_ms)

    @staticmethod
    def _safe_int(value: object, *, default: int, minimum: int = 0) -> int:
        try:
            coerced = int(float(value))
        except (TypeError, ValueError):
            return default
        return max(coerced, minimum)

    @staticmethod
    def _safe_float(value: object, *, default: float, minimum: float = 0.0) -> float:
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            return default
        return max(coerced, minimum)

    def is_retryable(self, error: Exception) -> bool:
        """Return *True* if *error* represents a transient failure."""
        # openai SDK errors ---------------------------------------------------
        try:
            from openai import (
                APIConnectionError as OAIConn,
                APITimeoutError as OAITimeout,
                InternalServerError as OAIInternal,
                RateLimitError as OAIRate,
            )

            if isinstance(error, (OAIConn, OAITimeout, OAIRate, OAIInternal)):
                return True
        except ImportError:
            pass

        # anthropic SDK errors -------------------------------------------------
        try:
            from anthropic import (
                APIConnectionError as AConn,
                APITimeoutError as ATimeout,
                InternalServerError as AInternal,
                RateLimitError as ARate,
            )

            if isinstance(error, (AConn, ATimeout, ARate, AInternal)):
                return True
        except ImportError:
            pass

        # Generic HTTP status code check (works with httpx.HTTPStatusError) ----
        status = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        if status is not None and int(status) in _RETRYABLE_STATUS_CODES:
            return True

        # Catch-all string heuristics (connection reset, etc.) -----------------
        msg = str(error).lower()
        transient_phrases = (
            "connection reset",
            "connection aborted",
            "overloaded",
            "temporarily unavailable",
            "bad gateway",
            "service unavailable",
        )
        return any(phrase in msg for phrase in transient_phrases)

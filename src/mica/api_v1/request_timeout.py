from __future__ import annotations

import os
from contextvars import ContextVar, Token

from fastapi import Request


_REQUEST_TIMEOUT_SECONDS: ContextVar[float | None] = ContextVar(
    "mica_api_request_timeout_seconds",
    default=None,
)


def _coerce_timeout_seconds(raw_value: object, *, default: float) -> float:
    if raw_value in (None, ""):
        return float(default)
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return float(default)


def _clamp_timeout_seconds(value: float, *, minimum: float, maximum: float) -> float:
    return max(float(minimum), min(float(value), float(maximum)))


def parse_request_timeout_seconds(
    request: Request | None,
    *,
    header_name: str = "x-request-timeout-seconds",
    env_var: str = "MICA_API_REQUEST_TIMEOUT_SECONDS",
    default: float = 30.0,
    minimum: float = 0.25,
    maximum: float = 60.0,
) -> float:
    env_default = _coerce_timeout_seconds(os.getenv(env_var), default=default)
    if request is None:
        return _clamp_timeout_seconds(env_default, minimum=minimum, maximum=maximum)

    header_value = request.headers.get(header_name, "")
    timeout_s = _coerce_timeout_seconds(header_value, default=env_default)
    return _clamp_timeout_seconds(timeout_s, minimum=minimum, maximum=maximum)


def set_current_request_timeout_seconds(timeout_s: float) -> Token:
    return _REQUEST_TIMEOUT_SECONDS.set(float(timeout_s))


def reset_current_request_timeout_seconds(token: Token) -> None:
    _REQUEST_TIMEOUT_SECONDS.reset(token)


def get_current_request_timeout_seconds(*, default: float | None = None) -> float | None:
    timeout_s = _REQUEST_TIMEOUT_SECONDS.get()
    if timeout_s is None:
        return default
    return float(timeout_s)
"""tool_registry_check.py — Slice-4 §7. Fail-fast on MICA_TOOLS / _spec drift.

Every tool exposed in `MICA_TOOLS` (the LLM-visible JSON-schema list in
`ws_bridge.py`) MUST also have a matching `_spec()` entry in
`tool_capability_registry._PUBLIC_TOOL_SPECS`. The matrix builder enforces
this at runtime, but the failure surfaces only when something tries to
call `build_tool_capability_matrix()`. This module raises immediately at
API/driver startup so the drift can never reach production silently.
"""
from __future__ import annotations

from typing import List, Set


class ToolRegistryDrift(RuntimeError):
    """Raised when MICA_TOOLS contains entries with no _spec wired."""


def verify_no_drift() -> List[str]:
    """Return the list of tool names verified, or raise ToolRegistryDrift.

    Imports happen lazily so this module is safe to import from anywhere
    (it does not pull in heavy provider deps at module load time).
    """
    from mica.agentic.ws_bridge import MICA_TOOLS  # noqa: WPS433
    from mica.agentic.tool_capability_registry import (  # noqa: WPS433
        build_tool_capability_matrix,
    )

    schema_names: Set[str] = set()
    for t in MICA_TOOLS:
        if not isinstance(t, dict):
            continue
        # OpenAI-style: {"type": "function", "function": {"name": ...}}
        fn = t.get("function")
        if isinstance(fn, dict) and "name" in fn:
            schema_names.add(fn["name"])
        # Flat fallback.
        elif "name" in t:
            schema_names.add(t["name"])
    try:
        matrix = build_tool_capability_matrix()
    except KeyError as exc:
        raise ToolRegistryDrift(
            f"capability matrix build failed (missing _spec): {exc!s}"
        ) from exc

    matrix_names: Set[str] = set(matrix.keys())
    missing = sorted(schema_names - matrix_names)
    if missing:
        raise ToolRegistryDrift(
            f"MICA_TOOLS schemas without _spec: {missing}"
        )

    return sorted(schema_names)

"""mudo_commands.py — CK6: Route-backed mudo.* read adapters.

Read-only views over MUDO codex and stale summary.
"""

from __future__ import annotations

from typing import Any, Dict

from mica.sdk.command_contracts import BackendCommandEnvelope


async def mudo_codex(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Return the MUDO Codex view — route-backed read adapter.

    GET /api/v1/mudos/{mudo_id}/codex
    """
    mudo_id = (args.get("mudo_id") or "").strip()
    if not mudo_id:
        raise RuntimeError("mudo.codex requires a non-empty mudo_id.")

    return {
        "summary": f"MUDO Codex for {mudo_id}",
        "result": {"mudo_id": mudo_id, "codex_mode": "read_only_projection"},
        "artifact_refs": [f"mudo://{mudo_id}"],
        "route_authority": "backend_api",
        "route_backed": True,
    }


async def mudo_stale_summary(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Return the MUDO stale summary — route-backed read adapter.

    GET /api/v1/mudos/{mudo_id}/stale
    """
    mudo_id = (args.get("mudo_id") or "").strip()
    if not mudo_id:
        raise RuntimeError("mudo.stale_summary requires a non-empty mudo_id.")

    return {
        "summary": f"MUDO Stale Summary for {mudo_id}",
        "result": {"mudo_id": mudo_id, "stale_mode": "read_only_projection"},
        "artifact_refs": [f"mudo://{mudo_id}"],
        "route_authority": "backend_api",
        "route_backed": True,
    }

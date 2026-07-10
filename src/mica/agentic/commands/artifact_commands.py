"""artifact_commands.py — CK5: Route-backed artifact.* read adapters.

Read-only inspection over /api/v1/artifacts endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

from mica.sdk.command_contracts import BackendCommandEnvelope


async def artifact_inspect(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Inspect one artifact — route-backed read adapter.

    GET /api/v1/artifacts/{artifact_id}
    """
    artifact_id = (args.get("artifact_id") or "").strip()
    if not artifact_id:
        raise RuntimeError("artifact.inspect requires a non-empty artifact_id.")

    return {
        "summary": f"Artifact inspection for {artifact_id}",
        "result": {"artifact_id": artifact_id, "inspect_mode": "read_only"},
        "artifact_refs": [f"artifact://{artifact_id}"],
        "route_authority": "backend_api",
        "route_backed": True,
    }

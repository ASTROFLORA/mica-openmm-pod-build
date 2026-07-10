"""Poll envelope normalization — unifies literature job poll response shapes.

Pure helper: zero imports beyond builtins.  Safe to import at module level
from any router or test without triggering heavy dependency chains.
"""

from __future__ import annotations

from typing import Any, Dict


def normalize_poll_envelope(record: Dict[str, Any]) -> Dict[str, Any]:
    """Promote artifact/lineage fields to the top-level poll response envelope.

    Ensures a consistent shape for all literature job poll responses:
        artifact_manifest   – artifact manifest dict (default: {})
        artifact_list       – flat artifact list (default: [])
        query_spec_hash     – canonical query fingerprint (default: "")
        protocol_version    – query protocol version (default: "")
        updated_at          – timestamp; falls back to finished_at if absent

    Field promotion rules
    ---------------------
    - If the field is already present and truthy at top-level: leave it.
    - Else if the field exists and is truthy under ``record["result"]``: hoist it.
    - Else: set the field to its declared default.

    This is intentionally non-destructive: existing top-level values are never
    overwritten, and all other record keys are preserved unchanged.
    """
    result: Dict[str, Any] = record.get("result") or {}
    out = dict(record)

    for field, default in (
        ("artifact_manifest", {}),
        ("artifact_list", []),
        ("query_spec_hash", ""),
        ("protocol_version", ""),
    ):
        if out.get(field):
            # Already present and truthy at top level — keep as-is.
            continue
        nested = result.get(field)
        if nested:
            out[field] = nested
        elif field not in out:
            out[field] = default

    # Normalize updated_at: prefer explicit value, fall back to finished_at.
    if "updated_at" not in out:
        out["updated_at"] = out.get("finished_at")

    return out

"""src/mica/provenance/__init__.py — ASTROFLORA mirror shim.

The mirror vendors a minimal subset of `mica.provenance` so the CG lane
modules (`martinize2_adapter.py`, `insane_adapter.py`,
`topology_preprocessor.py`, `geometry_audit.py`, `overlap_remediation.py`,
`bootstrap_wrapper.py`, `ts2cg_adapter.py`) can import from a real
Python module — not from the upstream private `juaness38/MICA-ultimate`
repo. Only the surface used by the CG lane is vendored.

This file intentionally has NO upstream content beyond the thin
``__all__`` re-export; full provenance semantics (canonical provenance
events, MUDO ingestion, gates) live in upstream and are NOT vendored.
"""
from __future__ import annotations

from .receipts import (
    GatePayload,
    ReceiptCore,
    ReceiptHashes,
    ReceiptRefs,
    ServerlessPayload,
)

__all__ = [
    "ReceiptRefs",
    "ReceiptHashes",
    "GatePayload",
    "ServerlessPayload",
    "ReceiptCore",
]
from __future__ import annotations

import re
from typing import List

from models.analysis import ProtocolCue, ProtocolCueResult
from .protocol_transport import stable_hash, utcnow_iso


_SCIENTIFIC_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{3,}")


def evaluate_intake_cues(query: str, cues: List[ProtocolCue]) -> List[ProtocolCueResult]:
    text = str(query or "").strip()
    results: List[ProtocolCueResult] = []
    for cue in cues:
        if cue.phase != "intake":
            continue
        ok = bool(text) and bool(_SCIENTIFIC_TOKEN_RE.search(text)) and len(text) >= 8
        results.append(
            ProtocolCueResult(
                cue_id=cue.cue_id,
                target_prompt_node_id=cue.target_prompt_node_id,
                status="passed" if ok else "failed",
                note="intake scope accepted" if ok else "query too vague for scientific execution",
                execution_context_hash=stable_hash({"query": text, "cue_id": cue.cue_id}),
                timestamp=utcnow_iso(),
            )
        )
    return results

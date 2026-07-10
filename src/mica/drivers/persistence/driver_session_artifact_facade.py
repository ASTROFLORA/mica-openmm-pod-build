from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class DriverSessionArtifactFacade:
    """
    Facade for session-scoped artifact persistence.
    
    Owns:
    - Evidence ledger persistence
    - Final result markdown artifact persistence  
    - Hot-loop reinjection packet staging and persistence
    
    Dependencies: Uses conversation log facade for artifact directory, 
    artifact sync facade for GCS operations
    
    Call sites: 4 total (2 evidence + 1 final_result + 1 staging)
    
    Pattern:
    - Centralizes session artifact write/sync logic
    - Delegates directory management to conversation facade
    - Delegates GCS sync to artifact sync facade
    """

    def __init__(
        self,
        *,
        conversation_log_facade: Any,  # DriverConversationLogFacade
        artifact_sync_facade: Any,  # DriverArtifactSyncFacade
    ) -> None:
        self._conversation_log_facade = conversation_log_facade
        self._artifact_sync_facade = artifact_sync_facade

    def persist_evidence_ledger(
        self,
        session_id: str,
        evidence_ledgers: Optional[Dict[str, Any]],
    ) -> Optional[Path]:
        """Persist evidence ledger for session to JSON artifact."""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        
        if not evidence_ledgers:
            return None
        
        ledger = evidence_ledgers.get(sid)
        if ledger is None:
            return None
        
        ledger_path = self._conversation_log_facade.get_artifact_dir(sid) / "evidence_ledger.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(ledger.to_json(), encoding="utf-8")
        
        self._artifact_sync_facade.sync_session_artifact_to_gcs(
            local_path=ledger_path,
            session_id=sid,
            filename="evidence_ledger.json",
        )
        
        return ledger_path

    def persist_final_result_artifact(
        self,
        *,
        session_id: str,
        result: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Persist final synthesis markdown as artifact."""
        if not isinstance(result, dict):
            return None
        
        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return None
        
        text = str(
            final_result.get("paper_markdown")
            or final_result.get("answer")
            or final_result.get("summary")
            or ""
        ).strip()
        if not text:
            return None
        
        artifact_path = self._conversation_log_facade.get_artifact_dir(session_id) / "final_result.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(text, encoding="utf-8")
        
        self._artifact_sync_facade.sync_session_artifact_to_gcs(
            local_path=artifact_path,
            session_id=session_id,
            filename="final_result.md",
        )
        
        # Add to artifacts list if not already present
        artifacts = final_result.setdefault("artifacts", [])
        if isinstance(artifacts, list) and not any(
            isinstance(a, dict) and a.get("path") == str(artifact_path) for a in artifacts
        ):
            artifacts.append({
                "type": "final_synthesis_markdown",
                "path": str(artifact_path),
                "description": "Integrated final synthesis persisted from the driver result.",
            })
        
        return str(artifact_path)

    def stage_hot_loop_reinjection_packet(
        self,
        *,
        session_id: str,
        packet: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Path]]:
        """Stage hot-loop reinjection packet for later processing."""
        sid = str(session_id or "").strip()
        if not sid or not isinstance(packet, dict) or not packet:
            return None
        
        artifact_dir = self._conversation_log_facade.get_artifact_dir(sid)
        staged_packet_path = artifact_dir / "hot_loop_reinjection.staged.json"
        staged_residual_path = artifact_dir / "residual_inventory.staged.json"
        
        staged_packet_path.parent.mkdir(parents=True, exist_ok=True)
        staged_packet_path.write_text(
            json.dumps(packet, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )
        
        residual_payload = {
            "schema_version": "mica.residual_inventory.v0",
            "entries": list(packet.get("residual_tasks") or []),
        }
        staged_residual_path.write_text(
            json.dumps(residual_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )
        
        return {
            "packet": staged_packet_path,
            "residual": staged_residual_path,
        }

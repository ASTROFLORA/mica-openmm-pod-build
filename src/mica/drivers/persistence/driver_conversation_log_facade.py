from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .conversation import append_conversation_log, conversation_log_path

logger = logging.getLogger(__name__)


class DriverConversationLogFacade:
    """
    Facade for conversation logging and artifact directory management.
    
    Owns:
    - Conversation log lock (prevents concurrent writes)
    - Artifact directory creation and path resolution
    - Conversation log append operations (config + persistence delegation)
    
    Call sites: 7 total (2 append + 5 artifact_dir)
    
    Pattern:
    - Centralizes lock and config capture
    - Delegates persistence operations to drivers.persistence.conversation module
    """

    def __init__(
        self,
        *,
        checkpoint_dir: str,
        conversation_log_dirname: Optional[str],
        conversation_log_enabled: bool,
        conversation_log_max_entries: int,
    ) -> None:
        self._checkpoint_dir = checkpoint_dir
        self._conversation_log_dirname = conversation_log_dirname
        self._conversation_log_enabled = conversation_log_enabled
        self._conversation_log_max_entries = conversation_log_max_entries
        self._conversation_log_lock = asyncio.Lock()

    def get_artifact_dir(self, session_id: str) -> Path:
        """Get (and create) the conversation artifact directory for a session."""
        artifact_dir = conversation_log_path(
            self._checkpoint_dir,
            session_id,
            self._conversation_log_dirname,
        ).with_suffix("")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    async def append_conversation_log(
        self,
        *,
        session_id: str,
        user_query: str,
        mode: str,
        result: Optional[Dict[str, Any]],
        started_at: Any,  # datetime
        finished_at: Any,  # datetime
        error: Optional[str],
    ) -> None:
        """Append an entry to the conversation log."""
        await append_conversation_log(
            conversation_log_enabled=self._conversation_log_enabled,
            checkpoint_dir=self._checkpoint_dir,
            conversation_log_dirname=self._conversation_log_dirname,
            conversation_log_max_entries=self._conversation_log_max_entries,
            conversation_log_lock=self._conversation_log_lock,
            session_id=session_id,
            user_query=user_query,
            mode=mode,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
        )

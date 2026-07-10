from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .gcs_sync import DriverArtifactSync

logger = logging.getLogger(__name__)


class DriverArtifactSyncFacade:
    """
    Lazy-init facade for GCS artifact synchronization.
    
    Owns:
    - User-specific DriverArtifactSync instance creation and caching
    - Optional context-var user_id resolution
    - Best-effort sync call forwarding (run-scoped and session-scoped)
    
    Call sites: 12 total (4 run-artifact + 7 session-artifact + 1 ensure)
    
    Pattern:
    - lazy_init: First call with user_id creates sync; subsequent calls with same uid reuse
    - best_effort: All sync failures log + return None (no exceptions propagated)
    """

    def __init__(self) -> None:
        self._gcs_artifact_sync: Optional[DriverArtifactSync] = None

    def ensure_gcs_sync(self, user_id: Optional[str] = None) -> Optional[DriverArtifactSync]:
        """Return (and lazily create) the GCS artifact sync for the given user."""
        uid = (user_id or "").strip()
        if not uid:
            return None
        sync = self._gcs_artifact_sync
        if sync is not None and sync._user_id == uid:
            return sync
        self._gcs_artifact_sync = DriverArtifactSync(user_id=uid)
        return self._gcs_artifact_sync

    def sync_run_artifact_to_gcs(
        self,
        *,
        local_path: Optional[Path],
        session_id: str,
        run_id: str,
        filename: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """Best-effort upload a local artifact to GCS (run-scoped)."""
        if local_path is None:
            return None
        sync = self.ensure_gcs_sync(user_id=user_id)
        if sync is None:
            return None
        try:
            return sync.sync_file(
                local_path=Path(local_path),
                session_id=session_id,
                run_id=run_id,
                filename=filename,
            )
        except Exception as exc:
            logger.debug("GCS sync failed for %s: %s", local_path, exc)
            return None

    def sync_session_artifact_to_gcs(
        self,
        *,
        local_path: Optional[Path],
        session_id: str,
        filename: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """Best-effort upload a local artifact to GCS (session-scoped)."""
        if local_path is None:
            return None
        sync = self.ensure_gcs_sync(user_id=user_id)
        if sync is None:
            return None
        try:
            return sync.sync_session_file(
                local_path=Path(local_path),
                session_id=session_id,
                filename=filename,
            )
        except Exception as exc:
            logger.debug("GCS session sync failed for %s: %s", local_path, exc)
            return None

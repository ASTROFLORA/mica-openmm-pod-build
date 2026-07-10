from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .snapshots import restore_session_snapshot, save_session_snapshot


class DriverSnapshotFacade:
    """Facade for snapshot front-door save/restore operations."""

    def __init__(
        self,
        *,
        snapshots_enabled: bool,
        checkpoint_dir: str,
        snapshots_dirname: Optional[str],
        conversation_log_path_fn: Callable[[str], Path],
        saga_log_path_fn: Callable[[str], Path],
        append_saga_event_fn: Callable[..., Awaitable[Any]],
    ) -> None:
        self._snapshots_enabled = snapshots_enabled
        self._checkpoint_dir = checkpoint_dir
        self._snapshots_dirname = snapshots_dirname
        self._conversation_log_path_fn = conversation_log_path_fn
        self._saga_log_path_fn = saga_log_path_fn
        self._append_saga_event_fn = append_saga_event_fn

    async def save_session_snapshot(self, *, session_id: str, label: str, overwrite: bool = False) -> Path:
        return await save_session_snapshot(
            snapshots_enabled=self._snapshots_enabled,
            checkpoint_dir=self._checkpoint_dir,
            snapshots_dirname=self._snapshots_dirname,
            session_id=session_id,
            label=label,
            overwrite=overwrite,
            conversation_log_path_fn=self._conversation_log_path_fn,
            saga_log_path_fn=self._saga_log_path_fn,
            append_saga_event_fn=self._append_saga_event_fn,
        )

    async def restore_session_snapshot(self, *, session_id: str, label: str) -> Path:
        return await restore_session_snapshot(
            snapshots_enabled=self._snapshots_enabled,
            checkpoint_dir=self._checkpoint_dir,
            snapshots_dirname=self._snapshots_dirname,
            session_id=session_id,
            label=label,
            conversation_log_path_fn=self._conversation_log_path_fn,
            append_saga_event_fn=self._append_saga_event_fn,
        )

    def refresh_runtime_config(
        self,
        *,
        snapshots_enabled: Optional[bool] = None,
        checkpoint_dir: Optional[str] = None,
        snapshots_dirname: Optional[str] = None,
    ) -> None:
        if snapshots_enabled is not None:
            self._snapshots_enabled = snapshots_enabled
        if checkpoint_dir is not None:
            self._checkpoint_dir = checkpoint_dir
        if snapshots_dirname is not None:
            self._snapshots_dirname = snapshots_dirname

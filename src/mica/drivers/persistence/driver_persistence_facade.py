from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ...infrastructure.persistence import TimescaleEventStore
from .conversation import conversation_log_path
from .manifests import write_report_card, write_run_manifest
from .saga import append_saga_event, append_saga_event_timescale, best_effort_saga_mcp_metrics, get_timescale_store, saga_log_path
from .snapshots import sha256_file


class DriverPersistenceFacade:
    def __init__(
        self,
        *,
        checkpoint_dir: str,
        conversation_log_dirname: Optional[str],
        saga_log_dirname: Optional[str],
        run_manifest_dirname: Optional[str],
        saga_log_enabled: bool,
        saga_log_max_bytes: int,
        run_manifest_enabled: bool,
        report_card_enabled: bool,
        mcp_config_path: str,
        mcp_enabled: bool,
        session_run_ids: Dict[str, str],
    ) -> None:
        self._checkpoint_dir = checkpoint_dir
        self._conversation_log_dirname = conversation_log_dirname
        self._saga_log_dirname = saga_log_dirname
        self._run_manifest_dirname = run_manifest_dirname
        self._saga_log_enabled = saga_log_enabled
        self._saga_log_max_bytes = saga_log_max_bytes
        self._run_manifest_enabled = run_manifest_enabled
        self._report_card_enabled = report_card_enabled
        self._mcp_config_path = mcp_config_path
        self._mcp_enabled = mcp_enabled
        self._session_run_ids = session_run_ids
        self._saga_log_lock = asyncio.Lock()
        self._timescale_store: Optional[TimescaleEventStore] = None
        self._timescale_store_failed = False

    def _conversation_log_path(self, session_id: str) -> Path:
        return conversation_log_path(
            self._checkpoint_dir,
            session_id,
            self._conversation_log_dirname,
        )

    def _saga_log_path(self, session_id: str) -> Path:
        return saga_log_path(
            self._checkpoint_dir,
            session_id,
            self._saga_log_dirname,
        )

    async def append_saga_event(self, *, session_id: str, event: Dict[str, Any]) -> None:
        await append_saga_event(
            checkpoint_dir=self._checkpoint_dir,
            saga_log_dirname=self._saga_log_dirname,
            saga_log_enabled=self._saga_log_enabled,
            saga_log_max_bytes=self._saga_log_max_bytes,
            saga_log_lock=self._saga_log_lock,
            timescale_appender=self.append_saga_event_timescale,
            session_id=session_id,
            event=event,
        )

    async def append_saga_event_timescale(self, *, session_id: str, event: Dict[str, Any]) -> None:
        store = await self.get_timescale_store()
        await append_saga_event_timescale(
            session_id=session_id,
            event=event,
            timescale_store=store,
            session_run_ids=self._session_run_ids,
        )

    async def get_timescale_store(self) -> Optional[TimescaleEventStore]:
        store, failed = await get_timescale_store(self._timescale_store, self._timescale_store_failed)
        self._timescale_store = store
        self._timescale_store_failed = failed
        return store

    def write_run_manifest(
        self,
        *,
        session_id: str,
        mode: str,
        started_at: datetime,
        finished_at: datetime,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> Optional[Path]:
        return write_run_manifest(
            checkpoint_dir=self._checkpoint_dir,
            run_manifest_dirname=self._run_manifest_dirname,
            run_manifest_enabled=self._run_manifest_enabled,
            session_id=session_id,
            mode=mode,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            error=error,
            session_run_ids=self._session_run_ids,
            conversation_log_path_fn=self._conversation_log_path,
            saga_log_path_fn=self._saga_log_path,
            sha256_file_fn=sha256_file,
            mcp_config_path=self._mcp_config_path,
            timescale_appender=self.append_saga_event_timescale,
        )

    def write_report_card(
        self,
        *,
        session_id: str,
        mode: str,
        started_at: datetime,
        finished_at: datetime,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> Optional[Path]:
        saga_mcp_metrics_fn = lambda sid: best_effort_saga_mcp_metrics(
            checkpoint_dir=self._checkpoint_dir,
            session_id=sid,
            saga_log_dirname=self._saga_log_dirname,
        )
        return write_report_card(
            checkpoint_dir=self._checkpoint_dir,
            run_manifest_dirname=self._run_manifest_dirname,
            report_card_enabled=self._report_card_enabled,
            session_id=session_id,
            mode=mode,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            error=error,
            conversation_log_path_fn=self._conversation_log_path,
            saga_log_path_fn=self._saga_log_path,
            sha256_file_fn=sha256_file,
            saga_mcp_metrics_fn=saga_mcp_metrics_fn,
            mcp_enabled=self._mcp_enabled,
        )

    async def close(self) -> None:
        if self._timescale_store is None:
            return
        try:
            await self._timescale_store.close()
        except Exception:
            pass
        finally:
            self._timescale_store = None

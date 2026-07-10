from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional


_ASSET_TYPE_MAP = {
    "document": "other",
    "image": "other",
    "data": "other",
    "text": "other",
    "markdown": "other",
    "json": "other",
}


def _normalize_asset_type(asset_type: str) -> str:
    value = str(asset_type or "other").strip().lower()
    if not value:
        return "other"
    return _ASSET_TYPE_MAP.get(value, value)


class DriverWorkspaceClient:
    """Best-effort workspace access for direct driver mode."""

    def _backend(self):
        from mica.api_v1.routers.workspace import _get_backend

        return _get_backend()

    def ensure_session_sync(
        self,
        *,
        user_id: str,
        workspace_id: str = "",
        name: str = "MICA Driver Session",
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        backend = self._backend()
        if workspace_id:
            return backend.get_session(user_id, workspace_id).model_dump()
        meta = backend.create_session(user_id, name, description)
        return meta.model_dump()

    async def list_sessions(self, *, user_id: str) -> Dict[str, Any]:
        def _work() -> Dict[str, Any]:
            backend = self._backend()
            return {
                "ok": True,
                "sessions": [session.model_dump() for session in backend.list_sessions(user_id)],
                "backend": "local-direct",
            }

        return await asyncio.to_thread(_work)

    async def list_assets(self, *, user_id: str, session_id: str) -> Dict[str, Any]:
        def _work() -> Dict[str, Any]:
            backend = self._backend()
            return {
                "ok": True,
                "assets": list(backend.list_assets(user_id, session_id)),
                "backend": "local-direct",
            }

        return await asyncio.to_thread(_work)

    async def read_document(self, *, user_id: str, session_id: str, asset_id: str) -> Dict[str, Any]:
        def _work() -> Dict[str, Any]:
            from mica.api_v1.routers.workspace import _extract_text_from_bytes

            backend = self._backend()
            data, asset_record = backend.read_asset_content(user_id, session_id, asset_id)
            result = _extract_text_from_bytes(
                data,
                asset_record["name"],
                asset_record.get("asset_type", "other"),
            )
            result["asset"] = {
                "asset_id": asset_record["asset_id"],
                "name": asset_record["name"],
                "asset_type": asset_record.get("asset_type", "other"),
                "size_bytes": asset_record.get("size_bytes", len(data)),
            }
            result["ok"] = True
            result["backend"] = "local-direct"
            return result

        return await asyncio.to_thread(_work)

    async def add_text_asset(
        self,
        *,
        user_id: str,
        session_id: str,
        name: str,
        content: str,
        asset_type: str = "document",
    ) -> Dict[str, Any]:
        def _work() -> Dict[str, Any]:
            backend = self._backend()
            asset = backend.add_asset(
                user_id=user_id,
                session_id=session_id,
                asset_type=_normalize_asset_type(asset_type),
                name=name,
                data=(content or "").encode("utf-8"),
            )
            return {"ok": True, "asset": asset, "backend": "local-direct"}

        return await asyncio.to_thread(_work)

    def publish_file_sync(
        self,
        *,
        user_id: str,
        session_id: str,
        local_path: Path,
        name: Optional[str] = None,
        asset_type: str = "data",
    ) -> Dict[str, Any]:
        backend = self._backend()
        asset = backend.add_asset(
            user_id=user_id,
            session_id=session_id,
            asset_type=_normalize_asset_type(asset_type),
            name=name or local_path.name,
            data=Path(local_path).read_bytes(),
        )
        return {"ok": True, "asset": asset, "backend": "local-direct"}
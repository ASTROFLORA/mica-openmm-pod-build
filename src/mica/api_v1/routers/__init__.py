"""API v1 router package.

This package is the intended home for v1 HTTP routers that are being migrated
from the legacy surface under `src/routes/*`.

Migration map (high level):
- Legacy `src/routes/mudo.py`       -> `mica.api_v1.routers.mudo`
- Legacy `src/routes/workspace.py`  -> `mica.api_v1.routers.workspaces`
- Legacy `src/routes/rag.py`        -> `mica.api_v1.routers.rag`
- Legacy `src/routes/analytics.py`  -> `mica.api_v1.routers.metrics`

Explicitly NOT part of v1 core:
- Legacy `src/routes/proteins.py`   (stub/experimental)
- Legacy `src/routes/websocket.py`  (placeholder auth; replaced by `/ws/mica`)
- Legacy `src/routes/ops.py`        (broken; requires rewrite)
"""

__all__: list[str] = []

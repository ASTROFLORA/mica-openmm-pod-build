from __future__ import annotations

import os

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from mica.config.dotenv_loader import seed_env_from_dotenv


def _parse_cors_origins() -> tuple[list[str], bool]:
    raw = (
        os.getenv("WS_CORS_ALLOW_ORIGINS")
        or os.getenv("CORS_ALLOW_ORIGINS")
        or os.getenv("CORS_ORIGINS")
        or "http://localhost:3000,http://localhost:5173"
    )
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        origins = ["http://localhost:5173"]

    allow_credentials = (os.getenv("CORS_ALLOW_CREDENTIALS", "true").strip().lower() in {"1", "true", "yes"})
    if "*" in origins:
        # Star origins are incompatible with credentials; force safe behavior.
        allow_credentials = False

    return origins, allow_credentials


def create_app() -> FastAPI:
    seed_env_from_dotenv()
    app = FastAPI(title="MICA WS App", version="0.1")

    cors_origins, cors_allow_credentials = _parse_cors_origins()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "Origin",
            "User-Agent",
            "X-Requested-With",
        ],
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.websocket("/ws/mica")
    async def mica_ws(websocket: WebSocket) -> None:
        from mica.ws_agentic import handle_mica_agentic_websocket
        await handle_mica_agentic_websocket(websocket)

    @app.websocket("/ws/md/{job_id}")
    async def md_ws(websocket: WebSocket, job_id: str) -> None:
        from mica.ws_md import handle_md_websocket
        await handle_md_websocket(websocket, job_id)

    return app


app = create_app()

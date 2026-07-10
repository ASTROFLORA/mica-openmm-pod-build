from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List


def normalized_route_map(app: Any) -> Dict[str, List[str]]:
    route_map: Dict[str, List[str]] = {}
    for route in getattr(app, "routes", []):
        path = str(getattr(route, "path", "") or "")
        methods = getattr(route, "methods", set()) or set()
        if path and not methods and route.__class__.__name__.lower().endswith("websocketroute"):
            route_map.setdefault("WEBSOCKET", []).append(path)
            continue
        for method in sorted(str(m).upper() for m in methods):
            route_map.setdefault(method, []).append(path)
    return route_map


def route_exists(route_map: Dict[str, List[str]], method: str, path: str) -> bool:
    return path in set(route_map.get(method.upper(), []))


def runtime_fingerprint_payload(
    *,
    app: Any,
    compute_router_loaded: bool,
    process_start_time: int | None = None,
) -> Dict[str, Any]:
    route_map = normalized_route_map(app)
    expected_routes = [
        "GET /api/v1/compute/ws-ticket/authority",
        "POST /api/v1/compute/ws-ticket",
    ]
    route_loaded = route_exists(route_map, "GET", "/api/v1/compute/ws-ticket/authority") and route_exists(
        route_map,
        "POST",
        "/api/v1/compute/ws-ticket",
    )

    buildversion = os.getenv("MICA_BUILDVERSION", "").strip()
    if not buildversion:
        for candidate in (Path("/app/.buildversion"), Path("/tmp/.buildversion")):
            try:
                if candidate.exists():
                    buildversion = candidate.read_text(encoding="utf-8", errors="replace").strip()
                    if buildversion:
                        break
            except Exception:
                continue

    code_ref = f"runtime_metadata.py:{Path(__file__).stat().st_mtime_ns}"
    code_path_fingerprint = hashlib.sha256(code_ref.encode("utf-8")).hexdigest()[:16]
    image_digest = (
        os.getenv("MICA_MD_DOCKER_IMAGE_DIGEST")
        or os.getenv("MICA_OPENMM_POD_IMAGE_DIGEST")
        or ""
    ).strip()

    return {
        "git_sha": os.getenv("GITHUB_SHA") or os.getenv("MICA_GIT_SHA") or "",
        "build_sha": image_digest,
        "image_digest": image_digest,
        "app_version": buildversion or "dev",
        "deployment_environment": (
            os.getenv("ENVIRONMENT")
            or os.getenv("RAILWAY_ENVIRONMENT_NAME")
            or os.getenv("MICA_ENV")
            or ""
        ),
        "railway_service_name": os.getenv("RAILWAY_SERVICE_NAME") or os.getenv("MICA_COMPONENT") or "",
        "railway_deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID") or "",
        "route_loaded": route_loaded,
        "router_module_loaded": compute_router_loaded,
        "compute_router_loaded": compute_router_loaded,
        "ws_ticket_router_loaded": compute_router_loaded,
        "build_time": os.getenv("MICA_BUILD_TIME", ""),
        "process_start_time": int(process_start_time if process_start_time is not None else time.time()),
        "code_path_fingerprint": code_path_fingerprint,
        "expected_routes": expected_routes,
        "route_map": route_map,
    }


def runtime_routes_payload(app: Any) -> Dict[str, Any]:
    route_map = normalized_route_map(app)
    expected_compute_ticket_routes = [
        "GET /api/v1/compute/ws-ticket/authority",
        "POST /api/v1/compute/ws-ticket",
    ]
    compute_ticket_routes_mounted = route_exists(route_map, "GET", "/api/v1/compute/ws-ticket/authority") and route_exists(
        route_map,
        "POST",
        "/api/v1/compute/ws-ticket",
    )
    return {
        "route_map": route_map,
        "expected_compute_ticket_routes": expected_compute_ticket_routes,
        "compute_ticket_routes_mounted": compute_ticket_routes_mounted,
    }

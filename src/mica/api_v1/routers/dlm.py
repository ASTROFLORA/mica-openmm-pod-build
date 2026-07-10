"""
DLM Presets & Literature Scan API
----------------------------------
Exposes the 5 DLM literature scan presets and a scan trigger endpoint.

Endpoints:
  GET  /api/v1/dlm/presets          – list all DLM presets
  GET  /api/v1/dlm/preset/{name}    – detail for a single preset
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency as _user_dependency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Preset imports (tolerant — API still boots without full DLM stack)
# ---------------------------------------------------------------------------
_DLM_PRESETS_AVAILABLE = False
_DLM_PRESETS: Dict[str, Dict[str, Any]] = {}
_DLM_LOAD_ERROR: Optional[str] = None

try:
    from mica.memory.dlm.presets import (
        DLM_PRESETS as _DLM_PRESETS,
        get_dlm_preset as _get_dlm_preset,
        list_dlm_presets as _list_dlm_presets,
        ALL_DLM_PRESET_NAMES,
    )
    _DLM_PRESETS_AVAILABLE = True
except Exception as _e:
    _DLM_LOAD_ERROR = str(_e)

    # Fallback inline definitions so the router always responds
    ALL_DLM_PRESET_NAMES: List[str] = [  # type: ignore[no-redef]
        "quick-scan", "standard", "deep-research", "exhaustive", "llm-context",
    ]

    def _get_dlm_preset(name: str) -> Dict[str, Any]:  # type: ignore[misc]
        raise KeyError(name)

    def _list_dlm_presets() -> List[Dict[str, Any]]:  # type: ignore[misc]
        return []


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1/dlm", tags=["dlm"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DLMPresetInfo(BaseModel):
    name: str
    description: str
    max_papers: int
    sources: List[str]
    download_pdfs: bool
    extract_entities: bool
    run_atom: bool
    citation_depth: int = 0
    format: str = "xml"


class DLMGraphRepairExportRequest(BaseModel):
    pdf_path: str = Field(..., description="Absolute or server-local PDF path to repair and export")
    output_dir: str | None = Field(default=None, description="Optional output directory for graph artifacts")
    provider_id: str = Field(default="deepinfra", description="LLM provider id for the graph repair runner")
    model_id: str | None = Field(default=None, description="Optional explicit model override")
    max_pages: int = Field(default=40, ge=1, le=400)
    max_candidates: int = Field(default=0, ge=0)
    tool_budget: int = Field(default=24, ge=1, le=128)
    include_cooccurs: bool = Field(default=False)
    clear_dlm_cache: bool = Field(default=False)


class DLMGraphRepairExportResult(BaseModel):
    ok: bool = True
    output_dir: str
    graph_html_path: str
    summary: Dict[str, Any]
    graph: Dict[str, Any]
    graph_layers: Dict[str, Any]
    evidence_index: Dict[str, Any]
    demoted_edges_audit: Dict[str, Any]
    runtime_report: Dict[str, Any]
    artifact_inventory: Dict[str, Any]


def _load_graph_repair_runtime():
    try:
        from tools.dlm_graph_repair_runtime import run_graph_repair_runtime
    except Exception as exc:  # pragma: no cover - fail closed if runtime wiring is unavailable
        raise HTTPException(status_code=500, detail=f"dlm_graph_repair_runtime_unavailable: {exc}") from exc
    return run_graph_repair_runtime


def _load_json_artifact(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"missing_graph_export_artifact:{path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"invalid_graph_export_artifact:{path.name}:{exc}") from exc


def _default_graph_export_dir(pdf_path: Path) -> Path:
    prefix = f"dlm_graph_export_{pdf_path.stem[:24]}_"
    return Path(tempfile.mkdtemp(prefix=prefix))


# ---------------------------------------------------------------------------
# GET /api/v1/dlm/presets — list all DLM presets
# ---------------------------------------------------------------------------
@router.get("/presets")
def list_all_dlm_presets(_user: str = Depends(_user_dependency)) -> dict:
    """Return all registered DLM literature scan presets."""
    presets: Dict[str, Any] = {}
    names = list(_DLM_PRESETS.keys()) if _DLM_PRESETS_AVAILABLE else ALL_DLM_PRESET_NAMES
    for name in names:
        try:
            raw = _get_dlm_preset(name)
            presets[name] = DLMPresetInfo(**raw).model_dump()
        except Exception as e:
            presets[name] = {"name": name, "error": str(e)}
    return {
        "ok": True,
        "count": len(presets),
        "presets": presets,
        "backend_available": _DLM_PRESETS_AVAILABLE,
        "load_error": _DLM_LOAD_ERROR,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/dlm/preset/{name} — single preset detail
# ---------------------------------------------------------------------------
@router.get("/preset/{name}")
def get_dlm_preset_detail(name: str, _user: str = Depends(_user_dependency)) -> dict:
    """Return full configuration for a single DLM preset."""
    try:
        raw = _get_dlm_preset(name)
        info = DLMPresetInfo(**raw)
        return {"ok": True, "preset": info.model_dump()}
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown DLM preset: {name!r}. Available: {ALL_DLM_PRESET_NAMES}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/graph-repair/export", response_model=DLMGraphRepairExportResult)
def run_dlm_graph_repair_export(
    request: DLMGraphRepairExportRequest,
    _user: str = Depends(_user_dependency),
) -> DLMGraphRepairExportResult:
    """Run the PDF-bound GraphPatch repair/export lane and return the layered graph contract."""
    pdf_path = Path(request.pdf_path).expanduser().resolve()
    if not pdf_path.exists() or not pdf_path.is_file():
        raise HTTPException(status_code=404, detail=f"pdf_not_found:{pdf_path}")

    output_dir = (
        Path(request.output_dir).expanduser().resolve()
        if request.output_dir
        else _default_graph_export_dir(pdf_path)
    )

    try:
        summary = _load_graph_repair_runtime()(
            pdf_path=pdf_path,
            output_dir=output_dir,
            provider_id=request.provider_id,
            model_id=request.model_id,
            max_pages=request.max_pages,
            max_candidates=request.max_candidates,
            tool_budget=request.tool_budget,
            include_cooccurs=request.include_cooccurs,
            clear_dlm_cache=request.clear_dlm_cache,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("dlm graph repair export failed for %s", pdf_path)
        raise HTTPException(status_code=500, detail=f"dlm_graph_repair_export_failed:{exc}") from exc

    materialized_dir = Path(str(summary.get("output_dir") or output_dir)).expanduser().resolve()
    return DLMGraphRepairExportResult(
        output_dir=str(materialized_dir),
        graph_html_path=str(materialized_dir / "graph.html"),
        summary=summary,
        graph=_load_json_artifact(materialized_dir / "graph.json"),
        graph_layers=_load_json_artifact(materialized_dir / "graph_layers.json"),
        evidence_index=_load_json_artifact(materialized_dir / "evidence_index.json"),
        demoted_edges_audit=_load_json_artifact(materialized_dir / "demoted_edges.audit.json"),
        runtime_report=_load_json_artifact(materialized_dir / "runtime_report.json"),
        artifact_inventory=_load_json_artifact(materialized_dir / "artifact_inventory.json"),
    )


# ---------------------------------------------------------------------------
# P1-5: Graph Repair Packet Promotion to Workspace
# ---------------------------------------------------------------------------

class DLMGraphRepairPromoteRequest(BaseModel):
    output_dir: str = Field(..., description="Output directory from a previous graph-repair/export run")
    session_id: str = Field(..., description="Target workspace session ID")
    asset_type: str = Field(default="json", description="Asset type classification")


class DLMGraphRepairPromoteResult(BaseModel):
    ok: bool = True
    promoted_files: List[str]
    session_id: str
    output_dir: str


@router.post("/graph-repair/promote", response_model=DLMGraphRepairPromoteResult)
def promote_graph_repair_to_workspace(
    request: DLMGraphRepairPromoteRequest,
    _user: str = Depends(_user_dependency),
) -> DLMGraphRepairPromoteResult:
    """Promote graph repair artifacts from server-local output_dir into a workspace session."""
    import shutil

    source_dir = Path(request.output_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"output_dir_not_found:{source_dir}")

    # Validate that this looks like a graph repair output
    required = ["graph.json", "graph.html", "runtime_report.json"]
    missing = [f for f in required if not (source_dir / f).exists()]
    if missing:
        raise HTTPException(status_code=400, detail=f"not_a_graph_repair_output:missing:{','.join(missing)}")

    # Determine workspace session directory
    workspace_base = Path(tempfile.gettempdir()) / "mica_workspaces" / request.session_id
    target_dir = workspace_base / "graph_repair"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy all artifacts
    promoted = []
    for item in source_dir.iterdir():
        if item.is_file():
            dest = target_dir / item.name
            shutil.copy2(item, dest)
            promoted.append(item.name)

    return DLMGraphRepairPromoteResult(
        promoted_files=promoted,
        session_id=request.session_id,
        output_dir=str(target_dir),
    )

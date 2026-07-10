"""S-R4 · Storage v1 Router — schema versioning, DR replay drill, closure v1.

Exposes:
  GET  /api/v1/storage/schema                    → schema registry
  GET  /api/v1/storage/schema/{target}          → versions of a target
  GET  /api/v1/storage/schema/{target}/{v}/fields → field specs
  POST /api/v1/storage/schema/dual-read        → dual-read adapter
  POST /api/v1/storage/schema/migrate          → try migration
  POST /api/v1/storage/dr/replay-drill         → run DR replay drill
  GET  /api/v1/storage/dr/metrics               → DR metrics
  GET  /api/v1/storage/v1/closure               → build Storage v1 closure
  GET  /api/v1/storage/v1/freeze                → freeze status
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mica.storage.dr_replay_drill import (
    STORAGE_DR_REPLAY_DRILL_SCHEMA_ID,
    BackupRecord,
    DRReplayDrill,
    DrillReceipt,
    get_dr_replay_drill,
)
from mica.storage.schema_versioning import (
    DestructiveMigrationError,
    FieldSpec,
    MigrationPolicy,
    MigrationReceipt,
    SchemaRegistryEntry,
    StorageSchemaRegistry,
    apply_additive_migration,
    dual_read_artifact,
    get_storage_schema_registry,
    is_destructive_migration,
    plan_migration,
    try_migrate,
)
from mica.storage.v1_closure import (
    STORAGE_V1_CLOSURE_SCHEMA_ID,
    STORAGE_V1_FREEZE_SCHEMA_ID,
    StorageV1Closure,
    StorageV1ClosureBuilder,
    StorageV1Freeze,
    get_storage_v1_builder,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/storage",
    tags=["storage-v1"],
)


def _get_registry() -> StorageSchemaRegistry:
    return get_storage_schema_registry()


def _get_drill() -> DRReplayDrill:
    return get_dr_replay_drill()


def _get_builder() -> StorageV1ClosureBuilder:
    return get_storage_v1_builder()


# ── F0 · Schema versioning ────────────────────────────────────────────────


@router.get("/schema")
async def get_schema_registry(
    registry: StorageSchemaRegistry = Depends(_get_registry),
):
    """Lista todos los schemas versionados registrados."""
    return {
        "targets": registry.all_targets(),
        "entries": [e.model_dump() for e in registry.all_entries()],
    }


@router.get("/schema/{target}")
async def get_schema_versions(
    target: str,
    registry: StorageSchemaRegistry = Depends(_get_registry),
):
    """Lista las versiones de un target."""
    return {
        "target": target,
        "versions": registry.list_versions(target),
    }


@router.get("/schema/{target}/{version}/fields")
async def get_schema_fields(
    target: str,
    version: str,
    registry: StorageSchemaRegistry = Depends(_get_registry),
):
    """Detalle de los campos de un target+version."""
    entry = registry.get(target, version)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Schema not found: {target} {version}",
        )
    return entry.model_dump()


class DualReadRequest(BaseModel):
    target: str
    raw: Dict[str, Any]


@router.post("/schema/dual-read")
async def dual_read(
    body: DualReadRequest,
    registry: StorageSchemaRegistry = Depends(_get_registry),
):
    """S-R4-F0 · Dual-read: lee vN y vN-1 transparentemente."""
    try:
        result = dual_read_artifact(body.raw, body.target, registry)
        return {"target": body.target, "data": result, "ok": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class MigrateRequest(BaseModel):
    target: str
    raw: Dict[str, Any]
    to_version: str = "v1"


@router.post("/schema/migrate")
async def try_migrate_endpoint(
    body: MigrateRequest,
    registry: StorageSchemaRegistry = Depends(_get_registry),
):
    """S-R4-F0 · Intenta una migración. Destructiva = BLOCKED."""
    to_entry = registry.get(body.target, body.to_version)
    if to_entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Target version not found: {body.target} {body.to_version}",
        )
    try:
        new_raw, receipt = try_migrate(body.raw, to_entry)
        return {
            "target": body.target,
            "data": new_raw,
            "migration_receipt": receipt.model_dump(),
            "ok": True,
        }
    except DestructiveMigrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── F1 · DR replay drill ────────────────────────────────────────────────


class ReplayDrillRequest(BaseModel):
    backup_path: str = "backup://storage/2026_07_01"


@router.post("/dr/replay-drill", response_model=Dict[str, Any])
async def run_replay_drill(
    body: ReplayDrillRequest = ReplayDrillRequest(),
    drill: DRReplayDrill = Depends(_get_drill),
):
    backup = drill.snapshot(backup_path=body.backup_path)
    receipt = drill.replay(backup=backup)
    return receipt.model_dump()


@router.get("/dr/metrics")
async def dr_metrics(
    drill: DRReplayDrill = Depends(_get_drill),
):
    """Métricas del DR replay drill."""
    return drill.metrics()


# ── F2 · Storage v1 closure ──────────────────────────────────────────────


class ClosureRequest(BaseModel):
    test_count: int = 0
    test_passed: int = 0
    test_failed: int = 0


@router.get("/v1/closure", response_model=Dict[str, Any])
async def get_v1_closure(
    builder: StorageV1ClosureBuilder = Depends(_get_builder),
):
    """S-R4-F2 · Storage v1 closure (suma auditable de R1..R4)."""
    closure = builder.get_closure("v1")
    if closure is None:
        # Construir uno con defaults (sin tests, sin drill)
        closure = builder.build_closure()
    return closure.model_dump()


@router.post("/v1/closure/build", response_model=Dict[str, Any])
async def build_v1_closure(
    body: ClosureRequest = ClosureRequest(),
    builder: StorageV1ClosureBuilder = Depends(_get_builder),
):
    """S-R4-F2 · Construye el closure v1 con la suite verde como precondicion."""
    closure = builder.build_closure(
        test_count=body.test_count,
        test_passed=body.test_passed,
        test_failed=body.test_failed,
    )
    return closure.model_dump()


@router.get("/v1/freeze")
async def get_v1_freeze(
    builder: StorageV1ClosureBuilder = Depends(_get_builder),
):
    """S-R4-F3 · Estado del freeze de la superficie v1."""
    closure = builder.get_closure("v1")
    if closure is None or closure.freeze is None:
        raise HTTPException(
            status_code=404,
            detail="No freeze found. Build the v1 closure first.",
        )
    return closure.freeze.model_dump()

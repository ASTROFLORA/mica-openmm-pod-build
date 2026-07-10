from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency


router = APIRouter(prefix="/api/v1/presenta", tags=["presenta"], dependencies=[Depends(user_dependency)])


class PresentaBlockProvenanceModel(BaseModel):
    kind: Literal[
        "ai-diagram",
        "asset-url",
        "legacy-svg-image",
        "workspace-asset",
        "presenta-deck-artifact",
        "manual",
    ]
    importedAt: str
    importedBy: Literal["toolbar", "inspector", "canvas", "api", "artifact-loader"]
    prompt: Optional[str] = None
    diagramType: Optional[str] = None
    sourceUrl: Optional[str] = None
    runId: Optional[str] = None
    assetId: Optional[str] = None
    artifactId: Optional[str] = None
    title: Optional[str] = None
    note: Optional[str] = None


class PresentaMutationReceiptModel(BaseModel):
    id: str
    timestamp: str
    action: Literal[
        "insert-svg-scene",
        "convert-image-to-svg-scene",
        "translate-svg-scene-element",
        "load-presenta-deck-artifact",
    ]
    actor: Literal["toolbar", "inspector", "canvas", "api", "artifact-loader"]
    status: Literal["applied"]
    summary: str
    slideId: Optional[str] = None
    blockId: Optional[str] = None
    elementId: Optional[str] = None
    source: Optional[PresentaBlockProvenanceModel] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class PresentaAuditTrailModel(BaseModel):
    schemaVersion: Literal["mica.presenta_trace.v1"]
    receipts: List[PresentaMutationReceiptModel] = Field(default_factory=list)
    updatedAt: str


class PresentaDeckGeneratedFromModel(BaseModel):
    presentation_id: str
    title: str
    slide_count: int = Field(ge=0)
    block_count: int = Field(ge=0)


class PresentaDeckArtifactModel(BaseModel):
    artifact_id: str
    artifact_type: Literal["presenta_deck"]
    schema_version: Literal["mica.presenta_deck.v1"]
    created_at: str
    updated_at: str
    source_run_id: Optional[str] = None
    generated_from: PresentaDeckGeneratedFromModel
    provenance_refs: List[PresentaBlockProvenanceModel] = Field(default_factory=list)
    receipts: List[PresentaMutationReceiptModel] = Field(default_factory=list)
    presentation: Dict[str, Any]


@router.post("/deck/validate")
async def validate_presenta_deck(payload: PresentaDeckArtifactModel) -> Dict[str, Any]:
    return {
        "ok": True,
        "artifact": payload.model_dump(),
        "summary": {
            "artifact_id": payload.artifact_id,
            "slide_count": payload.generated_from.slide_count,
            "block_count": payload.generated_from.block_count,
            "receipt_count": len(payload.receipts),
            "provenance_count": len(payload.provenance_refs),
        },
    }
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mica.api_v1.auth import user_dependency
from mica.schemas.operator_directive import (
    OperatorDirective,
    build_operator_directive_prompt,
    operator_directive_artifact_paths,
)
from mica.storage.gcs_user_storage import get_storage_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/operator-directives", tags=["operator-directives"])


class PublishOperatorDirectiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    directive_id: str = Field(..., min_length=1)
    artifact_paths: Dict[str, str]
    gcs_uris: Dict[str, str]
    prompt_markdown: str
    bucket_name: str = ""


def publish_operator_directive_artifacts(*, directive: OperatorDirective, user_id: str) -> PublishOperatorDirectiveResponse:
    storage = get_storage_manager()
    bucket_info = storage.ensure_bucket(user_id)
    artifact_paths = operator_directive_artifact_paths(directive)
    directive_json = json.dumps(directive.to_json_payload(), indent=2, ensure_ascii=False)
    prompt_markdown = build_operator_directive_prompt(directive)

    metadata = {
        "directive_id": directive.directive_id,
        "lane_id": directive.lane_id,
        "route_card_id": directive.route_card_id,
        "closure_state": directive.closure_state,
        "artifact_kind": "operator_directive",
    }

    directive_uri = storage.upload_text(
        user_id=user_id,
        object_path=artifact_paths["directive"],
        text=directive_json,
        content_type="application/json; charset=utf-8",
        metadata=metadata,
    )
    prompt_uri = storage.upload_text(
        user_id=user_id,
        object_path=artifact_paths["prompt"],
        text=prompt_markdown,
        content_type="text/markdown; charset=utf-8",
        metadata={**metadata, "artifact_kind": "operator_directive_prompt"},
    )

    logger.info(
        "Published operator directive %s to %s and %s",
        directive.directive_id,
        artifact_paths["directive"],
        artifact_paths["prompt"],
    )

    return PublishOperatorDirectiveResponse(
        directive_id=directive.directive_id,
        artifact_paths=artifact_paths,
        gcs_uris={"directive": directive_uri, "prompt": prompt_uri},
        prompt_markdown=prompt_markdown,
        bucket_name=bucket_info.bucket_name,
    )


@router.post("/publish", response_model=PublishOperatorDirectiveResponse)
def publish_operator_directive(
    directive: OperatorDirective,
    user_id: str = Depends(user_dependency),
) -> PublishOperatorDirectiveResponse:
    try:
        return publish_operator_directive_artifacts(directive=directive, user_id=user_id)
    except Exception as exc:
        logger.exception("publish_operator_directive failed for %s", directive.directive_id)
        raise HTTPException(status_code=502, detail=f"Failed to publish operator directive: {exc}") from exc
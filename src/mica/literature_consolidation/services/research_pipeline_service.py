from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ResearchPipelineExecutionRequest(BaseModel):
    query: str = Field(...)
    entities: List[str] = Field(default_factory=list)
    pdb_ids: List[str] = Field(default_factory=list)
    dlm_preset: str = "standard"
    lmp_preset: str = "structural"
    generate_report: bool = True
    session_id: Optional[str] = None
    user_id: str = "agent"


async def run_research_pipeline(payload: ResearchPipelineExecutionRequest) -> Dict[str, Any]:
    from mica.pipelines.research_orchestrator import PipelineConfig, ResearchOrchestrator

    config = PipelineConfig(
        query=payload.query,
        entities=payload.entities,
        pdb_ids=payload.pdb_ids,
        dlm_preset=payload.dlm_preset,
        lmp_preset=payload.lmp_preset,
        generate_report=payload.generate_report,
    )

    orchestrator = ResearchOrchestrator(
        session_id=payload.session_id,
        user_id=payload.user_id,
    )
    result = await orchestrator.run(config)
    return result.to_dict()

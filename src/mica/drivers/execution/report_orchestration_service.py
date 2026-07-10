"""Research report orchestration helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Dict


async def run_report_orchestration_branch(
    *,
    name: str,
    args: Dict[str, Any],
    user_id: str,
) -> str:
    try:
        from mica.pipelines.research_orchestrator import PipelineConfig, ResearchOrchestrator

        orchestrator = ResearchOrchestrator(session_id=None, user_id=user_id)
        result = await orchestrator.run(
            PipelineConfig(
                query=args.get("query", ""),
                entities=[
                    entity.strip()
                    for entity in args.get("entities", "").split(",")
                    if entity.strip()
                ],
                pdb_ids=[],
                generate_report=(name == "generate_report"),
            )
        )
        return json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

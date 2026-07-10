"""ATOM facts helpers extracted from AgenticDriver loop executor."""

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional


async def run_atom_facts_branch(
    *,
    args: Dict[str, Any],
    session_id: Optional[str],
    user_id: str,
    workspace_id_getter_fn: Callable[[], Optional[str]],
    build_runtime_consumption_context_fn: Callable[..., Awaitable[Dict[str, Any]]],
) -> str:
    try:
        query_text = str(
            args.get("subject", "") or args.get("entity", "") or args.get("predicate", "")
        ).strip()
        if not query_text:
            return json.dumps({"error": "Missing subject/entity/predicate"}, ensure_ascii=False)

        current_workspace_id = workspace_id_getter_fn()
        runtime_context = await build_runtime_consumption_context_fn(
            query=query_text,
            session_id=session_id,
            user_id=user_id,
            workspace_id=current_workspace_id,
            limit=max(1, int(args.get("limit", 5) or 5)),
            force=True,
        )
        response = {
            "status": "ok" if str(runtime_context.get("state") or "") != "degraded" else "degraded",
            "query": query_text,
            "facts": list(runtime_context.get("atom_facts") or []),
            "graph_facts": list(runtime_context.get("graph_facts") or []),
            "graph_hits": list(runtime_context.get("edge_hits") or []),
            "provenance": {
                "graph_hit_count": int(runtime_context.get("graph_hit_count") or 0),
                "fact_hit_count": int(runtime_context.get("fact_hit_count") or 0),
                "degraded": list(runtime_context.get("degraded") or []),
                "consumption_closure": dict(runtime_context.get("consumption_closure") or {}),
            },
        }
        if not response["facts"] and not response["graph_facts"]:
            from mica.memory.dlm.encoder import DLMEncoder

            encoder = DLMEncoder()
            degraded_result = await asyncio.to_thread(encoder.encode, query_text)
            response["status"] = "degraded"
            response["fallback_extraction"] = degraded_result
            response["provenance"]["degraded"] = list(
                dict.fromkeys(
                    list(response["provenance"].get("degraded") or []) + ["encoder_fallback_used"]
                )
            )
        return json.dumps(response, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

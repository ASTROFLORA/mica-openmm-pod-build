"""Agentic loop result assembly extracted from AgenticDriver."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from .helpers import _truncate_text


async def execute_with_agentic_loop(
    driver_self: Any,
    user_query: str,
    mode: str,
    session_id: Optional[str],
    provider_id: str = "anthropic",
    model_id: Optional[str] = None,
    reinjection_packet: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    session_id = session_id or str(uuid.uuid4())

    if driver_self._should_use_direct_structure_path(user_query):
        return await driver_self._execute_direct_structure_request(
            user_query=user_query,
            session_id=session_id,
        )

    final_text_parts: List[str] = []
    tool_uses: Dict[str, int] = {}
    errors: List[str] = []
    cost_usd = 0.0
    iterations = 0
    native_sources: Dict[str, Dict[str, Any]] = {}
    native_claims: List[Dict[str, Any]] = []

    async for event in driver_self.run_streaming(
        query=user_query,
        provider_id=provider_id,
        model_id=model_id,
        session_id=session_id,
        reinjection_packet=reinjection_packet,
    ):
        cls = type(event).__name__
        if cls == "TextDelta":
            final_text_parts.append(getattr(event, "text", ""))
        elif cls == "StepFinish":
            iterations = getattr(event, "step", iterations)
            cost_usd += float(getattr(event, "cost_usd", 0.0) or 0.0)
        elif cls == "ToolCallStart":
            name = getattr(event, "name", "unknown")
            tool_uses[name] = tool_uses.get(name, 0) + 1
        elif cls == "SideData":
            payload = getattr(event, "payload", {}) or {}
            side_claims, side_sources = driver_self._extract_native_evidence_from_side_data(
                agent=str(getattr(event, "agent", "driver") or "driver"),
                channel=str(getattr(event, "channel", "unknown") or "unknown"),
                payload=payload if isinstance(payload, dict) else {},
            )
            native_claims.extend(side_claims)
            for source in side_sources:
                source_id = str(source.get("source_id") or "").strip()
                if source_id:
                    native_sources[source_id] = source
        elif cls == "AgentTurn":
            role = str(getattr(event, "role", "") or "")
            text = str(getattr(event, "text", "") or "")
            agent = str(getattr(event, "agent", "subagent") or "subagent")
            if role == "done" and text.strip():
                source_ids: List[str] = []
                for source in driver_self._extract_sources_from_text(text):
                    source_id = str(source.get("source_id") or "").strip()
                    if source_id:
                        native_sources[source_id] = source
                        source_ids.append(source_id)
                native_claims.append(
                    {
                        "claim_id": f"{agent}-done-{len(native_claims)+1}",
                        "section": agent,
                        "text": _truncate_text(text.strip(), max_len=1200),
                        "strength": "supported" if source_ids else "suggestive",
                        "confidence": 0.72 if source_ids else 0.45,
                        "source_ids": source_ids,
                        "counterevidence_ids": [],
                    }
                )
        elif cls == "Error":
            errors.append(getattr(event, "message", str(event)))

    final_answer = "".join(final_text_parts).strip()
    final_summary = final_answer or f"MICA processed the request: {user_query}."
    pipeline_output = getattr(driver_self, "_latest_stream_pipeline_outputs", {}).pop(session_id, None)

    result = {
        "session_id": session_id,
        "final_result": {
            "summary": final_summary,
            "answer": final_answer or final_summary,
            "claims": native_claims,
            "sources": list(native_sources.values()),
            "artifacts": [],
            "paper": {
                "abstract": final_summary,
                "background": f"Question addressed: {user_query}",
                "methods": "AgenticLoop fallback transport with streamed tool and side-data evidence capture.",
                "findings": [],
                "limitations": [],
                "next_steps": [],
                "references": list(native_sources.values()),
            },
        },
        "lab_reports": [],
        "quality_score": 0.0,
        "quality_metrics": {},
        "peer_feedback": [],
        "provenance": {
            "iterations": iterations,
            "converged": not bool(errors),
            "tool_uses": tool_uses,
            "logs": [],
            "errors": errors,
        },
        "cost_usd": round(cost_usd, 6),
        "runtime": {
            "transport_path": "agentic_loop_fallback",
            "forced_reinjection": bool(reinjection_packet),
            "hot_loop_reinjection_packet_id": str((reinjection_packet or {}).get("packet_id") or ""),
        },
    }
    driver_self._attach_runtime_skills(result=result, session_id=session_id)
    if pipeline_output is not None:
        driver_self._attach_pipeline_output(result=result, pipeline_output=pipeline_output)
    return result
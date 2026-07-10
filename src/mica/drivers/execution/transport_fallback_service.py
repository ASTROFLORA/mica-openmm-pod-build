"""Fallback transport execution helpers extracted from AgenticDriver."""

import json
from typing import Any, Callable, Dict, Optional


async def run_fallback_transport_execution(
    *,
    worker: str,
    prompt: str,
    specialist_drivers: Dict[str, Any],
    bridge_obj: Any,
    thermodynamic_routing_service_obj: Any,
    biorouter_obj: Any,
    config_obj: Any,
    execute_with_mcp_fn: Callable[[str, Any], Any],
    redact_text_fn: Callable[[str], str],
    truncate_text_fn: Callable[[str], str],
    redact_obj_fn: Callable[[Any], Any],
    emit_event_fn: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    # Specialist drivers
    if worker in specialist_drivers:
        try:
            enriched_context: Dict[str, Any] = {"worker": worker}
            if worker == "biodynamo" and emit_event_fn is not None:
                def _emit_remote_md_progress(patch: Dict[str, Any]) -> None:
                    payload = dict(patch or {})
                    payload.setdefault("worker", worker)
                    emit_event_fn(
                        event_type="RemoteMDProgress",
                        node_id="execute",
                        workflow_id=None,
                        data=payload,
                    )

                enriched_context["_remote_md_registry_event_sink"] = _emit_remote_md_progress

            if bridge_obj is not None:
                try:
                    extracted = bridge_obj.extract_entities(prompt)
                    bio_hints: Dict[str, Any] = {}
                    if extracted.uniprot_ids:
                        bio_hints["uniprot_ids"] = list(extracted.uniprot_ids)
                    if extracted.gene_names:
                        bio_hints["gene_names"] = list(extracted.gene_names)
                    if extracted.protein_names:
                        bio_hints["protein_names"] = list(extracted.protein_names)
                    if bio_hints:
                        enriched_context["biological_hints"] = bio_hints
                except Exception:
                    pass

            result = await specialist_drivers[worker].execute(
                query=prompt,
                context=enriched_context,
                thermodynamic_context=thermodynamic_routing_service_obj.get_thermodynamic_snapshot(
                    prompt,
                    biorouter=biorouter_obj,
                    config=config_obj,
                ),
            )
            answer = str(result.get("answer", ""))
            confidence = result.get("confidence", 0.8)
            return {
                "status": "SUCCESS",
                "worker": worker,
                "backend_type": "specialist",
                "response": truncate_text_fn(redact_text_fn(answer), max_len=4000),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.8,
                "data": {
                    "answer": truncate_text_fn(redact_text_fn(answer), max_len=4000),
                    "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.8,
                },
            }
        except Exception as exc:
            return {
                "status": "FAILED",
                "worker": worker,
                "backend_type": "specialist",
                "error": str(exc),
                "errors": [str(exc)],
                "data": {},
            }

    mcp_worker = worker if worker.startswith("mcp_") else f"mcp_{worker}"
    if bool(getattr(config_obj, "mcp_enabled", False)):
        try:
            from mica.drivers.agentic_types import AgenticSession

            session = AgenticSession(user_query=prompt)
            mcp_result = await execute_with_mcp_fn(mcp_worker, session)
            safe = {k: v for k, v in (mcp_result or {}).items() if k not in {"args"}}
            return {
                "status": "SUCCESS" if safe.get("status") == "success" else "FAILED",
                "worker": worker,
                "backend_type": "mcp",
                "response": truncate_text_fn(redact_text_fn(str(safe.get("message") or "")), max_len=2000),
                "data": {"mcp": redact_obj_fn(safe)},
                "errors": [] if safe.get("status") == "success" else [str(safe.get("message") or "MCP failed")],
            }
        except Exception:
            pass

    # Backend-native hard path for tools that can be executed over local API even
    # when specialist and MCP backends are unavailable.
    if worker == "run_dlm_graph_repair_export":
        payload: Dict[str, Any] = {}
        try:
            parsed = json.loads(prompt) if isinstance(prompt, str) else None
            if isinstance(parsed, dict):
                if isinstance(parsed.get("args"), dict):
                    payload = dict(parsed.get("args") or {})
                else:
                    payload = {
                        key: value
                        for key, value in parsed.items()
                        if key not in {"worker", "prompt", "query"}
                    }
        except Exception:
            payload = {}

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "http://localhost:8080/api/v1/dlm/graph-repair/export",
                    json=payload,
                    headers={"X-User-Id": "agent_cli"},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return {
                            "status": "FAILED",
                            "worker": worker,
                            "backend_type": "backend_native_api",
                            "error": f"HTTP {resp.status}",
                            "errors": [f"HTTP {resp.status}"],
                            "data": {"body": text[:2000]},
                        }
                    return {
                        "status": "SUCCESS",
                        "worker": worker,
                        "backend_type": "backend_native_api",
                        "response": truncate_text_fn(redact_text_fn(text), max_len=4000),
                        "data": {"raw": text[:20000]},
                    }
        except Exception as exc:
            return {
                "status": "FAILED",
                "worker": worker,
                "backend_type": "backend_native_api",
                "error": str(exc),
                "errors": [str(exc)],
                "data": {},
            }

    return {
        "status": "FAILED",
        "worker": worker,
        "backend_type": "fallback",
        "error": f"No backend available for worker '{worker}'",
        "errors": [f"No backend available for worker '{worker}'"],
        "data": {},
    }

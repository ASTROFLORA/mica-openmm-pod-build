"""Hypothesis and preset helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Callable, Dict


async def run_list_dlm_presets_branch(
    *,
    name: str,
    args: Dict[str, Any],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    try:
        from mica.memory.dlm.presets import list_dlm_presets

        return json.dumps({"presets": list_dlm_presets()}, ensure_ascii=False, default=str)
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Could not load local DLM presets; returning degraded status instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )


async def run_generate_hypotheses_branch(
    *,
    name: str,
    args: Dict[str, Any],
    coerce_seed_entities_fn: Callable[[Dict[str, Any]], Any],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    query_text, seeds = coerce_seed_entities_fn(args)
    if not query_text:
        return degraded_tool_response_fn(
            name,
            "No query or entities were provided for hypothesis generation.",
            args_payload=args,
        )
    try:
        from mica.memory.dlm.hypothesis_generator import HypothesisGenerator

        gen = HypothesisGenerator()
        hyps = await gen.generate(
            seed_entities=seeds,
            max_hypotheses=int(args.get("max_hypotheses", 10)),
        )
        out = [
            ({
                "hypothesis": item.get("explanation") or f"{item.get('entity_a')} ↔ {item.get('entity_b')}",
                "confidence": item.get("score"),
                "entity_a": item.get("entity_a"),
                "entity_b": item.get("entity_b"),
                "intermediaries": item.get("intermediaries", []),
            } if isinstance(item, dict) else {"hypothesis": str(item)})
            for item in [h.to_dict() if hasattr(h, "to_dict") else h for h in (hyps or [])]
        ]
        return json.dumps({
            "count": len(out),
            "query": query_text,
            "seed_entities": seeds,
            "hypotheses": out,
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
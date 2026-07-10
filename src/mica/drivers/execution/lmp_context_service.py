"""LMP/KG canonical context helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Awaitable, Callable, Dict


async def run_lmp_context_branch(
    *,
    name: str,
    args: Dict[str, Any],
    fallback_transport_execution_fn: Callable[[str, str], Awaitable[Dict[str, Any]]],
    transport_payload_or_degraded_fn: Callable[..., str],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    try:
        if name == "list_lmp_presets":
            from bsm.lmp.presets import PRESET_REGISTRY

            presets_info = [
                {
                    "name": preset.name,
                    "description": preset.description,
                    "blocks": {
                        key.replace("include_", ""): getattr(preset, key)
                        for key in (
                            "include_identity",
                            "include_nesy_grammar",
                            "include_semantics",
                            "include_geometry",
                            "include_features",
                            "include_knowledge_graph",
                            "include_trajectory_ifp",
                        )
                    },
                }
                for preset in PRESET_REGISTRY.values()
            ]
            return json.dumps({"presets": presets_info}, ensure_ascii=False)

        if name == "load_knowledge_graph":
            pdb_id = args.get("pdb_id", "")
            from mica.drivers.dlm_lmp_bridge import get_bridge

            bridge = get_bridge()
            biological_context = bridge.get_biological_context(pdb_id)
            if biological_context:
                return json.dumps(
                    {
                        "source": "lmp_local",
                        "protein": pdb_id,
                        "context": biological_context.to_compact_dict()
                        if hasattr(biological_context, "to_compact_dict")
                        else str(biological_context),
                    },
                    ensure_ascii=False,
                    default=str,
                )

        result = await fallback_transport_execution_fn(name, json.dumps(args))
        return transport_payload_or_degraded_fn(name, result, args_payload=args)
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "LMP tool degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )

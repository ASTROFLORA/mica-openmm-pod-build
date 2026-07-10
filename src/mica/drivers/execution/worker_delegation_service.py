"""Worker delegation helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Awaitable, Callable, Dict


async def run_execute_worker_branch(
    *,
    name: str,
    args: Dict[str, Any],
    fallback_transport_execution_fn: Callable[[str, str], Awaitable[Dict[str, Any]]],
    transport_payload_or_degraded_fn: Callable[..., str],
) -> str:
    worker = args.get("worker", "biodynamo")
    prompt = args.get("prompt", args.get("query", json.dumps(args)))
    result = await fallback_transport_execution_fn(worker, prompt)
    return transport_payload_or_degraded_fn(name, result, args_payload=args)

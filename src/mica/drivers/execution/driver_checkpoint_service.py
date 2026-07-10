"""Driver-owned checkpoint helpers extracted from AgenticDriver loop executor."""

import inspect
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict


def _supported_checkpoint_kwargs(
    checkpoint_fn: Callable[..., Awaitable[Dict[str, Any]]],
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        signature = inspect.signature(checkpoint_fn)
    except (TypeError, ValueError):
        return kwargs

    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return kwargs

    return {
        name: value
        for name, value in kwargs.items()
        if name in signature.parameters
    }


async def _invoke_checkpoint_fn(
    checkpoint_fn: Callable[..., Awaitable[Dict[str, Any]]],
    **kwargs: Any,
) -> Dict[str, Any]:
    return await checkpoint_fn(**_supported_checkpoint_kwargs(checkpoint_fn, kwargs))


async def run_driver_delegated_checkpoint_branch(
    *,
    name: str,
    args: Dict[str, Any],
    run_driver_owned_delegated_checkpoint_fn: Callable[..., Awaitable[Dict[str, Any]]],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    try:
        checkpoint = await _invoke_checkpoint_fn(
            run_driver_owned_delegated_checkpoint_fn,
            workspace_root=Path(str(args.get("workspace_root") or os.getcwd())),
            objective=str(
                args.get("objective")
                or "Prove driver-owned delegated modify+test on a disposable probe."
            ),
            probe_name=str(args.get("probe_name") or "delegated_probe"),
            initial_value=int(args.get("initial_value", 1)),
            updated_value=int(args.get("updated_value", 2)),
            apply_same_diff=args.get("apply_same_diff", False),
            target_relative_path=(
                str(args.get("target_relative_path"))
                if args.get("target_relative_path") not in (None, "")
                else None
            ),
            target_callable_name=(
                str(args.get("target_callable_name"))
                if args.get("target_callable_name") not in (None, "")
                else None
            ),
        )
        return json.dumps(checkpoint, ensure_ascii=False, default=str)
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Driver-owned delegated checkpoint degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )


async def run_driver_staging_deploy_checkpoint_branch(
    *,
    name: str,
    args: Dict[str, Any],
    run_driver_owned_staging_deploy_checkpoint_fn: Callable[..., Awaitable[Dict[str, Any]]],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    try:
        checkpoint = await _invoke_checkpoint_fn(
            run_driver_owned_staging_deploy_checkpoint_fn,
            workspace_root=Path(str(args.get("workspace_root") or os.getcwd())),
            objective=str(
                args.get("objective")
                or "Package, validate, and deploy the same driver-owned candidate root to staging."
            ),
            candidate_name=str(args.get("candidate_name") or "staging-candidate"),
            project_id=str(args.get("project_id") or ""),
            environment_id=str(args.get("environment_id") or ""),
            staging_service=str(args.get("staging_service") or "mica-driver-staging"),
            public_base_url=str(
                args.get("public_base_url")
                or "https://mica-driver-staging-production.up.railway.app"
            ),
            readiness_url=(
                str(args.get("readiness_url")) if args.get("readiness_url") not in (None, "") else None
            ),
            py_compile_targets=args.get("py_compile_targets") or [],
            pytest_args=args.get("pytest_args") or [],
            deployment_patterns=args.get("deployment_patterns") or [],
            commit_sha=(str(args.get("commit_sha")) if args.get("commit_sha") not in (None, "") else None),
            max_wall_seconds=int(args.get("max_wall_seconds", 900)),
            dry_run=bool(args.get("dry_run", False)),
            cli_bin=(str(args.get("cli_bin")) if args.get("cli_bin") not in (None, "") else None),
            api_token=(str(args.get("api_token")) if args.get("api_token") not in (None, "") else None),
            upstream_checkpoint_result_path=(
                str(args.get("upstream_checkpoint_result_path"))
                if args.get("upstream_checkpoint_result_path") not in (None, "")
                else None
            ),
        )
        return json.dumps(checkpoint, ensure_ascii=False, default=str)
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Driver-owned staging deploy checkpoint degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )

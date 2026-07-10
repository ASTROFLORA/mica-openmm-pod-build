"""Driver self-experimentation helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Awaitable, Callable, Dict, Optional


async def run_driver_experiment_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    invoke_feed_tool_fn: Callable[[str, Dict[str, Any]], Awaitable[Any]],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    """Run hypothesis→experiment→insight cycle with feed testimony.
    
    Slice-3 §C1-C6: disposable Modal sandbox + allow-listed secrets +
    scrubbed readback + hypothesis→insight feed testimony.
    """
    hypothesis_text = str(args.get("hypothesis") or "").strip()
    cmd_preview = " ".join(str(x) for x in (args.get("command_argv") or []))[:200]
    parent_post_id: Optional[str] = None
    
    # 1. Publish hypothesis feed post BEFORE the experiment.
    try:
        hyp_post = await invoke_feed_tool_fn("publish_cue", {
            "post_type": "hypothesis",
            "topic": "experiment",
            "title": f"experiment: {hypothesis_text[:80]}",
            "body": json.dumps({
                "hypothesis": hypothesis_text,
                "command_argv_preview": cmd_preview,
                "git_sha": args.get("git_sha"),
                "secret_names_requested": list(args.get("secret_names") or []),
                "timeout_s": int(args.get("timeout_s") or 300),
            }, default=str),
        })
        if isinstance(hyp_post, dict):
            parent_post_id = hyp_post.get("id") or hyp_post.get("post_id")
    except Exception:  # noqa: BLE001
        pass

    # 2. Run the experiment.
    try:
        from mica.sandbox.driver_experiment import DriverExperimentRunner
        runner = DriverExperimentRunner.get(executor_obj)
        exp_res = await runner.run(
            hypothesis=hypothesis_text,
            command_argv=list(args.get("command_argv") or []),
            git_sha=args.get("git_sha"),
            secret_names=list(args.get("secret_names") or []),
            timeout_s=int(args.get("timeout_s") or 300),
            readback_paths=list(args.get("readback_paths") or []),
            session_id=args.get("session_id"),
            snapshot_on_pass=bool(args.get("snapshot_on_pass") or False),
            install_mica_deps=bool(args.get("install_mica_deps") or False),
            driver_session_id=getattr(executor_obj, "session_id", None)
                or getattr(executor_obj, "user_id", None),
            feed_hypothesis_post_id=parent_post_id,
        )
        res_payload = exp_res.to_dict()
    except Exception as exc:  # noqa: BLE001
        res_payload = {
            "verdict": "ambiguous",
            "error": f"{type(exc).__name__}: {exc}",
            "exit_code": -1,
        }

    # 3. Publish insight feed post AFTER, linked by parent_id.
    try:
        await invoke_feed_tool_fn("publish_cue", {
            "post_type": "insight",
            "topic": "experiment",
            "title": (
                f"result [{res_payload.get('verdict', '?')}] "
                f"exit={res_payload.get('exit_code', -1)}"
            ),
            "body": json.dumps({
                "parent_hypothesis_post_id": parent_post_id,
                "experiment_id": res_payload.get("experiment_id"),
                "verdict": res_payload.get("verdict"),
                "exit_code": res_payload.get("exit_code"),
                "duration_s": res_payload.get("duration_s"),
                "cost_estimate_usd": res_payload.get("cost_estimate_usd"),
                "sandbox_id": res_payload.get("sandbox_id"),
                "snapshot_image_id": res_payload.get("snapshot_image_id"),
                "stdout_tail": (res_payload.get("stdout") or "")[-2000:],
                "stderr_tail": (res_payload.get("stderr") or "")[-1000:],
                "readback_paths": res_payload.get("readback_paths") or [],
                "error": res_payload.get("error"),
            }, default=str),
            "parent_id": parent_post_id,
        })
    except Exception:  # noqa: BLE001
        pass

    return json.dumps(res_payload, ensure_ascii=False, default=str)


async def replay_experiment_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    """Slice-4 §10: deterministic re-run from Modal snapshot."""
    try:
        from mica.sandbox.driver_experiment import DriverExperimentRunner
        runner = DriverExperimentRunner.get(executor_obj)
        out = await runner.replay(str(args.get("experiment_id") or ""))
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        return degraded_tool_response_fn(
            name,
            f"Replay failed: {type(exc).__name__}",
            args_payload=args,
            extra={
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


async def get_experiment_quota_status_branch(
    *,
    name: str,
    args: Dict[str, Any],
    executor_obj: Any,
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    """Slice-4 §9: read-only quota status + cost tracking."""
    try:
        from mica.sandbox.driver_experiment import DriverExperimentRunner
        runner = DriverExperimentRunner.get(executor_obj)
        return json.dumps(runner.quota_status(args.get("session_id")))
    except Exception as exc:  # noqa: BLE001
        return degraded_tool_response_fn(
            name,
            f"Quota status unavailable: {type(exc).__name__}",
            args_payload=args,
            extra={
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

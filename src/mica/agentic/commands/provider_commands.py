"""provider_commands.py — real Command Kernel handlers for provider.* family."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from mica.sdk.command_contracts import BackendCommandEnvelope
from mica.serverless_models import ModelInvocationRequest, build_default_serverless_model_gateway
from mica.serverless_models.contracts import ServerlessModelDescriptor
from mica.serverless_models.provider_endpoint_control import (
    KillRequest,
    default_kill_ledger,
    default_registry,
    evaluate_zombies,
    execute_gated_kill,
    summarize_cost_bleed,
    sync_invocation_result_to_registry,
)
from mica.serverless_models.provider_capability_matrix import (
    capability_matrix_payload,
    select_capability_candidates,
)


def _kernel_blocked(*, code: str, message: str):
    from mica.agentic.command_kernel import _KernelBlocked

    raise _KernelBlocked(code=code, message=message)


def _provider_gateway_cache_key(kernel: Any, envelope: BackendCommandEnvelope) -> str:
    user_id = str(getattr(kernel, "user_id", "") or "anonymous").strip() or "anonymous"
    workspace_id = str(envelope.workspace_id or "global").strip() or "global"
    return f"{workspace_id}:{user_id}"


def _provider_gateway_artifact_root(kernel: Any, envelope: BackendCommandEnvelope) -> str:
    user_id = str(getattr(kernel, "user_id", "") or "anonymous").strip() or "anonymous"
    workspace_id = str(envelope.workspace_id or "global").strip() or "global"
    safe_user = (
        user_id.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace("*", "_")
        .replace("?", "_")
        .replace("\"", "_")
        .replace("<", "_")
        .replace(">", "_")
        .replace("|", "_")
    )
    safe_workspace = (
        workspace_id.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace("*", "_")
        .replace("?", "_")
        .replace("\"", "_")
        .replace("<", "_")
        .replace(">", "_")
        .replace("|", "_")
    )
    return str(Path(".artifacts/serverless_models/command_kernel") / safe_workspace / safe_user)


def _get_provider_gateway(kernel: Any, envelope: BackendCommandEnvelope):
    cache = getattr(kernel, "_provider_gateway_cache", None)
    if cache is None:
        cache = {}
        setattr(kernel, "_provider_gateway_cache", cache)
    cache_key = _provider_gateway_cache_key(kernel, envelope)
    gateway = cache.get(cache_key)
    if gateway is None:
        gateway = build_default_serverless_model_gateway(
            artifact_base_dir=_provider_gateway_artifact_root(kernel, envelope)
        )
        cache[cache_key] = gateway
    return gateway


def _provider_control_root_from_gateway(gateway: Any, kernel: Any, envelope: BackendCommandEnvelope) -> Path:
    result_store = getattr(gateway, "_result_store", None)
    base_dir = getattr(result_store, "_base_dir", None)
    if base_dir:
        return Path(base_dir).parent
    return Path(_provider_gateway_artifact_root(kernel, envelope))


def _provider_registry(kernel: Any, envelope: BackendCommandEnvelope, gateway: Any | None = None):
    runtime_gateway = gateway or _get_provider_gateway(kernel, envelope)
    return default_registry(_provider_control_root_from_gateway(runtime_gateway, kernel, envelope))


def _provider_kill_ledger(kernel: Any, envelope: BackendCommandEnvelope, gateway: Any | None = None):
    runtime_gateway = gateway or _get_provider_gateway(kernel, envelope)
    return default_kill_ledger(_provider_control_root_from_gateway(runtime_gateway, kernel, envelope))


def _provider_job_index(kernel: Any) -> dict[str, str]:
    index = getattr(kernel, "_provider_job_index", None)
    if index is None:
        index = {}
        setattr(kernel, "_provider_job_index", index)
    return index


def _request_index(kernel: Any) -> dict[str, str]:
    index = getattr(kernel, "_provider_request_index", None)
    if index is None:
        index = {}
        setattr(kernel, "_provider_request_index", index)
    return index


def _coerce_mapping(value: Any) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            _kernel_blocked(
                code="schema_validation_failed",
                message="Expected JSON object for provider command mapping argument.",
            )
        return dict(parsed)
    _kernel_blocked(
        code="schema_validation_failed",
        message="Expected object or JSON object string for provider command mapping argument.",
    )


def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    return []


def _provider_selection_args(args: Dict[str, Any]) -> Dict[str, Any]:
    selection_keys = {
        "provider",
        "gpu",
        "gpu_type",
        "region",
        "feature",
        "features",
        "serverless",
    }
    return {key: args[key] for key in selection_keys if key in args}


def _descriptor_matches_provider(descriptor: ServerlessModelDescriptor, provider_name: str) -> bool:
    return provider_name in list(descriptor.provider_preference or [])


def _cache_result_indices(kernel: Any, result: Any) -> None:
    request_id = str(getattr(result, "request_id", "") or "").strip()
    provider_job_id = str(getattr(result, "provider_job_id", "") or "").strip()
    if request_id:
        _request_index(kernel)[request_id] = request_id
    if provider_job_id and request_id:
        _provider_job_index(kernel)[provider_job_id] = request_id


def _lookup_request_id_by_provider_job_id(kernel: Any, gateway: Any, provider_job_id: str) -> str | None:
    cached = _provider_job_index(kernel).get(provider_job_id)
    if cached:
        return cached

    for request_id, result in dict(getattr(gateway, "_results", {})).items():
        if str(getattr(result, "provider_job_id", "") or "").strip() == provider_job_id:
            _provider_job_index(kernel)[provider_job_id] = request_id
            return request_id

    result_store = getattr(gateway, "_result_store", None)
    base_dir = getattr(result_store, "_base_dir", None)
    if base_dir:
        for candidate in Path(base_dir).glob("*.json"):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            result_payload = payload.get("result") or payload
            if str(result_payload.get("provider_job_id") or "").strip() == provider_job_id:
                request_id = str(result_payload.get("request_id") or "").strip()
                if request_id:
                    _provider_job_index(kernel)[provider_job_id] = request_id
                    return request_id
    return None


async def provider_select(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Select a provider and list its available models from the canonical gateway."""

    requested_provider = str(args.get("provider") or "").strip().lower()
    provider_name = requested_provider or "modal"
    gateway = _get_provider_gateway(kernel, envelope)
    provider_map = dict(getattr(gateway, "_providers", {}) or {})
    provider = provider_map.get(provider_name)
    requested_gpu = str(args.get("gpu") or args.get("gpu_type") or "").strip().lower()
    requested_region = str(args.get("region") or "").strip().lower()
    requested_features = []
    requested_features.extend(_string_list(args.get("features")))
    feature = str(args.get("feature") or "").strip()
    if feature:
        requested_features.append(feature)
    require_serverless = args.get("serverless")
    if isinstance(require_serverless, str):
        lowered = require_serverless.strip().lower()
        require_serverless = lowered in {"1", "true", "yes", "serverless"}

    descriptors = [
        descriptor
        for descriptor in gateway.list_models()
        if _descriptor_matches_provider(descriptor, provider_name)
    ]

    matched_capabilities, rejected_providers = select_capability_candidates(
        available_provider_names=set(provider_map.keys()),
        gpu=requested_gpu,
        region=requested_region,
        require_serverless=require_serverless if isinstance(require_serverless, bool) else None,
        required_features=requested_features,
    )

    selected_provider = provider_name
    fallback: list[str] = []
    status = "selected" if provider and provider.is_available() else "provider_unavailable"
    matched_summary = []
    selection_reason = "explicit_provider" if requested_provider else "default_provider"
    fallback_used = False
    if requested_gpu or requested_region or requested_features or isinstance(require_serverless, bool):
        ordered_capabilities = list(matched_capabilities)
        if requested_provider:
            ordered_capabilities.sort(key=lambda item: (item.provider != requested_provider, item.provider))
        if ordered_capabilities:
            chosen_index = None
            for index, capability in enumerate(ordered_capabilities):
                candidate_provider = provider_map.get(capability.provider)
                if candidate_provider and candidate_provider.is_available():
                    chosen_index = index
                    selected_provider = capability.provider
                    provider = candidate_provider
                    matched_summary = [capability.to_dict()]
                    break
                rejected_providers.append(
                    {"provider": capability.provider, "reasons": ["provider_unavailable"]}
                )
            if chosen_index is not None:
                fallback = [item.provider for item in ordered_capabilities[chosen_index + 1 :]]
                fallback_used = chosen_index > 0
                selection_reason = "ordered_fallback" if fallback_used else "capability_match"
                status = "selected"
            else:
                selected_provider = ""
                provider = None
                fallback = [item.provider for item in ordered_capabilities]
                fallback_used = bool(fallback)
                status = "all_providers_unavailable"
            descriptors = [
                descriptor
                for descriptor in gateway.list_models()
                if _descriptor_matches_provider(descriptor, selected_provider)
            ]
        else:
            selected_provider = ""
            status = "provider_capability_not_implemented"

    matrix = capability_matrix_payload()
    return {
        "summary": (
            f"Provider selection resolved to '{selected_provider or provider_name}' "
            f"with {len(descriptors)} matching models."
        ),
        "result": {
            "status": status,
            "provider": selected_provider or provider_name,
            "provider_available": bool(provider and provider.is_available()),
            "models": [descriptor.model_id for descriptor in descriptors],
            "selected": selected_provider or provider_name,
            "matched_capabilities": matched_summary,
            "fallback": fallback,
            "fallback_used": fallback_used,
            "rejected_providers": rejected_providers,
            "capability_matrix_schema": matrix["schema_id"],
            "reason": selection_reason,
        },
    }


async def provider_matrix(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    del args
    gateway = _get_provider_gateway(kernel, envelope)
    provider_map = dict(getattr(gateway, "_providers", {}) or {})
    matrix = capability_matrix_payload()
    available = []
    unavailable = []
    for capability in matrix["providers"]:
        provider_name = str(capability.get("provider") or "")
        runtime = provider_map.get(provider_name)
        item = dict(capability)
        item["registered"] = provider_name in provider_map
        item["available"] = bool(runtime and runtime.is_available())
        if item["available"]:
            available.append(item)
        else:
            unavailable.append(item)
    return {
        "summary": f"Provider capability matrix resolved with {len(available)} available and {len(unavailable)} unavailable providers.",
        "result": {
            "schema_id": matrix["schema_id"],
            "providers": available + unavailable,
            "available_providers": [item["provider"] for item in available],
            "unavailable_providers": [item["provider"] for item in unavailable],
        },
    }


async def provider_run_job(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Run a real provider-backed serverless invocation through the canonical gateway."""

    provider_name = str(args.get("provider") or "").strip().lower() or "modal"
    model_id = str(args.get("model_id") or "").strip()
    if not model_id:
        _kernel_blocked(code="missing_model_id", message="provider.run_job requires model_id.")

    quetzal_gate_ref = str(args.get("quetzal_gate_ref") or "").strip()
    budget_ref = str(args.get("budget_ref") or "").strip()
    if not quetzal_gate_ref:
        _kernel_blocked(
            code="missing_quetzal_gate_ref",
            message="provider.run_job requires quetzal_gate_ref before execution.",
        )
    if not budget_ref:
        _kernel_blocked(
            code="missing_budget_ref",
            message="provider.run_job requires budget_ref before execution.",
        )

    gateway = _get_provider_gateway(kernel, envelope)
    selection = await provider_select(kernel, _provider_selection_args(args), envelope)
    selection_result = dict(selection.get("result") or {})
    explicit_provider = str(args.get("provider") or "").strip().lower()
    if selection_result.get("status") == "provider_capability_not_implemented":
        return {
            "summary": "Provider job blocked before execution because no provider satisfies the requested capability contract.",
            "result": {
                "status": "blocked",
                "provider": explicit_provider or selection_result.get("provider") or "",
                "provider_job_id": "",
                "resolved_via": "serverless_model_gateway",
                "artifact_uris": [],
                "error": "No provider satisfies the requested capability contract.",
                "blocker_code": "provider_capability_not_implemented",
                "fallback_chain": list(selection_result.get("fallback") or []),
                "attempts": [],
            },
            "artifact_refs": [],
            "state_after": {
                "provider_job_id": "",
                "provider_request_id": "",
                "provider_job_state": "blocked",
                "blocker_code": "provider_capability_not_implemented",
            },
        }

    candidate_providers: list[str] = []
    selected_provider = str(selection_result.get("selected") or selection_result.get("provider") or "").strip()
    if selected_provider:
        candidate_providers.append(selected_provider)
    for candidate in list(selection_result.get("fallback") or []):
        normalized = str(candidate or "").strip().lower()
        if normalized and normalized not in candidate_providers:
            candidate_providers.append(normalized)
    if explicit_provider and explicit_provider not in candidate_providers:
        candidate_providers.insert(0, explicit_provider)
    if not candidate_providers:
        candidate_providers.append(explicit_provider or "modal")

    request_id = str(args.get("request_id") or f"provider-{uuid4().hex[:16]}").strip()
    inputs = _coerce_mapping(args.get("inputs"))
    metadata = _coerce_mapping(args.get("metadata"))
    metadata.update(
        {
            "workspace_id": envelope.workspace_id,
            "study_id": envelope.study_id,
            "quetzal_gate_ref": quetzal_gate_ref,
            "budget_ref": budget_ref,
        }
    )
    request = ModelInvocationRequest(
        request_id=request_id,
        model_id=model_id,
        user_id=str(getattr(kernel, "user_id", "") or "command_kernel"),
        session_id=str(envelope.session_id or f"provider-session-{request_id}"),
        run_id=str(args.get("run_id") or request_id),
        inputs=inputs,
        metadata=metadata,
        requested_by="command_kernel.provider.run_job",
        provider_override=candidate_providers[0],
    )
    attempts: list[dict[str, Any]] = []
    result = None
    blocker_code = ""
    surfaced_status = ""
    surfaced_blocker_code = ""
    for index, candidate_provider in enumerate(candidate_providers):
        candidate_request = ModelInvocationRequest(
            request_id=request.request_id,
            model_id=request.model_id,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=request.run_id,
            inputs=dict(request.inputs),
            input_files=dict(request.input_files),
            metadata=dict(request.metadata),
            requested_by=request.requested_by,
            provider_override=candidate_provider,
        )
        result = await gateway.invoke(candidate_request)
        _cache_result_indices(kernel, result)
        blocker_code = str(result.metrics.get("blocker_code") or "").strip()
        surfaced_status = "blocked" if blocker_code else result.state
        attempts.append(
            {
                "provider": candidate_provider,
                "status": surfaced_status,
                "blocker_code": blocker_code or None,
            }
        )
        if blocker_code == "provider_unavailable" and index + 1 < len(candidate_providers):
            continue
        if blocker_code == "provider_capability_not_implemented" and index + 1 < len(candidate_providers):
            continue
        break

    assert result is not None
    surfaced_blocker_code = blocker_code or None
    if len(candidate_providers) > 1 and blocker_code == "provider_unavailable" and all(
        str(item.get("blocker_code") or "") == "provider_unavailable" for item in attempts
    ):
        surfaced_blocker_code = "all_providers_unavailable"
        surfaced_status = "blocked"
    endpoint_record = sync_invocation_result_to_registry(
        _provider_registry(kernel, envelope, gateway),
        result,
        owning_run_ref=request.run_id,
        owning_run_state="terminated" if surfaced_status in {"completed", "failed", "cancelled", "blocked"} else "active",
        orphan_gpu_seconds=float(result.metrics.get("gpu_seconds") or 0.0),
    )
    return {
        "summary": (
            f"Provider job executed via canonical gateway: provider={result.provider} "
            f"model={result.model_id} state={surfaced_status}"
        ),
        "result": {
            "status": surfaced_status,
            "provider": result.provider,
            "model_id": result.model_id,
            "request_id": result.request_id,
            "provider_job_id": result.provider_job_id,
            "resolved_via": "serverless_model_gateway",
            "artifact_uris": list(result.artifact_uris),
            "error": result.error,
            "blocker_code": surfaced_blocker_code,
            "fallback_chain": candidate_providers,
            "attempts": attempts,
            "endpoint_ref": endpoint_record.endpoint_ref,
        },
        "artifact_refs": list(result.artifact_uris),
        "state_after": {
            "provider_job_id": result.provider_job_id,
            "provider_request_id": result.request_id,
            "provider_job_state": surfaced_status,
            "blocker_code": surfaced_blocker_code,
            "endpoint_ref": endpoint_record.endpoint_ref,
        },
    }


async def provider_job_status(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    """Query real provider job status via canonical request/result correlation."""

    gateway = _get_provider_gateway(kernel, envelope)
    request_id = str(args.get("request_id") or "").strip()
    provider_job_id = str(args.get("provider_job_id") or args.get("job_id") or "").strip()
    if not request_id and provider_job_id:
        request_id = _lookup_request_id_by_provider_job_id(kernel, gateway, provider_job_id) or ""
    if not request_id:
        _kernel_blocked(
            code="missing_request_correlation",
            message="provider.job.status requires request_id or a provider_job_id already correlated by this runtime.",
        )

    result = await gateway.get_result(
        request_id,
        user_id=str(getattr(kernel, "user_id", "") or "command_kernel"),
    )
    _cache_result_indices(kernel, result)
    endpoint_record = sync_invocation_result_to_registry(
        _provider_registry(kernel, envelope, gateway),
        result,
        owning_run_ref=str(result.request_id or ""),
        owning_run_state="terminated" if result.state in {"completed", "failed", "cancelled"} else "active",
        orphan_gpu_seconds=float(result.metrics.get("gpu_seconds") or 0.0),
    )
    return {
        "summary": f"Provider job {result.provider_job_id or request_id} status: {result.state}",
        "result": {
            "status": result.state,
            "provider": result.provider,
            "model_id": result.model_id,
            "request_id": result.request_id,
            "provider_job_id": result.provider_job_id,
            "artifact_uris": list(result.artifact_uris),
            "error": result.error,
            "endpoint_ref": endpoint_record.endpoint_ref,
        },
        "artifact_refs": list(result.artifact_uris),
        "state_after": {
            "provider_job_id": result.provider_job_id,
            "provider_request_id": result.request_id,
            "provider_job_state": result.state,
            "endpoint_ref": endpoint_record.endpoint_ref,
        },
    }


async def provider_endpoints(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    gateway = _get_provider_gateway(kernel, envelope)
    registry = _provider_registry(kernel, envelope, gateway)
    provider_filter = str(args.get("provider") or "").strip().lower()
    records = registry.list_records()
    if provider_filter:
        records = [record for record in records if record.provider == provider_filter]
    cost_bleed = summarize_cost_bleed(records)
    payload = [record.to_dict() for record in records]
    return {
        "summary": f"Provider endpoint registry returned {len(payload)} records.",
        "result": {
            "status": "ok",
            "endpoints": payload,
            "count": len(payload),
            "cost_bleed": cost_bleed,
        },
        "state_after": {
            "provider_endpoint_count": len(payload),
            "provider_cost_bleed_status": cost_bleed["status"],
        },
    }


async def provider_zombies(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    gateway = _get_provider_gateway(kernel, envelope)
    registry = _provider_registry(kernel, envelope, gateway)
    records = registry.list_records()
    provider_filter = str(args.get("provider") or "").strip().lower()
    if provider_filter:
        records = [record for record in records if record.provider == provider_filter]
    verdicts = evaluate_zombies(records)
    cost_bleed = summarize_cost_bleed(records)
    payload = [verdict.to_dict() for verdict in verdicts]
    return {
        "summary": f"Provider zombie scan returned {len(payload)} candidates.",
        "result": {
            "status": "ok",
            "zombies": payload,
            "count": len(payload),
            "cost_bleed": cost_bleed,
        },
        "state_after": {
            "provider_zombie_count": len(payload),
            "provider_cost_bleed_status": cost_bleed["status"],
        },
        "evidence_refs": [item["endpoint_ref"] for item in payload],
    }


async def provider_kill(
    kernel: Any,
    args: Dict[str, Any],
    envelope: BackendCommandEnvelope,
) -> Dict[str, Any]:
    gateway = _get_provider_gateway(kernel, envelope)
    registry = _provider_registry(kernel, envelope, gateway)
    ledger = _provider_kill_ledger(kernel, envelope, gateway)
    scope = str(args.get("scope") or "").strip().lower() or "endpoint"
    if scope not in {"endpoint", "provider", "global"}:
        _kernel_blocked(code="invalid_kill_scope", message="provider.kill scope must be endpoint, provider, or global.")
    target_ref = str(args.get("target_ref") or args.get("endpoint_ref") or args.get("provider") or "").strip()
    gate_ref = str(args.get("gate_ref") or args.get("quetzal_gate_ref") or "").strip()
    reason = str(args.get("reason") or "").strip() or "operator_requested"
    outcome = await execute_gated_kill(
        gateway=gateway,
        registry=registry,
        ledger=ledger,
        kill_request=KillRequest(scope=scope, target_ref=target_ref, gate_ref=gate_ref, reason=reason),
    )
    primary_audit_ref = str((outcome.get("audit_refs") or [""])[0] or "").strip()
    return {
        "summary": f"Provider kill scope={scope} status={outcome.get('status')}.",
        "result": {
            "status": outcome.get("status"),
            "reason": outcome.get("reason") or "",
            "scope": scope,
            "target_ref": target_ref,
            "teardown_confirmed": bool(outcome.get("status") == "killed"),
            "results": list(outcome.get("results") or []),
            "audit_ref": primary_audit_ref,
            "audit_refs": list(outcome.get("audit_refs") or []),
        },
        "state_after": {
            "provider_kill_scope": scope,
            "provider_kill_status": outcome.get("status"),
            "provider_kill_count": len(outcome.get("results") or []),
        },
        "evidence_refs": list(outcome.get("audit_refs") or []),
    }

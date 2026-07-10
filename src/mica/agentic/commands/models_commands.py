from __future__ import annotations

from typing import Any, Dict
from mica.serverless_models.registry import get_default_model_registry
from mica.quetzal.gates import QuetzalGate
from mica.provenance.contracts import ProvenanceWriter
from mica.serverless_models.gateway import ServerlessGateway


class DefaultCostModel:
    def estimate(self, model_ref: str, payload_in: Dict[str, Any]) -> float:
        return 0.05


class DefaultProvider:
    async def invoke(self, rev: Any, payload_in: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "artifact_ref": f"artifact://workspace/models/{rev.runtime.modal_app}/{rev.runtime.modal_function}",
            "sha256": "sha256:" + "a" * 64,
            "job_id": "job_default_1"
        }


_DEFAULT_SERVERLESS_GATEWAY = None


def get_default_serverless_gateway() -> ServerlessGateway:
    global _DEFAULT_SERVERLESS_GATEWAY
    if _DEFAULT_SERVERLESS_GATEWAY is None:
        _DEFAULT_SERVERLESS_GATEWAY = ServerlessGateway(
            registry=get_default_model_registry(),
            gate=QuetzalGate(),
            provider=DefaultProvider(),
            prov_writer=ProvenanceWriter(),
            cost_model=DefaultCostModel(),
        )
    return _DEFAULT_SERVERLESS_GATEWAY


async def models_invoke(kernel: Any, args: Dict[str, Any], envelope: Any) -> Dict[str, Any]:
    model_ref = str(args.get("model_ref") or "").strip()
    payload_in = args.get("payload_in") or {}
    budget_ceiling_usd = float(args.get("budget_ceiling_usd") or 1.0)
    actor_id = str(args.get("actor_id") or "agent:command_kernel").strip()

    gw = getattr(kernel, "_serverless_gateway", None) or get_default_serverless_gateway()
    res = await gw.invoke(
        model_ref=model_ref,
        workspace_id=envelope.workspace_id,
        payload_in=payload_in,
        budget_ceiling_usd=budget_ceiling_usd,
        actor_id=actor_id,
    )

    if res["status"] == "blocked":
        from mica.agentic.command_kernel import _KernelBlocked
        raise _KernelBlocked(
            code=res["reason_codes"][0] if res["reason_codes"] else "policy_blocked",
            message=f"Model invocation blocked by policy: {res.get('reason_codes')}"
        )

    return {
        "summary": f"models.invoke -> {res['status']}",
        "result": {
            "status": res["status"],
            "artifact_ref": res.get("artifact_ref"),
            "receipt_id": res.get("receipt_id"),
        },
        "state_after": {},
        "artifact_refs": [res["artifact_ref"]] if res.get("artifact_ref") else [],
        "resource_refs": [],
        "evidence_refs": [],
        "usd": 0.05,
        "tool_calls": 1,
    }

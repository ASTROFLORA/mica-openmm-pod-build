from __future__ import annotations
from typing import Any, Dict, List, Optional
from datetime import datetime

from mica.contracts.unified.types import (
    UnifiedTrustState,
    UnifiedVisibility,
    UnifiedTypedBlocker,
    UnifiedCodeIdentity,
    UnifiedTenancyContext,
    UnifiedSecretMountReceipt,
    UnifiedReceiptCore,
)

# Resilient imports from frozen lanes
try:
    from mica.provenance.receipts import ReceiptCore, GatePayload, ServerlessPayload
except ImportError:
    ReceiptCore = None
    GatePayload = None
    ServerlessPayload = None

try:
    from mica.context_steward.contracts import (
        TrustState as CSTrustState,
        ContextBlock as CSContextBlock,
        PromptAssemblyReceipt as CSPromptAssemblyReceipt,
        PermissionDecisionReceipt as CSPermissionDecisionReceipt,
    )
except ImportError:
    CSTrustState = None
    CSContextBlock = None
    CSPromptAssemblyReceipt = None
    CSPermissionDecisionReceipt = None


def to_unified_trust_state(state: str) -> UnifiedTrustState:
    """Map string representation of trust state to unified enum."""
    val = str(state).lower().strip()
    if val in ("source_canonical", "doctrine_versioned"):
        return UnifiedTrustState.SOURCE_CANONICAL
    if val in ("resolved_identity", "identity_minted"):
        return UnifiedTrustState.RESOLVED_IDENTITY
    if val in ("promoted_claim", "claim_promoted"):
        return UnifiedTrustState.PROMOTED_CLAIM
    if val in ("graph_receipted", "graph_active"):
        return UnifiedTrustState.GRAPH_RECEIPTED
    if val in ("execution_observed", "observed"):
        return UnifiedTrustState.EXECUTION_OBSERVED
    if val in ("static_derived", "derived"):
        return UnifiedTrustState.STATIC_DERIVED
    if val in ("projection", "projected"):
        return UnifiedTrustState.PROJECTION
    if val in ("retrieval_candidate", "retrieved"):
        return UnifiedTrustState.RETRIEVAL_CANDIDATE
    if val in ("local_uncommitted", "cs_local_trace_only", "bsm_local_trace_only"):
        return UnifiedTrustState.LOCAL_UNCOMMITTED
    if val in ("degraded", "execution_degraded"):
        return UnifiedTrustState.DEGRADED
    if val in ("missing", "not_found"):
        return UnifiedTrustState.MISSING
    if val in ("conflicting", "conflict"):
        return UnifiedTrustState.CONFLICTING
    
    return UnifiedTrustState.LOCAL_UNCOMMITTED


def to_unified_visibility(vis: str) -> UnifiedVisibility:
    """Map string representation of visibility to unified enum."""
    val = str(vis).lower().strip()
    if val == "sandbox_only":
        return UnifiedVisibility.SANDBOX_ONLY
    if val == "tenant_private":
        return UnifiedVisibility.TENANT_PRIVATE
    if val == "consortium_shared":
        return UnifiedVisibility.CONSORTIUM_SHARED
    if val == "mica_global":
        return UnifiedVisibility.MICA_GLOBAL
    if val == "external_public":
        return UnifiedVisibility.EXTERNAL_PUBLIC
    
    return UnifiedVisibility.SANDBOX_ONLY


def to_unified_blocker(blocker: Any) -> UnifiedTypedBlocker:
    """Map a lane blocker to a UnifiedTypedBlocker."""
    if not blocker:
        raise ValueError("Cannot map empty blocker")
    
    # Check if dict
    if isinstance(blocker, dict):
        return UnifiedTypedBlocker(
            blocker_code=blocker.get("blocker_code", "unknown_blocker"),
            classification=blocker.get("classification", "broken"),
            human_message=blocker.get("human_message", "No message provided"),
            safe_fallback=blocker.get("safe_fallback", "none"),
            production_ready=blocker.get("production_ready", False),
        )
    
    # Assume object with attributes
    return UnifiedTypedBlocker(
        blocker_code=getattr(blocker, "blocker_code", "unknown_blocker"),
        classification=getattr(blocker, "classification", "broken"),
        human_message=getattr(blocker, "human_message", "No message provided"),
        safe_fallback=getattr(blocker, "safe_fallback", "none"),
        production_ready=getattr(blocker, "production_ready", False),
    )


def to_unified_tenancy(context: Any) -> UnifiedTenancyContext:
    """Map any tenancy context (LOTenancyContext/RTenancyContext) to UnifiedTenancyContext."""
    if not context:
        raise ValueError("Cannot map empty tenancy context")
    
    if isinstance(context, dict):
        data = context
    else:
        # Pydantic or class object
        data = context.model_dump() if hasattr(context, "model_dump") else context.__dict__
    
    # Extract visibility ceiling
    ceiling_str = data.get("visibility_ceiling", "sandbox_only")
    ceiling = to_unified_visibility(ceiling_str)

    # Extract residency region
    residency = data.get("data_residency")
    residency_region = None
    if isinstance(residency, dict):
        residency_region = residency.get("region")
    elif residency is not None:
        residency_region = getattr(residency, "region", None)

    # Extract effective permissions
    perms = data.get("effective_permission", {})
    if not isinstance(perms, dict):
        perms = perms.model_dump() if hasattr(perms, "model_dump") else getattr(perms, "__dict__", {})

    return UnifiedTenancyContext(
        tenant_ref=data.get("tenant_ref", "unknown_tenant"),
        actor_ref=data.get("actor_ref", "unknown_actor"),
        owner_user_id=data.get("owner_user_id"),
        scope_ref=data.get("scope_ref", "unknown_scope"),
        permission_scope_ref=data.get("permission_scope_ref", "unknown_perm_scope"),
        scope_kind=data.get("scope_kind", "unknown_scope_kind"),
        visibility_ceiling=ceiling,
        provider_policy_ref=data.get("provider_policy_ref"),
        data_residency_region=residency_region,
        can_read=perms.get("can_read", False),
        can_write=perms.get("can_write", False),
        can_create_artifact=perms.get("can_create_artifact", False),
        can_run_sandbox=perms.get("can_run_sandbox", False),
        can_mount_secret=perms.get("can_mount_secret", False),
        can_deploy_model=perms.get("can_deploy_model", False),
        can_export=perms.get("can_export", False),
        can_promote_visibility=perms.get("can_promote_visibility", False),
        source_authority=data.get("source_authority", "conservative_shim"),
    )


def to_unified_receipt(receipt: Any) -> UnifiedReceiptCore:
    """Map any receipt (ReceiptCore, HarnessRunReceiptBundle, etc.) to UnifiedReceiptCore."""
    if not receipt:
        raise ValueError("Cannot map empty receipt")

    # If it is already a UnifiedReceiptCore, return it
    if isinstance(receipt, UnifiedReceiptCore):
        return receipt

    if isinstance(receipt, dict):
        data = receipt
    else:
        # Pydantic or standard object
        data = receipt.model_dump() if hasattr(receipt, "model_dump") else receipt.__dict__

    # 1. Detect if it is a standard ReceiptCore (P0)
    if "receipt_id" in data:
        # Extract inputs/outputs/artifacts/policies from refs object
        refs = data.get("refs") or {}
        if not isinstance(refs, dict):
            refs = refs.model_dump() if hasattr(refs, "model_dump") else getattr(refs, "__dict__", {})

        # Extract hashes
        hashes = data.get("hashes") or {}
        if not isinstance(hashes, dict):
            hashes = hashes.model_dump() if hasattr(hashes, "model_dump") else getattr(hashes, "__dict__", {})

        # Extract payload gate policy decision if it is a GatePayload
        policy_decision = None
        payload = data.get("payload")
        if payload:
            if isinstance(payload, dict):
                policy_decision = payload.get("decision")
            else:
                policy_decision = getattr(payload, "decision", None)

        def parse_dt(dt_str: Optional[str]) -> Optional[datetime]:
            if not dt_str:
                return None
            try:
                return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except Exception:
                return None

        return UnifiedReceiptCore(
            receipt_ref=data["receipt_id"],
            receipt_version=data.get("receipt_version", "v1"),
            kind=data.get("kind", "unknown"),
            execution_status=data.get("status", "ok"),
            policy_decision=policy_decision,
            workspace_ref=data.get("workspace_id"),
            tenant_ref=None,
            actor_ref=data.get("actor_id"),
            operation_name=data.get("operation_name", "unknown_operation"),
            input_refs=refs.get("input_refs", []),
            output_refs=refs.get("output_refs", []) or refs.get("artifact_refs", []),
            parent_receipt_refs=refs.get("parent_receipt_refs", []),
            policy_refs=refs.get("policy_refs", []),
            started_at=parse_dt(data.get("started_at")),
            ended_at=parse_dt(data.get("ended_at")),
            trace_ref=data.get("trace_id"),
        )

    # 2. Detect if it is a HarnessRunReceiptBundle (HN)
    elif "bundle_ref" in data:
        # Map Harness fields
        code_id_data = data.get("code_identity")
        code_identity = None
        if code_id_data:
            if not isinstance(code_id_data, dict):
                code_id_data = code_id_data.model_dump() if hasattr(code_id_data, "model_dump") else getattr(code_id_data, "__dict__", {})
            code_identity = UnifiedCodeIdentity(
                code_ref=code_id_data.get("code_ref", "unknown_code"),
                code_sha256=code_id_data.get("code_sha256", ""),
                repo_commit_sha=code_id_data.get("repo_commit_sha"),
                repo_diff_sha256=code_id_data.get("repo_diff_sha256"),
                lockfile_sha256=code_id_data.get("lockfile_sha256"),
                image_digest=code_id_data.get("image_digest"),
            )

        return UnifiedReceiptCore(
            receipt_ref=data["bundle_ref"],
            receipt_version="v1",
            kind="sandbox",
            execution_status=data.get("status", "completed"),
            policy_decision="allow" if data.get("secret_scan_passed", True) else "reject",
            workspace_ref=None,
            tenant_ref=None,
            actor_ref=None,
            operation_name="harness.run",
            input_refs=data.get("input_refs", []),
            output_refs=data.get("artifact_refs", []),
            trace_ref=data.get("run_ref"),
            estimated_cost_usd=data.get("estimated_cost_usd"),
            actual_cost_usd=data.get("actual_cost_usd"),
            code_identity=code_identity,
        )

    # 3. Detect if it is a LOGateDecisionReceipt (LO)
    elif "event_kind" in data and data.get("event_kind") == "LOGateDecisionEvaluated":
        return UnifiedReceiptCore(
            receipt_ref=data["receipt_ref"],
            receipt_version="v1",
            kind="gate",
            execution_status="completed",
            policy_decision=data.get("decision"),
            workspace_ref=data.get("mudo_commit_ref"),
            tenant_ref=data.get("tenant_ref"),
            actor_ref=data.get("actor_ref"),
            operation_name=f"gate.{str(data.get('gate_kind')).lower()}",
            input_refs=data.get("input_refs", []),
            policy_refs=data.get("policy_refs", []),
        )

    # Lossy check / fallback fallback
    raise ValueError(f"cul_lossy_mapping: Unable to map receipt payload of structure: {list(data.keys())}")


def to_unified_secret_receipt(receipt: Any) -> UnifiedSecretMountReceipt:
    """Map SecretMountReceipt to UnifiedSecretMountReceipt."""
    if not receipt:
        raise ValueError("Cannot map empty secret receipt")
        
    if isinstance(receipt, dict):
        data = receipt
    else:
        data = receipt.model_dump() if hasattr(receipt, "model_dump") else receipt.__dict__

    return UnifiedSecretMountReceipt(
        receipt_ref=data.get("receipt_ref", "unknown_receipt"),
        secret_mount_ref=data.get("secret_mount_ref", "unknown_ref"),
        decision=data.get("decision", "rejected"),
        reason_codes=data.get("reason_codes", []),
        mounted_value_logged=data.get("mounted_value_logged", False),
        audit_ref=data.get("audit_ref", "unknown_audit"),
    )


def to_unified_code_identity(proof: Any) -> UnifiedCodeIdentity:
    """Map CodeIdentityProof to UnifiedCodeIdentity."""
    if not proof:
        raise ValueError("Cannot map empty code identity proof")
        
    if isinstance(proof, dict):
        data = proof
    else:
        data = proof.model_dump() if hasattr(proof, "model_dump") else proof.__dict__

    return UnifiedCodeIdentity(
        code_ref=data.get("code_ref", "unknown"),
        code_sha256=data.get("code_sha256", ""),
        repo_commit_sha=data.get("repo_commit_sha"),
        repo_diff_sha256=data.get("repo_diff_sha256"),
        lockfile_sha256=data.get("lockfile_sha256"),
        image_digest=data.get("image_digest"),
    )

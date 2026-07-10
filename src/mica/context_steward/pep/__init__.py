from __future__ import annotations
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from mica.context_steward.contracts import (
    PermissionDecisionReceipt,
    Decision,
    Obligation,
    ContextEnvelope,
    ContextBlock,
    BlockKind,
    TrustState,
)

# Registry to track active envelopes for invalidation
_active_envelopes: Dict[str, ContextEnvelope] = {}

class PDPClient:
    def __init__(self, backend_url: Optional[str] = None):
        self.backend_url = backend_url

    async def decide(
        self,
        actor_ref: str,
        action_class: str,
        resource_class: str,
        resource_ref: str,
        environment_attrs: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecisionReceipt:
        # Mock / live switch
        if not self.backend_url:
            # Degraded mode: serve preview marked degraded
            return PermissionDecisionReceipt(
                permission_decision_ref=f"decision-degraded-{uuid.uuid4().hex[:8]}",
                decision=Decision.ALLOW_WITH_OBLIGATIONS,
                policy_bundle_ref="policy-degraded-fallback",
                obligations=[Obligation.WATERMARK, Obligation.NO_CACHE],
                effective_ttl=0,
                decided_at=datetime.utcnow(),
            )
        
        # If live backend is available (but mock for tests)
        # We can implement a try-except block calling backend
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.backend_url.rstrip('/')}/api/v1/tenancy/effective-permission",
                    json={
                        "principal_ref": actor_ref,
                        "scope_ref": resource_ref,
                        "target_ref": f"study://{resource_class}/main",
                    },
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    is_allowed = data.get("is_allowed", False)
                    # Convert EffectivePermissionResponse to PermissionDecisionReceipt
                    return PermissionDecisionReceipt(
                        permission_decision_ref=f"decision-live-{uuid.uuid4().hex[:8]}",
                        decision=Decision.ALLOW if is_allowed else Decision.DENY,
                        policy_bundle_ref="policy-live-pdp",
                        obligations=[],
                        effective_ttl=300,
                        decided_at=datetime.utcnow(),
                    )
        except Exception:
            pass

        # Fallback to degraded preview if connection fails
        return PermissionDecisionReceipt(
            permission_decision_ref=f"decision-degraded-fallback-{uuid.uuid4().hex[:8]}",
            decision=Decision.ALLOW_WITH_OBLIGATIONS,
            policy_bundle_ref="policy-degraded-fallback",
            obligations=[Obligation.WATERMARK, Obligation.NO_CACHE],
            effective_ttl=0,
            decided_at=datetime.utcnow(),
        )

def pre_assembly_pep(
    blocks: List[ContextBlock],
    pdp_receipt: PermissionDecisionReceipt,
) -> List[ContextBlock]:
    if pdp_receipt.decision == Decision.DENY:
        # Exclude everything
        return []

    filtered = []
    for b in blocks:
        # If PDP requires redact_fields, we filter out sensitive blocks (e.g. memory_digest)
        if Obligation.REDACT_FIELDS in pdp_receipt.obligations:
            if b.block_kind in (BlockKind.MEMORY_DIGEST, BlockKind.RETRIEVED_CONTEXT):
                continue
        filtered.append(b)
    return filtered

def tool_boundary_pep(
    requested_tools: List[str],
    allowed_by_kernel: List[str],
    pdp_receipt: PermissionDecisionReceipt,
) -> List[str]:
    """Intersect requested_tools with allowed_by_kernel and PDP allowed scope."""
    if pdp_receipt.decision == Decision.DENY:
        return []
    
    # Simple intersection
    allowed = [t for t in requested_tools if t in allowed_by_kernel]
    
    # If degraded (watermark/no_cache active but no deep tools allowed)
    if pdp_receipt.policy_bundle_ref == "policy-degraded-fallback":
        # Sensitive tools like run_md_simulation, execute are blocked in degraded mode
        allowed = [t for t in allowed if t not in ("run_md_simulation", "execute", "protocol.submit")]
        
    return allowed

def post_output_pep(
    driver_output: str,
    pdp_receipt: PermissionDecisionReceipt,
) -> str:
    """Enforce PDP obligations on output."""
    if pdp_receipt.decision == Decision.DENY:
        raise ValueError("Cannot process output for DENY decision")
        
    # Enforce watermark obligation
    if Obligation.WATERMARK in pdp_receipt.obligations:
        if "[SYNTHETIC_ORIGIN]" not in driver_output:
            driver_output = f"[SYNTHETIC_ORIGIN] {driver_output}"
            
    # Enforce audit_required obligation
    if Obligation.AUDIT_REQUIRED in pdp_receipt.obligations:
        # Print audit log or call Tenancy Audit module
        pass
        
    return driver_output

# Invalidation
def register_envelope(envelope: ContextEnvelope):
    _active_envelopes[envelope.context_envelope_ref] = envelope

def invalidate_envelopes(reason: str):
    """Invalidate all active envelopes based on policy/SCIM events."""
    global _active_envelopes
    # We clear the active cache or mark them as degraded
    for envelope in list(_active_envelopes.values()):
        for block in envelope.blocks:
            block.trust_state = TrustState.DEGRADED
    _active_envelopes.clear()

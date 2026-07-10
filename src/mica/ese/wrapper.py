"""
ese.wrapper — normalize_and_register()  (SLICE P3 · J.12.2)

ESE_FROM_SERVERLESS_INFERENCE_V1:
  Entry:  sequence (str) + optional params
  Model:  ESMDance (serverless_model://modal/esmdance@0.1.0)
  Output: EseNormalizeResult with
            artifact_ref  artifact://workspace/{ws}/ese/{kind}/{id}
            sha256        canonical content hash
            model_ref     serverless_model://…
            inference_receipt_id
            quetzal_receipt_id
            mudo_commit_ref (future MUDO — None in PoC)

Decision tree:
  1. Resolve ESMDance from ModelRegistry → QuetzalGate pre-check
  2. If gate PASS → ServerlessGateway.invoke() (real) → normalize raw → ese_lite
     Then build ese_graph_lite ALSO (derived_from_ese_lite=True)
  3. If gate BLOCK (registered_but_blocked / budget / input invalid)
     → deterministic ese_graph_lite only
  4. Emit artifact://… ref + sha256 + QuetzalReceiptCore for the artifact gate

The wrapper does NOT duplicate storage; it relies on the Gateway receipt mechanism
for durable provenance.  MUDO commit is deferred (flagged in result).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mica.provenance.artifacts import canonical_sha256
from mica.provenance.receipts import (
    GatePayload,
    ReceiptCore,
    ReceiptHashes,
    ReceiptRefs,
    ServerlessPayload,
)
from mica.quetzal.gates import InvokeContext, QuetzalGate, QuetzalVerdict
from mica.serverless_models.registry import (
    ModelNotRegistered,
    ModelRegistry,
    ModelRevision,
    get_default_model_registry,
)

from .contracts import (
    EseArtifact,
    EseGraphLitePayload,
    EseLitePayload,
    EseLiteResidueFeature,
    EseNormalizeResult,
)
from .graph_lite import build_ese_graph_lite


# ── Constants ─────────────────────────────────────────────────────────────

ESMDANCE_MODEL_REF = "serverless_model://modal/esmdance@0.1.0"
ESE_LITE_ARTIFACT_KIND = "ese_lite"
ESE_GRAPH_LITE_ARTIFACT_KIND = "ese_graph_lite"
MAX_CLAIM_TIER = "screening_signal"

_DEFAULT_BUDGET_USD = 0.20
_DEFAULT_ACTOR_ID = "ese_wrapper"


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_id(prefix: str = "ese") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _artifact_ref(workspace_id: str, kind: str, artifact_id: str) -> str:
    return f"artifact://workspace/{workspace_id}/ese/{kind}/{artifact_id}"


def _make_artifact_receipt(
    *,
    receipt_id: str,
    workspace_id: str,
    actor_id: str,
    artifact_ref: str,
    sha256: str,
    verdict: QuetzalVerdict,
) -> ReceiptCore:
    """Quetzal gate receipt for the ESE artifact (ese_artifact_gate)."""
    return ReceiptCore(
        receipt_id=receipt_id,
        kind="gate",
        status="pass" if verdict.decision == "pass" else "blocked",
        workspace_id=workspace_id,
        actor_id=actor_id,
        operation_name="quetzal.ese_artifact_gate",
        refs=ReceiptRefs(
            policy_refs=[f"quetzal_policy://{verdict.gate_name}"],
            artifact_refs=[artifact_ref],
        ),
        hashes=ReceiptHashes(request_hash=sha256, content_hash=sha256),
        started_at=_now(),
        ended_at=_now(),
        trace_id=f"trace_{sha256[7:15]}",
        payload=GatePayload(
            gate_name=verdict.gate_name,
            decision=verdict.decision,
            reason_codes=list(verdict.reason_codes),
            max_allowed_tier=verdict.max_allowed_tier,
            provider_job_created=False,
        ),
    )


def _make_inference_receipt(
    *,
    receipt_id: str,
    workspace_id: str,
    actor_id: str,
    model_ref: str,
    rev: ModelRevision,
    artifact_ref: str,
    sha256: str,
    inference_result: Dict[str, Any],
) -> ReceiptCore:
    """Serverless inference receipt for a completed ESMDance call."""
    return ReceiptCore(
        receipt_id=receipt_id,
        kind="serverless",
        status="completed",
        workspace_id=workspace_id,
        actor_id=actor_id,
        operation_name=f"models.invoke:{model_ref}",
        refs=ReceiptRefs(
            output_refs=[artifact_ref],
            artifact_refs=[artifact_ref],
        ),
        hashes=ReceiptHashes(
            request_hash=sha256,
            output_hash=sha256,
            content_hash=sha256,
        ),
        started_at=_now(),
        ended_at=_now(),
        trace_id=f"trace_{sha256[7:15]}",
        payload=ServerlessPayload(
            model_ref=model_ref,
            model_revision_ref=rev.revision_ref,
            modal_app=rev.runtime.modal_app,
            modal_function=rev.runtime.modal_function,
            input_schema_version="v1",
            output_schema_version="v1",
            provider_job_id=inference_result.get("job_id") or "",
        ),
    )


# ── ESMDance raw-output normalizer ─────────────────────────────────────────

def _normalize_esmdance_output(
    *,
    sequence: str,
    raw_output: Dict[str, Any],
    model_ref: str,
    model_revision_ref: str,
) -> EseLitePayload:
    """
    Normalize raw ESMDance inference output into a typed EseLitePayload.

    ESMDance returns (from mica-esmdance Modal app):
      {
        "global_embedding": [...],             # 1280-dim float list (optional)
        "residue_flexibilities": [...],        # per-residue float (length == len(sequence))
        "residue_confidences": [...],          # per-residue float
        "per_residue_embeddings": [[...]],     # optional (may be large)
        "confidence": float,                   # aggregate
        "job_id": "...",                       # provider job ref
      }

    We only lift what we need; embeddings are omitted from the primary payload
    to keep it small (raw_output_ref can hold verbose artifacts if needed).
    """
    seq = sequence.upper().strip()
    n = len(seq)

    global_emb: Optional[List[float]] = raw_output.get("global_embedding") or None
    flexibilities: List[float] = list(raw_output.get("residue_flexibilities") or [])
    confidences: List[float] = list(raw_output.get("residue_confidences") or [])
    aggregate_conf: Optional[float] = raw_output.get("confidence")

    # Align per-residue arrays defensively
    residue_features: List[EseLiteResidueFeature] = []
    for i in range(n):
        residue_features.append(
            EseLiteResidueFeature(
                residue_index=i,
                aa=seq[i],
                flexibility=flexibilities[i] if i < len(flexibilities) else None,
                confidence=confidences[i] if i < len(confidences) else None,
                # Per-residue embeddings intentionally omitted from primary payload
            )
        )

    return EseLitePayload(
        model_ref=model_ref,
        model_revision_ref=model_revision_ref,
        sequence=seq,
        global_embedding=global_emb,
        residue_features=residue_features,
        confidence=aggregate_conf,
    )


# ── Artifact gate (ese_artifact_gate) ─────────────────────────────────────

class EseArtifactGate:
    """
    Deterministic Quetzal gate for ESE artifacts.

    Rules (J.12.4):
      - PASS if artifact_kind is a known kind AND sha256 is non-empty AND
        max_claim_tier is within allowed bounds.
      - BLOCK otherwise.
    """
    name = "quetzal.ese_artifact_gate"
    _ALLOWED_KINDS = frozenset({"ese_lite", "ese_graph_lite", "ese_graph_inference"})
    _MAX_ALLOWED_TIER = "screening_signal"

    def evaluate(
        self,
        *,
        artifact_kind: str,
        sha256: str,
        max_claim_tier: str,
    ) -> QuetzalVerdict:
        from mica.quetzal.gates import QuetzalVerdict
        if artifact_kind not in self._ALLOWED_KINDS:
            return QuetzalVerdict(
                self.name, "block",
                ("unknown_artifact_kind",),
                self._MAX_ALLOWED_TIER,
            )
        if not sha256 or not sha256.startswith("sha256:"):
            return QuetzalVerdict(
                self.name, "block",
                ("missing_or_malformed_sha256",),
                self._MAX_ALLOWED_TIER,
            )
        if max_claim_tier not in ("screening_signal",):
            return QuetzalVerdict(
                self.name, "warn",
                ("claim_tier_elevated",),
                self._MAX_ALLOWED_TIER,
            )
        return QuetzalVerdict(self.name, "pass")


_ESE_ARTIFACT_GATE = EseArtifactGate()


# ── normalize_and_register ─────────────────────────────────────────────────

async def normalize_and_register(
    sequence: str,
    *,
    workspace_id: str,
    actor_id: str = _DEFAULT_ACTOR_ID,
    budget_ceiling_usd: float = _DEFAULT_BUDGET_USD,
    registry: Optional[ModelRegistry] = None,
    # Optional ServerlessGateway.invoke override for testing
    _serverless_invoke: Optional[Any] = None,
    # If True, always build ese_graph_lite alongside ese_lite
    build_graph_lite: bool = True,
) -> EseNormalizeResult:
    """
    Main entry-point for SLICE P3 ESE wrapper (J.12.2 ESE_FROM_SERVERLESS_INFERENCE_V1).

    Args:
        sequence: Amino acid sequence string.
        workspace_id: Active workspace identifier.
        actor_id: Audit actor (agent or user id).
        budget_ceiling_usd: Max allowed inference cost.
        registry: ModelRegistry instance (defaults to get_default_model_registry()).
        _serverless_invoke: Async callable for testing — replaces real provider dispatch.
            Signature: async (model_ref, payload_in) -> dict
        build_graph_lite: Always compute ese_graph_lite (defaults True).

    Returns:
        EseNormalizeResult
    """
    if not sequence or not sequence.strip():
        return EseNormalizeResult(
            status="blocked",
            reason="empty_sequence",
            gate_verdict="block",
            inference_blocked=True,
        )

    reg = registry or get_default_model_registry()
    gate = QuetzalGate()

    # ── 1. Resolve ESMDance from registry + QuetzalGate pre-check ────────
    try:
        rev = reg.resolve(ESMDANCE_MODEL_REF)
        model_status = getattr(rev, "status", "registered")
        revision_ref = rev.revision_ref
    except ModelNotRegistered:
        rev = None
        model_status = "unregistered"
        revision_ref = None

    seq_payload = {"sequence": sequence}
    est_cost = 0.05  # placeholder cost estimate; real cost model wired in P4

    ctx = InvokeContext(
        model_ref=ESMDANCE_MODEL_REF,
        model_revision_ref=revision_ref,
        workspace_id=workspace_id,
        input_valid=bool(sequence.strip()),
        estimated_usd=est_cost,
        budget_ceiling_usd=budget_ceiling_usd,
        model_status=model_status,
    )
    verdict = gate.evaluate(ctx)
    inference_blocked = verdict.decision == "block"

    # ── 2A. Gate PASS — run real ESMDance inference ───────────────────────
    ese_lite: Optional[EseLitePayload] = None
    inference_receipt: Optional[ReceiptCore] = None

    if not inference_blocked and rev is not None:
        if _serverless_invoke is not None:
            raw_output: Dict[str, Any] = await _serverless_invoke(
                ESMDANCE_MODEL_REF, seq_payload
            )
        else:
            # Real Modal invocation path — delegated to the provider layer.
            # In PoC without live Modal credentials, this raises RuntimeError
            # which we catch and convert to fallback.
            try:
                from mica.serverless_models.providers.modal import ModalProviderAdapter
                provider = ModalProviderAdapter()
                raw_output = await provider.invoke(rev, seq_payload)
            except Exception as exc:
                # Graceful fallback: log the provider error and fall through
                # to ese_graph_lite without crashing the wrapper.
                inference_blocked = True
                ese_lite = None
                raw_output = {}
                _reason_detail = str(exc)

        if not inference_blocked and raw_output:
            ese_lite = _normalize_esmdance_output(
                sequence=sequence,
                raw_output=raw_output,
                model_ref=ESMDANCE_MODEL_REF,
                model_revision_ref=rev.revision_ref,
            )

    # ── 2B. Gate BLOCK / fallback — deterministic graph lite only ─────────
    if build_graph_lite:
        graph_lite_payload = build_ese_graph_lite(sequence, ese_lite)
    else:
        graph_lite_payload = None

    # ── 3. Select canonical artifact for this result ──────────────────────
    if ese_lite is not None:
        primary_kind = ESE_LITE_ARTIFACT_KIND
        primary_payload_dict = ese_lite.model_dump()
        primary_model_ref: Optional[str] = ESMDANCE_MODEL_REF
        status = "completed"
        reason: Optional[str] = None
    else:
        # Fallback to graph_lite
        assert graph_lite_payload is not None, "build_graph_lite must be True when inference blocked"
        primary_kind = ESE_GRAPH_LITE_ARTIFACT_KIND
        primary_payload_dict = graph_lite_payload.model_dump()
        primary_model_ref = None
        status = "fallback_graph_lite"
        reason = "inference_blocked" if inference_blocked else "model_unavailable"

    # ── 4. Canonical SHA256 + artifact_ref ───────────────────────────────
    payload_bytes = json.dumps(primary_payload_dict, sort_keys=True, default=str).encode()
    sha256 = canonical_sha256(payload_bytes)
    artifact_id = _fresh_id("ese")
    artifact_ref = _artifact_ref(workspace_id, primary_kind, artifact_id)

    # ── 5. ESE artifact gate (quetzal.ese_artifact_gate) ─────────────────
    gate_verdict = _ESE_ARTIFACT_GATE.evaluate(
        artifact_kind=primary_kind,
        sha256=sha256,
        max_claim_tier=MAX_CLAIM_TIER,
    )
    quetzal_receipt = _make_artifact_receipt(
        receipt_id=_fresh_id("qrcpt"),
        workspace_id=workspace_id,
        actor_id=actor_id,
        artifact_ref=artifact_ref,
        sha256=sha256,
        verdict=gate_verdict,
    )

    # ── 6. Inference receipt (if serverless inference ran) ────────────────
    if ese_lite is not None and rev is not None:
        inference_receipt = _make_inference_receipt(
            receipt_id=_fresh_id("ircpt"),
            workspace_id=workspace_id,
            actor_id=actor_id,
            model_ref=ESMDANCE_MODEL_REF,
            rev=rev,
            artifact_ref=artifact_ref,
            sha256=sha256,
            inference_result=raw_output,  # type: ignore[arg-type]
        )

    # ── 7. Assemble EseArtifact ───────────────────────────────────────────
    artifact = EseArtifact(
        artifact_kind=primary_kind,
        artifact_ref=artifact_ref,
        sha256=sha256,
        model_ref=primary_model_ref,
        inference_receipt_id=inference_receipt.receipt_id if inference_receipt else None,
        quetzal_receipt_id=quetzal_receipt.receipt_id,
        mudo_commit_ref=None,   # deferred to P4 MUDO integration
        payload=primary_payload_dict,
    )

    return EseNormalizeResult(
        status=status,
        artifact=artifact,
        reason=reason,
        gate_verdict=gate_verdict.decision,
        inference_blocked=inference_blocked,
    )

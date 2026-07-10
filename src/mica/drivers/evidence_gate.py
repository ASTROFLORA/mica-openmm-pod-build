"""
S1.6 — Evidence Gate for Synthesis Quality Control.

A deterministic checkpoint that inspects the EvidenceLedger before
allowing a synthesis to be marked as final / publication-ready.

Usage inside agentic_driver.py or langgraph nodes:

    from mica.drivers.evidence_gate import EvidenceGate, GateVerdict

    gate = EvidenceGate(ledger)
    verdict = gate.evaluate()
    if not verdict.passed:
        # Force refinement loop — do NOT publish
        ...

The gate never blocks autonomously; it returns a structured verdict that
the orchestrator (driver or node) acts on.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from mica.drivers.cold_evidence import build_cold_evidence_spine
from mica.drivers.evidence_ledger import EvidenceEntry, EvidenceLedger


def evaluate_monomero_checkpoint_bcif_smic_run(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Classify the future monomero checkpoint + preview + SMIC evidence packet."""
    if not evidence:
        return {}

    def _list(*keys: str) -> List[Dict[str, Any]]:
        for key in keys:
            value = evidence.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        runtime = evidence.get("runtime_receipts") if isinstance(evidence.get("runtime_receipts"), dict) else {}
        for key in keys:
            value = runtime.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _dict(*keys: str) -> Dict[str, Any]:
        for key in keys:
            value = evidence.get(key)
            if isinstance(value, dict):
                return value
        runtime = evidence.get("runtime_receipts") if isinstance(evidence.get("runtime_receipts"), dict) else {}
        for key in keys:
            value = runtime.get(key)
            if isinstance(value, dict):
                return value
        return {}

    blocks: List[str] = []
    warnings: List[str] = []
    terminal_result = _dict("md_execution_result_v1", "terminal_result", "canonical_result")
    status = terminal_result.get("status") if isinstance(terminal_result.get("status"), dict) else {}
    terminal_state = str(status.get("state") or evidence.get("terminal_state") or "").lower()
    provider_state = str(evidence.get("provider_state") or evidence.get("provider_status") or "").lower()
    provider_success_only = bool(evidence.get("provider_success_only", False))
    single_websocket_route = str(evidence.get("websocket_route") or evidence.get("md_websocket_route") or "/ws/md/{job_id}")
    second_websocket_declared = bool(evidence.get("second_websocket_created", False))

    trajectory_frames = _list("trajectory_frame_event_receipts", "trajectory_frames")
    preview_receipts = _list("bcif_preview_receipts", "preview_receipts")
    artifact_receipts = _list("artifact_transmission_receipts", "artifact_receipts")
    artifact_by_ref: Dict[str, Dict[str, Any]] = {}
    for item in artifact_receipts:
        for key in ("artifact_ref", "object_uri", "payload_ref"):
            ref = str(item.get(key) or "").strip()
            if ref:
                artifact_by_ref[ref] = item
    checkpoint_receipts = [
        item for item in artifact_receipts
        if str(item.get("durability_class") or "") == "checkpoint" or str(item.get("artifact_ref") or "").endswith(".cpt")
    ]
    smic_doc = _dict("smic_metric_receipts", "smic_summary")
    smic_receipts = list(smic_doc.get("metric_receipts") or []) if isinstance(smic_doc, dict) else []
    live_smic = smic_doc.get("live_metric_receipts") if isinstance(smic_doc.get("live_metric_receipts"), dict) else {}
    if not smic_receipts and live_smic:
        smic_receipts = list(live_smic.get("metric_receipts") or [])
    completed_smic = [item for item in smic_receipts if str(item.get("metric_status") or "") == "completed"]
    blocked_smic = [item for item in smic_receipts if str(item.get("metric_status") or "") in {"degraded", "inapplicable", "blocked"}]

    claimed_browser_preview = bool(evidence.get("browser_live_preview_claimed", False))
    if claimed_browser_preview and not trajectory_frames:
        blocks.append("browser_live_preview_claimed_without_trajectory_frame_receipts")
    if second_websocket_declared or single_websocket_route != "/ws/md/{job_id}":
        blocks.append("single_websocket_contract_violation")
    if provider_success_only:
        blocks.append("provider_status_alone_cannot_create_terminal_success")

    preview_checks = {
        "bcif_preview_completed": False,
        "bcif_preview_degraded_fallback": False,
        "bcif_preview_failed": False,
        "trajectory_frame_stream_completed": False,
        "trajectory_frame_stream_degraded": False,
        "preview_artifact_readback_failed": False,
    }
    for receipt in preview_receipts:
        classification = str(receipt.get("classification") or "")
        if classification in preview_checks:
            preview_checks[classification] = True
        trajectory_classification = str(receipt.get("trajectory_frame_classification") or "")
        if trajectory_classification in preview_checks:
            preview_checks[trajectory_classification] = True

    for frame in trajectory_frames:
        if not frame.get("preview_not_canonical", False):
            blocks.append("trajectory_frame_missing_preview_not_canonical")
            break
        if str(frame.get("durability_class") or "") != "stream-preview":
            blocks.append("trajectory_frame_missing_stream_preview_durability")
            break
        bcif_status = str(frame.get("bcif_preview_status") or "")
        payload_ref = str(frame.get("payload_ref") or "")
        bcif_ref = str(frame.get("bcif_preview_ref") or "")
        preview_payload_format = str(frame.get("preview_payload_format") or "")
        artifact_meta = artifact_by_ref.get(payload_ref, {})
        readback_verified = bool(frame.get("readback_verified", False) or artifact_meta.get("readback_verified", False))
        content_type = str(frame.get("content_type") or artifact_meta.get("content_type") or "")
        if payload_ref and readback_verified:
            preview_checks["trajectory_frame_stream_completed"] = True
        else:
            preview_checks["trajectory_frame_stream_degraded"] = True
        if bcif_status == "implemented" and not (
            payload_ref.lower().endswith(".bcif")
            or bcif_ref.lower().endswith(".bcif")
            or preview_payload_format.lower() in {"bcif", "binarycif"}
        ):
            blocks.append("bcif_implemented_without_binary_preview_ref")
            break
        if bcif_status == "implemented" and not readback_verified:
            preview_checks["preview_artifact_readback_failed"] = True
            blocks.append("preview_artifact_readback_failed")
            break
        if bcif_status == "implemented" and not content_type:
            blocks.append("bcif_implemented_without_content_type")
            break
        if bcif_status == "implemented":
            preview_checks["bcif_preview_completed"] = True
        elif bcif_status == "dropped":
            preview_checks["bcif_preview_failed"] = True
            blocks.append("bcif_preview_failed")
            break
        if bcif_status != "implemented" and str(frame.get("fallback_event_format") or "") not in {"artifact_ref", "pdb_preview"}:
            blocks.append("bcif_degraded_preview_missing_explicit_fallback")
            break
        if bcif_status != "implemented":
            preview_checks["bcif_preview_degraded_fallback"] = True

    if terminal_state in {"failed", "error", "cancelled"} or evidence.get("failure_receipt_present"):
        classification = "failed_runtime"
    elif provider_state in {"provisioning", "queued", "pending", "running"} and terminal_state not in {"completed", "succeeded"}:
        classification = "external_hold"
    elif terminal_state in {"completed", "succeeded"} and checkpoint_receipts and completed_smic and not blocks:
        classification = "passed_monomero_checkpoint_bcif_smic_run"
    else:
        classification = "partial_monomero_checkpoint_bcif_smic_run"

    if not trajectory_frames:
        warnings.append("no_trajectory_frame_receipts_observed")
    if not checkpoint_receipts:
        warnings.append("no_checkpoint_receipts_observed")
    if not smic_receipts:
        warnings.append("no_smic_metric_receipts_observed")
    if blocked_smic:
        warnings.append("smic_metric_blockers_present")

    return {
        "classification": classification,
        "passed": classification == "passed_monomero_checkpoint_bcif_smic_run",
        "blocks": list(dict.fromkeys(blocks)),
        "warnings": list(dict.fromkeys(warnings)),
        "trajectory_frame_receipt_count": len(trajectory_frames),
        "implemented_bcif_preview_count": len([
            frame for frame in trajectory_frames
            if str(frame.get("bcif_preview_status") or "") == "implemented"
        ]),
        "checkpoint_receipt_count": len(checkpoint_receipts),
        "smic_metric_receipt_count": len(smic_receipts),
        "completed_smic_metric_count": len(completed_smic),
        "preview_receipt_count": len(preview_receipts),
        "preview_checks": preview_checks,
        "preview_classification": (
            "preview_artifact_readback_failed" if preview_checks["preview_artifact_readback_failed"] else
            "bcif_preview_failed" if preview_checks["bcif_preview_failed"] else
            "bcif_preview_completed" if preview_checks["bcif_preview_completed"] else
            "bcif_preview_degraded_fallback" if preview_checks["bcif_preview_degraded_fallback"] else
            "trajectory_frame_stream_degraded"
        ),
        "bcif_hard_required": False,
        "accepted_preview_fallbacks": ["artifact_ref", "pdb_preview"],
        "accepted_preview_payload_formats": ["bcif", "binarycif", "pdb"],
        "single_websocket_route": single_websocket_route,
    }


# ── Verdict ──────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class GateVerdict:
    """Immutable result of an evidence gate evaluation."""

    passed: bool
    """True if all gate criteria are satisfied."""

    reason: str
    """Human-readable explanation of the verdict."""

    unsupported_critical: List[str]
    """claim_ids of critical claims that lack sufficient evidence."""

    contradicted_claims: List[str]
    """claim_ids that have been actively contradicted."""

    min_confidence: float
    """Lowest algorithmic_confidence among critical claims (1.0 if none)."""

    misleading_support_detected: bool = False
    """True when contract-level provenance semantics block promotion."""

    promotion_block_reasons: List[str] = dataclasses.field(default_factory=list)
    """Structured reasons why publication/evidence-backed promotion is blocked."""

    next_required_evidence: str = ""
    """Explicit next evidence action that operators must take before promotion."""

    cold_evidence_spine: Dict[str, Any] = dataclasses.field(default_factory=dict)
    """Phase 2 cold evidence metadata attached at promotion/publication boundaries."""

    evaluated_at: str = dataclasses.field(
        default_factory=lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ── Gate ─────────────────────────────────────────────────────────────

class EvidenceGate:
    """
    Deterministic quality gate that evaluates an EvidenceLedger.

    Configurable thresholds:

    - ``min_critical_confidence``: minimum algorithmic_confidence required
      for *every* critical claim (default: 0.4).
    - ``block_on_contradicted``: if True, any contradicted claim fails
      the gate (default: True).
    - ``block_on_unsupported_critical``: if True, any unsupported
      critical claim fails the gate (default: True).
    """

    def __init__(
        self,
        ledger: EvidenceLedger,
        *,
        min_critical_confidence: float = 0.4,
        min_provenance_relevance: float = 0.65,
        block_on_contradicted: bool = True,
        block_on_unsupported_critical: bool = True,
        final_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._ledger = ledger
        self.min_critical_confidence = min_critical_confidence
        self.min_provenance_relevance = min_provenance_relevance
        self.block_on_contradicted = block_on_contradicted
        self.block_on_unsupported_critical = block_on_unsupported_critical
        self._final_result = final_result or {}

    # ── Core evaluation ──────────────────────────────────────────────

    def evaluate(self) -> GateVerdict:
        """Run the gate and return a structured verdict."""
        reasons: list[str] = []
        unsupported_ids: list[str] = []
        contradicted_ids: list[str] = []
        promotion_block_reasons: list[str] = []
        min_conf = 1.0

        # Gather critical claims
        critical: Sequence[EvidenceEntry] = self._ledger.get_for_review(
            severity_filter="critical",
        )

        for entry in critical:
            if entry.algorithmic_confidence < min_conf:
                min_conf = entry.algorithmic_confidence

            # Check unsupported
            if self.block_on_unsupported_critical and entry.status == "unsupported":
                unsupported_ids.append(entry.claim_id)

            # Check low confidence
            if entry.algorithmic_confidence < self.min_critical_confidence:
                if entry.claim_id not in unsupported_ids:
                    unsupported_ids.append(entry.claim_id)

        # Check contradicted claims (any severity)
        if self.block_on_contradicted:
            for entry in self._ledger.get_contradicted_claims():
                contradicted_ids.append(entry.claim_id)

        # Build reasons
        if unsupported_ids:
            reasons.append(
                f"{len(unsupported_ids)} critical claim(s) lack sufficient evidence: "
                f"{unsupported_ids}"
            )
        if contradicted_ids:
            reasons.append(
                f"{len(contradicted_ids)} claim(s) are contradicted: "
                f"{contradicted_ids}"
            )
        if min_conf < self.min_critical_confidence and critical:
            reasons.append(
                f"Minimum confidence ({min_conf:.2f}) is below threshold "
                f"({self.min_critical_confidence:.2f})"
            )

        misleading_support_detected = False
        if self._final_result:
            cold_evidence_spine = build_cold_evidence_spine(
                query=str(self._final_result.get("query") or ""),
                final_result=self._final_result,
            )
            firewall = cold_evidence_spine.get("firewall") or {}
            invariants = cold_evidence_spine.get("invariants") or {}
            if firewall.get("action") in {"challenge", "block"}:
                promotion_block_reasons.extend(list(firewall.get("reasons") or []))
            if not invariants.get("passed", True):
                promotion_block_reasons.extend(list(invariants.get("failed_checks") or []))

            if bool(self._final_result.get("misleading_support_detected", False)):
                misleading_support_detected = True
                promotion_block_reasons.extend(
                    list(self._final_result.get("misleading_support_reasons") or ["misleading support detected"])
                )

            provenance_relevance_score = float(self._final_result.get("provenance_relevance_score", 0.0) or 0.0)
            if provenance_relevance_score < self.min_provenance_relevance:
                promotion_block_reasons.append(
                    f"Provenance relevance score ({provenance_relevance_score:.2f}) is below threshold ({self.min_provenance_relevance:.2f})"
                )

            for claim in self._final_result.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                if str(claim.get("strength") or "") not in {"supported", "observed"}:
                    continue
                if str(claim.get("claim_kind") or "") != "positive_scientific":
                    continue
                if not claim.get("relevant_source_ids"):
                    promotion_block_reasons.append(
                        f"Supported claim {claim.get('claim_id', '?')} lacks relevant supporting sources"
                    )

            monomero_evidence = self._final_result.get("monomero_checkpoint_bcif_smic_run")
            if isinstance(monomero_evidence, dict):
                monomero_verdict = evaluate_monomero_checkpoint_bcif_smic_run(monomero_evidence)
                classification = str(monomero_verdict.get("classification") or "")
                if classification and classification != "passed_monomero_checkpoint_bcif_smic_run":
                    promotion_block_reasons.append(
                        f"Monomero checkpoint/preview/SMIC evidence classified as {classification}"
                    )
                for block in monomero_verdict.get("blocks") or []:
                    promotion_block_reasons.append(str(block))

        if promotion_block_reasons:
            reasons.append("Promotion blocked: " + "; ".join(dict.fromkeys(promotion_block_reasons)))

        passed = len(reasons) == 0
        reason = "All evidence criteria met." if passed else "; ".join(reasons)
        if passed:
            next_required_evidence = "Proceed to final synthesis"
        elif contradicted_ids:
            next_required_evidence = "Resolve contradicted claims and rerun promotion evidence gate"
        elif unsupported_ids:
            next_required_evidence = "Collect evidence for unsupported critical claims and rerun promotion evidence gate"
        elif promotion_block_reasons:
            next_required_evidence = "Resolve promotion block reasons and rerun promotion evidence gate"
        else:
            next_required_evidence = "Review evidence ledger findings and rerun promotion evidence gate"

        return GateVerdict(
            passed=passed,
            reason=reason,
            unsupported_critical=unsupported_ids,
            contradicted_claims=contradicted_ids,
            min_confidence=min_conf if critical else 1.0,
            misleading_support_detected=misleading_support_detected,
            promotion_block_reasons=list(dict.fromkeys(promotion_block_reasons)),
            next_required_evidence=next_required_evidence,
            cold_evidence_spine=cold_evidence_spine if self._final_result else {},
        )

    # ── Convenience ──────────────────────────────────────────────────

    def passes(self) -> bool:
        """Quick boolean check — equivalent to ``evaluate().passed``."""
        return self.evaluate().passed

    def summary(self) -> str:
        """One-line summary suitable for logging."""
        v = self.evaluate()
        status = "PASS" if v.passed else "FAIL"
        return (
            f"EvidenceGate {status}: "
            f"unsupported_critical={len(v.unsupported_critical)}, "
            f"contradicted={len(v.contradicted_claims)}, "
            f"min_conf={v.min_confidence:.2f}"
        )

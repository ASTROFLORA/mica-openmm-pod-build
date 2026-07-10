"""
hot_loop_reinjection_service.py — I09-B extraction.

Pure-computation builder for the hot-loop reinjection packet.
Zero self.* references: extracted as a module-level function.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List


def build_hot_loop_reinjection_packet(
    *,
    user_query: str,
    session_id: str,
    run_id: str,
    result: Dict[str, Any],
    critic_verdict: Dict[str, Any],
    retry_plan: Dict[str, Any],
    current_execution_path: str,
) -> Dict[str, Any]:
    """Build the reinjection packet for a hot-loop critic retry."""
    from mica.agentic.core import build_negative_memory_summary

    cognitive_layer = result.get("cognitive_layer") if isinstance(result.get("cognitive_layer"), dict) else {}
    ach_state = cognitive_layer.get("hypothesis_competition") if isinstance(cognitive_layer.get("hypothesis_competition"), dict) else {}
    challenged_claim_ids = [str(item) for item in list(critic_verdict.get("challenged_claim_ids") or []) if str(item)]
    rejected_hypothesis_ids = [str(item) for item in list(ach_state.get("rejected_hypothesis_ids") or []) if str(item)]
    unresolved_rival_hypothesis_ids = [str(item) for item in list(critic_verdict.get("unresolved_rival_hypothesis_ids") or []) if str(item)]
    contradiction_pressure = float(ach_state.get("contradiction_pressure", 0.0) or 0.0)
    ach_entries = [entry for entry in list(ach_state.get("entries") or []) if isinstance(entry, dict)]
    ach_entries_by_hypothesis = {
        str(entry.get("hypothesis_id") or "").strip(): entry
        for entry in ach_entries
        if str(entry.get("hypothesis_id") or "").strip()
    }
    final_result = result.get("final_result") if isinstance(result.get("final_result"), dict) else {}
    claims = [claim for claim in list(final_result.get("claims") or []) if isinstance(claim, dict)]
    claims_by_id = {
        str(claim.get("claim_id") or "").strip(): claim
        for claim in claims
        if str(claim.get("claim_id") or "").strip()
    }

    residual_tasks: List[Dict[str, Any]] = []
    branch_tombstones: List[Dict[str, Any]] = []
    for index, hypothesis_id in enumerate(rejected_hypothesis_ids, start=1):
        entry = ach_entries_by_hypothesis.get(hypothesis_id, {})
        hypothesis_text = str(entry.get("text") or "").strip()
        tombstone_class = "heretical" if unresolved_rival_hypothesis_ids else "archaeological"
        action = "soft_repulsion"
        residual_tasks.append(
            {
                "residual_id": f"residual-hypothesis-{index}",
                "kind": "rejected_hypothesis",
                "origin_hypothesis_id": hypothesis_id,
                "reason": "rejected_by_ach",
                "required_action": "Either strengthen this hypothesis with genuinely new evidence or abandon it explicitly.",
            }
        )
        branch_tombstones.append(
            {
                "tombstone_id": f"tombstone-hypothesis-{index}",
                "tombstone_class": tombstone_class,
                "action": action,
                "target_type": "hypothesis",
                "target_id": hypothesis_id,
                "hypothesis_id": hypothesis_id,
                "reason": "rejected_by_ach",
                "appealable": tombstone_class == "heretical",
                "resurrection_cost": round(0.4 + contradiction_pressure + (0.15 if tombstone_class == "heretical" else 0.05), 3),
                "visibility_default": "full",
                "match_strings": [value for value in [hypothesis_id, hypothesis_text] if value],
            }
        )
    for index, claim_id in enumerate(challenged_claim_ids, start=1):
        claim = claims_by_id.get(claim_id, {})
        claim_text = str(claim.get("text") or "").strip()
        residual_tasks.append(
            {
                "residual_id": f"residual-claim-{index}",
                "kind": "challenged_claim",
                "origin_claim_id": claim_id,
                "reason": "challenged_by_continuous_critic",
                "required_action": "Resolve this claim with evidence or mark it as abandoned.",
            }
        )
        branch_tombstones.append(
            {
                "tombstone_id": f"tombstone-claim-{index}",
                "tombstone_class": "operational",
                "action": "prune_context",
                "target_type": "claim",
                "target_id": claim_id,
                "claim_id": claim_id,
                "reason": "challenged_by_continuous_critic",
                "appealable": False,
                "resurrection_cost": round(0.35 + contradiction_pressure, 3),
                "visibility_default": "full",
                "match_strings": [value for value in [claim_id, claim_text] if value],
            }
        )

    rupture_energy = round(
        max(contradiction_pressure, 0.0)
        + 0.05 * len(rejected_hypothesis_ids)
        + 0.03 * len(challenged_claim_ids),
        3,
    )
    soft_repulsion_warnings = [
        {
            "warning_id": f"soft-repulsion-{index}",
            "tombstone_id": str(tombstone.get("tombstone_id") or f"soft-{index}"),
            "tombstone_class": str(tombstone.get("tombstone_class") or "archaeological"),
            "target_type": str(tombstone.get("target_type") or "hypothesis"),
            "target_id": str(tombstone.get("target_id") or ""),
            "resurrection_cost": float(tombstone.get("resurrection_cost") or 0.0),
            "warning": "This branch remains explorable only under explicit anomaly handling or stronger evidence.",
            "recommended_action": "Search for materially new evidence before reviving this route.",
        }
        for index, tombstone in enumerate(branch_tombstones, start=1)
        if str(tombstone.get("tombstone_class") or "").strip().lower() in {"archaeological", "heretical"}
    ]
    appeal_trigger_reasons: List[str] = []
    if unresolved_rival_hypothesis_ids:
        appeal_trigger_reasons.append("unresolved_rival_hypotheses")
    if contradiction_pressure >= 0.6:
        appeal_trigger_reasons.append("contradiction_pressure")
    if soft_repulsion_warnings:
        appeal_trigger_reasons.append("soft_repulsion_pressure")
    appeal_regime_active = bool(
        unresolved_rival_hypothesis_ids
        and soft_repulsion_warnings
    ) or contradiction_pressure >= 0.8

    packet = {
        "schema_version": "mica.hot_loop_reinjection.v0",
        "packet_id": str(uuid.uuid4()),
        "session_id": session_id,
        "run_id": run_id,
        "query": user_query,
        "source": "continuous_critic",
        "retry_required": True,
        "current_execution_path": current_execution_path,
        "target_execution_path": str(retry_plan.get("retry_execution_path") or current_execution_path),
        "temperature": float(retry_plan.get("temperature") or 0.0),
        "phase": str(retry_plan.get("phase") or ""),
        "negative_memory_mode": "full",
        "visible_tombstone_classes": ["operational", "archaeological", "heretical"],
        "contradiction_pressure": contradiction_pressure,
        "challenged_claim_ids": challenged_claim_ids,
        "rejected_hypothesis_ids": rejected_hypothesis_ids,
        "unresolved_rival_hypothesis_ids": unresolved_rival_hypothesis_ids,
        "retry_guidance": str(
            critic_verdict.get("retry_guidance")
            or "Compare rival hypotheses, resolve contradictions, and explicitly abandon unsupported claims."
        ),
        "rationale": list(critic_verdict.get("rationale") or []),
        "residual_tasks": residual_tasks,
        "branch_tombstones": branch_tombstones,
        "soft_repulsion_warnings": soft_repulsion_warnings,
        "appeal_regime_state": {
            "appeal_regime_active": appeal_regime_active,
            "policy": "paradigmatic_appeal" if appeal_regime_active else "normal",
            "trigger": ",".join(appeal_trigger_reasons),
            "promotion_ceiling": "investigative_scaffold" if appeal_regime_active else "standard",
            "appeal_candidates": [warning.get("target_id") for warning in soft_repulsion_warnings if warning.get("target_id")],
            "activation_score": round(
                contradiction_pressure
                + 0.1 * len(unresolved_rival_hypothesis_ids)
                + 0.05 * len(soft_repulsion_warnings),
                3,
            ),
        },
        "rupture_energy_events": [
            {
                "event_id": f"rupture-{run_id or session_id or 'session'}-1",
                "source": "continuous_critic",
                "released_energy": rupture_energy,
                "trigger": "contradiction_pressure_retry",
                "assigned_mode": "full",
                "assigned_role": "agentic_loop",
            }
        ] if rupture_energy > 0.0 else [],
        "forbidden_moves": [
            "Do not restate rejected hypotheses as valid without new evidence.",
            "Do not finalize challenged claims without resolving or abandoning them explicitly.",
        ],
        "required_outputs": [
            "updated_hypothesis_table",
            "claim_resolution_log",
            "explicit_abandonments",
        ],
    }
    packet["negative_memory_summary"] = build_negative_memory_summary(packet, negative_memory_mode="full")
    return packet

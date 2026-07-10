"""Thin adapter over EpistemicFirewall extracted from AgenticDriver (DD-I08).

Moves the two pure helper methods (_attach_epistemic_firewall_verdict,
_build_pre_routing_firewall_result) and the single instantiation call out of
the driver so the driver no longer holds any construction logic for
EpistemicFirewall — it is a dependency-owned contract from OBSERVABILITY_AND_SECURITY.

Exports
-------
FirewallVerdict                   – re-exported for import consolidation
evaluate_pre_routing_firewall     – thin factory call; returns FirewallVerdict
attach_firewall_verdict_to_result – pure dict mutator (was driver method)
build_pre_routing_firewall_result – pure dict builder (was driver method)
"""

from __future__ import annotations

from typing import Any, Dict

from ..cold_evidence import EpistemicFirewall, FirewallVerdict  # noqa: F401 (re-export)


def evaluate_pre_routing_firewall(query: str) -> FirewallVerdict:
    """Evaluate the epistemic firewall before query routing.

    Wraps ``EpistemicFirewall().evaluate_pre_routing`` so the driver never
    constructs the firewall object directly.
    """
    return EpistemicFirewall().evaluate_pre_routing(query=query)


def attach_firewall_verdict_to_result(
    *,
    result: Dict[str, Any],
    verdict: FirewallVerdict,
) -> None:
    """Stamp *verdict* payload into all canonical positions of *result*.

    Mutates result in-place; returns None.  Extracted from
    ``AgenticDriver._attach_epistemic_firewall_verdict``.
    """
    verdict_payload = verdict.to_dict()
    result["epistemic_firewall"] = verdict_payload
    runtime_state = result.get("runtime")
    if not isinstance(runtime_state, dict):
        runtime_state = {}
        result["runtime"] = runtime_state
    runtime_state["epistemic_firewall"] = verdict_payload
    final_result = result.get("final_result")
    if isinstance(final_result, dict):
        final_result["epistemic_firewall"] = verdict_payload
        dossier_envelope = final_result.get("dossier_envelope")
        if isinstance(dossier_envelope, dict):
            dossier_envelope["epistemic_firewall"] = verdict_payload


def build_pre_routing_firewall_result(
    *,
    session_id: str,
    run_id: str,
    user_query: str,
    verdict: FirewallVerdict,
) -> Dict[str, Any]:
    """Build the canonical blocked-by-firewall result dict.

    Extracted from ``AgenticDriver._build_pre_routing_firewall_result``.
    """
    rationale = "; ".join(verdict.reasons) or "Query was blocked by the epistemic firewall before routing."
    return {
        "session_id": session_id,
        "run_id": run_id,
        "execution_path": "pre_routing_firewall",
        "runtime": {
            "transport_path": "pre_routing_firewall",
            "degradation_flags": ["pre_routing_firewall_block"],
            "fallbacks_used": [],
            "capabilities_unavailable": [],
            "epistemic_firewall": verdict.to_dict(),
        },
        "final_result": {
            "summary": "The request was halted before execution because it encoded an unsupported high-certainty premise.",
            "answer": rationale,
            "claims": [],
            "sources": [],
            "artifacts": [],
            "findings": [],
            "query": user_query,
        },
        "epistemic_firewall": verdict.to_dict(),
    }

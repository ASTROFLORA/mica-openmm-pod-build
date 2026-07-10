from __future__ import annotations

from typing import Any, Dict, List, Optional

from models.analysis import ProtocolCue


class DynamicCueRegistry:
    def __init__(self) -> None:
        self._packs: Dict[str, List[Dict[str, Any]]] = {
            "default_scientific_light": [
                {
                    "cue_id": "intake_scientific_scope_check",
                    "label": "Check scientific scope at intake",
                    "phase": "intake",
                    "priority": "high",
                    "mode": "blocking",
                    "question": "Is the query scientifically scoped enough to execute meaningful research steps?",
                    "pass_condition": "query contains a scientific target, objective, or biological subject",
                    "fail_action": "pause",
                    "rationale": "Prevents vague or contaminated sessions from entering expensive execution.",
                },
                {
                    "cue_id": "planning_counter_hypothesis_check",
                    "label": "Keep a competing explanation alive",
                    "phase": "planning",
                    "priority": "high",
                    "mode": "audit",
                    "question": "Has the plan preserved at least one competing explanation or falsification target?",
                    "fail_action": "revise_plan",
                    "rationale": "Preserves ACH-like competition before evidence pruning.",
                },
                {
                    "cue_id": "tool_output_capture_check",
                    "label": "Capture actionable output from each tool",
                    "phase": "post_tool",
                    "priority": "normal",
                    "mode": "audit",
                    "question": "Did this tool produce an actionable artifact or observation?",
                    "fail_action": "warn",
                    "rationale": "Prevents decorative tool use without downstream evidence value.",
                },
                {
                    "cue_id": "promotion_evidence_gate",
                    "label": "Block unsupported claim promotion",
                    "phase": "promotion",
                    "priority": "critical",
                    "mode": "blocking",
                    "question": "Does the current conclusion expose contradiction, missing control, and next evidence step?",
                    "fail_action": "contradiction_search_required",
                    "rationale": "Prevents unsupported publication-style conclusions.",
                },
            ],
            "kinase_network_audit_v1": [
                {
                    "cue_id": "structure_identity_check",
                    "label": "Verify structure identity before interpretation",
                    "phase": "pre_tool",
                    "priority": "high",
                    "mode": "blocking",
                    "trigger_capabilities": ["structure.model.prediction", "structure.analysis", "structure.kg"],
                    "trigger_study_types": ["kinase_network_audit"],
                    "question": "Have chain identity, residue numbering, construct boundaries, and missing loops been checked?",
                    "required_artifacts": ["structure_summary"],
                    "pass_condition": "structure input resolves to pdb_id, query, or KG target",
                    "fail_action": "pause",
                    "rationale": "Prevents incorrect kinase structure interpretation.",
                },
                {
                    "cue_id": "literature_objective_check",
                    "label": "Define literature objective before scan",
                    "phase": "pre_tool",
                    "priority": "high",
                    "mode": "blocking",
                    "trigger_capabilities": ["literature.search.primary", "literature.search.deep"],
                    "trigger_study_types": ["kinase_network_audit"],
                    "question": "Is the literature query explicit enough to recover primary evidence rather than generic summaries?",
                    "required_artifacts": ["literature_query"],
                    "pass_condition": "query length > 3 and not purely generic",
                    "fail_action": "revise_plan",
                    "rationale": "Improves primary-evidence hygiene for kinase signaling runs.",
                },
                {
                    "cue_id": "literature_primary_evidence_check",
                    "label": "Check primary evidence yield",
                    "phase": "post_tool",
                    "priority": "normal",
                    "mode": "audit",
                    "trigger_capabilities": ["literature.search.primary", "literature.search.deep"],
                    "trigger_study_types": ["kinase_network_audit"],
                    "question": "Did the scan return primary evidence or only sparse/no results?",
                    "fail_action": "warn",
                    "rationale": "Prevents thin literature evidence from silently becoming a conclusion.",
                },
            ],
        }

    def load_cue_pack(
        self,
        cue_pack_id: str,
        *,
        aft_adjustments: Optional[List[Dict[str, Any]]] = None,
    ) -> List[ProtocolCue]:
        base: List[Dict[str, Any]] = list(self._packs["default_scientific_light"])
        if cue_pack_id != "default_scientific_light":
            base.extend(list(self._packs.get(cue_pack_id) or []))
        elif cue_pack_id in self._packs:
            base = list(self._packs[cue_pack_id])

        merged_by_id: Dict[str, ProtocolCue] = {}
        for item in base:
            cue = ProtocolCue(**item)
            merged_by_id[cue.cue_id] = cue
        merged: List[ProtocolCue] = list(merged_by_id.values())
        if not aft_adjustments:
            return merged
        by_id = {cue.cue_id: cue for cue in merged}
        for adjustment in aft_adjustments:
            cue_id = str(adjustment.get("cue_id") or "").strip()
            if not cue_id or cue_id not in by_id:
                continue
            cue = by_id[cue_id].model_copy(deep=True)
            if adjustment.get("priority"):
                cue.priority = str(adjustment["priority"])
            if adjustment.get("pass_condition"):
                cue.pass_condition = str(adjustment["pass_condition"])
            if adjustment.get("fail_action"):
                cue.fail_action = str(adjustment["fail_action"])
            cue.is_aft_optimized = True
            by_id[cue_id] = cue
        return list(by_id.values())


def default_cue_pack_for_study_type(study_type: str, strictness: str) -> str:
    normalized = str(study_type or "").strip().lower()
    if normalized == "kinase_network_audit":
        return "kinase_network_audit_v1"
    return "default_scientific_light" if strictness != "generic" else "default_scientific_light"


REGISTRY = DynamicCueRegistry()

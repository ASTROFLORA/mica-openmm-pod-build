from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models.analysis import PromptNode, PromptProtocol, ProtocolRuntimeEnvelope, ProtocolType, ScientificProtocol

from .protocol_cue_registry import REGISTRY, default_cue_pack_for_study_type


_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_STUDY_MANIFEST = _ROOT / "tools" / "mica_agent_live_study_cases.json"

_CANONICAL_STUDY_TYPE_ALIASES: Dict[str, str] = {
    "tp53_dna_damage_audit": "dna_damage_response_audit",
    "egfr_resistance_landscape": "targeted_therapy_resistance_landscape",
    "kras_g12c_intervention_gaps": "mutant_selective_intervention_gap_analysis",
    "braf_mapk_rewiring_audit": "mapk_rewiring_and_relapse_audit",
    "alk_fusion_precision_audit": "fusion_driven_precision_oncology_audit",
    "brca1_repair_pathway_gaps": "dna_repair_pathway_gap_study",
    "parp1_synthetic_lethality_audit": "synthetic_lethality_evidence_audit",
    "jak2_myeloproliferative_audit": "driver_mutation_disease_mechanism_audit",
    "stat3_inflammation_oncology_bridge": "inflammation_oncology_bridge_audit",
    "wnk1_spak_osr1_network_audit": "kinase_network_audit",
    "kinase_network_and_regulator_audit": "kinase_network_audit",
    "cftr_gating_rescue_audit": "protein_rescue_and_gating_study",
    "ace2_host_virus_interface_audit": "host_pathogen_interface_audit",
    "tnf_inflammation_intervention_audit": "inflammatory_intervention_audit",
    "il6r_cytokine_blockade_audit": "cytokine_blockade_and_biomarker_audit",
    "apoe_neurodegeneration_risk_audit": "risk_biology_and_neurodegeneration_audit",
    "lrrk2_parkinson_mechanism_audit": "neurodegenerative_kinase_mechanism_audit",
    "snca_aggregation_evidence_audit": "aggregation_and_toxicity_evidence_audit",
    "hif1a_hypoxia_program_audit": "hypoxia_program_audit",
    "esr1_endocrine_resistance_audit": "endocrine_resistance_and_receptor_state_audit",
    "mtor_growth_control_audit": "growth_control_and_pathway_integration_audit",
}


def _load_manifest_cases() -> List[Dict[str, Any]]:
    try:
        payload = json.loads(_DEFAULT_STUDY_MANIFEST.read_text(encoding="utf-8"))
        return list((payload or {}).get("cases") or [])
    except Exception:
        return []


def infer_study_case(query: str) -> Optional[Dict[str, Any]]:
    lower_query = str(query or "").strip().lower()
    if not lower_query:
        return None
    for case in _load_manifest_cases():
        case_id = str(case.get("id") or "").strip().lower()
        protein = str(case.get("protein") or "").strip().lower()
        if case_id and case_id in lower_query:
            return case
        if protein and protein in lower_query:
            return case
    return None


def _canonicalize_study_type(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return _CANONICAL_STUDY_TYPE_ALIASES.get(normalized, normalized or "scientific_general")


def infer_study_type(query: str, benchmark_case: Optional[Dict[str, Any]] = None) -> str:
    if benchmark_case:
        for candidate in (benchmark_case.get("study_type"), benchmark_case.get("id")):
            canonical = _canonicalize_study_type(str(candidate or ""))
            if canonical and canonical != "scientific_general":
                return canonical
    lower_query = str(query or "").lower()
    if any(token in lower_query for token in ("wnk1", "spak", "osr1", "kinase")):
        return "kinase_network_audit"
    return "scientific_general"


def infer_protocol_type(query: str, study_type: str) -> ProtocolType:
    lower_query = str(query or "").lower()
    if any(token in lower_query for token in ("structure", "pdb", "binding", "domain")):
        return ProtocolType.STRUCTURE_PREDICTION
    if study_type == "kinase_network_audit":
        return ProtocolType.PROTEIN_FUNCTION_ANALYSIS
    return ProtocolType.PROTEIN_FUNCTION_ANALYSIS


def build_prompt_protocol(query: str, tool_names: List[str], study_type: str) -> PromptProtocol:
    nodes: List[PromptNode] = []
    previous_node_id: Optional[str] = None
    for index, tool_name in enumerate(tool_names or [], start=1):
        node_id = f"pn-{index:02d}-{tool_name}"
        nodes.append(
            PromptNode(
                node_id=node_id,
                tool_name=tool_name,
                parameters={"query": query},
                dependencies=[previous_node_id] if previous_node_id else [],
            )
        )
        previous_node_id = node_id
    return PromptProtocol(
        protocol_type=infer_protocol_type(query, study_type),
        nodes=nodes,
        created_by="scientific_protocol_cues_runtime",
        estimated_duration=max(300, len(nodes) * 90),
    )


def build_protocol_runtime(
    *,
    query: str,
    tool_names: List[str],
    strictness: str = "scientific_light",
    aft_adjustments: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[ScientificProtocol, PromptProtocol, ProtocolRuntimeEnvelope, Optional[Dict[str, Any]]]:
    benchmark_case = infer_study_case(query)
    study_type = infer_study_type(query, benchmark_case)
    prompt_protocol = build_prompt_protocol(query, tool_names, study_type)
    prompt_protocol_hash = hashlib.sha256(prompt_protocol.model_dump_json().encode("utf-8")).hexdigest()[:8]
    cue_pack_id = default_cue_pack_for_study_type(study_type, strictness)
    cues = REGISTRY.load_cue_pack(cue_pack_id, aft_adjustments=aft_adjustments)
    protocol_id = f"spc-{prompt_protocol.protocol_id}"
    envelope = ProtocolRuntimeEnvelope(
        protocol_id=protocol_id,
        protocol_label=str((benchmark_case or {}).get("id") or study_type).replace("_", " ").title(),
        prompt_protocol_hash=prompt_protocol_hash,
        study_type=study_type,
        cue_pack_id=cue_pack_id,
        strictness=str(strictness or "scientific_light"),
        cues=cues,
        cue_counts={
            "total": len(cues),
            "pending": len(cues),
            "passed": 0,
            "failed": 0,
            "skipped": 0,
        },
    )
    scientific_protocol = ScientificProtocol(
        input={
            "query": query,
            "study_type": study_type,
            "benchmark_case_id": (benchmark_case or {}).get("id"),
        },
        plan={
            "steps": [
                {
                    "prompt_node_id": node.node_id,
                    "tool_name": node.tool_name,
                    "dependencies": list(node.dependencies),
                }
                for node in prompt_protocol.nodes
            ],
            "dependencies": [list(node.dependencies) for node in prompt_protocol.nodes],
            "estimated_resources": {"tool_count": len(prompt_protocol.nodes)},
            "confidence": 0.4,
        },
        metadata={
            "version": "1.0.0",
            "contributors": ["mica-core", "scientific-protocol-cues-runtime"],
            "tags": [study_type, strictness],
            "citations": [],
            "benchmark_study_type_label": (benchmark_case or {}).get("study_type"),
            "protocol_runtime": {
                "prompt_protocol_hash": prompt_protocol_hash,
                "cue_pack_id": cue_pack_id,
                "active_phase": "intake",
                "strictness": strictness,
                "aft_cycle": 0,
            },
        },
    )
    return scientific_protocol, prompt_protocol, envelope, benchmark_case

#!/usr/bin/env python
"""
Export JSONL datasets for finetuning LLMs on **LMP XML** (protein structure/function).

**LMP-centric tasks (the real value):**
1. protein2xml      – UniProt ID + gene + states → multi-state LMP XML
2. xml2features     – LMP XML → structured JSON (domains, PTMs, ligands, conformations)
3. xml_state_diff   – XML state A vs B → semantic diff (PTM status changes, conformation triggers)
4. xml_repair       – invalid XML + errors → corrected XML
5. xml_critique     – XML → validation report (schema/vocab/causality/biology)

**DLM tasks (text scanning, secondary):**
6. text2dlm         – raw text → DLM JSON (entities, sections)

Splits are by `uniprot_id` (not row-level) to avoid leakage.

Usage:
    python scripts/export_finetune_jsonl.py \\
        --cache-dir lmp_cache \\
        --out-dir outputs/finetune_jsonl \\
        --states Apo_Inactive Active \\
        --splits 0.8 0.1 0.1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Allow running without pip install
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# NOTE:
# This script remains as a stable CLI entrypoint.
# The importable entrypoint lives in `bsm.lmp.finetune.export_llm_jsonl`.

from mica.memory.dlm.encoder import DLMEncoder, EncodedDocument
from mica.memory.dlm_lmp.pipeline import DLM_LMP_Pipeline
from bsm.lmp.generator import LMPGenerator
from bsm.lmp.validator import LMPValidator, ValidationResult
from bsm.lmp.parser import LMPParser


# ---------------------------------------------------------------------------
# Data classes for JSONL records
# ---------------------------------------------------------------------------

@dataclass
class FinetuneRecord:
    """Single JSONL record for finetuning."""
    task: str  # text2dlm | dlm2xml | xml_repair | xml_critique
    split: str  # train | val | test
    uniprot_id: Optional[str]
    input: str  # task input (text, JSON, or XML)
    output: str  # expected output
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Splitting logic (by uniprot_id)
# ---------------------------------------------------------------------------

def assign_split(uniprot_id: str, splits: Tuple[float, float, float]) -> str:
    """Deterministic split assignment based on hash of uniprot_id."""
    h = int(hashlib.sha256(uniprot_id.encode()).hexdigest(), 16) % 1000
    train_thresh = int(splits[0] * 1000)
    val_thresh = train_thresh + int(splits[1] * 1000)
    if h < train_thresh:
        return "train"
    elif h < val_thresh:
        return "val"
    else:
        return "test"


# ---------------------------------------------------------------------------
# Task generators
# ---------------------------------------------------------------------------

def generate_text2dlm(
    text: str,
    encoded: EncodedDocument,
    uniprot_ids: Set[str],
    splits: Tuple[float, float, float],
) -> List[FinetuneRecord]:
    """Generate text → DLM JSON records."""
    records = []
    # Use first detected protein as anchor for split (or "unknown")
    anchor = next(iter(uniprot_ids), "unknown")
    split = assign_split(anchor, splits)

    dlm_output = {
        "sections": [
            {"section": s.section, "start": s.start_idx, "end": s.end_idx}
            for s in encoded.sections
        ],
        "entities": encoded.entities,
        "metadata": encoded.metadata,
    }

    records.append(
        FinetuneRecord(
            task="text2dlm",
            split=split,
            uniprot_id=anchor if anchor != "unknown" else None,
            input=text,
            output=json.dumps(dlm_output, ensure_ascii=False),
            metadata={"entity_count": len(encoded.entities)},
        )
    )
    return records


def generate_dlm2xml(
    encoded: EncodedDocument,
    enriched_entities: List[Any],
    generator: LMPGenerator,
    validator: LMPValidator,
    splits: Tuple[float, float, float],
    states: List[str],
) -> List[FinetuneRecord]:
    """Generate DLM JSON → LMP XML records (one per uniprot_id × state)."""
    records = []

    # Group by uniprot_id
    uniprot_map: Dict[str, Dict[str, Any]] = {}
    for ent in enriched_entities:
        uid = getattr(ent, "uniprot_id", None)
        if not uid:
            continue
        if uid not in uniprot_map:
            uniprot_map[uid] = {
                "gene_name": (ent.metadata or {}).get("gene_name") or ent.text,
                "mentions": [],
            }
        uniprot_map[uid]["mentions"].append(
            {"text": ent.text, "section": ent.section, "type": ent.entity_type}
        )

    for uid, info in uniprot_map.items():
        split = assign_split(uid, splits)
        try:
            xml_by_state = generator.generate_multi_state(
                uniprot_id=uid,
                gene_name=info["gene_name"],
                states=states,
            )
        except Exception as e:
            # Skip if generation fails (missing cache, etc.)
            continue

        for state, xml_str in xml_by_state.items():
            # Validate (basic mode)
            # We don't write to disk; validate from string by writing temp
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(xml_str)
                tmp_path = Path(tmp.name)

            val_result = validator.validate(
                tmp_path, validate_biology=False
            )
            tmp_path.unlink(missing_ok=True)

            dlm_input = {
                "uniprot_id": uid,
                "gene_name": info["gene_name"],
                "mentions": info["mentions"],
                "request_state": state,
            }

            records.append(
                FinetuneRecord(
                    task="dlm2xml",
                    split=split,
                    uniprot_id=uid,
                    input=json.dumps(dlm_input, ensure_ascii=False),
                    output=xml_str,
                    metadata={
                        "state": state,
                        "valid": val_result.is_valid,
                        "errors": len(val_result.errors),
                        "warnings": len(val_result.warnings),
                    },
                )
            )

    return records


def generate_xml_critique(
    uid: str,
    xml_str: str,
    val_result: ValidationResult,
    splits: Tuple[float, float, float],
) -> FinetuneRecord:
    """Generate XML → critique (validation report) record."""
    split = assign_split(uid, splits)

    critique_output = {
        "is_valid": val_result.is_valid,
        "errors": [
            {"category": e.category, "message": e.message, "path": e.element_path}
            for e in val_result.errors
        ],
        "warnings": [
            {"category": w.category, "message": w.message, "path": w.element_path}
            for w in val_result.warnings
        ],
    }

    return FinetuneRecord(
        task="xml_critique",
        split=split,
        uniprot_id=uid,
        input=xml_str,
        output=json.dumps(critique_output, ensure_ascii=False),
        metadata={"error_count": len(val_result.errors)},
    )


def generate_xml_repair(
    uid: str,
    broken_xml: str,
    fixed_xml: str,
    errors: List[Dict[str, str]],
    splits: Tuple[float, float, float],
) -> FinetuneRecord:
    """Generate broken XML + errors → fixed XML record."""
    split = assign_split(uid, splits)

    repair_input = {"xml": broken_xml, "errors": errors}

    return FinetuneRecord(
        task="xml_repair",
        split=split,
        uniprot_id=uid,
        input=json.dumps(repair_input, ensure_ascii=False),
        output=fixed_xml,
        metadata={"error_count": len(errors)},
    )


# ---------------------------------------------------------------------------
# LMP-CENTRIC TASK GENERATORS (The real value!)
# ---------------------------------------------------------------------------

def generate_protein2xml(
    uid: str,
    gene_name: str,
    xml_by_state: Dict[str, str],
    splits: Tuple[float, float, float],
) -> List[FinetuneRecord]:
    """
    Generate protein2xml records: UniProt ID + gene → multi-state LMP XML.
    
    This is the core LMP generation task where the model learns to
    produce full molecular portraits from minimal input.
    """
    records = []
    split = assign_split(uid, splits)

    for state, xml_str in xml_by_state.items():
        prompt_input = {
            "uniprot_id": uid,
            "gene_name": gene_name,
            "target_state": state,
            "instruction": (
                f"Generate a Living Molecular Portrait (LMP) XML for {gene_name} "
                f"({uid}) in the {state} conformational state. Include domains, "
                f"PTMs with causal triggers, binding sites, ligands, and "
                f"conformational features with ESE signatures."
            ),
        }

        records.append(
            FinetuneRecord(
                task="protein2xml",
                split=split,
                uniprot_id=uid,
                input=json.dumps(prompt_input, ensure_ascii=False),
                output=xml_str,
                metadata={"state": state, "gene_name": gene_name},
            )
        )

    return records


def extract_lmp_features(xml_str: str) -> Dict[str, Any]:
    """
    Parse LMP XML and extract structured features for xml2features task.
    
    Returns:
        Dict with domains, PTMs, binding_sites, ligands, conformations, interfaces
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return {"error": "XML parse error"}

    features: Dict[str, Any] = {
        "protein": {
            "uniprot_id": root.get("uniprot_id"),
            "gene_name": root.get("gene_name"),
            "organism": root.get("organism"),
            "state": root.get("state"),
        },
        "domains": [],
        "ptms": [],
        "binding_sites": [],
        "ligands": [],
        "conformations": [],
        "interfaces": [],
        "structural": {},
    }

    # Extract domains
    for domain in root.findall(".//Domain"):
        domain_info = {
            "name": domain.get("name"),
            "type": domain.get("type"),
            "start": int(domain.get("start", 0)),
            "end": int(domain.get("end", 0)),
            "motifs": [],
            "ptm_count": len(domain.findall("PTM")),
            "binding_site_count": len(domain.findall("BindingSite")),
        }
        for motif in domain.findall("Motif"):
            domain_info["motifs"].append({
                "name": motif.get("name"),
                "pattern": motif.get("pattern"),
                "start": int(motif.get("start", 0)),
                "end": int(motif.get("end", 0)),
            })
        features["domains"].append(domain_info)

    # Extract PTMs (from anywhere in the document)
    for ptm in root.findall(".//PTM"):
        ptm_info = {
            "id": ptm.get("id"),
            "type": ptm.get("type"),
            "residue": ptm.get("residue"),
            "position": int(ptm.get("position", 0)),
            "status": ptm.get("status"),
            "causal_trigger": ptm.get("causal_trigger"),
        }
        features["ptms"].append(ptm_info)

    # Extract binding sites and ligands
    for bs in root.findall(".//BindingSite"):
        bs_info = {
            "type": bs.get("type"),
            "residues": bs.get("residues"),
            "ligands": [],
        }
        for lig in bs.findall("Ligand"):
            lig_info = {
                "id": lig.get("id"),
                "name": lig.get("name"),
                "type": lig.get("type"),
                "effect": lig.get("effect"),
            }
            bs_info["ligands"].append(lig_info)
            features["ligands"].append(lig_info)
        features["binding_sites"].append(bs_info)

    # Extract conformations
    for conf in root.findall(".//Conformation"):
        conf_info = {
            "state_name": conf.get("state_name"),
            "trigger": conf.get("trigger"),
            "ese_signature": conf.get("ese_signature"),
            "confidence": conf.get("confidence"),
            "feature_states": [],
        }
        for fs in conf.findall("FeatureState"):
            conf_info["feature_states"].append({
                "feature_ref": fs.get("feature_ref"),
                "state": fs.get("state"),
                "description": fs.text.strip() if fs.text else None,
            })
        features["conformations"].append(conf_info)

    # Extract interfaces
    for iface in root.findall(".//Interface"):
        features["interfaces"].append({
            "partner_protein": iface.get("partner_protein"),
            "interface_residues": iface.get("interface_residues"),
            "type": iface.get("type"),
        })

    # --- Structural data extraction (v4.1) ---
    structural_data = {}

    # AlphaFold model
    for ns_prefix in ("", "{http://lmp.bsm.org}"):
        af_elem = root.find(f".//{ns_prefix}AlphaFoldModel")
        if af_elem is not None:
            structural_data["alphafold"] = {
                "entry_id": af_elem.get("entry_id", ""),
                "avg_plddt": af_elem.get("avg_plddt", ""),
                "model_date": af_elem.get("model_date", ""),
                "version": af_elem.get("version", ""),
                "uniprot_start": af_elem.get("uniprot_start", ""),
                "uniprot_end": af_elem.get("uniprot_end", ""),
            }
            # PAE summary
            pae_elem = af_elem.find(f"{ns_prefix}PAESummary")
            if pae_elem is not None:
                structural_data["alphafold"]["pae_mean"] = pae_elem.get("mean_pae", "")
                structural_data["alphafold"]["pae_max"] = pae_elem.get("max_pae", "")
            break

    # Secondary Structure
    for ns_prefix in ("", "{http://lmp.bsm.org}"):
        ss_elem = root.find(f".//{ns_prefix}SecondaryStructure")
        if ss_elem is not None:
            ss_data = {
                "method": ss_elem.get("method", ""),
                "helix_fraction": ss_elem.get("helix_fraction", ""),
                "strand_fraction": ss_elem.get("strand_fraction", ""),
                "coil_fraction": ss_elem.get("coil_fraction", ""),
                "segments": [],
            }
            for seg in ss_elem.findall(f"{ns_prefix}Segment"):
                ss_data["segments"].append({
                    "type": seg.get("type", ""),
                    "start": seg.get("start", ""),
                    "end": seg.get("end", ""),
                    "chain": seg.get("chain", ""),
                    "length": seg.get("length", ""),
                })
            structural_data["secondary_structure"] = ss_data
            break

    # Structural Quality
    for ns_prefix in ("", "{http://lmp.bsm.org}"):
        sq_elem = root.find(f".//{ns_prefix}StructuralQuality")
        if sq_elem is not None:
            sq_data = {"source": sq_elem.get("source", "")}
            rg_elem = sq_elem.find(f"{ns_prefix}Rg")
            if rg_elem is not None:
                sq_data["rg"] = rg_elem.get("value", "")
                sq_data["rg_unit"] = rg_elem.get("unit", "angstrom")
            rama_elem = sq_elem.find(f"{ns_prefix}Ramachandran")
            if rama_elem is not None:
                sq_data["ramachandran_favored"] = rama_elem.get("favored", "")
                sq_data["ramachandran_allowed"] = rama_elem.get("allowed", "")
                sq_data["ramachandran_outlier"] = rama_elem.get("outlier", "")
                sq_data["ramachandran_favored_pct"] = rama_elem.get("favored_pct", "")
            cd_elem = sq_elem.find(f"{ns_prefix}ContactDensity")
            if cd_elem is not None:
                sq_data["total_contacts"] = cd_elem.get("total_contacts", "")
                sq_data["contacts_per_residue"] = cd_elem.get("contacts_per_residue", "")
            structural_data["quality"] = sq_data
            break

    # Network Annotation
    for ns_prefix in ("", "{http://lmp.bsm.org}"):
        na_elem = root.find(f".//{ns_prefix}NetworkAnnotation")
        if na_elem is not None:
            hubs = []
            for hub in na_elem.findall(f"{ns_prefix}Hub"):
                hubs.append({
                    "residue_id": hub.get("residue_id", ""),
                    "chain": hub.get("chain", ""),
                    "betweenness": hub.get("betweenness", ""),
                    "degree": hub.get("degree", ""),
                    "allosteric_candidate": hub.get("allosteric_candidate", ""),
                })
            structural_data["network_hubs"] = hubs
            break

    if structural_data:
        features["structural"] = structural_data

    return features


def generate_xml2features(
    uid: str,
    xml_str: str,
    state: str,
    splits: Tuple[float, float, float],
) -> FinetuneRecord:
    """
    Generate xml2features record: LMP XML → structured JSON features.
    
    This teaches the model to parse and understand LMP structure,
    extracting domains, PTMs, causality, conformations, etc.
    """
    split = assign_split(uid, splits)
    features = extract_lmp_features(xml_str)

    return FinetuneRecord(
        task="xml2features",
        split=split,
        uniprot_id=uid,
        input=xml_str,
        output=json.dumps(features, ensure_ascii=False, indent=2),
        metadata={
            "state": state,
            "domain_count": len(features.get("domains", [])),
            "ptm_count": len(features.get("ptms", [])),
            "ligand_count": len(features.get("ligands", [])),
        },
    )


def compute_state_diff(
    features_a: Dict[str, Any],
    features_b: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compute semantic diff between two LMP states.
    
    Focuses on biologically meaningful differences:
    - PTM status changes (phosphorylated → unphosphorylated)
    - Conformation changes (DFG-in vs DFG-out, etc.)
    - Ligand occupancy changes
    """
    diff: Dict[str, Any] = {
        "state_a": features_a.get("protein", {}).get("state"),
        "state_b": features_b.get("protein", {}).get("state"),
        "ptm_changes": [],
        "conformation_changes": [],
        "ligand_changes": [],
    }

    # Index PTMs by position for comparison
    ptms_a = {p["position"]: p for p in features_a.get("ptms", [])}
    ptms_b = {p["position"]: p for p in features_b.get("ptms", [])}

    all_positions = set(ptms_a.keys()) | set(ptms_b.keys())
    for pos in sorted(all_positions):
        ptm_a = ptms_a.get(pos)
        ptm_b = ptms_b.get(pos)

        if ptm_a and ptm_b:
            # Both exist - check for status change
            if ptm_a.get("status") != ptm_b.get("status"):
                diff["ptm_changes"].append({
                    "position": pos,
                    "residue": ptm_a.get("residue"),
                    "type": ptm_a.get("type"),
                    "status_a": ptm_a.get("status"),
                    "status_b": ptm_b.get("status"),
                    "change": f"{ptm_a.get('status')} → {ptm_b.get('status')}",
                })
        elif ptm_a and not ptm_b:
            diff["ptm_changes"].append({
                "position": pos,
                "residue": ptm_a.get("residue"),
                "type": ptm_a.get("type"),
                "change": f"removed (was {ptm_a.get('status')})",
            })
        elif ptm_b and not ptm_a:
            diff["ptm_changes"].append({
                "position": pos,
                "residue": ptm_b.get("residue"),
                "type": ptm_b.get("type"),
                "change": f"added ({ptm_b.get('status')})",
            })

    # Compare conformations
    confs_a = {c.get("state_name"): c for c in features_a.get("conformations", [])}
    confs_b = {c.get("state_name"): c for c in features_b.get("conformations", [])}

    for name in set(confs_a.keys()) | set(confs_b.keys()):
        conf_a = confs_a.get(name)
        conf_b = confs_b.get(name)

        if conf_a and conf_b:
            # Compare ESE signatures
            ese_a = conf_a.get("ese_signature")
            ese_b = conf_b.get("ese_signature")
            if ese_a != ese_b:
                diff["conformation_changes"].append({
                    "state_name": name,
                    "ese_a": ese_a,
                    "ese_b": ese_b,
                    "feature_states_changed": True,
                })
        elif conf_a:
            diff["conformation_changes"].append({
                "state_name": name,
                "change": "removed in state B",
            })
        elif conf_b:
            diff["conformation_changes"].append({
                "state_name": name,
                "change": "added in state B",
            })

    # Compare ligands
    ligs_a = {l.get("id"): l for l in features_a.get("ligands", []) if l.get("id")}
    ligs_b = {l.get("id"): l for l in features_b.get("ligands", []) if l.get("id")}

    for lig_id in set(ligs_a.keys()) | set(ligs_b.keys()):
        lig_a = ligs_a.get(lig_id)
        lig_b = ligs_b.get(lig_id)

        if lig_a and not lig_b:
            diff["ligand_changes"].append({
                "ligand": lig_a.get("name"),
                "change": "unbound in state B",
            })
        elif lig_b and not lig_a:
            diff["ligand_changes"].append({
                "ligand": lig_b.get("name"),
                "change": "bound in state B",
            })

    return diff


def generate_xml_state_diff(
    uid: str,
    xml_state_a: str,
    xml_state_b: str,
    state_a: str,
    state_b: str,
    splits: Tuple[float, float, float],
) -> FinetuneRecord:
    """
    Generate xml_state_diff record: two LMP XMLs → semantic diff.
    
    This teaches the model to understand conformational transitions,
    PTM cascades, and allosteric regulation.
    """
    split = assign_split(uid, splits)

    features_a = extract_lmp_features(xml_state_a)
    features_b = extract_lmp_features(xml_state_b)
    diff = compute_state_diff(features_a, features_b)

    diff_input = {
        "state_a_xml": xml_state_a,
        "state_b_xml": xml_state_b,
        "instruction": (
            f"Compare the two LMP XMLs for {uid} and describe the molecular "
            f"differences between {state_a} and {state_b} states. Focus on "
            f"PTM status changes, conformational transitions, and ligand binding."
        ),
    }

    return FinetuneRecord(
        task="xml_state_diff",
        split=split,
        uniprot_id=uid,
        input=json.dumps(diff_input, ensure_ascii=False),
        output=json.dumps(diff, ensure_ascii=False, indent=2),
        metadata={
            "state_a": state_a,
            "state_b": state_b,
            "ptm_changes": len(diff.get("ptm_changes", [])),
            "conformation_changes": len(diff.get("conformation_changes", [])),
            "ligand_changes": len(diff.get("ligand_changes", [])),
        },
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_document(
    text: str,
    encoder: DLMEncoder,
    pipeline: DLM_LMP_Pipeline,
    generator: LMPGenerator,
    validator: LMPValidator,
    splits: Tuple[float, float, float],
    states: List[str],
) -> List[FinetuneRecord]:
    """Process a single document and generate all task records."""
    records: List[FinetuneRecord] = []

    # 1. Encode with DLM
    encoded = encoder.encode(text)

    # 2. Enrich with bridge pipeline
    bridge_result = pipeline.process_text(text)

    # Collect uniprot_ids
    uniprot_ids: Set[str] = set()
    for ent in bridge_result.enriched_entities:
        uid = getattr(ent, "uniprot_id", None)
        if uid:
            uniprot_ids.add(uid)

    # Task (secondary): text2dlm
    records.extend(generate_text2dlm(text, encoded, uniprot_ids, splits))

    # ===========================================================================
    # LMP-CENTRIC TASKS (the main value)
    # ===========================================================================
    
    # Build XML cache per protein to avoid redundant generation
    xml_cache: Dict[str, Dict[str, str]] = {}  # {uniprot_id: {state: xml_str}}
    gene_cache: Dict[str, str] = {}  # {uniprot_id: gene_name}

    for ent in bridge_result.enriched_entities:
        uid = getattr(ent, "uniprot_id", None)
        if not uid or uid in xml_cache:
            continue
        gene = (ent.metadata or {}).get("gene_name") or ent.text
        gene_cache[uid] = gene

        try:
            xml_by_state = generator.generate_multi_state(
                uniprot_id=uid, gene_name=gene, states=states
            )
            xml_cache[uid] = xml_by_state
        except Exception:
            continue

    # Generate LMP-centric tasks for each protein
    for uid, xml_by_state in xml_cache.items():
        gene = gene_cache.get(uid, uid)

        # === Task: protein2xml ===
        # UniProt ID + gene → multi-state LMP XML
        records.extend(generate_protein2xml(uid, gene, xml_by_state, splits))

        # === Task: xml2features ===
        # LMP XML → structured JSON (domains, PTMs, ligands, conformations)
        for state, xml_str in xml_by_state.items():
            records.append(generate_xml2features(uid, xml_str, state, splits))

        # === Task: xml_state_diff ===
        # Compare different states (if we have 2+ states)
        state_list = list(xml_by_state.keys())
        if len(state_list) >= 2:
            for i in range(len(state_list) - 1):
                state_a = state_list[i]
                state_b = state_list[i + 1]
                records.append(
                    generate_xml_state_diff(
                        uid,
                        xml_by_state[state_a],
                        xml_by_state[state_b],
                        state_a,
                        state_b,
                        splits,
                    )
                )

        # === Task: xml_critique ===
        # LMP XML → validation report
        for state, xml_str in xml_by_state.items():
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(xml_str)
                tmp_path = Path(tmp.name)

            val_result = validator.validate(tmp_path, validate_biology=True)
            tmp_path.unlink(missing_ok=True)

            records.append(generate_xml_critique(uid, xml_str, val_result, splits))

            # === Task: xml_repair ===
            # If errors, create repair task
            if val_result.errors:
                error_list = [
                    {"category": e.category, "message": e.message, "path": e.element_path}
                    for e in val_result.errors
                ]
                records.append(
                    generate_xml_repair(uid, xml_str, xml_str, error_list, splits)
                )

    # Legacy: dlm2xml (kept for compatibility but protein2xml is preferred)
    records.extend(
        generate_dlm2xml(
            encoded,
            bridge_result.enriched_entities,
            generator,
            validator,
            splits,
            states,
        )
    )

    return records


# ---------------------------------------------------------------------------
# LMP-FIRST mode: Process proteins directly (no text scanning)
# ---------------------------------------------------------------------------

def process_protein_direct(
    uniprot_id: str,
    gene_name: Optional[str],
    generator: LMPGenerator,
    validator: LMPValidator,
    splits: Tuple[float, float, float],
    states: List[str],
) -> List[FinetuneRecord]:
    """
    Generate LMP-centric records directly from a UniProt ID (no text input).
    
    This is the "pure LMP" mode — bypasses DLM text scanning entirely.
    """
    records: List[FinetuneRecord] = []

    # Use gene_name from cache if not provided
    if not gene_name:
        gene_name = uniprot_id  # Fallback to ID

    try:
        xml_by_state = generator.generate_multi_state(
            uniprot_id=uniprot_id, gene_name=gene_name, states=states
        )
    except Exception as e:
        print(f"  [SKIP] {uniprot_id}: {e}")
        return records

    # === Task: protein2xml ===
    records.extend(generate_protein2xml(uniprot_id, gene_name, xml_by_state, splits))

    # === Task: xml2features ===
    for state, xml_str in xml_by_state.items():
        records.append(generate_xml2features(uniprot_id, xml_str, state, splits))

    # === Task: xml_state_diff ===
    state_list = list(xml_by_state.keys())
    if len(state_list) >= 2:
        for i in range(len(state_list) - 1):
            state_a = state_list[i]
            state_b = state_list[i + 1]
            records.append(
                generate_xml_state_diff(
                    uniprot_id,
                    xml_by_state[state_a],
                    xml_by_state[state_b],
                    state_a,
                    state_b,
                    splits,
                )
            )

    # === Task: xml_critique ===
    for state, xml_str in xml_by_state.items():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(xml_str)
            tmp_path = Path(tmp.name)

        val_result = validator.validate(tmp_path, validate_biology=True)
        tmp_path.unlink(missing_ok=True)

        records.append(generate_xml_critique(uniprot_id, xml_str, val_result, splits))

        # === Task: xml_repair ===
        if val_result.errors:
            error_list = [
                {"category": e.category, "message": e.message, "path": e.element_path}
                for e in val_result.errors
            ]
            records.append(
                generate_xml_repair(uniprot_id, xml_str, xml_str, error_list, splits)
            )

    return records


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export JSONL for LLM finetuning on LMP (protein biology) XML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # LMP-first mode: directly from UniProt IDs (recommended!)
  python scripts/export_finetune_jsonl.py --proteins P12931,P00533,P04626

  # Text mode: extract from scientific text
  python scripts/export_finetune_jsonl.py --input-dir data/papers

  # Demo mode: built-in samples
  python scripts/export_finetune_jsonl.py
        """,
    )
    ap.add_argument(
        "--proteins",
        type=str,
        help="Comma-separated UniProt IDs for LMP-first mode (e.g., P12931,P00533)",
    )
    ap.add_argument(
        "--input-dir",
        type=Path,
        help="Directory with .txt files for text mode. If omitted, uses built-in samples.",
    )
    ap.add_argument("--cache-dir", type=Path, default=Path("lmp_cache"))
    ap.add_argument("--out-dir", type=Path, default=Path("outputs/finetune_jsonl"))
    ap.add_argument(
        "--splits",
        nargs=3,
        type=float,
        default=[0.8, 0.1, 0.1],
        metavar=("TRAIN", "VAL", "TEST"),
        help="Split ratios (must sum to 1.0)",
    )
    ap.add_argument(
        "--states",
        nargs="+",
        default=["Apo_Inactive", "Active"],
        help="Conformational states to generate",
    )
    args = ap.parse_args()

    splits = tuple(args.splits)
    if abs(sum(splits) - 1.0) > 0.01:
        print("ERROR: splits must sum to 1.0")
        return 1

    # Initialize components
    generator = LMPGenerator(cache_dir=args.cache_dir)
    validator = LMPValidator(strict=False)

    all_records: List[FinetuneRecord] = []

    # ===========================================================================
    # MODE 1: LMP-FIRST (--proteins) — The recommended path!
    # ===========================================================================
    if args.proteins:
        print("=" * 60)
        print("LMP-FIRST MODE: Processing proteins directly")
        print("=" * 60)
        protein_ids = [p.strip() for p in args.proteins.split(",") if p.strip()]

        for uid in protein_ids:
            print(f"Processing: {uid}")
            records = process_protein_direct(
                uniprot_id=uid,
                gene_name=None,  # Will be resolved from cache
                generator=generator,
                validator=validator,
                splits=splits,
                states=args.states,
            )
            all_records.extend(records)
            print(f"  → Generated {len(records)} records")

    # ===========================================================================
    # MODE 2: TEXT MODE (--input-dir or built-in samples)
    # ===========================================================================
    else:
        print("=" * 60)
        print("TEXT MODE: Processing scientific text via DLM → LMP pipeline")
        print("=" * 60)

        encoder = DLMEncoder()
        pipeline = DLM_LMP_Pipeline(
            cache_dir=args.cache_dir, enable_layer2=True, enable_layer3=False
        )

        # Collect input texts
        if args.input_dir and args.input_dir.exists():
            texts = [
                (f.stem, f.read_text(encoding="utf-8"))
                for f in sorted(args.input_dir.glob("*.txt"))
            ]
        else:
            # Built-in samples for demo
            texts = [
                (
                    "sample_src",
                    "ABSTRACT\nSRC is a proto-oncogene tyrosine-protein kinase.\n\n"
                    "RESULTS\nWe found significant correlation between SRC activity and breast cancer.\n\n"
                    "REFERENCES\nSmith et al. (2020) reported similar findings.",
                ),
                (
                    "sample_egfr",
                    "ABSTRACT\nEGFR mutations drive non-small cell lung cancer.\n\n"
                    "METHODS\nWe used Western blot to detect phosphorylation of EGFR at Y1068.\n\n"
                    "RESULTS\nEGFR inhibitors reduced tumor growth.",
                ),
            ]

        for doc_id, text in texts:
            print(f"Processing: {doc_id}")
            records = process_document(
                text, encoder, pipeline, generator, validator, splits, args.states
            )
            for r in records:
                r.metadata["doc_id"] = doc_id
            all_records.extend(records)
            print(f"  → Generated {len(records)} records")

    # Write JSONL files by split
    args.out_dir.mkdir(parents=True, exist_ok=True)

    split_files = {
        "train": args.out_dir / "train.jsonl",
        "val": args.out_dir / "val.jsonl",
        "test": args.out_dir / "test.jsonl",
    }
    for split, path in split_files.items():
        with open(path, "w", encoding="utf-8") as f:
            for r in all_records:
                if r.split == split:
                    f.write(r.to_json() + "\n")

    # Summary
    task_counts = {}
    split_counts = {"train": 0, "val": 0, "test": 0}
    for r in all_records:
        task_counts[r.task] = task_counts.get(r.task, 0) + 1
        split_counts[r.split] += 1

    print(f"\n=== Export Summary ===")
    print(f"Total records: {len(all_records)}")
    print(f"By task: {json.dumps(task_counts)}")
    print(f"By split: {json.dumps(split_counts)}")
    print(f"Output dir: {args.out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

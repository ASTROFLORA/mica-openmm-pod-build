"""
LMP XML → BUDO V3 Parser
=========================

Converts LMP v2–v4 XML (rich UniProt data, literature evidence, temporal
knowledge) into BUDO V3 sentient protein objects.

This parser bridges:
- LMP (XML acquisition layer, v2 through v4)
- BUDO V3 (canonical sentient entity)
- GraphRAG (Timescale + Neo4j persistence)

v4 additions (from DLM-LMP convergence):
- ``<literature_evidence>`` → BUDO provenance + cross_references
- ``<temporal_knowledge>``  → BUDO go_terms / kegg_pathways enrichment

Author: MICA Team / BSM Division
Date: 2026-01-21  (v4 support: 2026-04-01)
Phase: Integration (LMP → BUDO → GraphRAG)
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree as ET

from bsm.schemas.budo_v3 import (
    BudoV3,
    BudoPTM,
    BudoLigand,
    BudoConformation,
    BudoDomain,
    BudoInterface,
    BudoProvenance,
    BudoFunctionalState,
    BudoCrossReference,
    FunctionalState,
    ConfidenceLevel,
)
from bsm.schemas.cea import CEAEntity, ExternalReferences


logger = logging.getLogger(__name__)


class LMPParseError(Exception):
    """Raised when LMP XML parsing fails."""
    pass


async def parse_lmp_xml_to_budo(xml_path: str) -> BudoV3:
    """
    Parse LMP v2.0 XML file to BUDO V3 object.
    
    Args:
        xml_path: Path to LMP XML file (absolute or relative)
    
    Returns:
        BudoV3 object with complete annotations
    
    Raises:
        LMPParseError: If XML is malformed or required fields missing
        FileNotFoundError: If XML file doesn't exist
    
    Example:
        >>> budo = await parse_lmp_xml_to_budo("proteins/ABL1_HUMAN.xml")
        >>> print(budo.canonical_name)  # "ABL1_HUMAN"
        >>> print(len(budo.domains))    # 3
    """
    path = Path(xml_path)
    if not path.exists():
        raise FileNotFoundError(f"LMP XML file not found: {xml_path}")
    
    try:
        tree = ET.parse(str(path))
        root = tree.getroot()
    except ET.ParseError as e:
        raise LMPParseError(f"Invalid XML: {e}")
    
    # Extract core identity (required fields)
    # Handle both <Protein> as root and nested <Protein>
    protein_elem = root if root.tag == "Protein" else root.find("./Protein")
    if protein_elem is None:
        protein_elem = root  # Fallback to root
    
    try:
        uniprot_id = _get_required_text(protein_elem, "./UniProtID", "UniProtID")
        canonical_name = _get_required_text(protein_elem, "./CanonicalName", "CanonicalName")
        sequence = _get_required_text(protein_elem, "./Sequence", "Sequence")
    except LMPParseError as e:
        raise LMPParseError(f"Missing required field in {xml_path}: {e}")
    
    # CEA identity resolution
    cea = _create_cea_entity(protein_elem, uniprot_id, canonical_name)
    
    # Parse organism (optional, default to Unknown)
    organism_elem = protein_elem.find("./Organism")
    organism = organism_elem.text if organism_elem is not None else "Unknown"
    
    # Parse domains with all LMP v2.0 extensions
    domains = _parse_domains(protein_elem)
    
    # Parse protein-protein interfaces
    interfaces = _parse_interfaces(protein_elem)

    # --- LMP v4 extensions (from DLM-LMP convergence) ---
    literature_refs = _parse_literature_evidence(root)
    background_refs = _parse_background_evidence(root)
    temporal_facts = _parse_temporal_knowledge(root)
    governed_candidate_audit = _parse_governed_candidate_audit(root)
    
    # Functional state (will be updated by Chronoracle later)
    functional_state = BudoFunctionalState(
        current=FunctionalState.UNKNOWN,
        predicted=None,
        prediction_confidence=None,
        history=[],
        last_updated=datetime.now(timezone.utc),
        updated_by="lmp_parser"
    )
    
    # Provenance tracking
    provenance = BudoProvenance(
        created_by="lmp_pipeline",
        updated_by="lmp_pipeline",
        source="UniProt",
        confidence=ConfidenceLevel.HIGH,
        version=1
    )
    
    # Build metadata with v4 extensions if present
    budo_metadata: Dict[str, Any] = {}
    if literature_refs:
        budo_metadata["literature_evidence"] = literature_refs
    if background_refs:
        budo_metadata["background_evidence"] = background_refs
    if temporal_facts:
        budo_metadata["temporal_knowledge"] = temporal_facts
    if governed_candidate_audit:
        budo_metadata["governed_candidate_audit"] = governed_candidate_audit

    accepted_literature = [item for item in literature_refs if str(item.get("evidence_state") or "").strip().lower() == "accepted_truth"]
    novelty_literature = [item for item in literature_refs if str(item.get("evidence_state") or "").strip().lower() == "novelty_governed"]
    accepted_temporal = [item for item in temporal_facts if str(item.get("evidence_state") or "").strip().lower() == "accepted_truth"]
    novelty_temporal = [item for item in temporal_facts if str(item.get("evidence_state") or "").strip().lower() == "novelty_governed"]

    if accepted_literature:
        budo_metadata["accepted_literature_evidence"] = accepted_literature
    if novelty_literature:
        budo_metadata["novelty_governed_literature_evidence"] = novelty_literature
    if accepted_temporal:
        budo_metadata["accepted_temporal_knowledge"] = accepted_temporal
    if novelty_temporal:
        budo_metadata["novelty_governed_temporal_knowledge"] = novelty_temporal
    if accepted_literature or novelty_literature or accepted_temporal or novelty_temporal or governed_candidate_audit:
        budo_metadata["downstream_evidence_projection"] = {
            "accepted_literature_count": len(accepted_literature),
            "novelty_governed_literature_count": len(novelty_literature),
            "accepted_temporal_count": len(accepted_temporal),
            "novelty_governed_temporal_count": len(novelty_temporal),
            "candidate_audit_record_count": int(governed_candidate_audit.get("record_count") or 0),
            "candidate_audit_decision_counts": dict(governed_candidate_audit.get("decision_counts") or {}),
        }

    # Build BUDO V3 object
    # Use XML id attr directly for BudoV3 (must match ^budo:[A-Z0-9_]+-[SDLQF]$)
    # This is separate from CEA root id (which has _v suffix, no modal suffix)
    _xml_protein_id = protein_elem.get("id", "")
    _budo_v3_id = (
        _xml_protein_id
        if _xml_protein_id.startswith("budo:")
        else f"budo:{canonical_name.replace('-', '_').upper()}-S"
    )
    budo = BudoV3(
        budoId=_budo_v3_id,
        canonical_name=canonical_name,
        recommended_name=canonical_name,  # Same as canonical for now
        organism=organism,
        taxonomy_id=str(9606 if "human" in organism.lower() else 0),  # String, default to human or 0
        sequence=sequence,
        sequence_length=len(sequence),
        domains=domains,
        interfaces=interfaces,
        functionalState=functional_state,
        provenance=provenance,
        embeddings=[],  # Will be populated by embedding pipeline
        cross_references=_build_cross_references(cea.cross_references),
        metadata=budo_metadata,
    )
    
    logger.info(
        f"Parsed BUDO from LMP XML: {budo.budoId} "
        f"({len(domains)} domains, {len(interfaces)} interfaces, "
        f"{len(literature_refs)} lit refs, {len(background_refs)} background refs, "
        f"{len(temporal_facts)} temporal facts)"
    )
    
    return budo


def _get_required_text(root: ET.Element, xpath: str, field_name: str) -> str:
    """Extract required text field or raise error."""
    elem = root.find(xpath)
    if elem is None or not elem.text:
        raise LMPParseError(f"Required field '{field_name}' not found at {xpath}")
    return elem.text.strip()


def _create_cea_entity(protein_elem: ET.Element, uniprot_id: str, canonical_name: str) -> CEAEntity:
    """Create CEA entity with identity resolution."""
    external_refs = _parse_external_refs(protein_elem)
    
    # Generate CEA root identity: budo:NAME_v1 (no modal suffix, lowercase _v for CEA validator)
    # BudoV3.budoId is set separately from the XML id attr (see parse_lmp_xml_to_budo).
    base_name = canonical_name.replace('-', '_').upper()
    xml_id = protein_elem.get("id", "")
    if xml_id.startswith("budo:"):
        # Strip modal suffix (-S/-D/etc.) then ensure _v1 present for CEA
        core = xml_id[len("budo:"):]
        if len(core) >= 2 and core[-2] == "-" and core[-1] in "SDLQF":
            core = core[:-2]  # e.g. EGFR_HUMAN-S -> EGFR_HUMAN
        if "_v" not in core.lower():
            core = f"{core}_v1"
        budo_id = f"budo:{core}"
    else:
        budo_id = f"budo:{base_name}_v1"
    
    # Get organism for CEA (optional)
    organism_elem = protein_elem.find("./Organism")
    organism = organism_elem.text if organism_elem is not None else None
    
    cea = CEAEntity(
        budo_id=budo_id,
        entity_type="Protein",
        name=canonical_name,  # Use canonical_name as preferred label
        organism=organism,
        version="1.0",  # CEA version separate from BUDO ID
        cross_references=external_refs,
        audit={"curator": "lmp_parser", "pipeline": "lmp_v2"}
    )
    
    return cea


def _parse_external_refs(protein_elem: ET.Element) -> ExternalReferences:
    """Extract cross-references from XML into ExternalReferences object."""
    refs_data = {
        "uniprot": None,
        "pdb": [],
        "chembl": None,
        "pubchem": None,
    }
    
    for ref_elem in protein_elem.findall(".//CrossReference"):
        db = ref_elem.get("database", "").lower()
        ref_id = ref_elem.get("id")
        
        if not ref_id:
            continue
        
        if "uniprot" in db:
            refs_data["uniprot"] = ref_id
        elif "pdb" in db:
            refs_data["pdb"].append(ref_id)
        elif "chembl" in db:
            refs_data["chembl"] = ref_id
        elif "pubchem" in db:
            refs_data["pubchem"] = ref_id
    
    return ExternalReferences(**refs_data)


def _build_cross_references(external_refs: ExternalReferences) -> List[BudoCrossReference]:
    """Convert ExternalReferences to list of BudoCrossReference."""
    cross_refs = []
    
    # UniProt
    if external_refs.uniprot:
        cross_refs.append(BudoCrossReference(
            database="UniProt",
            identifier=external_refs.uniprot,
            url=f"https://www.uniprot.org/uniprotkb/{external_refs.uniprot}"
        ))
    
    # PDB
    for pdb in external_refs.pdb:
        cross_refs.append(BudoCrossReference(
            database="PDB",
            identifier=pdb,
            url=f"https://www.rcsb.org/structure/{pdb}"
        ))
    
    # ChEMBL
    if external_refs.chembl:
        cross_refs.append(BudoCrossReference(
            database="ChEMBL",
            identifier=external_refs.chembl,
            url=f"https://www.ebi.ac.uk/chembl/compound_report_card/{external_refs.chembl}"
        ))
    
    return cross_refs


def _parse_domains(root: ET.Element) -> List[BudoDomain]:
    """Parse all domains with LMP v2.0 extensions."""
    domains = []
    
    for domain_elem in root.findall(".//Domain"):
        domain_id = domain_elem.get("id", "unknown_domain")
        domain_name = domain_elem.find("Name")
        if domain_name is not None and domain_name.text:
            domain_name_text = domain_name.text
        else:
            # Fall back to the name attribute (LMP XML attribute style)
            domain_name_text = domain_elem.get("name", "Unknown Domain")
        
        domain_type = domain_elem.get("type", "UNKNOWN")
        
        # Positions
        try:
            start_pos = int(domain_elem.get("start", "1"))
            end_pos = int(domain_elem.get("end", "1"))
        except (ValueError, TypeError):
            logger.warning(f"Invalid domain positions for {domain_id}, skipping")
            continue
        
        # Sequence (optional)
        seq_elem = domain_elem.find("Sequence")
        sequence = seq_elem.text.strip() if seq_elem is not None and seq_elem.text else None
        
        # Structure ID (optional)
        structure_elem = domain_elem.find("StructureID")
        structure_id = structure_elem.text.strip() if structure_elem is not None and structure_elem.text else None
        
        # LMP v2.0 Extensions
        ptms = _parse_ptms(domain_elem)
        ligands = _parse_ligands(domain_elem)
        conformations = _parse_conformations(domain_elem)
        motifs = _parse_motifs(domain_elem)
        catalytic_residues = _parse_catalytic_residues(domain_elem)
        
        domain = BudoDomain(
            domain_id=domain_id,
            domain_name=domain_name_text,
            domain_type=domain_type,
            start_position=start_pos,
            end_position=end_pos,
            sequence=sequence,
            structure_id=structure_id,
            ese_signature=None,  # ESE will be added by MD pipeline
            cath_id=domain_elem.get("cath_id"),
            cath_code=domain_elem.get("cath_code"),
            pfam_id=domain_elem.get("pfam_id"),
            interpro_id=domain_elem.get("interpro_id"),
            superfamily_id=domain_elem.get("superfamily_id"),
            funfam_number=domain_elem.get("funfam_number"),
            functional_annotations={},
            ptms=ptms,
            ligands=ligands,
            conformations=conformations,
            motifs=motifs,
            catalytic_residues=catalytic_residues
        )
        
        domains.append(domain)
    
    return domains


def _parse_ptms(domain_elem: ET.Element) -> List[BudoPTM]:
    """Extract PTMs from domain XML. Maps LMP XML attributes to GAP-1 BudoPTM schema."""
    ptms = []

    for ptm_elem in domain_elem.findall(".//PTM"):
        try:
            evidence_str = ptm_elem.get("evidence", "")
            pubmed_ids = [e.strip() for e in evidence_str.split(",") if e.strip()]

            ptm = BudoPTM(
                position=int(ptm_elem.get("position", "1")),
                residue=(ptm_elem.get("residue", "S")[:1] or "S"),
                ptm_type=ptm_elem.get("type", "phosphorylation"),
                enzyme=ptm_elem.get("enzyme") or ptm_elem.get("causal_trigger") or None,
                source=ptm_elem.get("source") or ptm_elem.get("status") or "unknown",
                pubmed_ids=pubmed_ids,
            )
            ptms.append(ptm)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid PTM data: {e}, skipping")
            continue

    return ptms


def _parse_ligands(domain_elem: ET.Element) -> List[BudoLigand]:
    """Extract ligands from domain XML. Maps LMP XML attributes to GAP-1 BudoLigand schema."""
    ligands = []

    for lig_elem in domain_elem.findall(".//Ligand"):
        try:
            binding_residues_str = (
                lig_elem.get("binding_residues", "")
                or lig_elem.get("binding_site_residues", "")
            )
            binding_residues = [
                int(r.strip())
                for r in binding_residues_str.split(",")
                if r.strip().isdigit()
            ]

            affinity_str = lig_elem.get("affinity") or lig_elem.get("binding_affinity")
            affinity = float(affinity_str) if affinity_str else None

            lig_id = lig_elem.get("id", "")
            chembl_id = lig_id if lig_id.upper().startswith("CHEMBL") else None
            pubchem_id = lig_id if lig_id.isdigit() else lig_elem.get("pubchem_id")

            ligand = BudoLigand(
                chembl_id=chembl_id,
                pubchem_id=pubchem_id,
                name=lig_elem.get("name") or lig_elem.get("ligand_name") or "Unknown",
                binding_residues=binding_residues,
                affinity_nm=affinity,
            )
            ligands.append(ligand)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid ligand data: {e}, skipping")
            continue

    return ligands


def _parse_conformations(domain_elem: ET.Element) -> List[BudoConformation]:
    """Extract conformational states from domain XML. Maps to GAP-1 BudoConformation schema."""
    conformations = []

    for conf_elem in domain_elem.findall(".//Conformation"):
        try:
            state = (
                conf_elem.get("state")
                or conf_elem.get("state_name")
                or "unknown"
            )
            conf_id = (
                conf_elem.get("id")
                or conf_elem.get("trigger_id")
                or f"conf_{len(conformations) + 1}"
            )
            pdb_id = conf_elem.get("pdb_id")
            resolution_str = conf_elem.get("resolution")
            resolution = float(resolution_str) if resolution_str else None

            conf = BudoConformation(
                conformation_id=conf_id,
                pdb_id=pdb_id,
                state=state,
                resolution=resolution,
                ese_signature=None,  # ESE populated downstream by MD pipeline
            )
            conformations.append(conf)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid conformation data: {e}, skipping")
            continue

    return conformations


def _parse_motifs(domain_elem: ET.Element) -> List[Dict[str, Any]]:
    """Extract sequence motifs (DFG, APE, etc.)."""
    motifs = []
    
    for motif_elem in domain_elem.findall(".//Motif"):
        try:
            motif = {
                "motif_name": motif_elem.get("name", "Unknown"),
                "sequence": motif_elem.get("sequence", ""),
                "start": int(motif_elem.get("start", "0")),
                "end": int(motif_elem.get("end", "0")),
                "type": motif_elem.get("type", "structural")
            }
            motifs.append(motif)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid motif data: {e}, skipping")
            continue
    
    return motifs


def _parse_catalytic_residues(domain_elem: ET.Element) -> List[int]:
    """Extract catalytic residue positions from M-CSA annotations."""
    cat_elem = domain_elem.find(".//CatalyticResidues")
    if cat_elem is None:
        return []
    
    positions_str = cat_elem.text or ""
    positions = []
    
    for p in positions_str.split(","):
        try:
            positions.append(int(p.strip()))
        except (ValueError, TypeError):
            continue
    
    return positions


def _parse_interfaces(root: ET.Element) -> List[BudoInterface]:
    """Extract protein-protein interaction interfaces. Maps to GAP-1 BudoInterface schema."""
    interfaces = []

    for if_elem in root.findall(".//Interface"):
        try:
            residues_str = (
                if_elem.get("interface_residues", "")
                or if_elem.get("residues", "")
            )
            interface_residues = [
                int(r.strip())
                for r in residues_str.split(",")
                if r.strip().isdigit()
            ]

            strength_str = (
                if_elem.get("haddock_score")
                or if_elem.get("strength")
                or if_elem.get("interaction_strength")
            )
            haddock_score = float(strength_str) if strength_str else None

            interface_type_str = (
                if_elem.get("interface_type")
                or if_elem.get("type")
                or "experimental"
            )

            interface = BudoInterface(
                partner_protein_id=(
                    if_elem.get("partner")
                    or if_elem.get("partner_budo_id")
                    or "unknown"
                ),
                partner_chain=if_elem.get("partner_chain"),
                interface_residues=interface_residues,
                interface_type=interface_type_str,
                haddock_score=haddock_score,
                source_pdb=if_elem.get("source_pdb"),
            )
            interfaces.append(interface)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid interface data: {e}, skipping")
            continue

    return interfaces


def _parse_literature_evidence(root: ET.Element) -> List[Dict[str, Any]]:
    """Parse ``<literature_evidence>`` block injected by DLM-LMP convergence (v4)."""
    refs: List[Dict[str, Any]] = []
    lit_elems = root.findall(".//literature_evidence")
    for lit_elem in lit_elems:
        host_entity = lit_elem.get("entity", "")

        for paper_elem in lit_elem.findall("paper"):
            paper: Dict[str, Any] = {
                "doi": paper_elem.get("doi", ""),
                "title": paper_elem.get("title", ""),
                "authors": paper_elem.get("authors", ""),
                "year": paper_elem.get("year", ""),
                "entity": host_entity,
                "source_plane": paper_elem.get("source_plane") or lit_elem.get("source_plane", "paper"),
                "source_system": paper_elem.get("source_system") or lit_elem.get("source_system", "dlm"),
                "paper_only": (paper_elem.get("paper_only") or lit_elem.get("paper_only", "true")).lower() == "true",
                "evidence_state": paper_elem.get("evidence_state") or lit_elem.get("evidence_state", ""),
                "projection_lane": paper_elem.get("projection_lane") or lit_elem.get("projection_lane", ""),
                "graph_truth_status": paper_elem.get("graph_truth_status") or lit_elem.get("graph_truth_status", ""),
            }
            # Collect child <fact> elements under <paper>
            nested_facts = []
            for fact_elem in paper_elem.findall("fact"):
                payload_summary = fact_elem.get("semantic_kernel_payload_summary_json")
                try:
                    parsed_payload_summary = json.loads(payload_summary) if payload_summary else None
                except json.JSONDecodeError:
                    parsed_payload_summary = None
                nested_facts.append({
                    "predicate": fact_elem.get("predicate", ""),
                    "object": fact_elem.get("object", ""),
                    "confidence": float(fact_elem.get("confidence", "0.0")),
                    "trigger_source": fact_elem.get("trigger_source", ""),
                    "relation_category": fact_elem.get("relation_category", ""),
                    "source_plane": fact_elem.get("source_plane") or paper.get("source_plane", "paper"),
                    "source_system": fact_elem.get("source_system") or paper.get("source_system", "dlm"),
                    "paper_only": (fact_elem.get("paper_only") or "true").lower() == "true",
                    "evidence_state": fact_elem.get("evidence_state", ""),
                    "projection_lane": fact_elem.get("projection_lane", ""),
                    "graph_truth_status": fact_elem.get("graph_truth_status", ""),
                    "llm_route_decision": fact_elem.get("llm_route_decision", ""),
                    "llm_refinement_scope": fact_elem.get("llm_refinement_scope", ""),
                    "semantic_kernel_payload_summary": parsed_payload_summary,
                })
            if nested_facts:
                paper["facts"] = nested_facts
            refs.append(paper)

        # Some convergence outputs store <fact> directly under <literature_evidence>
        for fact_elem in lit_elem.findall("fact"):
            payload_summary = fact_elem.get("semantic_kernel_payload_summary_json")
            try:
                parsed_payload_summary = json.loads(payload_summary) if payload_summary else None
            except json.JSONDecodeError:
                parsed_payload_summary = None
            refs.append({
                "entity": host_entity,
                "predicate": fact_elem.get("predicate", ""),
                "object": fact_elem.get("object", ""),
                "confidence": float(fact_elem.get("confidence", "0.0")),
                "trigger_source": fact_elem.get("trigger_source", ""),
                "relation_category": fact_elem.get("relation_category", ""),
                "source_plane": fact_elem.get("source_plane") or lit_elem.get("source_plane", "paper"),
                "source_system": fact_elem.get("source_system") or lit_elem.get("source_system", "dlm"),
                "paper_only": (fact_elem.get("paper_only") or lit_elem.get("paper_only", "true")).lower() == "true",
                "evidence_state": fact_elem.get("evidence_state", ""),
                "projection_lane": fact_elem.get("projection_lane", ""),
                "graph_truth_status": fact_elem.get("graph_truth_status", ""),
                "llm_route_decision": fact_elem.get("llm_route_decision", ""),
                "llm_refinement_scope": fact_elem.get("llm_refinement_scope", ""),
                "semantic_kernel_payload_summary": parsed_payload_summary,
            })

    # Preserve paper evidence that could not be attached to a structural node.
    for corpus_elem in root.findall(".//literature_corpus"):
        for paper_elem in corpus_elem.findall("paper"):
            refs.append({
                "doi": paper_elem.get("doi", ""),
                "title": paper_elem.get("title", ""),
                "authors": paper_elem.get("authors", ""),
                "year": paper_elem.get("year", ""),
                "source_plane": paper_elem.get("source_plane") or corpus_elem.get("source_plane", "paper"),
                "source_system": paper_elem.get("source_system") or corpus_elem.get("source_system", "dlm"),
                "paper_only": (paper_elem.get("paper_only") or corpus_elem.get("paper_only", "true")).lower() == "true",
            })

    return refs


def _parse_background_evidence(root: ET.Element) -> List[Dict[str, Any]]:
    """Parse explicit non-paper evidence block injected by DLM-LMP convergence."""
    bg_elem = root.find(".//background_evidence")
    if bg_elem is None:
        return []

    refs: List[Dict[str, Any]] = []
    for ent_elem in bg_elem.findall("entity"):
        item: Dict[str, Any] = {
            "name": ent_elem.get("name", ""),
            "entity_type": ent_elem.get("entity_type", ""),
            "uniprot_id": ent_elem.get("uniprot_id", ""),
            "source_plane": ent_elem.get("source_plane") or bg_elem.get("source_plane", "background"),
            "source_system": ent_elem.get("source_system") or bg_elem.get("source_system", "lmp_metadata"),
            "paper_only": (ent_elem.get("paper_only") or "false").lower() == "true",
        }

        flags_elem = ent_elem.find("metadata_flags")
        if flags_elem is not None:
            item["metadata_flags"] = dict(flags_elem.attrib)

        counts_elem = ent_elem.find("metadata_counts")
        if counts_elem is not None:
            # Keep numeric values when possible.
            counts: Dict[str, Any] = {}
            for key, value in counts_elem.attrib.items():
                try:
                    counts[key] = float(value) if "." in str(value) else int(value)
                except (TypeError, ValueError):
                    counts[key] = value
            item["metadata_counts"] = counts

        refs.append(item)

    return refs


def _parse_temporal_knowledge(root: ET.Element) -> List[Dict[str, Any]]:
    """Parse ``<temporal_knowledge>`` block injected by DLM-LMP convergence (v4)."""
    quintuples: List[Dict[str, Any]] = []
    tk_elem = root.find(".//temporal_knowledge")
    if tk_elem is None:
        return quintuples

    for q_elem in tk_elem.findall("quintuple"):
        payload_summary = q_elem.get("semantic_kernel_payload_summary_json")
        try:
            parsed_payload_summary = json.loads(payload_summary) if payload_summary else None
        except json.JSONDecodeError:
            parsed_payload_summary = None
        quintuples.append({
            "subject": q_elem.get("subject", ""),
            "predicate": q_elem.get("predicate", ""),
            "object": q_elem.get("object", ""),
            "time": q_elem.get("time", ""),
            "confidence": float(q_elem.get("confidence", "0.0")),
            "trigger_source": q_elem.get("trigger_source", ""),
            "relation_category": q_elem.get("relation_category", ""),
            "source_plane": q_elem.get("source_plane") or tk_elem.get("source_plane", "paper"),
            "source_system": q_elem.get("source_system") or tk_elem.get("source_system", "atom"),
            "paper_only": (q_elem.get("paper_only") or "true").lower() == "true",
            "evidence_state": q_elem.get("evidence_state", ""),
            "projection_lane": q_elem.get("projection_lane", ""),
            "graph_truth_status": q_elem.get("graph_truth_status", ""),
            "llm_route_decision": q_elem.get("llm_route_decision", ""),
            "llm_refinement_scope": q_elem.get("llm_refinement_scope", ""),
            "semantic_kernel_payload_summary": parsed_payload_summary,
        })

    return quintuples


def _parse_governed_candidate_audit(root: ET.Element) -> Dict[str, Any]:
    audit_elem = root.find(".//governed_candidate_audit")
    if audit_elem is None:
        return {}

    records: List[Dict[str, Any]] = []
    for record_elem in audit_elem.findall("record"):
        reasons_raw = record_elem.get("promotion_reasons_json")
        try:
            reasons = json.loads(reasons_raw) if reasons_raw else []
        except json.JSONDecodeError:
            reasons = []
        decision = record_elem.get("promotion_decision") or record_elem.get("decision") or "candidate"
        records.append({
            "subject": record_elem.get("subject", ""),
            "predicate": record_elem.get("predicate", ""),
            "object": record_elem.get("object", ""),
            "decision": decision,
            "promotion_decision": decision,
            "promotion_reasons": reasons,
            "kernel_context_decision": record_elem.get("kernel_context_decision", ""),
            "llm_route_decision": record_elem.get("llm_route_decision", ""),
            "source_plane": record_elem.get("source_plane", ""),
            "source_system": record_elem.get("source_system", ""),
        })

    decision_counts: Dict[str, int] = {}
    for key in ("hold", "rejected", "candidate"):
        raw_value = audit_elem.get(f"{key}_count")
        if raw_value is None:
            continue
        try:
            decision_counts[key] = int(raw_value)
        except ValueError:
            continue
    if not decision_counts:
        for record in records:
            key = str(record.get("promotion_decision") or record.get("decision") or "candidate").strip().lower()
            if key:
                decision_counts[key] = decision_counts.get(key, 0) + 1

    return {
        "record_count": len(records),
        "decision_counts": decision_counts,
        "records": records,
        "hold": [item for item in records if str(item.get("promotion_decision") or item.get("decision") or "").strip().lower() == "hold"],
        "rejected": [item for item in records if str(item.get("promotion_decision") or item.get("decision") or "").strip().lower() == "rejected"],
    }


async def parse_lmp_xml_string_to_budo(xml_string: str) -> BudoV3:
    """Parse an in-memory LMP XML string (v2-v4) into a BUDO V3 object.

    Identical logic to :func:`parse_lmp_xml_to_budo` but operates on a raw
    string instead of a file path.  Useful after DLM-LMP convergence produces
    the enriched XML in memory.
    """
    import tempfile, os
    # Write to a temp file and delegate to the file-based parser so all
    # validation / error messages are consistent.
    fd, tmp_path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(xml_string)
        return await parse_lmp_xml_to_budo(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Synchronous wrapper for backwards compatibility
def parse_lmp_xml_to_budo_sync(xml_path: str) -> BudoV3:
    """
    Synchronous wrapper for parse_lmp_xml_to_budo.
    
    Use this in non-async contexts.
    """
    import asyncio
    return asyncio.run(parse_lmp_xml_to_budo(xml_path))

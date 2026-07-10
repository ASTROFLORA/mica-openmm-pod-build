"""
BUDO V3 (Biological Unified Data Object) Schema
================================================

Sentient, mutable protein entities with ESE signatures and multi-modal embeddings.

Author: Alex Rodriguez (Chief Data Architect)
Lab: Alex Rodriguez AI Systems Architecture Lab
Phase: 0.002 - Schema Definition & Standards
Date: October 2, 2025
Version: 3.0.0

References:
- BSM-BUDO-CEA Unified Master Roadmap
- BioSchemas Protein Profile v0.11
- JSON-LD 1.1 Specification
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from bsm.schemas.domain_ontology import DomainClass


class ModalitySuffix(str, Enum):
    """Modality suffixes for composite BUDO IDs"""

    STRUCTURE = "S"  # Structure-based
    DYNAMICS = "D"  # Dynamics/MD-based
    LITERATURE = "L"  # Literature-based
    SEQUENCE = "Q"  # Sequence-based
    FUNCTIONAL = "F"  # Functional/experimental


class FunctionalState(str, Enum):
    """Functional states for BUDO objects"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    ALLOSTERIC = "allosteric"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


class ConfidenceLevel(str, Enum):
    """Confidence levels for annotations"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    PREDICTED = "predicted"


# ============================================================================
# NESTED MODELS
# ============================================================================


class BudoProvenance(BaseModel):
    """Provenance tracking for BUDO object creation and updates"""

    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = Field(description="Service or user that created the object")
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = Field(description="Service or user that last updated the object")
    version: int = Field(default=1, ge=1)
    source: str = Field(description="Data source (UniProt, PDB, mdCATH, etc.)")
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class BudoCrossReference(BaseModel):
    """Cross-references to external databases"""

    database: str = Field(description="Database name (UniProt, PDB, GO, etc.)")
    identifier: str = Field(description="Identifier in external database")
    url: Optional[HttpUrl] = Field(None, description="Direct link to resource")
    version: Optional[str] = Field(None, description="Version of the reference")


class BudoESESignature(BaseModel):
    """Emergent Structural Ensemble (ESE) signature from MD trajectories"""

    ese_vector: List[float] = Field(
        description="512D ESE feature vector",
        min_length=512,
        max_length=512,
    )
    trajectory_id: str = Field(description="mdCATH or BioDynamo trajectory ID")
    rmsd_mean: float = Field(description="Mean RMSD (Angstroms)")
    rmsd_std: float = Field(description="RMSD standard deviation")
    radius_of_gyration: float = Field(description="Radius of gyration (Angstroms)")
    rmsf_features: List[float] = Field(description="Per-residue RMSF features")
    extraction_date: datetime = Field(default_factory=datetime.utcnow)
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    temperature_kelvin: Optional[float] = Field(
        None, description="Simulation temperature in Kelvin (e.g., 300.0)"
    )
    ese_extraction_method: Optional[str] = Field(
        None, description="svd | moments — determined by Hartigan dip test on RMSD time series"
    )
    anharmonicity_index: Optional[float] = Field(
        None, description="Per-domain deviation from linear T-scaling of RMSF"
    )


class BudoFunctionalState(BaseModel):
    """Mutable functional state with Chronoracle integration"""

    current: FunctionalState = Field(default=FunctionalState.UNKNOWN)
    predicted: Optional[FunctionalState] = Field(
        None, description="Chronoracle prediction"
    )
    prediction_confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Confidence score [0-1]"
    )
    history: List[Dict[str, Any]] = Field(
        default_factory=list, description="State transition history"
    )
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = Field(default="system", description="Update source")


class BudoEmbedding(BaseModel):
    """Multi-modal embedding representation"""

    embedding_type: str = Field(
        description="Type: ese, pubmedbert, multimodal, sequence"
    )
    vector: List[float] = Field(description="Embedding vector")
    dimensionality: int = Field(description="Vector dimensionality")
    milvus_collection: str = Field(description="Milvus collection name")
    vector_id: Optional[str] = Field(None, description="ID in Milvus collection")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BudoVariant(BaseModel):
    """Protein variant (mutation, splice variant, etc.)"""

    variant_id: str = Field(description="Unique variant identifier")
    variant_type: str = Field(
        description="Type: mutation, splice, PTM, chimera, etc."
    )
    position: Optional[int] = Field(None, description="Position in sequence")
    original_residue: Optional[str] = Field(None, description="Original amino acid")
    mutant_residue: Optional[str] = Field(None, description="Mutant amino acid")
    description: str = Field(description="Human-readable description")
    clinical_significance: Optional[str] = Field(
        None, description="Clinical significance (if any)"
    )
    cross_references: List[BudoCrossReference] = Field(default_factory=list)


class BudoPTM(BaseModel):
    """Post-translational modification at a specific residue"""

    position: int = Field(ge=1, description="Residue position (1-based)")
    residue: str = Field(max_length=1, description="One-letter amino acid code")
    ptm_type: str = Field(description="phosphorylation, acetylation, ubiquitination, etc.")
    enzyme: Optional[str] = Field(None, description="Enzyme responsible for the PTM")
    source: str = Field(default="unknown", description="PhosphoSitePlus, UniProt, LMP, mdCATH")
    pubmed_ids: List[str] = Field(default_factory=list)


class BudoLigand(BaseModel):
    """Ligand binding annotation"""

    chembl_id: Optional[str] = Field(None, description="ChEMBL compound ID")
    pubchem_id: Optional[str] = Field(None, description="PubChem CID")
    name: str = Field(description="Ligand common name")
    binding_residues: List[int] = Field(
        default_factory=list, description="Residue positions involved in binding"
    )
    affinity_nm: Optional[float] = Field(
        None, description="Binding affinity in nM"
    )


class BudoConformation(BaseModel):
    """Conformational state from MD trajectory or experimental structure"""

    conformation_id: str = Field(description="Unique conformation identifier")
    pdb_id: Optional[str] = Field(None, description="PDB ID of this conformation")
    state: str = Field(description="active, inactive, intermediate, apo, holo")
    resolution: Optional[float] = Field(
        None, description="Crystallographic resolution in Angstroms"
    )
    ese_signature: Optional[BudoESESignature] = Field(
        None, description="ESE signature for this specific conformation"
    )


class BudoInterface(BaseModel):
    """Protein-protein interface annotation"""

    partner_protein_id: str = Field(
        description="UniProt accession or BUDO ID of the interface partner"
    )
    partner_chain: Optional[str] = Field(
        None, description="Chain identifier of the partner in the complex"
    )
    interface_residues: List[int] = Field(
        default_factory=list, description="Residue positions at the interface"
    )
    interface_type: str = Field(
        default="experimental",
        description="experimental, predicted, docking",
    )
    haddock_score: Optional[float] = Field(
        None, description="HADDOCK docking score (lower = better)"
    )
    source_pdb: Optional[str] = Field(
        None, description="PDB ID of the complex structure"
    )


class BudoDomain(BaseModel):
    """Protein domain with structural and functional annotations"""

    domain_id: str = Field(description="Unique domain identifier (CATH, Pfam, etc.)")
    domain_name: str = Field(description="Domain name")
    domain_type: str = Field(description="Type: CATH, Pfam, InterPro, etc.")
    domain_class: DomainClass = Field(
        default=DomainClass.UNKNOWN,
        description="Semantic class from domain_ontology.DomainClass (structural, disordered, functional, etc.)",
    )
    start_position: int = Field(ge=1, description="Start position in sequence")
    end_position: int = Field(ge=1, description="End position in sequence")
    sequence: Optional[str] = Field(None, description="Domain sequence")
    structure_id: Optional[str] = Field(None, description="PDB ID or structure ID")
    ese_signature: Optional[BudoESESignature] = Field(
        None, description="ESE signature for this domain"
    )
    functional_annotations: Dict[str, Any] = Field(
        default_factory=dict, description="GO terms, KEGG pathways, etc."
    )
    # ---- GAP-1: annotation fields ----
    cath_id: Optional[str] = Field(
        None, description="Full CATH node code (e.g., 3.30.930.10.29.2.1.1.1)"
    )
    cath_code: Optional[str] = Field(
        None, description="4-level CATH superfamily code (e.g., 3.30.930.10)"
    )
    pfam_id: Optional[str] = Field(
        None, description="Pfam accession (e.g., PF00017)"
    )
    interpro_id: Optional[str] = Field(
        None, description="InterPro accession (e.g., IPR000980)"
    )
    superfamily_id: Optional[str] = Field(
        None, description="CATH superfamily canonical (e.g., 3.30.930.10)"
    )
    funfam_number: Optional[str] = Field(
        None, description="CATH functional family number within the superfamily"
    )
    ptms: List[BudoPTM] = Field(
        default_factory=list, description="Post-translational modifications"
    )
    ligands: List[BudoLigand] = Field(
        default_factory=list, description="Ligand binding annotations"
    )
    conformations: List[BudoConformation] = Field(
        default_factory=list, description="Conformational states"
    )
    motifs: List[Dict[str, Any]] = Field(
        default_factory=list, description="Sequence motifs (prosite, ELM, etc.)"
    )
    catalytic_residues: List[int] = Field(
        default_factory=list, description="Residue positions with catalytic roles"
    )
    semantic_richness: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Annotation completeness [0.0=blind mdCATH, 1.0=fully annotated with EC/GO/structure quality]",
    )

    @field_validator("end_position")
    @classmethod
    def validate_positions(cls, v: int, info) -> int:
        """Ensure end_position > start_position"""
        if "start_position" in info.data and v <= info.data["start_position"]:
            raise ValueError("end_position must be greater than start_position")
        return v


# ============================================================================
# MAIN BUDO V3 MODEL
# ============================================================================


class BudoV3(BaseModel):
    """
    BUDO V3: Biological Unified Data Object (Sentient Edition)

    A sentient, mutable protein entity with:
    - Identity resolution via CEA
    - Multi-modal embeddings (ESE + PubMedBERT)
    - Mutable functional states (Chronoracle)
    - Rich biological annotations
    - BioSchemas JSON-LD compliance
    """

    # ========================================================================
    # IDENTITY & CORE METADATA
    # ========================================================================

    budoId: str = Field(
        description="Composite BUDO ID from CEA (e.g., budo:ABL1_HUMAN_v1-D)",
        pattern=r"^budo:[A-Z0-9_]+-[SDLQF]$",
    )

    canonical_name: str = Field(description="Canonical protein name (e.g., ABL1_HUMAN)")

    recommended_name: str = Field(
        description="Recommended protein name (e.g., Tyrosine-protein kinase ABL1)"
    )

    organism: str = Field(description="Source organism (e.g., Homo sapiens)")

    taxonomy_id: str = Field(description="NCBI Taxonomy ID")

    # ========================================================================
    # SEQUENCE & STRUCTURE
    # ========================================================================

    sequence: str = Field(description="Amino acid sequence (one-letter code)")

    sequence_length: int = Field(ge=1, description="Sequence length")

    molecular_weight: Optional[float] = Field(
        None, ge=0.0, description="Molecular weight (Da)"
    )

    isoelectric_point: Optional[float] = Field(
        None, ge=0.0, le=14.0, description="Isoelectric point (pI)"
    )

    # ========================================================================
    # DOMAINS & VARIANTS
    # ========================================================================

    domains: List[BudoDomain] = Field(
        default_factory=list, description="Protein domains"
    )

    variants: List[BudoVariant] = Field(
        default_factory=list, description="Protein variants"
    )

    # ========================================================================
    # FUNCTIONAL STATE (MUTABLE)
    # ========================================================================

    functionalState: BudoFunctionalState = Field(
        default_factory=BudoFunctionalState,
        description="Current and predicted functional state",
    )

    # ========================================================================
    # ESE SIGNATURES
    # ========================================================================

    ese_signatures: List[BudoESESignature] = Field(
        default_factory=list,
        description="ESE signatures from MD trajectories",
    )

    interfaces: List[BudoInterface] = Field(
        default_factory=list,
        description="Protein-protein interface annotations",
    )

    # ========================================================================
    # MULTI-MODAL EMBEDDINGS
    # ========================================================================

    embeddings: List[BudoEmbedding] = Field(
        default_factory=list,
        description="Multi-modal embeddings (ESE, PubMedBERT, etc.)",
    )

    # ========================================================================
    # BIOLOGICAL ANNOTATIONS
    # ========================================================================

    go_terms: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Gene Ontology terms (BP, MF, CC)",
    )

    kegg_pathways: List[str] = Field(
        default_factory=list, description="KEGG pathway IDs"
    )

    reactome_pathways: List[str] = Field(
        default_factory=list, description="Reactome pathway IDs"
    )

    ec_numbers: List[str] = Field(
        default_factory=list, description="Enzyme Commission numbers"
    )

    # ========================================================================
    # CROSS-REFERENCES
    # ========================================================================

    cross_references: List[BudoCrossReference] = Field(
        default_factory=list,
        description="Cross-references to UniProt, PDB, GO, KEGG, etc.",
    )

    # ========================================================================
    # PROVENANCE & METADATA
    # ========================================================================

    provenance: BudoProvenance = Field(
        default_factory=BudoProvenance, description="Provenance tracking"
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )

    # ========================================================================
    # JSON-LD CONTEXT (BioSchemas)
    # ========================================================================

    context: str = Field(
        default="https://schema.org/",
        alias="@context",
        description="JSON-LD context",
    )

    type: str = Field(
        default="Protein", alias="@type", description="BioSchemas type"
    )

    # ========================================================================
    # METHODS
    # ========================================================================

    def add_ese_signature(self, ese: BudoESESignature) -> None:
        """Add an ESE signature to the BUDO object"""
        self.ese_signatures.append(ese)
        self.provenance.updated_at = datetime.utcnow()
        self.provenance.version += 1

    def add_embedding(self, embedding: BudoEmbedding) -> None:
        """Add an embedding to the BUDO object"""
        self.embeddings.append(embedding)
        self.provenance.updated_at = datetime.utcnow()

    def update_functional_state(
        self,
        new_state: FunctionalState,
        confidence: Optional[float] = None,
        source: str = "chronoracle",
    ) -> None:
        """Update functional state with provenance tracking"""
        # Record history
        history_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "from_state": self.functionalState.current.value,
            "to_state": new_state.value,
            "source": source,
            "confidence": confidence,
        }
        self.functionalState.history.append(history_entry)

        # Update current state
        self.functionalState.current = new_state
        if confidence:
            self.functionalState.prediction_confidence = confidence
        self.functionalState.last_updated = datetime.utcnow()
        self.functionalState.updated_by = source

        # Update provenance
        self.provenance.updated_at = datetime.utcnow()
        self.provenance.updated_by = source
        self.provenance.version += 1

    def _get_uniprot_id(self) -> str:
        """Extract UniProt accession from cross_references, fallback to canonical_name."""
        for ref in self.cross_references:
            if ref.database.lower() == "uniprot":
                return ref.identifier
        return self.canonical_name

    def to_lmp_xml(self) -> str:
        """Generate LMP v2 XML from BUDO V3 state.

        Produces XML compatible with parse_lmp_xml_to_budo() for round-trip fidelity.
        All GAP-1 CATH/PTM/Ligand/Conformation/Interface fields are preserved.
        """
        from xml.etree.ElementTree import Element, SubElement, tostring
        from xml.dom.minidom import parseString

        def _set_attr(elem: Any, key: str, value: Any) -> None:
            if value is None:
                return
            elem.set(key, str(value))

        def _set_json_attr(elem: Any, key: str, value: Any) -> None:
            if value is None:
                return
            elem.set(key, json.dumps(value, sort_keys=True))

        root = Element("Protein")
        root.set("id", self.budoId)

        # Core identity
        uid = SubElement(root, "UniProtID")
        uid.text = self._get_uniprot_id()
        can = SubElement(root, "CanonicalName")
        can.text = self.canonical_name
        org = SubElement(root, "Organism")
        org.text = self.organism
        seq = SubElement(root, "Sequence")
        seq.text = self.sequence

        # Cross-references
        for ref in self.cross_references:
            cr = SubElement(root, "CrossReference")
            cr.set("database", ref.database)
            cr.set("id", ref.identifier)

        # Domains
        for domain in self.domains:
            de = SubElement(root, "Domain")
            de.set("id", domain.domain_id)
            de.set("type", domain.domain_type)
            de.set("start", str(domain.start_position))
            de.set("end", str(domain.end_position))
            if domain.cath_id:
                de.set("cath_id", domain.cath_id)
            if domain.cath_code:
                de.set("cath_code", domain.cath_code)
            if domain.pfam_id:
                de.set("pfam_id", domain.pfam_id)
            if domain.interpro_id:
                de.set("interpro_id", domain.interpro_id)
            if domain.superfamily_id:
                de.set("superfamily_id", domain.superfamily_id)
            if domain.funfam_number:
                de.set("funfam_number", domain.funfam_number)
            nm = SubElement(de, "Name")
            nm.text = domain.domain_name
            if domain.sequence:
                sqd = SubElement(de, "Sequence")
                sqd.text = domain.sequence
            # PTMs
            for ptm in domain.ptms:
                pe = SubElement(de, "PTM")
                pe.set("position", str(ptm.position))
                pe.set("residue", ptm.residue)
                pe.set("type", ptm.ptm_type)
                if ptm.enzyme:
                    pe.set("enzyme", ptm.enzyme)
                pe.set("source", ptm.source)
                if ptm.pubmed_ids:
                    pe.set("evidence", ",".join(ptm.pubmed_ids))
            # Ligands
            for lig in domain.ligands:
                le = SubElement(de, "Ligand")
                le.set("id", lig.chembl_id or lig.pubchem_id or "")
                le.set("name", lig.name)
                if lig.binding_residues:
                    le.set("binding_residues", ",".join(str(r) for r in lig.binding_residues))
                if lig.affinity_nm is not None:
                    le.set("affinity", str(lig.affinity_nm))
            # Conformations
            for conf in domain.conformations:
                ce = SubElement(de, "Conformation")
                ce.set("id", conf.conformation_id)
                ce.set("state", conf.state)
                if conf.pdb_id:
                    ce.set("pdb_id", conf.pdb_id)
                if conf.resolution is not None:
                    ce.set("resolution", str(conf.resolution))
            # Motifs
            for motif in domain.motifs:
                me = SubElement(de, "Motif")
                me.set("name", str(motif.get("motif_name", "")))
                me.set("sequence", str(motif.get("sequence", "")))
                me.set("start", str(motif.get("start", 0)))
                me.set("end", str(motif.get("end", 0)))
            # Catalytic residues
            if domain.catalytic_residues:
                cre = SubElement(de, "CatalyticResidues")
                cre.text = ",".join(str(r) for r in domain.catalytic_residues)

        # Interfaces
        for iface in self.interfaces:
            ie = SubElement(root, "Interface")
            ie.set("partner_budo_id", iface.partner_protein_id)
            if iface.partner_chain:
                ie.set("partner_chain", iface.partner_chain)
            ie.set("type", iface.interface_type)
            if iface.interface_residues:
                ie.set("interface_residues", ",".join(str(r) for r in iface.interface_residues))
            if iface.haddock_score is not None:
                ie.set("strength", str(iface.haddock_score))
            if iface.source_pdb:
                ie.set("source_pdb", iface.source_pdb)

        literature_records = list(self.metadata.get("literature_evidence") or [])
        if not literature_records:
            literature_records = list(self.metadata.get("accepted_literature_evidence") or []) + list(
                self.metadata.get("novelty_governed_literature_evidence") or []
            )
        if literature_records:
            grouped_records: Dict[str, List[Dict[str, Any]]] = {}
            for record in literature_records:
                if not isinstance(record, dict):
                    continue
                entity_name = str(record.get("entity") or self.canonical_name or "").strip() or self.canonical_name
                grouped_records.setdefault(entity_name, []).append(record)

            for entity_name, records in grouped_records.items():
                lit_el = SubElement(root, "literature_evidence")
                lit_el.set("entity", entity_name)
                accepted_count = sum(1 for item in records if str(item.get("evidence_state") or "").strip().lower() == "accepted_truth")
                novelty_count = sum(1 for item in records if str(item.get("evidence_state") or "").strip().lower() == "novelty_governed")
                _set_attr(lit_el, "accepted_fact_count", accepted_count)
                _set_attr(lit_el, "novelty_fact_count", novelty_count)

                for record in records:
                    if not isinstance(record, dict):
                        continue
                    if record.get("doi") or record.get("title"):
                        paper_el = SubElement(lit_el, "paper")
                        _set_attr(paper_el, "doi", record.get("doi"))
                        _set_attr(paper_el, "title", record.get("title"))
                        _set_attr(paper_el, "authors", record.get("authors"))
                        _set_attr(paper_el, "year", record.get("year"))
                        _set_attr(paper_el, "source_plane", record.get("source_plane"))
                        _set_attr(paper_el, "source_system", record.get("source_system"))
                        _set_attr(paper_el, "paper_only", record.get("paper_only"))
                        _set_attr(paper_el, "evidence_state", record.get("evidence_state"))
                        _set_attr(paper_el, "projection_lane", record.get("projection_lane"))
                        _set_attr(paper_el, "graph_truth_status", record.get("graph_truth_status"))
                        for fact in list(record.get("facts") or []):
                            if not isinstance(fact, dict):
                                continue
                            fact_el = SubElement(paper_el, "fact")
                            _set_attr(fact_el, "predicate", fact.get("predicate"))
                            _set_attr(fact_el, "object", fact.get("object"))
                            _set_attr(fact_el, "confidence", fact.get("confidence"))
                            _set_attr(fact_el, "trigger_source", fact.get("trigger_source"))
                            _set_attr(fact_el, "relation_category", fact.get("relation_category"))
                            _set_attr(fact_el, "source_plane", fact.get("source_plane"))
                            _set_attr(fact_el, "source_system", fact.get("source_system"))
                            _set_attr(fact_el, "paper_only", fact.get("paper_only"))
                            _set_attr(fact_el, "evidence_state", fact.get("evidence_state"))
                            _set_attr(fact_el, "projection_lane", fact.get("projection_lane"))
                            _set_attr(fact_el, "graph_truth_status", fact.get("graph_truth_status"))
                            _set_attr(fact_el, "llm_route_decision", fact.get("llm_route_decision"))
                            _set_attr(fact_el, "llm_refinement_scope", fact.get("llm_refinement_scope"))
                            _set_json_attr(fact_el, "semantic_kernel_payload_summary_json", fact.get("semantic_kernel_payload_summary"))
                    if record.get("predicate") or record.get("object"):
                        fact_el = SubElement(lit_el, "fact")
                        _set_attr(fact_el, "predicate", record.get("predicate"))
                        _set_attr(fact_el, "object", record.get("object"))
                        _set_attr(fact_el, "confidence", record.get("confidence"))
                        _set_attr(fact_el, "trigger_source", record.get("trigger_source"))
                        _set_attr(fact_el, "relation_category", record.get("relation_category"))
                        _set_attr(fact_el, "source_plane", record.get("source_plane"))
                        _set_attr(fact_el, "source_system", record.get("source_system"))
                        _set_attr(fact_el, "paper_only", record.get("paper_only"))
                        _set_attr(fact_el, "evidence_state", record.get("evidence_state"))
                        _set_attr(fact_el, "projection_lane", record.get("projection_lane"))
                        _set_attr(fact_el, "graph_truth_status", record.get("graph_truth_status"))
                        _set_attr(fact_el, "llm_route_decision", record.get("llm_route_decision"))
                        _set_attr(fact_el, "llm_refinement_scope", record.get("llm_refinement_scope"))
                        _set_json_attr(fact_el, "semantic_kernel_payload_summary_json", record.get("semantic_kernel_payload_summary"))

        temporal_records = list(self.metadata.get("temporal_knowledge") or [])
        if not temporal_records:
            temporal_records = list(self.metadata.get("accepted_temporal_knowledge") or []) + list(
                self.metadata.get("novelty_governed_temporal_knowledge") or []
            )
        if temporal_records:
            tk = SubElement(root, "temporal_knowledge")
            accepted_count = sum(1 for item in temporal_records if str(item.get("evidence_state") or "").strip().lower() == "accepted_truth")
            novelty_count = sum(1 for item in temporal_records if str(item.get("evidence_state") or "").strip().lower() == "novelty_governed")
            _set_attr(tk, "quintuple_count", len(temporal_records))
            _set_attr(tk, "accepted_quintuple_count", accepted_count)
            _set_attr(tk, "novelty_quintuple_count", novelty_count)
            for record in temporal_records:
                if not isinstance(record, dict):
                    continue
                q_el = SubElement(tk, "quintuple")
                _set_attr(q_el, "subject", record.get("subject"))
                _set_attr(q_el, "predicate", record.get("predicate"))
                _set_attr(q_el, "object", record.get("object") or record.get("obj"))
                _set_attr(q_el, "time", record.get("time") or record.get("timestamp"))
                _set_attr(q_el, "confidence", record.get("confidence"))
                _set_attr(q_el, "trigger_source", record.get("trigger_source"))
                _set_attr(q_el, "relation_category", record.get("relation_category"))
                _set_attr(q_el, "source_plane", record.get("source_plane"))
                _set_attr(q_el, "source_system", record.get("source_system"))
                _set_attr(q_el, "paper_only", record.get("paper_only"))
                _set_attr(q_el, "source", record.get("source"))
                _set_attr(q_el, "evidence_state", record.get("evidence_state"))
                _set_attr(q_el, "projection_lane", record.get("projection_lane"))
                _set_attr(q_el, "graph_truth_status", record.get("graph_truth_status"))
                _set_attr(q_el, "llm_route_decision", record.get("llm_route_decision"))
                _set_attr(q_el, "llm_refinement_scope", record.get("llm_refinement_scope"))
                _set_json_attr(q_el, "semantic_kernel_payload_summary_json", record.get("semantic_kernel_payload_summary"))

        candidate_audit = self.metadata.get("governed_candidate_audit") if isinstance(self.metadata.get("governed_candidate_audit"), dict) else {}
        candidate_records = list(candidate_audit.get("records") or [])
        if candidate_records:
            audit_el = SubElement(root, "governed_candidate_audit")
            _set_attr(audit_el, "record_count", len(candidate_records))
            for key, value in sorted(dict(candidate_audit.get("decision_counts") or {}).items()):
                _set_attr(audit_el, f"{key}_count", value)
            for record in candidate_records:
                if not isinstance(record, dict):
                    continue
                rec_el = SubElement(audit_el, "record")
                decision = record.get("promotion_decision") or record.get("decision")
                _set_attr(rec_el, "subject", record.get("subject"))
                _set_attr(rec_el, "predicate", record.get("predicate"))
                _set_attr(rec_el, "object", record.get("object"))
                _set_attr(rec_el, "decision", decision)
                _set_attr(rec_el, "promotion_decision", decision)
                _set_attr(rec_el, "kernel_context_decision", record.get("kernel_context_decision"))
                _set_attr(rec_el, "llm_route_decision", record.get("llm_route_decision"))
                _set_attr(rec_el, "source_plane", record.get("source_plane"))
                _set_attr(rec_el, "source_system", record.get("source_system"))
                _set_json_attr(rec_el, "promotion_reasons_json", record.get("promotion_reasons") or record.get("reasons") or [])

        xml_str = tostring(root, encoding="unicode")
        return parseString(xml_str).toprettyxml(indent="  ")

    def to_jsonld(self) -> Dict[str, Any]:
        """Convert to JSON-LD format (BioSchemas Protein v0.11)"""
        return {
            "@context": "https://schema.org/",
            "@type": "Protein",
            "@id": self.budoId,
            "name": self.recommended_name,
            "identifier": self.budoId,
            "alternativeName": self.canonical_name,
            "organism": {"@type": "Organism", "name": self.organism},
            "hasSequenceAnnotation": [
                {
                    "@type": "SequenceRange",
                    "name": domain.domain_name,
                    "rangeStart": domain.start_position,
                    "rangeEnd": domain.end_position,
                }
                for domain in self.domains
            ],
            "sameAs": [ref.url for ref in self.cross_references if ref.url],
        }

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "budoId": "budo:ABL1_HUMAN_v1-D",
                "canonical_name": "ABL1_HUMAN",
                "recommended_name": "Tyrosine-protein kinase ABL1",
                "organism": "Homo sapiens",
                "taxonomy_id": "9606",
                "sequence": "MLEICLKLV...",
                "sequence_length": 1130,
                "domains": [
                    {
                        "domain_id": "1.10.510.10",
                        "domain_name": "SH2 domain",
                        "domain_type": "CATH",
                        "start_position": 120,
                        "end_position": 220,
                    }
                ],
                "functionalState": {"current": "active", "predicted": "active"},
            }
        },
    )


# ============================================================================
# EXPORT
# ============================================================================

__all__ = [
    "BudoV3",
    "BudoDomain",
    "BudoVariant",
    "BudoFunctionalState",
    "BudoESESignature",
    "BudoEmbedding",
    "BudoProvenance",
    "BudoCrossReference",
    "BudoPTM",
    "BudoLigand",
    "BudoConformation",
    "BudoInterface",
    "ModalitySuffix",
    "FunctionalState",
    "ConfidenceLevel",
]

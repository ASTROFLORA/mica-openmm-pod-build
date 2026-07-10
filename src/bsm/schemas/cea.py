"""Canonical Entity Atlas (CEA) schemas.

These models capture the unified identity surface for proteins inside
Biological Semantic Memory, including modality-specific identifiers and
ligand/compound relationships required for CEA Phase 1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field, field_validator


BUDO_ID_PATTERN = "budo:"


class CompositeIdentifiers(BaseModel):
    """Standardised modality identifiers derived from the root ``budoId``."""

    structure: Optional[str] = Field(
        None,
        description="Structure-centric identifier (e.g., budo:ABL1_HUMAN_v1-S)",
    )
    sequence: Optional[str] = Field(
        None,
        description="Sequence-centric identifier (e.g., budo:ABL1_HUMAN_v1-Q)",
    )
    dynamics: Optional[str] = Field(
        None,
        description="Dynamics identifier (mdCATH / ESE pipelines)",
    )
    metabolism: Optional[str] = Field(
        None,
        description="Metabolism identifier (e.g., KEGG, Reactome)",
    )
    interactions: Optional[str] = Field(
        None,
        description="Protein-protein/ligand interaction identifier",
    )
    phenotype: Optional[str] = Field(
        None,
        description="Phenotype/disease identifier (clinical links)",
    )
    network: Optional[str] = Field(
        None,
        description="Interaction network identifier (STRING, BioGRID)",
    )

    @field_validator(
        "structure",
        "sequence",
        "dynamics",
        "metabolism",
        "interactions",
        "phenotype",
        "network",
        mode="after",
    )
    def validate_suffix(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not value.startswith(BUDO_ID_PATTERN):
            raise ValueError("Composite identifiers must begin with 'budo:'")
        return value


class ExternalReferences(BaseModel):
    """Cross-reference catalogue for external knowledge bases."""

    uniprot: Optional[str] = Field(None, description="UniProt accession")
    pdb: List[str] = Field(default_factory=list, description="Experimental structures")
    alphafold: Optional[str] = Field(None, description="AlphaFold model accession")
    cath: List[str] = Field(default_factory=list, description="CATH domain identifiers")
    md_cath: List[str] = Field(default_factory=list, description="mdCATH domain identifiers")
    omim: Optional[str] = Field(None, description="OMIM disease accession")
    chembl: Optional[str] = Field(None, description="ChEMBL target identifier")
    pubchem: Optional[str] = Field(None, description="PubChem CID")
    string: Optional[str] = Field(None, description="STRING protein identifier")
    ncbi_gene: Optional[str] = Field(None, description="NCBI Gene identifier")


class LigandAssociation(BaseModel):
    """Association between a BUDO entity and a small molecule."""

    ligand_id: str = Field(..., description="Primary identifier (ChEMBL, DrugBank, PubChem, etc.)")
    name: str = Field(..., description="Ligand common name")
    source: Literal["ChEMBL", "DrugBank", "PubChem", "Custom"] = Field(
        ..., description="Originating database"
    )
    relationship: Literal[
        "activator",
        "inhibitor",
        "agonist",
        "antagonist",
        "binder",
        "metabolite",
        "cofactor",
    ] = Field(..., description="Type of biochemical relationship")
    affinity_nm: Optional[float] = Field(
        None,
        ge=0,
        description="Binding affinity (nanomolar) when available",
    )
    smiles: Optional[str] = Field(None, description="Canonical SMILES string")
    inchi_key: Optional[str] = Field(None, description="InChI Key")
    evidence: Optional[str] = Field(
        None,
        description="Free-text provenance (publication, assay, etc.)",
    )
    references: List[str] = Field(
        default_factory=list,
        description="External references supporting the interaction",
    )


class VariantAnnotation(BaseModel):
    """Variant level annotations linked back to the root entity."""

    variant_id: str = Field(..., description="Variant identifier (HGVS-based)")
    label: str = Field(..., description="Human readable name")
    impact_type: Literal[
        "gain-of-function",
        "loss-of-function",
        "ambiguous",
        "unknown",
    ] = Field(..., description="Predicted functional impact")
    molecular_consequence: Optional[str] = Field(
        None, description="Mechanistic interpretation of variant"
    )
    phenotype: Optional[str] = Field(
        None, description="Linked phenotype or disease state"
    )
    evidence: List[str] = Field(
        default_factory=list, description="References or datasets supporting the annotation"
    )


class AuditTrail(BaseModel):
    """Traceability metadata for the CEA registry."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    curator: Optional[str] = Field(
        None, description="Person or agent responsible for the latest update"
    )
    pipeline: Optional[str] = Field(
        None,
        description="Automated pipeline responsible for ingesting/updating the entity",
    )


class CEAEntity(BaseModel):
    """Canonical identity surface for a biological entity within BSM."""

    budo_id: str = Field(..., description="Root identity managed by CEA (budo:ENTITY_vX)")
    entity_type: Literal["Protein", "Complex", "SmallMolecule"] = Field(
        "Protein", description="Type of entity represented"
    )
    name: str = Field(..., description="Preferred label")
    organism: Optional[str] = Field(None, description="Organism taxonomy label")
    version: str = Field("1.0", description="Version identifier for this entity")
    description: Optional[str] = Field(None, description="Free-text summary")
    composite_ids: CompositeIdentifiers = Field(
        default_factory=CompositeIdentifiers,
        description="Modality-specific identifiers derived from the root budoId",
    )
    cross_references: ExternalReferences = Field(
        default_factory=ExternalReferences,
        description="External database mappings",
    )
    ligands: List[LigandAssociation] = Field(
        default_factory=list,
        description="Collection of experimentally supported ligand associations",
    )
    variants: List[VariantAnnotation] = Field(
        default_factory=list,
        description="Known or predicted variant annotations",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Free-form tags that can be used for searching/filtering",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata not covered by structured fields",
    )
    audit: AuditTrail = Field(
        default_factory=AuditTrail,
        description="Traceability metadata for compliance",
    )

    @field_validator("budo_id", mode="after")
    def validate_budo_id(cls, value: str) -> str:
        if not value.startswith(BUDO_ID_PATTERN):
            raise ValueError("budo_id must begin with 'budo:'")
        if "_v" not in value:
            raise ValueError("budo_id must include a version suffix (e.g., _v1)")
        return value

    @field_validator("tags", mode="after")
    def normalise_tags(cls, value: List[str]) -> List[str]:
        return [item.strip().lower() for item in value]

    def keyword_tokens(self) -> List[str]:
        """Generate keyword tokens for Milvus indexing and search."""

        tokens = {value for value in self.tags if value}
        tokens.add(f"budo_id:{self.budo_id}")
        if self.cross_references.uniprot:
            tokens.add(f"uniprot:{self.cross_references.uniprot}")
        if self.cross_references.chembl:
            tokens.add(f"chembl:{self.cross_references.chembl}")
        for ligand in self.ligands:
            tokens.add(f"ligand:{ligand.ligand_id}")
        return sorted(tokens)

"""
BioSite V3 Schema
=================

Site-specific biological entities linked to BUDO objects with ESE signatures.

Author: Alex Rodriguez & Sofia Petrov
Lab: Alex Rodriguez AI Systems Architecture Lab / Sofia Petrov Engineering Lab
Phase: 0.002 - Schema Definition & Standards
Date: October 2, 2025
Version: 3.0.0
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .budo_v3 import (
    BudoESESignature,
    BudoProvenance,
    ConfidenceLevel,
    FunctionalState,
)


class BioSiteType(str, Enum):
    """Types of biological sites"""

    ACTIVE_SITE = "active_site"
    BINDING_SITE = "binding_site"
    ALLOSTERIC_SITE = "allosteric_site"
    CATALYTIC_SITE = "catalytic_site"
    PTM_SITE = "ptm_site"  # Post-translational modification
    INTERFACE = "interface"  # Protein-protein interface
    EPITOPE = "epitope"
    MOTIF = "motif"
    UNKNOWN = "unknown"


class ConformationalState(str, Enum):
    """Conformational states of BioSites"""

    OPEN = "open"
    CLOSED = "closed"
    INTERMEDIATE = "intermediate"
    FLEXIBLE = "flexible"
    RIGID = "rigid"
    UNKNOWN = "unknown"


# ============================================================================
# NESTED MODELS
# ============================================================================


class BioSiteResidue(BaseModel):
    """Individual residue in a BioSite"""

    position: int = Field(ge=1, description="Position in protein sequence")
    residue_name: str = Field(description="Three-letter amino acid code")
    residue_code: str = Field(
        description="One-letter amino acid code", min_length=1, max_length=1
    )
    role: Optional[str] = Field(None, description="Role in site (catalytic, binding)")
    is_conserved: bool = Field(default=False, description="Evolutionary conservation")


class BioSiteLigand(BaseModel):
    """Ligand bound to or interacting with a BioSite"""

    ligand_id: str = Field(description="Ligand identifier (PDB HET ID, ChEMBL, etc.)")
    ligand_name: str = Field(description="Ligand name")
    binding_affinity: Optional[float] = Field(
        None, description="Binding affinity (Kd, Ki, IC50)"
    )
    affinity_unit: Optional[str] = Field(None, description="Unit (nM, uM, etc.)")
    interaction_type: str = Field(
        description="Type: covalent, non-covalent, hydrogen_bond"
    )


class BioSiteFunctionalState(BaseModel):
    """Site-specific functional state"""

    current_state: FunctionalState = Field(default=FunctionalState.UNKNOWN)
    conformational_state: ConformationalState = Field(
        default=ConformationalState.UNKNOWN
    )
    predicted_state: Optional[FunctionalState] = Field(None)
    prediction_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    history: List[Dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# MAIN BIOSITE V3 MODEL
# ============================================================================


class BioSiteV3(BaseModel):
    """
    BioSite V3: Site-specific biological entity

    Represents functional sites within proteins with:
    - Parent BUDO object reference
    - Site-specific ESE signatures
    - Conformational states
    - Residue-level annotations
    - Ligand interactions
    """

    # ========================================================================
    # IDENTITY & CORE METADATA
    # ========================================================================

    biosite_id: str = Field(
        description="Unique BioSite identifier (linked to BUDO ID)",
        pattern=r"^biosite:[A-Z0-9_]+-[SDLQF]-\d+$",
    )

    parent_budo_id: str = Field(
        description="Parent BUDO object ID",
        pattern=r"^budo:[A-Z0-9_]+-[SDLQF]$",
    )

    site_name: str = Field(description="BioSite name (e.g., ATP binding site)")

    site_type: BioSiteType = Field(description="Type of biological site")

    # ========================================================================
    # RESIDUES & STRUCTURE
    # ========================================================================

    residues: List[BioSiteResidue] = Field(
        description="Residues comprising the BioSite"
    )

    start_position: int = Field(ge=1, description="Start position in protein sequence")

    end_position: int = Field(ge=1, description="End position in protein sequence")

    sequence_motif: Optional[str] = Field(
        None, description="Consensus sequence motif (if any)"
    )

    # ========================================================================
    # FUNCTIONAL STATE
    # ========================================================================

    functional_state: BioSiteFunctionalState = Field(
        default_factory=BioSiteFunctionalState,
        description="Site-specific functional and conformational state",
    )

    # ========================================================================
    # ESE SIGNATURE (SITE-SPECIFIC)
    # ========================================================================

    ese_signature: Optional[BudoESESignature] = Field(
        None, description="ESE signature specific to this BioSite"
    )

    # ========================================================================
    # LIGANDS & INTERACTIONS
    # ========================================================================

    ligands: List[BioSiteLigand] = Field(
        default_factory=list, description="Ligands interacting with this site"
    )

    interacting_proteins: List[str] = Field(
        default_factory=list, description="BUDO IDs of interacting proteins"
    )

    # ========================================================================
    # ANNOTATIONS
    # ========================================================================

    go_terms: List[Dict[str, Any]] = Field(
        default_factory=list, description="Site-specific GO terms"
    )

    biological_function: Optional[str] = Field(
        None, description="Biological function description"
    )

    diseases: List[str] = Field(
        default_factory=list, description="Associated diseases (if known)"
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
    # METHODS
    # ========================================================================

    def add_ligand(self, ligand: BioSiteLigand) -> None:
        """Add a ligand interaction to the BioSite"""
        self.ligands.append(ligand)
        self.provenance.updated_at = datetime.utcnow()
        self.provenance.version += 1

    def update_conformational_state(
        self,
        new_state: ConformationalState,
        confidence: Optional[float] = None,
        source: str = "chronoracle",
    ) -> None:
        """Update conformational state with provenance tracking"""
        history_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "from_state": self.functional_state.conformational_state.value,
            "to_state": new_state.value,
            "source": source,
            "confidence": confidence,
        }
        self.functional_state.history.append(history_entry)

        self.functional_state.conformational_state = new_state
        if confidence:
            self.functional_state.prediction_confidence = confidence
        self.functional_state.last_updated = datetime.utcnow()

        self.provenance.updated_at = datetime.utcnow()
        self.provenance.updated_by = source
        self.provenance.version += 1

    def set_ese_signature(self, ese: BudoESESignature) -> None:
        """Set ESE signature for this BioSite"""
        self.ese_signature = ese
        self.provenance.updated_at = datetime.utcnow()
        self.provenance.version += 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation"""
        return self.model_dump()

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "biosite_id": "biosite:ABL1_HUMAN_v1-D-001",
                "parent_budo_id": "budo:ABL1_HUMAN_v1-D",
                "site_name": "ATP binding site",
                "site_type": "binding_site",
                "residues": [
                    {
                        "position": 315,
                        "residue_name": "ASP",
                        "residue_code": "D",
                        "role": "catalytic",
                        "is_conserved": True,
                    }
                ],
                "start_position": 300,
                "end_position": 330,
                "functional_state": {
                    "current_state": "active",
                    "conformational_state": "open",
                },
            }
        },
    )


# ============================================================================
# EXPORT
# ============================================================================

__all__ = [
    "BioSiteV3",
    "BioSiteResidue",
    "BioSiteLigand",
    "BioSiteFunctionalState",
    "BioSiteType",
    "ConformationalState",
]

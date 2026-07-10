"""
BSM Schemas Package
===================

Unified schemas for BSM-BUDO-CEA project.

Author: Alex Rodriguez (Chief Data Architect)
Date: October 8, 2025
Version: 1.0.0
"""

from .budo_v3 import (
    BudoV3,
    BudoDomain,
    BudoVariant,
    BudoFunctionalState,
    BudoESESignature,
    BudoEmbedding,
    BudoProvenance,
    BudoCrossReference,
    ModalitySuffix,
    FunctionalState,
    ConfidenceLevel,
)

from .biosite_v3 import (
    BioSiteV3,
    BioSiteType,
    ConformationalState,
    BioSiteResidue,
    BioSiteLigand,
    BioSiteFunctionalState,
)

from .cea import (
    CEAEntity,
    CompositeIdentifiers,
    ExternalReferences,
    LigandAssociation,
    VariantAnnotation,
    AuditTrail,
)

__all__ = [
    # BUDO V3
    "BudoV3",
    "BudoDomain",
    "BudoVariant",
    "BudoFunctionalState",
    "BudoESESignature",
    "BudoEmbedding",
    "BudoProvenance",
    "BudoCrossReference",
    "ModalitySuffix",
    "FunctionalState",
    "ConfidenceLevel",
    # BioSite V3
    "BioSiteV3",
    "BioSiteType",
    "ConformationalState",
    "BioSiteResidue",
    "BioSiteLigand",
    "BioSiteFunctionalState",
    # CEA
    "CEAEntity",
    "CompositeIdentifiers",
    "ExternalReferences",
    "LigandAssociation",
    "VariantAnnotation",
    "AuditTrail",
]

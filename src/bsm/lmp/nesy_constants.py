"""
LMP v2.0 NeSy Canonical Vocabulary and Ontology

This module defines the OFFICIAL ontology for LMP v2.0 NeSy markers.
ALL mappers must import and use these canonical vocabularies.

NO mapper should invent its own markers. Any feature that cannot be
mapped to a canonical marker should be labeled as 'UNKNOWN_FEATURE'
for manual review.

References:
- LMP.MD Sections 2.1-2.6 (XML syntax)
- LMP.MD Section 4.2 (Linearized syntax for PLMs)
- LMP.MD ANEXO (Complete NeSy specification)

Author: AI University Research Team
Date: November 3, 2025
"""

from typing import Dict, Optional
from dataclasses import dataclass

# ============================================================================
# CANONICAL PTM VOCABULARY
# ============================================================================

@dataclass
class PTMType:
    """Canonical PTM type definition"""
    nesy_prefix: str           # e.g., 'P', 'Ac', 'Me1'
    uniprot_keywords: list     # UniProt FT description keywords
    residues: list             # Valid residues (e.g., ['S', 'T', 'Y'])
    enzyme_pattern: str        # Regex pattern to extract enzyme

# Canonical PTM types (from LMP v2.0 specification)
CANONICAL_PTMS: Dict[str, PTMType] = {
    'phosphorylation': PTMType(
        nesy_prefix='P',
        uniprot_keywords=['phospho', 'phosphorylation'],
        residues=['S', 'T', 'Y'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|mediated by ([A-Z][A-Z0-9]+)'
    ),
    'acetylation': PTMType(
        nesy_prefix='Ac',
        uniprot_keywords=['acetyl', 'acetylation'],
        residues=['K'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|acetyltransferase ([A-Z0-9]+)'
    ),
    'methylation_1': PTMType(
        nesy_prefix='Me1',
        uniprot_keywords=['mono-methyl', 'monomethyl'],
        residues=['K', 'R'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|methyltransferase ([A-Z0-9]+)'
    ),
    'methylation_2': PTMType(
        nesy_prefix='Me2',
        uniprot_keywords=['di-methyl', 'dimethyl'],
        residues=['K', 'R'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|methyltransferase ([A-Z0-9]+)'
    ),
    'methylation_3': PTMType(
        nesy_prefix='Me3',
        uniprot_keywords=['tri-methyl', 'trimethyl'],
        residues=['K'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|methyltransferase ([A-Z0-9]+)'
    ),
    'methylation': PTMType(  # Generic fallback
        nesy_prefix='Me',
        uniprot_keywords=['methyl', 'methylation'],
        residues=['K', 'R'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|methyltransferase ([A-Z0-9]+)'
    ),
    'ubiquitination': PTMType(
        nesy_prefix='Ub',
        uniprot_keywords=['ubiquitin', 'ubiquitination'],
        residues=['K'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)|E3 ligase ([A-Z0-9]+)'
    ),
    'sumoylation': PTMType(
        nesy_prefix='SUMO',
        uniprot_keywords=['sumo', 'sumoylation'],
        residues=['K'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'disulfide_bond': PTMType(
        nesy_prefix='C-S-S-C',
        uniprot_keywords=['disulfide bond', 'cross-link'],
        residues=['C'],
        enzyme_pattern=None  # Not enzymatically catalyzed
    ),
    'palmitoylation': PTMType(
        nesy_prefix='Pal',
        uniprot_keywords=['palmitoyl', 'palmitoylation'],
        residues=['S', 'C'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'n_glycosylation': PTMType(
        nesy_prefix='N-Glyc',
        uniprot_keywords=['n-glycosyl', 'n-linked', 'n-glycan'],
        residues=['N'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'o_glycosylation': PTMType(
        nesy_prefix='O-Glyc',
        uniprot_keywords=['o-glycosyl', 'o-linked', 'o-glycan'],
        residues=['S', 'T'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'glycosylation': PTMType(  # Generic fallback
        nesy_prefix='Glyc',
        uniprot_keywords=['glycosyl', 'glycosylation', 'glcnac'],
        residues=['N', 'S', 'T'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'adp_ribosylation': PTMType(
        nesy_prefix='ADP-Rib',
        uniprot_keywords=['adp-ribosyl', 'adp ribosylation'],
        residues=['E', 'D', 'K', 'R'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'glycyl_lysine': PTMType(
        nesy_prefix='K-Gly',
        uniprot_keywords=['glycyl lysine', 'glycine-lysine', 'k-gly'],
        residues=['K'],
        enzyme_pattern=None  # Crosslink
    ),
    'lysine_lactylation': PTMType(
        nesy_prefix='K-La',
        uniprot_keywords=['lactyl', 'lactylation', 'lysine lactylation', 'n6-lactoyllysine'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_carboxylation': PTMType(
        nesy_prefix='K-Car',
        uniprot_keywords=['carboxyl', 'carboxylation', 'lysine carboxylation', 'lysine 5-hydroxylation and carboxylation'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'tryptophan_crosslink': PTMType(
        nesy_prefix='W-W',
        uniprot_keywords=['tryptophan-3-yl)-tryptophan', 'trp-trp', 'cross-link'],
        residues=['W'],
        enzyme_pattern=None
    ),
    
    # ========================================================================
    # FORENSIC ANALYSIS ADDITIONS (November 4, 2025)
    # From 50-protein analysis - 20 new PTM types identified
    # ========================================================================
    
    # LYSINE ACYLATIONS (Non-acetyl) - 40 total occurrences
    'lysine_succinylation': PTMType(
        nesy_prefix='K-Succ',
        uniprot_keywords=['succinyl', 'n6-succinyllysine'],
        residues=['K'],
        enzyme_pattern=None  # Add when discovered
    ),
    'lysine_2_hydroxyisobutyrylation': PTMType(
        nesy_prefix='K-Hib',
        uniprot_keywords=['2-hydroxyisobutyryl', 'hydroxyisobutyryl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_malonylation': PTMType(
        nesy_prefix='K-Mal',
        uniprot_keywords=['malonyl', 'n6-malonyllysine'],
        residues=['K'],
        enzyme_pattern=None
    ),
    
    # CYSTEINE MODIFICATIONS - 13 total occurrences
    's_nitrosylation': PTMType(
        nesy_prefix='C-NO',
        uniprot_keywords=['s-nitrosocysteine', 'nitrosocysteine', 'nitrosylation'],
        residues=['C'],
        enzyme_pattern=None
    ),
    'cysteine_persulfide': PTMType(
        nesy_prefix='C-SSH',
        uniprot_keywords=['cysteine persulfide', 'persulfide'],
        residues=['C'],
        enzyme_pattern=None
    ),
    's_succinylcysteine': PTMType(
        nesy_prefix='C-Succ',
        uniprot_keywords=['s-(2-succinyl)', 'succinyl cysteine'],
        residues=['C'],
        enzyme_pattern=None
    ),
    'adp_ribosylcysteine': PTMType(
        nesy_prefix='C-ADPr',
        uniprot_keywords=['adp-ribosylcysteine'],
        residues=['C'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    
    # METHIONINE OXIDATION - 3 occurrences
    'methionine_sulfoxide': PTMType(
        nesy_prefix='M-SO',
        uniprot_keywords=['methionine sulfoxide', 'methionine (r)-sulfoxide'],
        residues=['M'],
        enzyme_pattern=None  # Oxidative damage
    ),
    
    # GLUTAMATE/ASPARTATE MODIFICATIONS - 24 occurrences
    'gamma_carboxyglutamate': PTMType(
        nesy_prefix='E-Car',
        uniprot_keywords=['4-carboxyglutamate', 'gamma-carboxyglutamate', 'carboxyglutamate'],
        residues=['E'],
        enzyme_pattern=None  # Vitamin K-dependent
    ),
    '3_hydroxyaspartate': PTMType(
        nesy_prefix='D-Hyd',
        uniprot_keywords=['3-hydroxyaspartate', 'hydroxyaspartate', '(3r)-3-hydroxyaspartate'],
        residues=['D'],
        enzyme_pattern=None
    ),
    'polyglutamylation': PTMType(
        nesy_prefix='E-Poly',
        uniprot_keywords=['glutamyl polyglutamate', 'polyglutamate', '5-glutamyl'],
        residues=['E'],
        enzyme_pattern=None
    ),
    
    # PROLINE MODIFICATIONS - 2 occurrences
    '4_hydroxyproline': PTMType(
        nesy_prefix='P-Hyd',
        uniprot_keywords=['4-hydroxyproline', 'hydroxyproline'],
        residues=['P'],
        enzyme_pattern=None
    ),
    
    # ARGININE MODIFICATIONS - 2 occurrences
    'citrullination': PTMType(
        nesy_prefix='R-Cit',
        uniprot_keywords=['citrulline', 'citrullination', 'deimination'],
        residues=['R'],
        enzyme_pattern=r'padi\d*'  # PADI enzymes
    ),
    'adp_riboxanated_arginine': PTMType(
        nesy_prefix='R-ADPr',
        uniprot_keywords=['adp-riboxanated', 'adp-ribosylation'],
        residues=['R'],
        enzyme_pattern=None
    ),
    
    # TYROSINE MODIFICATIONS - 3 occurrences
    'nitrotyrosine': PTMType(
        nesy_prefix='Y-NO2',
        uniprot_keywords=['nitrotyrosine', "3'-nitrotyrosine"],
        residues=['Y'],
        enzyme_pattern=None  # Oxidative/nitrosative stress
    ),
    
    # ASPARAGINE MODIFICATIONS - 7 occurrences
    'asparagine_deamidation': PTMType(
        nesy_prefix='N-Deam',
        uniprot_keywords=['deamidated asparagine', 'deamidation'],
        residues=['N'],
        enzyme_pattern=None  # Spontaneous degradation
    ),
    
    # N-TERMINAL MODIFICATIONS - 3 occurrences
    'pyroglutamate': PTMType(
        nesy_prefix='Q-Pyro',
        uniprot_keywords=['pyrrolidone carboxylic', 'pyroglutamic', 'pyroglutamate'],
        residues=['Q'],
        enzyme_pattern=None  # N-terminal cyclization
    ),
    'n_pyruvate_iminyl_valine': PTMType(
        nesy_prefix='V-Pyr',
        uniprot_keywords=['pyruvate 2-iminyl', 'n-pyruvate'],
        residues=['V'],
        enzyme_pattern=None  # Hemoglobin variant
    ),
    'n_acetylthreonine': PTMType(
        nesy_prefix='Ac-T',
        uniprot_keywords=['n-acetylthreonine'],
        residues=['T'],
        enzyme_pattern=None  # N-terminal processing
    ),
    
    # ADDITIONAL MOD MARKERS (from final gap analysis)
    'o_amp_threonine': PTMType(
        nesy_prefix='T-AMP',
        uniprot_keywords=['o-amp-threonine', 'amp-threonine', 'ampylation'],
        residues=['T'],
        enzyme_pattern=None
    ),
    'glutamine_deamidation': PTMType(
        nesy_prefix='Q-Deam',
        uniprot_keywords=['deamidated glutamine', 'glutamine deamidation'],
        residues=['Q'],
        enzyme_pattern=None
    ),
    
    # TRYPTOPHAN CROSSLINK (already added, ensuring coverage)
    'tryptophan_tryptophan_crosslink': PTMType(
        nesy_prefix='W-W',
        uniprot_keywords=['tryptophan-3-yl)-tryptophan', 'trp-trp', 'tryptophan crosslink', 'tryptophanyl', '(trp-trp)'],
        residues=['W'],
        enzyme_pattern=None
    ),
    
    # PROTEIN CONJUGATION CROSSLINKS
    'ubiquitination': PTMType(
        nesy_prefix='K-Ub',
        uniprot_keywords=['glycyl lysine isopeptide', 'ubiquitin', 'lys-gly', 'g-cter in ubiquitin'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'sumoylation': PTMType(
        nesy_prefix='K-SUMO',
        uniprot_keywords=['sumo', 'g-cter in sumo'],
        residues=['K'],
        enzyme_pattern=None
    ),
    
    # ========================================================================
    # 100-PROTEIN FORENSIC ADDITIONS (November 2025)
    # From comprehensive extraction: 22 new PTM types
    # Total occurrences: ~200 across 107 proteins
    # Expected improvement: 94.7% → 98-99% canonical
    # ========================================================================
    
    # LYSINE ACYLATIONS (High-frequency histone marks) - 88 occurrences
    'lysine_glutarylation': PTMType(
        nesy_prefix='K-Glu',
        uniprot_keywords=['n6-glutaryllysine', 'glutaryl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_beta_hydroxybutyrylation': PTMType(
        nesy_prefix='K-Bhb',
        uniprot_keywords=['n6-(beta-hydroxybutyryl)lysine', 'beta-hydroxybutyryl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_crotonylation': PTMType(
        nesy_prefix='K-Cro',
        uniprot_keywords=['n6-crotonyllysine', 'crotonyl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_butyrylation': PTMType(
        nesy_prefix='K-Bu',
        uniprot_keywords=['n6-butyryllysine', 'butyryl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_propionylation': PTMType(
        nesy_prefix='K-Pro',
        uniprot_keywords=['n6-propionyllysine', 'propionyl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    
    # LYSINE MODIFICATIONS (Structural/Enzymatic) - 15 occurrences
    'lysine_hydroxylation': PTMType(
        nesy_prefix='K-Hyd',
        uniprot_keywords=['5-hydroxylysine', 'hydroxylysine'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_allysine': PTMType(
        nesy_prefix='K-Ald',
        uniprot_keywords=['allysine'],
        residues=['K'],
        enzyme_pattern=None  # Oxidative deamination product
    ),
    'lysine_dopaminylation': PTMType(
        nesy_prefix='K-Dop',
        uniprot_keywords=['5-glutamyl dopamine', 'dopaminylation'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_serotonylation': PTMType(
        nesy_prefix='K-Ser',
        uniprot_keywords=['5-glutamyl serotonin', 'serotonylation'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_adp_ribosylation': PTMType(
        nesy_prefix='K-ADPr',
        uniprot_keywords=['n6-(adp-ribosyl)lysine', 'adp-ribosyl lysine'],
        residues=['K'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    
    # LYSINE LIPIDATION - 3 occurrences
    'lysine_myristoylation': PTMType(
        nesy_prefix='K-Myr',
        uniprot_keywords=['n6-myristoyl lysine'],
        residues=['K'],
        enzyme_pattern=None
    ),
    'lysine_decanoylation': PTMType(
        nesy_prefix='K-Dec',
        uniprot_keywords=['n6-decanoyllysine', 'decanoyl'],
        residues=['K'],
        enzyme_pattern=None
    ),
    
    # N-TERMINAL ACETYLATION - 24 occurrences
    'n_terminal_acetylation': PTMType(
        nesy_prefix='N-Ac',
        uniprot_keywords=['n-acetylmethionine', 'n-acetylalanine', 'n-acetylserine', 
                         'n-acetylproline', 'n-acetylvaline', 'n-acetylaspartate'],
        residues=['M', 'A', 'S', 'P', 'V', 'D'],  # N-terminal positions
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    
    # N-TERMINAL MYRISTOYLATION - 4 occurrences
    'myristoylation': PTMType(
        nesy_prefix='N-Myr',
        uniprot_keywords=['n-myristoyl glycine'],
        residues=['G'],  # N-terminal
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    
    # POLY-ADP-RIBOSYLATION (Distinct from mono-ADP-ribosylation) - 17 occurrences
    'glutamate_poly_adp_ribosylation': PTMType(
        nesy_prefix='E-pADPr',
        uniprot_keywords=['polyadp-ribosyl glutamic'],
        residues=['E'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'aspartate_poly_adp_ribosylation': PTMType(
        nesy_prefix='D-pADPr',
        uniprot_keywords=['polyadp-ribosyl aspartic'],
        residues=['D'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    'serine_adp_ribosylation': PTMType(
        nesy_prefix='S-ADPr',
        uniprot_keywords=['adp-ribosylserine'],
        residues=['S'],
        enzyme_pattern=r'by ([A-Z][A-Z0-9]+)'
    ),
    
    # PROLINE 3-HYDROXYLATION - 5 occurrences
    '3_hydroxyproline': PTMType(
        nesy_prefix='P-3Hyd',
        uniprot_keywords=['3-hydroxyproline'],
        residues=['P'],
        enzyme_pattern=None
    ),
    
    # SERINE O-ACETYLATION - 1 occurrence
    'o_acetylserine': PTMType(
        nesy_prefix='S-Ac',
        uniprot_keywords=['o-acetylserine'],
        residues=['S'],
        enzyme_pattern=None
    ),
    
    # TYROSINE SULFONATION - 2 occurrences
    'sulfotyrosine': PTMType(
        nesy_prefix='Y-SO3',
        uniprot_keywords=['sulfotyrosine'],
        residues=['Y'],
        enzyme_pattern=None
    ),
    
    # HISTIDINE METHYLATION - 1 occurrence
    'histidine_methylation': PTMType(
        nesy_prefix='H-Me',
        uniprot_keywords=['tele-methylhistidine'],
        residues=['H'],
        enzyme_pattern=None
    ),
    
    # ARGININE GLYCOSYLATION - 2 occurrences
    'arginine_glycosylation': PTMType(
        nesy_prefix='R-Glyc',
        uniprot_keywords=['n-beta-linked (glcnac) arginine'],
        residues=['R'],
        enzyme_pattern=None
    ),
    
    # ========================================================================
    # PLANT PROTEIN FORENSIC ADDITIONS (November 4, 2025)
    # From 19 plant protein analysis - 3 new functional marker types
    # Coverage boost: 58.3% → 100.0%
    # ========================================================================
    
    # BINDING SITES - 76 occurrences in plant proteins
    # Includes: chlorophyll binding, cofactor binding, metal binding
    # Example: Photosystem II (P12329) - 23 chlorophyll binding sites
    # ChEBI references: chlorophyll a (58416), Mg (25107)
    'binding_site': PTMType(
        nesy_prefix='BIND',
        uniprot_keywords=['binding site', 'active site', 'axial binding'],
        residues=['S', 'T', 'Y', 'K', 'R', 'E', 'D', 'H', 'C', 'N', 'Q', 'W', 'F'],  # All can bind ligands
        enzyme_pattern=None  # Not enzymatic - ligand interaction
    ),
    
    # FUNCTIONAL SITES - 3 occurrences
    # Catalytic residues, transition state stabilizers
    # Example: Rubisco (P00877) Lys334 - transition state stabilizer
    'functional_site': PTMType(
        nesy_prefix='SITE',
        uniprot_keywords=['site', 'transition state stabilizer'],
        residues=['K', 'R', 'E', 'D', 'H', 'S', 'T', 'Y', 'C'],  # Catalytic residues
        enzyme_pattern=None  # Defines catalytic mechanism
    ),
    
    # ACTIVE SITE MODIFICATIONS - 1 occurrence
    # Transient covalent modifications during catalysis
    # Example: PEP carboxylase (P25858) His156 - nucleophile
    # Forms phospho-enzyme intermediate (PubMed: 22607208)
    'active_site_modification': PTMType(
        nesy_prefix='MOD',
        uniprot_keywords=['active site', 'nucleophile', 'phosphoserine intermediate'],
        residues=['S', 'T', 'H', 'C', 'D', 'E'],  # Nucleophilic residues
        enzyme_pattern=None  # Part of catalytic cycle
    ),
}

# Disulfide bond canonical marker (REMOVED - now in CANONICAL_PTMS)
# DISULFIDE_MARKER = 'C-S-S-C'  # Two markers: one at each Cys

# ============================================================================
# CANONICAL BINDING SITE VOCABULARY
# ============================================================================

@dataclass
class BindingSiteType:
    """Canonical binding site type definition"""
    nesy_marker: str           # e.g., 'ATP', 'ION:Zn'
    uniprot_keywords: list     # UniProt FT description keywords
    parameter_pattern: str     # Regex to extract parameter (e.g., ion type)

CANONICAL_BINDING_SITES: Dict[str, BindingSiteType] = {
    'ATP-binding': BindingSiteType(
        nesy_marker='ATP',
        uniprot_keywords=['atp', 'adenosine triphosphate'],
        parameter_pattern=None
    ),
    'GTP-binding': BindingSiteType(
        nesy_marker='GTP',
        uniprot_keywords=['gtp', 'guanosine triphosphate'],
        parameter_pattern=None
    ),
    'NTP-binding': BindingSiteType(
        nesy_marker='NTP',
        uniprot_keywords=['nucleotide', 'ntp', 'adp', 'adenosine diphosphate'],
        parameter_pattern=None
    ),
    'NAD-binding': BindingSiteType(
        nesy_marker='NAD',
        uniprot_keywords=['nad', 'nadh', 'nadp', 'nadph', 'nicotinamide'],
        parameter_pattern=None
    ),
    'substrate': BindingSiteType(
        nesy_marker='SUB',
        uniprot_keywords=['substrate', 'glyceraldehyde', 'bisphosphoglycerate', '2,3-bpg'],
        parameter_pattern=None
    ),
    'bicarbonate-binding': BindingSiteType(
        nesy_marker='HCO3',
        uniprot_keywords=['bicarbonate', 'hydrogencarbonate', 'carbonate'],
        parameter_pattern=None
    ),
    'ion-binding': BindingSiteType(
        nesy_marker='ION:{}',  # Parameter: Zn, Ca, Mg, Fe, etc.
        uniprot_keywords=['ion', 'metal', 'zinc', 'calcium', 'magnesium', 'iron', 'copper'],
        parameter_pattern=r'(zinc|zn\(2\+\)|calcium|ca\(2\+\)|magnesium|mg\(2\+\)|iron|fe\(2\+\)|fe\(3\+\)|copper|cu\(2\+\))'
    ),
    'DNA-binding': BindingSiteType(
        nesy_marker='DNA:{}',  # Parameter: Major, Minor, Backbone
        uniprot_keywords=['dna', 'nucleotide'],
        parameter_pattern=r'(major groove|minor groove|backbone)'
    ),
    'RNA-binding': BindingSiteType(
        nesy_marker='RNA',
        uniprot_keywords=['rna'],
        parameter_pattern=None
    ),
    'catalytic': BindingSiteType(
        nesy_marker='CAT',
        uniprot_keywords=['catalytic', 'active site', 'catalysis'],
        parameter_pattern=None
    ),
    'substrate': BindingSiteType(
        nesy_marker='SUB',
        uniprot_keywords=['substrate'],
        parameter_pattern=None
    ),
    'cofactor': BindingSiteType(
        nesy_marker='COF',
        uniprot_keywords=['cofactor', 'coenzyme'],
        parameter_pattern=None
    ),
    'lipid-binding': BindingSiteType(
        nesy_marker='LIP',
        uniprot_keywords=['lipid', 'phospholipid', 'membrane'],
        parameter_pattern=None
    ),
    'heme-binding': BindingSiteType(
        nesy_marker='HEME',
        uniprot_keywords=['heme', 'haem', 'porphyrin'],
        parameter_pattern=None
    ),
    'oxygen-binding': BindingSiteType(
        nesy_marker='OXY',
        uniprot_keywords=['oxygen', 'o2', 'dioxygen'],
        parameter_pattern=None
    ),
    'inositol-phosphate': BindingSiteType(
        nesy_marker='INO',
        uniprot_keywords=['inositol', 'phosphoinositide'],
        parameter_pattern=None
    ),
    'ip6-binding': BindingSiteType(
        nesy_marker='IP6',
        uniprot_keywords=['hexakisphosphate', 'ip6', 'phytic acid'],
        parameter_pattern=None
    ),
    'ip4-binding': BindingSiteType(
        nesy_marker='IP4',
        uniprot_keywords=['tetrakisphosphate', 'ip4'],
        parameter_pattern=None
    ),
    'drug-binding': BindingSiteType(
        nesy_marker='DRUG',
        uniprot_keywords=['drug', 'inhibitor', 'antagonist', 'agonist', 'ligand'],
        parameter_pattern=None
    ),
    'protein-interaction': BindingSiteType(
        nesy_marker='PROT-INT',
        uniprot_keywords=['interaction with', 'binds', 'partner', 'interacts with'],
        parameter_pattern=r'interaction with ([A-Z0-9]+)|binds ([A-Z0-9]+)'
    ),
    'dimerization-site': BindingSiteType(
        nesy_marker='DIM',
        uniprot_keywords=['dimerization', 'heterodimerization', 'interface', 'dimer interface'],
        parameter_pattern=None
    ),
    'kinase-binding': BindingSiteType(
        nesy_marker='KIN-BIND',
        uniprot_keywords=['cdk7 binding', 'kinase binding', 'cdk binding'],
        parameter_pattern=r'([A-Z0-9]+) binding'
    ),
    'ligand-site': BindingSiteType(
        nesy_marker='LIG-SITE',
        uniprot_keywords=['implicated in', 'ligand binding', 'agonist binding', 'catechol'],
        parameter_pattern=None
    ),
    'translocation-breakpoint': BindingSiteType(
        nesy_marker='TRANS-BP',
        uniprot_keywords=['breakpoint', 'translocation'],
        parameter_pattern=None
    ),
    'hydrophobic-barrier': BindingSiteType(
        nesy_marker='HYDRO-BAR',
        uniprot_keywords=['hydrophobic barrier'],
        parameter_pattern=None
    ),
    'catalytic-activation': BindingSiteType(
        nesy_marker='CAT-ACT',
        uniprot_keywords=['activates', 'activation', 'thiol group'],
        parameter_pattern=None
    ),
    'isomerization-site': BindingSiteType(
        nesy_marker='ISOM',
        uniprot_keywords=['isomerization', 'proline isomerization'],
        parameter_pattern=None
    ),
    'cleavage-site': BindingSiteType(
        nesy_marker='CLEAV',
        uniprot_keywords=['cleavage', 'cleaved', 'cleavage site'],
        parameter_pattern=r'by (viral|host|caspase|furin|protease|[\w-]+)'
    ),
}

# ============================================================================
# CANONICAL DOMAIN VOCABULARY (Pfam → NeSy)
# ============================================================================# ============================================================================
# CANONICAL DOMAIN VOCABULARY (Pfam → NeSy)
# ============================================================================

# Pfam ID → Canonical NeSy domain marker
PFAM_TO_NESY_DOMAIN: Dict[str, str] = {
    'PF00069': 'DOM:Kinase',           # Protein kinase domain
    'PF07714': 'DOM:Kinase',           # Protein tyrosine kinase
    'PF00018': 'DOM:SH3',              # SH3 domain
    'PF00017': 'DOM:SH2',              # SH2 domain
    'PF00595': 'DOM:PDZ',              # PDZ domain
    'PF00169': 'DOM:PH',               # PH domain
    'PF00168': 'DOM:C2',               # C2 domain
    'PF00130': 'DOM:C1',               # C1 domain
    'PF00041': 'DOM:Fn3',              # Fibronectin type III domain
    'PF07679': 'DOM:Ig',               # Immunoglobulin I-set domain
    'PF00047': 'DOM:Ig',               # Immunoglobulin domain
    'PF00071': 'DOM:Ras',              # Ras family
    'PF00076': 'DOM:RRM',              # RNA recognition motif
    'PF00096': 'DOM:Zn_finger',        # Zinc finger, C2H2 type
    'PF00104': 'DOM:Hormone_recep',    # Ligand-binding domain of nuclear hormone receptor
    'PF00249': 'DOM:Myb',              # Myb-like DNA-binding domain
    'PF00270': 'DOM:DEAD',             # DEAD/DEAH box helicase
    'PF00400': 'DOM:WD40',             # WD domain, G-beta repeat
    'PF00515': 'DOM:TPR',              # Tetratricopeptide repeat
    'PF01352': 'DOM:KRAB',             # KRAB box
    'PF00531': 'DOM:Death',            # Death domain
    'PF00619': 'DOM:CARD',             # Caspase recruitment domain
    'PF00653': 'DOM:BIR',              # BIR repeat
    'PF00560': 'DOM:LRR',              # Leucine Rich Repeat
    # Add more Pfam mappings as needed
}

# PROSITE ID → Canonical NeSy domain marker
PROSITE_TO_NESY_DOMAIN: Dict[str, str] = {
    'PS50011': 'DOM:Kinase',           # Protein kinase domain profile
    'PS50001': 'DOM:SH3',              # SH3 domain profile
    'PS50002': 'DOM:SH2',              # SH2 domain profile
    'PS50106': 'DOM:PDZ',              # PDZ domain profile
    'PS50003': 'DOM:PH',               # PH domain profile
    'PS50004': 'DOM:C2',               # C2 domain profile
    # Add more PROSITE mappings as needed
}

# ============================================================================
# CANONICAL MOTIF VOCABULARY
# ============================================================================

CANONICAL_MOTIFS: Dict[str, str] = {
    'NLS': 'MOT:NLS',                  # Nuclear localization signal
    'NES': 'MOT:NES',                  # Nuclear export signal
    'DFG': 'MOT:DFG',                  # DFG motif (kinases)
    'GXGXXG': 'MOT:GXGXXG',            # Glycine-rich loop (kinases)
    'HRD': 'MOT:HRD',                  # HRD motif (kinases)
    'APE': 'MOT:APE',                  # APE motif (kinases)
    'NPXY': 'MOT:NPXY',                # NPXY motif (endocytosis)
    'YXXL': 'MOT:YXXL',                # YXXL motif (clathrin binding)
    'KDEL': 'MOT:KDEL',                # ER retention signal
    'RGD': 'MOT:RGD',                  # RGD integrin binding motif
}

# ============================================================================
# CANONICAL REGULATORY & PPI VOCABULARY
# ============================================================================

@dataclass
class RegulatorySiteType:
    """Canonical regulatory site type definition"""
    nesy_marker_open: str      # e.g., '<PPI:{}'
    nesy_marker_close: str     # e.g., '</PPI>'
    uniprot_keywords: list     # UniProt FT description keywords
    parameter_pattern: Optional[str] = None  # For extracting PPI partner ID

CANONICAL_REGULATORY_SITES: Dict[str, RegulatorySiteType] = {
    'allosteric': RegulatorySiteType(
        nesy_marker_open=r'\ALLO\\',
        nesy_marker_close=r'/ALLO\\',
        uniprot_keywords=['allosteric', 'allostery', 'allosteric site'],
        parameter_pattern=None
    ),
    'pam': RegulatorySiteType(
        nesy_marker_open=r'\PAM\\',
        nesy_marker_close=r'/PAM\\',
        uniprot_keywords=['positive allosteric modulator', 'pam site'],
        parameter_pattern=None
    ),
    'nam': RegulatorySiteType(
        nesy_marker_open=r'\NAM\\',
        nesy_marker_close=r'/NAM\\',
        uniprot_keywords=['negative allosteric modulator', 'nam site'],
        parameter_pattern=None
    ),
    'ppi_interface': RegulatorySiteType(
        nesy_marker_open='<PPI:{}',
        nesy_marker_close='</PPI>',
        uniprot_keywords=['interaction with', 'dimerization', 'binds', 'interface'],
        parameter_pattern=r'interaction with ([A-Z0-9]+)|binds ([A-Z0-9]+)|partner ([A-Z0-9]+)'
    ),
    'g_protein_coupling': RegulatorySiteType(
        nesy_marker_open='<G-PROT>',
        nesy_marker_close='</G-PROT>',
        uniprot_keywords=['g-protein coupled', 'g protein coupled', 'g(s) coupled', 'g(i) coupled', 'g(q) coupled'],
        parameter_pattern=r'g\(([si]q?)\)'  # Extract G protein type
    ),
    'arrestin_coupling': RegulatorySiteType(
        nesy_marker_open='<ARREST>',
        nesy_marker_close='</ARREST>',
        uniprot_keywords=['arrestin', 'beta-arrestin', 'arrestin coupling'],
        parameter_pattern=None
    ),
    'regulatory_region': RegulatorySiteType(
        nesy_marker_open='<REG>',
        nesy_marker_close='</REG>',
        uniprot_keywords=['regulatory', 'regulation', 'activation loop', 'inhibitory'],
        parameter_pattern=None
    ),
}

# ============================================================================
# CANONICAL PROCESSING & STRUCTURAL SITES
# ============================================================================

@dataclass
class ProcessingSiteType:
    """Processing and structural site type definition"""
    nesy_marker: str           # e.g., 'CLEAVE', 'SIG'
    uniprot_keywords: list     # UniProt FT description keywords

CANONICAL_PROCESSING_SITES: Dict[str, ProcessingSiteType] = {
    'cleavage': ProcessingSiteType(
        nesy_marker='CLEAVE',
        uniprot_keywords=['cleavage', 'cleavage site', 'proteolytic']
    ),
    'signal_peptide': ProcessingSiteType(
        nesy_marker='SIG',
        uniprot_keywords=['signal', 'signal peptide', 'signal sequence']
    ),
    'propeptide': ProcessingSiteType(
        nesy_marker='PRO',
        uniprot_keywords=['propeptide', 'proprotein']
    ),
    'transit_peptide': ProcessingSiteType(
        nesy_marker='TRANSIT',
        uniprot_keywords=['transit', 'transit peptide', 'mitochondrial targeting']
    ),
    'transmembrane': ProcessingSiteType(
        nesy_marker='TMD',
        uniprot_keywords=['transmembrane', 'tm helix', 'membrane-spanning']
    ),
}

# ============================================================================
# CANONICAL LIGAND VOCABULARY
# ============================================================================

@dataclass
class LigandType:
    """Canonical ligand marker definition for drug discovery
    
    Ligands are pharmacological compounds that bind to proteins.
    NeSy uses punctual markers (+AGO[], +INH[T1:], etc.) to annotate ligand binding sites.
    
    Attributes:
        nesy_marker: NeSy marker template (e.g., '+AGO[{}' for agonist)
        uniprot_keywords: Keywords to search in UniProt FT descriptions
        requires_state: Optional conformational state required (e.g., 'dfg-in' for T1 inhibitors)
    """
    nesy_marker: str
    uniprot_keywords: list
    requires_state: Optional[str] = None

CANONICAL_LIGAND_MARKERS: Dict[str, LigandType] = {
    'agonist': LigandType(
        nesy_marker='+AGO[{}',
        uniprot_keywords=['agonist', 'activator', 'full agonist', 'partial agonist'],
        requires_state=None
    ),
    'antagonist': LigandType(
        nesy_marker='+ANT[{}',
        uniprot_keywords=['antagonist', 'blocker', 'inverse agonist'],
        requires_state=None
    ),
    'inhibitor_type1': LigandType(
        nesy_marker='+INH[T1:{}',
        uniprot_keywords=['inhibitor', 'type i inhibitor', 'type-i inhibitor', 'type 1 inhibitor'],
        requires_state='dfg-in'  # Type I inhibitors bind DFG-in conformation
    ),
    'inhibitor_type2': LigandType(
        nesy_marker='+INH[T2:{}',
        uniprot_keywords=['inhibitor', 'type ii inhibitor', 'type-ii inhibitor', 'type 2 inhibitor'],
        requires_state='dfg-out'  # Type II inhibitors bind DFG-out conformation
    ),
    'inhibitor_allosteric': LigandType(
        nesy_marker='+INH[ALLO:{}',
        uniprot_keywords=['allosteric inhibitor', 'allosteric modulator', 'non-competitive inhibitor'],
        requires_state=None
    ),
    'inhibitor_generic': LigandType(
        nesy_marker='+INH[{}',
        uniprot_keywords=['inhibitor'],  # Generic fallback if type unknown
        requires_state=None
    ),
    'fragment': LigandType(
        nesy_marker='+FRAG[{}',
        uniprot_keywords=['fragment', 'fragment-based', 'fragment screening'],
        requires_state=None
    ),
}

# ============================================================================
# CANONICAL STATE MARKERS
# ============================================================================

CANONICAL_STATES: Dict[str, str] = {
    'active': 'ACTIVE',
    'inactive': 'INACTIVE',
    'dfg-in': 'DFG-IN',
    'dfg-out': 'DFG-OUT',
    'open': 'OPEN',
    'closed': 'CLOSED',
}

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def normalize_ion_name(ion_str: str) -> str:
    """Normalize ion name to canonical form"""
    ion_map = {
        'zinc': 'Zn',
        'zn(2+)': 'Zn',
        'zn2+': 'Zn',
        'calcium': 'Ca2+',
        'ca(2+)': 'Ca2+',
        'ca2+': 'Ca2+',
        'magnesium': 'Mg2+',
        'mg(2+)': 'Mg2+',
        'mg2+': 'Mg2+',
        'iron': 'Fe',
        'fe(2+)': 'Fe2+',
        'fe(3+)': 'Fe3+',
        'copper': 'Cu',
        'cu(2+)': 'Cu2+',
    }
    return ion_map.get(ion_str.lower(), ion_str)

def normalize_dna_binding_mode(mode_str: str) -> str:
    """Normalize DNA binding mode to canonical form"""
    mode_map = {
        'major groove': 'Major',
        'minor groove': 'Minor',
        'backbone': 'Backbone',
    }
    return mode_map.get(mode_str.lower(), 'Unknown')

def get_methylation_level(description: str) -> str:
    """
    Extract methylation level from description
    
    DEPRECATED: Use explicit methylation_1, _2, _3 entries in CANONICAL_PTMS instead
    This is kept for backward compatibility only
    """
    desc_lower = description.lower()
    if 'mono-methyl' in desc_lower or 'monomethyl' in desc_lower:
        return 'Me1'
    elif 'di-methyl' in desc_lower or 'dimethyl' in desc_lower:
        return 'Me2'
    elif 'tri-methyl' in desc_lower or 'trimethyl' in desc_lower:
        return 'Me3'
    else:
        return 'Me'  # Default (unknown level)

# ============================================================================
# VALIDATION
# ============================================================================

def is_valid_nesy_marker(marker_type: str) -> bool:
    """Check if a marker type is canonical"""
    
    # Check PTMs
    for ptm in CANONICAL_PTMS.values():
        if marker_type.startswith(ptm.nesy_prefix):
            return True
    
    # Check binding sites
    for site in CANONICAL_BINDING_SITES.values():
        base_marker = site.nesy_marker.split(':')[0]
        if marker_type.startswith(base_marker):
            return True
    
    # Check domains
    if marker_type.startswith('DOM:'):
        return True
    
    # Check motifs
    if marker_type.startswith('MOT:'):
        return True
    
    # Check states
    if marker_type in CANONICAL_STATES.values():
        return True
    
    # Check regulatory sites
    for site in CANONICAL_REGULATORY_SITES.values():
        base_marker = site.nesy_marker_open.replace('{}', '').replace('<', '').replace('>', '').replace(r'\\', '')
        if marker_type.startswith(base_marker) or marker_type == base_marker:
            return True
    
    # Check processing sites
    for site in CANONICAL_PROCESSING_SITES.values():
        if marker_type.startswith(site.nesy_marker) or marker_type == site.nesy_marker:
            return True
    
    # Check ligand markers
    for ligand in CANONICAL_LIGAND_MARKERS.values():
        base_marker = ligand.nesy_marker.replace('{}', '').replace('[', '').replace(']', '')
        if marker_type.startswith(base_marker):
            return True
    
    # Check disulfide (now in CANONICAL_PTMS as 'disulfide_bond')
    if marker_type == 'C-S-S-C':
        return True
    
    # Check structural regions
    if marker_type in ['COIL', 'REG', 'PPI', 'CAT', 'SUB']:
        return True
    
    return False

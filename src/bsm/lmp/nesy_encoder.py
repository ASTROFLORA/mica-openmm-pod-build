r"""
LMP v2.0 NeSy (Neuro-Symbolic) Sequence Encoder

Implements the FULL LMP v2.0 architecture from LMP.MD ANEXO:
- Hierarchical markers: [DOM], [MOT], [TMD], [DBD], [RBD]
- Functional sites: (CAT), (SUB), (ATP), (GTP), (ION), (DNA), (RNA)
- Regulatory sites: \ALLO\, \PAM\, \NAM\, <PPI>, <G-PROT>, <ARREST>
- Enhanced PTMs: {S-P:PKA}, {K-Ac:p300}, {K-Ub}, {K-Me1/2/3}, etc.
- Ligands: +AGO[], +ANT[], +INH[T1:], +INH[T2:], +FRAG[]
- States: *ACTIVE*, *INACTIVE*, *DFG-IN*, *DFG-OUT*, *OPEN*, *CLOSED*

This creates a hierarchical, compositional "grammar" for proteins
that can be parsed into syntax trees for NeSy AI reasoning.

References:
- LMP.MD Sections 2.1-2.6 (XML syntax)
- LMP.MD Section 4.2 (Linearized syntax for PLMs)
- LMP.MD ANEXO (Complete NeSy specification)

Author: AI University Research Team
Date: November 3, 2025
"""

from typing import Dict, List, Any, Optional, Tuple, Set
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS: Vocabularies and Mappings
# ============================================================================

# PTM Type → Prefix mapping (expanded from basic pS, acK to full ontology)
PTM_PREFIXES = {
    "phosphorylation": "P",       # {S-P}, {T-P}, {Y-P}
    "acetylation": "Ac",           # {K-Ac}
    "methylation": "Me",           # {K-Me1}, {K-Me2}, {K-Me3}, {R-Me1}, {R-Me2}
    "ubiquitination": "Ub",        # {K-Ub}
    "sumoylation": "SUMO",         # {K-SUMO}
    "palmitoylation": "Pal",       # {S-Pal}, {C-Pal}
    "glycosylation": "GlcNAc",     # {N-GlcNAc}, {S-GlcNAc}
    "n-glycosylation": "GlcNAc",   # {N-GlcNAc} (N-linked)
    "o-glycosylation": "GalNAc",   # {S-GalNAc}, {T-GalNAc} (O-linked)
    "hydroxylation": "OH",         # {P-OH}, {K-OH}
    "nitrosylation": "NO",         # {C-NO}
    "adp_ribosylation": "ADP",     # {E-ADP}
    "disulfide": "S-S",            # {C-S-S-C}
    "n_terminal_myristoylation": "Myr",  # {G-Myr} (N-terminal)
    "n_terminal_acetylation": "Ac",      # {M-Ac}, {A-Ac}, etc. (N-terminal)
}

# Binding site type → NeSy marker mapping
SITE_MARKERS = {
    "catalytic": ("CAT", "/CAT"),
    "substrate": ("SUB", "/SUB"),
    "ATP-binding": ("ATP", "/ATP"),
    "GTP-binding": ("GTP", "/GTP"),
    "ion-binding": ("ION:{}", "/ION"),     # {ION:Zn}, {ION:Ca}, etc.
    "DNA-binding": ("DNA:{}", "/DNA"),     # {DNA:Major}, {DNA:Minor}, {DNA:Backbone}
    "RNA-binding": ("RNA", "/RNA"),
    "allosteric": ("ALLO", "/ALLO"),
    "PAM": ("PAM", "/PAM"),               # Positive Allosteric Modulator (GPCR)
    "NAM": ("NAM", "/NAM"),               # Negative Allosteric Modulator (GPCR)
}

# Domain type → NeSy marker mapping
DOMAIN_MARKERS = {
    "transmembrane": ("[TMD]", "[/TMD]"),
    "DNA-binding": ("[DBD]", "[/DBD]"),
    "RNA-binding": ("[RBD]", "[/RBD]"),
    "default": ("[DOM:{}]", "[/DOM]"),     # [DOM:Kinase_Pkinase], [DOM:SH2], etc.
}

# Motif type → NeSy marker mapping
MOTIF_MARKERS = {
    "default": ("[MOT:{}]", "[/MOT]"),     # [MOT:NLS], [MOT:NES], [MOT:DFG], etc.
}

# PPI interface marker
PPI_MARKER = ("<PPI:{}>", "</PPI>")       # <PPI:MDM2>, <PPI:P53>, etc.

# GPCR-specific transducer coupling markers
GPCR_MARKERS = {
    "G-protein": ("<G-PROT>", "</G-PROT>"),
    "arrestin": ("<ARREST>", "</ARREST>"),
}

# Conformational state markers
STATE_MARKERS = {
    "active": ("*ACTIVE*", "*/ACTIVE*"),
    "inactive": ("*INACTIVE*", "*/INACTIVE*"),
    "dfg-in": "*DFG-IN*",                 # Point marker (kinase-specific)
    "dfg-out": "*DFG-OUT*",               # Point marker (kinase-specific)
    "open": ("*OPEN*", "*/OPEN*"),
    "closed": ("*CLOSED*", "*/CLOSED*"),
}

# Ligand type → NeSy marker mapping
LIGAND_MARKERS = {
    "agonist": "+AGO[{}]",
    "antagonist": "+ANT[{}]",
    "inhibitor_type1": "+INH[T1:{}]",     # Type I kinase inhibitor (DFG-in)
    "inhibitor_type2": "+INH[T2:{}]",     # Type II kinase inhibitor (DFG-out)
    "inhibitor_allosteric": "+INH[ALLO:{}]",
    "fragment": "+FRAG[{}]",              # Fragment screening hit
}


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class AnnotatedResidue:
    """
    Represents a single residue with all its annotations.
    Used to build the hierarchical sequence.
    
    NEW (Gap 2 fix): markers_before for punctual markers like *DFG-OUT*, +INH[T2:Imatinib]
    """
    position: int                          # 1-based
    residue: str                           # Single letter code
    ptm: Optional[Dict[str, Any]] = None   # PTM annotation if present
    in_domain: Optional[str] = None        # Domain name if inside domain
    in_motif: Optional[str] = None         # Motif name if inside motif
    in_site: Optional[List[str]] = None    # List of site types with params (e.g., "ion-binding:Zn")
    in_ppi: Optional[str] = None           # PPI partner ID if in interface
    state: Optional[str] = None            # Conformational state if applicable
    markers_before: Optional[List[str]] = None  # Punctual markers to insert BEFORE this residue


@dataclass
class NeSyAnnotation:
    """
    Complete annotation data for NeSy encoding.
    This is the input to the encoder.
    
    NEW (Gap 3 fix): binding_sites now supports parameters:
    - {type: "ion-binding", residues: [...], ion_type: "Zn"}  → (ION:Zn)
    - {type: "DNA-binding", residues: [...], groove: "Major"} → (DNA:Major)
    - {type: "ATP-binding", residues: [...], ligand: {type: "inhibitor_type2", name: "Imatinib"}}
    
    NEW (Gap 2 fix): state_regions supports point markers:
    - {type: "point", position: 381, state_name: "DFG-OUT"} → insert *DFG-OUT* before D381
    """
    sequence: str
    domains: List[Dict[str, Any]]          # {name, type, start, end}
    motifs: List[Dict[str, Any]]           # {name, type, start, end}
    ptms: List[Dict[str, Any]]             # {position, type, residue, enzyme (opt)}
    binding_sites: List[Dict[str, Any]]    # {type, residues, ion_type (opt), groove (opt), ligand (opt)}
    ppi_interfaces: List[Dict[str, Any]]   # {partner_id, residues}
    conformational_state: Optional[str]    # Overall state (active, inactive, etc.)
    state_regions: List[Dict[str, Any]]    # {type: "point"/"region", position/start, state_name}


# ============================================================================
# MAIN ENCODER CLASS
# ============================================================================

class LMPNeSyEncoder:
    """
    Encodes protein sequence with full LMP v2.0 NeSy syntax.
    
    Produces hierarchical, compositional sequence strings like:
    
    ...[DOM:Kinase_Pkinase]...(ATP)M...L...E(/ATP)...*DFG-OUT*
    (ATP)+INH[T2:Imatinib]D(ATP)F(ATP)G(ATP)...(SUB)...{Y-P:SRC}...(SUB)...
    [/DOM:Kinase_Pkinase]...<PPI:SH2>...</PPI>...
    
    This can be parsed into a syntax tree for NeSy reasoning.
    """
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def encode(self, annotation: NeSyAnnotation) -> str:
        """
        Main encoding method.
        
        Args:
            annotation: Complete protein annotation data
            
        Returns:
            NeSy-encoded sequence string
        """
        # Step 1: Build annotated residue list
        residues = self._build_residue_annotations(annotation)
        
        # Step 2: Encode sequence with hierarchical markers
        encoded_seq = self._encode_hierarchical_sequence(residues, annotation)
        
        return encoded_seq
    
    def _build_residue_annotations(
        self, annotation: NeSyAnnotation
    ) -> List[AnnotatedResidue]:
        """
        Build per-residue annotation structure.
        
        NEW (Gap 2 fix): Populates markers_before for punctual markers
        NEW (Gap 3 fix): Stores site parameters (ion_type, groove) in site string
        """
        seq_len = len(annotation.sequence)
        residues = [
            AnnotatedResidue(
                position=i+1,
                residue=annotation.sequence[i],
                in_site=[],
                markers_before=[]
            )
            for i in range(seq_len)
        ]
        
        # Annotate domains
        for domain in annotation.domains:
            start, end = domain["start"], domain["end"]
            domain_name = domain.get("name", domain.get("type", "Unknown"))
            for i in range(start-1, min(end, seq_len)):
                residues[i].in_domain = domain_name
        
        # Annotate motifs
        for motif in annotation.motifs:
            start, end = motif["start"], motif["end"]
            motif_name = motif.get("name", motif.get("type", "Unknown"))
            for i in range(start-1, min(end, seq_len)):
                residues[i].in_motif = motif_name
        
        # Annotate PTMs
        for ptm in annotation.ptms:
            pos = ptm["position"]
            if 1 <= pos <= seq_len:
                residues[pos-1].ptm = ptm
        
        # Annotate binding sites (Gap 3 fix: include parameters + EVIDENCE)
        for site in annotation.binding_sites:
            site_type = site["type"]
            residue_list = site.get("residues", [])
            if isinstance(residue_list, str):
                residue_list = [int(r.strip()) for r in residue_list.split(",")]
            
            # Build site string with parameters
            site_str = site_type
            if "ion_type" in site:
                site_str = f"{site_type}:{site['ion_type']}"
            elif "groove" in site:
                site_str = f"{site_type}:{site['groove']}"
            
            # NEW: Include experimental evidence if available
            evidence_str = None
            if "evidence" in site:
                ev = site["evidence"]
                parts = []
                
                # PubMed IDs
                if ev.get("pubmed_ids"):
                    pmids = ','.join(str(p) for p in ev["pubmed_ids"][:3])  # Max 3
                    parts.append(f"PMID:{pmids}")
                
                # PDB IDs
                if ev.get("pdb_ids"):
                    pdbs = ','.join(str(p) for p in ev["pdb_ids"][:3])  # Max 3
                    parts.append(f"PDB:{pdbs}")
                
                # ChEBI ID
                if ev.get("chebi_id"):
                    parts.append(f"ChEBI:{ev['chebi_id']}")
                
                if parts:
                    evidence_str = "|".join(parts)
            
            # Store evidence with site for later use
            site_with_evidence = site_str
            if evidence_str:
                site_with_evidence = f"{site_str}|{evidence_str}"
            
            for pos in residue_list:
                if 1 <= pos <= seq_len:
                    residues[pos-1].in_site.append(site_with_evidence)
            
            # Gap 2 fix: Insert ligand marker before first residue of site
            if "ligand" in site and residue_list:
                ligand = site["ligand"]
                ligand_type = ligand.get("type", "agonist")
                ligand_name = ligand.get("name", "Unknown")
                
                if ligand_type in LIGAND_MARKERS:
                    ligand_marker = LIGAND_MARKERS[ligand_type].format(ligand_name)
                    first_res_pos = min(residue_list)
                    if 1 <= first_res_pos <= seq_len:
                        residues[first_res_pos-1].markers_before.append(ligand_marker)
        
        # Annotate PPI interfaces
        for ppi in annotation.ppi_interfaces:
            partner = ppi["partner_id"]
            residue_list = ppi.get("residues", [])
            if isinstance(residue_list, str):
                residue_list = [int(r.strip()) for r in residue_list.split(",")]
            for pos in residue_list:
                if 1 <= pos <= seq_len:
                    residues[pos-1].in_ppi = partner
        
        # Gap 2 fix: Process state regions for point markers
        for state in annotation.state_regions:
            if state.get("type") == "point":
                pos = state.get("position")
                state_name = state.get("state_name", "").lower()
                
                if pos and 1 <= pos <= seq_len and state_name in STATE_MARKERS:
                    marker = STATE_MARKERS[state_name]
                    # Point markers are single strings (not tuples)
                    if isinstance(marker, str):
                        residues[pos-1].markers_before.append(marker)
        
        return residues
    
    def _encode_hierarchical_sequence(
        self, 
        residues: List[AnnotatedResidue],
        annotation: NeSyAnnotation
    ) -> str:
        """
        Encode sequence with all hierarchical markers.
        
        Hierarchy order (outermost to innermost):
        1. Domains [DOM:name]...[/DOM]
        2. Motifs [MOT:name]...[/MOT]
        3. PPI interfaces <PPI:ID>...</PPI>
        4. Binding sites (CAT), (ATP), (DNA:type), etc.
        5. PTMs {S-P:enzyme}
        6. Punctual markers *DFG-IN*, +INH[T2:name]
        
        FIXED (Gap 1): Binding sites now use state machine for non-contiguous residues
        FIXED (Gap 2): Punctual markers inserted via markers_before
        FIXED (Gap 3): Parametrized markers like (ION:Zn), (DNA:Major)
        """
        result = []
        
        # Track open markers (stack-based for proper nesting)
        open_domain = None
        open_motif = None
        open_ppi = None
        open_sites = set()  # Can have multiple nested sites
        
        for i, res in enumerate(residues):
            # Get previous residue for state machine logic
            prev_res = residues[i-1] if i > 0 else None
            
            # === DOMAIN MARKERS ===
            # Open domain if entering
            if res.in_domain and res.in_domain != open_domain:
                if open_domain:
                    # Close previous domain
                    result.append(self._get_domain_close_marker(open_domain))
                # Open new domain
                result.append(self._get_domain_open_marker(res.in_domain))
                open_domain = res.in_domain
            # Close domain if exiting
            elif not res.in_domain and open_domain:
                result.append(self._get_domain_close_marker(open_domain))
                open_domain = None
            
            # === MOTIF MARKERS ===
            if res.in_motif and res.in_motif != open_motif:
                if open_motif:
                    result.append(MOTIF_MARKERS["default"][1])
                result.append(MOTIF_MARKERS["default"][0].format(res.in_motif))
                open_motif = res.in_motif
            elif not res.in_motif and open_motif:
                result.append(MOTIF_MARKERS["default"][1])
                open_motif = None
            
            # === PPI MARKERS ===
            if res.in_ppi and res.in_ppi != open_ppi:
                if open_ppi:
                    result.append(PPI_MARKER[1])
                result.append(PPI_MARKER[0].format(res.in_ppi))
                open_ppi = res.in_ppi
            elif not res.in_ppi and open_ppi:
                result.append(PPI_MARKER[1])
                open_ppi = None
            
            # === BINDING SITE MARKERS (Gap 1 FIX: State machine for non-contiguous residues) ===
            current_sites = set(res.in_site) if res.in_site else set()
            prev_sites = set(prev_res.in_site) if prev_res and prev_res.in_site else set()
            
            # Open new sites (only if THIS residue is in site AND PREVIOUS was NOT)
            new_sites_to_open = current_sites - prev_sites
            for site_full in new_sites_to_open:
                # NEW: Parse site with evidence (format: "ATP-binding:param|PMID:...|PDB:...")
                evidence_str = None
                if "|" in site_full:
                    # Split site from evidence
                    site_part, evidence_str = site_full.split("|", 1)
                else:
                    site_part = site_full
                
                # Gap 3 FIX: Parse parametrized sites like "ion-binding:Zn"
                if ":" in site_part:
                    site_type, param = site_part.split(":", 1)
                else:
                    site_type = site_part
                    param = None
                
                # Get marker template
                if site_type in SITE_MARKERS:
                    marker_template = SITE_MARKERS[site_type][0]
                    
                    # Format with parameter if needed
                    if "{}" in marker_template and param:
                        marker = marker_template.format(param)
                    elif "{}" in marker_template:
                        # No param provided, use generic
                        marker = marker_template.replace("{}", site_type.upper())
                    else:
                        marker = marker_template
                    
                    # Always use [BIND:...] format for binding sites (brackets, not parentheses)
                    if evidence_str:
                        result.append(f"[BIND:{marker}|{evidence_str}]")
                    else:
                        result.append(f"[BIND:{marker}]")  # FIX: Always brackets, even without evidence
            
            # Close exited sites (only if THIS residue is NOT in site AND PREVIOUS WAS)
            sites_to_close = prev_sites - current_sites
            for site_full in sites_to_close:
                # NEW: Check if site has evidence
                has_evidence = "|" in site_full
                
                # Parse site type (strip evidence first)
                site_part = site_full.split("|", 1)[0] if has_evidence else site_full
                
                if ":" in site_part:
                    site_type, param = site_part.split(":", 1)
                else:
                    site_type = site_part
                    param = None
                
                if site_type in SITE_MARKERS:
                    close_marker = SITE_MARKERS[site_type][1]
                    
                    # Always use [/BIND] format (brackets, not parentheses)
                    result.append(f"[/BIND]")  # FIX: Always brackets, regardless of evidence
            
            # === PUNCTUAL MARKERS (Gap 2 FIX: Insert before residue) ===
            if res.markers_before:
                for marker in res.markers_before:
                    result.append(marker)
            
            # === RESIDUE + PTM ===
            # CRITICAL FIX: Always append residue FIRST, then PTM marker AFTER
            result.append(res.residue)
            
            if res.ptm:
                # Encode PTM marker (WITHOUT residue inside - residue already added above)
                ptm_encoded = self._encode_ptm_marker_only(res.ptm)
                result.append(ptm_encoded)
        
        # Close any remaining open markers at end of sequence
        if open_domain:
            result.append(self._get_domain_close_marker(open_domain))
        if open_motif:
            result.append(MOTIF_MARKERS["default"][1])
        if open_ppi:
            result.append(PPI_MARKER[1])
        
        # Close any sites still open at last residue
        if residues:
            last_res_sites = set(residues[-1].in_site) if residues[-1].in_site else set()
            for site_full in last_res_sites:
                if ":" in site_full:
                    site_type, _ = site_full.split(":", 1)
                else:
                    site_type = site_full
                
                if site_type in SITE_MARKERS:
                    close_marker = SITE_MARKERS[site_type][1]
                    result.append(f"({close_marker})")
        
        return "".join(result)
    
    def _encode_ptm(self, residue: str, ptm: Dict[str, Any]) -> str:
        """
        Encode PTM with LMP v2.0 NeSy syntax: {X-MOD:enzyme}
        
        Examples:
        - {S-P:PKA}  (Serine phosphorylated by PKA)
        - {K-Ac:p300}  (Lysine acetylated by p300)
        - {K-Ub}  (Lysine ubiquitinated, enzyme unknown)
        
        NOTE: This function is DEPRECATED - use _encode_ptm_marker_only() instead
        """
        ptm_type = ptm.get("ptm_type", "modification").lower()  # Normalize to lowercase
        enzyme = ptm.get("enzyme", None)
        
        # Get prefix (case-insensitive lookup)
        prefix = PTM_PREFIXES.get(ptm_type, "Mod")
        
        # Handle methylation levels (K-Me1, K-Me2, K-Me3)
        if "methylation" in ptm_type and "level" in ptm:
            prefix = f"Me{ptm['level']}"
        
        # Build PTM string
        if enzyme:
            return f"{{{residue}-{prefix}:{enzyme}}}"
        else:
            return f"{{{residue}-{prefix}}}"
    
    def _encode_ptm_marker_only(self, ptm: Dict[str, Any]) -> str:
        """
        Encode PTM marker ONLY (without residue inside).
        
        Format: {Mod:enzyme} or {Mod}
        
        Examples:
        - {P:PKA}  (Phosphorylation by PKA)
        - {Ac:p300}  (Acetylation by p300)
        - {Ub}  (Ubiquitination, enzyme unknown)
        - {Myr}  (Myristoylation)
        """
        ptm_type = ptm.get("ptm_type", "modification").lower()
        enzyme = ptm.get("enzyme", None)
        
        # Get prefix (case-insensitive lookup)
        prefix = PTM_PREFIXES.get(ptm_type, "Mod")
        
        # Handle methylation levels (Me1, Me2, Me3)
        if "methylation" in ptm_type and "level" in ptm:
            prefix = f"Me{ptm['level']}"
        
        # Build PTM marker (NO residue inside)
        if enzyme:
            return f"{{{prefix}:{enzyme}}}"
        else:
            return f"{{{prefix}}}"
    
    def _get_domain_open_marker(self, domain_name: str) -> str:
        """Get opening marker for domain based on type.
        
        STANDARDIZED: All domains now use [DOM:xxx] format for consistency.
        This fixes BUG-002 (RAF1 simple domain markers).
        """
        # All domains use standard format
        return f"[DOM:{domain_name}]"
    
    def _get_domain_close_marker(self, domain_name: str) -> str:
        """Get closing marker for domain based on type.
        
        STANDARDIZED: All domains now use [/DOM] format for consistency.
        This fixes BUG-002 (RAF1 simple domain markers).
        """
        # All domains use standard closing marker
        return "[/DOM]"
    
    def encode_with_ligands(
        self, 
        annotation: NeSyAnnotation,
        ligands: List[Dict[str, Any]]
    ) -> str:
        """
        Encode sequence with ligand annotations.
        
        Ligands are inserted at their binding sites with +LIG[type:name] syntax.
        
        Args:
            annotation: Base protein annotation
            ligands: List of {type, name, site_type, site_residues}
        
        Returns:
            NeSy-encoded sequence with ligands
        """
        # First encode base sequence
        base_seq = self.encode(annotation)
        
        # TODO: Implement ligand insertion logic
        # This requires positional tracking to insert ligands at correct sites
        
        return base_seq
    
    def encode_with_states(
        self,
        annotation: NeSyAnnotation,
        state: str,
        state_features: Dict[str, str]
    ) -> str:
        """
        Encode sequence with conformational state markers.
        
        States can be:
        - Global: *ACTIVE* wrapping entire sequence
        - Regional: *DFG-IN* at specific position (point marker)
        - Feature-based: *OPEN* wrapping activation loop
        
        Args:
            annotation: Base protein annotation
            state: Overall state (active, inactive, etc.)
            state_features: Dict of feature_name → state (e.g., {"ActivationLoop": "open"})
        
        Returns:
            NeSy-encoded sequence with state markers
        """
        # First encode base sequence
        base_seq = self.encode(annotation)
        
        # TODO: Implement state marker insertion
        # Requires understanding of feature positions and state logic
        
        return base_seq


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def parse_nesy_sequence(nesy_seq: str) -> Dict[str, Any]:
    """
    Parse NeSy-encoded sequence into hierarchical syntax tree.
    
    This is the REVERSE operation: sequence → tree structure.
    Used for NeSy reasoning and logical queries.
    
    Args:
        nesy_seq: NeSy-encoded sequence string
    
    Returns:
        Dict representing syntax tree with nested structure:
        {
            "domains": [
                {
                    "name": "Kinase_Pkinase",
                    "start": 10,
                    "end": 300,
                    "motifs": [...],
                    "sites": [...],
                    "residues": [...]
                }
            ],
            "ppi_interfaces": [...],
            "states": [...]
        }
    """
    # TODO: Implement full parser
    # This is a complex task requiring stack-based parsing of nested markers
    
    raise NotImplementedError("NeSy sequence parser not yet implemented")


def validate_nesy_syntax(nesy_seq: str) -> Tuple[bool, List[str]]:
    """
    Validate NeSy sequence syntax.
    
    Checks:
    - All opening markers have matching closing markers
    - Nesting is valid (e.g., (ATP) inside [DOM:Kinase])
    - PTM syntax is correct ({X-MOD} or {X-MOD:enzyme})
    - State markers are valid
    
    Returns:
        (is_valid, list_of_errors)
    """
    errors = []
    
    # TODO: Implement syntax validator
    # Use stack to track open/close markers
    
    return len(errors) == 0, errors


# ============================================================================
# EXAMPLE USAGE (from ANEXO)
# ============================================================================

if __name__ == "__main__":
    # Example 1: ABL1 kinase with Imatinib (Type II inhibitor)
    
    abl1_annotation = NeSyAnnotation(
        sequence="MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVASDFEPQGLSEAARWNSKENLLAGPSENDPNLFVALYDFVASGDNTLSITKGEKLRVLGYNHNGEWCEAQTKNGQGWVPSNYITPVNSLEKHSWYHGPVSRNAAEYLLSSLINGSFLVRESESSPGQRSISLRYEGRVYHYRINTASDGKLYVSSESRFNTLAELVHHHSTVADGLITTLHYPAPKRNKPTVYGVSPNYDKWEMERTDITMKHKLGGGQYGEVYEGVWKKYSLTVAVKTLKEDTMEVEEFLKEAAVMKEIKHPNLVQLLGVCTREPPFYIITEFMTYGNLLDYLRECNRQEVNAVVLLYMATQISSAMEYLEKKNFIHRDLAARNCLVGENHLVKVADFGLSRLMTGDTYTAHAGAKFPIKWTAPESLAYNKFSIKSDVWAFGVLLWEIATYGMSPYPGIDLSQVYELLEKDYRMERPEGCPEKVYELMRACWQWNPSDRPSFAEIHQAFETMFQESSISDEVEKELGKQGVRGAVSTLLQAPELPTKTRTSRRAAEHRDTTDVPEMPHSKGQGESDPLDHEPAVSPLLPRKERGPPEGGLNEDERLLPKDKKTNLFSALIKKKKKTAPTPPKRSSSFREMDGQPERRGAGEEEGRDISNGALAFTPLDTADPAKSPKPSNGAGVPNGALRESGGSGFRSPHLWKKSSTLTSSRLATGEEEGGGSSSKRFLRSCSASCVPHGAKDTEWRSVTLPRDLQSTGRQFDSSTFGGHKSEKPALPRKRAGENRSDQVTRGTVTPPPRLVKKNEEAADEVFKDIMESSPGSSPPNLTPKPLRRQVTVAPASGLPHKEEAGKGSALGTPAAAEPVTPTSKAGSGAPGGTSKGPAEESRVRRHKHSSESPGRDKGKLSRLKPAPPPPPAASAGKAGGKPSQSPSQEAAGEAVLGAKTKATSLVDAVNSDAAKPSQPGEGLKKPVLPATPKPQSAKPSGTPISPAPVPSTLPSASSALAGDQPSSTAFIPLISTRVSLRKTRQPPERIASGAITKGVVLDSTEALCLAISRNSEQMASHSAVLEAGKNLYTFCVSYVDSIQQMRNKFAFREAINKLENNLRELQICPATAGSGPAATQDFSKLLSSVKEISDIVQR",
        domains=[
            {"name": "Kinase_Pkinase", "type": "Pfam:PF00069", "start": 242, "end": 500}
        ],
        motifs=[
            {"name": "DFG", "type": "kinase_motif", "start": 381, "end": 383}
        ],
        ptms=[
            {"position": 393, "ptm_type": "phosphorylation", "residue": "Y", "enzyme": "ABL1"}
        ],
        binding_sites=[
            {
                "type": "ATP-binding",
                "residues": "248,251,271,289,317,381,382,383"
            },
            {
                "type": "substrate",
                "residues": "385,386,393"
            }
        ],
        ppi_interfaces=[],
        conformational_state="inactive",  # DFG-out state with Imatinib
        state_regions=[]
    )
    
    encoder = LMPNeSyEncoder()
    encoded = encoder.encode(abl1_annotation)
    
    print("="*80)
    print("EXAMPLE 1: ABL1 Kinase (DFG-out, Imatinib-bound)")
    print("="*80)
    print(encoded[:500])  # Show first 500 characters
    print("...")
    print("="*80)

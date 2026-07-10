"""
PAS (Protein Annotation Specificity) Annotators Module
======================================================

This module implements "Expert Lenses" for the LMP system.
Instead of relying solely on generic UniProt annotations, these classes
apply domain-specific biological rules to enrich the data.

Philosophy:
- 100% Sequence-based (no PDB required)
- Rule-based enrichment (regex, relative positions, keyword heuristics)
- Enhances "Data" into "Knowledge"

Supported Families:
- Kinases (Implemented)
- GPCRs (Planned)
- Nuclear Receptors (Planned)
- Proteases (Planned)
"""

from typing import Dict, List, Any, Optional
import re
import logging

logger = logging.getLogger(__name__)

class PASAnnotator:
    """Base class for all PAS Annotators"""
    
    def annotate(self, domain: Dict[str, Any], sequence: str, ptms: List[Dict], uniprot_data: Dict) -> Dict[str, Any]:
        """
        Enrich a domain with specific biological context.
        
        Args:
            domain: The generic domain dict (type, start, end)
            sequence: Full protein sequence
            ptms: List of extracted PTMs
            uniprot_data: Raw UniProt data for cross-referencing features
            
        Returns:
            Enriched domain dictionary
        """
        raise NotImplementedError("Subclasses must implement annotate()")

    def _get_domain_sequence(self, domain: Dict, full_sequence: str) -> str:
        """Helper to extract domain subsequence (1-based indexing in domain)"""
        start = domain.get("start", 1) - 1
        end = domain.get("end", len(full_sequence))
        return full_sequence[start:end]

    def _get_relative_position(self, abs_position: int, domain: Dict) -> int:
        """Convert absolute sequence position to domain-relative position"""
        return abs_position - (domain.get("start", 1) - 1)


class KinasePASAnnotator(PASAnnotator):
    """
    Expert Lens for Protein Kinases.
    
    Detects:
    - DFG Motifs (Asp-Phe-Gly)
    - Activation Loops (T-loop)
    - Catalytic Residues (Active sites)
    - Regulatory PTM context
    """
    
    def annotate(self, domain: Dict[str, Any], sequence: str, ptms: List[Dict], uniprot_data: Dict) -> Dict[str, Any]:
        # 1. Extract domain context
        domain_seq = self._get_domain_sequence(domain, sequence)
        domain_start = domain.get("start", 1)
        
        # 2. Detect Structural Motifs (Text-based)
        dfg_info = self._find_dfg_motif(domain_seq, domain_start)
        act_loop_info = self._find_activation_loop(domain_seq, dfg_info, domain_start)
        
        # 3. Detect Functional Sites (UniProt-based)
        catalytic_info = self._find_catalytic_residues(uniprot_data, domain_start, domain.get("end"))
        
        # 4. Enrich PTMs with Kinase Context
        enriched_ptms = self._enrich_ptms(ptms, act_loop_info, domain_start)
        
        # 5. Construct PAS Data
        pas_data = {
            "family": "Kinase",
            "motifs": {},
            "regions": {},
            "sites": {}
        }
        
        if dfg_info:
            pas_data["motifs"]["DFG"] = dfg_info
            
        if act_loop_info:
            pas_data["regions"]["ActivationLoop"] = act_loop_info
            
        if catalytic_info:
            pas_data["sites"]["Catalytic"] = catalytic_info
            
        if enriched_ptms:
            pas_data["regulatory_ptms"] = enriched_ptms

        # Return enriched domain
        return {
            **domain,
            "pas_annotations": pas_data
        }

    def _find_dfg_motif(self, domain_seq: str, domain_start: int) -> Optional[Dict]:
        """Find DFG motif (Asp-Phe-Gly) - critical for catalysis"""
        # Simple regex for DFG
        match = re.search(r"DFG", domain_seq)
        if match:
            start_rel = match.start()
            return {
                "sequence": "DFG",
                "start_absolute": domain_start + start_rel,
                "end_absolute": domain_start + start_rel + 2,
                "state_inference": "DFG-in (competent)" # Default assumption without structure
            }
        return None

    def _find_activation_loop(self, domain_seq: str, dfg_info: Optional[Dict], domain_start: int) -> Optional[Dict]:
        """
        Heuristic detection of Activation Loop.
        Rule: Usually starts at DFG and extends ~20-30 residues.
        """
        if not dfg_info:
            return None
            
        # Heuristic: Activation loop starts at DFG and is ~20-30 residues long
        dfg_start_abs = dfg_info["start_absolute"]
        loop_start = dfg_start_abs
        loop_end = loop_start + 25 # Approximation
        
        # Refinement: Look for APE motif (Ala-Pro-Glu) which often ends the loop
        # Search in the window [DFG+10 : DFG+40]
        dfg_rel = dfg_info["start_absolute"] - domain_start
        search_window = domain_seq[dfg_rel+10 : dfg_rel+40]
        ape_match = re.search(r"APE", search_window)
        
        if ape_match:
            # If APE found, loop ends there
            loop_end = domain_start + (dfg_rel + 10 + ape_match.start()) + 2
            
        return {
            "start": loop_start,
            "end": loop_end,
            "description": "Inferred from DFG motif position"
        }

    def _find_catalytic_residues(self, uniprot_data: Dict, dom_start: int, dom_end: int) -> List[Dict]:
        """Extract 'Active site' features from UniProt that fall in this domain"""
        sites = []
        for feature in uniprot_data.get("features", []):
            if feature.get("type") == "Active site":
                location = feature.get("location", {})
                # Handle different location formats safely
                try:
                    pos = location.get("start", {}).get("value")
                    if pos and dom_start <= pos <= dom_end:
                        sites.append({
                            "position": pos,
                            "description": feature.get("description", "Active site"),
                            "role": "Catalytic"
                        })
                except (AttributeError, TypeError):
                    continue
        return sites

    def _enrich_ptms(self, ptms: List[Dict], act_loop: Optional[Dict], domain_start: int) -> List[Dict]:
        """Tag PTMs as 'activating' if they are in the activation loop or described as such"""
        enriched = []
        
        for ptm in ptms:
            pos = ptm.get("position")
            if not pos:
                continue
                
            is_relevant = False
            context = []
            
            # Rule 1: Inside Activation Loop?
            if act_loop and (act_loop["start"] <= pos <= act_loop["end"]):
                is_relevant = True
                context.append("In Activation Loop")
                ptm["regulatory_role"] = "likely_activating"
                
            # Rule 2: Description keywords
            desc = ptm.get("description", "").lower()
            if "activat" in desc or "auto-activat" in desc:
                is_relevant = True
                context.append("Annotated as Activating")
                ptm["regulatory_role"] = "activating"
            elif "inhibit" in desc:
                is_relevant = True
                context.append("Annotated as Inhibitory")
                ptm["regulatory_role"] = "inhibitory"
                
            if is_relevant:
                # Create a copy to avoid mutating original list in unexpected ways if shared
                enriched_ptm = ptm.copy()
                enriched_ptm["pas_context"] = ", ".join(context)
                enriched.append(enriched_ptm)
                
        return enriched

class GPCRPASAnnotator(PASAnnotator):
    """
    Expert Lens for G Protein-Coupled Receptors (GPCRs).
    
    Detects:
    - Transmembrane helices (TM1-TM7) from UniProt topology features
    - Conserved motifs: D/ERY (TM3-ICL2), NPxxY (TM7)
    - GPCR class classification (A/B/C)
    - Regulatory PTMs: palmitoylation (C-tail), phosphorylation (ICL3/C-tail)
    """
    
    def annotate(self, domain: Dict[str, Any], sequence: str, ptms: List[Dict], uniprot_data: Dict) -> Dict[str, Any]:
        domain_seq = self._get_domain_sequence(domain, sequence)
        domain_start = domain.get("start", 1)
        
        tm_regions = self._find_tm_regions(uniprot_data, domain_start, domain.get("end", len(sequence)))
        dry_motif = self._find_dry_motif(domain_seq, domain_start)
        npxxy_motif = self._find_npm_motif(domain_seq, domain_start)
        gpcr_class = self._classify_gpcr_class(domain_seq, uniprot_data)
        enriched_ptms = self._enrich_ptms(ptms, tm_regions, domain_start, domain.get("end", len(sequence)))
        
        pas_data = {
            "family": "GPCR",
            "classification": gpcr_class,
            "motifs": {},
            "regions": {},
            "sites": {},
        }
        
        if dry_motif:
            pas_data["motifs"]["DRY"] = dry_motif
        if npxxy_motif:
            pas_data["motifs"]["NPxxY"] = npxxy_motif
        if tm_regions:
            pas_data["regions"]["TransmembraneHelices"] = tm_regions
        if enriched_ptms:
            pas_data["regulatory_ptms"] = enriched_ptms
        
        return {**domain, "pas_annotations": pas_data}
    
    def _find_tm_regions(self, uniprot_data: Dict, dom_start: int, dom_end: int) -> List[Dict]:
        """Extract transmembrane regions from UniProt topology features."""
        tm_regions = []
        for feature in uniprot_data.get("features", []):
            if feature.get("type") in ("Transmembrane", "Topological domain"):
                desc = (feature.get("description") or "").lower()
                if feature.get("type") == "Transmembrane" or "helical" in desc:
                    location = feature.get("location", {})
                    try:
                        start = location.get("start", {}).get("value")
                        end = location.get("end", {}).get("value")
                        if start and end and dom_start <= start <= dom_end:
                            tm_regions.append({
                                "start": start,
                                "end": end,
                                "length": end - start + 1,
                                "description": feature.get("description", "Transmembrane helix"),
                            })
                    except (AttributeError, TypeError):
                        continue
        # Sort and label TM1-TM7
        tm_regions.sort(key=lambda x: x["start"])
        for i, tm in enumerate(tm_regions[:7], 1):
            tm["label"] = f"TM{i}"
        return tm_regions
    
    def _find_dry_motif(self, domain_seq: str, domain_start: int) -> Optional[Dict]:
        """Find D/ERY motif (conserved at TM3/ICL2 junction). Variant: D/E-R-Y/W."""
        match = re.search(r"[DE]R[YW]", domain_seq)
        if match:
            return {
                "sequence": match.group(),
                "start_absolute": domain_start + match.start(),
                "end_absolute": domain_start + match.end() - 1,
                "role": "G-protein coupling (ionic lock)",
            }
        return None
    
    def _find_npm_motif(self, domain_seq: str, domain_start: int) -> Optional[Dict]:
        """Find NPxxY motif (conserved in TM7). N-P-x-x-Y pattern."""
        match = re.search(r"NP..Y", domain_seq)
        if match:
            return {
                "sequence": match.group(),
                "start_absolute": domain_start + match.start(),
                "end_absolute": domain_start + match.end() - 1,
                "role": "Receptor activation / internalization",
            }
        return None
    
    def _classify_gpcr_class(self, domain_seq: str, uniprot_data: Dict) -> Dict[str, str]:
        """Classify GPCR class from keywords and sequence features."""
        keywords = " ".join(
            kw.get("value", "") if isinstance(kw, dict) else str(kw)
            for kw in uniprot_data.get("keywords", [])
        ).lower()
        
        protein_name = (uniprot_data.get("proteinDescription", {})
                       .get("recommendedName", {})
                       .get("fullName", {})
                       .get("value", "")).lower()
        
        if "rhodopsin" in keywords or "rhodopsin" in protein_name:
            return {"class": "A", "basis": "Rhodopsin-like (keyword match)"}
        if "secretin" in keywords or "secretin" in protein_name:
            return {"class": "B", "basis": "Secretin-like (keyword match)"}
        if "metabotropic glutamate" in keywords or "metabotropic" in protein_name:
            return {"class": "C", "basis": "Metabotropic glutamate-like (keyword match)"}
        
        # Heuristic: Class A is most common (~80% of GPCRs)
        if re.search(r"[DE]R[YW]", domain_seq) and re.search(r"NP..Y", domain_seq):
            return {"class": "A", "basis": "DRY+NPxxY motifs detected (heuristic)"}
        
        return {"class": "unknown", "basis": "Insufficient data for classification"}
    
    def _enrich_ptms(self, ptms: List[Dict], tm_regions: List[Dict], dom_start: int, dom_end: int) -> List[Dict]:
        """Tag PTMs relevant to GPCR signaling."""
        enriched = []
        # Estimate C-tail start as after last TM
        c_tail_start = tm_regions[-1]["end"] + 1 if tm_regions else dom_end - 50
        
        for ptm in ptms:
            pos = ptm.get("position")
            if not pos:
                continue
            
            ptm_type = (ptm.get("type") or "").lower()
            context = []
            
            # Palmitoylation in C-tail = membrane anchoring
            if "palmitoyl" in ptm_type and pos >= c_tail_start:
                context.append("C-tail palmitoylation (membrane anchor)")
                ptm_copy = ptm.copy()
                ptm_copy["regulatory_role"] = "membrane_anchoring"
                ptm_copy["pas_context"] = ", ".join(context)
                enriched.append(ptm_copy)
                
            # Phosphorylation in ICL3 or C-tail = desensitization/arrestin recruitment
            elif "phospho" in ptm_type and pos >= c_tail_start:
                context.append("C-tail phosphorylation (arrestin recruitment / desensitization)")
                ptm_copy = ptm.copy()
                ptm_copy["regulatory_role"] = "desensitization"
                ptm_copy["pas_context"] = ", ".join(context)
                enriched.append(ptm_copy)
                
            # Glycosylation in N-terminus (before TM1)
            elif "glyco" in ptm_type:
                if tm_regions and pos < tm_regions[0].get("start", dom_start):
                    context.append("N-terminal glycosylation (ligand recognition)")
                    ptm_copy = ptm.copy()
                    ptm_copy["regulatory_role"] = "ligand_recognition"
                    ptm_copy["pas_context"] = ", ".join(context)
                    enriched.append(ptm_copy)
        
        return enriched


# Registry of available annotators
PAS_REGISTRY = {
    "Kinase": KinasePASAnnotator(),
    "GPCR": GPCRPASAnnotator(),
}

def get_pas_annotator(domain_type: str) -> Optional[PASAnnotator]:
    """Factory method to get the correct annotator for a domain type"""
    d_type = domain_type.lower()
    
    if "kinase" in d_type:
        return PAS_REGISTRY["Kinase"]
    
    if any(kw in d_type for kw in ("gpcr", "g-protein", "7tm", "rhodopsin", "serpentine", "7_transmembrane")):
        return PAS_REGISTRY["GPCR"]
    
    return None

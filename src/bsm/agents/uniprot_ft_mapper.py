"""
UniProt Feature Table → NeSy Mapper

Maps UniProt Feature Table annotations to LMP v2.0 NeSy markers.
Supports all major feature types: BINDING, DOMAIN, MOD_RES, TRANSMEM, etc.

Uses canonical ontology from nesy_constants.py for consistent marker generation.

Author: Dr. Yuan Chen
Date: November 3, 2025
"""

from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass
import logging
import re

# Import canonical ontology
try:
    from bsm.lmp.nesy_constants import (
        CANONICAL_PTMS,
        CANONICAL_BINDING_SITES,
        CANONICAL_REGULATORY_SITES,
        CANONICAL_LIGAND_MARKERS,
        PFAM_TO_NESY_DOMAIN,
        PROSITE_TO_NESY_DOMAIN,
    )
    ONTOLOGY_LOADED = True
except ImportError:
    ONTOLOGY_LOADED = False
    CANONICAL_PTMS = {}
    CANONICAL_BINDING_SITES = {}
    CANONICAL_REGULATORY_SITES = {}
    CANONICAL_LIGAND_MARKERS = {}
    PFAM_TO_NESY_DOMAIN = {}
    PROSITE_TO_NESY_DOMAIN = {}

logger = logging.getLogger(__name__)


@dataclass
class NeSyMarker:
    """Representa un marcador NeSy con metadata"""
    marker_type: str          # Ej: 'ATP', 'DOM:Kinase', 'S-P'
    start_pos: int
    end_pos: int
    source: str               # 'uniprot_ft', 'string', etc.
    confidence: float         # 0.0-1.0
    evidence: List[str] = None
    parameters: Optional[str] = None  # Para marcadores parametrizados
    
    def __post_init__(self):
        if self.evidence is None:
            self.evidence = []
    
    def to_nesy_string(self) -> str:
        """Convierte a string NeSy"""
        if self.marker_type.startswith('DOM:'):
            return f"[{self.marker_type}]"
        elif self.marker_type.startswith('ION:'):
            return f"({self.marker_type})"
        elif self.marker_type.startswith('DNA:'):
            return f"({self.marker_type})"
        elif '-' in self.marker_type and len(self.marker_type) <= 5:
            # PTM marker
            return f"{{{self.marker_type}}}"
        else:
            # Binding site
            return f"({self.marker_type})"


class UniProtFTMapper:
    """Mapea UniProt Feature Table a marcadores NeSy LMP v2.0"""
    
    def __init__(self):
        if not ONTOLOGY_LOADED:
            logger.warning("Canonical ontology not loaded - using fallback heuristics")
        
        # Mapeo de tipos de features a funciones de procesamiento
        self.feature_mapping = {
            'BINDING': self._map_binding_site,
            'Binding site': self._map_binding_site,  # REST API format
            'NP_BIND': self._map_nucleotide_binding,
            'Nucleotide binding': self._map_nucleotide_binding,  # REST API format
            'CA_BIND': lambda ft: [('ION:Ca2+', self._get_start(ft), self._get_end(ft), {})],
            'Calcium binding': lambda ft: [('ION:Ca2+', self._get_start(ft), self._get_end(ft), {})],
            'DNA_BIND': self._map_dna_binding,
            'DNA binding': self._map_dna_binding,  # REST API format
            'ZN_FING': lambda ft: [('ION:Zn', self._get_start(ft), self._get_end(ft), {})],
            'Zinc finger': lambda ft: [('ION:Zn', self._get_start(ft), self._get_end(ft), {})],
            'METAL': self._map_metal_binding,
            'Metal binding': self._map_metal_binding,  # REST API format
            
            # Domains
            'DOMAIN': self._map_domain,
            'Domain': self._map_domain,  # REST API format
            'REPEAT': self._map_repeat,
            'Repeat': self._map_repeat,  # REST API format
            'COILED': lambda ft: [('COIL', self._get_start(ft), self._get_end(ft), {})],
            'Coiled coil': lambda ft: [('COIL', self._get_start(ft), self._get_end(ft), {})],
            'REGION': self._map_region,
            'Region': self._map_region,  # REST API format
            
            # PTMs
            'MOD_RES': self._map_modification,
            'Modified residue': self._map_modification,  # REST API format
            'CARBOHYD': lambda ft: [('N-Glyc', self._get_start(ft), self._get_start(ft), {})],
            'Glycosylation': lambda ft: [('N-Glyc', self._get_start(ft), self._get_start(ft), {})],
            'LIPID': lambda ft: [('LIP', self._get_start(ft), self._get_end(ft), {})],
            'Lipidation': lambda ft: [('LIP', self._get_start(ft), self._get_end(ft), {})],
            'CROSSLNK': self._map_crosslink,
            'Cross-link': self._map_crosslink,  # REST API format
            'DISULFID': self._map_disulfide,
            'Disulfide bond': self._map_disulfide,  # REST API format
            
            # Regulatory
            'ACT_SITE': lambda ft: [('CAT', self._get_start(ft), self._get_end(ft), {})],
            'Active site': lambda ft: [('CAT', self._get_start(ft), self._get_end(ft), {})],  # REST API format
            'SITE': self._map_site,
            'Site': self._map_site,  # REST API format
        }
    
    @staticmethod
    def _get_start(feature: Dict) -> int:
        """Extract start position from either FlatFile or REST API format"""
        if 'begin' in feature:
            return feature['begin']
        elif 'location' in feature and 'start' in feature['location']:
            return feature['location']['start'].get('value', 0)
        return 0
    
    @staticmethod
    def _get_end(feature: Dict) -> int:
        """Extract end position from either FlatFile or REST API format"""
        if 'end' in feature:
            return feature['end']
        elif 'location' in feature and 'end' in feature['location']:
            return feature['location']['end'].get('value', 0)
        return 0
    
    @staticmethod
    def _get_description(feature: Dict) -> str:
        """Extract description from either FlatFile or REST API format"""
        return feature.get('description', '')
    
    def map_features_to_nesy(self, features: List[Dict]) -> List[NeSyMarker]:
        """
        Convierte lista de features UniProt a marcadores NeSy
        
        Args:
            features: Lista de features de UniProt (formato dict)
            
        Returns:
            Lista de NeSyMarker objects
        """
        nesy_markers = []
        
        for ft in features:
            ft_type = ft.get('type')
            
            if ft_type in self.feature_mapping:
                try:
                    mapper_func = self.feature_mapping[ft_type]
                    results = mapper_func(ft)
                    
                    for marker_type, start, end, params in results:
                        nesy_markers.append(NeSyMarker(
                            marker_type=marker_type,
                            start_pos=start,
                            end_pos=end,
                            source='uniprot_ft',
                            confidence=0.9,  # Alta confianza para datos curados UniProt
                            evidence=ft.get('evidences', []),
                            parameters=params.get('param') if params else None
                        ))
                
                except Exception as e:
                    logger.warning(f"Failed to map feature {ft_type}: {e}")
                    continue
        
        return nesy_markers
    
    def _map_binding_site(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea sitios de unión general - revisa description Y ligand"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        # NEW: Check ligand field (REST API has structured ligand info!)
        ligand_info = ft.get('ligand', {})
        ligand_name = ligand_info.get('name', '').lower() if ligand_info else ''
        
        # Combine description and ligand name for matching
        combined = description + ' ' + ligand_name
        
        # Mapeo basado en descripción o ligand name
        if 'atp' in combined:
            return [('ATP', start, end, {})]
        elif 'gtp' in combined:
            return [('GTP', start, end, {})]
        elif 'adp' in combined or 'adenosine diphosphate' in combined:
            return [('NTP', start, end, {})]  # ADP is nucleotide
        elif 'gdp' in combined:
            return [('GTP', start, end, {})]  # GTP/GDP binding
        elif 'nucleotide' in combined or 'ntp' in combined:
            return [('NTP', start, end, {})]  # generic nucleotide
        elif 'nad' in combined or 'nicotinamide' in combined:
            return [('NAD', start, end, {})]  # NEW: NAD/NADH/NADP
        elif 'fad' in combined:
            return [('NAD', start, end, {})]  # FAD also in NAD family
        elif 'glyceraldehyde' in combined or 'bisphosphoglycerate' in combined or '2,3-bpg' in combined:
            return [('SUB', start, end, {})]  # NEW: metabolic substrates
        elif 'bicarbonate' in combined or 'hydrogencarbonate' in combined or 'carbonate' in combined:
            return [('HCO3', start, end, {})]  # NEW: bicarbonate
        elif 'bilirubin' in combined:
            return [('SUB', start, end, {})]  # Bilirubin as substrate
        elif 'ergotamine' in combined or 'agonist' in combined:
            return [('DRUG', start, end, {})]  # NEW: agonists/drugs
        elif 'cofactor' in combined or 'coenzyme' in combined:
            return [('COF', start, end, {})]  # generic cofactor
        elif 'heme' in combined or 'haem' in combined:
            return [('HEME', start, end, {})]  # NEW: heme group
        elif 'o2' in combined or 'oxygen' in combined:
            return [('OXY', start, end, {})]  # NEW: oxygen binding
        elif 'inositol' in combined:
            # Inositol phosphates (signaling molecules)
            if 'hexakis' in combined or 'ip6' in combined:
                return [('IP6', start, end, {})]  # NEW: IP6
            elif 'tetrakis' in combined or 'ip4' in combined:
                return [('IP4', start, end, {})]  # NEW: IP4
            else:
                return [('INO', start, end, {})]  # NEW: generic inositol
        elif 'calcium' in combined or 'ca(2+)' in combined or 'ca2+' in combined:
            return [('ION:Ca2+', start, end, {})]
        elif 'zinc' in combined or 'zn(2+)' in combined or 'zn2+' in combined or 'zn' in ligand_name:
            return [('ION:Zn', start, end, {})]
        elif 'magnesium' in combined or 'mg(2+)' in combined or 'mg2+' in combined:
            return [('ION:Mg2+', start, end, {})]
        elif 'copper' in combined or 'cu cation' in combined or 'cu(2+)' in combined:
            return [('ION:Cu', start, end, {})]  # NEW: copper
        elif 'iron' in combined or 'fe' in combined:
            return [('ION:Fe', start, end, {})]
        elif 'lipid' in combined or 'phospholipid' in combined or 'membrane' in combined:
            return [('LIP', start, end, {})]  # lipid binding
        elif 'dna' in combined:
            # Detectar tipo de unión DNA
            if 'major groove' in combined:
                return [('DNA:Major', start, end, {})]
            elif 'minor groove' in combined:
                return [('DNA:Minor', start, end, {})]
            else:
                return [('DNA', start, end, {})]
        elif 'rna' in combined:
            return [('RNA', start, end, {})]
        elif 'substrate' in combined:
            return [('SUB', start, end, {})]
        elif 'carazolol' in combined or 'timolol' in combined or 'drug' in combined or 'inhibitor' in combined:
            return [('DRUG', start, end, {})]  # NEW: drug/inhibitor binding
        else:
            # Binding site desconocido
            return [('BIND', start, end, {})]
    
    def _map_nucleotide_binding(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea sitios de unión a nucleótidos"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        if 'atp' in description:
            return [('ATP', start, end, {})]
        elif 'gtp' in description:
            return [('GTP', start, end, {})]
        else:
            return [('NTP', start, end, {})]  # Genérico
    
    def _map_dna_binding(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea regiones de unión a DNA"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        if 'major groove' in description:
            return [('DNA:Major', start, end, {})]
        elif 'minor groove' in description:
            return [('DNA:Minor', start, end, {})]
        else:
            return [('DNA', start, end, {})]
    
    def _map_metal_binding(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea sitios de unión a metales"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        # Detectar tipo de metal
        if 'zinc' in description or 'zn' in description:
            return [('ION:Zn', start, end, {})]
        elif 'iron' in description or 'fe' in description:
            return [('ION:Fe', start, end, {})]
        elif 'calcium' in description or 'ca' in description:
            return [('ION:Ca2+', start, end, {})]
        elif 'magnesium' in description or 'mg' in description:
            return [('ION:Mg2+', start, end, {})]
        elif 'copper' in description or 'cu' in description:
            return [('ION:Cu', start, end, {})]
        else:
            return [('ION', start, end, {})]  # Metal genérico
    
    def _map_modification(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea modificaciones post-traduccionales usando ontología canónica"""
        description = self._get_description(ft).lower()
        pos = self._get_start(ft)
        
        # Use canonical ontology if loaded
        if ONTOLOGY_LOADED and CANONICAL_PTMS:
            for ptm_name, ptm_type in CANONICAL_PTMS.items():
                # Check if any keyword matches (using substring match for PTMs since they're specific)
                # For PTMs, substring matching is OK because they're specific chemical terms
                if any(kw.lower() in description for kw in ptm_type.uniprot_keywords):
                    # Extract enzyme if pattern provided
                    enzyme = None
                    if ptm_type.enzyme_pattern:
                        match = re.search(ptm_type.enzyme_pattern, description, re.IGNORECASE)
                        if match:
                            # Get first non-None group
                            enzyme = next((g for g in match.groups() if g), None)
                    
                    # Return canonical marker
                    marker = ptm_type.nesy_prefix
                    if enzyme:
                        marker = f"{marker}:{enzyme}"
                    
                    return [(marker, pos, pos, {'enzyme': enzyme} if enzyme else {})]
        
        # Fallback heuristics (legacy)
        if 'phospho' in description:
            # Detectar residuo
            if 'serine' in description or description.startswith('phosphoserine'):
                return [('S-P', pos, pos, {})]
            elif 'threonine' in description or description.startswith('phosphothreonine'):
                return [('T-P', pos, pos, {})]
            elif 'tyrosine' in description or description.startswith('phosphotyrosine'):
                return [('Y-P', pos, pos, {})]
            else:
                return [('P', pos, pos, {})]  # Fosforilación genérica
        
        elif 'acetyl' in description:
            return [('K-Ac', pos, pos, {})]
        
        elif 'methyl' in description:
            if 'dimethyl' in description:
                return [('K-Me2', pos, pos, {})]
            elif 'trimethyl' in description:
                return [('K-Me3', pos, pos, {})]
            else:
                return [('K-Me1', pos, pos, {})]
        
        elif 'ubiquitin' in description:
            return [('K-Ub', pos, pos, {})]
        
        elif 'sumoyl' in description:
            return [('K-SUMO', pos, pos, {})]
        
        elif 'palmitoyl' in description:
            return [('C-Pal', pos, pos, {})]
        
        elif 'n-glycosyl' in description or 'glycosyl' in description:
            return [('N-Glyc', pos, pos, {})]
        
        else:
            return [('MOD', pos, pos, {})]  # Modificación genérica
    
    def _map_domain(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea dominios estructurales/funcionales"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        # Detectar tipo de dominio
        if 'kinase' in description:
            return [('DOM:Kinase', start, end, {})]
        elif 'sh2' in description:
            return [('DOM:SH2', start, end, {})]
        elif 'sh3' in description:
            return [('DOM:SH3', start, end, {})]
        elif 'pdz' in description:
            return [('DOM:PDZ', start, end, {})]
        elif 'ph' in description and 'pleckstrin' in description:
            return [('DOM:PH', start, end, {})]
        elif 'immunoglobulin' in description or 'ig-like' in description:
            return [('DOM:Ig', start, end, {})]
        elif 'egf-like' in description:
            return [('DOM:EGF', start, end, {})]
        elif 'ankyrin' in description:
            return [('DOM:ANK', start, end, {})]
        elif 'wd' in description and 'repeat' in description:
            return [('DOM:WD40', start, end, {})]
        else:
            # Dominio genérico - usar nombre si es corto
            domain_name = ft.get('description', 'Unknown')[:20]
            domain_name = re.sub(r'[^a-zA-Z0-9_]', '', domain_name)
            return [('DOM:' + domain_name, start, end, {})]
    
    def _map_repeat(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea regiones repetidas"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        # Usar MOT para repeats
        repeat_name = ft.get('description', 'Repeat')[:20]
        repeat_name = re.sub(r'[^a-zA-Z0-9_]', '', repeat_name)
        return [('MOT:' + repeat_name, start, end, {})]
    
    def _map_region(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea regiones de interés"""
        description = self._get_description(ft).lower()
        start, end = self._get_start(ft), self._get_end(ft)
        
        # Regiones especiales
        if 'catalytic' in description:
            return [('CAT', start, end, {})]
        elif 'substrate' in description:
            return [('SUB', start, end, {})]
        elif 'dimerization' in description or 'interaction' in description:
            return [('PPI', start, end, {})]
        elif 'coiled' in description or 'coil' in description:
            return [('COIL', start, end, {})]  # Coiled-coil structural region
        else:
            return [('REG', start, end, {})]  # Región genérica
    
    def _map_site(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea sitios de interés usando ontología canónica"""
        description = self._get_description(ft).lower()
        pos = self._get_start(ft)
        
        # SKIP: "Not glycated" sites - these are negative annotations
        if 'not glycated' in description:
            return []  # Don't create a marker for negative evidence
        
        # SKIP: Aspirin-acetylated - too specific, not biologically relevant
        if 'aspirin' in description:
            return []
        
        # Use canonical ontology if loaded
        if ONTOLOGY_LOADED and CANONICAL_BINDING_SITES:
            for site_name, site_type in CANONICAL_BINDING_SITES.items():
                # Check if any keyword matches (with word boundaries)
                for keyword in site_type.uniprot_keywords:
                    # Use word boundary matching to avoid false positives like 'ion' in 'translocation'
                    pattern = r'\b' + re.escape(keyword) + r'\b'
                    if re.search(pattern, description, re.IGNORECASE):
                        # Extract parameter if pattern provided
                        param = None
                        if site_type.parameter_pattern:
                            match = re.search(site_type.parameter_pattern, description, re.IGNORECASE)
                            if match:
                                param = next((g for g in match.groups() if g), None)
                        
                        # Return canonical marker
                        marker = site_type.nesy_marker
                        if param and '{}' in marker:
                            marker = marker.replace('{}', param)
                        
                        return [(marker, pos, pos, {'param': param} if param else {})]
        
        # Fallback for legacy patterns
        if 'cleavage' in description:
            return [('CLEAVE', pos, pos, {})]
        elif 'n-glycosyl' in description or 'n-linked' in description:
            return [('N-Glyc', pos, pos, {})]
        elif 'o-glycosyl' in description or 'o-linked' in description:
            return [('O-Glyc', pos, pos, {})]
        elif 'glycosylation' in description:
            return [('Glyc', pos, pos, {})]  # Generic glycosylation
        else:
            return [('SITE', pos, pos, {})]
    
    def _map_crosslink(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea crosslinks usando ontología canónica"""
        description = self._get_description(ft).lower()
        pos = self._get_start(ft)
        
        # Use canonical ontology if loaded
        if ONTOLOGY_LOADED and CANONICAL_PTMS:
            for ptm_name, ptm_type in CANONICAL_PTMS.items():
                # Only check PTMs that are crosslinks
                if 'crosslink' in ptm_name or 'ubiquitin' in ptm_name or 'sumo' in ptm_name:
                    # Check if any keyword matches (substring match for PTMs)
                    if any(kw.lower() in description for kw in ptm_type.uniprot_keywords):
                        marker = ptm_type.nesy_prefix
                        return [(marker, pos, pos, {})]
        
        # Fallback for legacy patterns
        if 'glycyl lysine' in description:
            return [('K-Gly', pos, pos, {})]
        else:
            return [('XLINK', pos, pos, {})]
    
    def _map_disulfide(self, ft: Dict) -> List[Tuple[str, int, int, Dict]]:
        """Mapea puentes disulfuro - formato canónico C-S-S-C"""
        start = self._get_start(ft)
        end = self._get_end(ft)
        
        # Use canonical marker C-S-S-C
        return [('C-S-S-C', start, start, {'partner': end})]

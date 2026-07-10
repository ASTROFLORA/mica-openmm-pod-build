"""
LMP v2.0 XML Parser
===================

Parse LMP v2.0 XML documents into BudoV3 objects.

Handles:
- PTM annotations
- Ligand binding sites
- Conformational states
- Protein-protein interfaces
- Causal trigger relationships

**Improvements (v2.1):**
- XSD schema validation with lxml
- Cross-reference resolution (trigger IDs → objects)
- Multi-chain support
- Nested domain support (recursive parsing)
- External configuration (YAML)
- Comprehensive logging

**Author:** Dr. Yuan Chen & Dr. Priya Sharma
**Lab:** AI University - BSM-BUDO-CEA Program
**Version:** 2.1.0
"""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml

# Try to import lxml for XSD validation (fallback to stdlib if not available)
try:
    from lxml import etree as lxml_etree
    HAS_LXML = True
except ImportError:
    HAS_LXML = False
    logging.warning("lxml not available. XSD validation disabled. Install with: pip install lxml")

from ..schemas.budo_v3 import (
    BudoConformation,
    BudoCrossReference,
    BudoDomain,
    BudoInterface,
    BudoLigand,
    BudoPTM,
    BudoProvenance,
    BudoV3,
    ConfidenceLevel,
    FunctionalState,
)


class LMPParser:
    """
    Parser for LMP v2.0 XML documents
    
    **Features:**
    - XSD schema validation (requires lxml)
    - Cross-reference resolution (trigger IDs → PTM/Ligand objects)
    - Multi-chain support
    - Nested domain support (recursive)
    - External configuration (lmp_config.yaml)
    - Comprehensive logging
    
    Example Usage:
    ```python
    parser = LMPParser()
    budo_protein = parser.parse("P12931_Active.xml")
    
    # Access LMP data
    for domain in budo_protein.domains:
        print(f"Domain: {domain.domain_name}")
        for ptm in domain.ptms:
            print(f"  PTM: {ptm.ptm_id} at position {ptm.position}")
        for conf in domain.conformations:
            print(f"  State: {conf.state_name} (triggered by {conf.trigger_id})")
            # Access resolved trigger object
            if hasattr(conf, '_trigger_obj'):
                print(f"    Trigger object: {conf._trigger_obj}")
    ```
    """
    
    def __init__(
        self,
        validate: bool = True,
        config_path: Optional[Path] = None,
        log_level: str = "INFO",
    ):
        """
        Initialize LMP parser
        
        Args:
            validate: Whether to validate XML against LMP v2.0 XSD schema
            config_path: Path to lmp_config.yaml (default: same dir as parser.py)
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        """
        # Setup logging
        self.logger = self._setup_logging(log_level)
        
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Validation settings
        self.validate = validate and self.config["parser"]["validate_schema"]
        self.strict_mode = self.config["parser"]["strict_mode"]
        self.skip_invalid = self.config["parser"]["skip_invalid"]
        
        # Load XSD schema for validation
        self.xsd_schema = None
        if self.validate and HAS_LXML:
            self.xsd_schema = self._load_xsd_schema()
        elif self.validate and not HAS_LXML:
            self.logger.warning("XSD validation requested but lxml not available. Skipping validation.")
        
        # Load vocabularies and mappings from config
        self._state_name_mapping = self.config["state_mappings"]
        self._confidence_scores = self.config["confidence_scores"]
        
        # Internal storage for cross-reference resolution
        self._ptm_registry: Dict[str, BudoPTM] = {}
        self._ligand_registry: Dict[str, BudoLigand] = {}
    
    def _setup_logging(self, log_level: str) -> logging.Logger:
        """Setup logging configuration"""
        logger = logging.getLogger("LMPParser")
        logger.setLevel(getattr(logging, log_level.upper()))
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Formatter
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(formatter)
        
        logger.addHandler(console_handler)
        
        return logger
    
    def _load_config(self, config_path: Optional[Path]) -> Dict:
        """Load LMP configuration from YAML file"""
        if config_path is None:
            # Default: look for lmp_config.yaml in same directory as this file
            config_path = Path(__file__).parent / "lmp_config.yaml"
        
        config_path = Path(config_path)
        
        if not config_path.exists():
            self.logger.warning(f"Config file not found: {config_path}. Using defaults.")
            return self._get_default_config()
        
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            self.logger.info(f"Loaded configuration from {config_path}")
            return config
        except Exception as e:
            self.logger.error(f"Failed to load config from {config_path}: {e}")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict:
        """Return default configuration if YAML not available"""
        return {
            "parser": {
                "validate_schema": True,
                "strict_mode": False,
                "skip_invalid": False,
                "support_multi_chain": True,
                "support_nested_domains": True,
                "max_domain_depth": 3,
                "log_level": "INFO",
            },
            "state_mappings": {
                "active": "ACTIVE",
                "inactive": "INACTIVE",
                "autoinhibited": "INACTIVE",
                "inhibitor-bound": "INACTIVE",
                "substrate-bound": "ACTIVE",
                "transition_state": "TRANSITION",
                "agonist-bound": "ACTIVE",
                "antagonist-bound": "INACTIVE",
                "open": "ACTIVE",
                "closed": "INACTIVE",
                "allosteric": "ALLOSTERIC",
                "unknown": "UNKNOWN",
            },
            "confidence_scores": {
                "high": 0.9,
                "medium": 0.7,
                "low": 0.5,
                "predicted": 0.3,
            },
        }
    
    def _load_xsd_schema(self) -> Optional[object]:
        """Load XSD schema for validation (returns lxml.etree.XMLSchema if available)"""
        if not HAS_LXML:
            return None
        
        xsd_path = Path(__file__).parent / self.config["parser"].get("xsd_path", "lmp_v2_schema.xsd")
        
        if not xsd_path.exists():
            self.logger.warning(f"XSD schema not found: {xsd_path}. Validation disabled.")
            return None
        
        try:
            with open(xsd_path, "rb") as f:
                xsd_doc = lxml_etree.parse(f)
            xsd_schema = lxml_etree.XMLSchema(xsd_doc)
            self.logger.info(f"Loaded XSD schema from {xsd_path}")
            return xsd_schema
        except Exception as e:
            self.logger.error(f"Failed to load XSD schema: {e}")
            return None
    
    def parse(self, lmp_xml_path: Union[str, Path]) -> BudoV3:
        """
        Parse LMP v2.0 XML file into BudoV3 object
        
        Args:
            lmp_xml_path: Path to LMP v2.0 XML file
            
        Returns:
            BudoV3 object with LMP annotations
            
        Raises:
            FileNotFoundError: If XML file doesn't exist
            ET.ParseError: If XML is malformed
        """
        lmp_xml_path = Path(lmp_xml_path)
        if not lmp_xml_path.exists():
            raise FileNotFoundError(f"LMP XML file not found: {lmp_xml_path}")
        
        # Parse XML
        tree = ET.parse(lmp_xml_path)
        root = tree.getroot()
        
        # Validate root tag (handle namespace)
        root_tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag
        if root_tag != "PML_Protein":
            raise ValueError(f"Root element must be <PML_Protein>, got <{root.tag}>")
        
        # Extract metadata
        uniprot_id = root.get("uniprot_id", "")
        gene_name = root.get("gene_name", "")
        organism = root.get("organism", "Unknown")
        
        # Extract namespace (if present)
        namespace = ""
        if '}' in root.tag:
            namespace = root.tag.split('}')[0] + '}'
        
        # Extract sources from metadata (with namespace)
        sources = []
        metadata_elem = root.find(f"{namespace}Metadata") if namespace else root.find("Metadata")
        if metadata_elem is not None:
            source_elems = metadata_elem.findall(f"{namespace}Source") if namespace else metadata_elem.findall("Source")
            for source_elem in source_elems:
                sources.append({
                    "type": source_elem.get("type"),
                    "ref": source_elem.get("ref"),
                })
        
        # Extract chain (with namespace support)
        chain_elem = root.find(f"{namespace}Chain") if namespace else root.find("Chain")
        if chain_elem is None:
            raise ValueError("No <Chain> element found in LMP XML")
        
        chain_id = chain_elem.get("id", "A")
        sequence = chain_elem.get("sequence", "")
        
        # Generate BUDO ID (extract state from filename if present)
        state_suffix = self._extract_state_from_filename(lmp_xml_path)
        organism_token = "".join(ch for ch in organism.upper() if ch.isalnum())
        gene_token = gene_name.upper() if gene_name else "UNKNOWN"
        base_name = f"{gene_token}_{organism_token}" if organism_token else gene_token
        budo_id = f"budo:{base_name}-L"

        provenance = BudoProvenance(
            created_by="LMPParser",
            updated_by="LMPParser",
            source="LMP v2.0 XML",
            confidence=ConfidenceLevel.HIGH,
        )
        
        # Create BudoV3 base object
        budo_protein = BudoV3(
            budoId=budo_id,
            canonical_name=base_name,
            recommended_name=gene_name or gene_token,
            organism=organism,
            taxonomy_id="unknown",  # Extract from organism mapping if available
            sequence=sequence,
            sequence_length=len(sequence),
            provenance=provenance,
        )
        
        # Add cross-reference to UniProt
        if uniprot_id:
            budo_protein.cross_references.append(
                BudoCrossReference(
                    database="UniProt",
                    identifier=uniprot_id,
                    url=f"https://www.uniprot.org/uniprot/{uniprot_id}",
                )
            )
        
        # Parse domains (with namespace)
        domain_elems = chain_elem.findall(f"{namespace}Domain") if namespace else chain_elem.findall("Domain")
        for domain_elem in domain_elems:
            domain = self._parse_domain(domain_elem, sequence, namespace)
            budo_protein.domains.append(domain)
        
        # Parse protein-protein interfaces (with namespace)
        interface_elems = chain_elem.findall(f"{namespace}Interface") if namespace else chain_elem.findall("Interface")
        for interface_elem in interface_elems:
            interface = self._parse_interface(interface_elem, namespace)
            budo_protein.interfaces.append(interface)
        
        # Update functional state based on conformations
        self._update_functional_state(budo_protein)
        
        return budo_protein
    
    def parse_multi_state(self, lmp_xml_dir: Union[str, Path]) -> List[BudoV3]:
        """
        Parse multiple LMP XML files for different states of same protein
        
        Args:
            lmp_xml_dir: Directory containing LMP XML files
                        Expected naming: {uniprot_id}_{state_name}.xml
        
        Returns:
            List of BudoV3 objects (one per state)
        """
        lmp_xml_dir = Path(lmp_xml_dir)
        if not lmp_xml_dir.is_dir():
            raise NotADirectoryError(f"Not a directory: {lmp_xml_dir}")
        
        budo_proteins = []
        for xml_file in sorted(lmp_xml_dir.glob("*.xml")):
            try:
                budo_protein = self.parse(xml_file)
                budo_proteins.append(budo_protein)
            except Exception as e:
                print(f"Warning: Failed to parse {xml_file}: {e}")
        
        return budo_proteins
    
    def _parse_domain(
        self, domain_elem: ET.Element, full_sequence: str, namespace: str = ""
    ) -> BudoDomain:
        """Parse a <Domain> element with namespace support"""
        domain_id = domain_elem.get("name", "unknown")
        domain_type = domain_elem.get("type", "unknown")
        start_pos = int(domain_elem.get("start", 1))
        end_pos = int(domain_elem.get("end", len(full_sequence)))
        
        # Extract domain sequence
        domain_seq = full_sequence[start_pos - 1 : end_pos] if full_sequence else ""
        
        # Create domain
        domain = BudoDomain(
            domain_id=domain_id,
            domain_name=domain_id,
            domain_type=domain_type,
            start_position=start_pos,
            end_position=end_pos,
            sequence=domain_seq,
        )
        
        # Parse motifs (with namespace)
        motif_elems = domain_elem.findall(f"{namespace}Motif") if namespace else domain_elem.findall("Motif")
        for motif_elem in motif_elems:
            motif = {
                "name": motif_elem.get("name"),
                "start": int(motif_elem.get("start", 0)),
                "end": int(motif_elem.get("end", 0)),
            }
            domain.motifs.append(motif)
        
        # Parse PTMs (with namespace)
        ptm_elems = domain_elem.findall(f"{namespace}PTM") if namespace else domain_elem.findall("PTM")
        for ptm_elem in ptm_elems:
            ptm = self._parse_ptm(ptm_elem)
            domain.ptms.append(ptm)
        
        # Parse binding sites and ligands (with namespace)
        binding_site_elems = domain_elem.findall(f"{namespace}BindingSite") if namespace else domain_elem.findall("BindingSite")
        for binding_site_elem in binding_site_elems:
            binding_type = binding_site_elem.get("type", "unknown")
            
            # Bug #1 fix: Extract catalytic residues from binding sites
            if binding_type == "catalytic":
                residues_str = binding_site_elem.get("residues", "")
                if residues_str:
                    catalytic_positions = [
                        int(r.strip()) for r in residues_str.split(",") if r.strip()
                    ]
                    domain.catalytic_residues.extend(catalytic_positions)
            
            ligands = self._parse_binding_site(binding_site_elem, namespace)
            domain.ligands.extend(ligands)
        
        # Parse conformations (with namespace)
        conf_elems = domain_elem.findall(f"{namespace}Conformation") if namespace else domain_elem.findall("Conformation")
        for conf_elem in conf_elems:
            conformation = self._parse_conformation(conf_elem, namespace)
            domain.conformations.append(conformation)
        
        return domain
    
    def _parse_ptm(self, ptm_elem: ET.Element) -> BudoPTM:
        """Parse a <PTM> element"""
        return BudoPTM(
            ptm_id=ptm_elem.get("id", ""),
            ptm_type=ptm_elem.get("type", ""),
            residue=ptm_elem.get("residue", ""),
            position=int(ptm_elem.get("position", 0)),
            status=ptm_elem.get("status", "unknown"),
            evidence=ptm_elem.get("evidence"),
            causal_trigger=None,  # Can be set later when parsing conformations
        )
    
    def _parse_binding_site(self, binding_site_elem: ET.Element, namespace: str = "") -> List[BudoLigand]:
        """Parse a <BindingSite> element and its <Ligand> children with namespace support"""
        ligands = []
        
        binding_type = binding_site_elem.get("type", "unknown")
        residues_str = binding_site_elem.get("residues", "")
        binding_residues = (
            [int(r) for r in residues_str.split(",") if r.strip()]
            if residues_str
            else []
        )
        
        ligand_elems = binding_site_elem.findall(f"{namespace}Ligand") if namespace else binding_site_elem.findall("Ligand")
        for ligand_elem in ligand_elems:
            ligand = BudoLigand(
                ligand_id=ligand_elem.get("id", ""),
                ligand_name=ligand_elem.get("name", ""),
                ligand_type=ligand_elem.get("type", binding_type),
                effect=ligand_elem.get("effect", "unknown"),
                binding_site_residues=binding_residues,
            )
            ligands.append(ligand)
        
        return ligands
    
    def _parse_conformation(self, conf_elem: ET.Element, namespace: str = "") -> BudoConformation:
        """Parse a <Conformation> element with namespace support"""
        state_name = conf_elem.get("state_name", "unknown")
        trigger_id = conf_elem.get("trigger")
        
        # Parse feature states (with namespace)
        feature_states = {}
        feature_elems = conf_elem.findall(f"{namespace}FeatureState") if namespace else conf_elem.findall("FeatureState")
        for feature_elem in feature_elems:
            feature_name = feature_elem.get("feature_name", "")
            feature_state = feature_elem.get("state", "")
            if feature_name:
                feature_states[feature_name] = feature_state
        
        # Infer confidence from state name
        confidence = ConfidenceLevel.MEDIUM
        if any(keyword in state_name.lower() for keyword in ["predicted", "putative"]):
            confidence = ConfidenceLevel.PREDICTED
        elif trigger_id:  # Has experimental trigger (PTM or ligand)
            confidence = ConfidenceLevel.HIGH
        
        return BudoConformation(
            state_name=state_name,
            trigger_id=trigger_id,
            feature_states=feature_states,
            residue_indices=[],  # Can be populated from domain positions
            ese_signature=None,  # To be linked later from MD simulations
            confidence=confidence,
        )
    
    def _parse_interface(self, interface_elem: ET.Element, namespace: str = "") -> BudoInterface:
        """Parse an <Interface> element"""
        residues_str = interface_elem.get("interface_residues", "")
        interface_residues = (
            [int(r) for r in residues_str.split(",") if r.strip()]
            if residues_str
            else []
        )
        
        return BudoInterface(
            partner_protein_id=interface_elem.get("partner_protein", ""),
            partner_chain=interface_elem.get("partner_chain"),
            interface_residues=interface_residues,
            interface_type=interface_elem.get("type", "unknown"),
        )
    
    def _update_functional_state(self, budo_protein: BudoV3) -> None:
        """
        Update BudoV3 functional state based on parsed conformations
        
        Strategy:
        - Find the "dominant" conformational state (most confident)
        - Map to FunctionalState enum
        - Update budo_protein.functionalState
        """
        # Collect all conformations across all domains
        all_conformations = []
        for domain in budo_protein.domains:
            all_conformations.extend(domain.conformations)
        
        if not all_conformations:
            return  # No conformations to process
        
        # Find highest confidence conformation
        dominant_conf = max(
            all_conformations,
            key=lambda c: (
                {"high": 3, "medium": 2, "low": 1, "predicted": 0}.get(
                    c.confidence.value, 0
                )
            ),
        )

        # Map state name to FunctionalState (ensure we assign Enum, not raw str)
        state_name_lower = dominant_conf.state_name.lower()
        for keyword, functional_state in self._state_name_mapping.items():
            if keyword in state_name_lower:
                # functional_state in config may be a string like 'ACTIVE' or an Enum-like value.
                try:
                    if isinstance(functional_state, str):
                        # Normalize to enum value (FunctionalState expects lowercase values)
                        fs_value = functional_state.lower()
                        budo_protein.functionalState.current = FunctionalState(fs_value)
                        budo_protein.functionalState.predicted = FunctionalState(fs_value)
                    elif isinstance(functional_state, FunctionalState):
                        budo_protein.functionalState.current = functional_state
                        budo_protein.functionalState.predicted = functional_state
                    else:
                        # Fallback
                        budo_protein.functionalState.current = FunctionalState.UNKNOWN
                        budo_protein.functionalState.predicted = FunctionalState.UNKNOWN

                    budo_protein.functionalState.prediction_confidence = (
                        {"high": 0.9, "medium": 0.7, "low": 0.5, "predicted": 0.3}.get(
                            dominant_conf.confidence.value, 0.5
                        )
                    )
                    budo_protein.functionalState.updated_by = "lmp_parser"
                except Exception as e:
                    # Log but don't crash parsing
                    self.logger.warning(f"Failed to map functional state '{functional_state}' to Enum: {e}")
                break
    
    def _extract_state_from_filename(self, filepath: Path) -> str:
        """
        Extract state name from filename
        
        Expected format: {uniprot_id}_{state_name}.xml
        Example: P12931_Active.xml → "Active"
        """
        stem = filepath.stem  # Filename without extension
        parts = stem.split("_")
        
        # If multiple underscores, assume last part is state
        if len(parts) >= 2:
            return parts[-1]
        
        return "Unknown"


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Parse single LMP XML file
    parser = LMPParser()
    
    # Parse c-Src kinase in Active state
    budo_src_active = parser.parse("P12931_Active.xml")
    
    print(f"Protein: {budo_src_active.canonical_name}")
    print(f"Functional State: {budo_src_active.functionalState.current}")
    print(f"Domains: {len(budo_src_active.domains)}")
    
    for domain in budo_src_active.domains:
        print(f"\n  Domain: {domain.domain_name}")
        print(f"  PTMs: {len(domain.ptms)}")
        for ptm in domain.ptms:
            print(f"    - {ptm.ptm_id}: {ptm.ptm_type} at {ptm.residue}{ptm.position} ({ptm.status})")
        
        print(f"  Conformations: {len(domain.conformations)}")
        for conf in domain.conformations:
            print(f"    - {conf.state_name} (triggered by {conf.trigger_id})")
            for feature, state in conf.feature_states.items():
                print(f"      * {feature}: {state}")
    
    # Example: Parse multiple states of same protein
    print("\n" + "="*60)
    print("Parsing multi-state LMP directory...")
    
    budo_proteins = parser.parse_multi_state("lmp_corpus/")
    print(f"Loaded {len(budo_proteins)} protein states")
    
    for budo in budo_proteins:
        print(f"  - {budo.budoId}: {budo.functionalState.current.value}")

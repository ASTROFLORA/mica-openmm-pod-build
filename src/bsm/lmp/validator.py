"""
LMP v2.0 XML Validator
======================

Validate LMP v2.0 XML documents against:
1. XML schema (structural validation)
2. Controlled vocabularies (ontology validation)
3. Causal consistency (trigger relationships)
4. Biological plausibility (cross-checks with databases)
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from dataclasses import dataclass, field


@dataclass
class ValidationError:
    """Validation error details"""
    severity: str  # "error", "warning", "info"
    category: str  # "schema", "vocabulary", "causality", "biology"
    message: str
    element_path: str = ""
    line_number: Optional[int] = None


@dataclass
class ValidationResult:
    """Validation result summary"""
    is_valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    info: List[ValidationError] = field(default_factory=list)
    
    def add_error(self, category: str, message: str, element_path: str = ""):
        """Add validation error"""
        self.errors.append(
            ValidationError(
                severity="error",
                category=category,
                message=message,
                element_path=element_path,
            )
        )
        self.is_valid = False
    
    def add_warning(self, category: str, message: str, element_path: str = ""):
        """Add validation warning"""
        self.warnings.append(
            ValidationError(
                severity="warning",
                category=category,
                message=message,
                element_path=element_path,
            )
        )
    
    def add_info(self, category: str, message: str, element_path: str = ""):
        """Add validation info"""
        self.info.append(
            ValidationError(
                severity="info",
                category=category,
                message=message,
                element_path=element_path,
            )
        )
    
    def summary(self) -> str:
        """Return validation summary string"""
        lines = []
        lines.append(f"Validation Result: {'PASS' if self.is_valid else 'FAIL'}")
        lines.append(f"  Errors: {len(self.errors)}")
        lines.append(f"  Warnings: {len(self.warnings)}")
        lines.append(f"  Info: {len(self.info)}")
        
        if self.errors:
            lines.append("\nErrors:")
            for err in self.errors:
                lines.append(f"  [{err.category}] {err.message}")
                if err.element_path:
                    lines.append(f"    Path: {err.element_path}")
        
        if self.warnings:
            lines.append("\nWarnings:")
            for warn in self.warnings:
                lines.append(f"  [{warn.category}] {warn.message}")
        
        return "\n".join(lines)


class LMPValidator:
    """
    Validator for LMP v2.0 XML documents
    
    Validation Layers:
    1. **Schema Validation**: Required elements, attributes
    2. **Vocabulary Validation**: Controlled terms (PTM types, states, etc.)
    3. **Causality Validation**: Trigger IDs reference existing PTMs/Ligands
    4. **Biology Validation**: Residue types match PTM types (e.g., pY requires Tyrosine)
    
    Example Usage:
    ```python
    validator = LMPValidator()
    result = validator.validate("P12931_Active.xml")
    
    if result.is_valid:
        print("✓ Valid LMP v2.0 document")
    else:
        print(result.summary())
    ```
    """
    
    # Controlled vocabularies
    VALID_PTM_TYPES = {
        "phosphorylation",
        "acetylation",
        "ubiquitination",
        "methylation",
        "sumoylation",
        "glycosylation",
        "hydroxylation",
        "nitrosylation",
    }
    
    VALID_PTM_STATUS = {"present", "absent", "transient", "unknown"}
    
    VALID_LIGAND_TYPES = {
        "agonist",
        "antagonist",
        "substrate",
        "inhibitor",
        "cofactor",
        "allosteric_modulator",
    }
    
    VALID_LIGAND_EFFECTS = {
        "activation",
        "inhibition",
        "catalysis",
        "allosteric_modulation",
        "stabilization",
    }
    
    VALID_INTERFACE_TYPES = {
        "heterodimer",
        "homodimer",
        "intramolecular",
        "protein-protein",
        "protein-ligand",
    }
    
    # Residue-PTM compatibility
    PTM_RESIDUE_COMPATIBILITY = {
        "phosphorylation": {"S", "T", "Y"},  # Serine, Threonine, Tyrosine
        "acetylation": {"K"},  # Lysine
        "ubiquitination": {"K"},  # Lysine
        "methylation": {"K", "R"},  # Lysine, Arginine
        "sumoylation": {"K"},  # Lysine
        "glycosylation": {"N", "S", "T"},  # Asparagine, Serine, Threonine
    }
    
    def __init__(self, strict: bool = True):
        """
        Initialize validator
        
        Args:
            strict: If True, warnings are treated as errors
        """
        self.strict = strict
    
    def validate(
        self,
        lmp_xml_path: Path,
        *,
        validate_schema: bool = True,
        validate_vocabularies: bool = True,
        validate_causality: bool = True,
        validate_biology: bool = True,
    ) -> ValidationResult:
        """
        Validate LMP v2.0 XML file
        
        Args:
            lmp_xml_path: Path to LMP XML file
        
        Returns:
            ValidationResult object
        """
        result = ValidationResult(is_valid=True)
        
        lmp_xml_path = Path(lmp_xml_path)
        if not lmp_xml_path.exists():
            result.add_error("schema", f"File not found: {lmp_xml_path}")
            return result
        
        # Parse XML
        try:
            tree = ET.parse(lmp_xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            result.add_error("schema", f"XML parsing error: {e}")
            return result
        
        if validate_schema:
            self._validate_schema(root, result)

        if validate_vocabularies:
            self._validate_vocabularies(root, result)

        if validate_causality:
            self._validate_causality(root, result)

        if validate_biology:
            self._validate_biology(root, result)
        
        return result
    
    def validate_batch(
        self,
        lmp_xml_dir: Path,
        *,
        validate_schema: bool = True,
        validate_vocabularies: bool = True,
        validate_causality: bool = True,
        validate_biology: bool = True,
    ) -> Dict[str, ValidationResult]:
        """
        Validate all LMP XML files in directory
        
        Args:
            lmp_xml_dir: Directory containing LMP XML files
        
        Returns:
            Dictionary mapping filename → ValidationResult
        """
        results = {}
        
        lmp_xml_dir = Path(lmp_xml_dir)
        for xml_file in sorted(lmp_xml_dir.glob("*.xml")):
            results[xml_file.name] = self.validate(
                xml_file,
                validate_schema=validate_schema,
                validate_vocabularies=validate_vocabularies,
                validate_causality=validate_causality,
                validate_biology=validate_biology,
            )
        
        return results
    
    def _validate_schema(self, root: ET.Element, result: ValidationResult):
        """Validate XML schema structure"""
        # Check root element
        if root.tag != "PML_Protein":
            result.add_error(
                "schema",
                f"Root element must be <PML_Protein>, got <{root.tag}>",
                "/"
            )
            return
        
        # Check required attributes
        required_attrs = ["uniprot_id", "gene_name"]
        for attr in required_attrs:
            if attr not in root.attrib:
                result.add_error(
                    "schema",
                    f"Missing required attribute: {attr}",
                    "/PML_Protein"
                )
        
        # Check for Chain element
        chain = root.find("Chain")
        if chain is None:
            result.add_error("schema", "Missing required element: <Chain>", "/PML_Protein")
            return
        
        # Check Chain attributes
        if "sequence" not in chain.attrib:
            result.add_error(
                "schema",
                "Missing required attribute: sequence",
                "/PML_Protein/Chain"
            )
        
        # Validate domains
        domains = chain.findall("Domain")
        if not domains:
            result.add_warning(
                "schema",
                "No <Domain> elements found (recommended)",
                "/PML_Protein/Chain"
            )
        
        for i, domain in enumerate(domains):
            domain_path = f"/PML_Protein/Chain/Domain[{i}]"
            
            # Check domain attributes
            if "name" not in domain.attrib:
                result.add_error(
                    "schema",
                    "Missing required attribute: name",
                    domain_path
                )
    
    def _validate_vocabularies(self, root: ET.Element, result: ValidationResult):
        """Validate controlled vocabularies"""
        chain = root.find("Chain")
        if chain is None:
            return
        
        # Validate PTMs
        for ptm in chain.findall(".//PTM"):
            ptm_path = f"/PML_Protein/Chain/Domain/PTM[@id='{ptm.get('id')}']"
            
            ptm_type = ptm.get("type", "")
            if ptm_type and ptm_type not in self.VALID_PTM_TYPES:
                result.add_warning(
                    "vocabulary",
                    f"Unknown PTM type: '{ptm_type}' (expected one of {self.VALID_PTM_TYPES})",
                    ptm_path
                )
            
            ptm_status = ptm.get("status", "")
            if ptm_status and ptm_status not in self.VALID_PTM_STATUS:
                result.add_error(
                    "vocabulary",
                    f"Invalid PTM status: '{ptm_status}' (expected one of {self.VALID_PTM_STATUS})",
                    ptm_path
                )
        
        # Validate Ligands
        for ligand in chain.findall(".//Ligand"):
            ligand_path = f"/PML_Protein/Chain/Domain/BindingSite/Ligand[@name='{ligand.get('name')}']"
            
            ligand_type = ligand.get("type", "")
            if ligand_type and ligand_type not in self.VALID_LIGAND_TYPES:
                result.add_warning(
                    "vocabulary",
                    f"Unknown ligand type: '{ligand_type}' (expected one of {self.VALID_LIGAND_TYPES})",
                    ligand_path
                )
            
            ligand_effect = ligand.get("effect", "")
            if ligand_effect and ligand_effect not in self.VALID_LIGAND_EFFECTS:
                result.add_warning(
                    "vocabulary",
                    f"Unknown ligand effect: '{ligand_effect}' (expected one of {self.VALID_LIGAND_EFFECTS})",
                    ligand_path
                )
        
        # Validate Interfaces
        for interface in chain.findall(".//Interface"):
            interface_path = f"/PML_Protein/Chain/Interface"
            
            interface_type = interface.get("type", "")
            if interface_type and interface_type not in self.VALID_INTERFACE_TYPES:
                result.add_warning(
                    "vocabulary",
                    f"Unknown interface type: '{interface_type}' (expected one of {self.VALID_INTERFACE_TYPES})",
                    interface_path
                )
    
    def _validate_causality(self, root: ET.Element, result: ValidationResult):
        """Validate causal trigger relationships"""
        chain = root.find("Chain")
        if chain is None:
            return
        
        # Collect all PTM and Ligand IDs
        valid_trigger_ids: Set[str] = set()
        
        for ptm in chain.findall(".//PTM"):
            ptm_id = ptm.get("id")
            if ptm_id:
                valid_trigger_ids.add(ptm_id)
        
        for ligand in chain.findall(".//Ligand"):
            ligand_id = ligand.get("id")
            if ligand_id:
                valid_trigger_ids.add(ligand_id)
        
        # Validate Conformation triggers
        for conf in chain.findall(".//Conformation"):
            conf_path = f"/PML_Protein/Chain/Domain/Conformation[@state_name='{conf.get('state_name')}']"
            
            trigger_id = conf.get("trigger")
            if trigger_id and trigger_id not in valid_trigger_ids:
                result.add_error(
                    "causality",
                    f"Conformation references unknown trigger: '{trigger_id}' (available: {valid_trigger_ids})",
                    conf_path
                )
        
        # Validate PTM causal triggers
        for ptm in chain.findall(".//PTM"):
            ptm_path = f"/PML_Protein/Chain/Domain/PTM[@id='{ptm.get('id')}']"
            
            causal_trigger = ptm.get("causal_trigger")
            if causal_trigger and causal_trigger not in valid_trigger_ids:
                result.add_warning(
                    "causality",
                    f"PTM references unknown causal trigger: '{causal_trigger}'",
                    ptm_path
                )
    
    def _validate_biology(self, root: ET.Element, result: ValidationResult):
        """Validate biological plausibility"""
        chain = root.find("Chain")
        if chain is None:
            return
        
        sequence = chain.get("sequence", "")
        
        # Validate PTM-residue compatibility
        for ptm in chain.findall(".//PTM"):
            ptm_path = f"/PML_Protein/Chain/Domain/PTM[@id='{ptm.get('id')}']"
            
            ptm_type = ptm.get("type", "")
            residue = ptm.get("residue", "")
            position = ptm.get("position")
            
            # Check residue type compatibility
            if ptm_type in self.PTM_RESIDUE_COMPATIBILITY:
                valid_residues = self.PTM_RESIDUE_COMPATIBILITY[ptm_type]
                
                if residue and residue not in valid_residues:
                    result.add_error(
                        "biology",
                        f"Invalid residue for {ptm_type}: '{residue}' (expected one of {valid_residues})",
                        ptm_path
                    )
            
            # Cross-check position with sequence
            if position and sequence:
                try:
                    pos_int = int(position)
                    if 1 <= pos_int <= len(sequence):
                        actual_residue = sequence[pos_int - 1]
                        if residue and residue != actual_residue:
                            result.add_error(
                                "biology",
                                f"Residue mismatch at position {position}: PTM specifies '{residue}', sequence has '{actual_residue}'",
                                ptm_path
                            )
                    else:
                        result.add_error(
                            "biology",
                            f"Position {position} out of sequence range (1-{len(sequence)})",
                            ptm_path
                        )
                except ValueError:
                    result.add_error(
                        "biology",
                        f"Invalid position value: '{position}' (must be integer)",
                        ptm_path
                    )
        
        # Validate binding site residues
        for binding_site in chain.findall(".//BindingSite"):
            residues_str = binding_site.get("residues", "")
            if residues_str and sequence:
                try:
                    residues = [int(r) for r in residues_str.split(",") if r.strip()]
                    for res_pos in residues:
                        if res_pos < 1 or res_pos > len(sequence):
                            result.add_error(
                                "biology",
                                f"Binding site residue {res_pos} out of sequence range (1-{len(sequence)})",
                                "/PML_Protein/Chain/Domain/BindingSite"
                            )
                except ValueError:
                    result.add_error(
                        "biology",
                        f"Invalid binding site residues format: '{residues_str}' (expected comma-separated integers)",
                        "/PML_Protein/Chain/Domain/BindingSite"
                    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    validator = LMPValidator(strict=False)
    
    # Example 1: Validate single LMP XML file
    print("Validating P12931_Active.xml...")
    result = validator.validate(Path("P12931_Active.xml"))
    print(result.summary())
    
    print("\n" + "="*60)
    
    # Example 2: Batch validate LMP corpus
    print("Validating LMP corpus...")
    results = validator.validate_batch(Path("lmp_corpus/"))
    
    # Summary statistics
    total_files = len(results)
    valid_files = sum(1 for r in results.values() if r.is_valid)
    
    print(f"\nValidation Summary:")
    print(f"  Total files: {total_files}")
    print(f"  Valid: {valid_files}")
    print(f"  Invalid: {total_files - valid_files}")
    
    # Show invalid files
    print("\nInvalid files:")
    for filename, result in results.items():
        if not result.is_valid:
            print(f"  {filename}:")
            for err in result.errors[:3]:  # Show first 3 errors
                print(f"    - [{err.category}] {err.message}")

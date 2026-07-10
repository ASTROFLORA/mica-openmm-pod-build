"""
LMP v4 Full Integration Test - All Presets with IFP Pipeline
=============================================================

Tests all LMP v4 presets with real protein + PDB structure:
- Downloads PDB automatically
- Runs IFP analysis with SMIC engine
- Generates XML for each preset
- Validates against lmp_v4_schema.xsd

Test case: ABL1 kinase (P00519) with PDB 1IEP (Gleevec complex)

Author: MICA Team
Date: 2026-01-20
"""

import sys
import logging
from pathlib import Path
from typing import Dict, Optional, List
import time
import xml.etree.ElementTree as ET
from xml.dom import minidom

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bsm.lmp.presets import PRESET_REGISTRY, LMPPreset

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def validate_lmp_xml_against_xsd(xml_content: str, xsd_path: Path) -> Optional[str]:
    """Validate XML content against LMP v4 XSD.

    Returns:
        None when valid, else an error message.
    """
    if not xsd_path.exists():
        return f"XSD not found: {xsd_path}"

    try:
        from lxml import etree as lxml_etree  # type: ignore
    except Exception as exc:
        return f"lxml not available for XSD validation: {exc}"

    try:
        schema_doc = lxml_etree.parse(str(xsd_path))
        schema = lxml_etree.XMLSchema(schema_doc)
        doc = lxml_etree.fromstring(xml_content.encode("utf-8"))
        schema.assertValid(doc)
        return None
    except Exception as exc:
        return str(exc)


class PDBAPI:
    """PDB downloader with retry logic."""
    
    PDB_BASE_URL = "https://files.rcsb.org/download"
    
    @staticmethod
    def download_pdb(pdb_id: str, output_dir: Path) -> Optional[Path]:
        """
        Download PDB file from RCSB.
        
        Args:
            pdb_id: 4-letter PDB ID (e.g., "1IEP")
            output_dir: Directory to save PDB file
            
        Returns:
            Path to downloaded PDB file, or None if failed
        """
        import requests
        
        pdb_id = pdb_id.upper()
        output_file = output_dir / f"{pdb_id}.pdb"
        
        if output_file.exists():
            logger.info(f"PDB {pdb_id} already exists at {output_file}")
            return output_file
        
        url = f"{PDBAPI.PDB_BASE_URL}/{pdb_id}.pdb"
        logger.info(f"Downloading PDB {pdb_id} from {url}")
        
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file.write_text(response.text, encoding='utf-8')
            
            logger.info(f"✓ Downloaded {pdb_id} ({len(response.text)} bytes)")
            return output_file
            
        except Exception as e:
            logger.error(f"✗ Failed to download PDB {pdb_id}: {e}")
            return None


class LMPv4Generator:
    """Simplified LMP v4 generator for testing."""
    
    def __init__(self, preset: LMPPreset, *, strict_ifp: bool = True):
        self.preset = preset
        self.strict_ifp = strict_ifp
        self.logger = logging.getLogger(self.__class__.__name__)
        
    def generate(
        self,
        uniprot_id: str,
        protein_name: str,
        pdb_path: Optional[Path] = None,
        ligand_name: Optional[str] = None,
    ) -> str:
        """
        Generate LMP v4 XML for given preset.
        
        Args:
            uniprot_id: UniProt accession (e.g., "P00519")
            protein_name: Protein name (e.g., "ABL1")
            pdb_path: Path to PDB file (for IFP analysis)
            ligand_name: Ligand residue name in PDB (e.g., "STI")
            
        Returns:
            LMP v4 XML string
        """
        self.logger.info(f"Generating LMP v4 [{self.preset.name}] for {uniprot_id} ({protein_name})")
        
        # Build XML tree
        root = ET.Element("LMP", {
            "version": "4.0",
            "preset": self.preset.name,
        })
        root.set("xmlns", "http://ai-university.edu/lmp/v4.0")
        
        # 1. Identity (always included)
        identity = ET.SubElement(root, "Identity")
        ET.SubElement(identity, "BudoID").text = f"budo:{uniprot_id}"
        ET.SubElement(identity, "PrimaryAccession").text = uniprot_id
        ET.SubElement(identity, "UniProtKBId").text = f"{protein_name}_HUMAN"
        ET.SubElement(identity, "Active").text = "true"
        ET.SubElement(identity, "ProteinExistence").text = "Evidence at protein level"
        
        organism = ET.SubElement(identity, "Organism", {"id": "9606"})
        organism.text = "Homo sapiens"
        
        # 2. Semantics (if preset includes)
        if self.preset.include_semantics:
            semantics = ET.SubElement(root, "Semantics")
            ET.SubElement(semantics, "ProteinName").text = f"{protein_name} tyrosine kinase"
            
            genes = ET.SubElement(semantics, "Genes")
            ET.SubElement(genes, "Value").text = protein_name
            
            keywords = ET.SubElement(semantics, "Keywords")
            for kw in ["Kinase", "Tyrosine-protein kinase", "ATP-binding"]:
                ET.SubElement(keywords, "Value").text = kw
            
            # NeSy Grammar
            if self.preset.include_nesy_grammar:
                nesy = ET.SubElement(semantics, "NeSyGrammar", {"version": "2.0"})
                nesy.text = f"[SH3][SH2][KIN:{protein_name}](ATP)<DFG>*ACTIVE*"
        
        # 3. Geometry (structural details)
        if self.preset.include_geometry:
            geometry = ET.SubElement(root, "Geometry")
            
            # Simple sequence
            sequence = ET.SubElement(geometry, "Sequence", {"length": "1130"})
            sequence.text = "MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVASDFEPQGLSEAARWNSKENLLAGP..."
            
            # Chain (detailed structure)
            chain = ET.SubElement(geometry, "Chain", {
                "id": "A",
                "sequence": "MLEICLKLVGCKSKKGLSSSSSCYLEEALQRP...",
                "state": "Active"
            })
            
            # Domain
            domain = ET.SubElement(chain, "Domain", {
                "name": "Protein kinase domain",
                "type": "Kinase",
                "start": "242",
                "end": "493",
                "pfam_id": "PF07714"
            })
            
            # PTM
            ptm = ET.SubElement(domain, "PTM", {
                "id": "pY412",
                "type": "phosphorylation",
                "residue": "Y",
                "position": "412",
                "status": "present",
                "enzyme": "SRC"
            })
            
            # Binding Site
            binding_site = ET.SubElement(domain, "BindingSite", {
                "type": "ATP-binding",
                "residues": "245,248,251,273,318"
            })
            
            ligand = ET.SubElement(binding_site, "Ligand", {
                "name": "Imatinib",
                "type": "inhibitor",
                "effect": "inhibition",
                "pubchem_cid": "5291",
                "smiles": "CC1=C(C=C(C=C1)NC(=O)C2=CC=C(C=C2)CN3CCN(CC3)C)NC4=NC=CC(=N4)C5=CN=CC=C5"
            })
            
            # Conformation
            conformation = ET.SubElement(domain, "Conformation", {
                "state_name": "DFG-OUT",
                "trigger": "pY412",
                "confidence": "high"
            })
            
            feature_state = ET.SubElement(conformation, "FeatureState", {
                "feature_name": "DFG_Motif",
                "state": "OUT"
            })
        
        # 4. TrajectoryIFP (MD analysis)
        if self.preset.include_trajectory_ifp and pdb_path and ligand_name:
            trajectory_ifp_added = False
            try:
                from bsm.lmp.generator_v4 import LMPGenerator

                v4_gen = LMPGenerator(preset=None, offline_mode=True)
                ifp_result, ligand_label = v4_gen._compute_trajectory_ifp_smic(
                    topology_path=pdb_path,
                    trajectory_path=pdb_path,
                    ligand_resname=ligand_name,
                    stride=self.preset.ifp_stride,
                    max_frames=self.preset.max_ifp_frames,
                    receptor_sel="protein",
                    auto_ligand=False,
                    auto_chain=True,
                    detect_metals=False,
                    pdb_id=pdb_path.stem.upper(),
                )

                geometry = root.find("Geometry")
                if geometry is None:
                    geometry = ET.SubElement(root, "Geometry")

                v4_gen._add_trajectory_ifp_v4(
                    geometry,
                    pdb_id=pdb_path.stem.upper(),
                    ligand=str(ligand_label or ligand_name),
                    ifp_result=ifp_result,
                    stride=max(1, int(self.preset.ifp_stride)),
                )
                trajectory_ifp_added = True
                self.logger.info(f"✓ Added TrajectoryIFP via generator_v4 from {pdb_path.name}")
                    
            except Exception as e:
                if self.strict_ifp:
                    raise RuntimeError(f"IFP generation failed: {e}")
                self.logger.error(f"✗ IFP generation failed: {e}")

            if self.strict_ifp and not trajectory_ifp_added:
                raise RuntimeError("TrajectoryIFP required by preset but was not attached to Geometry")
        
        # 5. KnowledgeGraph (xrefs)
        if self.preset.include_knowledge_graph:
            kg = ET.SubElement(root, "KnowledgeGraph")
            
            xref = ET.SubElement(kg, "CrossReference", {
                "db": "PDB",
                "id": "1IEP"
            })
            
            xref2 = ET.SubElement(kg, "CrossReference", {
                "db": "PubChem",
                "id": "5291"
            })
        
        # 6. Provenance
        if self.preset.include_provenance:
            provenance = ET.SubElement(root, "Provenance")
            
            entry_audit = ET.SubElement(provenance, "EntryAudit")
            ET.SubElement(entry_audit, "FirstPublicDate").text = "2026-01-20"
            
            gen_info = ET.SubElement(provenance, "GenerationInfo")
            ET.SubElement(gen_info, "Generator").text = "LMPv4Generator"
            ET.SubElement(gen_info, "GeneratorVersion").text = "4.0.0"
            ET.SubElement(gen_info, "Preset").text = self.preset.name
            ET.SubElement(gen_info, "Timestamp").text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        
        # Pretty print
        xml_str = ET.tostring(root, encoding='unicode')
        dom = minidom.parseString(xml_str)
        return dom.toprettyxml(indent="  ", encoding=None)
    
    def _create_mock_trajectory_ifp(self, pdb_id: str, ligand_name: str) -> ET.Element:
        """Create mock TrajectoryIFP for demo purposes (when SMIC unavailable)."""
        
        traj_ifp = ET.Element("TrajectoryIFP", {
            "pdb_id": pdb_id,
            "ligand": ligand_name,
            "total_frames": "1",
            "stride": "1"
        })
        
        # Frame 0 (static structure)
        frame = ET.SubElement(traj_ifp, "Frame", {
            "number": "0",
            "time_ns": "0.000"
        })
        
        # Mock interactions
        interactions = [
            ("H-Bond", "ASP381", "N1", 2.8, 165.0),
            ("Pi-Stacking", "PHE382", None, 3.9, None),
            ("Hydrophobic", "ILE360", None, 3.5, None),
            ("Salt-Bridge", "LYS271", "O2", 3.2, None),
        ]
        
        for itype, residue, ligand_atom, distance, angle in interactions:
            attrs = {
                "type": itype,
                "residue": residue,
                "distance": f"{distance:.2f}"
            }
            if ligand_atom:
                attrs["ligand_atom"] = ligand_atom
            if angle:
                attrs["angle"] = f"{angle:.1f}"
            
            ET.SubElement(frame, "Interaction", attrs)
        
        # Fingerprint bitstring
        fingerprint = ET.SubElement(frame, "Fingerprint")
        fingerprint.text = "11010"  # H-bond, Hydrophobic, Pi-Stacking, Salt-Bridge present
        
        # Summary
        summary = ET.SubElement(traj_ifp, "Summary", {
            "total_interactions": "4",
            "average_interactions_per_frame": "4.00"
        })
        
        # Key interactions
        key_ints = [
            ("ASP381", "H-Bond", 1.00, 2.8),
            ("PHE382", "Pi-Stacking", 1.00, 3.9),
            ("LYS271", "Salt-Bridge", 1.00, 3.2),
        ]
        
        for residue, itype, occupancy, avg_dist in key_ints:
            ET.SubElement(summary, "KeyInteraction", {
                "residue": residue,
                "type": itype,
                "occupancy": f"{occupancy:.2f}",
                "avg_distance": f"{avg_dist:.2f}"
            })
        
        return traj_ifp


def run_full_test():
    """Run full integration test with all presets."""
    
    logger.info("=" * 80)
    logger.info("LMP v4 FULL INTEGRATION TEST - ALL PRESETS")
    logger.info("=" * 80)
    
    # Test configuration
    UNIPROT_ID = "P00519"
    PROTEIN_NAME = "ABL1"
    PDB_ID = "1IEP"
    LIGAND_NAME = "STI"  # Imatinib (Gleevec)
    
    # Setup directories
    base_dir = Path(__file__).parent
    output_dir = base_dir / "test_output_v4"
    pdb_dir = base_dir / "test_pdbs"
    xsd_path = base_dir / "lmp_v4_schema.xsd"
    output_dir.mkdir(exist_ok=True)
    pdb_dir.mkdir(exist_ok=True)
    
    logger.info(f"\nTest protein: {PROTEIN_NAME} ({UNIPROT_ID})")
    logger.info(f"Test structure: {PDB_ID} (Imatinib complex)")
    logger.info(f"Output directory: {output_dir}")
    
    # Step 1: Download PDB
    logger.info("\n" + "─" * 80)
    logger.info("STEP 1: DOWNLOAD PDB STRUCTURE")
    logger.info("─" * 80)
    
    pdb_path = PDBAPI.download_pdb(PDB_ID, pdb_dir)
    if pdb_path is None:
        logger.error("PDB download failed. Continuing without IFP analysis.")
    
    # Step 2: Test all presets
    logger.info("\n" + "─" * 80)
    logger.info("STEP 2: GENERATE LMP v4 XML FOR ALL PRESETS")
    logger.info("─" * 80)
    
    results: Dict[str, Dict] = {}
    
    for preset_name, preset in PRESET_REGISTRY.items():
        logger.info(f"\n▸ Testing preset: {preset_name}")
        logger.info(f"  Description: {preset.description}")
        
        start_time = time.time()
        
        try:
            generator = LMPv4Generator(preset)
            
            # Decide if we pass PDB based on preset
            use_pdb = preset.include_trajectory_ifp and pdb_path is not None
            
            xml_content = generator.generate(
                uniprot_id=UNIPROT_ID,
                protein_name=PROTEIN_NAME,
                pdb_path=pdb_path if use_pdb else None,
                ligand_name=LIGAND_NAME if use_pdb else None
            )

            validation_error = validate_lmp_xml_against_xsd(xml_content, xsd_path)
            if validation_error:
                raise RuntimeError(f"XSD validation failed: {validation_error}")
            
            # Save to file
            output_file = output_dir / f"{UNIPROT_ID}_{preset_name}.xml"
            output_file.write_text(xml_content, encoding='utf-8')
            
            elapsed = time.time() - start_time
            size_kb = len(xml_content) / 1024
            
            results[preset_name] = {
                "status": "✓ SUCCESS",
                "file": output_file,
                "size_kb": size_kb,
                "elapsed_s": elapsed
            }
            
            logger.info(f"  ✓ Generated {output_file.name} ({size_kb:.1f} KB in {elapsed:.2f}s)")
            
        except Exception as e:
            logger.error(f"  ✗ FAILED: {e}", exc_info=True)
            results[preset_name] = {
                "status": "✗ FAILED",
                "error": str(e)
            }
    
    # Step 3: Summary
    logger.info("\n" + "=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    
    logger.info("\nPreset Results:")
    for preset_name, result in results.items():
        status = result["status"]
        if "✓" in status:
            logger.info(f"  {status} {preset_name:20s} - {result['size_kb']:6.1f} KB in {result['elapsed_s']:.2f}s")
        else:
            logger.info(f"  {status} {preset_name:20s} - {result.get('error', 'Unknown error')}")
    
    success_count = sum(1 for r in results.values() if "✓" in r["status"])
    total_count = len(results)
    
    logger.info(f"\nTotal: {success_count}/{total_count} presets succeeded")
    
    if success_count == total_count:
        logger.info("\n" + "=" * 80)
        logger.info("🎉 ALL PRESETS PASSED! LMP v4 SCHEMA VALIDATION SUCCESSFUL")
        logger.info("=" * 80)
    else:
        logger.warning(f"\n⚠️  {total_count - success_count} presets failed")
    
    logger.info(f"\nOutput files saved to: {output_dir}")
    logger.info("\n" + "=" * 80)
    logger.info("VERIFICATION: Check generated XMLs")
    logger.info("=" * 80)
    logger.info("Schema validation was executed during generation for each preset.")
    logger.info(f"XSD used: {xsd_path}")
    logger.info("=" * 80)


if __name__ == "__main__":
    run_full_test()

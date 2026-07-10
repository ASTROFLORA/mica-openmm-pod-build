"""
SMIC Bridge - IFP DataFrame to LMP v4 XML Converter
===================================================

Converts SMIC IFP Engine DataFrames to LMP v4 TrajectoryIFP XML elements.

Supports:
- H-bond analysis (donor/acceptor)
- π-π stacking (face-to-face, edge-to-face, offset)
- Salt bridges
- Hydrophobic contacts
- Water bridges
- Halogen bonds

Author: MICA Team
Date: 2026-01-20
"""

import sys
from pathlib import Path
from typing import Optional, Dict, List
import xml.etree.ElementTree as ET
import logging
import numpy as np

# Add SMIC path
SMIC_PATH = Path(__file__).parent.parent.parent.parent / "workers" / "smic" / "python"
if SMIC_PATH.exists():
    sys.path.insert(0, str(SMIC_PATH))

logger = logging.getLogger(__name__)

try:
    from smic_core.ifp_engine import IFPEngine, MDANALYSIS_AVAILABLE
    SMIC_AVAILABLE = True
except ImportError as e:
    logger.warning(f"SMIC IFP Engine not available: {e}")
    SMIC_AVAILABLE = False
    IFPEngine = None
    MDANALYSIS_AVAILABLE = False


class SMICBridge:
    """Bridge between SMIC IFP Engine and LMP v4 XML."""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        
        if not SMIC_AVAILABLE:
            self.logger.warning("SMIC not available. IFP generation disabled.")
        elif not MDANALYSIS_AVAILABLE:
            self.logger.warning("MDAnalysis not available. IFP analysis requires: pip install MDAnalysis")
    
    def generate_ifp_xml(
        self,
        pdb_path: Path,
        ligand_name: str,
        trajectory_path: Optional[Path] = None,
        stride: int = 1,
        max_frames: int = 500,
        min_occupancy: float = 0.1,
    ) -> Optional[ET.Element]:
        """
        Generate TrajectoryIFP XML element from PDB/trajectory.
        
        Args:
            pdb_path: Path to PDB structure (used as topology)
            ligand_name: Ligand residue name (e.g., "STI", "ATP", "LIG")
            trajectory_path: Optional trajectory file (.dcd, .xtc, .trr)
            stride: Frame stride (1 = every frame)
            max_frames: Maximum frames to process
            min_occupancy: Minimum occupancy to include in summary
            
        Returns:
            TrajectoryIFP XML Element, or None if generation failed
        """
        if not SMIC_AVAILABLE or not MDANALYSIS_AVAILABLE:
            self.logger.error("Cannot generate IFP: SMIC/MDAnalysis not available")
            return None
        
        if not pdb_path.exists():
            self.logger.error(f"PDB file not found: {pdb_path}")
            return None
        
        self.logger.info(f"Generating IFP for {pdb_path.name} (ligand={ligand_name})")
        
        try:
            engine = IFPEngine({})

            topology = str(pdb_path)
            traj = str(trajectory_path) if trajectory_path else str(pdb_path)

            self.logger.info("Running IFP analysis (SMIC IFPEngine.generate_ifp)...")
            ifp_result = engine.generate_ifp(
                topology=topology,
                trajectory=traj,
                receptor_sel="protein",
                ligand_sel=f"resname {ligand_name}",
                stride=max(1, int(stride)),
                start_frame=0,
                end_frame=None,
                analyze_water_bridges=True,
                verbose=False,
            )

            if ifp_result is None or int(getattr(ifp_result, "n_frames", 0) or 0) <= 0:
                self.logger.warning("No IFP results generated")
                return None

            return self._convert_to_xml(
                ifp_result=ifp_result,
                pdb_id=pdb_path.stem.upper(),
                ligand_name=ligand_name,
                stride=stride,
                max_frames=max_frames,
                min_occupancy=min_occupancy,
            )
            
        except Exception as e:
            self.logger.error(f"IFP generation failed: {e}", exc_info=True)
            return None
    
    def _map_smic_ifp_type_to_schema(self, ifp_type: str) -> str:
        mapping = {
            "HY": "Hydrophobic",
            "HD": "H-Bond",
            "HA": "H-Bond",
            "WB": "Water-Bridge",
            "IP": "Salt-Bridge",
            "IN": "Salt-Bridge",
            "IO": "Metal-Coordination",
            "HL": "Halogen-Bond",
            "AR": "Pi-Stacking",
        }
        return mapping.get((ifp_type or "").strip().upper(), "Hydrophobic")

    def _ifp_fingerprint_bits(self, mapped_types: set[str]) -> str:
        order = [
            "H-Bond",
            "Hydrophobic",
            "Pi-Stacking",
            "Pi-Cation",
            "Salt-Bridge",
            "Water-Bridge",
            "Halogen-Bond",
            "Metal-Coordination",
        ]
        return "".join("1" if t in mapped_types else "0" for t in order)

    def _convert_to_xml(
        self,
        ifp_result,
        pdb_id: str,
        ligand_name: str,
        stride: int,
        max_frames: int,
        min_occupancy: float,
    ) -> ET.Element:
        """
        Convert IFP DataFrame to TrajectoryIFP XML element.
        
        Args:
            results: IFP results DataFrame from SMIC engine
            pdb_id: PDB identifier
            ligand_name: Ligand residue name
            stride: Frame stride used
            min_occupancy: Minimum occupancy for key interactions
            
        Returns:
            TrajectoryIFP XML Element
        """
        # Create root TrajectoryIFP element
        total_frames = int(getattr(ifp_result, "n_frames", 0) or 0)
        
        traj_ifp = ET.Element("TrajectoryIFP", {
            "pdb_id": pdb_id,
            "ligand": ligand_name,
            "total_frames": str(total_frames),
            "stride": str(stride),
        })

        contact_occupancy = getattr(ifp_result, "contact_occupancy", {}) or {}
        frame_results = getattr(ifp_result, "frame_results", []) or []
        time_ps = getattr(ifp_result, "time_ps", None)
        if time_ps is not None and hasattr(time_ps, "__len__") and len(time_ps) >= 2:
            try:
                dt_ps = float(time_ps[1] - time_ps[0])
                if dt_ps > 0:
                    traj_ifp.set("time_step_ps", str(dt_ps))
            except Exception:
                pass

        # Build frames (bounded for XML size)
        n_emit = min(int(total_frames), int(max_frames) if max_frames else int(total_frames))
        total_interactions = 0
        distances_by_key: Dict[tuple, List[float]] = {}
        angles_by_key: Dict[tuple, List[float]] = {}
        resname_by_resid: Dict[int, str] = {}

        for fr in frame_results[:n_emit]:
            frame_elem = ET.Element("Frame", {
                "number": str(int(getattr(fr, "frame", 0) or 0)),
            })
            try:
                t_ns = float(getattr(fr, "time_ps", 0.0) or 0.0) / 1000.0
                frame_elem.set("time_ns", f"{t_ns:.3f}")
            except Exception:
                pass

            mapped_types: set[str] = set()
            contacts = getattr(fr, "contacts", []) or []
            total_interactions += len(contacts)

            for c in contacts:
                ifp_type = str(getattr(c, "ifp_type", "") or "")
                schema_type = self._map_smic_ifp_type_to_schema(ifp_type)
                mapped_types.add(schema_type)

                receptor_resid = getattr(c, "receptor_resid", None)
                ligand_resid = getattr(c, "ligand_resid", None)
                receptor_resname = (getattr(c, "receptor_resname", "") or "").strip()
                if isinstance(receptor_resid, int) and receptor_resname:
                    resname_by_resid.setdefault(receptor_resid, receptor_resname)

                residue_label = (
                    f"{receptor_resname}{receptor_resid}"
                    if receptor_resname and receptor_resid is not None
                    else str(receptor_resid or "UNK")
                )

                interaction = ET.Element("Interaction", {
                    "type": schema_type,
                    "residue": residue_label,
                })

                try:
                    dist = float(getattr(c, "distance", 0.0) or 0.0)
                    interaction.set("distance", f"{dist:.3f}")
                except Exception:
                    dist = None

                key = None
                try:
                    if receptor_resid is not None and ligand_resid is not None:
                        key = (int(receptor_resid), int(ligand_resid), ifp_type)
                        occ = contact_occupancy.get(key)
                        if occ is not None:
                            interaction.set("occupancy", str(float(occ)))
                except Exception:
                    key = None

                if key is not None:
                    if dist is not None:
                        distances_by_key.setdefault(key, []).append(float(dist))

                    ang = None
                    try:
                        md = getattr(c, "metadata", None)
                        if isinstance(md, dict):
                            ang = md.get("angle") or md.get("dha_angle") or md.get("pi_angle")
                        if ang is not None:
                            angf = float(ang)
                            angles_by_key.setdefault(key, []).append(angf)
                            interaction.set("angle", f"{angf:.2f}")
                    except Exception:
                        pass

                frame_elem.append(interaction)

            fp_elem = ET.Element("Fingerprint")
            fp_elem.text = self._ifp_fingerprint_bits(mapped_types)
            frame_elem.append(fp_elem)

            traj_ifp.append(frame_elem)

        # Summary + KeyInteraction (NO MOCKS)
        summary = ET.Element("Summary")
        summary.set("total_interactions", str(int(total_interactions)))
        if total_frames:
            summary.set("average_interactions_per_frame", f"{(float(total_interactions) / float(total_frames)):.3f}")

        try:
            ranked = sorted(contact_occupancy.items(), key=lambda kv: kv[1], reverse=True)
            for (receptor_resid, _lig_resid, ifp_type), occ in ranked:
                if float(occ) < float(min_occupancy):
                    break
                key_elem = ET.Element("KeyInteraction")
                resname = resname_by_resid.get(int(receptor_resid), "")
                key_elem.set("residue", f"{resname}{int(receptor_resid)}" if resname else str(int(receptor_resid)))
                key_elem.set("type", self._map_smic_ifp_type_to_schema(str(ifp_type)))
                key_elem.set("occupancy", f"{float(occ):.6f}")

                key = (int(receptor_resid), int(_lig_resid), str(ifp_type))
                dvals = distances_by_key.get(key) or []
                if dvals:
                    key_elem.set("avg_distance", f"{(float(np.mean(dvals))):.3f}")
                avals = angles_by_key.get(key) or []
                if avals:
                    key_elem.set("avg_angle", f"{(float(np.mean(avals))):.2f}")

                summary.append(key_elem)
        except Exception:
            pass

        traj_ifp.append(summary)
        
        return traj_ifp
    
        return traj_ifp


def test_smic_bridge():
    """Test SMIC bridge with example PDB."""
    
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    logger.info("Testing SMIC Bridge...")
    
    bridge = SMICBridge()
    
    # Test with mock PDB path
    pdb_path = Path(__file__).parent / "test_pdbs" / "1IEP.pdb"
    
    if not pdb_path.exists():
        logger.warning(f"Test PDB not found: {pdb_path}. Creating mock XML.")
        pdb_path = Path("mock.pdb")
    
    # Generate IFP XML
    ifp_xml = bridge.generate_ifp_xml(
        pdb_path=pdb_path,
        ligand_name="STI",
        stride=1,
        max_frames=100
    )
    
    if ifp_xml is not None:
        xml_str = ET.tostring(ifp_xml, encoding='unicode')
        logger.info(f"Generated IFP XML:\n{xml_str}")
        logger.info("✓ SMIC Bridge test passed")
    else:
        logger.error("✗ SMIC Bridge test failed")


if __name__ == "__main__":
    test_smic_bridge()

# LMP Unified — Roadmap de Implementación (3 Fases)

**Versión:** 1.0  
**Fecha:** 2026-01-20  
**Objetivo:** Construir el primer Language Modeling Protocol para Dinámica Molecular

---

## Resumen Ejecutivo

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ROADMAP OVERVIEW                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  PHASE 1: NeSy Offline (1 semana)                                       │
│  ├── UniProt JSON → NeSyAnnotation mapper                               │
│  ├── Integrar NeSy encoder en generator_unified.py                      │
│  ├── Extender XSD v4 con <NeSyGrammar>                                  │
│  └── Tests: cobertura vs v2 > 90%                                       │
│                                                                          │
│  PHASE 2: IFP → LMP Bridge (1 semana)                                   │
│  ├── IFPTrajectoryResult → XML serializer                               │
│  ├── Extender XSD v4 con <TrajectoryIFP>                                │
│  ├── Integrar SMIC bridge en preset=md-ifp                              │
│  └── Tests: trayectoria de 1000 frames < 10s                            │
│                                                                          │
│  PHASE 3: Presets + GCS + Finetune (1 semana)                           │
│  ├── Preset router con 5 presets                                        │
│  ├── GCS upload integration                                             │
│  ├── Extender finetune exporters                                        │
│  └── E2E: UniProt → LMP → GCS → Finetune                                │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: NeSy Offline (Core Semantic Layer)

### Objetivo
Portar la capacidad más valiosa de v2 (NeSy encoder) a un pipeline 100% offline usando el JSON de UniProt que ya descargamos.

### Duración Estimada
5-7 días

### Entregables

| # | Archivo | Descripción |
|---|---------|-------------|
| 1.1 | `nesy_offline_mapper.py` | Convierte UniProt JSON → `NeSyAnnotation` |
| 1.2 | `generator_unified.py` | Esqueleto con arquitectura de presets |
| 1.3 | `lmp_v4_schema.xsd` | Extensión con `<NeSyGrammar>` en `<Semantics>` |
| 1.4 | `tests/test_nesy_offline.py` | Suite de tests para mapper + encoder |

### Implementación Detallada

#### 1.1 `nesy_offline_mapper.py`

```python
"""
Map UniProt JSON entry to NeSyAnnotation for offline NeSy encoding.

This bridges the gap between v3's deterministic snapshots and v2's rich
neuro-symbolic grammar WITHOUT requiring network access.
"""

from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple
from .nesy_encoder import NeSyAnnotation

# Feature type → NeSy category mapping
FEATURE_TYPE_MAP = {
    # Domains
    "Domain": "domain",
    "Repeat": "domain",
    "Zinc finger": "domain",
    "Coiled coil": "domain",
    
    # Motifs
    "Motif": "motif",
    "Short sequence motif": "motif",
    "Compositional bias": "motif",
    
    # PTMs
    "Modified residue": "ptm",
    "Glycosylation": "ptm",
    "Disulfide bond": "ptm",
    "Cross-link": "ptm",
    "Lipidation": "ptm",
    
    # Binding
    "Binding site": "binding",
    "Active site": "binding",
    "Metal binding": "binding",
    "Nucleotide binding": "binding",
    "DNA binding": "binding",
    "Calcium binding": "binding",
    
    # Regions
    "Region": "region",
    "Transmembrane": "transmembrane",
    "Signal peptide": "signal",
    "Propeptide": "propeptide",
}

# PTM description → type + enzyme parsing
PTM_PATTERNS = {
    r"Phospho(serine|threonine|tyrosine)": ("phosphorylation", None),
    r"Phospho\w+; by (\w+)": ("phosphorylation", r"\1"),
    r"N6-acetyllysine": ("acetylation", None),
    r"N6-acetyllysine; by (\w+)": ("acetylation", r"\1"),
    r"Omega-N-methylarginine": ("methylation", None),
    r"N6-methyllysine": ("methylation", None),
    r"GPI-anchor": ("gpi_anchor", None),
    r"S-palmitoyl cysteine": ("palmitoylation", None),
    r"N-myristoyl glycine": ("n_terminal_myristoylation", None),
}

def map_uniprot_to_nesy(entry: Dict[str, Any]) -> NeSyAnnotation:
    """
    Convert UniProt JSON entry to NeSyAnnotation.
    
    Args:
        entry: Parsed UniProt JSON (from entry.json.gz)
        
    Returns:
        NeSyAnnotation ready for LMPNeSyEncoder.encode()
    """
    sequence = entry.get("sequence", {}).get("value", "")
    
    domains = []
    motifs = []
    ptms = []
    binding_sites = []
    
    for feat in entry.get("features", []):
        category = FEATURE_TYPE_MAP.get(feat.get("type"), None)
        if not category:
            continue
            
        loc = feat.get("location", {})
        start = _extract_position(loc.get("start"))
        end = _extract_position(loc.get("end"))
        desc = feat.get("description", "")
        
        if category == "domain":
            domains.append(_build_domain(feat, start, end, desc))
        elif category == "motif":
            motifs.append(_build_motif(feat, start, end, desc))
        elif category == "ptm":
            ptm = _build_ptm(feat, start, sequence, desc)
            if ptm:
                ptms.append(ptm)
        elif category == "binding":
            binding_sites.append(_build_binding_site(feat, start, end, desc))
        elif category == "transmembrane":
            domains.append({
                "name": "TMD",
                "type": "transmembrane",
                "start": start,
                "end": end,
            })
    
    return NeSyAnnotation(
        sequence=sequence,
        domains=domains,
        motifs=motifs,
        ptms=ptms,
        binding_sites=binding_sites,
        ppi_interfaces=[],  # Requires external data (STRING/IntAct)
        conformational_state=_infer_state(entry),
        state_regions=[],
    )

def _extract_position(loc_part: Any) -> Optional[int]:
    if isinstance(loc_part, dict):
        return loc_part.get("value")
    return None

def _build_domain(feat: Dict, start: int, end: int, desc: str) -> Dict:
    # ... implementation
    pass

def _build_ptm(feat: Dict, pos: int, seq: str, desc: str) -> Optional[Dict]:
    # ... implementation with PTM_PATTERNS matching
    pass
```

**Criterio de éxito:** Dado el mismo `P12931` (c-Src), el NeSy grammar offline debe contener:
- ✓ `[DOM:SH3]`, `[DOM:SH2]`, `[DOM:Kinase_Pkinase]`
- ✓ `{Y-P}` para sitios de fosforilación conocidos
- ✓ `(ATP)` para el sitio de unión a ATP

#### 1.2 `generator_unified.py` (Skeleton)

```python
"""
LMP Unified Generator (v4.0)
============================

Preset-based generator that unifies:
- v3's deterministic offline generation
- v2's NeSy semantic richness
- SMIC's MD-IFP fingerprints
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import xml.etree.ElementTree as ET

from .nesy_encoder import LMPNeSyEncoder, NeSyAnnotation
from .nesy_offline_mapper import map_uniprot_to_nesy
from .presets import PRESET_REGISTRY, LMPPreset


@dataclass(frozen=True)
class LMPInput:
    """Input data for LMP generation."""
    accession: str
    entry: Dict[str, Any]           # UniProt JSON
    meta: Optional[Dict] = None
    pdb_path: Optional[Path] = None
    trajectory_path: Optional[Path] = None
    topology_path: Optional[Path] = None


class LMPUnifiedGenerator:
    """
    Generate LMP XML documents with configurable presets.
    
    Example:
        generator = LMPUnifiedGenerator()
        
        # Load local snapshot
        inp = generator.load_snapshot("./snapshots/P12931")
        
        # Generate with specific preset
        xml = generator.generate(inp, preset="nesy-core")
        
        # Or generate all presets
        for preset_name, xml in generator.generate_all_presets(inp):
            Path(f"P12931_{preset_name}.xml").write_text(xml)
    """
    
    NS = "http://ai-university.edu/lmp/v4.0"
    
    def __init__(
        self,
        xsd_path: Optional[Path] = None,
        validate: bool = True,
    ):
        self.xsd_path = xsd_path or (Path(__file__).parent / "lmp_v4_schema.xsd")
        self.validate = validate
        self._nesy_encoder = LMPNeSyEncoder()
        self._xsd_schema = self._load_xsd() if validate else None
    
    def load_snapshot(self, snapshot_dir: Path) -> LMPInput:
        """Load UniProt snapshot from directory."""
        snapshot_dir = Path(snapshot_dir)
        entry_path = snapshot_dir / "entry.json.gz"
        meta_path = snapshot_dir / "meta.json"
        
        entry = self._read_gz_json(entry_path)
        meta = self._read_json(meta_path) if meta_path.exists() else None
        
        acc = (meta or {}).get("accession") or entry.get("primaryAccession")
        
        return LMPInput(accession=acc, entry=entry, meta=meta)
    
    def generate(
        self,
        inp: LMPInput,
        preset: str = "full",
    ) -> str:
        """Generate LMP XML for given preset."""
        preset_config = PRESET_REGISTRY.get(preset)
        if not preset_config:
            raise ValueError(f"Unknown preset: {preset}")
        
        root = self._create_root()
        
        # Always include Identity
        self._add_identity(root, inp)
        
        # Conditional blocks based on preset
        if preset_config.include_nesy_grammar or preset_config.include_semantics:
            self._add_semantics(root, inp, preset_config)
        
        if preset_config.include_geometry or preset_config.include_features:
            self._add_geometry(root, inp, preset_config)
        
        if preset_config.include_trajectory_ifp and inp.trajectory_path:
            self._add_trajectory_ifp(root, inp)
        
        if preset_config.include_knowledge_graph:
            self._add_knowledge_graph(root, inp)
        
        if preset_config.include_provenance:
            self._add_provenance(root, inp, preset_config)
        
        return self._serialize(root)
    
    def _add_semantics(
        self,
        root: ET.Element,
        inp: LMPInput,
        preset: LMPPreset,
    ) -> None:
        """Add Semantics block, including NeSy grammar if enabled."""
        sem = ET.SubElement(root, f"{{{self.NS}}}Semantics")
        
        # Basic semantics (from v3)
        self._add_protein_name(sem, inp)
        self._add_genes(sem, inp)
        self._add_keywords(sem, inp)
        
        # NeSy Grammar (the key addition)
        if preset.include_nesy_grammar:
            annotation = map_uniprot_to_nesy(inp.entry)
            nesy_str = self._nesy_encoder.encode(annotation)
            
            ng = ET.SubElement(sem, f"{{{self.NS}}}NeSyGrammar")
            ng.set("version", "2.0")
            ng.set("length", str(len(annotation.sequence)))
            ng.text = nesy_str
    
    # ... rest of implementation
```

#### 1.3 XSD Extension

```xml
<!-- In lmp_v4_schema.xsd -->

<xs:complexType name="SemanticsType">
  <xs:sequence>
    <xs:element name="ProteinName" type="xs:string" minOccurs="0"/>
    <xs:element name="Genes" type="lmp:StringListType" minOccurs="0"/>
    <xs:element name="Keywords" type="lmp:StringListType" minOccurs="0"/>
    
    <!-- NEW: NeSy Grammar block -->
    <xs:element name="NeSyGrammar" type="lmp:NeSyGrammarType" minOccurs="0"/>
    
    <xs:element name="Comment" type="lmp:TypedTextType" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence>
</xs:complexType>

<xs:complexType name="NeSyGrammarType">
  <xs:simpleContent>
    <xs:extension base="xs:string">
      <xs:attribute name="version" type="xs:string" default="2.0"/>
      <xs:attribute name="length" type="xs:nonNegativeInteger"/>
    </xs:extension>
  </xs:simpleContent>
</xs:complexType>
```

### Tests Phase 1

```python
# tests/test_nesy_offline.py

def test_mapper_extracts_domains():
    """Verify domain extraction from UniProt JSON."""
    entry = load_test_entry("P12931")  # c-Src
    annotation = map_uniprot_to_nesy(entry)
    
    domain_names = {d["name"] for d in annotation.domains}
    assert "SH3" in domain_names or any("SH3" in d["name"] for d in annotation.domains)
    assert "SH2" in domain_names or any("SH2" in d["name"] for d in annotation.domains)

def test_nesy_grammar_contains_markers():
    """Verify NeSy output contains expected markers."""
    entry = load_test_entry("P12931")
    annotation = map_uniprot_to_nesy(entry)
    nesy_str = LMPNeSyEncoder().encode(annotation)
    
    assert "[DOM:" in nesy_str
    assert "[/DOM]" in nesy_str

def test_coverage_vs_v2():
    """Compare offline NeSy coverage against v2 online."""
    # Load v2-generated XML (pre-generated reference)
    v2_nesy = extract_nesy_from_v2_xml("P12931_reference.xml")
    
    # Generate offline
    entry = load_test_entry("P12931")
    annotation = map_uniprot_to_nesy(entry)
    v4_nesy = LMPNeSyEncoder().encode(annotation)
    
    # Compare marker counts
    v2_markers = count_nesy_markers(v2_nesy)
    v4_markers = count_nesy_markers(v4_nesy)
    
    coverage = len(v4_markers & v2_markers) / len(v2_markers)
    assert coverage > 0.90, f"Coverage {coverage:.1%} below 90% threshold"
```

---

## Phase 2: IFP → LMP Bridge (Dynamics Layer)

### Objetivo
Integrar los fingerprints de dinámica molecular (IFP) como bloque XML nativo en LMP.

### Duración Estimada
5-7 días

### Entregables

| # | Archivo | Descripción |
|---|---------|-------------|
| 2.1 | `ifp_xml_bridge.py` | `IFPTrajectoryResult` → XML serializer |
| 2.2 | `lmp_v4_schema.xsd` | Extensión con `<TrajectoryIFP>` |
| 2.3 | `generator_unified.py` | Método `_add_trajectory_ifp()` |
| 2.4 | `tests/test_ifp_bridge.py` | Suite de tests |

### Implementación Detallada

#### 2.1 `ifp_xml_bridge.py`

```python
"""
Bridge between SMIC IFP Engine and LMP XML format.

Converts IFPTrajectoryResult to XML elements for embedding in
<Geometry><TrajectoryIFP> blocks.
"""

from typing import Optional
import xml.etree.ElementTree as ET

# Import from SMIC (relative path adjustment needed)
import sys
sys.path.insert(0, str(Path(__file__).parents[3] / "workers/smic/python"))
from smic_core.ifp_engine import IFPTrajectoryResult, IFPFrameResult, IFPContact


class IFPXMLBridge:
    """Convert IFP results to LMP XML format."""
    
    def __init__(self, ns: str = "http://ai-university.edu/lmp/v4.0"):
        self.ns = ns
    
    def trajectory_to_xml(
        self,
        result: IFPTrajectoryResult,
        parent: ET.Element,
        *,
        max_frames: int = 500,
        stride: int = 1,
        min_occupancy: float = 0.1,
    ) -> ET.Element:
        """
        Convert IFPTrajectoryResult to <TrajectoryIFP> XML element.
        
        Args:
            result: IFP analysis result
            parent: Parent XML element (usually <Geometry>)
            max_frames: Maximum frames to include (for size control)
            stride: Frame stride for output
            min_occupancy: Minimum occupancy to include in summary
            
        Returns:
            The created TrajectoryIFP element
        """
        ns = self.ns
        
        # Root element
        traj = ET.SubElement(parent, f"{{{ns}}}TrajectoryIFP")
        traj.set("frames", str(result.n_frames))
        traj.set("stride", str(stride))
        
        # Metadata
        meta = ET.SubElement(traj, f"{{{ns}}}Metadata")
        ET.SubElement(meta, f"{{{ns}}}Receptor").text = result.receptor_name
        ET.SubElement(meta, f"{{{ns}}}Ligand").text = result.ligand_name
        ET.SubElement(meta, f"{{{ns}}}TotalFrames").text = str(result.n_frames)
        
        if result.time_ps is not None and len(result.time_ps) > 0:
            total_ns = result.time_ps[-1] / 1000.0
            ET.SubElement(meta, f"{{{ns}}}TotalTime_ns").text = f"{total_ns:.2f}"
        
        # Frame data (respect limits)
        frames_elem = ET.SubElement(traj, f"{{{ns}}}Frames")
        for i, frame_result in enumerate(result.frame_results[::stride]):
            if i >= max_frames:
                break
            self._frame_to_xml(frame_result, frames_elem)
        
        # Occupancy summary
        occ_elem = ET.SubElement(traj, f"{{{ns}}}Occupancy")
        for key, occ in sorted(result.contact_occupancy.items(), 
                                key=lambda x: -x[1]):
            if occ < min_occupancy:
                continue
            resid, lig_resid, ifp_type = key
            c = ET.SubElement(occ_elem, f"{{{ns}}}Contact")
            c.set("type", ifp_type)
            c.set("receptorResid", str(resid))
            c.set("ligandResid", str(lig_resid))
            c.set("occupancy", f"{occ:.3f}")
        
        return traj
    
    def _frame_to_xml(
        self,
        frame: IFPFrameResult,
        parent: ET.Element,
    ) -> ET.Element:
        """Convert single frame to XML."""
        ns = self.ns
        
        f = ET.SubElement(parent, f"{{{ns}}}Frame")
        f.set("index", str(frame.frame))
        f.set("time_ps", f"{frame.time_ps:.1f}")
        f.set("n_contacts", str(frame.total_ifp_count))
        
        # Active IFP types as attribute
        if frame.active_ifps:
            f.set("active", ",".join(sorted(set(frame.active_ifps))))
        
        # Individual contacts
        for contact in frame.contacts:
            c = ET.SubElement(f, f"{{{ns}}}Contact")
            c.set("type", contact.ifp_type)
            c.set("receptor", f"{contact.receptor_resname}{contact.receptor_resid}")
            c.set("ligand", f"{contact.ligand_resname}{contact.ligand_resid}")
            c.set("distance", f"{contact.distance:.2f}")
            
            # Extra metadata if available
            if contact.metadata:
                if "stacking_type" in contact.metadata:
                    c.set("stacking", contact.metadata["stacking_type"])
        
        return f
```

#### 2.2 XSD Extension

```xml
<!-- TrajectoryIFP block in lmp_v4_schema.xsd -->

<xs:complexType name="TrajectoryIFPType">
  <xs:sequence>
    <xs:element name="Metadata" type="lmp:IFPMetadataType"/>
    <xs:element name="Frames" type="lmp:IFPFramesType" minOccurs="0"/>
    <xs:element name="Occupancy" type="lmp:IFPOccupancyType" minOccurs="0"/>
  </xs:sequence>
  <xs:attribute name="frames" type="xs:nonNegativeInteger" use="required"/>
  <xs:attribute name="stride" type="xs:nonNegativeInteger" default="1"/>
</xs:complexType>

<xs:complexType name="IFPMetadataType">
  <xs:sequence>
    <xs:element name="Receptor" type="xs:string"/>
    <xs:element name="Ligand" type="xs:string"/>
    <xs:element name="TotalFrames" type="xs:nonNegativeInteger" minOccurs="0"/>
    <xs:element name="TotalTime_ns" type="xs:decimal" minOccurs="0"/>
  </xs:sequence>
</xs:complexType>

<xs:complexType name="IFPFramesType">
  <xs:sequence>
    <xs:element name="Frame" type="lmp:IFPFrameType" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence>
</xs:complexType>

<xs:complexType name="IFPFrameType">
  <xs:sequence>
    <xs:element name="Contact" type="lmp:IFPContactType" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence>
  <xs:attribute name="index" type="xs:nonNegativeInteger" use="required"/>
  <xs:attribute name="time_ps" type="xs:decimal" use="required"/>
  <xs:attribute name="n_contacts" type="xs:nonNegativeInteger"/>
  <xs:attribute name="active" type="xs:string"/>
</xs:complexType>

<xs:complexType name="IFPContactType">
  <xs:attribute name="type" type="xs:string" use="required"/>
  <xs:attribute name="receptor" type="xs:string" use="required"/>
  <xs:attribute name="ligand" type="xs:string" use="required"/>
  <xs:attribute name="distance" type="xs:decimal" use="required"/>
  <xs:attribute name="stacking" type="xs:string"/>
</xs:complexType>
```

### Tests Phase 2

```python
# tests/test_ifp_bridge.py

def test_trajectory_to_xml_structure():
    """Verify XML structure from IFP result."""
    result = create_mock_ifp_result(n_frames=100)
    
    root = ET.Element("Geometry")
    bridge = IFPXMLBridge()
    bridge.trajectory_to_xml(result, root)
    
    traj = root.find("{http://ai-university.edu/lmp/v4.0}TrajectoryIFP")
    assert traj is not None
    assert traj.get("frames") == "100"
    
    frames = traj.find("{http://ai-university.edu/lmp/v4.0}Frames")
    assert len(frames) > 0

def test_performance_1000_frames():
    """Verify 1000 frame trajectory processes in < 10s."""
    import time
    
    result = create_mock_ifp_result(n_frames=1000, contacts_per_frame=50)
    
    start = time.perf_counter()
    root = ET.Element("Geometry")
    bridge = IFPXMLBridge()
    bridge.trajectory_to_xml(result, root, max_frames=1000)
    elapsed = time.perf_counter() - start
    
    assert elapsed < 10.0, f"Took {elapsed:.1f}s, expected < 10s"

def test_xsd_validation():
    """Verify generated XML validates against schema."""
    result = create_mock_ifp_result(n_frames=50)
    
    generator = LMPUnifiedGenerator(validate=True)
    inp = create_test_input_with_trajectory()
    
    # Should not raise
    xml = generator.generate(inp, preset="md-ifp")
    assert "<TrajectoryIFP" in xml
```

---

## Phase 3: Presets + GCS + Finetune (Integration Layer)

### Objetivo
Completar el sistema end-to-end: presets configurables, output a GCS, y exportadores de finetuning actualizados.

### Duración Estimada
5-7 días

### Entregables

| # | Archivo | Descripción |
|---|---------|-------------|
| 3.1 | `presets.py` | Definiciones de los 5 presets |
| 3.2 | `generator_unified.py` | GCS upload integration |
| 3.3 | `finetune/export_plm_labels.py` | Soporte NeSy tokens |
| 3.4 | `finetune/export_llm_jsonl.py` | Soporte presets |
| 3.5 | E2E integration test | Pipeline completo |

### Implementación Detallada

#### 3.1 `presets.py`

```python
"""
LMP Preset Definitions

Each preset controls which blocks are included in generated XML.
Presets are optimized for different consumers:
- PLMs: sequence + tokens (minimal)
- LLMs: semantic context (readable)
- MD pipelines: trajectory data (numerical)
- Archives: everything (complete)
"""

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class LMPPreset:
    """Configuration for LMP generation."""
    name: str
    description: str
    
    # Block inclusion flags
    include_identity: bool = True
    include_nesy_grammar: bool = False
    include_semantics: bool = False
    include_geometry: bool = False
    include_features: bool = False
    include_knowledge_graph: bool = False
    include_trajectory_ifp: bool = False
    include_provenance: bool = True
    embed_ground_truth: bool = False
    
    # Size limits
    max_ifp_frames: int = 500
    ifp_stride: int = 1
    ifp_min_occupancy: float = 0.1


PRESET_REGISTRY: Dict[str, LMPPreset] = {
    
    "nesy-core": LMPPreset(
        name="nesy-core",
        description="Minimal: Identity + NeSy grammar for PLM tokenization",
        include_nesy_grammar=True,
    ),
    
    "semantic": LMPPreset(
        name="semantic",
        description="Semantic context for LLM injection",
        include_semantics=True,
        include_knowledge_graph=True,
    ),
    
    "structural": LMPPreset(
        name="structural",
        description="Geometry + features for structural analysis",
        include_geometry=True,
        include_features=True,
    ),
    
    "md-ifp": LMPPreset(
        name="md-ifp",
        description="MD trajectory IFP fingerprints",
        include_geometry=True,
        include_trajectory_ifp=True,
        max_ifp_frames=1000,
    ),
    
    "full": LMPPreset(
        name="full",
        description="Complete archive with all blocks",
        include_nesy_grammar=True,
        include_semantics=True,
        include_geometry=True,
        include_features=True,
        include_knowledge_graph=True,
        include_trajectory_ifp=True,
        embed_ground_truth=True,
        max_ifp_frames=500,
    ),
}


def get_preset(name: str) -> LMPPreset:
    """Get preset by name, with validation."""
    if name not in PRESET_REGISTRY:
        valid = ", ".join(PRESET_REGISTRY.keys())
        raise ValueError(f"Unknown preset '{name}'. Valid: {valid}")
    return PRESET_REGISTRY[name]


def preset_for_consumer(consumer: str) -> LMPPreset:
    """Get recommended preset for a given consumer type."""
    consumer_map = {
        "plm": "nesy-core",
        "esm2": "nesy-core",
        "prott5": "nesy-core",
        "llm": "semantic",
        "gpt": "semantic",
        "claude": "semantic",
        "md": "md-ifp",
        "dynamics": "md-ifp",
        "archive": "full",
        "complete": "full",
    }
    preset_name = consumer_map.get(consumer.lower(), "full")
    return PRESET_REGISTRY[preset_name]
```

#### 3.2 GCS Integration

```python
# In generator_unified.py

from mica.infrastructure.storage.user_storage_manager import UserStorageManager


class LMPUnifiedGenerator:
    # ... existing code ...
    
    def __init__(
        self,
        xsd_path: Optional[Path] = None,
        validate: bool = True,
        storage_manager: Optional[UserStorageManager] = None,
    ):
        # ... existing init ...
        self._storage = storage_manager
    
    async def generate_to_gcs(
        self,
        inp: LMPInput,
        preset: str,
        user_id: str,
        *,
        bucket_prefix: str = "lmp",
    ) -> str:
        """
        Generate LMP XML and upload to user's GCS bucket.
        
        Returns:
            GCS URI of uploaded file
        """
        if self._storage is None:
            raise RuntimeError("Storage manager not configured")
        
        # Generate XML
        xml_str = self.generate(inp, preset)
        
        # Build path
        filename = f"preset_{preset}.xml"
        gcs_path = f"{bucket_prefix}/{inp.accession}/{filename}"
        
        # Get or create user bucket
        bucket = await self._storage.get_or_create_bucket(user_id)
        
        # Upload
        uri = await self._storage.upload_string(
            bucket, 
            gcs_path, 
            xml_str,
            content_type="application/xml",
        )
        
        return uri
    
    async def generate_all_to_gcs(
        self,
        inp: LMPInput,
        user_id: str,
        presets: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """
        Generate all specified presets and upload to GCS.
        
        Returns:
            Dict mapping preset name to GCS URI
        """
        if presets is None:
            presets = list(PRESET_REGISTRY.keys())
        
        results = {}
        for preset_name in presets:
            # Skip md-ifp if no trajectory
            if preset_name == "md-ifp" and not inp.trajectory_path:
                continue
            
            uri = await self.generate_to_gcs(inp, preset_name, user_id)
            results[preset_name] = uri
        
        return results
```

#### 3.3 Finetune Exporter Updates

```python
# In finetune/export_plm_labels.py, add NeSy token extraction

def extract_nesy_vocabulary(nesy_grammar: str) -> Dict[str, List[str]]:
    """
    Extract structured vocabulary from NeSy grammar.
    
    Returns dict with keys: domains, ptms, sites, states
    """
    vocab = {
        "domains": [],
        "ptms": [],
        "sites": [],
        "states": [],
    }
    
    # Domain markers: [DOM:name]
    vocab["domains"] = re.findall(r'\[DOM:([^\]]+)\]', nesy_grammar)
    
    # PTM markers: {residue-type:enzyme}
    vocab["ptms"] = re.findall(r'\{([^}]+)\}', nesy_grammar)
    
    # Binding sites: (TYPE) or (TYPE:param)
    vocab["sites"] = re.findall(r'\(([A-Z]+(?::[^)]+)?)\)', nesy_grammar)
    
    # State markers: *STATE*
    vocab["states"] = re.findall(r'\*([A-Z-]+)\*', nesy_grammar)
    
    return vocab


def generate_plm_tokens_from_preset(
    xml_str: str,
    preset: str = "nesy-core",
) -> List[str]:
    """
    Generate PLM-compatible token sequence from LMP XML.
    
    For preset=nesy-core, extracts NeSy grammar and tokenizes.
    """
    root = ET.fromstring(xml_str)
    ns = {"lmp": "http://ai-university.edu/lmp/v4.0"}
    
    nesy_elem = root.find(".//lmp:NeSyGrammar", ns)
    if nesy_elem is None or not nesy_elem.text:
        # Fallback to raw sequence
        seq_elem = root.find(".//lmp:Sequence", ns)
        return list(seq_elem.text) if seq_elem is not None else []
    
    nesy_grammar = nesy_elem.text
    
    # Tokenize: split by markers while preserving them
    tokens = []
    current_pos = 0
    
    # Pattern to match all NeSy markers
    marker_pattern = re.compile(
        r'(\[[^\]]+\]|\{[^}]+\}|\([^)]+\)|\*[^*]+\*|<[^>]+>)'
    )
    
    for match in marker_pattern.finditer(nesy_grammar):
        # Add residues before this marker
        if match.start() > current_pos:
            residues = nesy_grammar[current_pos:match.start()]
            tokens.extend(list(residues))
        
        # Add marker as single token
        tokens.append(match.group(0))
        current_pos = match.end()
    
    # Add remaining residues
    if current_pos < len(nesy_grammar):
        tokens.extend(list(nesy_grammar[current_pos:]))
    
    return tokens
```

### E2E Integration Test

```python
# tests/test_e2e_lmp_unified.py

import pytest
from pathlib import Path

@pytest.mark.asyncio
async def test_full_pipeline():
    """
    End-to-end test: UniProt snapshot → LMP presets → GCS → Finetune export
    """
    # Setup
    generator = LMPUnifiedGenerator(validate=True)
    test_snapshot = Path("tests/fixtures/snapshots/P12931")
    
    # Step 1: Load snapshot
    inp = generator.load_snapshot(test_snapshot)
    assert inp.accession == "P12931"
    
    # Step 2: Generate all presets
    for preset_name in ["nesy-core", "semantic", "structural", "full"]:
        xml = generator.generate(inp, preset=preset_name)
        
        # Validate XML structure
        root = ET.fromstring(xml)
        assert root.get("version") == "4.0"
        
        # Preset-specific checks
        if preset_name == "nesy-core":
            nesy = root.find(".//{http://ai-university.edu/lmp/v4.0}NeSyGrammar")
            assert nesy is not None
            assert "[DOM:" in nesy.text
        
        if preset_name == "semantic":
            keywords = root.find(".//{http://ai-university.edu/lmp/v4.0}Keywords")
            assert keywords is not None
    
    # Step 3: Finetune export
    xml_nesy = generator.generate(inp, preset="nesy-core")
    tokens = generate_plm_tokens_from_preset(xml_nesy)
    
    # Verify tokens include both residues and markers
    assert len(tokens) > 100  # Has residues
    assert any(t.startswith("[DOM:") for t in tokens)  # Has domain markers
    
    print(f"✓ Pipeline complete: {len(tokens)} tokens generated")


@pytest.mark.asyncio
async def test_gcs_upload(mock_storage):
    """Test GCS upload integration."""
    generator = LMPUnifiedGenerator(
        validate=True,
        storage_manager=mock_storage,
    )
    
    inp = generator.load_snapshot("tests/fixtures/snapshots/P12931")
    
    uri = await generator.generate_to_gcs(
        inp,
        preset="nesy-core",
        user_id="test_user_123",
    )
    
    assert uri.startswith("gs://mica-md-test_user_123/")
    assert "P12931" in uri
    assert "preset_nesy-core.xml" in uri
```

---

## Timeline Summary

```
Week 1: Phase 1 (NeSy Offline)
├── Day 1-2: nesy_offline_mapper.py
├── Day 3-4: generator_unified.py skeleton
├── Day 5: XSD extension
└── Day 6-7: Tests + coverage audit

Week 2: Phase 2 (IFP Bridge)
├── Day 1-2: ifp_xml_bridge.py
├── Day 3: XSD TrajectoryIFP
├── Day 4-5: Integration with SMIC
└── Day 6-7: Performance tests

Week 3: Phase 3 (Integration)
├── Day 1-2: presets.py + router
├── Day 3-4: GCS integration
├── Day 5: Finetune exporter updates
└── Day 6-7: E2E tests + documentation
```

---

## Riesgos y Mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|--------|--------------|---------|------------|
| NeSy mapper no cubre todos los feature types | Media | Alto | Audit exhaustivo de UniProt feature types |
| IFP performance con 10k frames | Baja | Medio | Stride + max_frames configurable |
| GCS latency en upload | Baja | Bajo | Async + batching |
| XSD breaking changes | Media | Alto | Versioned namespaces (v4.0) |

---

## Criterios de Éxito (Definition of Done)

### Phase 1 Complete When:
- [ ] `map_uniprot_to_nesy()` handles top-20 feature types
- [ ] NeSy grammar coverage vs v2 ≥ 90%
- [ ] All tests pass with `pytest tests/test_nesy_offline.py`
- [ ] XSD validates generated XML

### Phase 2 Complete When:
- [ ] `IFPXMLBridge` converts real trajectory data
- [ ] 1000-frame trajectory processes in < 10s
- [ ] XSD validates TrajectoryIFP blocks
- [ ] Integration with SMIC bridge works

### Phase 3 Complete When:
- [ ] All 5 presets generate valid XML
- [ ] GCS upload works with UserStorageManager
- [ ] Finetune exporters handle NeSy tokens
- [ ] E2E test passes: snapshot → LMP → GCS → export

---

## Próximo Paso

**Empezar Phase 1.1:** Crear `nesy_offline_mapper.py` con el mapeo de UniProt features a `NeSyAnnotation`.

¿Procedo?

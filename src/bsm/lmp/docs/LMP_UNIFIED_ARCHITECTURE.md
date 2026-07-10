# LMP Unified Architecture — Documentación Técnica Completa

**Versión:** 4.0-alpha  
**Fecha:** 2026-01-20  
**Autores:** MICA Team  

---

## 1. Visión General

### 1.1 El Problema

Actualmente existen **tres pipelines fragmentados** para representar proteínas:

| Pipeline | Fortaleza | Debilidad |
|----------|-----------|-----------|
| **LMP v2** | NeSy completo, semántica biológica rica | Requiere red, no determinista |
| **LMP v3** | Reproducible, offline, escalable | Sin NeSy, plano semánticamente |
| **SMIC IFP** | MD-IFP production-ready | Aislado, no emite XML/LMP |

### 1.2 La Solución: LMP Unified

Un **generador unificado con presets** que:

1. **Fuentes offline-first**: UniProt JSON local, PDB local, trayectorias MD
2. **Presets modulares**: Cada consumidor (PLM, LLM, MD) obtiene exactamente lo que necesita
3. **NeSy determinista**: Gramática neuro-simbólica calculada offline del JSON
4. **IFP integrado**: Fingerprints de MD como bloque XML nativo
5. **Cloud-native**: Output directo a GCS buckets por usuario

```
┌─────────────────────────────────────────────────────────────────────┐
│                     LMP UNIFIED GENERATOR (v4.0)                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │
│  │ UniProt     │  │ PDB         │  │ MD Traj     │   DATA SOURCES  │
│  │ JSON.gz     │  │ .cif/.pdb   │  │ .dcd/.xtc   │                 │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                 │
│         │                │                │                         │
│         ▼                ▼                ▼                         │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                   CORE PROCESSORS                         │      │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────────────────┐│      │
│  │  │ NeSy       │ │ Geometry   │ │ IFP Engine            ││      │
│  │  │ Encoder    │ │ Mapper     │ │ (MDAnalysis)          ││      │
│  │  │ (offline)  │ │ (PDB→XML)  │ │ (trajectory→contacts) ││      │
│  │  └────────────┘ └────────────┘ └────────────────────────┘│      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                    PRESET ROUTER                          │      │
│  │                                                           │      │
│  │  preset=nesy-core  → Identity + NeSyGrammar              │      │
│  │  preset=semantic   → Identity + Semantics + Keywords      │      │
│  │  preset=structural → Geometry + PDB xrefs + Features      │      │
│  │  preset=md-ifp     → TrajectoryIFP (contacts per frame)   │      │
│  │  preset=full       → All blocks combined                  │      │
│  │                                                           │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                    OUTPUT LAYER                           │      │
│  │                                                           │      │
│  │  Local: ./output/{accession}/preset_{name}.xml           │      │
│  │  GCS:   gs://mica-md-{user}/lmp/{accession}/...          │      │
│  │                                                           │      │
│  └──────────────────────────────────────────────────────────┘      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Componentes del Sistema

### 2.1 Data Sources (Fuentes de Datos)

#### 2.1.1 UniProt Snapshot (Offline)

**Ubicación actual:** `scanner_v3.py` ya descarga y almacena:
- `entry.json.gz` — JSON completo de UniProtKB
- `meta.json` — Metadata del snapshot

**Campos clave para NeSy offline:**
```python
entry = {
    "primaryAccession": "P12931",
    "sequence": {"value": "MGSNK...", "length": 536},
    "features": [
        {"type": "Domain", "location": {...}, "description": "SH3"},
        {"type": "Modified residue", "location": {...}, "description": "Phosphoserine"},
        {"type": "Binding site", "location": {...}, "ligand": {"name": "ATP"}},
    ],
    "keywords": [{"name": "Kinase"}, {"name": "Phosphoprotein"}],
    "comments": [...],
    "uniProtKBCrossReferences": [{"database": "PDB", "id": "2SRC"}, ...],
}
```

#### 2.1.2 PDB Structure (Offline)

**Formatos soportados:** `.pdb`, `.cif`, `.mmcif`

**Información extraíble:**
- Coordenadas atómicas
- Cadenas y entidades
- Ligandos (non-polymer entities)
- Secondary structure (HELIX/SHEET records)

#### 2.1.3 MD Trajectory

**Formatos soportados:** `.dcd`, `.xtc`, `.trr` (via MDAnalysis)

**Topology:** `.pdb`, `.psf`, `.gro`

**Procesamiento:** IFP Engine existente en `workers/smic/python/smic_core/ifp_engine.py`

---

### 2.2 Core Processors

#### 2.2.1 NeSy Encoder (Offline Mode)

**Archivo:** `src/bsm/lmp/nesy_encoder.py`

**Clase principal:** `LMPNeSyEncoder`

**Input:** `NeSyAnnotation` dataclass
```python
@dataclass
class NeSyAnnotation:
    sequence: str
    domains: List[Dict]      # {name, type, start, end}
    motifs: List[Dict]       # {name, type, start, end}
    ptms: List[Dict]         # {position, type, residue, enzyme}
    binding_sites: List[Dict] # {type, residues, ion_type, ligand}
    ppi_interfaces: List[Dict]
    conformational_state: Optional[str]
    state_regions: List[Dict]
```

**Output:** NeSy-encoded string
```
MGSN...[DOM:SH3]...[/DOM]...[DOM:Kinase_Pkinase]...
(ATP)M...{S-P:PKA}...L...E(/ATP)...*DFG-IN*...
(CAT)D...F...G(/CAT)...[/DOM]...
```

**Clave para v4:** Este encoder **NO requiere red**. Solo necesita:
1. Secuencia (del JSON)
2. Features mapeados a dominios/PTMs/sitios (del JSON)

El mapeo `UniProt features → NeSyAnnotation` es la pieza que falta y se implementará en Phase 1.

#### 2.2.2 IFP Engine

**Archivo:** `workers/smic/python/smic_core/ifp_engine.py`

**Clase principal:** `IFPEngine`

**Tipos de interacción detectados (MD-IFP compatible):**

| Código | Tipo | Threshold |
|--------|------|-----------|
| AR | π-stacking, cation-π | 5.5 Å |
| HY | Hydrophobic | 4.0 Å |
| HD | H-bond (receptor→ligand) | 3.5 Å |
| HA | H-bond (ligand→receptor) | 3.5 Å |
| WB | Water bridge | 3.5 Å |
| IP | Salt bridge (+) | 4.5 Å |
| IN | Salt bridge (-) | 4.5 Å |
| HL | Halogen bond | 3.5 Å |

**Output:** `IFPTrajectoryResult`
```python
@dataclass
class IFPTrajectoryResult:
    n_frames: int
    frame_results: List[IFPFrameResult]
    ifp_matrix: pd.DataFrame
    contact_occupancy: Dict[Tuple, float]
```

**Referencia:** Kokh et al., J. Chem. Phys. 153, 125102 (2020) — MD-IFP

---

### 2.3 Preset System

#### 2.3.1 Preset Registry

```python
@dataclass
class LMPPreset:
    """Configuration for what to include in generated XML."""
    name: str
    include_identity: bool = True
    include_nesy_grammar: bool = False
    include_semantics: bool = False
    include_geometry: bool = False
    include_features: bool = False
    include_knowledge_graph: bool = False
    include_trajectory_ifp: bool = False
    include_provenance: bool = True
    embed_ground_truth: bool = False

PRESET_REGISTRY = {
    "nesy-core": LMPPreset(
        name="nesy-core",
        include_nesy_grammar=True,
        # Minimal: just Identity + NeSy string for PLM tokenization
    ),
    "semantic": LMPPreset(
        name="semantic",
        include_semantics=True,
        include_knowledge_graph=True,
        # For LLM context injection
    ),
    "structural": LMPPreset(
        name="structural",
        include_geometry=True,
        include_features=True,
        # For structural analysis / PDB-focused workflows
    ),
    "md-ifp": LMPPreset(
        name="md-ifp",
        include_trajectory_ifp=True,
        # For MD analysis / time-series ML
    ),
    "full": LMPPreset(
        name="full",
        include_nesy_grammar=True,
        include_semantics=True,
        include_geometry=True,
        include_features=True,
        include_knowledge_graph=True,
        include_trajectory_ifp=True,
        embed_ground_truth=True,
        # Complete archive format
    ),
}
```

#### 2.3.2 Preset Selection Logic

```python
def select_preset(
    *,
    consumer: str = None,  # "plm", "llm", "md", "archive"
    preset: str = None,    # explicit preset name
) -> LMPPreset:
    if preset:
        return PRESET_REGISTRY[preset]
    
    consumer_map = {
        "plm": "nesy-core",
        "llm": "semantic",
        "md": "md-ifp",
        "archive": "full",
    }
    return PRESET_REGISTRY[consumer_map.get(consumer, "full")]
```

---

### 2.4 XML Schema Extensions (v4)

#### 2.4.1 New Block: `<NeSyGrammar>`

```xml
<Semantics>
  <ProteinName>Proto-oncogene tyrosine-protein kinase Src</ProteinName>
  <Genes><Value>SRC</Value></Genes>
  
  <!-- NEW: NeSy encoded sequence -->
  <NeSyGrammar version="2.0" length="536">
    MGSN...[DOM:SH3]...[/DOM]...[DOM:Kinase_Pkinase]...
    (ATP)M...{S-P:PKA}...L...E(/ATP)...*DFG-IN*...
  </NeSyGrammar>
</Semantics>
```

#### 2.4.2 New Block: `<TrajectoryIFP>`

```xml
<Geometry>
  <Sequence length="536">MGSN...</Sequence>
  
  <!-- NEW: IFP from MD trajectory -->
  <TrajectoryIFP frames="1000" stride="5" dt_ps="2.0">
    <Metadata>
      <Receptor>chainid A</Receptor>
      <Ligand>resname LIG</Ligand>
      <TotalTime_ns>10.0</TotalTime_ns>
    </Metadata>
    
    <Frame index="0" time_ps="0.0">
      <Contact type="HD" receptor="ASP381" ligand="LIG1" distance="2.81"/>
      <Contact type="AR" receptor="PHE382" ligand="LIG1" distance="4.21" stacking="parallel"/>
      <Contact type="HY" receptor="LEU393" ligand="LIG1" distance="3.92"/>
    </Frame>
    
    <Frame index="5" time_ps="10.0">
      <!-- ... contacts for this frame ... -->
    </Frame>
    
    <!-- Summary statistics -->
    <Occupancy>
      <Contact type="HD" receptor="ASP381" occupancy="0.87"/>
      <Contact type="AR" receptor="PHE382" occupancy="0.63"/>
    </Occupancy>
  </TrajectoryIFP>
</Geometry>
```

---

### 2.5 GCS Integration

#### 2.5.1 User Storage Structure

```
gs://mica-md-{user_id}/
├── input/                    # User uploads
│   ├── proteins/
│   │   └── {accession}/
│   │       ├── entry.json.gz     # UniProt snapshot
│   │       └── meta.json
│   ├── structures/
│   │   └── {pdb_id}.pdb
│   └── trajectories/
│       └── {job_id}/
│           ├── topology.pdb
│           └── trajectory.dcd
│
├── lmp/                      # Generated LMP documents
│   └── {accession}/
│       ├── preset_nesy-core.xml
│       ├── preset_semantic.xml
│       ├── preset_structural.xml
│       ├── preset_md-ifp.xml
│       └── preset_full.xml
│
├── finetune/                 # Training data exports
│   ├── plm/
│   │   ├── sequences.fasta
│   │   ├── ptm_labels.txt
│   │   └── domain_bio.txt
│   └── llm/
│       ├── train.jsonl
│       ├── val.jsonl
│       └── test.jsonl
│
└── output/                   # MD simulation outputs
    └── {job_id}/
        └── ...
```

#### 2.5.2 Upload API

```python
class LMPUnifiedGenerator:
    async def generate_to_gcs(
        self,
        accession: str,
        preset: str,
        user_id: str,
        *,
        snapshot_dir: Path = None,
        pdb_path: Path = None,
        trajectory_path: Path = None,
    ) -> str:
        """Generate and upload to user's GCS bucket."""
        
        # Generate XML
        xml_str = self.generate(accession, preset, ...)
        
        # Upload to GCS
        gcs_path = f"gs://mica-md-{user_id}/lmp/{accession}/preset_{preset}.xml"
        await self.storage_manager.upload_string(gcs_path, xml_str)
        
        return gcs_path
```

---

## 3. Cableado: Cómo Conectar los Componentes

### 3.1 UniProt JSON → NeSyAnnotation

```python
def uniprot_json_to_nesy_annotation(entry: dict) -> NeSyAnnotation:
    """Map UniProt JSON to NeSyAnnotation for offline NeSy encoding."""
    
    sequence = entry.get("sequence", {}).get("value", "")
    
    # Map features to domains/motifs/PTMs
    domains = []
    motifs = []
    ptms = []
    binding_sites = []
    
    for feat in entry.get("features", []):
        ftype = feat.get("type", "")
        loc = feat.get("location", {})
        start = loc.get("start", {}).get("value")
        end = loc.get("end", {}).get("value")
        desc = feat.get("description", "")
        
        if ftype == "Domain":
            domains.append({
                "name": desc or ftype,
                "type": _classify_domain(desc),
                "start": start,
                "end": end,
            })
        elif ftype == "Motif":
            motifs.append({
                "name": desc,
                "type": "default",
                "start": start,
                "end": end,
            })
        elif ftype == "Modified residue":
            ptm_type, enzyme = _parse_ptm_description(desc)
            ptms.append({
                "position": start,
                "type": ptm_type,
                "residue": sequence[start-1] if start else "?",
                "enzyme": enzyme,
            })
        elif ftype in ("Binding site", "Active site", "Nucleotide binding"):
            binding_sites.append({
                "type": _classify_binding_site(ftype, desc),
                "residues": list(range(start, end+1)) if start and end else [],
            })
    
    return NeSyAnnotation(
        sequence=sequence,
        domains=domains,
        motifs=motifs,
        ptms=ptms,
        binding_sites=binding_sites,
        ppi_interfaces=[],  # Would need external data
        conformational_state=None,
        state_regions=[],
    )
```

### 3.2 IFPTrajectoryResult → XML

```python
def ifp_result_to_xml(
    result: IFPTrajectoryResult,
    parent: ET.Element,
    ns: str,
    *,
    max_frames: int = 1000,
    stride: int = 1,
) -> ET.Element:
    """Convert IFP result to TrajectoryIFP XML element."""
    
    traj_elem = ET.SubElement(parent, f"{{{ns}}}TrajectoryIFP")
    traj_elem.set("frames", str(result.n_frames))
    traj_elem.set("stride", str(stride))
    
    # Metadata
    meta = ET.SubElement(traj_elem, f"{{{ns}}}Metadata")
    ET.SubElement(meta, f"{{{ns}}}Receptor").text = result.receptor_name
    ET.SubElement(meta, f"{{{ns}}}Ligand").text = result.ligand_name
    
    # Frames (respect stride and max_frames)
    for i, frame_result in enumerate(result.frame_results[::stride][:max_frames]):
        frame_elem = ET.SubElement(traj_elem, f"{{{ns}}}Frame")
        frame_elem.set("index", str(frame_result.frame))
        frame_elem.set("time_ps", f"{frame_result.time_ps:.1f}")
        
        for contact in frame_result.contacts:
            c_elem = ET.SubElement(frame_elem, f"{{{ns}}}Contact")
            c_elem.set("type", contact.ifp_type)
            c_elem.set("receptor", f"{contact.receptor_resname}{contact.receptor_resid}")
            c_elem.set("ligand", f"{contact.ligand_resname}{contact.ligand_resid}")
            c_elem.set("distance", f"{contact.distance:.2f}")
    
    # Occupancy summary
    occ_elem = ET.SubElement(traj_elem, f"{{{ns}}}Occupancy")
    for key, occ in result.contact_occupancy.items():
        if occ > 0.1:  # Only include significant contacts
            resid, lig_resid, ifp_type = key
            c_elem = ET.SubElement(occ_elem, f"{{{ns}}}Contact")
            c_elem.set("type", ifp_type)
            c_elem.set("receptor", str(resid))
            c_elem.set("occupancy", f"{occ:.2f}")
    
    return traj_elem
```

### 3.3 Finetune Pipeline Integration

```python
# In export_plm_labels.py, add support for NeSy tokens

def extract_nesy_tokens(nesy_grammar: str) -> List[str]:
    """Extract structural tokens from NeSy grammar for PLM vocabulary."""
    tokens = []
    
    # Domain markers
    tokens.extend(re.findall(r'\[DOM:[^\]]+\]', nesy_grammar))
    
    # PTM markers
    tokens.extend(re.findall(r'\{[^}]+\}', nesy_grammar))
    
    # Binding site markers
    tokens.extend(re.findall(r'\([A-Z]+(?::[^)]+)?\)', nesy_grammar))
    
    # State markers
    tokens.extend(re.findall(r'\*[A-Z-]+\*', nesy_grammar))
    
    return tokens
```

---

## 4. Referencias Científicas

### 4.1 Interaction Fingerprints

- **MD-IFP:** Kokh et al., J. Chem. Phys. 153, 125102 (2020)
  - "The MD-IFP is a python workflow for the generation and analysis of protein-ligand interaction fingerprints from Molecular Dynamics trajectories."
  - Source: HITS-MCM/MD-IFP (GitHub)

- **IFP for ML:** "Machine learning models using protein-ligand interaction fingerprints show promise as target-specific scoring functions in drug discovery."
  - MDPI Biomolecules 2024

- **InterMap:** "Accelerated Detection of Interaction Fingerprints on Large MD Trajectories"
  - bioRxiv 2025

### 4.2 Protein Language Models

- **ESM-2 Fine-tuning for PTMs:** "ESM-2, a deep learning model, is designed to understand the 'language' of proteins... QLoRA for PTM site prediction."
  - HuggingFace blog (AmelieSchreiber)

- **Token Classification:** "We fine-tuned ESM2 and ProtT5 to predict diverse protein features at amino acid resolution, using a token classification setup."
  - ScienceDirect 2025

- **Structural Context:** "Protein stability prediction by fine-tuning a protein language model."
  - PMC 2024

### 4.3 Neuro-Symbolic AI

- **NeSy for KGs:** "Neurosymbolic AI is an increasingly active area of research that combines symbolic reasoning methods with deep learning."
  - arXiv:2302.07200

- **Protein Function:** "The impact of incomplete knowledge on the evaluation of protein function prediction: a structured-output learning perspective."
  - Edinburgh pure.ed.ac.uk

- **KG + LLM:** "Knowledge Graphs are used to guide deep models, while offering a path toward grounding symbols and inducing knowledge from low-level data."
  - AllegroGraph

---

## 5. Métricas de Éxito

| Métrica | Target | Medición |
|---------|--------|----------|
| Tiempo de generación (preset=nesy-core) | < 100ms | Benchmark local |
| Tiempo de generación (preset=full + 1000 frames) | < 10s | Benchmark local |
| Cobertura NeSy offline vs v2 online | > 90% | Audit de features mapeados |
| Validación XSD | 100% | pytest suite |
| Compatibilidad con finetune exporters | 100% | Integration tests |
| Upload GCS (10 MB XML) | < 5s | Network benchmark |

---

## 6. Apéndice: Archivos Clave

```
src/bsm/lmp/
├── generator_unified.py      # NEW: Main unified generator
├── nesy_encoder.py           # Existing: NeSy encoding logic
├── nesy_offline_mapper.py    # NEW: UniProt JSON → NeSyAnnotation
├── ifp_xml_bridge.py         # NEW: IFPResult → XML
├── presets.py                # NEW: Preset definitions
├── lmp_v4_schema.xsd         # NEW: Extended schema
├── generator_v3.py           # Keep: Base deterministic logic
├── generator.py              # Keep: v2 reference implementation
└── finetune/
    ├── export_plm_labels.py  # Extend: NeSy token support
    └── export_llm_jsonl.py   # Extend: Preset-aware export

workers/smic/python/smic_core/
├── ifp_engine.py             # Existing: IFP generation
├── smic_ifp_bridge.py        # Existing: Tier-aware bridge
└── lmp_ifp_adapter.py        # NEW: Bridge to LMP

src/mica/infrastructure/storage/
└── user_storage_manager.py   # Extend: LMP output paths
```

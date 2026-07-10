# BLUEPRINT — LMP Full Structural Mode + AlphaFold Integration + MD Analysis Enrichment

Date: 2026-04-02
Status: SPEC — awaiting review
Document ID: MICA-SPEC-LMP-STRUCT-v1.0

**Companion documents:**
- `src/bsm/lmp/docs/LMP_UNIFIED_ARCHITECTURE.md`
- `src/bsm/lmp/docs/LMP_SMIC_COMPLEX_EXPANSION_PROPOSAL.md`
- `src/bsm/lmp/docs/LMP_UNIFIED_ROADMAP.md`
- `workers/smic/python/smic_core/md_analisys/docs/API_BLUEPRINT.md`

> **Scope:** Evolucionar LMP de un protocolo centrado en secuencia+NeSy hacia un **protocolo full-structural** 
> que integre nativamente: (1) metadatos reales de AlphaFold DB, (2) métricas estructurales de SMIC md_analysis 
> (DSSP, RMSD/RMSF, contactos, network analysis), (3) confianza por residuo (pLDDT/PAE), y 
> (4) alineación completa de los presets `structural` y `full` con esta visión.
>
> **Purpose:** Blueprint ejecutable con 6 fases, gap analysis completo, non-regression tests, y rubric benchmark
> para medir progreso iterativo según la skill `iterative-benchmark-improvement`.

---

## Table of Contents

1. [Executive Gap Analysis](#1-executive-gap-analysis)
2. [AlphaFold API Integration Design](#2-alphafold-api-integration-design)
3. [SMIC md_analysis Bridge for Static PDB Metrics](#3-smic-md_analysis-bridge-for-static-pdb-metrics)
4. [Schema Evolution (XSD v4.1)](#4-schema-evolution-xsd-v41)
5. [Preset Alignment — `structural` + `full`](#5-preset-alignment--structural--full)
6. [Implementation Phases (6 Sprints)](#6-implementation-phases-6-sprints)
7. [Benchmark Rubric & Iteration Framework](#7-benchmark-rubric--iteration-framework)
8. [Non-Regression Test Matrix](#8-non-regression-test-matrix)
9. [Files to Modify / Create](#9-files-to-modify--create)
10. [Residual Gaps & Future Seeds](#10-residual-gaps--future-seeds)

---

## 1. Executive Gap Analysis

### 1.1 Current State (Source Audit)

| Componente | Archivo | Estado Actual | Gap Confirmado |
|------------|---------|---------------|----------------|
| **AlphaFold client** | *(no existe)* | Solo mencionado en `context_extractor.py#L280-288` como tool suggestion heurística | **GAP-AF-1**: No existe cliente AlphaFold. No se descarga mmCIF/PDB predicho. No se extrae pLDDT/PAE. |
| **DSSP / Secondary Structure** | *(no existe en LMP)* | SMIC tiene `analysis_dssp.py` funcional pero no conectado a LMP. `smic/ml/bridge.py#L282-283` tiene `# TODO: Integrate DSSP` | **GAP-SS-1**: No hay secondary structure en XML. Ni desde PDB experimental ni AlphaFold. |
| **pLDDT / Confidence per-residue** | *(no existe)* | AlphaFold API devuelve `confidenceVersion`, `pdbUrl`, `cifUrl` con B-factors=pLDDT pero no se consume | **GAP-CONF-1**: Cero métricas de confianza estructural. El preset `structural` no sabe si la geometría es confiable. |
| **PAE (Predicted Aligned Error)** | *(no existe)* | AlphaFold API endpoint devuelve `paeDocUrl` pero no se consume | **GAP-PAE-1**: Sin PAE matrix. Crucial para evaluar confianza en interfaces de dominio. |
| **Geometric metrics** | `pdb_metrics.py` | Solo COM, Rg, bounding box. Per-chain solamente. | **GAP-GEO-1**: Falta: solvent accessibility (ASA), B-factor stats, clash score, Ramachandran stats. |
| **Contact map / Q-native** | *(no existe en LMP)* | SMIC `analysis_contacts.py` lo calcula pero no se puente | **GAP-CONT-1**: No hay contact map estático en XML. |
| **Network centrality** | *(no existe en LMP)* | SMIC `analysis_network.py` calcula betweenness/closeness/eigenvector pero no se puente | **GAP-NET-1**: No hay hub residues ni allosteric pathway metadata. |
| **AlphaFold ↔ UniProt mapping** | *(no existe)* | LMP usa UniProt accession como key. AlphaFold API usa UniProt accession. Mapping trivial pero no implementado. | **GAP-MAP-1**: Sin resolver: multiple AlphaFold fragments per long UniProt entry. |
| **Preset `structural`** | `presets.py#L68-72` | Solo flags `include_geometry=True, include_features=True`. Sin ninguna metadata estructural real. | **GAP-PRES-1**: `structural` es un preset vacío de estructura real. Solo tiene features UniProt. |
| **Preset `full`** | `presets.py#L104-116` | Incluye todo pero hereda la pobreza del structural. | **GAP-PRES-2**: `full` promete "complete archive" pero no tiene un solo dato de estructura 3D real. |
| **PAS Annotators coverage** | `pas_annotators.py` | Solo `KinasePASAnnotator` implementado. GPCR, NuclearReceptor, Protease → "PLANNED" | **GAP-PAS-1**: Cobertura familiar limitada a kinases. |

### 1.2 Gap Priority Matrix (Largest Absolute Gap First)

| Gap ID | Sub-score Impact | Headroom | Priority | Strategy |
|--------|-----------------|----------|----------|----------|
| **GAP-AF-1** | Structure fidelity: 0/2.5 | 2.5 | **P0 — CRITICAL** | AlphaFold client + metadata extraction |
| **GAP-SS-1** | Structure annotation: 0/2.5 | 2.5 | **P0 — CRITICAL** | DSSP from PDB + secondary structure XML |
| **GAP-CONF-1** | Confidence quality: 0/2.5 | 2.5 | **P0 — CRITICAL** | pLDDT per-residue from B-factor column |
| **GAP-PRES-1** | Preset completeness: 0.5/2.5 | 2.0 | **P1 — HIGH** | Wire structural pipeline into preset |
| **GAP-GEO-1** | Geometry depth: 0.8/2.5 | 1.7 | **P1 — HIGH** | Extend pdb_metrics with ASA, Ramachandran |
| **GAP-PAE-1** | Domain confidence: 0/2.5 | 2.5 | **P1 — HIGH** | PAE matrix download + domain-level confidence |
| **GAP-CONT-1** | Contact annotation: 0/2.5 | 2.5 | **P2 — MEDIUM** | Static contact map from SMIC bridge |
| **GAP-NET-1** | Allosteric metadata: 0/2.5 | 2.5 | **P2 — MEDIUM** | Hub residues from network analysis |
| **GAP-MAP-1** | Multi-fragment: 0/1.0 | 1.0 | **P3 — LOW** | Fragment stitching for long sequences |
| **GAP-PAS-1** | Family coverage: 0.3/2.5 | 2.2 | **P3 — LOW** | New PAS annotators (out of scope here) |

---

## 2. AlphaFold API Integration Design

### 2.1 AlphaFold DB API — Confirmed Endpoints

**Base URL:** `https://alphafold.ebi.ac.uk/api`

| Endpoint | Method | Input | Output | For LMP |
|----------|--------|-------|--------|---------|
| `/prediction/{uniprot_accession}` | GET | UniProt accession (e.g., P00520) | JSON array of model metadata | Model ID, PDB URL, CIF URL, PAE URL, confidence version, gene, organism |
| `/annotations/{qualifier}.json` | GET | UniProt range (e.g., P00520:1-200) | JSON annotations per residue | Per-residue pLDDT, disorder, secondary structure |
| `/uniprot/summary/{qualifier}.json` | GET | UniProt accession | Summary JSON | Quick quality overview, entry date |

### 2.2 AlphaFold Response Schema (from `/prediction/{accession}`)

```json
[{
  "entryId": "AF-P00520-F1",
  "gene": "ABL1",
  "uniprotAccession": "P00520",
  "uniprotId": "ABL1_MOUSE",
  "uniprotDescription": "Tyrosine-protein kinase ABL1",
  "taxId": 10090,
  "organismScientificName": "Mus musculus",
  "uniprotStart": 1,
  "uniprotEnd": 1123,
  "uniprotSequence": "MLEICLKLVG...",
  "modelCreatedDate": "2022-06-01",
  "latestVersion": 4,
  "allVersions": [1, 2, 3, 4],
  "isReviewed": true,
  "isReferenceProteome": true,
  "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-P00520-F1-model_v4.cif",
  "bcifUrl": "https://alphafold.ebi.ac.uk/files/AF-P00520-F1-model_v4.bcif",
  "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-P00520-F1-model_v4.pdb",
  "paeDocUrl": "https://alphafold.ebi.ac.uk/files/AF-P00520-F1-predicted_aligned_error_v4.json",
  "confidenceVersion": 4,
  "confidenceAvgLocalScore": 78.52
}]
```

### 2.3 Proposed Module: `alphafold_client.py`

```python
"""
AlphaFold DB API Client for LMP structural enrichment.

Fetches: metadata, PDB/mmCIF files, PAE matrices, per-residue pLDDT.
Cache-first: all downloads cached locally with configurable TTL.
Graceful degradation: if AlphaFold is down, LMP generates without structure.

Endpoints used (documented at https://alphafold.ebi.ac.uk/api-docs):
  - GET /api/prediction/{uniprot_accession}  → model metadata + file URLs
  - GET /api/annotations/{qualifier}.json    → per-residue annotations
  - File downloads: pdbUrl, cifUrl, paeDocUrl from prediction response
"""

@dataclass
class AlphaFoldModelMeta:
    entry_id: str                    # "AF-P00520-F1"
    uniprot_accession: str           # "P00520"
    gene: Optional[str]              # "ABL1"
    organism: Optional[str]          # "Mus musculus"
    model_version: int               # 4
    confidence_avg_plddt: float      # 78.52
    confidence_version: int          # 4
    uniprot_start: int               # 1
    uniprot_end: int                 # 1123
    pdb_url: str
    cif_url: str
    pae_url: Optional[str]
    model_created_date: str

@dataclass  
class AlphaFoldStructure:
    meta: AlphaFoldModelMeta
    pdb_path: Optional[Path]          # Local cached PDB
    cif_path: Optional[Path]          # Local cached CIF
    pae_matrix: Optional[List[List[float]]]  # N×N PAE
    plddt_per_residue: Optional[List[float]] # Per-residue pLDDT from B-factor
    secondary_structure: Optional[List[str]] # DSSP assignments (H/E/C per residue)
    
class AlphaFoldClient:
    BASE_URL = "https://alphafold.ebi.ac.uk/api"
    
    def __init__(self, cache_dir: Path, ttl_seconds: int = 86400 * 30):
        ...
    
    def fetch_prediction(self, uniprot_accession: str) -> List[AlphaFoldModelMeta]:
        """GET /prediction/{accession} → list of AlphaFoldModelMeta"""
        ...
    
    def fetch_annotations(self, uniprot_accession: str) -> Dict:
        """GET /annotations/{accession}.json → per-residue data"""
        ...
    
    def download_structure(self, meta: AlphaFoldModelMeta) -> AlphaFoldStructure:
        """Download PDB + PAE + extract pLDDT from B-factor column."""
        ...
    
    def get_structure_for_accession(self, accession: str) -> Optional[AlphaFoldStructure]:
        """Full pipeline: fetch meta → download best model → extract metrics."""
        ...
    
    def extract_plddt_from_pdb(self, pdb_path: Path) -> List[float]:
        """Parse B-factor column from AlphaFold PDB (B-factor = pLDDT)."""
        ...
    
    def parse_pae_json(self, pae_path: Path) -> List[List[float]]:
        """Parse PAE JSON into NxN matrix."""
        ...
```

### 2.4 pLDDT Extraction Strategy

AlphaFold stores pLDDT in the **B-factor column** of PDB files. Each Cα has its pLDDT as B-factor.

```python
def extract_plddt_from_pdb(pdb_path: Path) -> List[Tuple[int, str, float]]:
    """Returns list of (residue_number, residue_name, pLDDT_score)."""
    plddt = []
    seen = set()
    with open(pdb_path) as f:
        for line in f:
            if line.startswith("ATOM") and line[12:16].strip() == "CA":
                resid = int(line[22:26].strip())
                if resid not in seen:
                    seen.add(resid)
                    resname = line[17:20].strip()
                    bfactor = float(line[60:66].strip())
                    plddt.append((resid, resname, bfactor))
    return plddt
```

### 2.5 PAE Matrix — Domain Confidence

PAE (Predicted Aligned Error) tells us how confident AlphaFold is about the **relative position** of two residues. 
- Low PAE between domain A and domain B → confident in their interface
- High PAE → domains might be independently predicted (not reliable as complex)

**Use in LMP:** Generate `<DomainConfidence>` elements with inter-domain PAE averages.

```json
// AlphaFold PAE JSON format:
[{
  "predicted_aligned_error": [[0.5, 1.2, ...], [1.2, 0.4, ...], ...],
  "max_predicted_aligned_error": 31.75
}]
```

---

## 3. SMIC md_analysis Bridge for Static PDB Metrics

### 3.1 Available SMIC Capabilities for Static PDB Analysis

The SMIC md_analysis suite already has **production-grade** modules that can analyze a single PDB 
(not just trajectories). These capabilities are immediately available for structural enrichment:

| Module | Static PDB Use | Output for LMP |
|--------|---------------|----------------|
| **analysis_dssp.py** | Yes (single frame = static PDB) | Per-residue secondary structure: H (helix), E (strand), C (coil) |
| **analysis_contacts.py** | Yes (contact map from single frame) | Residue-residue contact pairs, Cβ distance matrix |
| **analysis_rmsd.py** | Limited (needs reference, but Rg yes) | Radius of gyration, per-residue B-factor-like flexibility |
| **analysis_network.py** | Yes (from contact matrix) | Betweenness centrality, closeness, hub residues, shortest paths |
| **analysis_contact_density.py** | Yes (per-residue count) | Contact density per residue (correlated with core/surface) |
| **analysis_pocket_volume.py** | Yes (ConvexHull from single frame) | Binding pocket volume in ų |

### 3.2 Proposed Module: `structural_metrics.py`

```python
"""
Structural metrics bridge to SMIC md_analysis.

Computes static structural features from a single PDB file:
- DSSP secondary structure per residue
- Contact map (Cβ-Cβ, 8Å cutoff)  
- Per-residue solvent accessibility (ASA)
- Network centrality (hub residues)
- Extended geometry (Rg, asphericity, eccentricity)
- Ramachandran regions (favored/allowed/outlier counts)

Designed for single-frame (static) analysis of:
- Experimental PDB structures
- AlphaFold predicted models  
- Representative MD frames (cluster medoids)
"""

@dataclass
class SecondaryStructureAssignment:
    residue_id: int
    residue_name: str
    chain_id: str
    dssp_code: str       # H, E, C, G, I, T, S, B
    dssp_simplified: str  # H (helix), E (strand), C (coil)

@dataclass
class ContactMapEntry:
    residue_i: int
    residue_j: int
    chain_i: str
    chain_j: str
    distance: float
    contact_type: str    # intra-chain, inter-chain, domain-interface

@dataclass
class ResidueAccessibility:
    residue_id: int
    residue_name: str
    asa_absolute: float  # Å²
    asa_relative: float  # fraction of max ASA for this residue type
    classification: str  # buried (<25%), partially-exposed (25-50%), exposed (>50%)

@dataclass
class HubResidue:
    residue_id: int
    chain_id: str
    betweenness_centrality: float
    degree_centrality: float
    closeness_centrality: float
    is_allosteric_candidate: bool  # Top 10% betweenness

@dataclass
class StructuralMetrics:
    """Complete structural metrics for a PDB file."""
    source: str                                    # "experimental" | "alphafold" | "md_frame"
    secondary_structure: List[SecondaryStructureAssignment]
    ss_composition: Dict[str, float]               # {"helix": 0.45, "strand": 0.20, "coil": 0.35}
    contact_map: List[ContactMapEntry]
    accessibility: List[ResidueAccessibility]
    hub_residues: List[HubResidue]
    geometry: Dict[str, Any]                       # Rg, asphericity, eccentricity, volume
    ramachandran: Dict[str, int]                   # {"favored": 450, "allowed": 30, "outlier": 2}
    plddt_per_residue: Optional[List[float]]       # Only for AlphaFold models
    pae_matrix: Optional[List[List[float]]]        # Only for AlphaFold models
    domain_confidence: Optional[Dict[str, float]]  # Inter-domain PAE averages

class StructuralMetricsComputer:
    def compute_from_pdb(self, pdb_path: Path, *, source: str = "experimental") -> StructuralMetrics:
        ...
    def compute_dssp(self, pdb_path: Path) -> List[SecondaryStructureAssignment]:
        ...
    def compute_contact_map(self, pdb_path: Path, cutoff: float = 8.0) -> List[ContactMapEntry]:
        ...
    def compute_accessibility(self, pdb_path: Path) -> List[ResidueAccessibility]:
        ...
    def compute_network_centrality(self, contact_map: List[ContactMapEntry]) -> List[HubResidue]:
        ...
    def compute_ramachandran(self, pdb_path: Path) -> Dict[str, int]:
        ...
```

### 3.3 DSSP Integration Detail

```python
# Using MDTraj (already a dependency via SMIC):
import mdtraj as md

traj = md.load(str(pdb_path))
dssp = md.compute_dssp(traj, simplified=True)  # Returns array of 'H', 'E', 'C'

# Mapping:
# H → helix (α-helix, 3₁₀-helix, π-helix)
# E → strand (β-sheet)
# C → coil (everything else: turns, bends, loops)
```

---

## 4. Schema Evolution (XSD v4.1)

### 4.1 New Schema Elements

```xml
<!-- NEW: AlphaFold metadata block inside Geometry -->
<xs:complexType name="AlphaFoldModelType">
  <xs:sequence>
    <xs:element name="ConfidencePerResidue" minOccurs="0">
      <xs:complexType>
        <xs:sequence>
          <xs:element name="Residue" maxOccurs="unbounded">
            <xs:complexType>
              <xs:attribute name="id" type="xs:nonNegativeInteger" use="required"/>
              <xs:attribute name="name" type="xs:string" use="optional"/>
              <xs:attribute name="pLDDT" type="xs:float" use="required"/>
              <xs:attribute name="confidence_class" type="xs:string" use="optional"/>
              <!-- confidence_class: "very_high" (>90), "confident" (70-90), 
                   "low" (50-70), "very_low" (<50) -->
            </xs:complexType>
          </xs:element>
        </xs:sequence>
      </xs:complexType>
    </xs:element>
    <xs:element name="PAESummary" minOccurs="0">
      <xs:complexType>
        <xs:attribute name="mean_pae" type="xs:float" use="optional"/>
        <xs:attribute name="max_pae" type="xs:float" use="optional"/>
        <xs:element name="DomainPair" minOccurs="0" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="domain_a" type="xs:string" use="required"/>
            <xs:attribute name="domain_b" type="xs:string" use="required"/>
            <xs:attribute name="mean_pae" type="xs:float" use="required"/>
            <xs:attribute name="confident_interface" type="xs:boolean" use="optional"/>
          </xs:complexType>
        </xs:element>
      </xs:complexType>
    </xs:element>
  </xs:sequence>
  <xs:attribute name="entry_id" type="xs:string" use="required"/>
  <xs:attribute name="version" type="xs:nonNegativeInteger" use="required"/>
  <xs:attribute name="avg_plddt" type="xs:float" use="optional"/>
  <xs:attribute name="model_date" type="xs:string" use="optional"/>
  <xs:attribute name="uniprot_start" type="xs:nonNegativeInteger" use="optional"/>
  <xs:attribute name="uniprot_end" type="xs:nonNegativeInteger" use="optional"/>
</xs:complexType>

<!-- NEW: Secondary structure block inside Geometry -->
<xs:complexType name="SecondaryStructureType">
  <xs:sequence>
    <xs:element name="Segment" maxOccurs="unbounded">
      <xs:complexType>
        <xs:attribute name="type" type="xs:string" use="required"/>
        <!-- "helix", "strand", "coil" -->
        <xs:attribute name="start" type="xs:nonNegativeInteger" use="required"/>
        <xs:attribute name="end" type="xs:nonNegativeInteger" use="required"/>
        <xs:attribute name="chain" type="xs:string" use="optional"/>
        <xs:attribute name="length" type="xs:nonNegativeInteger" use="optional"/>
      </xs:complexType>
    </xs:element>
  </xs:sequence>
  <xs:attribute name="method" type="xs:string" use="optional"/>
  <!-- "dssp", "stride", "alphafold_annotation" -->
  <xs:attribute name="helix_fraction" type="xs:float" use="optional"/>
  <xs:attribute name="strand_fraction" type="xs:float" use="optional"/>
  <xs:attribute name="coil_fraction" type="xs:float" use="optional"/>
</xs:complexType>

<!-- NEW: Structural quality summary -->
<xs:complexType name="StructuralQualityType">
  <xs:sequence>
    <xs:element name="Rg" minOccurs="0">
      <xs:complexType>
        <xs:attribute name="value" type="xs:float" use="required"/>
        <xs:attribute name="unit" type="xs:string" default="angstrom"/>
      </xs:complexType>
    </xs:element>
    <xs:element name="Ramachandran" minOccurs="0">
      <xs:complexType>
        <xs:attribute name="favored" type="xs:nonNegativeInteger"/>
        <xs:attribute name="allowed" type="xs:nonNegativeInteger"/>
        <xs:attribute name="outlier" type="xs:nonNegativeInteger"/>
        <xs:attribute name="favored_pct" type="xs:float"/>
      </xs:complexType>
    </xs:element>
    <xs:element name="ContactDensity" minOccurs="0">
      <xs:complexType>
        <xs:attribute name="total_contacts" type="xs:nonNegativeInteger"/>
        <xs:attribute name="contacts_per_residue" type="xs:float"/>
      </xs:complexType>
    </xs:element>
  </xs:sequence>
  <xs:attribute name="source" type="xs:string" use="optional"/>
  <!-- "experimental", "alphafold", "md_representative" -->
</xs:complexType>

<!-- NEW: Hub residues / allosteric annotation -->
<xs:complexType name="NetworkAnnotationType">
  <xs:sequence>
    <xs:element name="Hub" minOccurs="0" maxOccurs="unbounded">
      <xs:complexType>
        <xs:attribute name="residue_id" type="xs:nonNegativeInteger" use="required"/>
        <xs:attribute name="chain" type="xs:string" use="optional"/>
        <xs:attribute name="betweenness" type="xs:float" use="required"/>
        <xs:attribute name="degree" type="xs:float" use="optional"/>
        <xs:attribute name="allosteric_candidate" type="xs:boolean" use="optional"/>
      </xs:complexType>
    </xs:element>
  </xs:sequence>
</xs:complexType>

<!-- EXTENDED GeometryType to include new blocks -->
<xs:complexType name="GeometryType">
  <xs:sequence>
    <xs:element name="Sequence" type="lmp:SequenceType" minOccurs="0"/>
    <xs:element name="Feature" type="lmp:FeatureType" minOccurs="0" maxOccurs="unbounded"/>
    <xs:element name="Chain" type="lmp:ChainType" minOccurs="0" maxOccurs="unbounded"/>
    <!-- NEW structural blocks -->
    <xs:element name="AlphaFoldModel" type="lmp:AlphaFoldModelType" minOccurs="0"/>
    <xs:element name="SecondaryStructure" type="lmp:SecondaryStructureType" minOccurs="0"/>
    <xs:element name="StructuralQuality" type="lmp:StructuralQualityType" minOccurs="0"/>
    <xs:element name="NetworkAnnotation" type="lmp:NetworkAnnotationType" minOccurs="0"/>
    <!-- Existing MD blocks -->
    <xs:element name="TrajectoryIFP" type="lmp:TrajectoryIFPType" minOccurs="0"/>
  </xs:sequence>
</xs:complexType>
```

### 4.2 Example XML Output (preset=structural)

```xml
<lmp:LMP version="4.0" preset="structural" xmlns:lmp="http://ai-university.edu/lmp/v4.0">
  <lmp:Identity>
    <lmp:BudoID>budo:P00520-S</lmp:BudoID>
    <lmp:PrimaryAccession>P00520</lmp:PrimaryAccession>
    <lmp:UniProtKBId>ABL1_MOUSE</lmp:UniProtKBId>
    <lmp:Organism id="10090">Mus musculus</lmp:Organism>
  </lmp:Identity>
  
  <lmp:Geometry>
    <lmp:Sequence length="1123">MLEICLKLVG...</lmp:Sequence>
    
    <lmp:AlphaFoldModel entry_id="AF-P00520-F1" version="4" avg_plddt="78.52"
                        model_date="2022-06-01" uniprot_start="1" uniprot_end="1123">
      <lmp:ConfidencePerResidue>
        <lmp:Residue id="1" name="M" pLDDT="45.2" confidence_class="low"/>
        <lmp:Residue id="2" name="L" pLDDT="52.1" confidence_class="low"/>
        <!-- ... -->
        <lmp:Residue id="300" name="K" pLDDT="92.4" confidence_class="very_high"/>
      </lmp:ConfidencePerResidue>
      <lmp:PAESummary mean_pae="8.3" max_pae="31.75">
        <lmp:DomainPair domain_a="SH3" domain_b="SH2" mean_pae="4.2" confident_interface="true"/>
        <lmp:DomainPair domain_a="SH2" domain_b="Kinase" mean_pae="5.1" confident_interface="true"/>
        <lmp:DomainPair domain_a="Kinase" domain_b="C-tail" mean_pae="18.7" confident_interface="false"/>
      </lmp:PAESummary>
    </lmp:AlphaFoldModel>
    
    <lmp:SecondaryStructure method="dssp" helix_fraction="0.38" strand_fraction="0.22" coil_fraction="0.40">
      <lmp:Segment type="coil" start="1" end="15" chain="A" length="15"/>
      <lmp:Segment type="strand" start="16" end="22" chain="A" length="7"/>
      <lmp:Segment type="helix" start="30" end="48" chain="A" length="19"/>
      <!-- ... -->
    </lmp:SecondaryStructure>
    
    <lmp:StructuralQuality source="alphafold">
      <lmp:Rg value="32.5" unit="angstrom"/>
      <lmp:Ramachandran favored="980" allowed="35" outlier="3" favored_pct="96.3"/>
      <lmp:ContactDensity total_contacts="4523" contacts_per_residue="4.03"/>
    </lmp:StructuralQuality>
    
    <lmp:NetworkAnnotation>
      <lmp:Hub residue_id="271" chain="A" betweenness="0.73" degree="12" allosteric_candidate="true"/>
      <lmp:Hub residue_id="381" chain="A" betweenness="0.68" degree="10" allosteric_candidate="true"/>
      <lmp:Hub residue_id="412" chain="A" betweenness="0.55" degree="8" allosteric_candidate="false"/>
    </lmp:NetworkAnnotation>
    
    <lmp:Feature type="Domain" start="63" end="118" description="SH3 domain"/>
    <lmp:Feature type="Domain" start="127" end="225" description="SH2 domain"/>
    <lmp:Feature type="Domain" start="242" end="493" description="Protein kinase domain"/>
  </lmp:Geometry>
  
  <lmp:Provenance>
    <lmp:GenerationInfo>
      <lmp:Generator>LMPUnifiedGenerator</lmp:Generator>
      <lmp:GeneratorVersion>4.1.0</lmp:GeneratorVersion>
      <lmp:Preset>structural</lmp:Preset>
      <lmp:StructuralSources>
        <lmp:Source type="alphafold" id="AF-P00520-F1" version="4"/>
        <lmp:Source type="dssp" method="mdtraj"/>
      </lmp:StructuralSources>
    </lmp:GenerationInfo>
  </lmp:Provenance>
</lmp:LMP>
```

---

## 5. Preset Alignment — `structural` + `full`

### 5.1 New Preset Flags

```python
@dataclass(frozen=True)
class LMPPreset:
    # ... existing flags ...
    
    # NEW: Structural enrichment flags
    include_alphafold: bool = False          # Fetch AlphaFold model + pLDDT + PAE
    include_secondary_structure: bool = False # DSSP computation
    include_structural_quality: bool = False  # Rg, Ramachandran, contact density
    include_network_annotation: bool = False  # Hub residues, centrality
    include_contact_map: bool = False         # Static residue-residue contacts
    alphafold_download_pdb: bool = False      # Actually download the PDB file
    alphafold_download_pae: bool = False      # Download PAE matrix
```

### 5.2 Updated Preset Registry

| Preset | `include_alphafold` | `include_secondary_structure` | `include_structural_quality` | `include_network_annotation` |
|--------|--------------------|-----------------------------|-----------------------------|-----------------------------|
| `nesy-core` | ✗ | ✗ | ✗ | ✗ |
| `semantic` | ✗ | ✗ | ✗ | ✗ |
| **`structural`** | **✓** | **✓** | **✓** | **✓** |
| `v2-compat` | ✗ | ✗ | ✗ | ✗ |
| `md-ifp` | ✗ | ✗ | ✗ | ✗ |
| **`full`** | **✓** | **✓** | **✓** | **✓** |
| `plm-esm2` | ✗ | ✗ | ✗ | ✗ |
| `plm-prott5` | ✗ | ✗ | ✗ | ✗ |
| `llm-context` | ✗ | ✓ (summary only) | ✗ | ✗ |

### 5.3 Preset `structural` — Full Wiring

```python
"structural": LMPPreset(
    name="structural",
    description="Full structural mode: AlphaFold + DSSP + quality metrics + network centrality",
    include_geometry=True,
    include_features=True,
    include_alphafold=True,
    include_secondary_structure=True,
    include_structural_quality=True,
    include_network_annotation=True,
    alphafold_download_pdb=True,
    alphafold_download_pae=True,
),
```

### 5.4 Preset `full` — Master Archive

```python
"full": LMPPreset(
    name="full",
    description="Complete archive: all blocks including AlphaFold structural data",
    include_nesy_grammar=True,
    include_semantics=True,
    include_geometry=True,
    include_features=True,
    include_knowledge_graph=True,
    include_trajectory_ifp=True,
    include_provenance=True,
    embed_ground_truth=True,
    include_alphafold=True,
    include_secondary_structure=True,
    include_structural_quality=True,
    include_network_annotation=True,
    alphafold_download_pdb=True,
    alphafold_download_pae=True,
    max_ifp_frames=500,
),
```

---

## 6. Implementation Phases (6 Sprints)

### Sprint 1: AlphaFold Client + Cache (P0)

**Goal:** Reliable AlphaFold API client with caching and graceful degradation.

| # | Deliverable | File | Tests |
|---|------------|------|-------|
| 1.1 | `AlphaFoldClient` class | `alphafold_client.py` (NEW) | `test_alphafold_client.py` |
| 1.2 | Prediction endpoint handler | `alphafold_client.py` | Integration test with P00520 |
| 1.3 | PDB download + cache | `alphafold_client.py` | Cache hit/miss test |
| 1.4 | pLDDT extraction from B-factor | `alphafold_client.py` | Verify known pLDDT values |
| 1.5 | PAE JSON parser | `alphafold_client.py` | Matrix size = sequence length² |
| 1.6 | Graceful degradation (API down) | `alphafold_client.py` | Timeout → None, not crash |

**Acceptance Criteria:**
- `client.get_structure_for_accession("P00520")` returns `AlphaFoldStructure` with pLDDT list
- Cache prevents re-download within TTL
- API timeout returns None (graceful)
- pLDDT values match AlphaFold DB website for known protein

### Sprint 2: Structural Metrics Bridge (P0)

**Goal:** DSSP + contact map + quality metrics from any PDB file.

| # | Deliverable | File | Tests |
|---|------------|------|-------|
| 2.1 | `StructuralMetricsComputer` class | `structural_metrics.py` (NEW) | `test_structural_metrics.py` |
| 2.2 | DSSP secondary structure | `structural_metrics.py` | Known helix/strand counts for 1IEP |
| 2.3 | Contact map (Cβ, 8Å) | `structural_metrics.py` | Non-trivial contacts for multi-domain |
| 2.4 | Solvent accessibility (ASA) | `structural_metrics.py` | Buried core vs exposed surface |
| 2.5 | Network centrality | `structural_metrics.py` | Hub residues identified |
| 2.6 | Ramachandran stats | `structural_metrics.py` | >95% favored for good PDB |

**Acceptance Criteria:**
- DSSP on 1IEP.pdb produces recognizable secondary structure pattern
- Contact map has >100 contacts per chain
- Hub residues include known catalytic residues
- Ramachandran >95% favored for clean experimental PDB

### Sprint 3: Schema Extension (XSD v4.1)

**Goal:** Extend XSD to accommodate new structural blocks without breaking existing presets.

| # | Deliverable | File | Tests |
|---|------------|------|-------|
| 3.1 | `AlphaFoldModel` type in XSD | `lmp_v4_schema.xsd` | Schema validates example XML |
| 3.2 | `SecondaryStructure` type | `lmp_v4_schema.xsd` | Validates DSSP output format |
| 3.3 | `StructuralQuality` type | `lmp_v4_schema.xsd` | Validates quality block |
| 3.4 | `NetworkAnnotation` type | `lmp_v4_schema.xsd` | Validates hub residue block |
| 3.5 | Extend `GeometryType` sequence | `lmp_v4_schema.xsd` | All new elements optional |
| 3.6 | Backward compat verification | `test_v4_all_presets.py` | ALL existing presets still validate |

**Acceptance Criteria:**
- All 8 existing presets generate XML that validates against new XSD
- New structural XML also validates
- No required attributes broken

### Sprint 4: Generator Integration

**Goal:** Wire AlphaFold + metrics into `generator_unified.py` preset pipeline.

| # | Deliverable | File | Tests |
|---|------------|------|-------|
| 4.1 | `_add_alphafold_model()` method | `generator_unified.py` | Emits `<AlphaFoldModel>` |
| 4.2 | `_add_secondary_structure()` | `generator_unified.py` | Emits `<SecondaryStructure>` |
| 4.3 | `_add_structural_quality()` | `generator_unified.py` | Emits `<StructuralQuality>` |
| 4.4 | `_add_network_annotation()` | `generator_unified.py` | Emits `<NetworkAnnotation>` |
| 4.5 | Wire into `generate()` with preset gates | `generator_unified.py` | Only emitted when preset flag is True |
| 4.6 | `LMPInput` extension with structural data | `generator_unified.py` | Accept AlphaFoldStructure + StructuralMetrics |

**Acceptance Criteria:**
- `preset=structural` generates XML with all 4 new blocks
- `preset=nesy-core` generates XML WITHOUT any new blocks  
- `preset=full` includes everything
- All validate against XSD v4.1

### Sprint 5: Preset + Config Update

**Goal:** Update presets, config, and context extractor for structural awareness.

| # | Deliverable | File | Tests |
|---|------------|------|-------|
| 5.1 | New preset flags | `presets.py` | `get_preset("structural").include_alphafold == True` |
| 5.2 | Updated preset definitions | `presets.py` | `structural` and `full` emit structure |
| 5.3 | Config: AlphaFold section | `lmp_config.yaml` | TTL, cache dir, API base URL |
| 5.4 | Context extractor: structural awareness | `context_extractor.py` | Extracts pLDDT, SS from XML |
| 5.5 | `preset_for_consumer()` routing | `presets.py` | "structure" → structural, "alphafold" → structural |

**Acceptance Criteria:**
- `preset_for_consumer("structure")` returns `structural` preset
- Config YAML validated
- Context extractor produces structural summary for driver injection

### Sprint 6: Benchmark Harness + E2E

**Goal:** Build iterative benchmark; run baseline; validate full pipeline.

| # | Deliverable | File | Tests |
|---|------------|------|-------|
| 6.1 | Benchmark corpus (10 diverse proteins) | `benchmark_corpus/` | Kinases, GPCRs, IDPs, enzymes, channels |
| 6.2 | Benchmark harness with 5 sub-scores | `benchmark_structural.py` | Scores 0-10 scale |
| 6.3 | Baseline run (iteration 0) | Output JSON | All metrics recorded |
| 6.4 | E2E test: UniProt → AlphaFold → DSSP → XML | `test_e2e_structural.py` | Full pipeline for P00520 |
| 6.5 | Non-regression suite | `test_non_regression_structural.py` | All existing tests pass + new |

---

## 7. Benchmark Rubric & Iteration Framework

### 7.1 Scoring Rubric (aligned with `iterative-benchmark-improvement` skill)

**Total Score = Sub-A + Sub-B + Sub-C + Sub-D + Sub-E = 0-10**

| Sub-score | Name | Max | Scoring Criteria |
|-----------|------|-----|-----------------|
| **A** | Structure Fidelity | 2.0 | Has AlphaFold model? (+0.5) Has pLDDT? (+0.5) pLDDT >70 avg? (+0.5) Has PAE? (+0.5) |
| **B** | Annotation Depth | 2.0 | Has DSSP? (+0.5) Has contact map? (+0.5) Has accessibility? (+0.5) Has network? (+0.5) |
| **C** | Quality Metrics | 2.0 | Has Rg? (+0.5) Has Ramachandran? (+0.5) Has contact density? (+0.5) Confidence class per residue? (+0.5) |
| **D** | Preset Compliance | 2.0 | `structural` emits all structure blocks? (+1.0) `full` emits everything? (+0.5) No structure in `nesy-core`? (+0.5) |
| **E** | Schema Validity | 2.0 | XSD validates? (+1.0) No empty blocks? (+0.5) Provenance includes structural sources? (+0.5) |

### 7.2 Benchmark Corpus (10 proteins)

| # | UniProt | Gene | Challenge | Why Included |
|---|---------|------|-----------|-------------|
| 01 | P00520 | ABL1 (mouse) | Multi-domain kinase | Reference kinase; known DFG-in/out states |
| 02 | P04637 | TP53 | IDP regions | Large disordered regions (low pLDDT) |
| 03 | P0DTC2 | Spike (SARS-CoV-2) | Huge, multi-fragment | AlphaFold fragment stitching challenge |
| 04 | P68871 | HBB | Small, well-folded | Very high pLDDT; simple baseline |
| 05 | P00533 | EGFR | Kinase + extracellular | Mixed pLDDT domains |
| 06 | Q9Y6K9 | NF-κB | DNA-binding + IDP | Domain interface confidence |
| 07 | P29274 | A2AR | GPCR (membrane) | 7-TM helix challenge for DSSP |
| 08 | P42212 | GFP | β-barrel | Unique fold; high strand content |
| 09 | P00698 | HEWL (Lysozyme) | Small enzyme | Well-characterized; Ramachandran gold standard |
| 10 | P10636 | MAPT (Tau) | Almost fully disordered | Extreme IDP; pLDDT < 50 everywhere |

### 7.3 Baseline Expectation

| Metric | Baseline (current) | Target (post-Sprint 6) |
|--------|-------------------|----------------------|
| Mean score | ~1.0/10 (schema valid + identity) | ≥7.5/10 |
| Sub-A (Structure) | 0.0 | ≥1.5 |
| Sub-B (Annotation) | 0.0 | ≥1.5 |
| Sub-C (Quality) | 0.0 | ≥1.5 |
| Sub-D (Preset) | 0.5 | ≥1.5 |
| Sub-E (Schema) | 0.5 | ≥1.5 |
| Proteins scoring ≥8/10 | 0/10 | ≥6/10 |

---

## 8. Non-Regression Test Matrix

### 8.1 Existing Tests That MUST NOT Break

| Test | File | What It Verifies | Risk |
|------|------|-----------------|------|
| **All-presets generation** | `test_v4_all_presets.py` | All 8 presets generate valid XML | Schema changes could break validation |
| **XSD validation** | `test_v4_all_presets.py::validate_lmp_xml_against_xsd()` | Every preset validates against XSD | Adding required elements would break |
| **IFP integration** | `test_v4_all_presets.py` (IFP section) | `full` and `md-ifp` embed TrajectoryIFP | Geometry rearrangement could break element order |
| **v4 summary** | `summary_v4_test.py` | File counts and IFP presence check | New files shouldn't confuse counter |
| **NeSy offline** | Implicit in all-presets (`nesy-core`) | NeSy grammar generated from JSON | Import changes in generator_unified could break |
| **Parser roundtrip** | `parser.py` (LMPParser class) | v2→BUDO parser handles XML | New elements must be ignored gracefully |
| **Context extractor** | `context_extractor.py` | Extracts context from v4 XML | New elements should be additive, not breaking |
| **BUDO parser** | `budo_parser.py` | LMP XML → BUDO V3 entities | Must skip unknown elements |

### 8.2 New Tests to Create

| Test ID | Test | Verifies | Priority |
|---------|------|----------|----------|
| **NR-01** | `test_existing_presets_still_valid` | All 8 presets validate against new XSD | P0 |
| **NR-02** | `test_structural_preset_has_alphafold` | `structural` includes AlphaFoldModel | P0 |
| **NR-03** | `test_full_preset_has_everything` | `full` includes ALL blocks including structural | P0 |
| **NR-04** | `test_nesy_core_no_structure` | `nesy-core` does NOT include structural blocks | P0 |
| **NR-05** | `test_alphafold_client_cache_hit` | Second fetch returns cached, no HTTP | P0 |
| **NR-06** | `test_alphafold_client_graceful_timeout` | API timeout → None, not crash | P0 |
| **NR-07** | `test_plddt_extraction` | B-factor to pLDDT mapping correct | P0 |
| **NR-08** | `test_pae_matrix_parsing` | PAE JSON → N×N matrix sizes match | P1 |
| **NR-09** | `test_dssp_known_protein` | DSSP on known PDB matches expected SS | P1 |
| **NR-10** | `test_contact_map_nontrivial` | Contact map has >0 contacts | P1 |
| **NR-11** | `test_network_hub_residues` | Hub detection produces ranked list | P1 |
| **NR-12** | `test_schema_backward_compat` | XML from old generator validates in new XSD | P0 |
| **NR-13** | `test_structural_without_mdanalysis` | Graceful fallback if MDAnalysis not available | P1 |
| **NR-14** | `test_idp_protein_low_plddt` | IDP proteins (P10636) handled with low confidence classes | P2 |
| **NR-15** | `test_multi_fragment_alphafold` | Long sequences with multiple AlphaFold fragments | P2 |
| **NR-16** | `test_context_extractor_structural` | Context extractor reads new structural blocks | P1 |
| **NR-17** | `test_budo_parser_ignores_structural` | BUDO parser gracefully skips unknown elements | P1 |
| **NR-18** | `test_domain_confidence_from_pae` | Inter-domain PAE → confidence classification | P2 |
| **NR-19** | `test_ramachandran_quality` | Ramachandran stats computed correctly | P2 |
| **NR-20** | `test_benchmark_harness_sanity` | Benchmark scores known-good protein ≥ 8/10 | P1 |

---

## 9. Files to Modify / Create

### 9.1 Files to Create

| File | Purpose | Sprint |
|------|---------|--------|
| `src/bsm/lmp/alphafold_client.py` | AlphaFold DB API client + cache + pLDDT/PAE extraction | Sprint 1 |
| `src/bsm/lmp/structural_metrics.py` | DSSP + contacts + ASA + network centrality bridge | Sprint 2 |
| `tests/test_alphafold_client.py` | AlphaFold client unit + integration tests | Sprint 1 |
| `tests/test_structural_metrics.py` | Structural metrics tests | Sprint 2 |
| `tests/test_non_regression_structural.py` | Full non-regression suite (NR-01 through NR-20) | Sprint 6 |
| `src/bsm/lmp/benchmark_corpus/` | 10 benchmark proteins (UniProt JSON snapshots) | Sprint 6 |
| `src/bsm/lmp/benchmark_structural.py` | Benchmark harness with 5-dimension scoring | Sprint 6 |

### 9.2 Files to Modify

| File | Change Type | What Changes | Sprint |
|------|------------|-------------|--------|
| `src/bsm/lmp/lmp_v4_schema.xsd` | EXTEND | Add AlphaFoldModel, SecondaryStructure, StructuralQuality, NetworkAnnotation types to GeometryType | Sprint 3 |
| `src/bsm/lmp/presets.py` | EXTEND | Add 6 new flags to LMPPreset; update `structural` and `full` presets | Sprint 5 |
| `src/bsm/lmp/generator_unified.py` | EXTEND | Add `_add_alphafold_model()`, `_add_secondary_structure()`, `_add_structural_quality()`, `_add_network_annotation()` methods; extend `generate()` flow; extend `LMPInput` dataclass | Sprint 4 |
| `src/bsm/lmp/lmp_config.yaml` | EXTEND | Add `alphafold:` section with API URL, cache TTL, timeout | Sprint 5 |
| `src/bsm/lmp/context_extractor.py` | EXTEND | Add structural context extraction (pLDDT summary, SS composition, hub residues) | Sprint 5 |
| `src/bsm/lmp/pdb_metrics.py` | EXTEND | Add ASA, Ramachandran, extended geometry (asphericity, eccentricity) | Sprint 2 |
| `src/bsm/lmp/test_v4_all_presets.py` | EXTEND | Add structural preset validation assertions | Sprint 6 |

---

## 10. Residual Gaps & Future Seeds

### 10.1 Acknowledged Residual Gaps (Not in Scope)

| Gap | Reason Deferred | Future Trigger |
|-----|----------------|----------------|
| **Multi-fragment stitching** | AlphaFold returns multiple fragments for proteins >2700 residues. Current implementation takes first fragment. | When benchmark corpus protein P0DTC2 (Spike) fails |
| **AlphaFold Multimer** | Complex predictions require separate API. Single-chain first. | When complex expansion proposal (LMP_SMIC_COMPLEX) is executed |
| **GPCR/Protease PAS annotators** | Significant domain knowledge required per family | After structural pipeline stabilizes |
| **PAE-guided domain boundary refinement** | PAE clustering could refine UniProt domain annotations | After Sprint 6 if domain confidence scores are low |
| **Structure comparison** (TM-score, GDT) | Requires experimentalrference structure | Useful for MD representative frame comparison |
| **Pocket volume from SMIC** | Requires running ConvexHull on selected residues | When binding site analysis is prioritized |

### 10.2 Seeds for Next Milestone

1. **AlphaFold → ESE link**: Use pLDDT confidence to weight ESE (Embedding Space Enrichment) entries. Low pLDDT residues should be deprioritized in embedding space.

2. **Structural NeSy tokens**: New NeSy markers derived from structure: `[SS:HELIX]`, `[SS:STRAND]`, `[pLDDT:HIGH]`, `[HUB:42]`. This would make the nesy-core preset structure-aware for PLMs.

3. **DSSP-aware kinase PAS**: The KinasePASAnnotator could use DSSP to verify that the activation loop is actually a loop (C/coil) in inactive state and extended in active.

4. **AlphaSync integration**: For enriched residue-level data beyond pLDDT (accessibility, contacts, disorder prediction) via `https://alphasync.stjude.org/api`.

5. **Structural embedding injection**: Feed AlphaFold per-residue pLDDT as a 1D embedding alongside ESM-2/ProtT5 vectors.

---

## Appendix A: AlphaFold pLDDT Confidence Classes

| Range | Class | Interpretation | LMP Use |
|-------|-------|---------------|---------|
| pLDDT > 90 | `very_high` | Backbone and sidechain positions reliable | Full structural annotation |
| 70 < pLDDT ≤ 90 | `confident` | Backbone reliable, sidechain less certain | Backbone-level annotation |
| 50 < pLDDT ≤ 70 | `low` | May be disordered or uncertain fold | Flag as low confidence |
| pLDDT ≤ 50 | `very_low` | Likely disordered, use sequence-only features | Exclude from structural features |

## Appendix B: Domain Confidence from PAE

```python
def compute_domain_confidence(pae_matrix, domains):
    """
    For each pair of domains, compute mean inter-domain PAE.
    
    Low PAE (< 5 Å) → domains have confident relative positioning
    High PAE (> 15 Å) → domains are independently modeled (flexible linker)
    """
    for i, dom_a in enumerate(domains):
        for j, dom_b in enumerate(domains):
            if i >= j:
                continue
            pae_block = pae_matrix[dom_a.start:dom_a.end, dom_b.start:dom_b.end]
            mean_pae = np.mean(pae_block)
            confident = mean_pae < 10.0  # Threshold from AlphaFold team
            yield DomainPairConfidence(dom_a.name, dom_b.name, mean_pae, confident)
```

## Appendix C: Dependency Requirements

```
# New dependencies (all optional, graceful degradation):
mdtraj>=1.9.7      # DSSP, contacts, Ramachandran (already in SMIC)
MDAnalysis>=2.5.0   # Network analysis, ASA (already in SMIC)
networkx>=3.0       # Graph centrality (already in SMIC)
requests>=2.28      # AlphaFold API (already in LMP)
numpy>=1.24         # PAE matrix ops (already everywhere)
```

No new hard dependencies. All structural computation modules use `try/except ImportError` 
for graceful degradation — same pattern as existing `pdb_metrics.py` and `smic_bridge.py`.

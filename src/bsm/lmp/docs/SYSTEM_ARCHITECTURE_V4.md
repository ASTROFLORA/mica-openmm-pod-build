# LMP Generator v4 — System Architecture

> **File**: `src/bsm/lmp/generator_v4.py`  
> **Lines**: ~7,573  
> **Class**: `LMPGenerator`  
> **Version**: v4.3 (Multi-API enrichment + circuit breakers)  
> **Last updated**: 2026-03-22

---

## 1. Overview

The LMP (Ligand–Macromolecule Profile) Generator v4 is a monolithic Python class that produces XML documents describing protein states with structural, functional, pharmacological, and genomic annotations. It fetches data from **14 external biological APIs** and assembles it into a schema-validated XML output.

Each protein generates one XML document per conformational state (active, inactive, transition, etc.), with typical output sizes of 500–1500 KB per state.

### Key Capabilities

| Capability | Description |
|---|---|
| Multi-state generation | Infers active/inactive/transition states from PTMs and domain annotations |
| 14-API enrichment | STRING-DB, OpenTargets, ChEMBL, KEGG, Reactome, ProteinAtlas, GO, Ensembl, HPO, GTEx + UniProt, PDB, PubChem, AlphaFold |
| NeSy encoding | Neuro-Symbolic sequence encoding with hierarchical markers |
| Structural analysis | AlphaFold pLDDT, DSSP secondary structure, contact maps, network centrality |
| PDB-centric mode | Generate from PDB ID directly (PLIP interaction analysis via subprocess isolation) |
| Preset system | 8+ presets controlling which blocks to include |
| Disk caching | Per-API response caching with 30-day TTL |
| Rate limiting | Per-API rate limiters (14 separate intervals) |
| Circuit breakers | Per-API circuit breakers (threshold=3, cooldown=120s) |

---

## 2. Architecture Diagram

```
┌──────────────────────────────────────────────────────────┐
│                     LMPGenerator                          │
│  __init__(cache_dir, preset, rate_limit, offline_mode)   │
├───────────────┬──────────────────────────────────────────┤
│   Entry Points│                                          │
│               │  generate_lmp_v4_multi_state()  ←─ main  │
│               │  generate_from_pdb()            ←─ PDB   │
├───────────────┴──────────────────────────────────────────┤
│                                                          │
│  ┌─────────────────────────────────────────────┐         │
│  │        Phase 1: Data Fetching               │         │
│  │  _fetch_uniprot()     → UniProt REST API    │         │
│  │  _fetch_pdb()         → RCSB PDB            │         │
│  │  _extract_ptms/domains/binding_sites()      │         │
│  └─────────────────┬───────────────────────────┘         │
│                    │                                     │
│  ┌─────────────────▼───────────────────────────┐         │
│  │        Phase 2: State Inference             │         │
│  │  _infer_states(ptms, domains)               │         │
│  │  STATE_KEYWORDS: active/inactive/transition │         │
│  └─────────────────┬───────────────────────────┘         │
│                    │                                     │
│  ┌─────────────────▼───────────────────────────┐         │
│  │   Phase 3: Per-State XML Assembly           │         │
│  │  _build_state_xml_v4()                      │         │
│  │  ├── _add_nesy_grammar_v4()                 │         │
│  │  ├── _add_semantics_v4()                    │         │
│  │  ├── _add_geometry_v4()                     │         │
│  │  ├── _add_features_v4()                     │         │
│  │  ├── _add_knowledge_graph_v4()              │         │
│  │  │   └─ Multi-API Enrichment (14 APIs)      │         │
│  │  ├── _add_provenance_v4()                   │         │
│  │  └── _add_structural_v4() [AlphaFold]       │         │
│  └─────────────────┬───────────────────────────┘         │
│                    │                                     │
│  ┌─────────────────▼───────────────────────────┐         │
│  │    Phase 4: Serialization & Output          │         │
│  │  ET → minidom prettyprint → UTF-8 XML       │         │
│  │  Optional PubChem sidecar JSON              │         │
│  └─────────────────────────────────────────────┘         │
│                                                          │
│  ┌─────────────────────────────────────────────┐         │
│  │    Resilience Layer                         │         │
│  │  _safe_api_get()    → JSON GET              │         │
│  │  _safe_api_post()   → JSON POST             │         │
│  │  _safe_api_get_text() → text GET            │         │
│  │  _rate_limit_wait(api_name)                 │         │
│  │  _is_circuit_open(api_name)                 │         │
│  │  _record_api_success/failure(api_name)      │         │
│  └─────────────────────────────────────────────┘         │
│                                                          │
│  ┌─────────────────────────────────────────────┐         │
│  │    Caching Layer                            │         │
│  │  cache_dir/{api}_{key}.json                 │         │
│  │  TTL = 30 days | Max = 1 GB                 │         │
│  └─────────────────────────────────────────────┘         │
└──────────────────────────────────────────────────────────┘
```

---

## 3. XML Output Structure

Each generated XML follows `lmp_v4_schema.xsd`:

```xml
<lmp:LMP xmlns:lmp="http://bsm.bioinformatics.org/lmp/v4">
  <lmp:Header>
    <lmp:UniProtAccession>P04637</lmp:UniProtAccession>
    <lmp:GeneName>TP53</lmp:GeneName>
    <lmp:Organism>Homo sapiens</lmp:Organism>
    <lmp:StateName>active</lmp:StateName>
  </lmp:Header>

  <lmp:NeSyGrammar>       <!-- Neuro-symbolic sequence encoding -->
  <lmp:Semantics>          <!-- Keywords, comments, functional annotations -->
  <lmp:Geometry>           <!-- 3D coordinates, secondary structure -->
  <lmp:Features>           <!-- PTMs, domains, binding sites, motifs -->
  <lmp:KnowledgeGraph>     <!-- Cross-references + multi-API data -->
    <lmp:CrossReference>   <!-- UniProt, PDB, InterPro, Pfam, etc. -->
    <lmp:StringInteraction>
    <lmp:OpenTargetsAssociation>
    <lmp:ChEMBLBioactivity>
    <lmp:KEGGPathway>
    <lmp:ReactomePathway>
    <lmp:ProteinAtlasExpression>
    <lmp:GOAnnotation>
    <lmp:EnsemblGene>
    <lmp:HPOPhenotype>
    <lmp:GTExExpression>
  </lmp:KnowledgeGraph>
  <lmp:Provenance>         <!-- Audit trail, generation metadata -->
  <lmp:AlphaFoldStructure> <!-- pLDDT, PAE, DSSP, contacts -->
</lmp:LMP>
```

---

## 4. Preset System

Presets control which XML blocks are generated. Defined in `src/bsm/lmp/presets.py`.

| Preset | NeSy | Semantics | Geometry | Features | KG | AlphaFold | Multi-API | IFP |
|--------|------|-----------|----------|----------|----|-----------|-----------|-----|
| `nesy-core` | ✓ | | | | | | | |
| `semantic` | | ✓ | | | ✓ | | | |
| `structural` | | | ✓ | ✓ | ✓ | ✓ | ✓ (partial) | |
| `v2-compat` | ✓ | ✓ | ✓ | ✓ | | | | |
| `md-ifp` | | | ✓ | | | | | ✓ |
| **`full`** | **✓** | **✓** | **✓** | **✓** | **✓** | **✓** | **✓ (all 14)** | **✓** |
| `plm-esm2` | | | | | ✓ | | | |

The `full` preset enables all 14 APIs and all XML blocks. It is the preset used in all benchmark iterations.

**Preset aliases**: `archive`, `complete`, `all`, `master` → all resolve to `full`.

---

## 5. Constructor Parameters

```python
gen = LMPGenerator(
    cache_dir=Path("my_cache"),   # Response cache location (default: "lmp_cache")
    rate_limit=0.5,               # Global fallback rate limit in seconds
    config_path=Path("lmp.yaml"), # Optional YAML config override
    preset="full",                # Preset name (controls which blocks to include)
    offline_mode=False,           # If True, disable all network fetching
)
```

### YAML Configuration

When `config_path` is provided, the generator reads `generator:` section from the YAML:

```yaml
generator:
  cache_dir: "lmp_cache"
  rate_limit: 0.5
  pubchem:
    enabled: true
    timeout_seconds: 10
    max_ligands_per_pdb: 20
    include_synonyms: false
  string_db:
    enabled: true
    species: 9606
    min_score: 400
    max_partners: 25
  opentargets:
    enabled: true
    max_diseases: 20
  chembl:
    max_activities: 50
  kegg:
    enabled: true
  reactome:
    max_pathways: 30
  protein_atlas:
    enabled: true
  ensembl:
    enabled: true
  gene_ontology:
    enabled: true
  hpo:
    enabled: true
  gtex:
    enabled: true
```

Each API section supports `enabled` (bool), `timeout_seconds` (float), and API-specific parameters.

---

## 6. Entry Points

### `generate_lmp_v4_multi_state()`

Primary entry point for generating multi-state XML from a UniProt accession.

```python
result = gen.generate_lmp_v4_multi_state(
    uniprot_id="P04637",       # UniProt accession (required)
    gene_name="TP53",          # Gene symbol (required)
    organism="Homo sapiens",   # Organism (default: "Homo sapiens")
    states=None,               # List of state names (None = auto-infer)
    pdb_ids=None,              # Optional PDB IDs to include geometry from
    pdb_id_for_ifp=None,       # PDB ID for trajectory IFP
    trajectory_path=None,      # Path to MD trajectory file
    ligand_resname=None,       # Ligand residue name for IFP
    require_ifp=False,         # Raise if IFP cannot be computed
)
# Returns: Dict[str, str]  →  {"active": "<xml>...", "inactive": "<xml>..."}
```

### `generate_from_pdb()`

Alternative entry point for PDB-centric generation:

```python
xml = gen.generate_from_pdb(
    pdb_id="6FBK",        # PDB identifier
    chain_id="A",         # Optional chain filter
    state_name="6FBK_A",  # Optional custom state name
)
# Returns: str  →  Single XML string
```

---

## 7. Module Dependencies

```
generator_v4.py
├── nesy_encoder.py       → LMPNeSyEncoder, NeSyAnnotation
├── pas_annotators.py     → get_pas_annotator (phosphorylation site prediction)
├── presets.py            → LMPPreset, get_preset
├── alphafold_client.py   → AlphaFoldClient (optional)
├── structural_metrics.py → StructuralMetricsComputer (optional)
└── stdlib:
    ├── requests          → HTTP client for all 14 APIs
    ├── xml.etree         → XML construction
    ├── xml.dom.minidom   → Pretty printing
    ├── json, base64      → Encoding
    └── logging           → Per-module logging
```

---

## 8. Data Flow for Multi-API Enrichment

Within `_build_state_xml_v4()` → `_add_knowledge_graph_v4()`:

```
gene_name (from UniProt)
    │
    ├─→ _add_string_interactions_v4(gene_name)     → <StringInteraction>
    ├─→ _add_opentargets_v4(gene_name)             → <OpenTargetsAssociation>
    ├─→ _add_chembl_bioactivity_v4(uniprot_id)     → <ChEMBLBioactivity>
    ├─→ _add_kegg_pathways_v4(gene_name)           → <KEGGPathway>
    ├─→ _add_reactome_pathways_v4(uniprot_id)      → <ReactomePathway>
    ├─→ _add_protein_atlas_v4(gene_name)           → <ProteinAtlasExpression>
    ├─→ _add_go_enrichment_v4(uniprot_id)          → <GOAnnotation>
    ├─→ _add_ensembl_v4(gene_name)                 → <EnsemblGene>
    ├─→ _add_hpo_phenotypes_v4(gene_name)          → <HPOPhenotype>
    └─→ _add_gtex_expression_v4(gene_name)         → <GTExExpression>
```

Each `_add_*` method follows the same pattern:
1. Check preset flag (`_preset_bool`)
2. Check API-specific `enabled` config
3. Call `_fetch_*` method (network + caching)
4. Parse response into XML sub-elements
5. Wrap in `try/except` (best-effort, failures don't abort generation)

---

## 9. Code Map (Key Line Ranges)

| Range | Section |
|---|---|
| 1–100 | Module docstring, imports, retry decorator |
| 100–220 | Class constants (API bases, PTM residues, state keywords) |
| 230–380 | `__init__()` — config loading, rate limits, circuit breakers, per-API config |
| 380–510 | PLIP subprocess isolation |
| 510–2420 | Core generation: UniProt fetch, PDB fetch, state inference, XML assembly |
| 2420–2540 | Multi-API enrichment orchestration in `_add_knowledge_graph_v4()` |
| 2540–2570 | Circuit breaker helpers (`_is_circuit_open`, `_record_api_success/failure`) |
| 2570–2720 | Safe API methods (`_safe_api_get`, `_safe_api_post`, `_safe_api_get_text`) |
| 2720–2810 | STRING-DB integration |
| 2810–2930 | OpenTargets GraphQL integration |
| 2930–3020 | ChEMBL integration |
| 3020–3120 | KEGG integration (with exact-match fix) |
| 3120–3180 | Reactome integration |
| 3180–3320 | ProteinAtlas integration |
| 3320–3400 | GO/QuickGO integration |
| 3400–3470 | Ensembl integration |
| 3470–3550 | HPO integration |
| 3550–3610 | GTEx integration |
| 3610–3800 | `generate_lmp_v4_multi_state()` — main entry point |
| 7520–7575 | `_rate_limit_wait()`, `generate_from_pdb()` |

---

## 10. Error Handling Philosophy

The generator follows a **best-effort enrichment** model:

1. **Core data** (UniProt, sequence): Required — failure aborts generation
2. **Structural data** (PDB, AlphaFold): Optional — failure produces XML without geometry
3. **Multi-API enrichment** (10 APIs): Best-effort — each wrapped in independent `try/except`
4. **Rate limiting**: Automatic — per-API interval enforcement
5. **Circuit breaking**: Automatic — 3 consecutive 5xx/timeout failures trip the breaker
6. **4xx errors**: Treated as "no data available" (not a failure), circuit stays healthy
7. **429 Too Many Requests**: Automatic retry with `Retry-After` header or exponential backoff

No single API failure can crash the generation pipeline. The XML will contain whichever data was successfully fetched.

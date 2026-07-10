# API Reference

## DLMLMPBridge

**Location**: `src/mica/drivers/dlm_lmp_bridge.py`

### Constructor

```python
class DLMLMPBridge:
    def __init__(
        self,
        enable_dlm: bool = True,
        enable_entity_mapper: bool = True,
        enable_lmp_validation: bool = True,
        confidence_threshold: float = 0.8,
        disambiguation_delta: float = 0.2
    )
```

**Parameters**:
- `enable_dlm` (bool): Enable DLM-based entity extraction (default: True)
- `enable_entity_mapper` (bool): Enable knowledge base linking (default: True)
- `enable_lmp_validation` (bool): Enable LMP schema validation (default: True)
- `confidence_threshold` (float): Minimum confidence for auto-execution (default: 0.8)
- `disambiguation_delta` (float): Maximum difference for ambiguity detection (default: 0.2)

**Example**:
```python
bridge = DLMLMPBridge(
    enable_dlm=True,
    confidence_threshold=0.75
)
```

### Public Methods

#### process_query()

```python
def process_query(
    self,
    query: str,
    tool_type: str = "pdb",
    context: Optional[Dict[str, Any]] = None
) -> BridgeResult
```

**Description**: Main entry point for processing natural language queries.

**Parameters**:
- `query` (str): Natural language query (e.g., "Fetch structure 1TUP")
- `tool_type` (str): MCP tool type (default: "pdb")
- `context` (Optional[Dict]): Additional context (e.g., session history)

**Returns**: `BridgeResult` with extracted entities, linked data, tool arguments

**Raises**: `ValueError` if tool_type is invalid

**Example**:
```python
result = bridge.process_query(
    "Fetch p53 structure",
    tool_type="pdb"
)

print(result.extracted.protein_names)  # ["p53"]
print(result.confidence)  # 0.85
print(result.args)  # {"uniprot_id": "P04637"}
```

---

#### resolve_clarification()

```python
def resolve_clarification(
    self,
    bridge_result: BridgeResult,
    user_choice: Dict[str, Any]
) -> BridgeResult
```

**Description**: Resolve ambiguous entity mappings based on user selection.

**Parameters**:
- `bridge_result` (BridgeResult): Original result with clarification_prompt
- `user_choice` (Dict): User selection (e.g., `{"entity": "TP53", "mapping_index": 0}`)

**Returns**: Updated `BridgeResult` with resolved entity and rebuilt arguments

**Raises**: `ValueError` if user_choice is invalid

**Example**:
```python
result = bridge.process_query("Fetch TP53", tool_type="pdb")

if result.clarification_prompt:
    user_choice = {"entity": "TP53", "mapping_index": 0}
    resolved = bridge.resolve_clarification(result, user_choice)
    
    print(resolved.clarification_prompt)  # None
    print(resolved.args)  # {"uniprot_id": "P04637"}
```

---

#### suggest_tools_from_markers()

```python
def suggest_tools_from_markers(
    self,
    markers: Dict[str, List[str]]
) -> List[str]
```

**Description**: Suggest MCP tools based on NeSy markers.

**Parameters**:
- `markers` (Dict[str, List[str]]): NeSy markers by category

**Returns**: List of suggested tool names (deduplicated)

**Example**:
```python
markers = {
    "structural": ["structure", "domain"],
    "evolutionary": ["conserved"]
}

tools = bridge.suggest_tools_from_markers(markers)
# ["pdb", "alphafold", "structure_alignment", "blast", "phylogeny", "alignment"]
```

### Private Methods

#### _extract_entities()

```python
def _extract_entities(self, query: str) -> ExtractedEntities
```

**Description**: Extract entities from query using DLM + regex fallback.

**Parameters**:
- `query` (str): Natural language query

**Returns**: `ExtractedEntities` with proteins, genes, domains, PTMs, organisms

**Example**:
```python
entities = bridge._extract_entities("Fetch p53 structure")
# ExtractedEntities(protein_names=["p53"], uniprot_ids=[], pdb_ids=[], ...)
```

---

#### _link_entities()

```python
def _link_entities(
    self,
    extracted: ExtractedEntities
) -> LinkedEntities
```

**Description**: Link extracted entities to knowledge base (UniProt, PDB).

**Parameters**:
- `extracted` (ExtractedEntities): Extracted entities

**Returns**: `LinkedEntities` with mappings and confidence scores

**Example**:
```python
linked = bridge._link_entities(entities)
# LinkedEntities(uniprot_mappings=[EntityMapping(...)])
```

---

#### _fill_tool_args()

```python
def _fill_tool_args(
    self,
    extracted: ExtractedEntities,
    linked: LinkedEntities,
    tool_type: str
) -> Dict[str, Any]
```

**Description**: Fill tool arguments using extracted and linked entities.

**Parameters**:
- `extracted` (ExtractedEntities): Extracted entities
- `linked` (LinkedEntities): Linked entities
- `tool_type` (str): MCP tool type

**Returns**: Dictionary of tool arguments

**Example**:
```python
args = bridge._fill_tool_args(extracted, linked, "pdb")
# {"pdb_id": "1TUP", "format": "cif"}
```

---

#### _validate_args()

```python
def _validate_args(
    self,
    tool_type: str,
    args: Dict[str, Any]
) -> List[str]
```

**Description**: Validate tool arguments (PTM compatibility, types, required fields).

**Parameters**:
- `tool_type` (str): MCP tool type
- `args` (Dict): Tool arguments

**Returns**: List of validation error messages (empty if valid)

**Example**:
```python
args = {"ptm_type": "phosphorylation", "residue": "A"}
errors = bridge._validate_args("ptm_modification", args)
# ["PTM 'phosphorylation' incompatible with residue 'A'. Compatible: S, T, Y"]
```

---

#### _validate_ptm_operation()

```python
def _validate_ptm_operation(
    self,
    tool_type: str,
    args: Dict[str, Any]
) -> List[str]
```

**Description**: Validate PTM-residue compatibility.

**Parameters**:
- `tool_type` (str): MCP tool type
- `args` (Dict): Tool arguments with `ptm_type` and `residue`

**Returns**: List of validation error messages (empty if valid)

**Example**:
```python
args = {"ptm_type": "acetylation", "residue": "K"}
errors = bridge._validate_ptm_operation("ptm_modification", args)
# []
```

---

#### _extract_nesy_markers()

```python
def _extract_nesy_markers(
    self,
    text: str
) -> Dict[str, List[str]]
```

**Description**: Extract NeSy markers from text.

**Parameters**:
- `text` (str): Query text

**Returns**: Dictionary of markers by category (evolutionary, functional, structural, comparative, dynamic, interaction)

**Example**:
```python
markers = bridge._extract_nesy_markers("Compare conserved domains")
# {
#     "comparative": ["compare"],
#     "evolutionary": ["conserved"],
#     "structural": ["domains"]
# }
```

---

#### _build_search_query()

```python
def _build_search_query(
    self,
    extracted: ExtractedEntities,
    linked: LinkedEntities
) -> str
```

**Description**: Build PDB search query from entities.

**Parameters**:
- `extracted` (ExtractedEntities): Extracted entities
- `linked` (LinkedEntities): Linked entities

**Returns**: PDB search query string

**Example**:
```python
query = bridge._build_search_query(extracted, linked)
# "uniprot:P04637 domain:DNA-binding method:X-ray"
```

## BridgeResult

**Location**: `src/mica/drivers/dlm_lmp_bridge.py`

### Dataclass Definition

```python
@dataclass
class BridgeResult:
    extracted: ExtractedEntities
    linked: LinkedEntities
    args: Dict[str, Any]
    tool_type: str
    confidence: float
    validation_errors: List[str] = field(default_factory=list)
    clarification_prompt: Optional[str] = None
    search_query: Optional[str] = None
    telemetry: Dict[str, Any] = field(default_factory=dict)
```

**Fields**:
- `extracted` (ExtractedEntities): Extracted entities from query
- `linked` (LinkedEntities): Linked knowledge base mappings
- `args` (Dict[str, Any]): Tool arguments for MCP execution
- `tool_type` (str): MCP tool type (e.g., "pdb", "alphafold")
- `confidence` (float): Overall confidence score (0-1)
- `validation_errors` (List[str]): Validation error messages
- `clarification_prompt` (Optional[str]): User prompt for ambiguous entities
- `search_query` (Optional[str]): PDB search query (if pre-search needed)
- `telemetry` (Dict[str, Any]): Execution metrics and metadata

### Methods

#### is_ready_for_execution()

```python
def is_ready_for_execution(self) -> bool
```

**Description**: Check if result is ready for MCP tool execution.

**Returns**: True if no validation errors, no clarification needed, and args present

**Example**:
```python
if result.is_ready_for_execution():
    pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
```

---

#### needs_pre_search()

```python
def needs_pre_search(self) -> bool
```

**Description**: Check if pre-search workflow is required.

**Returns**: True if no explicit IDs and search_query present

**Example**:
```python
if result.needs_pre_search():
    final = await driver._execute_with_pre_search("pdb", result.search_query, result)
```

## ExtractedEntities

**Location**: `src/mica/drivers/dlm_lmp_bridge.py`

### Dataclass Definition

```python
@dataclass
class ExtractedEntities:
    protein_names: List[str] = field(default_factory=list)
    gene_names: List[str] = field(default_factory=list)
    uniprot_ids: List[str] = field(default_factory=list)
    pdb_ids: List[str] = field(default_factory=list)
    domains: List[str] = field(default_factory=list)
    ptms: List[Dict[str, Any]] = field(default_factory=list)
    organisms: List[str] = field(default_factory=list)
    nesy_markers: Dict[str, List[str]] = field(default_factory=dict)
```

**Fields**:
- `protein_names` (List[str]): Protein names (e.g., ["p53", "TP53"])
- `gene_names` (List[str]): Gene names (e.g., ["BRCA1"])
- `uniprot_ids` (List[str]): UniProt accession IDs (e.g., ["P04637"])
- `pdb_ids` (List[str]): PDB structure IDs (e.g., ["1TUP"])
- `domains` (List[str]): Protein domains (e.g., ["DNA-binding domain"])
- `ptms` (List[Dict]): PTM specifications (e.g., [{"type": "phosphorylation", "residue": "S", "position": 15}])
- `organisms` (List[str]): Organism names (e.g., ["human", "Homo sapiens"])
- `nesy_markers` (Dict[str, List[str]]): NeSy markers by category

### Methods

#### has_explicit_ids()

```python
def has_explicit_ids(self) -> bool
```

**Description**: Check if explicit IDs (PDB, UniProt) are present.

**Returns**: True if pdb_ids or uniprot_ids non-empty

**Example**:
```python
if not entities.has_explicit_ids():
    # Pre-search workflow needed
    pass
```

---

#### is_empty()

```python
def is_empty(self) -> bool
```

**Description**: Check if no entities extracted.

**Returns**: True if all fields empty

**Example**:
```python
if entities.is_empty():
    print("No entities extracted from query")
```

## LinkedEntities

**Location**: `src/mica/drivers/dlm_lmp_bridge.py`

### Dataclass Definition

```python
@dataclass
class LinkedEntities:
    uniprot_mappings: List[EntityMapping] = field(default_factory=list)
    pdb_mappings: List[EntityMapping] = field(default_factory=list)
```

**Fields**:
- `uniprot_mappings` (List[EntityMapping]): UniProt mappings with confidence
- `pdb_mappings` (List[EntityMapping]): PDB mappings with confidence

## EntityMapping

**Location**: `src/mica/drivers/dlm_lmp_bridge.py`

### Dataclass Definition

```python
@dataclass
class EntityMapping:
    text: str
    kb_id: str
    confidence: float
    synonyms: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

**Fields**:
- `text` (str): Original entity text from query
- `kb_id` (str): Knowledge base ID (e.g., "P04637", "1TUP")
- `confidence` (float): Mapping confidence score (0-1)
- `synonyms` (List[str]): Alternative names
- `metadata` (Dict): Additional metadata (e.g., organism, description)

## AgenticDriver Integration

**Location**: `src/mica/drivers/agentic_driver.py`

### Bridge-Enhanced Methods

#### _build_tool_args()

```python
async def _build_tool_args(
    self,
    schema: Dict[str, Any],
    identifiers: List[str],
    query: str,
    tool_type: str
) -> Tuple[Dict[str, Any], Optional[BridgeResult]]
```

**Description**: Build tool arguments using bridge (if available) or regex fallback.

**Parameters**:
- `schema` (Dict): MCP tool schema
- `identifiers` (List[str]): Entity identifiers
- `query` (str): Natural language query
- `tool_type` (str): MCP tool type

**Returns**: Tuple of (tool_args, bridge_result)

**Example**:
```python
args, bridge_result = await driver._build_tool_args(
    schema=pdb_schema,
    identifiers=[],
    query="Fetch p53 structure",
    tool_type="pdb"
)

if bridge_result and bridge_result.needs_pre_search():
    # Trigger pre-search
    final = await driver._execute_with_pre_search("pdb", bridge_result.search_query, bridge_result)
```

---

#### _execute_with_pre_search()

```python
async def _execute_with_pre_search(
    self,
    server_name: str,
    search_query: str,
    bridge_result: BridgeResult
) -> Dict[str, Any]
```

**Description**: Execute PDB pre-search workflow with ranking and fallback.

**Parameters**:
- `server_name` (str): MCP server name (e.g., "pdb")
- `search_query` (str): PDB search query
- `bridge_result` (BridgeResult): Bridge result with context

**Returns**: Dictionary with structure data and telemetry

**Example**:
```python
result = await driver._execute_with_pre_search(
    "pdb",
    "uniprot:P04637",
    bridge_result
)

print(result["pdb_id"])  # "2AC0"
print(result["telemetry"]["execution_time_ms"])  # 1250
```

---

#### _search_and_select_pdb()

```python
async def _search_and_select_pdb(
    self,
    search_query: str,
    bridge_result: BridgeResult
) -> Dict[str, Any]
```

**Description**: Search PDB API and select top-ranked structure.

**Parameters**:
- `search_query` (str): PDB search query
- `bridge_result` (BridgeResult): Bridge result with context

**Returns**: Dictionary with selected structure or error

**Example**:
```python
result = await driver._search_and_select_pdb("uniprot:P04637", bridge_result)

if result["status"] == "success":
    print(result["pdb_id"])  # "2AC0"
    print(result["rank_score"])  # 0.92
```

---

#### _rank_pdb_structures()

```python
def _rank_pdb_structures(
    self,
    structures: List[Dict[str, Any]],
    bridge_result: BridgeResult
) -> List[Dict[str, Any]]
```

**Description**: Rank PDB structures by resolution, method, organism, quality.

**Parameters**:
- `structures` (List[Dict]): PDB search results
- `bridge_result` (BridgeResult): Bridge result with organism context

**Returns**: Sorted list of structures with rank_score

**Example**:
```python
ranked = driver._rank_pdb_structures(pdb_results, bridge_result)

for struct in ranked[:5]:
    print(f"{struct['pdb_id']}: score={struct['rank_score']:.2f}, res={struct['resolution']}")
```

---

#### _fallback_to_alphafold()

```python
async def _fallback_to_alphafold(
    self,
    bridge_result: BridgeResult
) -> Dict[str, Any]
```

**Description**: Fallback to AlphaFold when PDB search fails.

**Parameters**:
- `bridge_result` (BridgeResult): Bridge result with UniProt mapping

**Returns**: Dictionary with AlphaFold predicted structure

**Example**:
```python
result = await driver._fallback_to_alphafold(bridge_result)

print(result["uniprot_id"])  # "P04637"
print(result["fallback_source"])  # "alphafold"
print(result["confidence_note"])  # "AlphaFold prediction (not experimental)"
```

## Constants

### PTM_RESIDUE_COMPATIBILITY

```python
PTM_RESIDUE_COMPATIBILITY = {
    "phosphorylation": ["S", "T", "Y"],
    "acetylation": ["K"],
    "methylation": ["K", "R"],
    "ubiquitination": ["K"],
    "sumoylation": ["K"],
    "glycosylation": ["N", "S", "T"],
    "palmitoylation": ["C"],
    "myristoylation": ["G"],
    "nitrosylation": ["C"],
}
```

**Description**: Matrix of PTM types to compatible amino acid residues.

**Usage**:
```python
compatible = PTM_RESIDUE_COMPATIBILITY["phosphorylation"]
# ["S", "T", "Y"]
```

## Utility Functions

### get_bridge()

```python
def get_bridge() -> DLMLMPBridge
```

**Description**: Get singleton bridge instance.

**Returns**: DLMLMPBridge instance

**Example**:
```python
from mica.drivers.dlm_lmp_bridge import get_bridge

bridge = get_bridge()
result = bridge.process_query("Fetch 1TUP", "pdb")
```

---

### BRIDGE_AVAILABLE

```python
BRIDGE_AVAILABLE: bool
```

**Description**: Flag indicating if bridge dependencies are available.

**Usage**:
```python
from mica.drivers.dlm_lmp_bridge import BRIDGE_AVAILABLE

if BRIDGE_AVAILABLE:
    bridge = get_bridge()
else:
    # Fallback to regex
    pass
```

## Type Definitions

### Type Aliases

```python
from typing import Dict, List, Any, Optional, Tuple

ToolArgs = Dict[str, Any]
NeSyMarkers = Dict[str, List[str]]
ValidationErrors = List[str]
```

## Complete Example

```python
from mica.drivers.dlm_lmp_bridge import DLMLMPBridge, BRIDGE_AVAILABLE
from mica.drivers.agentic_driver import AgenticDriver, AgenticDriverConfig

# Setup
if not BRIDGE_AVAILABLE:
    raise RuntimeError("Bridge dependencies not available")

bridge = DLMLMPBridge(
    enable_dlm=True,
    confidence_threshold=0.8
)

driver = AgenticDriver(AgenticDriverConfig(enable_bridge=True))

# Process query
query = "Fetch p53 DNA-binding domain structure"
result = bridge.process_query(query, tool_type="pdb")

# Handle results
if result.clarification_prompt:
    print(result.clarification_prompt)
    choice = {"entity": "p53", "mapping_index": 0}
    result = bridge.resolve_clarification(result, choice)

if result.needs_pre_search():
    final = await driver._execute_with_pre_search(
        "pdb",
        result.search_query,
        result
    )
    print(f"✅ Fetched {final['pdb_id']} (score: {final['telemetry']['rank_score']:.2f})")
elif result.is_ready_for_execution():
    pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
    print(f"✅ Fetched {pdb_data['pdb_id']}")
else:
    print(f"❌ Validation errors: {result.validation_errors}")
```

## Summary

This API reference covers:
- ✅ **DLMLMPBridge** class with 10+ public/private methods
- ✅ **BridgeResult** dataclass with 2 key methods
- ✅ **ExtractedEntities** and **LinkedEntities** data structures
- ✅ **AgenticDriver** integration methods (6 methods)
- ✅ **Constants** and **utilities** for bridge usage

All methods include signatures, parameters, return types, descriptions, and examples.

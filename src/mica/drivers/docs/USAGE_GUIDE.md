# DLM-LMP Bridge Usage Guide

## Quick Start

### Installation

```python
# Bridge is part of MICA drivers
from mica.drivers.dlm_lmp_bridge import DLMLMPBridge

# Create bridge instance
bridge = DLMLMPBridge(
    enable_dlm=True,
    enable_entity_mapper=True,
    enable_lmp_validation=True
)
```

### Basic Usage

```python
# Process a query
query = "Fetch structure of p53"
result = bridge.process_query(query, tool_type="pdb")

# Check if ready for execution
if result.is_ready_for_execution():
    print(f"Tool arguments: {result.args}")
    # {"pdb_id": "1TUP", ...}
```

## Configuration

### DLMLMPBridge Options

```python
@dataclass
class DLMLMPBridge:
    # Enable/disable components
    enable_dlm: bool = True                # DLM entity extraction
    enable_entity_mapper: bool = True      # KB linking
    enable_lmp_validation: bool = True     # Schema validation
    
    # Confidence thresholds
    confidence_threshold: float = 0.8      # Min confidence for auto-execution
    disambiguation_delta: float = 0.2      # Max difference for ambiguity detection
    
    # Validation settings
    strict_validation: bool = True         # Enforce all required fields
    allow_partial_fills: bool = False      # Allow incomplete tool args
```

### AgenticDriver Integration

```python
from mica.drivers.agentic_driver import AgenticDriver, AgenticDriverConfig

config = AgenticDriverConfig(
    enable_bridge=True,
    bridge_confidence_threshold=0.8,
)

driver = AgenticDriver(config)
```

## Core Workflows

### Workflow 1: Direct Execution (Explicit IDs)

**Use Case**: User provides explicit identifiers (PDB ID, UniProt ID)

**Query**: "Fetch structure 1TUP"

**Steps**:
1. Extract entities (regex fallback)
2. Build tool arguments
3. Execute MCP tool

**Code**:
```python
result = bridge.process_query("Fetch structure 1TUP", tool_type="pdb")

# Result
assert result.extracted.pdb_ids == ["1TUP"]
assert result.confidence == 1.0
assert result.is_ready_for_execution() == True
assert result.args == {"pdb_id": "1TUP"}

# Execute
pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
```

### Workflow 2: Pre-Search (No Explicit IDs)

**Use Case**: User provides protein name without structure ID

**Query**: "Fetch p53 DNA-binding domain structure"

**Steps**:
1. Extract entities (DLM + regex)
2. Link to knowledge base (UniProt)
3. Detect pre-search need
4. Build search query
5. Search PDB API
6. Rank structures
7. Select best structure
8. Download structure

**Code**:
```python
# Bridge processing
result = bridge.process_query(
    "Fetch p53 DNA-binding domain structure",
    tool_type="pdb"
)

# Result
assert result.extracted.protein_names == ["p53"]
assert result.extracted.domains == ["DNA-binding domain"]
assert result.needs_pre_search() == True
assert result.search_query == "uniprot:P04637 domain:DNA-binding method:X-ray"

# AgenticDriver handles pre-search
final_result = await driver._execute_with_pre_search(
    "pdb",
    result.search_query,
    result
)

# Returns top-ranked structure
# final_result = {
#     "pdb_id": "2AC0",
#     "resolution": 1.8,
#     "method": "X-RAY",
#     "structure": <PDB file data>,
#     "telemetry": {
#         "execution_time_ms": 1250,
#         "alternatives": ["1TUP", "3KMD", ...],
#         "rank_score": 0.92
#     }
# }
```

### Workflow 3: Fallback Chain (PDB → AlphaFold)

**Use Case**: PDB search returns no results

**Query**: "Fetch structure of obscure protein ABC123"

**Steps**:
1. Pre-search PDB (empty results)
2. Extract UniProt ID from bridge result
3. Fallback to AlphaFold
4. Fetch predicted structure

**Code**:
```python
result = bridge.process_query("Fetch structure of ABC123", tool_type="pdb")

# Pre-search PDB
pdb_result = await driver._search_and_select_pdb(result.search_query, result)

if pdb_result["status"] == "failed":
    # Automatic fallback
    af_result = await driver._fallback_to_alphafold(result)
    
    # Result
    # af_result = {
    #     "uniprot_id": "Q9Y123",
    #     "structure": <AlphaFold predicted structure>,
    #     "confidence_note": "AlphaFold prediction (not experimental)",
    #     "fallback_source": "alphafold",
    #     "telemetry": {...}
    # }
```

### Workflow 4: Clarification Dialog (Ambiguous Entities)

**Use Case**: Multiple entities match user query

**Query**: "Fetch TP53"

**Steps**:
1. Extract entity
2. Link to knowledge base (multiple matches)
3. Detect ambiguity
4. Generate clarification prompt
5. User selects entity
6. Resolve clarification
7. Rebuild tool arguments
8. Execute

**Code**:
```python
# Initial query
result = bridge.process_query("Fetch TP53", tool_type="pdb")

# Check for clarification
if result.clarification_prompt:
    print(result.clarification_prompt)
    # "Multiple matches for TP53:
    #   1. P04637 (Human tumor protein p53) - confidence: 0.9
    #   2. Q9UMS4 (TP53-regulated inhibitor) - confidence: 0.85
    # Please select: [0-1]"
    
    # User selects option 1 (index 0)
    user_choice = {"entity": "TP53", "mapping_index": 0}
    
    # Resolve clarification
    resolved = bridge.resolve_clarification(result, user_choice)
    
    # Result
    assert resolved.clarification_prompt is None
    assert resolved.is_ready_for_execution() == True
    assert resolved.args == {"uniprot_id": "P04637", ...}
    
    # Execute
    pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", resolved.args)
```

### Workflow 5: PTM Validation

**Use Case**: Validate PTM operations before execution

**Query**: "Simulate phosphorylation of p53 at serine 15"

**Steps**:
1. Extract entities (protein, PTM type, residue, position)
2. Validate PTM-residue compatibility
3. Build tool arguments
4. Execute

**Code**:
```python
result = bridge.process_query(
    "Simulate phosphorylation of p53 at serine 15",
    tool_type="ptm_modification"
)

# Check validation
if result.validation_errors:
    print(f"Validation errors: {result.validation_errors}")
else:
    print(f"Valid PTM operation: {result.args}")
    # {
    #     "protein_id": "P04637",
    #     "ptm_type": "phosphorylation",
    #     "residue": "S",
    #     "position": 15
    # }
    
    # Execute
    ptm_result = await driver.call_mcp_tool(
        "ptm_modification",
        "simulate_ptm",
        result.args
    )
```

### Workflow 6: NeSy Tool Routing

**Use Case**: Intelligent tool selection based on intent

**Query**: "Compare conserved residues in p53 structure"

**Steps**:
1. Extract NeSy markers
2. Suggest tools based on markers
3. Filter available tools
4. Execute with best tool

**Code**:
```python
result = bridge.process_query(
    "Compare conserved residues in p53 structure",
    tool_type="pdb"
)

# Extract markers
markers = result.extracted.nesy_markers
# {
#     "comparative": ["compare"],
#     "evolutionary": ["conserved"],
#     "structural": ["structure"]
# }

# Suggest tools
tools = bridge.suggest_tools_from_markers(markers)
# ["pdb", "alphafold", "structure_alignment", "blast", "phylogeny", "alignment"]

# Filter available
available = [t for t in tools if t in driver.mcp_servers]

# Execute pipeline
structure = await driver.execute_tool("pdb", {"pdb_id": "1TUP"})
homologs = await driver.execute_tool("blast", {"query": structure.sequence})
alignment = await driver.execute_tool("alignment", {"sequences": homologs})
conservation = map_conservation_to_structure(alignment, structure)
```

## Advanced Usage

### Custom Entity Extraction

```python
from mica.drivers.dlm_lmp_bridge import ExtractedEntities

# Manually create entities
entities = ExtractedEntities(
    protein_names=["p53"],
    uniprot_ids=["P04637"],
    domains=["DNA-binding domain"],
    nesy_markers={"structural": ["domain"]}
)

# Process with custom entities
result = bridge.process_query_with_entities(
    query="Fetch structure",
    entities=entities,
    tool_type="pdb"
)
```

### Custom Validation

```python
# Add custom validator
def validate_temperature(args):
    if "temperature" in args:
        temp = args["temperature"]
        if temp < 273 or temp > 373:
            return [f"Temperature {temp}K out of range (273-373K)"]
    return []

# Extend bridge
class CustomBridge(DLMLMPBridge):
    def _validate_args(self, tool_type, args):
        errors = super()._validate_args(tool_type, args)
        errors.extend(validate_temperature(args))
        return errors
```

### Batch Processing

```python
queries = [
    "Fetch structure 1TUP",
    "Fetch structure of p53",
    "Fetch EGFR kinase domain structure"
]

results = []
for query in queries:
    result = bridge.process_query(query, tool_type="pdb")
    results.append(result)

# Filter ready for execution
ready = [r for r in results if r.is_ready_for_execution()]

# Filter needs pre-search
pre_search = [r for r in results if r.needs_pre_search()]

# Execute ready queries
for result in ready:
    pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)

# Process pre-search queries
for result in pre_search:
    final = await driver._execute_with_pre_search("pdb", result.search_query, result)
```

## Integration Patterns

### Pattern 1: REST API Integration

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()
bridge = DLMLMPBridge()

class QueryRequest(BaseModel):
    query: str
    tool_type: str = "pdb"

@app.post("/process")
async def process_query(request: QueryRequest):
    result = bridge.process_query(request.query, request.tool_type)
    
    if result.clarification_prompt:
        return {
            "status": "needs_clarification",
            "prompt": result.clarification_prompt
        }
    
    if result.needs_pre_search():
        return {
            "status": "needs_pre_search",
            "search_query": result.search_query
        }
    
    if result.is_ready_for_execution():
        return {
            "status": "ready",
            "args": result.args,
            "confidence": result.confidence
        }
    
    return {
        "status": "error",
        "errors": result.validation_errors
    }
```

### Pattern 2: CLI Tool

```python
import argparse
from mica.drivers.dlm_lmp_bridge import DLMLMPBridge

def main():
    parser = argparse.ArgumentParser(description="MICA Bridge CLI")
    parser.add_argument("query", help="Natural language query")
    parser.add_argument("--tool", default="pdb", help="Tool type")
    parser.add_argument("--no-dlm", action="store_true", help="Disable DLM")
    
    args = parser.parse_args()
    
    bridge = DLMLMPBridge(enable_dlm=not args.no_dlm)
    result = bridge.process_query(args.query, args.tool)
    
    if result.clarification_prompt:
        print(result.clarification_prompt)
        choice = int(input("Select: "))
        user_choice = {"entity": "...", "mapping_index": choice}
        result = bridge.resolve_clarification(result, user_choice)
    
    if result.is_ready_for_execution():
        print(f"Tool arguments: {result.args}")
        print(f"Confidence: {result.confidence}")
    else:
        print(f"Errors: {result.validation_errors}")

if __name__ == "__main__":
    main()
```

### Pattern 3: Jupyter Notebook

```python
from mica.drivers.dlm_lmp_bridge import DLMLMPBridge
from mica.drivers.agentic_driver import AgenticDriver
import asyncio

# Setup
bridge = DLMLMPBridge()
driver = AgenticDriver()

# Interactive query
query = input("Enter query: ")
result = bridge.process_query(query, tool_type="pdb")

# Display results
print(f"Extracted entities: {result.extracted}")
print(f"Confidence: {result.confidence}")

# Execute if ready
if result.is_ready_for_execution():
    pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
    
    # Visualize structure
    import nglview as nv
    view = nv.show_structure_file(pdb_data["file_path"])
    view.display()
```

## Error Handling

### Handling Bridge Unavailable

```python
from mica.drivers.dlm_lmp_bridge import BRIDGE_AVAILABLE

if BRIDGE_AVAILABLE:
    bridge = get_bridge()
    result = bridge.process_query(query, tool_type)
else:
    # Fallback to regex
    pdb_id = extract_pdb_id_regex(query)
    result = {"pdb_id": pdb_id}
```

### Handling DLM Failures

```python
try:
    result = bridge.process_query(query, tool_type="pdb")
except Exception as e:
    # Regex fallback
    logger.warning(f"DLM extraction failed: {e}, using regex fallback")
    pdb_id = extract_pdb_id_regex(query)
    result = BridgeResult(
        extracted=ExtractedEntities(pdb_ids=[pdb_id]),
        args={"pdb_id": pdb_id},
        confidence=0.7  # Lower confidence for regex
    )
```

### Handling Low Confidence

```python
result = bridge.process_query(query, tool_type="pdb")

if result.confidence < 0.5:
    print(f"⚠️ Low confidence ({result.confidence:.2f})")
    confirm = input("Proceed anyway? (y/n): ")
    
    if confirm.lower() != "y":
        print("Aborted")
        return
    
    # Execute with warning
    pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
    print("✅ Completed (low confidence)")
```

### Handling Validation Errors

```python
result = bridge.process_query(query, tool_type="ptm_modification")

if result.validation_errors:
    print("❌ Validation errors:")
    for error in result.validation_errors:
        print(f"  - {error}")
    
    # Attempt correction
    if "incompatible" in result.validation_errors[0]:
        # Extract compatible residues from error message
        compatible = extract_compatible_residues(result.validation_errors[0])
        print(f"💡 Try one of these residues: {', '.join(compatible)}")
    
    return None
```

## Debugging

### Enable Debug Logging

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("mica.drivers.dlm_lmp_bridge")

result = bridge.process_query(query, tool_type="pdb")
# DEBUG: DLM extraction: 0.15s
# DEBUG: Entity linking: 0.23s
# DEBUG: Tool arg filling: 0.01s
# DEBUG: Total processing: 0.39s
```

### Inspect Intermediate Results

```python
result = bridge.process_query(query, tool_type="pdb")

# Extracted entities
print(f"Entities: {result.extracted}")
print(f"  Proteins: {result.extracted.protein_names}")
print(f"  PDB IDs: {result.extracted.pdb_ids}")
print(f"  Domains: {result.extracted.domains}")
print(f"  NeSy Markers: {result.extracted.nesy_markers}")

# Linked entities
print(f"Linked: {result.linked}")
print(f"  UniProt Mappings: {result.linked.uniprot_mappings}")
print(f"  PDB Mappings: {result.linked.pdb_mappings}")

# Tool arguments
print(f"Args: {result.args}")
print(f"Confidence: {result.confidence}")
print(f"Validation Errors: {result.validation_errors}")
```

### Telemetry Analysis

```python
result = bridge.process_query(query, tool_type="pdb")

# Check telemetry
if result.telemetry:
    print(f"Extraction method: {result.telemetry.get('extraction_method')}")
    print(f"Linking confidence: {result.telemetry.get('linking_confidence')}")
    print(f"Validation errors: {result.telemetry.get('validation_errors')}")
    print(f"NeSy markers: {result.telemetry.get('nesy_markers_detected')}")

# After execution
final_result = await driver._execute_with_pre_search("pdb", result.search_query, result)

print(f"Execution time: {final_result['telemetry']['execution_time_ms']}ms")
print(f"Fallback used: {final_result['telemetry']['fallback_used']}")
print(f"Alternatives: {final_result['telemetry'].get('alternatives', [])}")
```

## Performance Optimization

### Caching

```python
from functools import lru_cache

class OptimizedBridge(DLMLMPBridge):
    @lru_cache(maxsize=100)
    def _extract_entities_cached(self, query: str):
        return super()._extract_entities(query)
    
    def _extract_entities(self, query: str):
        return self._extract_entities_cached(query)
```

### Async Processing

```python
import asyncio

async def process_queries_parallel(queries):
    results = await asyncio.gather(*[
        asyncio.to_thread(bridge.process_query, q, "pdb")
        for q in queries
    ])
    return results

# Usage
queries = ["Fetch 1TUP", "Fetch p53", "Fetch EGFR"]
results = await process_queries_parallel(queries)
```

### Lazy Loading

```python
class LazyBridge:
    def __init__(self):
        self._bridge = None
    
    def bridge(self):
        if self._bridge is None:
            self._bridge = DLMLMPBridge()
        return self._bridge
    
    def process_query(self, query, tool_type):
        return self.bridge().process_query(query, tool_type)
```

## Best Practices

### 1. Always Check Confidence

```python
result = bridge.process_query(query, tool_type="pdb")

if result.confidence < 0.8:
    print(f"⚠️ Low confidence: {result.confidence:.2f}")
    # Consider asking for clarification
```

### 2. Handle Clarification

```python
if result.clarification_prompt:
    # Always present to user
    print(result.clarification_prompt)
    # Get user input
    choice = get_user_choice()
    # Resolve
    result = bridge.resolve_clarification(result, choice)
```

### 3. Use Pre-Search When Appropriate

```python
if result.needs_pre_search():
    # Use driver's pre-search workflow
    final = await driver._execute_with_pre_search("pdb", result.search_query, result)
else:
    # Direct execution
    final = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
```

### 4. Validate Before Execution

```python
if result.validation_errors:
    # Don't execute
    print(f"Validation failed: {result.validation_errors}")
    return None

# Safe to execute
pdb_data = await driver.call_mcp_tool("pdb", "fetch_structure", result.args)
```

### 5. Track Telemetry

```python
result = bridge.process_query(query, tool_type="pdb")

# Log telemetry for monitoring
logger.info(f"Query processed", extra={
    "confidence": result.confidence,
    "extraction_method": result.telemetry.get("extraction_method"),
    "needs_pre_search": result.needs_pre_search(),
})
```

## Common Use Cases

### Use Case 1: Structure Retrieval

```python
# Explicit ID
result = bridge.process_query("Fetch structure 1TUP", tool_type="pdb")
# Direct execution

# Protein name
result = bridge.process_query("Fetch p53 structure", tool_type="pdb")
# Pre-search workflow

# Domain-specific
result = bridge.process_query("Fetch p53 DNA-binding domain", tool_type="pdb")
# Pre-search with domain filter
```

### Use Case 2: Sequence Analysis

```python
# Homology search
result = bridge.process_query("Find homologs of BRCA1", tool_type="blast")
# NeSy markers: evolutionary

# Alignment
result = bridge.process_query("Align p53 sequences from human, mouse, rat", tool_type="alignment")
# NeSy markers: comparative
```

### Use Case 3: PTM Analysis

```python
# Phosphorylation
result = bridge.process_query(
    "Simulate phosphorylation of p53 at S15",
    tool_type="ptm_modification"
)
# PTM validation

# Multiple PTMs
result = bridge.process_query(
    "Analyze acetylation sites in histone H3",
    tool_type="ptm_analysis"
)
# Functional markers
```

### Use Case 4: Dynamics Simulation

```python
# MD simulation
result = bridge.process_query(
    "Simulate dynamics of EGFR kinase domain",
    tool_type="md_simulation"
)
# NeSy markers: dynamic

# Trajectory analysis
result = bridge.process_query(
    "Analyze flexibility in p53 tetramer trajectory",
    tool_type="trajectory_analysis"
)
# NeSy markers: dynamic + structural
```

## Troubleshooting

### Problem: Low Confidence

**Symptoms**: `result.confidence < 0.5`

**Solutions**:
1. Check entity extraction: `result.extracted`
2. Verify linking: `result.linked`
3. Provide more context in query
4. Use explicit IDs if available

### Problem: No Entities Extracted

**Symptoms**: `result.extracted.is_empty()`

**Solutions**:
1. Enable DLM: `bridge.enable_dlm = True`
2. Check regex patterns
3. Verify query format
4. Add more keywords

### Problem: Validation Errors

**Symptoms**: `result.validation_errors` not empty

**Solutions**:
1. Check PTM compatibility
2. Verify required fields
3. Check argument types
4. Review error messages

### Problem: Pre-Search Fails

**Symptoms**: PDB search returns no results

**Solutions**:
1. Check search query: `result.search_query`
2. Broaden search (remove filters)
3. Fallback to AlphaFold (automatic)
4. Verify entity mapping

## Summary

The DLM-LMP Bridge provides:
- ✅ **6 workflows** for different query types
- ✅ **3 integration patterns** (API, CLI, Notebook)
- ✅ **5 best practices** for robust usage
- ✅ **46/46 tests passing** for reliability
- ✅ **Comprehensive error handling** for production

This guide enables users to leverage the bridge for natural language querying of bioinformatics tools with high accuracy and robustness.

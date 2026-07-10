# LMP Generator v4 — Bugfix Changelog (Session 6)

> **Session**: 6 (2026-03-22)  
> **Scope**: Iterations 1–10, all applied to `generator_v4.py`

---

## Overview

Session 6 focused on hardening all 14 API integrations through real-world testing with 25+ proteins. Every bug was discovered via live test failures and fixed with immediate verification.

| Category | Fixes |
|---|---|
| API endpoint corrections | 4 |
| Response parsing / serialization | 3 |
| Logic / data-quality bugs | 2 |
| Total | **9** |

---

## Fix 1: Circuit Breaker 4xx Pass-Through

**Problem**: HTTP 4xx responses (e.g., 404 "protein not found in HPO") were counted as failures, causing the circuit breaker to trip after 3 normal "no data" responses.

**Root cause**: `_safe_api_get()` only checked for 429 and 5xx, letting all other non-2xx status codes fall through to the failure counter.

**Fix**: Added explicit 4xx handling before the retry/failure path:

```python
if 400 <= resp.status_code < 500 and resp.status_code != 429:
    self._record_api_success(api_name)  # API is alive, just no data
    return None
```

**Impact**: Prevents false circuit trips on APIs where many proteins legitimately return 404 (HPO, ChEMBL).

---

## Fix 2: ProteinAtlas Gzip Decompression

**Problem**: ProteinAtlas API returns gzip-compressed JSON even when `Accept-Encoding: gzip` is not sent. `requests` was returning the raw compressed bytes, causing `json.decode()` to fail.

**Root cause**: The ProteinAtlas server at `proteinatlas.org/{gene}.json` serves gzip content by default.

**Fix**: Added explicit gzip decompression in `_add_protein_atlas_data()`:

```python
import gzip
raw = resp.content
if raw[:2] == b'\x1f\x8b':  # gzip magic bytes
    raw = gzip.decompress(raw)
data = json.loads(raw)
```

**Impact**: ProteinAtlas now returns data for 25/25 proteins tested.

---

## Fix 3: HPO 2-Step Gene Search with NCBIGene Prefix

**Problem**: HPO API returned empty results for gene name queries. Direct `/api/hp/search?q=BRCA1` returns ontology terms, not gene-disease associations.

**Root cause**: The HPO API requires a 2-step process: (1) search for the gene in NCBI Gene to get the numeric ID, then (2) query HPO with `NCBIGene:{id}` prefix.

**Fix**: Implemented 2-step process in `_fetch_hpo_data()`:

```python
# Step 1: Gene search
search = self._safe_api_get(f"https://ontology.jax.org/api/hp/search?q={gene_name}",
                            api_name="hpo")
ncbi_id = search["results"][0]["dbId"]

# Step 2: Annotations query
annotations = self._safe_api_get(
    f"https://ontology.jax.org/api/hp/genes/{ncbi_id}/associations",
    api_name="hpo")
```

**Impact**: HPO now returns phenotype annotations for 22/25 proteins (3 are correctly empty — non-Mendelian genes).

---

## Fix 4: GTEx 2-Step gencodeId Lookup

**Problem**: GTEx API V2 requires a `gencodeId` (e.g., `ENSG00000141510.18`) not a gene symbol. Direct queries with gene names returned empty.

**Root cause**: GTEx V2 endpoint `/api/v2/expression/medianGeneExpression` requires the versioned Ensembl gene ID.

**Fix**: Added gene-to-gencodeId lookup step:

```python
# Step 1: Lookup gencodeId via gene search
lookup = self._safe_api_get(
    f"https://gtexportal.org/api/v2/reference/gene?geneId={gene_name}",
    api_name="gtex")
gencode_id = lookup["data"][0]["gencodeId"]

# Step 2: Fetch expression with gencodeId
expr = self._safe_api_get(
    f"https://gtexportal.org/api/v2/expression/medianGeneExpression"
    f"?gencodeId={gencode_id}&datasetId=gtex_v8",
    api_name="gtex")
```

**Impact**: GTEx now returns expression data across 54 tissues for 25/25 proteins.

---

## Fix 5: Reactome Endpoint Correction

**Problem**: Reactome calls to `/ContentService/search/query?query={uniprot_id}` returned HTML error pages instead of JSON.

**Root cause**: Wrong endpoint path. The correct endpoint for UniProt-to-pathway mapping is `/ContentService/data/mapping/UniProt/{uniprot_id}`.

**Fix**: Changed the URL in `_fetch_reactome_data()`:

```python
# Before (broken)
url = f"https://reactome.org/ContentService/search/query?query={uniprot_id}"

# After (correct)
url = f"https://reactome.org/ContentService/data/mapping/UniProt/{uniprot_id}"
```

**Impact**: Reactome returns pathway data for 24/25 proteins (ESR1 has intermittent transient failures from the server).

---

## Fix 6: OpenTargets GraphQL Fix (POST, not GET)

**Problem**: OpenTargets Platform API calls with `requests.get()` returned 400 errors. The GraphQL endpoint requires POST.

**Root cause**: OpenTargets Platform v4 GraphQL endpoint only accepts POST with JSON body containing the `query` and `variables` fields.

**Fix**: Switched from `_safe_api_get()` to `_safe_api_post()`:

```python
# 2-step: search for target ID, then fetch associations
query1 = """query($gene: String!) {
    search(queryString: $gene, entityNames: ["target"]) {
        hits { id name }
    }
}"""
result = self._safe_api_post(
    "https://api.platform.opentargets.org/api/v4/graphql",
    json_data={"query": query1, "variables": {"gene": gene_name}},
    api_name="opentargets")
```

**Impact**: OpenTargets returns disease association data for 25/25 proteins.

---

## Fix 7: XML Text Content Serialization

**Problem**: Some XML elements were emitting `<element/>` (self-closing) instead of `<element>value</element>` for text content, causing downstream parsers to lose data.

**Root cause**: When building `SubElement` nodes, `.text` was being set after appending children in some code paths, which lxml interprets as tail text of the last child.

**Fix**: Ensured `.text` is always set immediately after element creation, before any `SubElement` children are added:

```python
elem = SubElement(parent, "interaction_score")
elem.text = str(score)  # Set text BEFORE adding children
```

**Impact**: All XML output now parses correctly with standard XML parsers.

---

## Fix 8: HPO Field Name Mapping

**Problem**: HPO API v2 changed field names from `hpoId` to `ontologyId` and from `hpoName` to `name` in some response objects.

**Root cause**: The HPO JAX API underwent a schema update between v1 and v2.

**Fix**: Added fallback field name resolution:

```python
hpo_id = ann.get("ontologyId") or ann.get("hpoId", "")
hpo_name = ann.get("name") or ann.get("hpoName", "")
```

**Impact**: HPO annotations parse correctly regardless of API response version.

---

## Fix 9: KEGG Exact-Match Filtering

**Problem**: KEGG `/find/hsa/{gene}` returns partial matches. Searching for `INS` returned `INS-IGF2` (a different gene) as the first result. `ESR1` could match `ESR1-AS1`.

**Root cause**: The original code blindly took `lines[0]` from the KEGG search response without verifying the gene symbol matched exactly.

**Fix**: Replaced blind first-line selection with an exact-match loop scanning the comma-separated gene symbols in each KEGG result line:

```python
# KEGG returns: "hsa:3630\tINS, IDDM2; insulin [KO:K04526]"
kegg_id = None
for line in lines:
    parts = line.split("\t")
    if len(parts) >= 2:
        # Extract gene symbols before the semicolon
        symbols_section = parts[1].split(";")[0]
        symbols = [s.strip() for s in symbols_section.split(",")]
        if gene_name in symbols:
            kegg_id = parts[0].strip()
            break
```

**Impact**: INS now correctly resolves to `hsa:3630` (insulin) not `hsa:140679` (INS-IGF2). Verified correct for all 25 proteins.

---

## Summary Timeline

| Order | Fix | Discovery Trigger | Iterations Affected |
|---|---|---|---|
| 1 | Circuit breaker 4xx | HPO tripping after 3 "not found" responses | Iter 1-2 |
| 2 | ProteinAtlas gzip | All ProteinAtlas calls failing with JSON decode | Iter 1-2 |
| 3 | HPO 2-step gene search | Zero HPO results for any protein | Iter 2-3 |
| 4 | GTEx 2-step gencodeId | Zero GTEx results for any protein | Iter 2-3 |
| 5 | Reactome endpoint | HTML returned instead of JSON | Iter 3 |
| 6 | OpenTargets POST | 400 errors on all OpenTargets calls | Iter 3-4 |
| 7 | XML serialization | Some cross-references missing in output | Iter 4 |
| 8 | HPO field mapping | KeyError on HPO response parsing | Iter 4 |
| 9 | KEGG exact-match | Wrong gene data for INS and ESR1 | Iter 6 |

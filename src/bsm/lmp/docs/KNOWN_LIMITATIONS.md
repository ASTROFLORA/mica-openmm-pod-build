# LMP Generator v4 — Known Limitations

> **Version**: v4.3  
> **Last updated**: 2026-03-22

---

## 1. True Negatives (Expected Missing Data)

These are proteins where specific APIs correctly return no data. These are **not bugs**.

### 1.1 PDB — Mitochondrial Proteins

| Protein | UniProt | PDB Structures | Reason |
|---|---|---|---|
| MT-CO1 | P00395 | 0 | Mitochondrial-encoded, integral membrane — no X-ray/cryo-EM structures deposited |

**Workaround**: AlphaFold provides predicted structures for MT-CO1 (and all mitochondrial proteins). The generator falls back to AlphaFold data when PDB returns 0.

### 1.2 ChEMBL — Targets Without Bioactivity Data

| Protein | UniProt | ChEMBL Target | Bioactivities (pchembl) | Reason |
|---|---|---|---|---|
| INS | P01308 | CHEMBL5881 | 0 | Target exists but has no assays with pchembl_value > 0 |

**Explanation**: INS (insulin) is a hormone, not a typical drug target. ChEMBL has the target entry but no drug-like activity measurements.

### 1.3 HPO — Non-Mendelian Disease Genes

| Protein | UniProt | HPO Annotations | Reason |
|---|---|---|---|
| PTGS2 | P35354 | 0 | Cyclooxygenase-2 — complex disease associations, not monogenic |
| ACE2 | Q9BYF1 | 0 | SARS-CoV-2 receptor — infectious disease, not Mendelian |
| CDK2 | P24941 | 0 | Cell cycle kinase — somatic cancer driver, not germline |

**Explanation**: HPO (Human Phenotype Ontology) catalogs phenotypes of Mendelian (monogenic) diseases. Genes primarily involved in complex diseases, infectious disease, or somatic cancer do not have HPO annotations.

### 1.4 Reactome — Intermittent Transient Failures

| Protein | UniProt | Reactome | Occurrence | Reason |
|---|---|---|---|---|
| ESR1 | P03372 | 0 (sometimes) | Non-deterministic | Reactome ContentService intermittent server issues |

**Explanation**: ESR1 has known Reactome pathways. The occasional 0-result is a transient server-side issue, not a data gap. The circuit breaker handles this correctly (failure count stays below threshold=3, circuit stays closed).

---

## 2. API-Level Limitations

### 2.1 STRING-DB — Single Species

STRING-DB integration is hardcoded to `species=9606` (Homo sapiens). Non-human proteins are not supported.

### 2.2 KEGG — Partial Match Risk

Despite the exact-match fix (Fix 9), KEGG's `/find/hsa/{gene}` endpoint can return unexpected results for genes with unusual naming. The exact-match loop mitigates this but requires that the gene symbol appears exactly as-is in KEGG's comma-separated symbols list.

**Edge case**: Genes with symbols that are substrings of other gene symbols (e.g., `AR` vs `ARAF`, `AR` vs `ARNT`). The current implementation handles this correctly because it matches against the full symbol, not a substring.

### 2.3 OpenTargets — GraphQL Rate Limiting

OpenTargets GraphQL endpoint has undocumented rate limits that are more aggressive than the REST API. Our 0.2s interval (5 req/s) has been validated but may need adjustment under heavy concurrent usage.

### 2.4 ProteinAtlas — Gzip Encoding

ProteinAtlas always returns gzip-compressed responses regardless of `Accept-Encoding`. The generator handles this transparently, but other clients must implement gzip decompression.

### 2.5 GTEx — Dataset Version Lock

GTEx integration is locked to `datasetId=gtex_v8`. When GTEx releases v9+, the hardcoded dataset ID will need updating.

---

## 3. Architectural Limitations

### 3.1 Sequential API Calls

All 14 API enrichments run **sequentially** within `_add_knowledge_graph_v4()`. With per-API rate limits summing to ~20s of wait time per protein, this is the primary bottleneck.

**Potential improvement**: APIs with no cross-dependencies (e.g., STRING-DB and HPO) could run in parallel with `asyncio` or `concurrent.futures`. Not implemented to keep the codebase simple and avoid complex error handling.

### 3.2 No Incremental Updates

The generator creates complete XML from scratch each time. There is no mechanism to update specific sections (e.g., re-enrich only ChEMBL data) without regenerating the entire output.

### 3.3 Memory Usage for Large Proteins

TTN (34,350 AA) generates LMP files of ~1.2 MB. For batch processing of hundreds of large proteins, memory usage could become significant. The generator processes one protein at a time and does not stream XML output.

### 3.4 Cache Invalidation

The disk cache uses a simple TTL (30 days) without checking if upstream data has changed. Rapidly evolving databases (e.g., PDB with new structures) may serve stale data within the TTL window.

---

## 4. Platform Limitations

### 4.1 Disk Space

The generator requires ~500 KB of disk per protein (XML output + cached API responses). For large-scale batch runs (>1,000 proteins), ensure adequate disk space.

### 4.2 Network Dependency

Without network access, the generator operates in `offline_mode` using only cached data. Proteins not previously cached will produce empty enrichment sections.

### 4.3 Python Version

Tested on Python 3.13.7. Some dependencies (lxml, requests) may have version-specific behavior on older Python versions.

---

## 5. Data Quality Caveats

### 5.1 Cross-Reference Counts Vary

The number of cross-references per protein varies widely:

| Metric | Min | Max | Avg |
|---|---|---|---|
| CRefs/protein | ~450 | ~900 | 635 |
| XML size | ~580 KB | ~1.2 MB | 814 KB |

Proteins with larger knowledge graph footprints (e.g., TP53, BRCA1, BRAF) generate significantly more cross-references than smaller or less-studied proteins.

### 5.2 HPO Coverage Ceiling

HPO will never reach 100% coverage because it only catalogs Mendelian diseases. Approximately 12% of the human proteome consists of genes without Mendelian disease associations.

### 5.3 AlphaFold Confidence Varies

AlphaFold predicted structures have variable confidence (pLDDT). Intrinsically disordered regions consistently show low confidence. The generator reports AlphaFold data without filtering by confidence — downstream consumers should check pLDDT scores.

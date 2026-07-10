# LMP Generator v4 — Rate Limiting & Resilience

> **Module**: `generator_v4.py`  
> **Version**: v4.3  
> **Last validated**: 2026-03-22 — 25 proteins, zero 429 responses, zero circuit breaker trips

---

## 1. Rate Limiting Architecture

### 1.1 Per-API Rate Limiters

Each of the 14 integrated APIs has an independent rate limiter tracking its own `last_request_time` and minimum interval. This ensures that a burst of calls to one API does not starve or overwhelm another.

```python
self._api_rate_limits = {
    "uniprot":        0.35,   # ~3 req/s
    "pdb":            0.25,   # ~4 req/s
    "pubchem":        0.25,   # ~4 req/s (documented: 5 req/s)
    "alphafold":      0.25,   # ~4 req/s
    "string":         1.0,    # 1 req/s (STRING-DB recommendation)
    "opentargets":    0.2,    # ~5 req/s
    "chembl":         0.35,   # ~3 req/s
    "kegg":           0.35,   # ~3 req/s (undocumented)
    "reactome":       0.25,   # ~4 req/s
    "protein_atlas":  0.5,    # ~2 req/s (conservative)
    "ensembl":        0.07,   # ~15 req/s (Ensembl allows 15/s)
    "go":             0.2,    # ~5 req/s
    "hpo":            0.5,    # ~2 req/s (conservative)
    "gtex":           0.5,    # ~2 req/s (conservative)
}
```

### 1.2 Rate Limit Enforcement

The `_rate_limit_wait(api_name)` method (line 7523) enforces the minimum interval:

```python
def _rate_limit_wait(self, api_name=None):
    if api_name and api_name in self._api_rate_limits:
        limit = self._api_rate_limits[api_name]
        elapsed = time.time() - self._api_last_request.get(api_name, 0.0)
        if elapsed < limit:
            time.sleep(limit - elapsed)
        self._api_last_request[api_name] = time.time()
    else:
        # Global fallback
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.time()
```

**Key design decisions**:
- Per-API tracking uses `self._api_last_request[api_name]` (dict of floats)
- Global fallback (`self.rate_limit`, default 0.5s) for any call not tagged with an API name
- `time.sleep()` is called with the exact remaining interval (no busy-wait)

### 1.3 Rate Limits vs Documented API Limits

| API | Our Limit | Documented Limit | Headroom |
|-----|-----------|------------------|----------|
| UniProt | 3 req/s | ~3 req/s | ~0% |
| RCSB PDB | 4 req/s | 10 req/s | +150% |
| PubChem | 4 req/s | 5 req/s | +25% |
| AlphaFold | 4 req/s | ~10 req/s | +150% |
| STRING-DB | 1 req/s | 1 req/s recommend | 0% |
| OpenTargets | 5 req/s | ~10 req/s | +100% |
| ChEMBL | 3 req/s | ~3 req/s | ~0% |
| KEGG | 3 req/s | Undocumented | Conservative |
| Reactome | 4 req/s | ~10 req/s | +150% |
| ProteinAtlas | 2 req/s | Undocumented | Conservative |
| Ensembl | 15 req/s | 15 req/s | 0% |
| QuickGO | 5 req/s | ~10 req/s | +100% |
| HPO (JAX) | 2 req/s | Undocumented | Conservative |
| GTEx | 2 req/s | Undocumented | Conservative |

All limits are at or below documented API limits. APIs with undocumented limits use conservative 2 req/s.

---

## 2. HTTP 429 Retry Logic

All three safe API methods (`_safe_api_get`, `_safe_api_post`, `_safe_api_get_text`) handle HTTP 429 (Too Many Requests) identically:

```python
if resp.status_code == 429:
    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
    self.logger.warning("429 from %s — backing off %.1fs (attempt %d/%d)",
                        api_name, retry_after, attempt + 1, max_retries)
    time.sleep(min(retry_after, 30))
    continue
```

**Behavior**:
1. Read `Retry-After` header from the response
2. If absent, use exponential backoff: 1s, 2s, 4s
3. Cap wait time at 30 seconds (prevent pathological `Retry-After` values)
4. Retry up to `max_retries` (default 3) times
5. A 429 does **not** count as a circuit breaker failure

**Validation**: Across 25 proteins (Iter 10, 1616 seconds), **zero 429 responses** were observed.

---

## 3. Circuit Breaker Pattern

### 3.1 Design

Each API has an independent circuit breaker that trips after N consecutive **server-side** failures:

```
CLOSED ──(3 consecutive 5xx/timeout)──→ OPEN ──(120s cooldown)──→ HALF-OPEN
    ↑                                                                  │
    └──────────(1 success)──────────────────────────────────────────────┘
```

### 3.2 Configuration

```python
self._circuit_breaker_threshold = 3    # consecutive failures to trip
self._circuit_breaker_cooldown = 120.0 # seconds before half-open
```

### 3.3 State Tracking

```python
self._api_fail_count: Dict[str, int]    # failures per API
self._api_circuit_open_until: Dict[str, float]  # unix timestamp when cooldown expires
```

### 3.4 Implementation

**Check if open** (`_is_circuit_open`, line 2544):
```python
def _is_circuit_open(self, api_name):
    if self._api_fail_count.get(api_name, 0) >= self._circuit_breaker_threshold:
        if time.time() < self._api_circuit_open_until.get(api_name, 0.0):
            return True
        # Cooldown elapsed — half-open: reset counter, allow one attempt
        self._api_fail_count[api_name] = 0
    return False
```

**Record success** (`_record_api_success`, line 2553):
```python
def _record_api_success(self, api_name):
    self._api_fail_count[api_name] = 0
```

**Record failure** (`_record_api_failure`, line 2556):
```python
def _record_api_failure(self, api_name):
    self._api_fail_count[api_name] += 1
    if self._api_fail_count[api_name] >= self._circuit_breaker_threshold:
        self._api_circuit_open_until[api_name] = time.time() + self._circuit_breaker_cooldown
```

### 3.5 What Counts as a Failure?

| HTTP Status | Counts as failure? | Behavior |
|---|---|---|
| 2xx | No (success) | Reset failure counter |
| 400-428 (4xx, not 429) | **No** | Return None, mark as success (API is alive, just no data) |
| 429 | **No** | Retry with backoff (handled separately) |
| 5xx | **Yes** | Increment failure counter |
| Connection timeout | **Yes** | Increment failure counter |
| DNS / network error | **Yes** | Increment failure counter |

**Critical design decision**: 4xx errors are treated as "no data available" and **reset** the failure counter (call `_record_api_success`). This prevents the circuit from tripping on legitimate "not found" responses (e.g., querying HPO for a gene with no Mendelian disease annotations).

---

## 4. Safe API Methods

Three resilient request methods wrap all external API calls:

### 4.1 `_safe_api_get()` (line 2570)

```python
def _safe_api_get(self, url, *, timeout=15, headers=None, params=None,
                  api_name="generic", max_retries=3) -> Optional[Dict]:
```

- Returns parsed JSON on success, `None` on failure
- Pre-checks: `offline_mode`, `_can_fetch_network()`, `_is_circuit_open()`
- Enforces per-API rate limit before each request
- Handles 429 with retry + backoff
- 4xx → return None + mark as success
- 5xx → retry with exponential backoff
- After all retries exhausted → `_record_api_failure()`

### 4.2 `_safe_api_post()` (line 2624)

Same as `_safe_api_get()` but uses `requests.post()` with `json=json_data`.

Default headers: `Content-Type: application/json`, `Accept: application/json`.

Used by: OpenTargets (GraphQL queries).

### 4.3 `_safe_api_get_text()` (line 2676)

Same resilience pattern but returns raw `resp.text` instead of parsed JSON.

Used by: KEGG (which returns tab-separated text).

---

## 5. Caching

### 5.1 Disk Cache

API responses are cached to disk to avoid redundant requests:

```
{cache_dir}/
  uniprot_P04637.json
  pdb_1TUP.json
  pubchem_STI.json
  alphafold_P04637.json
  ...
```

### 5.2 Cache Configuration

```python
CACHE_TTL_DAYS = 30        # Expiration time
MAX_CACHE_SIZE_MB = 1000   # Maximum cache directory size
```

Cache keys are sanitized with `_CACHE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")`.

---

## 6. Offline Mode

When `offline_mode=True`:
- All network calls are skipped (safe API methods return `None` immediately)
- The generator relies entirely on cached data
- Useful for development, testing, or environments without internet access

---

## 7. Resilience Audit Results

### Iter 9 — Rate Limit Stress Test (5 rapid-fire proteins)

| Metric | Result |
|---|---|
| Proteins tested | TP53, IKBKB, PGR, RELA, MTOR |
| Total time | 510 seconds |
| 429 responses | **0** |
| Circuit breaker trips | **0** |
| API coverage | 5/5 at 11/11 APIs |

### Iter 10 — 25-Protein Regression

| Metric | Result |
|---|---|
| Proteins tested | 25 unique human proteins |
| Total time | 1,616 seconds (27 minutes) |
| 429 responses | **0** |
| Circuit breaker trips | **0** |
| API failures (5xx) | 0 |
| Transient failures | 1 (Reactome ESR1, below threshold, circuit stayed closed) |
| Total API calls | ~350 |
| Cross-references generated | 15,873 |

**Conclusion**: The rate-limiting and circuit-breaker system is battle-tested across 25 diverse proteins with zero rate-limit violations and zero circuit trips.

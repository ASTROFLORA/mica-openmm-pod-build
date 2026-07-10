# TC05 — UniProt Errors + Backoff (429)

**Objetivo**
- Confirmar manejo robusto de caída de red.
- Confirmar backoff en HTTP 429 (rate limit) y que luego recupera.

**Setup**
- Caso A: `requests.request` levanta `Network Down`.
- Caso B: `requests.request` retorna 429 con `Retry-After`, luego 200.

**Expectativa**
- Caso A: `scan_uniprot()` retorna `[]` sin explotar.
- Caso B: `scan_uniprot()` retorna IDs y ejecuta al menos una espera (`sleep`).

**Observado**
- Pasa.

**Post-mortem**
- Se detectó un bug de testabilidad: `sleep_fn=time.sleep` como default se “fijaba” al definir la función.

**Fix aplicado**
- `sleep_fn` ahora es `None` por defecto y se resuelve en runtime, permitiendo `patch('time.sleep')`.

**Notas**
- Backoff es deliberadamente minimalista (3 reintentos). Ajustable si se necesita.

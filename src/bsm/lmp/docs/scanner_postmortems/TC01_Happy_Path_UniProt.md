# TC01 — Happy Path UniProt

**Objetivo**
- Validar que `scan_uniprot()` + `build_dataset()` generan XMLs y registran manifest correctamente.

**Setup**
- `scan_uniprot` mockeado (sin red) retornando `P01308`.
- `generate_multi_state` mockeado retornando 2 estados (`Active`, `Inactive`) con XML mínimo.

**Expectativa**
- 1 registro `OK` en results.
- `dataset_manifest.jsonl` existe y contiene `id`, `kind=uniprot`, `dataset`, `files`.
- Se crean 2 XMLs en `tests/dlm_lmp/scanner_output/tc01_insulin/`.

**Observado**
- Pasa.

**Post-mortem**
- No se detectaron fallas.

**Acción/Optimización**
- Se mantuvo comportamiento actual.

**Riesgos/Notas**
- En producción, el volumen de estados puede variar; el scanner guarda uno por estado.

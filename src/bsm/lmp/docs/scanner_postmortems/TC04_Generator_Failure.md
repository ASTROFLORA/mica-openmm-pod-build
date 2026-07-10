# TC04 — Generator Failure

**Objetivo**
- Validar que si el generador falla, el scanner registra error y continúa.

**Setup**
- `generate_multi_state` mockeado para lanzar `Exception("Simulated Boom")`.

**Expectativa**
- `build_dataset()` devuelve 1 resultado con `status=ERROR` y `error`.
- Se escribe un registro en `dataset_manifest.jsonl`.

**Observado**
- Pasa.

**Post-mortem**
- El failure es intencional.

**Acción/Optimización**
- Se añadió `elapsed_ms` y `kind` al record para diagnóstico.

**Notas**
- El scanner actualmente no implementa “retry por item”; se puede agregar si el caso lo requiere, pero sería una capa extra.

# TC09 — Empty Targets

**Objetivo**
- Evitar side effects si `target_ids` está vacío.

**Setup**
- Llama `build_dataset(target_ids=[], ...)`.

**Expectativa**
- Retorna `[]`.
- No crea directorios ni manifest.

**Observado**
- Pasa.

**Post-mortem**
- Se ajustó el flujo para chequear `not target_ids` antes de crear output.

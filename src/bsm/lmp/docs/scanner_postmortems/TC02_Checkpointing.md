# TC02 — Checkpointing

**Objetivo**
- Validar que el scanner no re-procesa IDs ya generados.

**Setup**
- Re-ejecuta el mismo `uid` y `dataset_name` del TC01.
- Spy sobre `_worker_wrapper`.

**Expectativa**
- `build_dataset()` retorna `[]` (nada pendiente).
- `_worker_wrapper` no se llama.

**Observado**
- Pasa.

**Post-mortem**
- La heurística actual: “existe algún `uid_*.xml` en target_dir”.

**Acción/Optimización**
- Se mantiene por simplicidad (sin sobreingeniería).

**Riesgos/Notas**
- Si cambias el naming o borras archivos parcial, la heurística puede saltarse trabajos; el manifest puede ser una fuente más robusta si se requiere.

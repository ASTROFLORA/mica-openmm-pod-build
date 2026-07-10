# TC06 — PDB Discovery por Ligando

**Objetivo**
- Validar que `scan_pdb_by_ligand()` parsea correctamente la respuesta RCSB Search y retorna PDB IDs.

**Setup**
- Mock de `requests.request` devolviendo `result_set` con `1IEP` y `2ABL`.

**Expectativa**
- Retorna lista exacta de IDs.

**Observado**
- Pasa.

**Post-mortem**
- Sin fallas.

**Notas**
- En producción, conviene paginar si `limit` es grande.

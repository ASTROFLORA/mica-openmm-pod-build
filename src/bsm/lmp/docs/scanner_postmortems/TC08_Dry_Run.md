# TC08 — Dry Run

**Objetivo**
- Garantizar que `dry_run=True` no toca filesystem ni dispara generación.

**Setup**
- Ejecuta `build_dataset(..., dry_run=True)`.

**Expectativa**
- Retorna `None`.
- No crea directorios ni manifest.

**Observado**
- Pasa.

**Post-mortem**
- La primera versión creaba directorio antes de chequear `dry_run`.

**Fix aplicado**
- `dry_run` ahora retorna antes de `_setup_output()`.

# TC10 — Concurrency + Jitter

**Objetivo**
- Confirmar que cada worker aplica jitter (`sleep`) para ser “cortés” con APIs.

**Setup**
- Patch de `time.sleep`.
- `generate_multi_state` mockeado para ser instantáneo.

**Expectativa**
- `time.sleep` llamado al menos una vez por target.

**Observado**
- Pasa.

**Post-mortem**
- Sin fallas.

**Notas**
- Esto no reemplaza un rate limiter global compartido, pero reduce riesgo de bursts. Se mantiene simple para evitar sobreingeniería.

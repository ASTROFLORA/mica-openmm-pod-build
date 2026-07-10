# TC07 — PDB Generation Routing

**Objetivo**
- Asegurar que si se pasan PDB IDs, el scanner usa `generate_from_pdb` (no UniProt flow).

**Setup**
- `generate_from_pdb` mockeado para retornar XML mínimo.

**Expectativa**
- `kind=pdb`, `status=OK`.
- Se crea un archivo `{pdb_id}_pdb.xml`.
- El manifest registra la ejecución.

**Observado**
- Pasa.

**Post-mortem**
- Esto nació como “limitación” inicial del scanner.

**Fix aplicado**
- Auto-detección de PDB IDs (regex) y branch explícito.

**Notas**
- Heurística PDB: 4 chars empezando con dígito. Se puede ampliar si aparecen casos raros.

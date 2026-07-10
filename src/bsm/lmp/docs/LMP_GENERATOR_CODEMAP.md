# LMP Generator — Codemap (índice de navegación)

Este documento es un **índice navegable** de las secciones más importantes del generador.

Archivo principal:
- `src/bsm/lmp/generator.py`

---

## Entry points (lo que llamas desde fuera)

- `class LMPGenerator` (clase principal)
- `LMPGenerator.generate_multi_state(...)` (línea aprox. 432)
- `LMPGenerator.generate_from_pdb(pdb_id, chain_id=None, state_name=None)` (línea aprox. 2841)
- `LMPGenerator.generate_from_mcsa(uniprot_id, catalytic_residues, output_dir)`

---

## Fetchers (capa de datos / integración)

### UniProt

- `LMPGenerator._fetch_uniprot(uniprot_id)` (línea aprox. 558)
  - Incluye caché, reintentos y degradación “minimal payload”.
  - Dispara enrichment vía InterPro.

### PDB (RCSB + FASTA)

- `LMPGenerator._fetch_pdb(pdb_id)` (línea aprox. 722)
  - Entry metadata
  - FASTA → secuencias por cadena
  - Mapeo polymer entity → UniProt cuando está disponible

---

## Ligands pipeline

- `LMPGenerator._fetch_pdb_ligands(pdb_id)` (línea aprox. 985)
  - Extrae ligandos de non-polymer entities
  - Mapea a ChEBI
  - Enriquecimiento PubChem opcional

- `LMPGenerator._map_ligand_to_chebi(ligand_id)` (línea aprox. 1109)

### CCD helpers (PDB Chemical Component Dictionary)

- `LMPGenerator._fetch_ccd(ligand_id, cache_file=None)` (línea aprox. 1164)
- `LMPGenerator._extract_ccd_descriptors(ligand_id)` (línea aprox. 1200)
  - Normaliza/extrae `inchi_key`, `inchi`, `smiles` si existen

---

## PubChem enrichment (opcional, directo REST)

- `LMPGenerator._pubchem_rate_limit_wait()` (línea aprox. 1236)
- `LMPGenerator._pubchem_get_json(url, cache_name=...)` (línea aprox. 1242)
  - Caché en disco (best-effort)

- `LMPGenerator._pubchem_resolve_cid(...)` (línea aprox. 1269)
  - Estrategia: InChIKey → InChI → SMILES → Name

- `LMPGenerator._pubchem_fetch_properties(cid)` (línea aprox. 1312)
- `LMPGenerator._pubchem_fetch_synonyms(cid)` (línea aprox. 1330)

---

## Structure / annotations

- `LMPGenerator._fetch_secondary_structure(pdb_id)` (línea aprox. 1381)
  - PDBe

Otras funciones importantes (según tus necesidades):
- binding sites / pockets
- detección de interfaces PPI
- state inference
- XML emission

---

## Config loading

- El generador intenta importar `yaml` (PyYAML). Si no está disponible, opera con defaults.
- En las versiones recientes, `LMPGenerator` soporta `config_path=...` para cargar settings (best-effort).


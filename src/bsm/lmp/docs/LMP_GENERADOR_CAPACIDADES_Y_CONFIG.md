# LMP v2.0 — Documentación exhaustiva del Generador

Fecha: 2025-12-25

Este documento describe **capacidades**, **configuraciones** y **arquitectura de código** del generador LMP v2.0 implementado en este repo.

> Alcance: el generador (Python) en `src/bsm/lmp/generator.py` y su configuración en `src/bsm/lmp/lmp_config.yaml`. También se mencionan interacciones relevantes con el validador y el XSD.

---

## 1) ¿Qué es el generador LMP?

El generador LMP crea documentos XML “LMP v2.0” para representar una proteína en uno o varios estados funcionales/estructurales.

Tiene dos modos principales:

1. **Generación multi-estado desde UniProt**: dado un `uniprot_id` y estado(s), genera uno o más XMLs con dominios, PTMs, motivos y sitios de unión inferidos/extraídos.
2. **Generación desde PDB**: dado un `pdb_id` (y opcionalmente una cadena), construye un XML con secuencia(s) desde FASTA PDB, mapea a UniProt cuando es posible para anotar, y agrega ligandos, estructura secundaria, sitios de unión e interfaces.

---

## 2) Puntos de entrada (API pública)

### 2.1 `LMPGenerator` (clase principal)

- Archivo: `src/bsm/lmp/generator.py`
- Clase: `LMPGenerator`

### 2.2 Métodos “top-level” más usados

1) `generate_multi_state(...)`
- Entrada: `uniprot_id`, `gene_name`, `organism`, `states` (opcional), `pdb_ids` (opcional)
- Salida: `Dict[state_name, xml_string]`
- Flujo: UniProt → extracción de features (PTM/domains/binding/motifs) → PAS → inferencia de estados → `_generate_lmp_xml`.

2) `generate_from_pdb(pdb_id, chain_id=None, state_name=None)`
- Entrada: PDB id y opcional cadena
- Salida: `xml_string`
- Flujo: RCSB entry + FASTA → parse cadenas → ligandos → sec. estructura (PDBe) → binding sites (RCSB) → detección interfaces → mapeo a UniProt (si existe) → construcción XML.

3) `generate_from_mcsa(uniprot_id, catalytic_residues, output_dir)`
- Entrada: uniprot + lista de residuos catalíticos
- Salida: lista de archivos XML generados en disco

---

## 3) Fuentes de datos externas (APIs)

> Nota: todas las llamadas son **directas** vía `requests` (no MCP).

### 3.1 UniProt REST
- Base: `https://rest.uniprot.org/uniprotkb`
- Endpoint típico: `/{uniprot_id}.json`
- Funciones implicadas:
  - `_fetch_uniprot` (incluye caché + validación)
  - `_try_uniprot_id_mapping` (ID mapping si 404)

### 3.2 InterPro (EBI)
- Base: `https://www.ebi.ac.uk/interpro/api`
- Uso: enriquecer coordenadas de dominios.
- Se consulta desde `_fetch_uniprot`.

### 3.3 RCSB PDB REST
- Base: `https://data.rcsb.org/rest/v1/core`
- Usos:
  - entry metadata: `/entry/{pdb_id}`
  - polymer entity: `/polymer_entity/{pdb_id}/{entity_id}` para mapear cadenas a UniProt
  - nonpolymer entity: `/nonpolymer_entity/{pdb_id}/{entity_id}` para ligandos
  - chemcomp (CCD): `/chemcomp/{ligand_id}` para crossrefs/descritores

### 3.4 RCSB FASTA
- `https://www.rcsb.org/fasta/entry/{pdb_id}`
- Se parsea para obtener secuencias por cadena.

### 3.5 PDBe Secondary Structure
- `https://www.ebi.ac.uk/pdbe/api/pdb/entry/secondary_structure/{pdb_id}`

### 3.6 PubChem PUG REST (enriquecimiento opcional)
- Base: `https://pubchem.ncbi.nlm.nih.gov/rest/pug`
- Endpoints usados:
  - Resolver CID:
    - `/compound/inchikey/{inchikey}/cids/JSON`
    - `/compound/inchi/{inchi}/cids/JSON`
    - `/compound/smiles/{smiles}/cids/JSON`
    - `/compound/name/{name}/cids/JSON`
  - Propiedades:
    - `/compound/cid/{cid}/property/{field1,field2,...}/JSON`
  - Sinónimos (opcional):
    - `/compound/cid/{cid}/synonyms/JSON`

---

## 4) Cache, TTL, y rate limiting

### 4.1 Rate limiting

- El generador aplica rate limiting global con `_rate_limit_wait()`.
- PubChem tiene rate limiting separado y configurable (ver configuración).

### 4.2 Cache de respuestas

El generador mantiene archivos JSON en un directorio de caché.

- Directorio por defecto: `lmp_cache/` (puede venir de config).
- Estrategia:
  - Reusa JSON si el archivo existe y no expiró.
  - Si el cache está corrupto, lo borra y reintenta.

### 4.3 TTL efectivo del cache

Importante: el código del generador usa TTL en **días** y limpieza por tamaño:

- `CACHE_TTL_DAYS` (constante en código, por defecto 30 días)
- `MAX_CACHE_SIZE_MB` (por defecto 1000 MB)

> Nota de diseño: en `lmp_config.yaml` existe `generator.cache_ttl` (segundos) pero actualmente el TTL efectivo del generador está controlado por la constante en código.

---

## 5) Ligandos: extracción + ChEBI + PubChem

### 5.1 Extracción desde PDB

La extracción de ligandos se basa en “non-polymer entities”:

- Se consulta la entry para obtener `non_polymer_entity_ids`.
- Se consulta cada `nonpolymer_entity` para obtener `comp_id` (ligand_id), nombre, fórmula.

Salida (estructura interna típica):

```json
{
  "ligand_id": "ATP",
  "name": "ADENOSINE-5'-TRIPHOSPHATE",
  "formula": "C10 H16 N5 O13 P3",
  "chebi_id": "CHEBI:15422",
  "type": "non-polymer",
  "inchi_key": "... (si CCD lo trae)",
  "smiles": "... (si CCD lo trae)",
  "pubchem": { "cid": 5957, "properties": { ... } }
}
```

### 5.2 Mapeo a ChEBI

- Se usa un diccionario `COMMON_LIGANDS` para ligandos comunes.
- Si no está, se consulta CCD y se busca `pdbx_reference_molecule` con `resource_name == "ChEBI"`.

### 5.3 Descriptores químicos desde CCD

Para enriquecer con PubChem, el generador intenta extraer descriptores del CCD:

- `inchi_key`
- `inchi`
- `smiles`

Esto sucede antes de llamar a PubChem.

### 5.4 Enriquecimiento PubChem (opcional)

Cuando está habilitado (config), el generador:

1. Resuelve un CID usando el mejor identificador disponible:
   - Prioridad: InChIKey → InChI → SMILES → Name
2. Obtiene propiedades configuradas por `property_fields`.
3. Opcional: obtiene sinónimos.
4. Cachea resultados y, si está habilitado, escribe un sidecar JSON.

### 5.5 Sidecar JSON (recomendado para no cambiar esquema)

Por defecto, el enriquecimiento PubChem se persiste en un archivo JSON en `cache_dir`:

- Prefijo configurable: `generator.pubchem.sidecar_prefix` (default: `pubchem_enrichment`)
- Ejemplo: `pubchem_enrichment_6FBK.json`

Esto evita forzar un cambio del XSD.

---

## 6) Configuración: `lmp_config.yaml`

Archivo: `src/bsm/lmp/lmp_config.yaml`

### 6.1 Sección `generator`

Campos relevantes:

- `generator.uniprot_api`: URL base UniProt
- `generator.pdb_api`: URL base PDB core/entry
- `generator.phosphosite_api`: placeholder (puede requerir auth)
- `generator.rate_limit`: rate limit general de requests
- `generator.cache_dir`: directory de cache

#### 6.1.1 Sub-sección `generator.pubchem`

- `enabled`: (bool) habilita enriquecimiento PubChem (default: false)
- `api_base`: base PUG REST
- `timeout_seconds`: timeout `requests.get`
- `rate_limit`: rate limit dedicado para PubChem
- `max_ligands_per_pdb`: tope de ligandos enriquecidos por estructura PDB
- `include_synonyms`: descarga sinónimos (más tráfico)
- `property_fields`: lista de propiedades a solicitar
- `write_sidecar_json`: escribe sidecar JSON con el payload
- `sidecar_prefix`: prefijo del archivo sidecar
- `xml_include_pubchem_cid`: si true, añade `pubchem_cid` como atributo en `<Ligand>` del XML

Recomendación por defecto:
- `enabled: true`
- `write_sidecar_json: true`
- `xml_include_pubchem_cid: false` (mantener schema estable)

---

## 7) Construcción del XML y compatibilidad con esquema

### 7.1 XML generado

- Se construye con `xml.etree.ElementTree`.
- Se formatea con `minidom`.

### 7.2 Nota crítica: deriva vs XSD

Existe una tensión entre el XSD y lo que emite el generador, especialmente en el bloque “Ligands” generado desde PDB:

- El XSD `lmp_v2_schema.xsd` define `LigandType` de forma restringida.
- El generador añade atributos como `formula`, `chebi_id`, `binding_site`, y opcionalmente `pubchem_cid`.

Si planeas validar *estrictamente* el XML emitido por el generador contra el XSD, deberías:

- (A) Ajustar el generador para emitir únicamente lo permitido por el XSD, o
- (B) Ampliar el XSD, o
- (C) Mantener esos campos en sidecar/metadata y no en el XML.

---

## 8) Diagnóstico, logging y degradación

- UniProt usa reintentos con backoff y fallback a payload mínimo si falla.
- Se emiten logs estructurados para llamadas UniProt (para observabilidad).
- Errores de caches corruptos se tratan como no fatales.

---

## 9) Guía de uso rápida

### 9.1 Instanciación

```python
from pathlib import Path
from src.bsm.lmp.generator import LMPGenerator

gen = LMPGenerator(
    config_path=Path("src/bsm/lmp/lmp_config.yaml"),
)
```

### 9.2 Multi-state (UniProt)

```python
docs = gen.generate_multi_state(
    uniprot_id="P12931",
    gene_name="SRC",
    organism="Homo sapiens",
    states=["Inactive", "Active"],
)

for state, xml_str in docs.items():
    Path(f"P12931_{state}.xml").write_text(xml_str, encoding="utf-8")
```

### 9.3 Desde PDB

```python
xml_str = gen.generate_from_pdb("6FBK")
Path("6FBK.xml").write_text(xml_str, encoding="utf-8")
```

---

## 10) Mapa de código (puntos de interés)

- `LMPGenerator.generate_multi_state`: pipeline UniProt → features → estados → XML
- `_fetch_uniprot`: caching + validación + InterPro enrichment
- `_fetch_pdb`: metadata + FASTA + mapping entity→UniProt
- `_fetch_pdb_ligands`: extracción de ligandos + ChEBI + PubChem (opcional)
- `_map_ligand_to_chebi` / `_fetch_ccd`: CCD + crossrefs
- `_pubchem_*`: helpers para PUG REST
- `generate_from_pdb`: pipeline PDB end-to-end

---

## 11) Próximas mejoras sugeridas (no implementadas aquí)

1) Hacer que TTL de cache respete `generator.cache_ttl` (segundos) del YAML.
2) Resolver de forma consistente la compatibilidad con el XSD en el bloque `<Ligands>`.
3) (Opcional) Unificar configuración: que `uniprot_api`/`pdb_api` en YAML realmente reemplacen constantes en código.


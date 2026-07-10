# UniProt OpenAPI Audit → Propuesta de Dataset “Ground Truth” por Proteína

Fecha: 2026-01-17

Este documento audita el contrato **real** expuesto en [src/bsm/lmp/docs/uniprotopenapi.json](src/bsm/lmp/docs/uniprotopenapi.json) y propone una estrategia práctica para construir un **“rayos X” (snapshot) de cada proteína** como dataset de ground-truth: más pesado y más lento, pero **lo más completo y trazable posible**.

## 1) Objetivo operativo

Queremos un artefacto por proteína que permita:
- Reconstruir identidad, contexto biológico, evidencia y posiciones de features sin volver a llamar a UniProt.
- Re-entrenar/validar pipelines NeSy (como LMP v2/v3) usando “verdad de referencia”.
- Reproducibilidad: saber con qué versión/fecha/evidencia fue generado.

## 2) Endpoints relevantes (según OpenAPI)

### 2.1 Entrada por accession
- `GET /uniprotkb/{accession}`
  - Devuelve `application/json` con schema `UniProtKBEntry`.
  - Soporta `fields` para pedir secciones específicas (muy útil para *modo dataset* vs *modo rápido*).
  - Soporta `version` (nota: el propio OpenAPI advierte que con `version` los formatos se restringen a `fasta` y `txt`).

### 2.2 Búsqueda paginada
- `GET /uniprotkb/search`
  - Paginación via `size` con máximo 500 (OpenAPI lo declara explícitamente).
  - Soporta `fields`, `sort`, `includeIsoform`.

### 2.3 Descarga por stream
- `GET /uniprotkb/stream`
  - Para descargas grandes “en un solo download” (OpenAPI menciona límite máximo de 10 millones de entries).
  - Soporta `fields`, `sort`, `includeIsoform`, `download`.
  - El OpenAPI menciona “asynchronous download job” para requests más grandes y pausable/resumible.

## 3) Modelo de datos esencial (schema `UniProtKBEntry`)

En el OpenAPI, `UniProtKBEntry` incluye (entre otros):
- `primaryAccession`, `uniProtkbId`, `secondaryAccessions`
- `entryType`, `active`, `inactiveReason`, `fragment`
- `entryAudit` (incluye `sequenceVersion` y `entryVersion`)
- `annotationScore`, `proteinExistence`
- `proteinDescription`
- `genes`, `geneLocations`
- `organism`, `lineages` (TaxonomyLineage)
- `sequence`
- `features` (`UniProtKBFeature`, con `type`, `location`, `description`, `evidences`, `ligand`, `ligandPart`)
- `comments` (con `commentType` y subtipos, incluyendo FUNCTION, SUBCELLULAR LOCATION, DISEASE, INTERACTION, PATHWAY, PTM, etc.)
- `keywords`
- `uniProtKBCrossReferences`
- `references` (citaciones con crossrefs como PubMed/DOI)
- `internalSection` (líneas internas/evidenceLines; útil para auditoría avanzada)

## 4) “Campos esenciales” para el rayos-X (prioridad + por qué)

Aquí no buscamos minimalismo: buscamos **cobertura**. Aun así, conviene priorizar para (a) poder hacer “modo rápido”, y (b) diseñar almacenamiento normalizado.

### 4.1 Tier A — Identidad y reproducibilidad (SIEMPRE)
Estos campos hacen que el snapshot sea un *ground truth dataset* y no solo un JSON suelto.
- `primaryAccession` + `uniProtkbId` + `secondaryAccessions`
  - Permiten resolver merges/obsoletos y mantener continuidad.
- `entryType`, `active`, `inactiveReason`, `fragment`
  - Permiten saber si el entry es válido/curado y por qué.
- `entryAudit.lastAnnotationUpdateDate`, `entryAudit.lastSequenceUpdateDate`, `entryAudit.sequenceVersion`, `entryAudit.entryVersion`
  - Permiten “cache invalidation” limpio y auditoría temporal.
- `proteinExistence`, `annotationScore`
  - Señalan calidad/evidencia global.

### 4.2 Tier B — Núcleo biológico (SIEMPRE)
- `proteinDescription`
  - “Nombre recomendado”, alternativos, EC numbers, etc. (base para función y para vincular a metabolismo).
- `genes`, `geneLocations`
  - Importante para vínculo genómico.
- `organism` (taxonId, scientificName, commonName, lineages)
  - Identidad taxonómica + contexto evolutivo.
- `lineages` (TaxonomyLineage objects)
  - Útil si quieres grafo taxonómico estructurado (rank/taxonId/nombres).
- `sequence`
  - Es el átomo: sin secuencia no hay “rayos X”.

### 4.3 Tier C — “Rayos X” funcional/estructural (MUY RECOMENDADO)
- `features` (`UniProtKBFeature`)
  - Lo más cercano a una “radiografía” a nivel residuo: dominios, sitios activos, PTMs, regiones, transmembrana, etc.
  - Claves: `type`, `location.start/end`, `description`, `evidences`, `ligand`/`ligandPart` (cuando aplica).
- `comments` (`Comment`)
  - La capa semántica humana: FUNCTION, CATALYTIC ACTIVITY, COFACTOR, PATHWAY, SUBCELLULAR LOCATION, DISEASE, INTERACTION, PTM, DOMAIN, SIMILARITY, CAUTION, SEQUENCE CAUTION.
  - Se puede usar para RAG y para derivar un “SemanticContext” normalizado.
- `keywords` (`Keyword`)
  - Etiquetas controladas por categoría; muy útiles como features discretos.

### 4.4 Tier D — Conectividad de grafo (RECOMENDADO para NeSy-KG)
- `uniProtKBCrossReferences` (`UniProtKBCrossReference`)
  - Es el puente hacia PDB, AlphaFoldDB, InterPro, Pfam, GO, Reactome, ChEBI, etc.
  - Guardar también `properties` y `evidences` porque ahí vive la semántica fina.
- `references` (`UniProtKBReference` → `Citation`)
  - Permite reconstruir evidencia bibliográfica y, crucialmente, anclar claims a PubMed/DOI.

### 4.5 Tier E — Auditoría avanzada (opcional pero valiosa)
- `internalSection`
  - Útil si quieres trazabilidad interna/evidenceLines/curator lines, debugging y dataset forensics.

## 5) Propuesta de formato de dataset (por proteína)

### 5.1 Archivo “snapshot” (raw)
Un archivo por accession (canónico):
- `datasets/uniprot_ground_truth/{accession}/uniprot_entry.json.gz`

Contenido recomendado (wrapper de dataset):
- `meta`:
  - `accession`, `fetched_at`, `source_url`, `request_fields` (si usaste `fields`), `api_server`.
  - `entryType`, `active`, `entryVersion`, `sequenceVersion`, `lastSequenceUpdateDate`, `lastAnnotationUpdateDate`.
  - `sha256` del payload raw.
- `uniprot`:
  - el `UniProtKBEntry` (tal cual, completo).

Motivo: conserva el ground truth *literal* y permite re-procesar con extractores futuros.

### 5.2 Archivo “normalized” (derivado)
Un archivo por accession:
- `datasets/uniprot_ground_truth/{accession}/normalized.json`

Objetivo: representar el “rayos X” en un esquema estable (tu contrato interno), por ejemplo:
- identidad: nombres, gene(s), taxonId, lineage
- secuencia: length, crc/sha, composición opcional
- features normalizados: lista compacta con start/end/type/desc/evidence
- comments normalizados: por tipo → texto(s) + ids si existen
- xrefs normalizados: por database → ids + propiedades claves

Motivo: acelera consumo y reduce coupling a cambios de UniProt.

### 5.3 Manifest global
- `datasets/uniprot_ground_truth/manifest.jsonl`
  - una línea por accession con estado: OK/ERROR, bytes, entryVersion, sequenceVersion, timestamps.

## 6) Soluciones a bottlenecks (propuestas concretas)

### 6.1 Bottleneck: latencia + rate limits por host
Soluciones:
- Throttling compartido por host (ya existe patrón en el scanner): aplicar también en el “downloader” de snapshots.
- Backoff robusto en `429/503` respetando `Retry-After`.
- Concurrencia baja y controlada: `max_workers` pequeño + reservas por host.

### 6.2 Bottleneck: tamaño de datos
Soluciones:
- Comprimir snapshots (`.json.gz`). Para dataset masivo, el ahorro es grande.
- Separar “raw” (completo) de “normalized” (compacto).
- Política de “fields”:
  - Modo dataset: `fields` vacío (o “lo más completo posible”).
  - Modo incremental: pedir solo `features,comments,keywords,uniProtKBCrossReferences,proteinDescription,genes,organism,lineages,entryAudit,sequence`.

### 6.3 Bottleneck: invalidación del cache
Soluciones:
- La clave no es TTL fijo, sino **`entryAudit.sequenceVersion` + `entryAudit.entryVersion`**.
- Regla: si `sequenceVersion` cambia, invalidar absolutamente todo derivado. Si solo cambia `entryVersion`, re-ejecutar normalización semántica.

### 6.4 Bottleneck: IDs obsoletos (merged/deleted)
Soluciones:
- Usar `secondaryAccessions` como mapa “hacia atrás”.
- Registrar `inactiveReason` cuando aplique.
- Integrar (si hace falta) ID mapping batch para listas grandes.

### 6.5 Bottleneck: “textos libres” difíciles de normalizar
Soluciones:
- Guardar raw siempre.
- Para normalized: normalizar primero por `commentType` (FUNCTION, DISEASE, SUBCELLULAR LOCATION, etc.) y conservar la evidencia.
- No intentar “NLP heavy” en el primer paso; guardar y derivar luego.

## 7) Paquete mínimo de campos para un “rayos X usable”

Si mañana quieres un snapshot robusto sin ir a 100% maximalista, el conjunto mínimo recomendado (vía `fields`) sería:
- Identidad: `accession,id,protein_name,gene_names,organism_name,organism_id,keyword,annotation_score,protein_existence`
- Secuencia: `sequence`
- Features: `features` (dominios, PTMs, active site, binding site, transmembrana, etc.)
- Contexto: `cc_function,cc_subcellular_location,cc_disease,cc_pathway,cc_interaction,cc_catalytic_activity,cc_cofactor`
- Grafo: `database(PDB,AlphaFoldDB,InterPro,Pfam,GO,Reactome,ChEBI,...)`
- Evidencia: incluir evidences donde existan.

Nota: los nombres exactos de `fields` son los del endpoint “result-fields” (OpenAPI enlaza a esa lista). Para el dataset, puedes guardar también el string de `fields` usado en `meta.request_fields`.

## 8) Cómo encaja con LMP (v2/v3)

- LMP v2 usa principalmente `sequence` + `features` (dominios/PTMs/binding sites) y fuentes externas PDB/PDBe.
- Para LMP v3 (NeSy-KG) el dataset “ground truth” desde UniProt aporta:
  - `Identity`: `proteinDescription`, `genes`, `organism`, `lineages`, `entryAudit`.
  - `SemanticContext`: `comments` + `keywords`.
  - `KnowledgeGraph`: `uniProtKBCrossReferences` + `references`.

## 9) Próximo paso recomendado

1) Definir el contrato interno `normalized.json` (campos estables + esquema versionado).
2) Implementar un “snapshot downloader” que escriba `uniprot_entry.json.gz` + `manifest.jsonl`.
3) Implementar normalizador que derive `normalized.json` sin perder evidencias.


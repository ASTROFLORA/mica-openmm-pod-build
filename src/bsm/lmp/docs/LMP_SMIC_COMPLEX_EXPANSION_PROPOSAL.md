# LMP + SMIC — Propuesta de Expansión a Complejos (Chain-Based IFP + Métricas de Interfaz)

**Versión:** 1.0  
**Fecha:** 2026-01-22  
**Audience:** Ingeniería/Investigación (personas nuevas a LMP/SMIC)  
**Estado:** Propuesta técnica (con evidencia ejecutada en auditor)

---

## 0. TL;DR

- **LMP** es el formato XML “Language Modeling Protocol” para representar una proteína con **Identity + Semantics + Geometry + KnowledgeGraph + Provenance** (y extensiones como **TrajectoryIFP**).
- **SMIC** es el motor de interacciones (MD-IFP) basado en **MDAnalysis** que calcula huellas de interacción entre dos selecciones (receptor/ligando).
- Hoy ya existe una integración real (sin mocks) en el generador v4: `TrajectoryIFP` se emite dentro del LMP.
- El siguiente salto: soportar **complejos** (proteína–péptido, proteína–proteína, proteína–DNA/RNA) de forma **nativa**:
  - dejar de depender de “ligando pequeño”
  - seleccionar automáticamente por **cadenas**
  - añadir **métricas geométricas/energéticas** de interfaz (auditables)
  - mantener el preset `full` como “lo máximo” (sin recortes)

---

## 1. Contexto y motivación

En biología estructural real, muchos PDBs relevantes no son “proteína + ligando small-molecule”, sino complejos:

- proteína–péptido (motifs, SLiMs, degrons)
- proteína–proteína (PPIs, dominios de anclaje)
- proteína–ácido nucleico

Si el pipeline solo entiende ligandos “no-protein”, se pierden justo los casos donde el **contacto** es el fenómeno biológico principal.

Este repo ya contiene:
- un generador LMP v4 con presets
- un engine de IFP robusto (SMIC)
- un auditor end-to-end que prueba descargas reales y generación real

La propuesta define cómo **expandir** la integración para cubrir complejos sin romper compatibilidad.

---

## 2. Primeros conceptos (para alguien que no conoce LMP)

### 2.1 ¿Qué es LMP?

LMP (Language Modeling Protocol) es un XML con un esquema v4 que organiza información en bloques:

- **Identity**: acceso UniProt, organismo, lineages, etc.
- **Semantics**: nombre, genes, comentarios UniProt, etc.
- **Geometry**: cadenas y dominios; sitios de unión; (opcional) dinámica/IFP.
- **KnowledgeGraph**: cross-references (PDB, KEGG, ChEMBL, etc.), citas, edges derivadas.
- **Provenance**: auditoría de generación, timestamps, ground truth embebido, etc.

Archivos relevantes:
- Esquema: `src/bsm/lmp/lmp_v4_schema.xsd`
- Presets: `src/bsm/lmp/presets.py`
- Generador v4: `src/bsm/lmp/generator_v4.py`

### 2.2 Presets: por qué existen

Los presets permiten producir “exactamente lo necesario” según el consumidor.

Ejemplos típicos:
- `v2-compat`: produce XML legacy (root `PML_Protein`) idéntico al generador v2 donde aplica.
- `md-ifp`: centra el output en el bloque `TrajectoryIFP`.
- `full`: emite todo lo anterior, incluyendo KnowledgeGraph y Provenance.

**Nota importante:** `full` no se considera “ruidoso”: se considera “máxima evidencia”.

---

## 3. Primeros conceptos (para alguien que no conoce SMIC)

### 3.1 ¿Qué es SMIC en este repo?

SMIC es un stack híbrido (JS+Python existe en la visión del repo), pero para esta integración el componente crítico es:

- Engine Python: `workers/smic/python/smic_core/ifp_engine.py`

Este engine usa **MDAnalysis** para:
- cargar topología (PDB/mmCIF/etc.) y trayectoria (DCD/XTC/etc.)
- seleccionar dos grupos de átomos (`receptor_sel` y `ligand_sel`)
- computar interacciones por frame (H-bonds, hydrophobic, aromatic, ionic, etc.)

### 3.2 Qué entrega SMIC

El engine devuelve `IFPTrajectoryResult` con:
- matriz de IFP (frames × columnas)
- lista de contactos
- ocupancias y resumen

Este output ya se serializa a LMP v4 como `<TrajectoryIFP ...>`.

---

## 4. Estado actual de la integración LMP↔SMIC (real, auditable)

### 4.1 Dónde vive la integración

- Orquestación y serialización: `src/bsm/lmp/generator_v4.py`
- Engine IFP: `workers/smic/python/smic_core/ifp_engine.py`

### 4.2 Qué ya funciona hoy (puntos clave)

- Descarga real de PDB (RCSB) cuando se requiere topología.
- Ejecución real de SMIC IFPEngine (sin mocks).
- Emisión de `TrajectoryIFP` dentro del XML v4.

### 4.3 “Complejos por cadena” ya está habilitado

Para PDBs sin ligandos small-molecule pero sí con partner chains (p.ej. péptido), el generador v4:
- intenta descubrir ligandos “no-protein”
- si no hay, cae a **ligando por cadena** (ej. `CHAIN:P`)

Evidencia recomendada (audit):
- Auditor: `scripts/audit_full_6fbk.py`
- Output: `outputs/audit_6fbk/lmp_v4_full.xml`

---

## 5. Problema abierto: complejos de verdad (PPI/peptide/DNA) y “más allá del IFP básico”

Habilitar chain-based IFP resuelve el bloqueo “no hay ligando”, pero aún faltan dos cosas para que esto sea productivo:

1) **Selección robusta** de qué cadenas forman el interfaz principal.
2) **Señales ricas** (geométricas/energéticas) para complejos, no solo el resumen MD-IFP clásico.

---

## 6. Propuesta de expansión (roadmap técnico)

### Capa A — Selección robusta de receptor/ligando por interfaz (para complejos)

**Problema:** “cadena más corta” funciona para proteína–péptido, pero en PPIs grandes puede elegir mal.

**Propuesta:** seleccionar el par de cadenas (i,j) que maximiza evidencia de interfaz:

- A1) Score por contactos rápidos en frame 0
  - contar contactos Cα–Cα < 8Å o heavy-atom < 4.5Å
  - seleccionar top-1 o top-K pares
- A2) Permitir multi-chain ligand
  - `ligand_sel = (protein and chainid B) or (protein and chainid C)`
- A3) Configuración explícita sin romper `full`
  - añadir preset adicional (ej. `complex-ifp`) o knobs en config

**Output esperado:** contexto de selección embebido en `mica:smic_context` (ya existe).

### Capa B — PPI-IFP: contactos residuo↔residuo con identidad de cadena

**Problema:** en PPI, el “ligand_resid” sin cadena pierde interpretabilidad.

**Propuesta:** enriquecer contactos con:
- chain_receptor, chain_ligand
- resid y resname para ambos lados
- (opcional) atom names

**Sin tocar el XSD:**
- serializar como `Property` adicional por contacto o como JSON embebido en `Provenance`.

**Con cambio de XSD (opcional):**
- añadir `InterfaceContacts` o `InterfaceMetrics` dentro de `Geometry`.

### Capa C — Métricas geométricas de interfaz (auditables)

Estas métricas no intentan ser “kcal/mol”, pero sí describen interfaz:

- C1) #contactos heavy-atom y Cα
- C2) hotspots (top residuos por conteo de contactos)
- C3) distancia COM(chain i) ↔ COM(chain j)
- C4) (opcional) ΔSASA aproximado (buried area)

**Recomendación:** guardar como `interface_metrics.json` y/o embed base64 en `Provenance`.

### Capa D — Scoring energético (heurístico, explícito)

**Objetivo:** tener una señal tipo “fuerza” sin prometer energía física exacta.

- D1) score por potencial estadístico (residue-residue potential)
- D2) penalización por burying polar (proxy de desolvación)
- D3) estabilidad temporal (si hay trayectoria): varianza de contactos / persistencia

**Nota:** debe etiquetarse como `heuristic_score`.

### Capa E — Multi-state / multi-interface (modo investigación)

- correr la misma métrica sobre varios PDBs/estados
- agregar media + dispersión
- identificar interfaz “conservada” vs “variable”

---

## 7. Consideraciones de modelo de datos (XML/XSD)

Hay dos estrategias, compatibles entre sí:

### Estrategia 1 (rápida, sin romper schema): adjuntar JSON en Provenance
- Ventajas: cero migración de XSD; auditoría fácil; iteración rápida.
- Desventajas: consumidores XML deben parsear JSON.

### Estrategia 2 (limpia, formal): extender `lmp_v4_schema.xsd`
- Ventajas: output tipado; fácil validación; herramientas XML estándar.
- Desventajas: migración y compatibilidad; requiere versionado del schema.

Recomendación práctica: empezar con Estrategia 1 y estabilizar el contrato; luego formalizar con Estrategia 2.

---

## 8. Validación y auditoría (cómo demostrar que funciona)

### 8.1 Auditoría mínima requerida

- Probar que para un complejo el pipeline produce:
  - selección receptor/ligando consistente
  - `TrajectoryIFP` presente
  - métricas de interfaz (Capa C) presentes

### 8.2 Estructura recomendada de outputs

En `outputs/audit_<pdb>/`:
- `lmp_v4_full.xml`
- `interface_metrics.json` (si aplica)
- `smic_ifp_summary.json`
- `audit_report.json`

### 8.3 Comando de referencia

En Windows (venv MICA):

```powershell
Set-Location 'c:\Users\busta\Downloads\MICA\astroflora-core-feature-spectra-worker-integration-1'
& 'C:\Users\busta\Downloads\MICA\.venv\Scripts\python.exe' -u scripts\audit_full_6fbk.py
```

---

## 9. Riesgos y mitigaciones

- **MDAnalysis no tiene `dt` o cargas:** algunos análisis (H-bonds/water bridges) pueden warn/fallar.
  - Mitigación: fallbacks geométricos; métricas por distancia; registrar warnings en Provenance.
- **chainid vs segid:** depende de cómo MDAnalysis parsea PDB/mmCIF.
  - Mitigación: probar ambos; registrar `ligand_sel` real usado.
- **Complejos multímeros:** puede haber múltiples interfaces.
  - Mitigación: top-K interfaces + reporte.

---

## 10. Plan de trabajo sugerido (entregables)

1) Implementar selección por interfaz (A1) y soporte multi-chain ligand (A2).
2) Emitir contactos con chain IDs (B).
3) Generar `interface_metrics.json` y referenciarlo/embederlo (C).
4) Añadir scoring heurístico (D).
5) Multi-state aggregation (E).

**Criterio de éxito (aceptación):**
- Para un PDB complejo (p.ej. 6FBK), el auditor debe mostrar:
  - `TrajectoryIFP present: True`
  - `ligand` y `ligand_sel` por cadena
  - al menos una métrica geométrica (contact count / hotspots)

---

## 11. Glosario rápido

- **IFP (Interaction Fingerprint):** vector/huella que resume tipos de interacción entre dos grupos (receptor/ligando) por frame.
- **Chain-based ligand:** tratar una cadena (peptídica/proteica) como el “ligando” para calcular interacciones entre cadenas.
- **Interface metrics:** medidas geométricas/estadísticas del contacto entre cadenas (no necesariamente energía física).


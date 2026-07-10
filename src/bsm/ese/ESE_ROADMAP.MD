# ESE Roadmap — Contexto Razonado (Oct 21, 2025)

Este documento resume únicamente lo que hemos razonado hasta ahora sobre las necesidades del ESE. Aún no incluye el diseño detallado de métodos ni la especificación completa de pipelines.

## Visión Arquitectónica Identificada
- **TrajectoryLoader (MDAnalysis):** capa común para leer trayectorias/topologías GPCRmd (con anotaciones ricas) y mdCATH (dominios ciegos), cuidando saneo de frames, slicing temporal y metadatos básicos.
- **DynamicFeatureBank:** colecciones de features físicas y conformacionales (contact maps, RMSD/RMSF segmentadas, ángulos críticos, distancias de bolsillo, redes de comunicación) con interfaz reutilizable `(universe) -> Dict[str, FeaturePayload]`.
- **BiologicalAnnotator:** exclusivo de GPCRmd; enlaza ligandos, estados, clases y sitios alostéricos con las features dinámicas para aportar semántica biológica.
- **BlindAtlasEmitter:** rama paralela para mdCATH que genera huellas “blind domain” sin anotaciones, etiquetadas para evitar interpretaciones biológicas indebidas.
- **ESEAssembler:** responsable de transformar las features (sean anotadas o ciegas) en la firma espectral de 512 dimensiones; aquí también decidimos fusionar otras modalidades como MDGraphEmb.

## Requisitos Iniciales
- Inventariar endpoints y archivos GPCRmd necesarios (trayectorias, topologías, anotaciones richer).
- Documentar las diferencias de formato entre entradas GPCRmd y mdCATH y cómo detectarlas automáticamente.
- Establecer reglas de validación: número mínimo de frames, unidades, manejo de átomos faltantes.

## Feature Stack Termo-Física (Huella por Dominio/Región)
- Añadir señales energéticas por frame (potencial/kinética) reutilizando datos de GPCRmd o recalculando con OpenMM cuando falten.
- Medir ocupación de agua, modos PCA de baja frecuencia y dinámicas de ligandos/comunicación (ej. betweenness TM3–TM6, toggles conformacionales).
- Consolidar estadísticos por dominio (mdCATH) y por región funcional (GPCRmd) en un “ThermoDynamicsAtlas” con bandera `semantics = annotated|blind`.

## Fusión Semántica Multimodal
- Usar metadatos GPCRmd (ligandos, mutaciones, pathways, etiquetas de comunicación) para generar embeddings textuales con PubMedBERT y, opcionalmente, FedCoder tal como hace SPACE.
- Diseñar la concatenación controlada: `ESE512 + Thermo256 + PubMedBERT768 (+ FedCoder opcional)`, con pesos configurables según modalidad disponible.
- Para mdCATH mantener únicamente las componentes físicas/espectrales y marcar `has_bio_semantics = false`.

## Estrategia de Validación
- **Clustering cuantitativo:**
	- mdCATH → comparar agrupamientos (KMeans/HDBSCAN) contra etiquetas de dominio usando ARI/NMI.
	- GPCRmd → medir ARI/NMI/Silhouette frente a clase GPCR, estado activo/inactivo y familia.
- **Exploración visual:** UMAP/PaCMAP y animaciones de densidad para revisar la separación de estados.
- **Calidad numérica:** guardas de NaN/Inf, verificación de dimensiones y reproducibilidad de PCA/descomposición espectral.
- **Telemetría continua:** dashboards con varianza explicada, correlación entre modos y etiquetas; alertas cuando ARI caiga bajo umbrales (ej. 0.7).

## Preguntas Abiertas
- ¿Qué eventos biológicos específicos debe priorizar ESE (ej. toggle TM3–TM6, redes de agua internas)?
- ¿Mantener MDGraphEmb como modalidad adicional o reemplazarlo completamente con señales generadas por MDAnalysis?
- ¿Almacenar blind vs annotated en colecciones separadas o sólo con flags dentro de la misma cápsula Milvus/Neo4j?

## Próximos Pasos (Aún no ejecutados)
- Crear el módulo `src/bsm/ese/io/mdanalysis_loader.py` con pruebas mínimas (`.psf/.dcd`, `.pdb/.xtc`).
- Implementar los extractores de features prioritarios y las validaciones asociadas.
- Definir contenedores de datos para la fusión multimodal y preparar fixtures GPCR vs mdCATH para testing.
- Preparar métricas e infraestructura de observabilidad antes de conectar con producción.

> Nota: Esta versión del roadmap captura únicamente las ideas consolidadas durante la etapa de análisis. Los detalles de implementación, pipelines automatizados y políticas de despliegue se añadirán en fases posteriores.
# CUL — Contract Unification Layer (Capa de Unificación de Contratos)

## Propósito

El Namespace `mica.contracts.unified` es una capa canónica **aditiva y propose-only**. 

**Regla de oro:**
- Las especificaciones congeladas (frozen specs) de cada lane (`Lane HN`, `Lane LO`, `Lane BSM`, `Lane CS`, `Lane I`, `SLICE P0`, etc.) son la **única y absoluta fuente de verdad (source of truth)** de sus respectivos contratos.
- La CUL **nunca** actúa como autoridad de verdad ni redefine las especificaciones congeladas en origen. Su único trabajo es actuar como **traductor/adaptador** bidireccional puro.
- Nada reemplaza a las especificaciones originales; los adapters mapean de forma segura (e informan errores con blockers tipados de tipo `cul_lossy_mapping` si hay pérdidas de información en la traducción).

## Estructura

- `types.py`: Contiene las definiciones de esquemas unificados (`UnifiedTrustState`, `UnifiedVisibility`, `UnifiedTypedBlocker`, `UnifiedTenancyContext`, `UnifiedSecretMountReceipt`, `UnifiedReceiptCore`, `UnifiedCodeIdentity`).
- `adapters.py`: Contiene las funciones puras de mapeo bidireccional (`to_unified_*` y sus inversas).

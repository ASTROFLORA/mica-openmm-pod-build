# LMP Scanner — Guía de Uso

El `LMPScanner` (`src/bsm/lmp/scanner.py`) es el orquestador para generar datasets masivos.

## Características
- **Discovery**: Búsqueda integrada en UniProt y PDB.
- **Robustez**: Checkpointing (no repite trabajo), throttling compartido por host (anti-burst), backoff 429/503, Manifest (log JSONL).
- **Metadata**: Inyecta tags de contexto en los XMLs generados.
- **Escala**: `scan_uniprot()` pagina automáticamente (tamaño de página <= 500) usando `Link: rel="next"`.

## Ejemplo: Generar Dataset de Quinasas Humanas

```python
from src.bsm.lmp.scanner import LMPScanner

# 1. Inicializar
scanner = LMPScanner()

# 2. Buscar IDs (Discovery)
# "family:kinase AND organism_id:9606 AND reviewed:true"
ids = scanner.scan_uniprot("family:kinase AND organism_id:9606 AND reviewed:true", limit=50)

# 3. Generar Dataset (Batch)
# Esto creará ./output/human_kinome/ con los XMLs y un dataset_manifest.jsonl
scanner.build_dataset(
    target_ids=ids, 
    dataset_name="human_kinome",
    context_tags={"source": "uniprot_query_kinase", "organism": "human"},
    max_workers=4
)
```

## Ejemplo: Generar Dataset por Ligando (Drug Repurposing)

```python
# Buscar estructuras con Imatinib (STI)
pdb_ids = scanner.scan_pdb_by_ligand("STI", limit=100)

# Generar (el scanner detecta PDB IDs y enruta a generate_from_pdb automáticamente)
scanner.build_dataset(
    target_ids=pdb_ids,
    dataset_name="pdb_sti",
    context_tags={"source": "rcsb_ligand_search", "ligand": "STI"},
    max_workers=2,
)
```

## API de Alta Usabilidad: Query → Dataset

```python
scanner = LMPScanner()

scanner.build_dataset_from_uniprot_query(
    query="family:kinase AND organism_id:9606 AND reviewed:true",
    dataset_name="human_kinome",
    limit=50,
    max_workers=2,
)
```

## Seguridad Anti-Ban / Runs Grandes (ej: 10,000 targets)

Recomendación práctica:
- Mantén `max_workers` bajo (1–4) para corridas largas.
- Usa el throttling compartido del scanner (por defecto limita el ritmo por host). Si necesitas más agresividad, ajusta `request_min_interval_s`.
- Considera desactivar PubChem para corridas masivas (o usar configuración “safe”), ya que multiplica requests por target.

Ejemplo conservador:

```python
scanner = LMPScanner(
    request_min_interval_s=0.25,  # ~4 req/s por host (compartido entre hilos)
    worker_jitter_s=(0.5, 2.0),
)

scanner.build_dataset_from_uniprot_query(
    query="family:kinase AND organism_id:9606 AND reviewed:true",
    dataset_name="human_kinome_10k",
    limit=10_000,
    max_workers=2,
)
```

## Configuración
El scanner usa la misma configuración que el generador (`src/bsm/lmp/lmp_config.yaml`).

Notas:
- El throttling del scanner es adicional al del generador (y solo aplica a discovery del scanner).

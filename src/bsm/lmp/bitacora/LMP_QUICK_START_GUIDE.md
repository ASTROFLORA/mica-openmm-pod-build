# ⚡ LMP v2.0 - QUICK START GUIDE

> **Last Updated**: Nov 2, 2025 (from LMPLOGS.MD analysis)  
> **Current Status**: 85% complete, testing M-CSA 10 proteins  
> **Next Milestone**: Complete testing → Approve full corpus generation

---

## 🚀 COMANDO INMEDIATO (AHORA)

```powershell
# 1. Navegar al proyecto
cd C:\Users\busta\Downloads\MICA

# 2. Activar venv
.\.venv\Scripts\Activate.ps1

# 3. Verificar dependencias
pip list | Select-String "lxml|pyyaml|pandas"

# 4. Si faltan dependencias:
pip install lxml pyyaml pandas requests

# 5. Ejecutar test M-CSA 10
python test_lmp_module.py mcsa_10

# 6. Revisar resultados
cat test_lmp_mcsa_sample\logs\test_mcsa_10.log

# 7. Si pasa, ejecutar M-CSA 100
python test_lmp_module.py mcsa_100
```

---

## 📋 TESTS DISPONIBLES

| Comando | Descripción | Status | ETA |
|---------|-------------|--------|-----|
| `python test_lmp_module.py synthetic` | Proteínas sintéticas (c-Src: Active/Inactive/Inhibitor-bound) | ✅ PASSING | 30 seg |
| `python test_lmp_module.py mcsa_10` | 10 proteínas M-CSA (validation básica) | 🔄 RUNNING | 2 min |
| `python test_lmp_module.py mcsa_100` | 100 proteínas M-CSA (scaling validation) | ⏸️ PENDING | 5 min |

**Validaciones automáticas**:
- ✅ XSD schema compliance
- ✅ Cross-reference integrity (PTM/Ligand triggers)
- ✅ Functional state auto-update
- ✅ Multi-state data augmentation (1→2-3 examples)
- ✅ Biology checks (PTM-residue compatibility)

---

## 🔧 SI HAY ERRORES

### Error 1: `state_annotator.py` - `load_training_dataset()`

**Síntoma**:
```
AttributeError: 'BudoConformation' object has no attribute 'functionalState'
```

**Fix**:
```python
# Archivo: src/bsm/lmp/state_annotator.py
# Línea ~200

# ANTES (incorrecto):
state = conformation.functionalState

# DESPUÉS (correcto):
state = conformation.state_name  # 'Active', 'Inactive', etc.
```

---

### Error 2: XSD Validation Failed

**Síntoma**:
```
lxml.etree.XMLSyntaxError: Start tag expected
```

**Fix**:
```powershell
# Usar XSD limpio (ya creado)
# Archivo: src/bsm/lmp/lmp_v2_schema.xsd

# Verificar que empieza con <?xml version="1.0"?>
# Sin comentarios Python antes del XML
```

---

### Error 3: Import Errors

**Síntoma**:
```
ModuleNotFoundError: No module named 'lxml'
```

**Fix**:
```powershell
# Instalar dependencias completas
.\.venv\Scripts\python.exe -m pip install lxml pyyaml pandas requests

# Verificar instalación
python -c "import lxml; import yaml; import pandas; print('OK')"
```

---

## 📦 GENERAR CORPUS COMPLETO (Cuando tests pasen)

### Paso 1: Preparar M-CSA Dataset

**Opción A: Download from M-CSA**
```powershell
# URL: https://www.ebi.ac.uk/thornton-srv/m-csa/
# Download: M-CSA database (CSV format)
# Save as: mcsa_dataset.csv
```

**Opción B: Formato esperado**
```csv
uniprot_id,ec_number,catalytic_residues,protein_name
P12931,2.7.10.2,"Y419,K295,D386",Proto-oncogene tyrosine-protein kinase Src
P00698,1.1.1.1,"S139,Y151,K174",Alcohol dehydrogenase 1
P00766,3.4.21.4,"H57,D102,S195",Chymotrypsinogen A
...
```

---

### Paso 2: Generar LMP Corpus (Automatizado)

```powershell
# Generar para todas las proteínas M-CSA
python -m bsm.lmp.generator `
    --input mcsa_dataset.csv `
    --output lmp_corpus_mcsa `
    --states 3 `
    --num-proteins 1003 `
    --validate `
    --cache-dir lmp_cache

# Parámetros:
#   --states: Número de estados por proteína (2-3)
#   --validate: XSD validation on-the-fly
#   --cache-dir: Cache API responses (UniProt/PDB)
```

**Timeline**: 3 días (automatizado)  
**Output**: `lmp_corpus_mcsa/` con 2,000-3,000 XML files

---

### Paso 3: Validar Corpus (Batch)

```powershell
# Validar todos los XML files
python -m bsm.lmp.validator `
    --input lmp_corpus_mcsa `
    --output validation_report.json `
    --xsd src/bsm/lmp/lmp_v2_schema.xsd

# Revisar reporte
python -c "import json; print(json.load(open('validation_report.json'))['summary'])"
```

**Expected**:
```json
{
  "total_files": 2500,
  "valid": 2475,
  "invalid": 25,
  "validation_rate": 0.99
}
```

---

### Paso 4: Parse Corpus → BudoV3 Objects

```powershell
# Convertir XML → BudoV3 JSON objects
python -m bsm.lmp.parser `
    --input lmp_corpus_mcsa `
    --output budo_objects `
    --multi-state `
    --resolve-cross-refs

# Output: budo_objects/
#   ├── P12931_c-Src_Active.json
#   ├── P12931_c-Src_Inactive.json
#   └── ...
```

---

### Paso 5: Anotar para ChronosFold Training

```powershell
# Generar dataset de entrenamiento
python -m bsm.lmp.state_annotator `
    --input lmp_corpus_mcsa `
    --output chronosfold_dataset `
    --link-ese-signatures `
    --export-format pytorch

# Output: chronosfold_dataset/
#   ├── train.pt (2,000 examples)
#   ├── val.pt (300 examples)
#   ├── test.pt (200 examples)
#   └── state_prototypes.pt
```

---

## 🧠 INTEGRAR CON CHRONOSFOLD-MDGE

### Código de Ejemplo (Python)

```python
import torch
from bsm.lmp import LMPParser, StateAnnotator
from chronosfold.models import ChronosFoldMDGE
from chronosfold.losses import LMPStateAwareContrastiveLoss

# 1. Load LMP-annotated dataset
annotator = StateAnnotator()
train_data = annotator.load_training_dataset(
    "chronosfold_dataset",
    include_ese=True
)

# train_data: List[Dict]
# [
#   {
#     "protein_id": "budo:P12931_c-Src_v1",
#     "state": "Active",
#     "sequence": "MGSNKSKPK...",
#     "ese_signature": "ese_kinase_active_loop_open",
#     "ptms": [{"ptm_id": "pY419", "status": "present"}],
#     "catalytic_residues": [419, 295, 386]
#   },
#   ...
# ]

# 2. Initialize model
model = ChronosFoldMDGE(
    esm_c_dim=1280,
    gearnet_dim=512,
    hidden_dim=1024,
    num_states=4  # Active, Inactive, Apo, Holo
)

# 3. Initialize LMP contrastive loss
lmp_loss = LMPStateAwareContrastiveLoss(
    temperature=0.07,
    num_states=4
)

# 4. Training loop
for batch in train_loader:
    # Forward pass
    embeddings = model(batch)
    
    # Compute state-aware contrastive loss
    loss_contrastive = lmp_loss(
        embeddings=embeddings,
        lmp_states=batch["states"],  # ['Active', 'Inactive', ...]
        lmp_prototypes=model.state_prototypes
    )
    
    # Compute task loss (catalytic site prediction)
    loss_task = F.binary_cross_entropy_with_logits(
        model.predict_catalytic(embeddings),
        batch["catalytic_labels"]
    )
    
    # Total loss
    loss = 0.3 * loss_task + 0.7 * loss_contrastive
    
    # Backward + optimize
    loss.backward()
    optimizer.step()
```

---

## 📊 VERIFICAR PROGRESO

### Métricas de Testing

```powershell
# Visualizar estadísticas del corpus
python -c "
import json
stats = json.load(open('test_lmp_mcsa_sample/statistics.csv'))
print(f'Total proteins: {stats[\"total_proteins\"]}')
print(f'Total LMP docs: {stats[\"total_lmp_docs\"]}')
print(f'Avg states per protein: {stats[\"avg_states_per_protein\"]:.2f}')
print(f'XSD validation rate: {stats[\"xsd_validation_rate\"]:.2%}')
"
```

**Targets**:
- ✅ Total proteins: 10 (test) → 100 (validation) → 1,003 (full)
- ✅ Total LMP docs: 25 (test) → 250 (validation) → 2,500 (full)
- ✅ Avg states per protein: 2.0-2.5
- ✅ XSD validation rate: >99%

---

### Métricas de ChronosFold Training

```python
# Tracking metrics con MLflow/WandB
import mlflow

mlflow.log_metric("auprc", 0.45)  # Target: ≥ 0.45 (Phase 1)
mlflow.log_metric("auroc", 0.78)  # Target: ≥ 0.78
mlflow.log_metric("precision_at_10", 0.30)  # Target: ≥ 30%

# State-specific metrics
for state in ["Active", "Inactive", "Apo", "Holo"]:
    state_auprc = evaluate_state(model, test_data, state)
    mlflow.log_metric(f"auprc_{state}", state_auprc)
```

---

## 🎯 ROADMAP EJECUTIVO

| Semana | Tarea | Comando | Output |
|--------|-------|---------|--------|
| **0 (NOW)** | M-CSA 10 test | `python test_lmp_module.py mcsa_10` | Test passing ✅ |
| **0 (HOY)** | M-CSA 100 test | `python test_lmp_module.py mcsa_100` | Scaling validated ✅ |
| **1** | Generate corpus | `python -m bsm.lmp.generator --input mcsa_dataset.csv` | 2,000-3,000 XML ✅ |
| **1** | Validate corpus | `python -m bsm.lmp.validator --input lmp_corpus_mcsa` | >99% valid ✅ |
| **1** | Annotate dataset | `python -m bsm.lmp.state_annotator --output chronosfold_dataset` | Training data ✅ |
| **2** | Implement Phase 1 | Code ChronosFold-MDGE + LMP loss | Model ready ✅ |
| **2** | Train Phase 1 | A40 10h, $10 | AUPRC ≥ 0.45 ✅ |
| **3-5** | Phase 2 (MD) | Add MDGraphEMB | AUPRC 0.55 ✅ |
| **6-7** | Phase 3 (MSA) | Add MSA Transformer | AUPRC 0.60-0.65 ✅ |

---

## 📚 ARCHIVOS IMPORTANTES

**Core LMP Module**:
```
src/bsm/lmp/
├── parser.py              # 450 lines ✅
├── generator.py           # 550 lines ✅
├── validator.py           # 450 lines ✅
├── state_annotator.py     # 400 lines ✅
├── lmp_v2_schema.xsd      # 300 lines ✅
└── lmp_config.yaml        # 250 lines ✅
```

**BUDO Schema**:
```
src/bsm/schemas/budo_v3.py  # +200 lines LMP extensions ✅
```

**Testing**:
```
test_lmp_module.py          # Test suite ✅
```

**Documentation** (NUEVOS):
```
LMP_CURRENT_STATUS_AND_NEXT_STEPS.md  # 40 pages technical
LMP_EXECUTIVE_SUMMARY.md              # 15 pages concise
LMP_INTEGRATION_ROADMAP.md            # Visual architecture
LMP_QUICK_START_GUIDE.md              # This file (cheat sheet)
```

---

## 🚨 TROUBLESHOOTING

### Problema: Test se cuelga

**Solución**:
```powershell
# Verificar procesos Python
Get-Process python

# Terminar si es necesario
Stop-Process -Name python -Force

# Re-run con timeout
timeout /t 300 python test_lmp_module.py mcsa_10
```

---

### Problema: API rate limit (UniProt/PDB)

**Solución**:
```python
# Editar: src/bsm/lmp/lmp_config.yaml

generator:
  api_rate_limit: 5  # Reduce de 10 a 5 requests/sec
  retry_attempts: 5
  retry_delay: 2.0   # Incrementar delay
```

---

### Problema: Out of memory

**Solución**:
```powershell
# Generar corpus en batches
python -m bsm.lmp.generator `
    --input mcsa_dataset.csv `
    --output lmp_corpus_mcsa `
    --batch-size 100 `  # Process 100 proteins at a time
    --start-idx 0 `
    --end-idx 100
```

---

## ✅ CHECKLIST FINAL

**Antes de generar corpus completo**:

- [ ] ✅ Synthetic test PASSING
- [ ] 🔄 M-CSA 10 test PASSING
- [ ] ⏸️ M-CSA 100 test PASSING
- [ ] ⏸️ M-CSA dataset CSV downloaded/prepared
- [ ] ⏸️ XSD validation >99%
- [ ] ⏸️ Performance <5 min for 100 proteins

**Cuando todos estén ✅**:

```powershell
# APROBACIÓN FINAL: Generar corpus completo
python -m bsm.lmp.generator --input mcsa_dataset.csv --output lmp_corpus_mcsa --states 3
```

---

## 💡 TIPS

1. **Cache API responses**: Ahorra tiempo y dinero
   ```powershell
   # Cache se guarda en: lmp_cache/
   # Reutilizable entre runs
   ```

2. **Parallel processing**: Acelera generación
   ```powershell
   # Usar multiprocessing (ya implementado en generator.py)
   # Default: 4 workers
   ```

3. **Incremental validation**: Valida mientras generas
   ```powershell
   # Flag --validate en generator ya hace esto
   ```

4. **Version control**: Commitea el corpus
   ```bash
   git add lmp_corpus_mcsa/
   git commit -m "LMP v2.0 corpus (2,500 docs, M-CSA 1,003 proteins)"
   ```

---

## 🎯 DECISION POINT

**Pregunta**: ¿Proceder con generación full-scale?

**Si M-CSA 10 y 100 tests PASAN** → ✅ **SÍ, PROCEDER**

**Si hay problemas** → Fix primero, luego re-evaluar

---

## 📞 SOPORTE

**Documentación completa**:
- `LMP_CURRENT_STATUS_AND_NEXT_STEPS.md` - Technical deep dive
- `LMP_EXECUTIVE_SUMMARY.md` - Visual summary
- `LMP_INTEGRATION_ROADMAP.md` - Architecture diagrams
- `LMPLOGS.MD` - Full history (3,776 lines)

**ByteRover Knowledge**:
```python
from mcp_byterover import retrieve_knowledge

knowledge = retrieve_knowledge("LMP v2.0 module implementation")
```

---

**STATUS**: 🟢 **READY** (85% complete, testing in progress)

**NEXT ACTION**: `python test_lmp_module.py mcsa_10` ← **EXECUTE NOW**

---

⚡ **LMP v2.0 - State-Aware Protein Modeling Made Simple** ⚡

# 🎯 LMP v2.0 Implementation Roadmap

> **Timeline**: 7 weeks (Nov 2025 - Dec 2025)  
> **Goal**: State-aware protein modeling with ChronosFold-MDGE  
> **Target**: AUPRC 0.30 → 0.45 (+50%) → 0.60-0.65

---

## 📅 Overview

```
Week 0 ━━━━━━━━━━━━━━━━━━━━━━━┓
                                ┃ Testing & Validation
Week 1 ━━━━━━━━━━━━━━━━━━━━━━━┛
       ━━━━━━━━━━━━━━━━━━━━━━━┓
Week 2 ━━━━━━━━━━━━━━━━━━━━━━━┫ Corpus Generation
       ━━━━━━━━━━━━━━━━━━━━━━━┛
       ━━━━━━━━━━━━━━━━━━━━━━━┓
Week 3 ━━━━━━━━━━━━━━━━━━━━━━━┫ Phase 1: Baseline (ESM-C + GearNet + LMP)
       ━━━━━━━━━━━━━━━━━━━━━━━┛
       ━━━━━━━━━━━━━━━━━━━━━━━┓
Week 4 ━━━━━━━━━━━━━━━━━━━━━━━┫
       ━━━━━━━━━━━━━━━━━━━━━━━┫ Phase 2: + Dynamics (MDGraphEMB)
Week 5 ━━━━━━━━━━━━━━━━━━━━━━━┛
       ━━━━━━━━━━━━━━━━━━━━━━━┓
Week 6 ━━━━━━━━━━━━━━━━━━━━━━━┫ Phase 3: + Evolution (MSA)
       ━━━━━━━━━━━━━━━━━━━━━━━┫
Week 7 ━━━━━━━━━━━━━━━━━━━━━━━┛
```

---

## Week 0: Testing & Validation (NOW - Nov 2-8, 2025)

### 🎯 Objective
Complete LMP module testing and validate scalability.

### 📋 Tasks

#### Day 1-2: M-CSA 10 Protein Test
- [🔄] **Fix state_annotator.py** (if needed)
  - Review line ~200: `load_training_dataset()`
  - Verify LMP state → FunctionalState enum mapping
  - Test cross-reference resolution

- [🔄] **Run M-CSA 10 test**
  ```powershell
  python test_lmp_module.py mcsa_10
  ```

- [🔄] **Validate results**
  - XSD validation rate: Target >99%
  - Multi-state coverage: Target 2.0-2.5 states/protein
  - Parse success rate: Target 100%

**Deliverable**: Test report showing all validations passing

#### Day 3-4: M-CSA 100 Protein Test (Scaling)
- [⏸️] **Run M-CSA 100 test**
  ```powershell
  python test_lmp_module.py mcsa_100
  ```

- [⏸️] **Performance validation**
  - Total time: Target <5 minutes
  - Memory usage: Target <4GB
  - API rate limiting: Verify no errors

- [⏸️] **Edge case validation**
  - Multi-chain proteins
  - Nested domains
  - Missing PTM data
  - Incomplete PDB structures

**Deliverable**: Performance report + edge case handling validation

#### Day 5-7: Prepare M-CSA Dataset
- [⏸️] **Download M-CSA database**
  - Source: https://www.ebi.ac.uk/thornton-srv/m-csa/
  - Format: CSV with columns: `uniprot_id`, `ec_number`, `catalytic_residues`, `protein_name`
  - Total entries: 1,003 enzymes

- [⏸️] **Data cleaning**
  - Remove duplicates
  - Validate UniProt IDs
  - Check catalytic residue format
  - Add missing EC numbers (if possible)

- [⏸️] **Create dataset splits**
  ```csv
  # train.csv (70%): 702 proteins
  # val.csv (15%): 150 proteins
  # test.csv (15%): 151 proteins
  ```

**Deliverable**: Clean M-CSA dataset ready for corpus generation

### ✅ Success Criteria
- [ ] M-CSA 10 test: 100% passing
- [ ] M-CSA 100 test: <5 min, >99% XSD valid
- [ ] M-CSA dataset: 1,003 proteins, clean CSV
- [ ] Performance: Scalable to 1,000+ proteins

### 💰 Cost: $0 (testing only)

---

## Week 1-2: Corpus Generation (Nov 9-22, 2025)

### 🎯 Objective
Generate complete LMP v2.0 corpus (2,000-3,000 XML documents) for M-CSA dataset.

### 📋 Tasks

#### Week 1, Day 1-3: Automated Generation
- [⏸️] **Setup generation pipeline**
  ```powershell
  # Configure API credentials (if needed)
  # UniProt: Public API (no key)
  # PDB: Public API (no key)
  # PhosphoSitePlus: Optional (requires registration)
  ```

- [⏸️] **Run generator for train set** (702 proteins)
  ```powershell
  python -m bsm.lmp.generator \
      --input mcsa_train.csv \
      --output lmp_corpus/train \
      --states 3 \
      --validate \
      --cache-dir lmp_cache \
      --parallel-workers 4
  ```
  - Expected time: 8-12 hours
  - Expected output: ~1,400-2,100 XML files

- [⏸️] **Run generator for val set** (150 proteins)
  ```powershell
  python -m bsm.lmp.generator \
      --input mcsa_val.csv \
      --output lmp_corpus/val \
      --states 3
  ```
  - Expected time: 2-3 hours
  - Expected output: ~300-450 XML files

- [⏸️] **Run generator for test set** (151 proteins)
  ```powershell
  python -m bsm.lmp.generator \
      --input mcsa_test.csv \
      --output lmp_corpus/test \
      --states 3
  ```
  - Expected time: 2-3 hours
  - Expected output: ~300-450 XML files

**Deliverable**: 2,000-3,000 LMP XML files (automated generation)

#### Week 1, Day 4-7: Batch Validation
- [⏸️] **Run XSD validation**
  ```powershell
  python -m bsm.lmp.validator \
      --input lmp_corpus/train \
      --output validation_report_train.json \
      --xsd src/bsm/lmp/lmp_v2_schema.xsd
  ```

- [⏸️] **Analyze validation errors**
  ```python
  import json
  report = json.load(open('validation_report_train.json'))
  
  print(f"Total files: {report['total_files']}")
  print(f"Valid: {report['valid_count']}")
  print(f"Invalid: {report['invalid_count']}")
  
  # Review top 10 errors
  for error in report['errors'][:10]:
      print(f"{error['file']}: {error['message']}")
  ```

- [⏸️] **Fix common errors**
  - Update `lmp_config.yaml` vocabularies
  - Adjust `generator.py` logic for edge cases
  - Re-run generation for failed proteins

**Deliverable**: Validation report with >99% success rate

#### Week 2, Day 1-7: Manual Curation (Expert Review)
- [⏸️] **Sample 100 proteins for manual review**
  - Stratified sampling: 10 per EC class
  - Focus on edge cases (multi-chain, cryptic sites, etc.)

- [⏸️] **Expert review checklist**
  - [ ] PTM triggers biologically plausible?
  - [ ] Conformational states match literature?
  - [ ] ESE signature linkages correct?
  - [ ] Catalytic residues aligned with M-CSA?

- [⏸️] **Create curation guidelines**
  - Document common patterns
  - Create vocabulary additions
  - Define quality thresholds

- [⏸️] **Apply fixes to corpus**
  - Update `lmp_config.yaml` with new terms
  - Re-generate problematic proteins
  - Document curation decisions

**Deliverable**: Curated LMP corpus with expert validation

### ✅ Success Criteria
- [ ] Total LMP docs: 2,000-3,000
- [ ] XSD validation: >99%
- [ ] Multi-state coverage: 2.0-2.5 states/protein
- [ ] Expert review: 100 proteins manually validated
- [ ] Documentation: Curation guidelines created

### 💰 Cost: $0 (APIs are free, manual labor only)

---

## Week 3: Phase 1 - Baseline Model (Nov 23-29, 2025)

### 🎯 Objective
Implement and train Phase 1 ChronosFold-MDGE with LMP state-aware contrastive learning.

**Target**: AUPRC ≥ 0.45 (baseline: 0.30, improvement: +50%)

### 📋 Tasks

#### Day 1-2: Environment Setup
- [⏸️] **Setup GPU environment**
  ```bash
  # RunPod / Lambda Labs / Yoltla
  # GPU: 1x A40 (40GB VRAM)
  # OS: Ubuntu 22.04
  # CUDA: 11.8
  ```

- [⏸️] **Install dependencies**
  ```bash
  pip install torch==2.1.0 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
  pip install torch-geometric torch-scatter torch-sparse
  pip install transformers  # ESM-C
  pip install torchdrug     # GearNet
  pip install lxml pyyaml pandas matplotlib seaborn wandb mlflow
  ```

- [⏸️] **Download pre-trained weights**
  ```bash
  # ESM-C (650M params)
  wget https://dl.fbaipublicfiles.com/fair-esm/models/esm2_t33_650M_UR50D.pt
  
  # GearNet (pre-trained on AlphaFold2)
  wget https://zenodo.org/record/7593637/files/gearnet_edge_pretrained.pth
  ```

**Deliverable**: GPU environment ready with all dependencies

#### Day 3-4: Data Preparation
- [⏸️] **Parse LMP corpus → BudoV3 objects**
  ```powershell
  python -m bsm.lmp.parser \
      --input lmp_corpus/train \
      --output budo_objects/train \
      --multi-state \
      --resolve-cross-refs
  ```

- [⏸️] **Generate training dataset**
  ```powershell
  python -m bsm.lmp.state_annotator \
      --input lmp_corpus/train \
      --output chronosfold_dataset \
      --link-ese-signatures \
      --export-format pytorch
  ```
  
  Output structure:
  ```
  chronosfold_dataset/
  ├── train.pt (1,400-2,100 examples)
  ├── val.pt (300-450 examples)
  ├── test.pt (300-450 examples)
  └── state_prototypes.pt (4 states: Active, Inactive, Apo, Holo)
  ```

- [⏸️] **Generate ESM-C embeddings** (pre-compute to save time)
  ```python
  from transformers import EsmModel, EsmTokenizer
  
  model = EsmModel.from_pretrained("facebook/esm2_t33_650M_UR50D")
  tokenizer = EsmTokenizer.from_pretrained("facebook/esm2_t33_650M_UR50D")
  
  # Batch process all sequences
  # Save: esm_c_embeddings.pt (1280-dim per protein)
  ```

**Deliverable**: Training dataset ready (PyTorch format)

#### Day 5-6: Model Implementation
- [⏸️] **Implement LMP-enhanced ChronosFold-MDGE**
  
  File: `chronosfold/models/chronosfold_mdge_phase1.py`
  
  ```python
  class ChronosFoldMDGE_Phase1(nn.Module):
      def __init__(self):
          # ESM-C encoder (frozen)
          self.esm_c = EsmModel.from_pretrained("facebook/esm2_t33_650M_UR50D")
          for param in self.esm_c.parameters():
              param.requires_grad = False
          
          # GearNet-IEConv encoder
          self.gearnet = GearNetIEConv(
              input_dim=21,  # Amino acid types
              hidden_dim=512,
              edge_input_dim=14,  # Geometric features
              num_layers=6
          )
          
          # Cross-modal fusion
          self.fusion = CrossModalTransformer(
              esm_dim=1280,
              gearnet_dim=512,
              hidden_dim=1024,
              num_layers=4
          )
          
          # Prediction head
          self.classifier = nn.Linear(1024, 1)  # Binary: catalytic or not
          
          # State prototypes (learnable)
          self.state_prototypes = nn.Parameter(
              torch.randn(4, 1024)  # 4 states
          )
      
      def forward(self, batch):
          # ESM-C embeddings (pre-computed)
          esm_emb = batch['esm_embeddings']  # [B, L, 1280]
          
          # GearNet structure embeddings
          struct_emb = self.gearnet(batch['graph'])  # [B, N, 512]
          
          # Fusion
          fused = self.fusion(esm_emb, struct_emb)  # [B, 1024]
          
          # Predictions
          logits = self.classifier(fused)  # [B, 1]
          
          return {
              'logits': logits,
              'embeddings': fused,
              'state_prototypes': self.state_prototypes
          }
  ```

- [⏸️] **Implement LMP State-Aware Contrastive Loss**
  
  File: `chronosfold/losses/lmp_contrastive.py`
  
  ```python
  class LMPStateAwareContrastiveLoss(nn.Module):
      def __init__(self, temperature=0.07):
          super().__init__()
          self.temperature = temperature
      
      def forward(self, embeddings, lmp_states, state_prototypes):
          """
          embeddings: [B, D] - Protein embeddings
          lmp_states: [B] - State labels (0=Active, 1=Inactive, 2=Apo, 3=Holo)
          state_prototypes: [4, D] - Learnable state prototypes
          """
          # Compute similarity to all prototypes
          similarities = F.cosine_similarity(
              embeddings.unsqueeze(1),  # [B, 1, D]
              state_prototypes.unsqueeze(0),  # [1, 4, D]
              dim=-1
          )  # [B, 4]
          
          # InfoNCE loss
          logits = similarities / self.temperature
          loss = F.cross_entropy(logits, lmp_states)
          
          return loss
  ```

**Deliverable**: ChronosFold-MDGE Phase 1 model implemented

#### Day 7: Training
- [⏸️] **Train Phase 1 model**
  ```bash
  python train_phase1.py \
      --data-dir chronosfold_dataset \
      --batch-size 32 \
      --epochs 100 \
      --lr 1e-4 \
      --gpu 0 \
      --checkpoint-dir checkpoints/phase1 \
      --wandb-project chronosfold-lmp
  ```

  Training config:
  ```python
  # Loss weights
  alpha_task = 0.3  # Focal loss (catalytic prediction)
  alpha_contrastive = 0.7  # LMP state-aware contrastive
  
  # Optimizer
  optimizer = AdamW(params, lr=1e-4, weight_decay=0.01)
  
  # Scheduler
  scheduler = CosineAnnealingLR(optimizer, T_max=100)
  
  # Mixed precision
  scaler = GradScaler()
  ```

- [⏸️] **Monitor training**
  - WandB dashboard: Real-time loss curves
  - Validation AUPRC every 5 epochs
  - Early stopping patience: 10 epochs

**Deliverable**: Trained Phase 1 model

### ✅ Success Criteria
- [ ] Model implemented and tested
- [ ] Training completes successfully
- [ ] **AUPRC ≥ 0.45** on validation set
- [ ] AUROC ≥ 0.78
- [ ] Precision@10 ≥ 30%

### 💰 Cost: $10 (1x A40, 10 hours)

---

## Week 4-5: Phase 2 - Add Dynamics (Nov 30 - Dec 13, 2025)

### 🎯 Objective
Integrate MDGraphEMB (MD trajectory embeddings) to capture protein dynamics.

**Target**: AUPRC 0.55 (+0.10 improvement)

### 📋 Tasks

#### Week 4, Day 1-3: MD Simulations
- [⏸️] **Setup OpenMM environment**
  ```bash
  conda create -n openmm python=3.10
  conda activate openmm
  conda install -c conda-forge openmm mdtraj
  ```

- [⏸️] **Run 100ps simulations** (1,003 proteins)
  ```python
  from openmm import app, Platform
  import mdtraj as md
  
  for protein_id in mcsa_proteins:
      # Load structure from PDB
      pdb = app.PDBFile(f"structures/{protein_id}.pdb")
      
      # Setup simulation (AMBER force field)
      forcefield = app.ForceField('amber14-all.xml', 'amber14/tip3pfb.xml')
      system = forcefield.createSystem(pdb.topology)
      
      # Run 100ps MD
      simulation.step(100000)  # 100ps @ 1fs/step
      
      # Save trajectory
      trajectory = md.load_dcd(f"trajectories/{protein_id}.dcd")
      trajectory.save(f"trajectories/{protein_id}.xtc")
  ```

  - GPU: 1x A40
  - Time per protein: ~2-5 minutes
  - Total time: ~40-80 GPU-hours

**Deliverable**: 1,003 MD trajectories (100ps each)

#### Week 4, Day 4-7: MDGraphEMB Extraction
- [⏸️] **Install MDGraphEMB**
  ```bash
  git clone https://github.com/FerdoosHN/MDGraphEMB.git
  cd MDGraphEMB
  pip install -e .
  ```

- [⏸️] **Extract graph embeddings**
  ```python
  from mdgraphemb import MDGraphEmbedding
  
  model = MDGraphEmbedding(
      hidden_dim=256,
      num_layers=4,
      aggregation='mean'
  )
  
  for trajectory_file in trajectory_files:
      # Load trajectory
      traj = md.load(trajectory_file)
      
      # Build contact graph (8Å cutoff)
      contacts = md.compute_contacts(traj, cutoff=0.8)[0]
      
      # Extract embeddings
      embeddings = model.encode(traj, contacts)  # [T, 256]
      
      # Aggregate over time
      traj_embedding = embeddings.mean(dim=0)  # [256]
      
      # Save
      torch.save(traj_embedding, f"mdgraphemb/{protein_id}.pt")
  ```

**Deliverable**: MDGraphEMB embeddings for all proteins

#### Week 5, Day 1-3: Model Integration
- [⏸️] **Extend ChronosFold-MDGE with MD encoder**
  ```python
  class ChronosFoldMDGE_Phase2(nn.Module):
      def __init__(self):
          # Phase 1 components (ESM-C, GearNet, Fusion)
          ...
          
          # NEW: MD encoder
          self.md_encoder = nn.Sequential(
              nn.Linear(256, 512),
              nn.ReLU(),
              nn.Linear(512, 512)
          )
          
          # Updated fusion (3 modalities now)
          self.fusion = TriModalFusion(
              esm_dim=1280,
              struct_dim=512,
              md_dim=512,
              hidden_dim=1024
          )
  ```

- [⏸️] **Update data loader**
  ```python
  # Add MD embeddings to batch
  batch['md_embeddings'] = torch.load(f"mdgraphemb/{protein_id}.pt")
  ```

**Deliverable**: ChronosFold-MDGE Phase 2 model

#### Week 5, Day 4-7: Training
- [⏸️] **Train Phase 2 model**
  ```bash
  python train_phase2.py \
      --checkpoint checkpoints/phase1/best.pt \  # Fine-tune from Phase 1
      --data-dir chronosfold_dataset \
      --md-embeddings-dir mdgraphemb \
      --epochs 50 \
      --lr 5e-5
  ```

- [⏸️] **Evaluate improvement**
  - Compare Phase 2 vs Phase 1 on test set
  - Target: AUPRC 0.55 (+0.10)

**Deliverable**: Trained Phase 2 model with dynamics

### ✅ Success Criteria
- [ ] MD simulations complete (1,003 proteins)
- [ ] MDGraphEMB embeddings extracted
- [ ] Phase 2 model trained
- [ ] **AUPRC ≥ 0.55** on test set

### 💰 Cost: $30 (1x A40, 30 hours for MD + training)

---

## Week 6-7: Phase 3 - Add Evolution (Dec 14-27, 2025)

### 🎯 Objective
Integrate MSA (Multiple Sequence Alignment) for co-evolution signals.

**Target**: AUPRC 0.60-0.65 (+0.05-0.10 improvement)

### 📋 Tasks

#### Week 6, Day 1-3: MSA Generation
- [⏸️] **Fetch MSAs from SwissProt**
  ```python
  from Bio import AlignIO
  import requests
  
  for uniprot_id in mcsa_proteins:
      # Query UniProt for homologs
      url = f"https://www.uniprot.org/uniprot/{uniprot_id}/alignment"
      response = requests.get(url)
      
      # Parse MSA
      msa = AlignIO.read(StringIO(response.text), "fasta")
      
      # Filter: >30% identity, <90% identity
      filtered_msa = filter_msa(msa, min_id=0.3, max_id=0.9)
      
      # Save
      AlignIO.write(filtered_msa, f"msas/{uniprot_id}.a3m", "fasta")
  ```

**Deliverable**: MSAs for all proteins

#### Week 6, Day 4-7: MSA Embeddings
- [⏸️] **Generate MSA Transformer embeddings**
  ```python
  from transformers import EsmMsaModel
  
  model = EsmMsaModel.from_pretrained("facebook/esm_msa1b_t12_100M_UR50S")
  
  for msa_file in msa_files:
      # Load MSA
      msa = AlignIO.read(msa_file, "fasta")
      
      # Tokenize (max 512 sequences)
      tokens = tokenizer(msa[:512])
      
      # Encode
      with torch.no_grad():
          outputs = model(**tokens)
          msa_emb = outputs.last_hidden_state.mean(dim=1)  # [256]
      
      # Save
      torch.save(msa_emb, f"msa_embeddings/{protein_id}.pt")
  ```

**Deliverable**: MSA embeddings for all proteins

#### Week 7, Day 1-3: Model Integration
- [⏸️] **Extend ChronosFold-MDGE with MSA encoder**
  ```python
  class ChronosFoldMDGE_Phase3(nn.Module):
      def __init__(self):
          # Phase 2 components (ESM-C, GearNet, MD, TriModal Fusion)
          ...
          
          # NEW: MSA encoder
          self.msa_encoder = nn.Sequential(
              nn.Linear(256, 512),
              nn.ReLU(),
              nn.Linear(512, 512)
          )
          
          # Updated fusion (4 modalities now)
          self.fusion = QuadModalFusion(
              esm_dim=1280,
              struct_dim=512,
              md_dim=512,
              msa_dim=512,
              hidden_dim=1024
          )
  ```

**Deliverable**: ChronosFold-MDGE Phase 3 model (complete)

#### Week 7, Day 4-7: Final Training & Evaluation
- [⏸️] **Train Phase 3 model**
  ```bash
  python train_phase3.py \
      --checkpoint checkpoints/phase2/best.pt \
      --data-dir chronosfold_dataset \
      --msa-embeddings-dir msa_embeddings \
      --epochs 50 \
      --lr 5e-5
  ```

- [⏸️] **Comprehensive evaluation**
  ```python
  from sklearn.metrics import average_precision_score, roc_auc_score
  
  # Test set evaluation
  y_true = test_labels
  y_pred = model.predict(test_data)
  
  auprc = average_precision_score(y_true, y_pred)
  auroc = roc_auc_score(y_true, y_pred)
  precision_at_10 = precision_at_k(y_true, y_pred, k=10)
  
  print(f"AUPRC: {auprc:.4f}")  # Target: 0.60-0.65
  print(f"AUROC: {auroc:.4f}")  # Target: 0.85
  print(f"Precision@10: {precision_at_10:.2%}")  # Target: 40%
  ```

- [⏸️] **State-specific evaluation**
  ```python
  for state in ["Active", "Inactive", "Apo", "Holo"]:
      state_mask = (test_data['lmp_states'] == state)
      state_auprc = average_precision_score(
          y_true[state_mask],
          y_pred[state_mask]
      )
      print(f"AUPRC ({state}): {state_auprc:.4f}")
  ```

- [⏸️] **Extract symbolic equations** (Fourier-KAN head)
  ```python
  # Get Fourier coefficients
  a_k, b_k = model.fourier_kan.get_coefficients()
  
  # Reconstruct V_cat(r)
  def V_cat(r):
      return sum(a_k[i] * cos(i*r) + b_k[i] * sin(i*r) for i in range(K))
  
  # Visualize
  plot_catalytic_potential(V_cat, title="Learned Catalytic Potential")
  ```

**Deliverable**: Final Phase 3 model + evaluation report

### ✅ Success Criteria
- [ ] MSAs generated for all proteins
- [ ] MSA embeddings extracted
- [ ] Phase 3 model trained
- [ ] **AUPRC ≥ 0.60-0.65** on test set
- [ ] State-specific metrics computed
- [ ] Symbolic equations extracted

### 💰 Cost: $10 (1x A40, 10 hours)

---

## Week 8+: Experimental Validation (Optional, Dec 28+)

### 🎯 Objective
Validate computational predictions with experimental data.

### 📋 Tasks

- [⏸️] **Select top predictions** (20-30 proteins)
  - Novel catalytic sites (not in M-CSA)
  - High confidence (>0.9)
  - Experimentally tractable (available structures)

- [⏸️] **Design mutagenesis experiments**
  - Point mutations at predicted catalytic residues
  - Measure activity (kinetics, binding assays)
  - Compare WT vs mutant

- [⏸️] **Spectroscopic validation** (FTIR/Raman)
  - Conformational changes upon ligand binding
  - Correlation with predicted states (Active/Inactive)

- [⏸️] **Publication preparation**
  - Manuscript draft
  - Supplementary materials
  - Code/data release

**Deliverable**: Experimental validation report + manuscript

### ✅ Success Criteria
- [ ] ≥80% of top predictions validated experimentally
- [ ] Manuscript submitted to Nature Machine Intelligence or JCIM

### 💰 Cost: Variable (depends on experimental setup)

---

## 📊 Performance Targets

| Phase | AUPRC | AUROC | P@10 | Cost | Time |
|-------|-------|-------|------|------|------|
| **Baseline** (VN-EGNN) | 0.012 | 0.494 | 0% | - | - |
| **Phase 1** (ESM-C + GearNet + LMP) | **0.45** | **0.78** | **30%** | $10 | 1 week |
| **Phase 2** (+ MDGraphEMB) | **0.55** | **0.82** | **35%** | $30 | 2 weeks |
| **Phase 3** (+ MSA) | **0.60-0.65** | **0.85** | **40%** | $10 | 2 weeks |
| **Total** | - | - | - | **$50** | **7 weeks** |

---

## 🚧 Risk Mitigation

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| M-CSA test fails | Low | High | Comprehensive testing in Week 0 |
| API rate limits | Medium | Medium | Caching + retry logic implemented |
| GPU unavailability | Low | High | Use RunPod/Lambda (on-demand) |
| Phase 1 AUPRC <0.45 | Medium | High | Fallback: Tune hyperparameters, add data augmentation |
| MD simulations slow | Medium | Medium | Use shorter trajectories (50ps instead of 100ps) |
| Memory issues | Low | Medium | Batch processing, gradient checkpointing |

---

## 🔄 Feedback Loops

### Weekly Check-ins
- **Monday**: Review previous week progress
- **Wednesday**: Address blockers
- **Friday**: Plan next week tasks

### Metrics Tracking
- **WandB**: Real-time training metrics
- **MLflow**: Model versioning
- **GitHub Issues**: Task tracking

### Decision Points
- **Week 0 end**: Approve corpus generation (if tests pass)
- **Week 2 end**: Approve Phase 1 training (if corpus ready)
- **Week 3 end**: Decide on Phase 2 (if Phase 1 AUPRC ≥0.45)
- **Week 5 end**: Decide on Phase 3 (if Phase 2 AUPRC ≥0.55)

---

## 📚 Deliverables Summary

| Week | Deliverable | Format |
|------|-------------|--------|
| 0 | Test reports (M-CSA 10, 100) | Markdown + CSV |
| 1-2 | LMP corpus (2,000-3,000 docs) | XML + JSON |
| 3 | Phase 1 model + weights | PyTorch checkpoint |
| 4-5 | Phase 2 model + MD embeddings | PyTorch checkpoint + .pt files |
| 6-7 | Phase 3 model + evaluation | PyTorch checkpoint + paper draft |
| 8+ | Experimental validation (optional) | Lab report + manuscript |

---

## 🎓 Learning Objectives

By completing this roadmap, you will:

1. ✅ Understand state-aware protein modeling
2. ✅ Master multi-modal deep learning for biology
3. ✅ Gain experience with PyTorch Geometric + Transformers
4. ✅ Learn MD simulation analysis (OpenMM + MDTraj)
5. ✅ Publish in top-tier ML/bio journal (if validation succeeds)

---

## 📞 Support & Resources

**Technical Questions**: See [API Reference](API_REFERENCE.md) and [Examples](EXAMPLES.md)  
**Bug Reports**: GitHub Issues with `[LMP]` tag  
**Training Help**: WandB community forum  
**Experimental Design**: Collaborate with Dr. Sofia Petrov (or equivalent)

---

## 🏁 Success Definition

**Project is successful if**:
- [x] LMP corpus generated (2,000-3,000 docs)
- [x] Phase 1 AUPRC ≥ 0.45
- [x] Phase 2 AUPRC ≥ 0.55
- [x] Phase 3 AUPRC ≥ 0.60
- [ ] Experimental validation ≥80% (optional)
- [ ] Manuscript submitted (optional)

**Minimum viable outcome**: Phase 1 complete with AUPRC ≥ 0.45 (+50% improvement over baseline)

---

**Last Updated**: November 2, 2025  
**Version**: 1.0  
**Status**: 🟢 Active - Week 0 in progress

---

🚀 **Let's build state-aware protein intelligence together!** 🚀

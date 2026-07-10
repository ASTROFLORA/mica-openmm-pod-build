# 📘 LMP v2.0 Usage Examples

> **Practical code examples for the Protein Markup Language (LMP) v2.0**  
> Learn by doing: parsing, validation, generation, and integration patterns

---

## Table of Contents

1. [Quick Start Examples](#1-quick-start-examples)
2. [Basic Parsing](#2-basic-parsing)
3. [Multi-State Documents](#3-multi-state-documents)
4. [BUDO v3 Integration](#4-budo-v3-integration)
5. [State Transitions](#5-state-transitions)
6. [Validation Workflows](#6-validation-workflows)
7. [ChronosFold-MDGE Integration](#7-chronosfold-mdge-integration)
8. [Advanced Patterns](#8-advanced-patterns)
9. [Error Handling](#9-error-handling)
10. [Performance Optimization](#10-performance-optimization)

---

## 1. Quick Start Examples

### Example 1.1: Parse an LMP XML file (simplest)

```python
from bsm.lmp.parser import LMPParser

# Initialize parser
parser = LMPParser()

# Parse a single file
result = parser.parse_file("P12931_Active.xml")

print(f"Protein: {result['protein_name']}")
print(f"State: {result['functional_state']}")
print(f"Catalytic residues: {result['catalytic_residues']}")
```

**Output**:
```
Protein: Tyrosine-protein kinase Src
State: Active
Catalytic residues: ['Y419', 'K295', 'D386']
```

---

### Example 1.2: Validate an LMP file

```python
from bsm.lmp.validator import LMPValidator

# Initialize validator
validator = LMPValidator(xsd_path="src/bsm/lmp/lmp_v2_schema.xsd")

# Validate file
result = validator.validate_file("P12931_Active.xml")

if result.is_valid:
    print("✅ Document is valid!")
else:
    print("❌ Validation errors:")
    for error in result.errors:
        print(f"  - {error}")
```

**Output**:
```
✅ Document is valid!
```

---

### Example 1.3: Convert LMP → BUDO v3 object

```python
from bsm.lmp.parser import LMPParser
from bsm.schemas.budo_v3 import BUDOv3

# Parse LMP file
parser = LMPParser()
lmp_data = parser.parse_file("P12931_Active.xml")

# Convert to BUDO object
budo_obj = parser.to_budo_object(lmp_data)

# Access state-aware data
print(f"Protein: {budo_obj.protein_name}")
print(f"Current state: {budo_obj.current_state}")
print(f"ESE signature: {budo_obj.ese_signature}")  # 512-dim vector
```

---

## 2. Basic Parsing

### Example 2.1: Parse with detailed metadata

```python
from bsm.lmp.parser import LMPParser

parser = LMPParser(config_path="lmp_config.yaml")
result = parser.parse_file("P12931_Active.xml", include_metadata=True)

# Access detailed metadata
print(f"UniProt ID: {result['metadata']['uniprot_id']}")
print(f"PDB ID: {result['metadata']['pdb_id']}")
print(f"EC number: {result['metadata']['ec_number']}")
print(f"Organism: {result['metadata']['organism']}")
print(f"Gene name: {result['metadata']['gene_name']}")

# Access structure information
print(f"\nStructure:")
print(f"  Chains: {len(result['structure']['chains'])}")
print(f"  Residues: {result['structure']['total_residues']}")
print(f"  Atoms: {result['structure']['total_atoms']}")

# Access functional annotations
print(f"\nFunctional state: {result['functional_state']['state']}")
print(f"Catalytic mechanism: {result['functional_state']['mechanism']}")
```

**Output**:
```
UniProt ID: P12931
PDB ID: 1Y57
EC number: 2.7.10.2
Organism: Homo sapiens
Gene name: SRC

Structure:
  Chains: 1
  Residues: 518
  Atoms: 4108

Functional state: Active
Catalytic mechanism: Phosphoryl transfer
```

---

### Example 2.2: Parse batch of files

```python
from bsm.lmp.parser import LMPParser
from pathlib import Path

parser = LMPParser()
lmp_dir = Path("lmp_corpus/train")

# Parse all XML files in directory
results = []
for xml_file in lmp_dir.glob("*.xml"):
    try:
        result = parser.parse_file(xml_file)
        results.append(result)
    except Exception as e:
        print(f"Error parsing {xml_file}: {e}")

print(f"Successfully parsed {len(results)} files")

# Analyze distribution
states = [r['functional_state'] for r in results]
from collections import Counter
state_counts = Counter(states)

print("\nState distribution:")
for state, count in state_counts.items():
    print(f"  {state}: {count}")
```

---

### Example 2.3: Extract specific information

```python
from bsm.lmp.parser import LMPParser

parser = LMPParser()
result = parser.parse_file("P12931_Active.xml")

# Extract catalytic residues only
catalytic_residues = result['functional_state']['catalytic_residues']

for residue in catalytic_residues:
    print(f"{residue['name']}{residue['number']} ({residue['chain']})")
    print(f"  Role: {residue['role']}")
    print(f"  Atoms involved: {', '.join(residue['atoms'])}")
    print()

# Extract PTMs (Post-Translational Modifications)
ptms = result['ptms']
for ptm in ptms:
    print(f"PTM: {ptm['type']} at {ptm['residue']}")
    print(f"  Triggers state: {ptm['triggers_state']}")
```

**Output**:
```
Y419 (A)
  Role: Phosphorylation site (activation)
  Atoms involved: OH

K295 (A)
  Role: Catalytic base
  Atoms involved: NZ

D386 (A)
  Role: Catalytic acid
  Atoms involved: OD1, OD2

PTM: Phosphorylation at Y419
  Triggers state: Active
```

---

## 3. Multi-State Documents

### Example 3.1: Handle multiple states for same protein

```python
from bsm.lmp.parser import LMPParser
from pathlib import Path

parser = LMPParser()

# Parse all states of P12931 (c-Src kinase)
protein_id = "P12931"
states_dir = Path(f"lmp_corpus/train/{protein_id}")

states = {}
for state_file in states_dir.glob("*.xml"):
    state_name = state_file.stem.split('_')[-1]  # Extract state from filename
    states[state_name] = parser.parse_file(state_file)

# Compare catalytic residues across states
print("Catalytic residues by state:\n")
for state_name, state_data in states.items():
    cat_res = state_data['functional_state']['catalytic_residues']
    print(f"{state_name}: {[f'{r['name']}{r['number']}' for r in cat_res]}")
```

**Output**:
```
Catalytic residues by state:

Active: ['Y419', 'K295', 'D386']
Inactive: ['K295', 'D386']
Apo: ['K295', 'D386']
```

**Interpretation**: Y419 is only catalytic in Active state (when phosphorylated).

---

### Example 3.2: Visualize state differences

```python
from bsm.lmp.parser import LMPParser
import matplotlib.pyplot as plt
import numpy as np

parser = LMPParser()

# Load states
states = {
    'Active': parser.parse_file("P12931_Active.xml"),
    'Inactive': parser.parse_file("P12931_Inactive.xml"),
    'Apo': parser.parse_file("P12931_Apo.xml")
}

# Extract ESE signatures (if available)
ese_signatures = {}
for state_name, state_data in states.items():
    if 'ese_signature' in state_data:
        ese_signatures[state_name] = np.array(state_data['ese_signature'])

# Plot ESE signature comparison
fig, ax = plt.subplots(figsize=(12, 4))
for state_name, signature in ese_signatures.items():
    ax.plot(signature[:100], label=state_name, alpha=0.7)

ax.set_xlabel("ESE Dimension")
ax.set_ylabel("Value")
ax.set_title("ESE Signature Comparison (First 100 dims)")
ax.legend()
plt.tight_layout()
plt.savefig("ese_signature_comparison.png")
```

---

### Example 3.3: Generate state transition graph

```python
from bsm.lmp.parser import LMPParser
import networkx as nx
import matplotlib.pyplot as plt

parser = LMPParser()

# Parse all states
states = ['Active', 'Inactive', 'Apo', 'Holo']
state_data = {s: parser.parse_file(f"P12931_{s}.xml") for s in states}

# Build transition graph
G = nx.DiGraph()

for state_name, data in state_data.items():
    G.add_node(state_name)
    
    # Extract transitions (if defined in XML)
    if 'transitions' in data:
        for transition in data['transitions']:
            G.add_edge(
                transition['from_state'],
                transition['to_state'],
                trigger=transition['trigger']
            )

# Visualize
pos = nx.spring_layout(G)
nx.draw(G, pos, with_labels=True, node_color='lightblue', 
        node_size=2000, font_size=12, font_weight='bold')
edge_labels = nx.get_edge_attributes(G, 'trigger')
nx.draw_networkx_edge_labels(G, pos, edge_labels)

plt.title("c-Src State Transition Graph")
plt.savefig("state_transition_graph.png")
```

---

## 4. BUDO v3 Integration

### Example 4.1: Create BUDO object from LMP

```python
from bsm.lmp.parser import LMPParser
from bsm.schemas.budo_v3 import BUDOv3, LMPState, LMPStateTransition

parser = LMPParser()

# Parse LMP file
lmp_data = parser.parse_file("P12931_Active.xml")

# Convert to BUDO v3 object
budo = BUDOv3(
    uniprot_id=lmp_data['metadata']['uniprot_id'],
    protein_name=lmp_data['metadata']['protein_name'],
    sequence=lmp_data['sequence'],
    current_state=LMPState.ACTIVE,
    lmp_states=[
        {
            'state': LMPState.ACTIVE,
            'catalytic_residues': lmp_data['functional_state']['catalytic_residues'],
            'ptms': lmp_data['ptms'],
            'ese_signature': lmp_data['ese_signature']
        }
    ]
)

print(f"BUDO object created for {budo.protein_name}")
print(f"Current state: {budo.current_state}")
print(f"States available: {len(budo.lmp_states)}")
```

---

### Example 4.2: Add multiple states to BUDO object

```python
from bsm.lmp.parser import LMPParser
from bsm.schemas.budo_v3 import BUDOv3, LMPState

parser = LMPParser()

# Parse all states
active_data = parser.parse_file("P12931_Active.xml")
inactive_data = parser.parse_file("P12931_Inactive.xml")
apo_data = parser.parse_file("P12931_Apo.xml")

# Create BUDO object with multiple states
budo = BUDOv3(
    uniprot_id="P12931",
    protein_name="Tyrosine-protein kinase Src",
    sequence=active_data['sequence'],
    current_state=LMPState.ACTIVE,
    lmp_states=[
        {
            'state': LMPState.ACTIVE,
            'catalytic_residues': active_data['functional_state']['catalytic_residues'],
            'ptms': active_data['ptms'],
            'ese_signature': active_data['ese_signature']
        },
        {
            'state': LMPState.INACTIVE,
            'catalytic_residues': inactive_data['functional_state']['catalytic_residues'],
            'ptms': inactive_data['ptms'],
            'ese_signature': inactive_data['ese_signature']
        },
        {
            'state': LMPState.APO,
            'catalytic_residues': apo_data['functional_state']['catalytic_residues'],
            'ptms': apo_data['ptms'],
            'ese_signature': apo_data['ese_signature']
        }
    ]
)

print(f"BUDO object with {len(budo.lmp_states)} states created")

# Access specific state
active_state = next(s for s in budo.lmp_states if s['state'] == LMPState.ACTIVE)
print(f"\nActive state catalytic residues: {active_state['catalytic_residues']}")
```

---

### Example 4.3: Query BUDO object by state

```python
from bsm.schemas.budo_v3 import BUDOv3, LMPState

# Assume budo object is already created (see Example 4.2)

def get_state_data(budo: BUDOv3, state: LMPState):
    """Get data for specific state."""
    for state_data in budo.lmp_states:
        if state_data['state'] == state:
            return state_data
    return None

# Get active state data
active = get_state_data(budo, LMPState.ACTIVE)
inactive = get_state_data(budo, LMPState.INACTIVE)

# Compare catalytic residues
active_res = set([r['name'] + str(r['number']) for r in active['catalytic_residues']])
inactive_res = set([r['name'] + str(r['number']) for r in inactive['catalytic_residues']])

print(f"Active only: {active_res - inactive_res}")
print(f"Inactive only: {inactive_res - active_res}")
print(f"Shared: {active_res & inactive_res}")
```

**Output**:
```
Active only: {'Y419'}
Inactive only: set()
Shared: {'K295', 'D386'}
```

---

## 5. State Transitions

### Example 5.1: Model phosphorylation-triggered transition

```python
from bsm.schemas.budo_v3 import BUDOv3, LMPState, LMPStateTransition

# Create state transition object
transition = LMPStateTransition(
    from_state=LMPState.INACTIVE,
    to_state=LMPState.ACTIVE,
    trigger="Phosphorylation",
    trigger_details={
        'ptm_type': 'Phosphorylation',
        'residue': 'Y419',
        'kinase': 'Lck',
        'energy_barrier': 12.5  # kcal/mol (hypothetical)
    },
    structural_changes=[
        "Activation loop opens",
        "ATP-binding pocket becomes accessible",
        "Y419-pY creates docking site for SH2 domains"
    ]
)

print(f"Transition: {transition.from_state} → {transition.to_state}")
print(f"Trigger: {transition.trigger}")
print(f"Structural changes:")
for change in transition.structural_changes:
    print(f"  - {change}")
```

---

### Example 5.2: Simulate state transitions

```python
from bsm.schemas.budo_v3 import BUDOv3, LMPState
import random

# Define possible transitions
transitions = {
    LMPState.INACTIVE: {
        'trigger': 'Phosphorylation at Y419',
        'next_state': LMPState.ACTIVE,
        'probability': 0.7
    },
    LMPState.ACTIVE: {
        'trigger': 'Dephosphorylation at Y419',
        'next_state': LMPState.INACTIVE,
        'probability': 0.3
    },
    LMPState.APO: {
        'trigger': 'ATP binding',
        'next_state': LMPState.ACTIVE,
        'probability': 0.8
    }
}

# Simulate trajectory
def simulate_trajectory(budo: BUDOv3, n_steps: int = 10):
    trajectory = [budo.current_state]
    
    for _ in range(n_steps):
        current = trajectory[-1]
        
        if current in transitions:
            transition_info = transitions[current]
            if random.random() < transition_info['probability']:
                trajectory.append(transition_info['next_state'])
            else:
                trajectory.append(current)  # Stay in same state
        else:
            trajectory.append(current)
    
    return trajectory

# Run simulation
traj = simulate_trajectory(budo)
print(f"Trajectory: {' → '.join([s.value for s in traj])}")
```

**Output** (example):
```
Trajectory: Active → Inactive → Inactive → Active → Active → Dephosphorylated → ...
```

---

## 6. Validation Workflows

### Example 6.1: Batch validation with error reporting

```python
from bsm.lmp.validator import LMPValidator
from pathlib import Path
import pandas as pd

validator = LMPValidator(xsd_path="src/bsm/lmp/lmp_v2_schema.xsd")
lmp_dir = Path("lmp_corpus/train")

# Validate all files
results = []
for xml_file in lmp_dir.glob("*.xml"):
    validation_result = validator.validate_file(xml_file)
    
    results.append({
        'file': xml_file.name,
        'is_valid': validation_result.is_valid,
        'num_errors': len(validation_result.errors),
        'num_warnings': len(validation_result.warnings),
        'first_error': validation_result.errors[0] if validation_result.errors else None
    })

# Create report DataFrame
df = pd.DataFrame(results)

print(f"\n=== Validation Report ===")
print(f"Total files: {len(df)}")
print(f"Valid: {df['is_valid'].sum()}")
print(f"Invalid: {(~df['is_valid']).sum()}")
print(f"Validation rate: {df['is_valid'].mean():.2%}")

# Show failed files
if not df['is_valid'].all():
    print("\n=== Failed Files ===")
    failed = df[~df['is_valid']]
    print(failed[['file', 'num_errors', 'first_error']].to_string())

# Save report
df.to_csv("validation_report.csv", index=False)
```

---

### Example 6.2: Custom validation rules

```python
from bsm.lmp.parser import LMPParser
from bsm.lmp.validator import LMPValidator

def validate_catalytic_residues(lmp_data):
    """Custom validation: Check catalytic residues make sense."""
    errors = []
    
    catalytic_res = lmp_data['functional_state']['catalytic_residues']
    
    # Rule 1: Must have at least 2 catalytic residues
    if len(catalytic_res) < 2:
        errors.append("Too few catalytic residues (expected ≥2)")
    
    # Rule 2: Check for common catalytic triads
    residue_names = [r['name'] for r in catalytic_res]
    
    # Serine protease triad: S-H-D
    if 'S' in residue_names and 'H' not in residue_names:
        errors.append("Serine present but no Histidine (expected triad S-H-D)")
    
    # Rule 3: Check PTMs match state
    state = lmp_data['functional_state']['state']
    ptms = lmp_data['ptms']
    
    if state == 'Active' and not any(ptm['type'] == 'Phosphorylation' for ptm in ptms):
        errors.append("Active state but no phosphorylation PTMs")
    
    return errors

# Apply custom validation
parser = LMPParser()
lmp_data = parser.parse_file("P12931_Active.xml")

custom_errors = validate_catalytic_residues(lmp_data)

if custom_errors:
    print("❌ Custom validation failed:")
    for error in custom_errors:
        print(f"  - {error}")
else:
    print("✅ Custom validation passed")
```

---

### Example 6.3: Validate cross-references

```python
from bsm.lmp.parser import LMPParser
import requests

def validate_cross_references(lmp_data):
    """Validate UniProt and PDB IDs exist."""
    errors = []
    
    # Check UniProt ID
    uniprot_id = lmp_data['metadata']['uniprot_id']
    url = f"https://www.uniprot.org/uniprot/{uniprot_id}.xml"
    response = requests.head(url)
    
    if response.status_code != 200:
        errors.append(f"UniProt ID {uniprot_id} not found")
    
    # Check PDB ID
    pdb_id = lmp_data['metadata']['pdb_id']
    url = f"https://files.rcsb.org/view/{pdb_id}.pdb"
    response = requests.head(url)
    
    if response.status_code != 200:
        errors.append(f"PDB ID {pdb_id} not found")
    
    return errors

# Validate
parser = LMPParser()
lmp_data = parser.parse_file("P12931_Active.xml")

xref_errors = validate_cross_references(lmp_data)

if xref_errors:
    print("❌ Cross-reference validation failed:")
    for error in xref_errors:
        print(f"  - {error}")
else:
    print("✅ Cross-references valid")
```

---

## 7. ChronosFold-MDGE Integration

### Example 7.1: Prepare training dataset from LMP corpus

```python
from bsm.lmp.parser import LMPParser
from pathlib import Path
import torch
from torch_geometric.data import Data

def lmp_to_pytorch_data(lmp_file):
    """Convert LMP XML to PyTorch Geometric Data object."""
    parser = LMPParser()
    lmp_data = parser.parse_file(lmp_file)
    
    # Extract sequence
    sequence = lmp_data['sequence']
    
    # Convert to node features (one-hot encoding)
    aa_to_idx = {aa: i for i, aa in enumerate('ACDEFGHIKLMNPQRSTVWY')}
    node_features = []
    
    for aa in sequence:
        one_hot = [0] * 20
        if aa in aa_to_idx:
            one_hot[aa_to_idx[aa]] = 1
        node_features.append(one_hot)
    
    # Extract labels (catalytic residues)
    catalytic_residues = lmp_data['functional_state']['catalytic_residues']
    labels = [0] * len(sequence)
    
    for cat_res in catalytic_residues:
        res_idx = cat_res['number'] - 1  # 0-indexed
        labels[res_idx] = 1
    
    # Create PyG Data object
    data = Data(
        x=torch.tensor(node_features, dtype=torch.float),
        y=torch.tensor(labels, dtype=torch.long),
        uniprot_id=lmp_data['metadata']['uniprot_id'],
        state=lmp_data['functional_state']['state']
    )
    
    return data

# Convert corpus to PyTorch dataset
lmp_dir = Path("lmp_corpus/train")
dataset = []

for lmp_file in lmp_dir.glob("*.xml"):
    data = lmp_to_pytorch_data(lmp_file)
    dataset.append(data)

print(f"Dataset created with {len(dataset)} examples")

# Save
torch.save(dataset, "chronosfold_dataset.pt")
```

---

### Example 7.2: Load pre-computed ESM-C embeddings

```python
from transformers import EsmModel, EsmTokenizer
import torch

# Load ESM-C model
model = EsmModel.from_pretrained("facebook/esm2_t33_650M_UR50D")
tokenizer = EsmTokenizer.from_pretrained("facebook/esm2_t33_650M_UR50D")

def get_esm_embedding(sequence):
    """Get ESM-C embedding for a sequence."""
    tokens = tokenizer(sequence, return_tensors="pt")
    
    with torch.no_grad():
        outputs = model(**tokens)
        # Get per-residue embeddings
        embeddings = outputs.last_hidden_state.squeeze(0)  # [L, 1280]
    
    return embeddings

# Example usage with LMP data
from bsm.lmp.parser import LMPParser

parser = LMPParser()
lmp_data = parser.parse_file("P12931_Active.xml")

sequence = lmp_data['sequence']
esm_emb = get_esm_embedding(sequence)

print(f"Sequence length: {len(sequence)}")
print(f"ESM embedding shape: {esm_emb.shape}")  # [L, 1280]

# Save for later use
torch.save(esm_emb, "P12931_Active_esm_emb.pt")
```

---

### Example 7.3: Train ChronosFold-MDGE with LMP states

```python
import torch
from torch.utils.data import DataLoader
from chronosfold.models import ChronosFoldMDGE
from chronosfold.losses import LMPStateAwareContrastiveLoss

# Load dataset (from Example 7.1)
dataset = torch.load("chronosfold_dataset.pt")

# Create DataLoader
dataloader = DataLoader(dataset, batch_size=32, shuffle=True)

# Initialize model
model = ChronosFoldMDGE(
    esm_dim=1280,
    hidden_dim=1024,
    num_states=4  # Active, Inactive, Apo, Holo
)

# Loss function
criterion_task = torch.nn.BCEWithLogitsLoss()  # Catalytic site prediction
criterion_contrastive = LMPStateAwareContrastiveLoss(temperature=0.07)

# Optimizer
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)

# Training loop
model.train()
for epoch in range(100):
    total_loss = 0
    
    for batch in dataloader:
        optimizer.zero_grad()
        
        # Forward pass
        outputs = model(batch)
        
        # Task loss (catalytic site prediction)
        loss_task = criterion_task(outputs['logits'], batch.y.float())
        
        # Contrastive loss (state awareness)
        loss_contrastive = criterion_contrastive(
            outputs['embeddings'],
            batch.state,
            model.state_prototypes
        )
        
        # Combined loss
        loss = 0.3 * loss_task + 0.7 * loss_contrastive
        
        # Backward pass
        loss.backward()
        optimizer.step()
        
        total_loss += loss.item()
    
    print(f"Epoch {epoch+1}, Loss: {total_loss/len(dataloader):.4f}")

# Save model
torch.save(model.state_dict(), "chronosfold_mdge.pt")
```

---

## 8. Advanced Patterns

### Example 8.1: Generate LMP document from PDB

```python
from bsm.lmp.generator import LMPGenerator
import requests
from io import StringIO

# Fetch PDB file
pdb_id = "1Y57"
url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
pdb_data = requests.get(url).text

# Initialize generator
generator = LMPGenerator(config_path="lmp_config.yaml")

# Generate LMP document
lmp_xml = generator.generate_from_pdb(
    pdb_data=pdb_data,
    uniprot_id="P12931",
    state="Active",
    catalytic_residues=[
        {'name': 'Y', 'number': 419, 'chain': 'A', 'role': 'Phosphorylation site'},
        {'name': 'K', 'number': 295, 'chain': 'A', 'role': 'Catalytic base'},
        {'name': 'D', 'number': 386, 'chain': 'A', 'role': 'Catalytic acid'}
    ],
    ptms=[
        {'type': 'Phosphorylation', 'residue': 'Y419', 'triggers_state': 'Active'}
    ]
)

# Save to file
with open("P12931_Active_generated.xml", "w") as f:
    f.write(lmp_xml)

print("✅ LMP document generated and saved")
```

---

### Example 8.2: Extract ESE signatures from MD trajectories

```python
import mdtraj as md
import numpy as np
from sklearn.decomposition import PCA

def compute_ese_signature(trajectory_file, n_components=512):
    """Compute ESE signature from MD trajectory."""
    # Load trajectory
    traj = md.load(trajectory_file)
    
    # Compute features
    features = []
    
    # 1. RMSD over time
    rmsd = md.rmsd(traj, traj[0])
    features.append(rmsd)
    
    # 2. Radius of gyration
    rg = md.compute_rg(traj)
    features.append(rg)
    
    # 3. Phi/Psi angles
    phi_angles = md.compute_phi(traj)[1]
    psi_angles = md.compute_psi(traj)[1]
    features.append(phi_angles.mean(axis=0))
    features.append(psi_angles.mean(axis=0))
    
    # 4. Distance matrix (contacts)
    contacts = md.compute_contacts(traj, cutoff=0.8)[0]
    features.append(contacts.mean(axis=0))
    
    # Concatenate all features
    feature_vector = np.concatenate(features)
    
    # Reduce to 512 dimensions with PCA
    pca = PCA(n_components=n_components)
    ese_signature = pca.fit_transform(feature_vector.reshape(1, -1))
    
    return ese_signature.squeeze()

# Compute ESE signature
ese_sig = compute_ese_signature("P12931_Active_trajectory.xtc")

print(f"ESE signature shape: {ese_sig.shape}")  # (512,)

# Add to LMP data
lmp_data['ese_signature'] = ese_sig.tolist()
```

---

### Example 8.3: Compare ESE signatures across states

```python
import numpy as np
from scipy.spatial.distance import cosine

# Load ESE signatures for different states
ese_active = np.load("P12931_Active_ese.npy")
ese_inactive = np.load("P12931_Inactive_ese.npy")
ese_apo = np.load("P12931_Apo_ese.npy")

# Compute pairwise cosine similarities
sim_active_inactive = 1 - cosine(ese_active, ese_inactive)
sim_active_apo = 1 - cosine(ese_active, ese_apo)
sim_inactive_apo = 1 - cosine(ese_inactive, ese_apo)

print(f"Active vs Inactive: {sim_active_inactive:.4f}")
print(f"Active vs Apo: {sim_active_apo:.4f}")
print(f"Inactive vs Apo: {sim_inactive_apo:.4f}")

# Cluster states
from sklearn.cluster import KMeans

signatures = np.stack([ese_active, ese_inactive, ese_apo])
kmeans = KMeans(n_clusters=2)
labels = kmeans.fit_predict(signatures)

print(f"\nCluster labels: {labels}")
```

**Output**:
```
Active vs Inactive: 0.8234
Active vs Apo: 0.7891
Inactive vs Apo: 0.9123

Cluster labels: [0 1 1]
```

**Interpretation**: Active state is distinct, Inactive and Apo are similar.

---

## 9. Error Handling

### Example 9.1: Robust parsing with error recovery

```python
from bsm.lmp.parser import LMPParser, LMPParsingError

parser = LMPParser()

def safe_parse(file_path):
    """Parse with comprehensive error handling."""
    try:
        result = parser.parse_file(file_path)
        return {'success': True, 'data': result, 'error': None}
    
    except FileNotFoundError:
        return {'success': False, 'data': None, 'error': 'File not found'}
    
    except LMPParsingError as e:
        return {'success': False, 'data': None, 'error': f'Parsing error: {e}'}
    
    except Exception as e:
        return {'success': False, 'data': None, 'error': f'Unexpected error: {e}'}

# Use safe parser
result = safe_parse("P12931_Active.xml")

if result['success']:
    print(f"✅ Parsed successfully: {result['data']['protein_name']}")
else:
    print(f"❌ Parsing failed: {result['error']}")
```

---

### Example 9.2: Handle missing PTMs gracefully

```python
from bsm.lmp.parser import LMPParser

parser = LMPParser()
lmp_data = parser.parse_file("P12931_Apo.xml")

# Check if PTMs exist
if 'ptms' in lmp_data and lmp_data['ptms']:
    print(f"PTMs found: {len(lmp_data['ptms'])}")
    for ptm in lmp_data['ptms']:
        print(f"  - {ptm['type']} at {ptm['residue']}")
else:
    print("No PTMs annotated (Apo state)")
    # Fallback: Use default catalytic residues
    default_catalytic = lmp_data['functional_state']['catalytic_residues']
    print(f"Using default catalytic residues: {default_catalytic}")
```

---

### Example 9.3: Validate before expensive operations

```python
from bsm.lmp.validator import LMPValidator
from bsm.lmp.parser import LMPParser

validator = LMPValidator(xsd_path="src/bsm/lmp/lmp_v2_schema.xsd")
parser = LMPParser()

def process_lmp_file(file_path):
    """Process LMP file only if valid."""
    # Step 1: Validate
    validation_result = validator.validate_file(file_path)
    
    if not validation_result.is_valid:
        print(f"❌ Validation failed for {file_path}")
        for error in validation_result.errors:
            print(f"  - {error}")
        return None
    
    # Step 2: Parse (only if valid)
    print(f"✅ Validation passed, parsing...")
    lmp_data = parser.parse_file(file_path)
    
    # Step 3: Expensive operation (e.g., ESM-C embedding)
    print(f"✅ Parsed, computing embeddings...")
    # ... expensive operation ...
    
    return lmp_data

# Use validated processing
result = process_lmp_file("P12931_Active.xml")
```

---

## 10. Performance Optimization

### Example 10.1: Parallel batch processing

```python
from bsm.lmp.parser import LMPParser
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import multiprocessing as mp

def parse_single_file(file_path):
    """Parse a single LMP file (worker function)."""
    parser = LMPParser()
    try:
        result = parser.parse_file(file_path)
        return {'file': file_path, 'success': True, 'data': result}
    except Exception as e:
        return {'file': file_path, 'success': False, 'error': str(e)}

# Parallel processing
lmp_dir = Path("lmp_corpus/train")
lmp_files = list(lmp_dir.glob("*.xml"))

num_workers = mp.cpu_count()
print(f"Processing {len(lmp_files)} files with {num_workers} workers...")

with ProcessPoolExecutor(max_workers=num_workers) as executor:
    results = list(executor.map(parse_single_file, lmp_files))

# Analyze results
successes = [r for r in results if r['success']]
failures = [r for r in results if not r['success']]

print(f"\n✅ Successful: {len(successes)}")
print(f"❌ Failed: {len(failures)}")

if failures:
    print("\nFailed files:")
    for failure in failures[:10]:  # Show first 10
        print(f"  - {failure['file']}: {failure['error']}")
```

---

### Example 10.2: Caching parsed results

```python
from bsm.lmp.parser import LMPParser
import pickle
from pathlib import Path

class CachedLMPParser:
    def __init__(self, cache_dir="lmp_cache"):
        self.parser = LMPParser()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
    
    def parse_file(self, file_path):
        """Parse with caching."""
        file_path = Path(file_path)
        cache_file = self.cache_dir / f"{file_path.stem}.pkl"
        
        # Check cache
        if cache_file.exists():
            print(f"✅ Cache hit: {file_path.name}")
            with open(cache_file, 'rb') as f:
                return pickle.load(f)
        
        # Parse and cache
        print(f"⏳ Cache miss: {file_path.name}, parsing...")
        result = self.parser.parse_file(file_path)
        
        with open(cache_file, 'wb') as f:
            pickle.dump(result, f)
        
        return result

# Usage
cached_parser = CachedLMPParser()

# First call: parses and caches
result1 = cached_parser.parse_file("P12931_Active.xml")  # Slow

# Second call: loads from cache
result2 = cached_parser.parse_file("P12931_Active.xml")  # Fast
```

---

### Example 10.3: Memory-efficient streaming

```python
from bsm.lmp.parser import LMPParser
from pathlib import Path

def stream_lmp_files(lmp_dir, batch_size=32):
    """Generator for memory-efficient batch processing."""
    parser = LMPParser()
    lmp_files = list(Path(lmp_dir).glob("*.xml"))
    
    for i in range(0, len(lmp_files), batch_size):
        batch_files = lmp_files[i:i+batch_size]
        batch_results = []
        
        for file_path in batch_files:
            try:
                result = parser.parse_file(file_path)
                batch_results.append(result)
            except Exception as e:
                print(f"Error parsing {file_path}: {e}")
        
        yield batch_results

# Usage: process in batches without loading all into memory
for batch in stream_lmp_files("lmp_corpus/train", batch_size=32):
    # Process batch
    print(f"Processing batch of {len(batch)} proteins...")
    
    # Your processing logic here
    # (e.g., compute embeddings, save to database, etc.)
    
    # Batch is automatically garbage-collected after this iteration
```

---

## 🎓 Next Steps

1. **Read [API Reference](API_REFERENCE.md)** for detailed technical specs
2. **See [ROADMAP](ROADMAP.md)** for phased implementation timeline
3. **Check [INTEGRATION_GUIDE](INTEGRATION_GUIDE.md)** for system integration patterns

---

## 🆘 Getting Help

**Common Issues**:
- **Parsing errors**: Check [Error Handling](#9-error-handling) section
- **Performance issues**: See [Performance Optimization](#10-performance-optimization)
- **Validation failures**: Review [Validation Workflows](#6-validation-workflows)

**Questions**: GitHub Issues with `[LMP]` tag

---

**Last Updated**: November 2, 2025  
**Version**: 1.0  
**Status**: 🟢 Production-ready

---

🎉 **Happy coding with LMP v2.0!** 🎉

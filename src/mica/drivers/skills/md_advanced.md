---
name: md_advanced
tier: 2
domain: molecular_dynamics
keywords: [free_energy, metadynamics, umbrella_sampling, replica_exchange, steered_md]
description: Advanced molecular dynamics techniques — enhanced sampling and free energy calculations
---

# Advanced Molecular Dynamics

## When to Use
- Standard MD simulations are insufficient to sample relevant conformational changes.
- Free energy differences between states are needed (ΔG calculations).
- Studying rare events (protein folding, ligand unbinding).

## Enhanced Sampling Methods

### Metadynamics
- Add history-dependent bias along collective variables (CVs).
- Requires careful CV selection — RMSD, distance, dihedral angles.
- Tools: PLUMED plugin for GROMACS/OpenMM.

### Umbrella Sampling
- Restrain system along reaction coordinate windows.
- Combine with WHAM or MBAR for PMF reconstruction.
- Typically 20-40 windows per degree of freedom.

### Replica Exchange (REMD)
- Run parallel replicas at different temperatures.
- Exchange attempts every N steps based on Metropolis criterion.
- Good for protein folding studies.

## Free Energy Calculations
- **FEP** (Free Energy Perturbation) — alchemical transformations.
- **TI** (Thermodynamic Integration) — numerical integration over lambda.
- Both require careful setup of lambda windows.

## Prerequisites
- Completed equilibration (> 10 ns conventional MD).
- Validated force field parameters for all components.
- Converged energy minimization and NVT/NPT checks.

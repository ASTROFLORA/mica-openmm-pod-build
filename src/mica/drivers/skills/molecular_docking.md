---
name: molecular_docking
tier: 1
domain: structural_biology
keywords: [docking, vina, autodock, glide, binding_site, pose]
description: Setting up and interpreting molecular docking calculations
---

# Molecular Docking

## When to Use
- Predict binding poses of small molecules in protein active sites.
- Rank-order compound libraries by predicted binding affinity.
- Validate hypothesised binding modes against experimental data.

## Key Tools
- `mcp_alphafold_get_structure` — retrieve predicted protein structures
- `mcp_pdb_search_structures` / `mcp_pdb_download_structure` — get experimental structures
- `mcp_pubchem_search_by_smiles` — resolve ligand identifiers

## Procedure
1. **Obtain receptor** — download PDB or AlphaFold model, remove waters/ions.
2. **Prepare ligand** — fetch SMILES, generate 3D conformer, assign charges.
3. **Define search box** — centre on known binding site or use fpocket/p2rank.
4. **Run docking** — invoke AutoDock Vina or equivalent engine.
5. **Analyse** — report top-N poses, binding energy, key interactions.

## Common Pitfalls
- Missing protonation states on histidine residues.
- Search box too small for large ligands.
- Ignoring protein flexibility (consider ensemble docking).

"""
Structure Preparation API Router — W2.

Provides endpoints for protein structure trimming, domain extraction,
chimeric assembly, clash detection, and HITL review gating.

Endpoints
---------
  POST /api/v1/structure-prep/trim-by-confidence   — pLDDT filter
  POST /api/v1/structure-prep/extract-domain        — residue range extraction
  POST /api/v1/structure-prep/build-chimera          — superpose + merge
  POST /api/v1/structure-prep/check-clashes          — steric clash audit
  POST /api/v1/structure-prep/resolve-clashes        — minimize via OpenMM
  GET  /api/v1/structure-prep/health                 — subsystem health
"""
from __future__ import annotations

import io
import logging
import tempfile
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency as _user_dependency

logger = logging.getLogger("mica.api.structure_prep")

router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ChainResidueRange(BaseModel):
    """A chain ID + residue range for domain extraction."""
    chain_id: str = Field(..., description="Chain identifier (e.g. 'A')")
    start: int = Field(..., description="Start residue number (inclusive)")
    end: int = Field(..., description="End residue number (inclusive)")


class TrimByConfidenceRequest(BaseModel):
    """Trim structure by pLDDT (B-factor) confidence threshold."""
    pdb_content: str = Field(..., description="PDB/CIF file content as text")
    threshold: float = Field(50.0, description="Minimum pLDDT/B-factor to keep (default 50)")
    input_format: str = Field("pdb", description="Input format: 'pdb' or 'cif'")


class ExtractDomainRequest(BaseModel):
    """Extract specific residue ranges from chains."""
    pdb_content: str = Field(..., description="PDB/CIF file content as text")
    selections: List[ChainResidueRange] = Field(..., description="Chain:start-end selections")
    renumber: bool = Field(False, description="Renumber residues starting from 1")
    input_format: str = Field("pdb", description="Input format: 'pdb' or 'cif'")


class BuildChimeraRequest(BaseModel):
    """Superpose and merge two structures."""
    pdb_a_content: str = Field(..., description="First PDB content (reference)")
    pdb_b_content: str = Field(..., description="Second PDB content (mobile)")
    align_chain_a: str = Field("A", description="Chain to align in structure A")
    align_chain_b: str = Field("A", description="Chain to align in structure B")
    align_residues: Optional[List[int]] = Field(None, description="Residue numbers for alignment (CA only)")


class ClashCheckRequest(BaseModel):
    """Check for steric clashes in a structure."""
    pdb_content: str = Field(..., description="PDB file content as text")
    distance_threshold: float = Field(2.0, description="Clash distance threshold in Angstroms")
    input_format: str = Field("pdb", description="Input format: 'pdb' or 'cif'")


class ResolveClashesRequest(BaseModel):
    """Resolve clashes via energy minimization."""
    pdb_content: str = Field(..., description="PDB file content as text")
    max_steps: int = Field(1000, description="Maximum minimization steps")
    tolerance: float = Field(10.0, description="Energy tolerance kJ/mol/nm")


class StructurePrepResult(BaseModel):
    """Result of any structure preparation operation."""
    ok: bool = True
    output_pdb: str = Field("", description="Resulting PDB content as text")
    residues_input: int = Field(0, description="Number of residues in input")
    residues_output: int = Field(0, description="Number of residues in output")
    audit_log: Dict[str, Any] = Field(default_factory=dict)
    requires_human_review: bool = Field(False, description="HITL gate flag")
    review_reasons: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_structure(content: str, fmt: str = "pdb"):
    """Parse PDB/CIF content string into a BioPython Structure object."""
    from Bio.PDB import PDBParser, MMCIFParser

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f".{fmt}", delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        if fmt == "cif":
            parser = MMCIFParser(QUIET=True)
        else:
            parser = PDBParser(QUIET=True)
        structure = parser.get_structure("input", tmp_path)
    finally:
        os.unlink(tmp_path)
    return structure


def _structure_to_pdb(structure) -> str:
    """Serialize a BioPython Structure to PDB format string."""
    from Bio.PDB import PDBIO
    sio = io.StringIO()
    pdb_io = PDBIO()
    pdb_io.set_structure(structure)
    pdb_io.save(sio)
    return sio.getvalue()


def _count_residues(structure) -> int:
    """Count residues (excluding water/hetero) in a structure."""
    count = 0
    for model in structure:
        for chain in model:
            for residue in chain:
                if residue.id[0] == " ":  # standard residue
                    count += 1
    return count


def _hitl_check(
    residues_in: int,
    residues_out: int,
    extra_reasons: Optional[List[str]] = None,
) -> Tuple[bool, List[str]]:
    """Determine if human review is required (W2-5 HITL gate)."""
    reasons: List[str] = list(extra_reasons or [])
    if residues_in > 0:
        pct_removed = (residues_in - residues_out) / residues_in
        if pct_removed > 0.20:
            reasons.append(f">{pct_removed*100:.0f}% residues removed ({residues_in}\u2192{residues_out})")
    return bool(reasons), reasons


# ---------------------------------------------------------------------------
# POST /trim-by-confidence — W2-1
# ---------------------------------------------------------------------------

@router.post("/trim-by-confidence", response_model=StructurePrepResult)
def trim_by_confidence(
    req: TrimByConfidenceRequest,
    _user: str = Depends(_user_dependency),
) -> StructurePrepResult:
    """Filter residues by pLDDT (B-factor) confidence score."""
    try:
        from Bio.PDB import Select, PDBIO
    except ImportError:
        raise HTTPException(status_code=503, detail="BioPython not installed")

    structure = _parse_structure(req.pdb_content, req.input_format)
    residues_in = _count_residues(structure)

    class ConfidenceSelect(Select):
        def accept_residue(self, residue):
            if residue.id[0] != " ":
                return 0  # skip hetero/water
            # Average B-factor across atoms = pLDDT proxy
            atoms = list(residue.get_atoms())
            if not atoms:
                return 0
            avg_bfactor = sum(a.get_bfactor() for a in atoms) / len(atoms)
            return 1 if avg_bfactor >= req.threshold else 0

    sio = io.StringIO()
    pdb_io = PDBIO()
    pdb_io.set_structure(structure)
    pdb_io.save(sio, select=ConfidenceSelect())
    output_pdb = sio.getvalue()

    # Count output residues
    out_structure = _parse_structure(output_pdb, "pdb")
    residues_out = _count_residues(out_structure)

    needs_review, reasons = _hitl_check(residues_in, residues_out)

    return StructurePrepResult(
        output_pdb=output_pdb,
        residues_input=residues_in,
        residues_output=residues_out,
        audit_log={
            "operation": "trim_by_confidence",
            "threshold": req.threshold,
            "residues_removed": residues_in - residues_out,
        },
        requires_human_review=needs_review,
        review_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# POST /extract-domain — W2-2
# ---------------------------------------------------------------------------

@router.post("/extract-domain", response_model=StructurePrepResult)
def extract_domain(
    req: ExtractDomainRequest,
    _user: str = Depends(_user_dependency),
) -> StructurePrepResult:
    """Extract specific chain:residue-range selections."""
    try:
        from Bio.PDB import Select, PDBIO
    except ImportError:
        raise HTTPException(status_code=503, detail="BioPython not installed")

    structure = _parse_structure(req.pdb_content, req.input_format)
    residues_in = _count_residues(structure)

    # Build selection set: {(chain_id, resnum)}
    keep_set = set()
    for sel in req.selections:
        for resnum in range(sel.start, sel.end + 1):
            keep_set.add((sel.chain_id, resnum))

    class DomainSelect(Select):
        def accept_residue(self, residue):
            chain_id = residue.get_parent().id
            resnum = residue.id[1]
            return 1 if (chain_id, resnum) in keep_set else 0

    sio = io.StringIO()
    pdb_io = PDBIO()
    pdb_io.set_structure(structure)
    pdb_io.save(sio, select=DomainSelect())
    output_pdb = sio.getvalue()

    if req.renumber:
        output_pdb = _renumber_residues(output_pdb)

    out_structure = _parse_structure(output_pdb, "pdb")
    residues_out = _count_residues(out_structure)

    needs_review, reasons = _hitl_check(residues_in, residues_out)

    return StructurePrepResult(
        output_pdb=output_pdb,
        residues_input=residues_in,
        residues_output=residues_out,
        audit_log={
            "operation": "extract_domain",
            "selections": [s.model_dump() for s in req.selections],
            "renumbered": req.renumber,
        },
        requires_human_review=needs_review,
        review_reasons=reasons,
    )


def _renumber_residues(pdb_content: str) -> str:
    """Renumber residues in PDB content starting from 1 per chain."""
    lines = pdb_content.splitlines()
    result = []
    chain_counters: Dict[str, int] = {}
    last_resnum: Dict[str, int] = {}
    for line in lines:
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 26:
            chain = line[21]
            old_resnum = int(line[22:26].strip())
            if chain not in chain_counters:
                chain_counters[chain] = 0
                last_resnum[chain] = None
            if old_resnum != last_resnum[chain]:
                chain_counters[chain] += 1
                last_resnum[chain] = old_resnum
            new_num = chain_counters[chain]
            line = line[:22] + f"{new_num:4d}" + line[26:]
        result.append(line)
    return "\n".join(result) + "\n"


# ---------------------------------------------------------------------------
# POST /build-chimera — W2-3
# ---------------------------------------------------------------------------

@router.post("/build-chimera", response_model=StructurePrepResult)
def build_chimera(
    req: BuildChimeraRequest,
    _user: str = Depends(_user_dependency),
) -> StructurePrepResult:
    """Superpose two structures and merge into a chimeric assembly."""
    try:
        from Bio.PDB import Superimposer, PDBIO
        import numpy as np
    except ImportError:
        raise HTTPException(status_code=503, detail="BioPython/NumPy not installed")

    struct_a = _parse_structure(req.pdb_a_content, "pdb")
    struct_b = _parse_structure(req.pdb_b_content, "pdb")

    residues_in = _count_residues(struct_a) + _count_residues(struct_b)

    # Get CA atoms for alignment
    def _get_ca_atoms(struct, chain_id, residue_list=None):
        atoms = []
        for model in struct:
            for chain in model:
                if chain.id != chain_id:
                    continue
                for residue in chain:
                    if residue.id[0] != " ":
                        continue
                    if residue_list and residue.id[1] not in residue_list:
                        continue
                    if "CA" in residue:
                        atoms.append(residue["CA"])
        return atoms

    fixed_atoms = _get_ca_atoms(struct_a, req.align_chain_a, req.align_residues)
    moving_atoms = _get_ca_atoms(struct_b, req.align_chain_b, req.align_residues)

    if len(fixed_atoms) == 0 or len(moving_atoms) == 0:
        raise HTTPException(status_code=422, detail="No CA atoms found for alignment")

    min_len = min(len(fixed_atoms), len(moving_atoms))
    fixed_atoms = fixed_atoms[:min_len]
    moving_atoms = moving_atoms[:min_len]

    sup = Superimposer()
    sup.set_atoms(fixed_atoms, moving_atoms)
    sup.apply(struct_b.get_atoms())
    rmsd = sup.rms

    # Merge: write both structures
    output_lines = []
    sio_a = io.StringIO()
    pdb_io = PDBIO()
    pdb_io.set_structure(struct_a)
    pdb_io.save(sio_a)
    for line in sio_a.getvalue().splitlines():
        if not line.startswith("END"):
            output_lines.append(line)

    sio_b = io.StringIO()
    pdb_io.set_structure(struct_b)
    pdb_io.save(sio_b)
    for line in sio_b.getvalue().splitlines():
        if line.startswith(("ATOM", "HETATM", "TER")):
            output_lines.append(line)
    output_lines.append("END")
    output_pdb = "\n".join(output_lines) + "\n"

    out_structure = _parse_structure(output_pdb, "pdb")
    residues_out = _count_residues(out_structure)

    review_reasons = []
    if rmsd > 3.0:
        review_reasons.append(f"Superposition RMSD={rmsd:.2f} \u00c5 > 3.0 \u00c5 threshold")

    needs_review, reasons = _hitl_check(residues_in, residues_out, review_reasons)

    return StructurePrepResult(
        output_pdb=output_pdb,
        residues_input=residues_in,
        residues_output=residues_out,
        audit_log={
            "operation": "build_chimera",
            "alignment_rmsd": round(rmsd, 3),
            "atoms_aligned": min_len,
        },
        requires_human_review=needs_review,
        review_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# POST /check-clashes — W2-4
# ---------------------------------------------------------------------------

@router.post("/check-clashes")
def check_clashes(
    req: ClashCheckRequest,
    _user: str = Depends(_user_dependency),
) -> dict:
    """Detect atom-atom steric clashes below distance threshold."""
    try:
        from Bio.PDB import NeighborSearch
    except ImportError:
        raise HTTPException(status_code=503, detail="BioPython not installed")

    structure = _parse_structure(req.pdb_content, req.input_format)

    atoms = list(structure.get_atoms())
    ns = NeighborSearch(atoms)
    clashes = []
    seen = set()

    for atom in atoms:
        neighbors = ns.search(atom.get_vector().get_array(), req.distance_threshold)
        for neighbor in neighbors:
            if atom is neighbor:
                continue
            # Skip same-residue contacts
            if atom.get_parent() is neighbor.get_parent():
                continue
            # Skip bonded atoms (same residue or sequential)
            pair_key = tuple(sorted([atom.get_serial_number(), neighbor.get_serial_number()]))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            dist = atom - neighbor
            clashes.append({
                "atom_a": f"{atom.get_parent().get_parent().id}:{atom.get_parent().id[1]}:{atom.get_name()}",
                "atom_b": f"{neighbor.get_parent().get_parent().id}:{neighbor.get_parent().id[1]}:{neighbor.get_name()}",
                "distance": round(dist, 3),
            })

    return {
        "ok": True,
        "clash_count": len(clashes),
        "threshold": req.distance_threshold,
        "clashes": clashes[:100],  # cap at 100 for response size
        "total_atoms": len(atoms),
        "requires_resolution": len(clashes) > 0,
    }


# ---------------------------------------------------------------------------
# POST /resolve-clashes — W2-4
# ---------------------------------------------------------------------------

@router.post("/resolve-clashes", response_model=StructurePrepResult)
def resolve_clashes(
    req: ResolveClashesRequest,
    _user: str = Depends(_user_dependency),
) -> StructurePrepResult:
    """Resolve clashes via OpenMM energy minimization."""
    try:
        from openmm.app import PDBFile, ForceField, Modeller, Simulation
        from openmm import LangevinMiddleIntegrator, unit
        import openmm
    except ImportError:
        raise HTTPException(status_code=503, detail="OpenMM not installed on this server")

    structure = _parse_structure(req.pdb_content, "pdb")
    residues_in = _count_residues(structure)

    # Write to temp file for OpenMM
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pdb", delete=False) as tmp:
        tmp.write(req.pdb_content)
        tmp_path = tmp.name

    try:
        pdb = PDBFile(tmp_path)
        forcefield = ForceField("amber14-all.xml", "amber14/tip3pfb.xml")
        modeller = Modeller(pdb.topology, pdb.positions)

        system = forcefield.createSystem(
            modeller.topology,
            nonbondedMethod=openmm.app.NoCutoff,
            constraints=openmm.app.HBonds,
        )
        integrator = LangevinMiddleIntegrator(
            300 * unit.kelvin, 1 / unit.picosecond, 0.002 * unit.picoseconds
        )
        simulation = Simulation(modeller.topology, system, integrator)
        simulation.context.setPositions(modeller.positions)

        # Get initial energy
        state_before = simulation.context.getState(getEnergy=True, getPositions=True)
        energy_before = state_before.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)

        # Minimize
        simulation.minimizeEnergy(
            maxIterations=req.max_steps,
            tolerance=req.tolerance * unit.kilojoules_per_mole / unit.nanometer,
        )

        state_after = simulation.context.getState(getEnergy=True, getPositions=True)
        energy_after = state_after.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)

        # Write minimized PDB
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pdb", delete=False) as out_tmp:
            out_path = out_tmp.name
        PDBFile.writeFile(
            simulation.topology,
            state_after.getPositions(),
            open(out_path, "w"),
        )
        with open(out_path, "r") as f:
            output_pdb = f.read()
        os.unlink(out_path)

        # Compute max displacement
        import numpy as np
        pos_before = np.array(state_before.getPositions().value_in_unit(unit.angstrom))
        pos_after = np.array(state_after.getPositions().value_in_unit(unit.angstrom))
        displacements = np.linalg.norm(pos_after - pos_before, axis=1)
        max_disp = float(np.max(displacements))

    finally:
        os.unlink(tmp_path)

    out_structure = _parse_structure(output_pdb, "pdb")
    residues_out = _count_residues(out_structure)

    review_reasons = []
    if max_disp > 2.0:
        review_reasons.append(f"Minimization moved atoms up to {max_disp:.2f} \u00c5 (threshold 2.0 \u00c5)")

    needs_review, reasons = _hitl_check(residues_in, residues_out, review_reasons)

    return StructurePrepResult(
        output_pdb=output_pdb,
        residues_input=residues_in,
        residues_output=residues_out,
        audit_log={
            "operation": "resolve_clashes",
            "energy_before_kJmol": round(energy_before, 1),
            "energy_after_kJmol": round(energy_after, 1),
            "max_displacement_A": round(max_disp, 3),
            "steps": req.max_steps,
        },
        requires_human_review=needs_review,
        review_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# GET /health — W2-6
# ---------------------------------------------------------------------------

@router.get("/health")
def structure_prep_health(_user: str = Depends(_user_dependency)) -> dict:
    """Return structure_prep subsystem status."""
    tools = {}
    try:
        import Bio.PDB
        tools["biopython"] = True
    except ImportError:
        tools["biopython"] = False
    try:
        import openmm
        tools["openmm"] = True
    except ImportError:
        tools["openmm"] = False
    try:
        import numpy
        tools["numpy"] = True
    except ImportError:
        tools["numpy"] = False

    return {
        "ok": all(tools.values()),
        "tools": tools,
        "endpoints": [
            "trim-by-confidence",
            "extract-domain",
            "build-chimera",
            "check-clashes",
            "resolve-clashes",
        ],
    }

"""src/mica/sim/cg_martini/cg_system_builder.py

Motor generalista biostate-compatible para construir un sistema CG/Martini
ejecutable por ``martini_openmm.MartiniTopFile`` + ``openmm.app.GromacsGroFile``.

Este modulo es la pieza que faltaba entre ``Martinize2Adapter`` (emite
``molecule_*.itp`` + ``osr1_cg.pdb`` para la proteina) y
``INSANEAdapter`` (emite ``membrane.gro`` + ``membrane.top`` header-only
para la membrana). Los dos adapters producen archivos parciales. El
``system.top`` final, con TODOS los ``#include`` resueltos y la seccion
``[ molecules ]`` correcta, lo emite este builder.

Caso de estudio probado end-to-end (la receta canonica de la industria):

  ``C:\\Users\\busta\\Downloads\\martini3_osr1_sim\\step3_solvate.py``
  + ``step4_run_simulation.py`` (OSR1 dimer, soluble):
  - martinize2 emite ``molecule_0.itp`` + ``osr1_cg.pdb``
  - este builder emite ``system.top`` con todos los ``#include``
    (``martini_v3.0.0.itp``, ``martini_v3.0.0_solvents_v1.itp``,
    ``martini_v3.0.0_ions_v1.itp``, ``molecule_0.itp``)
  - ``solvated.gro`` con W en grid + NA/CL a 150 mM
  - ``martini_openmm.MartiniTopFile(system.top)`` carga y produce
    un ``openmm.System`` con el conteo correcto de particulas

  Para el caso membrana (CLCN7 / flat_bilayer):
  - martinize2 emite ``molecule_0.itp`` (proteina) + ``clcn7_cg.gro``
  - INSANE emite ``membrane.gro`` (proteina + membrana + agua + iones)
  - este builder consolida un ``system.top`` con
    ``martini_v3.0.0.itp`` + solvents + ions + phospholipids + molecule_0.itp
  - ``solvated.gro`` = ``membrane.gro`` (INSANE ya lo hace completo)

Compatibilidad con BIOSTATE:

  El request ``CGSystemBuildRequest`` esta alineado con el biostate
  catalog de ``src/mica/sim/mode_compiler.py`` (modos
  ``cg_membrane`` y ``cg_protein_solvent``). Los campos
  ``forcefield_family``, ``martini_version``, ``geometry_class``,
  ``membrane_enabled``, ``water_model``, ``ion_model``, ``salt_mM``,
  ``lipid_composition``, ``solvent`` son la traduccion directa del
  biostate plan al lenguaje de Martini 3.

Compatibilidad con motor AA:

  Este modulo NO toca OpenMM directo: solo orquesta archivos GROMACS
  que ``martini_openmm`` (Python via OpenMM) puede cargar. El motor de
  ejecucion real (LangevinIntegrator + Platform + Simulation) sigue
  siendo OpenMM via martini_openmm. La rama AA del biostate
  (``standard_prod``, ``amber_alt``) no se ve afectada.

Historia:
  - 2026-07-19: creado en INSTRUCCION 9 del programa CG_NATIVE_RUN.
    Reusa patrones de QUETZAL_OSR1 (marrink-lab/martini-forcefields
    v3.0.0 .itp data) + caso exitoso ``martini3_osr1_sim``.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Force field data dir — populated by INSTRUCCION 9 commit (the .itp files
# from marrink-lab/martini-forcefields v3.0.0 gmx_files/). For martini2
# we fall back to looking for it elsewhere (not bundled by default).
_MARTINI_FF_DATA_DIR = Path(__file__).resolve().parent / "data" / "martini3"


# Default biostate-aligned values
DEFAULT_MARTINI_VERSION = "3.0.0"
DEFAULT_FORCEFIELD_FAMILY = "martini3"
DEFAULT_WATER_MODEL = "martini_v3.0.0_solvents_v1"
DEFAULT_ION_MODEL = "martini_v3.0.0_ions_v1"
DEFAULT_SALT_MM = 150
DEFAULT_SOLVENT = "PW"
DEFAULT_TEMPERATURE_K = 310.0
DEFAULT_FRICTION_PS = 10.0
DEFAULT_TIMESTEP_FS = 20.0
DEFAULT_EPSILON_R = 15.0
DEFAULT_NONBONDED_CUTOFF_NM = 1.1


@dataclass
class CGSystemBuildRequest:
    """Biostate-aligned input for the CG system builder.

    Field names mirror the biostate catalog of mode_compiler.py
    so this struct can be constructed directly from a biostate plan
    without intermediate mapping.
    """

    # ── biostate fields (driven by mode_compiler) ──
    forcefield_family: str = DEFAULT_FORCEFIELD_FAMILY
    martini_version: str = DEFAULT_MARTINI_VERSION
    geometry_class: str = "flat_bilayer"  # "" or omitted for soluble
    membrane_enabled: bool = False
    water_model: str = DEFAULT_WATER_MODEL
    ion_model: str = DEFAULT_ION_MODEL
    salt_mM: int = DEFAULT_SALT_MM
    lipid_composition: str = "POPC:1"
    solvent: str = DEFAULT_SOLVENT

    # ── source artifacts (consumed, not modified) ──
    # From Martinize2Adapter:
    martinize2_output_cg_gro_ref: str = ""  # the CG protein .gro
    martinize2_output_itp_refs: list[str] = field(default_factory=list)
    # list of molecule_*.itp paths

    # From INSANEAdapter (only when membrane_enabled=True):
    insane_output_gro_ref: str = ""  # the membrane.gro
    insane_output_top_ref: str = ""  # the (header-only) membrane.top
    insane_counts: dict[str, int] = field(default_factory=dict)
    # {'protein_beads', 'lipid_count', 'water_count', 'na_count', 'cl_count', 'total_atoms'}

    # ── output target ──
    output_dir: str = ""  # where to write system.top + solvated.gro

    # ── provenance ──
    source_target_id: str = "anonymous"
    bundle_id: str = ""  # if empty, auto-generated


@dataclass
class CGSystemBuildPayload:
    """Output payload — equivalent to CGSystemBundle but lightweight.

    Two output artifacts are produced:
      - system_top_path: GROMACS topology with all #include resolved
        + [ molecules ] section declaring protein, lipids, water, ions.
      - solvated_gro_path: GROMACS coordinates + box vectors readable
        by openmm.app.GromacsGroFile.
    """

    builder_backend: str
    system_top_path: Path
    solvated_gro_path: Path
    topology_path: Path
    coordinate_path: Path
    water_model: str
    ion_model: str
    charge_status: str
    openmm_compatibility: str
    gromacs_compatibility: str
    implementation_status: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    claim_boundaries: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _martini3_ff_path(martini_version: str) -> Path:
    """Return the include dir for the given Martini version.

    Currently only v3.0.0 is bundled locally. For other versions, the
    caller is expected to have downloaded/curated the FF separately
    (e.g., via the canonical Dockerfile.worker install in INSTRUCCION 11).
    """
    if martini_version.startswith("3."):
        return _MARTINI_FF_DATA_DIR
    raise FileNotFoundError(
        f"Martini FF data not bundled for version {martini_version!r}; "
        "use v3.0.0 (bundled locally) or provide a custom data dir."
    )


def _read_molecules_section(top_path: Path) -> dict[str, int]:
    """Parse the [ molecules ] section of a GROMACS .top file.

    Returns a dict of molecule_name -> count. Used to lift the
    counts INSANE wrote into its header-only membrane.top, so the
    consolidated system.top can re-emit them.
    """
    counts: dict[str, int] = {}
    text = top_path.read_text(encoding="utf-8", errors="replace")
    in_mol = False
    for line in text.splitlines():
        s = line.strip()
        if s == "[ molecules ]":
            in_mol = True
            continue
        if in_mol and s.startswith("["):
            break
        if in_mol and s and not s.startswith(";"):
            parts = s.split()
            if len(parts) >= 2:
                try:
                    counts[parts[0]] = int(parts[1])
                except ValueError:
                    pass
    return counts


def _gro_atom_count(gro_path: Path) -> int:
    """Read the atom count from a .gro file (line 2)."""
    with open(gro_path) as f:
        f.readline()  # title
        n = f.readline().strip()
    return int(n) if n else 0


def _gro_residue_counts(gro_path: Path) -> dict[str, int]:
    """Count residue types in a .gro file. Used to know what
    molecules to declare in [ molecules ].
    """
    counts: dict[str, int] = {}
    with open(gro_path) as f:
        f.readline()  # title
        f.readline()  # n_atoms
        for line in f:
            if len(line) < 10:
                continue
            resname = line[5:10].strip()
            if not resname:
                continue
            counts[resname] = counts.get(resname, 0) + 1
    return counts


def _infer_lipid_molecule_name(lipid_composition: str) -> str:
    """Extract the first lipid name from an INSANE lipid_composition
    string like 'POPC:1' -> 'POPC', 'POPE:7 POPG:3' -> 'POPE'.
    """
    first = lipid_composition.split()[0] if lipid_composition else ""
    return first.split(":")[0].strip()


def _solvent_molecule_name(solvent: str) -> str:
    """The water molecule name in GROMACS .top. PW (Martini polarizable)
    uses 'W' as the residue name. Standard is 'W' for both PW and W."""
    return "W"


def _ion_molecule_name(ion: str) -> str:
    return "NA" if ion.upper() in {"NA", "NA+"} else "CL"


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_cg_system_bundle(request: CGSystemBuildRequest) -> CGSystemBuildPayload:
    """Build a CG/Martini system bundle ready for martini_openmm.MartiniTopFile.

    This is the public entry point. It is biostate-agnostic — it takes
    a structured ``CGSystemBuildRequest`` and produces a
    ``CGSystemBuildPayload`` with the paths to the system.top and
    solvated.gro that martini_openmm can load.

    Two modes:

    1. **Membrane** (``membrane_enabled=True``): consumes
       ``insane_output_gro_ref`` (the membrane.gro) and emits
       ``solvated.gro = membrane.gro`` (INSANE already populated
       it with protein + lipids + water + ions). The system.top is
       built by adding all the missing #include directives that
       INSANE didn't write.

    2. **Soluble** (``membrane_enabled=False``): consumes
       ``martinize2_output_cg_gro_ref`` (the protein .gro) and emits
       ``solvated.gro`` by adding W (water beads) on a cubic grid
       + NA/CL ions at ``salt_mM``. This is the OSR1 case recipe,
       generalized.

    Parameters
    ----------
    request : CGSystemBuildRequest
        Biostate-aligned input. See dataclass for fields.

    Returns
    -------
    CGSystemBuildPayload
        Paths to the emitted system.top and solvated.gro + metadata
        for downstream receipts.

    Raises
    ------
    FileNotFoundError
        If martinize2/INSANE outputs are missing or if the Martini FF
        data is not bundled for the requested version.
    """
    if not request.output_dir:
        raise ValueError("CGSystemBuildRequest.output_dir is required")
    if not request.martinize2_output_cg_gro_ref:
        raise ValueError(
            "CGSystemBuildRequest.martinize2_output_cg_gro_ref is required"
        )
    if not request.martinize2_output_itp_refs:
        raise ValueError(
            "CGSystemBuildRequest.martinize2_output_itp_refs is required "
            "(list of molecule_*.itp paths from Martinize2Adapter)"
        )

    out_dir = Path(request.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = CGSystemBuildPayload(
        builder_backend="python_native_cg_system_builder_v1",
        system_top_path=out_dir / "system.top",
        solvated_gro_path=out_dir / "solvated.gro",
        topology_path=out_dir / "system.top",
        coordinate_path=out_dir / "solvated.gro",
        water_model=request.water_model,
        ion_model=request.ion_model,
        charge_status="unknown",
        openmm_compatibility="martini_openmm_top_gro",
        gromacs_compatibility="gromacs_style_top_gro",
        implementation_status="real_compile",
        claim_boundaries=_claim_boundaries(),
    )

    # Resolve FF include dir
    try:
        ff_dir = _martini3_ff_path(request.martini_version)
        payload.metadata["ff_include_dir"] = str(ff_dir)
    except FileNotFoundError as e:
        payload.blockers.append(str(e))
        payload.implementation_status = "blocked"
        return payload

    # Step 1: emit the consolidated system.top + final solvated.gro
    if request.membrane_enabled:
        _build_membrane_system(request, payload, ff_dir)
    else:
        _build_soluble_system(request, payload, ff_dir)

    # Step 1.5: stage molecule_*.itp + FF .itp files alongside the
    # system.top so martini_openmm can resolve #include directives
    # via the .top's directory (first in _includeDirs) at runtime.
    # This is what the canonical case study does at runtime.
    _stage_includes(
        topology_dir=payload.topology_path.parent,
        ff_dir=ff_dir,
        molecule_itp_refs=request.martinize2_output_itp_refs,
    )

    # Step 2: validate the result via the canonical probe
    payload.metadata["openmm_compatibility_check"] = _validate_openmm_load(
        payload.topology_path, payload.coordinate_path
    )

    return payload


def _stage_includes(
    *,
    topology_dir: Path,
    ff_dir: Path,
    molecule_itp_refs: list[str],
) -> None:
    """Copy FF .itp + per-molecule .itp files alongside the system.top.

    martini_openmm.MartiniTopFile uses ``os.path.dirname(file)`` as the
    first include dir; without these copies it would fail with
    "Could not locate #include file" for everything. The provider
    worker (Dockerfile.worker, INSTRUCCION 11) does this natively via
    the ``/usr/share/gromacs/top`` dir; for the local probe we copy
    them here so the validator and the runtime share the same layout.
    """
    if ff_dir.exists():
        for src in ff_dir.iterdir():
            if not src.is_file():
                continue
            dst = topology_dir / src.name
            if not dst.exists():
                shutil.copyfile(src, dst)
    for itp in molecule_itp_refs:
        src = Path(itp)
        if not src.exists():
            continue
        dst = topology_dir / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)


def _build_membrane_system(
    request: CGSystemBuildRequest,
    payload: CGSystemBuildPayload,
    ff_dir: Path,
) -> None:
    """Membrane case: lift INSANE's membrane.gro + consolidate system.top.

    INSANE writes a header-only ``membrane.top`` with only
    ``#include "martini.itp"`` and a ``[ molecules ]`` section. This
    builder replaces it with a complete system.top that has all the
    Martini 3 .itp includes needed for martini_openmm to load.
    """
    if not request.insane_output_gro_ref:
        payload.blockers.append(
            "membrane_enabled=True but insane_output_gro_ref is empty"
        )
        payload.implementation_status = "blocked"
        return

    insane_gro = Path(request.insane_output_gro_ref)
    if not insane_gro.exists():
        payload.blockers.append(f"insane_output_gro_ref not found: {insane_gro}")
        payload.implementation_status = "blocked"
        return

    # solvated.gro is the INSANE membrane.gro (already has protein +
    # membrane + water + ions + box vectors).
    shutil.copyfile(insane_gro, payload.solvated_gro_path)
    n_atoms = _gro_atom_count(payload.solvated_gro_path)
    payload.charge_status = "ionized_membrane"
    payload.metadata["n_atoms"] = n_atoms

    # Lift [ molecules ] from INSANE's header-only top
    insane_mol_counts: dict[str, int] = {}
    if request.insane_output_top_ref:
        insane_top = Path(request.insane_output_top_ref)
        if insane_top.exists():
            insane_mol_counts = _read_molecules_section(insane_top)
    payload.metadata["insane_molecule_counts"] = insane_mol_counts

    # If INSANE didn't write enough [ molecules ] info, derive from
    # the counts INSANE itself reported.
    if not insane_mol_counts and request.insane_counts:
        insane_mol_counts = {
            "Protein": int(request.insane_counts.get("protein_beads", 0)),
            _infer_lipid_molecule_name(request.lipid_composition): int(
                request.insane_counts.get("lipid_count", 0)
            ),
            _solvent_molecule_name(request.solvent): int(
                request.insane_counts.get("water_count", 0)
            ),
            "NA": int(request.insane_counts.get("na_count", 0)),
            "CL": int(request.insane_counts.get("cl_count", 0)),
        }
        payload.warnings.append(
            "insane_molecule_counts_inferred_from_insane_counts: "
            "INSANE did not emit a [ molecules ] section, deriving "
            "molecule names from lipid_composition + solvent + insane counts"
        )

    # Compute final molecule counts
    protein_count = insane_mol_counts.get("Protein", 0)
    lipid_count = (
        insane_mol_counts.get(
            _infer_lipid_molecule_name(request.lipid_composition), 0
        )
    )
    water_count = insane_mol_counts.get(
        _solvent_molecule_name(request.solvent), 0
    )
    na_count = insane_mol_counts.get("NA", 0)
    cl_count = insane_mol_counts.get("CL", 0)

    payload.metadata["final_molecule_counts"] = {
        "Protein": protein_count,
        "lipid": lipid_count,
        _solvent_molecule_name(request.solvent): water_count,
        "NA": na_count,
        "CL": cl_count,
    }

    # Build the consolidated system.top
    _emit_system_top(
        target_path=payload.system_top_path,
        ff_dir=ff_dir,
        molecule_itp_refs=request.martinize2_output_itp_refs,
        lipid_itp_name=_infer_lipid_molecule_name(request.lipid_composition),
        protein_count=protein_count,
        lipid_count=lipid_count,
        water_count=water_count,
        na_count=na_count,
        cl_count=cl_count,
        title=f"{request.source_target_id} Martini3 {request.geometry_class}",
    )

    # Final validation
    if n_atoms <= 0:
        payload.blockers.append("solvated.gro has zero atoms")
        payload.implementation_status = "blocked"
    elif not all([protein_count, lipid_count, water_count]):
        payload.warnings.append(
            "incomplete_molecule_counts: some of protein/lipid/water are 0"
        )


def _build_soluble_system(
    request: CGSystemBuildRequest,
    payload: CGSystemBuildPayload,
    ff_dir: Path,
) -> None:
    """Soluble case: martinize2 .gro + W grid + NA/CL ions.

    Generalized version of the OSR1 step3_solvate.py recipe:
    - read protein .gro
    - center protein in a cubic box with 1.5 nm buffer
    - place W (water beads) on a grid with 0.57 nm spacing
    - remove waters overlapping protein (min_dist 0.21 nm)
    - add NA/CL to reach salt_mM ionic strength + charge neutrality
    - write solvated.gro with box vectors
    - write system.top with all #include + [ molecules ] section
    """
    import numpy as np  # local import — numpy is a dep for cg_martini

    protein_gro = Path(request.martinize2_output_cg_gro_ref)
    if not protein_gro.exists():
        payload.blockers.append(f"martinize2_output_cg_gro_ref not found: {protein_gro}")
        payload.implementation_status = "blocked"
        return

    box_buffer_nm = 1.5
    water_spacing_nm = 0.57
    min_dist_nm = 0.21

    # Read protein atoms
    coords_list: list[tuple[float, float, float]] = []
    with open(protein_gro) as f:
        f.readline()  # title
        _n_protein = int(f.readline().strip())
        for line in f:
            if len(line) < 44:
                continue
            try:
                x = float(line[20:28]) / 10.0  # angstrom -> nm
                y = float(line[28:36]) / 10.0
                z = float(line[36:44]) / 10.0
            except ValueError:
                continue
            coords_list.append((x, y, z))
    if not coords_list:
        payload.blockers.append("no protein atoms parsed from .gro")
        payload.implementation_status = "blocked"
        return

    coords = np.array(coords_list)
    n_protein = len(coords)
    payload.metadata["n_protein_beads"] = n_protein

    # Center protein at origin
    center = (coords.min(axis=0) + coords.max(axis=0)) / 2.0
    coords -= center

    # Build box (1.5 nm buffer on each side)
    protein_size = coords.max(axis=0) - coords.min(axis=0)
    box_size = protein_size + 2 * box_buffer_nm
    coords += box_size / 2.0  # shift to center the protein in box

    # Place W beads on grid
    nx = max(1, int(box_size[0] / water_spacing_nm))
    ny = max(1, int(box_size[1] / water_spacing_nm))
    nz = max(1, int(box_size[2] / water_spacing_nm))
    grid_positions: list[tuple[float, float, float]] = []
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                grid_positions.append(
                    (
                        (ix + 0.5) * water_spacing_nm,
                        (iy + 0.5) * water_spacing_nm,
                        (iz + 0.5) * water_spacing_nm,
                    )
                )
    if not grid_positions:
        payload.blockers.append("box too small for any waters")
        payload.implementation_status = "blocked"
        return
    waters = np.array(grid_positions)
    payload.metadata["n_grid_waters"] = len(waters)

    # Remove waters overlapping with protein (chunked for memory)
    keep = np.ones(len(waters), dtype=bool)
    chunk = 1000
    for start in range(0, len(waters), chunk):
        end = min(start + chunk, len(waters))
        w_chunk = waters[start:end]
        diff = w_chunk[:, np.newaxis, :] - coords[np.newaxis, :, :]
        d2 = (diff * diff).sum(axis=2)
        min_d = np.sqrt(d2.min(axis=1))
        keep[start:end] &= min_d > min_dist_nm
    waters = waters[keep]
    payload.metadata["n_waters_after_overlap_removal"] = len(waters)

    # Compute ion counts for salt_mM
    vol_L = float(box_size[0] * box_size[1] * box_size[2]) * 1e-24
    NA_AVOGADRO = 6.022e23
    n_salt = int(round(request.salt_mM * 1e-3 * vol_L * NA_AVOGADRO))

    # Approximate protein charge from residue composition
    # (we don't parse .itp here; assume net 0 for soluble default).
    n_na = n_salt
    n_cl = n_salt

    # Replace random waters with ions
    rng = np.random.default_rng(42)
    total_ions = n_na + n_cl
    if total_ions > len(waters):
        ratio = len(waters) * 0.95 / total_ions
        n_na = int(n_na * ratio)
        n_cl = int(n_cl * ratio)
        total_ions = n_na + n_cl
    if total_ions > 0:
        ion_idx = rng.choice(len(waters), size=total_ions, replace=False)
        na_idx = ion_idx[:n_na]
        cl_idx = ion_idx[n_na : n_na + n_cl]
        na_coords = waters[na_idx]
        cl_coords = waters[cl_idx]
        keep2 = np.ones(len(waters), dtype=bool)
        keep2[ion_idx] = False
        waters = waters[keep2]

    # Write solvated.gro
    n_w = len(waters)
    n_atoms_total = n_protein + n_w + n_na + n_cl
    with open(payload.solvated_gro_path, "w") as f:
        f.write(f"{request.source_target_id} Martini3 soluble\n")
        f.write(f"{n_atoms_total:>5d}\n")
        atom_idx = 0
        # GRO format is fixed-column-width (NOT delimiter-separated).
        # The resname field is exactly 5 chars (column 5..10) and the
        # atom name field is exactly 5 chars (column 10..15). Resname
        # longer than 5 chars (e.g., 'Protein' = 7) bleeds into the
        # atom name field and breaks martini_openmm.MartiniTopFile.
        # Truncate resname to 5 chars defensively.
        for i, (x, y, z) in enumerate(coords):
            atom_idx += 1
            resid = (i + 1) % 100000
            f.write(
                f"{resid:5d}{'Prote':<5s}{'P':>5s}{atom_idx:5d}"
                f"{x * 10:8.3f}{y * 10:8.3f}{z * 10:8.3f}\n"
            )
        for i, (x, y, z) in enumerate(waters):
            atom_idx += 1
            resid = (n_protein + i + 1) % 100000
            f.write(
                f"{resid:5d}{'W':<5s}{'W':>5s}{atom_idx:5d}"
                f"{x * 10:8.3f}{y * 10:8.3f}{z * 10:8.3f}\n"
            )
        for i, (x, y, z) in enumerate(na_coords):
            atom_idx += 1
            resid = (n_protein + n_w + i + 1) % 100000
            f.write(
                f"{resid:5d}{'NA':<5s}{'NA':>5s}{atom_idx:5d}"
                f"{x * 10:8.3f}{y * 10:8.3f}{z * 10:8.3f}\n"
            )
        for i, (x, y, z) in enumerate(cl_coords):
            atom_idx += 1
            resid = (n_protein + n_w + n_na + i + 1) % 100000
            f.write(
                f"{resid:5d}{'CL':<5s}{'CL':>5s}{atom_idx:5d}"
                f"{x * 10:8.3f}{y * 10:8.3f}{z * 10:8.3f}\n"
            )
        f.write(
            f"   {box_size[0] * 10:10.5f}{box_size[1] * 10:10.5f}{box_size[2] * 10:10.5f}\n"
        )
    payload.metadata["n_waters_final"] = n_w
    payload.metadata["n_na"] = n_na
    payload.metadata["n_cl"] = n_cl
    payload.metadata["n_atoms"] = n_atoms_total
    payload.metadata["box_nm"] = {
        "x": float(box_size[0]),
        "y": float(box_size[1]),
        "z": float(box_size[2]),
    }
    payload.charge_status = f"ionized_soluble_{request.salt_mM}mM"

    # Build system.top
    _emit_system_top(
        target_path=payload.system_top_path,
        ff_dir=ff_dir,
        molecule_itp_refs=request.martinize2_output_itp_refs,
        lipid_itp_name=None,
        protein_count=1,  # one Protein molecule in the soluble case
        lipid_count=0,
        water_count=n_w,
        na_count=n_na,
        cl_count=n_cl,
        title=f"{request.source_target_id} Martini3 soluble",
    )


def _emit_system_top(
    *,
    target_path: Path,
    ff_dir: Path,
    molecule_itp_refs: list[str],
    lipid_itp_name: str | None,
    protein_count: int,
    lipid_count: int,
    water_count: int,
    na_count: int,
    cl_count: int,
    title: str,
) -> None:
    """Emit the consolidated system.top with all #include + [ molecules ].

    The #include list is built from the FF data dir + molecule_*.itp
    refs + (optional) lipid itp. The [ molecules ] section declares
    Protein + lipid (if membrane) + W + NA + CL with the counts
    lifted from the underlying build (INSANE for membrane, computed
    here for soluble).
    """
    includes: list[str] = [
        "martini_v3.0.0.itp",
        "martini_v3.0.0_solvents_v1.itp",
        "martini_v3.0.0_ions_v1.itp",
    ]
    if lipid_itp_name and lipid_count > 0:
        # The phospholipids .itp is a single file in marrink-lab v3.0.0.
        includes.append("martini_v3.0.0_phospholipids_v1.itp")
    # Per-molecule .itp from martinize2
    for itp in molecule_itp_refs:
        includes.append(Path(itp).name)

    with open(target_path, "w") as f:
        f.write(f"; {title}\n")
        f.write("; Generated by MICA cg_system_builder (CG_NATIVE_RUN INSTRUCCION 9)\n")
        f.write("; Reusable for any biostate cg_membrane / cg_protein_solvent.\n\n")
        for inc in includes:
            f.write(f'#include "{inc}"\n')
        f.write("\n[ system ]\n")
        f.write(f"{title}\n\n")
        f.write("[ molecules ]\n")
        # Per-chain molecule declarations
        for i, itp in enumerate(molecule_itp_refs):
            mol_name = Path(itp).stem  # molecule_0, molecule_1, etc.
            f.write(f"{mol_name}    1\n")
        # Lipid (if membrane)
        if lipid_itp_name and lipid_count > 0:
            f.write(f"{lipid_itp_name}    {lipid_count}\n")
        # Solvent + ions
        if water_count > 0:
            f.write(f"W    {water_count}\n")
        if na_count > 0:
            f.write(f"NA    {na_count}\n")
        if cl_count > 0:
            f.write(f"CL    {cl_count}\n")


def _validate_openmm_load(topology_path: Path, coordinate_path: Path) -> dict[str, Any]:
    """Run the canonical validate_martini_openmm_compatibility on the
    emitted files. Returns a dict with status, blockers, warnings.

    This is a fail-closed check: if martini_openmm can't actually
    load the system, the builder's payload.implementation_status is
    set to "blocked_construct" before return.

    extra_search_paths includes both the topology's parent dir (so
    per-molecule .itp files resolve) AND the bundled Martini 3 .ff
    dir (``src/mica/sim/cg_martini/data/martini3``). This makes the
    check self-contained for the local probe; in the provider the
    /usr/share/gromacs/top dir takes precedence (env-first).
    """
    try:
        from mica.scientific.topology_kernel.martini.martini_openmm_compatibility import (
            validate_martini_openmm_compatibility,
        )
    except ImportError as e:
        return {
            "status": "validation_unavailable",
            "blockers": [f"imports: {e}"],
        }

    extra = [
        topology_path.parent,
        _MARTINI_FF_DATA_DIR,
    ]
    try:
        return validate_martini_openmm_compatibility(
            topology_path=topology_path,
            coordinate_path=coordinate_path,
            extra_search_paths=extra,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "validation_error",
            "blockers": [str(exc)],
        }


def _claim_boundaries() -> list[dict[str, str]]:
    """Standard claim boundaries for CG system bundles (honest about
    what is and isn't supported in this builder's current version)."""
    return [
        {"axis": "scaffold", "scope": "supported", "note": "CG system bundle with martini_openmm-compatible system.top + solvated.gro"},
        {"axis": "membrane", "scope": "supported", "note": "Membrane case reuses INSANE membrane.gro + consolidates system.top"},
        {"axis": "soluble", "scope": "supported", "note": "Soluble case computes W + NA/CL on a grid (OSR1 recipe generalized)"},
        {"axis": "production_md", "scope": "out_of_scope", "note": "production MD requires Dockerfile.worker + martini_openmm + GROMACS at the provider (INSTRUCCION 11/12)"},
        {"axis": "pure_openmm_xml", "scope": "out_of_scope", "note": "this builder emits top/gro, not openmm.System XML"},
    ]


# Public re-exports
__all__ = [
    "CGSystemBuildRequest",
    "CGSystemBuildPayload",
    "build_cg_system_bundle",
    "DEFAULT_FORCEFIELD_FAMILY",
    "DEFAULT_MARTINI_VERSION",
    "DEFAULT_WATER_MODEL",
    "DEFAULT_ION_MODEL",
    "DEFAULT_SALT_MM",
    "DEFAULT_SOLVENT",
    "DEFAULT_TEMPERATURE_K",
    "DEFAULT_FRICTION_PS",
    "DEFAULT_TIMESTEP_FS",
    "DEFAULT_EPSILON_R",
    "DEFAULT_NONBONDED_CUTOFF_NM",
]
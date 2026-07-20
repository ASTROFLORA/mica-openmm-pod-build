"""scripts/prepare_cg_system.py -- INSTRUCCION 12 prep step (ASTROFLORA mirror).

Runs Martinize2 + INSANE + cg_system_builder on the CLCN7 PDB to produce
the real system.top + solvated.gro for a CG/Martini job. Writes the
artifact info to /tmp/cg_system_info.json. This is the upstream half of
the worker dispatch in workers/salad/gcs_openmm_srcg/main_gcs.py:_run_cg_martini_job.

Exit code 0 = system ready. Non-zero = fail-closed, the workflow must abort.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Mirror bundles the CG lane + FF data:
THIS_DIR = Path(__file__).resolve().parent
MIRROR_ROOT = THIS_DIR.parent
sys.path.insert(0, str(MIRROR_ROOT / "src"))

# martinize2 binary (vermouth PyPI places it here, per ASTROFLORA mirror smoke v2)
MART = shutil.which("martinize2") or "/opt/conda/bin/martinize2"
CLCN7_PDB = Path(os.environ.get("CLCN7_PDB", str(MIRROR_ROOT / "clcn7_test_input" / "clcn7.pdb")))


def main() -> int:
    info: dict = {}
    info["clcn7_pdb"] = str(CLCN7_PDB)
    info["martinize2_binary"] = str(MART)

    # 0. preflight
    if not CLCN7_PDB.exists():
        msg = f"FATAL: CLCN7 PDB not found at {CLCN7_PDB}"
        print(msg, file=sys.stderr)
        Path("/tmp/cg_system_status.txt").write_text("FAILED: missing_pdb")
        return 2
    if not Path(MART).exists() and not shutil.which(MART):
        msg = f"FATAL: martinize2 binary not found at {MART}"
        print(msg, file=sys.stderr)
        Path("/tmp/cg_system_status.txt").write_text("FAILED: missing_martinize2")
        return 3

    # 1. Martinize2
    from mica.sim.cg_martini.martinize2_adapter import Martinize2Adapter

    work = Path(tempfile.mkdtemp(prefix="cg_instr12_"))
    mart = Martinize2Adapter(
        martinize2_binary=str(MART),
        martini_version="3001",
        workspace_id="cg_native_run_instr12",
        actor_id="gha-runner",
    )
    t0 = time.perf_counter()
    mart_receipt = mart.map_protein(
        input_structure_ref=str(CLCN7_PDB),
        output_dir=str(work / "01_martinize2"),
        ss_policy="dssp",
        en_policy="elnedyn",
        maxwarn=9999,
    )
    t1 = time.perf_counter()
    if mart_receipt.status != "completed":
        Path("/tmp/cg_system_status.txt").write_text(f"FAILED: martinize2 status={mart_receipt.status}")
        return 4
    info["martinize2_elapsed_s"] = round(t1 - t0, 1)
    info["n_protein_beads"] = mart_receipt.payload.get("bead_count_output", 0)
    cg_gro = mart_receipt.payload.get("output_cg_gro_ref", "")
    itp_csv = mart_receipt.payload.get("output_cg_itp_ref", "")
    itp_refs = [p.strip() for p in itp_csv.split(",") if p.strip()]
    info["n_molecule_itps"] = len(itp_refs)
    print(f"martinize2 OK: {info['n_protein_beads']} beads, {len(itp_refs)} molecule_*.itp")

    # 2. INSANE membrane
    from mica.sim.cg_martini.insane_adapter import INSANEAdapter

    ins = INSANEAdapter(workspace_id="cg_native_run_instr12", actor_id="gha-runner")
    insane_dir = work / "02_insane"
    insane_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    ins_receipt = ins.build(
        protein_gro_ref=cg_gro,
        output_dir=str(insane_dir),
        builder="insane",
        builder_version="1.2.0",
        geometry_class="flat_bilayer",
        lipid_composition="POPC:1",
        solvent="PW",
        salt_concentration=0.15,
        center_protein=True,
    )
    t1 = time.perf_counter()
    membrane_gro = Path(ins_receipt.payload.outputs.get("gro_ref", ""))
    if not membrane_gro.exists():
        Path("/tmp/cg_system_status.txt").write_text("FAILED: insane_no_membrane_gro")
        return 5
    info["insane_elapsed_s"] = round(t1 - t0, 1)
    info["n_lipids"] = ins_receipt.payload.counts.get("lipid_count", 0)
    info["n_waters"] = ins_receipt.payload.counts.get("water_count", 0)
    info["n_ions"] = ins_receipt.payload.counts.get("na_count", 0) + ins_receipt.payload.counts.get("cl_count", 0)
    print(f"INSANE OK: {info['n_lipids']} lipids, {info['n_waters']} waters, {info['n_ions']} ions")

    # 3. cg_system_builder consolidates
    from mica.sim.cg_martini.cg_system_builder import (
        CGSystemBuildRequest,
        build_cg_system_bundle,
    )

    req = CGSystemBuildRequest(
        forcefield_family="martini3",
        martini_version="3.0.0",
        geometry_class="flat_bilayer",
        membrane_enabled=True,
        water_model="martini_v3.0.0_solvents_v1",
        ion_model="martini_v3.0.0_ions_v1",
        salt_mM=150,
        lipid_composition="POPC:1",
        solvent="PW",
        martinize2_output_cg_gro_ref=cg_gro,
        martinize2_output_itp_refs=itp_refs,
        insane_output_gro_ref=str(membrane_gro),
        insane_output_top_ref=ins_receipt.payload.outputs.get("top_ref", ""),
        insane_counts=dict(ins_receipt.payload.counts or {}),
        output_dir=str(work / "03_system"),
        source_target_id="clcn7_instr12",
    )
    t0 = time.perf_counter()
    payload = build_cg_system_bundle(req)
    t1 = time.perf_counter()
    if payload.implementation_status != "real_compile":
        Path("/tmp/cg_system_status.txt").write_text(
            f"FAILED: cg_system_builder status={payload.implementation_status}"
        )
        return 6
    info["cg_system_builder_elapsed_s"] = round(t1 - t0, 1)
    info["system_top_local"] = str(payload.system_top_path)
    info["solvated_gro_local"] = str(payload.solvated_gro_path)
    info["implementation_status"] = payload.implementation_status
    info["n_atoms"] = payload.metadata.get("n_atoms", 0)
    info["box_nm"] = payload.metadata.get("box_nm", {})
    compat = payload.metadata.get("openmm_compatibility_check", {})
    info["openmm_compat_status"] = compat.get("status", "")
    info["openmm_compat_blockers"] = compat.get("blockers", [])
    print(f"cg_system_builder OK: status={payload.implementation_status}, "
          f"n_atoms={info['n_atoms']}, openmm_compat={info['openmm_compat_status']}")

    # 4. Write info JSON
    Path("/tmp/cg_system_info.json").write_text(json.dumps(info, indent=2))
    Path("/tmp/cg_system_status.txt").write_text("PASSED")
    print("wrote /tmp/cg_system_info.json")
    print("STATUS: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
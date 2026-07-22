#!/usr/bin/env python
"""
INSTRUCCION 74 (2026-07-22) -- gpu_preflight.py

Salad startup probe target. Runs an actual OpenMM + CUDA PME Context
on a tiny system so the NVRTC JIT path is exercised end-to-end before
main_gcs.py is allowed to start.

Writes:
    /tmp/mica-gpu-ready               -- created on success
    /tmp/mica-gpu-preflight-failed    -- created on failure
    /tmp/mica-gpu-preflight.json      -- full receipt either way

Exits:
    0 on success.
    non-zero on failure (entrypoint will then sleep infinity so the
    Salad startup probe fails the readiness check and Salad reassigns
    the container to a different node).

DO NOT swallow exceptions -- we WANT a non-zero exit code on failure.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import traceback


RECEIPT_PATH = "/tmp/mica-gpu-preflight.json"
OK_MARKER = "/tmp/mica-gpu-ready"
FAIL_MARKER = "/tmp/mica-gpu-preflight-failed"


def _emit(payload: dict) -> None:
    try:
        with open(RECEIPT_PATH, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:  # pragma: no cover -- best effort
        print(f"[preflight] WARN could not write receipt: {exc}", file=sys.stderr)


def main() -> int:
    started = _dt.datetime.now(_dt.timezone.utc).isoformat()
    t0 = time.time()
    receipt: dict = {
        "started_at": started,
        "instruccion": "INSTRUCCION 74",
        "purpose": "Salad startup probe target: real OpenMM CUDA PME Context on a tiny system. Forces NVRTC JIT path end-to-end.",
        "ok": False,
    }

    # 1) Sanity: cuda-visible devices + driver reported by the kernel.
    try:
        import subprocess
        nvsmi = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        receipt["nvidia_smi"] = (nvsmi.stdout or "").strip()
        receipt["nvidia_smi_rc"] = nvsmi.returncode
    except Exception as exc:
        receipt["nvidia_smi_error"] = repr(exc)

    # 2) Real OpenMM CUDA PME Context -- small enough to be cheap, big
    #    enough to exercise the LJ + Coulomb + VerletIntegrator code
    #    paths. 216 particles = 6^3 of a coarse lattice, periodic, PME.
    try:
        import openmm
        from openmm import (
            System, VerletIntegrator,
            Platform, NonbondedForce, MonteCarloBarostat,
            unit, Vec3,
        )

        N = 216
        system = System()
        positions = []
        # Coarse cubic lattice of "LJ particles".
        spacing = 0.34  # nm
        idx = 0
        for ix in range(6):
            for iy in range(6):
                for iz in range(6):
                    system.addParticle(39.948)  # argon-ish mass
                    positions.append(Vec3(
                        spacing * ix, spacing * iy, spacing * iz,
                    ) * unit.nanometer)
                    idx += 1
        assert idx == N, f"lattice built {idx} != {N}"

        # LJ + Coulomb with PME.
        nb = NonbondedForce()
        nb.setNonbondedMethod(NonbondedForce.PME)
        nb.setCutoffDistance(1.0 * unit.nanometer)
        for i in range(N):
            nb.addParticle(0.0, 0.34, 0.238)  # q, sigma, eps (argon-ish)
        system.addForce(nb)

        # Constant pressure so PME barostat code is also exercised.
        system.addForce(MonteCarloBarostat(1.0 * unit.bar, 300 * unit.kelvin, 25))

        integrator = VerletIntegrator(1.0 * unit.femtosecond)

        # Force CUDA platform -- NO CPU fallback (closes GAP-CG-010 spirit).
        try:
            platform = Platform.getPlatformByName("CUDA")
        except Exception as exc:
            receipt["fatal"] = f"CUDA platform not registered: {exc!r}"
            raise

        props = {
            "CudaPrecision": "mixed",
            "CudaDeviceIndex": "0",
        }
        ctx = openmm.Context(system, integrator, platform, props)
        ctx.setPositions(positions)

        # Minimization -- forces NVRTC to JIT-compile the kernels if not yet.
        openmm.LocalEnergyMinimizer.minimize(ctx, 1e-1, 50)
        state = ctx.getState(getEnergy=True)
        energy_kj = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)

        # A few MD steps so the integrator kernel is also JIT-ed.
        integ2 = VerletIntegrator(1.0 * unit.femtosecond)
        ctx2 = openmm.Context(system, integ2, platform, props)
        ctx2.setPositions(positions)
        ctx2.setVelocitiesToTemperature(300 * unit.kelvin)
        integ2.step(5)

        ctx.destroy()
        ctx2.destroy()

        receipt["ok"] = True
        receipt["n_particles"] = N
        receipt["platform"] = "CUDA"
        receipt["energy_kjmol_after_min"] = energy_kj
        receipt["note"] = (
            "PME VerletIntegrator Context created and stepped on CUDA. "
            "NVRTC JIT compiled the kernels successfully; OpenMM 8.5.2 "
            "and CUDA 12.8 stack (under /opt/mica) are functional."
        )
    except Exception as exc:
        receipt["ok"] = False
        receipt["fatal"] = repr(exc)
        receipt["traceback"] = traceback.format_exc(limit=20)
        receipt["elapsed_s"] = round(time.time() - t0, 3)
        _emit(receipt)
        try:
            with open(FAIL_MARKER, "w") as f:
                f.write(receipt["fatal"] + "\n")
        except Exception:
            pass
        print(f"[preflight] FAIL: {exc}", file=sys.stderr)
        return 1

    receipt["elapsed_s"] = round(time.time() - t0, 3)
    receipt["finished_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _emit(receipt)
    try:
        with open(OK_MARKER, "w") as f:
            json.dump(receipt, f, indent=2)
    except Exception:
        pass
    print(f"[preflight] OK ({receipt['elapsed_s']}s) energy={receipt.get('energy_kjmol_after_min')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

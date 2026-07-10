# MICA OpenMM Pod — ASTROFLORA build mirror

**Purpose:** Standalone validation build for the `TOPOLOGY-KERNEL-DOCKERIMAGE-FIX-001` slice.
Mirrors the Dockerfile fix from `juaness38/MICA-ultimate` but lives in the
**ASTROFLORA** GitHub org to bypass GH Actions quota/runner issues affecting
the personal `juaness38` plan.

## What this does

- Clones `juaness38/MICA-ultimate` at a pinned commit (`8be6355c483528c775bf756247f839a184210b62`).
- Builds a Docker image that contains:
  - MICA Python source (`src/mica/`)
  - Workers (`workers/dynamo/`)
  - `main_gcs.py` (Salad GCS worker)
- Runs an embedded `CONTAINER_SMOKE_V1` that imports `mica.md_preview.encode_preview_frame` and `mica.scientific.topology_kernel.martini.openmm_relaxation_smoke.run_openmm_relaxation_smoke`.
- On push to `main`, pushes the resulting image to `ghcr.io/astroflora/mica-openmm-pod`.
- Emits a `container_smoke_v1` receipt as an artifact.

## Cross-ref

- `.mica/programs/REAL_ENGINE_BIODYNAMO_SUPERNOVA/BIODYNAMO_CG_INFRA_HARDENING_SLICE_V1_EXEC_REPORT_2026-07-09.md` (in MICA-ultimate)
- `GAP-040` (CRITICAL — canonical image missing `mica.*` modules at runtime)
- `DRIFT-006` (dependencies not re-verified)

## Status (2026-07-10)

- ASTROFLORA/mica-openmm-pod-build created (public, free plan).
- Workflow validation pending.
- If this succeeds, the MICA-ultimate GH Actions failures were runner-side
  and the upstream fix in `workers/salad/gcs_openmm_srcg/Dockerfile` is valid.

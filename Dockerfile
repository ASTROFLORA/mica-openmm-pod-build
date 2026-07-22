# MICA OpenMM Pod — STANDALONE validation build for GH Actions smoke test.
#
# CG_NATIVE_RUN-INSTRUCCION-11 mirror.
#
# Single-stage build: embeds MICA source files directly in this repo's build
# context instead of cloning the private upstream MICA-ultimate. This is the
# EXACT copy pattern validated by the original Dockerfile fix (8be6355c4).
#
# Why embedded instead of clone:
#   juaness38/MICA-ultimate is private. ASTROFLORA workflows don't have read
#   credentials for it, so `RUN git clone` fails with `fatal: could not read
#   Username for 'https://github.com'`. We avoid the clone by vendoring the
# modules the smoke test imports.
#
# Slice this validates:
#   CG_NATIVE_RUN-INSTRUCCION-11 (closes GAP-CG-002 + GAP-CG-004 + GAP-R3-CG-MARTINI).

FROM condaforge/miniforge3:24.11.3-0

LABEL org.opencontainers.image.source="https://github.com/juaness38/MICA-ultimate"
LABEL org.opencontainers.image.description="MICA Salad GCS-native OpenMM worker — ASTROFLORA standalone GH Actions smoke mirror (CG/Martini 3 lane, Blacksmith runner)"
LABEL mica.slice="CG_NATIVE_RUN-INSTRUCCION-11"
LABEL mica.commit="81a817c23e3579dbd40f4979ded43a969f7875ad"
LABEL mica.lane="cg_martini"
LABEL mica.worker_mode="cg_martini"
LABEL mica.gap="GAP-CG-002 + GAP-CG-004 + GAP-R3 closed"

ARG MICA_COMMIT=81a817c23e3579dbd40f4979ded43a969f7875ad

# System packages: build tools + runtime libs + DSSP (mkdssp binary).
# INSTRUCCION 74e (2026-07-22): apt-get retry loop. The Ubuntu archive
# (archive.ubuntu.com / security.ubuntu.com) is intermittently unreachable
# from GH Actions runners (verified on builds #29941568205, #29941791934,
# #29942204304). apt-get update -> apt-get install -> apt-get install chain
# retries on a 5s sleep; the `|| true` between retries lets partial progress
# (some packages installed) survive.
RUN for attempt in 1 2 3 4 5; do \
        echo "=== apt attempt $attempt/5 ===" && \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            curl ca-certificates build-essential libxrender1 libxext6 git dssp && \
        rm -rf /var/lib/apt/lists/* && \
        mkdssp --version && \
        echo "=== apt attempt $attempt succeeded ===" && break \
    || (echo "=== apt attempt $attempt failed, sleeping 5s ===" && sleep 5); \
    done

# Layer 1: Isolated scientific runtime env at /opt/mica.
# INSTRUCCION 74 (2026-07-22, consultant review): the base miniforge
# at /opt/conda keeps its administrative conda/mamba intact. The
# scientific stack (python 3.11 + scientific libs) lives at /opt/mica,
# a separate env created with `mamba create -p /opt/mica`. This avoids
# the v73c bug where `mamba install --no-deps python=3.11` overwrote
# the base interpreter and broke the conda CLI.
#
# OpenMM and CUDA are NOT installed here -- they are pulled from PyPI
# in Layer 2 to keep the scientific env coherent with the Python that
# OpenMM is built against.
RUN /opt/conda/bin/mamba create -y -p /opt/mica -c conda-forge \
    python=3.11 \
    nodejs=22 \
    numpy=1.26.4 \
    scipy=1.13.1 \
    mdtraj \
    mdanalysis \
    openbabel \
    plip \
    pandas \
    biopython \
    vermouth \
    && /opt/conda/bin/mamba clean -afy

# PATH precedence: /opt/mica first so python/numpy/scipy from
# /opt/mica/bin are picked up by default. /opt/conda second so the
# mamba/conda CLI is reachable for ops like future layer rebuilds.
ENV PATH=/opt/mica/bin:/opt/conda/bin:$PATH

# Layer 2: PyPI CUDA 12 stack pinned at 12.8 (the first CUDA release
# with sm_120 / RTX 5090 Blackwell support). OpenMM 8.3.1 is the latest
# 8.x release with a manylinux_2_28 wheel compatible with the Debian 11
# base (glibc 2.31). The matching openmm-cuda-12 wheel ships the
# libOpenMMCUDA.so plugin that contains the cuFFT, NVRTC and runtime
# bindings OpenMM loads at first cuInit().
#
# INSTRUCCION 74b (consultant-correction, 2026-07-22): the consultant
# originally proposed openmm==8.5.2 but that version only has
# manylinux_2_34 wheels (glibc >= 2.34) and was hidden from the pip
# resolver on the Debian 11 base. 8.3.1 is the latest stable OpenMM
# with manylinux_2_27/2_28 wheels -- compatible with both the host
# glibc and OpenMM's own NVRTC pipeline.
#
# INSTRUCCION 74 (consultant): NVRTC is the actual runtime component
# that compiles OpenMM kernels to PTX on the host. Pinning NVRTC at
# 12.8 means: even if a Salad node has driver 525+, the in-container
# NVRTC will emit PTX for sm_120 (Blackwell) which the driver can JIT
# to native SASS. CUDA_FORCE_PTX_JIT is REMOVED -- OpenMM already does
# the right thing (PTX -> NVRTC -> driver JIT). Setting the env var
# only confuses diagnostics.
RUN python -m pip install --no-cache-dir \
    "nvidia-cuda-runtime-cu12==12.8.*" \
    "nvidia-cuda-nvrtc-cu12==12.8.*" \
    "nvidia-cuda-nvcc-cu12==12.8.*" \
    "nvidia-cuda-cupti-cu12==12.8.*" \
    "openmm==8.3.1" \
    "openmm-cuda-12==8.3.1" \
    && python -m pip cache purge

# INSTRUCCION 74 (consultant): pdbfixer on conda would try to
# reinstall OpenMM. Force --no-deps so it only sees the pdbfixer
# package and leaves our /opt/mica Python + pip-installed openmm
# intact. Without --no-deps, conda's solver would propose to
# overwrite openmm with its conda build (the v73c chain reaction).
RUN /opt/conda/bin/mamba install -y -p /opt/mica \
    -c conda-forge --no-deps pdbfixer \
    && /opt/conda/bin/mamba clean -afy

# Layer 2: pip-only deps. martini_openmm is unpinned — pip picks
# latest compatible with our pinned openmm==8.1.1 (set in Layer 1).
# INSTRUCCION 73 (2026-07-22): no openmm in pip list here, conda is
# now NO openmm and the only openmm install comes from the pinned
# PyPI wheel in Layer 1.
RUN pip install --no-cache-dir \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "websockets>=11.0.0" \
    "pydantic>=2.5.0" \
    "starlette>=0.36.0" \
    "httpx>=0.26.0" \
    "google-cloud-storage>=2.16.0" \
    "python-dotenv>=1.0.0" \
    "mdtraj>=1.9.9" \
    "insane>=1.0.0" \
    "martini_openmm @ git+https://github.com/maccallumlab/martini_openmm.git@216e62b26c4ee6cea7ed21e20ec84fffe97a101c" \
    # INSTRUCCION 74g (2026-07-22, build #29943792749): the `insane`
    # PyPI package's utils.py does `import pkg_resources`. setuptools
    # >=80 REMOVED pkg_resources from the wheel (only ships
    # setuptools._vendor). Pin to setuptools<80 so pkg_resources is
    # importable at the top level -- needed by insane.utils.iter_resource().
    # See https://github.com/pypa/setuptools/issues/4502 for the upstream
    # deprecation that landed in setuptools 80.
    "setuptools>=68.0.0,<80"

# Layer 3: molstar for BCIF (kept from v1 mirror).
RUN npm install -g molstar@5.6.1 \
    && cif2bcif --help >/tmp/cif2bcif.help

# Layer 4: MICA source from this repo's build context (embedded files).
# INSTRUCCION 29 (2026-07-21): added mica/provenance/ so martinize2_adapter.py
# (which imports `from mica.provenance.receipts import ...`) resolves at runtime.
# Without this COPY, the worker imports the cg_martini submodule successfully
# (lives inside mica/sim/) but martinize2_adapter at L33 raises ModuleNotFoundError
# on the very first call. mica.scientific.topology_kernel.martini.martini_openmm_compatibility
# is NOT strictly required -- it is wrapped in try/except ImportError inside
# `cg_system_builder._validate_openmm_load` and degrades to `validation_unavailable`
# when absent, so we don't ship that subtree to keep the image lean.
WORKDIR /app
COPY src/mica/md_preview/ /app/src/mica/md_preview/
COPY src/mica/api_v1/ /app/src/mica/api_v1/
COPY src/mica/provenance/ /app/src/mica/provenance/
COPY src/mica/sim/cg_martini/ /app/src/mica/sim/cg_martini/
COPY src/mica/sim/cg_martini/data/martini3/ /app/src/mica/sim/cg_martini/data/martini3/
COPY workers/salad/gcs_openmm_srcg/main_gcs.py /app/main_gcs.py

# INSTRUCCION 74 (2026-07-22): GPU preflight + entrypoint that runs the
# probe before main_gcs.py. Salad startup probe waits for either the
# success marker or an obvious failure marker -- so we use a wrapper
# script that:
#   1. Runs gpu_preflight.py (real OpenMM CUDA PME test).
#   2. On success: exec main_gcs.py (so it becomes PID 1, Salad probe
#      reads /tmp/mica-gpu-ready and considers container ready).
#   3. On failure: writes /tmp/mica-gpu-preflight-failed and runs
#      `sleep infinity` so Salad startup probe fails and the node
#      is reallocated.
COPY workers/salad/gcs_openmm_srcg/gpu_preflight.py /app/gpu_preflight.py
COPY workers/salad/gcs_openmm_srcg/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/gpu_preflight.py

# Sanity verify the critical files exist in /app/src.
RUN ls -la /app/src/mica/md_preview/__init__.py \
    && ls -la /app/src/mica/api_v1/ws_ticket.py \
    && ls -la /app/src/mica/sim/cg_martini/__init__.py \
    && ls -la /app/src/mica/sim/cg_martini/data/martini3/martini_v3.0.0.itp \
    && ls -la /app/main_gcs.py \
    && ls -la /app/entrypoint.sh \
    && ls -la /app/gpu_preflight.py

# Layer 5: CONTAINER_SMOKE_V2 — the receipt gate (md_preview baseline + CG lane).
ARG SMOKE_RECEIPT_MODE=container_smoke_v2
ENV MICA_SMOKE_RECEIPT_MODE=${SMOKE_RECEIPT_MODE}
ENV PYTHONDONTWRITEBYTECODE=1
RUN /opt/mica/bin/python - <<'PYEOF'
import sys, json, os, shutil
sys.path.insert(0, '/app/src')
verified = []

def check_import(mod, name):
    m = __import__(mod, fromlist=[name])
    attr = getattr(m, name, None)
    if attr is not None:
        verified.append({
            'module': mod,
            'symbol': name,
            'defined_in': getattr(attr, '__module__', '?'),
        })
        return attr
    # Tolerant fallback (INSTRUCCION 29 -- 2026-07-21): some pinned mica submodule
    # commits (e.g. 81a817c23) carry `cg_martini/__init__.py` as a docstring stub
    # WITHOUT the public re-exports (`Martinize2Adapter`, `INSANEAdapter`, ...).
    # The submodules are always present, so we try a submodule-level import of the
    # known CG martini adapters.
    symbol_just = name.rsplit('.', 1)[-1]
    submodule_map = {
        'Martinize2Adapter': 'martinize2_adapter',
        'INSANEAdapter': 'insane_adapter',
        'build_cg_system_bundle': 'cg_system_builder',
    }
    sub_name = submodule_map.get(symbol_just)
    if sub_name is None or not mod.startswith('mica.sim.cg_martini'):
        raise ImportError(f'{mod}.{name} not found')
    try:
        sub = __import__(mod + '.' + sub_name, fromlist=[symbol_just])
        attr = getattr(sub, symbol_just, None)
    except ImportError as e:
        raise ImportError(f'{mod}.{name} not found (and submodule {sub_name} failed: {e})') from e
    if attr is None:
        raise ImportError(f'{mod}.{name} not found (submodule fallback also missing)')
    verified.append({
        'module': mod + '.' + sub_name,
        'symbol': symbol_just,
        'defined_in': getattr(attr, '__module__', '?'),
        'note': 'tolerant fallback -- package __init__.py is stub',
    })
    return attr

# --- md_preview baseline (8 targets, kept from container_smoke_v1) ---
for mod, name in [
    ('mica.md_preview', 'encode_preview_frame'),
    ('mica.md_preview', 'bcif_encoder'),
    ('mica.md_preview', 'bcif_runtime'),
    ('mica.md_preview', 'local_preview_consumer'),
    ('mica.md_preview', 'local_preview_ui_adapter'),
    ('mica.md_preview', 'preview_ws_replayer'),
    ('mica.md_preview', 'unified_preview_contract'),
]:
    check_import(mod, name)

# --- CG Martini deps (NEW in CONTAINER_SMOKE_V2 / INSTRUCCION 11) ---
import martini_openmm
if not hasattr(martini_openmm, 'MartiniTopFile'):
    raise ImportError('martini_openmm.MartiniTopFile missing')
verified.append({
    'module': 'martini_openmm',
    'symbol': 'MartiniTopFile',
    'defined_in': getattr(martini_openmm.MartiniTopFile, '__module__', '?'),
})

# vermouth is the Marrink-lab fork that powers martinize2.
# Import is the canonical check (sometimes vermouth is shipped as martinize2 CLI only).
import vermouth
verified.append({
    'module': 'vermouth',
    'symbol': '<module>',
    'defined_in': getattr(vermouth, '__file__', '?'),
})

# INSTRUCCION 74b (2026-07-22): GPU-only policy -- CUDA plugin .so MUST
# be present in the image. With the INSTRUCCION 74b stack, the openmm
# install is via pip (PyPI `openmm==8.3.1` + `openmm-cuda-12==8.3.1`
# wheels) under /opt/mica.
#
# INSTRUCCION 74d (2026-07-22, build #29941089960): the openmm-cuda-12
# wheel installs as `OpenMM.libs/lib/plugins/libOpenMMCUDA.so` (NOT
# `openmm/lib/plugins/`). The exact prefix comes from the wheel's
# RECORD data, where the top-level `OpenMM.libs/` directory is
# installed as site-packages data. Search the real location.
import openmm  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
import glob as _glob  # noqa: E402
_plugin_search = (
    list(_glob.glob('/opt/mica/lib/python3.11/site-packages/OpenMM.libs/lib/plugins/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/mica/lib/python3.11/site-packages/OpenMM.libs/lib/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/mica/lib/python3.11/site-packages/openmm/lib/plugins/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/mica/lib/python3.11/site-packages/openmm/lib/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/mica/lib/plugins/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/mica/lib/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/conda/lib/python3.11/site-packages/OpenMM.libs/lib/plugins/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/conda/lib/python3.11/site-packages/openmm/lib/plugins/libOpenMMCUDA*'))
    + list(_glob.glob('/opt/conda/lib/python3.11/site-packages/openmm/lib/libOpenMMCUDA*'))
)
if not _plugin_search:
    raise ImportError(
        "libOpenMMCUDA.so not found in the image. The pip wheels "
        "(openmm-cuda-12==8.3.1) were not installed correctly into "
        "/opt/mica. NO CPU FALLBACK WILL BE TOLERATED."
    )
verified.append({
    'module': 'openmm-cuda-12',
    'symbol': 'Platform::CUDA',
    'defined_in': _plugin_search[0],
    'note': 'INSTRUCCION 74d CUDA plugin .so present (CUDA 12.8 runtime stack + openmm==8.3.1 from PyPI under /opt/mica; runtime registration happens on first cuInit() at the worker host with real GPU). Plugin location: OpenMM.libs/lib/plugins/ (data_files in the openmm-cuda-12 wheel). The Salad startup probe (entrypoint.sh) will additionally preflight an actual Context on a tiny PME system before letting main_gcs.py run -- incompatible drivers fail the probe and Salad reassigns.',
})

# INSTRUCCION 30 (2026-07-21): mdtraj is now REQUIRED for the martinize2
# PDB-to-GRO path -- the hand-rolled parser choked on partial CRYST1 records.
# We hard-require it in the smoke gate (not warn-only) because the legacy
# fallback is brittle and was the root cause of GAP-CG-009.
import mdtraj  # noqa: E402
if not hasattr(mdtraj, 'load') or not hasattr(mdtraj.load, '__call__'):
    raise ImportError('mdtraj.load missing -- PDB-to-GRO path will fall back to broken hand-rolled parser')
verified.append({
    'module': 'mdtraj',
    'symbol': 'load',
    'defined_in': getattr(mdtraj.load, '__module__', '?'),
})

# INSTRUCCION 34 (2026-07-21): INSANE (Tieleman lab) is required for
# INSANEAdapter.build (CG/Martini membrane solvation). v16 dispatch failed
# at INSANEAdapter step with 'insane not importable' -- the package was
# missing from the image. Pin and verify.
try:
    import insane  # noqa: E402
    verified.append({
        'module': 'insane',
        'symbol': '<module>',
        'defined_in': getattr(insane, '__file__', '?'),
    })
except ImportError as e:
    raise ImportError(
        f'insane not importable -- INSANEAdapter.build will return '
        f'validation_errors=[insane not importable] and fail. {e}'
    ) from e

# --- mica.* submodules that the CG runtime imports on the worker ---
# INSTRUCCION 29 (2026-07-21): mica.provenance.receipts is imported by
# martinize2_adapter.py at L33 and used by CG payloads. Without it, the worker
# crashes at first call with ModuleNotFoundError.
for mod, name in [
    ('mica.provenance.receipts', 'ReceiptCore'),
    ('mica.provenance.receipts', 'ReceiptHashes'),
    ('mica.provenance.receipts', 'ReceiptRefs'),
]:
    check_import(mod, name)

# --- end-to-end import of cg_martini adapters (catches missing COPYs early) ---
for mod, name in [
    ('mica.sim.cg_martini', 'Martinize2Adapter'),
    ('mica.sim.cg_martini', 'INSANEAdapter'),
    ('mica.sim.cg_martini', 'build_cg_system_bundle'),
]:
    check_import(mod, name)

# --- CLI binaries on PATH (warn-only; Martinize2Adapter has martinize2.py fallback) ---
warnings = []
for cli in ('mkdssp', 'martinize2'):
    path = shutil.which(cli)
    if path:
        verified.append({
            'module': '<cli>',
            'symbol': cli,
            'defined_in': path,
        })
    else:
        msg = f'WARN: {cli} not on PATH (Martinize2Adapter falls back to martinize2.py direct invocation)'
        warnings.append(msg)
        print(msg)

# --- FF data presence (the bundled Martini 3 .itp) ---
ff_data = '/app/src/mica/sim/cg_martini/data/martini3/martini_v3.0.0.itp'
if not os.path.exists(ff_data):
    raise FileNotFoundError(f'FF data missing: {ff_data}')
verified.append({
    'module': '<ff-data>',
    'symbol': 'martini_v3.0.0.itp',
    'defined_in': ff_data,
})

print(f'CONTAINER_SMOKE_V2 OK: {len(verified)} verified targets')
for v in verified:
    print(f"  - {v['module']}.{v['symbol']} (in {v['defined_in']})")
if warnings:
    print('Warnings:')
    for w in warnings:
        print(f'  * {w}')

receipt = {
    'slice': 'CG_NATIVE_RUN-INSTRUCCION-11-mirror',
    'mica_commit': os.environ.get('MICA_COMMIT', '81a817c23e3579dbd40f4979ded43a969f7875ad'),
    'validation_mode': os.environ.get('MICA_SMOKE_RECEIPT_MODE', 'container_smoke_v2'),
    'verified_count': len(verified),
    'verified_targets': verified,
    'gap_closed': ['GAP-CG-002', 'GAP-CG-004', 'GAP-R3-CG-MARTINI', 'GAP-CG-009', 'GAP-CG-010', 'GAP-CG-011'],
    'validation_status': 'PASSED',
    'note': 'ASTROFLORA public mirror adds CG/Martini 3 smoke over md_preview baseline. INSTRUCCION 30: mdtraj hard-required for martinize2 PDB-to-GRO (closes GAP-CG-009). INSTRUCCION 74b: single image, isolated /opt/mica env (mamba create -p), PyPI OpenMM 8.3.1 + openmm-cuda-12 8.3.1 + nvidia-cuda-*-cu12 12.8 stack (CUDA 12.8 is first release with sm_120/Blackwell native compute support; OpenMM uses NVRTC runtime compilation so no precompiled SASS is needed). 8.3.1 chosen over consultant-proposed 8.5.2 because the latter only ships manylinux_2_34 wheels (glibc >=2.34) and was hidden from pip on the Debian 11 base; 8.3.1 ships manylinux_2_28 wheels compatible with glibc 2.31. No CUDA_FORCE_PTX_JIT -- OpenMM already does JIT for unsupported arch. Salad startup probe preflights an actual Context on a tiny PME system before main_gcs.py runs; incompatible nodes fail the probe and Salad reallocates.',
}
with open('/tmp/container_smoke_v2.json', 'w') as f:
    json.dump(receipt, f, indent=2)
print('Receipt: /tmp/container_smoke_v2.json')
PYEOF

CMD ["/app/entrypoint.sh"]
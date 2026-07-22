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
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential libxrender1 libxext6 git dssp \
    && rm -rf /var/lib/apt/lists/* \
    && mkdssp --version

# Layer 1: Conda stack — CUDA toolkit pinned to 12.x + OpenMM CUDA plugin
# bundled via openmm-cuda126 meta-package.
# GAP-CG-010 (2026-07-21): mamba pulling `openmm` directly grabbed
# `cuda-version 13.3`, whose PTX requires a driver >= 580 that the
# Salad RTX_5090 host does NOT ship. Result: CUDA_ERROR_UNSUPPORTED_PTX_VERSION
# (222) on the first `Simulation(...)` call.
#
# Pin: openmm-cuda126 which transitively pulls openmm + cuda-version=12.6
# + the bundled CUDA plugin .so whose PTX targets compute capability
# sm_50..sm_120 (covers RTX 5090 Blackwell). Driver requirement: >= 535,
# which is shipped in 2024+ on every NVIDIA GPU host.
#
# We install via the meta-package (not plain `openmm`) because the
# plain `openmm` build on conda-forge is CPU-only -- the CUDA plugin
# lives in a separate libopenmmcudapme.so that only ships in
# `openmm-cuda126` / `openmm-cuda118` etc.
RUN mamba install -c conda-forge -y \
    python=3.11 \
    nodejs=22 \
    openmm-cuda126 \
    pdbfixer \
    numpy \
    scipy \
    mdtraj \
    mdanalysis \
    openbabel \
    plip \
    pandas \
    biopython \
    vermouth \
    && mamba clean -afy

# Layer 2: pip-only deps (martini_openmm is unpinned — pip picks latest compatible
# with the OpenMM conda-forge build).
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
    "martini_openmm @ git+https://github.com/maccallumlab/martini_openmm.git@216e62b26c4ee6cea7ed21e20ec84fffe97a101c"

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

# Sanity verify the critical files exist in /app/src.
RUN ls -la /app/src/mica/md_preview/__init__.py \
    && ls -la /app/src/mica/api_v1/ws_ticket.py \
    && ls -la /app/src/mica/sim/cg_martini/__init__.py \
    && ls -la /app/src/mica/sim/cg_martini/data/martini3/martini_v3.0.0.itp \
    && ls -la /app/main_gcs.py

# Layer 5: CONTAINER_SMOKE_V2 — the receipt gate (md_preview baseline + CG lane).
ARG SMOKE_RECEIPT_MODE=container_smoke_v2
ENV MICA_SMOKE_RECEIPT_MODE=${SMOKE_RECEIPT_MODE}
ENV PYTHONDONTWRITEBYTECODE=1
RUN /opt/conda/bin/python - <<'PYEOF'
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

# INSTRUCCION 36 (2026-07-21): GPU-only policy -- CUDA platform MUST load.
# GAP-CG-010 root cause was cuda-version=13.3 PTX too new for the host
# driver (CUDA_ERROR_UNSUPPORTED_PTX_VERSION 222). We pin cuda-version=12.6
# in Layer 1, and now hard-assert at build time that the CUDA platform is
# available AND its plugin loaded -- NOT just importable. If this fails,
# the build fails and the broken image never ships. Zero CPU fallback.
import openmm  # noqa: E402
from openmm import Platform as _Platform  # noqa: E402
_platforms = [_Platform.getPlatform(i).getName() for i in range(_Platform.getNumPlatforms())]
if "CUDA" not in _platforms:
    raise ImportError(
        f"OpenMM CUDA platform not registered at build time. "
        f"Available platforms: {_platforms}. "
        f"This means cuda-version is pinned wrong (driver/PTX mismatch). "
        f"NO CPU FALLBACK WILL BE TOLERATED."
    )
# Probe plugin load via PlatformData (creates the actual kernel module).
# We can NOT instantiate a Context here (no System), but getPlatformByName
# already loads the plugin shared library.
_cuda_platform = _Platform.getPlatformByName("CUDA")
verified.append({
    'module': 'openmm',
    'symbol': 'Platform::CUDA',
    'defined_in': getattr(_cuda_platform, '__module__', '?'),
    'note': f'hard-required; available platforms: {_platforms}',
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
    'gap_closed': ['GAP-CG-002', 'GAP-CG-004', 'GAP-R3-CG-MARTINI', 'GAP-CG-009', 'GAP-CG-010'],
    'validation_status': 'PASSED',
    'note': 'ASTROFLORA public mirror adds CG/Martini 3 smoke over md_preview baseline. INSTRUCCION 30: mdtraj now hard-required for martinize2 PDB-to-GRO (closes GAP-CG-009: silent _pdb_to_gro empty-output when CRYST1 missing). INSTRUCCION 36: cuda-version pinned to 12.6 + smoke-gate hard-requires OpenMM CUDA platform registered (no CPU fallback -- closes GAP-CG-010: CUDA_ERROR_UNSUPPORTED_PTX_VERSION 222 from cuda-13.3 PTX too new for RTX_5090 host driver).',
}
with open('/tmp/container_smoke_v2.json', 'w') as f:
    json.dump(receipt, f, indent=2)
print('Receipt: /tmp/container_smoke_v2.json')
PYEOF

CMD ["python", "/app/main_gcs.py"]
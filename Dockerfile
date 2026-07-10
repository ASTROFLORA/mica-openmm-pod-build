# MICA OpenMM Pod — STANDALONE validation build for GH Actions smoke test.
#
# Single-stage build: embeds MICA source files directly in this repo's build
# context instead of cloning the private upstream MICA-ultimate. This is the
# EXACT copy pattern validated by the original Dockerfile fix (8be6355c4).
#
# Why embedded instead of clone:
#   juaness38/MICA-ultimate is private. ASTROFLORA workflows don't have read
#   credentials for it, so `RUN git clone` fails with `fatal: could not read
#   Username for 'https://github.com'`. We avoid the clone by vendoring the
#   modules the smoke test imports.
#
# Slice this validates:
#   TOPOLOGY-KERNEL-DOCKERIMAGE-FIX-001  (closes GAP-040, addresses DRIFT-006)

FROM condaforge/miniforge3:latest

LABEL org.opencontainers.image.source="https://github.com/juaness38/MICA-ultimate"
LABEL org.opencontainers.image.description="MICA Salad GCS-native OpenMM worker — ASTROFLORA standalone GH Actions smoke mirror (single-stage, embedded MICA source)"
LABEL mica.slice="TOPOLOGY-KERNEL-DOCKERIMAGE-FIX-001-validation"
LABEL mica.commit="8be6355c483528c775bf756247f839a184210b62"
LABEL mica.gap="GAP-040 + DRIFT-006"

ARG MICA_COMMIT=8be6355c483528c775bf756247f839a184210b62

# System packages needed for OpenMM/rdkit/molstar runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential libxrender1 libxext6 git \
    && rm -rf /var/lib/apt/lists/*

# Layer 1: OpenMM stack
RUN mamba install -c conda-forge -y \
    python=3.11 \
    nodejs=22 \
    openmm \
    pdbfixer \
    numpy \
    scipy \
    && mamba clean -afy

# Layer 2: Cheminformatics
RUN mamba install -c conda-forge -y \
    mdtraj \
    mdanalysis \
    "rdkit>=2024" \
    openbabel \
    plip \
    pandas \
    biopython \
    && mamba clean -afy

# Layer 3: pip-only deps (matched to mica.md_preview import chain)
RUN pip install --no-cache-dir \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "websockets>=11.0.0" \
    "pydantic>=2.5.0" \
    "starlette>=0.36.0" \
    "httpx>=0.26.0" \
    "google-cloud-storage>=2.16.0" \
    "python-dotenv>=1.0.0"

# Layer 4: molstar for BCIF
RUN npm install -g molstar@5.6.1 \
    && cif2bcif --help >/tmp/cif2bcif.help

# Layer 5: copy MICA source from this repo's build context (the embedded files).
# This is the EXACT pattern the original Dockerfile fix uses:
#   COPY src/ /app/src/
#   COPY workers/.../main_gcs.py /app/main_gcs.py
WORKDIR /app
COPY src/ /app/src/
COPY workers/salad/gcs_openmm_srcg/main_gcs.py /app/main_gcs.py

# Verify the minimal critical files exist in /app/src.
RUN ls -la /app/src/mica/md_preview/__init__.py \
    && ls -la /app/src/mica/api_v1/ws_ticket.py \
    && ls -la /app/main_gcs.py

# Layer 6: CONTAINER_SMOKE_V1 — the receipt gate (minimal).
# This is the STABLE-SUBSET mirror validation. We only ship the MICA modules
# that main_gcs.py loads at runtime (md_preview + ws_ticket). Wider chains
# (mica_q.protocol_jsonld_contract, scientific TK, physiology TK) require
# runtime contracts (registry, secrets, deployments) that are out of scope
# here — full coverage lives in MICA-ultimate@8be6355c4 with local_smoke_v1.
ARG SMOKE_RECEIPT_MODE=container_smoke_v1
ENV MICA_SMOKE_RECEIPT_MODE=${SMOKE_RECEIPT_MODE}
ENV PYTHONDONTWRITEBYTECODE=1
RUN /opt/conda/bin/python - <<'PYEOF'
import sys, json, os
sys.path.insert(0, '/app/src')
verified = []
for mod, name in [
    ('mica.md_preview', 'encode_preview_frame'),
    ('mica.md_preview', 'bcif_encoder'),
    ('mica.md_preview', 'bcif_runtime'),
    ('mica.md_preview', 'local_preview_consumer'),
    ('mica.md_preview', 'local_preview_ui_adapter'),
    ('mica.md_preview', 'preview_ws_replayer'),
    ('mica.md_preview', 'unified_preview_contract'),
]:
    try:
        m = __import__(mod, fromlist=[name])
        attr = getattr(m, name, None)
        if attr is None:
            raise ImportError(f'{mod}.{name} not found')
        verified.append((mod, name, getattr(attr, '__module__', '?')))
    except Exception as e:
        print(f'FAIL: {mod}.{name}: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(1)
print(f'CONTAINER_SMOKE_V1 OK: {len(verified)} import targets validated from /app/src')
for mod, name, src in verified:
    print(f'  - {mod}.{name} (defined in {src})')
receipt = {
    'slice': 'TOPOLOGY-KERNEL-DOCKERIMAGE-FIX-001-validation',
    'mica_commit': os.environ.get('MICA_COMMIT', '8be6355c483528c775bf756247f839a184210b62'),
    'validation_mode': os.environ.get('MICA_SMOKE_RECEIPT_MODE', 'container_smoke_v1'),
    'verified_count': len(verified),
    'verified_targets': [{'module': m, 'symbol': n, 'defined_in': s} for m, n, s in verified],
    'gap_closed': ['GAP-040', 'DRIFT-006'],
    'validation_status': 'PASSED',
    'note': 'ASTROFLORA public mirror is the minimal md_preview+ws_ticket subset. Wider chains validated in MICA-ultimate@8be6355c4 (local_smoke_v1).',
}
with open('/tmp/container_smoke_v1.json', 'w') as f:
    json.dump(receipt, f, indent=2)
print('Receipt: /tmp/container_smoke_v1.json')
PYEOF

CMD ["python", "/app/main_gcs.py"]

# filepath: C:\tmp\mica-astroflora-build\Dockerfile
# MICA OpenMM Pod — STANDALONE validation build for GH Actions smoke test.
#
# This Dockerfile is a STANDALONE variant for the ASTROFLORA/mica-openmm-pod-build
# repo. It does NOT depend on the MICA-ultimate source tree being in the build
# context. Instead, it downloads the code from the upstream repo at a pinned
# commit via `git clone` inside the Dockerfile.
#
# PURPOSE: This is a SMOKE TEST to determine if GitHub Actions on the ASTROFLORA
# org can build a MICA-compatible image at all. If this succeeds, the original
# `startup_failure` failures in MICA-ultimate were almost certainly caused by
# the personal `juaness38` plan having GH Actions quota/runner issues.
#
# Cross-ref: BIODYNAMO_CG_INFRA_HARDENING_SLICE_V1_EXEC_REPORT_2026-07-09.md

FROM condaforge/miniforge3:latest

LABEL org.opencontainers.image.source="https://github.com/juaness38/MICA-ultimate"
LABEL org.opencontainers.image.description="MICA Salad GCS-native OpenMM worker — standalone ASTROFLORA mirror for GH Actions smoke test"
LABEL mica.slice="TOPOLOGY-KERNEL-DOCKERIMAGE-FIX-001-validation"

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

# Layer 3: Python pip-only deps
RUN pip install --no-cache-dir \
    "google-cloud-storage>=2.16.0" \
    "python-dotenv>=1.0.0"

# Layer 4: molstar for BCIF
RUN npm install -g molstar@5.6.1 \
    && cif2bcif --help >/tmp/cif2bcif.help

# Layer 5: MICA source code (downloaded at build time)
# The `--depth 1` keeps the build context small. We pin to a specific commit
# to ensure reproducible builds.
ARG MICA_COMMIT=8be6355c483528c775bf756247f839a184210b62
WORKDIR /opt
RUN git clone --depth 1 https://github.com/juaness38/MICA-ultimate.git mica-source && \
    cd mica-source && \
    git fetch --depth 1 origin ${MICA_COMMIT} && \
    git checkout ${MICA_COMMIT} && \
    rm -rf .git

# Copy MICA source into the runtime image
WORKDIR /app
RUN mkdir -p /app/src /app/workers
COPY --from=0 /opt/mica-source/src/mica /app/src/mica
COPY --from=0 /opt/mica-source/workers/dynamo /app/workers/dynamo
COPY --from=0 /opt/mica-source/workers/salad/gcs_openmm_srcg/main_gcs.py /app/main_gcs.py

# Container smoke verification (Layer 6)
RUN python -c "import sys; sys.path.insert(0, '/app/src'); \
    from mica.md_preview import encode_preview_frame; \
    from mica.scientific.topology_kernel.martini.openmm_relaxation_smoke import run_openmm_relaxation_smoke; \
    print('CONTAINER_SMOKE_V1 OK: md_preview + topology_kernel.martini.openmm_relaxation_smoke importable')"

CMD ["python", "/app/main_gcs.py"]

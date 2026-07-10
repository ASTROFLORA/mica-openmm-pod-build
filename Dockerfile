# MICA OpenMM Pod — STANDALONE validation build for GH Actions smoke test.
#
# Multi-stage build:
#   builder: clones MICA-ultimate at pinned commit, prepares /opt/mica-source
#   runtime: installs all deps, copies from builder, runs container smoke

FROM condaforge/miniforge3:latest AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential libxrender1 libxext6 git \
    && rm -rf /var/lib/apt/lists/*

ARG MICA_COMMIT=8be6355c483528c775bf756247f839a184210b62
WORKDIR /opt
RUN git clone --depth 1 https://github.com/juaness38/MICA-ultimate.git mica-source && \
    cd mica-source && \
    git fetch --depth 1 origin ${MICA_COMMIT} && \
    git checkout ${MICA_COMMIT} && \
    rm -rf .git && \
    echo "Builder stage ready: /opt/mica-source at ${MICA_COMMIT}" && \
    ls -la /opt/mica-source/src/mica/md_preview/__init__.py && \
    ls -la /opt/mica-source/src/mica/scientific/topology_kernel/martini/openmm_relaxation_smoke.py

# ============================================================================
# Runtime stage
# ============================================================================
FROM condaforge/miniforge3:latest

LABEL org.opencontainers.image.source="https://github.com/juaness38/MICA-ultimate"
LABEL org.opencontainers.image.description="MICA Salad GCS-native OpenMM worker — standalone ASTROFLORA mirror for GH Actions smoke test"
LABEL mica.slice="TOPOLOGY-KERNEL-DOCKERIMAGE-FIX-001-validation"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential libxrender1 libxext6 \
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

# Layer 3: pip-only deps
RUN pip install --no-cache-dir \
    "google-cloud-storage>=2.16.0" \
    "python-dotenv>=1.0.0"

# Layer 4: molstar for BCIF
RUN npm install -g molstar@5.6.1 \
    && cif2bcif --help >/tmp/cif2bcif.help

# Layer 5: Copy MICA source from builder stage
WORKDIR /app
COPY --from=builder /opt/mica-source/src/mica /app/src/mica
COPY --from=builder /opt/mica-source/workers/dynamo /app/workers/dynamo
COPY --from=builder /opt/mica-source/workers/salad/gcs_openmm_srcg/main_gcs.py /app/main_gcs.py

# Layer 6: Container smoke verification (this is the receipt gate)
RUN python -c "import sys; sys.path.insert(0, '/app/src'); \
    from mica.md_preview import encode_preview_frame; \
    from mica.scientific.topology_kernel.martini.openmm_relaxation_smoke import run_openmm_relaxation_smoke; \
    from mica.scientific.topology_kernel import CGCaseStudyCompilation, CGRuntimeFoundation, MartiniAssetRegistry, MartiniLipidAssetRegistry; \
    from mica.sim.physiological_topology_kernel import PreparationProtocolDefaults; \
    from mica.sim.openmm_compiler import contracts; \
    from mica.sim.scientific_task_graph import TopologyAssembly; \
    from google.cloud import storage; \
    print('CONTAINER_SMOKE_V1 OK: 9 modules importable from /app/src'); \
    print('  - mica.md_preview.encode_preview_frame:', encode_preview_frame.__module__); \
    print('  - mica.scientific.topology_kernel.martini.openmm_relaxation_smoke.run_openmm_relaxation_smoke:', run_openmm_relaxation_smoke.__module__); \
    print('  - mica.scientific.topology_kernel.CGCaseStudyCompilation:', CGCaseStudyCompilation.__module__); \
    print('  - mica.scientific.topology_kernel.CGRuntimeFoundation:', CGRuntimeFoundation.__module__); \
    print('  - mica.scientific.topology_kernel.MartiniAssetRegistry:', MartiniAssetRegistry.__module__); \
    print('  - mica.scientific.topology_kernel.MartiniLipidAssetRegistry:', MartiniLipidAssetRegistry.__module__); \
    print('  - mica.sim.physiological_topology_kernel.PreparationProtocolDefaults:', PreparationProtocolDefaults.__module__); \
    print('  - mica.sim.openmm_compiler.contracts:', contracts.__module__); \
    print('  - mica.sim.scientific_task_graph.TopologyAssembly:', TopologyAssembly.__module__); \
    print('  - google.cloud.storage:', storage.__module__)"

CMD ["python", "/app/main_gcs.py"]

#!/usr/bin/env bash
# INSTRUCCION 74 (2026-07-22) -- entrypoint.sh
#
# Salad startup probe target. Runs gpu_preflight.py first; only if it
# exits 0 do we exec main_gcs.py (which becomes PID 1). On failure we
# write /tmp/mica-gpu-preflight-failed and `sleep infinity` so the
# Salad readiness probe times out and the node is reallocated.
#
# Marker conventions:
#   /tmp/mica-gpu-ready              -- touched on preflight success
#   /tmp/mica-gpu-preflight-failed   -- touched on preflight failure
#   /tmp/mica-gpu-preflight.json     -- full JSON receipt
set -u

echo "[entrypoint] starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[entrypoint] PATH=$PATH"
echo "[entrypoint] which python => $(command -v python || echo 'MISSING')"
echo "[entrypoint] which pip   => $(command -v pip   || echo 'MISSING')"

# Remove stale markers from previous runs in this container (re-allocated
# nodes can sometimes have a writeable root with stale files).
rm -f /tmp/mica-gpu-ready /tmp/mica-gpu-preflight-failed || true

# Run the preflight. We capture exit code but DO NOT hide stderr --
# Salad logs are the source of truth when the probe fails.
echo "[entrypoint] running gpu_preflight.py ..."
set +e
python /app/gpu_preflight.py
preflight_rc=$?
set -e

if [ "$preflight_rc" -ne 0 ]; then
    echo "[entrypoint] PREFLIGHT FAILED (rc=$preflight_rc). Marking /tmp/mica-gpu-preflight-failed and sleeping so Salad reallocates." >&2
    echo "rc=$preflight_rc" > /tmp/mica-gpu-preflight-failed || true
    # exec sleep so we ARE PID 1 -- otherwise shell would exit and the
    # container would restart, which on Salad just hits the same bad
    # node. We want the readiness probe to fail loudly with the marker
    # file present.
    exec sleep infinity
fi

echo "[entrypoint] preflight OK. exec main_gcs.py as PID 1."
exec python /app/main_gcs.py

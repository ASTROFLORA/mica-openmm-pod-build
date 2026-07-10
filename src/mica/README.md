# MICA Package Boundary Notes

This package mixes canonical live surfaces with a few legacy islands that still have active callers. The lists below call out the main surfaces and known islands; they are not a complete inventory.

## Primary canonical live surfaces

- `api_v1/`
- `drivers/`
- `infrastructure/`
- `scientific_workflow/`
- `storage/`
- `services/`
- `toolkg/`
- `worker/`
- `ws_agentic.py`

## Known legacy-but-live islands

- `spectra/`
- `scientific_driver.py`
- `enhanced_sampling/`
- `planning/`
- `integration/`

## Archived out of the live tree

- `agenticdriver.py` -> `archive/astroflora-core-feature-spectra-worker-integration-1/src/depricated/mica_legacy/agenticdriver.py`
- `spectra_DEPRICATED/` -> `archive/astroflora-core-feature-spectra-worker-integration-1/src/depricated/mica_legacy/spectra_DEPRICATED/`

Keep new canonical work on the live surfaces above. Treat the legacy islands as temporary compatibility surfaces until their callers are retired or modernized.
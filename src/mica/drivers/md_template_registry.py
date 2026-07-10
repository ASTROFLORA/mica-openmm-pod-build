from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

DEFAULT_TEMPLATE_ID = "complex_stability_v1"
DEFAULT_TEMPLATE_VERSION = "1.0.0"
DEFAULT_SCIENTIFIC_PHASE = "protein_ligand_production"
DEFAULT_LOCAL_PROCESSOR_SCRIPT = "workers/dynamo/biodynamo/processors/run_complex_stability.py"
DEFAULT_REMOTE_SIMULATION_SCRIPT = "runcomplex_paper_dodecaedrica.py"
DEFAULT_REMOTE_EXTRACTOR_SCRIPT = "extract_latest_pdb_every_10min.py"


@dataclass(frozen=True)
class LocalTemplateBinding:
    template_id: str
    template_version: str
    adapter_id: str
    family: str
    scientific_phase: str
    allowed_local_processor_scripts: Tuple[str, ...]
    allowed_remote_simulation_scripts: Tuple[str, ...]
    allowed_remote_extractor_scripts: Tuple[str, ...]


# Internal registry for local MD template bindings.
_LOCAL_TEMPLATE_BINDINGS: Dict[Tuple[str, str], LocalTemplateBinding] = {
    (DEFAULT_TEMPLATE_ID, DEFAULT_TEMPLATE_VERSION): LocalTemplateBinding(
        template_id=DEFAULT_TEMPLATE_ID,
        template_version=DEFAULT_TEMPLATE_VERSION,
        adapter_id="complex_stability_adapter",
        family="complex_stability",
        scientific_phase=DEFAULT_SCIENTIFIC_PHASE,
        allowed_local_processor_scripts=(DEFAULT_LOCAL_PROCESSOR_SCRIPT,),
        allowed_remote_simulation_scripts=(DEFAULT_REMOTE_SIMULATION_SCRIPT,),
        allowed_remote_extractor_scripts=(DEFAULT_REMOTE_EXTRACTOR_SCRIPT,),
    ),
}


def resolve_local_template_binding(
    *,
    template_id: str,
    template_version: str,
) -> LocalTemplateBinding:
    key = ((template_id or "").strip(), (template_version or "").strip())
    binding = _LOCAL_TEMPLATE_BINDINGS.get(key)
    if binding is None:
        raise ValueError(
            "Unknown local MD template binding "
            f"(template_id={template_id!r}, template_version={template_version!r})"
        )
    return binding


def build_template_ref_from_context(context: dict) -> dict:
    template_id = str(context.get("md_template_id", DEFAULT_TEMPLATE_ID) or DEFAULT_TEMPLATE_ID)
    template_version = str(
        context.get("md_template_version", DEFAULT_TEMPLATE_VERSION) or DEFAULT_TEMPLATE_VERSION
    )
    return {
        "template_id": template_id,
        "template_version": template_version,
    }

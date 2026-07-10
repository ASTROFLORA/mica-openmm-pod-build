from __future__ import annotations

from importlib import import_module
from typing import Any, Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "RunPodMDAdapter": (".runpod_md_adapter", "RunPodMDAdapter"),
    "SaladGCSAdapter": (".salad_gcs_adapter", "SaladGCSAdapter"),
    "VastGCSAdapter": (".vast_gcs_adapter", "VastGCSAdapter"),
    "VastMDAdapter": (".vast_md_adapter", "VastMDAdapter"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = target
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

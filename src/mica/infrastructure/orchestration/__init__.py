"""
Lazy exports for the orchestration package.

This package is imported by focused adapter tests that do not need the full
Vast+GCS or Google Cloud stack. Importing those heavy modules eagerly from the
package root makes lightweight contract tests pay unrelated startup cost and,
in this environment, can stall during importlib metadata scans inside the
Google stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "VastGCSOrchestrator": (".vast_gcs_integration", "VastGCSOrchestrator"),
    "MDJobConfig": (".vast_gcs_integration", "MDJobConfig"),
    "MDJobResult": (".vast_gcs_integration", "MDJobResult"),
    "MICA_DOCKER_IMAGES": (".vast_gcs_integration", "MICA_DOCKER_IMAGES"),
    "ForceFieldPolicy": (".forcefield_policy", "ForceFieldPolicy"),
    "ForceFieldSelector": (".forcefield_policy", "ForceFieldSelector"),
    "JobTransaction": (".job_transaction", "JobTransaction"),
    "NullDestroyFn": (".job_transaction", "NullDestroyFn"),
    "TransactionState": (".job_transaction", "TransactionState"),
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

"""
safe_pickle.py — Restricted pickle deserialization for MICA.

Standard pickle.loads() is equivalent to eval() and can execute arbitrary code.
This module provides safe_loads() with a RestrictedUnpickler that only allows
known-safe types (numpy arrays, builtins, collections).

OWASP A08:2021 — Software and Data Integrity Failures
CVE-2025-61765 reference — pickle RCE via untrusted sources

Usage:
    from mica.infrastructure.safe_pickle import safe_loads, safe_load

    data = safe_loads(blob)       # instead of pickle.loads(blob)
    data = safe_load(file_obj)    # instead of pickle.load(f)
"""

from __future__ import annotations

import io
import pickle
import logging
from typing import Any, FrozenSet, BinaryIO

logger = logging.getLogger("mica.infrastructure.safe_pickle")

# Only these modules+classes may be instantiated during deserialization.
_ALLOWED_MODULES: FrozenSet[tuple[str, str]] = frozenset({
    # numpy
    ("numpy", "ndarray"),
    ("numpy", "dtype"),
    ("numpy", "float64"),
    ("numpy", "float32"),
    ("numpy", "int64"),
    ("numpy", "int32"),
    ("numpy", "bool_"),
    ("numpy", "complex128"),
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"),
    ("numpy", "_core"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    # builtins
    ("builtins", "set"),
    ("builtins", "frozenset"),
    ("builtins", "bytes"),
    ("builtins", "complex"),
    ("builtins", "slice"),
    ("builtins", "range"),
    # collections
    ("collections", "OrderedDict"),
    ("collections", "defaultdict"),
    # datetime (for metadata)
    ("datetime", "datetime"),
    ("datetime", "date"),
    ("datetime", "timedelta"),
    # dataclasses that may be stored
    ("copy_reg", "_reconstructor"),
    ("copyreg", "_reconstructor"),
})


class RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that blocks instantiation of arbitrary classes.

    Only types listed in ``_ALLOWED_MODULES`` can be created.  Everything
    else raises ``pickle.UnpicklingError``.
    """

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) in _ALLOWED_MODULES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"Blocked unsafe pickle class: {module}.{name}"
        )


def safe_loads(data: bytes) -> Any:
    """Safe replacement for ``pickle.loads(data)``."""
    return RestrictedUnpickler(io.BytesIO(data)).load()


def safe_load(f: BinaryIO) -> Any:
    """Safe replacement for ``pickle.load(f)``."""
    return RestrictedUnpickler(f).load()

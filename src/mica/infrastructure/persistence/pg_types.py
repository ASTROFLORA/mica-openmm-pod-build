"""pg_types.py

Small helpers to keep asyncpg parameter passing consistent.

- asyncpg expects JSONB arguments to be strings when you cast with ::jsonb.
- pgvector parameters are commonly passed as string literals and cast with ::vector.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Sequence


def jsonb(value: Any) -> str:
    return json.dumps(value if value is not None else {})


def vector_literal(values: Sequence[float]) -> str:
    # Use Python's round-trippable float repr to preserve precision.
    # Postgres float input accepts scientific notation, so repr() is fine.
    out: list[str] = []
    for v in values:
        fv = float(v)
        if not math.isfinite(fv):
            raise ValueError("embedding contains NaN/Inf")
        out.append(repr(fv))
    return "[" + ",".join(out) + "]"


def one_hot_vector(dim: int, seed: int) -> str:
    if dim <= 0:
        raise ValueError("dim must be > 0")
    base = [0.0] * dim
    base[seed % dim] = 1.0
    if dim >= 2:
        base[(seed + 1) % dim] = 0.25
    return vector_literal(base)

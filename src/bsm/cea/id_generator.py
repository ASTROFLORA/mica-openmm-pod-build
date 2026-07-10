"""BUDO ID generation utilities for the Canonical Entity Atlas (CEA).

This module implements the standardized identity protocol defined in the
BSM-BUDO-CEA roadmap. IDs follow the pattern:

```
<namespace>:<slug>_v<version>
```

where `namespace` defaults to ``budo`` and the slug is derived from the
entity canonical name plus optional organism hints. Composite identifiers
for modality-specific tracks append suffixes (e.g., ``-S`` for structure).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from .exceptions import BudoIdError

_SLUG_SANITIZER = re.compile(r"[^a-z0-9_]+")
_VERSION_PATTERN = re.compile(r"_v(?P<version>\d{1,4})$", re.IGNORECASE)

_DEFAULT_SUFFIXES = {
    "structure": "-S",
    "sequence": "-Q",
    "expression": "-E",
    "dynamics": "-D",
    "metabolism": "-M",
    "interactions": "-I",
    "phenotype": "-P",
    "network": "-N",
}


@dataclass(frozen=True)
class BudoRootId:
    """Value object representing a normalized root BUDO identifier."""

    namespace: str
    slug: str
    version: int

    @property
    def value(self) -> str:
        return f"{self.namespace}:{self.slug}_v{self.version}"

    def bump(self, increment: int = 1) -> "BudoRootId":
        if increment < 1:
            raise BudoIdError("Version increments must be positive")
        return BudoRootId(self.namespace, self.slug, self.version + increment)


class BudoIdGenerator:
    """Factory for BUDO root and modality-specific identifiers."""

    def __init__(self, namespace: str = "budo", suffixes: Optional[Dict[str, str]] = None):
        if not namespace:
            raise BudoIdError("Namespace cannot be empty")
        self.namespace = namespace.lower()
        self.suffixes = {**_DEFAULT_SUFFIXES, **(suffixes or {})}
        for key, suffix in self.suffixes.items():
            if not suffix.startswith("-"):
                raise BudoIdError(f"Suffix for '{key}' must begin with '-'")

    @staticmethod
    def _slugify(name: str, organism: Optional[str] = None) -> str:
        if not name:
            raise BudoIdError("Entity name is required for slug creation")
        base = name
        if organism:
            base = f"{name}_{organism}"
        normalized = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
        sanitized = _SLUG_SANITIZER.sub("_", normalized.lower()).strip("_")
        sanitized = re.sub(r"_+", "_", sanitized)
        if not sanitized:
            raise BudoIdError("Slug normalization produced empty result")
        return sanitized

    def create_root_id(self, name: str, *, organism: Optional[str] = None, version: int = 1) -> BudoRootId:
        if version < 1:
            raise BudoIdError("Version must be >= 1")
        slug = self._slugify(name, organism)
        return BudoRootId(self.namespace, slug, version)

    def parse_root(self, budo_id: str) -> BudoRootId:
        if ":" not in budo_id:
            raise BudoIdError("Invalid BUDO ID: missing namespace")
        namespace, _, remainder = budo_id.partition(":")
        match = _VERSION_PATTERN.search(remainder)
        if not match:
            raise BudoIdError("Invalid BUDO ID: missing version suffix (_v<INT>)")
        version = int(match.group("version"))
        slug = remainder[: match.start()].strip("_")
        if not slug:
            raise BudoIdError("Invalid BUDO ID: empty slug")
        return BudoRootId(namespace.lower(), slug, version)

    def derive_suffix(self, budo_id: str, modality: str) -> str:
        if modality not in self.suffixes:
            raise BudoIdError(f"Unknown modality '{modality}'")
        return f"{budo_id}{self.suffixes[modality]}"

    def derive_all(self, budo_id: str, modalities: Optional[Iterable[str]] = None) -> Dict[str, str]:
        modalities = modalities or self.suffixes.keys()
        derived: Dict[str, str] = {}
        for modality in modalities:
            derived[modality] = self.derive_suffix(budo_id, modality)
        return derived

    def next_version(self, budo_id: str) -> BudoRootId:
        root = self.parse_root(budo_id)
        return root.bump()


__all__ = ["BudoIdGenerator", "BudoRootId", "BudoIdError"]

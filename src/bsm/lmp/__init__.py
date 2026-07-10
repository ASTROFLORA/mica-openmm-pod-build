"""LMP (Protein Markup Language) v2.0.

This package is used in both runtime systems and training-data exporters.

Important: we keep imports **lazy** to avoid pulling heavy dependencies
(e.g., schema models / pydantic) when callers only need lightweight submodules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "LMPParser",
    "LMPGenerator",
    "LMPValidator",
    "LMPStateAnnotator",
    "LmpV3Generator",
    "LMPScannerV3",
]


if TYPE_CHECKING:
    from .generator import LMPGenerator as LMPGenerator
    from .generator_v3 import LmpV3Generator as LmpV3Generator
    from .parser import LMPParser as LMPParser
    from .scanner_v3 import LMPScannerV3 as LMPScannerV3
    from .state_annotator import LMPStateAnnotator as LMPStateAnnotator
    from .validator import LMPValidator as LMPValidator


def __getattr__(name: str) -> Any:
    if name == "LMPGenerator":
        from .generator import LMPGenerator

        return LMPGenerator
    if name == "LmpV3Generator":
        from .generator_v3 import LmpV3Generator

        return LmpV3Generator
    if name == "LMPScannerV3":
        from .scanner_v3 import LMPScannerV3

        return LMPScannerV3
    if name == "LMPParser":
        from .parser import LMPParser

        return LMPParser
    if name == "LMPValidator":
        from .validator import LMPValidator

        return LMPValidator
    if name == "LMPStateAnnotator":
        from .state_annotator import LMPStateAnnotator

        return LMPStateAnnotator
    raise AttributeError(name)

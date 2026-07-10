"""
KB Quantification Normalizer — K5-1 (KB Slice 3)

Normalizes raw quantification from DLM extraction into canonical UO/QUDT units.
Produces quantification_bucket for fingerprint dedup and tier scoring.

Key objects:
- QuantificationNormalizer: normalizes value+unit → canonical value+unit+bucket
- NormalizationReceipt: audit trail for normalization decisions
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from .claim_atom import Quantification


# Canonical UO/QUDT unit mappings (simplified — real system loads from snapshots)
_UNIT_CANONICAL: Dict[str, str] = {
    "um": "uo:micrometer",
    "µm": "uo:micrometer",
    "nm": "uo/nanometer",
    "mm": "uo/millimeter",
    "cm": "uo/centimeter",
    "m": "uo/meter",
    "mg": "uo/milligram",
    "ug": "uo/microgram",
    "ng": "uo/nanogram",
    "pg": "uo/picogram",
    "mmol": "uo/millimole",
    "umol": "uo/micromole",
    "nmol": "uo/nanomole",
    "pmol": "uo/picomole",
    "mol": "uo/mole",
    "ml": "uo/milliliter",
    "ul": "uo/microliter",
    "s": "uo/second",
    "ms": "uo/millisecond",
    "min": "uo/minute",
    "h": "uo/hour",
    "day": "uo/day",
    "°C": "uo/degree_celsius",
    "K": "uo/kelvin",
    "fold": "uo/fold_change",
    "x": "uo/fold_change",
    "%": "uo/percent",
    "mM": "uo/millimolar",
    "uM": "uo/micromolar",
    "nM": "uo/nanomolar",
    "pM": "uo/picomolar",
    "M": "uo/molar",
    "Hz": "uo/hertz",
    "kDa": "uo/kilodalton",
    "Da": "uo/dalton",
    "kcal/mol": "uo/kilocalorie_per_mole",
    "kJ/mol": "uo/kilojoule_per_mole",
    "eV": "uo/electronvolt",
}

# Conversion factors to base unit
_UNIT_TO_BASE: Dict[str, Tuple[str, float]] = {
    "uo:micrometer": ("uo/meter", 1e-6),
    "uo/nanometer": ("uo/meter", 1e-9),
    "uo/millimeter": ("uo/meter", 1e-3),
    "uo/centimeter": ("uo/meter", 1e-2),
    "uo/meter": ("uo/meter", 1.0),
    "uo/milligram": ("uo/gram", 1e-3),
    "uo/microgram": ("uo/gram", 1e-6),
    "uo/nanogram": ("uo/gram", 1e-9),
    "uo/millimole": ("uo/mole", 1e-3),
    "uo/micromole": ("uo/mole", 1e-6),
    "uo/nanomole": ("uo/mole", 1e-9),
    "uo/milliliter": ("uo/liter", 1e-3),
    "uo/microliter": ("uo/liter", 1e-6),
    "uo/millisecond": ("uo/second", 1e-3),
    "uo/minute": ("uo/second", 60),
    "uo/hour": ("uo/second", 3600),
    "uo/millimolar": ("uo/molar", 1e-3),
    "uo/micromolar": ("uo/molar", 1e-6),
    "uo/nanomolar": ("uo/molar", 1e-9),
    "uo/picomolar": ("uo/molar", 1e-12),
    "uo/kilodalton": ("uo/dalton", 1000),
    "uo/percent": ("uo/dimensionless", 0.01),
    "uo/fold_change": ("uo/dimensionless", 1.0),
}


class QuantificationBucket(str, Enum):
    """Bucket categories for fingerprint dedup."""
    NONE = "none"
    INCREASE_STRONG = "increase_strong"
    INCREASE_MODERATE = "increase_moderate"
    INCREASE_WEAK = "increase_weak"
    DECREASE_STRONG = "decrease_strong"
    DECREASE_MODERATE = "decrease_moderate"
    DECREASE_WEAK = "decrease_weak"
    NO_EFFECT = "no_effect"
    QUALITATIVE = "qualitative"


@dataclass
class NormalizationReceipt:
    """Audit trail for quantification normalization."""
    receipt_ref: str = ""
    raw_value: Optional[float] = None
    raw_unit: Optional[str] = None
    canonical_value: Optional[float] = None
    canonical_unit_ref: Optional[str] = None
    quantification_bucket: str = "none"
    conversion_applied: bool = False
    conversion_factor: Optional[float] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _normalize_unit(unit: Optional[str]) -> Optional[str]:
    """Normalize unit string to canonical UO/QUDT reference."""
    if not unit:
        return None
    unit_clean = unit.strip().lower()
    return _UNIT_CANONICAL.get(unit_clean, f"uo:{unit_clean}")


def _convert_to_base(value: float, canonical_unit: str) -> Tuple[str, float]:
    """Convert value to base unit. Returns (base_unit_ref, converted_value)."""
    if canonical_unit in _UNIT_TO_BASE:
        base_unit, factor = _UNIT_TO_BASE[canonical_unit]
        return base_unit, value * factor
    return canonical_unit, value


def _compute_bucket(value: Optional[float], direction: Optional[str] = None) -> str:
    """Compute quantification bucket from value and direction."""
    if value is None:
        return QuantificationBucket.NONE.value
    if abs(value) >= 2.0:
        return QuantificationBucket.INCREASE_STRONG.value
    if abs(value) >= 1.2:
        return QuantificationBucket.INCREASE_MODERATE.value
    if abs(value) > 0:
        return QuantificationBucket.INCREASE_WEAK.value
    return QuantificationBucket.NO_EFFECT.value


def _quantification_bucket_from_relation(relation: Any) -> str:
    """Compute bucket from ExtractedRelation fields."""
    value = getattr(relation, "quantification_value", None)
    unit = getattr(relation, "quantification_unit", None)
    direction = getattr(relation, "direction", "unknown")
    if value is None and unit is None:
        return QuantificationBucket.NONE.value
    if value is not None:
        return _compute_bucket(value, direction)
    if unit:
        return QuantificationBucket.QUALITATIVE.value
    return QuantificationBucket.NONE.value


class QuantificationNormalizer:
    """K5-1: Normalizes raw quantification to canonical units with bucket."""

    def normalize_from_quantification(self, quant: Quantification) -> NormalizationReceipt:
        """Normalize a Quantification object."""
        if quant.value is None and quant.unit is None:
            return NormalizationReceipt(quantification_bucket=QuantificationBucket.NONE.value)

        canonical_unit = _normalize_unit(quant.unit)
        canonical_value = quant.value
        conversion_applied = False
        conversion_factor = None

        if quant.value is not None and canonical_unit:
            base_unit, converted = _convert_to_base(quant.value, canonical_unit)
            if converted != quant.value:
                canonical_value = converted
                canonical_unit = base_unit
                conversion_applied = True
                conversion_factor = converted / quant.value if quant.value else None

        bucket = _compute_bucket(canonical_value, None)

        raw = f"norm:{quant.value}:{quant.unit}:{canonical_value}:{canonical_unit}"
        receipt_ref = f"receipt://quant_norm/{hashlib.sha256(raw.encode()).hexdigest()[:12]}"

        return NormalizationReceipt(
            receipt_ref=receipt_ref,
            raw_value=quant.value,
            raw_unit=quant.unit,
            canonical_value=canonical_value,
            canonical_unit_ref=canonical_unit,
            quantification_bucket=bucket,
            conversion_applied=conversion_applied,
            conversion_factor=conversion_factor,
        )

    def normalize_from_relation(self, relation_dict: Dict[str, Any]) -> NormalizationReceipt:
        """Normalize from ExtractedRelation dict fields."""
        value = relation_dict.get("quantification_value")
        unit = relation_dict.get("quantification_unit")
        canonical_unit = _normalize_unit(unit)
        canonical_value = value
        conversion_applied = False
        conversion_factor = None

        if value is not None and canonical_unit:
            base_unit, converted = _convert_to_base(value, canonical_unit)
            if converted != value:
                canonical_value = converted
                canonical_unit = base_unit
                conversion_applied = True
                conversion_factor = converted / value if value else None

        bucket = _compute_bucket(canonical_value)

        raw = f"norm:{value}:{unit}:{canonical_value}:{canonical_unit}"
        receipt_ref = f"receipt://quant_norm/{hashlib.sha256(raw.encode()).hexdigest()[:12]}"

        return NormalizationReceipt(
            receipt_ref=receipt_ref,
            raw_value=value,
            raw_unit=unit,
            canonical_value=canonical_value,
            canonical_unit_ref=canonical_unit,
            quantification_bucket=bucket,
            conversion_applied=conversion_applied,
            conversion_factor=conversion_factor,
        )

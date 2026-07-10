from __future__ import annotations

from typing import Any, Dict, Iterable

CANONICAL_PHASE_SEQUENCE = (
    "minimization",
    "equilibration",
    "production",
)

DEFAULT_REQUIRED_ARTIFACT_TYPES = (
    "trajectory_dcd",
    "energy_csv",
)

DEFAULT_DURABILITY_ARTIFACT_TYPES = (
    "final_checkpoint",
)


def _normalize_phase_sequence(phases: Iterable[Any] | None) -> list[str]:
    if phases is None:
        return list(CANONICAL_PHASE_SEQUENCE)
    normalized = [str(phase).strip().lower() for phase in phases if str(phase).strip()]
    return normalized or list(CANONICAL_PHASE_SEQUENCE)


def build_scientific_protocol_metadata(
    context: Dict[str, Any],
    *,
    simulation_mode: str,
) -> Dict[str, Any]:
    forcefield = str(context.get("forcefield", "amber14sb") or "amber14sb")
    water_model = str(context.get("water_model", "tip3p") or "tip3p")
    ml_potential_context = context.get("ml_potential")

    if isinstance(ml_potential_context, dict):
        ml_potential = dict(ml_potential_context)
        ml_potential.setdefault("requested", bool(ml_potential.get("engine")))
        ml_potential.setdefault("supported", False)
    else:
        ml_potential = {
            "requested": bool(context.get("use_ml_potential", False)),
            "engine": str(context.get("ml_potential_engine", "") or ""),
            "supported": False,
        }

    return {
        "workflow": "protein_ligand_md",
        "simulation_mode": str(simulation_mode or "").strip().lower(),
        "forcefield": forcefield,
        "phases": _normalize_phase_sequence(context.get("scientific_phases")),
        "water_model": water_model,
        "ions": {
            "ionic_strength_molar": float(context.get("ionic_strength_molar", 0.15)),
            "positive_ion": str(context.get("positive_ion", "Na+") or "Na+"),
            "negative_ion": str(context.get("negative_ion", "Cl-") or "Cl-"),
        },
        "bvs": dict(context.get("bvs") or {}),
        "ml_potential": ml_potential,
        "artifact_manifest_expectations": {
            "required_artifact_types": list(DEFAULT_REQUIRED_ARTIFACT_TYPES),
            "durability_artifact_types": list(DEFAULT_DURABILITY_ARTIFACT_TYPES),
            "require_segment_evidence": bool(context.get("require_segment_evidence", False)),
        },
    }

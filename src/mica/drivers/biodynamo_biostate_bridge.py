from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import url2pathname


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value or []) if isinstance(value, (list, tuple)) else []


def _string(value: Any, default: str = "") -> str:
    return str(value or default).strip()


def _path_from_uri(input_uri: str) -> str:
    raw = _string(input_uri)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme != "file":
        return raw
    if parsed.scheme == "file":
        pathname = url2pathname(f"//{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path)
        return str(Path(pathname))
    return raw


def _forcefield_for_driver(raw_manifest: Mapping[str, Any] | None, compiled_plan: Mapping[str, Any]) -> str:
    manifest = _mapping(raw_manifest)
    if _string(manifest.get("forcefield_family")):
        return _string(manifest.get("forcefield_family")).lower()
    system = _mapping(manifest.get("system"))
    forcefield = _mapping(system.get("forcefield"))
    if _string(forcefield.get("family")):
        return _string(forcefield.get("family")).lower()

    normalized = {
        "ff14SB": "amber14sb",
        "ff19SB": "amber19sb",
        "CHARMM36m": "charmm36",
    }
    mode_manifest = _mapping(compiled_plan.get("mode_manifest"))
    return normalized.get(_string(mode_manifest.get("force_field")), "amber14sb")


def _first_ligand_smiles(compiled_plan: Mapping[str, Any]) -> str:
    components = _mapping(compiled_plan.get("components"))
    for ligand in _list(components.get("ligands")):
        if isinstance(ligand, Mapping):
            smiles = _string(ligand.get("smiles"))
            if smiles:
                return smiles
    return ""


def _phase_value(compiled_plan: Mapping[str, Any], phase_name: str, key: str, default: Any = None) -> Any:
    phases = _mapping(compiled_plan.get("phase_plan"))
    phase = _mapping(phases.get(phase_name))
    return phase.get(key, default)


def build_context_from_compiled_biostate(
    *,
    compiled_plan: Mapping[str, Any],
    raw_manifest: Mapping[str, Any] | None = None,
    compatibility_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = _mapping(raw_manifest)
    context = dict(compatibility_context or {})

    protein_pdb = _path_from_uri(_string(compiled_plan.get("structure_input_uri")))
    ligand_input_uri = _string(compiled_plan.get("ligand_input_uri"))
    ligand_input_path = _path_from_uri(ligand_input_uri)
    ligand_smiles = _string(context.get("ligand_smiles")) or _first_ligand_smiles(compiled_plan)
    scientific_task_graph = _mapping(compiled_plan.get("scientific_task_graph"))

    protocol = _mapping(compiled_plan.get("protocol"))
    economics = _mapping(compiled_plan.get("economics"))
    ml_potential = _mapping(compiled_plan.get("ml_potential"))
    lineage = _mapping(compiled_plan.get("lineage"))
    biostate_seed_metadata = _mapping(lineage.get("biostate_seed_metadata") or compiled_plan.get("biostate_seed_metadata"))

    derived: dict[str, Any] = {
        "protein_pdb": protein_pdb,
        "ligand_smiles": ligand_smiles,
        "min_max_iter": int(_phase_value(compiled_plan, "minimization", "steps", 10000) or 10000),
        "production_ns": float(_phase_value(compiled_plan, "production", "duration_ns", 50.0) or 50.0),
        "nvt_ps": float((_phase_value(compiled_plan, "equilibration", "duration_ns", 0.5) or 0.5) * 1000.0),
        "npt_duration_ps": float((_phase_value(compiled_plan, "equilibration", "duration_ns", 0.5) or 0.5) * 1000.0),
        "temperature_K": float(
            _phase_value(compiled_plan, "production", "temperature_k")
            or _phase_value(compiled_plan, "equilibration", "temperature_k")
            or 300.0
        ),
        "npt_pressure_bar": float(
            _phase_value(compiled_plan, "production", "pressure_atm")
            or _phase_value(compiled_plan, "equilibration", "pressure_atm")
            or 1.0
        ),
        "forcefield": _forcefield_for_driver(manifest, compiled_plan),
        "execution_class": _string(protocol.get("execution_class"), "production"),
        "checkpoint_policy": _string(protocol.get("checkpoint_policy"), "strict"),
        "storage_backend": _string(protocol.get("storage_backend"), "none"),
        "require_segment_evidence": bool(
            _mapping(protocol.get("artifact_manifest_expectations")).get("require_segment_evidence", False)
        ),
        "max_price_per_hour": economics.get("max_price_per_hour"),
        "max_total_cost_usd": economics.get("max_total_cost_usd"),
        "max_runtime_hours": economics.get("max_runtime_hours"),
        "preserve_instance_on_failure": bool(economics.get("preserve_instance_on_failure", True)),
        "potential_augmenter": _string(ml_potential.get("augmenter")),
        "potential_model": _string(ml_potential.get("model")),
        "potential_region_selector": _string(ml_potential.get("region_selector")),
        "job_name": _string(compiled_plan.get("biostate_id") or lineage.get("source_run_id"), "biostate_v2_run"),
        "compiled_biostate_plan": dict(compiled_plan),
        "biostate_manifest": manifest,
        "biostate_seed_metadata": biostate_seed_metadata,
        "biostate_import_receipt": {
            "authority": "BioStateV2DriverBridge",
            "task": _string(compiled_plan.get("task")),
            "requested_assay": _string(compiled_plan.get("requested_assay")),
            "biostate_seed_metadata": biostate_seed_metadata,
            "compatibility_inputs": {
                "simulation_mode": _string(context.get("simulation_mode")),
                "use_remote_vast": bool(context.get("use_remote_vast", False)),
                "execution_backend": _string(context.get("execution_backend")),
            },
        },
    }

    if scientific_task_graph:
        derived["scientific_task_graph"] = scientific_task_graph

    if ligand_input_path:
        suffix = Path(ligand_input_path).suffix.lower()
        if suffix == ".sdf":
            derived["docked_ligand_sdf"] = ligand_input_path
        else:
            derived["docked_ligand_pdb"] = ligand_input_path

    if not _string(context.get("simulation_mode")):
        task = _string(compiled_plan.get("task"))
        derived["simulation_mode"] = "complex" if task in {"protein_ligand_md", "complex_stability"} else "binding"

    for key, value in derived.items():
        if value not in (None, "", [], {}):
            context[key] = value
    return context

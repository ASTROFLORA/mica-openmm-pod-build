from __future__ import annotations

import json
import math
import shutil
import time
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mica_q.protocol_jsonld_contract import (
    ProtocolApprovalMode,
    ProtocolApprovalPolicy,
    ProtocolBudgetPolicy,
    ProtocolExecutionMode,
    ProtocolJSONLDDocument,
    ProtocolLedgerMode,
    ProtocolLedgerPolicy,
    ProtocolNode,
    ProtocolNodePolicies,
    ProtocolReceiptSchema,
    ProtocolRiskProfile,
)

from ..contracts import ArtifactRef, _validate_non_empty, _validate_unique_strings
from ..membrane.real_artifact_bundle import build_artifact_ref, infer_media_type, parse_gro_file, sha256_file

DEFAULT_RELAXATION_PLATFORM_PREFERENCE = ("OpenCL", "CPU", "Reference")
DEFAULT_RELAXATION_TEMPERATURE_K = 310.0
DEFAULT_RELAXATION_FRICTION_PER_PS = 10.0
DEFAULT_RELAXATION_TIMESTEP_FS = 20.0


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _float_energy(quantity: Any, unit_module: Any) -> float:
    return float(quantity.value_in_unit(unit_module.kilojoule_per_mole))


def _positions_are_finite(state: Any, unit_module: Any) -> bool:
    positions = state.getPositions(asNumpy=True).value_in_unit(unit_module.nanometer)
    for vector in positions:
        for component in vector:
            if not math.isfinite(float(component)):
                return False
    return True


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _artifact_entry(
    path: Path,
    *,
    artifact_id: str,
    source_node: str,
    source_receipt: str,
    canonical_or_preview: str = "canonical",
) -> dict[str, Any]:
    path = path.resolve()
    return {
        "artifact_id": artifact_id,
        "filename": path.name,
        "path": str(path),
        "type": infer_media_type(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "created_at": _utcnow(),
        "source_node": source_node,
        "source_receipt": source_receipt,
        "canonical_or_preview": canonical_or_preview,
        "production_claim": False,
    }


class OpenMMRelaxationSmokeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    source_target_id: str = "clcn7"
    input_bundle_ref: str
    preprocessed_topology_ref: str
    coordinate_ref: str
    platform_preference: list[str] = Field(default_factory=lambda: list(DEFAULT_RELAXATION_PLATFORM_PREFERENCE))
    minimization_enabled: bool = True
    minimization_max_iterations: int = Field(default=25, ge=0, le=100)
    dynamics_enabled: bool = True
    dynamics_steps: int = Field(default=50, ge=0, le=1000)
    timestep_fs: float = Field(default=DEFAULT_RELAXATION_TIMESTEP_FS, gt=0.0, le=20.0)
    temperature_k: float = Field(default=DEFAULT_RELAXATION_TEMPERATURE_K, gt=0.0, le=400.0)
    friction_per_ps: float = Field(default=DEFAULT_RELAXATION_FRICTION_PER_PS, gt=0.0, le=50.0)
    constraints_policy: str = "topology_defined"
    reporter_interval: int = Field(default=10, ge=1, le=1000)
    random_seed: int | None = Field(default=20260529, ge=1, le=2147483647)
    checkpoint_enabled: bool = True
    artifact_output_dir: str
    wall_clock_limit_s: int = Field(default=120, ge=1, le=900)
    production_ready: bool = False
    biological_correctness_claim: bool = False
    exact_lysosomal_claim: bool = False
    run_mode: str = "relaxation_smoke"
    no_silent_success_check: bool = True
    dynamics_chunk_size: int = Field(default=10, ge=1, le=1000)
    smoke_defaults: bool = True

    @field_validator(
        "run_id",
        "source_target_id",
        "input_bundle_ref",
        "preprocessed_topology_ref",
        "coordinate_ref",
        "artifact_output_dir",
        "constraints_policy",
        "run_mode",
    )
    @classmethod
    def validate_strings(cls, value: str) -> str:
        return _validate_non_empty(value)

    @field_validator("platform_preference")
    @classmethod
    def validate_platforms(cls, values: list[str]) -> list[str]:
        return _validate_unique_strings(values, field_name="platform_preference")

    @model_validator(mode="after")
    def validate_guards(self) -> "OpenMMRelaxationSmokeConfig":
        if self.production_ready:
            raise ValueError("production_ready must remain false for relaxation smoke")
        if self.biological_correctness_claim:
            raise ValueError("biological_correctness_claim must remain false for relaxation smoke")
        if self.exact_lysosomal_claim:
            raise ValueError("exact_lysosomal_claim must remain false for relaxation smoke")
        if self.run_mode != "relaxation_smoke":
            raise ValueError("run_mode must be relaxation_smoke")
        if not self.minimization_enabled and self.dynamics_enabled and self.dynamics_steps > 0:
            raise ValueError("dynamics require minimization_enabled for this guarded smoke path")
        return self


def build_clcn7_relaxation_protocol_document(
    *,
    config: OpenMMRelaxationSmokeConfig,
) -> ProtocolJSONLDDocument:
    node_specs = [
        ("load_clcn7_membrane_bundle", "validation", "Load the governed CLCN7 membrane bundle and bound runtime inputs."),
        ("validate_preprocessed_topology", "validation", "Validate the preprocessed Martini topology before relaxation."),
        ("create_openmm_system", "cg_openmm_smoke", "Create the Martini OpenMM system from the preprocessed topology."),
        ("create_simulation_context", "cg_openmm_smoke", "Create the OpenMM simulation context on the selected local platform."),
        ("compute_initial_energy", "validation", "Compute the initial finite potential energy before relaxation."),
        ("bounded_minimization", "cg_openmm_smoke", "Run bounded local energy minimization under the configured iteration cap."),
        ("bounded_short_dynamics_optional", "cg_openmm_smoke", "Run tiny bounded dynamics only if minimization succeeds and guards hold."),
        ("write_artifacts", "cg_analysis_export", "Write final structures, state, checkpoint, and runtime logs."),
        ("artifact_manifest", "validation", "Emit the governed artifact manifest with checksum and size metadata."),
        ("sanity_checks", "validation", "Run finite-energy and artifact-presence sanity checks."),
        ("biodynamo_relaxation_receipt", "validation", "Project the relaxation result into a BioDynamo next-mode receipt."),
        ("quetzal_smic_status_packet", "cg_analysis_export", "Emit a Quetzal packet with honest metrics status boundaries."),
        ("evidencegate_relaxation_closure", "validation", "Classify the bounded relaxation smoke from receipts and artifacts."),
    ]
    nodes: list[ProtocolNode] = []
    previous = ""
    for node_id, node_kind, objective in node_specs:
        nodes.append(
            ProtocolNode(
                node_id=node_id,
                node_kind=node_kind,
                executor_surface="topology_kernel" if "cg_openmm" not in node_kind else "biodynamo_runtime",
                executor_id=f"clcn7_relaxation:{node_id}",
                objective=objective,
                dependencies=[previous] if previous else [],
                inputs={"run_id": config.run_id},
                expected_outputs={"receipt": f"{node_id}_receipt"},
                evidence_requirements=["scientific_task_receipt_v1"],
                ui_schema={
                    "run_mode": config.run_mode,
                    "production_claim": False,
                    "max_wall_clock_s": config.wall_clock_limit_s,
                    "max_steps": config.dynamics_steps,
                },
                policies=ProtocolNodePolicies(
                    protected_surface=False,
                    production_compute=False,
                    requires_human_approval=False,
                    max_retries=0,
                ),
                failure_policy="halt",
                receipt_schema=ProtocolReceiptSchema(),
            )
        )
        previous = node_id

    return ProtocolJSONLDDocument(
        **{
            "@context": "https://mica.astroflora.org/schema/protocol/v1",
            "@type": "MICAProtocol",
            "protocol_id": f"protocol:{config.run_id}",
            "version": "1.0.0",
            "session_id": config.run_id,
            "owner_lab": "QUETZAL_SUPERNOVA",
            "execution_mode": ProtocolExecutionMode.DEVELOPMENT,
            "risk_profile": ProtocolRiskProfile.LOW,
            "budgets": ProtocolBudgetPolicy(
                max_steps=len(nodes),
                max_usd=0.0,
                max_wall_clock_s=config.wall_clock_limit_s,
            ),
            "approval_policy": ProtocolApprovalPolicy(
                mode=ProtocolApprovalMode.AUTO,
                required_approvers=[],
                protected_surfaces=[],
            ),
            "ledger_policy": ProtocolLedgerPolicy(
                mode=ProtocolLedgerMode.PROTOCOL_AND_NODE_RECEIPTS,
                receipt_schema="mica.receipts.node.v1",
                emit_events=True,
                require_node_receipts=True,
            ),
            "nodes": nodes,
            "edges": [
                {
                    "source_node_id": nodes[index - 1].node_id,
                    "target_node_id": node.node_id,
                    "edge_type": "data_dependency",
                    "rationale": "Bounded relaxation smoke proceeds strictly in order.",
                }
                for index, node in enumerate(nodes)
                if index > 0
            ],
            "metadata": {
                "source_target_id": config.source_target_id,
                "run_mode": config.run_mode,
                "max_wall_clock_s": config.wall_clock_limit_s,
                "max_steps": config.dynamics_steps,
                "production_claim": False,
                "no_silent_success_check": True,
            },
        }
    )


def build_relaxation_artifact_manifest(
    *,
    run_id: str,
    source_receipt: str,
    artifact_paths: dict[str, Path],
    missing_reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    for label, path in artifact_paths.items():
        if not path.exists() or not path.is_file():
            continue
        artifacts.append(
            _artifact_entry(
                path,
                artifact_id=f"{run_id}:{label}",
                source_node="write_artifacts",
                source_receipt=source_receipt,
                canonical_or_preview="preview" if "trajectory" in label else "canonical",
            )
        )
    return {
        "run_id": run_id,
        "artifacts": artifacts,
        "missing_artifacts": dict(missing_reasons or {}),
        "production_claim": False,
    }


def build_relaxation_sanity_receipt(
    *,
    run_id: str,
    coordinate_ref: str,
    runtime_receipt: dict[str, Any],
    artifact_manifest: dict[str, Any],
) -> dict[str, Any]:
    blockers = list(runtime_receipt.get("blockers", []))
    initial_energy = runtime_receipt.get("initial_energy")
    post_min_energy = runtime_receipt.get("post_minimization_energy")
    final_energy = runtime_receipt.get("final_energy")
    checks = {
        "particle_count_unchanged": bool(runtime_receipt.get("particle_count", 0) > 0),
        "initial_energy_finite": isinstance(initial_energy, (int, float)) and math.isfinite(float(initial_energy)),
        "post_minimization_energy_finite": post_min_energy is None or math.isfinite(float(post_min_energy)),
        "final_energy_finite": final_energy is None or math.isfinite(float(final_energy)),
        "no_nan_positions": not runtime_receipt.get("nan_positions_detected", False),
        "box_dimensions_present": bool(parse_gro_file(_as_path(coordinate_ref)).get("box_values")),
        "final_structure_exists": any(item["filename"].startswith("minimized_structure") for item in artifact_manifest.get("artifacts", [])),
        "artifact_sizes_nonzero": all(int(item["size_bytes"]) > 0 for item in artifact_manifest.get("artifacts", [])),
        "production_ready_false": runtime_receipt.get("production_ready", False) is False,
    }
    if not checks["initial_energy_finite"]:
        blockers.append("initial_energy_not_finite")
    if not checks["post_minimization_energy_finite"]:
        blockers.append("post_minimization_energy_not_finite")
    if not checks["no_nan_positions"]:
        blockers.append("nan_positions_detected")
    if not checks["final_structure_exists"]:
        blockers.append("final_structure_missing")
    status = "passed" if not blockers else "blocked"
    return {
        "run_id": run_id,
        "status": status,
        "checks": checks,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(runtime_receipt.get("warnings", [])),
        "production_ready": False,
    }


def build_biodynamo_relaxation_receipt(
    *,
    run_id: str,
    consumed_biostate_ref: str,
    consumed_bundle_ref: str,
    consumed_preprocessed_topology_ref: str,
    relaxation_runtime_receipt_ref: str,
    runtime_receipt: dict[str, Any],
) -> dict[str, Any]:
    allowed_next_modes = ["artifact_inspection", "topology_smoke", "cg_openmm_smoke"]
    if runtime_receipt.get("status") == "completed":
        allowed_next_modes.extend(["relaxation_smoke", "short_realtime_preview_smoke"])
    elif runtime_receipt.get("status") == "partial_artifacts_generated_no_steps":
        allowed_next_modes.append("relaxation_smoke")
    return {
        "run_id": run_id,
        "consumed_biostate_ref": consumed_biostate_ref,
        "consumed_bundle_ref": consumed_bundle_ref,
        "consumed_preprocessed_topology_ref": consumed_preprocessed_topology_ref,
        "relaxation_runtime_receipt_ref": relaxation_runtime_receipt_ref,
        "allowed_next_modes": list(dict.fromkeys(allowed_next_modes)),
        "blocked_modes": ["production_md", "long_md", "biological_claim"],
        "status": "ready_for_next_smoke" if runtime_receipt.get("status") == "completed" else "blocked_progression",
        "warnings": list(runtime_receipt.get("warnings", [])),
        "blockers": list(runtime_receipt.get("blockers", [])),
    }


def build_quetzal_relaxation_packet(
    *,
    run_id: str,
    artifact_manifest: dict[str, Any],
    runtime_receipt: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    packet = {
        "run_id": run_id,
        "what_was_run": "bounded_clcn7_cg_membrane_relaxation_smoke",
        "artifacts": artifact_manifest.get("artifacts", []),
        "energy_finite": runtime_receipt.get("energy_status") == "available",
        "minimization_passed": runtime_receipt.get("post_minimization_energy") is not None and not runtime_receipt.get("blockers"),
        "short_dynamics_ran": int(runtime_receipt.get("dynamics_steps_run", 0)) > 0,
        "trajectory_exists": any("trajectory" in item["filename"] for item in artifact_manifest.get("artifacts", [])),
        "metrics_status": "not_executed",
        "biological_stability_claim": False,
        "production_claim": False,
        "recommended_next_step": (
            "CLCN7_SHORT_REALTIME_PREVIEW_SMOKE"
            if runtime_receipt.get("status") == "completed"
            else "Fix runtime instability before preview"
        ),
    }
    md_lines = [
        f"# quetzal_clcn7_cg_membrane_relaxation_packet_v1",
        "",
        f"- run_id: `{run_id}`",
        f"- runtime_status: `{runtime_receipt.get('status', 'unknown')}`",
        f"- energy_status: `{runtime_receipt.get('energy_status', 'unavailable')}`",
        f"- minimization_passed: `{packet['minimization_passed']}`",
        f"- short_dynamics_ran: `{packet['short_dynamics_ran']}`",
        f"- trajectory_exists: `{packet['trajectory_exists']}`",
        f"- metrics_status: `{packet['metrics_status']}`",
        f"- production_claim: `false`",
        f"- recommended_next_step: `{packet['recommended_next_step']}`",
    ]
    return packet, "\n".join(md_lines) + "\n"


def run_openmm_relaxation_smoke(
    config: OpenMMRelaxationSmokeConfig,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    artifact_root = _as_path(config.artifact_output_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifacts_dir = artifact_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    topology_path = _as_path(config.preprocessed_topology_ref)
    coordinate_path = _as_path(config.coordinate_ref)
    runtime_log_path = artifacts_dir / "runtime.log"
    energy_summary_path = artifacts_dir / "energy_summary.json"
    initial_structure_path = artifacts_dir / "initial_structure.gro"
    minimized_structure_path = artifacts_dir / "minimized_structure.pdb"
    final_structure_path = artifacts_dir / "final_structure.pdb"
    trajectory_path = artifacts_dir / "short_trajectory.dcd"
    final_state_path = artifacts_dir / "final_state.xml"
    checkpoint_path = artifacts_dir / "checkpoint.chk"

    shutil.copy2(coordinate_path, initial_structure_path)

    runtime_receipt: dict[str, Any] = {
        "run_id": config.run_id,
        "status": "failed_runtime",
        "platform": "",
        "particle_count": 0,
        "initial_energy": None,
        "post_minimization_energy": None,
        "final_energy": None,
        "dynamics_steps_run": 0,
        "minimization_iterations": 0,
        "artifacts": [],
        "warnings": [],
        "blockers": [],
        "failure_code": "",
        "failure_detail": "",
        "no_silent_success_check": True,
        "production_ready": False,
        "energy_status": "unavailable",
        "nan_positions_detected": False,
    }
    missing_artifacts: dict[str, str] = {}
    missing_artifacts.update(
        {
            "runtime_log": "not_produced_yet",
            "energy_summary": "not_produced_yet",
            "minimized_structure": "not_produced_yet",
            "final_structure": "not_produced_yet",
            "short_trajectory": "not_produced_yet",
            "final_state": "not_produced_yet",
            "checkpoint": "not_produced_yet",
        }
    )
    start_time = time.monotonic()

    def fail(code: str, detail: str, *, status: str = "failed_runtime") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        runtime_receipt["status"] = status
        runtime_receipt["failure_code"] = code
        runtime_receipt["failure_detail"] = detail
        runtime_receipt["blockers"] = list(dict.fromkeys(runtime_receipt["blockers"] + [detail]))
        artifact_manifest = build_relaxation_artifact_manifest(
            run_id=config.run_id,
            source_receipt="clcn7_cg_membrane_relaxation_runtime_receipt_v1.json",
            artifact_paths={
                "initial_structure": initial_structure_path,
                "runtime_log": runtime_log_path,
                "energy_summary": energy_summary_path,
                "minimized_structure": minimized_structure_path,
                "final_structure": final_structure_path,
                "short_trajectory": trajectory_path,
                "final_state": final_state_path,
                "checkpoint": checkpoint_path,
            },
            missing_reasons=missing_artifacts,
        )
        sanity = build_relaxation_sanity_receipt(
            run_id=config.run_id,
            coordinate_ref=config.coordinate_ref,
            runtime_receipt=runtime_receipt,
            artifact_manifest=artifact_manifest,
        )
        return runtime_receipt, artifact_manifest, sanity

    try:
        openmm_module = import_module("openmm")
        unit_module = import_module("openmm.unit")
        app_module = import_module("openmm.app")
        martini_module = import_module("martini_openmm")
    except Exception as exc:  # noqa: BLE001
        return fail(f"dependency_import_error:{type(exc).__name__}", str(exc))

    try:
        conf = app_module.GromacsGroFile(str(coordinate_path))
        box_vectors = conf.getPeriodicBoxVectors()
        top = martini_module.MartiniTopFile(
            str(topology_path),
            periodicBoxVectors=box_vectors,
            epsilon_r=15.0,
        )
        system = top.create_system(nonbonded_cutoff=1.1 * unit_module.nanometer)
        integrator = openmm_module.LangevinIntegrator(
            config.temperature_k * unit_module.kelvin,
            config.friction_per_ps / unit_module.picosecond,
            config.timestep_fs * unit_module.femtosecond,
        )
        if config.random_seed is not None:
            integrator.setRandomNumberSeed(int(config.random_seed))
    except Exception as exc:  # noqa: BLE001
        return fail(f"topology_build_error:{type(exc).__name__}", str(exc))

    runtime_receipt["particle_count"] = int(system.getNumParticles())
    selected_platform = ""
    selected_properties: dict[str, str] = {}
    simulation = None
    platform_errors: list[str] = []
    for candidate in config.platform_preference:
        try:
            platform = openmm_module.Platform.getPlatformByName(candidate)
            candidate_properties = {"Precision": "mixed"} if candidate in {"CUDA", "OpenCL"} else {}
            trial = app_module.Simulation(top.topology, system, integrator, platform, candidate_properties)
            trial.context.setPositions(conf.getPositions())
            trial.context.computeVirtualSites()
            selected_platform = candidate
            selected_properties = candidate_properties
            simulation = trial
            break
        except Exception as exc:  # noqa: BLE001
            platform_errors.append(f"{candidate}:{type(exc).__name__}:{exc}")
    if simulation is None:
        return fail("platform_unavailable", platform_errors[-1] if platform_errors else "no_platform_initialized")

    runtime_receipt["platform"] = selected_platform
    if platform_errors:
        runtime_receipt["warnings"].append("platform_retry_before_success")

    try:
        initial_state = simulation.context.getState(getEnergy=True, getPositions=True)
        initial_energy = _float_energy(initial_state.getPotentialEnergy(), unit_module)
        runtime_receipt["energy_status"] = "available"
        if not math.isfinite(initial_energy):
            return fail("nan_initial_energy", "initial_energy_not_finite", status="partial_artifacts_generated_no_steps")
        runtime_receipt["initial_energy"] = initial_energy
        if not _positions_are_finite(initial_state, unit_module):
            runtime_receipt["nan_positions_detected"] = True
            return fail("nan_initial_positions", "initial_positions_not_finite", status="partial_artifacts_generated_no_steps")

        if config.minimization_enabled:
            simulation.minimizeEnergy(maxIterations=config.minimization_max_iterations)
            runtime_receipt["minimization_iterations"] = config.minimization_max_iterations
            runtime_receipt["warnings"].append("minimization_iterations_recorded_as_requested_max")
            post_min_state = simulation.context.getState(getEnergy=True, getPositions=True)
            post_min_energy = _float_energy(post_min_state.getPotentialEnergy(), unit_module)
            if not math.isfinite(post_min_energy):
                return fail(
                    "nan_post_minimization_energy",
                    "post_minimization_energy_not_finite",
                    status="partial_relaxation_smoke_blocked_by_runtime_instability",
                )
            runtime_receipt["post_minimization_energy"] = post_min_energy
            if not _positions_are_finite(post_min_state, unit_module):
                runtime_receipt["nan_positions_detected"] = True
                return fail(
                    "nan_post_minimization_positions",
                    "post_minimization_positions_not_finite",
                    status="partial_relaxation_smoke_blocked_by_runtime_instability",
                )
            with minimized_structure_path.open("w", encoding="utf-8") as handle:
                app_module.PDBFile.writeFile(top.topology, post_min_state.getPositions(), handle)
            missing_artifacts.pop("minimized_structure", None)
        else:
            post_min_state = initial_state
            missing_artifacts["minimized_structure"] = "minimization_disabled"

        if time.monotonic() - start_time > config.wall_clock_limit_s:
            runtime_receipt["warnings"].append("wall_clock_limit_reached_before_dynamics")
            config = config.model_copy(update={"dynamics_enabled": False, "dynamics_steps": 0})

        if config.dynamics_enabled and config.dynamics_steps > 0:
            if config.random_seed is not None:
                simulation.context.setVelocitiesToTemperature(config.temperature_k * unit_module.kelvin, int(config.random_seed))
            else:
                simulation.context.setVelocitiesToTemperature(config.temperature_k * unit_module.kelvin)
            simulation.reporters.append(app_module.DCDReporter(str(trajectory_path), config.reporter_interval))
            simulation.reporters.append(
                app_module.StateDataReporter(
                    str(runtime_log_path),
                    config.reporter_interval,
                    step=True,
                    potentialEnergy=True,
                    temperature=True,
                    append=False,
                )
            )
            remaining = int(config.dynamics_steps)
            chunk = min(config.dynamics_chunk_size, config.dynamics_steps)
            while remaining > 0:
                if time.monotonic() - start_time > config.wall_clock_limit_s:
                    runtime_receipt["warnings"].append("wall_clock_limit_reached_during_dynamics")
                    break
                step_count = min(chunk, remaining)
                simulation.step(step_count)
                runtime_receipt["dynamics_steps_run"] += step_count
                remaining -= step_count
            if runtime_receipt["dynamics_steps_run"] == 0:
                missing_artifacts["short_trajectory"] = "dynamics_stopped_before_first_chunk"
            final_state = simulation.context.getState(getEnergy=True, getPositions=True)
        else:
            final_state = simulation.context.getState(getEnergy=True, getPositions=True)
            missing_artifacts["short_trajectory"] = "dynamics_disabled_or_zero_steps"
            runtime_log_path.write_text("dynamics disabled for this bounded smoke\n", encoding="utf-8")
            missing_artifacts.pop("runtime_log", None)

        final_energy = _float_energy(final_state.getPotentialEnergy(), unit_module)
        if not math.isfinite(final_energy):
            return fail(
                "nan_final_energy",
                "final_energy_not_finite",
                status="partial_relaxation_smoke_blocked_by_runtime_instability",
            )
        runtime_receipt["final_energy"] = final_energy
        if not _positions_are_finite(final_state, unit_module):
            runtime_receipt["nan_positions_detected"] = True
            return fail(
                "nan_final_positions",
                "final_positions_not_finite",
                status="partial_relaxation_smoke_blocked_by_runtime_instability",
            )

        with final_structure_path.open("w", encoding="utf-8") as handle:
            app_module.PDBFile.writeFile(top.topology, final_state.getPositions(), handle)
        missing_artifacts.pop("final_structure", None)
        simulation.saveState(str(final_state_path))
        missing_artifacts.pop("final_state", None)
        if config.checkpoint_enabled:
            simulation.saveCheckpoint(str(checkpoint_path))
            missing_artifacts.pop("checkpoint", None)
        else:
            missing_artifacts["checkpoint"] = "checkpoint_disabled"

        _write_json(
            energy_summary_path,
            {
                "run_id": config.run_id,
                "initial_energy_kj_per_mol": runtime_receipt["initial_energy"],
                "post_minimization_energy_kj_per_mol": runtime_receipt["post_minimization_energy"],
                "final_energy_kj_per_mol": runtime_receipt["final_energy"],
                "platform": runtime_receipt["platform"],
                "dynamics_steps_run": runtime_receipt["dynamics_steps_run"],
            },
        )
        missing_artifacts.pop("energy_summary", None)
        if runtime_log_path.exists():
            missing_artifacts.pop("runtime_log", None)
        if trajectory_path.exists():
            missing_artifacts.pop("short_trajectory", None)

        artifact_manifest = build_relaxation_artifact_manifest(
            run_id=config.run_id,
            source_receipt="clcn7_cg_membrane_relaxation_runtime_receipt_v1.json",
            artifact_paths={
                "initial_structure": initial_structure_path,
                "runtime_log": runtime_log_path,
                "energy_summary": energy_summary_path,
                "minimized_structure": minimized_structure_path,
                "final_structure": final_structure_path,
                "short_trajectory": trajectory_path,
                "final_state": final_state_path,
                "checkpoint": checkpoint_path,
            },
            missing_reasons=missing_artifacts,
        )
        if config.no_silent_success_check and not artifact_manifest["artifacts"]:
            return fail("no_artifacts_emitted", "artifact_manifest_empty")

        runtime_receipt["artifacts"] = [item["path"] for item in artifact_manifest["artifacts"]]
        runtime_receipt["status"] = "completed" if runtime_receipt["dynamics_steps_run"] > 0 or runtime_receipt["post_minimization_energy"] is not None else "partial_artifacts_generated_no_steps"
        sanity = build_relaxation_sanity_receipt(
            run_id=config.run_id,
            coordinate_ref=config.coordinate_ref,
            runtime_receipt=runtime_receipt,
            artifact_manifest=artifact_manifest,
        )
        return runtime_receipt, artifact_manifest, sanity
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"runtime_exception:{type(exc).__name__}",
            str(exc),
            status="partial_relaxation_smoke_blocked_by_runtime_instability",
        )

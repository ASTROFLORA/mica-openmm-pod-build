from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mica.config.dotenv_loader import materialize_google_credentials_from_env, seed_env_from_dotenv
from mica.infrastructure.orchestration.salad_gcs_orchestrator import SaladMDJobConfig
import mica.storage.compute_durability as compute_durability
import mica.storage.gcs_user_storage as gcs_user_storage
from mica.unified_compute_client import _sanitize_remote_md_pdb_bytes

_PAPER_DODECAEDRICA_SAVING_INTERVAL_SECONDS = 180


class SaladMDContractError(ValueError):
    """Raised when the authoritative Salad+GCS submit contract cannot be prepared."""


@dataclass(frozen=True)
class SaladMDSubmitRequest:
    user_id: str
    job_id: str
    pdb_path: str = ""
    pdb_gcs_path: str | None = None
    coordinate_path: str = ""
    coordinate_gcs_path: str | None = None
    topology_path: str = ""
    topology_gcs_path: str | None = None
    preprocessed_topology_path: str = ""
    preprocessed_topology_gcs_path: str | None = None
    source_target_id: str = ""
    runtime_bundle_id: str = ""
    steps: int = 50_000_000
    gpu_type: str = "L40S"
    max_total_cost_usd: float = 50.0
    execution_class: str = "research"
    production_ns: float = 100.0
    simulation_mode: str = ""
    worker_mode: str = ""
    ligand_smiles: str = ""
    docked_ligand_pdb: str = ""
    benchmark_steps: int = 5000
    report_freq: int = 500
    saving_interval_seconds: int = 600
    chunk_steps_override: int = 0
    frame_interval_ps: float = 50.0


@dataclass(frozen=True)
class PreparedSaladMDSubmission:
    user_id: str
    job_id: str
    bucket_name: str
    storage_prefix: str
    input_prefix: str
    output_gcs_prefix: str
    pdb_gcs_path: str
    staged_inputs: dict[str, str]
    env_extra: dict[str, str]
    job_cfg: SaladMDJobConfig


@dataclass(frozen=True)
class SaladMDSubmissionOutcome:
    request: SaladMDSubmitRequest
    prepared: PreparedSaladMDSubmission
    result: Any


def _required_local_path(raw_path: str, field_name: str) -> Path:
    candidate = Path(str(raw_path or "").strip())
    if not candidate.exists():
        raise SaladMDContractError(
            f"Salad provider requires an existing local {field_name}: {raw_path}"
        )
    return candidate


def _coerce_local_path(raw_path: str, field_name: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise SaladMDContractError(f"Salad provider requires {field_name}")
    if text.startswith("file://"):
        parsed = urlparse(text)
        path_text = unquote(parsed.path or "")
        if parsed.netloc and not path_text.startswith("/"):
            path_text = f"/{path_text}"
        text = path_text.lstrip("/") if os.name == "nt" and path_text.startswith("/") and len(path_text) > 2 and path_text[2] == ":" else path_text
    return _required_local_path(text, field_name)


def _stage_bytes_input(
    *,
    storage: Any,
    user_id: str,
    object_path: str,
    source_path: Path,
    content_type: str,
) -> str:
    return storage.upload_bytes(
        user_id=user_id,
        object_path=object_path,
        data=source_path.read_bytes(),
        content_type=content_type,
    )


def _stage_optional_local_or_gcs_input(
    *,
    storage: Any,
    user_id: str,
    local_path: str,
    gcs_path: str | None,
    object_path: str,
    field_name: str,
    content_type: str,
) -> str:
    staged_gcs_path = str(gcs_path or "").strip()
    if not staged_gcs_path:
        local_text = str(local_path or "").strip()
        if local_text.startswith("gs://"):
            staged_gcs_path = local_text
    if staged_gcs_path:
        return staged_gcs_path
    local_source = _coerce_local_path(local_path, field_name)
    return _stage_bytes_input(
        storage=storage,
        user_id=user_id,
        object_path=object_path,
        source_path=local_source,
        content_type=content_type,
    )


def _gcs_parent_prefix(raw_path: str) -> str:
    text = str(raw_path or "").strip()
    if not text.startswith("gs://"):
        return ""
    without_scheme = text[5:]
    if "/" not in without_scheme:
        raise SaladMDContractError(f"Invalid GCS URI for Martini bundle input: {text}")
    bucket, object_name = without_scheme.split("/", 1)
    if not bucket or not object_name:
        raise SaladMDContractError(f"Invalid GCS URI for Martini bundle input: {text}")
    parent = object_name.rsplit("/", 1)[0] if "/" in object_name else ""
    if not parent:
        raise SaladMDContractError(
            f"Martini bundle GCS inputs must live under a prefix, got root object: {text}"
        )
    return f"gs://{bucket}/{parent}"


def _resolve_cg_bundle_prefix(
    *,
    default_prefix: str,
    coordinate_gcs_path: str,
    topology_gcs_path: str,
    preprocessed_topology_gcs_path: str,
) -> str:
    candidates = [
        _gcs_parent_prefix(path)
        for path in (
            coordinate_gcs_path,
            topology_gcs_path,
            preprocessed_topology_gcs_path,
        )
        if str(path or "").strip().startswith("gs://")
    ]
    if not candidates:
        return default_prefix
    first = candidates[0]
    mismatches = [candidate for candidate in candidates[1:] if candidate != first]
    if mismatches:
        raise SaladMDContractError(
            "Martini CG bundle GCS refs must share one common prefix for worker download."
        )
    return first


def _resolve_cg_input_object_name(*, bundle_prefix: str, gcs_path: str, fallback_name: str) -> str:
    text = str(gcs_path or "").strip()
    if not text.startswith("gs://"):
        return f"{bundle_prefix}/{fallback_name}".rstrip("/")
    if not text.startswith(f"{bundle_prefix}/"):
        raise SaladMDContractError(
            f"Martini CG bundle input {text} is outside the declared worker prefix {bundle_prefix}"
        )
    return text[len(f"{bundle_prefix}/") :]


_TOPOLOGY_INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"')


def _default_martini_include_roots() -> tuple[Path, ...]:
    repo_root = Path(__file__).resolve().parents[4]
    candidates = (
        repo_root / ".mica" / "external" / "M3-Lipid-Parameters" / "ITPs",
        repo_root / ".mica" / "programs" / "QUETZAL_SUPERNOVA" / "runtime_audits" / "clcn7_membrane_model_20260527" / "real_membrane_artifacts_v1",
        repo_root / ".mica" / "tmp" / "clcn7_membrane_probe",
    )
    return tuple(path for path in candidates if path.exists())


def _resolve_topology_include_path(include_name: str, *, owner_dir: Path) -> Path | None:
    candidate = (owner_dir / include_name).resolve()
    if candidate.exists():
        return candidate
    for search_root in _default_martini_include_roots():
        external_candidate = (search_root / include_name).resolve()
        if external_candidate.exists():
            return external_candidate
        basename_candidate = (search_root / Path(include_name).name).resolve()
        if basename_candidate.exists():
            return basename_candidate
    return None


def _collect_local_topology_include_closure(topology_path: Path) -> dict[str, Path]:
    staged: dict[str, Path] = {topology_path.name: topology_path.resolve()}
    pending: list[tuple[str, Path]] = [(topology_path.name, topology_path.resolve())]
    while pending:
        _label, current_path = pending.pop()
        for line in current_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _TOPOLOGY_INCLUDE_RE.match(line)
            if not match:
                continue
            include_name = match.group(1).strip()
            if not include_name:
                continue
            if include_name in staged:
                continue
            resolved = _resolve_topology_include_path(include_name, owner_dir=current_path.parent)
            if resolved is None:
                raise SaladMDContractError(
                    f"Salad CG runtime bundle staging could not resolve topology include '{include_name}' from {current_path}"
                )
            staged[include_name] = resolved
            pending.append((include_name, resolved))
    return staged


def _resolve_gcs_credentials_b64() -> str:
    seed_env_from_dotenv()
    materialize_google_credentials_from_env()

    creds_b64 = (os.getenv("SALAD_GCS_CREDENTIALS_B64") or "").strip()
    if creds_b64:
        return creds_b64

    creds_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not creds_path:
        return ""
    try:
        return base64.b64encode(Path(creds_path).read_bytes()).decode("utf-8")
    except OSError:
        return ""


def prepare_salad_md_submission(request: SaladMDSubmitRequest) -> PreparedSaladMDSubmission:
    bucket_name = compute_durability.compute_user_bucket_name(request.user_id)
    storage_prefix = compute_durability.canonical_compute_storage_prefix(
        lane="remote_md",
        job_id=request.job_id,
    )
    output_gcs_prefix = f"gs://{bucket_name}/{storage_prefix}"
    input_prefix = f"{storage_prefix}/input"

    simulation_mode = request.simulation_mode.strip().lower()
    worker_mode = request.worker_mode.strip().lower()
    cg_martini_requested = worker_mode == "cg_martini" or simulation_mode == "cg_martini" or any(
        (
            str(request.coordinate_path or "").strip(),
            str(request.coordinate_gcs_path or "").strip(),
            str(request.topology_path or "").strip(),
            str(request.topology_gcs_path or "").strip(),
            str(request.preprocessed_topology_path or "").strip(),
            str(request.preprocessed_topology_gcs_path or "").strip(),
        )
    )
    paper_dodecaedrica_requested = simulation_mode in {
        "paper_dodecaedrica",
        "runcomplex_paper_dodecaedrica",
        "osr1_paper_dodecaedrica",
    }
    complex_stability_requested = (
        simulation_mode == "complex_stability"
        or bool(request.ligand_smiles.strip())
        or bool(request.docked_ligand_pdb.strip())
    )
    if complex_stability_requested and not (
        request.ligand_smiles.strip() and request.docked_ligand_pdb.strip()
    ):
        raise SaladMDContractError(
            "Salad complex_stability requires both ligand_smiles and docked_ligand_pdb"
        )

    storage = gcs_user_storage.get_storage_manager()
    staged_inputs: dict[str, str] = {}
    env_extra: dict[str, str] = {}
    pdb_gcs_path = (request.pdb_gcs_path or "").strip()

    if cg_martini_requested:
        default_cg_bundle_prefix = f"{input_prefix}/cg_bundle"
        coordinate_gcs_path = _stage_optional_local_or_gcs_input(
            storage=storage,
            user_id=request.user_id,
            local_path=request.coordinate_path,
            gcs_path=request.coordinate_gcs_path,
            object_path=f"{default_cg_bundle_prefix}/coordinates.gro",
            field_name="coordinate_path",
            content_type="chemical/x-gro",
        )
        topology_local_path = None
        topology_raw = str(request.topology_path or "").strip()
        if topology_raw and not topology_raw.startswith("gs://"):
            topology_local_path = _coerce_local_path(topology_raw, "topology_path")
        topology_gcs_path = _stage_optional_local_or_gcs_input(
            storage=storage,
            user_id=request.user_id,
            local_path=topology_raw,
            gcs_path=request.topology_gcs_path,
            object_path=f"{default_cg_bundle_prefix}/{Path(topology_raw or 'system.top').name}",
            field_name="topology_path",
            content_type="text/plain",
        )
        preprocessed_local_path = None
        preprocessed_raw = str(request.preprocessed_topology_path or "").strip()
        if preprocessed_raw and not preprocessed_raw.startswith("gs://"):
            preprocessed_local_path = _coerce_local_path(
                preprocessed_raw,
                "preprocessed_topology_path",
            )
        preprocessed_topology_gcs_path = _stage_optional_local_or_gcs_input(
            storage=storage,
            user_id=request.user_id,
            local_path=preprocessed_raw,
            gcs_path=request.preprocessed_topology_gcs_path,
            object_path=f"{default_cg_bundle_prefix}/{Path(preprocessed_raw or 'preprocessed.top').name}",
            field_name="preprocessed_topology_path",
            content_type="text/plain",
        )
        cg_bundle_prefix = _resolve_cg_bundle_prefix(
            default_prefix=default_cg_bundle_prefix,
            coordinate_gcs_path=coordinate_gcs_path,
            topology_gcs_path=topology_gcs_path,
            preprocessed_topology_gcs_path=preprocessed_topology_gcs_path,
        )
        coordinate_object_name = _resolve_cg_input_object_name(
            bundle_prefix=cg_bundle_prefix,
            gcs_path=coordinate_gcs_path,
            fallback_name="coordinates.gro",
        )
        topology_basename = (topology_local_path or Path(topology_gcs_path)).name
        topology_object_name = _resolve_cg_input_object_name(
            bundle_prefix=cg_bundle_prefix,
            gcs_path=topology_gcs_path,
            fallback_name=topology_basename,
        )
        preprocessed_basename = (preprocessed_local_path or Path(preprocessed_topology_gcs_path)).name
        preprocessed_object_name = _resolve_cg_input_object_name(
            bundle_prefix=cg_bundle_prefix,
            gcs_path=preprocessed_topology_gcs_path,
            fallback_name=preprocessed_basename,
        )
        staged_inputs.update(
            {
                "cg_coordinate": coordinate_gcs_path,
                "cg_topology": topology_gcs_path,
                "cg_preprocessed_topology": preprocessed_topology_gcs_path,
            }
        )
        if topology_local_path is not None and preprocessed_local_path is not None:
            include_closure = _collect_local_topology_include_closure(preprocessed_local_path)
            include_closure.update(_collect_local_topology_include_closure(topology_local_path))
            for include_name, include_path in sorted(include_closure.items()):
                staged_inputs[f"cg_include:{include_name}"] = _stage_bytes_input(
                    storage=storage,
                    user_id=request.user_id,
                    object_path=f"{cg_bundle_prefix}/{include_name}",
                    source_path=include_path,
                    content_type="text/plain",
                )
        env_extra = {
            "SIMULATION_MODE": "cg_martini",
            "MICA_WORKER_MODE": "cg_martini",
            "CG_INPUT_PREFIX": cg_bundle_prefix,
            "CG_COORDINATE_GCS_OBJECT": f"{cg_bundle_prefix}/{coordinate_object_name}",
            "CG_TOPOLOGY_GCS_OBJECT": f"{cg_bundle_prefix}/{topology_object_name}",
            "CG_PREPROCESSED_TOPOLOGY_GCS_OBJECT": f"{cg_bundle_prefix}/{preprocessed_object_name}",
            "MICA_SOURCE_TARGET_ID": str(request.source_target_id or request.job_id or "").strip(),
            "MICA_CG_RUNTIME_BUNDLE_ID": str(request.runtime_bundle_id or request.job_id or "").strip(),
        }
        pdb_gcs_path = coordinate_gcs_path
    else:
        if not pdb_gcs_path:
            if not request.pdb_path.strip():
                raise SaladMDContractError(
                    "Salad provider requires pdb_gcs_path (gs:// URI) or a valid pdb_path for auto-staging"
                )
            protein_object_path = f"{input_prefix}/protein.pdb"
            protein_bytes = _sanitize_remote_md_pdb_bytes(
                _required_local_path(request.pdb_path, "pdb_path").read_bytes()
            )
            pdb_gcs_path = storage.upload_bytes(
                user_id=request.user_id,
                object_path=protein_object_path,
                data=protein_bytes,
                content_type="chemical/x-pdb",
            )
        staged_inputs["protein"] = pdb_gcs_path

    if paper_dodecaedrica_requested:
        env_extra = {
            "SIMULATION_MODE": "paper_dodecaedrica",
            "MICA_WORKER_MODE": "paper_dodecaedrica",
            "MICA_MD_PROCESSOR": "runcomplex_paper_dodecaedrica.py",
            "PRODUCTION_NS": str(request.production_ns),
            "PREPARE_SYSTEM": "true",
        }
    elif complex_stability_requested:
        ligand_object_path = f"{input_prefix}/docked_ligand.pdb"
        ligand_gcs_path = storage.upload_bytes(
            user_id=request.user_id,
            object_path=ligand_object_path,
            data=_required_local_path(request.docked_ligand_pdb, "docked_ligand_pdb").read_bytes(),
            content_type="chemical/x-pdb",
        )
        staged_inputs["docked_ligand"] = ligand_gcs_path
        env_extra = {
            "SIMULATION_MODE": "complex_stability",
            "LIGAND_SMILES": request.ligand_smiles.strip(),
            "DOCKED_LIGAND_GCS_BUCKET": bucket_name,
            "DOCKED_LIGAND_GCS_OBJECT": ligand_object_path,
            "PRODUCTION_NS": str(request.production_ns),
        }

    cfg_kwargs: dict[str, Any] = {
        "pdb_gcs_path": pdb_gcs_path,
        "output_gcs_prefix": output_gcs_prefix,
        "max_steps": request.steps,
        "benchmark_steps": request.benchmark_steps,
        "report_freq": request.report_freq,
        "saving_interval_seconds": request.saving_interval_seconds,
        "gpu_type_str": request.gpu_type,
        "job_id": request.job_id,
        "estimated_cost_usd": request.max_total_cost_usd,
        "execution_class": request.execution_class,
    }
    creds_b64 = _resolve_gcs_credentials_b64()
    if creds_b64:
        cfg_kwargs["gcs_credentials_b64"] = creds_b64
    if env_extra:
        cfg_kwargs["env_extra"] = env_extra
    if request.chunk_steps_override > 0:
        cfg_kwargs.setdefault("env_extra", {})
        cfg_kwargs["env_extra"]["CHUNK_STEPS_OVERRIDE"] = str(int(request.chunk_steps_override))
    cfg_kwargs.setdefault("env_extra", {})
    cfg_kwargs["env_extra"]["FRAME_INTERVAL_PS"] = str(float(request.frame_interval_ps))
    if paper_dodecaedrica_requested:
        cfg_kwargs["saving_interval_seconds"] = _PAPER_DODECAEDRICA_SAVING_INTERVAL_SECONDS

    return PreparedSaladMDSubmission(
        user_id=request.user_id,
        job_id=request.job_id,
        bucket_name=bucket_name,
        storage_prefix=storage_prefix,
        input_prefix=input_prefix,
        output_gcs_prefix=output_gcs_prefix,
        pdb_gcs_path=pdb_gcs_path,
        staged_inputs=staged_inputs,
        env_extra=env_extra,
        job_cfg=SaladMDJobConfig(**cfg_kwargs),
    )


async def submit_prepared_salad_md_job(
    client: Any,
    prepared: PreparedSaladMDSubmission,
    *,
    user_id: str,
) -> Any:
    return await client.submit_md_job(
        prepared.job_cfg,
        user_id=user_id,
        preferred_provider="salad",
    )


async def submit_salad_md_job(client: Any, request: SaladMDSubmitRequest) -> SaladMDSubmissionOutcome:
    prepared = prepare_salad_md_submission(request)
    result = await submit_prepared_salad_md_job(client, prepared, user_id=request.user_id)
    return SaladMDSubmissionOutcome(request=request, prepared=prepared, result=result)

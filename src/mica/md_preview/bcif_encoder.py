from __future__ import annotations

import dataclasses
import hashlib
import math
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional


CONTENT_TYPES = {
    "bcif": "application/octet-stream",
    "binarycif": "application/octet-stream",
    "cif": "chemical/x-cif",
    "mmcif": "chemical/x-cif",
    "pdb": "chemical/x-pdb",
    "pdb_preview": "chemical/x-pdb",
}


@dataclasses.dataclass(frozen=True)
class PreviewEncodeResult:
    status: str
    format: str
    output_path: str
    size_bytes: int
    sha256: str
    content_type: str
    frame_index: int
    step: int
    time_ps: float
    failure_code: str = ""
    failure_detail: str = ""
    preview_not_canonical: bool = True
    bcif_preview_status: str = "degraded_or_not_implemented"
    fallback_event_format: str = "artifact_ref"
    encoder: str = ""
    encoder_version: str = ""
    mmcif_path: str = ""
    source_topology_ref: str = ""
    source_positions_ref: str = ""
    source_trajectory_ref: str = ""
    encode_ms: float = 0.0
    dropped: bool = False
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _content_type(format_name: str) -> str:
    return CONTENT_TYPES.get(str(format_name or "").lower(), "application/octet-stream")


def _file_facts(path: Path) -> tuple[int, str]:
    if not path.exists() or not path.is_file():
        return 0, ""
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def _count_topology_atoms(topology_ref: Any) -> Optional[int]:
    atoms = getattr(topology_ref, "atoms", None)
    if not callable(atoms):
        return None
    try:
        return sum(1 for _ in atoms())
    except Exception:
        return None


def _count_positions(positions: Any) -> Optional[int]:
    if positions is None:
        return None
    try:
        return len(positions)
    except Exception:
        return None


def _write_mmcif(topology_ref: Any, positions: Any, output_path: Path) -> None:
    from openmm.app import PDBxFile

    with output_path.open("w", encoding="utf-8") as handle:
        PDBxFile.writeFile(topology_ref, positions, handle)


def _fallback_result(
    *,
    status: str,
    frame_index: int,
    step: int,
    time_ps: float,
    failure_code: str,
    failure_detail: str,
    metadata: Dict[str, Any],
    encode_ms: float,
    fallback_path: Optional[Path] = None,
    fallback_format: str = "pdb",
    dropped: bool = False,
) -> PreviewEncodeResult:
    output_path = fallback_path if fallback_path and fallback_path.exists() else None
    size_bytes, sha256_value = _file_facts(output_path) if output_path else (0, "")
    return PreviewEncodeResult(
        status=status,
        format=fallback_format if output_path else "artifact_ref",
        output_path=str(output_path or ""),
        size_bytes=size_bytes,
        sha256=sha256_value,
        content_type=_content_type(fallback_format if output_path else "artifact_ref"),
        frame_index=int(frame_index),
        step=int(step),
        time_ps=float(time_ps),
        failure_code=failure_code,
        failure_detail=failure_detail,
        preview_not_canonical=True,
        bcif_preview_status="degraded_or_not_implemented" if not dropped else "dropped",
        fallback_event_format="artifact_ref",
        encoder="",
        encoder_version="",
        source_topology_ref=str(metadata.get("source_topology_ref") or ""),
        source_positions_ref=str(metadata.get("source_positions_ref") or ""),
        source_trajectory_ref=str(metadata.get("source_trajectory_ref") or ""),
        encode_ms=encode_ms,
        dropped=bool(dropped),
        metadata=dict(metadata),
    )


def encode_preview_frame(
    topology_ref: Any,
    positions: Any,
    frame_index: int,
    step: int,
    time_ps: float,
    output_format: str,
    output_path: str | Path,
    metadata: Dict[str, Any] | None = None,
) -> PreviewEncodeResult:
    """Encode one browser-preview frame without changing canonical trajectory custody."""
    started = time.perf_counter()
    metadata_dict = dict(metadata or {})
    requested_format = str(output_format or "bcif").strip().lower()
    target_path = Path(output_path)
    fallback_path_text = str(metadata_dict.get("fallback_pdb_path") or metadata_dict.get("pdb_preview_path") or "")
    fallback_path = Path(fallback_path_text) if fallback_path_text else None
    fallback_format = str(metadata_dict.get("preview_fallback_format") or "pdb").strip().lower() or "pdb"

    topology_atom_count = _count_topology_atoms(topology_ref)
    position_count = _count_positions(positions)
    if topology_atom_count is not None and position_count is not None and topology_atom_count != position_count:
        return _fallback_result(
            status="failed",
            frame_index=frame_index,
            step=step,
            time_ps=time_ps,
            failure_code="topology_positions_mismatch",
            failure_detail=f"topology_atoms={topology_atom_count}; positions={position_count}",
            metadata={**metadata_dict, "topology_atom_count": topology_atom_count, "position_count": position_count},
            encode_ms=(time.perf_counter() - started) * 1000.0,
            fallback_path=fallback_path,
            fallback_format=fallback_format,
        )

    if requested_format not in {"bcif", "binarycif", "cif", "mmcif"}:
        return _fallback_result(
            status="degraded",
            frame_index=frame_index,
            step=step,
            time_ps=time_ps,
            failure_code="unsupported_preview_format",
            failure_detail=requested_format,
            metadata=metadata_dict,
            encode_ms=(time.perf_counter() - started) * 1000.0,
            fallback_path=fallback_path,
            fallback_format=fallback_format,
        )

    max_encode_ms = float(metadata_dict.get("preview_max_encode_ms") or 30_000.0)
    max_payload_bytes = int(metadata_dict.get("preview_max_payload_bytes") or 8 * 1024 * 1024)
    timeout_seconds = max(1, int(math.ceil(max_encode_ms / 1000.0)))
    mmcif_path = target_path.with_suffix(".cif") if requested_format in {"bcif", "binarycif"} else target_path
    bcif_path = target_path.with_suffix(".bcif")

    try:
        _write_mmcif(topology_ref, positions, mmcif_path)
    except Exception as exc:
        return _fallback_result(
            status="degraded",
            frame_index=frame_index,
            step=step,
            time_ps=time_ps,
            failure_code="mmcif_write_failed",
            failure_detail=f"{type(exc).__name__}: {exc}",
            metadata=metadata_dict,
            encode_ms=(time.perf_counter() - started) * 1000.0,
            fallback_path=fallback_path,
            fallback_format=fallback_format,
        )

    if requested_format in {"cif", "mmcif"}:
        size_bytes, sha256_value = _file_facts(mmcif_path)
        status = "completed" if size_bytes > 0 else "failed"
        return PreviewEncodeResult(
            status=status,
            format="cif",
            output_path=str(mmcif_path if size_bytes > 0 else ""),
            size_bytes=size_bytes,
            sha256=sha256_value,
            content_type=_content_type("cif"),
            frame_index=int(frame_index),
            step=int(step),
            time_ps=float(time_ps),
            failure_code="" if size_bytes > 0 else "mmcif_empty_output",
            failure_detail="",
            preview_not_canonical=True,
            bcif_preview_status="degraded_or_not_implemented",
            fallback_event_format="artifact_ref",
            mmcif_path=str(mmcif_path if size_bytes > 0 else ""),
            source_topology_ref=str(metadata_dict.get("source_topology_ref") or ""),
            source_positions_ref=str(metadata_dict.get("source_positions_ref") or ""),
            source_trajectory_ref=str(metadata_dict.get("source_trajectory_ref") or ""),
            encode_ms=(time.perf_counter() - started) * 1000.0,
            metadata=metadata_dict,
        )

    encoder = shutil.which(str(metadata_dict.get("encoder_bin") or "cif2bcif"))
    if not encoder:
        size_bytes, sha256_value = _file_facts(mmcif_path)
        return PreviewEncodeResult(
            status="degraded",
            format="cif" if size_bytes > 0 else (fallback_format if fallback_path and fallback_path.exists() else "artifact_ref"),
            output_path=str(mmcif_path if size_bytes > 0 else (fallback_path or "")),
            size_bytes=size_bytes or (_file_facts(fallback_path)[0] if fallback_path and fallback_path.exists() else 0),
            sha256=sha256_value or (_file_facts(fallback_path)[1] if fallback_path and fallback_path.exists() else ""),
            content_type=_content_type("cif" if size_bytes > 0 else fallback_format),
            frame_index=int(frame_index),
            step=int(step),
            time_ps=float(time_ps),
            failure_code="cif2bcif_not_found",
            failure_detail="BinaryCIF encoder executable was not found on PATH.",
            preview_not_canonical=True,
            bcif_preview_status="degraded_or_not_implemented",
            fallback_event_format="artifact_ref",
            encoder="",
            mmcif_path=str(mmcif_path if size_bytes > 0 else ""),
            source_topology_ref=str(metadata_dict.get("source_topology_ref") or ""),
            source_positions_ref=str(metadata_dict.get("source_positions_ref") or ""),
            source_trajectory_ref=str(metadata_dict.get("source_trajectory_ref") or ""),
            encode_ms=(time.perf_counter() - started) * 1000.0,
            metadata=metadata_dict,
        )

    try:
        completed = subprocess.run(
            [encoder, str(mmcif_path), str(bcif_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return _fallback_result(
            status="degraded",
            frame_index=frame_index,
            step=step,
            time_ps=time_ps,
            failure_code="cif2bcif_failed",
            failure_detail=f"{type(exc).__name__}: {exc}",
            metadata={**metadata_dict, "mmcif_path": str(mmcif_path)},
            encode_ms=(time.perf_counter() - started) * 1000.0,
            fallback_path=fallback_path if fallback_path and fallback_path.exists() else mmcif_path,
            fallback_format=fallback_format if fallback_path and fallback_path.exists() else "cif",
        )

    size_bytes, sha256_value = _file_facts(bcif_path)
    encode_ms = (time.perf_counter() - started) * 1000.0
    if size_bytes <= 0:
        return _fallback_result(
            status="degraded",
            frame_index=frame_index,
            step=step,
            time_ps=time_ps,
            failure_code="cif2bcif_empty_output",
            failure_detail="BinaryCIF encoder produced an empty output file.",
            metadata={**metadata_dict, "mmcif_path": str(mmcif_path)},
            encode_ms=encode_ms,
            fallback_path=mmcif_path,
            fallback_format="cif",
        )
    if size_bytes > max_payload_bytes:
        return _fallback_result(
            status="dropped",
            frame_index=frame_index,
            step=step,
            time_ps=time_ps,
            failure_code="preview_payload_too_large",
            failure_detail=f"size_bytes={size_bytes}; max_payload_bytes={max_payload_bytes}",
            metadata={**metadata_dict, "bcif_path": str(bcif_path), "mmcif_path": str(mmcif_path)},
            encode_ms=encode_ms,
            fallback_path=fallback_path if fallback_path and fallback_path.exists() else mmcif_path,
            fallback_format=fallback_format if fallback_path and fallback_path.exists() else "cif",
            dropped=True,
        )

    return PreviewEncodeResult(
        status="completed",
        format="bcif",
        output_path=str(bcif_path),
        size_bytes=size_bytes,
        sha256=sha256_value,
        content_type=_content_type("bcif"),
        frame_index=int(frame_index),
        step=int(step),
        time_ps=float(time_ps),
        preview_not_canonical=True,
        bcif_preview_status="implemented",
        fallback_event_format="artifact_ref",
        encoder=Path(encoder).name,
        encoder_version="",
        mmcif_path=str(mmcif_path),
        source_topology_ref=str(metadata_dict.get("source_topology_ref") or ""),
        source_positions_ref=str(metadata_dict.get("source_positions_ref") or ""),
        source_trajectory_ref=str(metadata_dict.get("source_trajectory_ref") or ""),
        encode_ms=encode_ms,
        metadata={
            **metadata_dict,
            "encoder_stdout": (completed.stdout or "")[-1000:],
            "encoder_stderr": (completed.stderr or "")[-1000:],
        },
    )
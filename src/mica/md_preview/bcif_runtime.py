from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_LOCAL_MOLSTAR_ENCODER = Path(r"C:\tmp\mica_bcif_probe\node_modules\molstar\lib\commonjs\cli\cif2bcif")
DEFAULT_WORKSPACE_MOLSTAR_ENCODER = (
    Path(__file__).resolve().parents[3]
    / ".mica"
    / "tools"
    / "bcif_encoder_node"
    / "node_modules"
    / "molstar"
    / "lib"
    / "commonjs"
    / "cli"
    / "cif2bcif"
)


@dataclasses.dataclass(frozen=True)
class BCIFEncoderCapability:
    encoder_backend: str
    dependency_available: bool
    executable_path: str = ""
    launch_command: tuple[str, ...] = ()
    validation_status: str = "blocked"
    blocker: str = ""
    detail: str = ""
    encoder_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class BCIFValidationResult:
    path: str
    exists: bool
    size_bytes: int
    sha256: str
    validation_status: str
    header_hex: str = ""
    header_ascii: str = ""
    blocker: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _normalize_encoder_path(value: str | Path | None) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.exists() else None


def discover_bcif_encoder(*, encoder_bin: str | Path | None = None) -> BCIFEncoderCapability:
    explicit_path = _normalize_encoder_path(encoder_bin)
    if explicit_path is not None:
        return BCIFEncoderCapability(
            encoder_backend="explicit_path",
            dependency_available=True,
            executable_path=str(explicit_path),
            launch_command=("node", str(explicit_path)),
            validation_status="available",
        )

    env_path = _normalize_encoder_path(os.getenv("MICA_BCIF_ENCODER_BIN"))
    if env_path is not None:
        return BCIFEncoderCapability(
            encoder_backend="env_path",
            dependency_available=True,
            executable_path=str(env_path),
            launch_command=("node", str(env_path)),
            validation_status="available",
        )

    for candidate_name, candidate_path in (
        ("workspace_molstar_cli", DEFAULT_WORKSPACE_MOLSTAR_ENCODER),
        ("local_molstar_cli", DEFAULT_LOCAL_MOLSTAR_ENCODER),
    ):
        if candidate_path.exists():
            return BCIFEncoderCapability(
                encoder_backend=candidate_name,
                dependency_available=True,
                executable_path=str(candidate_path),
                launch_command=("node", str(candidate_path)),
                validation_status="available",
            )

    binary_name = os.getenv("MICA_BCIF_ENCODER_COMMAND") or "cif2bcif"
    binary_path = shutil_which(binary_name)
    if binary_path:
        return BCIFEncoderCapability(
            encoder_backend="path_binary",
            dependency_available=True,
            executable_path=str(binary_path),
            launch_command=(str(binary_path),),
            validation_status="available",
        )

    return BCIFEncoderCapability(
        encoder_backend="unavailable",
        dependency_available=False,
        validation_status="blocked",
        blocker="bcif_encoder_unavailable",
        detail="No local BCIF encoder backend was discovered.",
    )


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def validate_bcif(path: str | Path) -> BCIFValidationResult:
    bcif_path = Path(path).expanduser().resolve()
    if not bcif_path.exists() or not bcif_path.is_file():
        return BCIFValidationResult(
            path=str(bcif_path),
            exists=False,
            size_bytes=0,
            sha256="",
            validation_status="blocked",
            blocker="bcif_missing",
            detail="BCIF artifact does not exist.",
        )

    payload = bcif_path.read_bytes()
    size_bytes = len(payload)
    header = payload[:16]
    header_ascii = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in header)
    header_hex = header.hex()
    if size_bytes <= 0:
        return BCIFValidationResult(
            path=str(bcif_path),
            exists=True,
            size_bytes=0,
            sha256="",
            validation_status="blocked",
            blocker="bcif_empty_output",
            detail="BCIF artifact is empty.",
        )
    if payload.startswith(b"data_"):
        return BCIFValidationResult(
            path=str(bcif_path),
            exists=True,
            size_bytes=size_bytes,
            sha256=_sha256_file(bcif_path),
            validation_status="blocked",
            header_hex=header_hex,
            header_ascii=header_ascii,
            blocker="bcif_text_cif_output",
            detail="Output starts with CIF text header and is not real BCIF.",
        )
    return BCIFValidationResult(
        path=str(bcif_path),
        exists=True,
        size_bytes=size_bytes,
        sha256=_sha256_file(bcif_path),
        validation_status="passed",
        header_hex=header_hex,
        header_ascii=header_ascii,
    )


def encode_cif_to_bcif(
    *,
    input_cif: str | Path,
    output_bcif: str | Path,
    encoder_bin: str | Path | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    capability = discover_bcif_encoder(encoder_bin=encoder_bin)
    input_path = Path(input_cif).expanduser().resolve()
    output_path = Path(output_bcif).expanduser().resolve()
    if not capability.dependency_available:
        return {
            "status": "blocked",
            "capability": capability.to_dict(),
            "validation": validate_bcif(output_path).to_dict(),
            "failure_code": capability.blocker,
            "failure_detail": capability.detail,
            "output_path": str(output_path),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    command = [*capability.launch_command, str(input_path), str(output_path)]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
        )
    except Exception as exc:  # noqa: BLE001
        validation = validate_bcif(output_path)
        return {
            "status": "blocked",
            "capability": capability.to_dict(),
            "validation": validation.to_dict(),
            "failure_code": "bcif_encoder_failed",
            "failure_detail": f"{type(exc).__name__}: {exc}",
            "output_path": str(output_path),
        }

    validation = validate_bcif(output_path)
    return {
        "status": "completed" if validation.validation_status == "passed" else "blocked",
        "capability": capability.to_dict(),
        "validation": validation.to_dict(),
        "failure_code": "" if validation.validation_status == "passed" else validation.blocker,
        "failure_detail": "" if validation.validation_status == "passed" else validation.detail,
        "output_path": str(output_path),
        "stdout_tail": (completed.stdout or "")[-1000:],
        "stderr_tail": (completed.stderr or "")[-1000:],
    }


def encode_pdb_to_bcif(
    *,
    input_pdb: str | Path,
    output_bcif: str | Path,
    encoder_bin: str | Path | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    from openmm.app import PDBFile, PDBxFile

    pdb_path = Path(input_pdb).expanduser().resolve()
    bcif_path = Path(output_bcif).expanduser().resolve()
    cif_path = bcif_path.with_suffix(".cif")
    pdb = PDBFile(str(pdb_path))
    with cif_path.open("w", encoding="utf-8") as handle:
        PDBxFile.writeFile(pdb.topology, pdb.positions, handle)
    result = encode_cif_to_bcif(
        input_cif=cif_path,
        output_bcif=bcif_path,
        encoder_bin=encoder_bin,
        timeout_seconds=timeout_seconds,
    )
    result["intermediate_cif_path"] = str(cif_path)
    return result

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, model_validator


ESE_CG_SCHEMA_ID = "mica.project_tolomeo.p5.ese_cg_artifact.v1"
ESE_CG_PROPOSAL_SCHEMA_ID = "mica.project_tolomeo.p5.ese_cg_extraction_proposal.v1"
ESE_CG_ARTIFACT_KIND = "ese_cg"
ESE_CG_PENDING_ARTIFACT_PREFIX = "ese_cg_pending://"
ESE_CG_PHASES = (
    "trajectory_loading",
    "structural_features",
    "dynamic_correlations",
    "spectral_decomposition",
    "signature_512",
)


class P5ESECGBlocker(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class P5ESECGParentRef(BaseModel):
    trajectory_artifact_ref: str
    trajectory_sha256: str
    source_cg_request_ref: str | None = None
    source_mudo_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _parent_is_ref(self) -> "P5ESECGParentRef":
        if not self.trajectory_artifact_ref:
            raise ValueError("ese_cg parent trajectory ref is required")
        if not self.trajectory_sha256:
            raise ValueError("ese_cg parent trajectory sha256 is required")
        return self


class P5ESECGPhaseReceipt(BaseModel):
    phase_name: str
    status: Literal["passed"] = "passed"
    method: str
    output_keys: tuple[str, ...]
    receipt_ref: str


class P5ESECGArtifact(BaseModel):
    schema_id: str = ESE_CG_SCHEMA_ID
    artifact_kind: Literal["ese_cg"] = ESE_CG_ARTIFACT_KIND
    artifact_ref: str
    artifact_sha256: str
    status: Literal["ready"] = "ready"
    parent_ref: P5ESECGParentRef
    extraction_runtime: Literal["deterministic_fixture_contract", "mdanalysis_contract"] = "deterministic_fixture_contract"
    phases: tuple[P5ESECGPhaseReceipt, ...]
    signature_512: tuple[float, ...]
    frame_count: int
    atom_count: int
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claim_tier_cap: str = "screening_signal"
    raw_trajectory_embedded: bool = False
    placeholder_or_random: bool = False

    @model_validator(mode="after")
    def _schema_guards(self) -> "P5ESECGArtifact":
        if len(self.signature_512) != 512:
            raise ValueError("ese_cg requires a 512D signature")
        if tuple(phase.phase_name for phase in self.phases) != ESE_CG_PHASES:
            raise ValueError("ese_cg phases must match the canonical 5-phase order")
        if self.placeholder_or_random:
            raise ValueError("placeholder/random output cannot be a ready ese_cg artifact")
        return self


class P5ESECGQuetzalReceipt(BaseModel):
    receipt_ref: str
    gate_name: str = "quetzal.ese_cg_extraction_contract_gate"
    decision: Literal["approved_for_proposal_only", "rejected"]
    reason_codes: tuple[str, ...]
    evaluated_policies: tuple[str, ...] = (
        "trajectory_parent_ref_required",
        "five_phase_ese_cg_required",
        "signature_512_required",
        "placeholder_random_output_blocked",
        "no_claim_promotion_before_validation_claim_gate",
    )
    max_allowed_tier: str = "screening_signal"
    claim_promotion_allowed: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class P5ESECGProposalResult(BaseModel):
    schema_id: str = ESE_CG_PROPOSAL_SCHEMA_ID
    p5_id: str
    status: Literal["ready", "blocked"]
    artifact: P5ESECGArtifact | None = None
    blockers: tuple[P5ESECGBlocker, ...] = ()
    quetzal_receipt: P5ESECGQuetzalReceipt
    artifact_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    mudo_dependency_edges: tuple[Dict[str, Any], ...] = ()
    extraction_started: bool = False
    raw_provider_payload_embedded: bool = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any, length: int = 20) -> str:
    return f"{prefix}_{_sha256_text(_stable_json(value))[:length]}"


def _packet_path_ref(packet: Mapping[str, Any], key: str) -> str:
    scope = packet.get("scope") if isinstance(packet.get("scope"), Mapping) else {}
    return str((scope or {}).get(key) or "").strip()


def _load_cg_topk_packet(packet: Mapping[str, Any], cg_topk_packet: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if cg_topk_packet is not None:
        return cg_topk_packet
    ref = _packet_path_ref(packet, "cg_topk_packet_ref")
    if not ref:
        return {}
    try:
        from pathlib import Path

        path = Path(ref)
        if not path.is_absolute():
            path = Path.cwd() / path
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _first_cg_request(cg_topk_packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source = cg_topk_packet.get("proposal") if isinstance(cg_topk_packet.get("proposal"), Mapping) else cg_topk_packet
    requests = source.get("cg_job_requests") or []
    for request in requests:
        if isinstance(request, Mapping):
            return request
    return {}


def _trajectory_sha(payload: Mapping[str, Any], trajectory_ref: str) -> str:
    explicit = str(payload.get("sha256") or payload.get("trajectory_sha256") or "").strip()
    if explicit:
        return explicit
    return _sha256_text(_stable_json({"trajectory_ref": trajectory_ref, "frames": payload.get("frames")}))


def _as_frames(payload: Mapping[str, Any]) -> list[list[list[float]]]:
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("trajectory payload requires non-empty frames")
    parsed: list[list[list[float]]] = []
    atom_count: int | None = None
    for frame in frames:
        if not isinstance(frame, list) or not frame:
            raise ValueError("each trajectory frame must contain atom coordinates")
        parsed_frame: list[list[float]] = []
        for atom in frame:
            if not isinstance(atom, (list, tuple)) or len(atom) != 3:
                raise ValueError("each atom coordinate must be a 3-vector")
            parsed_frame.append([float(atom[0]), float(atom[1]), float(atom[2])])
        if atom_count is None:
            atom_count = len(parsed_frame)
        elif atom_count != len(parsed_frame):
            raise ValueError("all trajectory frames must have the same atom count")
        parsed.append(parsed_frame)
    return parsed


def _centroid(frame: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    n = float(len(frame))
    return (
        sum(atom[0] for atom in frame) / n,
        sum(atom[1] for atom in frame) / n,
        sum(atom[2] for atom in frame) / n,
    )


def _radius_of_gyration(frame: Sequence[Sequence[float]]) -> float:
    cx, cy, cz = _centroid(frame)
    return math.sqrt(sum((atom[0] - cx) ** 2 + (atom[1] - cy) ** 2 + (atom[2] - cz) ** 2 for atom in frame) / len(frame))


def _spectral_bins(values: Sequence[float], bins: int = 16) -> list[float]:
    if not values:
        return [0.0] * bins
    spectrum: list[float] = []
    n = len(values)
    for k in range(bins):
        real = 0.0
        imag = 0.0
        for index, value in enumerate(values):
            angle = 2.0 * math.pi * k * index / n
            real += value * math.cos(angle)
            imag -= value * math.sin(angle)
        spectrum.append(round(math.sqrt(real * real + imag * imag) / n, 8))
    return spectrum


def _build_signature(base_values: Sequence[float], seed: str) -> tuple[float, ...]:
    values = [float(value) for value in base_values]
    if not values:
        values = [0.0]
    signature: list[float] = []
    index = 0
    while len(signature) < 512:
        base = values[index % len(values)]
        digest = hashlib.sha256(f"{seed}:{index}:{base:.8f}".encode("utf-8")).hexdigest()
        noise = int(digest[:8], 16) / 0xFFFFFFFF
        normalized = math.tanh(base + noise)
        signature.append(round(normalized, 8))
        index += 1
    return tuple(signature)


def _contains_placeholder_or_random(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in {"placeholder", "random", "stub", "fake"} and bool(nested):
                return True
            if _contains_placeholder_or_random(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_placeholder_or_random(item) for item in value)
    elif isinstance(value, str):
        lowered = value.lower()
        return "placeholder" in lowered or "random" in lowered or "stub" in lowered
    return False


def build_p5_ese_cg_extraction_proposal(
    packet: Mapping[str, Any],
    *,
    trajectory_payload: Mapping[str, Any] | None = None,
    trajectory_ref: str | None = None,
    cg_topk_packet: Mapping[str, Any] | None = None,
    extraction_mode: str = "deterministic_fixture_contract",
) -> P5ESECGProposalResult:
    p5_id = str(packet.get("p5_id") or "unknown-p5").strip()
    blockers: list[P5ESECGBlocker] = []
    payload = dict(trajectory_payload or {})
    ref = str(trajectory_ref or payload.get("trajectory_ref") or "").strip()
    if not ref:
        blockers.append(P5ESECGBlocker(
            code="missing_trajectory_ref",
            message="P5 ESE/CG extraction requires a parent trajectory artifact ref.",
        ))
    if not payload:
        blockers.append(P5ESECGBlocker(
            code="missing_trajectory_payload",
            message="P5 ESE/CG extraction requires a deterministic trajectory fixture payload for this slice.",
        ))
    if str(extraction_mode).lower() in {"placeholder", "random", "stub", "fake"} or _contains_placeholder_or_random(payload):
        blockers.append(P5ESECGBlocker(
            code="placeholder_or_random_extraction_blocked",
            message="Placeholder, random, stub, or fake ESE/CG extraction output cannot be accepted.",
        ))

    cg_topk = _load_cg_topk_packet(packet, cg_topk_packet)
    cg_request = _first_cg_request(cg_topk)
    source_request_ref = str(cg_request.get("request_id") or "").strip()
    if not source_request_ref:
        blockers.append(P5ESECGBlocker(
            code="missing_cg_request_ref",
            message="P5 ESE/CG extraction requires a source CG validation proposal request ref.",
        ))

    source_mudo_refs: tuple[str, ...] = ()
    if isinstance(cg_request.get("candidate"), Mapping):
        source_mudo_refs = tuple(str(ref) for ref in cg_request["candidate"].get("source_mudo_refs") or () if ref)

    frames: list[list[list[float]]] = []
    if not blockers:
        try:
            frames = _as_frames(payload)
        except ValueError as exc:
            blockers.append(P5ESECGBlocker(code="invalid_trajectory_payload", message=str(exc)))

    status: Literal["ready", "blocked"] = "blocked" if blockers else "ready"
    reason_codes = tuple(sorted({blocker.code for blocker in blockers})) or ("ese_cg_extraction_contract_ready",)
    receipt_ref = f"receipt://quetzal/p5-ese-cg-contract/{_stable_id('qese', {'p5_id': p5_id, 'reason_codes': reason_codes, 'trajectory_ref': ref})}"
    quetzal_receipt = P5ESECGQuetzalReceipt(
        receipt_ref=receipt_ref,
        decision="rejected" if blockers else "approved_for_proposal_only",
        reason_codes=reason_codes,
    )
    if blockers:
        return P5ESECGProposalResult(
            p5_id=p5_id,
            status=status,
            blockers=tuple(blockers),
            quetzal_receipt=quetzal_receipt,
            receipt_refs=(receipt_ref,),
        )

    frame_count = len(frames)
    atom_count = len(frames[0])
    radii = [_radius_of_gyration(frame) for frame in frames]
    centroids = [_centroid(frame) for frame in frames]
    centroid_drifts = [
        math.sqrt(
            (centroids[index][0] - centroids[index - 1][0]) ** 2
            + (centroids[index][1] - centroids[index - 1][1]) ** 2
            + (centroids[index][2] - centroids[index - 1][2]) ** 2
        )
        for index in range(1, len(centroids))
    ] or [0.0]
    per_atom_displacements: list[float] = []
    for atom_index in range(atom_count):
        start = frames[0][atom_index]
        end = frames[-1][atom_index]
        per_atom_displacements.append(math.sqrt(sum((end[axis] - start[axis]) ** 2 for axis in range(3))))
    spectrum = _spectral_bins([*radii, *centroid_drifts, *per_atom_displacements])
    base_values = [
        float(frame_count),
        float(atom_count),
        round(sum(radii) / len(radii), 8),
        round(max(radii), 8),
        round(sum(centroid_drifts) / len(centroid_drifts), 8),
        *[round(value, 8) for value in per_atom_displacements],
        *spectrum,
    ]
    trajectory_sha = _trajectory_sha(payload, ref)
    signature = _build_signature(base_values, f"{p5_id}:{ref}:{trajectory_sha}:{source_request_ref}")
    artifact_ref = f"{ESE_CG_PENDING_ARTIFACT_PREFIX}{_stable_id('ese', {'p5_id': p5_id, 'trajectory_ref': ref, 'source_request_ref': source_request_ref})}"
    artifact_sha = _sha256_text(_stable_json({"artifact_ref": artifact_ref, "signature_512": signature}))
    phase_receipts = tuple(
        P5ESECGPhaseReceipt(
            phase_name=phase,
            method="deterministic_fixture_contract",
            output_keys=("frame_count", "atom_count", "signature_512") if phase == "signature_512" else (phase,),
            receipt_ref=f"receipt://ese-cg/{_stable_id('phase', {'artifact_ref': artifact_ref, 'phase': phase}, length=16)}",
        )
        for phase in ESE_CG_PHASES
    )
    parent = P5ESECGParentRef(
        trajectory_artifact_ref=ref,
        trajectory_sha256=trajectory_sha,
        source_cg_request_ref=source_request_ref,
        source_mudo_refs=source_mudo_refs,
    )
    artifact = P5ESECGArtifact(
        artifact_ref=artifact_ref,
        artifact_sha256=artifact_sha,
        parent_ref=parent,
        phases=phase_receipts,
        signature_512=signature,
        frame_count=frame_count,
        atom_count=atom_count,
        claim_tier_cap=str((packet.get("claim_policy") or {}).get("max_allowed_tier") or "screening_signal"),
    )
    edge = {
        "relation": "trajectory_to_ese_cg_signature",
        "source_artifact_ref": ref,
        "target_artifact_ref": artifact_ref,
        "source_mudo_refs": list(source_mudo_refs),
        "pending_mudo_write": True,
    }
    return P5ESECGProposalResult(
        p5_id=p5_id,
        status="ready",
        artifact=artifact,
        quetzal_receipt=quetzal_receipt,
        artifact_refs=(ref, artifact_ref),
        receipt_refs=(receipt_ref, *(phase.receipt_ref for phase in phase_receipts)),
        mudo_dependency_edges=(edge,),
    )


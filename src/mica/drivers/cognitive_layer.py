from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mica.drivers.evidence_ledger import EvidenceEntry, EvidenceLedger


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _claim_strength_score(value: str) -> float:
    normalized = str(value or "").strip().lower()
    if normalized in {"supported", "observed"}:
        return 0.95
    if normalized in {"suggestive", "partial"}:
        return 0.65
    if normalized in {"contradicted"}:
        return 0.15
    return 0.30


def _entry_status(entry: EvidenceEntry | None, claim: Dict[str, Any]) -> str:
    if entry is not None and str(entry.status or "").strip():
        return str(entry.status)
    if list(claim.get("counterevidence_ids") or []):
        return "contradicted"
    return str(claim.get("strength") or "unsupported")


@dataclasses.dataclass(frozen=True)
class ACHVerdict:
    schema_version: str
    competition_open: bool
    primary_hypothesis_id: str
    leading_hypothesis_ids: List[str]
    rival_hypothesis_ids: List[str]
    rejected_hypothesis_ids: List[str]
    contradiction_pressure: float
    entries: List[Dict[str, Any]]
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class CriticVerdict:
    schema_version: str
    status: str
    challenged_claim_ids: List[str]
    contradicted_claim_ids: List[str]
    unsupported_critical_claim_ids: List[str]
    unresolved_rival_hypothesis_ids: List[str]
    retry_recommended: bool
    escalate_critique: bool
    rationale: List[str]
    retry_guidance: str
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return validate_critic_verdict(dataclasses.asdict(self))


class CriticVerdictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["mica.continuous_critic.v0"]
    status: Literal["accept", "challenge", "critical"]
    challenged_claim_ids: List[str] = Field(default_factory=list)
    contradicted_claim_ids: List[str] = Field(default_factory=list)
    unsupported_critical_claim_ids: List[str] = Field(default_factory=list)
    unresolved_rival_hypothesis_ids: List[str] = Field(default_factory=list)
    retry_recommended: bool = False
    escalate_critique: bool = False
    rationale: List[str] = Field(default_factory=list)
    retry_guidance: str = ""
    note: str = ""
    appeal_regime_state: Dict[str, Any] = Field(default_factory=dict)
    soft_repulsion_warnings: List[Dict[str, Any]] = Field(default_factory=list)
    negative_memory_review: Dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "challenged_claim_ids",
        "contradicted_claim_ids",
        "unsupported_critical_claim_ids",
        "unresolved_rival_hypothesis_ids",
        "rationale",
        mode="before",
    )
    @classmethod
    def _normalize_string_lists(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            value = [value]
        return [str(item).strip() for item in list(value or []) if str(item).strip()]

    @field_validator("retry_guidance", "note", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()


def validate_critic_verdict(payload: Mapping[str, Any] | CriticVerdict) -> Dict[str, Any]:
    if isinstance(payload, CriticVerdict):
        raw_payload = dataclasses.asdict(payload)
    else:
        raw_payload = dict(payload or {})
    contract = CriticVerdictContract.model_validate(raw_payload)
    return contract.model_dump(mode="python")


class ACHArbiter:
    def arbitrate(self, *, query: str, final_result: Dict[str, Any], ledger: EvidenceLedger) -> ACHVerdict:
        del query
        claims = [claim for claim in (final_result.get("claims") or []) if isinstance(claim, dict)]
        entries: List[Dict[str, Any]] = []

        for index, claim in enumerate(claims, start=1):
            claim_id = str(claim.get("claim_id") or f"claim-{index}")
            hypothesis_id = str(claim.get("hypothesis_id") or f"hypothesis-{claim_id}")
            ledger_entry = ledger.get_entry(claim_id)
            contradiction_refs = list(claim.get("counterevidence_ids") or [])
            if ledger_entry is not None:
                contradiction_refs = sorted(set(contradiction_refs + list(ledger_entry.negative_result_refs or [])))

            relevant_source_ids = list(claim.get("relevant_source_ids") or [])
            if ledger_entry is not None and not relevant_source_ids:
                relevant_source_ids = list(ledger_entry.relevant_source_ids or [])

            confidence = float(
                claim.get(
                    "confidence",
                    ledger_entry.algorithmic_confidence if ledger_entry is not None else 0.0,
                )
                or 0.0
            )
            strength_score = _claim_strength_score(_entry_status(ledger_entry, claim))
            support_bonus = min(1.0, len(relevant_source_ids) / 2.0)
            contradiction_penalty = min(1.0, len(contradiction_refs) / 2.0)
            score = _clamp((0.45 * strength_score) + (0.30 * confidence) + (0.20 * support_bonus) - (0.35 * contradiction_penalty))

            if contradiction_refs or strength_score <= 0.20:
                disposition = "rejected"
            elif score >= 0.72 and support_bonus > 0.0:
                disposition = "leading"
            else:
                disposition = "rival"

            entries.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "claim_id": claim_id,
                    "text": str(claim.get("text") or "").strip(),
                    "section": str(claim.get("section") or "finding"),
                    "score": round(score, 4),
                    "status": _entry_status(ledger_entry, claim),
                    "disposition": disposition,
                    "supporting_source_ids": sorted(set(relevant_source_ids)),
                    "contradicting_source_ids": sorted(set(str(ref) for ref in contradiction_refs if str(ref))),
                }
            )

        entries.sort(key=lambda entry: entry.get("score", 0.0), reverse=True)
        leading_ids = [entry["hypothesis_id"] for entry in entries if entry["disposition"] == "leading"]
        rival_ids = [entry["hypothesis_id"] for entry in entries if entry["disposition"] == "rival"]
        rejected_ids = [entry["hypothesis_id"] for entry in entries if entry["disposition"] == "rejected"]
        contradiction_pressure = round(
            (sum(1 for entry in entries if entry.get("contradicting_source_ids")) / len(entries)) if entries else 0.0,
            4,
        )

        primary = leading_ids[0] if leading_ids else entries[0]["hypothesis_id"] if entries else ""
        competition_open = len(rival_ids) > 0 or contradiction_pressure > 0.0 or len(leading_ids) > 1

        return ACHVerdict(
            schema_version="mica.ach_arbiter.v0",
            competition_open=competition_open,
            primary_hypothesis_id=primary,
            leading_hypothesis_ids=leading_ids,
            rival_hypothesis_ids=rival_ids,
            rejected_hypothesis_ids=rejected_ids,
            contradiction_pressure=contradiction_pressure,
            entries=entries,
            note="ACHArbiter keeps rival hypotheses explicit until contradiction pressure and evidence support settle competition.",
        )


class ContinuousCritic:
    def review(
        self,
        *,
        query: str,
        final_result: Dict[str, Any],
        ledger: EvidenceLedger,
        ach_state: Dict[str, Any],
    ) -> CriticVerdict:
        del query
        contradicted_claim_ids = [entry.claim_id for entry in ledger.get_contradicted_claims()]
        unsupported_critical_claim_ids = [entry.claim_id for entry in ledger.critical_unsupported_claims()]
        unresolved_rival_hypothesis_ids = list(ach_state.get("rival_hypothesis_ids") or [])
        challenged_claim_ids = sorted(set(contradicted_claim_ids + unsupported_critical_claim_ids))

        rationale: List[str] = []
        if contradicted_claim_ids:
            rationale.append(f"contradicted claims remain active: {contradicted_claim_ids}")
        if unsupported_critical_claim_ids:
            rationale.append(f"critical claims still lack cold support: {unsupported_critical_claim_ids}")
        if unresolved_rival_hypothesis_ids:
            rationale.append(f"rival hypotheses remain unresolved: {unresolved_rival_hypothesis_ids}")

        output_mode = str(final_result.get("output_mode") or "")
        retry_recommended = bool(challenged_claim_ids or unresolved_rival_hypothesis_ids)
        escalate_critique = bool(contradicted_claim_ids or (output_mode == "evidence_backed_answer" and unresolved_rival_hypothesis_ids))

        if contradicted_claim_ids:
            status = "critical"
        elif retry_recommended:
            status = "challenge"
        else:
            status = "accept"

        retry_guidance_lines = [
            "Compare the leading and rival hypotheses explicitly.",
            "Resolve contradicted evidence before presenting closure.",
            "Preserve critical entities and numbers while revising the synthesis.",
        ]
        if challenged_claim_ids:
            retry_guidance_lines.append(f"Re-evaluate claims: {challenged_claim_ids}.")

        return CriticVerdict(
            schema_version="mica.continuous_critic.v0",
            status=status,
            challenged_claim_ids=challenged_claim_ids,
            contradicted_claim_ids=contradicted_claim_ids,
            unsupported_critical_claim_ids=unsupported_critical_claim_ids,
            unresolved_rival_hypothesis_ids=unresolved_rival_hypothesis_ids,
            retry_recommended=retry_recommended,
            escalate_critique=escalate_critique,
            rationale=rationale,
            retry_guidance=" ".join(retry_guidance_lines),
            note="Continuous Critic re-checks the normalized claim set against the live evidence ledger before treating the synthesis as settled.",
        )
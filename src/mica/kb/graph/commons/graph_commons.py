from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class IssuerReputationProfile:
    issuer_profile_ref: str
    source_instance: str
    reputation_score: float
    trust_status: str
    signature_verified: bool
    prior_retractions: int
    poisoning_incidents: int
    local_promotions: int
    curator_endorsements: int
    reputation_tier: str
    anti_poisoning_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphCommonsPackage:
    commons_package_ref: str
    package_ref: str
    source_instance: str
    publication_scope: str
    edge_refs: tuple[str, ...]
    receipt_refs: tuple[str, ...]
    issuer_profile_ref: str
    federation_state: str
    provenance_policy_ref: str


@dataclass(frozen=True)
class CommonsAdmissionDecision:
    status: str
    decision_ref: str
    package_ref: str
    source_instance: str
    admission_policy_ref: str
    issuer_profile: IssuerReputationProfile
    anti_poisoning_flags: tuple[str, ...]
    reason_codes: tuple[str, ...]
    commons_package: GraphCommonsPackage | None = None
    created_by_receipt_ref: str | None = None


@dataclass(frozen=True)
class ExternalSubgraphPublication:
    publication_ref: str
    commons_package_ref: str
    package_ref: str
    source_instance: str
    publication_scope: str
    exported_edge_refs: tuple[str, ...]
    exported_receipt_refs: tuple[str, ...]
    blocked_edge_refs: tuple[str, ...]
    federation_state: str
    provenance_policy_ref: str


@dataclass(frozen=True)
class ExternalSubgraphPublicationDecision:
    status: str
    decision_ref: str
    commons_package_ref: str
    publication_policy_ref: str
    reason_codes: tuple[str, ...]
    publication: ExternalSubgraphPublication | None = None
    created_by_receipt_ref: str | None = None


@dataclass(frozen=True)
class LocalPromotionCandidate:
    candidate_ref: str
    commons_package_ref: str
    source_publication_ref: str
    package_ref: str
    source_instance: str
    target_scope: str
    candidate_edge_refs: tuple[str, ...]
    evidence_receipt_refs: tuple[str, ...]
    supporting_local_receipt_refs: tuple[str, ...]
    requested_by: str
    candidate_status: str
    provenance_policy_ref: str


@dataclass(frozen=True)
class LocalPromotionGateDecision:
    status: str
    decision_ref: str
    commons_package_ref: str
    promotion_policy_ref: str
    reason_codes: tuple[str, ...]
    candidate: LocalPromotionCandidate | None = None
    created_by_receipt_ref: str | None = None


class GraphCommonsRuntime:
    """G4.3 commons publication/admission over the federated import seam.

    This layer never replaces trust import. It only decides whether a signed,
    `external_asserted` package can be exposed as a public commons package.
    """

    admission_policy_ref = "graph_commons://g4p2/admission/v1"
    provenance_policy_ref = "graph_commons://g4p2/provenance/v1"
    publication_policy_ref = "graph_commons://g4p3/publication/v1"
    local_promotion_policy_ref = "graph_commons://g4p3/local-promotion/v1"
    minimum_reputation_score = 0.55

    def build_issuer_reputation_profile(
        self,
        *,
        package_ref: str,
        source_instance: str,
        trust_status: str,
        signature_verified: bool,
        prior_retractions: int = 0,
        poisoning_incidents: int = 0,
        local_promotions: int = 0,
        curator_endorsements: int = 0,
    ) -> IssuerReputationProfile:
        flags: list[str] = []
        score = 0.35
        normalized_trust = str(trust_status or "").strip().lower()
        if normalized_trust == "imported":
            score += 0.25
        else:
            flags.append("trust_status_not_imported")
        if signature_verified:
            score += 0.2
        else:
            flags.append("signature_not_verified")

        score += min(0.1, max(0, int(local_promotions)) * 0.02)
        score += min(0.1, max(0, int(curator_endorsements)) * 0.025)
        score -= min(0.25, max(0, int(prior_retractions)) * 0.08)
        score -= min(0.45, max(0, int(poisoning_incidents)) * 0.2)

        if prior_retractions > 0:
            flags.append("issuer_has_retractions")
        if poisoning_incidents > 0:
            flags.append("issuer_poisoning_history")

        bounded_score = round(min(1.0, max(0.0, score)), 4)
        if bounded_score >= 0.8:
            reputation_tier = "trusted"
        elif bounded_score >= self.minimum_reputation_score:
            reputation_tier = "provisional"
        else:
            reputation_tier = "restricted"

        payload = {
            "package_ref": package_ref,
            "source_instance": source_instance,
            "reputation_score": bounded_score,
            "trust_status": normalized_trust,
            "signature_verified": signature_verified,
            "prior_retractions": int(prior_retractions),
            "poisoning_incidents": int(poisoning_incidents),
            "local_promotions": int(local_promotions),
            "curator_endorsements": int(curator_endorsements),
            "flags": flags,
        }
        return IssuerReputationProfile(
            issuer_profile_ref=self._stable_ref("issuer_reputation://graphrag/", payload),
            source_instance=source_instance,
            reputation_score=bounded_score,
            trust_status=normalized_trust,
            signature_verified=bool(signature_verified),
            prior_retractions=max(0, int(prior_retractions)),
            poisoning_incidents=max(0, int(poisoning_incidents)),
            local_promotions=max(0, int(local_promotions)),
            curator_endorsements=max(0, int(curator_endorsements)),
            reputation_tier=reputation_tier,
            anti_poisoning_flags=tuple(flags),
        )

    def review_commons_package(
        self,
        *,
        package_ref: str,
        source_instance: str,
        trust_status: str,
        signature_verified: bool,
        federation_state: str,
        publication_scope: str,
        edge_refs: Iterable[str],
        receipt_refs: Iterable[str],
        prior_retractions: int = 0,
        poisoning_incidents: int = 0,
        local_promotions: int = 0,
        curator_endorsements: int = 0,
    ) -> CommonsAdmissionDecision:
        normalized_scope = str(publication_scope or "").strip().lower()
        if normalized_scope not in {"global", "org"}:
            raise ValueError("commons_publication_scope_must_be_global_or_org")
        normalized_state = str(federation_state or "").strip().lower()
        if normalized_state != "external_asserted":
            raise ValueError("commons_requires_external_asserted_state")

        normalized_edge_refs = tuple(sorted({ref.strip() for ref in edge_refs if str(ref).strip()}))
        normalized_receipt_refs = tuple(sorted({ref.strip() for ref in receipt_refs if str(ref).strip()}))
        if not normalized_edge_refs:
            raise ValueError("commons_edge_refs_required")
        if not normalized_receipt_refs:
            raise ValueError("commons_receipt_refs_required")

        issuer_profile = self.build_issuer_reputation_profile(
            package_ref=package_ref,
            source_instance=source_instance,
            trust_status=trust_status,
            signature_verified=signature_verified,
            prior_retractions=prior_retractions,
            poisoning_incidents=poisoning_incidents,
            local_promotions=local_promotions,
            curator_endorsements=curator_endorsements,
        )
        flags = list(issuer_profile.anti_poisoning_flags)
        reason_codes: list[str] = []
        blocked = False
        if issuer_profile.reputation_score < self.minimum_reputation_score:
            flags.append("issuer_reputation_below_floor")
            reason_codes.append("issuer_reputation_below_floor")
            blocked = True
        if poisoning_incidents > 0:
            reason_codes.append("issuer_poisoning_history")
            blocked = True
        if issuer_profile.trust_status != "imported":
            reason_codes.append("trust_status_not_imported")
            blocked = True
        if not issuer_profile.signature_verified:
            reason_codes.append("signature_not_verified")
            blocked = True

        commons_package = None
        created_by_receipt_ref = None
        if not blocked:
            commons_payload = {
                "package_ref": package_ref,
                "source_instance": source_instance,
                "publication_scope": normalized_scope,
                "edge_refs": list(normalized_edge_refs),
                "receipt_refs": list(normalized_receipt_refs),
                "issuer_profile_ref": issuer_profile.issuer_profile_ref,
                "federation_state": normalized_state,
            }
            commons_package = GraphCommonsPackage(
                commons_package_ref=self._stable_ref("graph_commons_package://graphrag/", commons_payload),
                package_ref=package_ref,
                source_instance=source_instance,
                publication_scope=normalized_scope,
                edge_refs=normalized_edge_refs,
                receipt_refs=normalized_receipt_refs,
                issuer_profile_ref=issuer_profile.issuer_profile_ref,
                federation_state=normalized_state,
                provenance_policy_ref=self.provenance_policy_ref,
            )
            created_by_receipt_ref = self._stable_ref(
                "receipt://graphrag/commons-package/",
                {
                    "commons_package_ref": commons_package.commons_package_ref,
                    "issuer_profile_ref": issuer_profile.issuer_profile_ref,
                    "status": "admitted",
                },
            )

        decision_status = "admitted" if commons_package is not None else "blocked"
        payload = {
            "package_ref": package_ref,
            "source_instance": source_instance,
            "decision_status": decision_status,
            "issuer_profile": asdict(issuer_profile),
            "anti_poisoning_flags": sorted(set(flags)),
            "reason_codes": sorted(set(reason_codes)),
            "commons_package_ref": commons_package.commons_package_ref if commons_package else None,
        }
        return CommonsAdmissionDecision(
            status=decision_status,
            decision_ref=self._stable_ref("commons_admission://graphrag/", payload),
            package_ref=package_ref,
            source_instance=source_instance,
            admission_policy_ref=self.admission_policy_ref,
            issuer_profile=issuer_profile,
            anti_poisoning_flags=tuple(sorted(set(flags))),
            reason_codes=tuple(sorted(set(reason_codes))),
            commons_package=commons_package,
            created_by_receipt_ref=created_by_receipt_ref,
        )

    def publish_external_subgraph(
        self,
        *,
        commons_package_ref: str,
        package_ref: str,
        source_instance: str,
        federation_state: str,
        publication_scope: str,
        edge_refs: Iterable[str],
        receipt_refs: Iterable[str],
        edge_policy_scopes: dict[str, str],
    ) -> ExternalSubgraphPublicationDecision:
        normalized_scope = str(publication_scope or "").strip().lower()
        if normalized_scope not in {"global", "org"}:
            raise ValueError("commons_publication_scope_must_be_global_or_org")
        normalized_state = str(federation_state or "").strip().lower()
        if normalized_state != "external_asserted":
            raise ValueError("commons_requires_external_asserted_state")

        normalized_edge_refs = tuple(sorted({ref.strip() for ref in edge_refs if str(ref).strip()}))
        normalized_receipt_refs = tuple(sorted({ref.strip() for ref in receipt_refs if str(ref).strip()}))
        if not normalized_edge_refs:
            raise ValueError("commons_edge_refs_required")
        if not normalized_receipt_refs:
            raise ValueError("commons_receipt_refs_required")

        allowed_scopes = {"global"} if normalized_scope == "global" else {"global", "org"}
        exported_edge_refs: list[str] = []
        blocked_edge_refs: list[str] = []
        for edge_ref in normalized_edge_refs:
            edge_scope = str(edge_policy_scopes.get(edge_ref, "") or "").strip().lower()
            if edge_scope in allowed_scopes:
                exported_edge_refs.append(edge_ref)
            else:
                blocked_edge_refs.append(edge_ref)

        if not exported_edge_refs:
            payload = {
                "commons_package_ref": commons_package_ref,
                "package_ref": package_ref,
                "source_instance": source_instance,
                "publication_scope": normalized_scope,
                "reason_codes": ["no_public_edges_eligible_for_commons_export"],
                "blocked_edge_refs": blocked_edge_refs,
            }
            return ExternalSubgraphPublicationDecision(
                status="blocked",
                decision_ref=self._stable_ref("commons_publication_decision://graphrag/", payload),
                commons_package_ref=commons_package_ref,
                publication_policy_ref=self.publication_policy_ref,
                reason_codes=("no_public_edges_eligible_for_commons_export",),
                publication=None,
                created_by_receipt_ref=None,
            )

        publication_payload = {
            "commons_package_ref": commons_package_ref,
            "package_ref": package_ref,
            "source_instance": source_instance,
            "publication_scope": normalized_scope,
            "exported_edge_refs": exported_edge_refs,
            "receipt_refs": list(normalized_receipt_refs),
        }
        publication = ExternalSubgraphPublication(
            publication_ref=self._stable_ref("graph_commons_publication://graphrag/", publication_payload),
            commons_package_ref=commons_package_ref,
            package_ref=package_ref,
            source_instance=source_instance,
            publication_scope=normalized_scope,
            exported_edge_refs=tuple(exported_edge_refs),
            exported_receipt_refs=normalized_receipt_refs,
            blocked_edge_refs=tuple(sorted(blocked_edge_refs)),
            federation_state=normalized_state,
            provenance_policy_ref=self.provenance_policy_ref,
        )
        receipt_ref = self._stable_ref(
            "receipt://graphrag/commons-publication/",
            {
                "publication_ref": publication.publication_ref,
                "commons_package_ref": commons_package_ref,
                "publication_scope": normalized_scope,
            },
        )
        decision_payload = {
            "publication_ref": publication.publication_ref,
            "commons_package_ref": commons_package_ref,
            "status": "published",
            "blocked_edge_refs": blocked_edge_refs,
        }
        return ExternalSubgraphPublicationDecision(
            status="published",
            decision_ref=self._stable_ref("commons_publication_decision://graphrag/", decision_payload),
            commons_package_ref=commons_package_ref,
            publication_policy_ref=self.publication_policy_ref,
            reason_codes=tuple(["private_edges_excluded_from_publication"] if blocked_edge_refs else []),
            publication=publication,
            created_by_receipt_ref=receipt_ref,
        )

    def create_local_promotion_candidate(
        self,
        *,
        commons_package_ref: str,
        source_publication_ref: str,
        package_ref: str,
        source_instance: str,
        federation_state: str,
        target_scope: str,
        candidate_edge_refs: Iterable[str],
        evidence_receipt_refs: Iterable[str],
        supporting_local_receipt_refs: Iterable[str],
        requested_by: str,
    ) -> LocalPromotionGateDecision:
        normalized_state = str(federation_state or "").strip().lower()
        if normalized_state != "external_asserted":
            raise ValueError("local_promotion_candidate_requires_external_asserted_state")
        normalized_target_scope = str(target_scope or "").strip().lower()
        if normalized_target_scope not in {"org", "lab", "study"}:
            raise ValueError("local_promotion_target_scope_must_be_org_lab_or_study")

        normalized_edge_refs = tuple(sorted({ref.strip() for ref in candidate_edge_refs if str(ref).strip()}))
        normalized_evidence_receipts = tuple(sorted({ref.strip() for ref in evidence_receipt_refs if str(ref).strip()}))
        normalized_local_receipts = tuple(sorted({ref.strip() for ref in supporting_local_receipt_refs if str(ref).strip()}))
        normalized_requested_by = str(requested_by or "").strip()

        if not normalized_edge_refs:
            raise ValueError("local_promotion_candidate_edge_refs_required")
        if not normalized_evidence_receipts:
            raise ValueError("local_promotion_candidate_receipt_refs_required")
        if not normalized_local_receipts:
            raise ValueError("local_promotion_supporting_local_receipt_refs_required")
        if not normalized_requested_by:
            raise ValueError("local_promotion_requested_by_required")

        candidate_payload = {
            "commons_package_ref": commons_package_ref,
            "source_publication_ref": source_publication_ref,
            "package_ref": package_ref,
            "source_instance": source_instance,
            "target_scope": normalized_target_scope,
            "candidate_edge_refs": list(normalized_edge_refs),
            "evidence_receipt_refs": list(normalized_evidence_receipts),
            "supporting_local_receipt_refs": list(normalized_local_receipts),
            "requested_by": normalized_requested_by,
        }
        candidate = LocalPromotionCandidate(
            candidate_ref=self._stable_ref("local_promotion_candidate://graphrag/", candidate_payload),
            commons_package_ref=commons_package_ref,
            source_publication_ref=source_publication_ref,
            package_ref=package_ref,
            source_instance=source_instance,
            target_scope=normalized_target_scope,
            candidate_edge_refs=normalized_edge_refs,
            evidence_receipt_refs=normalized_evidence_receipts,
            supporting_local_receipt_refs=normalized_local_receipts,
            requested_by=normalized_requested_by,
            candidate_status="candidate",
            provenance_policy_ref=self.provenance_policy_ref,
        )
        receipt_ref = self._stable_ref(
            "receipt://graphrag/local-promotion-candidate/",
            {
                "candidate_ref": candidate.candidate_ref,
                "commons_package_ref": commons_package_ref,
                "target_scope": normalized_target_scope,
            },
        )
        decision_payload = {
            "candidate_ref": candidate.candidate_ref,
            "commons_package_ref": commons_package_ref,
            "status": "candidate",
        }
        return LocalPromotionGateDecision(
            status="candidate",
            decision_ref=self._stable_ref("local_promotion_gate://graphrag/", decision_payload),
            commons_package_ref=commons_package_ref,
            promotion_policy_ref=self.local_promotion_policy_ref,
            reason_codes=("candidate_only_no_local_truth_promotion",),
            candidate=candidate,
            created_by_receipt_ref=receipt_ref,
        )

    @staticmethod
    def _stable_ref(prefix: str, payload: dict[str, object]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}"


__all__ = [
    "CommonsAdmissionDecision",
    "ExternalSubgraphPublication",
    "ExternalSubgraphPublicationDecision",
    "GraphCommonsPackage",
    "GraphCommonsRuntime",
    "IssuerReputationProfile",
    "LocalPromotionCandidate",
    "LocalPromotionGateDecision",
]

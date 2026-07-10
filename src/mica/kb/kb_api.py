"""
KB API REST — P4 (KB Slice 5 · P4 gap).

REST surface for KB claims with contract envelope, profile param, as-of.
Not a router (that lives in api_v1/routers/). This is the query/dispatch
layer that the router calls.

Key functions:
- get_claim_with_envelope: claim + contract envelope
- query_claims: filtered claim search
- get_claim_as_of: historical reconstruction
- claim_deprecation_check: deprecation policy enforcement
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .claim_versioning import ClaimFamily, ClaimVersion
from .asof_index import AsofIndex, AsofQuery, AsofSnapshot
from .semantic_contract_bundle import SemanticContractRegistry


@dataclass
class ContractEnvelope:
    """P4: Contract envelope for API responses."""
    api_version: str = "v1"
    schema_version: str = "1.0.0"
    semantic_contract_ref: Optional[str] = None
    semantic_contract_version: Optional[str] = None
    as_of_snapshot_ref: Optional[str] = None
    deprecation: Optional[Dict[str, Any]] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ClaimAPIResponse:
    """P4: Claim response with contract envelope."""
    claim_family_ref: str
    claim_version_ref: str
    claim_data: Dict[str, Any] = field(default_factory=dict)
    envelope: ContractEnvelope = field(default_factory=ContractEnvelope)
    profile: Optional[str] = None  # theory-ladenness profile (K11.7)
    as_of: Optional[datetime] = None
    is_deprecated: bool = False
    deprecation_notice: Optional[str] = None


class KBAPIDispatch:
    """P4: REST surface dispatch for KB claims.

    Serves /kb/claims/{ref} with contract envelope, ?profile=, ?as_of=.
    Consumer contract tests: GraphRAG, agents, product export.
    """

    def __init__(
        self,
        asof_index: Optional[AsofIndex] = None,
        contract_registry: Optional[SemanticContractRegistry] = None,
    ) -> None:
        self._asof = asof_index or AsofIndex()
        self._contracts = contract_registry or SemanticContractRegistry()
        self._families: Dict[str, ClaimFamily] = {}
        self._versions: Dict[str, ClaimVersion] = {}

    def register_family(self, family: ClaimFamily) -> None:
        self._families[family.family_ref] = family

    def register_version(self, version: ClaimVersion) -> None:
        self._versions[version.claim_version_ref] = version

    def get_claim_with_envelope(
        self,
        claim_version_ref: str,
        profile: Optional[str] = None,
    ) -> Optional[ClaimAPIResponse]:
        """Get a claim with contract envelope (P4 main endpoint)."""
        cv = self._versions.get(claim_version_ref)
        if not cv:
            return None

        # Check deprecation
        is_deprecated = cv.status.value in ("deprecated", "retracted", "superseded")
        deprecation_notice = None
        if is_deprecated:
            deprecation_notice = (
                f"Claim {claim_version_ref} is {cv.status.value}. "
                "This version is superseded and should not be used for new assertions."
            )

        # Build envelope
        active_contract = self._contracts.get_active()
        envelope = ContractEnvelope(
            semantic_contract_ref=cv.claim_family_ref,
            semantic_contract_version=active_contract.version if active_contract else None,
        )

        claim_data = {
            "subject": cv.atom.subject,
            "predicate_id": cv.atom.predicate.predicate_id,
            "object": cv.atom.object,
            "status": cv.status.value,
            "version_number": cv.version_number,
            "content_hash": cv.content_hash,
        }

        return ClaimAPIResponse(
            claim_family_ref=cv.claim_family_ref,
            claim_version_ref=claim_version_ref,
            claim_data=claim_data,
            envelope=envelope,
            profile=profile,
            as_of=cv.valid_from,
            is_deprecated=is_deprecated,
            deprecation_notice=deprecation_notice,
        )

    def get_claim_as_of(
        self,
        family_ref: str,
        as_of: datetime,
    ) -> Optional[ClaimAPIResponse]:
        """Reconstruct claim state at a historical point (INV-6)."""
        snapshot = self._asof.query_asof(
            AsofQuery(
                query_ref=f"api://{family_ref}/{as_of.isoformat()}",
                claim_family_ref=family_ref,
                as_of=as_of,
            )
        )

        if not snapshot.active_row:
            return None

        # INV-6: use contract from snapshot, not current active
        contract_ref = snapshot.semantic_contract_ref
        envelope = ContractEnvelope(
            semantic_contract_ref=contract_ref,
            as_of_snapshot_ref=snapshot.snapshot_ref,
        )

        claim_data = snapshot.active_row.claim_data or {}

        return ClaimAPIResponse(
            claim_family_ref=family_ref,
            claim_version_ref=snapshot.active_row.claim_version_ref,
            claim_data=claim_data,
            envelope=envelope,
            as_of=as_of,
        )

    def query_claims(
        self,
        scope_ref: Optional[str] = None,
        status: Optional[str] = None,
        profile: Optional[str] = None,
        limit: int = 50,
    ) -> List[ClaimAPIResponse]:
        """Filtered claim search."""
        results: List[ClaimAPIResponse] = []
        for cv in self._versions.values():
            if status and cv.status.value != status:
                continue
            if len(results) >= limit:
                break
            resp = self.get_claim_with_envelope(cv.claim_version_ref, profile=profile)
            if resp:
                results.append(resp)
        return results

    def breaking_change_requires_green(
        self,
        old_contract_ref: str,
        new_contract_ref: str,
    ) -> bool:
        """Check if breaking change requires consumer contract to be green."""
        old = self._contracts.get_version(old_contract_ref, "current")
        new = self._contracts.get_version(new_contract_ref, "current")
        if not old or not new:
            return True  # if we can't verify, treat as breaking
        # Check if any predicate mappings are incompatible
        for pm in new.predicate_mappings:
            if pm.get("kind") == "incompatible":
                return True
        return False

    def deprecation_never_silent(
        self,
        claim_version_ref: str,
    ) -> Dict[str, Any]:
        """Enforce: deprecation policy never silent (P4 acceptance test)."""
        cv = self._versions.get(claim_version_ref)
        if not cv:
            return {"error": "not_found"}
        is_deprecated = cv.status.value in ("deprecated", "retracted", "superseded")
        return {
            "claim_version_ref": claim_version_ref,
            "is_deprecated": is_deprecated,
            "status": cv.status.value,
            "deprecation_notice": (
                f"Status: {cv.status.value}. "
                "Deprecated claims return explicit deprecation notice in envelope."
                if is_deprecated else None
            ),
        }

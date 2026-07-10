from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


_SEVERITY_ORDER = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

_PUBLIC_BLOCKING_SEVERITIES = {"critical"}


@dataclass(frozen=True)
class GraphDomainMetric:
    domain_ref: str
    edge_count: int
    receipt_coverage_ratio: float
    stale_edge_ratio: float
    open_contradiction_ratio: float


@dataclass(frozen=True)
class KnowledgeDebtEntry:
    debt_ref: str
    scope_ref: str
    domain_ref: str
    debt_kind: str
    severity: str
    downstream_risk: str
    owner_ref: str | None
    affected_count: int
    hidden: bool
    sample_strategy: str
    reason: str
    sampled_edge_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeDebtLedger:
    ledger_ref: str
    scope_ref: str
    owner_ref: str | None
    debt_entries: tuple[KnowledgeDebtEntry, ...]
    hidden_critical_debt_count: int


@dataclass(frozen=True)
class GraphHealthMetrics:
    active_edge_count: int
    receipt_coverage_ratio: float
    stale_edge_ratio: float
    orphan_node_ratio: float
    orphan_edge_ratio: float
    open_contradiction_ratio: float
    hidden_critical_debt_ratio: float


@dataclass(frozen=True)
class GraphHealthReport:
    report_ref: str
    scope_ref: str
    owner_ref: str | None
    generated_at: str
    state: str
    metrics: GraphHealthMetrics
    domain_metrics: tuple[GraphDomainMetric, ...]
    knowledge_debt_ledger: KnowledgeDebtLedger
    public_gate_blocked: bool
    gate_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class GraphKnowledgeDebtGateDecision:
    scope_ref: str
    state: str
    allow_serve: bool
    public_surface: bool
    blocker_ref: str | None
    warning_ref: str | None
    reason_codes: tuple[str, ...]
    report_ref: str | None = None


@dataclass(frozen=True)
class StatisticalSamplingPlan:
    scope_ref: str
    sample_strategy: str
    sample_size: int
    sampled_edge_refs: tuple[str, ...]
    sampled_domains: tuple[str, ...]


@dataclass(frozen=True)
class GraphHealthAuditInputs:
    stale_edge_count: int = 0
    orphan_node_count: int = 0
    orphan_edge_count: int = 0
    open_contradiction_count: int = 0
    hidden_critical_debt_count: int = 0
    owner_ref: str | None = None
    sample_size: int = 12
    domain_usage_weights: Mapping[str, int] = field(default_factory=dict)
    issuer_risk_weights: Mapping[str, float] = field(default_factory=dict)


class StatisticalSampler:
    """Bounded statistical sampler for knowledge debt review queues."""

    def build_plan(
        self,
        *,
        scope_ref: str,
        manifest: Sequence[Mapping[str, Any]],
        strategy: str,
        sample_size: int,
        domain_usage_weights: Mapping[str, int] | None = None,
        issuer_risk_weights: Mapping[str, float] | None = None,
        targeted_edge_refs: Iterable[str] | None = None,
    ) -> StatisticalSamplingPlan:
        normalized_strategy = str(strategy or "targeted").strip().lower()
        size = max(1, int(sample_size))
        domain_usage_weights = domain_usage_weights or {}
        issuer_risk_weights = issuer_risk_weights or {}
        targeted = {str(ref).strip() for ref in (targeted_edge_refs or ()) if str(ref).strip()}

        records: list[dict[str, Any]] = []
        for item in manifest:
            edge_ref = str(item.get("edge_ref") or "").strip()
            if not edge_ref:
                continue
            domain_ref = self._domain_ref(item)
            issuer_ref = str(item.get("source_doi") or item.get("issuer_ref") or "issuer://unknown").strip()
            score = 0.0
            if edge_ref in targeted:
                score += 1000.0
            score += float(domain_usage_weights.get(domain_ref, 0))
            score += float(issuer_risk_weights.get(issuer_ref, 0.0))
            records.append(
                {
                    "edge_ref": edge_ref,
                    "domain_ref": domain_ref,
                    "issuer_ref": issuer_ref,
                    "score": score,
                }
            )

        if normalized_strategy == "targeted":
            ordered = sorted(records, key=lambda item: (-item["score"], item["edge_ref"]))
            selected = ordered[:size]
        elif normalized_strategy == "usage-weighted":
            ordered = sorted(
                records,
                key=lambda item: (-float(domain_usage_weights.get(item["domain_ref"], 0)), item["edge_ref"]),
            )
            selected = ordered[:size]
        elif normalized_strategy == "issuer-weighted":
            ordered = sorted(
                records,
                key=lambda item: (-float(issuer_risk_weights.get(item["issuer_ref"], 0.0)), item["edge_ref"]),
            )
            selected = ordered[:size]
        else:
            # stratified is the safe default for broad review.
            buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in records:
                buckets[item["domain_ref"]].append(item)
            selected = []
            domains = sorted(buckets.keys())
            if domains:
                quota = max(1, size // len(domains))
                for domain_ref in domains:
                    ordered = sorted(buckets[domain_ref], key=lambda item: (-item["score"], item["edge_ref"]))
                    selected.extend(ordered[:quota])
                if len(selected) < size:
                    spillover = sorted(
                        [item for item in records if item not in selected],
                        key=lambda item: (-item["score"], item["edge_ref"]),
                    )
                    selected.extend(spillover[: max(0, size - len(selected))])
                selected = selected[:size]

        sampled_edge_refs = tuple(item["edge_ref"] for item in selected)
        sampled_domains = tuple(sorted({item["domain_ref"] for item in selected}))
        return StatisticalSamplingPlan(
            scope_ref=scope_ref,
            sample_strategy=normalized_strategy,
            sample_size=size,
            sampled_edge_refs=sampled_edge_refs,
            sampled_domains=sampled_domains,
        )

    @staticmethod
    def _domain_ref(item: Mapping[str, Any]) -> str:
        domain = str(
            item.get("edge_kind")
            or item.get("predicate_id")
            or item.get("relationship")
            or "domain://unknown"
        ).strip()
        return domain or "domain://unknown"


class GraphKnowledgeDebtRuntime:
    """G4.5 knowledge debt ledger over existing graph health/store seams."""

    policy_ref = "graph_health://g4p5/knowledge-debt/v1"
    minimum_receipt_coverage_ratio = 0.95

    def __init__(self) -> None:
        self._reports: dict[str, GraphHealthReport] = {}
        self._sampler = StatisticalSampler()

    async def build_report_from_store(
        self,
        *,
        store: Any,
        scope_ref: str,
        user_id: str | None,
        workspace_id: str | None,
        global_only: bool,
        audit_inputs: GraphHealthAuditInputs,
    ) -> GraphHealthReport:
        coverage_snapshot = await store.build_node2vec_coverage_snapshot()
        manifest = await store.export_active_edge_manifest(
            user_id=user_id,
            workspace_id=workspace_id,
            global_only=global_only,
            limit=max(coverage_snapshot.active_edge_count, 1),
        )
        return self.build_report(
            scope_ref=scope_ref,
            manifest=manifest,
            active_edge_count=coverage_snapshot.active_edge_count,
            receipt_coverage_ratio=coverage_snapshot.active_edge_receipt_coverage_ratio,
            audit_inputs=audit_inputs,
        )

    def build_report(
        self,
        *,
        scope_ref: str,
        manifest: Sequence[Mapping[str, Any]],
        active_edge_count: int,
        receipt_coverage_ratio: float,
        audit_inputs: GraphHealthAuditInputs,
    ) -> GraphHealthReport:
        edge_count = max(0, int(active_edge_count))
        denominator = max(1, edge_count)

        stale_edge_ratio = max(0.0, audit_inputs.stale_edge_count / denominator)
        orphan_node_ratio = max(0.0, audit_inputs.orphan_node_count / denominator)
        orphan_edge_ratio = max(0.0, audit_inputs.orphan_edge_count / denominator)
        open_contradiction_ratio = max(0.0, audit_inputs.open_contradiction_count / denominator)
        hidden_critical_debt_ratio = max(0.0, audit_inputs.hidden_critical_debt_count / denominator)

        domain_counter: Counter[str] = Counter()
        for item in manifest:
            domain_counter[self._sampler._domain_ref(item)] += 1
        domain_metrics = tuple(
            GraphDomainMetric(
                domain_ref=domain_ref,
                edge_count=count,
                receipt_coverage_ratio=round(receipt_coverage_ratio, 6),
                stale_edge_ratio=round(stale_edge_ratio, 6),
                open_contradiction_ratio=round(open_contradiction_ratio, 6),
            )
            for domain_ref, count in sorted(domain_counter.items())
        )

        targeted_edge_refs = [str(item.get("edge_ref") or "").strip() for item in manifest[: audit_inputs.sample_size]]
        sampling_plan = self._sampler.build_plan(
            scope_ref=scope_ref,
            manifest=manifest,
            strategy="stratified",
            sample_size=audit_inputs.sample_size,
            domain_usage_weights=audit_inputs.domain_usage_weights,
            issuer_risk_weights=audit_inputs.issuer_risk_weights,
            targeted_edge_refs=targeted_edge_refs,
        )

        debt_entries: list[KnowledgeDebtEntry] = []
        if receipt_coverage_ratio < self.minimum_receipt_coverage_ratio:
            missing_receipts = max(0, edge_count - int(round(edge_count * receipt_coverage_ratio)))
            debt_entries.append(
                self._build_debt_entry(
                    scope_ref=scope_ref,
                    domain_ref="domain://all",
                    debt_kind="receipt_coverage_gap",
                    severity="critical" if receipt_coverage_ratio < 0.8 else "high",
                    downstream_risk="GraphRAG|KB|public_export",
                    owner_ref=audit_inputs.owner_ref,
                    affected_count=missing_receipts,
                    hidden=False,
                    sample_strategy=sampling_plan.sample_strategy,
                    sampled_edge_refs=sampling_plan.sampled_edge_refs,
                    reason=f"receipt_coverage_ratio={receipt_coverage_ratio:.4f} below floor",
                )
            )
        if audit_inputs.stale_edge_count > 0:
            debt_entries.append(
                self._build_debt_entry(
                    scope_ref=scope_ref,
                    domain_ref="domain://all",
                    debt_kind="stale_edges_present",
                    severity="medium" if stale_edge_ratio < 0.2 else "high",
                    downstream_risk="GraphRAG|KB",
                    owner_ref=audit_inputs.owner_ref,
                    affected_count=audit_inputs.stale_edge_count,
                    hidden=False,
                    sample_strategy=sampling_plan.sample_strategy,
                    sampled_edge_refs=sampling_plan.sampled_edge_refs,
                    reason=f"stale_edge_ratio={stale_edge_ratio:.4f}",
                )
            )
        if audit_inputs.orphan_node_count > 0:
            debt_entries.append(
                self._build_debt_entry(
                    scope_ref=scope_ref,
                    domain_ref="domain://all",
                    debt_kind="orphan_nodes_present",
                    severity="medium",
                    downstream_risk="GraphRAG|public_export",
                    owner_ref=audit_inputs.owner_ref,
                    affected_count=audit_inputs.orphan_node_count,
                    hidden=False,
                    sample_strategy="targeted",
                    sampled_edge_refs=(),
                    reason=f"orphan_node_ratio={orphan_node_ratio:.4f}",
                )
            )
        if audit_inputs.orphan_edge_count > 0:
            debt_entries.append(
                self._build_debt_entry(
                    scope_ref=scope_ref,
                    domain_ref="domain://all",
                    debt_kind="orphan_edges_present",
                    severity="high",
                    downstream_risk="GraphRAG|public_export|federation",
                    owner_ref=audit_inputs.owner_ref,
                    affected_count=audit_inputs.orphan_edge_count,
                    hidden=False,
                    sample_strategy=sampling_plan.sample_strategy,
                    sampled_edge_refs=sampling_plan.sampled_edge_refs,
                    reason=f"orphan_edge_ratio={orphan_edge_ratio:.4f}",
                )
            )
        if audit_inputs.open_contradiction_count > 0:
            debt_entries.append(
                self._build_debt_entry(
                    scope_ref=scope_ref,
                    domain_ref="domain://all",
                    debt_kind="open_contradictions_present",
                    severity="medium" if open_contradiction_ratio < 0.1 else "high",
                    downstream_risk="GraphRAG|KB|federation",
                    owner_ref=audit_inputs.owner_ref,
                    affected_count=audit_inputs.open_contradiction_count,
                    hidden=False,
                    sample_strategy="usage-weighted",
                    sampled_edge_refs=sampling_plan.sampled_edge_refs,
                    reason=f"open_contradiction_ratio={open_contradiction_ratio:.4f}",
                )
            )
        if audit_inputs.hidden_critical_debt_count > 0:
            debt_entries.append(
                self._build_debt_entry(
                    scope_ref=scope_ref,
                    domain_ref="domain://all",
                    debt_kind="hidden_critical_debt_present",
                    severity="critical",
                    downstream_risk="GraphRAG|commons|public_export",
                    owner_ref=audit_inputs.owner_ref,
                    affected_count=audit_inputs.hidden_critical_debt_count,
                    hidden=True,
                    sample_strategy="targeted",
                    sampled_edge_refs=sampling_plan.sampled_edge_refs,
                    reason="hidden critical debt blocks public serve",
                )
            )

        state = "green"
        gate_reason_codes: list[str] = []
        public_gate_blocked = any(
            entry.hidden and entry.severity in _PUBLIC_BLOCKING_SEVERITIES
            for entry in debt_entries
        )
        if public_gate_blocked:
            state = "red"
            gate_reason_codes.append("hidden_critical_knowledge_debt_present")
        elif debt_entries:
            state = "yellow"
            gate_reason_codes.append("knowledge_debt_present")

        ledger_payload = {
            "scope_ref": scope_ref,
            "owner_ref": audit_inputs.owner_ref,
            "debt_entries": [asdict(entry) for entry in debt_entries],
            "hidden_critical_debt_count": audit_inputs.hidden_critical_debt_count,
        }
        ledger = KnowledgeDebtLedger(
            ledger_ref=self._stable_ref("knowledge_debt_ledger://graphrag/", ledger_payload),
            scope_ref=scope_ref,
            owner_ref=audit_inputs.owner_ref,
            debt_entries=tuple(
                sorted(
                    debt_entries,
                    key=lambda item: (-_SEVERITY_ORDER[item.severity], item.debt_kind, item.debt_ref),
                )
            ),
            hidden_critical_debt_count=max(0, int(audit_inputs.hidden_critical_debt_count)),
        )
        metrics = GraphHealthMetrics(
            active_edge_count=edge_count,
            receipt_coverage_ratio=round(receipt_coverage_ratio, 6),
            stale_edge_ratio=round(stale_edge_ratio, 6),
            orphan_node_ratio=round(orphan_node_ratio, 6),
            orphan_edge_ratio=round(orphan_edge_ratio, 6),
            open_contradiction_ratio=round(open_contradiction_ratio, 6),
            hidden_critical_debt_ratio=round(hidden_critical_debt_ratio, 6),
        )
        report_payload = {
            "scope_ref": scope_ref,
            "owner_ref": audit_inputs.owner_ref,
            "state": state,
            "metrics": asdict(metrics),
            "domain_metrics": [asdict(item) for item in domain_metrics],
            "knowledge_debt_ledger_ref": ledger.ledger_ref,
            "public_gate_blocked": public_gate_blocked,
            "gate_reason_codes": sorted(gate_reason_codes),
        }
        report = GraphHealthReport(
            report_ref=self._stable_ref("graph_health_report://graphrag/", report_payload),
            scope_ref=scope_ref,
            owner_ref=audit_inputs.owner_ref,
            generated_at=self._utc_now(),
            state=state,
            metrics=metrics,
            domain_metrics=domain_metrics,
            knowledge_debt_ledger=ledger,
            public_gate_blocked=public_gate_blocked,
            gate_reason_codes=tuple(sorted(gate_reason_codes)),
        )
        return report

    def register_report(self, report: GraphHealthReport) -> GraphHealthReport:
        self._reports[report.scope_ref] = report
        return report

    def inspect_scope(self, *, scope_ref: str, public_surface: bool) -> GraphKnowledgeDebtGateDecision:
        report = self._reports.get(scope_ref)
        if report is None:
            return GraphKnowledgeDebtGateDecision(
                scope_ref=scope_ref,
                state="green",
                allow_serve=True,
                public_surface=public_surface,
                blocker_ref=None,
                warning_ref=None,
                reason_codes=("knowledge_debt_report_missing",),
                report_ref=None,
            )

        if public_surface and report.public_gate_blocked:
            return GraphKnowledgeDebtGateDecision(
                scope_ref=scope_ref,
                state="red",
                allow_serve=False,
                public_surface=public_surface,
                blocker_ref=f"{report.report_ref}#public-gate",
                warning_ref=None,
                reason_codes=report.gate_reason_codes,
                report_ref=report.report_ref,
            )
        if report.state == "yellow":
            return GraphKnowledgeDebtGateDecision(
                scope_ref=scope_ref,
                state="yellow",
                allow_serve=True,
                public_surface=public_surface,
                blocker_ref=None,
                warning_ref=f"{report.report_ref}#warning",
                reason_codes=report.gate_reason_codes,
                report_ref=report.report_ref,
            )
        return GraphKnowledgeDebtGateDecision(
            scope_ref=scope_ref,
            state=report.state,
            allow_serve=True,
            public_surface=public_surface,
            blocker_ref=None,
            warning_ref=None,
            reason_codes=report.gate_reason_codes or ("knowledge_debt_green",),
            report_ref=report.report_ref,
        )

    def _build_debt_entry(
        self,
        *,
        scope_ref: str,
        domain_ref: str,
        debt_kind: str,
        severity: str,
        downstream_risk: str,
        owner_ref: str | None,
        affected_count: int,
        hidden: bool,
        sample_strategy: str,
        sampled_edge_refs: Sequence[str],
        reason: str,
    ) -> KnowledgeDebtEntry:
        payload = {
            "scope_ref": scope_ref,
            "domain_ref": domain_ref,
            "debt_kind": debt_kind,
            "severity": severity,
            "downstream_risk": downstream_risk,
            "owner_ref": owner_ref,
            "affected_count": int(affected_count),
            "hidden": bool(hidden),
            "sample_strategy": sample_strategy,
            "sampled_edge_refs": list(sampled_edge_refs),
            "reason": reason,
        }
        return KnowledgeDebtEntry(
            debt_ref=self._stable_ref("knowledge_debt://graphrag/", payload),
            scope_ref=scope_ref,
            domain_ref=domain_ref,
            debt_kind=debt_kind,
            severity=severity,
            downstream_risk=downstream_risk,
            owner_ref=owner_ref,
            affected_count=max(0, int(affected_count)),
            hidden=bool(hidden),
            sample_strategy=sample_strategy,
            sampled_edge_refs=tuple(sampled_edge_refs),
            reason=reason,
        )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _stable_ref(prefix: str, payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return prefix + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "GraphDomainMetric",
    "GraphHealthAuditInputs",
    "GraphHealthMetrics",
    "GraphHealthReport",
    "GraphKnowledgeDebtGateDecision",
    "GraphKnowledgeDebtRuntime",
    "KnowledgeDebtEntry",
    "KnowledgeDebtLedger",
    "StatisticalSampler",
    "StatisticalSamplingPlan",
]

"""Deterministic Graph-of-Graphs protocol generator.

This module is a bounded local producer: it turns a structured GoG spec into
the canonical ``ProtocolJSONLDDocument`` and validates it through the shared
JSON-LD validator. It does not execute, persist, or dispatch protocols.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from mica_q.protocol_jsonld_contract import (
    ProtocolApprovalMode,
    ProtocolApprovalPolicy,
    ProtocolBudgetPolicy,
    ProtocolEdge,
    ProtocolEdgeType,
    ProtocolJSONLDDocument,
    ProtocolLedgerMode,
    ProtocolLedgerPolicy,
    ProtocolNode,
    ProtocolNodePolicies,
    ProtocolReceiptSchema,
)
from mica_q.protocol_jsonld_validator import validate_protocol_jsonld


QUALITY_PASS_THRESHOLD = 80


class GeneratorSpecError(ValueError):
    """Raised when a GoG driver spec cannot produce a valid protocol."""


@dataclass
class GogNodeSpec:
    node_id: str
    objective: str = ""
    depends_on: list[str] = field(default_factory=list)
    child_graph_id: str | None = None
    phase_id: str | None = None
    semantic_group: str | None = None
    collapsed_by_default: bool = True
    mock_block: bool = False
    tool_name: str = "run_workflow"


@dataclass
class GogProtocolSpec:
    protocol_id: str
    nodes: list[GogNodeSpec]
    graph_level: str = "campaign"
    campaign_id: str | None = None
    parent_graph_id: str | None = None
    version: str = "1.0.0"
    session_id: str = ""
    owner_lab: str = "MICA GoG Driver V2"
    description: str = ""
    name: str = ""
    execution_mode: str = "development"
    risk_profile: str = "low"

    def __post_init__(self) -> None:
        if self.campaign_id is None:
            self.campaign_id = self.protocol_id
        if not self.session_id:
            self.session_id = f"gog-driver-session-{uuid.uuid4().hex[:8]}"
        if not self.name:
            self.name = self.protocol_id.replace("-", " ").title()


@dataclass(frozen=True)
class QualityCheckResult:
    name: str
    max_points: int
    earned_points: int
    passed: bool
    note: str


@dataclass(frozen=True)
class QualityScoreReport:
    total_score: int
    max_score: int
    passed: bool
    checks: list[QualityCheckResult]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_score": self.total_score,
            "max_score": self.max_score,
            "passed": self.passed,
            "threshold": QUALITY_PASS_THRESHOLD,
            "checks": [check.__dict__ for check in self.checks],
        }


class GogProtocolQualityScorer:
    """Small structural scorer for the V2 GoG gauntlet contract."""

    def score(
        self,
        document: ProtocolJSONLDDocument | dict[str, Any],
        spec: GogProtocolSpec | None = None,
    ) -> QualityScoreReport:
        if hasattr(document, "model_dump"):
            doc = document.model_dump(mode="json", by_alias=False)
        else:
            doc = dict(document)

        checks: list[QualityCheckResult] = []

        def add(name: str, points: int, passed: bool, note: str) -> None:
            checks.append(QualityCheckResult(name, points, points if passed else 0, passed, note))

        try:
            validated = validate_protocol_jsonld(document)
            add("valid_schema", 20, True, "Document passes validate_protocol_jsonld")
            doc = validated.model_dump(mode="json", by_alias=False)
        except Exception as exc:  # pragma: no cover - exercised by failing report output
            add("valid_schema", 20, False, f"Validation failed: {exc}")

        nodes = list(doc.get("nodes") or [])
        edges = list(doc.get("edges") or [])
        add(
            "gog_metadata_present",
            15,
            bool(doc.get("graph_level") and doc.get("campaign_id")),
            "Document carries graph_level and campaign_id",
        )
        add(
            "all_nodes_have_child_graph_id",
            15,
            all(bool(node.get("child_graph_id")) for node in nodes),
            "Every node has child_graph_id",
        )
        add(
            "all_nodes_have_phase_id",
            10,
            all(bool(node.get("phase_id")) for node in nodes),
            "Every node has phase_id",
        )
        add(
            "explicit_blocked_path",
            15,
            any(bool((node.get("inputs") or {}).get("_mock_block")) for node in nodes),
            "At least one node models the blocked EvidenceGate path",
        )
        roots = [node for node in nodes if not node.get("dependencies")]
        add(
            "no_flat_spaghetti",
            10,
            len(roots) == 1 and all(node.get("dependencies") for node in nodes if node not in roots),
            "Exactly one root; non-root nodes depend on prior work",
        )
        dependency_edges = {
            (edge.get("source_node_id"), edge.get("target_node_id")) for edge in edges
        }
        dependency_pairs = {
            (dep, node.get("node_id"))
            for node in nodes
            for dep in list(node.get("dependencies") or [])
        }
        add(
            "edges_reconciled",
            10,
            dependency_edges == dependency_pairs,
            "Edges match node dependency declarations",
        )
        add(
            "no_off_graph_action",
            5,
            all(not (node.get("policies") or {}).get("protected_surface") for node in nodes),
            "No protected surfaces are requested",
        )

        total = sum(check.earned_points for check in checks)
        max_score = sum(check.max_points for check in checks)
        return QualityScoreReport(
            total_score=total,
            max_score=max_score,
            passed=total >= QUALITY_PASS_THRESHOLD,
            checks=checks,
        )


class GogProtocolGeneratorDriver:
    """Generate canonical protocol JSON-LD from a structured GoG spec."""

    _ALLOWED_GRAPH_LEVELS = {"campaign", "workflow", "phase", "task"}

    def generate(self, spec: GogProtocolSpec) -> ProtocolJSONLDDocument:
        self._validate_spec(spec)
        nodes = [self._build_node(node) for node in spec.nodes]
        edges = [
            ProtocolEdge(
                source_node_id=dep,
                target_node_id=node.node_id.strip(),
                edge_type=ProtocolEdgeType.CONTROL_DEPENDENCY,
                rationale=f"{node.node_id.strip()} depends on {dep}",
            )
            for node in spec.nodes
            for dep in node.depends_on
        ]
        document = ProtocolJSONLDDocument(
            **{
                "@context": "https://mica.ai/protocol/v1",
                "@type": "MICAProtocol",
            },
            protocol_id=spec.protocol_id.strip(),
            version=spec.version,
            session_id=spec.session_id.strip(),
            owner_lab=spec.owner_lab.strip() or "MICA GoG Driver V2",
            execution_mode=spec.execution_mode,
            risk_profile=spec.risk_profile,
            budgets=ProtocolBudgetPolicy(
                max_steps=max(len(nodes), 1),
                max_wall_clock_s=300,
                max_tool_calls=max(len(nodes), 1),
            ),
            approval_policy=ProtocolApprovalPolicy(
                mode=ProtocolApprovalMode.AUTO,
                required_approvers=[],
                protected_surfaces=[],
            ),
            ledger_policy=ProtocolLedgerPolicy(
                mode=ProtocolLedgerMode.PROTOCOL_AND_NODE_RECEIPTS,
                receipt_schema="mica.receipts.node.v1",
                emit_events=True,
                require_node_receipts=True,
                require_durable_lineage=False,
            ),
            nodes=nodes,
            edges=edges,
            metadata={
                "name": spec.name,
                "description": spec.description,
                "generated_by": "GogProtocolGeneratorDriver",
                "generator_version": "2.0",
                "quality_threshold": QUALITY_PASS_THRESHOLD,
            },
            parent_graph_id=spec.parent_graph_id,
            graph_level=spec.graph_level,
            campaign_id=spec.campaign_id,
        )
        return validate_protocol_jsonld(document)

    def _validate_spec(self, spec: GogProtocolSpec) -> None:
        if not str(spec.protocol_id or "").strip():
            raise GeneratorSpecError("protocol_id is required")
        if not spec.nodes:
            raise GeneratorSpecError("nodes must contain at least one node")
        if spec.graph_level not in self._ALLOWED_GRAPH_LEVELS:
            raise GeneratorSpecError(f"graph_level must be one of {sorted(self._ALLOWED_GRAPH_LEVELS)}")
        seen: set[str] = set()
        for node in spec.nodes:
            node_id = str(node.node_id or "").strip()
            if not node_id:
                raise GeneratorSpecError("node_id is required")
            if node_id in seen:
                raise GeneratorSpecError(f"Duplicate node_id: {node_id}")
            seen.add(node_id)
        for node in spec.nodes:
            for dep in node.depends_on:
                if dep not in seen:
                    raise GeneratorSpecError(f"dependency {dep!r} references missing node")
        if self._has_cycle({node.node_id: list(node.depends_on) for node in spec.nodes}):
            raise GeneratorSpecError("dependency cycle detected")

    @staticmethod
    def _has_cycle(dependencies: dict[str, list[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            visiting.add(node_id)
            for dep in dependencies.get(node_id, []):
                if visit(dep):
                    return True
            visiting.remove(node_id)
            visited.add(node_id)
            return False

        return any(visit(node_id) for node_id in dependencies)

    @staticmethod
    def _build_node(spec: GogNodeSpec) -> ProtocolNode:
        node_id = spec.node_id.strip()
        objective = spec.objective.strip() or f"Execute GoG child graph node {node_id}"
        child_graph_id = spec.child_graph_id or f"child-graph-{node_id}"
        phase_id = spec.phase_id or f"phase-{node_id}"
        semantic_group = spec.semantic_group or "workflow"
        return ProtocolNode(
            node_id=node_id,
            node_kind="tool",
            executor_surface="gog_local_workflow",
            executor_id="GogProtocolGeneratorDriver",
            objective=objective,
            dependencies=list(spec.depends_on),
            inputs={
                "tool_name": spec.tool_name or "run_workflow",
                "_mock_block": bool(spec.mock_block),
                "child_graph_id": child_graph_id,
                "phase_id": phase_id,
                "semantic_group": semantic_group,
            },
            expected_outputs={
                "artifact_refs": [f"protocol://{node_id}/artifacts/output"],
                "receipt_ref": f"protocol://{node_id}/receipts/node",
            },
            evidence_requirements=["node_receipt"],
            policies=ProtocolNodePolicies(protected_surface=False, production_compute=False),
            failure_policy="halt",
            receipt_schema=ProtocolReceiptSchema(),
            child_graph_id=child_graph_id,
            phase_id=phase_id,
            semantic_group=semantic_group,
            collapsed_by_default=bool(spec.collapsed_by_default),
            intent_summary=objective,
        )


def _make_v2_gauntlet_spec() -> GogProtocolSpec:
    return GogProtocolSpec(
        protocol_id="gog-driver-generated-campaign-v2",
        graph_level="campaign",
        campaign_id="gog-driver-generated-campaign-v2",
        owner_lab="MICA GoG Driver V2 Gauntlet",
        description="Driver-generated campaign: workflow-success completes, workflow-blocked fails EvidenceGate.",
        name="GoG Driver Generated Campaign V2",
        version="1.0.0",
        execution_mode="development",
        risk_profile="low",
        nodes=[
            GogNodeSpec(
                node_id="workflow-success",
                objective="Execute success workflow - load and validate data.",
                depends_on=[],
                child_graph_id="phase-success-001",
                phase_id="success_phase",
                semantic_group="validation",
                collapsed_by_default=False,
                mock_block=False,
                tool_name="run_workflow",
            ),
            GogNodeSpec(
                node_id="workflow-blocked",
                objective="Execute blocked workflow - contradicted by EvidenceGate.",
                depends_on=["workflow-success"],
                child_graph_id="phase-blocked-002",
                phase_id="analysis_phase",
                semantic_group="analysis",
                collapsed_by_default=True,
                mock_block=True,
                tool_name="run_analysis_workflow",
            ),
        ],
    )

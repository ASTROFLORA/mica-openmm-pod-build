"""protocol_authoring_service.py — Service and router for template-based Protocol Authoring."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.agentic.backend_command_manifest import BACKEND_COMMAND_MANIFEST
from mica_q.protocol_jsonld_validator import validate_protocol_jsonld

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kernel/protocols", tags=["protocol-authoring"])


class ProtocolAuthorRequest(BaseModel):
    goal: str
    workspace_id: str
    study_id: str
    allowed_capabilities: List[str] = Field(default_factory=list)
    available_artifacts: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    max_nodes: int = 20
    desired_outputs: List[str] = Field(default_factory=list)
    safety_mode: str = "sandbox"


class ProtocolAuthorResponse(BaseModel):
    protocol_proposal_id: str
    protocol_document: Dict[str, Any]
    compiler_id: str
    compiler_version: str
    source_context_refs: List[str]
    required_capabilities: List[str]
    missing_capabilities: List[str]
    blocked_capabilities: List[str]
    human_summary: str
    risk_notes: str
    validation_ready: bool


def _get_template_nodes_and_edges(template_name: str) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Return nodes, edges, and required command names for the specified template."""
    if template_name == "kb_literature_bootstrap":
        nodes = [
            {
                "node_id": "create-kb-node",
                "node_kind": "step",
                "objective": "Create Literature Knowledge Base",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": [],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "kb.create",
                    "name": "Literature Bootstrap KB",
                    "kb_type": "query"
                },
                "policies": {}
            },
            {
                "node_id": "ingest-kb-node",
                "node_kind": "step",
                "objective": "Ingest Sources to Knowledge Base",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["create-kb-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "kb.ingest",
                    "kb_id": "${create-kb-node.state_after.kb_id}",
                    "documents": [{"content": "Mock document citation content"}]
                },
                "policies": {}
            },
            {
                "node_id": "search-kb-node",
                "node_kind": "step",
                "objective": "Query Knowledge Base Semantically",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["ingest-kb-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "kb.semantic_search",
                    "kb_id": "${create-kb-node.state_after.kb_id}",
                    "query": "active compounds"
                },
                "policies": {}
            },
            {
                "node_id": "attach-artifact-node",
                "node_kind": "step",
                "objective": "Attach Search Results to Study",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["search-kb-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "artifact.attach_to_study",
                    "artifact_id": "art-literature-results"
                },
                "policies": {}
            }
        ]
        edges = [
            {"source_node_id": "create-kb-node", "target_node_id": "ingest-kb-node", "edge_type": "control_dependency"},
            {"source_node_id": "ingest-kb-node", "target_node_id": "search-kb-node", "edge_type": "control_dependency"},
            {"source_node_id": "search-kb-node", "target_node_id": "attach-artifact-node", "edge_type": "control_dependency"}
        ]
        required = ["kb.create", "kb.ingest", "kb.semantic_search", "artifact.attach_to_study"]
        return nodes, edges, required

    elif template_name == "graph_research_summary":
        nodes = [
            {
                "node_id": "search-kb-node",
                "node_kind": "step",
                "objective": "Perform Semantic Search",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": [],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "kb.semantic_search",
                    "kb_id": "kb-default-id",
                    "query": "primary target receptor"
                },
                "policies": {}
            },
            {
                "node_id": "query-graphrag-node",
                "node_kind": "step",
                "objective": "Query Sentient Knowledge Graph",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["search-kb-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "graphrag.query",
                    "query": "receptor interaction binding"
                },
                "policies": {}
            },
            {
                "node_id": "export-subgraph-node",
                "node_kind": "step",
                "objective": "Export Provenance Subgraph",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["query-graphrag-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "graphrag.export_decision_subgraph",
                    "limit": 10
                },
                "policies": {}
            }
        ]
        edges = [
            {"source_node_id": "search-kb-node", "target_node_id": "query-graphrag-node", "edge_type": "control_dependency"},
            {"source_node_id": "query-graphrag-node", "target_node_id": "export-subgraph-node", "edge_type": "control_dependency"}
        ]
        required = ["kb.semantic_search", "graphrag.query", "graphrag.export_decision_subgraph"]
        return nodes, edges, required

    else:  # study_research_report
        nodes = [
            {
                "node_id": "create-kb-node",
                "node_kind": "step",
                "objective": "Create Knowledge Base",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": [],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "kb.create",
                    "name": "Study Report KB"
                },
                "policies": {}
            },
            {
                "node_id": "search-kb-node",
                "node_kind": "step",
                "objective": "Search Literature",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["create-kb-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "kb.semantic_search",
                    "kb_id": "${create-kb-node.state_after.kb_id}",
                    "query": "study objectives"
                },
                "policies": {}
            },
            {
                "node_id": "attach-artifact-node",
                "node_kind": "step",
                "objective": "Link Study Artifact",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["search-kb-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "artifact.attach_to_study",
                    "artifact_id": "art-study-citations"
                },
                "policies": {}
            },
            {
                "node_id": "generate-report-node",
                "node_kind": "step",
                "objective": "Generate Analysis Report",
                "executor_surface": "models.invoke",
                "executor_id": "models.invoke",
                "dependencies": ["attach-artifact-node"],
                "failure_policy": "abort",
                "inputs": {
                    "tool_name": "models.invoke",
                    "command": "graphrag.query",
                    "query": "consolidated research summary"
                },
                "policies": {}
            }
        ]
        edges = [
            {"source_node_id": "create-kb-node", "target_node_id": "search-kb-node", "edge_type": "control_dependency"},
            {"source_node_id": "search-kb-node", "target_node_id": "attach-artifact-node", "edge_type": "control_dependency"},
            {"source_node_id": "attach-artifact-node", "target_node_id": "generate-report-node", "edge_type": "control_dependency"}
        ]
        required = ["kb.create", "kb.semantic_search", "artifact.attach_to_study", "graphrag.query"]
        return nodes, edges, required


@router.post("/author", response_model=ProtocolAuthorResponse)
async def author_protocol(
    body: ProtocolAuthorRequest,
    user_id: str = Depends(user_dependency),
):
    """Generate a valid ProtocolJSONLDDocument proposal based on deterministic templates."""
    # Guardrail 1: Check required scope
    if not body.workspace_id or not body.study_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_id and study_id are mandatory parameters for protocol authoring.",
        )

    # Guardrail 2: Check allowed capabilities non-empty
    if not body.allowed_capabilities:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="allowed_capabilities list cannot be empty.",
        )

    # Resolve template from goal/description keywords
    goal_lower = body.goal.lower()
    if "graph" in goal_lower or "sentient" in goal_lower:
        template_name = "graph_research_summary"
    elif "report" in goal_lower or "summary" in goal_lower:
        template_name = "study_research_report"
    else:
        template_name = "kb_literature_bootstrap"

    nodes, edges, required_caps = _get_template_nodes_and_edges(template_name)

    # Guardrail 3: Max nodes limit
    if len(nodes) > body.max_nodes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template requires {len(nodes)} nodes, which exceeds max_nodes limit of {body.max_nodes}.",
        )

    # Capability classification
    missing_capabilities: List[str] = []
    blocked_capabilities: List[str] = []

    for cap in required_caps:
        if cap not in body.allowed_capabilities:
            missing_capabilities.append(cap)
        elif cap in BACKEND_COMMAND_MANIFEST:
            entry = BACKEND_COMMAND_MANIFEST[cap]
            if entry.implemented_status != "implemented":
                blocked_capabilities.append(cap)
        else:
            blocked_capabilities.append(cap)

    validation_ready = len(missing_capabilities) == 0 and len(blocked_capabilities) == 0

    protocol_id = f"protocol-author-{uuid.uuid4()}"
    session_id = f"session-author-{uuid.uuid4()}"

    # Build compliant JSON-LD document structure
    protocol_document = {
        "@context": "https://mica.dev/context/v1",
        "@type": "Protocol",
        "protocol_id": protocol_id,
        "version": "1.0.0",
        "session_id": session_id,
        "owner_lab": "Protocol Authoring Service",
        "execution_mode": "sandbox",
        "risk_profile": "low",
        "budgets": {
            "max_steps": 20,
            "max_usd": 100.0,
            "max_wall_clock_s": 3600,
            "max_gpu_hours": 10.0,
            "max_tool_calls": 50
        },
        "approval_policy": {
            "mode": "auto",
            "required_approvers": [],
            "protected_surfaces": [],
            "allow_emergency_bypass": False
        },
        "ledger_policy": {
            "mode": "node_receipts",
            "receipt_schema": "mica.receipts.node.v1",
            "emit_events": True,
            "require_node_receipts": True,
            "require_durable_lineage": False
        },
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "name": f"Authoring Generated Protocol ({template_name})",
            "workspace_id": body.workspace_id,
            "study_id": body.study_id,
            "goal": body.goal
        }
    }

    # Validate syntax through validate_protocol_jsonld
    if validation_ready:
        try:
            validate_protocol_jsonld(protocol_document)
        except Exception as e:
            logger.error("JSON-LD validation failed on author output: %s", e)
            validation_ready = False

    return ProtocolAuthorResponse(
        protocol_proposal_id=f"proposal-{uuid.uuid4()}",
        protocol_document=protocol_document,
        compiler_id="mica.protocol_author.compiler",
        compiler_version="0.1.0",
        source_context_refs=[f"study://{body.study_id}"],
        required_capabilities=required_caps,
        missing_capabilities=missing_capabilities,
        blocked_capabilities=blocked_capabilities,
        human_summary=f"Bootstrap template '{template_name}' generated with {len(nodes)} nodes.",
        risk_notes="No critical risk profiles detected. Suitable for sandbox execution.",
        validation_ready=validation_ready
    )

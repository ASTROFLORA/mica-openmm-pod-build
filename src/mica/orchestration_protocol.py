"""
Unified orchestration protocol for AgenticDriver worker coordination.

This module defines shared interfaces and metadata schemas enabling:
1. DAG-based workflow orchestration across workers (Alchemist, BioDynamo, SMIC, Cellomics)
2. Standardized metadata exchange (pipeline_trace, tiers_executed, quality_metrics)
3. Inter-worker data handoff via M-UDO envelope with lineage tracking
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class WorkerType(enum.Enum):
    """Standard worker identifiers for orchestration routing."""
    ALCHEMIST = "alchemist"
    BIODYNAMO = "biodynamo_executor"
    SMIC = "smic"
    CELLOMICS = "cellomics"
    GENETICS_RAG = "genetics_rag"
    BIOHYPERGRAPH = "biohypergraph"
    SPECTRA = "spectra"
    DYNAMO = "dynamo"


@dataclass
class WorkerMetadata:
    """Standardized metadata returned by all workers."""
    worker_type: WorkerType
    pipeline_trace: List[str] = field(default_factory=list)
    """Ordered list of executed pipeline steps or tool names."""

    artifacts: Dict[str, Any] = field(default_factory=dict)
    """Map of output artifact names to their locations or inline data."""

    quality_metrics: Dict[str, float] = field(default_factory=dict)
    """Numeric quality indicators: bvs_score, confidence, rmsd, etc."""

    execution_time_ms: Optional[int] = None
    """Total execution duration in milliseconds."""

    errors: List[str] = field(default_factory=list)
    """Non-fatal warnings or error messages encountered during execution."""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_type": self.worker_type.value,
            "pipeline_trace": self.pipeline_trace,
            "artifacts": self.artifacts,
            "quality_metrics": self.quality_metrics,
            "execution_time_ms": self.execution_time_ms,
            "errors": self.errors,
        }


@dataclass
class MUDOEnvelope:
    """
    Morpheus Unified Data Object envelope for inter-worker handoff.
    
    Preserves lineage and intermediate results while enabling modular worker composition.
    """
    data: Dict[str, Any]
    """Core payload (structure, ligand, trajectory, embeddings, etc.)."""

    lineage: List[str] = field(default_factory=list)
    """Ordered history of transformations: ['biodynamo-scaffold-v1.0', 'alchemist-docking-v2.1']"""

    worker_metadata: Dict[str, WorkerMetadata] = field(default_factory=dict)
    """Map from worker_type.value to its execution metadata."""

    session_id: Optional[str] = None
    """Reference to originating AgenticDriver session."""

    def add_worker_result(self, worker_type: WorkerType, metadata: WorkerMetadata, lineage_entry: str) -> None:
        """Record a worker's contribution to the MUDO."""
        self.worker_metadata[worker_type.value] = metadata
        self.lineage.append(lineage_entry)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "data": self.data,
            "lineage": self.lineage,
            "worker_metadata": {k: v.to_dict() for k, v in self.worker_metadata.items()},
            "session_id": self.session_id,
        }


@dataclass
class DAGNode:
    """
    Node in a directed acyclic graph representing a single worker execution step.
    """
    node_id: str
    worker_type: WorkerType
    parameters: Dict[str, Any] = field(default_factory=dict)
    """Worker-specific configuration or tool arguments."""

    dependencies: List[str] = field(default_factory=list)
    """List of node_ids that must complete before this node can execute."""

    def __hash__(self) -> int:
        return hash(self.node_id)


@dataclass
class WorkflowDAG:
    """
    Directed acyclic graph defining a multi-worker orchestration workflow.
    """
    nodes: Dict[str, DAGNode] = field(default_factory=dict)
    """Map from node_id to DAGNode."""

    def add_node(self, node: DAGNode) -> None:
        self.nodes[node.node_id] = node

    def get_executable_nodes(self, completed: set[str]) -> List[DAGNode]:
        """Return nodes whose dependencies are all in the completed set."""
        executable = []
        for node in self.nodes.values():
            if node.node_id in completed:
                continue
            if all(dep in completed for dep in node.dependencies):
                executable.append(node)
        return executable

    def topological_order(self) -> List[DAGNode]:
        """Return nodes in topological sort order, or raise if cycle detected."""
        completed: set[str] = set()
        ordered: List[DAGNode] = []

        while len(completed) < len(self.nodes):
            executable = self.get_executable_nodes(completed)
            if not executable:
                remaining = set(self.nodes.keys()) - completed
                raise ValueError(f"Cycle detected or missing dependencies for nodes: {remaining}")
            for node in executable:
                ordered.append(node)
                completed.add(node.node_id)

        return ordered

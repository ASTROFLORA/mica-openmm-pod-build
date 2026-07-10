"""
MICA State Schema for LangGraph
Wraps MUDOEnvelope in a TypedDict-compatible state for LangGraph StateGraph.
"""
from typing import TypedDict, Annotated, List, Dict, Any
from operator import add


# Sprint 0 S0.1 — back-compat alias so existing `from .state import MICAState`
# still resolves.  New code should import ``LangGraphWorkflowState`` directly.
MICAState = None  # populated after class definition below


class LangGraphWorkflowState(TypedDict):
    """
    State schema for MUDO-based LangGraph workflows.

    .. note::
        This is **not** the canonical ``MICAState`` used by the
        ``AgenticDriver`` dispatch path.  That lives in
        ``drivers/types.py``.  This TypedDict is only for the
        pure-LangGraph MUDO refinement pipelines.

    Architecture:
    - `mudo`: Serialized MUDOEnvelope (core payload)
    - `lineage`: Append-only execution history (reducer: operator.add)
    - `last_quality_grade`: For conditional branching (A+, A, B, C, etc.)
    - `current_node_id`: Tracking execution state
    - `loop_count`: Prevent infinite refinement loops
    """
    
    # Core MUDO payload (dict serialization of MUDOEnvelope)
    mudo: Dict[str, Any]
    """
    Serialized MUDOEnvelope containing:
    - data: {protein_id, structure, trajectory, embeddings, etc.}
    - lineage: ['biodynamo-scaffold-v1', 'alchemist-docking-v2', ...]
    - worker_metadata: {worker_type: WorkerMetadata}
    - session_id: str
    """
    
    # Execution lineage (append-only with reducer)
    lineage: Annotated[List[str], add]
    """
    Ordered list of executed nodes. Uses operator.add reducer to append.
    Example: ['scaffold', 'docking', 'validation', 'docking', 'validation']
                                                    ^^^^^^^^  ^^^^^^^^^^
                                                    Refinement loop
    """
    
    # Conditional routing state
    last_quality_grade: str
    """
    Quality grade from last validation (SMIC worker).
    Used for conditional edges:
    - "A+", "A", "A-" → Export results (publication-ready)
    - "B+", "B", "B-" → Refinement loop (re-docking)
    - "C+", "C", "C-", "F" → Terminate (unrecoverable)
    """
    
    current_node_id: str
    """Current node being executed (for debugging/streaming)"""
    
    # Loop control
    loop_count: int
    """Number of refinement iterations (prevent infinite loops)"""
    
    # Optional: Backend tracking
    backend_trace: Annotated[List[str], add]
    """Track which backends were used: ['openai_native', 'claude_native', ...]"""
    
    # Optional: Error accumulation
    errors: Annotated[List[str], add]
    """Non-fatal errors encountered during execution"""


# Back-compat alias — see note at top of module.
MICAState = LangGraphWorkflowState  # type: ignore[assignment]


def create_initial_state(
    user_input: str,
    session_id: str,
    initial_data: Dict[str, Any] = None,
) -> LangGraphWorkflowState:
    """
    Create initial LangGraphWorkflowState for workflow execution.
    
    Args:
        user_input: User query/prompt
        session_id: Unique session identifier
        initial_data: Optional initial data for MUDO
    
    Returns:
        LangGraphWorkflowState ready for graph.invoke()
    """
    from ..orchestration_protocol import MUDOEnvelope
    
    # Create initial MUDO
    mudo = MUDOEnvelope(
        data=initial_data or {"user_input": user_input},
        session_id=session_id,
    )
    
    return {
        "mudo": mudo.to_dict(),
        "lineage": [],
        "last_quality_grade": "",
        "current_node_id": "START",
        "loop_count": 0,
        "backend_trace": [],
        "errors": [],
    }


def mudo_from_state(state: LangGraphWorkflowState) -> "MUDOEnvelope":
    """
    Extract MUDOEnvelope from state.
    
    Args:
        state: Current LangGraph state
    
    Returns:
        Reconstructed MUDOEnvelope object
    """
    from ..orchestration_protocol import MUDOEnvelope, WorkerMetadata, WorkerType
    
    mudo_dict = state["mudo"]
    
    # Reconstruct worker_metadata objects
    worker_metadata = {}
    for worker_key, metadata_dict in mudo_dict.get("worker_metadata", {}).items():
        worker_metadata[worker_key] = WorkerMetadata(
            worker_type=WorkerType(metadata_dict["worker_type"]),
            pipeline_trace=metadata_dict.get("pipeline_trace", []),
            artifacts=metadata_dict.get("artifacts", {}),
            quality_metrics=metadata_dict.get("quality_metrics", {}),
            execution_time_ms=metadata_dict.get("execution_time_ms"),
            errors=metadata_dict.get("errors", []),
        )
    
    return MUDOEnvelope(
        data=mudo_dict["data"],
        lineage=mudo_dict.get("lineage", []),
        worker_metadata=worker_metadata,
        session_id=mudo_dict.get("session_id"),
    )


def state_from_mudo(
    mudo: "MUDOEnvelope",
    lineage: List[str] = None,
    last_quality_grade: str = "",
    current_node_id: str = "",
    loop_count: int = 0,
    backend_trace: List[str] = None,
    errors: List[str] = None,
) -> LangGraphWorkflowState:
    """
    Create LangGraphWorkflowState from MUDOEnvelope.
    
    Args:
        mudo: MUDOEnvelope object
        lineage: Execution lineage
        last_quality_grade: Quality grade for routing
        current_node_id: Current node
        loop_count: Refinement iteration count
        backend_trace: Backend usage history
        errors: Error accumulation
    
    Returns:
        MICAState for LangGraph
    """
    return {
        "mudo": mudo.to_dict(),
        "lineage": lineage or [],
        "last_quality_grade": last_quality_grade,
        "current_node_id": current_node_id,
        "loop_count": loop_count,
        "backend_trace": backend_trace or [],
        "errors": errors or [],
    }

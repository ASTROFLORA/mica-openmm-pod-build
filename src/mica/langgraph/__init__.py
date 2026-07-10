"""
LangGraph Integration for MICA
Orchestration layer using LangGraph StateGraph with MICA workers.

Preserves:
- MUDOEnvelope as core state object
- TransportLayer with 7 backends
- All 19 workers unchanged

Adds:
- Cyclic workflows (refinement loops)
- Human-in-the-loop (interrupt API)
- Checkpointing/persistence
- Advanced streaming
- A2A deployment compatibility
"""

from .state import (
    LangGraphWorkflowState,
    MICAState,  # back-compat alias → LangGraphWorkflowState
    create_initial_state,
    mudo_from_state,
    state_from_mudo,
)
from .nodes import MICALangGraphNode
from .workflows import (
    create_protein_analysis_workflow,
    create_drug_discovery_workflow,
    create_simple_sequential_workflow,
)

__all__ = [
    "LangGraphWorkflowState",
    "MICAState",  # back-compat alias
    "create_initial_state",
    "mudo_from_state",
    "state_from_mudo",
    "MICALangGraphNode",
    "create_protein_analysis_workflow",
    "create_drug_discovery_workflow",
    "create_simple_sequential_workflow",
]

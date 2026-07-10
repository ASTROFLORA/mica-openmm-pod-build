"""
MICA LangGraph Workflows
Pre-built workflow graphs using LangGraph StateGraph with cyclic refinement.

Key Features:
- Cyclic refinement loops (quality-based re-execution)
- Conditional routing (quality grades → different paths)
- Max iteration limits (prevent infinite loops)
- Compatible with all 7 MICA backends
"""
from typing import Literal
from .state import MICAState, create_initial_state
from .nodes import (
    MICALangGraphNode,
    create_scaffold_node,
    create_docking_node,
    create_validation_node,
)
from ..drivers.agentic_driver import AgenticDriver

# LangGraph imports (conditional - only if installed)
try:
    from langgraph.graph import StateGraph, START, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None
    START = "START"
    END = "END"


def check_validation_quality(state: MICAState) -> Literal["export", "refine", "terminate"]:
    """
    Conditional edge function for validation routing.
    
    Decision logic:
    - A+, A, A- → Export (publication-ready)
    - B+, B, B- → Refine (re-docking if loop_count < 3)
    - C+, C, C-, F → Terminate (unrecoverable)
    
    Args:
        state: Current workflow state
    
    Returns:
        Next node identifier: "export", "refine", or "terminate"
    """
    quality_grade = state.get("last_quality_grade", "")
    loop_count = state.get("loop_count", 0)
    max_loops = 3  # Prevent infinite refinement
    
    # A grades → Export immediately
    if quality_grade in ["A+", "A", "A-"]:
        return "export"
    
    # B grades → Refine if under loop limit
    if quality_grade in ["B+", "B", "B-"]:
        if loop_count < max_loops:
            return "refine"
        else:
            # Max loops reached, export best effort
            return "export"
    
    # C grades and F → Terminate
    return "terminate"


def create_protein_analysis_workflow(
    driver: AgenticDriver,
    scaffold_template: str = "alanine_dipeptide",
    docking_candidates: int = 20,
    validation_tier: int = 3,
    enable_checkpointing: bool = False,
    checkpointer = None,
):
    """
    Create protein analysis workflow with cyclic refinement.
    
    Workflow:
        START → scaffold → docking → validation
                            ↑____________|
                          (if quality = B, loop)
    
    Args:
        driver: AgenticDriver instance
        scaffold_template: BioDynamo template name
        docking_candidates: Number of docking candidates
        validation_tier: SMIC validation tier (1-5)
        enable_checkpointing: Enable state persistence
        checkpointer: Checkpointer instance (InMemorySaver, PostgresSaver, etc.)
    
    Returns:
        Compiled LangGraph StateGraph
    
    Example:
        >>> driver = AgenticDriver()
        >>> workflow = create_protein_analysis_workflow(driver)
        >>> 
        >>> initial_state = create_initial_state(
        ...     user_input="Design COVID-19 inhibitor",
        ...     session_id="exp_001"
        ... )
        >>> 
        >>> # Execute workflow
        >>> result = await workflow.ainvoke(initial_state)
        >>> print(result["lineage"])
        ['scaffold', 'docking', 'validation', 'docking', 'validation']
        #                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        #                                     Refinement loop executed!
    """
    if not LANGGRAPH_AVAILABLE:
        raise ImportError(
            "LangGraph not installed. Run: pip install langgraph langchain-core"
        )
    
    # Create StateGraph
    workflow = StateGraph(MICAState)
    
    # Add nodes using factory functions
    workflow.add_node("scaffold", create_scaffold_node(driver, scaffold_template))
    workflow.add_node("docking", create_docking_node(driver, docking_candidates))
    workflow.add_node("validation", create_validation_node(driver, validation_tier))
    
    # Export node (terminal)
    def export_results(state: MICAState) -> MICAState:
        """Export final results and mark workflow complete."""
        state["current_node_id"] = "export"
        state["lineage"].append("export")
        return state
    
    workflow.add_node("export", export_results)
    
    # Add edges
    workflow.add_edge(START, "scaffold")
    workflow.add_edge("scaffold", "docking")
    workflow.add_edge("docking", "validation")
    
    # CRITICAL: Conditional edge with cyclic refinement
    def routing_with_loop_increment(state: MICAState) -> Literal["export", "refine", "terminate"]:
        """Wrapper that increments loop_count before routing."""
        decision = check_validation_quality(state)
        if decision == "refine":
            state["loop_count"] += 1
        return decision
    
    workflow.add_conditional_edges(
        "validation",
        routing_with_loop_increment,
        {
            "export": "export",
            "refine": "docking",  # ← CYCLE: back to docking
            "terminate": END,
        }
    )
    
    workflow.add_edge("export", END)
    
    # Compile with optional checkpointing
    if enable_checkpointing and checkpointer:
        return workflow.compile(checkpointer=checkpointer)
    else:
        return workflow.compile()


def create_drug_discovery_workflow(
    driver: AgenticDriver,
    enable_parallel: bool = True,
    enable_checkpointing: bool = False,
    checkpointer = None,
):
    """
    Create full drug discovery workflow with parallel execution.
    
    Workflow:
        START → scaffold → [docking + cellomics] → validation → export
                             ^^^^^^^^^^^^^^^^^^^^^^
                             Parallel execution
    
    Args:
        driver: AgenticDriver instance
        enable_parallel: Execute docking and cellomics in parallel
        enable_checkpointing: Enable state persistence
        checkpointer: Checkpointer instance
    
    Returns:
        Compiled LangGraph StateGraph
    """
    if not LANGGRAPH_AVAILABLE:
        raise ImportError(
            "LangGraph not installed. Run: pip install langgraph langchain-core"
        )
    
    workflow = StateGraph(MICAState)
    node_executor = MICALangGraphNode(driver)
    
    # Scaffold node
    workflow.add_node("scaffold", create_scaffold_node(driver))
    
    # Docking node
    workflow.add_node("docking", create_docking_node(driver, candidates=50))
    
    # Cellomics node
    async def cellomics_node(state: MICAState) -> MICAState:
        from ..orchestration_protocol import WorkerType
        return await node_executor.execute_worker_node(
            state,
            WorkerType.CELLOMICS,
            "cellomics",
            {"scale": "multi"},
        )
    workflow.add_node("cellomics", cellomics_node)
    
    # Validation node
    workflow.add_node("validation", create_validation_node(driver, tier=3))
    
    # Export node
    def export_results(state: MICAState) -> MICAState:
        state["current_node_id"] = "export"
        state["lineage"].append("export")
        return state
    workflow.add_node("export", export_results)
    
    # Add edges
    workflow.add_edge(START, "scaffold")
    
    if enable_parallel:
        # Parallel execution: scaffold → [docking + cellomics]
        workflow.add_edge("scaffold", "docking")
        workflow.add_edge("scaffold", "cellomics")
        workflow.add_edge("docking", "validation")
        workflow.add_edge("cellomics", "validation")
    else:
        # Sequential: scaffold → docking → cellomics → validation
        workflow.add_edge("scaffold", "docking")
        workflow.add_edge("docking", "cellomics")
        workflow.add_edge("cellomics", "validation")
    
    workflow.add_edge("validation", "export")
    workflow.add_edge("export", END)
    
    # Compile
    if enable_checkpointing and checkpointer:
        return workflow.compile(checkpointer=checkpointer)
    else:
        return workflow.compile()


def create_simple_sequential_workflow(driver: AgenticDriver):
    """
    Create simple sequential workflow (no cycles).
    
    For backward compatibility with DAGExecutor-style workflows.
    
    Workflow:
        START → scaffold → docking → validation → END
    
    Args:
        driver: AgenticDriver instance
    
    Returns:
        Compiled LangGraph StateGraph
    """
    if not LANGGRAPH_AVAILABLE:
        raise ImportError(
            "LangGraph not installed. Run: pip install langgraph langchain-core"
        )
    
    workflow = StateGraph(MICAState)
    
    workflow.add_node("scaffold", create_scaffold_node(driver))
    workflow.add_node("docking", create_docking_node(driver))
    workflow.add_node("validation", create_validation_node(driver))
    
    workflow.add_edge(START, "scaffold")
    workflow.add_edge("scaffold", "docking")
    workflow.add_edge("docking", "validation")
    workflow.add_edge("validation", END)
    
    return workflow.compile()


# Example usage documentation
"""
Example 1: Basic Workflow Execution
------------------------------------
from mica.drivers.agentic_driver import AgenticDriver
from mica.langgraph import create_protein_analysis_workflow, create_initial_state

driver = AgenticDriver()
workflow = create_protein_analysis_workflow(driver)

initial_state = create_initial_state(
    user_input="Design COVID-19 inhibitor targeting spike protein",
    session_id="exp_001"
)

result = await workflow.ainvoke(initial_state)

print(f"Lineage: {result['lineage']}")
print(f"Quality: {result['last_quality_grade']}")
print(f"Loops: {result['loop_count']}")


Example 2: With Checkpointing (Pause/Resume)
---------------------------------------------
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
workflow = create_protein_analysis_workflow(
    driver,
    enable_checkpointing=True,
    checkpointer=checkpointer
)

config = {"configurable": {"thread_id": "exp_001"}}

# Execute
result = await workflow.ainvoke(initial_state, config)

# Later: Resume from checkpoint
result2 = await workflow.ainvoke(None, config)  # Continues from last checkpoint


Example 3: Streaming Execution
-------------------------------
async for chunk in workflow.astream(initial_state, stream_mode="updates"):
    node_name = list(chunk.keys())[0]
    node_output = chunk[node_name]
    
    print(f"✓ Node {node_name} completed")
    print(f"  Quality: {node_output['last_quality_grade']}")
    print(f"  Lineage: {node_output['lineage']}")


Example 4: Human-in-the-Loop (requires langgraph.types.interrupt)
------------------------------------------------------------------
# In validation node:
from langgraph.types import interrupt, Command

def validation_with_hil(state: MICAState):
    # Execute validation
    state = await node_executor.execute_worker_node(...)
    
    # If borderline, ask human
    if state['last_quality_grade'] in ["B+", "B"]:
        decision = interrupt({
            "task": "Review borderline result",
            "quality": state['last_quality_grade'],
            "data": state['mudo']['data']
        })
        
        if decision == "approve":
            state['last_quality_grade'] = "A-"  # Override
    
    return state

# Run until interrupt
result = await workflow.ainvoke(initial_state, config)
print(result['__interrupt__'])  # Interrupt payload

# Resume with human decision
final = await workflow.ainvoke(Command(resume="approve"), config)
"""

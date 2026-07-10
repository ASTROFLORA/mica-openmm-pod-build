# BioDynamoDriver → LangGraph Migration Plan

**Date**: November 11, 2025  
**Context**: LEARNING_JOURNAL_MPI-UOS_PHASE6_IMPLEMENTATION.md recommendations  
**Status**: 🔴 CRITICAL GAP - BioDynamoDriver still Paradigm 3 (Stateful Nodes)

---

## 📊 Gap Analysis

### Current State (BioDynamoDriver)

**Location**: `src/mica/drivers/biodynamo_driver.py`

**Status**: ❌ **PARADIGM 3 (Stateful Nodes - Anti-Pattern)**

```python
# Current Implementation (PROBLEMATIC)
async def _execute_immd_workflow(self, context, enforce_msrp):
    """
    iMMD workflow - STATEFUL, NO LANGGRAPH
    """
    logger.info("🔄 iMMD Workflow Started")
    
    # TODO: Implement LangGraph iMMD workflow  ← LINE 559
    # For now, return placeholder
    
    return {
        "workflow": "immd_cycle",
        "status": "not_implemented",
        "message": "LangGraph iMMD workflow pending implementation",
    }
```

**Problems**:
- ❌ No LangGraph integration (documented but not implemented)
- ❌ No stateless nodes (would need instance variables for state)
- ❌ No checkpointing (worker failure = complete state loss)
- ❌ No fault tolerance for 3-day iMMD cycles
- ❌ Blocks Phase 6 (Dr. Marcus Weber needs persistent state for meta-cognition)

---

### Target State (DynamoWorker Pattern)

**Location**: `workers/dynamo/worker.py`

**Status**: ✅ **PARADIGM 2 (LangGraph - Stateless + Graph)**

```python
# Target Implementation (PRODUCTION-READY)
async def _execute_morpheus_workflow(self, request, config):
    """
    LangGraph orchestration with stateless nodes + checkpointing
    """
    
    # 1. DEFINE STATELESS NODES
    async def msrp_reasoning_node(state: Dict[str, Any]) -> Dict[str, Any]:
        # Pure function - no self.state
        msrp_result = await self.msrp_wrapper.execute_with_msrp(...)
        return {"lab_report": lab_report, "iteration": state["iteration"] + 1}
    
    async def quality_evaluation_node(state: Dict[str, Any]) -> Dict[str, Any]:
        quality_score = await self.quality_evaluator.evaluate(...)
        return {"quality_score": quality_score}
    
    async def peer_review_node(state: Dict[str, Any]) -> Dict[str, Any]:
        feedback = await self.msrp_wrapper.msrp_engine.pressure_engine.generate_peer_feedback(...)
        return {"peer_feedback": feedback}
    
    # 2. DEFINE CONDITIONAL ROUTING
    def peer_review_gate(state: Dict[str, Any]) -> str:
        if state["quality_score"] >= quality_threshold: return "accept"
        elif state["iteration"] >= max_iterations: return "reject"
        else: return "revise"  # Loop back
    
    # 3. BUILD GRAPH
    workflow_graph = StateGraph(Dict[str, Any])
    workflow_graph.add_node("reasoning", msrp_reasoning_node)
    workflow_graph.add_node("evaluation", quality_evaluation_node)
    workflow_graph.add_node("peer_review", peer_review_node)
    
    workflow_graph.add_edge("reasoning", "evaluation")
    workflow_graph.add_conditional_edges("evaluation", peer_review_gate, {
        "accept": "__end__",
        "reject": "__end__",
        "revise": "peer_review"
    })
    workflow_graph.add_edge("peer_review", "reasoning")  # THE LOOP
    
    # 4. COMPILE WITH CHECKPOINTING
    app = workflow_graph.compile()
    
    # 5. EXECUTE WITH FAULT TOLERANCE
    final_state = await app.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": task_id}}  # Checkpoint ID
    )
    
    return final_state
```

**Advantages**:
- ✅ **Stateless nodes** (pure functions, no instance variables)
- ✅ **Checkpointing** (worker fails → resume from last checkpoint)
- ✅ **Iterative loops** (native conditional edges)
- ✅ **Fault tolerance** (3-day iMMD cycle can resume after crash)
- ✅ **Reproducibility** (deterministic state transitions)
- ✅ **Phase 6 ready** (persistent state for Dr. Weber's analysis)

---

## 🎯 Migration Strategy

### Phase 1: Core iMMD Workflow Migration (Week 4)

**Objective**: Convert `_execute_immd_workflow` to LangGraph pattern

**Steps**:

1. **Define GraphState Schema**:
```python
from typing import TypedDict, List

class iMMDState(TypedDict):
    # Input
    initial_structure: str  # PDB path
    max_cycles: int
    convergence_threshold: float  # L_FES < 0.01
    
    # State tracking
    current_cycle: int
    current_resolution: str  # "CG" or "AA"
    trajectories: List[str]  # Trajectory file paths
    
    # Convergence metrics
    l_fes_history: List[float]
    converged: bool
    
    # Specialist outputs
    sampling_reports: List[Dict]
    state_classifications: List[Dict]
    free_energy_reports: List[Dict]
```

2. **Convert Specialists to Stateless Nodes**:
```python
async def cg_exploration_node(state: iMMDState) -> iMMDState:
    """
    Phase 1: CG Exploration (SamplingOrchestratorAgent)
    STATELESS: Takes state, returns new state
    """
    response = await self.route_to_specialist(
        query=f"Run CG exploration for cycle {state['current_cycle']}",
        specialist_id="sampling_orchestrator",
        enforce_msrp=True,
    )
    
    return {
        **state,  # Preserve existing state
        "trajectories": state["trajectories"] + [response["trajectory_path"]],
        "sampling_reports": state["sampling_reports"] + [response],
    }

async def state_classification_node(state: iMMDState) -> iMMDState:
    """
    Phase 2: State Classification (StateClassifierAgent)
    """
    response = await self.route_to_specialist(
        query=f"Classify conformational states in {state['trajectories'][-1]}",
        specialist_id="state_classifier",
        enforce_msrp=True,
    )
    
    return {
        **state,
        "state_classifications": state["state_classifications"] + [response],
    }

async def aa_refinement_node(state: iMMDState) -> iMMDState:
    """
    Phase 3: AA Refinement (iMMDControlAgent)
    """
    response = await self.route_to_specialist(
        query=f"Switch to AA resolution and refine structures",
        specialist_id="immd_control",
        enforce_msrp=True,
    )
    
    return {
        **state,
        "current_resolution": "AA",
        "trajectories": state["trajectories"] + [response["trajectory_path"]],
    }

async def convergence_check_node(state: iMMDState) -> iMMDState:
    """
    Phase 4: Convergence Check
    """
    # Calculate L_FES (free energy landscape convergence)
    l_fes = calculate_l_fes_metric(state["free_energy_reports"])
    
    return {
        **state,
        "l_fes_history": state["l_fes_history"] + [l_fes],
        "converged": l_fes < state["convergence_threshold"],
        "current_cycle": state["current_cycle"] + 1,
    }
```

3. **Define Conditional Routing**:
```python
def convergence_gate(state: iMMDState) -> str:
    """
    Routing logic: Continue or stop iMMD cycle
    """
    if state["converged"]:
        return "complete"  # L_FES < 0.01 → Success
    elif state["current_cycle"] >= state["max_cycles"]:
        return "max_cycles_reached"  # Failed to converge
    else:
        return "continue"  # Loop back to CG exploration
```

4. **Build LangGraph Workflow**:
```python
async def _execute_immd_langgraph_workflow(
    self,
    context: Dict[str, Any],
    enforce_msrp: bool,
) -> Dict[str, Any]:
    """
    LangGraph-orchestrated iMMD workflow.
    
    Workflow:
    CG Exploration → State Classification → AA Refinement → 
    Convergence Check → IF converged THEN Complete ELSE Loop
    """
    from langgraph.graph import StateGraph
    
    # Build graph
    workflow = StateGraph(iMMDState)
    
    # Add nodes
    workflow.add_node("cg_exploration", cg_exploration_node)
    workflow.add_node("state_classification", state_classification_node)
    workflow.add_node("aa_refinement", aa_refinement_node)
    workflow.add_node("convergence_check", convergence_check_node)
    
    # Add linear edges
    workflow.add_edge("cg_exploration", "state_classification")
    workflow.add_edge("state_classification", "aa_refinement")
    workflow.add_edge("aa_refinement", "convergence_check")
    
    # Add conditional loop
    workflow.add_conditional_edges(
        "convergence_check",
        convergence_gate,
        {
            "complete": "__end__",
            "max_cycles_reached": "__end__",
            "continue": "cg_exploration",  # THE LOOP
        }
    )
    
    # Set entry point
    workflow.set_entry_point("cg_exploration")
    
    # Compile with checkpointing
    app = workflow.compile()
    
    # Initial state
    initial_state: iMMDState = {
        "initial_structure": context.get("pdb_path"),
        "max_cycles": context.get("max_cycles", 10),
        "convergence_threshold": context.get("l_fes_threshold", 0.01),
        "current_cycle": 0,
        "current_resolution": "CG",
        "trajectories": [],
        "l_fes_history": [],
        "converged": False,
        "sampling_reports": [],
        "state_classifications": [],
        "free_energy_reports": [],
    }
    
    # Execute with checkpointing
    task_id = context.get("task_id", str(uuid.uuid4()))
    
    final_state = await app.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": f"immd-{task_id}"}}
    )
    
    return {
        "workflow": "immd_cycle",
        "status": "complete" if final_state["converged"] else "max_cycles_reached",
        "cycles_executed": final_state["current_cycle"],
        "final_l_fes": final_state["l_fes_history"][-1] if final_state["l_fes_history"] else None,
        "converged": final_state["converged"],
        "trajectories": final_state["trajectories"],
        "specialist_reports": {
            "sampling": final_state["sampling_reports"],
            "classification": final_state["state_classifications"],
        },
    }
```

---

### Phase 2: Add Checkpointing Persistence (Week 5)

**Objective**: Enable fault tolerance for long-running workflows

**Steps**:

1. **Add Redis Checkpointer** (production-ready persistence):
```python
from langgraph.checkpoint.redis import RedisSaver

# In BioDynamoDriver.__init__
self.checkpointer = RedisSaver(
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379")
)

# In workflow compilation
app = workflow.compile(checkpointer=self.checkpointer)
```

2. **Add Resume Capability**:
```python
async def resume_immd_workflow(self, task_id: str) -> Dict[str, Any]:
    """
    Resume failed iMMD workflow from checkpoint.
    
    Args:
        task_id: Original task ID (used as thread_id)
    
    Returns:
        Resumed workflow final state
    """
    # Workflow already compiled with checkpointer
    app = self._get_immd_workflow_graph()
    
    # Resume from checkpoint (no initial state needed)
    final_state = await app.ainvoke(
        None,  # State loaded from checkpoint
        config={"configurable": {"thread_id": f"immd-{task_id}"}}
    )
    
    return final_state
```

---

### Phase 3: Phase 6 Meta-Cognitive Integration (Week 6)

**Objective**: Enable Dr. Marcus Weber to analyze iMMD workflow history

**Steps**:

1. **Expose Checkpoint History**:
```python
async def get_immd_workflow_history(self, task_id: str) -> List[iMMDState]:
    """
    Retrieve complete state history for meta-cognitive analysis.
    
    Used by Dr. Marcus Weber to:
    - Identify convergence patterns
    - Detect force field accuracy issues
    - Find sampling coverage gaps
    """
    checkpoints = await self.checkpointer.aget_tuple(
        {"configurable": {"thread_id": f"immd-{task_id}"}}
    )
    
    history = []
    for checkpoint in checkpoints:
        history.append(checkpoint.state)
    
    return history
```

2. **Enhance Proactive Discovery**:
```python
async def proactive_problem_identification(
    self,
    context: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Phase 6: Analyze iMMD history for autonomous discoveries.
    """
    discoveries = []
    
    # Get all completed iMMD workflows
    immd_task_ids = await self._get_all_immd_task_ids()
    
    for task_id in immd_task_ids:
        history = await self.get_immd_workflow_history(task_id)
        
        # Check for non-convergence patterns
        if not history[-1]["converged"]:
            # Analyze L_FES progression
            l_fes_trend = analyze_convergence_trend(history)
            
            if l_fes_trend["status"] == "oscillating":
                discoveries.append({
                    "type": "convergence_failure",
                    "task_id": task_id,
                    "description": (
                        f"iMMD workflow oscillating L_FES: "
                        f"{l_fes_trend['amplitude']:.4f} amplitude. "
                        f"Possible force field mismatch between CG↔AA."
                    ),
                    "recommended_action": "Run PotentialBenchmarkAgent",
                    "severity": "HIGH",
                })
        
        # Check for sampling coverage gaps
        state_distributions = [
            s["state_classifications"][-1]["distribution"]
            for s in history if s["state_classifications"]
        ]
        
        undersampled_states = identify_undersampled_states(state_distributions)
        
        if undersampled_states:
            discoveries.append({
                "type": "sampling_gap",
                "task_id": task_id,
                "description": (
                    f"Undersampled conformational states: {undersampled_states}. "
                    f"SamplingOrchestratorAgent may need RAMD strategy."
                ),
                "recommended_action": "Trigger enhanced sampling",
                "severity": "MEDIUM",
            })
    
    if discoveries:
        logger.info(f"💡 BioDynamo Autonomous Discoveries: {len(discoveries)}")
        self.autonomous_discoveries.extend(discoveries)
    
    return discoveries
```

---

## 📋 Migration Checklist

### Week 4: Core Migration
- [ ] Define `iMMDState` TypedDict schema
- [ ] Convert `cg_exploration_node` to stateless function
- [ ] Convert `state_classification_node` to stateless function
- [ ] Convert `aa_refinement_node` to stateless function
- [ ] Convert `convergence_check_node` to stateless function
- [ ] Implement `convergence_gate` conditional routing
- [ ] Build LangGraph workflow in `_execute_immd_langgraph_workflow`
- [ ] Add `calculate_l_fes_metric` utility function
- [ ] Test with simple protein (e.g., alanine dipeptide)
- [ ] Verify checkpointing works (kill worker mid-cycle, resume)

### Week 5: Production Hardening
- [ ] Add Redis checkpointer integration
- [ ] Implement `resume_immd_workflow` method
- [ ] Add workflow progress monitoring (current cycle, L_FES trend)
- [ ] Add failure recovery tests (simulate OOM, network errors)
- [ ] Benchmark checkpoint overhead (< 5% acceptable)
- [ ] Document checkpoint cleanup strategy (retain last N checkpoints)

### Week 6: Phase 6 Integration
- [ ] Implement `get_immd_workflow_history` method
- [ ] Add `analyze_convergence_trend` utility
- [ ] Add `identify_undersampled_states` utility
- [ ] Enhance `proactive_problem_identification` with iMMD analysis
- [ ] Test autonomous discovery (force non-convergence scenario)
- [ ] Integrate with Dr. Marcus Weber meta-cognitive agent (if available)

---

## 🎓 LEARNING_JOURNAL Alignment

### Entry #3: Stateless Nodes
✅ **AFTER MIGRATION**: All iMMD nodes are pure functions (no `self.state`)

### Entry #4: Paradigm 2 (LangGraph)
✅ **AFTER MIGRATION**: BioDynamoDriver uses StateGraph + conditional edges

### Entry #5: Fault Tolerance
✅ **AFTER MIGRATION**: 3-day iMMD cycles can resume after worker failure

### Entry #6: Phase 6 Proactivity
✅ **AFTER MIGRATION**: Dr. Marcus Weber can analyze complete iMMD history

---

## 📊 Success Metrics

| Metric | Before (Paradigm 3) | After (Paradigm 2) |
|--------|---------------------|---------------------|
| **Fault Tolerance** | 0% (no checkpoints) | 100% (Redis checkpointer) |
| **Reproducibility** | Low (in-memory state) | High (deterministic graph) |
| **State Auditability** | None | Full (checkpoint history) |
| **Worker Scaling** | Blocked (stateful) | Enabled (stateless) |
| **iMMD Cycle Recovery** | Restart from scratch | Resume from last checkpoint |
| **Phase 6 Readiness** | ❌ Not supported | ✅ Full history access |

---

## 🚀 Next Steps

1. **Immediate**: Start Week 4 migration (define `iMMDState`, convert nodes)
2. **Week 5**: Add Redis checkpointing for production deployment
3. **Week 6**: Enable Dr. Marcus Weber's meta-cognitive analysis
4. **Post-Migration**: Apply same pattern to other workflows (drug discovery pipeline, force field benchmarking)

---

**Pattern Template**: Use `DynamoWorker._execute_morpheus_workflow` (lines 300-598) as reference implementation

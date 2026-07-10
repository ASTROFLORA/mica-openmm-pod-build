#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MICA State Helpers for LangGraph StateGraph
============================================

**Sprint 0 — Unified MICAState (S0.1)**

The **canonical** ``MICAState`` class now lives in ``drivers/types.py``
(the runtime dict subclass used everywhere at execution time).  This
module re-exports it for backwards compatibility and keeps the helper
functions that construct/update state dicts.

**Architecture**::

    types.py  ←  single canonical MICAState(dict)
    state.py  ←  helper functions (this file) — re-exports MICAState
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# ── Canonical MICAState lives in types.py ─────────────────────────────────
from .types import MICAState  # noqa: F401  (re-export for back-compat)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def create_initial_state(
    workflow_id: str,
    user_query: str,
    workflow_type: str = "reactive",
    quality_threshold: float = 0.85,
    max_iterations: int = 3,
    run_id: str = "",
    execution_path: str = "",
    delegation_session_id: str = "",
) -> MICAState:
    """
    Create initial MICAState for new workflow.
    
    Args:
        workflow_id: Unique workflow identifier
        user_query: User's natural language query
        workflow_type: "reactive" (user-triggered) or "proactive" (AI-generated)
        quality_threshold: Target quality score (default: 0.85)
        max_iterations: Max refinement loops (default: 3)
        run_id: Canonical run UUID (Sprint 0 S0.3)
        execution_path: Dispatch path taken (Sprint 0 S0.2)
        delegation_session_id: Parent session when spawned as sub-agent
        
    Returns:
        Initialized MICAState ready for LangGraph execution
    """
    
    return MICAState(
        # Sprint 0 canonical fields
        run_id=run_id,
        execution_path=execution_path,
        delegation_session_id=delegation_session_id,
        
        # Session metadata
        workflow_id=workflow_id,
        user_query=user_query,
        workflow_type=workflow_type,
        created_at=datetime.utcnow().isoformat() + "+00:00",
        
        # MSRP protocol
        msrp_current_phase=1,
        msrp_status={},
        problem_statement=None,
        research_questions=[],
        hypotheses=[],
        lab_reports=[],
        analysis_results={},
        conclusions=[],
        validation_status={},
        
        # Execution context
        iteration_count=0,
        max_iterations=max_iterations,
        subtasks=[],
        assigned_workers={},
        quality_score=0.0,
        quality_threshold=quality_threshold,
        quality_metrics={},
        peer_feedback=[],
        final_result=None,
        converged=False,
        
        # Proactive monitoring
        proactive_triggers=[],
        auto_generated_tasks=[],
        gap_signals=[],
        
        # Tool integration
        mcp_tool_results=[],
        specialist_outputs={},
        selected_tools=[],
        tool_capabilities={},
        
        # Governance
        requires_approval=False,
        approval_reason=None,
        estimated_cost_usd=0.0,
        estimated_hours=0.0,
        
        # Provenance
        logs=[],
        errors=[],
        atom_quintuples=[],
        event_log_ids=[]
    )


def update_state(state: MICAState, **updates) -> MICAState:
    """
    Immutable state update (returns new state dict).
    
    Args:
        state: Current state
        **updates: Key-value pairs to update
        
    Returns:
        New state with updates applied
        
    Example:
        >>> new_state = update_state(
        ...     state,
        ...     msrp_current_phase=2,
        ...     iteration_count=state["iteration_count"] + 1
        ... )
    """
    
    return {**state, **updates}


def append_to_list_field(
    state: MICAState, 
    field_name: str, 
    item: Any
) -> MICAState:
    """
    Append item to list field in state (immutable).
    
    Args:
        state: Current state
        field_name: Name of list field (e.g., "logs", "lab_reports")
        item: Item to append
        
    Returns:
        New state with item appended
        
    Example:
        >>> new_state = append_to_list_field(
        ...     state,
        ...     "logs",
        ...     {"timestamp": "...", "message": "..."}
        ... )
    """
    
    current_list = state.get(field_name, [])
    new_list = current_list + [item]
    return update_state(state, **{field_name: new_list})


def log_event(
    state: MICAState,
    event_type: str,
    message: str,
    **metadata
) -> MICAState:
    """
    Add log entry to state.
    
    Args:
        state: Current state
        event_type: Type of event (e.g., "node_executed", "quality_check")
        message: Human-readable message
        **metadata: Additional key-value pairs
        
    Returns:
        New state with log entry
        
    Example:
        >>> new_state = log_event(
        ...     state,
        ...     "quality_check",
        ...     "Quality score below threshold",
        ...     score=0.72,
        ...     threshold=0.85
        ... )
    """
    
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "+00:00",
        "event_type": event_type,
        "message": message,
        **metadata
    }
    
    return append_to_list_field(state, "logs", log_entry)

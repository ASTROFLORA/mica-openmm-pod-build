#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MICA Driver Types & State Definitions
=======================================

Extracted from ``agentic_driver.py`` (Phase 1 — Blueprint v3 §4.1).

Contains:
- ``MICAState`` — runtime state dict subclass used by LangGraph nodes
- ``TaskType`` / ``WorkflowState`` — routing enums
- ``ToolExecutionHook`` — protocol for tool-call interception

The canonical ``MICAState`` TypedDict in ``state.py`` is kept as the *schema
reference* and documentation anchor.  This module holds the **runtime** dict
subclass that the driver actually instantiates and mutates at execution time.

Both definitions are intentionally maintained:
- ``types.MICAState`` (dict subclass) → used by ``AgenticDriver`` + nodes
- ``state.MICAState`` (TypedDict, total=False) → used for static type
  checking, ``create_initial_state()`` factory, and documentation
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from typing_extensions import Protocol


# ============================================================================
# RUNTIME STATE (LangGraph dict subclass)
# ============================================================================

class MICAState(dict):
    """
    Central state object for LangGraph StateGraph (v3.0 SOTA).

    This replaces the stateful ``AgenticSession`` pattern from v2.0.
    All workflow state lives here and is externalised to
    ``PostgreSQL / AsyncSqliteSaver``.

    Key Principle (``DRIVERSOTADEEPR.MD``):
    - Nodes are STATELESS: ``def node(state: MICAState) -> MICAState``
    - Control flow in GRAPH: not inside nodes
    - State updates via RETURN: not ``self.attribute`` mutation
    """

    # ── Sprint 0 canonical fields ──────────────────────────────────────────
    run_id: str                     # UUID propagated to every event and log
    execution_path: str             # "direct" | "langgraph" | "agentic_loop" | "auto"
    delegation_session_id: str      # parent session when spawned as sub-agent

    # Session metadata
    workflow_id: str
    session_id: str
    user_query: str
    workflow_type: str  # "reactive" or "proactive"

    # MSRP tracking
    msrp_current_phase: int  # 1-5
    msrp_status: Dict[str, Any]

    # Execution state
    iteration_count: int
    max_iterations: int
    quality_threshold: float

    # Task decomposition
    intent: Dict[str, Any]
    subtasks: List[Dict[str, Any]]
    assigned_workers: Dict[str, str]

    # Results
    lab_reports: List[Dict[str, Any]]
    peer_feedback: List[Dict[str, Any]]
    quality_score: float
    quality_metrics: Dict[str, Any]
    specialist_outputs: Dict[str, Any]
    mcp_tool_results: List[Dict[str, Any]]

    # Proactive monitoring (Phase 6)
    proactive_triggers: List[Dict[str, Any]]
    auto_generated_tasks: List[Dict[str, Any]]
    _proactive_spawn_count: int  # Guard against infinite re-spawns

    # Final output
    final_result: Optional[Dict[str, Any]]
    converged: bool

    # Resolved artifact paths (propagated between drivers)
    resolved_pdb_path: str  # Local path from AlchemistDriver._resolve_protein_pdb()

    # Provenance
    created_at: str
    errors: List[str]
    logs: List[Dict[str, Any]]
    requires_approval: bool

    # Thermodynamic Cognition (BioRouter)
    soul: Dict[str, Any]  # Serialized CognitiveAttractorState


# ============================================================================
# ENUMS
# ============================================================================

class TaskType(Enum):
    """Task classification for routing decisions."""
    SIMPLE = "simple"           # Single-step queries (Moonshot)
    MODERATE = "moderate"       # Multi-step queries (Claude)
    COMPLEX = "complex"         # Multi-worker coordination (Gemini Pro)
    RESEARCH = "research"       # Autonomous discovery (Phase 6)


class WorkflowState(Enum):
    """FSM states for workflow execution."""
    ANALYZE = "analyze"
    ROUTE = "route"
    DECOMPOSE = "decompose"
    ASSIGN = "assign"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    SYNTHESIZE = "synthesize"
    COMPLETE = "complete"
    ERROR = "error"


# ============================================================================
# PROTOCOLS
# ============================================================================

class ToolExecutionHook(Protocol):
    """Best-effort hook interface around tool execution."""

    def before_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: Dict[str, Any],
        session_id: str,
    ) -> Any:
        ...

    def after_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: Dict[str, Any],
        session_id: str,
        result: Dict[str, Any],
    ) -> Any:
        ...

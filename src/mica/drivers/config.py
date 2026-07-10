#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MICA Driver Configuration
==========================

Extracted from ``agentic_driver.py`` (Phase 1 — Blueprint v3 §4.1).

Contains:
- ``AgenticDriverConfig``: 60+ field dataclass controlling every tunable knob
  of the AgenticDriver.
- ``AgenticSession``: Legacy per-run session tracker (kept for backward compat
  with the FSM ``agentic_loop`` path).

**Import contract** (Rule 7): ``agentic_driver.py`` re-exports these symbols so
that ``from mica.drivers.agentic_driver import AgenticDriverConfig`` keeps working.
"""

from __future__ import annotations

import uuid
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

# Keep config import cheap. These symbols are only used for type annotations,
# so avoid importing heavy ATOM/BSM stacks at runtime.
if TYPE_CHECKING:
    from ..memory.atom import ATOMMemoryConfig
    from bsm.communication.legacy_reports import (
        LabReport,
        PeerFeedback,
        QualityScore,
    )
else:  # pragma: no cover
    ATOMMemoryConfig = Any  # type: ignore

    class LabReport:  # type: ignore
        pass

    class PeerFeedback:  # type: ignore
        pass

    class QualityScore:  # type: ignore
        pass

from .types import TaskType, WorkflowState

# Dual-driver configuration (rigid production/development separation).
# Optional import — if system_config.py is not yet deployed, the driver
# falls back to its own hardcoded defaults (backward compatible).
try:
    from .system_config import (
        ProductionBioConfig,
        DevelopmentFullConfig,
        load_driver_config,
        DriverConfig,
    )
    _HAS_SYSTEM_CONFIG = True
except ImportError:  # pragma: no cover
    _HAS_SYSTEM_CONFIG = False
    ProductionBioConfig = None  # type: ignore
    DevelopmentFullConfig = None  # type: ignore
    load_driver_config = None  # type: ignore
    DriverConfig = None  # type: ignore


# ============================================================================
# DRIVER CONFIG
# ============================================================================

@dataclass
class AgenticDriverConfig:
    """Configuration for AgenticDriver.

    Every field has a sensible default so that
    ``AgenticDriverConfig()`` is a valid, runnable configuration.

    **Dual-driver architecture**: Use ``from_driver_config()`` to build
    from the rigid production/development separation::

        cfg = AgenticDriverConfig.from_driver_config()
        # Uses MICA_DRIVER_MODE env var to select bio or development

    See: ``DUAL_DRIVER_RIGID_SEPARATION_DIRECTIVE_2026-04-23.md``
    """

    # Checkpointing
    checkpoint_dir: str = "./.checkpoints"
    use_checkpointing: bool = True

    # MCP integration
    mcp_config_path: str = "src/mica/config/mcp_servers.json"
    mcp_enabled: bool = True
    mcp_lazy_connect: bool = True
    mcp_server_allowlist: Optional[List[str]] = None

    # MCP resiliency (timeouts / retries / circuit breaker)
    mcp_tool_timeout_s: float = 30.0
    mcp_tool_max_retries: int = 1
    mcp_tool_retry_backoff_s: float = 0.5
    mcp_tool_retry_backoff_max_s: float = 8.0
    mcp_circuit_breaker_enabled: bool = True
    mcp_circuit_failure_threshold: int = 3
    mcp_circuit_reset_after_s: float = 60.0

    # MCP Resources (Resource Fabric MVP)
    mcp_resources_enabled: bool = True
    mcp_resources_max_chars_per_resource: int = 4000
    mcp_resources_max_total_chars: int = 12000
    mcp_resources_nlp_enabled: bool = True
    mcp_resources_nlp_use_bridge: bool = True
    mcp_resources_nlp_bridge_allow_api: bool = False

    # MQA + structure acquisition
    enable_mqa: bool = True
    mqa_domain: str = "design"
    mqa_fetch_structures: bool = True
    mqa_use_pdb: bool = True
    mqa_use_alphafold: bool = True
    mqa_pdb_tool: Optional[str] = None
    mqa_alphafold_tool: Optional[str] = None

    # Quality control
    quality_threshold: float = 0.85
    max_iterations: int = 25

    # Specialist drivers
    enable_biodynamo: bool = True
    enable_alchemist: bool = True
    enable_smic: bool = True

    # Routing thresholds (token counts)
    simple_task_max_tokens: int = 1000
    moderate_task_max_tokens: int = 4000

    # Meta-cognitive (Phase 6)
    enable_autonomous_discovery: bool = False
    proactive_gap_detection: bool = False

    # Thermodynamic Cognition
    enable_thermodynamic_cognition: bool = True

    # LangGraph v3.0 (SOTA)
    use_langgraph_stategraph: bool = True
    enable_conditional_quality_gates: bool = True
    enable_proactive_monitoring_node: bool = True

    # ATOM temporal memory
    enable_atom_memory: bool = True
    atom_memory_config: Optional[ATOMMemoryConfig] = None
    # R23 store-as-wire: enable TimescaleAtomPersistentStore injection.
    # When True and a Timescale DSN is resolvable, ATOM gains persistence
    # and the W1/W2/W4 cascade activates (R05 injection pattern).
    enable_atom_store: bool = True

    # DLM-LMP Bridge
    enable_bridge: bool = True
    bridge_confidence_threshold: float = 0.8

    # Logging
    log_reasoning_chains: bool = True
    export_provenance: bool = True

    # Tool-use governance / security
    enable_tool_security: bool = True
    enable_tool_governance: bool = True
    governance_cost_threshold_usd: float = 100.0
    governance_auto_approve_threshold_usd: float = 10.0
    governance_autonomous_budget_usd_week: float = 500.0

    # TEA Protocol tracing
    tea_tracing_enabled: bool = False
    tea_trace_dirname: str = "tea_traces"

    # Conversation logging
    conversation_log_enabled: bool = True
    conversation_log_dirname: str = "conversation_logs"
    conversation_log_max_entries: int = 250

    # Saga log + snapshots
    saga_log_enabled: bool = True
    saga_log_dirname: str = "saga_logs"
    saga_log_max_bytes: int = 5_000_000

    snapshots_enabled: bool = True
    snapshots_dirname: str = "snapshots"

    # Per-run provenance manifest
    run_manifest_enabled: bool = True
    run_manifest_dirname: str = "run_manifests"

    # Evaluation report card
    report_card_enabled: bool = True

    # MQA blending weight
    mqa_weight: float = 0.4

    # Backend URL for bucket/workspace tool dispatch in direct-transport mode
    backend_url: Optional[str] = None
    workspace_local_tools_enabled: bool = True
    workspace_publish_run_artifacts: bool = True

    def __post_init__(self) -> None:
        env_mcp_config_path = os.getenv("MICA_MCP_CONFIG_PATH", "").strip()
        if env_mcp_config_path:
            self.mcp_config_path = env_mcp_config_path
        if self.backend_url and not self.backend_url.startswith(("http://", "https://")):
            raise ValueError(
                f"backend_url must start with http:// or https://, got: {self.backend_url!r}"
            )

    @property
    def checkpointing_enabled(self) -> bool:
        """Backward-compatible alias for older runtime checks."""
        return bool(self.use_checkpointing)

    # ------------------------------------------------------------------
    # Dual-driver factory (rigid separation)
    # ------------------------------------------------------------------

    @classmethod
    def from_driver_config(cls, driver_cfg: Optional["DriverConfig"] = None,
                           **overrides: Any) -> "AgenticDriverConfig":
        """Build an AgenticDriverConfig from the dual-driver system config.

        Uses ``MICA_DRIVER_MODE`` env var to select bio (production) or
        development configuration.  The production config has ZERO
        institutional tool fields — they don't exist, not disabled.

        Example::

            cfg = AgenticDriverConfig.from_driver_config()
            # or with explicit config:
            cfg = AgenticDriverConfig.from_driver_config(
                driver_cfg=ProductionBioConfig()
            )
        """
        if driver_cfg is None:
            if _HAS_SYSTEM_CONFIG:
                driver_cfg = load_driver_config()
            # else: fall back to pure defaults (backward compatible)

        kwargs: Dict[str, Any] = {}

        if driver_cfg is not None:
            # Map common fields from driver config to AgenticDriverConfig
            _FIELD_MAP = [
                "quality_threshold", "max_iterations",
                "enable_mqa", "mqa_domain", "mqa_fetch_structures",
                "mqa_use_pdb", "mqa_use_alphafold",
                "enable_biodynamo", "enable_alchemist", "enable_smic",
                "mcp_config_path", "mcp_enabled", "mcp_lazy_connect",
                "mcp_tool_timeout_s", "mcp_tool_max_retries",
                "mcp_tool_retry_backoff_s", "mcp_tool_retry_backoff_max_s",
                "mcp_circuit_breaker_enabled", "mcp_circuit_failure_threshold",
                "mcp_circuit_reset_after_s",
                "mcp_resources_enabled", "mcp_resources_max_chars_per_resource",
                "mcp_resources_max_total_chars",
                "mcp_resources_nlp_enabled", "mcp_resources_nlp_use_bridge",
                "mcp_resources_nlp_bridge_allow_api",
                "log_reasoning_chains", "export_provenance",
                "enable_tool_security", "enable_tool_governance",
                "governance_autonomous_budget_usd_week",
                "tea_tracing_enabled",
                "enable_autonomous_discovery", "proactive_gap_detection",
                "enable_thermodynamic_cognition",
                "enable_atom_memory", "enable_atom_store",
                "enable_bridge", "bridge_confidence_threshold",
                "use_checkpointing", "checkpoint_dir",
                "use_langgraph_stategraph",
                "enable_conditional_quality_gates",
                "enable_proactive_monitoring_node",
                "conversation_log_enabled", "conversation_log_dirname",
                "conversation_log_max_entries",
                "saga_log_enabled", "saga_log_dirname", "saga_log_max_bytes",
                "snapshots_enabled", "snapshots_dirname",
                "run_manifest_enabled", "run_manifest_dirname",
                "report_card_enabled",
                "mqa_weight",
                "workspace_local_tools_enabled", "workspace_publish_run_artifacts",
                "backend_url",
            ]
            for fname in _FIELD_MAP:
                if hasattr(driver_cfg, fname):
                    kwargs[fname] = getattr(driver_cfg, fname)

        # Caller overrides win
        kwargs.update(overrides)

        return cls(**kwargs)

    # ------------------------------------------------------------------
    # DEPRECATED: Legacy mode-toggle factory
    # ------------------------------------------------------------------

    @classmethod
    def from_system_config(cls, sys_cfg: Optional[Any] = None,
                           **overrides: Any) -> "AgenticDriverConfig":
        """DEPRECATED: Use from_driver_config() instead.

        This method used the mode-toggle design which was conceptually
        incorrect. See DUAL_DRIVER_RIGID_SEPARATION_DIRECTIVE_2026-04-23.md.
        """
        import warnings
        warnings.warn(
            "from_system_config() is DEPRECATED. "
            "Use from_driver_config() with ProductionBioConfig or DevelopmentFullConfig.",
            DeprecationWarning, stacklevel=2
        )
        return cls.from_driver_config(driver_cfg=None, **overrides)


# ============================================================================
# LEGACY SESSION TRACKER
# ============================================================================

@dataclass
class AgenticSession:
    """Session tracking for agentic workflow execution.

    Used by the FSM ``agentic_loop`` path.  LangGraph workflows store their
    state in ``MICAState`` instead.
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_query: str = ""
    task_type: Optional[TaskType] = None
    workflow_state: WorkflowState = WorkflowState.ANALYZE

    # Intent analysis
    intent: Dict[str, Any] = field(default_factory=dict)
    subtasks: List[Dict[str, Any]] = field(default_factory=list)

    # Worker assignments
    assigned_workers: Dict[str, str] = field(default_factory=dict)

    # Execution results
    lab_reports: Dict[str, LabReport] = field(default_factory=dict)
    quality_scores: Dict[str, QualityScore] = field(default_factory=dict)
    peer_feedback: Dict[str, List[PeerFeedback]] = field(default_factory=dict)

    # Provenance
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    iterations: int = 0
    errors: List[str] = field(default_factory=list)
    logs: List[Dict[str, Any]] = field(default_factory=list)

    # Final output
    final_result: Optional[Dict[str, Any]] = None
    converged: bool = False
    degradation_flags: List[str] = field(default_factory=list)
    capabilities_unavailable: List[str] = field(default_factory=list)
    fallbacks_used: List[str] = field(default_factory=list)

    # Thermodynamic Cognition
    soul: Dict[str, Any] = field(default_factory=dict)

    # MQA results
    mqa_results: Dict[str, Any] = field(default_factory=dict)

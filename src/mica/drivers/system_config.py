#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MICA Dual Driver Configuration — Rigid Production/Development Separation
========================================================================

**Canonical directive:**
``DUAL_DRIVER_RIGID_SEPARATION_DIRECTIVE_2026-04-23.md``

**Principle:** The production driver does NOT know MICA exists. It is a
bioinformatics AI assistant. No institutional memory, no agent feed, no
code access. Those tools do not exist in its surface — they are not
disabled, they are ABSENT.

The development driver is the AI University operator with full access.

There is NO mode toggle. There are TWO separate configurations with
ZERO overlap in institutional tooling.

Loading contract:
    1. ``MICA_DRIVER_MODE`` env var selects which config to load
    2. ``bio`` → ProductionBioConfig (fail-safe default)
    3. ``development`` → DevelopmentFullConfig
    4. Missing/invalid env var → ProductionBioConfig (fail-safe)
    5. Startup integrity check verifies no institutional tools in bio mode

Usage:
    >>> from mica.drivers.system_config import load_driver_config
    >>> cfg = load_driver_config()  # reads MICA_DRIVER_MODE
    >>> cfg.mcp_config_path
    'src/mica/config/mcp_servers_bio.json'
    >>> isinstance(cfg, ProductionBioConfig)
    True
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# ============================================================================
# PRODUCTION BIO DRIVER CONFIG — Zero institutional knowledge
# ============================================================================

@dataclass
class ProductionBioConfig:
    """Configuration for the production bioinformatics driver.

    This driver has ZERO knowledge of MICA's institutional systems.
    It is a clean bioinformatics AI assistant. No MemPalace, no Feed,
    no Knowledge Graph, no code access, no self-modification.

    These fields DO NOT EXIST on this config:
        - feed_enabled, mempalace_enabled, daily_reports_enabled
        - self_patching, autopoiesis, opencode_swarm
        - introspect_driver, patch_driver, search_github
        - graphrag_enabled, dlm_enabled, bsm_enabled, neo4j_enabled
        - memory_write_mode, context_isolation (always on)

    If you need any of these, you want DevelopmentFullConfig.
    """

    # ------------------------------------------------------------------
    # IDENTITY
    # ------------------------------------------------------------------
    driver_mode: str = "bio"
    version: str = "1.0.0"

    # ------------------------------------------------------------------
    # BIOINFORMATICS TOOLS (all enabled)
    # ------------------------------------------------------------------
    enable_mqa: bool = True
    mqa_domain: str = "design"
    mqa_fetch_structures: bool = True
    mqa_use_pdb: bool = True
    mqa_use_alphafold: bool = True
    mqa_pdb_tool: Optional[str] = None
    mqa_alphafold_tool: Optional[str] = None
    mqa_weight: float = 0.4

    # Specialist drivers (bioinformatics only)
    enable_biodynamo: bool = True
    enable_alchemist: bool = True
    enable_smic: bool = True

    # ------------------------------------------------------------------
    # QUALITY & EPISTEMIC GATES (stricter in production)
    # ------------------------------------------------------------------
    quality_threshold: float = 0.90
    max_iterations: int = 15

    evidence_gate_enabled: bool = True
    min_evidentiality_score: float = 0.8
    require_provenance: bool = True
    block_misleading_support: bool = True

    peer_review_enabled: bool = True
    peer_review_focus: str = "methodology"

    # ------------------------------------------------------------------
    # MCP TRANSPORT (bio servers only)
    # ------------------------------------------------------------------
    mcp_enabled: bool = True
    mcp_lazy_connect: bool = True
    mcp_config_path: str = "src/mica/config/mcp_servers_bio.json"
    mcp_server_allowlist: Optional[List[str]] = None

    # MCP resiliency
    mcp_tool_timeout_s: float = 30.0
    mcp_tool_max_retries: int = 1
    mcp_tool_retry_backoff_s: float = 0.5
    mcp_tool_retry_backoff_max_s: float = 8.0
    mcp_circuit_breaker_enabled: bool = True
    mcp_circuit_failure_threshold: int = 3
    mcp_circuit_reset_after_s: float = 60.0

    # MCP Resources
    mcp_resources_enabled: bool = True
    mcp_resources_max_chars_per_resource: int = 4000
    mcp_resources_max_total_chars: int = 12000
    mcp_resources_nlp_enabled: bool = True
    mcp_resources_nlp_use_bridge: bool = True
    mcp_resources_nlp_bridge_allow_api: bool = False

    # ------------------------------------------------------------------
    # WORKSPACE (user-scoped only)
    # ------------------------------------------------------------------
    workspace_local_tools_enabled: bool = True
    workspace_publish_run_artifacts: bool = True
    context_isolation: bool = True  # ALWAYS on in production
    backend_url: Optional[str] = None

    # ------------------------------------------------------------------
    # CHECKPOINTING
    # ------------------------------------------------------------------
    use_checkpointing: bool = True
    checkpoint_dir: str = "./.checkpoints"

    # LangGraph
    use_langgraph_stategraph: bool = True
    enable_conditional_quality_gates: bool = True
    enable_proactive_monitoring_node: bool = True

    # ------------------------------------------------------------------
    # LOGGING (privacy-focused in production)
    # ------------------------------------------------------------------
    log_reasoning_chains: bool = False  # Disabled for privacy
    export_provenance: bool = True
    conversation_log_enabled: bool = True
    conversation_log_dirname: str = "conversation_logs"
    conversation_log_max_entries: int = 250
    saga_log_enabled: bool = True
    saga_log_dirname: str = "saga_logs"
    saga_log_max_bytes: int = 5_000_000
    snapshots_enabled: bool = True
    snapshots_dirname: str = "snapshots"
    run_manifest_enabled: bool = True
    run_manifest_dirname: str = "run_manifests"
    report_card_enabled: bool = True

    # ------------------------------------------------------------------
    # TEA PROTOCOL (audit trail in production)
    # ------------------------------------------------------------------
    tea_tracing_enabled: bool = True
    tea_trace_dirname: str = "tea_traces"

    # ------------------------------------------------------------------
    # SECURITY (maximum in production)
    # ------------------------------------------------------------------
    enable_tool_security: bool = True
    enable_tool_governance: bool = True
    governance_cost_threshold_usd: float = 100.0
    governance_auto_approve_threshold_usd: float = 10.0
    governance_autonomous_budget_usd_week: float = 50.0
    require_human_approval: bool = True

    # ------------------------------------------------------------------
    # META-COGNITIVE (disabled in production for predictability)
    # ------------------------------------------------------------------
    enable_autonomous_discovery: bool = False
    proactive_gap_detection: bool = False
    enable_thermodynamic_cognition: bool = False

    # ------------------------------------------------------------------
    # ROUTING
    # ------------------------------------------------------------------
    simple_task_max_tokens: int = 1000
    moderate_task_max_tokens: int = 4000

    # ------------------------------------------------------------------
    # ATOM MEMORY (user-scoped only — no institutional ATOM)
    # ------------------------------------------------------------------
    enable_atom_memory: bool = True
    enable_atom_store: bool = True
    atom_memory_config: Any = None  # ATOMMemoryConfig or None

    # ------------------------------------------------------------------
    # BRIDGE
    # ------------------------------------------------------------------
    enable_bridge: bool = True
    bridge_confidence_threshold: float = 0.8

    # ------------------------------------------------------------------
    # INTEGRITY CHECK
    # ------------------------------------------------------------------
    def verify_integrity(self) -> list[str]:
        """Verify that NO institutional tools are present.

        Returns list of violation strings (empty = all clear).
        Called during startup. Any violation → STARTUP FAILURE.
        """
        violations: list[str] = []

        # These fields MUST NOT exist on ProductionBioConfig
        forbidden_fields = [
            "feed_enabled", "mempalace_enabled", "daily_reports_enabled",
            "mirror_enabled", "self_patching", "autopoiesis",
            "opencode_swarm", "introspect_driver", "patch_driver",
            "search_github", "py_compile_check", "graphrag_enabled",
            "dlm_enabled", "bsm_enabled", "neo4j_enabled",
            "memory_write_mode",
        ]
        for fname in forbidden_fields:
            if hasattr(self, fname):
                violations.append(
                    f"CRITICAL: ProductionBioConfig must NOT have field '{fname}'"
                )

        # These MUST be the production values
        if self.min_evidentiality_score < 0.8:
            violations.append(
                f"CRITICAL: evidentiality_threshold {self.min_evidentiality_score} < 0.8"
            )
        if not self.context_isolation:
            violations.append("CRITICAL: context_isolation must be True")
        if not self.require_human_approval:
            violations.append("CRITICAL: require_human_approval must be True")
        if "bio" not in self.mcp_config_path:
            violations.append(
                f"CRITICAL: mcp_config_path must point to bio servers, "
                f"got '{self.mcp_config_path}'"
            )

        return violations

    def summary(self) -> str:
        """Human-readable config summary for startup logging."""
        return "\n".join([
            f"MICA ProductionBioConfig v{self.version}",
            f"  Driver mode:       {self.driver_mode}",
            f"  MCP config:        {self.mcp_config_path}",
            f"  Evidentiality min: {self.min_evidentiality_score}",
            f"  Quality threshold: {self.quality_threshold}",
            f"  Max iterations:    {self.max_iterations}",
            f"  Context isolation: {self.context_isolation}",
            f"  Human approval:    {self.require_human_approval}",
            f"  Reasoning logs:    {self.log_reasoning_chains}",
            f"  TEA tracing:       {self.tea_tracing_enabled}",
            f"  Weekly budget:     ${self.governance_autonomous_budget_usd_week:.0f}",
            f"  Institutional tools: NONE (air gap enforced)",
        ])


# ============================================================================
# DEVELOPMENT FULL DRIVER CONFIG — AI University operator
# ============================================================================

@dataclass
class DevelopmentFullConfig:
    """Configuration for the development driver.

    This driver has FULL access to MICA's institutional systems.
    It is the AI University operator that can improve, debug, and
    extend the entire MICA system.
    """

    # ------------------------------------------------------------------
    # IDENTITY
    # ------------------------------------------------------------------
    driver_mode: str = "development"
    version: str = "1.0.0"

    # ------------------------------------------------------------------
    # ALL BIOINFORMATICS TOOLS (inherited from production)
    # ------------------------------------------------------------------
    enable_mqa: bool = True
    mqa_domain: str = "design"
    mqa_fetch_structures: bool = True
    mqa_use_pdb: bool = True
    mqa_use_alphafold: bool = True
    mqa_pdb_tool: Optional[str] = None
    mqa_alphafold_tool: Optional[str] = None
    mqa_weight: float = 0.4
    enable_biodynamo: bool = True
    enable_alchemist: bool = True
    enable_smic: bool = True

    # ------------------------------------------------------------------
    # QUALITY & EPISTEMIC GATES (more relaxed for iteration speed)
    # ------------------------------------------------------------------
    quality_threshold: float = 0.85
    max_iterations: int = 25
    evidence_gate_enabled: bool = True
    min_evidentiality_score: float = 0.6
    require_provenance: bool = True
    block_misleading_support: bool = True
    peer_review_enabled: bool = True
    peer_review_focus: str = "general"

    # ------------------------------------------------------------------
    # MCP TRANSPORT (all servers)
    # ------------------------------------------------------------------
    mcp_enabled: bool = True
    mcp_lazy_connect: bool = True
    mcp_config_path: str = "src/mica/config/mcp_servers_dev.json"
    mcp_server_allowlist: Optional[List[str]] = None
    mcp_tool_timeout_s: float = 30.0
    mcp_tool_max_retries: int = 1
    mcp_tool_retry_backoff_s: float = 0.5
    mcp_tool_retry_backoff_max_s: float = 8.0
    mcp_circuit_breaker_enabled: bool = True
    mcp_circuit_failure_threshold: int = 3
    mcp_circuit_reset_after_s: float = 60.0
    mcp_resources_enabled: bool = True
    mcp_resources_max_chars_per_resource: int = 4000
    mcp_resources_max_total_chars: int = 12000
    mcp_resources_nlp_enabled: bool = True
    mcp_resources_nlp_use_bridge: bool = True
    mcp_resources_nlp_bridge_allow_api: bool = False

    # ------------------------------------------------------------------
    # WORKSPACE
    # ------------------------------------------------------------------
    workspace_local_tools_enabled: bool = True
    workspace_publish_run_artifacts: bool = True
    context_isolation: bool = False  # Dev allows cross-tenant for debugging
    backend_url: Optional[str] = None

    # ------------------------------------------------------------------
    # CHECKPOINTING
    # ------------------------------------------------------------------
    use_checkpointing: bool = True
    checkpoint_dir: str = "./.checkpoints"
    use_langgraph_stategraph: bool = True
    enable_conditional_quality_gates: bool = True
    enable_proactive_monitoring_node: bool = True

    # ------------------------------------------------------------------
    # LOGGING (verbose for debugging)
    # ------------------------------------------------------------------
    log_reasoning_chains: bool = True
    export_provenance: bool = True
    conversation_log_enabled: bool = True
    conversation_log_dirname: str = "conversation_logs"
    conversation_log_max_entries: int = 250
    saga_log_enabled: bool = True
    saga_log_dirname: str = "saga_logs"
    saga_log_max_bytes: int = 5_000_000
    snapshots_enabled: bool = True
    snapshots_dirname: str = "snapshots"
    run_manifest_enabled: bool = True
    run_manifest_dirname: str = "run_manifests"
    report_card_enabled: bool = True

    # ------------------------------------------------------------------
    # TEA PROTOCOL
    # ------------------------------------------------------------------
    tea_tracing_enabled: bool = False
    tea_trace_dirname: str = "tea_traces"

    # ------------------------------------------------------------------
    # SECURITY (relaxed for dev velocity)
    # ------------------------------------------------------------------
    enable_tool_security: bool = True
    enable_tool_governance: bool = True
    governance_cost_threshold_usd: float = 100.0
    governance_auto_approve_threshold_usd: float = 10.0
    governance_autonomous_budget_usd_week: float = 500.0
    require_human_approval: bool = False

    # ------------------------------------------------------------------
    # META-COGNITIVE (enabled in development)
    # ------------------------------------------------------------------
    enable_autonomous_discovery: bool = True
    proactive_gap_detection: bool = True
    enable_thermodynamic_cognition: bool = True

    # ------------------------------------------------------------------
    # ROUTING
    # ------------------------------------------------------------------
    simple_task_max_tokens: int = 1000
    moderate_task_max_tokens: int = 4000

    # ------------------------------------------------------------------
    # ATOM MEMORY (full access)
    # ------------------------------------------------------------------
    enable_atom_memory: bool = True
    enable_atom_store: bool = True
    atom_memory_config: Any = None

    # ------------------------------------------------------------------
    # BRIDGE
    # ------------------------------------------------------------------
    enable_bridge: bool = True
    bridge_confidence_threshold: float = 0.8

    # ==================================================================
    # INSTITUTIONAL SYSTEMS — ONLY IN DEVELOPMENT
    # These fields DO NOT EXIST on ProductionBioConfig.
    # ==================================================================

    # Institutional memory
    feed_enabled: bool = True
    mempalace_enabled: bool = True
    daily_reports_enabled: bool = True
    mirror_enabled: bool = True
    memory_write_mode: str = "read_write"

    # Self-modification
    self_patching: bool = True
    autopoiesis: bool = True
    opencode_swarm: bool = True
    max_concurrent_agents: int = 3

    # Development tools
    introspect_driver: bool = True
    patch_driver: bool = True
    search_github: bool = True
    py_compile_check: bool = True

    # Institutional systems
    graphrag_enabled: bool = True
    dlm_enabled: bool = True
    bsm_enabled: bool = True
    neo4j_enabled: bool = True

    # ------------------------------------------------------------------
    # INTEGRITY CHECK
    # ------------------------------------------------------------------
    def verify_integrity(self) -> list[str]:
        """Development config always passes (all tools allowed)."""
        return []

    def summary(self) -> str:
        """Human-readable config summary for startup logging."""
        return "\n".join([
            f"MICA DevelopmentFullConfig v{self.version}",
            f"  Driver mode:       {self.driver_mode}",
            f"  MCP config:        {self.mcp_config_path}",
            f"  Evidentiality min: {self.min_evidentiality_score}",
            f"  Quality threshold: {self.quality_threshold}",
            f"  Max iterations:    {self.max_iterations}",
            f"  Context isolation: {self.context_isolation}",
            f"  Human approval:    {self.require_human_approval}",
            f"  Reasoning logs:    {self.log_reasoning_chains}",
            f"  Weekly budget:     ${self.governance_autonomous_budget_usd_week:.0f}",
            f"  Institutional tools: FULL ACCESS",
            f"  Self-patching:     {self.self_patching}",
            f"  Swarm:             {self.opencode_swarm}",
            f"  Feed:              {self.feed_enabled}",
            f"  MemPalace:         {self.mempalace_enabled}",
        ])


# ============================================================================
# CONFIG LOADER
# ============================================================================

# Union type for either config
DriverConfig = Union[ProductionBioConfig, DevelopmentFullConfig]

VALID_DRIVER_MODES = ("bio", "development")


def load_driver_config(
    driver_mode: Optional[str] = None,
) -> DriverConfig:
    """Load the appropriate driver configuration.

    Resolution order:
        1. ``driver_mode`` argument (explicit)
        2. ``MICA_DRIVER_MODE`` environment variable
        3. ``bio`` (production) as fail-safe default

    Args:
        driver_mode: Explicit driver mode ('bio' or 'development').

    Returns:
        ProductionBioConfig or DevelopmentFullConfig.

    Raises:
        ValueError: If driver_mode is not 'bio' or 'development'.
    """
    mode = driver_mode or os.getenv("MICA_DRIVER_MODE", "").strip() or "bio"

    if mode not in VALID_DRIVER_MODES:
        logger.warning(
            f"Invalid MICA_DRIVER_MODE '{mode}'. "
            f"Falling back to 'bio' (production). "
            f"Valid modes: {VALID_DRIVER_MODES}"
        )
        mode = "bio"

    if mode == "bio":
        config = ProductionBioConfig()
    else:
        config = DevelopmentFullConfig()

    # Integrity check
    violations = config.verify_integrity()
    if violations:
        violation_text = "\n".join(f"  - {v}" for v in violations)
        raise RuntimeError(
            f"Driver config integrity check FAILED for mode '{mode}':\n"
            f"{violation_text}\n"
            f"STARTUP ABORTED — fix config before deploying."
        )

    logger.info(config.summary())
    return config


# ============================================================================
# DEPRECATED: Legacy MicaSystemConfig (mode toggle)
# ============================================================================
# The mode-toggle design was conceptually incorrect. It allowed the
# production driver to "know about" institutional tools but have them
# disabled. The correct architecture is two separate configs with zero
# overlap. See DUAL_DRIVER_RIGID_SEPARATION_DIRECTIVE_2026-04-23.md.
#
# MicaSystemConfig is kept for backward compatibility during transition
# but should be removed once all callers migrate to load_driver_config().

_MICA_SYSTEM_CONFIG_DEPRECATION_WARNING = (
    "MicaSystemConfig (mode toggle) is DEPRECATED. "
    "Use load_driver_config() with ProductionBioConfig or DevelopmentFullConfig. "
    "See DUAL_DRIVER_RIGID_SEPARATION_DIRECTIVE_2026-04-23.md"
)


class MicaSystemConfig:
    """DEPRECATED: Mode-toggle config. Use load_driver_config() instead."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        import warnings
        warnings.warn(_MICA_SYSTEM_CONFIG_DEPRECATION_WARNING, DeprecationWarning,
                      stacklevel=2)
        # Delegate to new config
        mode = kwargs.pop("mode", "bio")
        if mode == "production":
            mode = "bio"
        self._config = load_driver_config(driver_mode=mode)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._config, name)

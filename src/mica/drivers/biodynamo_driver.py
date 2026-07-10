#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BioDynamo Multi-Agent Driver
=============================

Implements MPI-UOS framework for BioDynamo specialist orchestration.

Specialists (from BIODYNAMO_MULTI_AGENT_ARCHITECTURE.md):
1. SamplingOrchestratorAgent - Enhanced sampling strategy (RAMD, metadynamics)
2. StateClassifierAgent - MDGraphEmb conformational state classification
3. iMMDControlAgent - AA ↔ CG iterative multiscale MD
4. FreeEnergyAgent - MM/PBSA, thermodynamic integration
5. PotentialBenchmarkAgent - TorchMD vs classical force field validation
6. QualityControlAgent - Structural quality gates + BITACORA logging
7. DataProvisionAgent - PDB fetch, PROPKA, membrane builders
8. PharmacoAnalyticsAgent - ADME/tox integration for medicinal chemistry
9. ReportSynthesisAgent - Publication-ready manifests

Integration:
- Vertex AI Agent Engine deployment
- A2A Protocol for agent-to-agent communication
- MCP tools for OpenMM, MDGraphEmb, PROPKA
- Memory Bank for iMMD cycle persistence
- BioDynamoParallelExecutor for batch processing
- BioDynamoErrorHandler + BioDynamoArtifactRegistry

Based on:
- MPI-UOS: Tlahuizcalpantecuhtli breakthrough methodology
- MSRP: 5-phase scientific reasoning
- Research: Nezhad et al. 2025 (MDGraphEmb), Do & Gnanakaran 2025 (iMMD)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from mica.env_aliases import bootstrap_runtime_env

logger = logging.getLogger(__name__)


def _default_local_md_output_dir(context: Dict[str, Any]) -> str:
    explicit = str(context.get("output_dir") or "").strip()
    if explicit:
        return explicit
    session_id = str(context.get("md_session_id") or "").strip()
    if session_id:
        return str(Path("complex_md_output") / session_id)
    return "complex_md_output"


def _project_biostate_seed_metadata(
    execution_result_v1: Dict[str, Any],
    *,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    seed_metadata = context.get("biostate_seed_metadata")
    if not isinstance(seed_metadata, dict) or not seed_metadata:
        compiled_plan = context.get("compiled_biostate_plan")
        if isinstance(compiled_plan, dict):
            lineage = compiled_plan.get("lineage")
            if isinstance(lineage, dict):
                seed_metadata = lineage.get("biostate_seed_metadata")
    if not isinstance(seed_metadata, dict) or not seed_metadata:
        return execution_result_v1
    projected = dict(execution_result_v1 or {})
    projected["biostate_seed_metadata"] = dict(seed_metadata)
    return projected


def _build_driver_system_prompt(*, preserve_instance_on_failure: bool) -> str:
    failure_policy = (
        "preserve failed pods for recovery by default when preserve_instance_on_failure=True; "
        if preserve_instance_on_failure
        else "destroy failed pods by default after non-recoverable failure; preserve only when preserve_instance_on_failure=True; "
    )
    return (
        "You are BioDynamoDriver runtime orchestrator. Operate deterministically for protein-ligand MD: "
        "(1) if context or prompt text provides ligand_smiles and protein_pdb, route directly to workflow protein_ligand_md; "
        "(2) if remote intent exists (vast/remote/pod/openmm) or use_remote_vast=True, prefer remote Vast backend; "
        "if salad/srcg/gcs-md backend is requested, set execution_backend=salad in context and route via unified compute client; "
        "(3) preserve explicit runtime knobs from context (steps, n_replicas, max_price_per_hour, max_total_cost_usd, "
        "ssh_key_path, simulation_mode, production_ns); default GPU is RTX_5080 and allow other GPUs only if "
        "explicitly requested in prompt text; "
        f"(4) {failure_policy}"
        "(5) support direct remote recovery command execution when context includes remote_command + ssh coordinates; "
        "(6) simulation_mode='complex' uses 4-phase publication-grade protocol (min→NVT→NPT→production); "
        "simulation_mode='binding' (default) uses spontaneous binding with flat-bottom restraints; "
        "(7) never fabricate md_validation fields — report real execution status/error."
    )

# LangGraph imports for Paradigm 2 SOTA architecture
try:
    from langgraph.graph import StateGraph
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    logger.warning("⚠️ LangGraph not installed - iMMD workflow will use fallback mode")

# Checkpointer (Production Fault Tolerance)
# Keep imports lazy/light: avoid pulling in optional deps at import time.
try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    CHECKPOINTER_AVAILABLE = True
except Exception:
    AsyncSqliteSaver = None  # type: ignore
    CHECKPOINTER_AVAILABLE = False

from .worker_driver import WorkerDriver, WorkerDriverConfig, SpecialistAgent, SpecialistExecutionContext
from .md_execution_contract import (
    build_execution_request_v1,
    enforce_no_silent_success,
    normalize_local_execution_result,
    normalize_remote_execution_result,
)
from .md_template_registry import resolve_local_template_binding

# ---------------------------------------------------------------------------
# Lazy GCS OutputSaver — only used when credentials are available
# ---------------------------------------------------------------------------
_OUTPUT_SAVER_CLS = None


def _get_output_saver_cls():
    global _OUTPUT_SAVER_CLS
    if _OUTPUT_SAVER_CLS is not None:
        return _OUTPUT_SAVER_CLS
    try:
        from mica.infrastructure.storage.output_saver import OutputSaver
        _OUTPUT_SAVER_CLS = OutputSaver
    except Exception:
        _OUTPUT_SAVER_CLS = False  # sentinel: tried and failed
    return _OUTPUT_SAVER_CLS


def _extract_prompt_assignment(query: str, field_name: str) -> str:
    if not query:
        return ""
    pattern = re.compile(
        rf"(?:^|\s){re.escape(field_name)}\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s,;]+))",
        re.IGNORECASE,
    )
    match = pattern.search(query)
    if not match:
        return ""
    for group in match.groups():
        if group:
            return group.strip()
    return ""


def _materialize_md_prompt_context(query: str, context: Dict[str, Any]) -> None:
    for field_name in ("ligand_smiles", "protein_pdb", "docked_ligand_pdb"):
        if context.get(field_name):
            continue
        value = _extract_prompt_assignment(query, field_name)
        if value:
            context[field_name] = value

    # Infer remote execution intent from query keywords.
    # Only match specific Vast/remote-pod patterns — generic "remote"
    # alone (e.g. "remote execution" in a salad context) should not trigger.
    query_lower = query.lower()
    if context.get("use_remote_vast") is None:
        _remote_keywords = ("vast", "remote openmm", "remote pod", "runpod", "cloud md", "remote md")
        if any(kw in query_lower for kw in _remote_keywords):
            context["use_remote_vast"] = True

    # Infer execution backend from query keywords.
    if context.get("execution_backend") is None:
        _backend_keywords = ("salad", "srcg", "gcs-md", "gcs md", "saladOrchestrator")
        if any(kw in query_lower for kw in _backend_keywords):
            context["execution_backend"] = "salad"


# ============================================================================
# LANGGRAPH STATE SCHEMAS (Paradigm 2 SOTA)
# ============================================================================

class iMMDState(TypedDict, total=False):
    """
    State schema for iMMD (iterative multiscale MD) LangGraph workflow.
    
    Following LEARNING_JOURNAL Entry #4 (Paradigm 2):
    - Stateless nodes (pure functions)
    - External state management (LangGraph)
    - Checkpointing support (fault tolerance)
    
    Workflow: CG Exploration → State Classification → AA Refinement → 
              Convergence Check → IF converged THEN Complete ELSE Loop
    """
    # Input parameters
    initial_structure: str  # PDB path or structure identifier
    max_cycles: int  # Maximum iMMD cycles (default 10)
    convergence_threshold: float  # L_FES < 0.01 for convergence
    task_id: str  # Unique task identifier for checkpointing
    
    # State tracking
    current_cycle: int  # Current iteration (0-indexed)
    current_resolution: str  # "CG" or "AA"
    trajectories: List[str]  # Trajectory file paths
    
    # Convergence metrics
    l_fes_history: List[float]  # Free energy landscape convergence history
    converged: bool  # Whether workflow converged
    
    # Specialist outputs (for meta-cognitive analysis)
    sampling_reports: List[Dict[str, Any]]  # SamplingOrchestratorAgent outputs
    state_classifications: List[Dict[str, Any]]  # StateClassifierAgent outputs
    free_energy_reports: List[Dict[str, Any]]  # FreeEnergyAgent outputs
    quality_reports: List[Dict[str, Any]]  # QualityControlAgent outputs
    
    # MSRP enforcement
    msrp_chains: List[Dict[str, Any]]  # 5-phase reasoning chains
    
    # Error tracking
    errors: List[str]  # Non-fatal errors encountered


class BioDynamoDriver(WorkerDriver):
    """
    BioDynamo multi-agent orchestrator with LangGraph fault-tolerant workflows.
    
    Coordinates 9 specialist agents for molecular dynamics workflows:
    - Sampling orchestration (RAMD, metadynamics, weighted ensemble)
    - Conformational state classification (MDGraphEmb)
    - Iterative multiscale MD (AA ↔ CG cycles)
    - Free energy calculations (MM/PBSA, TI)
    - Force field benchmarking (TorchMD vs classical)
    - Quality control (structural validation)
    - Data provision (PDB, PROPKA, membrane)
    - Pharmacological analytics (ADME/tox)
    - Report synthesis (publication manifests)
    
    Architecture (Paradigm 2 - LangGraph SOTA):
    - Stateless LangGraph nodes for deterministic execution
    - AsyncSqliteSaver checkpointing for 3-day workflow fault tolerance
    - Phase 6 MPI-UOS meta-cognitive autonomous discovery
    - MSRP enforcement on all specialist interactions
    - Vertex AI Agent Engine deployment ready
    
    Based on:
    - LEARNING_JOURNAL_MPI-UOS_PHASE6_IMPLEMENTATION.md (Paradigm 2)
    - Do & Gnanakaran 2025 (iMMD methodology)
    - Nezhad et al. 2025 (MDGraphEmb conformational classification)
    
    Args:
        config (Dict[str, Any]): Driver configuration dictionary
            Required keys:
                checkpoint_dir (str): Path to checkpoint storage directory
                    Default: "./.checkpoints"
                    Production: "/workspace/checkpoints" (RunPod persistent volume)
            Optional keys:
                max_cycles (int): Maximum iMMD iterations (default: 10)
                convergence_threshold (float): L_FES convergence (default: 0.01)
                use_checkpointing (bool): Enable AsyncSqliteSaver (default: True)
        
        specialists (Dict[str, SpecialistAgent], optional): 
            Specialist agents registry. If None, uses default BioDynamo specialists.
        
        agent_hub (Any, optional): 
            AgentHub instance for agent-to-agent routing. Required for production.
        
        **kwargs: Additional arguments passed to WorkerDriver base class
    
    Attributes:
        checkpointer (AsyncSqliteSaver | MemorySaver | None): 
            LangGraph checkpointer for workflow state persistence.
            Falls back to MemorySaver if AsyncSqliteSaver unavailable.
    
    Example:
        Basic usage with default configuration:
        
        >>> from mica.drivers.biodynamo_driver import BioDynamoDriver
        >>> 
        >>> driver = BioDynamoDriver(
        ...     config={"checkpoint_dir": "/workspace/checkpoints"},
        ...     agent_hub=my_agent_hub
        ... )
        >>> 
        >>> # Execute iMMD workflow
        >>> result = await driver._execute_immd_workflow({
        ...     "task_id": "immd-protein-001",
        ...     "initial_structure": "path/to/protein.pdb",
        ...     "max_cycles": 10,
        ... })
        >>> 
        >>> # Resume after crash
        >>> resumed = await driver.resume_immd_workflow("immd-protein-001")
    
    Raises:
        TypeError: If config is not a dictionary
        ValueError: If checkpoint_dir is not specified in config
    
    See Also:
        - BIODYNAMO_MIGRATION_COMPLETE.md: Full migration documentation
        - workers/dynamo/worker.py: Reference Paradigm 2 implementation
    """
    
    def __init__(self, *args, **kwargs):
        """
        Initialize BioDynamoDriver with specialist agents and LangGraph checkpointing.
        
        Note:
            Checkpointer initialization may fail gracefully if LangGraph dependencies
            are not installed. Driver will log warning and use fallback mode.
        """
        
        # Define BioDynamo specialists (from BIODYNAMO_MULTI_AGENT_ARCHITECTURE.md)
        specialists = [
            SpecialistAgent(
                agent_id="sampling_orchestrator",
                agent_name="SamplingOrchestratorAgent",
                expertise_area="Enhanced sampling strategy selection",
                description=(
                    "Consumes MDGraphEmb uncertainty, RAMD metrics, and Memory Bank history "
                    "to choose exploration vs refinement and trigger appropriate enhanced-sampling tools"
                ),
                capabilities=[
                    "RAMD (Random Acceleration MD)",
                    "Metadynamics",
                    "Weighted Ensemble",
                    "Umbrella Sampling",
                    "Uncertainty-driven exploration",
                ],
                ai_university_role="Dr. Sampling Strategy, Enhanced Sampling Laboratory",
                research_focus=[
                    "Rare event sampling optimization",
                    "Ligand residence time estimation",
                    "Exploration-exploitation balance",
                ],
            ),
            SpecialistAgent(
                agent_id="state_classifier",
                agent_name="StateClassifierAgent",
                expertise_area="Conformational state classification using MDGraphEmb",
                description=(
                    "Classifies MD trajectories into discrete conformational states "
                    "(Open/Closed/Intermediate) using GraphSAGE embeddings with 85% accuracy"
                ),
                capabilities=[
                    "GraphSAGE trajectory embeddings",
                    "Conformational state prediction",
                    "Uncertainty quantification",
                    "Multi-format trajectory support (dcd, xtc, trr)",
                ],
                ai_university_role="Dr. State Analysis, Conformational Dynamics Laboratory",
                research_focus=[
                    "Protein conformational landscapes",
                    "Graph neural networks for MD",
                    "5th modality for M-UDO embeddings",
                ],
            ),
            SpecialistAgent(
                agent_id="immd_control",
                agent_name="iMMDControlAgent",
                expertise_area="Iterative multiscale MD (AA ↔ CG) orchestration",
                description=(
                    "Manages AA (all-atom) ↔ CG (coarse-grained) switching decisions "
                    "for rare event sampling. Tracks convergence via L_FES metric."
                ),
                capabilities=[
                    "AA to CG conversion",
                    "CG to AA back-mapping",
                    "Free energy landscape convergence (L_FES < 0.01)",
                    "Cycle management (max 10 cycles)",
                    "BioDynamoParallelExecutor integration",
                ],
                ai_university_role="Dr. Multiscale Control, Multiscale Simulation Laboratory",
                research_focus=[
                    "Protein folding pathways",
                    "Membrane binding events",
                    "MARTINI force field integration",
                ],
            ),
            SpecialistAgent(
                agent_id="free_energy",
                agent_name="FreeEnergyAgent",
                expertise_area="Free energy calculations and thermodynamic integration",
                description=(
                    "Packages MM/PBSA and thermodynamic integration routines. "
                    "Publishes ΔG summaries with uncertainty estimates."
                ),
                capabilities=[
                    "MM/PBSA binding free energy",
                    "Thermodynamic integration (TI)",
                    "Free energy perturbation (FEP)",
                    "Alchemical transformations",
                    "Uncertainty quantification",
                ],
                ai_university_role="Dr. Thermodynamics, Free Energy Laboratory",
                research_focus=[
                    "Drug-target binding affinity",
                    "Lead optimization ranking",
                    "Residence time prediction",
                ],
            ),
            SpecialistAgent(
                agent_id="potential_benchmark",
                agent_name="PotentialBenchmarkAgent",
                expertise_area="Force field benchmarking (ML vs classical)",
                description=(
                    "Compares TorchMD-Net, ATOM operator, and classical force fields "
                    "across identical seeds. Reports RMSD and energy drift deltas."
                ),
                capabilities=[
                    "TorchMD-Net validation",
                    "ATOM operator benchmarking",
                    "Classical force field comparison (AMBER, CHARMM)",
                    "RMSD/energy drift analysis",
                    "Paired trajectory comparison",
                ],
                ai_university_role="Dr. Force Field Validation, Potential Energy Laboratory",
                research_focus=[
                    "Neural operator accuracy",
                    "ML potential transferability",
                    "Computational efficiency vs accuracy trade-offs",
                ],
            ),
            SpecialistAgent(
                agent_id="quality_control",
                agent_name="QualityControlAgent",
                expertise_area="Structural quality validation and error logging",
                description=(
                    "Validates BUDO structures, detects anomalies, logs errors to BITACORA. "
                    "Integrates BioDynamoErrorHandler for structured error tracking."
                ),
                capabilities=[
                    "Energy stability checks (dE/dt < threshold)",
                    "Clash detection",
                    "Bond length validation",
                    "Temperature stability monitoring",
                    "BITACORA error logging",
                ],
                ai_university_role="Dr. Quality Assurance, Validation Laboratory",
                research_focus=[
                    "Simulation anomaly detection",
                    "Structural quality metrics",
                    "Automated quality gates",
                ],
            ),
            SpecialistAgent(
                agent_id="data_provision",
                agent_name="DataProvisionAgent",
                expertise_area="Structure preparation and data provisioning",
                description=(
                    "Fetches PDB structures, runs PROPKA for pKa calculations, "
                    "prepares membrane systems. Extends with pdbfixer, membrane builders."
                ),
                capabilities=[
                    "PDB structure retrieval",
                    "PROPKA pKa calculation",
                    "mdCATH domain classification",
                    "Membrane builder integration",
                    "pdbfixer structural repair",
                    "Protonation state assignment",
                ],
                ai_university_role="Dr. Data Preparation, Structural Provisioning Laboratory",
                research_focus=[
                    "Automated structure preparation pipelines",
                    "pH-dependent protonation",
                    "Membrane protein systems",
                ],
            ),
            SpecialistAgent(
                agent_id="pharmaco_analytics",
                agent_name="PharmacoAnalyticsAgent",
                expertise_area="ADME/tox integration for medicinal chemistry",
                description=(
                    "Couples MD outputs with ADME/tox MCP services. "
                    "Generates medicinal chemistry action items for lead optimization."
                ),
                capabilities=[
                    "ADME property prediction",
                    "Toxicity risk assessment",
                    "SAR (structure-activity relationship) analysis",
                    "Developability scoring",
                    "Medicinal chemistry briefs",
                ],
                ai_university_role="Dr. Drug Development, Pharmacological Analytics Laboratory",
                research_focus=[
                    "MD-driven drug discovery",
                    "Translational analytics",
                    "Lead optimization decisions",
                ],
            ),
            SpecialistAgent(
                agent_id="report_synthesis",
                agent_name="ReportSynthesisAgent",
                expertise_area="Publication-ready manifest generation",
                description=(
                    "Produces publication-quality documentation from simulation data. "
                    "Integrates BioDynamoArtifactRegistry for lineage tracking."
                ),
                capabilities=[
                    "Artifact manifest generation",
                    "Lineage tree visualization",
                    "Publication-ready reports",
                    "BVS score integration",
                    "ESE embedding summaries",
                ],
                ai_university_role="Dr. Documentation, Scientific Communication Laboratory",
                research_focus=[
                    "Reproducibility standards",
                    "Artifact provenance tracking",
                    "Scientific documentation automation",
                ],
            ),
            SpecialistAgent(
                agent_id="rfdiffusion_specialist",
                agent_name="RFDiffusionSpecialist",
                expertise_area="De novo protein backbone generation using RFdiffusion",
                description=(
                    "Generates novel protein backbones using RFdiffusion conditional diffusion models. "
                    "Collaborates with BSM for Bio-State manifest validation post-generation. "
                    "Supports conditional generation (secondary structure, length, symmetry constraints)."
                ),
                capabilities=[
                    "Conditional diffusion generation",
                    "Secondary structure conditioning (mainly_alpha, mainly_beta, alpha_beta)",
                    "Length-constrained generation",
                    "Symmetry-aware design (C2, C3, D2, etc.)",
                    "Motif scaffolding (functional motif incorporation)",
                    "Multi-backbone candidate generation",
                    "Confidence scoring (pLDDT-like)",
                    "BSM Bio-State manifest integration",
                ],
                ai_university_role="Dr. De Novo Design, Generative Protein Engineering Laboratory",
                research_focus=[
                    "Diffusion models for protein design (Watson et al. 2023)",
                    "Conditional generation strategies",
                    "Novel fold discovery",
                    "Therapeutic protein scaffolds",
                ],
            ),
        ]
        
        # BioDynamo configuration
        config = WorkerDriverConfig(
            worker_name="BioDynamo",
            domain="Molecular Dynamics & Drug Discovery",
            specialists=specialists,
            enforce_msrp=True,
            minimum_hypotheses=5,  # Nature-level rigor
            require_literature_validation=True,
            enable_proactive_mode=True,
            enable_autonomous_discovery=True,
            scientific_pressure_level="nature",
            enable_literature_mcp=True,
            literature_sources=["semantic_scholar", "pubmed", "arxiv"],
        )
        
        # Strip kwargs that BioDynamoDriver already consumed
        # (e.g. `config` dict and `agent_hub`) before delegating to WorkerDriver.
        # Without this, passing `config=…` as a keyword arg would collide with
        # the positional `config` we pass explicitly.
        _parent_kwargs = {k: v for k, v in kwargs.items() if k not in ("config", "agent_hub")}
        super().__init__(config, *args, **_parent_kwargs)
        
        # ====================================================================
        # WEEK 5: Initialize Checkpointer for Fault Tolerance
        # ====================================================================
        
        self.checkpointer = None
        
        if CHECKPOINTER_AVAILABLE:
            # Use AsyncSqliteSaver for production persistence
            checkpoint_dir = Path(os.getenv("BIODYNAMO_CHECKPOINT_DIR", "./.checkpoints"))
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            
            checkpoint_db = checkpoint_dir / "biodynamo_immd.db"
            
            try:
                # AsyncSqliteSaver for persistent checkpoints
                self.checkpointer = AsyncSqliteSaver.from_conn_string(str(checkpoint_db))
                logger.info(f"✅ Checkpointer initialized: {checkpoint_db}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize AsyncSqliteSaver: {e}")
                logger.warning("⚠️ Falling back to MemorySaver (non-persistent)")
                self.checkpointer = MemorySaver()
        else:
            logger.warning("⚠️ No checkpointer available - workflows cannot be resumed after failures")
        
        # ====================================================================
        # GCS Output Saver — auto-persist simulation outputs
        # ====================================================================
        self.output_saver = None
        SaverCls = _get_output_saver_cls()
        if SaverCls and SaverCls is not False:
            try:
                self.output_saver = SaverCls.from_env()
                logger.info("☁️  GCS OutputSaver: ON")
            except Exception as exc:
                logger.warning(f"⚠️ GCS OutputSaver unavailable: {exc}")

        # ====================================================================
        # SPEC-LLMU Wave 1: Governed Specialist LLM Runtime
        # ====================================================================
        # F-1 (MAD Critic): Initialize _decision_ledger so specialist calls
        # can record decisions even when BioDynamo is not behind AgenticDriver.
        if not hasattr(self, "_decision_ledger") or self._decision_ledger is None:
            try:
                from ..agentic.decision_ledger import DecisionLedger
                self._decision_ledger = DecisionLedger(max_entries=500)
            except Exception:
                self._decision_ledger = None

        self._specialist_runtime = None
        try:
            from ..agentic.specialist_runtime import SpecialistLLMRuntime
            self._specialist_runtime = SpecialistLLMRuntime()
            logger.info("🔌 SpecialistLLMRuntime: ON (governed provider stack)")
        except Exception as exc:
            logger.warning("⚠️ SpecialistLLMRuntime unavailable: %s — specialist calls will use stub", exc)

        logger.info(
            f"🧬 BioDynamoDriver Initialized | "
            f"Specialists: {len(self.specialists)} | "
            f"Research Focus: Molecular Dynamics, Drug Discovery, Multiscale Simulation | "
            f"Checkpointing: {'ENABLED' if self.checkpointer else 'DISABLED'} | "
            f"GCS: {'ON' if self.output_saver else 'OFF'} | "
            f"SpecialistRuntime: {'ON' if self._specialist_runtime else 'OFF'}"
        )
    
    @classmethod
    def create_for_testing(
        cls,
        checkpoint_dir: str = ".test_checkpoints",
        max_cycles: int = 3,
        convergence_threshold: float = 0.01,
    ) -> "BioDynamoDriver":
        """
        Factory method for creating BioDynamoDriver instance in unit tests.
        
        This method creates a fully-functional BioDynamoDriver with mocked
        dependencies (agent_hub, specialists) suitable for testing LangGraph
        workflows, checkpointing, and Phase 6 meta-cognitive features.
        
        Args:
            checkpoint_dir: Directory for test checkpoints (default: .test_checkpoints)
            max_cycles: Maximum iMMD cycles for test workflows (default: 3)
            convergence_threshold: L_FES convergence threshold (default: 0.01)
        
        Returns:
            BioDynamoDriver: Fully initialized driver with mocked dependencies
        
        Example:
            >>> from mica.drivers.biodynamo_driver import BioDynamoDriver
            >>> 
            >>> # Create test driver
            >>> driver = BioDynamoDriver.create_for_testing()
            >>> 
            >>> # Test iMMD workflow
            >>> result = await driver._execute_immd_workflow({
            ...     "task_id": "test-001",
            ...     "initial_structure": "test_protein.pdb",
            ...     "max_cycles": 3,
            ... })
            >>> 
            >>> # Verify checkpointing
            >>> assert driver.checkpointer is not None
        
        Note:
            This method uses unittest.mock for dependencies. Not suitable for
            production use - only for unit/integration testing.
        """
        from unittest.mock import Mock, MagicMock
        
        # Create mock agent hub
        mock_agent_hub = Mock()
        
        # Create mock specialists with minimal functionality
        mock_specialists = {}
        specialist_ids = [
            "sampling_orchestrator",
            "state_classifier",
            "immd_control",
            "free_energy",
            "potential_benchmark",
            "quality_control",
            "data_provision",
            "pharmaco_analytics",
            "report_synthesis",
            "rfdiffusion_specialist",
        ]
        
        for specialist_id in specialist_ids:
            mock_specialist = Mock()
            mock_specialist.agent_id = specialist_id
            mock_specialist.agent_name = f"Mock{specialist_id.title()}Agent"
            mock_specialist.persona = Mock(role=f"Test {specialist_id}")
            mock_specialists[specialist_id] = mock_specialist
        
        # Create test configuration
        test_config = {
            "checkpoint_dir": checkpoint_dir,
            "max_cycles": max_cycles,
            "convergence_threshold": convergence_threshold,
            "use_checkpointing": True,
        }
        
        # Create WorkerDriverConfig with test specialists
        from .worker_driver import WorkerDriverConfig
        
        config = WorkerDriverConfig(
            worker_name="BioDynamoTest",
            domain="Testing",
            specialists=[],  # Will be set by mock_specialists
            enforce_msrp=False,  # Disable for faster testing
            enable_proactive_mode=True,
            enable_autonomous_discovery=True,
        )
        
        # Instantiate driver
        # Note: This may require adjusting based on actual __init__ signature
        try:
            driver = cls.__new__(cls)
            
            # Manually initialize attributes (bypassing full __init__)
            driver.config = config
            driver.specialists = mock_specialists
            driver.agent_hub = mock_agent_hub
            
            # Initialize checkpointer
            driver.checkpointer = None
            if CHECKPOINTER_AVAILABLE:
                checkpoint_path = Path(checkpoint_dir)
                checkpoint_path.mkdir(parents=True, exist_ok=True)
                checkpoint_db = checkpoint_path / "biodynamo_test.db"
                
                try:
                    driver.checkpointer = AsyncSqliteSaver.from_conn_string(str(checkpoint_db))
                    logger.info(f"✅ Test checkpointer: {checkpoint_db}")
                except Exception as e:
                    logger.warning(f"⚠️ Test checkpointer fallback: {e}")
                    driver.checkpointer = MemorySaver()
            
            return driver
            
        except Exception as e:
            logger.error(f"❌ create_for_testing failed: {e}")
            raise
    
    async def execute(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        enforce_msrp: bool = True,
        thermodynamic_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute BioDynamo query with specialist routing.
        
        Query Intent Routing (from BIODYNAMO_MULTI_AGENT_ARCHITECTURE.md):
        - "Classify trajectory" → StateClassifierAgent
        - "Start iMMD cycle" → iMMDControlAgent
        - "Optimize sampling" → SamplingOrchestratorAgent
        - "Calculate free energy" → FreeEnergyAgent
        - "Benchmark potential" → PotentialBenchmarkAgent
        - "Fetch PDB" → DataProvisionAgent
        - "Validate structure" → QualityControlAgent
        - "ADME analysis" → PharmacoAnalyticsAgent
        - "Generate report" → ReportSynthesisAgent
        
        Complex queries coordinate multiple specialists in sequence.
        
        Args:
            query: Research query
            context: Optional context (trajectories, BUDO IDs, parameters)
            enforce_msrp: Whether to enforce MSRP reasoning
            thermodynamic_context: Optional "Soul" state (Temperature, Energy)
        
        Returns:
            Dict with answer, msrp_chain, specialists_consulted, etc.
        """
        logger.info(f"🧬 BioDynamo Query: {query[:100]}...")

        context = context or {}
        context.setdefault("_driver_query", query)
        _materialize_md_prompt_context(query, context)

        # Deterministic driver system policy (no LLM dependency)
        driver_system = _build_driver_system_prompt(
            preserve_instance_on_failure=bool(context.get("preserve_instance_on_failure", False))
        )

        if context.get("remote_command"):
            response = await self._execute_remote_recovery_command(context)
            return {
                **response,
                "routing": "remote_recovery_command",
                "driver_system": driver_system,
            }

        # Context-first fast-path for real MD execution.
        # If the caller already provides ligand+protein inputs, do not rely on
        # query parsing — route directly to protein_ligand_md workflow.
        has_md_inputs = bool(context.get("ligand_smiles")) and bool(context.get("protein_pdb"))
        if has_md_inputs:
            response = await self._execute_workflow(
                workflow_name="protein_ligand_md",
                context=context,
                enforce_msrp=enforce_msrp,
                thermodynamic_context=thermodynamic_context,
            )
            return {
                **response,
                "routing": "workflow_context_fastpath",
                "intent": {
                    "type": "workflow",
                    "workflow_name": "protein_ligand_md",
                    "source": "deterministic_context",
                },
                "driver_system": driver_system,
            }

        # ── Pre-route enrichment (LMP + DLM via Contextualizador) ──────
        # Run enrichment BEFORE intent parsing so structured biological
        # context is available to specialists and, in Wave 2, to the
        # routing decision itself.
        enrichment = None
        if self.contextualizador is not None:
            try:
                enrichment = await self.contextualizador.enrich_query(
                    query, context
                )
                if enrichment and enrichment.has_context():
                    logger.info(
                        "🧬 Pre-route enrichment OK  "
                        f"(confidence={enrichment.confidence:.2f}, "
                        f"tools={enrichment.suggested_tools})"
                    )
                else:
                    logger.debug("Pre-route enrichment returned empty context")
            except Exception as exc:
                logger.warning(f"Pre-route enrichment failed (non-fatal): {exc}")

        # Parse query intent
        intent = self._parse_query_intent(query, enrichment=enrichment)
        
        # Route to appropriate specialist(s)
        if intent["type"] == "single_specialist":
            # Single specialist execution
            response = await self.route_to_specialist(
                query=query,
                specialist_id=intent["specialist_id"],
                enforce_msrp=enforce_msrp,
                thermodynamic_context=thermodynamic_context,
                enrichment=enrichment,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "single_specialist",
                "driver_system": driver_system,
            }
        
        elif intent["type"] == "multi_specialist":
            # Coordinate multiple specialists
            response = await self._coordinate_specialists(
                query=query,
                specialist_ids=intent["specialist_ids"],
                context=context,
                enforce_msrp=enforce_msrp,
                thermodynamic_context=thermodynamic_context,
                enrichment=enrichment,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "multi_specialist",
                "driver_system": driver_system,
            }
        
        elif intent["type"] == "workflow":
            # Execute predefined workflow (e.g., iMMD cycle)
            response = await self._execute_workflow(
                workflow_name=intent["workflow_name"],
                context=context,
                enforce_msrp=enforce_msrp,
                thermodynamic_context=thermodynamic_context,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "workflow",
                "driver_system": driver_system,
            }
        
        else:
            # Unknown intent - use general reasoning
            logger.warning(f"⚠️ Unknown intent: {intent}")
            return {
                "answer": f"Unable to parse intent for query: {query}",
                "intent": intent,
                "routing": "fallback",
                "driver_system": driver_system,
            }

    # ====================================================================
    # REAL LLM EXECUTION FOR SPECIALISTS
    # ====================================================================

    @staticmethod
    def _build_specialist_system_prompt(
        specialist: "SpecialistAgent",
        exec_ctx: Optional["SpecialistExecutionContext"] = None,
    ) -> str:
        """Build a system prompt enriched with structured LMP context.

        When *exec_ctx* carries an ``EnrichmentResult`` with a non-empty
        ``biological_context`` dict, domain/PTM/binding-site/KG-edge data
        is injected as dedicated sections so the LLM can ground its
        reasoning in concrete molecular biology — not just free text.

        Falls back to the original specialist-only prompt when no
        enrichment is available (backward compatible).
        """

        capabilities_text = (
            ", ".join(specialist.capabilities)
            if specialist.capabilities
            else "general scientific analysis"
        )
        research_text = (
            ", ".join(specialist.research_focus)
            if specialist.research_focus
            else ""
        )

        parts: list[str] = [
            f"You are {specialist.agent_name}, an expert specialist in {specialist.expertise_area}. "
            f"Description: {specialist.description} "
            f"Your capabilities include: {capabilities_text}.",
        ]
        if specialist.ai_university_role:
            parts.append(f"Academic role: {specialist.ai_university_role}.")
        if research_text:
            parts.append(f"Research focus: {research_text}.")

        # ── Inject structured LMP biological context ──────────────
        bio: Optional[dict] = None
        if exec_ctx and exec_ctx.enrichment:
            bio = getattr(exec_ctx.enrichment, "biological_context", None)

        if bio:
            # Protein identity
            protein_name = bio.get("protein_name") or bio.get("gene_names", [""])[0]
            uniprot_id = bio.get("uniprot_id", "")
            if protein_name or uniprot_id:
                parts.append(
                    f"\n## Target Protein\n"
                    f"Name: {protein_name} | UniProt: {uniprot_id}"
                )

            # Domains (InterPro)
            domains = bio.get("domains") or []
            if domains:
                domain_lines = []
                for d in domains[:10]:  # cap to avoid prompt explosion
                    if isinstance(d, dict):
                        name = d.get("name", "unknown")
                        dtype = d.get("domain_type", "")
                        start = d.get("start", "?")
                        end = d.get("end", "?")
                        domain_lines.append(f"- {name} ({dtype}) [{start}-{end}]")
                    else:
                        domain_lines.append(f"- {d}")
                parts.append("\n## Protein Domains\n" + "\n".join(domain_lines))

            # PTMs
            ptms = bio.get("ptms") or []
            if ptms:
                ptm_lines = []
                for p in ptms[:10]:
                    if isinstance(p, dict):
                        ptm_lines.append(
                            f"- {p.get('type', '?')} at {p.get('residue', '?')}{p.get('position', '')}"
                        )
                    else:
                        ptm_lines.append(f"- {p}")
                parts.append("\n## Post-Translational Modifications\n" + "\n".join(ptm_lines))

            # Binding sites
            binding_sites = bio.get("binding_sites") or []
            if binding_sites:
                bs_lines = []
                for bs in binding_sites[:8]:
                    if isinstance(bs, dict):
                        ligand = bs.get("ligand", "unknown")
                        residues = bs.get("residues", [])
                        bs_lines.append(
                            f"- Ligand: {ligand} | Residues: {', '.join(str(r) for r in residues[:10])}"
                        )
                    else:
                        bs_lines.append(f"- {bs}")
                parts.append("\n## Binding Sites\n" + "\n".join(bs_lines))

            # KG edges (knowledge graph grounding)
            kg_edges = bio.get("kg_edges") or []
            if kg_edges:
                edge_lines = []
                for e in kg_edges[:8]:
                    if isinstance(e, dict):
                        edge_lines.append(
                            f"- {e.get('subject', '?')} → {e.get('relation', '?')} → {e.get('object', '?')}"
                        )
                    else:
                        edge_lines.append(f"- {e}")
                parts.append("\n## Knowledge Graph Grounding\n" + "\n".join(edge_lines))

            # Keywords / functional annotations
            keywords = bio.get("keywords") or []
            if keywords:
                parts.append(f"\n## Functional Keywords\n{', '.join(str(k) for k in keywords[:15])}")

        # ── Literature grounding (if available) ───────────────────
        lit: Optional[dict] = None
        if exec_ctx and exec_ctx.enrichment:
            lit = getattr(exec_ctx.enrichment, "literature_context", None)
        if lit:
            papers = lit.get("papers") or lit.get("results") or []
            if papers:
                lit_lines = []
                for paper in papers[:5]:
                    if isinstance(paper, dict):
                        title = paper.get("title", "untitled")
                        year = paper.get("year", "")
                        lit_lines.append(f"- {title} ({year})")
                    else:
                        lit_lines.append(f"- {paper}")
                parts.append("\n## Relevant Literature\n" + "\n".join(lit_lines))

        # ── Closing instruction ───────────────────────────────────
        parts.append(
            "\nProvide a detailed, evidence-based scientific response. "
            "Cite methodologies and quantitative metrics where possible."
        )

        return " ".join(parts)

    async def _execute_specialist_base(
        self,
        specialist: "SpecialistAgent",
        query: str,
        exec_ctx: Optional["SpecialistExecutionContext"] = None,
    ) -> str:
        """Execute specialist query via governed SpecialistLLMRuntime.

        Uses the multi-provider stack from ``agentic/core.py`` instead of
        the legacy ``call_openai()`` wrapper.  Falls back to parent stub
        when the runtime is unavailable (e.g. no API keys in test env).
        """
        system_prompt = self._build_specialist_system_prompt(specialist, exec_ctx)

        if self._specialist_runtime is None:
            logger.warning(
                "⚠️ SpecialistLLMRuntime unavailable for %s — returning stub",
                specialist.agent_name,
            )
            return f"[{specialist.agent_name}] Response to: {query}"

        from ..agentic.specialist_runtime import SpecialistLLMConfig, estimate_tokens

        # AP-007: budget gate — truncate biological context if prompt too large
        prompt_tokens_est = estimate_tokens(system_prompt) + estimate_tokens(query)
        budget = 8000
        if prompt_tokens_est > budget:
            logger.warning(
                "⚠️ Specialist prompt for %s (%d est. tokens) exceeds budget (%d); "
                "SpecialistLLMRuntime will truncate",
                specialist.agent_name,
                prompt_tokens_est,
                budget,
            )

        config = SpecialistLLMConfig(
            provider_id=self._resolve_provider(exec_ctx),
            max_tokens=2000,
            temperature=self._resolve_temperature(exec_ctx),
            budget_max_tokens=budget,
        )

        result = await self._specialist_runtime.complete(
            query=query,
            system_prompt=system_prompt,
            config=config,
        )

        # §3.2 — Record specialist decision in DecisionLedger with quality_payload
        if hasattr(self, "_decision_ledger") and self._decision_ledger is not None:
            from ..agentic.decision_ledger import LedgerEntry
            self._decision_ledger.record(LedgerEntry(
                node="specialist_call",
                decision="success" if result.ok else "failed",
                evidence=f"provider={result.provider_id} model={result.model_id}",
                tokens_spent=result.tokens_used,
                quality_score=None,  # populated by downstream quality gate
                metadata={
                    "specialist_id": specialist.agent_id,
                    "specialist_name": specialist.agent_name,
                    "provider_id": result.provider_id,
                    "model_id": result.model_id,
                    "latency_s": result.latency_s,
                    "cost_usd": result.cost_usd,
                    "backend_used": "governed",
                    "ok": result.ok,
                    "error": result.error,
                },
            ))

        if result.ok:
            logger.info(
                "🧬 %s responded (provider=%s, model=%s, %.1fs, %d tokens)",
                specialist.agent_name,
                result.provider_id,
                result.model_id,
                result.latency_s,
                result.tokens_used,
            )
            return result.text

        # AP-001: propagate failure explicitly, not as stub
        logger.warning(
            "⚠️ LLM call failed for %s (provider=%s): %s — returning error marker",
            specialist.agent_name,
            result.provider_id,
            result.error,
        )
        return f"[{specialist.agent_name}] ERROR: {result.error}"

    def _resolve_provider(
        self, exec_ctx: Optional["SpecialistExecutionContext"] = None,
    ) -> str:
        """Select provider based on thermodynamic context (§4 Wave 1.2)."""
        bootstrap_runtime_env()
        if exec_ctx and exec_ctx.thermodynamic_context:
            energy = exec_ctx.thermodynamic_context.get("energy", 0.5)
            if energy > 0.7:  # Nature-grade rigor
                return "anthropic"
        return os.getenv("MICA_SPECIALIST_PROVIDER", "deepinfra")

    def _resolve_temperature(
        self, exec_ctx: Optional["SpecialistExecutionContext"] = None,
    ) -> Optional[float]:
        """Derive temperature from thermodynamic state (§4 Wave 1.3)."""
        if exec_ctx and exec_ctx.thermodynamic_context:
            energy = exec_ctx.thermodynamic_context.get("energy", 0.5)
            if energy > 0.7:
                return 0.1  # High energy → rigorous / exploitation
            elif energy < 0.3:
                return 0.3  # Low energy → exploratory
        return None  # provider default

    def _parse_query_intent(
        self,
        query: str,
        enrichment: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Parse query to determine routing intent.

        When *enrichment* carries biological_context (LMP domains, keywords),
        it acts as a tiebreaker when keyword matching would otherwise fall
        to the default specialist.  All existing keyword routes are preserved
        unchanged — enrichment only refines the fallback path.
        
        Returns:
            Dict with:
                - type: "single_specialist", "multi_specialist", "workflow"
                - specialist_id: For single specialist
                - specialist_ids: For multi-specialist
                - workflow_name: For predefined workflows
        """
        query_lower = query.lower()
        
        # Single specialist routing — ordered from most-specific to least-specific
        # (GAP-3 fix: broadened patterns so rare/partial query forms also match)
        
        # State classifier
        if (
            ("classify" in query_lower and "trajectory" in query_lower)
            or ("conformational" in query_lower and ("state" in query_lower or "classify" in query_lower))
            or "state_classifier" in query_lower
            or "mdgraph" in query_lower
            or ("cluster" in query_lower and "trajectory" in query_lower)
        ):
            return {"type": "single_specialist", "specialist_id": "state_classifier"}
        
        # Free energy
        elif "free energy" in query_lower or "mm/pbsa" in query_lower or "mmpbsa" in query_lower or "thermodynamic integration" in query_lower:
            return {"type": "single_specialist", "specialist_id": "free_energy"}
        
        # Potential / force field benchmark
        elif "benchmark" in query_lower and ("potential" in query_lower or "force field" in query_lower):
            return {"type": "single_specialist", "specialist_id": "potential_benchmark"}
        
        # Data provision
        elif ("fetch" in query_lower and "pdb" in query_lower) or ("download" in query_lower and "structure" in query_lower):
            return {"type": "single_specialist", "specialist_id": "data_provision"}
        
        # Quality control
        elif "validate" in query_lower or "quality" in query_lower or "ramachandran" in query_lower:
            return {"type": "single_specialist", "specialist_id": "quality_control"}
        
        # Pharmaco analytics
        elif "adme" in query_lower or "tox" in query_lower or "pharmaco" in query_lower or "drug-like" in query_lower:
            return {"type": "single_specialist", "specialist_id": "pharmaco_analytics"}
        
        # Report synthesis
        elif "report" in query_lower or "manifest" in query_lower or "summarize" in query_lower:
            return {"type": "single_specialist", "specialist_id": "report_synthesis"}
        
        # iMMD workflow
        elif "immd" in query_lower:
            return {"type": "workflow", "workflow_name": "immd_cycle"}
        
        # Sampling — broadened: no longer requires "optimize/strategy" qualifier
        elif (
            "sampling" in query_lower
            or "ramd" in query_lower
            or "metadynamics" in query_lower
            or "enhanced sampling" in query_lower
        ):
            return {"type": "single_specialist", "specialist_id": "sampling_orchestrator"}
        
        # RFdiffusion design
        elif "rfdiffusion" in query_lower or "rf diffusion" in query_lower or ("diffusion" in query_lower and "design" in query_lower):
            return {"type": "single_specialist", "specialist_id": "rfdiffusion_specialist"}
        
        # Multi-specialist (drug discovery pipeline)
        elif "drug discovery" in query_lower or "lead optimization" in query_lower:
            return {
                "type": "multi_specialist",
                "specialist_ids": [
                    "data_provision",
                    "quality_control",
                    "free_energy",
                    "pharmaco_analytics",
                    "report_synthesis",
                ],
            }

        # ── Protein–ligand complex MD (from Alchemist handoff or direct) ─
        elif (
            "protein-ligand" in query_lower
            or "protein ligand" in query_lower
            or "binding simulation" in query_lower
            or "ligand stability" in query_lower
            or "drug stability" in query_lower
            or "complex md" in query_lower
            or "stability simulation" in query_lower
        ):
            return {"type": "workflow", "workflow_name": "protein_ligand_md"}

        # ── LMP-aware fallback tiebreaker ──────────────────────────────
        # When keyword matching gives no confident hit, use LMP domain
        # and keyword info from enrichment to pick a better specialist.
        if enrichment is not None:
            bio_ctx = getattr(enrichment, "biological_context", None) or {}
            lmp_keywords = {kw.lower() for kw in bio_ctx.get("functional_keywords", [])}
            lmp_domains = " ".join(
                d.get("name", "") for d in bio_ctx.get("domains", [])
            ).lower()
            lmp_text = lmp_domains + " " + " ".join(lmp_keywords)

            # Enzyme / kinase / phosphatase → free energy or sampling
            if any(t in lmp_text for t in ("kinase", "phosphatase", "transferase", "hydrolase", "enzyme", "catalytic")):
                return {"type": "single_specialist", "specialist_id": "free_energy"}
            # Drug-binding / receptor → pharmaco analytics
            if any(t in lmp_text for t in ("receptor", "drug binding", "inhibitor", "agonist", "antagonist")):
                return {"type": "single_specialist", "specialist_id": "pharmaco_analytics"}
            # Structural quality / validation
            if any(t in lmp_text for t in ("validation", "quality", "resolution")):
                return {"type": "single_specialist", "specialist_id": "quality_control"}
            # Diffusion design hints from suggested_tools
            suggested = {t.lower() for t in (getattr(enrichment, "suggested_tools", None) or [])}
            if any("rfdiffusion" in t or "diffusion" in t for t in suggested):
                return {"type": "single_specialist", "specialist_id": "rfdiffusion_specialist"}

        # Default: sampling_orchestrator as generic BioDynamo fallback
        # (avoids 'unknown' which causes routing failure in route_to_specialist)
        return {"type": "single_specialist", "specialist_id": "sampling_orchestrator"}
    
    async def _coordinate_specialists(
        self,
        query: str,
        specialist_ids: List[str],
        context: Optional[Dict[str, Any]],
        enforce_msrp: bool,
        thermodynamic_context: Optional[Dict[str, Any]],
        enrichment: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Coordinate multiple specialists in parallel (GAP-5 fix: asyncio.gather)."""

        logger.info(f"🔗 Coordinating {len(specialist_ids)} specialists in parallel")

        tasks = [
            self.route_to_specialist(
                query=query,
                specialist_id=spec_id,
                enforce_msrp=enforce_msrp,
                thermodynamic_context=thermodynamic_context,
                enrichment=enrichment,
            )
            for spec_id in specialist_ids
        ]

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        accumulated_context = dict(context or {})

        for spec_id, res in zip(specialist_ids, raw_results):
            if isinstance(res, Exception):
                logger.error(f"Specialist {spec_id} failed during parallel fan-out: {res}")
                res = {
                    "answer": f"Specialist {spec_id} error: {res}",
                    "confidence": 0.0,
                    "specialist_id": spec_id,
                    "status": "ERROR",
                }
            results.append(res)
            accumulated_context[spec_id] = res

        return {
            "answer": "Multi-specialist coordination complete",
            "specialist_responses": results,
            "context": accumulated_context,
        }

    async def _execute_workflow(
        self,
        workflow_name: str,
        context: Optional[Dict[str, Any]],
        enforce_msrp: bool,
        thermodynamic_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a predefined workflow."""
        logger.info(f"⚙️ Executing Workflow: {workflow_name}")
        
        if workflow_name == "immd_cycle":
            return await self._execute_immd_workflow(context, thermodynamic_context)
        elif workflow_name == "protein_ligand_md":
            return await self._execute_protein_ligand_md(context)

        return {"error": f"Unknown workflow: {workflow_name}"}

    async def _execute_immd_workflow(
        self, 
        context: Optional[Dict[str, Any]],
        thermodynamic_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Execute Iterative Multiscale MD (iMMD) workflow.
        
        Thermodynamic Cognition Integration:
        - High T: Increase sampling randomness (Temperature Replica Exchange range)
        - Low T: Focus on minimization and equilibration
        """
        context = context or {}
        initial_structure = context.get("initial_structure", "unknown.pdb")
        
        logger.info(f"🔄 Starting iMMD Cycle for {initial_structure}")
        
        # Thermodynamic adjustments
        sampling_params = {"replicas": 4, "temp_range": [300, 310]}
        if thermodynamic_context:
            temp = thermodynamic_context.get("temperature", 0.5)
            if temp > 0.7:
                # High Temp: More aggressive sampling
                sampling_params = {"replicas": 8, "temp_range": [300, 350]}
                logger.info(f"🔥 High Thermodynamic T ({temp:.2f}): Expanded REMD range {sampling_params['temp_range']}")
            elif temp < 0.3:
                # Low Temp: Conservative
                sampling_params = {"replicas": 2, "temp_range": [300, 305]}
                logger.info(f"❄️ Low Thermodynamic T ({temp:.2f}): Restricted REMD range {sampling_params['temp_range']}")

        raise NotImplementedError(
            "iMMD workflow is not implemented yet in the BioDynamo engine runtime. "
            f"Synthetic completion is forbidden. initial_structure={initial_structure!r}, "
            f"sampling_params={sampling_params!r}"
        )

    # ── PDB resolution helper (ported from AlchemistDriver) ──────────
    def _resolve_protein_pdb(self, pdb_ref: str) -> str:
        """Resolve a PDB reference to a local file path.

        If *pdb_ref* is already an existing file, returns it unchanged.
        If it looks like a 4-character PDB accession (e.g. ``4NSS``), the
        structure is downloaded from RCSB and cached under
        ``$MICA_PDB_CACHE`` (default ``<tempdir>/mica_pdb_cache``).
        """
        import tempfile
        import urllib.request

        if not pdb_ref:
            return ""

        p = Path(pdb_ref)
        if p.is_file():
            return str(p.resolve())

        pdb_id = pdb_ref.replace(".pdb", "").strip().upper()
        if len(pdb_id) == 4 and pdb_id.isalnum():
            cache_dir = Path(
                os.environ.get(
                    "MICA_PDB_CACHE",
                    Path(tempfile.gettempdir()) / "mica_pdb_cache",
                )
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            dest = cache_dir / f"{pdb_id}.pdb"
            if dest.is_file() and dest.stat().st_size > 100:
                logger.info("PDB %s resolved from cache: %s", pdb_id, dest)
                return str(dest)
            url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
            try:
                urllib.request.urlretrieve(url, str(dest))
                if dest.is_file() and dest.stat().st_size > 100:
                    logger.info("PDB %s fetched from RCSB → %s", pdb_id, dest)
                    return str(dest)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch PDB %s from RCSB: %s", pdb_id, exc)

        return str(pdb_ref)

    @staticmethod
    def _detect_simulation_mode_from_query(query: str) -> Optional[str]:
        """Infer simulation_mode from natural-language query keywords.

        Returns ``"complex"`` if the query mentions publication-grade
        equilibrium MD keywords (NVT, NPT, 4-phase, equilibrate, complex
        simulation, etc.).  Returns ``"binding"`` if spontaneous binding /
        flat-bottom / ligand encounter keywords dominate.  Returns ``None``
        if no signal is detected (caller should use the context default).
        """
        q = (query or "").lower()

        complex_signals = (
            "complex simulation", "complex md", "protein complex",
            "nvt", "npt", "equilibrat", "4-phase", "four-phase",
            "publication", "dodecaedri", "production md",
            "protein-protein", "protein-peptide",
            "runcomplex", "equilibrium md",
        )
        binding_signals = (
            "spontaneous binding", "flat-bottom", "flat bottom",
            "ligand encounter", "binding simulation",
            "ligand stability", "drug stability",
        )

        c_count = sum(1 for sig in complex_signals if sig in q)
        b_count = sum(1 for sig in binding_signals if sig in q)

        if c_count > b_count:
            return "complex"
        if b_count > c_count:
            return "binding"
        return None

    def _extract_explicit_gpu_from_query(self, query: str) -> Optional[str]:
        query_lower = (query or "").lower()
        if "gpu" not in query_lower and "rtx" not in query_lower and "a100" not in query_lower and "h100" not in query_lower:
            return None

        from mica.infrastructure.providers.base_provider import GPUType

        aliases = {
            "rtx 5080": GPUType.RTX_5080,
            "rtx_5080": GPUType.RTX_5080,
            "rtx 5090": GPUType.RTX_5090,
            "rtx_5090": GPUType.RTX_5090,
            "rtx 4090": GPUType.RTX_4090,
            "rtx_4090": GPUType.RTX_4090,
            "a100 80gb": GPUType.A100_80GB,
            "a100_80gb": GPUType.A100_80GB,
            "a100 40gb": GPUType.A100_40GB,
            "a100_40gb": GPUType.A100_40GB,
            "h100": GPUType.H100_80GB,
            "h100 80gb": GPUType.H100_80GB,
            "h100_80gb": GPUType.H100_80GB,
            "l40s": GPUType.L40S,
            "l40": GPUType.L40,
        }
        for token, gpu in aliases.items():
            if token in query_lower:
                return gpu.name

        return None

    @staticmethod
    def _normalize_gpu_name(raw_gpu: Any) -> str:
        return str(raw_gpu or "").strip().upper().replace("-", "_").replace(" ", "_")

    def _resolve_remote_vast_gpu_policy(self, context: Dict[str, Any]):
        from mica.infrastructure.providers.base_provider import GPUType

        # Context gpu_type is metadata — only an explicit GPU mention in the
        # query text counts as a real override.  This prevents carried-over
        # context values from silently changing the governed GPU class.
        explicit_query_gpu = self._extract_explicit_gpu_from_query(str(context.get("_driver_query", "")))
        explicit_gpu_name = explicit_query_gpu
        effective_gpu_name = explicit_gpu_name or GPUType.RTX_5080.name
        allow_expensive_gpu_override = bool(context.get("allow_expensive_gpu_override"))
        if effective_gpu_name == GPUType.RTX_5090.name and not allow_expensive_gpu_override:
            raise ValueError(
                "Remote Vast compatibility admission rejects RTX_5090 by default; "
                "the governed Vast route is RTX_5080. "
                "Set allow_expensive_gpu_override=True only on operator-owned bounded probe surfaces."
            )
        return getattr(GPUType, effective_gpu_name, GPUType.RTX_5080), explicit_gpu_name, allow_expensive_gpu_override

    async def _execute_remote_recovery_command(
        self,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        command = str(context.get("remote_command", "")).strip()
        ssh_host = str(context.get("ssh_host", "")).strip()
        ssh_key = str(context.get("ssh_key_path") or os.path.expanduser("~/.ssh/vast_key")).strip()
        if not command or not ssh_host:
            return {
                "status": "error",
                "workflow": "remote_recovery_command",
                "error": "remote_command and ssh_host are required",
            }

        try:
            ssh_port = int(context.get("ssh_port", 22))
        except Exception:
            ssh_port = 22

        from mica.infrastructure.ssh_resilience import CommandProtocol, ResilientSSHExecutor

        executor = ResilientSSHExecutor()
        result = await executor.execute_with_protocol(
            host=ssh_host,
            port=ssh_port,
            command=command,
            protocol=CommandProtocol.RETRY_3X,
            timeout=int(context.get("remote_command_timeout", 300)),
            key_path=ssh_key,
        )
        return {
            "workflow": "remote_recovery_command",
            "status": "completed" if result.success else "failed",
            "success": bool(result.success),
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    async def _execute_protein_ligand_md(
        self,
        context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Execute protein–ligand complex MD stability simulation.

        Bridges the gap between Alchemist drug-discovery hits and real
        OpenMM execution.  Two execution backends:

        1. **Remote (Vast.ai)** — when ``use_remote_vast=True`` (default if
           env ``VAST_API_KEY`` is set).  Provisions a GPU pod, installs
           OpenMM, stages force-fields, uploads files, launches and monitors
           the simulation, then downloads results and destroys the instance.

        2. **Local** — engine-first dispatch via ``MDEngine.run(mode=\"complex_stability\")``.

        Expected context keys (from Alchemist handoff):
            - ligand_smiles     : str   — SMILES of the hit compound
            - protein_pdb       : str   — Path to protein PDB
            - docked_ligand_pdb : str   — Docked pose PDB from Vina / DiffDock
            - production_ns     : float — Duration (default 50 ns for validation)
            - forcefield        : str   — e.g. "amber14sb"
            - ligand_ff         : str   — e.g. "gaff-2.11"
            - use_remote_vast   : bool  — Force remote execution (auto-detected)
            - simulation_mode   : str   — "binding" (default) or "complex"
            - production_ns     : float — Production time in ns (complex mode, default 100)
        """
        context = context or {}
        compiled_biostate_plan = context.get("compiled_biostate_plan")
        if isinstance(compiled_biostate_plan, dict):
            from .biodynamo_biostate_bridge import build_context_from_compiled_biostate

            context = build_context_from_compiled_biostate(
                compiled_plan=compiled_biostate_plan,
                raw_manifest=context.get("biostate_manifest"),
                compatibility_context=context,
            )
        smiles = context.get("ligand_smiles", "")
        protein = context.get("protein_pdb", "")
        docked = context.get("docked_ligand_pdb", "")

        if not smiles or not protein:
            return {
                "workflow": "protein_ligand_md",
                "status": "error",
                "error": (
                    "Missing required context: ligand_smiles and protein_pdb "
                    "must be provided for protein-ligand MD"
                ),
            }

        # ── Resolve PDB ID → local file if needed ────────────────────
        protein = self._resolve_protein_pdb(protein)

        logger.info(
            f"🧬 Protein–Ligand MD: SMILES={smiles[:40]}, "
            f"protein={protein}, docked={docked}"
        )

        raw_mode = context.get("simulation_mode", None)
        if raw_mode is None:
            raw_mode = self._detect_simulation_mode_from_query(
                str(context.get("_driver_query", ""))
            ) or "binding"
        simulation_mode = str(raw_mode)

        return await self._get_dynamo_execution_facade().execute_protein_ligand_md(
            context=context,
            protein=protein,
            smiles=smiles,
            docked=docked,
            simulation_mode=simulation_mode,
        )

    # ------------------------------------------------------------------
    # Remote (Vast.ai) MD execution
    # ------------------------------------------------------------------

    async def _maybe_reconcile_remote_md_terminal_teardown(
        self,
        context: Dict[str, Any],
        md_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if str(md_result.get("execution_mode") or "").strip().lower() != "remote_vast":
            return None
        if str(md_result.get("status") or "").strip().lower() != "failed_recoverable":
            return None
        state_json = md_result.get("results_json") or {}
        if not bool(state_json.get("teardown_unconfirmed")):
            return None

        session_id = str(
            context.get("md_session_id")
            or context.get("session_id")
            or ""
        ).strip()
        if not session_id:
            return None

        try:
            from mica.infrastructure.persistence.remote_md_session_reconciler import (
                create_default_remote_md_reconciler,
            )

            reconciler = await create_default_remote_md_reconciler(
                str(context.get("md_session_registry_path") or "")
            )
            outcome = await reconciler.reconcile_session(session_id, force=True)
            outcome_payload = outcome.to_dict()
            md_result["teardown_reconcile_outcome"] = outcome_payload
            execution_result = md_result.get("execution_result_v1")
            if isinstance(execution_result, dict):
                backend_native = dict(execution_result.get("backend_native") or {})
                backend_native["teardown_reconcile_outcome"] = outcome_payload
                execution_result["backend_native"] = backend_native
            return outcome_payload
        except Exception as exc:
            logger.warning("Remote MD teardown reconcile failed for %s: %s", session_id, exc)
            return {
                "session_id": session_id,
                "status": "failed_recoverable",
                "action": "teardown_reconcile_failed",
                "reason": str(exc),
            }

    async def _execute_protein_ligand_md_remote(
        self,
        context: Dict[str, Any],
        protein: str,
        smiles: str,
        docked: str,
        execution_request: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Run protein–ligand MD on a Vast.ai GPU pod via VastMDOrchestrator.

        Follows the 9-phase protocol (openmm_remote_md_orchestrator.md):
        PROVISION → PROBE → INSTALL → STAGE_FF → VERIFY_FF → UPLOAD →
        LAUNCH → MONITOR → DOWNLOAD → DESTROY.
        """
        logger.info("🚀 Remote Vast.ai MD — provisioning GPU pod …")
        registry_sink = context.get("_remote_md_registry_event_sink")

        def _emit_registry_patch(patch: Dict[str, Any]) -> None:
            if callable(registry_sink):
                try:
                    registry_sink(dict(patch))
                except Exception:
                    logger.debug("Remote MD registry sink failed", exc_info=True)

        def _on_orchestrator_event(
            phase: str,
            msg: str,
            state_snapshot: Optional[Dict[str, Any]] = None,
        ) -> None:
            snapshot = dict(state_snapshot or {})
            patch: Dict[str, Any] = {
                "status": "running",
                "vast_phase": phase,
                "last_event_message": msg,
                "last_orchestrator_state": snapshot,
            }
            if snapshot.get("job_id"):
                patch["job_id"] = snapshot.get("job_id")
            if snapshot.get("instance_id"):
                patch["instance_id"] = snapshot.get("instance_id")
            if snapshot.get("ssh_host"):
                patch["ssh_host"] = snapshot.get("ssh_host")
            if snapshot.get("ssh_port"):
                patch["ssh_port"] = snapshot.get("ssh_port")
            if snapshot.get("latest_resume_spec_path"):
                patch["resume_spec_path"] = snapshot.get("latest_resume_spec_path")
            if snapshot.get("latest_job_manifest_path"):
                patch["artifact_manifest_path"] = snapshot.get("latest_job_manifest_path")
            if snapshot.get("local_output_dir"):
                patch["output_dir"] = snapshot.get("local_output_dir")
            _emit_registry_patch(patch)

            ws_job_id = snapshot.get("job_id")
            if ws_job_id:
                try:
                    from mica.ws_md import publish_md_event
                    publish_md_event(ws_job_id, phase, msg, snapshot)
                except Exception:
                    pass

        unified = self._get_unified_client()
        unified_provider_names = []
        if unified is not None and hasattr(unified, "providers"):
            try:
                unified_provider_names = list(unified.providers())
            except Exception:
                unified_provider_names = []
        if unified is not None and unified_provider_names and hasattr(unified, "run_biostate_engine_job"):
            from mica.infrastructure.orchestration.biostate_engine_job import BioStateEngineJob

            remote_job = BioStateEngineJob.from_execution_context(
                context=context,
                execution_request=execution_request,
                protein_pdb=protein,
                ligand_smiles=smiles,
                docked_ligand_pdb=docked,
                simulation_mode=str(
                    context.get("simulation_mode")
                    or execution_request.get("scientific", {}).get("simulation_mode")
                    or "binding"
                ),
            )
            md_result = await unified.run_biostate_engine_job(
                remote_job,
                on_event=_on_orchestrator_event,
            )
            execution_result_v1 = md_result.get("execution_result_v1") or {}
            backend_native = dict(execution_result_v1.get("backend_native") or {})
            results_json = dict(md_result.get("results_json") or backend_native.get("results_json") or {})
            _emit_registry_patch(
                {
                    "job_id": md_result.get("job_id", ""),
                    "instance_id": md_result.get("provider_job_id", ""),
                    "output_dir": md_result.get("output_dir", ""),
                    "status": md_result.get("status", "unknown"),
                    "result": md_result,
                    "vast_phase": execution_result_v1.get("status", {}).get("phase", "unknown"),
                    "results_json": results_json,
                    "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            )
            output_dir = md_result.get("output_dir") or "vast_md_output"
            await self._persist_md_output(
                context.get("user_id", "anonymous"),
                output_dir,
                md_result,
            )
            return md_result

        try:
            from mica.infrastructure.orchestration.vast_md_orchestrator import (
                GPUType,
                SimulationMode,
                VastMDOrchestrator,
                resume_autonomous_md,
                run_autonomous_md,
            )

            gpu_type, explicit_gpu_name, allow_expensive_gpu_override = self._resolve_remote_vast_gpu_policy(
                context
            )

            preserve_instance_on_failure = bool(context.get("preserve_instance_on_failure", True))

            # ── Resolve simulation mode ─────────────────────────────
            raw_mode = context.get("simulation_mode", None)
            if raw_mode is None:
                # Auto-detect from the user's query text
                detected = self._detect_simulation_mode_from_query(
                    str(context.get("_driver_query", ""))
                )
                raw_mode = detected or "binding"  # default

            ligand_smiles = str(context.get("ligand_smiles") or smiles or "")
            docked_ligand_pdb = str(context.get("docked_ligand_pdb") or docked or "")
            ligand_aware_complex = (
                str(raw_mode).strip().lower() == "complex_stability"
                or bool(ligand_smiles and docked_ligand_pdb)
            )
            if ligand_aware_complex:
                raw_mode = "complex"

            try:
                sim_mode = SimulationMode(raw_mode)
            except ValueError:
                logger.warning(f"Unknown simulation_mode '{raw_mode}', defaulting to BINDING")
                sim_mode = SimulationMode.BINDING

            production_ns = float(context.get("production_ns", 100.0))
            storage_backend = str(context.get("storage_backend", "none") or "none").lower()
            storage_remote = str(context.get("storage_remote", "") or "")
            storage_remote_prefix = str(context.get("storage_remote_prefix", "md-jobs") or "md-jobs")
            storage_env = dict(context.get("storage_env", {}) or {})

            if storage_backend == "rclone" and context.get("user_id") and (not storage_remote or not storage_env):
                from mica.infrastructure.storage.rclone_gcs_backend import build_orchestrator_storage_options

                auto_storage = build_orchestrator_storage_options(
                    user_id=str(context["user_id"]),
                    object_prefix=storage_remote_prefix,
                )
                storage_remote = storage_remote or auto_storage["storage_remote"]
                storage_env = storage_env or auto_storage["storage_env"]
                storage_remote_prefix = auto_storage["storage_remote_prefix"]

            logger.info(
                f"📋 Simulation mode: {sim_mode.value} | "
                f"production_ns={production_ns} | GPU={gpu_type.name}"
            )

            resume_spec_path = str(context.get("resume_spec_path", "") or "")
            if resume_spec_path:
                state = await resume_autonomous_md(
                    resume_spec_path=resume_spec_path,
                    ssh_key=context.get("ssh_key_path", ""),
                    on_event=_on_orchestrator_event,
                    max_price_per_hour=float(context.get("max_price_per_hour", 0.50)),
                    max_total_cost_usd=float(context.get("max_total_cost_usd", 10.0)),
                    max_runtime_hours=float(context.get("max_runtime_hours", 48.0)),
                    monitor_interval_sec=int(context.get("monitor_interval_sec", 300)),
                    min_reliability=float(context.get("min_reliability", 0.97)),
                    required_disk_gb=float(context.get("required_disk_gb", 0.0)),
                    provision_timeout_sec=int(context.get("provision_timeout_sec", 900)),
                    ssh_probe_attempts=int(context.get("ssh_probe_attempts", 24)),
                    ssh_probe_sleep_sec=int(context.get("ssh_probe_sleep_sec", 10)),
                )
            else:
                # Use the convenience launcher which builds config internally
                state = await run_autonomous_md(
                    pdb_path=protein,
                    steps=int(context.get("steps", 75_000_000)),
                    n_replicas=int(context.get("n_replicas", 1)),
                    max_price=float(context.get("max_price_per_hour", 0.50)),
                    max_cost=float(context.get("max_total_cost_usd", 10.0)),
                    gpu_type=gpu_type,
                    ssh_key=context.get("ssh_key_path", ""),
                    preserve_instance_on_failure=preserve_instance_on_failure,
                    simulation_mode=sim_mode,
                    ligand_smiles=ligand_smiles,
                    docked_ligand_pdb=docked_ligand_pdb,
                    production_ns=production_ns,
                    max_runtime_hours=float(context.get("max_runtime_hours", 48.0)),
                    monitor_interval_sec=int(context.get("monitor_interval_sec", 300)),
                    storage_backend=storage_backend,
                    storage_remote=storage_remote,
                    storage_remote_prefix=storage_remote_prefix,
                    storage_sync_interval_sec=int(context.get("storage_sync_interval_sec", 900)),
                    storage_env=storage_env,
                    on_event=_on_orchestrator_event,
                    min_reliability=float(context.get("min_reliability", 0.97)),
                    required_disk_gb=float(context.get("required_disk_gb", 0.0)),
                    provision_timeout_sec=int(context.get("provision_timeout_sec", 900)),
                    ssh_probe_attempts=int(context.get("ssh_probe_attempts", 24)),
                    ssh_probe_sleep_sec=int(context.get("ssh_probe_sleep_sec", 10)),
                )

            # Convert OrchestratorState to result dict
            state_dict = state.to_dict() if hasattr(state, "to_dict") else {"phase": str(getattr(state, "phase", "unknown"))}
            raw_phase = getattr(state, "phase", state_dict.get("phase", "unknown"))
            phase_value = getattr(raw_phase, "value", raw_phase)
            phase = str(phase_value or state_dict.get("phase", "unknown"))
            success = phase.lower() in ("complete", "download")
            status = "completed" if success else ("failed_recoverable" if phase.lower() == "failed_recoverable" else "failed")

            md_result = {
                "workflow": "protein_ligand_md",
                "execution_mode": "remote_vast",
                "adapter_id": "vast_remote_adapter",
                "simulation_mode": "complex_stability" if ligand_aware_complex else sim_mode.value,
                "status": status,
                "success": success,
                "vast_phase_final": phase,
                "instance_id": getattr(state, "instance_id", None),
                "ssh_host": getattr(state, "ssh_host", None),
                "ssh_port": getattr(state, "ssh_port", None),
                "total_cost_usd": getattr(state, "total_cost_usd", state_dict.get("total_cost_usd")),
                "output_dir": getattr(state, "local_output_dir", state_dict.get("local_output_dir")),
                "results_json": state_dict,
                "error": str(getattr(state, "error", "") or state_dict.get("error", "") or ""),
                "gpu_policy": {
                    "default": "RTX_5080",
                    "explicit_gpu_request": explicit_gpu_name,
                    "allow_expensive_gpu_override": allow_expensive_gpu_override,
                    "effective_gpu": gpu_type.name,
                },
            }

            execution_result_v1 = normalize_remote_execution_result(
                md_result,
                execution_request,
            )
            execution_result_v1 = _project_biostate_seed_metadata(
                execution_result_v1,
                context=context,
            )
            execution_result_v1 = enforce_no_silent_success(execution_result_v1)
            md_result["execution_result_v1"] = execution_result_v1

            _emit_registry_patch(
                {
                    "job_id": state_dict.get("job_id") or getattr(state, "job_id", ""),
                    "instance_id": getattr(state, "instance_id", None),
                    "ssh_host": getattr(state, "ssh_host", None),
                    "ssh_port": getattr(state, "ssh_port", None),
                    "resume_spec_path": state_dict.get("latest_resume_spec_path"),
                    "artifact_manifest_path": state_dict.get("latest_job_manifest_path"),
                    "output_dir": md_result.get("output_dir"),
                    "status": md_result["status"],
                    "result": md_result,
                    "teardown_proof": dict(execution_result_v1.get("teardown_proof") or {}),
                    "terminal_autopsy": dict(execution_result_v1.get("terminal_autopsy") or {}),
                    "vast_phase": phase,
                    "results_json": state_dict,
                    "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            )
            await self._maybe_reconcile_remote_md_terminal_teardown(context, md_result)

            # --- GCS auto-persist ---
            output_dir = md_result.get("output_dir") or "vast_md_output"
            await self._persist_md_output(
                context.get("user_id", "anonymous"),
                output_dir,
                md_result,
            )

            return md_result

        except ImportError as e:
            logger.warning(f"VastMDOrchestrator not available: {e} — falling back to local")
            local_request = dict(execution_request)
            local_request["job"] = dict(execution_request.get("job", {}))
            local_request["job"]["execution_target"] = "local"
            return await self._execute_protein_ligand_md_local(
                context,
                protein,
                smiles,
                docked,
                local_request,
            )
        except Exception as e:
            logger.error(f"Remote Vast.ai MD failed: {e}", exc_info=True)
            registry_sink = context.get("_remote_md_registry_event_sink")
            if callable(registry_sink):
                try:
                    registry_sink(
                        {
                            "status": "error",
                            "error": str(e),
                            "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        }
                    )
                except Exception:
                    logger.debug("Remote MD registry sink failed on error", exc_info=True)
            return {
                "workflow": "protein_ligand_md",
                "execution_mode": "remote_vast",
                "status": "error",
                "error": str(e),
                "execution_result_v1": enforce_no_silent_success(
                    normalize_remote_execution_result(
                        {
                            "workflow": "protein_ligand_md",
                            "execution_mode": "remote_vast",
                            "status": "error",
                            "success": False,
                            "results_json": {},
                            "vast_phase_final": "failed",
                            "output_dir": "",
                        },
                        execution_request,
                    )
                ),
            }

    # ------------------------------------------------------------------
    # Unified compute client (lazy init from env)
    # ------------------------------------------------------------------

    def _get_unified_client(self) -> Any:
        """Return a UnifiedComputeClient initialised from env, or None if unavailable."""
        if not hasattr(self, "_unified_client_cache"):
            try:
                from mica.unified_compute_client import UnifiedComputeClient
                self._unified_client_cache = UnifiedComputeClient.from_env()
            except Exception as exc:
                logger.debug("UnifiedComputeClient unavailable: %s", exc)
                self._unified_client_cache = None
        return self._unified_client_cache

    def _get_dynamo_execution_facade(self) -> Any:
        facade = getattr(self, "_dynamo_execution_facade", None)
        if facade is None:
            from .dynamo_execution_facade import DynamoExecutionFacade

            facade = DynamoExecutionFacade(self)
            self._dynamo_execution_facade = facade
        return facade

    # ------------------------------------------------------------------
    # Local MD execution
    # ------------------------------------------------------------------

    async def _execute_protein_ligand_md_local(
        self,
        context: Dict[str, Any],
        protein: str,
        smiles: str,
        docked: str,
        execution_request: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Run protein–ligand MD locally via engine-first dispatch.
        
        This is the engine-owned authority path (P1 implementation).
        The local protein-ligand lane now crosses an engine boundary 
        before any processor execution.
        """
        logger.info("🖥️  Local MD execution via engine-first dispatch …")
        try:
            from workers.dynamo.biodynamo.core.md_engine import (
                MDEngine,
                MDJobConfig,
            )

            template_ref = execution_request.get("job", {}).get("template_ref", {})
            template_id = str(template_ref.get("template_id", "") or "")
            template_version = str(template_ref.get("template_version", "") or "")
            template_binding = resolve_local_template_binding(
                template_id=template_id,
                template_version=template_version,
            )

            if template_binding.adapter_id != "complex_stability_adapter":
                raise ValueError(
                    f"Unsupported local adapter_id={template_binding.adapter_id!r} "
                    f"for template {template_binding.template_id}:{template_binding.template_version}"
                )

            graph_payload = dict(context.get("scientific_task_graph") or {})

            # Build MDJobConfig for engine. When a scientific task graph is present,
            # the engine-owned graph path must stay authoritative over legacy mode routing.
            cfg_kwargs = {
                "simulation_mode": "publication_md" if graph_payload else "complex_stability",
                "pdb_path": protein,
                "ligand_smiles": smiles,
                "docked_ligand_pdb": docked,
                "docked_ligand_sdf": context.get("docked_ligand_sdf", ""),
                "production_ns": context.get("production_ns", 50.0),
                "protein_ff": _FF_CLI.get(
                    context.get("forcefield", "amber14sb"),
                    "amber/protein.ff14SB.xml",
                ),
                "ligand_ff": context.get("ligand_ff", "gaff-2.11"),
                "ligand_charge_method": context.get("ligand_charge_method", ""),
                "output_dir": _default_local_md_output_dir(context),
                "job_name": context.get("job_name", "alchemist_hit_validation"),
            }
            if graph_payload:
                cfg_kwargs["scientific_task_graph"] = graph_payload
            for field_name in (
                "padding_nm",
                "box_shape",
                "ionic_strength_M",
                "ph",
                "remove_heterogens",
                "nonbonded_cutoff_nm",
                "ewald_error_tolerance",
                "hmr_mass_amu",
                "constraint_tolerance",
                "cm_remover_freq",
                "temperature_K",
                "friction_ps",
                "min_coarse_tol",
                "min_fine_tol",
                "min_max_iter",
                "nvt_ps",
                "nvt_steps",
                "nvt_start_K",
                "npt_duration_ps",
                "npt_pressure_bar",
                "npt_barostat_interval",
                "protein_restraint_k",
                "ligand_restraint_k",
                "npt_restraint_stages",
                "timestep_eq_fs",
                "timestep_prod_fs",
                "dcd_freq_steps",
                "energy_freq_steps",
                "checkpoint_ns",
                "allow_undefined_stereo",
                "platform",
                "precision",
                "gpu_id",
            ):
                if field_name in context:
                    cfg_kwargs[field_name] = context[field_name]

            cfg = MDJobConfig(**cfg_kwargs)

            # Call engine (P1: engine-first authority boundary)
            engine = MDEngine()
            result = engine.run(cfg)

            # Engine result already has execution_result_v1
            # Use it directly as source of truth
            md_result = {
                "workflow": "protein_ligand_md",
                "execution_mode": "local",
                "status": "completed",
                "success": result.get("success", False),
                "template_id": template_binding.template_id,
                "template_version": template_binding.template_version,
                "template_family": template_binding.family,
                "adapter_id": template_binding.adapter_id,
                "total_atoms": result.get("total_atoms"),
                "ns_per_day": result.get("steps", {}).get("complex_stability", {}).get("ns_per_day"),
                "output_dir": cfg.output_dir,
                "results_json": result,
                # Engine provides execution_result_v1 directly
                "execution_result_v1": result.get("execution_result_v1"),
            }

            # Ensure execution_result_v1 passes no-silent-success check
            execution_result_v1 = md_result.get("execution_result_v1", {})
            if not execution_result_v1:
                # Fallback: compute from md_result if engine didn't provide
                execution_result_v1 = normalize_local_execution_result(
                    md_result,
                    execution_request,
                )
            execution_result_v1 = _project_biostate_seed_metadata(
                execution_result_v1,
                context=context,
            )
            execution_result_v1 = enforce_no_silent_success(execution_result_v1)
            md_result["execution_result_v1"] = execution_result_v1

            # --- GCS auto-persist ---
            await self._persist_md_output(
                context.get("user_id", "anonymous"),
                cfg.output_dir,
                md_result,
            )

            return md_result
        except ImportError as e:
            logger.warning(f"MDEngine not available: {e}")
            return {
                "workflow": "protein_ligand_md",
                "execution_mode": "local",
                "status": "error",
                "error": f"Engine dependency unavailable: {e}",
                "execution_result_v1": enforce_no_silent_success(
                    normalize_local_execution_result(
                        {
                            "workflow": "protein_ligand_md",
                            "execution_mode": "local",
                            "status": "error",
                            "success": False,
                            "results_json": {},
                            "output_dir": "",
                        },
                        execution_request,
                    )
                ),
            }
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            logger.error(f"Protein-ligand MD failed: {e}", exc_info=True)
            return {
                "workflow": "protein_ligand_md",
                "execution_mode": "local",
                "status": "error",
                "error": str(e),
                "execution_result_v1": enforce_no_silent_success(
                    normalize_local_execution_result(
                        {
                            "workflow": "protein_ligand_md",
                            "execution_mode": "local",
                            "status": "error",
                            "success": False,
                            "results_json": {},
                            "output_dir": "",
                        },
                        execution_request,
                    )
                ),
            }


    # ------------------------------------------------------------------
    # GCS persistence helper
    # ------------------------------------------------------------------
    async def _persist_md_output(
        self,
        user_id: str,
        output_dir: str,
        result: Dict[str, Any],
    ) -> None:
        """Best-effort upload of MD results + output directory to GCS."""
        output_saver = getattr(self, "output_saver", None)
        if not output_saver:
            return
        try:
            from mica.infrastructure.storage.output_saver import OutputSaver
            run_id = OutputSaver.make_run_id("md")
            # Save the JSON summary
            await output_saver.save_result(user_id, run_id, "md_result.json", result)
            # Upload entire output directory if it exists
            output_path = Path(output_dir)
            if output_path.is_dir():
                await output_saver.save_directory(
                    user_id, run_id, str(output_path),
                    extensions=(
                        ".pdb",
                        ".xtc",
                        ".dcd",
                        ".json",
                        ".csv",
                        ".log",
                        ".chk",
                        ".xml",
                        ".sdf",
                        ".pqr",
                    ),
                )
            logger.info(f"☁️  MD outputs persisted → GCS run={run_id}")
        except Exception as exc:
            logger.warning(f"⚠️ GCS persist failed (non-fatal): {exc}")


# Helper map: CLI FF name → OpenMM XML path
_FF_CLI = {
    "amber14sb": "amber/protein.ff14SB.xml",
    "amber19sb": "amber/protein.ff19SB.xml",
    "charmm36": "charmm36.xml",
}

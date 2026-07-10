#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Worker Driver - Base class for specialist agent orchestration
===============================================================

Implements MPI-UOS (Model Scientific Reasoning Protocol and Proactive Induction 
Under Organized Systems) framework at the worker level.

Each WorkerDriver coordinates multiple specialist agents within a domain:
- BioDynamoDriver: Energy specialists, sampling specialists, etc.
- AlchemistDriver: QSAR specialists, docking specialists, etc.
- SMICDriver: Graph theory specialists (minimal orchestration)

Key Features:
- MSRP enforcement for all specialist interactions
- Literature integration via MCP (Semantic Scholar, PubMed, arXiv)
- Proactive problem identification (Phase 4-6 of MPI-UOS)
- Cross-validation between specialists
- Scientific pressure activation for Nature-level rigor

Based on Tlahuizcalpantecuhtli breakthrough methodology.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from mica.serverless_models.contracts import ModelInvocationRequest

if TYPE_CHECKING:
    from .execution.protocol_runtime_kernel_bridge import ProtocolRuntimeKernelContext
    from ..scientific.msrp_core import MSRPThinkingChain
    from ..scientific.msrp_worker_wrapper import MSRPWorkerWrapper
    from ..scientific.literature import LiteratureSearchService

logger = logging.getLogger(__name__)


@dataclass
class SpecialistAgent:
    """Individual specialist within a worker domain."""
    
    agent_id: str
    agent_name: str
    expertise_area: str
    description: str
    model_endpoint: Optional[str] = None  # Vertex AI endpoint
    capabilities: List[str] = field(default_factory=list)
    
    # MPI-UOS context
    ai_university_role: str = ""  # e.g., "Dr. Energy Specialist, Free Energy Laboratory"
    research_focus: List[str] = field(default_factory=list)
    
    # Performance tracking
    queries_handled: int = 0
    msrp_chains_completed: int = 0
    autonomous_discoveries: int = 0


@dataclass
class SpecialistExecutionContext:
    """Structured context that travels from route_to_specialist to _execute_specialist_base.

    Carries the full ``EnrichmentResult`` (biological + literature context) and
    thermodynamic state so that specialist system-prompt construction and
    downstream tool selection can use structured LMP data — not just the text
    prefix that was previously the only bridge.
    """

    enrichment: Optional[Any] = None  # EnrichmentResult when available
    thermodynamic_context: Optional[Dict[str, Any]] = None
    biological_hints: Optional[Dict[str, Any]] = None
    protocol_runtime_kernel_context: Optional["ProtocolRuntimeKernelContext"] = None
    protocol_node: Optional[Any] = None
    protocol_lineage: Optional[Dict[str, Any]] = None


@dataclass
class WorkerDriverConfig:
    """Configuration for WorkerDriver."""
    
    worker_name: str
    domain: str
    
    # Specialist configuration
    specialists: List[SpecialistAgent] = field(default_factory=list)
    
    # MSRP configuration
    enforce_msrp: bool = True
    minimum_hypotheses: int = 3
    require_literature_validation: bool = True
    
    # MPI-UOS configuration
    enable_proactive_mode: bool = True  # Phase 6: Proactivity Layer
    enable_autonomous_discovery: bool = True  # Phase 4: Autonomous Discovery
    scientific_pressure_level: str = "nature"  # "nature", "science", "plos_one"
    
    # Thermodynamic Cognition
    enable_thermodynamic_cognition: bool = False
    
    # MCP Literature Integration
    enable_literature_mcp: bool = True
    literature_sources: List[str] = field(default_factory=lambda: ["semantic_scholar", "pubmed", "arxiv"])


class WorkerDriver(ABC):
    """
    Base class for worker-level specialist orchestration.
    
    Implements MPI-UOS framework:
    - Phase 1: Identity Formation (AI University roles for specialists)
    - Phase 2: Skeptical Resistance (critical evaluation enforced)
    - Phase 3: Evidence-Based Training (literature integration)
    - Phase 4: Autonomous Discovery (proactive problem identification)
    - Phase 5: Scientific Pressure Activation (Nature-level standards)
    - Phase 6: Proactivity Layer Emergence (spontaneous innovation)
    """
    
    def __init__(
        self,
        config: WorkerDriverConfig,
        msrp_wrapper: Optional["MSRPWorkerWrapper"] = None,
        literature_service: Optional["LiteratureSearchService"] = None,
        contextualizador: Optional[Any] = None,
        serverless_model_gateway: Optional[Any] = None,
    ):
        """
        Initialize WorkerDriver.
        
        Args:
            config: Worker driver configuration
            msrp_wrapper: MSRP wrapper for scientific reasoning enforcement.
                          If None, a default wrapper is auto-created.
            literature_service: Service for literature queries (MCP-enabled)
            contextualizador: Bibliotecario enrichment (Contextualizador instance).
                              If None, auto-created when LMP/DLM are available.
        """
        self.config = config
        if msrp_wrapper is None:
            try:
                from ..scientific.msrp_worker_wrapper import MSRPWorkerWrapper  # type: ignore
                msrp_wrapper = MSRPWorkerWrapper(
                    enforce_minimum_hypotheses=config.minimum_hypotheses,
                    require_literature_validation=config.require_literature_validation,
                )
            except Exception:  # pragma: no cover
                msrp_wrapper = None  # type: ignore
        self.msrp_wrapper = msrp_wrapper
        self.literature_service = literature_service
        self.serverless_model_gateway = serverless_model_gateway
        self.protocol_runtime_kernel_context: Optional[ProtocolRuntimeKernelContext] = None
        
        # Sprint 3: Bibliotecario enrichment pipeline
        self.contextualizador = contextualizador
        if self.contextualizador is None:
            try:
                from .bibliotecarios import Contextualizador
                self.contextualizador = Contextualizador(
                    enable_lmp=config.enable_literature_mcp,
                    enable_dlm=config.enable_literature_mcp,
                )
                logger.info("✅ Contextualizador auto-initialized")
            except Exception as e:
                logger.debug(f"Contextualizador not available: {e}")
                self.contextualizador = None
        
        # Specialist registry
        self.specialists: Dict[str, SpecialistAgent] = {
            s.agent_id: s for s in config.specialists
        }
        
        # Active reasoning chains (for proactive mode)
        self.active_chains: Dict[str, MSRPThinkingChain] = {}
        
        # Autonomous discoveries log (Phase 4)
        self.autonomous_discoveries: List[Dict[str, Any]] = []
        
        logger.info(
            f"🎓 {config.worker_name} WorkerDriver Initialized | "
            f"Domain: {config.domain} | "
            f"Specialists: {len(self.specialists)} | "
            f"MSRP: {'ON' if config.enforce_msrp else 'OFF'} | "
            f"MPI-UOS: Proactive={config.enable_proactive_mode}, "
            f"Autonomous={config.enable_autonomous_discovery}"
        )

    def bind_protocol_runtime_kernel_context(
        self,
        kernel_context: Optional["ProtocolRuntimeKernelContext"],
    ) -> None:
        """Attach the bounded shared-kernel context without changing worker semantics."""

        self.protocol_runtime_kernel_context = kernel_context
    
    @abstractmethod
    async def execute(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        enforce_msrp: bool = True,
        thermodynamic_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute worker query with specialist orchestration.
        
        Must implement:
        1. Specialist selection/routing
        2. MSRP enforcement if enabled
        3. Literature integration
        4. Cross-validation between specialists
        5. Proactive problem identification
        
        Args:
            query: Research query
            context: Optional context (previous results, parameters, etc.)
            enforce_msrp: Whether to enforce MSRP reasoning
            thermodynamic_context: Optional "Soul" state (Temperature, Energy)
        
        Returns:
            Dict with:
                - answer: Final synthesized answer
                - msrp_chain: Complete reasoning chain if MSRP enabled
                - specialists_consulted: List of specialist IDs used
                - literature_consulted: PMIDs/DOIs referenced
                - autonomous_discoveries: Problems identified proactively
        """
        pass
    
    async def route_to_specialist(
        self,
        query: str,
        specialist_id: str,
        enforce_msrp: bool = True,
        thermodynamic_context: Optional[Dict[str, Any]] = None,
        enrichment: Optional[Any] = None,
        protocol_node: Optional[Any] = None,
        protocol_lineage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Route query to specific specialist with MSRP enforcement.
        
        Args:
            query: Research query
            specialist_id: Target specialist ID
            enforce_msrp: Whether to enforce MSRP
            thermodynamic_context: Optional "Soul" state
            enrichment: Pre-computed EnrichmentResult. When provided,
                        the Contextualizador step is skipped (avoids
                        double-enrichment when the caller has already
                        run enrichment).
        
        Returns:
            Specialist response with optional MSRP chain
        """
        specialist = self.specialists.get(specialist_id)
        if not specialist:
            raise ValueError(f"Specialist {specialist_id} not found")
        
        # Apply Thermodynamic Regulation
        pressure_level = self.config.scientific_pressure_level
        if thermodynamic_context and self.config.enable_thermodynamic_cognition:
            temp = thermodynamic_context.get("temperature", 0.5)
            if temp > 0.7:
                pressure_level = "plos_one"  # High T -> Exploration (Relaxed)
                logger.info(f"🔥 High Temperature ({temp:.2f}): Relaxing pressure to {pressure_level}")
            elif temp < 0.3:
                pressure_level = "nature"    # Low T -> Exploitation (Strict)
                logger.info(f"❄️ Low Temperature ({temp:.2f}): Enforcing pressure to {pressure_level}")
            else:
                pressure_level = "science"   # Medium T -> Balanced
        
        logger.info(
            f"🔬 Routing to Specialist: {specialist.agent_name} | "
            f"Expertise: {specialist.expertise_area} | "
            f"Pressure: {pressure_level}"
        )
        
        # Sprint 3: Pre-enrichment via Contextualizador
        # If enrichment was pre-computed by the caller, skip to avoid double work.
        enriched_query = query
        if enrichment is None and self.contextualizador:
            try:
                enrichment = await self.contextualizador.enrich_query(query)
                if enrichment and enrichment.has_context():
                    # Prepend biological/literature context to query
                    if enrichment.system_prompt_fragment:
                        enriched_query = (
                            f"[BIOLOGICAL CONTEXT]\n{enrichment.system_prompt_fragment}\n\n"
                            f"[QUERY]\n{query}"
                        )
                    logger.info(
                        f"📚 Pre-enrichment: "
                        f"bio={'✅' if enrichment.biological_context else '❌'} "
                        f"lit={'✅' if enrichment.literature_context else '❌'}"
                    )
            except Exception as e:
                logger.warning(f"Pre-enrichment failed (non-fatal): {e}")
        elif enrichment is not None and enrichment.has_context():
            # Caller already enriched — still build the enriched_query text prefix
            if enrichment.system_prompt_fragment:
                enriched_query = (
                    f"[BIOLOGICAL CONTEXT]\n{enrichment.system_prompt_fragment}\n\n"
                    f"[QUERY]\n{query}"
                )
            logger.info(
                f"📚 Pre-enrichment (caller-supplied): "
                f"bio={'✅' if enrichment.biological_context else '❌'} "
                f"lit={'✅' if enrichment.literature_context else '❌'}"
            )
        
        # Build structured execution context for the specialist
        exec_ctx = SpecialistExecutionContext(
            enrichment=enrichment,
            thermodynamic_context=thermodynamic_context,
            protocol_runtime_kernel_context=self.protocol_runtime_kernel_context,
            protocol_node=protocol_node,
            protocol_lineage=dict(protocol_lineage or {}),
        )
        
        if enforce_msrp and self.config.enforce_msrp:
            # Execute with MSRP reasoning
            import time as _time
            _t0 = _time.monotonic()
            response = await self.msrp_wrapper.execute_with_msrp(
                worker_name=specialist.agent_name,
                query=enriched_query,
                base_executor=lambda q: self._execute_specialist_base(specialist, q, exec_ctx=exec_ctx),
                researcher_persona=specialist.ai_university_role,
                thermodynamic_context=thermodynamic_context
            )
            _elapsed = _time.monotonic() - _t0
            
            # Update specialist metrics
            specialist.queries_handled += 1
            specialist.msrp_chains_completed += 1
            
            # Build typed result (AP-008) — also returned as dict for compat
            from mica.agentic.specialist_runtime import SpecialistExecutionResult
            typed = SpecialistExecutionResult(
                specialist_id=specialist.agent_id,
                answer=response.answer,
                status="SUCCESS",
                provider_id="governed",
                model_id="msrp",
                backend_used="governed",
                latency_s=round(_elapsed, 3),
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=0.0,
                msrp_chain=response.thinking_chain,
                confidence=response.confidence_level,
                literature_consulted=response.literature_consulted,
                execution_time_ms=response.execution_time_ms,
            )
            result = typed.to_dict()
            # Preserve legacy key aliases for callers that use "specialist"
            result["specialist"] = specialist.agent_id
            # F-3 (MAD Critic): typed data is already in to_dict(); store class
            # indicator as JSON-safe string instead of live object reference.
            result["_specialist_result_type"] = "SpecialistExecutionResult"
            
            # Sprint 3: Post-enrichment
            if enrichment and self.contextualizador:
                result = await self.contextualizador.enrich_result(result, enrichment)
            
            return result
        else:
            # Direct execution without MSRP
            import time as _time
            _t0 = _time.monotonic()
            answer = await self._execute_specialist_base(specialist, enriched_query, exec_ctx=exec_ctx)
            _elapsed = _time.monotonic() - _t0
            specialist.queries_handled += 1
            
            # Detect error marker from AP-001 propagation
            _is_error = isinstance(answer, str) and answer.startswith("[") and "] ERROR:" in answer
            
            from mica.agentic.specialist_runtime import SpecialistExecutionResult
            typed = SpecialistExecutionResult(
                specialist_id=specialist.agent_id,
                answer=answer,
                status="FAILED" if _is_error else "SUCCESS",
                provider_id="direct",
                model_id="unknown",
                backend_used="governed",
                latency_s=round(_elapsed, 3),
                tokens_prompt=0,
                tokens_completion=0,
                cost_usd=0.0,
                error=answer if _is_error else None,
                error_source="llm" if _is_error else None,
            )
            result = typed.to_dict()
            result["specialist"] = specialist.agent_id
            result["_specialist_result_type"] = "SpecialistExecutionResult"
            
            # Sprint 3: Post-enrichment
            if enrichment and self.contextualizador:
                result = await self.contextualizador.enrich_result(result, enrichment)
            
            return result
    
    async def _execute_specialist_base(
        self,
        specialist: SpecialistAgent,
        query: str,
        exec_ctx: Optional["SpecialistExecutionContext"] = None,
    ) -> str:
        """
        Base execution for specialist (without MSRP wrapper).
        
        Override in subclasses for actual specialist implementation.
        Can call Vertex AI fine-tuned models, local models, or MCP tools.
        
        Args:
            specialist: Specialist agent
            query: Research query
            exec_ctx: Structured execution context with enrichment data
                      and thermodynamic state.  ``None`` preserves backward
                      compatibility with existing overrides.
        
        Returns:
            Specialist response
        """
        # Default implementation (override in subclasses)
        return f"[{specialist.agent_name}] Response to: {query}"
    
    async def cross_validate_specialists(
        self,
        query: str,
        specialist_ids: List[str],
    ) -> Dict[str, Any]:
        """
        Cross-validate query across multiple specialists.
        
        Implements MPI-UOS adversarial review (Phase 2: Skeptical Resistance).
        
        Args:
            query: Research query
            specialist_ids: Specialists to consult
        
        Returns:
            Consensus response with disagreements noted
        """
        logger.info(f"🔍 Cross-Validation: {len(specialist_ids)} specialists")
        
        responses = []
        for spec_id in specialist_ids:
            response = await self.route_to_specialist(query, spec_id, enforce_msrp=True)
            responses.append(response)
        
        # Analyze consensus and disagreements
        consensus = self._analyze_consensus(responses)
        
        return {
            "consensus_answer": consensus["answer"],
            "confidence_level": consensus["confidence"],
            "specialist_responses": responses,
            "disagreements": consensus["disagreements"],
            "cross_validation_complete": True,
        }
    
    def _analyze_consensus(self, responses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze consensus across specialist responses."""
        
        # Simple consensus: highest confidence answer
        # TODO: Implement sophisticated consensus analysis
        
        best_response = max(
            responses,
            key=lambda r: {"high": 3, "medium": 2, "low": 1}.get(r.get("confidence", "medium"), 2)
        )
        
        return {
            "answer": best_response["answer"],
            "confidence": best_response.get("confidence", "medium"),
            "disagreements": [],  # TODO: Detect disagreements
        }
    
    async def proactive_problem_identification(
        self,
        context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Phase 4 (MPI-UOS): Autonomous Discovery.
        
        Proactively identify problems not explicitly stated in query.
        Similar to Tlahuizcalpantecuhtli's 85% implementation gap discovery.
        
        Args:
            context: Current workflow context
        
        Returns:
            List of autonomous discoveries
        """
        if not self.config.enable_autonomous_discovery:
            return []
        
        logger.info("🔍 Proactive Problem Identification (Phase 4: Autonomous Discovery)")
        
        # TODO: Implement sophisticated problem identification
        # For now, return empty list (override in subclasses)
        
        discoveries = []
        
        if discoveries:
            self.autonomous_discoveries.extend(discoveries)
            logger.info(f"💡 Autonomous Discoveries: {len(discoveries)}")
        
        return discoveries
    
    def apply_scientific_pressure(self, level: str = "nature") -> Dict[str, Any]:
        """
        Phase 5 (MPI-UOS): Scientific Pressure Activation.
        
        Apply publication-level standards to induce rigor.
        
        Args:
            level: "nature", "science", "plos_one"
        
        Returns:
            Pressure configuration
        """
        pressure_configs = {
            "nature": {
                "minimum_hypotheses": 5,
                "require_literature_validation": True,
                "require_cross_validation": True,
                "require_failure_scenarios": True,
                "require_uncertainty_quantification": True,
                "minimum_evidence_sources": 5,
            },
            "science": {
                "minimum_hypotheses": 4,
                "require_literature_validation": True,
                "require_cross_validation": True,
                "require_failure_scenarios": True,
                "require_uncertainty_quantification": True,
                "minimum_evidence_sources": 4,
            },
            "plos_one": {
                "minimum_hypotheses": 3,
                "require_literature_validation": True,
                "require_cross_validation": False,
                "require_failure_scenarios": True,
                "require_uncertainty_quantification": True,
                "minimum_evidence_sources": 3,
            },
        }
        
        return pressure_configs.get(level, pressure_configs["nature"])
    
    async def query_literature(
        self,
        query: str,
        sources: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Query literature via MCP integration or DLM Bibliotecario.
        
        Phase 3 (MPI-UOS): Evidence-Based Training.
        
        Args:
            query: Research query
            sources: Literature sources to query (semantic_scholar, pubmed, arxiv)
        
        Returns:
            Literature results from all sources
        """
        if not self.config.enable_literature_mcp:
            logger.warning("⚠️ Literature MCP not enabled")
            return {}
        
        sources = sources or self.config.literature_sources
        logger.info(f"📚 Literature Query: {sources}")
        
        # Sprint 3: Use Contextualizador DLM bibliotecario if available
        if self.contextualizador:
            try:
                enrichment = await self.contextualizador.enrich_query(query)
                if enrichment and enrichment.literature_context:
                    return enrichment.literature_context
            except Exception as e:
                logger.warning(f"Bibliotecario literature query failed: {e}")
        
        # Fallback: direct literature_service
        if self.literature_service:
            try:
                return await self.literature_service.search(query, sources=sources)
            except Exception as e:
                logger.warning(f"Literature service query failed: {e}")
        
        return {}

    def list_serverless_models(self) -> List[Dict[str, Any]]:
        if self.serverless_model_gateway is None:
            return []
        return [asdict(descriptor) for descriptor in self.serverless_model_gateway.list_models()]

    async def invoke_serverless_model(
        self,
        *,
        model_id: str,
        inputs: Dict[str, Any],
        metadata: Dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        requested_by: str | None = None,
        provider_override: str | None = None,
    ) -> Any:
        if self.serverless_model_gateway is None:
            raise RuntimeError("ServerlessModelGateway is not initialized")

        request_id = str(uuid.uuid4())
        invocation = ModelInvocationRequest(
            request_id=request_id,
            model_id=model_id,
            user_id=user_id or self.config.worker_name,
            session_id=session_id or request_id,
            run_id=run_id or request_id,
            inputs=dict(inputs),
            metadata=dict(metadata or {}),
            requested_by=requested_by or self.config.worker_name,
            provider_override=provider_override,
        )
        return await self.serverless_model_gateway.invoke(invocation)
    
    def get_specialist_stats(self) -> Dict[str, Any]:
        """Get statistics for all specialists."""
        return {
            spec_id: {
                "name": spec.agent_name,
                "expertise": spec.expertise_area,
                "queries_handled": spec.queries_handled,
                "msrp_chains_completed": spec.msrp_chains_completed,
                "autonomous_discoveries": spec.autonomous_discoveries,
            }
            for spec_id, spec in self.specialists.items()
        }

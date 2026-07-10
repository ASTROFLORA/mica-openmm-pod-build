"""
MICA LangGraph Node Wrapper
Adapts MICA workers to LangGraph node functions while preserving existing architecture.

Key Design:
- REUSES AgenticDriver.execute_worker() (no rewriting workers)
- REUSES TransportLayer (all 7 backends work)
- PRESERVES MUDOEnvelope lineage tracking
- ADDS quality-based conditional routing
"""
import time
from typing import Dict, Any, Optional
from ..orchestration_protocol import WorkerType, WorkerMetadata, MUDOEnvelope
from ..drivers.agentic_driver import AgenticDriver
from .state import MICAState, mudo_from_state


class MICALangGraphNode:
    """
    Wrapper class that adapts MICA workers to LangGraph node functions.
    
    Architecture Philosophy:
    - Thin wrapper around existing AgenticDriver
    - Preserves M-UDO lineage tracking
    - Adds LangGraph-specific state management
    - Enables conditional routing via quality metrics
    
    Example:
        driver = AgenticDriver()
        node_executor = MICALangGraphNode(driver)
        
        # Use in LangGraph workflow
        workflow.add_node("scaffold", lambda s: node_executor.execute_worker_node(
            s, WorkerType.BIODYNAMO, "scaffold", {"template": "alanine_dipeptide"}
        ))
    """
    
    def __init__(self, agentic_driver: AgenticDriver):
        """
        Initialize node wrapper.
        
        Args:
            agentic_driver: Existing AgenticDriver instance with all workers loaded
        """
        self.driver = agentic_driver
        self.transport = agentic_driver.transport
    
    async def execute_worker_node(
        self,
        state: MICAState,
        worker_type: WorkerType,
        node_id: str,
        parameters: Dict[str, Any],
    ) -> MICAState:
        """
        Execute a MICA worker as a LangGraph node.
        
        This is the core adaptation function that:
        1. Extracts MUDOEnvelope from state
        2. Invokes existing worker via AgenticDriver
        3. Updates MUDOEnvelope with results
        4. Extracts quality metrics for routing
        5. Returns updated state
        
        Args:
            state: Current LangGraph state
            worker_type: Which worker to execute (ALCHEMIST, BIODYNAMO, SMIC, etc.)
            node_id: Unique identifier for this node execution
            parameters: Worker-specific configuration
        
        Returns:
            Updated MICAState with worker results incorporated
        """
        start_time = time.time()
        
        # 1. Reconstruct MUDOEnvelope from state
        mudo = mudo_from_state(state)
        session_id = mudo.session_id or state["mudo"].get("session_id", "unknown")
        
        # 2. Build prompt/input for worker (reuse DAGExecutor logic)
        prompt = self._build_worker_prompt(parameters, mudo)
        
        # 3. REUSE existing execute_worker (preserves all 7 backends!)
        try:
            raw_result = await self.driver.execute_worker(
                worker_type.value,
                prompt,
                session_id,
            )
        except Exception as exc:
            # Handle worker failure gracefully
            error_msg = f"{node_id} failed: {str(exc)}"
            state["errors"].append(error_msg)
            state["current_node_id"] = node_id
            return state
        
        # 4. Parse worker result and extract metadata
        metadata = self._parse_worker_result(worker_type, raw_result)
        execution_time_ms = int((time.time() - start_time) * 1000)
        metadata.execution_time_ms = execution_time_ms

        # 4b. Canonical quality scoring for routing (only on validation/SMIC)
        quality_grade = state.get("last_quality_grade", "")
        if worker_type == WorkerType.SMIC or node_id == "validation":
            quality_grade = metadata.quality_metrics.get("quality_grade", "")

            # Prefer Nature-standards evaluator when available
            try:
                findings_text = (
                    str(raw_result.get("response") or "")
                    or str((raw_result.get("data") or {}).get("answer") or "")
                    or str((raw_result.get("data") or {}).get("response") or "")
                    or str(raw_result.get("data") or "")
                )
                confidence = raw_result.get("confidence")
                metrics = {}
                if isinstance(confidence, (int, float)):
                    metrics["confidence"] = float(confidence)

                report = self.driver._build_minimal_lab_report(
                    worker_name=worker_type.value,
                    query=prompt,
                    findings_text=findings_text,
                    quantitative_metrics=metrics,
                    raw_attachments=[],
                )
                quality_score = self.driver.quality_evaluator.evaluate_quality(report)
                overall = float(getattr(quality_score, "overall_score", 0.0) or 0.0)
                metadata.quality_metrics["overall_score"] = overall
                quality_grade = self._infer_quality_grade(overall)
                metadata.quality_metrics["quality_grade"] = quality_grade
            except Exception:
                # Fall back to any existing grade or infer from BVS if present
                if not quality_grade:
                    bvs_score = metadata.quality_metrics.get("bvs_score", 0.0)
                    try:
                        quality_grade = self._infer_quality_grade(float(bvs_score))
                    except Exception:
                        quality_grade = ""
        
        # 5. Update MUDOEnvelope
        updated_data = raw_result.get("data", {})
        mudo.data.update(updated_data)
        
        lineage_entry = f"{worker_type.value}-{node_id}"
        mudo.add_worker_result(worker_type, metadata, lineage_entry)
        
        # 7. Update state
        state["mudo"] = mudo.to_dict()
        state["lineage"].append(lineage_entry)
        if worker_type == WorkerType.SMIC or node_id == "validation":
            state["last_quality_grade"] = quality_grade
        state["current_node_id"] = node_id
        
        # Track backend if available
        backend_used = raw_result.get("backend_type", "")
        if backend_used:
            state["backend_trace"].append(backend_used)
        
        return state
    
    def _build_worker_prompt(self, parameters: Dict[str, Any], mudo: MUDOEnvelope) -> str:
        """
        Build worker-specific prompt from parameters and MUDO data.
        
        This logic is adapted from DAGExecutor._build_worker_prompt().
        """
        # Extract user input or construct from parameters
        user_input = mudo.data.get("user_input", "")
        
        if not user_input:
            # Construct from parameters
            param_str = ", ".join(f"{k}={v}" for k, v in parameters.items())
            user_input = f"Execute with parameters: {param_str}"
        
        # Add context from previous workers
        if mudo.worker_metadata:
            context_parts = []
            for worker_key, metadata in mudo.worker_metadata.items():
                if metadata.pipeline_trace:
                    context_parts.append(
                        f"{worker_key} executed: {', '.join(metadata.pipeline_trace)}"
                    )
            
            if context_parts:
                context = "\n".join(context_parts)
                user_input = f"{user_input}\n\nPrevious context:\n{context}"
        
        # Add parameters as instruction
        if parameters:
            params_json = str(parameters)
            user_input = f"{user_input}\n\nParameters: {params_json}"
        
        return user_input
    
    def _parse_worker_result(
        self,
        worker_type: WorkerType,
        raw_result: Dict[str, Any],
    ) -> WorkerMetadata:
        """
        Parse worker result into standardized WorkerMetadata.
        
        Adapted from DAGExecutor._parse_worker_result().
        """
        # Extract pipeline trace (tiers for SMIC, tools for others)
        pipeline_trace = []
        if "tiers_executed" in raw_result:
            pipeline_trace = raw_result["tiers_executed"]
        elif "tools_used" in raw_result:
            pipeline_trace = raw_result["tools_used"]
        elif "pipeline" in raw_result:
            pipeline_trace = raw_result["pipeline"]
        
        # Extract quality metrics
        quality_metrics = {}
        if "bvs_score" in raw_result:
            quality_metrics["bvs_score"] = raw_result["bvs_score"]
        if "causality_score" in raw_result:
            quality_metrics["causality_score"] = raw_result["causality_score"]
        if "quality_grade" in raw_result:
            quality_metrics["quality_grade"] = raw_result["quality_grade"]
        if "binding_affinity" in raw_result:
            quality_metrics["binding_affinity"] = raw_result["binding_affinity"]
        
        # Extract artifacts
        artifacts = raw_result.get("artifacts", {})
        
        # Extract errors
        errors = raw_result.get("errors", [])
        
        return WorkerMetadata(
            worker_type=worker_type,
            pipeline_trace=pipeline_trace,
            artifacts=artifacts,
            quality_metrics=quality_metrics,
            execution_time_ms=None,  # Will be set by caller
            errors=errors,
        )
    
    def _infer_quality_grade(self, bvs_score: float) -> str:
        """
        Infer quality grade from BVS score.
        
        BVS (Biological Validation Score) scale:
        - 0.95+ → A+ (publication-ready)
        - 0.90-0.94 → A
        - 0.85-0.89 → A-
        - 0.80-0.84 → B+
        - 0.75-0.79 → B (acceptable, refinement recommended)
        - 0.70-0.74 → B-
        - 0.65-0.69 → C+ (marginal)
        - 0.60-0.64 → C
        - <0.60 → F (unrecoverable)
        """
        if bvs_score >= 0.95:
            return "A+"
        elif bvs_score >= 0.90:
            return "A"
        elif bvs_score >= 0.85:
            return "A-"
        elif bvs_score >= 0.80:
            return "B+"
        elif bvs_score >= 0.75:
            return "B"
        elif bvs_score >= 0.70:
            return "B-"
        elif bvs_score >= 0.65:
            return "C+"
        elif bvs_score >= 0.60:
            return "C"
        else:
            return "F"


def create_scaffold_node(driver: AgenticDriver, template: str = "alanine_dipeptide"):
    """
    Factory function for BioDynamo scaffold node.
    
    Args:
        driver: AgenticDriver instance
        template: Scaffold template name
    
    Returns:
        Async function compatible with LangGraph.add_node()
    """
    node_executor = MICALangGraphNode(driver)
    
    async def scaffold_node(state: MICAState) -> MICAState:
        return await node_executor.execute_worker_node(
            state,
            WorkerType.BIODYNAMO,
            "scaffold",
            {"template": template},
        )
    
    return scaffold_node


def create_docking_node(driver: AgenticDriver, candidates: int = 20):
    """
    Factory function for Alchemist docking node.
    
    Args:
        driver: AgenticDriver instance
        candidates: Number of docking candidates
    
    Returns:
        Async function compatible with LangGraph.add_node()
    """
    node_executor = MICALangGraphNode(driver)
    
    async def docking_node(state: MICAState) -> MICAState:
        return await node_executor.execute_worker_node(
            state,
            WorkerType.ALCHEMIST,
            "docking",
            {"candidates": candidates, "action": "docking"},
        )
    
    return docking_node


def create_validation_node(driver: AgenticDriver, tier: int = 3):
    """
    Factory function for SMIC validation node.
    
    Args:
        driver: AgenticDriver instance
        tier: Validation tier (1-5)
    
    Returns:
        Async function compatible with LangGraph.add_node()
    """
    node_executor = MICALangGraphNode(driver)
    
    async def validation_node(state: MICAState) -> MICAState:
        return await node_executor.execute_worker_node(
            state,
            WorkerType.SMIC,
            "validation",
            {"tier": tier},
        )
    
    return validation_node

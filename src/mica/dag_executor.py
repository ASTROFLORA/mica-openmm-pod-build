"""
DAG executor for AgenticDriver enabling parallel and dependency-aware workflow orchestration.

Provides topological execution of WorkflowDAG nodes, delegating to the appropriate worker
via the existing TransportLayer while accumulating results in a MUDOEnvelope.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from .orchestration_protocol import DAGNode, MUDOEnvelope, WorkerMetadata, WorkerType, WorkflowDAG

logger = logging.getLogger(__name__)


class DAGExecutor:
    """
    Execute WorkflowDAG nodes in topological order, parallelizing independent steps.
    """

    def __init__(self, transport_layer: Any) -> None:
        """
        :param transport_layer: TransportLayer instance from agentic_driver, used to invoke workers.
        """
        self.transport = transport_layer

    async def execute(
        self,
        workflow: WorkflowDAG,
        initial_mudo: MUDOEnvelope,
        session_id: str,
    ) -> MUDOEnvelope:
        """
        Execute the DAG workflow, updating the MUDO envelope with each worker's results.

        :param workflow: WorkflowDAG defining node dependencies.
        :param initial_mudo: Starting MUDO envelope (may contain scaffold or user input).
        :param session_id: Session identifier for tracing.
        :return: Final MUDO envelope after all nodes have executed.
        """
        completed: set[str] = set()
        current_mudo = initial_mudo
        current_mudo.session_id = session_id
        self._session_id = session_id  # Store for use in _execute_node

        try:
            ordered_nodes = workflow.topological_order()
        except ValueError as exc:
            logger.error("DAG topological sort failed: %s", exc)
            raise

        # Group nodes by dependency level for parallel execution
        levels = self._group_by_level(ordered_nodes, workflow)

        for level_nodes in levels:
            logger.info("Executing level with %d nodes: %s", len(level_nodes), [n.node_id for n in level_nodes])
            # Execute all nodes in this level concurrently
            tasks = [self._execute_node(node, current_mudo) for node in level_nodes]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for node, result in zip(level_nodes, results):
                if isinstance(result, Exception):
                    logger.error("Node %s failed: %s", node.node_id, result)
                    # Record error in MUDO
                    error_metadata = WorkerMetadata(
                        worker_type=node.worker_type,
                        errors=[str(result)],
                    )
                    current_mudo.add_worker_result(
                        node.worker_type,
                        error_metadata,
                        f"{node.worker_type.value}-error-{node.node_id}",
                    )
                    raise result  # Fail-fast on first error (configurable if needed)
                else:
                    # Merge result into MUDO
                    worker_metadata, updated_data = result
                    current_mudo.data.update(updated_data)
                    current_mudo.add_worker_result(
                        node.worker_type,
                        worker_metadata,
                        f"{node.worker_type.value}-{node.node_id}",
                    )
                    completed.add(node.node_id)

        logger.info("DAG execution complete. Completed nodes: %s", completed)
        return current_mudo

    async def _execute_node(self, node: DAGNode, mudo: MUDOEnvelope) -> tuple[WorkerMetadata, Dict[str, Any]]:
        """
        Execute a single DAG node by invoking the worker via transport layer.

        :param node: DAGNode to execute.
        :param mudo: Current MUDO envelope (read-only; return updated data separately).
        :return: (WorkerMetadata, updated_data_dict)
        """
        logger.debug("Executing node %s (worker=%s)", node.node_id, node.worker_type.value)

        # Build prompt or input for the worker from MUDO data + node parameters
        prompt = self._build_worker_prompt(node, mudo)

        # Invoke worker via transport with session_id
        session_id = getattr(self, '_session_id', 'default')
        raw_result = await self.transport.execute_worker(node.worker_type.value, prompt, session_id)

        # Parse result into standardized metadata
        metadata = self._parse_worker_result(node.worker_type, raw_result)

        # Extract updated data payload (e.g., new artifacts, embeddings, scores)
        updated_data = raw_result.get("data", {})

        return metadata, updated_data

    def _build_worker_prompt(self, node: DAGNode, mudo: MUDOEnvelope) -> str:
        """
        Construct a natural-language or structured prompt for the worker.

        For now, serializes node parameters and references MUDO data.
        In production, this would use a more sophisticated template or LLM-based prompt builder.
        """
        data_summary = str(mudo.data)[:200]  # truncate for brevity
        params_str = ", ".join(f"{k}={v}" for k, v in node.parameters.items())
        prompt = f"Execute {node.worker_type.value} with parameters: {params_str}. Context: {data_summary}"
        return prompt

    def _parse_worker_result(self, worker_type: WorkerType, result: Dict[str, Any]) -> WorkerMetadata:
        """
        Extract standardized metadata from worker's raw result dictionary.

        Adapts to existing worker response formats (pipeline_trace, tiers_executed, bvs_score, etc.).
        """
        metadata = WorkerMetadata(worker_type=worker_type)

        # Alchemist pattern
        if "pipeline_trace" in result:
            metadata.pipeline_trace = result["pipeline_trace"]
        # SMIC pattern
        if "tiers_executed" in result:
            metadata.pipeline_trace = result["tiers_executed"]
        # BioDynamo pattern
        if "workflow_results" in result:
            wr = result["workflow_results"]
            metadata.pipeline_trace = [
                step for step in ["nlp_analysis", "scaffold_generation", "validation", "execution", "biosite_export"]
                if step in wr
            ]

        # Quality metrics
        quality_keys = ["bvs_score", "confidence", "rmsd", "quality_grade", "causality_score"]
        for key in quality_keys:
            if key in result:
                metadata.quality_metrics[key] = float(result[key])

        # Artifacts
        if "artifact_manifest" in result:
            metadata.artifacts = {a["name"]: a.get("location", a) for a in result["artifact_manifest"]}
        elif "artifacts" in result:
            metadata.artifacts = result["artifacts"]

        # Execution time
        if "latency_ms_total" in result:
            metadata.execution_time_ms = result["latency_ms_total"]

        # Errors
        if "errors" in result:
            metadata.errors = result["errors"]
        elif "error" in result:
            metadata.errors = [result["error"]]

        return metadata

    def _group_by_level(self, ordered_nodes: List[DAGNode], workflow: WorkflowDAG) -> List[List[DAGNode]]:
        """
        Group nodes by dependency level for parallel execution within each level.

        :param ordered_nodes: Topologically sorted nodes.
        :param workflow: Original WorkflowDAG.
        :return: List of lists, each sublist containing nodes executable in parallel.
        """
        levels: List[List[DAGNode]] = []
        node_level: Dict[str, int] = {}

        for node in ordered_nodes:
            if not node.dependencies:
                level = 0
            else:
                level = max(node_level[dep] for dep in node.dependencies) + 1
            node_level[node.node_id] = level

            while len(levels) <= level:
                levels.append([])
            levels[level].append(node)

        return levels

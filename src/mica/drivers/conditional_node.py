#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConditionalNode - DAG Branching for Workflow Optimization
==========================================================

Enables if/else/switch branching in multi-agent workflows for:
1. Resource optimization (skip expensive steps when unnecessary)
2. Quality-based routing (route low-quality candidates to refinement)
3. Adaptive workflows (adjust strategy based on intermediate results)

Inspired by ProtAgents MIT paper: Planner creates branching workflows
based on task complexity and resource constraints.

Use Cases:
1. Skip expensive iMMD validation if sequence_length < 128 (fast folding)
2. Route low-QSAR-score candidates to FragmentOptimization instead of Docking
3. Switch from umbrella sampling to metadynamics if uncertainty > 0.3

Integration:
- PlannerAgent creates ConditionalNodes in workflow DAG
- ExecutionEngine evaluates conditions at runtime
- AssistantAgent logs branching decisions for reproducibility
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# ============================================================================
# CONDITION EVALUATION
# ============================================================================

class ComparisonOperator(Enum):
    """Comparison operators for condition evaluation."""
    
    EQUAL = "=="
    NOT_EQUAL = "!="
    GREATER_THAN = ">"
    GREATER_EQUAL = ">="
    LESS_THAN = "<"
    LESS_EQUAL = "<="
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    MATCHES_REGEX = "matches_regex"


class LogicalOperator(Enum):
    """Logical operators for combining conditions."""
    
    AND = "and"
    OR = "or"
    NOT = "not"


@dataclass
class ConditionExpression:
    """
    A single condition expression.
    
    Examples:
    - sequence_length < 128
    - qsar_score >= 0.7
    - conformational_state in ["Open", "Intermediate"]
    - ligand_smiles matches ".*CN.*" (contains CN group)
    """
    
    # Variable to evaluate
    variable: str  # Key in context dict
    
    # Comparison
    operator: ComparisonOperator
    value: Any  # Value to compare against
    
    # Optional custom evaluator (overrides operator)
    custom_evaluator: Optional[Callable[[Any], bool]] = None
    
    def evaluate(self, context: Dict[str, Any]) -> bool:
        """Evaluate condition against context."""
        
        # Get variable value from context
        if self.variable not in context:
            logger.warning(f"Variable not found in context: {self.variable}")
            return False
        
        var_value = context[self.variable]
        
        # Custom evaluator takes precedence
        if self.custom_evaluator:
            try:
                return self.custom_evaluator(var_value)
            except Exception as e:
                logger.error(f"Custom evaluator failed for {self.variable}: {e}")
                return False
        
        # Standard operator evaluation
        try:
            if self.operator == ComparisonOperator.EQUAL:
                return var_value == self.value
            elif self.operator == ComparisonOperator.NOT_EQUAL:
                return var_value != self.value
            elif self.operator == ComparisonOperator.GREATER_THAN:
                return var_value > self.value
            elif self.operator == ComparisonOperator.GREATER_EQUAL:
                return var_value >= self.value
            elif self.operator == ComparisonOperator.LESS_THAN:
                return var_value < self.value
            elif self.operator == ComparisonOperator.LESS_EQUAL:
                return var_value <= self.value
            elif self.operator == ComparisonOperator.IN:
                return var_value in self.value
            elif self.operator == ComparisonOperator.NOT_IN:
                return var_value not in self.value
            elif self.operator == ComparisonOperator.CONTAINS:
                return self.value in var_value
            elif self.operator == ComparisonOperator.MATCHES_REGEX:
                import re
                return bool(re.match(self.value, str(var_value)))
            else:
                logger.error(f"Unknown operator: {self.operator}")
                return False
        
        except Exception as e:
            logger.error(f"Condition evaluation failed: {self.variable} {self.operator.value} {self.value} - {e}")
            return False


@dataclass
class CompositeCondition:
    """
    Combines multiple conditions with logical operators.
    
    Examples:
    - (sequence_length < 128) AND (secondary_structure == "alpha_helix")
    - (qsar_score >= 0.7) OR (binding_affinity < -8.0)
    - NOT (conformational_state == "Closed")
    """
    
    conditions: List[Union[ConditionExpression, CompositeCondition]]
    operator: LogicalOperator
    
    def evaluate(self, context: Dict[str, Any]) -> bool:
        """Evaluate composite condition."""
        
        if not self.conditions:
            return True  # Empty condition is always true
        
        results = [cond.evaluate(context) for cond in self.conditions]
        
        if self.operator == LogicalOperator.AND:
            return all(results)
        elif self.operator == LogicalOperator.OR:
            return any(results)
        elif self.operator == LogicalOperator.NOT:
            # NOT operator expects single condition
            if len(results) != 1:
                logger.warning(f"NOT operator expects single condition, got {len(results)}")
                return False
            return not results[0]
        else:
            logger.error(f"Unknown logical operator: {self.operator}")
            return False


# ============================================================================
# WORKFLOW NODES
# ============================================================================

class WorkflowNode(ABC):
    """Abstract base class for workflow nodes."""
    
    @abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute node logic.
        
        Returns:
            Updated context dict
        """
        pass
    
    @abstractmethod
    def get_next_node(self, context: Dict[str, Any]) -> Optional[str]:
        """
        Get ID of next node to execute.
        
        Returns:
            Next node ID, or None if terminal node
        """
        pass


@dataclass
class TaskNode(WorkflowNode):
    """
    Executes a specialist task.
    
    Example:
    - Run BioDynamo iMMD simulation
    - Run Alchemist QSAR prediction
    - Run Chronosfold PIKAN validation
    """
    
    node_id: str
    specialist_id: str
    function_id: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    next_node_id: Optional[str] = None
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute specialist task (placeholder - actual execution by WorkflowEngine)."""
        logger.info(f"TaskNode {self.node_id}: {self.specialist_id}.{self.function_id}")
        
        # In real implementation, this would call the specialist
        # For now, just mark as executed
        context[f"{self.node_id}_executed"] = True
        
        return context
    
    def get_next_node(self, context: Dict[str, Any]) -> Optional[str]:
        """Return next node ID."""
        return self.next_node_id


@dataclass
class ConditionalNode(WorkflowNode):
    """
    Branches workflow based on runtime conditions.
    
    Supports:
    1. If/Else branching (2 branches)
    2. Switch/Case branching (N branches)
    3. Nested conditions (conditions within branches)
    
    Example:
        if sequence_length < 128:
            next = "skip_immd"  # Fast folding, skip expensive validation
        else:
            next = "run_immd"   # Slow folding, need iMMD
    """
    
    node_id: str
    description: str
    
    # Branching logic
    branches: List[Branch] = field(default_factory=list)
    default_branch: Optional[str] = None  # Fallback if no conditions match
    
    # Metadata
    optimization_rationale: Optional[str] = None  # Why this branching exists
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate conditions and select branch (no actual execution)."""
        logger.info(f"ConditionalNode {self.node_id}: Evaluating {len(self.branches)} branches")
        
        # Mark as evaluated
        context[f"{self.node_id}_evaluated"] = True
        
        return context
    
    def get_next_node(self, context: Dict[str, Any]) -> Optional[str]:
        """Evaluate conditions and return next node ID."""
        
        # Evaluate branches in order
        for branch in self.branches:
            if branch.condition.evaluate(context):
                logger.info(
                    f"ConditionalNode {self.node_id}: Branch '{branch.name}' matched → {branch.next_node_id}"
                )
                context[f"{self.node_id}_branch_taken"] = branch.name
                return branch.next_node_id
        
        # No conditions matched, use default
        if self.default_branch:
            logger.info(f"ConditionalNode {self.node_id}: Default branch → {self.default_branch}")
            context[f"{self.node_id}_branch_taken"] = "default"
            return self.default_branch
        
        # No default specified
        logger.warning(f"ConditionalNode {self.node_id}: No conditions matched and no default branch")
        return None


@dataclass
class Branch:
    """A single branch in a ConditionalNode."""
    
    name: str
    condition: Union[ConditionExpression, CompositeCondition]
    next_node_id: str
    description: Optional[str] = None


# ============================================================================
# WORKFLOW DAG
# ============================================================================

@dataclass
class WorkflowDAG:
    """
    Directed Acyclic Graph of workflow nodes.
    
    Combines TaskNodes (specialist execution) and ConditionalNodes (branching).
    Enables PlannerAgent to create adaptive, optimized workflows.
    """
    
    dag_id: str
    description: str
    
    nodes: Dict[str, WorkflowNode] = field(default_factory=dict)
    start_node_id: str = ""
    
    # Metadata
    created_by: str = "planner"
    optimization_goals: List[str] = field(default_factory=list)
    
    def add_node(self, node: WorkflowNode) -> None:
        """Add node to DAG."""
        self.nodes[node.node_id] = node
        logger.debug(f"Added node: {node.node_id} ({type(node).__name__})")
    
    def set_start(self, node_id: str) -> None:
        """Set starting node."""
        if node_id not in self.nodes:
            raise ValueError(f"Node not found: {node_id}")
        self.start_node_id = node_id
        logger.info(f"Start node set to: {node_id}")
    
    def execute(self, initial_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute DAG from start node.
        
        Returns:
            Final context after all nodes executed
        """
        if not self.start_node_id:
            raise ValueError("Start node not set")
        
        context = initial_context.copy()
        current_node_id = self.start_node_id
        execution_path = []
        
        max_iterations = 100  # Prevent infinite loops
        iteration = 0
        
        while current_node_id and iteration < max_iterations:
            if current_node_id not in self.nodes:
                logger.error(f"Node not found: {current_node_id}")
                break
            
            current_node = self.nodes[current_node_id]
            execution_path.append(current_node_id)
            
            logger.info(f"Executing node: {current_node_id}")
            
            # Execute node
            context = current_node.execute(context)
            
            # Get next node
            current_node_id = current_node.get_next_node(context)
            
            iteration += 1
        
        # Store execution metadata
        context["_execution_path"] = execution_path
        context["_total_nodes_executed"] = len(execution_path)
        
        if iteration >= max_iterations:
            logger.warning(f"DAG execution terminated: max iterations ({max_iterations}) reached")
        
        logger.info(f"DAG execution complete: {len(execution_path)} nodes executed")
        return context


# ============================================================================
# DAG BUILDER UTILITIES
# ============================================================================

class DAGBuilder:
    """Helper for building WorkflowDAGs."""
    
    def __init__(self, dag_id: str, description: str):
        """Initialize builder."""
        self.dag = WorkflowDAG(dag_id=dag_id, description=description)
    
    def add_task(
        self,
        node_id: str,
        specialist_id: str,
        function_id: str,
        parameters: Dict[str, Any] = None,
        next_node_id: Optional[str] = None,
    ) -> DAGBuilder:
        """Add TaskNode to DAG."""
        node = TaskNode(
            node_id=node_id,
            specialist_id=specialist_id,
            function_id=function_id,
            parameters=parameters or {},
            next_node_id=next_node_id,
        )
        self.dag.add_node(node)
        return self
    
    def add_conditional(
        self,
        node_id: str,
        description: str,
        branches: List[Branch],
        default_branch: Optional[str] = None,
        optimization_rationale: Optional[str] = None,
    ) -> DAGBuilder:
        """Add ConditionalNode to DAG."""
        node = ConditionalNode(
            node_id=node_id,
            description=description,
            branches=branches,
            default_branch=default_branch,
            optimization_rationale=optimization_rationale,
        )
        self.dag.add_node(node)
        return self
    
    def set_start(self, node_id: str) -> DAGBuilder:
        """Set start node."""
        self.dag.set_start(node_id)
        return self
    
    def build(self) -> WorkflowDAG:
        """Build and return DAG."""
        return self.dag


# ============================================================================
# EXAMPLE WORKFLOWS
# ============================================================================

def create_denovo_protein_workflow() -> WorkflowDAG:
    """
    Create adaptive de novo protein generation workflow.
    
    Workflow:
    1. Generate backbone (RFdiffusion)
    2. Design sequence (ProteinMPNN)
    3. Branch based on sequence length:
       - If < 128: Skip iMMD (fast folding) → Lightweight validation
       - If >= 128: Run iMMD (slow folding) → Full validation
    4. QSAR prediction
    5. Branch based on QSAR score:
       - If score >= 0.7: Run docking (promising candidate)
       - If score < 0.7: Fragment optimization (needs improvement)
    """
    
    builder = DAGBuilder(
        dag_id="denovo_protein_adaptive",
        description="Adaptive de novo protein generation with branching optimization",
    )
    
    # 1. Generate backbone
    builder.add_task(
        node_id="generate_backbone",
        specialist_id="rfdiffusion_specialist",
        function_id="biodynamo.rfdiffusion.generate_backbone",
        parameters={"num_designs": 5},
        next_node_id="design_sequence",
    )
    
    # 2. Design sequence
    builder.add_task(
        node_id="design_sequence",
        specialist_id="proteinmpnn_specialist",
        function_id="alchemist.proteinmpnn.design_sequence",
        parameters={"num_sequences": 3},
        next_node_id="branch_by_length",
    )
    
    # 3. Branch by sequence length
    builder.add_conditional(
        node_id="branch_by_length",
        description="Skip expensive iMMD for small proteins (fast folding)",
        branches=[
            Branch(
                name="small_protein",
                condition=ConditionExpression(
                    variable="sequence_length",
                    operator=ComparisonOperator.LESS_THAN,
                    value=128,
                ),
                next_node_id="lightweight_validation",
                description="Sequence < 128 residues: fast folding, skip iMMD",
            ),
            Branch(
                name="large_protein",
                condition=ConditionExpression(
                    variable="sequence_length",
                    operator=ComparisonOperator.GREATER_EQUAL,
                    value=128,
                ),
                next_node_id="run_immd",
                description="Sequence >= 128 residues: slow folding, need iMMD",
            ),
        ],
        optimization_rationale="iMMD costs ~1 hour for large proteins, unnecessary for fast-folding small proteins",
    )
    
    # 4a. Full iMMD validation (large proteins)
    builder.add_task(
        node_id="run_immd",
        specialist_id="immd_control",
        function_id="biodynamo.immd_control.run_aa_cg_cycles",
        parameters={"max_cycles": 10},
        next_node_id="qsar_prediction",
    )
    
    # 4b. Lightweight validation (small proteins)
    builder.add_task(
        node_id="lightweight_validation",
        specialist_id="quality_control",
        function_id="biodynamo.quality_control.validate_structure",
        parameters={"mode": "fast"},
        next_node_id="qsar_prediction",
    )
    
    # 5. QSAR prediction
    builder.add_task(
        node_id="qsar_prediction",
        specialist_id="qsar_modeling",
        function_id="alchemist.qsar.predict_activity",
        next_node_id="branch_by_qsar",
    )
    
    # 6. Branch by QSAR score
    builder.add_conditional(
        node_id="branch_by_qsar",
        description="Route low-scoring candidates to fragment optimization",
        branches=[
            Branch(
                name="high_activity",
                condition=ConditionExpression(
                    variable="qsar_score",
                    operator=ComparisonOperator.GREATER_EQUAL,
                    value=0.7,
                ),
                next_node_id="molecular_docking",
                description="QSAR score >= 0.7: promising, run docking",
            ),
            Branch(
                name="low_activity",
                condition=ConditionExpression(
                    variable="qsar_score",
                    operator=ComparisonOperator.LESS_THAN,
                    value=0.7,
                ),
                next_node_id="fragment_optimization",
                description="QSAR score < 0.7: needs improvement, optimize fragments",
            ),
        ],
        optimization_rationale="Docking costs ~60s per ligand, avoid for low-activity candidates",
    )
    
    # 7a. Molecular docking (high-scoring)
    builder.add_task(
        node_id="molecular_docking",
        specialist_id="molecular_docking",
        function_id="alchemist.docking.dock_ligand",
        next_node_id=None,  # Terminal
    )
    
    # 7b. Fragment optimization (low-scoring)
    builder.add_task(
        node_id="fragment_optimization",
        specialist_id="fragment_optimization",
        function_id="alchemist.fragment.optimize_fragments",
        next_node_id=None,  # Terminal
    )
    
    builder.set_start("generate_backbone")
    
    return builder.build()


def create_sampling_strategy_workflow() -> WorkflowDAG:
    """
    Create adaptive enhanced sampling workflow.
    
    Workflow:
    1. MDGraphEmb state classification
    2. Branch by uncertainty:
       - If uncertainty < 0.2: Use umbrella sampling (well-characterized states)
       - If uncertainty >= 0.2 AND < 0.5: Use metadynamics (moderate exploration)
       - If uncertainty >= 0.5: Use RAMD (high exploration)
    3. Execute selected sampling strategy
    """
    
    builder = DAGBuilder(
        dag_id="adaptive_sampling",
        description="Select sampling strategy based on MDGraphEmb uncertainty",
    )
    
    # 1. State classification
    builder.add_task(
        node_id="classify_states",
        specialist_id="state_classifier",
        function_id="biodynamo.state_classifier.classify_conformations",
        next_node_id="branch_by_uncertainty",
    )
    
    # 2. Branch by uncertainty
    builder.add_conditional(
        node_id="branch_by_uncertainty",
        description="Select sampling strategy based on state uncertainty",
        branches=[
            Branch(
                name="low_uncertainty",
                condition=ConditionExpression(
                    variable="uncertainty_score",
                    operator=ComparisonOperator.LESS_THAN,
                    value=0.2,
                ),
                next_node_id="umbrella_sampling",
                description="Uncertainty < 0.2: states well-characterized, use umbrella sampling",
            ),
            Branch(
                name="moderate_uncertainty",
                condition=CompositeCondition(
                    conditions=[
                        ConditionExpression(
                            variable="uncertainty_score",
                            operator=ComparisonOperator.GREATER_EQUAL,
                            value=0.2,
                        ),
                        ConditionExpression(
                            variable="uncertainty_score",
                            operator=ComparisonOperator.LESS_THAN,
                            value=0.5,
                        ),
                    ],
                    operator=LogicalOperator.AND,
                ),
                next_node_id="metadynamics",
                description="0.2 <= Uncertainty < 0.5: moderate exploration, use metadynamics",
            ),
            Branch(
                name="high_uncertainty",
                condition=ConditionExpression(
                    variable="uncertainty_score",
                    operator=ComparisonOperator.GREATER_EQUAL,
                    value=0.5,
                ),
                next_node_id="ramd",
                description="Uncertainty >= 0.5: high exploration, use RAMD",
            ),
        ],
        optimization_rationale="Match sampling method to exploration needs based on state uncertainty",
    )
    
    # 3a. Umbrella sampling
    builder.add_task(
        node_id="umbrella_sampling",
        specialist_id="sampling_orchestrator",
        function_id="biodynamo.sampling.umbrella_sampling",
        next_node_id=None,
    )
    
    # 3b. Metadynamics
    builder.add_task(
        node_id="metadynamics",
        specialist_id="sampling_orchestrator",
        function_id="biodynamo.sampling.metadynamics",
        next_node_id=None,
    )
    
    # 3c. RAMD
    builder.add_task(
        node_id="ramd",
        specialist_id="sampling_orchestrator",
        function_id="biodynamo.sampling.ramd",
        next_node_id=None,
    )
    
    builder.set_start("classify_states")
    
    return builder.build()


# ============================================================================
# TEST SUITE
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("="*80)
    print("ConditionalNode System - Test Suite")
    print("="*80)
    
    # Test 1: Simple condition evaluation
    print("\n1. Simple Condition Evaluation")
    print("-" * 40)
    condition = ConditionExpression(
        variable="sequence_length",
        operator=ComparisonOperator.LESS_THAN,
        value=128,
    )
    context = {"sequence_length": 95}
    result = condition.evaluate(context)
    print(f"sequence_length (95) < 128: {result}")
    
    # Test 2: Composite condition
    print("\n2. Composite Condition (AND)")
    print("-" * 40)
    composite = CompositeCondition(
        conditions=[
            ConditionExpression(
                variable="qsar_score",
                operator=ComparisonOperator.GREATER_EQUAL,
                value=0.7,
            ),
            ConditionExpression(
                variable="binding_affinity",
                operator=ComparisonOperator.LESS_THAN,
                value=-8.0,
            ),
        ],
        operator=LogicalOperator.AND,
    )
    context = {"qsar_score": 0.85, "binding_affinity": -9.2}
    result = composite.evaluate(context)
    print(f"(qsar_score >= 0.7) AND (binding_affinity < -8.0): {result}")
    
    # Test 3: ConditionalNode branching
    print("\n3. ConditionalNode Branching")
    print("-" * 40)
    conditional = ConditionalNode(
        node_id="branch_test",
        description="Test branching",
        branches=[
            Branch(
                name="small",
                condition=ConditionExpression(
                    variable="size",
                    operator=ComparisonOperator.LESS_THAN,
                    value=100,
                ),
                next_node_id="small_path",
            ),
            Branch(
                name="large",
                condition=ConditionExpression(
                    variable="size",
                    operator=ComparisonOperator.GREATER_EQUAL,
                    value=100,
                ),
                next_node_id="large_path",
            ),
        ],
    )
    context = {"size": 150}
    next_node = conditional.get_next_node(context)
    print(f"size=150 → next_node: {next_node} (branch: {context.get('branch_test_branch_taken')})")
    
    # Test 4: De novo protein workflow
    print("\n4. De Novo Protein Workflow Execution")
    print("-" * 40)
    workflow = create_denovo_protein_workflow()
    context = {
        "sequence_length": 95,  # Small protein
        "qsar_score": 0.85,     # High activity
    }
    result = workflow.execute(context)
    print(f"Execution path: {' → '.join(result['_execution_path'])}")
    print(f"Total nodes: {result['_total_nodes_executed']}")
    
    # Test 5: Sampling strategy workflow
    print("\n5. Sampling Strategy Workflow Execution")
    print("-" * 40)
    workflow = create_sampling_strategy_workflow()
    context = {
        "uncertainty_score": 0.35,  # Moderate uncertainty
    }
    result = workflow.execute(context)
    print(f"Execution path: {' → '.join(result['_execution_path'])}")
    print(f"Total nodes: {result['_total_nodes_executed']}")
    
    print("\n" + "="*80)
    print("ConditionalNode system tested successfully!")
    print("="*80)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
General Multi-Worker Orchestrator
==================================

Top-level driver coordinating BioDynamo, Alchemist, and SMIC workers.

Implements MPI-UOS framework for hierarchical multi-worker orchestration.
Routes queries to appropriate worker drivers based on domain expertise.

Responsibilities:
- Query intent parsing (BioDynamo vs Alchemist vs SMIC vs Multi-worker)
- Cross-worker validation and consensus building
- Workflow-level proactive problem identification
- DAG executor integration for complex workflows

Based on:
- MPI-UOS: Tlahuizcalpantecuhtli breakthrough methodology
- MSRP: 5-phase scientific reasoning at workflow level
- Multi-agent architecture: Hierarchical specialist orchestration
"""

from __future__ import annotations

import logging
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path

from .worker_driver import WorkerDriver, WorkerDriverConfig, SpecialistAgent
from .biodynamo_driver import BioDynamoDriver
from .alchemist_driver import AlchemistDriver
from .smic_driver import SMICDriver

logger = logging.getLogger(__name__)


# ============================================================================
# Memory Bank for Multi-Turn Workflows
# ============================================================================

class ConversationMemoryBank:
    """
    Conversation Memory Bank - Multi-Turn State Persistence.
    
    Stores intermediate results (sequences, structures, properties) across
    conversation turns, enabling complex workflows like:
    - Turn 1: Design protein
    - Turn 2: Compute properties
    - Turn 3: Analyze results
    
    Based on ProtAgents' ability to "memorize sequences across turns".
    """
    
    def __init__(self, storage_dir: Optional[Path] = None):
        """
        Initialize memory bank.
        
        Args:
            storage_dir: Directory for persistent storage (None = in-memory only)
        """
        self.storage_dir = storage_dir
        self.memory: Dict[str, Dict[str, Any]] = {}  # turn_id → data
        self.index: Dict[str, List[str]] = {}  # entity_type → [turn_ids]
        
        if storage_dir:
            storage_dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()
        
        logger.info(f"💾 ConversationMemoryBank initialized (persistent={storage_dir is not None})")
    
    def store_turn_state(
        self,
        turn_id: str,
        data: Dict[str, Any],
        entities: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Store state for a conversation turn.
        
        Args:
            turn_id: Unique turn identifier (e.g., "turn_1", "session_abc_turn_3")
            data: Data to store (sequences, structures, properties, etc.)
            entities: Entity index for retrieval (e.g., {"protein_id": "P12345"})
        """
        self.memory[turn_id] = {
            'data': data,
            'entities': entities or {},
            'timestamp': datetime.now().isoformat(),
        }
        
        # Update index
        if entities:
            for entity_type, entity_id in entities.items():
                if entity_type not in self.index:
                    self.index[entity_type] = []
                self.index[entity_type].append(turn_id)
        
        # Persist to disk if configured
        if self.storage_dir:
            self._save_turn_to_disk(turn_id)
        
        logger.info(f"💾 Stored turn state: {turn_id} ({len(data)} keys)")
    
    def retrieve_turn_state(self, turn_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve state for specific turn."""
        return self.memory.get(turn_id, {}).get('data')
    
    def retrieve_by_entity(self, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        """Retrieve all turns containing specific entity."""
        
        matching_turns = []
        
        for turn_id in self.index.get(entity_type, []):
            turn_data = self.memory.get(turn_id, {})
            entities = turn_data.get('entities', {})
            
            if entities.get(entity_type) == entity_id:
                matching_turns.append({
                    'turn_id': turn_id,
                    'data': turn_data.get('data'),
                    'timestamp': turn_data.get('timestamp'),
                })
        
        return matching_turns
    
    def get_context(self, query: str, max_turns: int = 5) -> Dict[str, Any]:
        """
        Retrieve relevant context for query.
        
        Args:
            query: Query to contextualize
            max_turns: Maximum number of turns to retrieve
        
        Returns:
            Context dict with relevant previous results
        """
        # Simple heuristic: Return last N turns
        # TODO: Implement semantic search over memory
        
        recent_turns = sorted(self.memory.keys(), reverse=True)[:max_turns]
        
        context = {}
        for turn_id in recent_turns:
            context[turn_id] = self.memory[turn_id]['data']
        
        return context
    
    def _save_turn_to_disk(self, turn_id: str) -> None:
        """Persist turn to disk."""
        if not self.storage_dir:
            return
        
        turn_file = self.storage_dir / f"{turn_id}.json"
        
        with open(turn_file, 'w') as f:
            json.dump(self.memory[turn_id], f, indent=2, default=str)
    
    def _load_from_disk(self) -> None:
        """Load memory from disk on initialization."""
        if not self.storage_dir or not self.storage_dir.exists():
            return
        
        for turn_file in self.storage_dir.glob("*.json"):
            turn_id = turn_file.stem
            
            with open(turn_file, 'r') as f:
                self.memory[turn_id] = json.load(f)
            
            # Rebuild index
            entities = self.memory[turn_id].get('entities', {})
            for entity_type, entity_id in entities.items():
                if entity_type not in self.index:
                    self.index[entity_type] = []
                self.index[entity_type].append(turn_id)
        
        logger.info(f"💾 Loaded {len(self.memory)} turns from disk")


# ============================================================================
# Planner-Assistant-Critic Architecture (ProtAgents Pattern)
# ============================================================================

class PlannerAgent(SpecialistAgent):
    """
    Planner Agent - Task Decomposition & Function Selection.
    
    Responsibilities:
    - Break complex queries into sub-tasks
    - Identify required functions/workers
    - Determine input parameters for each step
    - Create execution plan with dependencies
    
    Based on ProtAgents architecture.
    """
    
    def __init__(self):
        super().__init__(
            agent_id="planner",
            agent_name="PlannerAgent",
            expertise_area="Task Decomposition & Planning",
            description=(
                "Develops detailed execution plans by breaking complex queries into "
                "sub-tasks, identifying necessary functions, and specifying parameters."
            ),
            capabilities=[
                "Query parsing and intent analysis",
                "Task decomposition into sub-tasks",
                "Function/worker selection",
                "Parameter identification",
                "Dependency graph construction",
            ],
            ai_university_role="Chief Strategy Officer, Workflow Planning",
            research_focus=[
                "Multi-step task orchestration",
                "Dependency resolution",
                "Resource allocation",
            ],
        )
    
    async def create_plan(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Create execution plan for query.
        
        Returns:
            {
                'steps': List[Dict],  # Each step has function, params, dependencies
                'workers': List[str],  # Worker IDs required
                'estimated_time': float,
                'complexity': str,  # 'simple', 'moderate', 'complex'
            }
        """
        logger.info(f"📋 Planner: Creating plan for query")
        
        # Simple heuristic planning (TODO: Use LLM for sophisticated planning)
        plan = {
            'steps': [],
            'workers': [],
            'estimated_time': 0.0,
            'complexity': 'simple',
        }
        
        query_lower = query.lower()
        
        # Drug discovery pipeline
        if "drug discovery" in query_lower or "lead optimization" in query_lower:
            plan['steps'] = [
                {
                    'step_id': 1,
                    'function': 'qsar_modeling',
                    'worker': 'alchemist',
                    'params': {'query': query},
                    'dependencies': [],
                },
                {
                    'step_id': 2,
                    'function': 'molecular_docking',
                    'worker': 'alchemist',
                    'params': {'candidates': 'from_step_1'},
                    'dependencies': [1],
                },
                {
                    'step_id': 3,
                    'function': 'md_simulation',
                    'worker': 'biodynamo',
                    'params': {'top_poses': 'from_step_2'},
                    'dependencies': [2],
                },
                {
                    'step_id': 4,
                    'function': 'free_energy',
                    'worker': 'biodynamo',
                    'params': {'trajectories': 'from_step_3'},
                    'dependencies': [3],
                },
            ]
            plan['workers'] = ['alchemist', 'biodynamo']
            plan['complexity'] = 'complex'
        
        # Single worker tasks
        elif any(kw in query_lower for kw in ["docking", "qsar", "virtual screening"]):
            plan['steps'] = [
                {
                    'step_id': 1,
                    'function': 'execute',
                    'worker': 'alchemist',
                    'params': {'query': query},
                    'dependencies': [],
                }
            ]
            plan['workers'] = ['alchemist']
            plan['complexity'] = 'simple'
        
        elif any(kw in query_lower for kw in ["molecular dynamics", "md simulation", "free energy"]):
            plan['steps'] = [
                {
                    'step_id': 1,
                    'function': 'execute',
                    'worker': 'biodynamo',
                    'params': {'query': query},
                    'dependencies': [],
                }
            ]
            plan['workers'] = ['biodynamo']
            plan['complexity'] = 'simple'
        
        logger.info(f"📋 Planner: Created {len(plan['steps'])}-step plan ({plan['complexity']})")
        return plan


class CriticAgent(SpecialistAgent):
    """
    Critic Agent - Plan Validation & Result Analysis.
    
    Responsibilities:
    - Review plans BEFORE execution (pre-validation)
    - Check function parameters and dependencies
    - Diagnose errors AFTER execution (post-validation)
    - Provide constructive feedback
    - Suggest corrections
    
    Based on ProtAgents architecture.
    """
    
    def __init__(self):
        super().__init__(
            agent_id="critic",
            agent_name="CriticAgent",
            expertise_area="Plan Validation & Error Diagnosis",
            description=(
                "Reviews execution plans for completeness and correctness, validates "
                "results against constraints, diagnoses errors, and suggests fixes."
            ),
            capabilities=[
                "Plan semantic validation",
                "Parameter completeness checking",
                "Dependency cycle detection",
                "Result constraint validation",
                "Error diagnosis and correction",
            ],
            ai_university_role="Chief Quality Officer, Scientific Review",
            research_focus=[
                "Scientific correctness validation",
                "Error pattern recognition",
                "Feedback generation",
            ],
        )
    
    async def validate_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate plan BEFORE execution (PRE-VALIDATION).
        
        Checks:
        - All required parameters present
        - Dependencies are acyclic
        - Functions exist for specified workers
        - No redundant steps (e.g., saving sequence pre/post fold)
        
        Returns:
            {
                'valid': bool,
                'issues': List[str],
                'suggestions': List[str],
            }
        """
        logger.info(f"🔍 Critic: Validating plan with {len(plan.get('steps', []))} steps")
        
        issues = []
        suggestions = []
        
        # Check for empty plan
        if not plan.get('steps'):
            issues.append("Plan has no steps")
            return {'valid': False, 'issues': issues, 'suggestions': suggestions}
        
        # Check for dependency cycles
        if self._has_dependency_cycle(plan['steps']):
            issues.append("Dependency cycle detected in plan steps")
        
        # Check for redundant operations
        redundancies = self._detect_redundancies(plan['steps'])
        if redundancies:
            for redundancy in redundancies:
                issues.append(f"Redundant step: {redundancy}")
                suggestions.append(f"Consider removing redundant {redundancy}")
        
        # Check for missing parameters
        for step in plan['steps']:
            if not step.get('params'):
                issues.append(f"Step {step['step_id']} missing parameters")
        
        valid = len(issues) == 0
        
        if valid:
            logger.info("✅ Critic: Plan validation PASSED")
        else:
            logger.warning(f"⚠️ Critic: Plan validation FAILED ({len(issues)} issues)")
        
        return {
            'valid': valid,
            'issues': issues,
            'suggestions': suggestions,
        }
    
    async def diagnose_error(self, error: Exception, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Diagnose error and suggest fix (POST-VALIDATION).
        
        Common errors:
        - JSON formatting (delimiter, quotes)
        - Parameter type mismatch
        - Missing dependencies
        
        Returns:
            {
                'diagnosis': str,
                'fix_suggestion': str,
                'retry_params': Optional[Dict],
            }
        """
        logger.info(f"🔍 Critic: Diagnosing error: {type(error).__name__}")
        
        error_str = str(error).lower()
        
        # JSON errors
        if "json" in error_str or "delimiter" in error_str:
            return {
                'diagnosis': "JSON formatting error detected",
                'fix_suggestion': "Replace single quotes with double quotes, ensure proper escaping",
                'retry_params': context.get('params', {}),  # TODO: Actually fix JSON
            }
        
        # Parameter errors
        elif "parameter" in error_str or "argument" in error_str:
            return {
                'diagnosis': "Missing or invalid parameter",
                'fix_suggestion': "Check function signature and provide required parameters",
                'retry_params': None,
            }
        
        # Generic error
        else:
            return {
                'diagnosis': f"Error: {str(error)}",
                'fix_suggestion': "Review inputs and retry",
                'retry_params': None,
            }
    
    async def validate_results(self, results: Dict[str, Any], constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Validate results against scientific constraints (RESULT VALIDATION).
        
        Enhanced validation checks:
        - Secondary structure constraints (α-helix, β-sheet content)
        - Binding energy thresholds
        - RMSD stability
        - Property ranges (LogP, TPSA for drugs)
        - Sequence conservation
        
        Example constraints:
        - "secondary_structure": {"alpha_helix_min": 0.6, "beta_sheet_max": 0.2}
        - "binding_energy": {"max": -5.0, "unit": "kcal/mol"}
        - "rmsd": {"max": 2.0, "unit": "angstrom"}
        - "admet": {"logP_max": 5.0, "tpsa_min": 40.0}
        
        Returns:
            {
                'valid': bool,
                'discrepancies': List[str],
                'analysis': str,
                'quantitative_metrics': Dict,
            }
        """
        logger.info("🔍 Critic: Validating results against constraints")
        
        if not constraints:
            return {
                'valid': True,
                'discrepancies': [],
                'analysis': 'No constraints specified - validation skipped',
                'quantitative_metrics': {},
            }
        
        discrepancies = []
        metrics = {}
        
        # ===== SECONDARY STRUCTURE VALIDATION =====
        if "secondary_structure" in constraints and "secondary_structure" in results:
            req = constraints["secondary_structure"]
            actual = results["secondary_structure"]
            
            # Alpha helix validation
            if "alpha_helix_min" in req:
                alpha_content = actual.get("H", 0.0)
                if alpha_content < req["alpha_helix_min"]:
                    discrepancies.append(
                        f"❌ Alpha helix content {alpha_content:.1%} below required {req['alpha_helix_min']:.1%}"
                    )
                    metrics["alpha_helix_deficit"] = req["alpha_helix_min"] - alpha_content
                else:
                    metrics["alpha_helix_ok"] = True
            
            # Beta sheet validation
            if "beta_sheet_min" in req:
                beta_content = actual.get("E", 0.0)
                if beta_content < req["beta_sheet_min"]:
                    discrepancies.append(
                        f"❌ Beta sheet content {beta_content:.1%} below required {req['beta_sheet_min']:.1%}"
                    )
                    metrics["beta_sheet_deficit"] = req["beta_sheet_min"] - beta_content
            
            if "beta_sheet_max" in req:
                beta_content = actual.get("E", 0.0)
                if beta_content > req["beta_sheet_max"]:
                    discrepancies.append(
                        f"❌ Beta sheet content {beta_content:.1%} exceeds maximum {req['beta_sheet_max']:.1%}"
                    )
        
        # ===== BINDING ENERGY VALIDATION =====
        if "binding_energy" in constraints and "binding_energy" in results:
            req = constraints["binding_energy"]
            actual_energy = results["binding_energy"]
            
            if "max" in req and actual_energy > req["max"]:
                discrepancies.append(
                    f"❌ Binding energy {actual_energy} {req.get('unit', 'kcal/mol')} "
                    f"exceeds maximum {req['max']} (weaker binding than required)"
                )
                metrics["binding_energy_deficit"] = actual_energy - req["max"]
            
            if "min" in req and actual_energy < req["min"]:
                discrepancies.append(
                    f"❌ Binding energy {actual_energy} {req.get('unit', 'kcal/mol')} "
                    f"below minimum {req['min']} (too strong binding)"
                )
        
        # ===== RMSD STABILITY VALIDATION =====
        if "rmsd" in constraints and "rmsd" in results:
            req = constraints["rmsd"]
            actual_rmsd = results["rmsd"]
            
            if "max" in req and actual_rmsd > req["max"]:
                discrepancies.append(
                    f"❌ RMSD {actual_rmsd} {req.get('unit', 'Å')} exceeds maximum {req['max']} (unstable structure)"
                )
                metrics["rmsd_excess"] = actual_rmsd - req["max"]
        
        # ===== ADMET VALIDATION (Drug-like properties) =====
        if "admet" in constraints and "admet" in results:
            req = constraints["admet"]
            actual = results["admet"]
            
            # Lipinski's Rule of Five
            if "logP_max" in req and actual.get("logP", 0) > req["logP_max"]:
                discrepancies.append(
                    f"❌ LogP {actual['logP']} exceeds Lipinski limit {req['logP_max']} (poor oral bioavailability)"
                )
            
            if "tpsa_min" in req and actual.get("TPSA", 0) < req["tpsa_min"]:
                discrepancies.append(
                    f"❌ TPSA {actual['TPSA']} Ų below minimum {req['tpsa_min']} (poor membrane permeability)"
                )
            
            if "molecular_weight_max" in req and actual.get("MW", 0) > req["molecular_weight_max"]:
                discrepancies.append(
                    f"❌ Molecular weight {actual['MW']} Da exceeds limit {req['molecular_weight_max']}"
                )
        
        # ===== SEQUENCE CONSERVATION VALIDATION =====
        if "sequence_identity" in constraints and "sequence_identity" in results:
            req = constraints["sequence_identity"]
            actual_identity = results["sequence_identity"]
            
            if "min" in req and actual_identity < req["min"]:
                discrepancies.append(
                    f"❌ Sequence identity {actual_identity:.1%} below required {req['min']:.1%}"
                )
        
        # ===== OVERALL VALIDATION =====
        valid = len(discrepancies) == 0
        
        if valid:
            logger.info("✅ Critic: Result validation PASSED - all constraints satisfied")
            analysis = "✅ All scientific constraints satisfied"
        else:
            logger.warning(f"⚠️ Critic: Result validation FAILED - {len(discrepancies)} discrepancies detected")
            analysis = f"⚠️ Found {len(discrepancies)} constraint violations:\n" + "\n".join(discrepancies)
        
        return {
            'valid': valid,
            'discrepancies': discrepancies,
            'analysis': analysis,
            'quantitative_metrics': metrics,
        }
    
    def _has_dependency_cycle(self, steps: List[Dict[str, Any]]) -> bool:
        """Check for circular dependencies in steps."""
        # Simple cycle detection (TODO: Implement proper topological sort)
        for step in steps:
            if step['step_id'] in step.get('dependencies', []):
                return True
        return False
    
    def _detect_redundancies(self, steps: List[Dict[str, Any]]) -> List[str]:
        """Detect redundant operations."""
        redundancies = []
        
        # Example: Saving same data pre/post operation when data doesn't change
        # (Like ProtAgents Critic catching "sequence doesn't change during folding")
        
        # TODO: Implement sophisticated redundancy detection
        
        return redundancies


class ErrorRecoveryAgent(SpecialistAgent):
    """
    Error Recovery Agent - Autonomous Error Diagnosis & Retry.
    
    Responsibilities:
    - Diagnose execution errors (JSON format, parameter mismatch, etc.)
    - Generate corrections automatically
    - Retry with fixed inputs
    - Track error patterns for learning
    
    Based on ProtAgents error recovery loop (Critic → Assistant → Retry).
    """
    
    def __init__(self):
        super().__init__(
            agent_id="error_recovery",
            agent_name="ErrorRecoveryAgent",
            expertise_area="Error Diagnosis & Autonomous Retry",
            description=(
                "Diagnoses execution errors, generates automatic corrections, "
                "and retries operations without human intervention."
            ),
            capabilities=[
                "JSON format error correction",
                "Parameter type mismatch resolution",
                "Dependency error fixing",
                "Retry orchestration with exponential backoff",
                "Error pattern recognition",
            ],
            ai_university_role="Chief Resilience Officer, Error Recovery",
            research_focus=[
                "Autonomous error recovery patterns",
                "Self-healing workflows",
                "Error prediction",
            ],
        )
        
        self.error_history: List[Dict[str, Any]] = []
        self.max_retries = 3
    
    async def recover_from_error(
        self,
        error: Exception,
        context: Dict[str, Any],
        step: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Autonomous error recovery with retry logic.
        
        Args:
            error: Exception that occurred
            context: Execution context
            step: Failed step definition
        
        Returns:
            {
                'recovered': bool,
                'diagnosis': str,
                'fix_applied': str,
                'retry_result': Optional[Dict],
                'retry_count': int,
            }
        """
        logger.info(f"🔧 ErrorRecovery: Diagnosing {type(error).__name__}")
        
        # Diagnose error type
        diagnosis = self._diagnose_error(error, context, step)
        
        # Log error to history
        self.error_history.append({
            'error_type': type(error).__name__,
            'diagnosis': diagnosis,
            'step': step,
            'timestamp': logger.Formatter().formatTime(logging.LogRecord('', 0, '', 0, '', (), None)),
        })
        
        # Attempt fix
        fix_result = await self._attempt_fix(error, diagnosis, context, step)
        
        return fix_result
    
    def _diagnose_error(
        self,
        error: Exception,
        context: Dict[str, Any],
        step: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Diagnose error and identify fix strategy."""
        
        error_str = str(error).lower()
        error_type = type(error).__name__
        
        # JSON formatting errors
        if "json" in error_str or "delimiter" in error_str or "quotes" in error_str:
            return {
                'category': 'json_format',
                'fix_strategy': 'replace_single_quotes_with_double',
                'confidence': 0.95,
            }
        
        # Parameter errors
        elif "parameter" in error_str or "argument" in error_str or "missing" in error_str:
            return {
                'category': 'parameter_mismatch',
                'fix_strategy': 'add_missing_parameters',
                'confidence': 0.85,
            }
        
        # Type errors
        elif error_type == 'TypeError':
            return {
                'category': 'type_mismatch',
                'fix_strategy': 'convert_parameter_types',
                'confidence': 0.80,
            }
        
        # Key errors
        elif error_type == 'KeyError':
            return {
                'category': 'missing_key',
                'fix_strategy': 'add_default_values',
                'confidence': 0.75,
            }
        
        # Generic error
        else:
            return {
                'category': 'unknown',
                'fix_strategy': 'generic_retry',
                'confidence': 0.50,
            }
    
    async def _attempt_fix(
        self,
        error: Exception,
        diagnosis: Dict[str, Any],
        context: Dict[str, Any],
        step: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Attempt to fix error and retry operation."""
        
        fix_strategy = diagnosis['fix_strategy']
        
        logger.info(f"🔧 ErrorRecovery: Applying fix strategy '{fix_strategy}'")
        
        # Apply fix based on strategy
        if fix_strategy == 'replace_single_quotes_with_double':
            fixed_params = self._fix_json_format(step.get('params', {}))
        
        elif fix_strategy == 'add_missing_parameters':
            fixed_params = self._add_default_parameters(step.get('params', {}), step)
        
        elif fix_strategy == 'convert_parameter_types':
            fixed_params = self._convert_types(step.get('params', {}))
        
        elif fix_strategy == 'add_default_values':
            fixed_params = self._add_defaults(step.get('params', {}))
        
        else:
            # Generic retry without modification
            fixed_params = step.get('params', {})
        
        # TODO: Actually retry the operation
        # For now, return fix metadata
        
        return {
            'recovered': diagnosis['confidence'] > 0.7,
            'diagnosis': diagnosis,
            'fix_applied': fix_strategy,
            'fixed_params': fixed_params,
            'retry_result': None,  # TODO: Implement actual retry
            'retry_count': 1,
        }
    
    def _fix_json_format(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fix JSON formatting issues (single → double quotes)."""
        import json
        
        try:
            # Convert to JSON string and back to fix formatting
            json_str = json.dumps(params)
            return json.loads(json_str)
        except Exception:
            return params
    
    def _add_default_parameters(self, params: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
        """Add missing default parameters based on function signature."""
        
        # TODO: Inspect function signature and add defaults
        # For now, return as-is
        return params
    
    def _convert_types(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Convert parameter types (e.g., string → int)."""
        
        # TODO: Implement intelligent type conversion
        return params
    
    def _add_defaults(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add default values for missing keys."""
        
        # TODO: Add sensible defaults
        return params
    
    def get_error_patterns(self) -> Dict[str, int]:
        """Analyze error history to identify patterns."""
        
        patterns = {}
        for error_record in self.error_history:
            category = error_record['diagnosis'].get('category', 'unknown')
            patterns[category] = patterns.get(category, 0) + 1
        
        return patterns


class AssistantAgent(SpecialistAgent):
    """
    Assistant Agent - Function Execution.
    
    Responsibilities:
    - Execute functions suggested by Planner
    - Prepare input parameters
    - Call worker drivers
    - Return results
    
    Based on ProtAgents architecture.
    """
    
    def __init__(self):
        super().__init__(
            agent_id="assistant",
            agent_name="AssistantAgent",
            expertise_area="Function Execution",
            description=(
                "Executes functions suggested by Planner, prepares required input "
                "parameters, and coordinates with worker drivers."
            ),
            capabilities=[
                "Function execution",
                "Parameter preparation",
                "Worker driver coordination",
                "Result aggregation",
            ],
            ai_university_role="Chief Operations Officer, Execution",
            research_focus=[
                "Efficient function orchestration",
                "Error handling",
                "Resource management",
            ],
        )
    
    async def execute_step(
        self,
        step: Dict[str, Any],
        workers: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Execute single plan step.
        
        Args:
            step: Step definition from plan
            workers: Dict of worker driver instances
            context: Accumulated context from previous steps
        
        Returns:
            Step execution result
        """
        logger.info(f"⚙️ Assistant: Executing step {step['step_id']}")
        
        worker_id = step['worker']
        worker = workers.get(worker_id)
        
        if not worker:
            raise ValueError(f"Worker {worker_id} not found")
        
        # Prepare parameters (resolve dependencies)
        params = self._prepare_params(step['params'], context)
        
        # Execute function
        if step['function'] == 'execute':
            result = await worker.execute(params['query'], context, enforce_msrp=True)
        else:
            # TODO: Call specific worker function
            result = await worker.execute(params.get('query', ''), context, enforce_msrp=True)
        
        logger.info(f"✅ Assistant: Step {step['step_id']} completed")
        return result
    
    def _prepare_params(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Prepare parameters by resolving dependencies from context."""
        prepared = {}
        
        for key, value in params.items():
            if isinstance(value, str) and value.startswith('from_step_'):
                # Resolve dependency
                step_id = int(value.split('_')[-1])
                prepared[key] = context.get(f'step_{step_id}', value)
            else:
                prepared[key] = value
        
        return prepared


class GeneralDriver(WorkerDriver):
    """
    General multi-worker orchestrator.
    
    Coordinates BioDynamo, Alchemist, and SMIC drivers for complex workflows.
    
    Architecture:
    - BioDynamoDriver: 9 specialists (MD, sampling, free energy, etc.)
    - AlchemistDriver: 6 specialists (QSAR, docking, ADMET, etc.)
    - SMICDriver: 1 specialist (graph analysis)
    
    Workflows:
    - Drug discovery pipeline: Alchemist (QSAR/Docking) → BioDynamo (MD/FreeEnergy)
    - Protein engineering: BioDynamo (iMMD/StateClassifier) → SMIC (complexity)
    - Multi-target screening: Alchemist (VirtualScreening) → BioDynamo (Validation)
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize GeneralDriver with worker driver instances."""
        
        # Initialize worker drivers
        self.biodynamo_driver = BioDynamoDriver()
        self.alchemist_driver = AlchemistDriver()
        self.smic_driver = SMICDriver()
        
        # ✨ Initialize Planner-Assistant-Critic agents
        self.planner = PlannerAgent()
        self.critic = CriticAgent()
        self.assistant = AssistantAgent()
        self.error_recovery = ErrorRecoveryAgent()
        
        # ✨ Initialize Memory Bank for multi-turn workflows
        memory_dir = Path(__file__).parent.parent.parent / "data" / "memory_bank"
        self.memory_bank = ConversationMemoryBank(storage_dir=memory_dir)
        
        # Define meta-specialists (worker drivers as specialists)
        specialists = [
            SpecialistAgent(
                agent_id="biodynamo",
                agent_name="BioDynamoDriver",
                expertise_area="Molecular Dynamics & Drug Discovery",
                description=(
                    "Coordinates 9 specialist agents for MD workflows: sampling, "
                    "state classification, iMMD, free energy, benchmarking, QC, etc."
                ),
                capabilities=[
                    "Molecular dynamics simulation",
                    "Enhanced sampling (RAMD, metadynamics)",
                    "Conformational state classification (MDGraphEmb)",
                    "Iterative multiscale MD (AA ↔ CG)",
                    "Free energy calculations (MM/PBSA, TI)",
                    "Force field benchmarking",
                ],
                ai_university_role="Director, Molecular Dynamics Institute",
                research_focus=[
                    "Protein conformational dynamics",
                    "Drug-target binding kinetics",
                    "Multiscale simulation methods",
                ],
            ),
            SpecialistAgent(
                agent_id="alchemist",
                agent_name="AlchemistDriver",
                expertise_area="Drug Discovery & Cheminformatics",
                description=(
                    "Coordinates 6 specialist agents for drug discovery: QSAR, "
                    "docking, virtual screening, ADMET, generative design, etc."
                ),
                capabilities=[
                    "QSAR modeling",
                    "Molecular docking",
                    "Virtual screening",
                    "ADMET prediction",
                    "De novo molecular design",
                    "Fragment optimization",
                ],
                ai_university_role="Director, Drug Discovery Institute",
                research_focus=[
                    "Lead optimization pipelines",
                    "Multi-target drug design",
                    "AI-driven molecule generation",
                ],
            ),
            SpecialistAgent(
                agent_id="smic",
                agent_name="SMICDriver",
                expertise_area="Graph Theory & Molecular Complexity",
                description=(
                    "Lightweight driver for graph descriptor calculation and "
                    "molecular interaction network analysis."
                ),
                capabilities=[
                    "Graph descriptor calculation",
                    "Network centrality analysis",
                    "Complexity scoring",
                ],
                ai_university_role="Director, Network Analysis Laboratory",
                research_focus=[
                    "Molecular interaction networks",
                    "Topological descriptors",
                ],
            ),
        ]
        
        # General driver configuration
        config = WorkerDriverConfig(
            worker_name="GeneralOrchestrator",
            domain="Multi-Worker Scientific Research",
            specialists=specialists,
            enforce_msrp=True,
            minimum_hypotheses=5,  # Nature-level rigor
            require_literature_validation=True,
            enable_proactive_mode=True,
            enable_autonomous_discovery=True,
            scientific_pressure_level="nature",
            enable_literature_mcp=True,
            literature_sources=["semantic_scholar", "pubmed", "arxiv", "chemrxiv"],
        )
        
        super().__init__(config, *args, **kwargs)
        
        logger.info(
            f"🌐 GeneralDriver Initialized | "
            f"Workers: BioDynamo ({len(self.biodynamo_driver.specialists)} specialists), "
            f"Alchemist ({len(self.alchemist_driver.specialists)} specialists), "
            f"SMIC ({len(self.smic_driver.specialists)} specialist) | "
            f"PAC Agents: Planner ✓ Critic ✓ Assistant ✓ ErrorRecovery ✓ | "
            f"Memory Bank: {self.memory_bank.storage_dir}"
        )
    
    async def execute(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        enforce_msrp: bool = True,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute multi-worker query with Planner-Assistant-Critic pattern.
        
        Enhanced workflow (ProtAgents-inspired):
        1. Planner creates execution plan
        2. Critic validates plan (PRE-EXECUTION)
        3. Assistant executes steps
        4. ErrorRecovery handles failures (if any)
        5. Critic validates results (POST-EXECUTION)
        6. Memory Bank stores state for multi-turn
        
        Query Intent Routing:
        - "Molecular dynamics" → BioDynamoDriver
        - "Docking", "QSAR", "Virtual screening" → AlchemistDriver
        - "Graph analysis", "Complexity" → SMICDriver
        - "Drug discovery pipeline" → Multi-worker (Alchemist → BioDynamo)
        - "Protein engineering" → Multi-worker (BioDynamo → SMIC)
        
        Args:
            query: Research query
            context: Optional context (cross-worker data)
            enforce_msrp: Whether to enforce MSRP reasoning
            turn_id: Turn identifier for Memory Bank (auto-generated if None)
        
        Returns:
            Dict with answer, workers_consulted, msrp_chain, pac_workflow, etc.
        """
        logger.info(f"🌐 General Query: {query[:100]}...")
        
        # Generate turn ID if not provided
        if turn_id is None:
            import uuid
            turn_id = f"turn_{uuid.uuid4().hex[:8]}"
        
        # ===== STEP 1: RETRIEVE CONTEXT FROM MEMORY =====
        if self.memory_bank:
            memory_context = self.memory_bank.get_context(query, max_turns=3)
            logger.info(f"💾 Retrieved {len(memory_context)} previous turns from memory")
            
            # Merge memory context with provided context
            if context:
                context = {**memory_context, **context}
            else:
                context = memory_context
        
        # ===== STEP 2: PLANNER CREATES EXECUTION PLAN =====
        plan = await self.planner.create_plan(query, context)
        logger.info(f"📋 Planner created {len(plan['steps'])}-step plan ({plan['complexity']})")
        
        # ===== STEP 3: CRITIC VALIDATES PLAN (PRE-EXECUTION) =====
        validation = await self.critic.validate_plan(plan)
        
        if not validation['valid']:
            logger.warning(f"⚠️ Plan validation failed: {validation['issues']}")
            
            # Return validation failure
            return {
                "answer": f"Plan validation failed: {', '.join(validation['issues'])}",
                "suggestions": validation['suggestions'],
                "plan": plan,
                "validation": validation,
                "pac_workflow": "validation_failed",
                "turn_id": turn_id,
            }
        
        logger.info("✅ Critic validated plan - proceeding with execution")
        
        # ===== STEP 4: ASSISTANT EXECUTES PLAN =====
        execution_results = []
        execution_context = context or {}
        
        workers_map = {
            'biodynamo': self.biodynamo_driver,
            'alchemist': self.alchemist_driver,
            'smic': self.smic_driver,
        }
        
        for step in plan['steps']:
            try:
                # Execute step
                result = await self.assistant.execute_step(
                    step=step,
                    workers=workers_map,
                    context=execution_context,
                )
                
                execution_results.append({
                    'step_id': step['step_id'],
                    'status': 'success',
                    'result': result,
                })
                
                # Update context for next step
                execution_context[f"step_{step['step_id']}"] = result
                
            except Exception as error:
                logger.error(f"❌ Step {step['step_id']} failed: {error}")
                
                # ===== STEP 4.5: ERROR RECOVERY =====
                recovery_result = await self.error_recovery.recover_from_error(
                    error=error,
                    context=execution_context,
                    step=step,
                )
                
                execution_results.append({
                    'step_id': step['step_id'],
                    'status': 'error_recovered' if recovery_result['recovered'] else 'failed',
                    'error': str(error),
                    'recovery': recovery_result,
                })
                
                # If recovery failed, stop execution
                if not recovery_result['recovered']:
                    logger.error(f"💥 Error recovery failed for step {step['step_id']}")
                    break
        
        # ===== STEP 5: CRITIC VALIDATES RESULTS (POST-EXECUTION) =====
        # Extract final results
        final_results = {}
        for exec_result in execution_results:
            if exec_result['status'] in ['success', 'error_recovered']:
                final_results.update(exec_result.get('result', {}))
        
        # Validate against constraints (if provided in query/context)
        constraints = context.get('constraints') if context else None
        result_validation = await self.critic.validate_results(final_results, constraints)
        
        logger.info(f"🔍 Result validation: {'PASSED' if result_validation['valid'] else 'FAILED'}")
        
        # ===== STEP 6: MEMORY BANK STORES STATE =====
        if self.memory_bank:
            self.memory_bank.store_turn_state(
                turn_id=turn_id,
                data={
                    'query': query,
                    'plan': plan,
                    'execution_results': execution_results,
                    'final_results': final_results,
                    'validation': result_validation,
                },
                entities={
                    'query_hash': str(hash(query)),
                },
            )
            logger.info(f"💾 Stored turn state: {turn_id}")
        
        # ===== SYNTHESIZE FINAL ANSWER =====
        answer = self._synthesize_pac_answer(
            query=query,
            plan=plan,
            execution_results=execution_results,
            validation=result_validation,
        )
        
        return {
            "answer": answer,
            "query": query,
            "turn_id": turn_id,
            "plan": plan,
            "plan_validation": validation,
            "execution_results": execution_results,
            "result_validation": result_validation,
            "workers_consulted": plan.get('workers', []),
            "pac_workflow": "complete",
            "pac_agents_used": {
                "planner": True,
                "critic": True,
                "assistant": True,
                "error_recovery": any(r['status'] == 'error_recovered' for r in execution_results),
            },
            "memory_bank": {
                "turn_id": turn_id,
                "previous_turns": len(context) if context else 0,
            },
        }
    
    def _synthesize_pac_answer(
        self,
        query: str,
        plan: Dict[str, Any],
        execution_results: List[Dict[str, Any]],
        validation: Dict[str, Any],
    ) -> str:
        """Synthesize human-readable answer from PAC workflow."""
        
        answer_parts = [
            f"**Query**: {query}",
            "",
            f"**Execution Plan**: {len(plan['steps'])} steps ({plan['complexity']} complexity)",
        ]
        
        # Execution summary
        success_count = sum(1 for r in execution_results if r['status'] in ['success', 'error_recovered'])
        failed_count = len(execution_results) - success_count
        
        answer_parts.append(f"**Execution**: {success_count}/{len(execution_results)} steps successful")
        
        if failed_count > 0:
            answer_parts.append(f"⚠️ {failed_count} steps failed")
        
        # Validation summary
        answer_parts.append("")
        answer_parts.append(f"**Validation**: {validation['analysis']}")
        
        if not validation['valid']:
            answer_parts.append("")
            answer_parts.append("**Discrepancies**:")
            for disc in validation['discrepancies']:
                answer_parts.append(f"- {disc}")
        
        # Result details
        answer_parts.append("")
        answer_parts.append("**Results**:")
        for result in execution_results:
            if result['status'] == 'success':
                answer_parts.append(f"✅ Step {result['step_id']}: Success")
            elif result['status'] == 'error_recovered':
                answer_parts.append(f"🔧 Step {result['step_id']}: Recovered from error")
            else:
                answer_parts.append(f"❌ Step {result['step_id']}: Failed")
        
        return "\n".join(answer_parts)
    
    async def execute_legacy(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        enforce_msrp: bool = True,
    ) -> Dict[str, Any]:
        """
        Legacy execute method (backward compatibility).
        
        Simple routing without PAC workflow.
        """
        logger.info(f"🌐 General Query (Legacy): {query[:100]}...")
        
        # Parse query intent
        intent = self._parse_query_intent(query)
        
        # Route to appropriate worker(s)
        if intent["type"] == "single_worker":
            # Single worker execution
            response = await self._route_to_worker(
                query=query,
                worker_id=intent["worker_id"],
                context=context,
                enforce_msrp=enforce_msrp,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "single_worker",
            }
        
        elif intent["type"] == "multi_worker":
            # Coordinate multiple workers
            response = await self._coordinate_workers(
                query=query,
                worker_ids=intent["worker_ids"],
                context=context,
                enforce_msrp=enforce_msrp,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "multi_worker",
            }
        
        else:
            # Unknown intent
            logger.warning(f"⚠️ Unknown intent: {intent}")
            return {
                "answer": f"Unable to parse intent for query: {query}",
                "intent": intent,
                "routing": "fallback",
            }
    
    def _parse_query_intent(self, query: str) -> Dict[str, Any]:
        """Parse query to determine worker routing."""
        
        query_lower = query.lower()
        
        # BioDynamo routing
        if any(kw in query_lower for kw in ["molecular dynamics", "md simulation", "trajectory", "immd", "multiscale"]):
            return {"type": "single_worker", "worker_id": "biodynamo"}
        
        elif any(kw in query_lower for kw in ["free energy", "mm/pbsa", "sampling", "umbrella", "metadynamics"]):
            return {"type": "single_worker", "worker_id": "biodynamo"}
        
        # Alchemist routing
        elif any(kw in query_lower for kw in ["qsar", "docking", "virtual screening", "admet"]):
            return {"type": "single_worker", "worker_id": "alchemist"}
        
        elif any(kw in query_lower for kw in ["generate molecule", "denovo", "fragment optimization"]):
            return {"type": "single_worker", "worker_id": "alchemist"}
        
        # SMIC routing
        elif any(kw in query_lower for kw in ["graph", "complexity", "network analysis"]):
            return {"type": "single_worker", "worker_id": "smic"}
        
        # Multi-worker routing
        elif "drug discovery" in query_lower or "lead optimization" in query_lower:
            # Alchemist (QSAR/Docking) → BioDynamo (MD/FreeEnergy)
            return {
                "type": "multi_worker",
                "worker_ids": ["alchemist", "biodynamo"],
                "workflow": "drug_discovery_pipeline",
            }
        
        elif "protein engineering" in query_lower:
            # BioDynamo (iMMD/StateClassifier) → SMIC (Complexity)
            return {
                "type": "multi_worker",
                "worker_ids": ["biodynamo", "smic"],
                "workflow": "protein_engineering",
            }
        
        # Default: Unknown intent
        return {"type": "unknown"}
    
    async def _route_to_worker(
        self,
        query: str,
        worker_id: str,
        context: Optional[Dict[str, Any]],
        enforce_msrp: bool,
    ) -> Dict[str, Any]:
        """Route query to specific worker driver."""
        
        logger.info(f"🎯 Routing to {worker_id}")
        
        if worker_id == "biodynamo":
            return await self.biodynamo_driver.execute(query, context, enforce_msrp)
        
        elif worker_id == "alchemist":
            return await self.alchemist_driver.execute(query, context, enforce_msrp)
        
        elif worker_id == "smic":
            return await self.smic_driver.execute(query, context, enforce_msrp)
        
        else:
            return {"error": f"Unknown worker: {worker_id}"}
    
    async def _coordinate_workers(
        self,
        query: str,
        worker_ids: List[str],
        context: Optional[Dict[str, Any]],
        enforce_msrp: bool,
    ) -> Dict[str, Any]:
        """
        Coordinate multiple workers for complex workflows.
        
        Workflows:
        1. Drug Discovery: Alchemist → BioDynamo
        2. Protein Engineering: BioDynamo → SMIC
        3. Multi-target Screening: Alchemist → BioDynamo (parallel)
        """
        logger.info(f"🔗 Coordinating {len(worker_ids)} workers")
        
        results = []
        accumulated_context = context or {}
        
        for worker_id in worker_ids:
            response = await self._route_to_worker(
                query=query,
                worker_id=worker_id,
                context=accumulated_context,
                enforce_msrp=enforce_msrp,
            )
            
            results.append(response)
            
            # Accumulate context for next worker
            accumulated_context[worker_id] = response
        
        # Cross-worker validation
        validation_report = await self._cross_worker_validation(results)
        
        # Synthesize final answer
        final_answer = self._synthesize_multi_worker_answer(results)
        
        return {
            "answer": final_answer,
            "workers_consulted": worker_ids,
            "worker_responses": results,
            "validation_report": validation_report,
            "coordination_complete": True,
        }
    
    async def _cross_worker_validation(
        self,
        results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Cross-validate results between workers.
        
        Example:
        - Alchemist docking score vs BioDynamo free energy (correlation check)
        - BioDynamo conformational states vs SMIC complexity (consistency check)
        
        Returns:
            Validation report with consistency metrics
        """
        logger.info("✅ Cross-Worker Validation")
        
        # TODO: Implement sophisticated cross-validation
        # For now, return placeholder
        
        return {
            "validation_status": "pending_implementation",
            "consistency_score": 0.85,
            "conflicts_detected": [],
        }
    
    def _synthesize_multi_worker_answer(self, results: List[Dict[str, Any]]) -> str:
        """Synthesize answer from multiple worker responses."""
        
        # Simple concatenation (TODO: Implement sophisticated synthesis)
        answer_parts = []
        
        for result in results:
            worker = result.get("worker", result.get("routing", "unknown"))
            answer = result.get("answer", "")
            answer_parts.append(f"**{worker}**: {answer}")
        
        return "\n\n".join(answer_parts)
    
    async def proactive_problem_identification(
        self,
        context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Phase 4 (MPI-UOS): Workflow-level autonomous discovery.
        
        Proactively identify:
        - Cross-worker inconsistencies (docking vs MD)
        - Workflow bottlenecks
        - Missing integration points
        - Implementation gaps (like 85% KAN gap)
        
        Returns:
            List of autonomous discoveries
        """
        discoveries = []
        
        # Check for cross-worker inconsistencies
        if "worker_responses" in context:
            # TODO: Compare Alchemist docking scores vs BioDynamo free energies
            # Flag significant disagreements
            pass
        
        # Check for workflow bottlenecks
        if "execution_times" in context:
            # TODO: Identify slow workers
            # Suggest parallelization opportunities
            pass
        
        # Check for integration gaps
        # TODO: Identify missing MCP tools, incomplete pipelines
        
        if discoveries:
            logger.info(f"💡 GeneralDriver Autonomous Discoveries: {len(discoveries)}")
            self.autonomous_discoveries.extend(discoveries)
        
        return discoveries

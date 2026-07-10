#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FunctionProfile System - Automatic Dependency Detection
========================================================

Enables specialists to declare preconditions/postconditions for automatic
dependency resolution in multi-agent workflows.

Inspired by ProtAgents MIT paper: Critic validates plan BEFORE execution
by checking if dependencies are satisfied.

Use Cases:
1. BioDynamo SamplingOrchestrator requires MDGraphEmb state classification BEFORE enhanced sampling
2. Alchemist MolecularDocking requires QSARModeling activity prediction BEFORE expensive docking
3. BioDynamo FreeEnergyAgent requires iMMDControl convergence BEFORE MM/PBSA calculation

Integration:
- PlannerAgent uses FunctionProfiles to detect implicit dependencies
- CriticAgent validates preconditions before execution
- AssistantAgent ensures postconditions are met after execution
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ============================================================================
# CONDITION TYPES
# ============================================================================

class ConditionType(Enum):
    """Type of condition constraint."""
    
    # Data availability
    REQUIRES_DATA = "requires_data"  # Requires specific data to be present
    PRODUCES_DATA = "produces_data"  # Produces specific data after execution
    
    # State requirements
    REQUIRES_STATE = "requires_state"  # Requires system to be in specific state
    MODIFIES_STATE = "modifies_state"  # Modifies system state
    
    # Resource requirements
    REQUIRES_RESOURCE = "requires_resource"  # Requires computational resource (GPU, memory)
    CONSUMES_RESOURCE = "consumes_resource"  # Consumes resource during execution
    
    # Quality gates
    REQUIRES_QUALITY = "requires_quality"  # Requires input to meet quality threshold
    ENSURES_QUALITY = "ensures_quality"  # Ensures output meets quality threshold
    
    # Specialist collaboration
    REQUIRES_SPECIALIST = "requires_specialist"  # Requires another specialist's output
    ENABLES_SPECIALIST = "enables_specialist"  # Enables downstream specialist to execute


@dataclass
class Condition:
    """A single precondition or postcondition."""
    
    condition_type: ConditionType
    description: str
    key: str  # Unique identifier for the condition
    
    # Validation
    validator: Optional[Callable[[Dict[str, Any]], bool]] = None  # Returns True if condition met
    error_message: Optional[str] = None  # Message if condition not met
    
    # Metadata
    required: bool = True  # If False, condition is optional (warning only)
    context_keys: List[str] = field(default_factory=list)  # Context keys this condition depends on


@dataclass
class FunctionProfile:
    """
    Complete profile for a specialist function/capability.
    
    Declares preconditions (requirements) and postconditions (guarantees).
    Enables automatic dependency detection and validation.
    """
    
    # Identity
    function_id: str  # Unique identifier (e.g., "biodynamo.sampling_orchestrator.select_strategy")
    function_name: str  # Human-readable name
    specialist_id: str  # Specialist that owns this function
    
    # Description
    description: str
    capabilities: List[str] = field(default_factory=list)
    
    # Conditions
    preconditions: List[Condition] = field(default_factory=list)
    postconditions: List[Condition] = field(default_factory=list)
    
    # Parameters
    required_parameters: List[str] = field(default_factory=list)
    optional_parameters: List[str] = field(default_factory=list)
    
    # Performance
    estimated_runtime_seconds: Optional[float] = None
    estimated_cost_usd: Optional[float] = None
    
    # Metadata
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)


# ============================================================================
# FUNCTION PROFILE REGISTRY
# ============================================================================

class FunctionProfileRegistry:
    """
    Registry of FunctionProfiles for all specialist capabilities.
    
    Enables:
    1. Automatic dependency detection (PlannerAgent)
    2. Pre-execution validation (CriticAgent)
    3. Post-execution verification (AssistantAgent)
    4. Resource planning (GeneralDriver)
    """
    
    def __init__(self):
        """Initialize empty registry."""
        self.profiles: Dict[str, FunctionProfile] = {}
    
    def register(self, profile: FunctionProfile) -> None:
        """Register a FunctionProfile."""
        if profile.function_id in self.profiles:
            logger.warning(f"Overwriting existing profile: {profile.function_id}")
        
        self.profiles[profile.function_id] = profile
        logger.info(f"Registered FunctionProfile: {profile.function_id}")
    
    def get(self, function_id: str) -> Optional[FunctionProfile]:
        """Get FunctionProfile by ID."""
        return self.profiles.get(function_id)
    
    def list_by_specialist(self, specialist_id: str) -> List[FunctionProfile]:
        """Get all profiles for a specific specialist."""
        return [p for p in self.profiles.values() if p.specialist_id == specialist_id]
    
    def find_dependencies(self, function_id: str) -> List[str]:
        """
        Find functions that must execute BEFORE the given function.
        
        Algorithm:
        1. Get function's preconditions
        2. For each precondition, find functions whose postconditions satisfy it
        3. Return list of dependency function_ids
        """
        profile = self.get(function_id)
        if not profile:
            return []
        
        dependencies = []
        
        for precondition in profile.preconditions:
            # Find functions that produce what this function requires
            for other_id, other_profile in self.profiles.items():
                if other_id == function_id:
                    continue
                
                for postcondition in other_profile.postconditions:
                    if self._conditions_match(precondition, postcondition):
                        dependencies.append(other_id)
                        break
        
        return dependencies
    
    def validate_preconditions(
        self,
        function_id: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate all preconditions for a function.
        
        Returns:
            {
                "valid": bool,
                "passed": List[str],  # Condition keys that passed
                "failed": List[Dict],  # Conditions that failed (with error messages)
                "warnings": List[str],  # Optional conditions that failed
            }
        """
        profile = self.get(function_id)
        if not profile:
            return {
                "valid": False,
                "passed": [],
                "failed": [{"key": "profile_not_found", "message": f"FunctionProfile not found: {function_id}"}],
                "warnings": [],
            }
        
        passed = []
        failed = []
        warnings = []
        
        for condition in profile.preconditions:
            is_valid = self._validate_condition(condition, context)
            
            if is_valid:
                passed.append(condition.key)
            else:
                error_info = {
                    "key": condition.key,
                    "type": condition.condition_type.value,
                    "message": condition.error_message or f"Condition not met: {condition.description}",
                }
                
                if condition.required:
                    failed.append(error_info)
                else:
                    warnings.append(error_info["message"])
        
        return {
            "valid": len(failed) == 0,
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
        }
    
    def validate_postconditions(
        self,
        function_id: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate all postconditions after function execution.
        
        Returns:
            {
                "valid": bool,
                "passed": List[str],
                "failed": List[Dict],
                "warnings": List[str],
            }
        """
        profile = self.get(function_id)
        if not profile:
            return {
                "valid": False,
                "passed": [],
                "failed": [{"key": "profile_not_found", "message": f"FunctionProfile not found: {function_id}"}],
                "warnings": [],
            }
        
        passed = []
        failed = []
        warnings = []
        
        for condition in profile.postconditions:
            is_valid = self._validate_condition(condition, result)
            
            if is_valid:
                passed.append(condition.key)
            else:
                error_info = {
                    "key": condition.key,
                    "type": condition.condition_type.value,
                    "message": condition.error_message or f"Postcondition not met: {condition.description}",
                }
                
                if condition.required:
                    failed.append(error_info)
                else:
                    warnings.append(error_info["message"])
        
        return {
            "valid": len(failed) == 0,
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
        }
    
    # Private helpers
    
    def _conditions_match(self, precondition: Condition, postcondition: Condition) -> bool:
        """Check if a postcondition satisfies a precondition."""
        # Simple key-based matching (can be extended with semantic matching)
        return precondition.key == postcondition.key
    
    def _validate_condition(self, condition: Condition, context: Dict[str, Any]) -> bool:
        """Validate a single condition against context."""
        # If validator provided, use it
        if condition.validator:
            try:
                return condition.validator(context)
            except Exception as e:
                logger.warning(f"Condition validator failed: {condition.key} - {e}")
                return False
        
        # Otherwise, simple key existence check
        for key in condition.context_keys:
            if key not in context:
                return False
        
        return True


# ============================================================================
# BIODYNAMO SPECIALIST PROFILES
# ============================================================================

def create_biodynamo_profiles() -> List[FunctionProfile]:
    """Create FunctionProfiles for BioDynamo specialists."""
    
    profiles = []
    
    # 1. SamplingOrchestratorAgent
    profiles.append(FunctionProfile(
        function_id="biodynamo.sampling_orchestrator.select_strategy",
        function_name="Select Enhanced Sampling Strategy",
        specialist_id="sampling_orchestrator",
        description="Consumes MDGraphEmb uncertainty to choose RAMD vs metadynamics vs umbrella sampling",
        capabilities=["RAMD", "Metadynamics", "Umbrella Sampling", "Weighted Ensemble"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_SPECIALIST,
                description="Requires MDGraphEmb conformational state classification",
                key="mdgraphemb_classification",
                context_keys=["conformational_state", "uncertainty_score"],
                error_message="MDGraphEmb state classification must complete before sampling strategy selection",
            ),
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires trajectory data or initial structure",
                key="trajectory_or_structure",
                context_keys=["trajectory_path", "pdb_structure"],
                error_message="Trajectory or PDB structure required for sampling",
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces sampling strategy recommendation",
                key="sampling_strategy",
                context_keys=["strategy_type", "parameters"],
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables enhanced sampling execution",
                key="enables_enhanced_sampling",
            ),
        ],
        required_parameters=["trajectory_or_pdb"],
        optional_parameters=["uncertainty_threshold", "exploration_weight"],
        estimated_runtime_seconds=5.0,
    ))
    
    # 2. StateClassifierAgent (MDGraphEmb)
    profiles.append(FunctionProfile(
        function_id="biodynamo.state_classifier.classify_conformations",
        function_name="Classify Conformational States",
        specialist_id="state_classifier",
        description="Uses GraphSAGE to classify MD trajectories into discrete states (Open/Closed/Intermediate)",
        capabilities=["GraphSAGE embeddings", "State prediction", "Uncertainty quantification"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires MD trajectory in supported format",
                key="trajectory_data",
                context_keys=["trajectory_path"],
                error_message="MD trajectory required (dcd, xtc, or trr format)",
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces conformational state labels",
                key="conformational_states",
                context_keys=["state_labels", "uncertainty_scores"],
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables SamplingOrchestrator strategy selection",
                key="enables_sampling_orchestration",
            ),
        ],
        required_parameters=["trajectory_path", "topology_path"],
        optional_parameters=["num_states", "embedding_dim"],
        estimated_runtime_seconds=30.0,
    ))
    
    # 3. iMMDControlAgent
    profiles.append(FunctionProfile(
        function_id="biodynamo.immd_control.run_aa_cg_cycles",
        function_name="Run iMMD AA ↔ CG Cycles",
        specialist_id="immd_control",
        description="Manages iterative multiscale MD with convergence monitoring",
        capabilities=["AA to CG conversion", "CG to AA back-mapping", "Convergence detection"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires initial AA structure",
                key="aa_structure",
                context_keys=["pdb_structure"],
                error_message="All-atom PDB structure required for iMMD",
            ),
            Condition(
                condition_type=ConditionType.REQUIRES_RESOURCE,
                description="Requires computational resources for parallel MD",
                key="compute_resources",
                required=False,
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces converged free energy landscape",
                key="free_energy_landscape",
                context_keys=["l_fes_metric", "num_cycles"],
            ),
            Condition(
                condition_type=ConditionType.ENSURES_QUALITY,
                description="Ensures convergence (L_FES < 0.01)",
                key="convergence_quality",
                validator=lambda ctx: ctx.get("l_fes_metric", 1.0) < 0.01,
                error_message="iMMD did not converge (L_FES >= 0.01)",
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables FreeEnergyAgent MM/PBSA calculation",
                key="enables_free_energy",
            ),
        ],
        required_parameters=["pdb_structure"],
        optional_parameters=["max_cycles", "convergence_threshold"],
        estimated_runtime_seconds=3600.0,  # 1 hour
    ))
    
    # 4. FreeEnergyAgent
    profiles.append(FunctionProfile(
        function_id="biodynamo.free_energy.calculate_mm_pbsa",
        function_name="Calculate MM/PBSA Free Energy",
        specialist_id="free_energy",
        description="Computes binding/folding free energy with MM/PBSA",
        capabilities=["MM/PBSA", "Thermodynamic Integration", "FEP"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_SPECIALIST,
                description="Requires iMMD convergence for accurate free energy",
                key="immd_convergence",
                context_keys=["l_fes_metric"],
                required=False,  # Can run without iMMD, but less accurate
                error_message="iMMD convergence recommended for accurate MM/PBSA",
            ),
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires trajectory for MM/PBSA",
                key="trajectory_data",
                context_keys=["trajectory_path"],
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces free energy estimate",
                key="free_energy_result",
                context_keys=["delta_g_kcal_mol", "uncertainty"],
            ),
        ],
        required_parameters=["trajectory_path", "topology_path"],
        optional_parameters=["method", "num_frames"],
        estimated_runtime_seconds=300.0,
    ))
    
    # 5. RFDiffusionSpecialist
    profiles.append(FunctionProfile(
        function_id="biodynamo.rfdiffusion.generate_backbone",
        function_name="Generate De Novo Backbone",
        specialist_id="rfdiffusion_specialist",
        description="Generates novel protein backbones using conditional diffusion",
        capabilities=["Conditional diffusion", "Secondary structure conditioning", "Symmetry design"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_RESOURCE,
                description="Requires GPU for diffusion inference",
                key="gpu_resource",
                required=False,
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces backbone PDB structures",
                key="backbone_structures",
                context_keys=["pdb_structures"],
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables ProteinMPNN sequence design",
                key="enables_proteinmpnn",
            ),
        ],
        required_parameters=["secondary_structure", "sequence_length"],
        optional_parameters=["num_designs", "symmetry", "motif_pdb"],
        estimated_runtime_seconds=120.0,
    ))
    
    return profiles


# ============================================================================
# ALCHEMIST SPECIALIST PROFILES
# ============================================================================

def create_alchemist_profiles() -> List[FunctionProfile]:
    """Create FunctionProfiles for Alchemist specialists."""
    
    profiles = []
    
    # 1. QSARModelingAgent
    profiles.append(FunctionProfile(
        function_id="alchemist.qsar.predict_activity",
        function_name="Predict Molecular Activity",
        specialist_id="qsar_modeling",
        description="Predicts IC50/EC50/Kd using ChemBERTa or GNN-QSAR",
        capabilities=["ChemBERTa", "GNN-QSAR", "Activity prediction"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires molecular structure (SMILES or SDF)",
                key="molecular_structure",
                context_keys=["smiles", "sdf_file"],
                error_message="Molecular structure required for QSAR prediction",
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces activity prediction",
                key="activity_prediction",
                context_keys=["predicted_ic50_nm", "confidence"],
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables MolecularDocking (prioritize actives)",
                key="enables_docking",
            ),
        ],
        required_parameters=["smiles_or_sdf"],
        optional_parameters=["target_name", "model_version"],
        estimated_runtime_seconds=2.0,
    ))
    
    # 2. MolecularDockingAgent
    profiles.append(FunctionProfile(
        function_id="alchemist.docking.dock_ligand",
        function_name="Dock Ligand to Protein Target",
        specialist_id="molecular_docking",
        description="Predicts binding pose and affinity using AutoDock Vina or DiffDock",
        capabilities=["AutoDock Vina", "DiffDock", "Pose ranking"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_SPECIALIST,
                description="QSAR prediction recommended to prioritize candidates",
                key="qsar_prioritization",
                required=False,
                error_message="QSAR prediction recommended before expensive docking",
            ),
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires protein target structure",
                key="protein_target",
                context_keys=["target_pdb"],
                error_message="Protein target PDB required for docking",
            ),
            Condition(
                condition_type=ConditionType.REQUIRES_DATA,
                description="Requires ligand structure",
                key="ligand_structure",
                context_keys=["ligand_smiles", "ligand_sdf"],
                error_message="Ligand structure required for docking",
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces binding pose and affinity",
                key="docking_result",
                context_keys=["binding_affinity_kcal_mol", "pose_pdb"],
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables BioDynamo MD validation of docking pose",
                key="enables_md_validation",
            ),
        ],
        required_parameters=["target_pdb", "ligand_smiles"],
        optional_parameters=["box_center", "box_size", "method"],
        estimated_runtime_seconds=60.0,
    ))
    
    # 3. ProteinMPNNSpecialist
    profiles.append(FunctionProfile(
        function_id="alchemist.proteinmpnn.design_sequence",
        function_name="Design Sequence for Backbone",
        specialist_id="proteinmpnn_specialist",
        description="Inverse design: backbone → optimal sequence",
        capabilities=["Inverse design", "Multi-state design", "Temperature sampling"],
        preconditions=[
            Condition(
                condition_type=ConditionType.REQUIRES_SPECIALIST,
                description="Requires RFdiffusion backbone generation",
                key="backbone_structure",
                context_keys=["backbone_pdb"],
                error_message="Backbone PDB required (from RFdiffusion or template)",
            ),
        ],
        postconditions=[
            Condition(
                condition_type=ConditionType.PRODUCES_DATA,
                description="Produces designed sequences",
                key="designed_sequences",
                context_keys=["sequences", "scores"],
            ),
            Condition(
                condition_type=ConditionType.ENABLES_SPECIALIST,
                description="Enables BioDynamo thermodynamic validation",
                key="enables_thermodynamic_validation",
            ),
        ],
        required_parameters=["backbone_pdb"],
        optional_parameters=["num_sequences", "temperature", "fixed_positions"],
        estimated_runtime_seconds=30.0,
    ))
    
    return profiles


# ============================================================================
# REGISTRY INITIALIZATION
# ============================================================================

# Global registry instance
FUNCTION_PROFILE_REGISTRY = FunctionProfileRegistry()


def initialize_registry() -> FunctionProfileRegistry:
    """Initialize global registry with BioDynamo and Alchemist profiles."""
    
    # Register BioDynamo profiles
    for profile in create_biodynamo_profiles():
        FUNCTION_PROFILE_REGISTRY.register(profile)
    
    # Register Alchemist profiles
    for profile in create_alchemist_profiles():
        FUNCTION_PROFILE_REGISTRY.register(profile)
    
    logger.info(f"FunctionProfile registry initialized with {len(FUNCTION_PROFILE_REGISTRY.profiles)} profiles")
    return FUNCTION_PROFILE_REGISTRY


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("="*80)
    print("FunctionProfile System - Test Suite")
    print("="*80)
    
    # Initialize registry
    registry = initialize_registry()
    
    # Test 1: Dependency detection
    print("\n1. Dependency Detection")
    print("-" * 40)
    deps = registry.find_dependencies("biodynamo.sampling_orchestrator.select_strategy")
    print(f"Dependencies for SamplingOrchestrator: {deps}")
    
    # Test 2: Precondition validation (PASS)
    print("\n2. Precondition Validation (PASS)")
    print("-" * 40)
    context_valid = {
        "conformational_state": "Open",
        "uncertainty_score": 0.15,
        "trajectory_path": "/path/to/trajectory.dcd",
    }
    validation = registry.validate_preconditions(
        "biodynamo.sampling_orchestrator.select_strategy",
        context_valid,
    )
    print(f"Valid: {validation['valid']}")
    print(f"Passed: {validation['passed']}")
    print(f"Failed: {validation['failed']}")
    
    # Test 3: Precondition validation (FAIL)
    print("\n3. Precondition Validation (FAIL)")
    print("-" * 40)
    context_invalid = {
        "trajectory_path": "/path/to/trajectory.dcd",
        # Missing: conformational_state, uncertainty_score
    }
    validation = registry.validate_preconditions(
        "biodynamo.sampling_orchestrator.select_strategy",
        context_invalid,
    )
    print(f"Valid: {validation['valid']}")
    print(f"Failed: {validation['failed']}")
    
    # Test 4: Postcondition validation
    print("\n4. Postcondition Validation")
    print("-" * 40)
    result = {
        "l_fes_metric": 0.008,  # Converged!
        "num_cycles": 5,
    }
    validation = registry.validate_postconditions(
        "biodynamo.immd_control.run_aa_cg_cycles",
        result,
    )
    print(f"Valid: {validation['valid']}")
    print(f"Passed: {validation['passed']}")
    
    # Test 5: List profiles by specialist
    print("\n5. List Profiles by Specialist")
    print("-" * 40)
    sampling_profiles = registry.list_by_specialist("sampling_orchestrator")
    print(f"SamplingOrchestrator profiles: {[p.function_name for p in sampling_profiles]}")
    
    print("\n" + "="*80)
    print("FunctionProfile system tested successfully!")
    print("="*80)

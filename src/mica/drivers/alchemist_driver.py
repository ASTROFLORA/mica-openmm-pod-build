#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alchemist Multi-Agent Driver
============================

Implements MPI-UOS framework for Alchemist specialist orchestration.

Specialists (from drug discovery pipeline):
1. QSARModelingAgent - Quantitative structure-activity relationship
2. MolecularDockingAgent - Protein-ligand docking (AutoDock, Vina, Glide)
3. VirtualScreeningAgent - High-throughput screening
4. ADMETAgent - Absorption, Distribution, Metabolism, Excretion, Toxicity
5. De NovoDesignAgent - Generative molecular design
6. FragmentOptimizationAgent - Fragment-based lead optimization

Integration:
- Vertex AI Agent Engine deployment
- A2A Protocol for agent-to-agent communication
- MCP tools for RDKit, OpenBabel, ChemBERTa
- Memory Bank for compound library tracking
- COVID-19 screening playbook (QSAR → Docking → MD → MM/PBSA)

Based on:
- MPI-UOS: Tlahuizcalpantecuhtli breakthrough methodology
- MSRP: 5-phase scientific reasoning
- Research: Pandemic-era MD workflows (Amin et al. 2020, Hasan et al. 2022)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .worker_driver import WorkerDriver, WorkerDriverConfig, SpecialistAgent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy GCS import — only used if credentials are available
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

logger = logging.getLogger(__name__)


class AlchemistDriver(WorkerDriver):
    """
    Alchemist multi-agent orchestrator for drug discovery.
    
    Coordinates 6 specialist agents:
    - QSAR modeling (activity prediction)
    - Molecular docking (binding pose prediction)
    - Virtual screening (compound prioritization)
    - ADMET prediction (pharmacokinetic properties)
    - De novo design (generative chemistry)
    - Fragment optimization (fragment-based drug design)
    
    Architecture:
    - Vertex AI Agent Engine for deployment
    - A2A Protocol for inter-agent communication
    - MCP tools for cheminformatics (RDKit, OpenBabel)
    - Memory Bank for compound library tracking
    - COVID-19 playbook: QSAR → Docking → MD → MM/PBSA
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize AlchemistDriver with specialist agents."""
        
        # Define Alchemist specialists
        specialists = [
            SpecialistAgent(
                agent_id="qsar_modeling",
                agent_name="QSARModelingAgent",
                expertise_area="Quantitative structure-activity relationship modeling",
                description=(
                    "Builds predictive models relating molecular structure to biological activity. "
                    "Uses ChemBERTa, Graph Neural Networks, and traditional descriptors."
                ),
                capabilities=[
                    "ChemBERTa embeddings",
                    "Graph Neural Network QSAR",
                    "Descriptor-based models (RDKit)",
                    "Activity prediction (IC50, EC50, Kd)",
                    "Uncertainty quantification",
                    "SAR analysis",
                ],
                ai_university_role="Dr. QSAR Modeling, Computational Chemistry Laboratory",
                research_focus=[
                    "Activity prediction from structure",
                    "Virtual screening prioritization",
                    "Lead optimization guidance",
                ],
            ),
            SpecialistAgent(
                agent_id="molecular_docking",
                agent_name="MolecularDockingAgent",
                expertise_area="Protein-ligand docking and binding pose prediction",
                description=(
                    "Predicts binding poses and affinities using AutoDock Vina, Glide, and DiffDock. "
                    "Integrates with BioDynamo for MD validation."
                ),
                capabilities=[
                    "AutoDock Vina docking",
                    "Schrödinger Glide integration",
                    "NVIDIA DiffDock (ML-based)",
                    "Binding affinity estimation",
                    "Pose clustering and ranking",
                    "Cross-validation with BioDynamo MD",
                ],
                ai_university_role="Dr. Molecular Docking, Structural Bioinformatics Laboratory",
                research_focus=[
                    "Binding mode prediction",
                    "Virtual screening hit validation",
                    "Residence time correlation",
                ],
            ),
            SpecialistAgent(
                agent_id="virtual_screening",
                agent_name="VirtualScreeningAgent",
                expertise_area="High-throughput virtual screening",
                description=(
                    "Screens large compound libraries (ZINC, ChEMBL) against targets. "
                    "Prioritizes hits using QSAR, docking, and pharmacophore filters."
                ),
                capabilities=[
                    "ZINC database screening",
                    "ChEMBL library filtering",
                    "Pharmacophore matching",
                    "Lipinski's Rule of Five filtering",
                    "Hit prioritization algorithms",
                    "Multi-target screening",
                ],
                ai_university_role="Dr. Virtual Screening, Drug Discovery Laboratory",
                research_focus=[
                    "Compound library curation",
                    "Hit identification strategies",
                    "Scalable screening pipelines",
                ],
            ),
            SpecialistAgent(
                agent_id="admet_prediction",
                agent_name="ADMETAgent",
                expertise_area="ADMET property prediction",
                description=(
                    "Predicts absorption, distribution, metabolism, excretion, and toxicity properties. "
                    "Couples with BioDynamo PharmacoAnalyticsAgent for translational decisions."
                ),
                capabilities=[
                    "Lipophilicity (logP) prediction",
                    "Blood-brain barrier permeability",
                    "CYP450 metabolism prediction",
                    "hERG toxicity risk assessment",
                    "Oral bioavailability (F%)",
                    "AMES mutagenicity prediction",
                ],
                ai_university_role="Dr. ADMET Prediction, Pharmacokinetics Laboratory",
                research_focus=[
                    "Developability scoring",
                    "Lead optimization ADMET filters",
                    "Safety-driven design",
                ],
            ),
            SpecialistAgent(
                agent_id="denovo_design",
                agent_name="De NovoDesignAgent",
                expertise_area="Generative molecular design",
                description=(
                    "Generates novel molecules using REINVENT, MolGPT, and genetic algorithms. "
                    "Optimizes for multi-objective constraints (activity, ADMET, synthesizability)."
                ),
                capabilities=[
                    "REINVENT reinforcement learning",
                    "MolGPT transformer generation",
                    "Genetic algorithm optimization",
                    "Multi-objective optimization (Pareto frontier)",
                    "Synthesizability scoring",
                    "Scaffold hopping",
                ],
                ai_university_role="Dr. Generative Design, AI-Driven Chemistry Laboratory",
                research_focus=[
                    "Novel chemical space exploration",
                    "Multi-property optimization",
                    "AI-generated drug candidates",
                ],
            ),
            SpecialistAgent(
                agent_id="fragment_optimization",
                agent_name="FragmentOptimizationAgent",
                expertise_area="Fragment-based lead optimization",
                description=(
                    "Grows and links fragments into lead compounds. "
                    "Uses crystallographic fragment hits and structure-guided design."
                ),
                capabilities=[
                    "Fragment growing strategies",
                    "Fragment linking algorithms",
                    "Structure-based optimization",
                    "Ligand efficiency (LE) optimization",
                    "Scaffold merging",
                    "FEP-guided optimization (BioDynamo integration)",
                ],
                ai_university_role="Dr. Fragment Optimization, Structure-Based Design Laboratory",
                research_focus=[
                    "Fragment-to-lead progression",
                    "Ligand efficiency maximization",
                    "FEP-validated optimization",
                ],
            ),
            SpecialistAgent(
                agent_id="proteinmpnn_specialist",
                agent_name="ProteinMPNNSpecialist",
                expertise_area="Inverse protein sequence design using ProteinMPNN",
                description=(
                    "Designs amino acid sequences compatible with given protein backbones. "
                    "Uses message-passing neural networks (MPNN) for sequence optimization. "
                    "Collaborates with BioDynamo FreeEnergyAgent for thermodynamic validation."
                ),
                capabilities=[
                    "Backbone-to-sequence inverse design",
                    "Multi-state design (multiple conformations)",
                    "Partial sequence design (fixed regions)",
                    "Temperature-controlled sampling (exploration vs exploitation)",
                    "Sequence recovery benchmarking",
                    "TM-score prediction (structure match)",
                    "Collaborative validation with BioDynamo FreeEnergyAgent",
                    "Integration with AlphaFold2 for structure prediction",
                ],
                ai_university_role="Dr. Inverse Design, Computational Protein Engineering Laboratory",
                research_focus=[
                    "Message-passing neural networks (Dauparas et al. 2022)",
                    "Fixed-backbone sequence design",
                    "Thermodynamic stability optimization",
                    "Therapeutic protein engineering",
                ],
            ),
            SpecialistAgent(
                agent_id="boltz2_specialist",
                agent_name="Boltz2Specialist",
                expertise_area="Protein structure prediction using Boltz2",
                description=(
                    "Predicts protein structures from amino acid sequences using the shared Modal-backed Boltz2 deployment. "
                    "Produces structure candidates for downstream docking, protocol design, and mechanistic analysis."
                ),
                capabilities=[
                    "Single-sequence structure prediction",
                    "Boltz2 Modal deployment access",
                    "mmCIF/PDB structure artifact generation",
                    "Protocol-driven structure generation",
                ],
                ai_university_role="Dr. Structure Prediction, Computational Structural Biology Laboratory",
                research_focus=[
                    "Sequence-to-structure inference",
                    "Structure generation for downstream pipeline execution",
                ],
            ),
        ]
        
        # Alchemist configuration
        config = WorkerDriverConfig(
            worker_name="Alchemist",
            domain="Drug Discovery & Cheminformatics",
            specialists=specialists,
            enforce_msrp=True,
            minimum_hypotheses=5,  # Nature-level rigor
            require_literature_validation=True,
            enable_proactive_mode=True,
            enable_autonomous_discovery=True,
            scientific_pressure_level="nature",
            enable_literature_mcp=True,
            literature_sources=["semantic_scholar", "pubmed", "chemrxiv"],
        )
        
        _parent_kwargs = {k: v for k, v in kwargs.items() if k not in ("config", "agent_hub")}
        super().__init__(config, *args, **_parent_kwargs)

        # ── GCS OutputSaver (best-effort) ────────────────────────────────
        self.output_saver = None
        cls = _get_output_saver_cls()
        if cls and cls is not False:
            try:
                self.output_saver = cls.from_env()
                logger.info("\u2697\ufe0f AlchemistDriver: GCS OutputSaver ready")
            except Exception as exc:
                logger.debug("GCS OutputSaver not available: %s", exc)

        # ── RDKit tool-calling bridge ────────────────────────────────────
        self.rdkit_tools = None
        try:
            from workers.alchemist.native_core.tools import rdkit_tools
            self.rdkit_tools = rdkit_tools
            logger.info("\u2697\ufe0f AlchemistDriver: RDKit tools loaded")
        except Exception as exc:
            logger.debug("RDKit tools not available: %s", exc)

        # ── Docking engine bridge ────────────────────────────────────────
        self.docking_engine = None
        try:
            from workers.alchemist.native_core.tools import docking_engine
            self.docking_engine = docking_engine
            logger.info("\u2697\ufe0f AlchemistDriver: Docking engine loaded")
        except Exception as exc:
            logger.debug("Docking engine not available: %s", exc)

        # ── AlchemistPlanner (contract-based, no KAN) ────────────────────
        self.planner = None
        try:
            from workers.alchemist.cognitive_shell.alchemist_planner import (
                AlchemistPlanner,
            )
            self.planner = AlchemistPlanner()
            logger.info("\u2697\ufe0f AlchemistDriver: AlchemistPlanner loaded")
        except Exception as exc:
            logger.warning(
                "AlchemistPlanner NOT loaded — drug+MD chaining disabled: %s", exc
            )

        logger.info(
            f"\u2697\ufe0f AlchemistDriver Initialized | "
            f"Specialists: {len(self.specialists)} | "
            f"GCS: {'ON' if self.output_saver else 'OFF'} | "
            f"RDKit: {'ON' if self.rdkit_tools else 'OFF'} | "
            f"Docking: {'ON' if self.docking_engine else 'OFF'} | "
            f"Planner: {'ON' if self.planner else 'OFF'}"
        )
    
    async def execute(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        enforce_msrp: bool = True,
        thermodynamic_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute Alchemist query with specialist routing.
        
        Query Intent Routing:
        - "Predict activity" → QSARModelingAgent
        - "Dock molecule" → MolecularDockingAgent
        - "Screen library" → VirtualScreeningAgent
        - "Predict ADMET" → ADMETAgent
        - "Generate molecules" → De NovoDesignAgent
        - "Optimize fragment" → FragmentOptimizationAgent
        - "Drug discovery pipeline" → Multi-specialist coordination
        
        Args:
            query: Research query
            context: Optional context (SMILES, target PDB, library)
            enforce_msrp: Whether to enforce MSRP reasoning
            thermodynamic_context: Optional "Soul" state (Temperature, Energy)
        
        Returns:
            Dict with answer, msrp_chain, specialists_consulted, etc.
        """
        logger.info(f"⚗️ Alchemist Query: {query[:100]}...")
        
        # Parse query intent
        intent = self._parse_query_intent(query)

        # ── Detect combined drug-discovery + MD simulation intent ──────
        # If the planner is loaded and the query implies both molecule
        # creation AND simulation, route through plan_and_execute() which
        # chains de-novo → docking → ADMET → MD handoff automatically.
        if self.planner is not None and intent["type"] == "multi_specialist":
            query_lower = query.lower()
            wants_md = any(kw in query_lower for kw in (
                "simulat", "md", "dynamics", "stability", "openmm",
                "vast", "pod", "remote", "charmm", "amber",
            ))
            if wants_md:
                logger.info(
                    "⚗️🧬 Combined drug+MD intent detected — routing to plan_and_execute()"
                )
                # Build MUDO data from context
                mudo_data = dict(context or {})
                mudo_data.setdefault("query", query)
                return await self.plan_and_execute(
                    mudo_data=mudo_data,
                    task_description=query,
                    context=context,
                )
        
        # Route to appropriate specialist(s)
        if intent["type"] == "single_specialist":
            if intent["specialist_id"] == "proteinmpnn_specialist":
                response = await self._execute_proteinmpnn_query(
                    query=query,
                    context=context,
                )
                return {
                    **response,
                    "intent": intent,
                    "routing": "serverless_model",
                }

            if intent["specialist_id"] == "boltz2_specialist":
                response = await self._execute_boltz2_query(
                    query=query,
                    context=context,
                )
                return {
                    **response,
                    "intent": intent,
                    "routing": "serverless_model",
                }

            # Single specialist execution
            response = await self.route_to_specialist(
                query=query,
                specialist_id=intent["specialist_id"],
                enforce_msrp=enforce_msrp,
                thermodynamic_context=thermodynamic_context,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "single_specialist",
            }
        
        elif intent["type"] == "multi_specialist":
            # Coordinate multiple specialists (drug discovery pipeline)
            response = await self._coordinate_drug_discovery_pipeline(
                query=query,
                specialist_ids=intent["specialist_ids"],
                context=context,
                enforce_msrp=enforce_msrp,
            )
            
            return {
                **response,
                "intent": intent,
                "routing": "drug_discovery_pipeline",
            }
        
        else:
            # Unknown intent
            logger.warning(f"⚠️ Unknown intent: {intent}")
            return {
                "answer": f"Unable to parse intent for query: {query}",
                "intent": intent,
                "routing": "fallback",
            }

    # ------------------------------------------------------------------
    # Contract-based planning (replaces KAN planner)
    # ------------------------------------------------------------------

    async def plan_and_execute(
        self,
        mudo_data: Dict[str, Any],
        task_description: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Plan a pipeline using AlchemistPlanner contracts, then execute it.

        This is the new entry-point that replaces the KAN-based planning.
        It inspects the MUDO, builds a valid pipeline, and runs each step.

        Parameters
        ----------
        mudo_data : dict
            Raw data dict (or MUDO.data).
        task_description : str
            Natural-language description of the task.
        context : dict, optional
            Extra context (user_id, target_pdb, etc.).

        Returns
        -------
        dict
            plan, step_results, final_mudo_data, md_validation
        """
        if self.planner is None:
            return {
                "error": "AlchemistPlanner not loaded",
                "success": False,
                "md_validation_skipped": True,
            }

        from workers.alchemist.cognitive_shell.alchemist_planner import (
            inspect_mudo,
        )

        # 1. Plan
        plan = self.planner.plan(mudo_data, task_description)
        logger.info(
            "📋 Plan: %d steps, causality=%.2f, energy=%.1f — skipped=%s",
            len(plan.pipeline), plan.causality_score,
            plan.estimated_energy, plan.skipped_steps,
        )

        # 2. Execute each step
        step_results = []
        current_data = dict(mudo_data)
        for step in plan.pipeline:
            logger.info("▶ Executing pipeline step: %s", step.name)
            try:
                result = await self._execute_pipeline_step(
                    step.name, current_data, step.parameters,
                )
                step_results.append({"step": step.name, "success": True, "result": result})
                # Merge outputs back into current_data for next step
                if isinstance(result, dict):
                    current_data.update(result)
            except Exception as exc:
                logger.error("Pipeline step %s failed: %s", step.name, exc)
                step_results.append({"step": step.name, "success": False, "error": str(exc)})
                # Continue: some steps may still be valid

        # 3. Cross-driver handoff to BioDynamo for MD if needed
        md_validation = None
        step_names = [s.name for s in plan.pipeline]
        query_lower = (task_description or "").lower()
        md_requested = any(
            kw in query_lower
            for kw in (
                "md ",
                "molecular dynamics",
                "dynamics",
                "stability simulation",
                "protein-ligand",
                "protein ligand",
                "openmm",
                "vast",
                "remote",
                "pod",
                "simulation",
            )
        )
        if "md_validation" in step_names or md_requested or (context or {}).get("use_remote_vast"):
            md_validation = await self._handoff_to_biodynamo(current_data, context or {})

        # 4. Persist
        user_id = (context or {}).get("user_id", "anonymous")
        final_result = {
            "success": True,
            "plan": {
                "steps": [s.name for s in plan.pipeline],
                "causality_score": plan.causality_score,
                "estimated_energy": plan.estimated_energy,
                "capabilities_after": list(plan.capabilities_after),
                "skipped_steps": plan.skipped_steps,
            },
            "step_results": step_results,
            "final_mudo_data": current_data,
            "md_validation": md_validation,
        }
        await self._persist_pipeline_output(user_id, final_result)
        return final_result

    async def _execute_pipeline_step(
        self,
        step_name: str,
        data: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run a single pipeline step by name.

        Dispatch priority:
        1. Registered pipeline function (workers/alchemist/pipelines)
        2. **Direct RDKit / docking execution** (real tools, no LLM)
        3. Specialist LLM routing (fallback)
        """
        # ── 1. Try registered pipeline function ──────────────────────
        try:
            from workers.alchemist.pipelines import pipeline_registry
            fn = pipeline_registry.get_pipeline(step_name)
            result = fn(data, **params)
            if hasattr(result, "data"):
                return result.data  # MUDO -> extract data dict
            return result if isinstance(result, dict) else {"result": result}
        except (ValueError, ImportError):
            pass  # proceed to direct tool dispatch

        normalized_step_name = self._normalize_pipeline_step_name(step_name)

        # ── 2. Direct tool dispatch (real RDKit / docking) ───────────
        direct_result = await self._try_direct_tool_dispatch(normalized_step_name, data, params)
        if direct_result is not None:
            return direct_result

        # ── 3. Specialist LLM routing (fallback) ────────────────────
        logger.info("No registered pipeline or direct tool for '%s' — routing to specialist", step_name)
        specialist_map = {
            "denovo_design": "denovo_design",
            "admet_profile": "admet_prediction",
            "docking": "molecular_docking",
            "virtual_screening": "virtual_screening",
            "lead_optimization": "fragment_optimization",
            "predict_structure": "boltz2_specialist",
        }
        spec_id = specialist_map.get(normalized_step_name)
        if spec_id:
            resp = await self.route_to_specialist(
                query=f"Execute {normalized_step_name} step",
                specialist_id=spec_id,
                enforce_msrp=False,
            )
            return resp
        return {"warning": f"No pipeline or specialist for step '{step_name}'"}

    def _normalize_pipeline_step_name(self, step_name: str) -> str:
        """Collapse known function ids and aliases into executable step names."""
        normalized = (step_name or "").strip()
        alias_map = {
            "alchemist.proteinmpnn.design_sequence": "design_sequence",
            "alchemist.proteinmpnn.inverse_design": "design_sequence",
            "alchemist.boltz2.predict_structure": "predict_structure",
            "alchemist.boltz2.fold_sequence": "predict_structure",
            "alchemist.docking.dock_ligand": "docking",
            "alchemist.molecular_docking": "docking",
            "alchemist.qsar.predict_activity": "admet_profile",
            "alchemist.admet_prediction": "admet_profile",
            "alchemist.denovo_design": "denovo_design",
            "alchemist.lead_optimization": "lead_optimization",
            "alchemist.virtual_screening": "virtual_screening",
        }
        return alias_map.get(normalized, normalized)

    async def _try_direct_tool_dispatch(
        self,
        step_name: str,
        data: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Attempt to execute a step using real RDKit / docking tools.

        Returns *None* if the step is not handled here (caller should
        continue to the specialist fallback).
        """
        smiles = data.get("smiles", "") or params.get("smiles", "")

        # ── Docking step → real AutoDock Vina via docking_engine ─────
        if step_name == "docking":
            protein_pdb = self._resolve_protein_pdb(data, params)
            if not protein_pdb or not smiles:
                logger.warning("Docking step: missing protein_pdb or smiles — skipping direct dispatch")
                return None
            logger.info("⚗️  Direct docking dispatch: protein=%s, SMILES=%s", protein_pdb, smiles[:40])
            return self.execute_docking(
                protein_pdb=protein_pdb,
                ligand_smiles=smiles,
                center=params.get("center"),
                box_size=params.get("box_size"),
                exhaustiveness=params.get("exhaustiveness", 8),
                num_modes=params.get("num_modes", 9),
            )

        # ── ADMET profiling → real RDKit descriptors + Lipinski ──────
        if step_name == "admet_profile" and smiles:
            logger.info("⚗️  Direct ADMET dispatch via RDKit: SMILES=%s", smiles[:40])
            descriptors = self.execute_rdkit_tool("compute_descriptors", smiles=smiles)
            admet = self.execute_rdkit_tool("predict_admet_properties", smiles=smiles)
            return {
                "step": "admet_profile",
                "descriptors": descriptors,
                "admet_prediction": admet,
                "execution_mode": "direct_rdkit",
            }

        # ── Molecular descriptors / fingerprints ─────────────────────
        if step_name in ("compute_descriptors", "molecular_descriptors") and smiles:
            logger.info("⚗️  Direct descriptor dispatch via RDKit: SMILES=%s", smiles[:40])
            return self.execute_rdkit_tool("compute_descriptors", smiles=smiles)

        if step_name in ("compute_fingerprint", "fingerprint") and smiles:
            logger.info("⚗️  Direct fingerprint dispatch via RDKit: SMILES=%s", smiles[:40])
            return self.execute_rdkit_tool("compute_fingerprint", smiles=smiles)

        # ── Similarity search ────────────────────────────────────────
        if step_name == "similarity_search":
            ref = params.get("reference_smiles", smiles)
            target = params.get("target_smiles", "")
            if ref and target:
                logger.info("⚗️  Direct similarity dispatch via RDKit")
                return self.execute_rdkit_tool("compute_similarity", smiles_a=ref, smiles_b=target)

        # ── SMILES validation ────────────────────────────────────────
        if step_name == "validate_smiles" and smiles:
            return self.execute_rdkit_tool("validate_smiles", smiles=smiles)

        # ── Lead optimization (scaffold analysis) ────────────────────
        if step_name == "lead_optimization" and smiles:
            logger.info("⚗️  Direct scaffold+descriptors dispatch via RDKit")
            scaffold = self.execute_rdkit_tool("compute_scaffold", smiles=smiles)
            descriptors = self.execute_rdkit_tool("compute_descriptors", smiles=smiles)
            return {
                "step": "lead_optimization",
                "scaffold": scaffold,
                "descriptors": descriptors,
                "execution_mode": "direct_rdkit",
            }

        if step_name in ("proteinmpnn_design", "proteinmpnn_sequence_design", "design_sequence"):
            proteinmpnn_inputs = self._build_proteinmpnn_inputs(
                query="",
                context={**data, **params},
            )
            if proteinmpnn_inputs is None:
                logger.warning("ProteinMPNN step: missing PDB reference or chain assignment — skipping direct dispatch")
                return None
            return await self.invoke_serverless_model(
                model_id="proteinmpnn.design.sequence",
                inputs=proteinmpnn_inputs,
                user_id=str(params.get("user_id") or data.get("user_id") or self.config.worker_name),
                session_id=params.get("session_id") or data.get("session_id"),
                run_id=params.get("run_id") or data.get("run_id"),
                requested_by="Alchemist",
            )

        if step_name in ("boltz2_predict", "predict_structure", "fold_sequence"):
            boltz2_inputs = self._build_boltz2_inputs(
                query="",
                context={**data, **params},
            )
            if boltz2_inputs is None:
                logger.warning("Boltz2 step: missing sequence — skipping direct dispatch")
                return None
            return await self.invoke_serverless_model(
                model_id="boltz2.predict.structure",
                inputs=boltz2_inputs,
                user_id=str(params.get("user_id") or data.get("user_id") or self.config.worker_name),
                session_id=params.get("session_id") or data.get("session_id"),
                run_id=params.get("run_id") or data.get("run_id"),
                requested_by="Alchemist",
            )

        return None  # not handled — let caller continue to specialist

    # ── PDB resolution helper ──────────────────────────────────────────
    def _resolve_protein_pdb(
        self,
        data: Dict[str, Any],
        context: Dict[str, Any],
    ) -> str:
        """Ensure protein_pdb is a valid local file path, downloading from RCSB if needed.

        Checks multiple keys (protein_pdb, target_pdb, pdb_id) in both *data*
        and *context*.  If the value looks like a 4-char PDB accession code,
        it is fetched from RCSB and cached under ``$MICA_PDB_CACHE``
        (default ``<tempdir>/mica_pdb_cache``).
        """
        import tempfile
        import urllib.request

        pdb_ref = (
            data.get("protein_pdb")
            or data.get("target_pdb")
            or context.get("protein_pdb")
            or context.get("target_pdb")
            or context.get("pdb_id", "")
        )
        if not pdb_ref:
            return ""

        # Already a valid local file?
        p = Path(pdb_ref)
        if p.is_file():
            return str(p.resolve())

        # Looks like a 4-char PDB accession code (e.g. "4NSS")?
        pdb_id = pdb_ref.replace(".pdb", "").strip().upper()
        if len(pdb_id) == 4 and pdb_id.isalnum():
            cache_dir = Path(
                os.environ.get("MICA_PDB_CACHE", Path(tempfile.gettempdir()) / "mica_pdb_cache")
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

        # Pass through as-is (may still work if it's a valid path elsewhere)
        return str(pdb_ref)

    async def _handoff_to_biodynamo(
        self,
        data: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Hand off to BioDynamo for protein-ligand MD validation."""
        smiles = self._extract_smiles_for_md(data, context)
        pdb_ref = self._extract_pdb_ref_for_md(data, context)
        protein_pdb = self._resolve_protein_pdb(
            {**data, "protein_pdb": pdb_ref or data.get("protein_pdb", "")},
            context,
        )
        docked_pdb = data.get("docked_ligand_pdb", "")
        if not docked_pdb:
            docked_pdb = str(context.get("docked_ligand_pdb", "") or "")

        if not smiles or not protein_pdb:
            return {"error": "Need smiles + protein_pdb for MD handoff"}

        try:
            handoff_ctx = {
                "intent": "protein_ligand_md",
                "ligand_smiles": smiles,
                "docked_ligand_pdb": docked_pdb,
                "protein_pdb": protein_pdb,
                "production_ns": 50.0,
                "forcefield": "amber14sb",
                "ligand_ff": "gaff-2.11",
                "source": "alchemist_plan_and_execute",
            }
            # ── Propagate remote Vast.ai flag if present ──
            if context.get("use_remote_vast"):
                handoff_ctx["use_remote_vast"] = True
            else:
                query_text = " ".join(
                    str(context.get(k, ""))
                    for k in ("query", "prompt", "task_description")
                ).lower()
                if any(kw in query_text for kw in ("vast", "remote", "pod", "openmm")):
                    handoff_ctx["use_remote_vast"] = True

            # Optional remote execution tuning knobs
            for key in (
                "max_price_per_hour",
                "max_total_cost_usd",
                "steps",
                "n_replicas",
                "ssh_key_path",
                "gpu_type",
            ):
                if key in context and context.get(key) is not None:
                    handoff_ctx[key] = context.get(key)

            if hasattr(self, "agent_hub") and self.agent_hub is not None:
                result = await self.agent_hub.route(
                    target="biodynamo",
                    query="Run protein-ligand MD stability simulation",
                    context=handoff_ctx,
                )
                return result

            logger.warning("No agent_hub for BioDynamo handoff; using direct BioDynamoDriver fallback")
            from .biodynamo_driver import BioDynamoDriver

            biodynamo = BioDynamoDriver()
            return await biodynamo.execute(
                query="Run protein-ligand MD stability simulation",
                context=handoff_ctx,
                enforce_msrp=False,
            )
        except Exception as exc:
            logger.warning("BioDynamo handoff failed: %s", exc)
            return {"error": str(exc)}

    def _extract_smiles_for_md(self, data: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Extract a ligand SMILES deterministically from data/context/query text."""
        for key in ("smiles", "ligand_smiles"):
            value = data.get(key) or context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        # from prioritized hits (if present in context)
        hits = context.get("prioritized_hits") or data.get("prioritized_hits")
        if isinstance(hits, list) and hits:
            first = hits[0] if isinstance(hits[0], dict) else {}
            value = first.get("smiles", "")
            if isinstance(value, str) and value.strip():
                return value.strip()

        blob = "\n".join(
            str(context.get(k, ""))
            for k in ("query", "prompt", "task_description")
        )
        if blob:
            # e.g. ligand_smiles=CCO, smiles: CCO
            match = re.search(r"(?:ligand_)?smiles\s*[:=]\s*([^\s,;]+)", blob, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().strip('"\'')

        return ""

    def _extract_pdb_ref_for_md(self, data: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Extract protein/PDB reference deterministically from data/context/query text."""
        for key in ("protein_pdb", "target_pdb", "pdb_id"):
            value = data.get(key) or context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        blob = "\n".join(
            str(context.get(k, ""))
            for k in ("query", "prompt", "task_description")
        )
        if blob:
            # explicit key syntax first
            match = re.search(
                r"(?:protein_pdb|target_pdb|pdb_id)\s*[:=]\s*([^\s,;]+)",
                blob,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(1).strip().strip('"\'')

            # natural text: "PDB 6FBK"
            pdb_match = re.search(r"\bPDB\s*[:=]?\s*([0-9A-Za-z]{4})\b", blob, flags=re.IGNORECASE)
            if pdb_match:
                return pdb_match.group(1).strip()

        return ""

    def _extract_proteinmpnn_chains(self, query: str, context: Dict[str, Any]) -> str:
        for key in ("pdb_path_chains", "chains", "chain", "designed_chains"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        if query:
            chain_match = re.search(
                r"\bchains?\s*[:=]?\s*([A-Za-z0-9_,\-]+)",
                query,
                flags=re.IGNORECASE,
            )
            if chain_match:
                return chain_match.group(1).strip().upper().replace(" ", "")

        return ""

    def _build_proteinmpnn_inputs(self, query: str, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        source_context = dict(context or {})
        pdb_ref = (
            source_context.get("backbone_pdb")
            or source_context.get("pdb_text")
            or source_context.get("pdb_url")
            or self._extract_pdb_ref_for_md(source_context, source_context)
            or self._extract_pdb_ref_for_md({}, {"query": query})
        )
        chains = self._extract_proteinmpnn_chains(query, source_context)
        if not pdb_ref or not chains:
            return None

        inputs: Dict[str, Any] = {"pdb_path_chains": chains}
        pdb_ref_str = str(pdb_ref).strip()
        pdb_path = Path(pdb_ref_str)

        if pdb_path.is_file():
            try:
                inputs["pdb_text"] = pdb_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                inputs["pdb_text"] = pdb_path.read_text(encoding="latin-1")
        elif re.fullmatch(r"[0-9A-Za-z]{4}", pdb_ref_str):
            inputs["pdb_id"] = pdb_ref_str.upper()
        elif pdb_ref_str.lower().startswith(("http://", "https://")):
            inputs["pdb_url"] = pdb_ref_str
        elif "\n" in pdb_ref_str or "ATOM" in pdb_ref_str or "HEADER" in pdb_ref_str:
            inputs["pdb_text"] = pdb_ref_str
        else:
            return None

        optional_map = {
            "num_seq_per_target": "num_seq_per_target",
            "num_sequences": "num_seq_per_target",
            "batch_size": "batch_size",
            "sampling_temp": "sampling_temp",
            "temperature": "sampling_temp",
            "seed": "seed",
            "model_name": "model_name",
            "ca_only": "ca_only",
            "use_soluble_model": "use_soluble_model",
            "timeout_seconds": "timeout_seconds",
        }
        for source_key, target_key in optional_map.items():
            value = source_context.get(source_key)
            if value is not None and target_key not in inputs:
                inputs[target_key] = value

        return inputs

    def _extract_boltz2_sequence(self, query: str, context: Dict[str, Any]) -> str:
        for key in ("sequence", "protein_sequence", "aa_sequence", "uniprot_sequence"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                return re.sub(r"\s+", "", value).upper()

        if query:
            explicit_match = re.search(
                r"(?:sequence|protein_sequence|aa_sequence)\s*[:=]\s*([A-Za-z\n\s]{15,})",
                query,
                flags=re.IGNORECASE,
            )
            if explicit_match:
                return re.sub(r"\s+", "", explicit_match.group(1)).upper()

            generic_matches = re.findall(r"\b[A-Z]{20,}\b", query.upper())
            if generic_matches:
                return generic_matches[0]

        return ""

    def _build_boltz2_inputs(self, query: str, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        source_context = dict(context or {})
        sequence = self._extract_boltz2_sequence(query, source_context)
        yaml_text = str(source_context.get("boltz_input_yaml") or "").strip()
        if not sequence and not yaml_text:
            return None

        inputs: Dict[str, Any] = {}
        if yaml_text:
            inputs["boltz_input_yaml"] = yaml_text
        else:
            inputs["sequence"] = sequence
            inputs["chain_id"] = str(source_context.get("chain_id") or "A").strip() or "A"

        optional_map = {
            "recycling_steps": "recycling_steps",
            "sampling_steps": "sampling_steps",
            "diffusion_samples": "diffusion_samples",
            "output_format": "output_format",
            "use_msa_server": "use_msa_server",
            "args": "args",
            "timeout_seconds": "timeout_seconds",
        }
        for source_key, target_key in optional_map.items():
            value = source_context.get(source_key)
            if value is not None:
                inputs[target_key] = value

        return inputs

    async def _execute_proteinmpnn_query(
        self,
        *,
        query: str,
        context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        proteinmpnn_inputs = self._build_proteinmpnn_inputs(query, context)
        if proteinmpnn_inputs is None:
            return {
                "answer": (
                    "ProteinMPNN requires a backbone reference plus chain selection. "
                    "Provide one of pdb_id, pdb_url, pdb_text, or backbone_pdb, and set pdb_path_chains."
                ),
                "error": "missing_proteinmpnn_inputs",
            }

        result = await self.invoke_serverless_model(
            model_id="proteinmpnn.design.sequence",
            inputs=proteinmpnn_inputs,
            user_id=str((context or {}).get("user_id") or self.config.worker_name),
            session_id=(context or {}).get("session_id"),
            run_id=(context or {}).get("run_id"),
            requested_by="Alchemist",
        )
        return {
            "answer": "ProteinMPNN sequence design completed.",
            "model_id": "proteinmpnn.design.sequence",
            "inputs": proteinmpnn_inputs,
            "serverless_result": result,
        }

    async def _execute_boltz2_query(
        self,
        *,
        query: str,
        context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        boltz2_inputs = self._build_boltz2_inputs(query, context)
        if boltz2_inputs is None:
            return {
                "answer": (
                    "Boltz2 requires a protein sequence or a pre-built boltz_input_yaml payload. "
                    "Provide sequence, protein_sequence, aa_sequence, or boltz_input_yaml."
                ),
                "error": "missing_boltz2_inputs",
            }

        result = await self.invoke_serverless_model(
            model_id="boltz2.predict.structure",
            inputs=boltz2_inputs,
            user_id=str((context or {}).get("user_id") or self.config.worker_name),
            session_id=(context or {}).get("session_id"),
            run_id=(context or {}).get("run_id"),
            requested_by="Alchemist",
        )
        return {
            "answer": "Boltz2 structure prediction completed.",
            "model_id": "boltz2.predict.structure",
            "inputs": boltz2_inputs,
            "serverless_result": result,
        }
    
    def _parse_query_intent(self, query: str) -> Dict[str, Any]:
        """Parse query to determine routing intent."""
        
        query_lower = query.lower()

        # ── Check for combined drug+MD keywords FIRST ──────────────────
        _drug_kw = any(kw in query_lower for kw in (
            "drug", "design", "generate", "denovo", "lead optimization",
            "molecule", "compound", "pharmacophore", "covid",
        ))
        _md_kw = any(kw in query_lower for kw in (
            "simulat", "md ", "dynamics", "stability", "openmm",
            "vast", "pod", "remote", "charmm", "amber",
        ))
        if _drug_kw and _md_kw:
            return {
                "type": "multi_specialist",
                "specialist_ids": [
                    "denovo_design",
                    "molecular_docking",
                    "admet_prediction",
                ],
            }

        proteinmpnn_signal = any(kw in query_lower for kw in (
            "proteinmpnn",
            "inverse design",
            "inverse folding",
            "backbone-conditioned",
            "design sequence",
        ))
        protein_backbone_signal = any(kw in query_lower for kw in (
            "backbone",
            "pdb ",
            "pdb:",
            "chain ",
            "chains ",
        ))
        if proteinmpnn_signal or (protein_backbone_signal and "sequence" in query_lower and "design" in query_lower):
            return {"type": "single_specialist", "specialist_id": "proteinmpnn_specialist"}

        boltz2_signal = any(kw in query_lower for kw in (
            "boltz2",
            "predict structure",
            "structure prediction",
            "fold sequence",
            "sequence to structure",
        ))
        has_sequence_signal = bool(re.search(r"\b[A-Z]{20,}\b", query.upper())) or any(
            kw in query_lower for kw in ("sequence:", "protein_sequence", "aa_sequence")
        )
        if boltz2_signal and has_sequence_signal:
            return {"type": "single_specialist", "specialist_id": "boltz2_specialist"}
        
        # Single specialist routing
        if "qsar" in query_lower or "activity prediction" in query_lower:
            return {"type": "single_specialist", "specialist_id": "qsar_modeling"}
        
        elif "dock" in query_lower or "binding pose" in query_lower:
            return {"type": "single_specialist", "specialist_id": "molecular_docking"}
        
        elif "screen" in query_lower and "library" in query_lower:
            return {"type": "single_specialist", "specialist_id": "virtual_screening"}
        
        elif "admet" in query_lower or "adme" in query_lower:
            return {"type": "single_specialist", "specialist_id": "admet_prediction"}
        
        elif "generate" in query_lower or "design" in query_lower or "denovo" in query_lower:
            return {"type": "single_specialist", "specialist_id": "denovo_design"}
        
        elif "fragment" in query_lower and "optimize" in query_lower:
            return {"type": "single_specialist", "specialist_id": "fragment_optimization"}
        
        # Multi-specialist (full drug discovery pipeline)
        elif "drug discovery" in query_lower or "lead optimization" in query_lower or "covid" in query_lower:
            # COVID-19 playbook: QSAR → Docking → (BioDynamo MD) → (BioDynamo FreeEnergy)
            return {
                "type": "multi_specialist",
                "specialist_ids": [
                    "qsar_modeling",
                    "molecular_docking",
                    "admet_prediction",
                    "virtual_screening",
                ],
            }
        
        # Default: Unknown intent
        return {"type": "unknown"}
    
    async def _coordinate_drug_discovery_pipeline(
        self,
        query: str,
        specialist_ids: List[str],
        context: Optional[Dict[str, Any]],
        enforce_msrp: bool,
    ) -> Dict[str, Any]:
        """
        Coordinate drug discovery pipeline following COVID-19 playbook.
        
        Pipeline:
        1. QSAR Modeling - Predict activity
        2. Virtual Screening - Filter compound library
        3. Molecular Docking - Predict binding poses
        4. ADMET Prediction - Assess developability
        5. (BioDynamo MD validation - cross-worker)
        6. (BioDynamo FreeEnergy - cross-worker)
        
        Args:
            query: Research query
            specialist_ids: Specialists to coordinate
            context: Target, library, etc.
            enforce_msrp: Whether to enforce MSRP
        
        Returns:
            Pipeline results with prioritized hits
        """
        logger.info(f"🔬 Drug Discovery Pipeline: {len(specialist_ids)} specialists")
        
        results = []
        accumulated_context = context or {}
        
        for spec_id in specialist_ids:
            response = await self.route_to_specialist(
                query=query,
                specialist_id=spec_id,
                enforce_msrp=enforce_msrp,
            )
            
            results.append(response)
            
            # Accumulate context for next specialist
            accumulated_context[spec_id] = response
            
            # Filter compounds based on specialist output
            if spec_id == "qsar_modeling":
                # Filter by predicted activity
                accumulated_context["active_compounds"] = response.get("active_compounds", [])
            
            elif spec_id == "admet_prediction":
                # Filter by ADMET properties
                accumulated_context["developable_compounds"] = response.get("developable_compounds", [])
        
        # Synthesize final recommendations
        final_hits = self._synthesize_drug_discovery_hits(results)

        # ── Cross-driver handoff: Alchemist → BioDynamo ──────────────────
        # If we have hits with SMILES + docking results, hand off to
        # BioDynamo for protein–ligand MD stability validation.
        md_validation = None
        target_pdb = ""
        if final_hits:
            top_hit = final_hits[0]
            hit_smiles = top_hit.get("smiles", "")
            hit_docking = top_hit.get("docking_pose_pdb", "")
            target_pdb = self._resolve_protein_pdb(
                top_hit, accumulated_context,
            )

            if hit_smiles and target_pdb:
                logger.info(
                    f"🔗 Alchemist→BioDynamo handoff: SMILES={hit_smiles[:40]}, "
                    f"target={target_pdb}"
                )
                try:
                    md_validation = await self._handoff_to_biodynamo(
                        {
                            "smiles": hit_smiles,
                            "docked_ligand_pdb": hit_docking,
                            "protein_pdb": target_pdb,
                        },
                        {
                            **(context or {}),
                            "intent": "protein_ligand_md",
                            "production_ns": 50.0,
                            "forcefield": "amber14sb",
                            "ligand_ff": "gaff-2.11",
                            "source": "alchemist_drug_discovery_pipeline",
                        },
                    )
                    logger.info("✅ BioDynamo MD validation complete")
                except Exception as e:
                    logger.warning(f"BioDynamo handoff failed: {e}")
                    md_validation = {"error": str(e)}

        pipeline_result = {
            "answer": f"Drug discovery pipeline completed. {len(final_hits)} prioritized hits identified.",
            "prioritized_hits": final_hits,
            "specialists_consulted": specialist_ids,
            "specialist_responses": results,
            "md_validation": md_validation,
            "pipeline_complete": True,
            "resolved_pdb_path": target_pdb,
        }

        # ── Auto-save to GCS ─────────────────────────────────────────────
        user_id = (context or {}).get("user_id", "anonymous")
        await self._persist_pipeline_output(user_id, pipeline_result)

        return pipeline_result
    
    def _synthesize_drug_discovery_hits(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Synthesize final hit list from pipeline specialist results.

        Extracts structured compound data from each specialist phase and
        merges into a single ranked hit list.  Falls back to best-effort
        extraction if specialist responses lack standard keys.
        """
        hits_by_id: Dict[str, Dict[str, Any]] = {}

        for resp in results:
            # Each specialist may return compound lists under different keys
            compounds = (
                resp.get("active_compounds")
                or resp.get("top_compounds")
                or resp.get("developable_compounds")
                or resp.get("docking_results")
                or []
            )
            if isinstance(compounds, list):
                for cmpd in compounds:
                    cid = cmpd.get("compound_id") or cmpd.get("smiles", "UNK")
                    entry = hits_by_id.setdefault(cid, {
                        "compound_id": cid,
                        "smiles": cmpd.get("smiles", ""),
                    })
                    # Merge metrics
                    for key in ("predicted_activity", "docking_score",
                                "admet_score", "docking_pose_pdb", "binding_energy"):
                        if key in cmpd:
                            entry[key] = cmpd[key]

        # Sort by docking_score (lower is better) then predicted_activity (higher is better)
        ranked = sorted(
            hits_by_id.values(),
            key=lambda h: (h.get("docking_score", 0), -h.get("predicted_activity", 0)),
        )

        # Annotate recommendation
        for hit in ranked:
            hit["recommendation"] = "Prioritize for MD validation"

        if not ranked:
            # Fallback placeholder when specialists return unstructured text
            ranked = [{
                "compound_id": "PENDING",
                "smiles": "",
                "recommendation": "Re-run with structured specialist output",
            }]

        return ranked

    # ------------------------------------------------------------------
    # GCS persistence
    # ------------------------------------------------------------------

    async def _persist_pipeline_output(
        self,
        user_id: str,
        result: Dict[str, Any],
    ) -> None:
        """Best-effort upload of pipeline results to the user's GCS bucket."""
        output_saver = getattr(self, "output_saver", None)
        if not output_saver:
            return
        try:
            run_id = output_saver.make_run_id("alchemist_pipeline")
            await output_saver.save_result(
                user_id=user_id,
                run_id=run_id,
                filename="pipeline_result.json",
                data=result,
            )
        except Exception as exc:
            logger.warning("GCS persist failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # RDKit tool calling  (direct bridge to workers/alchemist/native_core)
    # ------------------------------------------------------------------

    def execute_rdkit_tool(
        self,
        tool_name: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Invoke an RDKit tool by name.

        Supported tool names (see ``rdkit_tools.py``)::

            compute_descriptors, compute_fingerprint, compute_similarity,
            predict_admet_properties, smiles_to_3d, smiles_to_mol_block,
            validate_smiles, compute_scaffold, enumerate_tautomers,
            compute_charges

        Parameters
        ----------
        tool_name : str
            Function name inside ``rdkit_tools``.
        **kwargs
            Arguments forwarded to the function.

        Returns
        -------
        dict
            ``{success: True, ...}`` or ``{success: False, error: "..."}``
        """
        if self.rdkit_tools is None:
            return {"success": False, "error": "rdkit_tools not available"}
        fn = getattr(self.rdkit_tools, tool_name, None)
        if fn is None:
            return {
                "success": False,
                "error": f"Unknown RDKit tool: {tool_name}",
                "available": [
                    n for n in dir(self.rdkit_tools)
                    if not n.startswith("_") and callable(getattr(self.rdkit_tools, n, None))
                ],
            }
        try:
            return fn(**kwargs)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Docking execution  (direct bridge to docking_engine)
    # ------------------------------------------------------------------

    def execute_docking(
        self,
        protein_pdb: str,
        ligand_smiles: str,
        center: Optional[Any] = None,
        box_size: Optional[Any] = None,
        exhaustiveness: int = 8,
        num_modes: int = 9,
    ) -> Dict[str, Any]:
        """Run AutoDock Vina docking and return scored poses.

        Parameters
        ----------
        protein_pdb : str
            Path to the receptor PDB file.
        ligand_smiles : str
            SMILES string for the molecule to dock.
        center, box_size : tuple(float,float,float) | None
            Docking grid centre and dimensions (Å).
            If None, ``detect_binding_site()`` is called first.
        exhaustiveness : int
            Vina exhaustiveness.
        num_modes : int
            Max poses to return.

        Returns
        -------
        dict
            ``{success: True, poses: [...], best_score_kcal: ..., ...}``
        """
        if self.docking_engine is None:
            return {"success": False, "error": "docking_engine not available"}
        try:
            return self.docking_engine.dock_molecule(
                protein_pdb=protein_pdb,
                ligand_smiles=ligand_smiles,
                center=center,
                box_size=box_size or (20.0, 20.0, 20.0),
                exhaustiveness=exhaustiveness,
                num_modes=num_modes,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def proactive_problem_identification(
        self,
        context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Phase 4 (MPI-UOS): Autonomous Discovery for Alchemist.
        
        Proactively identify:
        - QSAR model overfitting
        - Docking pose inconsistencies
        - ADMET liability patterns
        - Scaffold toxicity risks
        - Novel chemical space gaps
        
        Returns:
            List of autonomous discoveries
        """
        discoveries = []
        
        # Check for QSAR overfitting
        if "qsar_validation_metrics" in context:
            # TODO: Analyze train vs test performance
            # Flag significant gaps
            pass
        
        # Check for docking inconsistencies
        if "docking_poses" in context:
            # TODO: Compare multiple docking methods
            # Identify conflicting predictions
            pass
        
        # Check for ADMET liabilities
        if "admet_predictions" in context:
            # TODO: Identify systematic toxicity patterns
            # Flag common scaffolds with issues
            pass
        
        if discoveries:
            logger.info(f"💡 Alchemist Autonomous Discoveries: {len(discoveries)}")
            self.autonomous_discoveries.extend(discoveries)
        
        return discoveries

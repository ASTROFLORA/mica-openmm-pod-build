"""
🕵️ Proactive Monitoring Detectors (Phase 6 MPI-UOS)
==================================================

Implements the 6 detector types for autonomous gap detection and research initiation.
"""

from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

class BaseDetector:
    """Base class for all gap detectors."""
    def __init__(self, detector_id: str):
        self.detector_id = detector_id
        
    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Analyze state and return detected gaps."""
        raise NotImplementedError

class KnowledgeGapDetector(BaseDetector):
    """
    Detector 1: Identifies missing knowledge coverage.
    Checks for low confidence in lab reports or missing data.
    """
    def __init__(self):
        super().__init__("knowledge_gap")
        
    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        gaps = []
        # Check for low confidence reports
        lab_reports = state.get("lab_reports", [])
        for report in lab_reports:
            try:
                confidence = float(report.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 1.0
            if confidence < 0.6:
                gaps.append({
                    "type": "low_confidence_finding",
                    "description": f"Low confidence ({report.get('confidence')}) in report {report.get('subtask_id')}",
                    "priority": 0.7,
                    "source": report.get("subtask_id")
                })
        return gaps

class MethodologyGapDetector(BaseDetector):
    """
    Detector 2: Identifies incomplete or inconsistent methodologies.
    Checks if required steps were skipped or failed.
    """
    def __init__(self):
        super().__init__("methodology_gap")
        
    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        gaps = []
        # Check if quality gate was passed
        if not state.get("converged", False) and state.get("iteration_count", 0) > 2:
            gaps.append({
                "type": "convergence_failure",
                "description": "Workflow failed to converge after multiple iterations",
                "priority": 0.9
            })
        return gaps

class DataGapDetector(BaseDetector):
    """
    Detector 3: Dataset sufficiency.
    Checks if enough data points were collected.
    """
    def __init__(self):
        super().__init__("data_gap")
        
    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        gaps = []
        # Example: Check if literature search yielded results
        reports = state.get("lab_reports", [])
        if not reports and state.get("msrp_current_phase", 0) > 2:
             gaps.append({
                "type": "missing_data",
                "description": "No lab reports generated in execution phase",
                "priority": 0.8
            })
        return gaps

class ComputationalGapDetector(BaseDetector):
    """Detector 4: Identifies when computational resources could enhance analysis.

    Flags when:
    - Protein structure is mentioned but no MD simulation was run
    - Docking candidates exist but no scoring/validation was done
    - Trajectory data exists but no advanced analysis (PCA, clustering) was performed
    """
    def __init__(self):
        super().__init__("computational_gap")

    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        gaps = []
        reports = state.get("lab_reports", [])
        query = str(state.get("user_query", "")).lower()

        # Check if protein structure was discussed but no MD simulation was triggered
        has_structure = any(
            "pdb" in str(r).lower() or "structure" in str(r).lower()
            for r in reports
        )
        has_md = any(
            "simulation" in str(r).lower() or "trajectory" in str(r).lower()
            or "rmsd" in str(r).lower()
            for r in reports
        )
        if has_structure and not has_md and (
            "dynamics" in query or "flexibility" in query or "motion" in query
            or "conformational" in query
        ):
            gaps.append({
                "type": "missing_md_simulation",
                "description": (
                    "Protein structure data found but no MD simulation was performed. "
                    "Consider running molecular dynamics to analyze conformational flexibility."
                ),
                "priority": 0.6,
                "suggested_tools": ["mcp_scitoolagent.run_md_simulation"],
            })

        # Check if docking was discussed but no validation scoring
        has_docking_context = any(
            "dock" in str(r).lower() or "binding" in str(r).lower()
            or "ligand" in str(r).lower()
            for r in reports
        )
        has_scoring = any(
            "score" in str(r).lower() or "affinity" in str(r).lower()
            or "ic50" in str(r).lower()
            for r in reports
        )
        if has_docking_context and not has_scoring:
            gaps.append({
                "type": "missing_docking_validation",
                "description": (
                    "Docking/binding context found but no scoring validation. "
                    "Consider ChEMBL bioactivity lookup for experimental comparison."
                ),
                "priority": 0.5,
                "suggested_tools": ["mcp_chembl.search_activities"],
            })

        return gaps

class ExperimentalGapDetector(BaseDetector):
    """Detector 5: Suggests experimental validation when computational-only.

    Flags when:
    - Only computational data exists, no experimental references
    - Predictions lack cross-validation with known experimental data
    """
    def __init__(self):
        super().__init__("experimental_gap")

    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        gaps = []
        reports = state.get("lab_reports", [])

        # Check if we have predictions/computational results but no experimental references
        has_prediction = any(
            "predict" in str(r).lower() or "model" in str(r).lower()
            or "calculated" in str(r).lower() or "computed" in str(r).lower()
            for r in reports
        )
        has_experimental = any(
            "experimental" in str(r).lower() or "measured" in str(r).lower()
            or "crystal" in str(r).lower() or "assay" in str(r).lower()
            or "in vitro" in str(r).lower()
            for r in reports
        )

        if has_prediction and not has_experimental and len(reports) > 1:
            gaps.append({
                "type": "no_experimental_validation",
                "description": (
                    "Analysis appears purely computational with no experimental cross-validation. "
                    "Consider searching PubMed/Semantic Scholar for experimental studies on this target, "
                    "or ChEMBL for existing bioassay data."
                ),
                "priority": 0.65,
                "suggested_tools": [
                    "mcp_pubmed.search_pubmed_key_words",
                    "mcp_chembl.search_activities",
                ],
            })

        return gaps

class TheoreticalGapDetector(BaseDetector):
    """Detector 6: Identifies missing contextual/ontological knowledge.

    Flags when:
    - Protein is analyzed without pathway context
    - Gene is mentioned without interaction network context
    - Disease target lacks mechanism of action context
    """
    def __init__(self):
        super().__init__("theoretical_gap")

    def detect(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        gaps = []
        reports = state.get("lab_reports", [])
        query = str(state.get("user_query", "")).lower()

        # Protein analyzed without pathway context
        has_protein = any(
            "protein" in str(r).lower() or "kinase" in str(r).lower()
            or "receptor" in str(r).lower() or "enzyme" in str(r).lower()
            for r in reports
        )
        has_pathway = any(
            "pathway" in str(r).lower() or "kegg" in str(r).lower()
            or "reactome" in str(r).lower() or "signaling" in str(r).lower()
            for r in reports
        )
        has_network = any(
            "interaction" in str(r).lower() or "network" in str(r).lower()
            or "stringdb" in str(r).lower() or "interactome" in str(r).lower()
            for r in reports
        )

        if has_protein and not has_pathway:
            gaps.append({
                "type": "missing_pathway_context",
                "description": (
                    "Protein target analyzed without pathway context. "
                    "Consider querying KEGG or Reactome for pathway involvement."
                ),
                "priority": 0.55,
                "suggested_tools": [
                    "mcp_kegg.search_pathways",
                    "mcp_reactome.search_pathways",
                ],
            })

        if has_protein and not has_network:
            gaps.append({
                "type": "missing_interaction_context",
                "description": (
                    "Protein analyzed without interaction network context. "
                    "Consider STRING-DB for protein-protein interactions."
                ),
                "priority": 0.5,
                "suggested_tools": ["mcp_stringdb.get_interaction_network"],
            })

        # Disease target without mechanism of action
        if "disease" in query or "therapeutic" in query or "drug" in query:
            has_moa = any(
                "mechanism" in str(r).lower() or "moa" in str(r).lower()
                or "action" in str(r).lower()
                for r in reports
            )
            if not has_moa and len(reports) > 0:
                gaps.append({
                    "type": "missing_mechanism_of_action",
                    "description": (
                        "Disease/drug context detected but no mechanism of action data found. "
                        "Consider OpenTargets for target-disease associations."
                    ),
                    "priority": 0.6,
                    "suggested_tools": ["mcp_opentargets.search_targets"],
                })

        return gaps

class ProactiveSystem:
    """
    Orchestrates all detectors with consecutive-duplicate suppression.
    """
    def __init__(self):
        self.detectors = [
            KnowledgeGapDetector(),
            MethodologyGapDetector(),
            DataGapDetector(),
            ComputationalGapDetector(),
            ExperimentalGapDetector(),
            TheoreticalGapDetector()
        ]
        # Dedup state: track previous scan's trigger types and quality score
        self._prev_trigger_types: set = set()
        self._prev_quality: float = 0.0

    def scan(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        all_gaps = []
        for detector in self.detectors:
            try:
                gaps = detector.detect(state)
                for gap in gaps:
                    gap["detector"] = detector.detector_id
                all_gaps.extend(gaps)
            except Exception as e:
                logger.error(f"Detector {detector.detector_id} failed: {e}")

        # Sort by priority
        all_gaps.sort(key=lambda x: x.get("priority", 0), reverse=True)

        # --- Consecutive-duplicate suppression ---
        current_types = {(g.get("type"), g.get("detector")) for g in all_gaps}
        current_quality = float(state.get("quality_score", 0.0))

        if current_types and current_types == self._prev_trigger_types:
            quality_improved = current_quality > self._prev_quality + 0.01
            if not quality_improved:
                logger.info(
                    f"[PROACTIVE] Suppressing {len(all_gaps)} duplicate triggers "
                    f"(quality {self._prev_quality:.3f} -> {current_quality:.3f}, no improvement)"
                )
                self._prev_quality = current_quality
                return []  # Suppress identical re-fire

        self._prev_trigger_types = current_types
        self._prev_quality = current_quality
        return all_gaps

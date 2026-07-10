"""Continuous improvement agent for detecting pipeline gaps and generating recommendations."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PipelineGapAgent:
    """
    Evaluate AgenticDriver pipeline outputs to identify missing components,
    suggest new integrations, and recommend literature for feature gaps.
    """

    def __init__(
        self,
        documentation_roots: Optional[List[str]] = None,
        research_service: Optional[Any] = None,
    ):
        """
        :param documentation_roots: File paths to scan for TODOs / FIXMEs.
        :param research_service: Optional literature search service for research queries.
        """
        self.documentation_roots = documentation_roots or []
        self.research_service = research_service

    def evaluate(
        self,
        session_payload: Dict[str, Any],
        worker_outputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Assess pipeline execution quality and recommend next steps.

        :param session_payload: Full session context from AgenticDriver.
        :param worker_outputs: Map of worker names to their result dictionaries.
        :return: Analysis with identified_gaps, recommendations, research_query.
        """
        gaps = self._inspect_worker_outputs(worker_outputs)
        doc_issues = self._scan_documentation()
        
        # Fix: Initialize query before conditional block
        query: Optional[str] = None
        research_bundle = None
        
        if self.research_service:
            query = self._build_research_query(session_payload, worker_outputs)
            if query:
                try:
                    research_bundle = self.research_service.research_bundle(query, max_results=5)
                except Exception as exc:
                    logger.warning("Research bundle failed: %s", exc)

        recommendations = self._generate_recommendations(gaps, doc_issues)

        return {
            "identified_gaps": gaps,
            "documentation_issues": doc_issues,
            "recommendations": recommendations,
            "research_query": query,
            "research_results": research_bundle,
        }

    def _inspect_worker_outputs(self, worker_outputs: Dict[str, Any]) -> List[str]:
        """Detect missing pipeline_trace or tiers_executed in worker results."""
        gaps = []

        for worker_name, result in worker_outputs.items():
            if isinstance(result, dict):
                # Alchemist pattern
                if worker_name == "alchemist" and "pipeline_trace" not in result:
                    gaps.append(f"{worker_name}: missing pipeline_trace")
                
                # SMIC pattern
                if worker_name == "smic" and "tiers_executed" not in result:
                    gaps.append(f"{worker_name}: missing tiers_executed")
                
                # BioDynamo pattern
                if worker_name in ("biodynamo_executor", "biodynamo_scaffold", "biodynamo_nlp"):
                    if "workflow_results" not in result and "pipeline_trace" not in result:
                        gaps.append(f"{worker_name}: missing workflow_results or pipeline_trace")
                
                # General quality metrics check
                if "quality_metrics" not in result and "bvs_score" not in result:
                    gaps.append(f"{worker_name}: missing quality_metrics")

        return gaps

    def _scan_documentation(self) -> List[str]:
        """Find TODO/FIXME markers in documentation roots."""
        issues = []
        for root_str in self.documentation_roots:
            root_path = Path(root_str)
            if not root_path.exists():
                continue
            
            for file_path in root_path.rglob("*.md"):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    for marker in ("TODO", "FIXME"):
                        if marker in content:
                            count = content.count(marker)
                            issues.append(f"{file_path.name}: {count} {marker} markers")
                except Exception as exc:
                    logger.debug("Failed to scan %s: %s", file_path, exc)

        return issues

    def _build_research_query(
        self,
        session_payload: Dict[str, Any],
        worker_outputs: Dict[str, Any],
    ) -> Optional[str]:
        """Construct a literature search query based on session context."""
        user_prompt = session_payload.get("user_prompt", "")
        assigned_workers = session_payload.get("assigned_workers", [])
        
        # Extract key terms from prompt
        key_terms = []
        if "molecular dynamics" in user_prompt.lower():
            key_terms.append("molecular dynamics simulation")
        if "embedding" in user_prompt.lower():
            key_terms.append("protein embeddings")
        if "biodynamo" in user_prompt.lower():
            key_terms.append("biomolecular simulation")
        
        # Add worker-specific terms
        if "alchemist" in assigned_workers:
            key_terms.append("drug discovery pipeline")
        if "smic" in assigned_workers:
            key_terms.append("structural monitoring")
        
        if not key_terms:
            return None
        
        return " ".join(key_terms)

    def _generate_recommendations(
        self,
        gaps: List[str],
        doc_issues: List[str],
    ) -> List[str]:
        """Generate actionable recommendations based on detected gaps."""
        recommendations = []
        
        if gaps:
            recommendations.append(
                f"Address {len(gaps)} missing metadata fields in worker outputs to enable quality assessment"
            )
        
        if doc_issues:
            recommendations.append(
                f"Resolve {len(doc_issues)} documentation issues (TODO/FIXME markers) before production deployment"
            )
        
        if not gaps and not doc_issues:
            recommendations.append("Pipeline execution is complete and meets quality standards")
        
        return recommendations

    def export(self, analysis: Dict[str, Any], output_dir: Path) -> Path:
        """Persist analysis to disk for audit trails."""
        output_dir.mkdir(parents=True, exist_ok=True)
        payload_path = output_dir / "pipeline_gap_analysis.json"
        with payload_path.open("w", encoding="utf-8") as handle:
            json.dump(analysis, handle, indent=2)
        return payload_path

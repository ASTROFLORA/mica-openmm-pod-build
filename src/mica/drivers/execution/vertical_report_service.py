"""Vertical report execution helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Dict, Optional


def run_vertical_report(*, args: Dict[str, Any], state: Optional[Dict[str, Any]]) -> str:
    try:
        query = args.get("query", "")
        output_format = args.get("format", "both")
        if not state or not (state.get("synthesis") or state.get("artifact_bundle")):
            return json.dumps(
                {"error": "No bibliotecario synthesis available. Run consult_bibliotecario first."}
            )

        from mica.infrastructure.literature.literature_artifact_bundle import (
            build_vertical_report_sections_from_bundle,
        )

        sections = build_vertical_report_sections_from_bundle(
            dict(state.get("artifact_bundle") or {}),
            fallback_synthesis=str(state.get("role_synthesis") or state.get("synthesis") or ""),
        )
        if not sections:
            sections = [{"heading": "Synthesis", "text": state.get("synthesis", "")}]

        from mica.reports._docx_shared import PaperRecord

        paper_records = [
            PaperRecord.from_normalized_citation(citation)
            for citation in (state.get("normalized_citations") or [])
        ]

        from mica.sota_reports.sota_pipeline import SOTAPipeline
        from mica.timeline_reports.timeline_pipeline import TimelinePipeline

        sota_result = SOTAPipeline().run(
            sections=sections,
            topic=query or state.get("query", ""),
            output_format=output_format,
            paper_records=paper_records,
        )
        timeline_result = TimelinePipeline().run(
            sections=sections,
            entity_scope=query or state.get("query", ""),
            output_format=output_format,
            paper_records=paper_records,
        )

        result_payload = {
            "query": query,
            "format": output_format,
            "sota_claims": sota_result.claim_count,
            "timeline_events": timeline_result.event_count,
            "sota_summary": sota_result.summary,
            "timeline_summary": timeline_result.summary,
            "sota_docx_bytes_len": len(sota_result.docx_bytes) if sota_result.docx_bytes else 0,
            "timeline_docx_bytes_len": len(timeline_result.docx_bytes) if timeline_result.docx_bytes else 0,
        }
        if output_format == "both":
            result_payload["sota_markdown"] = sota_result.markdown[:2000]
            result_payload["timeline_markdown"] = timeline_result.markdown[:2000]

        return json.dumps(result_payload, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
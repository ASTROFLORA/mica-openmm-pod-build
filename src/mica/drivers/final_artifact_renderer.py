from __future__ import annotations

from typing import Any, Dict, List


class FinalArtifactRenderer:
    """Render normalized final-result artifacts into paper-grade markdown."""

    def render(
        self,
        *,
        query: str,
        summary: str,
        paper: Dict[str, Any],
        claims: List[Dict[str, Any]],
        sources: List[Dict[str, Any]],
        output_mode: str,
        metrics: Dict[str, Any],
        run_status: str,
        degradation_flags: List[str],
        capabilities_unavailable: List[str],
        fallbacks_used: List[str],
        failure_records: List[Dict[str, Any]],
        uncertainty_summary: str,
    ) -> Dict[str, str]:
        source_lookup = {
            str(source.get("source_id") or "").strip(): source
            for source in sources
            if isinstance(source, dict) and str(source.get("source_id") or "").strip()
        }

        abstract = str(paper.get("abstract") or summary or f"MICA addressed: {query}").strip()
        background = str(paper.get("background") or f"Question addressed: {query}").strip()
        methods = str(paper.get("methods") or "Evidence synthesis over available runtime outputs.").strip()

        sections: List[str] = [
            "## Abstract",
            abstract,
            "## Background and question framing",
            background,
            "## Evidence base",
            self._render_evidence_base(methods=methods, sources=sources, claims=claims, metrics=metrics),
            "## Findings",
            self._render_findings(
                claims=claims,
                findings=paper.get("findings"),
                source_lookup=source_lookup,
                output_mode=output_mode,
            ),
            "## Interpretation",
            self._render_interpretation(
                output_mode=output_mode,
                metrics=metrics,
                run_status=run_status,
                claims=claims,
                sources=sources,
                uncertainty_summary=uncertainty_summary,
            ),
            "## Cognitive competition",
            self._render_cognitive_competition(metrics=metrics),
            "## Thermodynamic routing",
            self._render_thermodynamic_routing(metrics=metrics),
            "## Promotion decision",
            self._render_promotion_decision(metrics=metrics),
            "## Limitations",
            self._render_limitations(
                paper=paper,
                degradation_flags=degradation_flags,
                capabilities_unavailable=capabilities_unavailable,
                fallbacks_used=fallbacks_used,
                uncertainty_summary=uncertainty_summary,
            ),
            "## Failure ledger",
            self._render_failure_ledger(failure_records=failure_records),
            "## Next actions",
            self._render_next_steps(paper=paper, query=query),
            "## References",
            self._render_references(sources=sources, output_mode=output_mode),
        ]

        markdown = "\n\n".join(section for section in sections if section and str(section).strip())
        return {
            "abstract": abstract,
            "paper_markdown": markdown.strip(),
        }

    def _render_evidence_base(
        self,
        *,
        methods: str,
        sources: List[Dict[str, Any]],
        claims: List[Dict[str, Any]],
        metrics: Dict[str, Any],
    ) -> str:
        source_types = sorted(
            {
                str(source.get("source_type") or "unknown").strip()
                for source in sources
                if isinstance(source, dict)
            }
        )
        relevance_coverage = float(metrics.get("claim_relevance_coverage", 0.0) or 0.0)
        raw_coverage = float(metrics.get("raw_claim_to_source_coverage", 0.0) or 0.0)
        relevant_source_count = int(metrics.get("relevant_source_count", 0) or 0)
        source_preview = []
        preview_candidates = [source for source in sources if str(source.get("relevance_status") or "") != "irrelevant"] or sources
        for source in preview_candidates[:4]:
            if not isinstance(source, dict):
                continue
            source_preview.append(self._source_link(source))

        lines = [
            f"Methods/data used: {methods}",
            f"This run retained {len(sources)} source record(s), of which {relevant_source_count} are currently relevant for scientific closure, across {len(source_types)} source type(s) and {len(claims)} normalized claim object(s).",
            f"Claim relevance coverage is {relevance_coverage:.0%}.",
        ]
        if abs(raw_coverage - relevance_coverage) >= 0.01:
            lines.append(f"Raw attachment coverage is {raw_coverage:.0%}, but relevance-aware coverage is used for epistemic scoring.")
        if source_types:
            lines.append(f"Source types: {', '.join(source_types)}.")
        if source_preview:
            lines.append(f"Representative official records: {', '.join(source_preview)}.")
        return "\n\n".join(lines)

    def _render_findings(
        self,
        *,
        claims: List[Dict[str, Any]],
        findings: Any,
        source_lookup: Dict[str, Dict[str, Any]],
        output_mode: str,
    ) -> str:
        paragraphs: List[str] = []
        for claim in claims[:8]:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            if not text:
                continue
            strength = str(claim.get("strength") or "suggestive").strip()
            confidence = claim.get("confidence")
            section = str(claim.get("section") or "finding").strip()
            citation_suffix = self._inline_citations(
                claim.get("source_ids") or [],
                source_lookup,
                output_mode=output_mode,
            )
            if isinstance(confidence, (int, float)):
                qualifier = f"{section}: {text} This claim is currently classified as {strength} (confidence {float(confidence):.2f})."
            else:
                qualifier = f"{section}: {text} This claim is currently classified as {strength}."
            if citation_suffix:
                qualifier = f"{qualifier} {citation_suffix}"
            paragraphs.append(qualifier.strip())

        if paragraphs:
            return "\n\n".join(paragraphs)

        if isinstance(findings, list) and findings:
            fallback_lines: List[str] = []
            for item in findings[:6]:
                if isinstance(item, dict):
                    label = str(item.get("subtask") or "finding").strip()
                    text = item.get("findings")
                    if isinstance(text, list):
                        text = "; ".join(str(x) for x in text[:3])
                    fallback_lines.append(f"- {label}: {str(text or item.get('summary') or '').strip()}")
                else:
                    fallback_lines.append(f"- {str(item).strip()}")
            if fallback_lines:
                return "\n".join(fallback_lines)

        return "MICA completed the run, but the current artifact does not yet contain sufficiently structured findings for a richer manuscript-style findings section."

    def _render_interpretation(
        self,
        *,
        output_mode: str,
        metrics: Dict[str, Any],
        run_status: str,
        claims: List[Dict[str, Any]],
        sources: List[Dict[str, Any]],
        uncertainty_summary: str,
    ) -> str:
        supported_claims = sum(
            1
            for claim in claims
            if isinstance(claim, dict) and str(claim.get("strength") or "").strip() in {"observed", "supported"}
        )
        if output_mode == "evidence_backed_answer":
            status_line = (
                f"The current synthesis is evidence-backed: {supported_claims} claim(s) are marked observed/supported and {len(sources)} official source record(s) are attached."
            )
        elif output_mode == "investigative_scaffold":
            status_line = (
                "This artifact is an investigative scaffold: it preserves domain structure and next-step utility, but it does not claim scientific closure."
            )
        elif output_mode == "calibrated_abstention":
            status_line = (
                "This artifact is a calibrated abstention: the system refused unsupported closure and limited itself to explicit evidence-state diagnosis and minimal next steps."
            )
        elif output_mode == "misleading_support_blocked":
            status_line = (
                "This artifact was blocked from evidence-backed interpretation because attached sources do not materially support the claim presentation."
            )
        elif run_status == "degraded":
            status_line = (
                "The current synthesis should be interpreted cautiously because the runtime reported degraded execution, which means some conclusions may rest on partial tooling, partial provenance, or fallback pathways."
            )
        else:
            status_line = (
                "The current synthesis did not complete cleanly enough to treat all conclusions as established; use the references and limitations sections before acting on this output."
            )
        return f"{status_line}\n\n{uncertainty_summary}"

    def _render_limitations(
        self,
        *,
        paper: Dict[str, Any],
        degradation_flags: List[str],
        capabilities_unavailable: List[str],
        fallbacks_used: List[str],
        uncertainty_summary: str,
    ) -> str:
        items: List[str] = []
        for limitation in paper.get("limitations") or []:
            text = str(limitation or "").strip()
            if text:
                items.append(text)
        if degradation_flags:
            items.append("Runtime degradation flags: " + ", ".join(str(flag) for flag in degradation_flags))
        if capabilities_unavailable:
            items.append(
                "Capabilities unavailable during execution: "
                + ", ".join(str(item) for item in capabilities_unavailable)
            )
        if fallbacks_used:
            items.append("Fallback paths used: " + ", ".join(str(item) for item in fallbacks_used))
        if uncertainty_summary and uncertainty_summary not in items:
            items.append(uncertainty_summary)
        if not items:
            items.append("No explicit limitations were captured for this run; absence of limitations should not be treated as absence of risk.")
        return "\n".join(f"- {item}" for item in items)

    def _render_failure_ledger(self, *, failure_records: List[Dict[str, Any]]) -> str:
        if not failure_records:
            return "- No structured causal failures were preserved for this run."
        lines: List[str] = []
        for record in failure_records[:12]:
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or record.get("tool") or "runtime").strip()
            failure_reason = str(record.get("failure_reason") or "UNKNOWN").strip()
            message = str(record.get("message") or record.get("note") or "").strip()
            attempted_fix = str(record.get("attempted_fix") or "").strip()
            unresolved = str(record.get("unresolved") or "").strip()
            line = f"- {source}: {failure_reason}. {message}"
            if attempted_fix:
                line += f" Attempted fix: {attempted_fix}."
            if unresolved:
                line += f" Remaining gap: {unresolved}."
            lines.append(line.strip())
        return "\n".join(lines) if lines else "- No structured causal failures were preserved for this run."

    def _render_cognitive_competition(self, *, metrics: Dict[str, Any]) -> str:
        cognitive_layer = metrics.get("cognitive_layer") if isinstance(metrics.get("cognitive_layer"), dict) else {}
        ach_state = cognitive_layer.get("hypothesis_competition") if isinstance(cognitive_layer.get("hypothesis_competition"), dict) else {}
        critic_pass = cognitive_layer.get("critic_pass") if isinstance(cognitive_layer.get("critic_pass"), dict) else {}

        if not ach_state and not critic_pass:
            return "The cognitive layer did not contribute an explicit ACH or critic verdict for this run."

        lines: List[str] = []
        primary_hypothesis_id = str(ach_state.get("primary_hypothesis_id") or "").strip()
        if primary_hypothesis_id:
            lines.append(f"Primary hypothesis: {primary_hypothesis_id}.")
        lines.append(
            "Competition remains open." if bool(ach_state.get("competition_open")) else "Competition is currently settled enough for a single leading hypothesis."
        )
        contradiction_pressure = ach_state.get("contradiction_pressure")
        if isinstance(contradiction_pressure, (int, float)):
            lines.append(f"Contradiction pressure is {float(contradiction_pressure):.0%}.")
        leading = list(ach_state.get("leading_hypothesis_ids") or [])
        rivals = list(ach_state.get("rival_hypothesis_ids") or [])
        rejected = list(ach_state.get("rejected_hypothesis_ids") or [])
        if leading:
            lines.append(f"Leading hypotheses: {', '.join(str(item) for item in leading)}.")
        if rivals:
            lines.append(f"Rival hypotheses still active: {', '.join(str(item) for item in rivals)}.")
        if rejected:
            lines.append(f"Rejected hypotheses: {', '.join(str(item) for item in rejected)}.")
        critic_status = str(critic_pass.get("status") or "accept").strip()
        lines.append(f"Continuous critic status: {critic_status}.")
        rationale = [str(item).strip() for item in (critic_pass.get("rationale") or []) if str(item).strip()]
        if rationale:
            lines.append(f"Critic rationale: {' '.join(rationale[:2])}")
        if critic_pass.get("retry_recommended"):
            lines.append("The critic recommended reopening execution before treating the synthesis as closed.")
        return "\n\n".join(lines)

    def _render_thermodynamic_routing(self, *, metrics: Dict[str, Any]) -> str:
        route_state = metrics.get("thermodynamic_routing") if isinstance(metrics.get("thermodynamic_routing"), dict) else {}
        if not route_state:
            return "Thermodynamic routing data was not captured for this run."
        if not route_state.get("enabled"):
            return str(route_state.get("note") or "Thermodynamic cognition was disabled for this run.")

        lines = [
            f"Preferred execution path: {str(route_state.get('preferred_execution_path') or 'unknown')}",
            f"Requested path: {str(route_state.get('requested_execution_path') or 'unknown')}",
        ]
        if isinstance(route_state.get("phase"), str) and route_state.get("phase"):
            lines.append(f"Thermodynamic phase: {route_state['phase']}.")
        if isinstance(route_state.get("temperature"), (int, float)):
            lines.append(f"Temperature: {float(route_state['temperature']):.2f}.")
        if isinstance(route_state.get("energy"), (int, float)):
            lines.append(f"Energy: {float(route_state['energy']):.2f}.")
        lines.append(f"Exploration budget: {int(route_state.get('exploration_budget') or 1)}.")
        retry_plan = route_state.get("retry_plan") if isinstance(route_state.get("retry_plan"), dict) else {}
        if retry_plan:
            if retry_plan.get("retried"):
                lines.append(f"One critique-driven retry was executed via {str(retry_plan.get('retry_execution_path') or route_state.get('preferred_execution_path') or 'unknown')}.")
            elif retry_plan.get("retry"):
                lines.append(f"A retry was recommended via {str(retry_plan.get('retry_execution_path') or 'unknown')}, but no completed retry is recorded in this artifact.")
            else:
                lines.append("No thermodynamic retry was executed.")
        note = str(route_state.get("note") or "").strip()
        if note:
            lines.append(note)
        return "\n\n".join(lines)

    def _render_promotion_decision(self, *, metrics: Dict[str, Any]) -> str:
        promotion_ledger = metrics.get("promotion_ledger") if isinstance(metrics.get("promotion_ledger"), dict) else {}
        if not promotion_ledger:
            return "Promotion rationale was not captured for this run."

        lines = [
            "Publication-style interpretation remained eligible." if promotion_ledger.get("publication_ready") else "Publication-style interpretation was blocked.",
            f"Gate status: {'passed' if promotion_ledger.get('gate_passed') else 'blocked'}.",
        ]
        firewall_action = str(promotion_ledger.get("firewall_action") or "accept").strip()
        lines.append(f"Cold evidence firewall action: {firewall_action}.")
        lines.append(
            "Invariant validation passed." if promotion_ledger.get("invariant_passed", True) else "Invariant validation detected unresolved drift."
        )
        block_reasons = [str(item).strip() for item in (promotion_ledger.get("block_reasons") or []) if str(item).strip()]
        if block_reasons:
            lines.append(f"Promotion rationale: {'; '.join(block_reasons)}.")
        note = str(promotion_ledger.get("note") or "").strip()
        if note:
            lines.append(note)
        return "\n\n".join(lines)

    def _render_next_steps(self, *, paper: Dict[str, Any], query: str) -> str:
        next_steps = [str(step or "").strip() for step in (paper.get("next_steps") or []) if str(step or "").strip()]
        if not next_steps:
            next_steps = [
                f"Validate the main claims against the official records cited for the query: {query}.",
                "Inspect contradictory or weakly sourced claims before treating them as established.",
                "Run a deeper tool-backed follow-up if critical claims still depend on fallback synthesis.",
            ]
        return "\n".join(f"- {step}" for step in next_steps)

    def _render_references(self, *, sources: List[Dict[str, Any]], output_mode: str) -> str:
        if not sources:
            return "- No official references were preserved for this run."
        lines: List[str] = []
        for source in sources[:20]:
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or "Untitled source").strip()
            evidence_snippet = str(source.get("evidence_snippet") or "").strip()
            label = self._source_link(source)
            relevance_status = str(source.get("relevance_status") or "").strip()
            if relevance_status:
                label = f"{label} [{relevance_status}]"
            if output_mode == "evidence_backed_answer" and relevance_status == "irrelevant":
                continue
            if evidence_snippet:
                lines.append(f"- {label} — {title}. Evidence: {evidence_snippet}")
            else:
                lines.append(f"- {label} — {title}")
        return "\n".join(lines) if lines else "- No official references were preserved for this run."

    def _inline_citations(
        self,
        source_ids: List[Any],
        source_lookup: Dict[str, Dict[str, Any]],
        *,
        output_mode: str,
    ) -> str:
        rendered = []
        for source_id in source_ids[:3]:
            source = source_lookup.get(str(source_id))
            if source:
                relevance_status = str(source.get("relevance_status") or "").strip()
                if relevance_status == "irrelevant":
                    continue
                rendered.append(self._source_link(source))
        if not rendered:
            return "Evidence link pending." if output_mode == "evidence_backed_answer" else ""
        if output_mode == "evidence_backed_answer":
            return "Supported by " + "; ".join(rendered) + "."
        return "Referenced records: " + "; ".join(rendered) + "."

    def _source_link(self, source: Dict[str, Any]) -> str:
        label = str(
            source.get("display_citation")
            or source.get("source_id")
            or source.get("title")
            or "source"
        ).strip()
        url = str(source.get("official_url") or "").strip()
        if url:
            return f"[{label}]({url})"
        return label
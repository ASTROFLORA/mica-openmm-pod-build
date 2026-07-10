"""
Route Card Service — extraction from AgenticDriver

Encapsulates route-card policy generation and query classification logic.
Extracted from src/mica/drivers/agentic_driver.py to reduce driver surface.
"""

import uuid
from typing import Any, Dict, List, Sequence, Tuple

from mica_q.adapters.sandbox_adapter import query_targets_mica_q_sandbox


class RouteCardService:
    """Generates and manages route-card decisions for queries."""

    def route_card_id_for_query(self, query: str) -> str:
        """Generate deterministic route card ID from query."""
        digest = uuid.uuid5(uuid.NAMESPACE_URL, str(query or "").strip() or "mica-empty-query")
        return f"route_card::{digest.hex[:16]}"

    def query_requires_scientific_route_card(self, query: str, intent_tags: Sequence[str]) -> bool:
        """Check if query requires scientific audit closure."""
        query_folded = str(query or "").casefold()
        if any(str(tag or "").strip() == "literature_search" for tag in (intent_tags or [])):
            audit_markers = (
                "audit", "evidence", "citation", "citations", "closure", "critique",
                "mechanism", "mechanistic", "gap", "gaps", "review",
            )
            if any(marker in query_folded for marker in audit_markers):
                return True
        return False

    def query_declares_docs_authority(self, query: str) -> bool:
        """Check if query explicitly declares docs authority routing."""
        query_folded = str(query or "").casefold()
        if not query_folded:
            return False
        declaration_markers = (
            ".mica/external_docs/", "service_routing", "service routing", "docs authority",
            "authoritative docs", "official docs", "consumed docs", "using docs", "routed via",
            "mirror:", "authority:", "http://", "https://",
        )
        return any(marker in query_folded for marker in declaration_markers)

    def query_requires_docs_authority(
        self,
        query: str,
        *,
        intent_tags: Sequence[str],
        planned_tool_names: Sequence[str],
        visible_tool_names: Sequence[str],
    ) -> Tuple[bool, str, List[str]]:
        """Check if query requires documented service authority."""
        query_folded = str(query or "").casefold()
        if not query_folded:
            return False, "", []

        depth_markers = (
            "troubleshoot", "troubleshooting", "debug", "debugging", "diagnose", "diagnostic",
            "incident", "outage", "repair", "fix", "failure", "broken", "not working",
            "regression", "integrate", "integration", "blueprint", "design", "implement",
            "implementation", "configure", "deployment", "production", "runtime",
        )
        if not any(marker in query_folded for marker in depth_markers):
            return False, "", []

        external_surface_markers = {
            "railway": "Railway", "neon": "Neon", "timescale": "Timescale", "tiger": "Tiger CLI",
            "google cloud": "Google Cloud", "cloud run": "Cloud Run", "gcs": "Google Cloud Storage",
            "runpod": "RunPod", "vast": "Vast.ai", "modal": "Modal", "milvus": "Milvus", "zilliz": "Zilliz",
            "uniprot": "UniProt", "openalex": "OpenAlex", "semantic scholar": "Semantic Scholar",
            "europe pmc": "Europe PMC", "pubmed": "PubMed", "crossref": "Crossref", "unpaywall": "Unpaywall",
            "clerk": "Clerk", "molstar": "Molstar", "mdanalysis": "MDAnalysis", "openmm": "OpenMM",
            "openbabel": "OpenBabel", "rdkit": "RDKit", "oauth": "OAuth", "jwks": "JWKS", "redis": "Redis",
        }
        surface_hits = [label for marker, label in external_surface_markers.items() if marker in query_folded]

        routed_terms = {
            str(item).strip().casefold()
            for item in list(intent_tags or []) + list(planned_tool_names or []) + list(visible_tool_names or [])
            if str(item).strip()
        }
        if "search_literature" in routed_terms and any(
            marker in query_folded for marker in ("provider", "providers", "full-text", "full text", "api", "apis")
        ):
            surface_hits.extend(["Literature APIs"])

        deduped_hits = list(dict.fromkeys(surface_hits))
        if not deduped_hits:
            return False, "", []

        return (
            True,
            "Deep work touches vendor/platform/API-backed surfaces that require routed docs authority first.",
            deduped_hits,
        )

    def query_prefers_federated_retrieve(
        self,
        query: str,
        *,
        intent_tags: Sequence[str],
        visible_tool_names: Sequence[str],
    ) -> Tuple[bool, str]:
        """Check if query benefits from federated retrieval first."""
        if "federated_retrieve" not in {str(name or "").strip() for name in (visible_tool_names or [])}:
            return False, ""

        query_folded = str(query or "").casefold()
        group_hits = {
            "live": any(marker in query_folded for marker in ("session", "feed", "cue", "tombstone", "progress", "recent", "overlap")),
            "durable": any(marker in query_folded for marker in ("memory", "mempalace", "zilliz", "history", "historical", "prior", "previous")),
            "structural": any(marker in query_folded for marker in ("graph", "seam", "route card", "agentic_driver", "architecture", "orchestration", "driver")),
            "audit": any(marker in query_folded for marker in ("audit", "drift", "parity", "runtime", "truth", "governance")),
        }
        if any(str(tag or "").strip() == "literature_search" for tag in (intent_tags or [])):
            group_hits["audit"] = True
        matched_groups = [label for label, matched in group_hits.items() if matched]
        if len(matched_groups) < 2:
            return False, ""
        return True, f"Query spans {', '.join(matched_groups)} context; start with federated_retrieve before narrower surfaces."

    def extract_explicit_tool_mentions(
        self,
        query: str,
        *,
        visible_tool_names: Sequence[str],
    ) -> List[str]:
        """Extract tool names explicitly mentioned in query."""
        query_folded = str(query or "").casefold()
        if not query_folded:
            return []
        return [
            str(name).strip()
            for name in visible_tool_names
            if str(name).strip() and str(name).casefold() in query_folded
        ]

    def build_route_card(self, *, query: str, routing_meta: Dict[str, Any]) -> Dict[str, Any]:
        """Build complete route card for query."""
        from mica.drivers.routing.product_intent_classifier import (
            ProductIntentDecision,
            classify_product_intent,
        )

        intent_tags = [str(item).strip() for item in list(routing_meta.get("intent_tags") or []) if str(item).strip()]
        planned_tools = list(dict.fromkeys(
            str(item).strip() for item in list(routing_meta.get("planned_tool_names") or []) if str(item).strip()
        ))
        product_decision_raw = routing_meta.get("product_intent_decision")
        if isinstance(product_decision_raw, ProductIntentDecision):
            product_decision = product_decision_raw
        elif isinstance(product_decision_raw, dict) and product_decision_raw:
            product_decision = classify_product_intent(query)
        else:
            product_decision = classify_product_intent(query)
        product_payload = product_decision.to_dict()
        constraints = product_decision.constraints
        product_first = product_decision.product_intent != "scientific_deferred"

        requires_scientific_closure = (
            False
            if product_first
            else self.query_requires_scientific_route_card(query, intent_tags)
        )
        visible_tool_names = [str(item).strip() for item in list(routing_meta.get("visible_tool_names") or []) if str(item).strip()]
        required_capabilities = list(dict.fromkeys(intent_tags))
        required_closure_stages: List[str] = []
        required_tool_names: List[str] = []
        blocked_capabilities: List[Dict[str, Any]] = []
        no_tool_justification = ""

        if constraints.no_tool:
            planned_tools = []
            required_tool_names = []
            no_tool_justification = (
                "Driver constitution: explicit no_tool / product_interview constraint; "
                "planned tools cleared before scientific routing."
            )
            blocked_capabilities.append(
                {
                    "mechanism": "DRIVER_CONSTRAINT_NO_TOOL",
                    "detail": no_tool_justification,
                    "capability_id": "driver.constraints.no_tool",
                    "severity": "error",
                    "missing_dependencies": [],
                }
            )
        if constraints.no_mutation:
            blocked_capabilities.append(
                {
                    "mechanism": "DRIVER_CONSTRAINT_NO_MUTATION",
                    "detail": "Driver constitution: no_mutation — write-side-effect tools blocked.",
                    "capability_id": "driver.constraints.no_mutation",
                    "severity": "error",
                    "missing_dependencies": [],
                }
            )

        if requires_scientific_closure:
            required_capabilities = list(dict.fromkeys(
                required_capabilities + ["literature_search", "claim.review.peer", "vertical_synthesis"]
            ))
            required_closure_stages = ["evidence_acquisition", "critique", "vertical_synthesis"]
            required_tool_names = ["consult_bibliotecario", "request_peer_review", "generate_vertical_report"]
            planned_tools = list(dict.fromkeys(planned_tools + required_tool_names))
            if not planned_tools:
                no_tool_justification = (
                    "Route authority requires evidence acquisition, critique, and vertical synthesis, "
                    "but no tools were planned for this scientific lane."
                )

        mica_q_preferred_tools = {
            "consult_bibliotecario", "knowledge_overview_pipeline", "literature_research_report", "run_mica_q_sandbox",
        }
        routed_tool_names = {
            str(item).strip()
            for key in ("planned_tool_names", "selected_tool_names", "visible_tool_names")
            for item in list(routing_meta.get(key) or [])
            if str(item).strip()
        }
        prefer_mica_q_sandbox = bool(
            query_targets_mica_q_sandbox(query)
            and "run_mica_q_sandbox" in set(visible_tool_names)
        )
        if prefer_mica_q_sandbox:
            planned_tools = list(dict.fromkeys(["run_mica_q_sandbox"] + planned_tools))
        prefer_federated_retrieve, fast_path_reason = self.query_prefers_federated_retrieve(
            query, intent_tags=intent_tags, visible_tool_names=sorted(routed_tool_names),
        )
        if prefer_federated_retrieve:
            planned_tools = list(dict.fromkeys(["federated_retrieve"] + planned_tools))

        docs_authority_required, docs_authority_reason, docs_surface_hits = self.query_requires_docs_authority(
            query, intent_tags=intent_tags, planned_tool_names=planned_tools, visible_tool_names=visible_tool_names,
        )
        docs_authority_declared = self.query_declares_docs_authority(query)
        if docs_authority_required:
            required_capabilities = list(dict.fromkeys(required_capabilities + ["docs.authority.declared"]))
            required_closure_stages = list(dict.fromkeys(["docs_authority"] + required_closure_stages))
            if not docs_authority_declared:
                detail = (
                    "Deep vendor/platform/API troubleshooting is blocked until docs authority is declared. "
                    "Route through .mica/external_docs/SERVICE_ROUTING.md and declare either a local mirror path or "
                    "an authoritative web/docs URL plus the concrete MICA service or CLI in use."
                )
                if docs_surface_hits:
                    detail += f" Triggered surfaces: {', '.join(docs_surface_hits)}."
                blocked_capabilities.append(
                    {
                        "mechanism": "DOCS_AUTHORITY_DECLARATION_REQUIRED",
                        "detail": detail,
                        "capability_id": "docs.authority.declared",
                        "severity": "error",
                        "missing_dependencies": [".mica/external_docs/SERVICE_ROUTING.md"],
                    }
                )

        if constraints.no_tool:
            # Re-assert after scientific/docs enrichment paths.
            planned_tools = []
            required_tool_names = []
            prefer_federated_retrieve = False
            prefer_mica_q_sandbox = False
            fast_path_reason = ""

        lane_class = (
            product_decision.product_lane_class
            if product_first
            else ("scientific_audit" if requires_scientific_closure else "general")
        )
        authority = (
            "mandatory"
            if (constraints.no_tool or constraints.no_mutation or requires_scientific_closure or docs_authority_required)
            else "advisory"
        )

        return {
            "route_card_id": self.route_card_id_for_query(query),
            "authority": authority,
            "lane_class": lane_class,
            "query": str(query or ""),
            "required": bool(
                constraints.no_tool
                or constraints.no_mutation
                or requires_scientific_closure
                or docs_authority_required
            ),
            "prefer_mica_q": (
                False
                if constraints.no_tool
                else (bool(routed_tool_names & mica_q_preferred_tools) or prefer_mica_q_sandbox)
            ),
            "required_capabilities": required_capabilities,
            "required_closure_stages": required_closure_stages,
            "required_tool_names": required_tool_names,
            "planned_tools": planned_tools,
            "blocked_capabilities": blocked_capabilities,
            "intent_tags": intent_tags,
            "prefer_federated_retrieve": prefer_federated_retrieve,
            "fast_path_reason": fast_path_reason,
            "no_tool_justification": no_tool_justification,
            "product_intent": product_decision.product_intent,
            "product_lane_class": product_decision.product_lane_class,
            "driver_constraints": constraints.to_dict(),
            "execution_mode_hint": product_decision.execution_mode_hint,
            "product_intent_decision": product_payload,
            "docs_authority": {
                "required": False if constraints.no_tool else docs_authority_required,
                "declared": docs_authority_declared,
                "reason": docs_authority_reason,
                "surface_focus": docs_surface_hits,
            },
        }

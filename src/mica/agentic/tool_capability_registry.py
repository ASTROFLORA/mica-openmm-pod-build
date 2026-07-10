from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, Iterable, Mapping, Tuple

from mica.agentic.backend_command_manifest import (
    manifest_entries_by_tool_alias,
    iter_manifest_entries,
)
from mica.tools_authority.tool_alias_registry import canonical_tool_name_for_command


@dataclass(frozen=True)
class ToolCapabilitySpec:
    tool_name: str
    surface: str
    capability_mode: str
    route_authority: str = "optional"
    closure_stage: str = ""
    sdk_group: str = "unassigned"
    failure_mode: str = "fail_closed"
    cost_class: str = "standard"
    transport_modes: Tuple[str, ...] = ()
    protocol_tags: Tuple[str, ...] = ()
    required_backend_workers: Tuple[str, ...] = ()
    required_external_hosts: Tuple[str, ...] = ()
    min_available_hosts: int = 0
    requires_sandbox: bool = False
    placeholder_policy: str = "forbidden"
    allow_local_only: bool = True
    requires_provider: bool = False
    protocol_eligible: bool = False
    gog_eligible: bool = False
    input_schema: Dict[str, Any] | None = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _spec(
    tool_name: str,
    *,
    surface: str,
    capability_mode: str,
    route_authority: str = "optional",
    closure_stage: str = "",
    sdk_group: str = "unassigned",
    failure_mode: str = "fail_closed",
    cost_class: str = "standard",
    transport_modes: Iterable[str] = (),
    required_backend_workers: Iterable[str] = (),
    protocol_tags: Iterable[str] = (),
    required_external_hosts: Iterable[str] = (),
    min_available_hosts: int = 0,
    requires_sandbox: bool = False,
    placeholder_policy: str = "forbidden",
    allow_local_only: bool = True,
    requires_provider: bool = False,
    protocol_eligible: bool = False,
    gog_eligible: bool = False,
    input_schema: Dict[str, Any] | None = None,
    notes: str = "",
) -> ToolCapabilitySpec:
    return ToolCapabilitySpec(
        tool_name=tool_name,
        surface=surface,
        capability_mode=capability_mode,
        route_authority=str(route_authority or "optional").strip() or "optional",
        closure_stage=str(closure_stage or "").strip(),
        sdk_group=str(sdk_group or "unassigned").strip() or "unassigned",
        failure_mode=str(failure_mode or "fail_closed").strip() or "fail_closed",
        cost_class=str(cost_class or "standard").strip() or "standard",
        transport_modes=tuple(str(item).strip() for item in transport_modes if str(item).strip()),
        protocol_tags=tuple(str(item).strip() for item in protocol_tags if str(item).strip()),
        required_backend_workers=tuple(str(item).strip() for item in required_backend_workers if str(item).strip()),
        required_external_hosts=tuple(str(item).strip() for item in required_external_hosts if str(item).strip()),
        min_available_hosts=max(0, int(min_available_hosts)),
        requires_sandbox=bool(requires_sandbox),
        placeholder_policy=placeholder_policy,
        allow_local_only=bool(allow_local_only),
        requires_provider=bool(requires_provider),
        protocol_eligible=bool(protocol_eligible),
        gog_eligible=bool(gog_eligible),
        input_schema=deepcopy(input_schema) if input_schema else None,
        notes=notes,
    )


def _schema(
    properties: Mapping[str, Any],
    *,
    required: Iterable[str] = (),
    additional_properties: bool = False,
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": deepcopy(dict(properties)),
        "required": [str(item) for item in required if str(item).strip()],
        "additionalProperties": bool(additional_properties),
    }


_LMP_STATE_ID_QUERY_RE = re.compile(
    r"\blmp:[^:\s]+:[A-Za-z0-9]+:[^:\s]+:[^:\s]+\b",
    re.IGNORECASE,
)


def infer_lmp_state_query_tool_names(
    query: str,
    *,
    available_tool_names: Iterable[str],
) -> Tuple[str, ...]:
    """Return bounded LMP state tools that match the query's intent.

    This is a planner-side hint for queries that already reference a canonical
    LMP ``state_id``. It keeps residue, pair, and AFDB-vs-PDB comparison asks on
    the bounded ``state_id`` surfaces instead of falling back to broader tools.
    """
    query_text = str(query or "")
    lowered = query_text.lower()
    if not lowered:
        return ()
    if "state_id" not in lowered and _LMP_STATE_ID_QUERY_RE.search(query_text) is None:
        return ()

    visible = {
        str(name).strip()
        for name in available_tool_names
        if str(name).strip()
    }
    if not visible:
        return ()

    wants_pair = any(
        token in lowered
        for token in ("pairwise", "pair", "pairs", "correlation", "correlated", "coupling")
    )
    wants_residue = not wants_pair and any(
        token in lowered
        for token in ("residue", "residues", "position", "positions", "chain", "chains")
    )
    wants_comparison = any(
        token in lowered
        for token in ("ledger", "overlap", "coverage", "afdb", "alphafold", "pdb")
    )
    wants_receipt = any(
        token in lowered
        for token in ("receipt", "catalog", "pocket", "visual", "visuals")
    )
    wants_dynamics = any(
        token in lowered
        for token in ("dynamic", "dynamics", "statistics", "rmsf", "fluctuation", "mobility")
    )

    ordered: list[str] = []
    if wants_pair:
        ordered.append("get_lmp_pair_dynamic_statistics")
    if wants_residue:
        ordered.append("get_lmp_residue_dynamic_statistics")
    if wants_comparison:
        ordered.extend([
            "get_lmp_structure_comparison_ledger",
            "get_lmp_state_receipt",
        ])
    if wants_dynamics and not (wants_pair or wants_residue):
        ordered.append("get_lmp_dynamic_statistics")
    if wants_receipt and "get_lmp_state_receipt" not in ordered:
        ordered.append("get_lmp_state_receipt")
    if not ordered:
        ordered.append("get_lmp_state_receipt")

    selected: list[str] = []
    seen: set[str] = set()
    for tool_name in ordered:
        if tool_name in visible and tool_name not in seen:
            seen.add(tool_name)
            selected.append(tool_name)
    return tuple(selected)


FEED_INPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "scroll_agent_feed": _schema(
        {
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            "topic": {"type": "string"},
            "agent_id": {"type": "string"},
            "post_type": {"type": "string"},
            "since": {"type": "string"},
        }
    ),
    "publish_cue": _schema(
        {
            "agent_id": {"type": "string", "description": "Identifier of the posting agent."},
            "post_type": {"type": "string", "default": "cue"},
            "topic": {"type": "string", "default": "general"},
            "title": {"type": "string", "description": "Canonical short headline (<=500 chars)."},
            "body": {"type": "string", "description": "Canonical body text (<=20000 chars)."},
            "intent": {"type": "string", "description": "Backward-compatible alias for title."},
            "content": {"type": "string", "description": "Backward-compatible alias for body."},
            "parent_id": {"type": "string"},
            "biological_context": {"type": "string"},
            "target_agents": {"type": "string"},
            "evidence": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Backward-compatible evidence list used by harness/skills.",
            },
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "session_id": {"type": "string"},
            "idempotency_key": {
                "type": "string",
                "description": "Optional dedupe token for retry-safe publish_cue calls.",
            },
            "graph_updated": {"type": "boolean"},
            "memory_updated": {"type": "boolean"},
            "files_touched": {"type": "string"},
            "context_questions_answered": {"type": "string"},
            "metadata": {"type": "object", "additionalProperties": True},
        },
        required=("agent_id",),
    ),
    "open_session_signature": _schema(
        {
            "agent_id": {"type": "string"},
            "task_description": {"type": "string"},
            "context_questions": {"type": "string"},
            "files_under_review": {"type": "string"},
            "current_situation": {"type": "string"},
            "mission": {"type": "string", "description": "Backward-compatible alias for task_description."},
        },
        required=("agent_id", "task_description", "context_questions"),
    ),
    "update_session_progress": _schema(
        {
            "session_id": {"type": "string"},
            "agent_id": {"type": "string"},
            "progress_notes": {"type": "string"},
            "current_situation": {"type": "string"},
            "next_actions": {"type": "string"},
            "files_touched_so_far": {"type": "string"},
            "graph_updated": {"type": "boolean"},
            "memory_updated": {"type": "boolean"},
            "milestone": {"type": "string", "description": "Backward-compatible alias for progress_notes."},
            "evidence": {"type": "array", "items": {"type": "string"}, "description": "Backward-compatible alias for files_touched_so_far."},
        },
        required=("session_id", "agent_id", "progress_notes", "current_situation"),
    ),
    "feed_stats": _schema({"topic": {"type": "string"}}),
    "feed_thread": _schema(
        {
            "post_id": {"type": "string"},
            "root_id": {"type": "string", "description": "Backward-compatible alias for post_id."},
        },
        required=("post_id",),
    ),
    "search_architecture_graph": _schema(
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
        },
        required=("query",),
    ),
    "inspect_architecture_graph_node": _schema(
        {
            "node_id": {"type": "string"},
            "source_file": {"type": "string", "description": "Repo-relative source_file from graph.json."},
            "neighbor_limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
    ),
    "federated_retrieve": _schema(
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
            "topic": {"type": "string"},
            "agent_id": {"type": "string"},
            "post_type": {"type": "string"},
            "since": {"type": "string"},
            "wing": {"type": "string"},
            "room": {"type": "string"},
            "entity": {"type": "string"},
            "direction": {"type": "string", "default": "both"},
        },
        required=("query",),
    ),
}


def get_tool_input_schema(
    tool_name: str,
    *,
    optional_fields: Iterable[str] = (),
) -> Dict[str, Any] | None:
    spec = TOOL_CAPABILITY_REGISTRY.get(str(tool_name or "").strip())
    if spec is None or spec.input_schema is None:
        return None
    schema = deepcopy(spec.input_schema)
    optional = {str(item).strip() for item in optional_fields if str(item).strip()}
    if optional and schema.get("required"):
        schema["required"] = [field for field in schema["required"] if field not in optional]
    return schema


def _schema_tool_name(tool_schema: Any) -> str:
    if not isinstance(tool_schema, Mapping):
        return ""
    function = tool_schema.get("function")
    if isinstance(function, Mapping):
        name = str(function.get("name") or "").strip()
        if name:
            return name
    return str(tool_schema.get("name") or "").strip()


def _tool_is_allowed_for_lane(
    name: str,
    spec: ToolCapabilitySpec,
    *,
    required: set[str],
    depth_preset_name: str,
) -> bool:
    if name in required:
        return True
    if spec.route_authority == "optional":
        return depth_preset_name != "fast"
    return False


_PUBLIC_TOOL_SPECS = {
    "list_serverless_models": _spec("list_serverless_models", surface="public", capability_mode="driver-native", allow_local_only=True, notes="Canonical shared model catalog; served from driver-owned ServerlessModelGateway — no backend worker or MCP required."),
    "run_serverless_model": _spec("run_serverless_model", surface="public", capability_mode="driver-native", allow_local_only=True, protocol_tags=("structure.model.prediction", "serverless.model.invoke"), notes="Generic serverless execution surface via driver-owned gateway (Modal/RunPod). Works in direct/local mode without backend API."),
    "search_protein": _spec("search_protein", surface="public", capability_mode="network-native", protocol_tags=("protein.annotation.reference",), required_external_hosts=("rest.uniprot.org",), min_available_hosts=1),
    "publish_agent_message": _spec("publish_agent_message", surface="public", capability_mode="driver-native", allow_local_only=True, protocol_tags=("agent.message.publish",), notes="Inter-agent message bus surface; usable in local-only mode (no backend required)."),
    "kb_job_submit": _spec("kb_job_submit", surface="public", capability_mode="backend-native", protocol_tags=("kb.job.submit", "kb.lifecycle"), required_backend_workers=("kb_jobs",), allow_local_only=False, notes="Submit KB ops jobs (quant_backfill, tier_recompute, retraction_batch). Idempotent via sha256 idempotency_key."),
    "kb_job_status": _spec("kb_job_status", surface="public", capability_mode="backend-native", protocol_tags=("kb.job.status", "kb.lifecycle"), required_backend_workers=("kb_jobs",), allow_local_only=False, notes="Get status + shard progress for a KB ops job."),
    "kb_job_cancel": _spec("kb_job_cancel", surface="public", capability_mode="backend-native", protocol_tags=("kb.job.cancel", "kb.lifecycle"), required_backend_workers=("kb_jobs",), allow_local_only=False, notes="Cancel a pending or running KB ops job."),
    "kb_slo_report": _spec("kb_slo_report", surface="public", capability_mode="backend-native", protocol_tags=("kb.slo.report",), required_backend_workers=("kb_slo",), allow_local_only=False, notes="Generate SLO report: SLI values, error budget, incidents."),
    "kb_migration_backfill": _spec("kb_migration_backfill", surface="public", capability_mode="backend-native", protocol_tags=("kb.migration.backfill", "kb.registry"), required_backend_workers=("kb_migration",), allow_local_only=False, notes="Submit idempotent unit registry backfill. Shadow dual-write + cutover gate."),
    "kb_projection_status": _spec("kb_projection_status", surface="public", capability_mode="backend-native", protocol_tags=("kb.projection.status", "kb.projection"), required_backend_workers=("kb_projection",), allow_local_only=False, notes="Check projection health (PROV-O / Neo4j / Milvus)."),
    "kb_retention_report": _spec("kb_retention_report", surface="public", capability_mode="backend-native", protocol_tags=("kb.retention.report", "kb.asof"), required_backend_workers=("kb_retention",), allow_local_only=False, notes="Asof index retention report: hot/warm/cold counts, bloat, transitions."),
    "resolve_pdb": _spec("resolve_pdb", surface="public", capability_mode="backend-native", protocol_tags=("structure.model.prediction",), required_backend_workers=("resolve_pdb",), allow_local_only=False),
    "analyze_structure": _spec("analyze_structure", surface="public", capability_mode="backend-native", protocol_tags=("structure.analysis",), required_backend_workers=("analyze_structure",), allow_local_only=False),
    "search_literature": _spec("search_literature", surface="public", capability_mode="network-native", protocol_tags=("literature.search.primary",), required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "add_to_workspace": _spec("add_to_workspace", surface="public", capability_mode="backend-native", required_backend_workers=("add_to_workspace",), allow_local_only=False),
    "visualize_molecule": _spec("visualize_molecule", surface="public", capability_mode="backend-native", required_backend_workers=("visualize_molecule",), allow_local_only=False),
    "run_deep_research": _spec("run_deep_research", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "load_knowledge_graph": _spec("load_knowledge_graph", surface="public", capability_mode="backend-native", protocol_tags=("structure.kg",), required_backend_workers=("load_knowledge_graph",), allow_local_only=False),
    "get_domain_coloring": _spec("get_domain_coloring", surface="public", capability_mode="backend-native", required_backend_workers=("get_domain_coloring",), allow_local_only=False),
    "list_lmp_presets": _spec("list_lmp_presets", surface="public", capability_mode="offline-native"),
    "generate_lmp": _spec("generate_lmp", surface="public", capability_mode="backend-native", required_backend_workers=("generate_lmp",), allow_local_only=False),
    "scan_imported_structure": _spec(
        "scan_imported_structure",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.imported.scan", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema=_schema(
            {
                "structure_uri": {"type": "string", "description": "Local, workspace-relative, or file:// PDB path"},
                "asset_id": {"type": "string", "description": "Optional stable structure asset id"},
                "workspace_id": {"type": "string", "description": "Optional workspace scope"},
                "execution_mode": {
                    "type": "string",
                    "enum": ["sync", "async"],
                    "default": "sync",
                    "description": "Use async to hit the canonical Redis/worker queue path and poll later for the materialized parent receipt.",
                },
                "identity_policy": {
                    "type": "string",
                    "enum": ["local_metadata", "local_then_remote_sequence", "local_then_remote_blast"],
                    "default": "local_metadata",
                },
                "remote_identity_timeout_seconds": {"type": "integer", "default": 30},
                "literature_policy": {"type": "object", "additionalProperties": True},
                "dlm_policy": {"type": "object", "additionalProperties": True},
                "smic_policy": {"type": "object", "additionalProperties": True},
                "serverless_policy": {"type": "object", "additionalProperties": True},
                "emit_lmp_xml": {"type": "boolean", "default": False},
                "validate_xsd": {"type": "boolean", "default": True},
            },
            required=("structure_uri",),
        ),
        notes="LMP imported-structure scan receipt surface exposed through the public ws bridge, including the canonical async queue path.",
    ),
    "get_scan_imported_structure_status": _spec(
        "get_scan_imported_structure_status",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.imported.scan", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema=_schema(
            {
                "job_id": {"type": "string", "description": "Async imported-structure scan job id"},
            },
            required=("job_id",),
        ),
        notes="Poll the canonical async imported-structure scan job from the public ws bridge / driver surface.",
    ),
    "get_lmp_state_receipt": _spec(
        "get_lmp_state_receipt",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.state.receipt", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema=_schema(
            {
                "state_id": {"type": "string", "description": "Canonical LMP state_id from the annotations manifest"},
                "allow_afdb_fallback": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true, compute AFDB-derived PocketSites if the cached XML does not already contain them",
                },
            },
            required=("state_id",),
        ),
        notes="Compact state_id retrieval surface for structured receipts with StructureCatalog, residue statistics, and optional AFDB enrichment.",
    ),
    "get_lmp_dynamic_statistics": _spec(
        "get_lmp_dynamic_statistics",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.state.dynamics", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema=_schema(
            {
                "state_id": {"type": "string", "description": "Canonical LMP state_id from the annotations manifest"},
            },
            required=("state_id",),
        ),
        notes="State_id retrieval surface for bounded DynamicsStatistics access without loading the full receipt.",
    ),
    "get_lmp_residue_dynamic_statistics": _spec(
        "get_lmp_residue_dynamic_statistics",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.state.dynamics", "structure.state.dynamics.residue", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema={
            **_schema(
                {
                    "state_id": {"type": "string", "description": "Canonical LMP state_id from the annotations manifest"},
                    "positions": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Explicit residue positions to match",
                    },
                    "chain": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Chain filter for residue-level stats",
                    },
                    "max_results": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                },
                required=("state_id",),
            ),
            "anyOf": [
                {"required": ["positions"]},
                {"required": ["chain"]},
            ],
        },
        notes="Bounded residue-level state_id query surface over the canonical DynamicsStatistics contract.",
    ),
    "get_lmp_pair_dynamic_statistics": _spec(
        "get_lmp_pair_dynamic_statistics",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.state.dynamics", "structure.state.dynamics.pair", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema={
            **_schema(
                {
                    "state_id": {"type": "string", "description": "Canonical LMP state_id from the annotations manifest"},
                    "pairs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "position_i": {"type": "integer"},
                                "position_j": {"type": "integer"},
                                "chain_i": {"type": "string", "minLength": 1},
                                "chain_j": {"type": "string", "minLength": 1},
                            },
                            "required": ["position_i", "position_j"],
                        },
                        "description": "Explicit residue-pair filters",
                    },
                    "chain_i": {"type": "string", "minLength": 1},
                    "chain_j": {"type": "string", "minLength": 1},
                    "max_results": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                },
                required=("state_id",),
            ),
            "anyOf": [
                {"required": ["pairs"]},
                {"required": ["chain_i"]},
                {"required": ["chain_j"]},
            ],
        },
        notes="Bounded pair-level state_id query surface over the canonical DynamicsStatistics contract.",
    ),
    "get_lmp_structure_comparison_ledger": _spec(
        "get_lmp_structure_comparison_ledger",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("structure.state.receipt", "structure.catalog.compare", "structure.annotation.lmp"),
        allow_local_only=False,
        input_schema=_schema(
            {
                "state_id": {"type": "string", "description": "Canonical LMP state_id from the annotations manifest"},
                "allow_afdb_fallback": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true, enrich the StructureCatalog with AFDB fallback before building the comparison ledger",
                },
            },
            required=("state_id",),
        ),
        notes="Deterministic AFDB-vs-PDB comparison ledger over the landed StructureCatalog plane.",
    ),
    "list_dlm_presets": _spec("list_dlm_presets", surface="public", capability_mode="offline-native"),
    "run_dlm_graph_repair_export": _spec(
        "run_dlm_graph_repair_export",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=("dlm.graph.repair", "graph.export.layered"),
        allow_local_only=False,
        requires_provider=True,
        input_schema=_schema(
            {
                "pdf_path": {"type": "string", "description": "Absolute or server-local path to a PDF"},
                "output_dir": {"type": "string", "description": "Optional output directory for graph artifacts"},
                "provider_id": {"type": "string", "default": "deepinfra"},
                "model_id": {"type": "string"},
                "max_pages": {"type": "integer", "default": 40, "minimum": 1, "maximum": 400},
                "max_candidates": {"type": "integer", "default": 0, "minimum": 0},
                "tool_budget": {"type": "integer", "default": 24, "minimum": 1, "maximum": 128},
                "include_cooccurs": {"type": "boolean", "default": False},
                "clear_dlm_cache": {"type": "boolean", "default": False},
            },
            required=("pdf_path",),
        ),
        notes="PDF-bound GraphPatch repair/export lane that rematerializes graph.json and graph.html via the canonical CLI renderer and returns layered graph sidecars.",
    ),
    "run_dlm_scan": _spec("run_dlm_scan", surface="public", capability_mode="network-native", protocol_tags=("literature.search.deep",), required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "generate_report": _spec("generate_report", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "list_workspace_sessions": _spec("list_workspace_sessions", surface="public", capability_mode="backend-native", required_backend_workers=("list_workspace_sessions",), allow_local_only=False),
    "list_workspace_assets": _spec("list_workspace_assets", surface="public", capability_mode="backend-native", required_backend_workers=("list_workspace_assets",), allow_local_only=False),
    "read_workspace_document": _spec("read_workspace_document", surface="public", capability_mode="backend-native", required_backend_workers=("read_workspace_document",), allow_local_only=False),
    "scan_workspace_document": _spec("scan_workspace_document", surface="public", capability_mode="backend-native", required_backend_workers=("scan_workspace_document",), allow_local_only=False),
    "get_workspace_scan_status": _spec("get_workspace_scan_status", surface="public", capability_mode="backend-native", required_backend_workers=("get_workspace_scan_status",), allow_local_only=False),
    "get_citations_and_references": _spec("get_citations_and_references", surface="public", capability_mode="backend-native", required_backend_workers=("get_citations_and_references",), allow_local_only=False),
    "scan_knowledge_base": _spec("scan_knowledge_base", surface="public", capability_mode="backend-native", required_backend_workers=("scan_knowledge_base",), allow_local_only=False),
    "get_knowledge_base_scan_status": _spec("get_knowledge_base_scan_status", surface="public", capability_mode="backend-native", required_backend_workers=("get_knowledge_base_scan_status",), allow_local_only=False),
    "promote_knowledge_base_scan": _spec("promote_knowledge_base_scan", surface="public", capability_mode="backend-native", required_backend_workers=("promote_knowledge_base_scan",), allow_local_only=False),
    "list_knowledge_base_atoms": _spec("list_knowledge_base_atoms", surface="public", capability_mode="backend-native", required_backend_workers=("list_knowledge_base_atoms",), allow_local_only=False),
    "query_mica_q": _spec(
        "query_mica_q",
        surface="public",
        capability_mode="backend-native",
        protocol_tags=(
            "mica_q.console.query",
            "literature.search.deep",
            "lmp.scan.imported_structure",
            "dlm.graph.repair",
        ),
        input_schema=_schema(
            {
                "query": {"type": "string", "description": "Natural-language or explicit MICA-Q console query"},
                "workspace_id": {"type": "string", "description": "Optional workspace scope for GraphRAG augmentation"},
                "session_id": {"type": "string", "description": "Optional session scope for GraphRAG augmentation"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            },
            required=("query",),
        ),
        notes="Public MICA-Q console surface over RetrievalMode.MICA_Q_MULTISURFACE with explicit literature and DLM verb mapping.",
    ),
    "run_bibliotecario_scan": _spec("run_bibliotecario_scan", surface="public", capability_mode="network-native", protocol_tags=("literature.search.deep",), required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "resolve_entity": _spec("resolve_entity", surface="public", capability_mode="backend-native", required_backend_workers=("resolve_entity",), allow_local_only=False),
    "query_atom_facts": _spec("query_atom_facts", surface="public", capability_mode="offline-native"),
    "download_pdf_to_workspace": _spec("download_pdf_to_workspace", surface="public", capability_mode="backend-native", required_backend_workers=("download_pdf_to_workspace",), allow_local_only=False),
    "search_protein_metadata": _spec("search_protein_metadata", surface="public", capability_mode="network-native", protocol_tags=("protein.annotation.reference",), required_external_hosts=("rest.uniprot.org",), min_available_hosts=1),
    "advanced_protein_search": _spec("advanced_protein_search", surface="public", capability_mode="network-native", protocol_tags=("protein.annotation.reference",), required_external_hosts=("rest.uniprot.org",), min_available_hosts=1),
    "milvus_hybrid_search": _spec("milvus_hybrid_search", surface="public", capability_mode="backend-native", required_backend_workers=("milvus_hybrid_search",), allow_local_only=False),
    "milvus_sequence_search": _spec("milvus_sequence_search", surface="public", capability_mode="backend-native", required_backend_workers=("milvus_sequence_search",), allow_local_only=False),
    "milvus_dct_search": _spec("milvus_dct_search", surface="public", capability_mode="backend-native", required_backend_workers=("milvus_dct_search",), allow_local_only=False),
    "milvus_stored_embedding_search": _spec("milvus_stored_embedding_search", surface="public", capability_mode="backend-native", required_backend_workers=("milvus_stored_embedding_search",), allow_local_only=False),
    "run_cascade_pipeline": _spec("run_cascade_pipeline", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "enrich_protein_pharma": _spec("enrich_protein_pharma", surface="public", capability_mode="backend-native", required_backend_workers=("enrich_protein_pharma",), allow_local_only=False),
    "query_co_occurrence": _spec("query_co_occurrence", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "track_entity_evolution": _spec("track_entity_evolution", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "generate_hypotheses": _spec("generate_hypotheses", surface="public", capability_mode="offline-native", protocol_tags=("claim.hypothesis.generation",)),
    "compile_research_briefing": _spec("compile_research_briefing", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "scan_drug_repurposing": _spec("scan_drug_repurposing", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "analyse_citation_impact": _spec("analyse_citation_impact", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "analyse_knowledge_decay": _spec("analyse_knowledge_decay", surface="public", capability_mode="network-native", required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1),
    "map_conformational_landscape": _spec("map_conformational_landscape", surface="public", capability_mode="backend-native", required_backend_workers=("map_conformational_landscape",), placeholder_policy="synthetic_only", allow_local_only=False, notes="Local fallback is only a literature-backed placeholder; never treat as capability parity."),
    "scan_pharmacovigilance": _spec("scan_pharmacovigilance", surface="public", capability_mode="backend-native", required_backend_workers=("scan_pharmacovigilance",), placeholder_policy="synthetic_only", allow_local_only=False, notes="Local fallback is only a placeholder summary."),
    "build_ortholog_dashboard": _spec("build_ortholog_dashboard", surface="public", capability_mode="backend-native", required_backend_workers=("build_ortholog_dashboard",), placeholder_policy="synthetic_only", allow_local_only=False, notes="Local fallback is only a placeholder summary."),
    "verify_citations": _spec("verify_citations", surface="public", capability_mode="network-native", required_external_hosts=("api.crossref.org", "eutils.ncbi.nlm.nih.gov"), min_available_hosts=1),
    "run_driver_delegated_checkpoint": _spec(
        "run_driver_delegated_checkpoint",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("control_plane.modify_test_checkpoint",),
        notes="Driver-owned delegated diff plus focused validation over a disposable allowlisted probe. Preserves a candidate artifact tree without mutating source files.",
    ),
    "run_driver_staging_deploy_checkpoint": _spec(
        "run_driver_staging_deploy_checkpoint",
        surface="public",
        capability_mode="network-native",
        protocol_tags=("control_plane.candidate_deploy_checkpoint",),
        required_external_hosts=("backboard.railway.app", "mica-driver-staging-production.up.railway.app"),
        min_available_hosts=1,
        notes="Driver-owned candidate packaging plus focused validation and staging deploy from the preserved candidate root.",
    ),
    "run_mica_q_sandbox": _spec(
        "run_mica_q_sandbox",
        surface="public",
        capability_mode="sandbox-native",
        requires_sandbox=True,
        allow_local_only=False,
        notes="Canonical MICA-Q sandbox execution surface for code, scripting, and dataset work routed through the shared sandbox lane.",
    ),
    "execute_in_sandbox": _spec("execute_in_sandbox", surface="public", capability_mode="sandbox-native", requires_sandbox=True, allow_local_only=False),
    "sandbox_session_status": _spec("sandbox_session_status", surface="public", capability_mode="offline-native"),
    "terminate_sandbox_session": _spec("terminate_sandbox_session", surface="public", capability_mode="offline-native"),
    "run_driver_experiment": _spec(
        "run_driver_experiment",
        surface="public",
        capability_mode="sandbox-native",
        requires_sandbox=True,
        allow_local_only=False,
        protocol_tags=("control_plane.driver_self_experimentation",),
        notes="Tier-3 disposable Modal sandbox with allow-listed secret injection and scrubbed readback. Pairs hypothesis→insight feed posts via parent_id for reconstructible testimony.",
    ),
    "replay_experiment": _spec(
        "replay_experiment",
        surface="public",
        capability_mode="sandbox-native",
        requires_sandbox=True,
        allow_local_only=False,
        protocol_tags=("control_plane.driver_self_experimentation",),
        notes="Slice-4 §10: re-run a snapshotted experiment and diff vs recorded output; determinism / flakiness probe.",
    ),
    "get_experiment_quota_status": _spec(
        "get_experiment_quota_status",
        surface="public",
        capability_mode="offline-native",
        notes="Slice-4 §9: read-only driver sandbox quota snapshot (used/remaining count and USD).",
    ),
    "search_institutional_knowledge": _spec(
        "search_institutional_knowledge",
        surface="spawn",
        capability_mode="backend-native",
        route_authority="required_for_scientific_audit",
        closure_stage="evidence_acquisition",
        protocol_tags=("audit.internal_memory", "knowledge.retrieval"),
        required_external_hosts=("api.openai.com", "api.runpod.ai"),
        min_available_hosts=1,
        allow_local_only=False,
        notes="DEV-ONLY institutional-memory retrieval bridge backed by Milvus with a low-latency OpenAI embedding fast-path and optional RunPod fallback. Must stay off the production biomedical lane.",
    ),
    "search_mica_institutional_memory": _spec(
        "search_mica_institutional_memory",
        surface="spawn",
        capability_mode="backend-native",
        route_authority="required_for_scientific_audit",
        closure_stage="evidence_acquisition",
        protocol_tags=("audit.internal_memory", "knowledge.retrieval"),
        required_external_hosts=("api.openai.com", "api.runpod.ai"),
        min_available_hosts=1,
        allow_local_only=False,
        notes="Alias for DEV-ONLY institutional-memory retrieval bridge with OpenAI fast-path and optional RunPod fallback; kept for prompt/runtime parity.",
    ),
    "scroll_agent_feed": _spec(
        "scroll_agent_feed",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("coordination.feed.read",),
        input_schema=FEED_INPUT_SCHEMAS["scroll_agent_feed"],
        notes="Reads the live agent feed when the active runtime exposes the feed surface directly or through a wrapper.",
    ),
    "publish_cue": _spec(
        "publish_cue",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("coordination.feed.write",),
        input_schema=FEED_INPUT_SCHEMAS["publish_cue"],
        notes="Publishes cue/decision/tombstone/insight/session-close events to the live coordination feed.",
    ),
    "open_session_signature": _spec(
        "open_session_signature",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("coordination.session.open",),
        input_schema=FEED_INPUT_SCHEMAS["open_session_signature"],
        notes="Mandatory session-open lifecycle call when the feed surface is callable in the active runtime.",
    ),
    "update_session_progress": _spec(
        "update_session_progress",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("coordination.session.progress",),
        input_schema=FEED_INPUT_SCHEMAS["update_session_progress"],
        notes="Mid-session lifecycle trace for the live coordination feed.",
    ),
    "feed_stats": _spec(
        "feed_stats",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("coordination.feed.stats",),
        input_schema=FEED_INPUT_SCHEMAS["feed_stats"],
        notes="Summarizes the live feed and recent posting activity.",
    ),
    "feed_thread": _spec(
        "feed_thread",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("coordination.feed.thread",),
        input_schema=FEED_INPUT_SCHEMAS["feed_thread"],
        notes="Retrieves one feed post and its comment thread.",
    ),
    "search_architecture_graph": _spec(
        "search_architecture_graph",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("structure.graph.search", "structure.graph.seams"),
        input_schema=FEED_INPUT_SCHEMAS["search_architecture_graph"],
        notes="Preferred structural retrieval entrypoint over graph.json for architecture review in feed-adjacent workflows.",
    ),
    "inspect_architecture_graph_node": _spec(
        "inspect_architecture_graph_node",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("structure.graph.inspect", "structure.graph.seams"),
        input_schema=FEED_INPUT_SCHEMAS["inspect_architecture_graph_node"],
        notes="Preferred graph node inspection tool returning canonical node metadata plus strongest coupled neighbors.",
    ),
    "federated_retrieve": _spec(
        "federated_retrieve",
        surface="public",
        capability_mode="offline-native",
        protocol_tags=("knowledge.retrieval", "coordination.feed.read", "structure.graph.seams"),
        input_schema=FEED_INPUT_SCHEMAS["federated_retrieve"],
        notes="Deprecated compatibility accelerator across live feed, durable memory, KG facts, and graph seams. Prefer direct feed + memory + graph tools.",
    ),
    "publish_operator_directive": _spec(
        "publish_operator_directive",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        closure_stage="coordination",
        required_backend_workers=("user_bucket_storage",),
        allow_local_only=False,
        notes="ODRC-2026-04-20 publish surface: writes canonical directive JSON + prompt Markdown to durable user-bucket storage.",
    ),
    # --- Bucket-native tools (Phase 1) ---
    "list_user_bucket_objects": _spec("list_user_bucket_objects", surface="public", capability_mode="backend-native", allow_local_only=False),
    "get_user_bucket_object_info": _spec("get_user_bucket_object_info", surface="public", capability_mode="backend-native", allow_local_only=False),
    "read_user_bucket_object_text": _spec("read_user_bucket_object_text", surface="public", capability_mode="backend-native", allow_local_only=False),
    "copy_user_bucket_object_to_workspace": _spec("copy_user_bucket_object_to_workspace", surface="public", capability_mode="backend-native", allow_local_only=False),
    "copy_user_bucket_object": _spec("copy_user_bucket_object", surface="public", capability_mode="backend-native", allow_local_only=False),
    "search_user_bucket_content": _spec("search_user_bucket_content", surface="public", capability_mode="backend-native", allow_local_only=False),
    # --- Workspace snapshot read tools (ODRC evidence surface) ---
    "list_workspace_files": _spec(
        "list_workspace_files",
        surface="public",
        capability_mode="backend-native",
        required_backend_workers=("user_bucket_storage",),
        allow_local_only=False,
        notes="Lists files under workspace_snapshots/latest/ in the user bucket. Required reconnaissance for any publish_operator_directive that alters source code.",
    ),
    "read_workspace_file_content": _spec(
        "read_workspace_file_content",
        surface="public",
        capability_mode="backend-native",
        required_backend_workers=("user_bucket_storage",),
        allow_local_only=False,
        notes="Reads a file from the synced repo snapshot. Required evidence for any publish_operator_directive that alters source code.",
    ),
    # --- Web search (Slice-1; R24 annex §9 mandatory research tool) ---
    "web_search": _spec(
        "web_search",
        surface="public",
        capability_mode="network-native",
        protocol_tags=("literature.search.web",),
        required_external_hosts=("api.firecrawl.dev",),
        min_available_hosts=1,
        allow_local_only=False,
        notes="Firecrawl v2 web search. Required by R24 annex §9 as part of the mandatory research tool surface alongside search_literature, run_deep_research, federated_retrieve, and search_institutional_knowledge.",
    ),
    # --- Slice-2 bootstrap: read-only IDE primitives (local FS) ---
    # Blueprint: tools/r29_runs/_SLICE2_OPERATIONAL_BLINDNESS_BLUEPRINT.md §3
    "repo_list_files": _spec(
        "repo_list_files",
        surface="public",
        capability_mode="offline-native",
        allow_local_only=True,
        notes="Lists files in the local MICA repo checkout (repo root). Read-only reconnaissance primitive for driver self-inspection.",
    ),
    "repo_grep": _spec(
        "repo_grep",
        surface="public",
        capability_mode="offline-native",
        allow_local_only=True,
        notes="Regex search across the local MICA repo. Read-only. Use before repo_read to locate symbols.",
    ),
    "repo_read": _spec(
        "repo_read",
        surface="public",
        capability_mode="offline-native",
        allow_local_only=True,
        notes="Bounded-slice read of a MICA repo file (local checkout). Read-only; always specify start_line/end_line.",
    ),
    # --- BackendCommandManifest convergence tools (2026-06-14) ---
    # Generated from 36 BackendCommandSpec entries to close the ToolKG/Backend gap
    "compute_jobs_submit": _spec(
        "compute_jobs_submit",
        surface="public",
        capability_mode="backend-native",
        route_authority="required_for_compute",
        closure_stage="evidence_acquisition",
        required_backend_workers=("compute",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "job_type": {"type": "string", "description": "Type of compute job (md_simulation, docking, etc.)"},
                "parameters": {"type": "object", "description": "Job-specific parameters"},
                "provider": {"type": "string", "default": "vast", "description": "Compute provider"},
            },
            required=("job_type",),
        ),
        notes="Unified compute job submission — canonical entrypoint for MD/docking/analysis jobs. Backed by POST /api/v1/compute/jobs.",
    ),
    "compute_jobs_status": _spec(
        "compute_jobs_status",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("compute",),
        allow_local_only=False,
        input_schema=_schema(
            {"job_id": {"type": "string", "description": "Compute job ID"}},
            required=("job_id",),
        ),
        notes="Query compute job status. Backed by GET /api/v1/compute/jobs/{job_id}.",
    ),
    "protocol_validate": _spec(
        "protocol_validate",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("protocol",),
        allow_local_only=False,
        input_schema={
            **_schema(
                {
                    "protocol_jsonld": {"type": "object", "description": "Canonical Protocol JSON-LD payload to validate."},
                    "protocol_json": {"type": "object", "description": "Backward-compatible alias for protocol_jsonld."},
                        "protocol_draft": {"type": "object", "description": "Agent-friendly alias for protocol_jsonld when the caller frames the payload as a draft."},
                        "tool_plan": {"type": "object", "description": "Simplified plan compiled server-side to ProtocolJSONLDDocument. Shape: {id?, name?, goal?, steps:[{id?, tool_name, params?, dependencies?}]}."},
                        "protocol_plan": {"type": "object", "description": "Alias for tool_plan."},
                        "steps": {"type": "array", "items": {"type": "object"}, "description": "Flat alias for tool_plan.steps when protocol_id/name/goal are top-level."},
                        "protocol_id": {"type": "string"},
                        "protocol_name": {"type": "string"},
                        "goal": {"type": "string"},
                        "protocol_path": {"type": "string", "description": "Workspace-local JSON file path to validate."},
                },
                additional_properties=False,
            ),
            "anyOf": [
                {"required": ["protocol_jsonld"]},
                {"required": ["protocol_json"]},
                {"required": ["protocol_draft"]},
                {"required": ["tool_plan"]},
                {"required": ["protocol_plan"]},
                {"required": ["steps"]},
                {"required": ["protocol_path"]},
            ],
        },
        notes="Validate a scientific protocol against MSRP schema. Low-risk, read-only. Backed by POST /api/v1/protocols/validate.",
    ),
    "protocol_run": _spec(
        "protocol_run",
        surface="public",
        capability_mode="backend-native",
        route_authority="required_for_scientific_audit",
        closure_stage="evidence_acquisition",
        required_backend_workers=("protocol",),
        allow_local_only=False,
        input_schema=_schema(
            {"protocol_json": {"type": "object", "description": "Protocol JSON-LD to execute"}},
            required=("protocol_json",),
        ),
        notes="Execute a validated scientific protocol. Higher risk (T3) — requires human-in-loop gating. Backed by POST /api/v1/protocols/run.",
    ),
    "study_list": _spec(
        "study_list",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("study",),
        allow_local_only=False,
        notes="List user studies. Product surface for agent workflow management. Backed by GET /api/v1/studies.",
    ),
    "kb_list": _spec(
        "kb_list",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("kb",),
        allow_local_only=False,
        notes="List knowledge bases. Backed by GET /api/v1/kbs.",
    ),
    "working_set_list": _spec(
        "working_set_list",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("working_set",),
        allow_local_only=False,
        notes="List working sets. Backed by GET /api/v1/working-sets.",
    ),
    "model_serverless_invoke": _spec(
        "model_serverless_invoke",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("serverless_models",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "model_name": {"type": "string", "description": "Serverless model name to invoke"},
                "payload": {"type": "object", "description": "Model-specific input payload"},
            },
            required=("model_name",),
        ),
        notes="Invoke a serverless model (ESM3, ProteinMPNN, etc.). Backed by POST /api/v1/serverless-models/invoke.",
    ),
    # --- Product object commands (2026-06-14) ---
    # Generated from BackendCommandManifest + POST schema discovery
    "study_create": _spec(
        "study_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("study",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "name": {"type": "string", "description": "Study name (1-200 chars)"},
                "description": {"type": "string", "description": "Optional description (max 2000 chars)"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("name",),
        ),
        notes="Create a durable scientific project container (Study). Backed by POST /api/v1/studies.",
    ),
    "study_get": _spec(
        "study_get",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("study",),
        allow_local_only=False,
        input_schema=_schema(
            {"study_id": {"type": "string", "description": "Study ID"}},
            required=("study_id",),
        ),
        notes="Get study details including artifact count. Backed by GET /api/v1/studies/{study_id}.",
    ),
    "study_attach_resource": _spec(
        "study_attach_resource",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("study",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "study_id": {"type": "string", "description": "Study ID"},
                "artifact_id": {"type": "string", "description": "Artifact ID to attach to study"},
            },
            required=("study_id", "artifact_id"),
        ),
        notes="Attach an artifact to a Study. Currently accepts artifact_id only (not resource URIs). Backed by POST /api/v1/studies/{study_id}/artifacts.",
    ),
    "working_set_create": _spec(
        "working_set_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("working_set",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "name": {"type": "string", "description": "WorkingSet name"},
                "study_id": {"type": "string", "description": "Parent study ID"},
            },
            required=("name",),
        ),
        notes="Create an active working context (WorkingSet) for a scientific task. Backed by POST /api/v1/working-sets.",
    ),
    "working_set_attach_resource": _spec(
        "working_set_attach_resource",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("working_set",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "working_set_id": {"type": "string", "description": "Working set ID"},
                "artifact_id": {"type": "string", "description": "Artifact ID to attach to the working set"},
                "artifact_ref_type": {"type": "string", "description": "Optional explicit ref type override"},
                "position": {"type": "integer", "default": 0},
                "config": {"type": "object"},
            },
            required=("working_set_id", "artifact_id"),
        ),
        notes="Attach a resource to a WorkingSet. Backed by working-sets resource attachment route.",
    ),
    "lab_create": _spec(
        "lab_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("laboratory",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "display_name": {"type": "string", "description": "Laboratory display name"},
                "description": {"type": "string", "description": "Optional laboratory description"},
                "org_ref": {"type": "string", "description": "Optional organization reference"},
                "metadata": {"type": "object", "description": "Optional lab metadata"},
            },
            required=("display_name",),
        ),
        notes="Create a durable Laboratory root. Backed by POST /api/v1/labs.",
    ),
    "lab_list": _spec(
        "lab_list",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("laboratory",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema({}),
        notes="List laboratories visible to the caller. Backed by GET /api/v1/labs.",
    ),
    "lab_get": _spec(
        "lab_get",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("laboratory",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {"lab_id": {"type": "string", "description": "Laboratory ID"}},
            required=("lab_id",),
        ),
        notes="Get one laboratory by ID. Backed by GET /api/v1/labs/{lab_id}.",
    ),
    "knowledge_space_create": _spec(
        "knowledge_space_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("knowledge_space",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "lab_id": {"type": "string", "description": "Owning laboratory ID"},
                "display_name": {"type": "string", "description": "Knowledge Space display name"},
                "slug": {"type": "string"},
                "description": {"type": "string"},
                "primary_parent_space_id": {"type": "string"},
                "review_cadence": {"type": "string"},
                "health_status": {"type": "string"},
                "metadata": {"type": "object"},
            },
            required=("lab_id", "display_name"),
        ),
        notes="Create a durable Knowledge Space. Backed by POST /api/v1/knowledge-spaces.",
    ),
    "knowledge_space_list": _spec(
        "knowledge_space_list",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("knowledge_space",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "lab_id": {"type": "string"},
                "archived": {"type": "boolean", "default": False},
            },
        ),
        notes="List Knowledge Spaces visible to the caller. Backed by GET /api/v1/knowledge-spaces.",
    ),
    "knowledge_space_get": _spec(
        "knowledge_space_get",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("knowledge_space",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {"space_id": {"type": "string", "description": "Knowledge Space ID"}},
            required=("space_id",),
        ),
        notes="Get one Knowledge Space by ID. Backed by GET /api/v1/knowledge-spaces/{space_id}.",
    ),
    "knowledge_space_create_membership": _spec(
        "knowledge_space_create_membership",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("knowledge_space",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "space_id": {"type": "string", "description": "Parent Knowledge Space ID"},
                "child_space_id": {"type": "string"},
                "member_kb_ref": {"type": "string"},
                "relation_type": {"type": "string"},
                "expansion_policy": {"type": "string"},
                "primary_parent": {"type": "boolean"},
                "metadata": {"type": "object"},
            },
            required=("space_id",),
        ),
        notes="Create a Knowledge Membership. Backed by POST /api/v1/knowledge-spaces/{space_id}/memberships.",
    ),
    "research_line_create": _spec(
        "research_line_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("research_line",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "lab_id": {"type": "string", "description": "Owning laboratory ID"},
                "display_name": {"type": "string", "description": "Research Line display name"},
                "slug": {"type": "string"},
                "description": {"type": "string"},
                "primary_question": {"type": "string"},
                "status": {"type": "string"},
                "metadata": {"type": "object"},
            },
            required=("lab_id", "display_name"),
        ),
        notes="Create a durable Research Line. Backed by POST /api/v1/research-lines.",
    ),
    "research_line_list": _spec(
        "research_line_list",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("research_line",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "lab_id": {"type": "string"},
                "archived": {"type": "boolean", "default": False},
            },
        ),
        notes="List Research Lines visible to the caller. Backed by GET /api/v1/research-lines.",
    ),
    "research_line_get": _spec(
        "research_line_get",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("research_line",),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {"line_id": {"type": "string", "description": "Research Line ID"}},
            required=("line_id",),
        ),
        notes="Get one Research Line by ID. Backed by GET /api/v1/research-lines/{line_id}.",
    ),
    "research_line_link_study": _spec(
        "research_line_link_study",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("research_line", "study"),
        allow_local_only=False,
        protocol_eligible=True,
        gog_eligible=True,
        input_schema=_schema(
            {
                "line_id": {"type": "string", "description": "Research Line ID"},
                "study_id": {"type": "string", "description": "Study ID"},
            },
            required=("line_id", "study_id"),
        ),
        notes="Link a Study to a Research Line. Backed by POST /api/v1/research-lines/{line_id}/studies/{study_id}.",
    ),
    "kb_create": _spec(
        "kb_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("kb",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "name": {"type": "string", "description": "KB name (1-200 chars)"},
                "kb_type": {"type": "string", "default": "QUERY", "description": "KB type (QUERY default)"},
                "canonical_query": {"type": "string", "description": "Canonical query for this KB"},
                "target_entities": {"type": "array", "items": {"type": "string"}},
                "target_topics": {"type": "array", "items": {"type": "string"}},
            },
            required=("name",),
        ),
        notes="Create a semantic knowledge base over documents/artifacts. Backed by POST /api/v1/kbs.",
    ),
    "kb_ingest_documents": _spec(
        "kb_ingest_documents",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("kb",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "kb_id": {"type": "string", "description": "KB ID to ingest into"},
                "documents": {"type": "array", "items": {"type": "object"}, "description": "Documents to ingest"},
            },
            required=("kb_id",),
        ),
        notes="Ingest documents into a KB. Backed by POST /api/v1/kbs/{kb_id}/scan.",
    ),
    "kb_semantic_query": _spec(
        "kb_semantic_query",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("kb",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "kb_id": {"type": "string", "description": "KB ID to query"},
                "query": {"type": "string", "description": "Natural language query"},
                "entity_focus": {"type": "array", "items": {"type": "string"}},
                "topic_focus": {"type": "array", "items": {"type": "string"}},
            },
            required=("kb_id", "query"),
        ),
        notes="Run a semantic query over a KB. Backed by POST /api/v1/kbs/{kb_id}/runs. Requires KB to exist first.",
    ),
    "artifact_create": _spec(
        "artifact_create",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("artifact",),
        allow_local_only=False,
        input_schema=_schema(
            {
                "artifact_type": {"type": "string", "description": "Type: structure, paper, figure, report, etc."},
                "display_name": {"type": "string", "description": "Human-readable name"},
                "ref_url": {"type": "string", "description": "Reference URL or resource URI"},
            },
            required=("artifact_type", "display_name"),
        ),
        notes="Create a persisted artifact with GCS/provenance. Backed by POST /api/v1/artifacts.",
    ),
    "artifact_get": _spec(
        "artifact_get",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("artifact",),
        allow_local_only=False,
        notes="Get artifact details. Backed by GET /api/v1/artifacts/{id}.",
    ),
    "artifact_signed_url": _spec(
        "artifact_signed_url",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("artifact",),
        allow_local_only=False,
        notes="Get a signed GCS download URL for an artifact. Backed by GET /api/v1/artifacts/{id}/download.",
    ),
    # ── GraphRAG product tools (TSG-002) ─────────────────────────────
    "graphrag_query": _spec(
        "graphrag_query",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("graphrag",),
        allow_local_only=False,
        protocol_tags=("graph.query.hybrid", "graph.retrieval.scientific"),
        input_schema=_schema(
            {
                "query_text": {"type": "string", "description": "Natural language query for graph search"},
                "study_id": {"type": "string", "description": "Optional study scope filter"},
                "kb_id": {"type": "string", "description": "Optional KB scope filter"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            required=("query_text",),
        ),
        notes="Hybrid FTS+pgvector search across GraphRAG edges and facts. Returns graph facts with provenance (source_doi, claim_key). Backed by POST /api/v1/graphrag/query.",
    ),
    "graphrag_hop1": _spec(
        "graphrag_hop1",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("graphrag",),
        allow_local_only=False,
        protocol_tags=("graph.traversal.hop1", "graph.expansion.entity"),
        input_schema=_schema(
            {
                "seed_nodes": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
                "study_id": {"type": "string", "description": "Optional study scope filter"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
            },
            required=("seed_nodes",),
        ),
        notes="1-hop graph traversal from seed entity names. Returns connected entities and relationships. Backed by POST /api/v1/graphrag/hop1.",
    ),
    "graphrag_write_claim": _spec(
        "graphrag_write_claim",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("graphrag",),
        allow_local_only=False,
        protocol_tags=("graph.write.claim", "provenance.scientific"),
        input_schema=_schema(
            {
                "content": {"type": "string", "description": "Scientific claim text"},
                "study_id": {"type": "string", "description": "Study scope"},
                "entities": {"type": "array", "items": {"type": "string"}},
                "source_doi": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            required=("content", "study_id"),
        ),
        notes="Write a scientific claim to GraphRAG with auto-computed claim_key and provenance. Deduplicated by content hash. Backed by POST /api/v1/graphrag/claim.",
    ),
    "graphrag_write_lmp_graph": _spec(
        "graphrag_write_lmp_graph",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("graphrag", "lmp"),
        allow_local_only=False,
        protocol_tags=("graph.write.lmp", "lmp.v4.persist", "biological.graph"),
        input_schema=_schema(
            {
                "study_id": {"type": "string", "description": "Study to link the LMP graph to"},
                "lmp_file": {"type": "string", "description": "LMP v4 XML filename from GCS corpus"},
                "max_edges": {"type": "integer", "default": 500, "minimum": 1, "maximum": 2000},
            },
            required=("study_id", "lmp_file"),
        ),
        notes="Parse LMP v4 biological graph and persist protein/domain/pathway/disease/pharmacology nodes into GraphRAG. Backed by POST /api/v1/graphrag/lmp.",
    ),
    "graphrag_export_decision_subgraph": _spec(
        "graphrag_export_decision_subgraph",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("graphrag",),
        allow_local_only=False,
        protocol_tags=("graph.export.decision", "evidence.subgraph"),
        input_schema=_schema(
            {
                "study_id": {"type": "string", "description": "Study scope"},
                "query_focus": {"type": "string", "description": "Question to answer with the subgraph"},
            },
            required=("query_focus",),
        ),
        notes="Export a bounded evidence subgraph for Scientific Decision Cards. Combines semantic search + hop-1 traversal. Backed by POST /api/v1/graphrag/export-decision-subgraph.",
    ),
}


_SYNTHESIZED_AGENT_SURFACE_SPECS = {
    "mica.command.run": _spec(
        "mica.command.run",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("kernel_gateway",),
        allow_local_only=False,
        protocol_tags=("command.kernel", "kernel.gateway", "agent.wrapper"),
        input_schema=_schema(
            {
                "command_name": {
                    "type": "string",
                    "description": "Canonical backend command name executed through the unified kernel gateway.",
                },
                "args": {
                    "type": "object",
                    "description": "Primary argument object forwarded to the backend command runtime.",
                    "additionalProperties": True,
                },
                "arguments": {
                    "type": "object",
                    "description": "Backward-compatible alias for args.",
                    "additionalProperties": True,
                },
                "workspace_id": {"type": "string"},
                "workspace": {"type": "string"},
                "study_id": {"type": "string"},
                "study": {"type": "string"},
            },
            required=("command_name",),
            additional_properties=False,
        ),
        notes="Synthesized public wrapper exposed by /api/v1/kernel/agent-tools and dispatched by ws_bridge into the canonical kernel gateway command path.",
    ),
    "mica.protocol.submit": _spec(
        "mica.protocol.submit",
        surface="public",
        capability_mode="backend-native",
        route_authority="optional",
        required_backend_workers=("kernel_gateway",),
        allow_local_only=False,
        protocol_tags=("protocol.submit", "protocol.jsonld", "agent.wrapper"),
        input_schema={
            **_schema(
                {
                    "protocol_jsonld": {
                        "description": "ProtocolJSONLDDocument payload to submit.",
                        "oneOf": [{"type": "object"}, {"type": "string"}],
                    },
                    "protocolJsonld": {
                        "description": "Backward-compatible alias for protocol_jsonld.",
                        "oneOf": [{"type": "object"}, {"type": "string"}],
                    },
                    "protocol_json_ld": {
                        "description": "Backward-compatible alias for protocol_jsonld.",
                        "oneOf": [{"type": "object"}, {"type": "string"}],
                    },
                    "protocol_json": {
                        "description": "Alias for protocol_jsonld when the caller frames the payload as plain protocol JSON.",
                        "type": "object",
                    },
                      "protocol_draft": {
                          "description": "Agent-friendly alias for protocol_jsonld when reusing a draft payload.",
                          "type": "object",
                      },
                      "tool_plan": {
                          "description": "Simplified plan compiled server-side to ProtocolJSONLDDocument. Shape: {id?, name?, goal?, steps:[{id?, tool_name, params?, dependencies?}]}.",
                          "type": "object",
                      },
                      "protocol_plan": {
                          "description": "Alias for tool_plan.",
                          "type": "object",
                      },
                      "steps": {
                          "description": "Flat alias for tool_plan.steps when protocol_id/name/goal are top-level.",
                          "type": "array",
                          "items": {"type": "object"},
                      },
                      "protocol_id": {"type": "string"},
                      "protocol_name": {"type": "string"},
                      "goal": {"type": "string"},
                      "protocol_path": {
                          "description": "Workspace-scoped path to a canonical ProtocolJSONLDDocument file.",
                          "type": "string",
                      },
                    "workspace_id": {"type": "string"},
                    "workspace": {"type": "string"},
                    "study_id": {"type": "string"},
                    "study": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                required=("workspace_id", "study_id"),
                additional_properties=False,
            ),
            "anyOf": [
                {"required": ["protocol_jsonld"]},
                {"required": ["protocolJsonld"]},
                {"required": ["protocol_json_ld"]},
                {"required": ["protocol_json"]},
                  {"required": ["protocol_draft"]},
                  {"required": ["tool_plan"]},
                  {"required": ["protocol_plan"]},
                  {"required": ["steps"]},
                  {"required": ["protocol_path"]},
            ],
        },
        notes="Synthesized public wrapper for background ProtocolJSONLD submission through the kernel gateway.",
    ),
}


_SPAWN_TOOL_SPECS = {
    "consult_bibliotecario": _spec("consult_bibliotecario", surface="spawn", capability_mode="network-native", route_authority="required_for_scientific_audit", closure_stage="evidence_acquisition", protocol_tags=("literature.search.deep",), required_external_hosts=("api.semanticscholar.org", "eutils.ncbi.nlm.nih.gov", "api.biorxiv.org", "api.openalex.org", "content.openalex.org"), min_available_hosts=1, requires_provider=True, notes="DEPRECATED (2026-06-14): spawn-only tool without executor dispatch. Superseded by search_literature + run_bibliotecario_scan."),
    "consult_specialist": _spec("consult_specialist", surface="spawn", capability_mode="backend-native", route_authority="optional", required_backend_workers=("consult_specialist",), allow_local_only=False, protocol_tags=("specialist.delegation", "sandbox.modal_tier2"), notes="ACTIVE (2026-06-15): dispatched via run_consult_specialist_branch in loop_primary_dispatch_service. Routes to Modal Tier 2 (GPU sandbox) or host specialist drivers (Tier 0/1). Deprecation reversed — tool has active executor."),
    "request_peer_review": _spec("request_peer_review", surface="spawn", capability_mode="offline-native", route_authority="required_for_scientific_audit", closure_stage="critique", protocol_tags=("claim.review.peer",), requires_provider=True, notes="DEPRECATED (2026-06-14): spawn-only tool without executor dispatch. Superseded by verify_citations + query_atom_facts."),
    "consult_expert": _spec("consult_expert", surface="spawn", capability_mode="offline-native", requires_provider=True, notes="DEPRECATED (2026-06-14): spawn-only tool without executor dispatch. Superseded by run_deep_research."),
    "generate_vertical_report": _spec("generate_vertical_report", surface="spawn", capability_mode="offline-native", route_authority="required_for_scientific_audit", closure_stage="vertical_synthesis", notes="DEPRECATED (2026-06-14): spawn-only tool without executor dispatch. Superseded by compile_research_briefing + generate_report."),
}


def _command_kernel_tool_specs() -> Dict[str, ToolCapabilitySpec]:
    specs: Dict[str, ToolCapabilitySpec] = {}
    for entry in iter_manifest_entries():
        canonical_name = canonical_tool_name_for_command(entry.command_name)
        spec_names = [entry.command_name, canonical_name, *list(entry.tool_aliases)]
        for spec_name in dict.fromkeys(str(name or "").strip() for name in spec_names if str(name or "").strip()):
            # Preserve synthesized public wrappers such as ``mica.command.run`` and
            # ``mica.protocol.submit``. Their backend-native visibility contract is
            # stronger than the generic manifest-backed local-only fallback.
            if spec_name in _SYNTHESIZED_AGENT_SURFACE_SPECS:
                continue
            specs[spec_name] = _spec(
                spec_name,
                surface="public",
                capability_mode="backend-native",
                route_authority="optional",
                allow_local_only=True,
                input_schema=entry.input_schema or None,
                protocol_tags=("command.kernel", f"binding.{entry.binding_surface}"),
                protocol_eligible=bool(entry.protocol_step_eligible),
                gog_eligible=bool(entry.campaign_eligible),
                notes=(
                    f"Manifest-backed backend command surface for {entry.command_name}. "
                    f"Authority: {entry.backend_authority}."
                ),
            )
    return specs


TOOL_CAPABILITY_REGISTRY: Dict[str, ToolCapabilitySpec] = {
    **_PUBLIC_TOOL_SPECS,
    **_SYNTHESIZED_AGENT_SURFACE_SPECS,
    **_SPAWN_TOOL_SPECS,
    **_command_kernel_tool_specs(),
}


def get_tool_capability(tool_name: str) -> ToolCapabilitySpec:
    try:
        return TOOL_CAPABILITY_REGISTRY[tool_name]
    except KeyError as exc:
        raise KeyError(f"No capability registry entry exists for tool '{tool_name}'") from exc


def build_tool_capability_matrix(tool_names: Iterable[str] | None = None) -> Dict[str, Dict[str, Any]]:
    names = list(tool_names or TOOL_CAPABILITY_REGISTRY.keys())
    return {name: get_tool_capability(name).to_dict() for name in names}


def tool_names_requiring_backend(tool_names: Iterable[str]) -> Tuple[str, ...]:
    required = []
    for name in tool_names:
        normalized = str(name or "").strip()
        if not normalized or normalized not in TOOL_CAPABILITY_REGISTRY:
            continue
        spec = TOOL_CAPABILITY_REGISTRY[normalized]
        if spec.required_backend_workers:
            required.append(normalized)
    return tuple(sorted(dict.fromkeys(required)))


def validate_tool_registry_coverage(tool_names: Iterable[str]) -> Tuple[str, ...]:
    missing = sorted(
        {
            str(name).strip()
            for name in tool_names
            if str(name).strip() and str(name).strip() not in TOOL_CAPABILITY_REGISTRY
        }
    )
    return tuple(missing)


def validate_backend_command_manifest_parity(tool_names: Iterable[str] | None = None) -> Tuple[str, ...]:
    if tool_names is None:
        names = []
        for entry in iter_manifest_entries():
            if entry.implemented_status == "implemented":
                names.append(entry.command_name)
                names.extend(entry.tool_aliases)
        tool_names = names

    normalized = tuple(
        str(name).strip()
        for name in tool_names
        if str(name).strip()
    )
    from mica.agentic.backend_command_manifest import BACKEND_COMMAND_MANIFEST
    manifest_entries = manifest_entries_by_tool_alias(normalized)
    for name in normalized:
        if name in BACKEND_COMMAND_MANIFEST and name not in manifest_entries:
            manifest_entries[name] = BACKEND_COMMAND_MANIFEST[name]
    for entry in iter_manifest_entries():
        canonical_name = canonical_tool_name_for_command(entry.command_name)
        if canonical_name in normalized and canonical_name not in manifest_entries:
            manifest_entries[canonical_name] = entry

    mismatched = sorted(
        {
            name
            for name in normalized
            if name not in manifest_entries
            or manifest_entries[name].implemented_status != "implemented"
            or name not in TOOL_CAPABILITY_REGISTRY
        }
    )
    return tuple(mismatched)


def registry_items() -> Mapping[str, ToolCapabilitySpec]:
    return TOOL_CAPABILITY_REGISTRY


def lane_filter_dropped_tool_names(
    tool_schemas: Iterable[Dict[str, Any]],
    lane: str,
    *,
    depth_preset_name: str = "standard",
) -> Tuple[str, ...]:
    """Return tool names rejected by the lane filter.

    This is the explicit inspection helper for registry drift and lane-level
    authority mismatches. Unknown, nameless, and unauthorized tools are all
    reported here instead of being silently tolerated.
    """
    dropped: list[str] = []
    required = set(required_tools_for_lane(lane))
    for schema in tool_schemas:
        name = _schema_tool_name(schema)
        if not name:
            dropped.append("<unnamed>")
            continue
        spec = TOOL_CAPABILITY_REGISTRY.get(name)
        if spec is None:
            dropped.append(name)
            continue
        if not _tool_is_allowed_for_lane(
            name,
            spec,
            required=required,
            depth_preset_name=depth_preset_name,
        ):
            dropped.append(name)
    return tuple(dict.fromkeys(dropped))


# ── NewDawn: lane-aware enforcement (WI-12) ──────────────────────────────────

def required_tools_for_lane(lane: str) -> Tuple[str, ...]:
    """Return tool names whose route_authority is 'required_for_<lane>'."""
    tag = f"required_for_{lane}"
    return tuple(
        name for name, spec in TOOL_CAPABILITY_REGISTRY.items()
        if spec.route_authority == tag
    )


def filter_tools_for_lane(
    tool_schemas: Iterable[Dict[str, Any]],
    lane: str,
    *,
    depth_preset_name: str = "standard",
) -> list:
    """Filter tool schemas, keeping only those allowed for the given lane/depth.

    Rules:
    - Tools registered as ``required_for_<lane>`` are always included.
    - Tools registered as ``optional`` are included in *standard* and *deep* presets.
    - In *fast* preset, optional tools are dropped.
    - Unknown, nameless, and lane-unauthorized tools are dropped.
    """
    result = []
    required = set(required_tools_for_lane(lane))
    for schema in tool_schemas:
        name = _schema_tool_name(schema)
        if not name:
            continue
        spec = TOOL_CAPABILITY_REGISTRY.get(name)
        if spec is None:
            continue
        if _tool_is_allowed_for_lane(
            name,
            spec,
            required=required,
            depth_preset_name=depth_preset_name,
        ):
            result.append(schema)
    return result

# -- Bio-mode surface contract (Slice 7.1) ------------------------------------
# Reference: docs/specs/SPEC_7_1_TOOL_SURFACE_SEGREGATION_CONTRACT_2026-04-24.md §3.1

FORBIDDEN_BIO_TOOLS: frozenset = frozenset({
    "scroll_agent_feed",
    "publish_cue",
    "open_session_signature",
    "update_session_progress",
    "feed_stats",
    "feed_thread",
    "federated_retrieve",
    "search_institutional_knowledge",
    "search_mica_institutional_memory",
    "publish_operator_directive",
    "run_driver_delegated_checkpoint",
    "run_driver_staging_deploy_checkpoint",
    "run_mica_q_sandbox",
    "execute_in_sandbox",
    "sandbox_session_status",
    "terminate_sandbox_session",
    "run_driver_experiment",
    "replay_experiment",
    "get_experiment_quota_status",
    "repo_list_files",
    "repo_grep",
    "repo_read",
})


def get_bio_tool_names() -> frozenset:
    """Return tool names permitted in bio (production) mode.

    All registered names minus FORBIDDEN_BIO_TOOLS. Single authoritative source
    for the bio tool surface. Call this instead of TOOL_CAPABILITY_REGISTRY.keys()
    when building the bio driver tool list.
    """
    return frozenset(TOOL_CAPABILITY_REGISTRY.keys()) - FORBIDDEN_BIO_TOOLS


def get_dev_tool_names() -> frozenset:
    """Return all registered tool names. Development driver has no restriction."""
    return frozenset(TOOL_CAPABILITY_REGISTRY.keys())

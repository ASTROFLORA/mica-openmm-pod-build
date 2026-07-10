"""Bridge between :class:`AgenticLoop` and the WebSocket handler.

Converts AgenticLoop events to Alejandria-compatible WebSocket messages
(STREAM_TOKEN, TEXT_MESSAGE, TOOL_CALL, STATE_UPDATE) and provides a
tool executor that dispatches to the MICA backend API.

Also handles:
- MCP resource injection (auto-injects relevant context before LLM call)
- Expanded tool set (mirrors mica_backend_mcp.py capabilities)
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import quote
from typing import Any, Callable, Awaitable, Dict, List, Optional

from .core import AgenticLoop, LoopConfig, ProviderRegistry
from .events import ResourceInjected
from .execution_mode_selector import AgenticExecutionRequest, AgenticExecutionSelection, select_agentic_execution_mode
from .protocol_cue_injector import ProtocolCueRuntimeManager, ScientificInterrupt
from .runtime_authority_contract import RuntimeAuthorityResolver, authorize_tool_invocation
from .tool_capability_registry import (
    TOOL_CAPABILITY_REGISTRY,
    build_tool_capability_matrix,
    get_tool_input_schema,
    get_tool_capability,
    infer_lmp_state_query_tool_names,
    validate_tool_registry_coverage,
    filter_tools_for_lane,
)
from .backend_command_manifest import (
    canonical_backend_command_name,
    get_backend_command_manifest_entry,
    is_backend_command_name,
    iter_manifest_entries,
)
from .command_kernel import UnifiedAgentCommandKernel
from mica.sdk.command_contracts import BackendCommandEnvelope, BackendCommandPolicy
from mica.tools_authority.tool_surface_exporter import (
    build_manifest_openai_function_tools,
    dedupe_runtime_tool_surface,
    govern_runtime_tool_surface,
)

logger = logging.getLogger(__name__)


def recommend_agentic_execution_mode(
    *,
    tool_names: List[str],
    child_workflow_refs: Optional[List[str]] = None,
    expected_artifacts: Optional[List[str]] = None,
    explicit_lane_hints: Optional[List[str]] = None,
    requires_provider: bool = False,
    durable_replay_required: bool = False,
    estimated_steps: int = 0,
    goal_hint: str = "",
) -> AgenticExecutionSelection:
    """Resolve the canonical APF primitive for an agentic task.

    This keeps `tool` / `protocol` / `gog` selection as an explicit runtime
    policy instead of an implicit prompt convention.
    """

    request = AgenticExecutionRequest(
        tool_names=tuple(tool_names or ()),
        child_workflow_refs=tuple(child_workflow_refs or ()),
        expected_artifacts=tuple(expected_artifacts or ()),
        explicit_lane_hints=tuple(explicit_lane_hints or ()),
        requires_provider=bool(requires_provider),
        durable_replay_required=bool(durable_replay_required),
        estimated_steps=max(0, int(estimated_steps or 0)),
        goal_hint=goal_hint,
    )
    return select_agentic_execution_mode(request)

_PDB_ID_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_UNIPROT_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)
_FENCED_SANDBOX_CODE_RE = re.compile(
    r"^\s*```(?P<language>[A-Za-z0-9_+-]+)?\s*\n(?P<code>[\s\S]*?)\n?```\s*$",
    re.IGNORECASE,
)
_PYTHON_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_FENCED_SANDBOX_LANGUAGE_ALIASES = {
    "bash": "bash",
    "py": "python",
    "python": "python",
    "r": "r",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
}


class _NoOpProtocolRuntimeManager:
    """Minimal protocol manager for trivial WS runtime turns."""

    def __init__(self) -> None:
        self.protocol_events: List[Dict[str, Any]] = []

    def pre_tool_gate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"events": [], "blocked": False}

    def post_tool_gate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"events": [], "blocked": False}

    def finalize(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def runtime_payload(self) -> Dict[str, Any]:
        return {}


def _feed_function_parameters(
    tool_name: str,
    *,
    optional_fields: tuple[str, ...] = (),
) -> Dict[str, Any]:
    schema = get_tool_input_schema(tool_name, optional_fields=optional_fields)
    if schema is None:
        return {"type": "object", "properties": {}, "required": []}
    return schema


def _backend_command_tool_schemas() -> List[Dict[str, Any]]:
    return build_manifest_openai_function_tools()


def _build_generate_lmp_request_payload(
    args: Dict[str, Any], *, default_preset: str = "llm-context"
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "preset": str(args.get("preset") or default_preset or "llm-context").strip() or "llm-context",
    }
    for key in ("pdb_id", "uniprot", "gene", "out_dir"):
        value = str(args.get(key) or "").strip()
        if value:
            payload[key] = value.upper() if key in {"pdb_id", "uniprot"} else value

    for key in ("pdb_ids", "states"):
        value = args.get(key)
        if isinstance(value, list) and value:
            payload[key] = value

    for key in ("validate_xsd", "offline"):
        if key in args:
            payload[key] = bool(args.get(key))

    if not payload.get("pdb_id") and not payload.get("uniprot"):
        query = str(args.get("query") or "").strip()
        if query:
            query_upper = query.upper()
            if _PDB_ID_RE.fullmatch(query):
                payload["pdb_id"] = query_upper
            elif _UNIPROT_ACCESSION_RE.fullmatch(query_upper):
                payload["uniprot"] = query_upper

    if not payload.get("pdb_id") and not payload.get("uniprot"):
        raise ValueError(
            "generate_lmp requires 'pdb_id' or 'uniprot'. Resolve the protein/entity first instead of sending a free-text gene symbol."
        )
    return payload


def _bridge_degraded_tool_response(
    tool_name: str,
    message: str,
    *,
    args_payload: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "error": message,
        "tool": tool_name,
    }
    if args_payload is not None:
        payload["args_payload"] = args_payload
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


def _build_ws_protocol_projection_state(protocol_payload: Dict[str, Any]) -> Dict[str, Any]:
    legacy_protocol_runtime = dict(protocol_payload.get("protocol_runtime") or {})
    unified_protocol_runtime = protocol_payload.get("unified_protocol_runtime")
    if not isinstance(unified_protocol_runtime, dict):
        nested_unified = legacy_protocol_runtime.get("unified_protocol_runtime")
        if isinstance(nested_unified, dict):
            unified_protocol_runtime = nested_unified
    if not isinstance(unified_protocol_runtime, dict):
        alias_unified = protocol_payload.get("unified_runtime")
        if isinstance(alias_unified, dict):
            unified_protocol_runtime = alias_unified

    resolved_unified = dict(unified_protocol_runtime or {})
    projection_source = "runtime.protocol.unified" if resolved_unified else "legacy.protocol_runtime"
    projection_note = ""
    degraded = False

    if resolved_unified:
        if not str(resolved_unified.get("projection_id") or "").strip():
            protocol_id = str(resolved_unified.get("protocol_id") or legacy_protocol_runtime.get("protocol_id") or "unknown-protocol")
            run_id = str(
                resolved_unified.get("run_id")
                or legacy_protocol_runtime.get("run_id")
                or legacy_protocol_runtime.get("session_id")
                or "unknown-run"
            )
            graph_status = str(resolved_unified.get("graph_run_status") or "unknown")
            resolved_unified["projection_id"] = f"ws:{protocol_id}:{run_id}:{graph_status}"
            projection_note = "projection_id synthesized at WS seam because unified payload did not provide one."
    else:
        degraded = True
        projection_note = "Unified projection unavailable; WS payload is using legacy protocol_manager.runtime_payload() compatibility path."

    run_receipts = list(resolved_unified.get("run_receipts") or [])
    node_receipts = list(resolved_unified.get("node_receipts") or [])
    unified_run_status = str(resolved_unified.get("graph_run_status") or "")
    mirrored_run_status = str((run_receipts[0] if run_receipts else {}).get("status") or "")
    terminal_status_conflict = bool(unified_run_status and mirrored_run_status and unified_run_status != mirrored_run_status)
    node_ids_from_projection = sorted(
        str(receipt.get("node_id") or "")
        for receipt in node_receipts
        if str(receipt.get("node_id") or "")
    )
    node_ids_from_run = sorted(
        str(node_id)
        for node_id in list((run_receipts[0] if run_receipts else {}).get("executed_node_ids") or [])
        if str(node_id)
    )
    receipt_ref_match = node_ids_from_projection == node_ids_from_run if run_receipts and node_receipts else not resolved_unified

    return {
        "protocol_runtime": legacy_protocol_runtime,
        "protocol_events": list(protocol_payload.get("protocol_events") or []),
        "scientific_protocol": dict(protocol_payload.get("scientific_protocol") or {}),
        "prompt_protocol": dict(protocol_payload.get("prompt_protocol") or {}),
        "unified_protocol_runtime": resolved_unified,
        "protocol_runtime_projection": {
            "source": projection_source,
            "degraded": degraded,
            "note": projection_note,
            "projection_id": str(resolved_unified.get("projection_id") or ""),
            "terminal_status_conflict": terminal_status_conflict,
            "receipt_ref_match": receipt_ref_match,
            "node_ids_match": node_ids_from_projection == node_ids_from_run if run_receipts and node_receipts else not resolved_unified,
        },
    }

# Lazy singleton
_registry: Optional[ProviderRegistry] = None
_registry_lock = asyncio.Lock()
_ws_toolkg_smoke_checked: bool = False


def _apply_toolkg_selection(
    tools: List[Dict[str, Any]],
    *,
    selected_names: set[str],
    scientific_lane: bool,
) -> List[Dict[str, Any]]:
    """Reduce the WS tool surface from ToolKG output.

    ToolKG returning no tool matches for a non-scientific query should not
    leave the full registry exposed to the provider prompt. That path causes
    prompt inflation and can stall trivial WS runs before the first token.
    """

    if not tools:
        return []

    if not selected_names:
        return tools if scientific_lane else []

    available_tool_names = {
        str(t.get("function", t).get("name") or "").strip()
        for t in tools
        if str(t.get("function", t).get("name") or "").strip()
    }
    always_keep = {
        name
        for name in {"search_protein", "search_literature", "add_to_workspace", "visualize_molecule"}
        if name in available_tool_names
    }
    filtered_names = selected_names | always_keep
    if len(filtered_names) >= len(available_tool_names):
        return tools
    return [
        t for t in tools
        if t.get("function", t).get("name") in filtered_names
    ]


async def get_registry() -> ProviderRegistry:
    """Return or create a :class:`ProviderRegistry` singleton."""
    global _registry
    if _registry is not None:
        return _registry
    async with _registry_lock:
        if _registry is not None:
            return _registry
        try:
            from mica.config.dotenv_loader import seed_env_from_dotenv
            seed_env_from_dotenv()
        except Exception:
            pass
        _registry = ProviderRegistry.from_env()
        return _registry


# ---------------------------------------------------------------------------
# Tool definitions for the MICA backend (OpenAI function-calling format)
# ---------------------------------------------------------------------------

# Core research & analysis tools (7 original)
_CORE_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_protein",
            "description": "Fallback protein metadata lookup by name or UniProt accession. Use only for explicit accession/metadata needs or when LMP resolution fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Protein name, gene symbol, or UniProt ID"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_pdb",
            "description": "Resolve a PDB ID or protein name to a PDB file and extract structural metadata via LMP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "4-letter PDB code (e.g. 2VBH)"},
                    "query": {"type": "string", "description": "Alternative: a protein name to search"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_structure",
            "description": "Analyze a protein structure: domains, binding sites, active residues. Uses LMP XML knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "PDB code to analyze"},
                },
                "required": ["pdb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_literature",
            "description": "Search scientific literature via Semantic Scholar. Returns papers with titles, abstracts, citations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_workspace",
            "description": "Add a file or asset to the user's workspace session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "enum": ["pdb", "document", "image", "data"],
                        "description": "Type of asset",
                    },
                    "name": {"type": "string", "description": "Asset name"},
                    "content": {"type": "string", "description": "Asset content or URL"},
                },
                "required": ["asset_type", "name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "visualize_molecule",
            "description": "Generate a molecular visualization with specific coloring/representation presets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "PDB code"},
                    "preset": {
                        "type": "string",
                        "enum": [
                            "default", "hydrophobicity", "charge", "bfactor",
                            "secondary_structure", "domains", "conservation",
                        ],
                        "description": "Visualization preset",
                    },
                    "highlight_residues": {
                        "type": "string",
                        "description": "Comma-separated residue numbers to highlight",
                    },
                },
                "required": ["pdb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_deep_research",
            "description": "Launch a deep research pipeline on a topic. Returns a job ID for async tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Research topic"},
                    "preset": {
                        "type": "string",
                        "enum": ["quick-scan", "standard", "deep-research", "exhaustive"],
                        "description": "Research depth preset",
                        "default": "standard",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

# Extended tools from mica_backend_mcp.py (most important additions)
_EXTENDED_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "load_knowledge_graph",
            "description": (
                "Load LMP v4 XML knowledge graph for a protein. Returns the FULL biological context "
                "including: Identity, Geometry (secondary structure/DSSP, coordinates), Features "
                "(domains, PTMs, binding sites), Semantics (GO, keywords, xrefs), KnowledgeGraph "
                "(interactions, pathways), NeSyGrammar. This is the CANONICAL source for all "
                "structural and biological context — prefer over external MCP tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "PDB code, UniProt accession, or gene name"},
                },
                "required": ["pdb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_domain_coloring",
            "description": "Extract domain-level coloring data from the KG for 3D visualization. Returns per-residue color assignments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "PDB code"},
                },
                "required": ["pdb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_lmp_presets",
            "description": (
                "List all LMP v4 presets with block composition. Use to choose the right preset: "
                "llm-context (default for research), structural (3D), semantic (lightweight), "
                "nesy-core (PLM), full (all blocks). Each preset controls which XML blocks are generated."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_lmp",
            "description": (
                "Submit an async LMP v4 knowledge graph generation job for a protein/PDB. "
                "Returns a job ID to poll. For scientific audits, mechanistic analysis, and structure-gap reviews, "
                "prefer preset='full'. Use resolve_entity/resolve_pdb first when all you have is a free-text gene or protein name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pdb_id": {"type": "string", "description": "4-letter PDB code when available"},
                    "uniprot": {"type": "string", "description": "UniProt accession when available"},
                    "query": {
                        "type": "string",
                        "description": "Optional alias only when the value is already a PDB code or UniProt accession",
                    },
                    "preset": {
                        "type": "string",
                        "description": (
                            "LMP preset: full (scientific audit default), llm-context (compact research), structural (3D geometry), "
                            "semantic (lightweight), nesy-core (PLM tokens), "
                            "md-ifp (MD trajectories)"
                        ),
                        "default": "full",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_imported_structure",
            "description": (
                "Scan a local/imported PDB through LMP structure-asset infrastructure. "
                "Supports both the inline receipt path and the canonical async queue path. "
                "Returns chain reconstruction, evidence-derived identity status, static contacts, "
                "Bibliotecario handoff strategy, SMIC handoff, and serverless suppression decision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "structure_uri": {"type": "string", "description": "Local, workspace-relative, or file:// PDB path"},
                    "asset_id": {"type": "string", "description": "Optional stable structure asset id"},
                    "workspace_id": {"type": "string", "description": "Optional workspace scope"},
                    "execution_mode": {
                        "type": "string",
                        "enum": ["sync", "async"],
                        "description": "Use async to hit the canonical Redis/worker queue path and poll later for the final materialized receipt.",
                        "default": "sync",
                    },
                    "identity_policy": {
                        "type": "string",
                        "enum": ["local_metadata", "local_then_remote_sequence", "local_then_remote_blast"],
                        "description": "Identity resolution depth. Remote BLAST is explicit and timeout-bounded.",
                        "default": "local_metadata",
                    },
                    "remote_identity_timeout_seconds": {"type": "integer", "description": "Remote identity timeout", "default": 30},
                    "literature_policy": {"type": "object", "description": "Literature handoff policy; defaults require fulltext but do not run async search."},
                    "dlm_policy": {"type": "object", "description": "DLM materialization policy for downstream slices."},
                    "smic_policy": {"type": "object", "description": "SMIC static/execution policy for downstream structural modules."},
                    "serverless_policy": {"type": "object", "description": "Serverless generation suppression/approval policy."},
                    "emit_lmp_xml": {"type": "boolean", "description": "Request downstream LMP XML generation", "default": False},
                    "validate_xsd": {"type": "boolean", "description": "Require XSD validation when XML is emitted", "default": True},
                },
                "required": ["structure_uri"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scan_imported_structure_status",
            "description": (
                "Poll the canonical async imported-structure scan job and return the latest parent receipt. "
                "Use after `scan_imported_structure` with execution_mode='async'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Async imported-structure scan job id"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lmp_state_receipt",
            "description": (
                "Resolve a canonical LMP state_id into a compact structural receipt. "
                "Returns source_kind, structure_origin, AlphaFold metadata, visuals, PocketSites, "
                "optional structure_path, and any cached DynamicsStatistics. "
                "Use this instead of passing raw LMP XML or structural files to the model."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_id": {
                        "type": "string",
                        "description": "Canonical LMP state_id from the annotations manifest",
                    },
                    "allow_afdb_fallback": {
                        "type": "boolean",
                        "description": "When true, compute AFDB-derived PocketSites if the cached XML does not already contain them",
                        "default": True,
                    },
                },
                "required": ["state_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lmp_dynamic_statistics",
            "description": (
                "Resolve only the DynamicsStatistics block for a canonical LMP state_id. "
                "Returns run metadata, dataset references, residue dynamic statistics, and pair dynamic statistics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_id": {
                        "type": "string",
                        "description": "Canonical LMP state_id from the annotations manifest",
                    },
                },
                "required": ["state_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lmp_residue_dynamic_statistics",
            "description": (
                "Resolve a bounded residue-level DynamicsStatistics query for a canonical LMP state_id. "
                "Accepts explicit positions and/or a chain filter plus max_results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_id": {
                        "type": "string",
                        "description": "Canonical LMP state_id from the annotations manifest",
                    },
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
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matched residue stats to return (1-200)",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 200,
                    },
                },
                "required": ["state_id"],
                "anyOf": [
                    {"required": ["positions"]},
                    {"required": ["chain"]},
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lmp_pair_dynamic_statistics",
            "description": (
                "Resolve a bounded pair-level DynamicsStatistics query for a canonical LMP state_id. "
                "Accepts explicit residue pairs and/or chain filters plus max_results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_id": {
                        "type": "string",
                        "description": "Canonical LMP state_id from the annotations manifest",
                    },
                    "pairs": {
                        "type": "array",
                        "minItems": 1,
                        "description": "Explicit pair filters",
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
                    },
                    "chain_i": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Chain filter to match either side of a pair",
                    },
                    "chain_j": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Secondary chain filter to match either side of a pair",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matched pair stats to return (1-200)",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 200,
                    },
                },
                "required": ["state_id"],
                "anyOf": [
                    {"required": ["pairs"]},
                    {"required": ["chain_i"]},
                    {"required": ["chain_j"]},
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lmp_structure_comparison_ledger",
            "description": (
                "Resolve a deterministic AFDB-vs-PDB comparison ledger for a canonical LMP state_id. "
                "Returns a stable ledger_id plus StructureCatalog-derived overlap entries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_id": {
                        "type": "string",
                        "description": "Canonical LMP state_id from the annotations manifest",
                    },
                    "allow_afdb_fallback": {
                        "type": "boolean",
                        "description": "When true, enrich the StructureCatalog with AFDB fallback before building the comparison ledger",
                        "default": True,
                    },
                },
                "required": ["state_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dlm_presets",
            "description": "List available DLM literature scan presets (quick-scan, standard, deep-research, exhaustive, llm-context).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_dlm_graph_repair_export",
            "description": "Run the PDF-bound GraphPatch repair/export lane and return layered graph JSON plus sidecars from the canonical CLI renderer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pdf_path": {"type": "string", "description": "Absolute or server-local path to a PDF"},
                    "output_dir": {"type": "string", "description": "Optional output directory for graph artifacts"},
                    "provider_id": {"type": "string", "description": "LLM provider id", "default": "deepinfra"},
                    "model_id": {"type": "string", "description": "Optional explicit model override"},
                    "max_pages": {"type": "integer", "description": "Max PDF pages to extract", "default": 40},
                    "max_candidates": {"type": "integer", "description": "Optional cap on candidates included in the repair prompt", "default": 0},
                    "tool_budget": {"type": "integer", "description": "Maximum local tool calls allowed in the repair loop", "default": 24},
                    "include_cooccurs": {"type": "boolean", "description": "Carry cooccurrence edges into the baseline graph artifacts", "default": False},
                    "clear_dlm_cache": {"type": "boolean", "description": "Clear DLM cache state before extraction", "default": False},
                },
                "required": ["pdf_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_dlm_scan",
            "description": "Run a DLM literature scan with a specific preset. Searches Semantic Scholar, PubMed, arXiv.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "preset": {"type": "string", "description": "DLM preset name", "default": "standard"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": "Generate a DOCX research report from deep research results. Returns download URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Research topic for the report"},
                    "session_id": {"type": "string", "description": "Session ID with existing research data"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_sessions",
            "description": "List all workspace sessions for the current user.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_assets",
            "description": "List all assets in a specific workspace session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Workspace session ID"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_document",
            "description": (
                "Read and extract text content from a document (PDF, DOCX, or plain text) "
                "stored in the user's workspace. Use list_workspace_assets first to find the "
                "asset_id, then call this tool to read the document content. "
                "Returns the extracted text for analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Workspace session ID containing the document"},
                    "asset_id": {"type": "string", "description": "Asset ID of the document to read"},
                },
                "required": ["session_id", "asset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_workspace_document",
            "description": "Run section-aware DLM scanning over a workspace document and return sections, entities, and candidate claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Workspace session ID containing the document"},
                    "asset_id": {"type": "string", "description": "Asset ID of the document to scan"},
                    "mode": {
                        "type": "string",
                        "enum": ["extract_only", "dlm_sections", "dlm_sections_and_atom"],
                        "default": "dlm_sections",
                    },
                },
                "required": ["session_id", "asset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_scan_status",
            "description": "Fetch the current status/result of a workspace document scan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scan_id": {"type": "string", "description": "Workspace scan ID"},
                },
                "required": ["scan_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_citations_and_references",
            "description": "Get citation graph (papers that cite and are cited by a given paper). Uses Semantic Scholar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string", "description": "Semantic Scholar paper ID, DOI, or PMID"},
                    "direction": {
                        "type": "string",
                        "enum": ["citations", "references", "both"],
                        "default": "both",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["paper_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_knowledge_base",
            "description": "Scan knowledge-base source documents into DLM sections and candidate claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "Knowledge base ID"},
                    "mode": {
                        "type": "string",
                        "enum": ["extract_only", "dlm_sections", "dlm_sections_and_atom"],
                        "default": "dlm_sections",
                    },
                    "session_id": {"type": "string", "description": "Optional workspace session containing source assets"},
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_knowledge_base_scan_status",
            "description": "Poll the current scan status for a knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "Knowledge base ID"},
                },
                "required": ["kb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "promote_knowledge_base_scan",
            "description": "Promote a completed KB scan into ATOM using EvidenceGate-backed promotion checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "Knowledge base ID"},
                    "scan_id": {"type": "string", "description": "Scan ID to promote"},
                    "minimum_evidentiality_score": {"type": "number", "default": 0.5},
                },
                "required": ["kb_id", "scan_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_base_atoms",
            "description": "List promoted ATOM entries for a knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "Knowledge base ID"},
                },
                "required": ["kb_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Real web search via Firecrawl v2. Returns live results from the public web "
                "(not institutional KB). Use this when the literature KB is insufficient or "
                "you need current documentation, release notes, or general web evidence. "
                "Results include title, url, snippet, and category. Anti-mock: fails honestly "
                "if FIRECRAWL_API_KEY is unset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (1-25, default 10)", "default": 10},
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["web", "news", "images"]},
                        "description": "Firecrawl source channels (default ['web'])",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["github", "research", "pdf", "news"]},
                        "description": "Optional category filter",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # ── Slice-2 bootstrap: minimum-viable IDE primitives (read-only) ──
    # Blueprint: tools/r29_runs/_SLICE2_OPERATIONAL_BLINDNESS_BLUEPRINT.md §3
    # Scope: local MICA repo, read-only. No writes, no shell execution.
    {
        "type": "function",
        "function": {
            "name": "repo_list_files",
            "description": (
                "List files in the MICA repo. Scoped to the repo root for safety. "
                "Use this BEFORE web_search when a question is about MICA's own code. "
                "Supports glob filtering. Returns relative paths + sizes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory from repo root (default '.')", "default": "."},
                    "glob": {"type": "string", "description": "Filename glob (e.g. '*.py', '**/test_*.py'). Default '**/*'.", "default": "**/*"},
                    "max_results": {"type": "integer", "description": "Cap (default 200)", "default": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repo_grep",
            "description": (
                "Regex search across the MICA repo (like ripgrep). Returns {path, line_no, line} "
                "matches. Use this to locate a symbol, docstring, or TODO before reading files. "
                "Prefer over blind `repo_read` — grep first, read second."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python-regex pattern (case-insensitive)"},
                    "include_glob": {"type": "string", "description": "File glob to scope (default '**/*.py')", "default": "**/*.py"},
                    "max_results": {"type": "integer", "description": "Cap on matches (default 120)", "default": 120},
                    "path": {"type": "string", "description": "Relative subdir scope (default '.')", "default": "."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repo_read",
            "description": (
                "Read a bounded slice of a MICA repo file. ALWAYS specify start_line/end_line "
                "to avoid huge reads. Returns {path, total_lines, start_line, end_line, content}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "start_line": {"type": "integer", "description": "1-based, default 1", "default": 1},
                    "end_line": {"type": "integer", "description": "1-based inclusive, default start_line+200", "default": 0},
                },
                "required": ["path"],
            },
        },
    },
    # ── Slice-2 §C3: agent-feed tools as first-class LLM surface ──
    # Blueprint: tools/r29_runs/_SLICE2_OPERATIONAL_BLINDNESS_BLUEPRINT.md §C3
    # The native executor branch already exists in agentic_driver.py (R24, 2026-04-21).
    # Before Slice-2 the LLM could not plan a feed call because no schema was published.
    {
        "type": "function",
        "function": {
            "name": "publish_cue",
            "description": (
                "Publish a post to the shared agent feed. Use for decisions, insights, cues, "
                "hypotheses, artifacts, and session lifecycle events. The feed is the A2A (agent-to-agent) "
                "coordination surface — other agents and the operator dashboard read it live. "
                "Valid post_type values: insight | decision | cue | hypothesis | comment | artifact | "
                "tombstone | session_open | session_progress | session_close. Plain names only."
            ),
            "parameters": _feed_function_parameters("publish_cue", optional_fields=("agent_id",)),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_agent_feed",
            "description": (
                "Read recent posts from the shared agent feed with optional filters. Use to observe "
                "what peer agents have published, find open sessions, or audit coordination activity."
            ),
            "parameters": _feed_function_parameters("scroll_agent_feed"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "federated_retrieve",
            "description": (
                "Deprecated compatibility accelerator across the live feed, canonical durable "
                "MemPalace agents palace, KG facts, and first-party graph seams. Prefer direct feed, "
                "direct durable memory, and direct graph tools."
            ),
            "parameters": _feed_function_parameters("federated_retrieve"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_architecture_graph",
            "description": (
                "Search the canonical first-party architecture graph for matching files/directories and their strongest seams. "
                "Preferred structural review tool over broad repo exploration."
            ),
            "parameters": _feed_function_parameters("search_architecture_graph"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_architecture_graph_node",
            "description": (
                "Inspect one node from the canonical first-party architecture graph and list its strongest coupled neighbors."
            ),
            "parameters": _feed_function_parameters("inspect_architecture_graph_node"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_session_signature",
            "description": (
                "Open a coordination session on the agent feed. Emits a session_open post and returns "
                "a session_id to anchor subsequent progress and close posts. Use at the start of any "
                "non-trivial multi-step task."
            ),
            "parameters": _feed_function_parameters("open_session_signature", optional_fields=("agent_id",)),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_session_progress",
            "description": (
                "Append a progress checkpoint to an open session. Use at every meaningful milestone "
                "so operators and peer agents see live progress."
            ),
            "parameters": _feed_function_parameters("update_session_progress", optional_fields=("agent_id",)),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feed_stats",
            "description": (
                "Summary counters over the shared agent feed (total posts, per-topic, per-agent, per-type). "
                "Use for a quick overview of coordination activity."
            ),
            "parameters": _feed_function_parameters("feed_stats"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feed_thread",
            "description": (
                "Return a root post plus its full comment/reply thread (breadth-first). "
                "Use to read a coordination thread end-to-end once you have a root post id."
            ),
            "parameters": _feed_function_parameters("feed_thread"),
        },
    },
]

# Combined tool set
MICA_TOOLS: List[Dict[str, Any]] = govern_runtime_tool_surface(
    _CORE_TOOLS + _EXTENDED_TOOLS + _backend_command_tool_schemas()
)

# User-bucket native tools — direct bucket browsing/reading without workspace proxy
_BUCKET_NATIVE_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_user_bucket_objects",
            "description": (
                "List objects in the user's GCS bucket. Use prefix to filter by namespace "
                "(e.g. 'workspaces/', 'driver_runs/', 'literature/papers/'). "
                "Returns object names and sizes. Set include_metadata=true for content_type and timestamps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Object prefix filter (e.g. 'driver_runs/')", "default": ""},
                    "max_results": {"type": "integer", "description": "Max objects to return (1-5000)", "default": 200},
                    "include_metadata": {"type": "boolean", "description": "Include content_type, updated, md5", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_bucket_object_info",
            "description": "Get metadata for a specific object in the user's GCS bucket (size, content_type, updated, md5, custom metadata).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full object path in the bucket"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_user_bucket_object_text",
            "description": (
                "Read and extract text from an object in the user's GCS bucket. "
                "Supports plain text, JSON, XML, PDB, CIF, Markdown, CSV, PDF (via PyMuPDF/pypdf). "
                "Use this to inspect bucket objects without going through workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full object path in the bucket"},
                    "max_chars": {"type": "integer", "description": "Max characters to return", "default": 80000},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_user_bucket_object_to_workspace",
            "description": (
                "Copy an object from the user's GCS bucket into a workspace session as an asset. "
                "This is the canonical bucket → workspace bridge. The object bytes are copied "
                "and registered as a workspace asset with provenance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Source object path in the bucket"},
                    "workspace_session_id": {"type": "string", "description": "Target workspace session ID"},
                    "asset_type": {
                        "type": "string",
                        "enum": ["pdb", "pdf", "xml", "other"],
                        "description": "Asset type for workspace",
                        "default": "other",
                    },
                    "name": {"type": "string", "description": "Override asset name (defaults to filename)"},
                },
                "required": ["path", "workspace_session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_user_bucket_object",
            "description": "Copy an object within the user's GCS bucket (e.g. from driver_runs/ to exports/).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {"type": "string", "description": "Source object path"},
                    "dest_path": {"type": "string", "description": "Destination object path"},
                },
                "required": ["source_path", "dest_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_user_bucket_content",
            "description": (
                "Multi-pattern text search across objects in the user's GCS bucket using Aho-Corasick. "
                "Pass a list of search terms (protein names, genes, compounds, etc.) and an optional prefix "
                "to scope the search. Returns matching objects ranked by total hits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search terms to match (e.g. ['TP53', 'kinase', 'SH2'])",
                    },
                    "prefix": {"type": "string", "description": "Bucket prefix to scope search", "default": ""},
                    "max_results": {"type": "integer", "description": "Max matching objects", "default": 50},
                },
                "required": ["terms"],
            },
        },
    },
]

# Bibliotecario tools — expose underutilized memory module capabilities
_BIBLIOTECARIO_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_mica_q",
            "description": (
                "Query the public MICA-Q multisurface console. Supports natural-language retrieval plus explicit verbs "
                "such as lit:deep-scan <query>, lit:imported-structure.submit-async <structure_uri>, "
                "lit:imported-structure.status <job_id>, and dlm:graph-repair.export <pdf_path>."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language or explicit MICA-Q console query"},
                    "workspace_id": {"type": "string", "description": "Optional workspace scope for GraphRAG augmentation"},
                    "session_id": {"type": "string", "description": "Optional session scope for GraphRAG augmentation"},
                    "limit": {"type": "integer", "description": "Maximum multisurface hits to return", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bibliotecario_scan",
            "description": "Launch a bibliotecario research scan. Presets: entity-scan (fast entity discovery), "
                           "literature-review (standard review with DOCX), deep-synthesis (citation chasing + enriched XML), "
                           "temporal-evolution (entity trends over time), co-occurrence-map (entity relationship graph), "
                           "pdf-harvest (bulk download PDFs to workspace).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Research query"},
                    "preset": {
                        "type": "string",
                        "enum": ["entity-scan", "literature-review", "deep-synthesis",
                                 "temporal-evolution", "co-occurrence-map", "pdf-harvest"],
                        "description": "Scan preset",
                        "default": "entity-scan",
                    },
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Canonical entity terms supplied by an upstream structural or literature tool.",
                    },
                    "extra_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Supplementary recall queries compiled from LMP/SMIC context or user-supplied search strategy.",
                    },
                    "pdb_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "PDB identifiers that seed structural-context literature expansion.",
                    },
                    "lmp_handoff": {
                        "type": "object",
                        "description": "Structured LMP/SMIC handoff payload for deterministic Bibliotecario query expansion.",
                    },
                    "require_full_text": {
                        "type": "boolean",
                        "description": "When true, full-text capable papers are treated as degraded if only abstract text is available.",
                        "default": True,
                    },
                    "max_papers": {"type": "integer", "description": "Max papers to fetch", "default": 200},
                    "session_id": {"type": "string", "description": "Workspace session to store scan artifacts such as pdf-harvest manifests"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_entity",
            "description": "Resolve an entity name to knowledge base IDs (UniProt for proteins, HGNC for genes, "
                           "MONDO for diseases, DrugBank/ChEMBL for drugs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Entity name (protein, gene, disease, or drug)"},
                    "entity_type": {"type": "string", "enum": ["protein", "gene", "disease", "drug"],
                                    "description": "Type hint"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_atom_facts",
            "description": "Query the ATOM temporal knowledge graph. Retrieves facts (subject-predicate-object quintuples) "
                           "with temporal validity. Use temperature_mode 'focused' for high-confidence or 'exploratory' for novel connections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Filter by entity name"},
                    "predicate": {"type": "string", "description": "Filter by relation type"},
                    "temperature_mode": {"type": "string", "enum": ["focused", "exploratory"], "default": "focused"},
                    "limit": {"type": "integer", "default": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_pdf_to_workspace",
            "description": "Download a research PDF and store it in the user's GCS workspace. "
                           "Can fetch from ArXiv, Semantic Scholar, or a direct URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Direct PDF URL"},
                    "arxiv_id": {"type": "string", "description": "ArXiv paper ID (e.g. 2301.12345)"},
                    "paper_id": {"type": "string", "description": "Semantic Scholar paper ID"},
                    "session_id": {"type": "string", "description": "Workspace session to store the PDF"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_protein_metadata",
            "description": "Search the protein metadata cache. Filter by kinase status, PTMs, domains, binding sites, "
                           "disease associations, or drug targets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text search (protein name, gene, disease)"},
                    "is_kinase": {"type": "boolean", "description": "Filter for kinases only"},
                    "has_ptms": {"type": "boolean", "description": "Filter for proteins with PTMs"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advanced_protein_search",
            "description": "Advanced protein metadata search with pharmacological filters. "
                           "Search by approved drugs, protein family, disease associations, organism, and more.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text search"},
                    "has_approved_drugs": {"type": "boolean", "description": "Only proteins with approved drugs"},
                    "protein_family": {"type": "string", "description": "Protein family: GPCR, Kinase, Ion Channel, etc."},
                    "min_approved_drugs": {"type": "integer", "description": "Minimum number of approved drugs"},
                    "has_disease": {"type": "string", "description": "Disease association substring"},
                    "has_pathway": {"type": "string", "description": "Pathway substring"},
                    "organism": {"type": "string", "description": "Organism name substring"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_serverless_models",
            "description": "List every shared serverless model exposed by the common MICA catalog. "
                           "Use this to inspect all frontend/driver-accessible models without relying on a specialist-specific tool.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_serverless_model",
            "description": "Invoke any shared serverless model from the common MICA catalog using its model_id and typed inputs. "
                           "This is the generic execution surface for driver, specialists, and operator workflows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {"type": "string", "description": "Catalog model id, e.g. proteinmpnn.design.sequence"},
                    "inputs": {"type": "object", "description": "Typed model inputs matching the catalog schema"},
                    "provider_override": {"type": "string", "description": "Optional provider override"},
                    "session_id": {"type": "string", "description": "Optional explicit session id"},
                    "run_id": {"type": "string", "description": "Optional explicit run id"},
                },
                "required": ["model_id", "inputs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "milvus_hybrid_search",
            "description": "Semantic + scalar hybrid search on indexed literature. "
                           "Uses BioBERT embeddings + year/citation filters for precision retrieval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "min_year": {"type": "integer", "description": "Minimum publication year"},
                    "max_year": {"type": "integer", "description": "Maximum publication year"},
                    "min_citations": {"type": "integer", "description": "Minimum citation count"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "milvus_sequence_search",
            "description": "Embed a protein sequence with serverless ESM2 and search Milvus in one flow. "
                           "Reports collection compatibility explicitly and can fall back to a compatible sequence collection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sequence": {"type": "string", "description": "Protein sequence to embed and search"},
                    "model_id": {"type": "string", "default": "esm2.embed.sequence.t30"},
                    "requested_collection_name": {"type": "string", "default": "dctdomain_embeddings"},
                    "fallback_collection_name": {"type": "string", "description": "Optional compatible fallback collection"},
                    "strict_requested_collection": {"type": "boolean", "default": False},
                    "pooling": {"type": "string", "default": "mean"},
                    "normalize_embedding": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["sequence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "milvus_dct_search",
            "description": "Generate DCTdomain fingerprints through the validated RunPod worker and search the Milvus DCT collection in one flow. "
                           "Uses Milvus for candidate retrieval and exact L1 reranking to preserve the local DCT query semantics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sequence": {"type": "string", "description": "Protein sequence to search through the DCTdomain pipeline"},
                    "pid": {"type": "string", "description": "Optional identifier for the query sequence"},
                    "collection_name": {"type": "string", "default": "dctdomain_embeddings"},
                    "runpod_endpoint_id": {"type": "string", "description": "Optional override for the RunPod DCT endpoint"},
                    "maxlen": {"type": "integer", "default": 500},
                    "threshold": {"type": "number", "default": 2.6},
                    "candidate_limit": {"type": "integer", "default": 50},
                    "wait_ms": {"type": "integer", "default": 300000},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["sequence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "milvus_stored_embedding_search",
            "description": "Reuse an embedding already stored in Milvus to search the same or another Milvus collection. "
                           "Avoids recomputing embeddings for proteins already indexed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "protein_id": {"type": "string", "description": "Protein ID whose stored embedding will be reused"},
                    "source_collection_name": {"type": "string", "default": "protein_sequences_embeddings"},
                    "target_collection_name": {"type": "string"},
                    "exclude_source": {"type": "boolean", "default": True},
                    "normalize_query": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["protein_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cascade_pipeline",
            "description": "Launch the full cascade super pipeline: literature scan → entity extraction → "
                           "KB resolution → ATOM quintuples → Milvus indexing → pharma enrichment → DLM-LMP convergence. "
                           "Returns a job_id for polling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Protein name, UniProt ID, or research query"},
                    "uniprot_id": {"type": "string", "description": "UniProt accession for targeted scan"},
                    "preset": {"type": "string", "enum": ["quick-scan", "standard", "deep-research", "exhaustive"],
                               "default": "standard", "description": "DLM scan depth preset"},
                    "max_papers": {"type": "integer", "default": 200, "description": "Max papers to fetch"},
                    "enable_milvus": {"type": "boolean", "default": True},
                    "enable_pharma": {"type": "boolean", "default": True},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enrich_protein_pharma",
            "description": "Enrich a protein with pharmacological data from DrugBank/ChEMBL/OpenTargets. "
                           "Returns approved drugs, clinical trials, mechanisms of action, ChEMBL IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uniprot_id": {"type": "string", "description": "UniProt accession ID (e.g. P04637)"},
                },
                "required": ["uniprot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_co_occurrence",
            "description": "Find papers where multiple entities co-occur using the ATOM knowledge graph. "
                           "Builds a co-occurrence matrix across all indexed literature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of entity names to find co-occurrences for",
                    },
                    "min_papers": {"type": "integer", "default": 1, "description": "Minimum shared papers for a pair"},
                },
                "required": ["entities"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "track_entity_evolution",
            "description": "Track how an entity's mentions and associations evolved over publication years. "
                           "Returns yearly mention counts, co-occurring entities, publication trends.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity name"},
                    "entity_type": {"type": "string", "description": "Entity type (protein, gene, drug, disease)"},
                    "start_year": {"type": "integer", "description": "Start year", "default": 2015},
                    "end_year": {"type": "integer", "description": "End year", "default": 2025},
                },
                "required": ["entity"],
            },
        },
    },
    # --- P0–P3 novel feature tools ---
    {
        "type": "function",
        "function": {
            "name": "generate_hypotheses",
            "description": "Generate research hypotheses based on knowledge-graph gap analysis. "
                           "Finds unexplored links between entities and scores them by intermediary evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Seed entity names (proteins, genes, drugs, diseases)",
                    },
                    "max_hypotheses": {"type": "integer", "default": 10},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["entities"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compile_research_briefing",
            "description": "Compile a structured multi-section research briefing from scan results. "
                           "Produces executive summary, key findings, entity landscape, pharma context, timeline, and gaps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Research query or protein name"},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_drug_repurposing",
            "description": "Scan for drug repurposing opportunities via 3 strategies: co-occurrence intermediary, "
                           "literature trend detection, and cross-indication analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "protein": {"type": "string", "description": "Target protein name (e.g. EGFR, ABL1)"},
                    "max_alerts": {"type": "integer", "default": 20},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["protein"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyse_citation_impact",
            "description": "Analyse citation velocity, detect publication bursts (2σ), and identify sleeping-beauty papers "
                           "for an entity. Computes recency-weighted impact scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity name"},
                    "entity_type": {"type": "string", "enum": ["protein", "gene", "drug", "disease"],
                                    "default": "protein"},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["entity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyse_knowledge_decay",
            "description": "Analyse confidence erosion of knowledge-graph facts using a thermodynamic decay model. "
                           "Detects contradicted, stale, or well-confirmed facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity to analyse"},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["entity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "map_conformational_landscape",
            "description": "Map the conformational landscape of a protein across all PDB structures. "
                           "Classifies apo/holo/active/inactive states with resolution ranking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uniprot_id": {"type": "string", "description": "UniProt accession (e.g. P00519)"},
                    "gene_name": {"type": "string", "description": "Gene symbol (e.g. ABL1)"},
                    "max_structures": {"type": "integer", "default": 50},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_pharmacovigilance",
            "description": "Detect post-market safety signals from openFDA FAERS data. "
                           "Computes PRR disproportionality and identifies shared adverse events across drugs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Protein or drug name"},
                    "entity_type": {"type": "string", "enum": ["protein", "drug"], "default": "protein"},
                    "max_drugs": {"type": "integer", "default": 10},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["entity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_ortholog_dashboard",
            "description": "Build a cross-species ortholog dashboard. Shows conservation scores, PDB availability, "
                           "and disease annotations across 10 model organisms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "gene_name": {"type": "string", "description": "Gene symbol (e.g. ABL1)"},
                    "species": {"type": "string", "default": "homo_sapiens"},
                    "preset": {"type": "string", "description": "DLM preset: quick-scan, standard, deep-research, exhaustive, llm-context"},
                },
                "required": ["gene_name"],
            },
        },
    },
]

MICA_TOOLS = govern_runtime_tool_surface(MICA_TOOLS + _BIBLIOTECARIO_TOOLS)

# ── Citation verification tool ──────────────────────────────────────────────
_CITATION_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "verify_citations",
            "description": (
                "Verify DOIs and PMIDs against CrossRef/DataCite/NCBI registries. "
                "Returns integrity score, per-citation status (verified/not_found/retracted), "
                "and flags papers that have been retracted. Use BEFORE delivering any research "
                "output to ensure citation accuracy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text containing DOIs (10.xxxx/...) and/or PMIDs to extract and verify",
                    },
                    "dois": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit list of DOIs/PMIDs to verify (alternative to text extraction)",
                    },
                },
            },
        },
    },
]

MICA_TOOLS = govern_runtime_tool_surface(MICA_TOOLS + _CITATION_TOOLS)

# ---------------------------------------------------------------------------
# Product object tools — Study, WorkingSet, KB, Artifact (2026-06-14)
# ---------------------------------------------------------------------------

_PRODUCT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "kb_list",
            "description": "List available knowledge bases (KBs). Use this before kb_semantic_query when you need a real KB id instead of guessing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "public": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true, list only public KBs instead of private workspace KBs.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "study_create",
            "description": "Create a durable scientific project container (Study). Studies group investigations around a topic. Required for project-level scientific work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Study name (1-200 chars)"},
                    "description": {"type": "string", "description": "Optional description (max 2000 chars)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "study_get",
            "description": "Get a durable Study by ID, including metadata and artifact counts. Use this after study_create or when resuming work on an existing Study.",
            "parameters": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID"},
                },
                "required": ["study_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "study_attach_resource",
            "description": "Attach an artifact to a Study. Artifacts must be created first via artifact_create. Use this to link papers, manifests, structures, and reports to a Study.",
            "parameters": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study ID"},
                    "artifact_id": {"type": "string", "description": "Artifact ID to attach (created via artifact_create)"},
                },
                "required": ["study_id", "artifact_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "working_set_create",
            "description": "Create a WorkingSet for an active scientific task inside a Study. Use this to hold the bounded artifact context the agent is currently manipulating.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "WorkingSet name"},
                    "study_id": {"type": "string", "description": "Optional parent Study ID"},
                    "description": {"type": "string", "description": "Optional WorkingSet description"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "working_set_attach_resource",
            "description": "Attach an artifact to a WorkingSet. Use this after artifact_create when you want the current task context to retain that artifact explicitly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "working_set_id": {"type": "string", "description": "WorkingSet ID"},
                    "artifact_id": {"type": "string", "description": "Artifact ID to attach"},
                    "artifact_ref_type": {"type": "string", "description": "Optional explicit ref type override"},
                    "position": {"type": "integer", "default": 0},
                    "config": {"type": "object", "description": "Optional item config"},
                },
                "required": ["working_set_id", "artifact_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lab_create",
            "description": "Create a Laboratory root for durable collaboration, knowledge, and studies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "display_name": {"type": "string", "description": "Laboratory display name"},
                    "description": {"type": "string", "description": "Optional laboratory description"},
                    "org_ref": {"type": "string", "description": "Optional organization reference"},
                    "metadata": {"type": "object", "description": "Optional lab metadata"},
                },
                "required": ["display_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lab_list",
            "description": "List laboratories visible to the current caller.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lab_get",
            "description": "Get one laboratory by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lab_id": {"type": "string", "description": "Laboratory ID"},
                },
                "required": ["lab_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_space_create",
            "description": "Create a durable Knowledge Space inside a Laboratory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lab_id": {"type": "string", "description": "Owning laboratory ID"},
                    "display_name": {"type": "string", "description": "Knowledge Space display name"},
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "primary_parent_space_id": {"type": "string"},
                    "review_cadence": {"type": "string"},
                    "health_status": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["lab_id", "display_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_space_list",
            "description": "List Knowledge Spaces visible to the current caller.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lab_id": {"type": "string"},
                    "archived": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_space_get",
            "description": "Get one Knowledge Space by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "space_id": {"type": "string", "description": "Knowledge Space ID"},
                },
                "required": ["space_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_space_create_membership",
            "description": "Create a membership edge between a Knowledge Space and another Space or KB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "space_id": {"type": "string", "description": "Parent Knowledge Space ID"},
                    "child_space_id": {"type": "string"},
                    "member_kb_ref": {"type": "string"},
                    "relation_type": {"type": "string"},
                    "expansion_policy": {"type": "string"},
                    "primary_parent": {"type": "boolean"},
                    "metadata": {"type": "object"},
                },
                "required": ["space_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_line_create",
            "description": "Create a durable Research Line for long-horizon scientific direction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lab_id": {"type": "string", "description": "Owning laboratory ID"},
                    "display_name": {"type": "string", "description": "Research Line display name"},
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "primary_question": {"type": "string"},
                    "status": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["lab_id", "display_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_line_list",
            "description": "List Research Lines visible to the current caller.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lab_id": {"type": "string"},
                    "archived": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_line_get",
            "description": "Get one Research Line by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string", "description": "Research Line ID"},
                },
                "required": ["line_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_line_link_study",
            "description": "Attach an existing Study to a Research Line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string", "description": "Research Line ID"},
                    "study_id": {"type": "string", "description": "Study ID"},
                },
                "required": ["line_id", "study_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_create",
            "description": "Create a semantic knowledge base (KB) over documents/artifacts. KBs support semantic queries using BioLinkBERT embeddings + Milvus vector search. Create a KB before ingesting documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "KB name (1-200 chars)"},
                    "kb_type": {"type": "string", "enum": ["query", "entity", "topic", "proteome", "project", "report_derived"], "default": "query", "description": "KB type"},
                    "canonical_query": {"type": "string", "description": "Canonical query describing what this KB covers"},
                    "target_entities": {"type": "array", "items": {"type": "string"}, "description": "Entities this KB focuses on"},
                    "target_topics": {"type": "array", "items": {"type": "string"}, "description": "Topics this KB covers"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_semantic_query",
            "description": "Run a semantic query over a KB using BioLinkBERT embeddings + Milvus vector search. Use this to find documents by meaning (not just keywords). Requires KB to exist with ingested documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "KB ID to query"},
                    "query": {"type": "string", "description": "Natural language query"},
                    "mode": {"type": "string", "enum": ["semantic", "keyword", "hybrid"], "default": "semantic", "description": "Search mode"},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
                "required": ["kb_id", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "artifact_create",
            "description": "Create a persisted artifact with provenance. Artifacts are durable objects stored in GCS with SHA256 hashing. Use for reports, evidence tables, manifests, and any output that needs durable storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_type": {"type": "string", "description": "Type: report, manifest, evidence_table, structure, paper, lmp_xml, note"},
                    "display_name": {"type": "string", "description": "Human-readable name"},
                    "ref_url": {"type": "string", "description": "Reference URL or resource URI (mica://...) for this artifact"},
                    "source": {"type": "string", "description": "Source: generated, imported, extracted"},
                    "mime_type": {"type": "string", "description": "MIME type (application/json, text/plain, etc.)"},
                },
                "required": ["artifact_type", "display_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "artifact_signed_url",
            "description": "Get a signed GCS download URL for an artifact. URLs expire after 15 minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "description": "Artifact ID"},
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_agent_message",
            "description": "Publish a typed message to the inter-agent bus. Used by Bibliotecario to emit findings/manifests/snippets to Driver, and by Driver to send follow-up requests. This is the primary inter-agent collaboration surface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_type": {"type": "string", "enum": ["finding", "manifest_ready", "snippet_ready", "followup_request", "tool_use_summary", "blocker", "lrr_include", "final_receipt"], "description": "Type of agent message"},
                    "summary": {"type": "string", "description": "Bounded summary (max 500 chars, no raw dumps)"},
                    "manifest_uri": {"type": "string", "description": "Optional DLM manifest URI"},
                    "snippet_uri": {"type": "string", "description": "Optional bounded snippet URI"},
                    "resource_refs": {"type": "array", "items": {"type": "string"}, "description": "mica:// resource URIs referenced"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Confidence score (0-1)"},
                    "to_agent": {"type": "string", "description": "Target agent: driver, bibliotecario, broadcast"},
                },
                "required": ["message_type", "summary"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graphrag_query",
            "description": "Query the MICA GraphRAG scientific memory layer. Searches across entities, claims, and relationships using hybrid FTS+vector search. Use this to find connected scientific facts, not just text passages. Returns graph facts with provenance (source_doi, claim_key).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {"type": "string", "description": "Natural language query (e.g., 'EGFR resistance mechanisms in colorectal cancer')"},
                    "study_id": {"type": "string", "description": "Optional study scope filter"},
                    "kb_id": {"type": "string", "description": "Optional KB scope filter"},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
                "required": ["query_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graphrag_hop1",
            "description": "Traverse 1-hop neighbors in the GraphRAG scientific graph. Given seed entities, returns all connected entities and their relationships. Use to expand a set of genes/proteins/diseases into their graph context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seed_nodes": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20, "description": "Entity names to expand from"},
                    "study_id": {"type": "string", "description": "Optional study scope filter"},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
                },
                "required": ["seed_nodes"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graphrag_write_claim",
            "description": "Write a scientific claim to GraphRAG with provenance. Claims are deduplicated by SHA256 content hash. Every claim requires source attribution (DOI or resource URI). Use this to persist findings from literature into the product graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study scope (recommended)"},
                    "kb_id": {"type": "string", "description": "KB scope (optional)"},
                    "content": {"type": "string", "description": "The scientific claim text (1-2000 chars, no raw dumps)"},
                    "fact_type": {"type": "string", "enum": ["finding", "claim", "observation", "hypothesis"], "default": "finding", "description": "Type of claim"},
                    "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names mentioned in the claim"},
                    "source_doi": {"type": "string", "description": "DOI of the source paper"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 1.0},
                },
                "required": ["content", "study_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graphrag_write_lmp_graph",
            "description": "Parse a BUDO/LMP v4 biological knowledge graph file and persist its protein, domain, pathway, disease, and pharmacology nodes/edges into GraphRAG. Links the graph to a Study for product-scoped queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study to link the LMP graph to"},
                    "lmp_file": {"type": "string", "description": "LMP v4 XML filename (e.g., 'sp_P00533_EGFR_HUMAN_Inactive.xml')"},
                    "max_edges": {"type": "integer", "default": 500, "minimum": 1, "maximum": 2000},
                },
                "required": ["study_id", "lmp_file"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graphrag_export_decision_subgraph",
            "description": "Export a bounded subgraph from GraphRAG suitable for a Scientific Decision Card. Combines semantic search + graph traversal to produce a focused evidence graph with provenance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string", "description": "Study to scope the export"},
                    "query_focus": {"type": "string", "description": "What question should the subgraph answer?"},
                },
                "required": ["query_focus"],
                "additionalProperties": False,
            },
        },
    },
]

MICA_TOOLS = govern_runtime_tool_surface(MICA_TOOLS + _PRODUCT_TOOLS)

_SANDBOX_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_driver_delegated_checkpoint",
            "description": (
                "Run a bounded driver-owned delegated modify+test checkpoint on a disposable probe file. "
                "The driver creates an allowlisted probe under tmp/driver_owned_checkpoints, delegates one edit "
                "through the fenced GHP executor, preserves the candidate artifact tree, and runs focused validation "
                "against that candidate. Use this to prove the Level-5 modify+test loop without touching source files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_root": {
                        "type": "string",
                        "description": "Workspace root where the disposable probe artifacts will be created. Defaults to the current working directory.",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Natural-language objective recorded on the delegated task and checkpoint ledger.",
                    },
                    "probe_name": {
                        "type": "string",
                        "description": "Short label for the disposable probe run. Used to name the artifact directory.",
                    },
                    "initial_value": {
                        "type": "integer",
                        "description": "Initial return value written into the disposable probe module before delegation.",
                    },
                    "updated_value": {
                        "type": "integer",
                        "description": "Return value the delegated runner must write into the preserved candidate.",
                    },
                    "apply_same_diff": {
                        "type": "boolean",
                        "description": "When true, apply the same preserved unified diff back to the workspace probe under rollback control after candidate validation passes.",
                    },
                    "target_relative_path": {
                        "type": "string",
                        "description": "Optional repo-relative target file to mutate instead of the disposable tmp probe. Use this for bounded real-file lineage slices.",
                    },
                    "target_callable_name": {
                        "type": "string",
                        "description": "Callable imported from the candidate/apply target during focused validation. Defaults to delegated_value.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_driver_staging_deploy_checkpoint",
            "description": (
                "Package the current API deploy slice into a preserved driver-owned candidate root, run focused validation "
                "against that candidate, and deploy staging from that same candidate root. Use this to prove candidate->validation->deploy continuity instead of deploying directly from the live workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_root": {
                        "type": "string",
                        "description": "Workspace root that owns the deployment slice and the tmp/driver_owned_checkpoints artifacts.",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Natural-language objective recorded on the checkpoint ledger.",
                    },
                    "candidate_name": {
                        "type": "string",
                        "description": "Short label for the preserved candidate root.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Railway project UUID for the staging target.",
                    },
                    "environment_id": {
                        "type": "string",
                        "description": "Railway environment UUID for the staging target.",
                    },
                    "staging_service": {
                        "type": "string",
                        "description": "Railway service name to deploy (defaults to mica-driver-staging).",
                    },
                    "public_base_url": {
                        "type": "string",
                        "description": "Public base URL used for health/readiness smoke probes.",
                    },
                    "readiness_url": {
                        "type": "string",
                        "description": "Optional explicit readiness URL. Defaults to <public_base_url>/api/v1/readiness.",
                    },
                    "py_compile_targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Candidate-relative Python files to compile before deploy.",
                    },
                    "pytest_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "pytest arguments executed inside the preserved candidate root before deploy.",
                    },
                    "deployment_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional allowlist patterns copied into the candidate root. Defaults to the API deploy slice plus tests when pytest_args are present.",
                    },
                    "upstream_checkpoint_result_path": {
                        "type": "string",
                        "description": "Optional path to an upstream delegated checkpoint_result.json. When provided, staging verifies candidate->apply->staging continuity for that same lineage.",
                    },
                    "commit_sha": {
                        "type": "string",
                        "description": "Optional commit label recorded on the deploy candidate. Defaults to git rev-parse --short HEAD or the run id fallback.",
                    },
                    "max_wall_seconds": {
                        "type": "integer",
                        "description": "Maximum wall-clock budget for validation and deploy orchestration.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, preserve/validate the candidate root but do not perform a live deploy.",
                    },
                    "cli_bin": {
                        "type": "string",
                        "description": "Optional Railway CLI binary override.",
                    },
                    "api_token": {
                        "type": "string",
                        "description": "Optional explicit Railway API token. Omit to prefer the shell-authenticated CLI session.",
                    },
                },
                "required": ["workspace_root", "project_id", "environment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_mica_q_sandbox",
            "description": (
                "Run sandbox/code/dataset work through the canonical MICA-Q sandbox lane. "
                "Use this when the request is generic coding, scripting, or dataset processing rather than MD. "
                "Provide executable code directly, or provide a language-specific snippet in `request` for quick one-liners."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": (
                            "MICA-Q sandbox request or a language-specific one-liner. "
                            "If `code` is omitted and this is a runnable python/bash/R snippet or fenced code block, it will be executed directly. "
                            "Otherwise bounded dataset/code requests may be synthesized into explicit code before sandbox execution."
                        ),
                    },
                    "code": {
                        "type": "string",
                        "description": "Executable code to run inside the sandbox. Preferred for multi-line or non-trivial work.",
                    },
                    "code_ref": {
                        "type": "string",
                        "description": (
                            "Optional reference to uploaded source code file name (from upload_files) to execute when `code` is omitted."
                        ),
                    },
                    "workload_kind": {
                        "type": "string",
                        "enum": ["analysis", "code", "dataset"],
                        "description": "High-level workload class. Defaults from the MICA-Q sandbox normalizer.",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "r"],
                        "description": "Execution language. Default: python.",
                    },
                    "allow_synthesis": {
                        "type": "boolean",
                        "description": "Allow governed code synthesis for natural-language dataset/code requests when `code` is omitted. Default: true.",
                    },
                    "synthesis_provider": {
                        "type": "string",
                        "enum": ["deepinfra", "fireworks"],
                        "description": "Preferred LLM provider for governed synthesis. Default: deepinfra with explicit fallback to the other configured provider.",
                    },
                    "gpu": {
                        "type": "string",
                        "enum": ["T4", "L4", "A10G", "A100", "A100-80GB", "H100"],
                        "description": "Optional GPU type when the workload needs accelerator-backed execution.",
                    },
                    "preset": {
                        "type": "string",
                        "enum": [
                            "md-openmm", "md-gromacs", "structure",
                            "ml-torch", "analysis", "chronosfold",
                        ],
                        "description": "Optional sandbox image preset.",
                    },
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional pip packages to install beyond the preset.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 300, max 3600).",
                    },
                    "cpu": {
                        "type": "number",
                        "description": "Guaranteed CPU request in cores for the sandbox runtime.",
                    },
                    "memory_mb": {
                        "type": "integer",
                        "description": "Guaranteed memory request in MB for the sandbox runtime.",
                    },
                    "memory_limit_mb": {
                        "type": "integer",
                        "description": "Optional hard memory limit in MB. Must be >= memory_mb.",
                    },
                    "storage_mb": {
                        "type": "integer",
                        "description": "Optional ephemeral disk size in MiB. Modal defaults to 512 GiB and caps explicit requests at 3 TiB.",
                    },
                    "upload_files": {
                        "type": "object",
                        "description": "Files to upload as {filename: base64-encoded content}.",
                    },
                    "download_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filenames to retrieve after execution.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Reuse a named sandbox session across multiple calls.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory inside the sandbox. Default: /sandbox.",
                    },
                },
                "required": ["request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_in_sandbox",
            "description": (
                "Execute arbitrary code in an isolated cloud sandbox (Modal). "
                "Use for computations that require:\n"
                "  1. GPU (MD simulations, ML inference, structure prediction)\n"
                "  2. Packages not available in the agent's environment\n"
                "  3. Long-running processes (up to 1 hour)\n"
                "  4. Isolated filesystem for intermediate files\n"
                "\n"
                "The sandbox is ephemeral — code runs in a fresh container unless "
                "session_id is provided for multi-step workflows.\n"
                "\n"
                "WHEN TO USE vs. predefined tools:\n"
                "  • Use execute_in_sandbox for custom analysis, pipelines, "
                "plotting, or anything not covered by existing MICA tools.\n"
                "  • Use predefined tools (search_protein, analyze_structure, etc.) "
                "when they directly answer the need — they're faster and typed.\n"
                "\n"
                "Available presets: md-openmm, md-gromacs, structure, ml-torch, "
                "analysis, chronosfold. If no preset is given, auto-detection is attempted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python/bash/R code to execute. Must be self-contained "
                            "(include all imports). Print results to stdout."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "r"],
                        "description": "Code language. Default: python.",
                    },
                    "gpu": {
                        "type": "string",
                        "enum": ["T4", "L4", "A10G", "A100", "A100-80GB", "H100"],
                        "description": (
                            "GPU type. Use T4 for basic MD / structure. "
                            "A10G for moderate ML. A100/H100 for large-scale inference. "
                            "Omit for CPU-only tasks."
                        ),
                    },
                    "preset": {
                        "type": "string",
                        "enum": [
                            "md-openmm", "md-gromacs", "structure",
                            "ml-torch", "analysis", "chronosfold",
                        ],
                        "description": (
                            "Pre-configured environment with relevant packages. "
                            "Auto-detected from code if omitted."
                        ),
                    },
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional pip packages to install beyond the preset.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 300, max 3600).",
                    },
                    "cpu": {
                        "type": "number",
                        "description": "Guaranteed CPU request in cores for the sandbox runtime.",
                    },
                    "memory_mb": {
                        "type": "integer",
                        "description": "Guaranteed memory request in MB for the sandbox runtime.",
                    },
                    "memory_limit_mb": {
                        "type": "integer",
                        "description": "Optional hard memory limit in MB. Must be >= memory_mb.",
                    },
                    "storage_mb": {
                        "type": "integer",
                        "description": "Optional ephemeral disk size in MiB. Modal defaults to 512 GiB and caps explicit requests at 3 TiB.",
                    },
                    "upload_files": {
                        "type": "object",
                        "description": (
                            "Files to upload: {filename: base64-encoded content}. "
                            "Use for PDB files, trajectories, configs, etc."
                        ),
                    },
                    "download_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filenames to retrieve after execution (plots, results, etc.)."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Reuse a named sandbox across multiple calls. "
                            "Use for multi-step workflows (e.g., setup → run → analyze). "
                            "Omit for one-shot executions."
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_session_status",
            "description": (
                "Check the status of active sandbox sessions. "
                "Returns active session IDs, execution history, and cumulative cost."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminate_sandbox_session",
            "description": (
                "Terminate a specific sandbox session to free resources and stop billing. "
                "Use after completing a multi-step sandbox workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session_id to terminate.",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_driver_experiment",
            "description": (
                "TIER-3 DRIVER SELF-EXPERIMENTATION. Run a bounded command inside a "
                "disposable Modal sandbox that has a fresh clone of MICA and "
                "operator-allow-listed secrets injected as environment variables. "
                "The sandbox process never returns raw secret VALUES — stdout/stderr/readback "
                "is scrubbed server-side. Use this to TEST THE DRIVER'S OWN CODE, run "
                "MICA pytest, or invoke tools/mica_agent.py recursively with a bounded "
                "step budget.\n\n"
                "WHEN TO USE:\n"
                "  • Verify a hypothesis about MICA code (py_compile, unit test, import chain).\n"
                "  • Reproduce a runtime bug in a clean environment.\n"
                "  • Run mica_agent.py with a different provider/prompt to compare.\n\n"
                "WHAT YOU CANNOT DO:\n"
                "  • Request arbitrary secret names — only mica-driver-dev, mica-driver-db, "
                "mica-driver-gcs, mica-driver-feed are allowed.\n"
                "  • Nest another run_driver_experiment (one recursion level maximum).\n"
                "  • Exceed 15 min wall time / 4 vCPU / 8 GB memory.\n\n"
                "TESTIMONY: The driver publishes a hypothesis feed post BEFORE and an "
                "insight feed post AFTER, linked by parent_id — so the experiment is "
                "reconstructible from the feed alone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis": {
                        "type": "string",
                        "description": (
                            "One-sentence statement of what this experiment will prove or "
                            "falsify. Published to the feed as a hypothesis post."
                        ),
                    },
                    "command_argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "argv to execute inside /workspace/mica. Example: "
                            "['python', '-m', 'py_compile', 'src/mica/agentic/ws_bridge.py']."
                        ),
                    },
                    "git_sha": {
                        "type": "string",
                        "description": "Optional commit SHA to checkout after cloning.",
                    },
                    "secret_names": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "mica-driver-dev", "mica-driver-db",
                                "mica-driver-gcs", "mica-driver-feed",
                            ],
                        },
                        "description": (
                            "Allow-listed secret names to inject as env vars. "
                            "VALUES never returned."
                        ),
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Wall-time cap in seconds (max 900; max 300 for recursive mica_agent).",
                    },
                    "readback_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Relative paths (under /workspace/mica) to read back after the "
                            "command completes. Max 20 files, 1 MB each. Content is scrubbed."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Reuse the same sandbox across multiple experiments (saves git "
                            "clone time). Must be terminated via terminate_sandbox_session."
                        ),
                    },
                    "snapshot_on_pass": {
                        "type": "boolean",
                        "description": "If exit_code==0, snapshot the sandbox filesystem for later reuse.",
                    },
                    "install_mica_deps": {
                        "type": "boolean",
                        "description": "If true, pip install -r requirements_worker.txt after clone.",
                    },
                },
                "required": ["hypothesis", "command_argv"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replay_experiment",
            "description": (
                "Re-run a previously snapshotted driver experiment from its Modal "
                "filesystem snapshot and diff the output against the recorded run. "
                "Returns verdict 'same' | 'divergent' | 'no_snapshot' | 'not_found'. "
                "Use this to prove determinism claims or detect flaky behaviour."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "ID returned by a previous run_driver_experiment call.",
                    },
                },
                "required": ["experiment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_experiment_quota_status",
            "description": (
                "Return read-only driver sandbox quota usage for a session bucket: "
                "used/remaining count and USD cost against the configured per-session "
                "ceilings. Use before launching a batch of experiments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session bucket to inspect. Omit for the default bucket.",
                    },
                },
                "required": [],
            },
        },
    },
]

MICA_TOOLS = govern_runtime_tool_surface(MICA_TOOLS + _SANDBOX_TOOLS)

# Bucket-native tools
MICA_TOOLS = govern_runtime_tool_surface(MICA_TOOLS + _BUCKET_NATIVE_TOOLS)


# ---------------------------------------------------------------------------
# KB Operations tools -- day-2 control plane for all K5 services
# ---------------------------------------------------------------------------

_KB_OPS_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "kb_job_submit",
            "description": (
                "Submit a KB operations job (quant_backfill, tier_recompute, retraction_batch, etc.). "
                "Requires scope_ref and idempotency_key. Returns job_ref and accepted receipt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_kind": {"type": "string", "description": "Job type: quant_backfill, tier_recompute, retraction_batch"},
                    "scope_ref": {"type": "string", "description": "Scope reference, e.g. workspace://lab_wnk"},
                    "idempotency_key": {"type": "string", "description": "Idempotency key (sha256). Required."},
                    "budget_ref": {"type": "string", "description": "Optional budget reference"},
                },
                "required": ["job_kind", "scope_ref", "idempotency_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_job_status",
            "description": "Get status of a KB operations job. Returns job_ref, status, shard progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_ref": {"type": "string", "description": "Job reference"},
                },
                "required": ["job_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_job_cancel",
            "description": "Cancel a pending or running KB operations job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_ref": {"type": "string", "description": "Job reference to cancel"},
                },
                "required": ["job_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_slo_report",
            "description": "Generate SLO report for KB services. Shows SLI values, error budget, incidents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Optional scope filter, e.g. kb.quant"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_migration_backfill",
            "description": "Submit an idempotent unit registry backfill job. Shadow dual-write, cutover gate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope_ref": {"type": "string", "description": "Scope reference"},
                    "target_version": {"type": "string", "description": "Target registry version, e.g. v2"},
                    "source_version": {"type": "string", "description": "Source registry version"},
                    "idempotency_key": {"type": "string", "description": "Optional idempotency key"},
                },
                "required": ["scope_ref", "target_version"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_projection_status",
            "description": "Check projection health (PROV-O/Neo4j/Milvus). Returns state and drift.",
            "parameters": {
                "type": "object",
                "properties": {
                    "projection_ref": {"type": "string", "description": "Optional projection reference"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_retention_report",
            "description": "Generate asof index retention report. Hot/warm/cold counts, bloat, transitions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

MICA_TOOLS = govern_runtime_tool_surface(MICA_TOOLS + _KB_OPS_TOOLS)


# ---------------------------------------------------------------------------
# Resource injection helper
# ---------------------------------------------------------------------------

async def _inject_resources(user_text: str, mcp_sessions: dict | None = None) -> tuple[str, int, int]:
    """Attempt to inject MCP resources into the query based on detected entities.

    Returns ``(augmented_text, resource_count, total_chars)``
    where augmented_text is the original text possibly prepended with context.
    Falls back gracefully if resources_fabric is unavailable.
    """
    try:
        from mica.mcp.resources_fabric import MCPResourceGateway, format_resource_context
    except ImportError:
        return user_text, 0, 0

    if not mcp_sessions:
        # Try to get sessions from the global driver if available
        try:
            from mica.drivers import agentic_driver as _drv
            mcp_sessions = getattr(_drv, "_active_mcp_sessions", None)
        except Exception:
            pass

    if not mcp_sessions:
        return user_text, 0, 0

    try:
        # Build a lightweight config for auto-inject
        mcp_config = {}
        for server_name in mcp_sessions:
            mcp_config[server_name] = {
                "auto_inject_resources": {
                    "enabled": True,
                    "triggers": [],  # Empty = always try
                    "uris": [],
                    "max_resources": 3,
                }
            }

        gateway = MCPResourceGateway(
            mcp_sessions=mcp_sessions,
            mcp_config=mcp_config,
        )
        plan = gateway.plan_for_query(user_text)
        if not plan:
            return user_text, 0, 0

        injection_timeout_sec = float(os.getenv("MICA_WS_RESOURCE_INJECTION_TIMEOUT_SEC", "5"))
        resources = await asyncio.wait_for(
            gateway.materialize(
                plan,
                max_chars_per_resource=4000,
                max_total_chars=12000,
            ),
            timeout=max(0.1, injection_timeout_sec),
        )
        successful = [r for r in resources if not r.error and r.text]
        if not successful:
            return user_text, 0, 0

        context_block = format_resource_context(successful)
        total_chars = sum(len(r.text) for r in successful)
        augmented = f"[AUTO-INJECTED MCP RESOURCES]\n{context_block}\n\n[USER QUERY]\n{user_text}"
        return augmented, len(successful), total_chars

    except asyncio.TimeoutError:
        logger.warning("Resource injection timed out (non-fatal)")
        return user_text, 0, 0
    except Exception as exc:
        logger.debug("Resource injection failed (non-fatal): %s", exc)
        return user_text, 0, 0


# ---------------------------------------------------------------------------
# Resource-first literature response builder
# ---------------------------------------------------------------------------

async def _build_resource_first_response(
    raw_json: str,
    query: str,
    user_id: str = "agent",
    base_url: str = "",
) -> str:
    """Transform a raw literature search result into a resource-first response.

    Instead of dumping full paper JSON (potentially 30K+ chars) into the
    model context, this builds a compact manifest, registers results in the
    DLM MCP cache for progressive disclosure, and returns a lightweight
    pointer with top-N titles as immediate bait.

    Returns
    -------
    str
        JSON string with ``manifest_uri``, ``paper_count``, ``top_titles``,
        and a progressive-disclosure instruction.
    """
    import json as _json

    try:
        raw = _json.loads(raw_json)
    except Exception:
        # If parsing fails, return raw as-is (non-breaking fallback)
        logger.warning("_build_resource_first_response: could not parse API JSON, passing through")
        return raw_json

    # Extract papers from common API response shapes
    papers: list = []
    if isinstance(raw, dict):
        papers = raw.get("papers") or raw.get("results") or raw.get("data") or []
    elif isinstance(raw, list):
        papers = raw

    if not papers:
        # No papers to manifest — return original with a note
        if isinstance(raw, dict):
            raw["_resource_first_note"] = "No papers found to manifest; progressive disclosure not applied."
        return _json.dumps(raw, ensure_ascii=False, default=str)

    # --- Build manifest ---
    try:
        from mica.memory.dlm.manifest_builder import build_manifest, _qhash
        from mica.mcp_servers.python_servers.dlm_resources_mcp import register_dlm_results

        # Register in DLM MCP cache FIRST to get the authoritative hash
        # (register_dlm_results uses 12-char sha256, which is the canonical
        # cache key that dlm_resources_mcp uses for lookup)
        cache_hash = register_dlm_results(query, papers)
        logger.info("DLM cache registered %d papers for query=%r hash=%s", len(papers), query, cache_hash)

        # Build manifest with the same hash for consistency
        manifest = build_manifest(query, papers, max_papers=30)
        # Override the manifest hash to match the DLM cache key
        manifest["query_hash"] = cache_hash
        qhash = cache_hash

        # Persist manifest to GCS (best-effort)
        gcs_path = None
        try:
            from mica.memory.dlm.manifest_builder import persist_manifest
            from mica.storage.gcs_user_storage import get_storage_manager
            storage = get_storage_manager()
            gcs_path = persist_manifest(storage, user_id, manifest)
        except Exception as exc:
            logger.debug("Manifest GCS persist skipped (non-fatal): %s", exc)

        # Build compact response
        top_titles = [
            {
                "rank": i + 1,
                "title": (p.get("title") or "")[:120],
                "year": p.get("year"),
                "paper_id": p.get("paperId") or p.get("doi") or p.get("id"),
            }
            for i, p in enumerate(papers[:5])
        ]

        response = {
            "manifest_uri": f"mica://dlm/manifest/{qhash}",
            "paper_count": len(papers),
            "included_in_manifest": manifest.get("included_count", min(len(papers), 30)),
            "top_titles": top_titles,
            "gcs_path": gcs_path,
            "instruction": (
                "Use MCP resource 'mica://dlm/manifest/{hash}' to browse all titles and scores. "
                "Use 'mica://dlm/doc/{paper_id}/abstract' for paper details (~500 chars). "
                "Use 'mica://dlm/doc/{paper_id}/full' only when deep analysis is required (~20K chars)."
            ).replace("{hash}", qhash),
        }
        return _json.dumps(response, ensure_ascii=False, default=str)

    except Exception as exc:
        logger.warning(
            "_build_resource_first_response: manifest build failed (%s), falling back to truncated raw",
            exc,
        )
        # Fallback: return truncated raw result
        if isinstance(raw, dict):
            if "papers" in raw:
                raw["_resource_first_error"] = str(exc)
                raw["_truncated"] = True
                raw["papers"] = [
                    {k: v for k, v in p.items() if k in ("title", "year", "doi", "paperId")}
                    for p in raw["papers"][:10]
                ]
        return _json.dumps(raw, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Module-level HTTP helper (used by executor closure below)
# ---------------------------------------------------------------------------

async def _call_api(base_url: str, method: str, path: str, **kwargs: Any) -> str:
    """Make an HTTP call to the backend and return the response as a string."""
    import aiohttp

    url = f"{base_url}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, **kwargs, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            text = await resp.text()
            if resp.status >= 400:
                return json.dumps({"error": f"HTTP {resp.status}", "body": text[:500]})
            return text


# ---------------------------------------------------------------------------
# Tool executor: dispatches to the MICA backend HTTP API
# ---------------------------------------------------------------------------

async def create_backend_executor(
    base_url: str = "http://localhost:8080",
    user_id: str = "agent",
    authorization: Optional[str] = None,
    session_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    study_id: Optional[str] = None,
    runtime_authority: Optional[Dict[str, Any]] = None,
) -> Callable[[str, str, Dict[str, Any]], Awaitable[str]]:
    """Create a tool executor that calls the MICA backend API."""
    import aiohttp

    _session_id = session_id or uuid.uuid4().hex[:12]
    _workspace_id = str(workspace_id or "").strip()
    _study_id = str(study_id or "").strip()
    _command_kernel = UnifiedAgentCommandKernel(user_id=user_id)

    async def _exec_api(base_url: str, method: str, path: str, **kwargs: Any) -> str:
        headers = dict(kwargs.pop("headers", {}) or {})
        has_authorization = any(str(key).lower() == "authorization" for key in headers)
        if authorization and not has_authorization:
            headers.setdefault("Authorization", authorization)
            has_authorization = True
        if user_id and not has_authorization:
            headers.setdefault("X-User-Id", user_id)
        return await _call_api(base_url, method, path, headers=headers, **kwargs)

    def _looks_executable_sandbox_code(text: str, language: str) -> bool:
        body = str(text or "").strip()
        if not body:
            return False

        if _FENCED_SANDBOX_CODE_RE.fullmatch(body):
            return True

        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            return False
        folded_lines = [line.casefold() for line in lines]

        folded = body.casefold()
        if language == "python":
            markers = (
                "import ", "from ", "print(", "def ", "for ", "while ", "if ", "with ",
                "class ", "lambda ", "try:", "except ", "raise ", "return ", "@", "[", "{", "(",
            )
            return any(line.startswith(markers) for line in folded_lines) or any(
                _PYTHON_ASSIGNMENT_RE.match(line) for line in lines
            )
        if language == "bash":
            markers = (
                "#!/bin/bash", "echo ", "python ", "pip ", "ls", "cd ", "mkdir ",
                "cat ", "grep ", "awk ", "sed ", "export ", "rm ", "cp ", "mv ", "find ",
            )
            return any(line.startswith(markers) for line in folded_lines) or any(
                op in body for op in ("&&", "|", ";", "$(")
            )
        if language == "r":
            markers = ("library(", "print(", "ggplot(", "plot(", "data.frame(", "read.csv(")
            return any(line.startswith(markers) for line in folded_lines) or "<-" in body
        return False

    def _extract_explicit_sandbox_code(text: str, language: str) -> tuple[Optional[str], Optional[str]]:
        body = str(text or "").strip()
        if not body:
            return None, None

        fence_match = _FENCED_SANDBOX_CODE_RE.fullmatch(body)
        if fence_match:
            code = str(fence_match.group("code") or "").strip()
            fence_language = _FENCED_SANDBOX_LANGUAGE_ALIASES.get(
                str(fence_match.group("language") or "").strip().casefold()
            )
            return (code or None), fence_language

        if _looks_executable_sandbox_code(body, language):
            return body, None
        return None, None

    def _decorate_json_result(payload_text: str, **extra: Any) -> str:
        try:
            payload = json.loads(payload_text)
        except Exception:
            return payload_text
        if isinstance(payload, dict):
            for key, value in extra.items():
                if value is not None and key not in payload:
                    payload[key] = value
            return json.dumps(payload, ensure_ascii=False, default=str)
        return payload_text

    async def executor(name: str, call_id: str, args: Dict[str, Any]) -> str:
        try:
            protocol_surface_aliases = {
                "protocol.validate": "mica.protocol.validate",
                "protocol.author": "mica.protocol.author",
                "protocol.submit": "mica.protocol.submit",
                "protocol.run.status": "mica.protocol.status",
                "protocol.node.receipts": "mica.protocol.receipts",
            }
            name = protocol_surface_aliases.get(name, name)
            authority_check = authorize_tool_invocation(runtime_authority, name) if runtime_authority else {"allowed": True}
            if not authority_check.get("allowed"):
                return json.dumps(
                    {
                        "status": "unavailable",
                        "failure_reason": str(authority_check.get("reason") or "TOOL_NOT_AUTHORIZED_FOR_ROUTE"),
                        "note": f"Tool '{name}' is outside the resolved authority bundle for this route.",
                        "route_card_id": authority_check.get("route_card_id"),
                        "authority": authority_check.get("authority"),
                        "required_capabilities": authority_check.get("required_capabilities") or [],
                        "required_closure_stages": authority_check.get("required_closure_stages") or [],
                        "hard_block_reasons": authority_check.get("hard_block_reasons") or [],
                        "invocable_tools": authority_check.get("invocable_tools") or [],
                    },
                    ensure_ascii=False,
                )

            if name.startswith("mica."):
                import json as _json_mod
                if name == "mica.capabilities.list":
                    res_text = await _exec_api(base_url, "GET", "/api/v1/kernel/capabilities")
                    return res_text
                
                elif name == "mica.protocol.validate":
                    payload = {
                        "protocol_jsonld": args.get("protocol_jsonld") or args.get("protocolJsonld") or args.get("protocol_json_ld"),
                        "protocol_json": args.get("protocol_json"),
                        "protocol_draft": args.get("protocol_draft"),
                        "tool_plan": args.get("tool_plan"),
                        "protocol_plan": args.get("protocol_plan") or args.get("protocolPlan"),
                        "steps": args.get("steps"),
                        "protocol_id": args.get("protocol_id"),
                        "protocol_name": args.get("protocol_name"),
                        "goal": args.get("goal"),
                        "protocol_path": args.get("protocol_path") or args.get("path"),
                        "workspace_id": args.get("workspace_id") or args.get("workspace") or _workspace_id,
                        "study_id": args.get("study_id") or args.get("study") or _study_id,
                        "prepare_executor_request": bool(args.get("prepare_executor_request", False)),
                    }
                    envelope = BackendCommandEnvelope(
                        command_name="protocol.validate",
                        session_id=_session_id,
                        study_id=payload.get("study_id") or None,
                        workspace_id=payload.get("workspace_id") or None,
                        request_identity={
                            "call_id": call_id,
                            "surface": "ws_bridge",
                            "user_id": user_id,
                        },
                        arguments=payload,
                        policy=BackendCommandPolicy(allow_side_effects=False),
                    )
                    result = await _command_kernel.execute(envelope)
                    return result.model_dump_json()
                
                elif name == "mica.protocol.author":
                    payload = {
                        "goal": args.get("goal"),
                        "workspace_id": args.get("workspace_id") or args.get("workspace"),
                        "study_id": args.get("study_id") or args.get("study"),
                        "allowed_capabilities": args.get("allowed_capabilities", []),
                        "available_artifacts": args.get("available_artifacts", []),
                        "constraints": args.get("constraints", []),
                        "max_nodes": args.get("max_nodes", 20),
                        "desired_outputs": args.get("desired_outputs", []),
                        "safety_mode": args.get("safety_mode", "sandbox")
                    }
                    res_text = await _exec_api(base_url, "POST", "/api/v1/kernel/protocols/author", json=payload)
                    return res_text
                
                elif name == "mica.protocol.submit":
                    payload = {
                        "protocolJsonld": args.get("protocol_jsonld") or args.get("protocolJsonld") or args.get("protocol_json_ld"),
                        "protocol_json": args.get("protocol_json"),
                        "protocol_draft": args.get("protocol_draft"),
                        "tool_plan": args.get("tool_plan"),
                        "protocolPlan": args.get("protocol_plan") or args.get("protocolPlan"),
                        "steps": args.get("steps"),
                        "protocol_id": args.get("protocol_id"),
                        "protocol_name": args.get("protocol_name"),
                        "goal": args.get("goal"),
                        "protocol_path": args.get("protocol_path") or args.get("path"),
                        "workspace_id": args.get("workspace_id") or args.get("workspace"),
                        "study_id": args.get("study_id") or args.get("study"),
                        "idempotency_key": args.get("idempotency_key")
                    }
                    res_text = await _exec_api(base_url, "POST", "/api/v1/kernel/protocols/submit", json=payload)
                    return res_text

                command_name = None
                if name == "mica.command.run":
                    command_name = args.get("command_name")
                    cmd_args = args.get("args") or args.get("arguments") or {}
                else:
                    mapping = {
                        "mica.kb.list": "kb.list",
                        "mica.kb.create": "kb.create",
                        "mica.kb.ingest": "kb.ingest",
                        "mica.kb.search": "kb.semantic_search",
                        "mica.graphrag.query": "graphrag.query",
                        "mica.artifact.attach_to_study": "artifact.attach_to_study",
                        "mica.protocol.status": "protocol.run.status",
                        "mica.protocol.receipts": "protocol.node.receipts",
                    }
                    command_name = mapping.get(name)
                    if not command_name:
                        stripped_name = str(name or "").removeprefix("mica.")
                        canonical_command = canonical_backend_command_name(stripped_name)
                        if is_backend_command_name(canonical_command):
                            command_name = canonical_command
                    cmd_args = dict(args or {})
                
                if not command_name:
                    return _json_mod.dumps({
                        "ok": False,
                        "success": False,
                        "status": "blocked",
                        "summary": f"Unknown command requested via {name}.",
                        "blocker_code": "unknown_command"
                    })

                if is_backend_command_name(command_name):
                    allow_side_effects = bool(
                        args.get("allow_side_effects", False)
                        or args.get("policy", {}).get("allow_side_effects", False)
                    )
                    try:
                        manifest_entry = get_backend_command_manifest_entry(command_name)
                    except KeyError:
                        manifest_entry = None
                    if manifest_entry and manifest_entry.side_effects:
                        allow_side_effects = allow_side_effects or command_name in {"kb.create", "kb.ingest"}
                    envelope = BackendCommandEnvelope(
                        command_name=command_name,
                        session_id=_session_id,
                        study_id=(
                            args.get("study_id")
                            or args.get("study")
                            or cmd_args.get("study_id")
                            or _study_id
                            or None
                        ),
                        workspace_id=(
                            args.get("workspace_id")
                            or args.get("workspace")
                            or cmd_args.get("workspace_id")
                            or _workspace_id
                            or None
                        ),
                        request_identity={
                            "call_id": call_id,
                            "surface": "ws_bridge",
                            "user_id": user_id,
                        },
                        arguments=cmd_args,
                        policy=BackendCommandPolicy(allow_side_effects=allow_side_effects),
                    )
                    result = await _command_kernel.execute(envelope)
                    return result.model_dump_json()

                payload = {
                    "command_name": command_name,
                    "workspace_id": (
                        args.get("workspace_id")
                        or args.get("workspace")
                        or cmd_args.get("workspace_id")
                        or _workspace_id
                    ),
                    "study_id": (
                        args.get("study_id")
                        or args.get("study")
                        or cmd_args.get("study_id")
                        or _study_id
                    ),
                    "session_id": _session_id,
                    "arguments": cmd_args,
                    "policy": {
                        "allow_side_effects": True
                    }
                }
                res_text = await _exec_api(base_url, "POST", "/api/v1/kernel/execute", json=payload)
                return res_text

            alias_args = dict(args or {})
            alias_name = str(name or "").strip()
            if alias_name == "graphrag_query":
                canonical = "graphrag.query"
                alias_args = {
                    **alias_args,
                    "query": alias_args.get("query")
                    or alias_args.get("query_text")
                    or alias_args.get("prompt")
                    or "",
                }
            elif alias_name == "graphrag_hop1":
                canonical = "graphrag.hop1"
            else:
                canonical = canonical_backend_command_name(name)
            _product_surface_rest_bypass_commands = {
                "lab.create",
                "lab.list",
                "lab.get",
                "knowledge_space.create",
                "knowledge_space.list",
                "knowledge_space.get",
                "knowledge_space.membership.create",
                "research_line.create",
                "research_line.list",
                "research_line.get",
                "research_line.link_study",
            }
            if is_backend_command_name(canonical) and canonical not in _product_surface_rest_bypass_commands:
                if canonical.startswith("protocol.reviews."):
                    assert not any(p in canonical.split(".") for p in ("p5", "p6", "p7", "post_p6")), f"Canonical reviews command '{canonical}' contains phase label"
                allow_side_effects = bool(alias_args.get("allow_side_effects", False) or alias_args.get("policy", {}).get("allow_side_effects", False))
                envelope = BackendCommandEnvelope(
                    command_name=canonical,
                    session_id=_session_id,
                    study_id=alias_args.get("study_id") or _study_id or None,
                    working_set_id=alias_args.get("working_set_id"),
                    workspace_id=alias_args.get("workspace_id") or _workspace_id or None,
                    request_identity={
                        "call_id": call_id,
                        "surface": "ws_bridge",
                        "user_id": user_id,
                    },
                    arguments=alias_args,
                    resource_refs=list(alias_args.get("resource_refs") or []),
                    policy=BackendCommandPolicy(allow_side_effects=allow_side_effects),
                )
                result = await _command_kernel.execute(envelope)
                return result.model_dump_json()

            if name == "search_protein":
                q = args.get("query", "")
                return await _exec_api(base_url, "GET", f"/api/v1/graph/lmp_v4/graph?q={q}")

            elif name == "resolve_pdb":
                payload = _build_generate_lmp_request_payload(
                    {
                        "pdb_id": args.get("pdb_id"),
                        "query": args.get("query", ""),
                        "preset": args.get("preset", "structural"),
                    },
                    default_preset="structural",
                )
                return await _exec_api(base_url, "POST", "/api/v1/lmp/generate", json=payload)

            elif name == "analyze_structure":
                return await _exec_api(
                    base_url, "GET",
                    f"/api/v1/graph/lmp_v4/domains?pdb_id={args['pdb_id']}",
                )

            elif name == "search_literature":
                raw = await _exec_api(base_url, "POST", "/api/v1/research/deep-scan/sync", json={
                    "query": args["query"],
                    "max_papers": args.get("limit", 10),
                    "sources": ["semantic_scholar", "pubmed", "openalex"],
                })
                from mica.resources.resource_first_response import build_resource_first_literature_response
                result = build_resource_first_literature_response(
                    raw, query=args["query"], source_tool="search_literature",
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            elif name == "add_to_workspace":
                return await _exec_api(base_url, "POST", f"/api/v1/workspace/sessions/{_session_id}/assets", json={
                    "user_id": user_id,
                    "asset_type": args["asset_type"],
                    "name": args["name"],
                    "content": args["content"],
                })

            elif name == "visualize_molecule":
                pdb_id = args.get("pdb_id", "")
                preset = args.get("preset", "default")
                return await _exec_api(
                    base_url, "GET",
                    f"/api/v1/graph/lmp_v4/viewer?pdb_id={pdb_id}&preset={preset}",
                )

            elif name == "run_deep_research":
                raw = await _exec_api(base_url, "POST", "/api/v1/research/pipeline", json={
                    "query": args["query"],
                    "preset": args.get("preset", "standard"),
                    "user_id": user_id,
                    "session_id": _session_id,
                })
                from mica.resources.resource_first_response import build_resource_first_literature_response
                result = build_resource_first_literature_response(
                    raw, query=args["query"], source_tool="run_deep_research",
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            elif name == "web_search":
                # Real web search via Firecrawl v2 — direct driver call, no API round-trip.
                # Anti-mock: if FIRECRAWL_API_KEY is absent, surface a structured error
                # payload rather than inventing results.
                try:
                    from mica.drivers.websearch.firecrawl_client import (
                        FirecrawlClientError,
                        FirecrawlNotConfigured,
                        FirecrawlSearchClient,
                    )
                except Exception as exc:  # pragma: no cover - import failure is fatal
                    return {"error": f"firecrawl client import failed: {exc!r}"}

                try:
                    client = FirecrawlSearchClient()
                except FirecrawlNotConfigured as exc:
                    return {
                        "error": "web_search_not_configured",
                        "reason": "not_configured",
                        "detail": str(exc),
                    }
                try:
                    payload_result = await client.search(
                        query=args["query"],
                        limit=int(args.get("limit", 10) or 10),
                        sources=list(args.get("sources") or ["web"]),
                        categories=list(args.get("categories") or []),
                    )
                except FirecrawlClientError as exc:
                    return {"error": "web_search_failed", "detail": str(exc)}
                # search() returns {query, limit, sources, categories, status, results[], raw_keys}
                # where results[] is a list of dicts already normalized via FirecrawlResult.to_dict().
                return payload_result

            # --- Extended tools ---
            elif name == "load_knowledge_graph":
                pdb_id = args.get("pdb_id", "")
                return await _exec_api(base_url, "GET", f"/api/v1/graph/lmp_v4/graph?q={pdb_id}")

            elif name == "get_domain_coloring":
                return await _exec_api(
                    base_url, "GET",
                    f"/api/v1/graph/lmp_v4/domains?pdb_id={args['pdb_id']}",
                )

            elif name == "list_lmp_presets":
                return await _exec_api(base_url, "GET", "/api/v1/lmp/presets")

            elif name == "generate_lmp":
                payload = _build_generate_lmp_request_payload(args, default_preset="llm-context")
                return await _exec_api(base_url, "POST", "/api/v1/lmp/generate", json=payload)

            elif name == "scan_imported_structure":
                path = "/api/v1/lmp/imported-structures/scan/async" if str(args.get("execution_mode") or "sync").strip().lower() == "async" else "/api/v1/lmp/imported-structures/scan"
                return await _exec_api(base_url, "POST", path, json={
                    "structure_uri": args["structure_uri"],
                    "asset_id": args.get("asset_id"),
                    "workspace_id": args.get("workspace_id"),
                    "identity_policy": args.get("identity_policy", "local_metadata"),
                    "remote_identity_timeout_seconds": args.get("remote_identity_timeout_seconds", 30),
                    "literature_policy": args.get("literature_policy", {}),
                    "dlm_policy": args.get("dlm_policy", {}),
                    "smic_policy": args.get("smic_policy", {}),
                    "serverless_policy": args.get("serverless_policy", {}),
                    "emit_lmp_xml": args.get("emit_lmp_xml", False),
                    "validate_xsd": args.get("validate_xsd", True),
                })

            elif name == "get_scan_imported_structure_status":
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/lmp/imported-structures/scan/{quote(str(args['job_id']), safe='')}",
                )

            elif name == "get_lmp_state_receipt":
                state_id = quote(str(args["state_id"]), safe="")
                allow_afdb_fallback = "true" if bool(args.get("allow_afdb_fallback", True)) else "false"
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/lmp/annotations/state/{state_id}/receipt?allow_afdb_fallback={allow_afdb_fallback}",
                )

            elif name == "get_lmp_dynamic_statistics":
                state_id = quote(str(args["state_id"]), safe="")
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/lmp/annotations/state/{state_id}/dynamic-statistics",
                )

            elif name == "get_lmp_residue_dynamic_statistics":
                state_id = quote(str(args["state_id"]), safe="")
                return await _exec_api(
                    base_url,
                    "POST",
                    f"/api/v1/lmp/annotations/state/{state_id}/dynamic-statistics/residue-query",
                    json={
                        "positions": args.get("positions", []),
                        "chain": args.get("chain"),
                        "max_results": args.get("max_results", 50),
                    },
                )

            elif name == "get_lmp_pair_dynamic_statistics":
                state_id = quote(str(args["state_id"]), safe="")
                return await _exec_api(
                    base_url,
                    "POST",
                    f"/api/v1/lmp/annotations/state/{state_id}/dynamic-statistics/pair-query",
                    json={
                        "pairs": args.get("pairs", []),
                        "chain_i": args.get("chain_i"),
                        "chain_j": args.get("chain_j"),
                        "max_results": args.get("max_results", 50),
                    },
                )

            elif name == "get_lmp_structure_comparison_ledger":
                state_id = quote(str(args["state_id"]), safe="")
                allow_afdb_fallback = "true" if bool(args.get("allow_afdb_fallback", True)) else "false"
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/lmp/annotations/state/{state_id}/structure-comparison-ledger?allow_afdb_fallback={allow_afdb_fallback}",
                )

            elif name == "list_dlm_presets":
                return await _exec_api(base_url, "GET", "/api/v1/dlm/presets")

            elif name == "run_dlm_graph_repair_export":
                return await _exec_api(base_url, "POST", "/api/v1/dlm/graph-repair/export", json={
                    "pdf_path": args["pdf_path"],
                    "output_dir": args.get("output_dir"),
                    "provider_id": args.get("provider_id", "deepinfra"),
                    "model_id": args.get("model_id"),
                    "max_pages": args.get("max_pages", 40),
                    "max_candidates": args.get("max_candidates", 0),
                    "tool_budget": args.get("tool_budget", 24),
                    "include_cooccurs": bool(args.get("include_cooccurs", False)),
                    "clear_dlm_cache": bool(args.get("clear_dlm_cache", False)),
                })

            elif name == "run_dlm_scan":
                raw = await _exec_api(base_url, "POST", "/api/v1/research/deep-scan/sync", json={
                    "query": args["query"],
                    "max_papers": 50,
                    "sources": ["semantic_scholar", "pubmed", "openalex"],
                })
                from mica.resources.resource_first_response import build_resource_first_literature_response
                result = build_resource_first_literature_response(
                    raw, query=args["query"], source_tool="run_dlm_scan",
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            elif name == "generate_report":
                # /research/report removed — fall back to pipeline/sync which returns structured output
                return await _exec_api(base_url, "POST", "/api/v1/research/pipeline/sync", json={
                    "query": args["query"],
                    "preset": args.get("preset", "quick-scan"),
                    "session_id": args.get("session_id", _session_id),
                    "user_id": user_id,
                })

            elif name == "list_workspace_sessions":
                return await _exec_api(base_url, "GET", f"/api/v1/workspace/sessions?user_id={user_id}")

            elif name == "list_workspace_assets":
                sid = args["session_id"]
                return await _exec_api(base_url, "GET", f"/api/v1/workspace/sessions/{sid}/assets")

            elif name == "read_workspace_document":
                sid = args["session_id"]
                aid = args["asset_id"]
                return await _exec_api(base_url, "GET", f"/api/v1/workspace/sessions/{sid}/assets/{aid}/read")

            elif name == "scan_workspace_document":
                sid = args["session_id"]
                aid = args["asset_id"]
                return await _exec_api(base_url, "POST", f"/api/v1/workspace/sessions/{sid}/assets/{aid}/scan", json={
                    "mode": args.get("mode", "dlm_sections"),
                })

            elif name == "get_workspace_scan_status":
                return await _exec_api(base_url, "GET", f"/api/v1/workspace/scans/{args['scan_id']}")

            elif name == "get_citations_and_references":
                # No dedicated /citations endpoint — use metadata search with the paper_id as query.
                # NOTE: /metadata/search returns protein metadata, not citation graph nodes.
                # The adapter will extract whatever results are available and normalize them.
                paper_id = args["paper_id"]
                limit = args.get("limit", 20)
                raw = await _exec_api(base_url, "POST", "/api/v1/research/metadata/search", json={
                    "query": paper_id,
                    "limit": limit,
                })
                from mica.resources.resource_first_response import build_resource_first_literature_response
                result = build_resource_first_literature_response(
                    raw, query=f"citations:{paper_id}", source_tool="get_citations_and_references",
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            # --- Bibliotecario tools ---
            elif name == "run_bibliotecario_scan":
                return await _exec_api(base_url, "POST", "/api/v1/research/bibliotecario/scan", json={
                    "query": args["query"],
                    "preset": args.get("preset", "entity-scan"),
                    "entities": args.get("entities", []),
                    "extra_queries": args.get("extra_queries", []),
                    "pdb_ids": args.get("pdb_ids", []),
                    "lmp_handoff": args.get("lmp_handoff", {}),
                    "max_papers": args.get("max_papers", 200),
                    "session_id": args.get("session_id", _session_id),
                    "require_full_text": args.get("require_full_text", True),
                    "user_id": user_id,
                })

            elif name == "query_mica_q":
                return await _exec_api(base_url, "POST", "/api/v1/research/mica-q/query", json={
                    "query": args["query"],
                    "workspace_id": args.get("workspace_id"),
                    "session_id": args.get("session_id", _session_id),
                    "limit": args.get("limit", 10),
                })

            elif name == "resolve_entity":
                return await _exec_api(base_url, "POST", "/api/v1/research/entity/resolve", json={
                    "name": args["name"],
                    "entity_type": args.get("entity_type"),
                })

            elif name == "scan_knowledge_base":
                return await _exec_api(base_url, "POST", f"/api/v1/kbs/{args['kb_id']}/scan", json={
                    "mode": args.get("mode", "dlm_sections"),
                    "session_id": args.get("session_id", ""),
                    "asset_ids": args.get("asset_ids", []),
                })

            elif name == "get_knowledge_base_scan_status":
                return await _exec_api(base_url, "GET", f"/api/v1/kbs/{args['kb_id']}/scan-status")

            elif name == "promote_knowledge_base_scan":
                return await _exec_api(base_url, "POST", f"/api/v1/kbs/{args['kb_id']}/promote", json={
                    "scan_id": args["scan_id"],
                    "minimum_evidentiality_score": args.get("minimum_evidentiality_score", 0.5),
                })

            elif name == "list_knowledge_base_atoms":
                return await _exec_api(base_url, "GET", f"/api/v1/kbs/{args['kb_id']}/atoms")

            elif name == "query_atom_facts":
                return await _exec_api(base_url, "POST", "/api/v1/research/atom/query", json={
                    "entity": args.get("entity"),
                    "predicate": args.get("predicate"),
                    "temperature_mode": args.get("temperature_mode", "focused"),
                    "limit": args.get("limit", 30),
                })

            elif name == "download_pdf_to_workspace":
                return await _exec_api(base_url, "POST", "/api/v1/research/pdf/download-to-workspace", json={
                    "url": args.get("url"),
                    "arxiv_id": args.get("arxiv_id"),
                    "paper_id": args.get("paper_id"),
                    "session_id": args.get("session_id", _session_id),
                    "user_id": user_id,
                })

            elif name == "search_protein_metadata":
                return await _exec_api(base_url, "POST", "/api/v1/research/metadata/search", json={
                    "query": args.get("query"),
                    "is_kinase": args.get("is_kinase"),
                    "has_ptms": args.get("has_ptms"),
                    "limit": args.get("limit", 10),
                })

            elif name == "advanced_protein_search":
                return await _exec_api(base_url, "POST", "/api/v1/research/metadata/search", json={
                    "query": args.get("query"),
                    "has_approved_drugs": args.get("has_approved_drugs"),
                    "protein_family": args.get("protein_family"),
                    "min_approved_drugs": args.get("min_approved_drugs"),
                    "has_disease": args.get("has_disease"),
                    "has_pathway": args.get("has_pathway"),
                    "organism": args.get("organism"),
                    "limit": args.get("limit", 20),
                })

            elif name == "list_serverless_models":
                return await _exec_api(base_url, "GET", "/api/v1/serverless-models")

            elif name == "run_serverless_model":
                return await _exec_api(base_url, "POST", "/api/v1/serverless-models/invoke", json={
                    "model_id": args["model_id"],
                    "inputs": dict(args.get("inputs") or {}),
                    "session_id": args.get("session_id") or _session_id,
                    "run_id": args.get("run_id"),
                    "requested_by": "ws_bridge_tool",
                    "provider_override": args.get("provider_override"),
                })

            elif name == "milvus_hybrid_search":
                return await _exec_api(base_url, "POST", "/api/v1/research/milvus/search", json={
                    "query": args["query"],
                    "min_year": args.get("min_year"),
                    "max_year": args.get("max_year"),
                    "min_citations": args.get("min_citations"),
                    "limit": args.get("limit", 10),
                })

            elif name == "milvus_sequence_search":
                return await _exec_api(base_url, "POST", "/api/v1/research/milvus/sequence-search", json={
                    "sequence": args["sequence"],
                    "model_id": args.get("model_id", "esm2.embed.sequence.t30"),
                    "requested_collection_name": args.get("requested_collection_name", "dctdomain_embeddings"),
                    "fallback_collection_name": args.get("fallback_collection_name"),
                    "strict_requested_collection": args.get("strict_requested_collection", False),
                    "pooling": args.get("pooling", "mean"),
                    "normalize_embedding": args.get("normalize_embedding", False),
                    "limit": args.get("limit", 10),
                })

            elif name == "milvus_dct_search":
                return await _exec_api(base_url, "POST", "/api/v1/research/milvus/dct-search", json={
                    "sequence": args["sequence"],
                    "pid": args.get("pid"),
                    "collection_name": args.get("collection_name", "dctdomain_embeddings"),
                    "runpod_endpoint_id": args.get("runpod_endpoint_id"),
                    "maxlen": args.get("maxlen", 500),
                    "threshold": args.get("threshold", 2.6),
                    "candidate_limit": args.get("candidate_limit", 50),
                    "wait_ms": args.get("wait_ms", 300000),
                    "limit": args.get("limit", 10),
                })

            elif name == "milvus_stored_embedding_search":
                return await _exec_api(base_url, "POST", "/api/v1/research/milvus/stored-embedding-search", json={
                    "protein_id": args["protein_id"],
                    "source_collection_name": args.get("source_collection_name", "protein_sequences_embeddings"),
                    "target_collection_name": args.get("target_collection_name"),
                    "exclude_source": args.get("exclude_source", True),
                    "normalize_query": args.get("normalize_query", False),
                    "limit": args.get("limit", 10),
                })

            elif name == "run_cascade_pipeline":
                raw = await _exec_api(base_url, "POST", "/api/v1/research/cascade/run", json={
                    "query": args["query"],
                    "uniprot_id": args.get("uniprot_id"),
                    "preset": args.get("preset", "standard"),
                    "max_papers": args.get("max_papers", 200),
                    "enable_milvus": args.get("enable_milvus", True),
                    "enable_pharma": args.get("enable_pharma", True),
                    "user_id": user_id,
                    "session_id": _session_id,
                })
                from mica.resources.resource_first_response import build_resource_first_literature_response
                result = build_resource_first_literature_response(
                    raw, query=args["query"], source_tool="run_cascade_pipeline",
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            elif name == "enrich_protein_pharma":
                # Direct call — no API endpoint yet, call module directly
                try:
                    from mica.memory.dlm.pharma_enrichment import enrich_single_protein
                    result = enrich_single_protein(args["uniprot_id"])
                    return json.dumps(result)
                except Exception as exc:
                    return json.dumps({"error": f"Pharma enrichment failed: {exc}"})

            elif name == "query_co_occurrence":
                return await _exec_api(base_url, "GET", "/api/v1/research/entity/co-occurrence", params={
                    "entities": ",".join(args["entities"]),
                    "min_papers": args.get("min_papers", 1),
                })

            elif name == "track_entity_evolution":
                return await _exec_api(base_url, "GET", "/api/v1/research/entity/evolution", params={
                    "entity": args["entity"],
                    "entity_type": args.get("entity_type", "protein"),
                    "start_year": args.get("start_year", 2015),
                    "end_year": args.get("end_year", 2025),
                })

            # --- P0–P3 novel feature tools ---
            elif name == "generate_hypotheses":
                return await _exec_api(base_url, "POST", "/api/v1/research/hypothesis/generate", json={
                    "entities": args["entities"],
                    "max_hypotheses": args.get("max_hypotheses", 10),
                    "preset": args.get("preset"),
                })

            elif name == "compile_research_briefing":
                return await _exec_api(base_url, "POST", "/api/v1/research/briefing/compile", json={
                    "query": args["query"],
                    "preset": args.get("preset"),
                })

            elif name == "scan_drug_repurposing":
                return await _exec_api(base_url, "POST", "/api/v1/research/repurposing/scan", json={
                    "protein": args["protein"],
                    "max_alerts": args.get("max_alerts", 20),
                    "preset": args.get("preset"),
                })

            elif name == "analyse_citation_impact":
                return await _exec_api(base_url, "POST", "/api/v1/research/citation-impact/analyse", json={
                    "entity": args["entity"],
                    "entity_type": args.get("entity_type", "protein"),
                    "preset": args.get("preset"),
                })

            elif name == "analyse_knowledge_decay":
                return await _exec_api(base_url, "POST", "/api/v1/research/knowledge-decay/analyse", json={
                    "entity": args["entity"],
                    "preset": args.get("preset"),
                })

            elif name == "map_conformational_landscape":
                return await _exec_api(base_url, "POST", "/api/v1/research/conformational/map", json={
                    "uniprot_id": args.get("uniprot_id"),
                    "gene_name": args.get("gene_name"),
                    "max_structures": args.get("max_structures", 50),
                    "preset": args.get("preset"),
                })

            elif name == "scan_pharmacovigilance":
                return await _exec_api(base_url, "POST", "/api/v1/research/pharmacovigilance/scan", json={
                    "entity": args["entity"],
                    "entity_type": args.get("entity_type", "protein"),
                    "max_drugs": args.get("max_drugs", 10),
                    "preset": args.get("preset"),
                })

            elif name == "build_ortholog_dashboard":
                return await _exec_api(base_url, "POST", "/api/v1/research/ortholog/dashboard", json={
                    "gene_name": args["gene_name"],
                    "species": args.get("species", "homo_sapiens"),
                    "preset": args.get("preset"),
                })

            # --- Bucket-native tools (Phase 1) ---
            elif name == "list_user_bucket_objects":
                params: Dict[str, Any] = {}
                if args.get("prefix"):
                    params["prefix"] = args["prefix"]
                if args.get("max_results"):
                    params["max_results"] = args["max_results"]
                if args.get("include_metadata"):
                    params["include_metadata"] = "true"
                return await _exec_api(base_url, "GET", "/api/v1/user-bucket/objects", params=params,
                                       headers={"X-User-Id": user_id})

            elif name == "get_user_bucket_object_info":
                return await _exec_api(base_url, "GET", "/api/v1/user-bucket/objects/info",
                                       params={"path": args["path"]},
                                       headers={"X-User-Id": user_id})

            elif name == "read_user_bucket_object_text":
                params_r: Dict[str, Any] = {"path": args["path"]}
                if args.get("max_chars"):
                    params_r["max_chars"] = args["max_chars"]
                return await _exec_api(base_url, "GET", "/api/v1/user-bucket/objects/read",
                                       params=params_r,
                                       headers={"X-User-Id": user_id})

            elif name == "copy_user_bucket_object_to_workspace":
                _ctw_body: Dict[str, Any] = {
                    "object_path": args["path"],
                    "workspace_session_id": args.get("workspace_session_id", _session_id),
                }
                if args.get("asset_type"):
                    _ctw_body["asset_type"] = args["asset_type"]
                if args.get("name"):
                    _ctw_body["name"] = args["name"]
                return await _exec_api(base_url, "POST", "/api/v1/user-bucket/copy-to-workspace",
                                       json=_ctw_body,
                                       headers={"X-User-Id": user_id})

            elif name == "copy_user_bucket_object":
                return await _exec_api(base_url, "POST", "/api/v1/user-bucket/copy",
                                       json={
                                           "source_path": args["source_path"],
                                           "dest_path": args["dest_path"],
                                       },
                                       headers={"X-User-Id": user_id})

            elif name == "search_user_bucket_content":
                return await _exec_api(base_url, "POST", "/api/v1/user-bucket/search-content",
                                       json={
                                           "terms": args["terms"],
                                           "prefix": args.get("prefix", ""),
                                           "max_results": args.get("max_results", 50),
                                       },
                                       headers={"X-User-Id": user_id})

            # --- Citation verification tools ---
            elif name == "verify_citations":
                # Forward to milvus search using extracted DOIs/text as query
                text = args.get("text") or " ".join(args.get("dois", []))
                return await _exec_api(base_url, "POST", "/api/v1/research/milvus/search", json={
                    "query": text or "citation verification",
                    "limit": 10,
                })

            # --- Sandbox execution tools ---
            elif name == "run_mica_q_sandbox":
                from mica.drivers.execution.sandbox_session_service import run_execute_in_sandbox_branch
                from mica.drivers.execution.sandbox_code_synthesis_service import synthesize_sandbox_code
                from mica_q.adapters.sandbox_adapter import build_sandbox_request_from_tool_args

                normalized_request = build_sandbox_request_from_tool_args(args)
                request_text = str(args.get("request") or args.get("objective") or args.get("query") or "").strip()
                code = str(args.get("code") or "").strip()
                code_ref = str(args.get("code_ref") or "").strip()
                detected_language: Optional[str] = None
                synthesis_result: Optional[dict[str, Any]] = None
                if not code:
                    code, detected_language = _extract_explicit_sandbox_code(
                        request_text,
                        normalized_request.language,
                    )
                if not code and not code_ref and bool(args.get("allow_synthesis", True)):
                    synthesis = await synthesize_sandbox_code(
                        request_text=request_text or normalized_request.objective,
                        workload_kind=normalized_request.workload_kind,
                        language_hint=str(args.get("language") or normalized_request.language),
                        preferred_provider=str(args.get("synthesis_provider") or "deepinfra"),
                        registry=await get_registry(),
                    )
                    synthesis_result = synthesis.to_dict()
                    if synthesis.ok:
                        code = synthesis.code
                        detected_language = synthesis.language
                if not code and not code_ref:
                    return json.dumps(
                        {
                            "status": "requires_explicit_code",
                            "tool_surface": "mica_q_sandbox",
                            "message": (
                                "MICA-Q sandbox routing is available, but this request does not include runnable code. "
                                "Provide `code` or `code_ref`, supply a language-specific python/bash/R snippet or fenced code block, "
                                "or allow governed synthesis for bounded dataset/code requests."
                            ),
                            "execution_request": normalized_request.to_tool_args(),
                            "governed_synthesis": synthesis_result,
                        },
                        ensure_ascii=False,
                    )

                effective_language = str(
                    args.get("language") or detected_language or normalized_request.language
                ).strip() or normalized_request.language
                execution_request = normalized_request.to_tool_args()
                execution_request["language"] = effective_language
                effective_packages = args.get("packages")
                if effective_packages in (None, []) and synthesis_result:
                    effective_packages = synthesis_result.get("packages")

                sandbox_args = {
                    "code": code,
                    "language": effective_language,
                    "gpu": args.get("gpu"),
                    "preset": args.get("preset"),
                    "packages": effective_packages,
                    "timeout": args.get("timeout", 300),
                    "cpu": args.get("cpu", 2.0),
                    "memory_mb": args.get("memory_mb", args.get("memory", 2048)),
                    "memory_limit_mb": args.get("memory_limit_mb"),
                    "storage_mb": args.get("storage_mb"),
                    "session_id": args.get("session_id"),
                    "upload_files": args.get("upload_files"),
                    "download_files": args.get("download_files"),
                    "workdir": args.get("workdir", "/sandbox"),
                }
                if code_ref:
                    sandbox_args["code_ref"] = code_ref
                result = await run_execute_in_sandbox_branch(
                    name=name,
                    args=sandbox_args,
                    executor_obj=executor,
                    degraded_tool_response_fn=_bridge_degraded_tool_response,
                )
                return _decorate_json_result(
                    result,
                    tool_surface="mica_q_sandbox",
                    execution_request=execution_request,
                    request=request_text or normalized_request.objective,
                    workload_kind=normalized_request.workload_kind,
                    language=effective_language,
                    governed_synthesis=synthesis_result,
                )

            elif name == "execute_in_sandbox":
                # R28 W5 closure: route ``driver_self_test`` operations through
                # the LocalBackend runner so the agentic loop can prove the
                # sandbox path without requiring Modal credentials. Heavy
                # specialists still require MODAL_TOKEN_*; we only short-circuit
                # the driver-lab smoke ops.
                operation = (args.get("operation") or "").strip()
                specialist = (args.get("specialist") or "").strip() or "driver_self_test"
                if specialist == "driver_self_test" and operation.startswith("repo_sandbox_"):
                    try:
                        from mica.sandbox.specialist_task import ModalSpecialistTask
                        from mica.sandbox.runners import get_runner
                        from mica.sandbox.specialist_pool import ModalSpecialistPool
                        from mica.sandbox.redactor import scrub as _scrub_legacy
                        task_parameters = dict(args.get("parameters") or {})
                        requested_backend = str(args.get("backend") or task_parameters.get("backend") or "").strip().lower()
                        if requested_backend:
                            task_parameters.setdefault("backend", requested_backend)
                        task = ModalSpecialistTask(
                            task_id=args.get("task_id") or f"ws-{int(time.time())}",
                            specialist=specialist,
                            operation=operation,
                            parameters=task_parameters,
                            timeout=int(args.get("timeout") or 60),
                        )
                        task.validate()
                        if requested_backend == "modal":
                            pool = ModalSpecialistPool()
                            result = await pool.spawn(task)
                            selected_backend = "modal"
                        else:
                            runner = get_runner(specialist)
                            result = runner(task)
                            selected_backend = requested_backend or "local"
                        # Slice-4 §2/§6: scrub legacy sandbox outputs + audit.
                        scrubbed_answer = (
                            _scrub_legacy(result.answer) if isinstance(result.answer, str)
                            else result.answer
                        )
                        try:
                            from mica.agentic.tools import agent_feed as _af_legacy
                            await _af_legacy.publish_cue(
                                agent_id=getattr(executor, "user_id", None) or "driver",
                                post_type="artifact",
                                topic="legacy_sandbox_audit",
                                title=f"legacy_sandbox_call: {operation}",
                                body=json.dumps({
                                    "audit_tag": "legacy_sandbox_call",
                                    "specialist": specialist,
                                    "operation": operation,
                                    "task_id": result.task_id,
                                    "status": result.status,
                                    "duration_s": result.duration_s,
                                }, default=str),
                            )
                        except Exception as _af_exc:  # noqa: BLE001
                            logger.warning(
                                "legacy_sandbox audit post failed: %s", _af_exc,
                            )
                        return json.dumps({
                            "status": result.status,
                            "task_id": result.task_id,
                            "backend": selected_backend,
                            "answer": scrubbed_answer,
                            "structured_data": result.structured_data,
                            "duration_s": result.duration_s,
                            "specialist": specialist,
                            "operation": operation,
                        })
                    except Exception as exc:  # noqa: BLE001
                        return json.dumps({
                            "status": "failed",
                            "error": f"driver_self_test_dispatch_failed: {exc}",
                            "specialist": specialist,
                            "operation": operation,
                        })

                from mica.drivers.execution.sandbox_session_service import run_execute_in_sandbox_branch

                return await run_execute_in_sandbox_branch(
                    name=name,
                    args=args,
                    executor_obj=executor,
                    degraded_tool_response_fn=_bridge_degraded_tool_response,
                )

            elif name == "sandbox_session_status":
                from mica.drivers.execution.sandbox_session_service import run_sandbox_session_status_branch

                return await run_sandbox_session_status_branch(
                    executor_obj=executor,
                    specialist_pool=None,
                )

            elif name == "terminate_sandbox_session":
                from mica.drivers.execution.sandbox_session_service import run_terminate_sandbox_session_branch

                return await run_terminate_sandbox_session_branch(
                    executor_obj=executor,
                    args=args,
                )

            elif name == "run_driver_delegated_checkpoint":
                from mica.drivers.agentic_driver import _run_driver_owned_delegated_checkpoint
                from mica.drivers.execution.driver_checkpoint_service import (
                    run_driver_delegated_checkpoint_branch,
                )

                return await run_driver_delegated_checkpoint_branch(
                    name=name,
                    args=args,
                    run_driver_owned_delegated_checkpoint_fn=_run_driver_owned_delegated_checkpoint,
                    degraded_tool_response_fn=_bridge_degraded_tool_response,
                )

            elif name == "run_driver_staging_deploy_checkpoint":
                from mica.drivers.agentic_driver import _run_driver_owned_staging_deploy_checkpoint
                from mica.drivers.execution.driver_checkpoint_service import (
                    run_driver_staging_deploy_checkpoint_branch,
                )

                return await run_driver_staging_deploy_checkpoint_branch(
                    name=name,
                    args=args,
                    run_driver_owned_staging_deploy_checkpoint_fn=_run_driver_owned_staging_deploy_checkpoint,
                    degraded_tool_response_fn=_bridge_degraded_tool_response,
                )

            elif name == "run_driver_experiment":
                # Tier-3 driver self-experimentation (Slice-3).
                try:
                    from mica.sandbox.driver_experiment import DriverExperimentRunner
                    runner = DriverExperimentRunner.get(executor)
                    res = await runner.run(
                        hypothesis=str(args.get("hypothesis", "")),
                        command_argv=list(args.get("command_argv") or []),
                        git_sha=args.get("git_sha"),
                        secret_names=list(args.get("secret_names") or []),
                        timeout_s=int(args.get("timeout_s") or 300),
                        readback_paths=list(args.get("readback_paths") or []),
                        session_id=args.get("session_id"),
                        snapshot_on_pass=bool(args.get("snapshot_on_pass") or False),
                        install_mica_deps=bool(args.get("install_mica_deps") or False),
                    )
                    return json.dumps(res.to_dict())
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({
                        "verdict": "ambiguous",
                        "error": f"run_driver_experiment_dispatch_failed: {type(exc).__name__}: {exc}",
                    })

            elif name == "replay_experiment":
                try:
                    from mica.sandbox.driver_experiment import DriverExperimentRunner
                    runner = DriverExperimentRunner.get(executor)
                    out = await runner.replay(str(args.get("experiment_id") or ""))
                    return json.dumps(out)
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({
                        "verdict": "error",
                        "error": f"replay_experiment_dispatch_failed: {type(exc).__name__}: {exc}",
                    })

            elif name == "get_experiment_quota_status":
                try:
                    from mica.sandbox.driver_experiment import DriverExperimentRunner
                    runner = DriverExperimentRunner.get(executor)
                    return json.dumps(runner.quota_status(args.get("session_id")))
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({
                        "error": f"quota_status_failed: {type(exc).__name__}: {exc}",
                    })

            # --- Public backend-native product/runtime parity tools (2026-07-03) ---
            elif name == "protocol_run":
                protocol_jsonld = (
                    args.get("protocol_jsonld")
                    or args.get("protocolJsonld")
                    or args.get("protocol_json")
                )
                if not isinstance(protocol_jsonld, dict) or not protocol_jsonld:
                    return json.dumps(
                        {
                            "error": "protocol_run_requires_protocol_jsonld",
                            "tool": name,
                            "capability_mode": "backend-native",
                        }
                    )
                payload = {
                    "protocolJsonld": protocol_jsonld,
                    "nodeReceipts": list(args.get("nodeReceipts") or args.get("node_receipts") or []),
                    "id": str(args.get("id") or ""),
                    "name": str(args.get("name") or ""),
                    "description": str(args.get("description") or ""),
                    "goal": str(args.get("goal") or ""),
                    "source": str(args.get("source") or "ws_bridge_tool"),
                    "metadata": dict(args.get("metadata") or {}),
                    "mode": str(args.get("mode") or "production"),
                    "session_id": args.get("session_id") or _session_id,
                    "mcp_enabled": bool(args.get("mcp_enabled", True)),
                    "resource_fabric_enabled": bool(args.get("resource_fabric_enabled", False)),
                    "study_id": args.get("study_id"),
                    "protocol_run_id": args.get("protocol_run_id"),
                }
                return await _exec_api(base_url, "POST", "/api/v1/protocol-drafts/execute", json=payload)

            elif name == "compute_jobs_submit":
                payload = dict(args.get("parameters") or {})
                payload.setdefault("job_type", args.get("job_type", "generic"))
                payload.setdefault("provider", args.get("provider", "vast"))
                if args.get("name") is not None:
                    payload.setdefault("name", args.get("name"))
                if args.get("execution_class") is not None:
                    payload.setdefault("execution_class", args.get("execution_class"))
                return await _exec_api(base_url, "POST", "/api/v1/compute/jobs", json=payload)

            elif name == "compute_jobs_status":
                job_id = str(args.get("job_id") or "").strip()
                if not job_id or job_id.lower() in {"all", "*", "list"}:
                    return await _exec_api(base_url, "GET", "/api/v1/compute/jobs")
                return await _exec_api(base_url, "GET", f"/api/v1/compute/jobs/{quote(job_id, safe='')}")

            elif name == "study_list":
                query_parts: list[str] = []
                if args.get("limit") is not None:
                    query_parts.append(f"limit={int(args['limit'])}")
                if args.get("offset") is not None:
                    query_parts.append(f"offset={int(args['offset'])}")
                if args.get("archived") is not None:
                    query_parts.append(f"archived={'true' if bool(args['archived']) else 'false'}")
                path = "/api/v1/studies"
                if query_parts:
                    path = f"{path}?{'&'.join(query_parts)}"
                return await _exec_api(base_url, "GET", path)

            elif name == "study_get":
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/studies/{quote(str(args['study_id']), safe='')}",
                )

            elif name == "working_set_list":
                query_parts: list[str] = []
                if args.get("study_id"):
                    query_parts.append(f"study_id={quote(str(args['study_id']), safe='')}")
                if args.get("limit") is not None:
                    query_parts.append(f"limit={int(args['limit'])}")
                if args.get("offset") is not None:
                    query_parts.append(f"offset={int(args['offset'])}")
                path = "/api/v1/working-sets"
                if query_parts:
                    path = f"{path}?{'&'.join(query_parts)}"
                return await _exec_api(base_url, "GET", path)

            elif name == "working_set_create":
                payload = {"name": args["name"]}
                if args.get("study_id"):
                    payload["study_id"] = args["study_id"]
                if args.get("description"):
                    payload["description"] = args["description"]
                if args.get("layout_data"):
                    payload["layout_data"] = args["layout_data"]
                return await _exec_api(base_url, "POST", "/api/v1/working-sets", json=payload)

            elif name == "working_set_attach_resource":
                payload = {
                    "artifact_ref_type": args.get("artifact_ref_type") or "artifact",
                    "artifact_ref_id": args["artifact_id"],
                    "position": int(args.get("position", 0)),
                    "config": dict(args.get("config") or {}),
                }
                return await _exec_api(
                    base_url,
                    "POST",
                    f"/api/v1/working-sets/{quote(str(args['working_set_id']), safe='')}/items",
                    json=payload,
                )

            elif name == "lab_create":
                payload = {"display_name": args["display_name"]}
                if args.get("description"):
                    payload["description"] = args["description"]
                if args.get("org_ref"):
                    payload["org_ref"] = args["org_ref"]
                if args.get("metadata"):
                    payload["metadata"] = dict(args["metadata"])
                return await _exec_api(base_url, "POST", "/api/v1/labs", json=payload)

            elif name == "lab_list":
                return await _exec_api(base_url, "GET", "/api/v1/labs")

            elif name == "lab_get":
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/labs/{quote(str(args['lab_id']), safe='')}",
                )

            elif name == "knowledge_space_create":
                payload = {
                    "lab_id": args["lab_id"],
                    "display_name": args["display_name"],
                }
                for optional_key in (
                    "slug",
                    "description",
                    "primary_parent_space_id",
                    "review_cadence",
                    "health_status",
                ):
                    if args.get(optional_key):
                        payload[optional_key] = args[optional_key]
                if args.get("metadata"):
                    payload["metadata"] = dict(args["metadata"])
                return await _exec_api(base_url, "POST", "/api/v1/knowledge-spaces", json=payload)

            elif name == "knowledge_space_list":
                query_parts: list[str] = []
                if args.get("lab_id"):
                    query_parts.append(f"lab_id={quote(str(args['lab_id']), safe='')}")
                if args.get("archived") is not None:
                    query_parts.append(f"archived={'true' if bool(args['archived']) else 'false'}")
                path = "/api/v1/knowledge-spaces"
                if query_parts:
                    path = f"{path}?{'&'.join(query_parts)}"
                return await _exec_api(base_url, "GET", path)

            elif name == "knowledge_space_get":
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/knowledge-spaces/{quote(str(args['space_id']), safe='')}",
                )

            elif name == "knowledge_space_create_membership":
                payload = {}
                for optional_key in (
                    "child_space_id",
                    "member_kb_ref",
                    "relation_type",
                    "expansion_policy",
                    "primary_parent",
                ):
                    if optional_key in args and args.get(optional_key) is not None:
                        payload[optional_key] = args[optional_key]
                payload["metadata"] = dict(args.get("metadata") or {})
                return await _exec_api(
                    base_url,
                    "POST",
                    f"/api/v1/knowledge-spaces/{quote(str(args['space_id']), safe='')}/memberships",
                    json=payload,
                )

            elif name == "research_line_create":
                payload = {
                    "lab_id": args["lab_id"],
                    "display_name": args["display_name"],
                }
                for optional_key in ("slug", "description", "primary_question", "status"):
                    if args.get(optional_key):
                        payload[optional_key] = args[optional_key]
                if args.get("metadata"):
                    payload["metadata"] = dict(args["metadata"])
                return await _exec_api(base_url, "POST", "/api/v1/research-lines", json=payload)

            elif name == "research_line_list":
                query_parts: list[str] = []
                if args.get("lab_id"):
                    query_parts.append(f"lab_id={quote(str(args['lab_id']), safe='')}")
                if args.get("archived") is not None:
                    query_parts.append(f"archived={'true' if bool(args['archived']) else 'false'}")
                path = "/api/v1/research-lines"
                if query_parts:
                    path = f"{path}?{'&'.join(query_parts)}"
                return await _exec_api(base_url, "GET", path)

            elif name == "research_line_get":
                return await _exec_api(
                    base_url,
                    "GET",
                    f"/api/v1/research-lines/{quote(str(args['line_id']), safe='')}",
                )

            elif name == "research_line_link_study":
                return await _exec_api(
                    base_url,
                    "POST",
                    f"/api/v1/research-lines/{quote(str(args['line_id']), safe='')}/studies/{quote(str(args['study_id']), safe='')}",
                )

            elif name == "model_serverless_invoke":
                model_id = args.get("model_id") or args.get("model_name") or args.get("model")
                if not model_id:
                    return json.dumps(
                        {
                            "error": "model_serverless_invoke_requires_model_name",
                            "tool": name,
                            "capability_mode": "backend-native",
                        }
                    )
                return await _exec_api(base_url, "POST", "/api/v1/serverless-models/invoke", json={
                    "model_id": model_id,
                    "inputs": dict(args.get("inputs") or args.get("payload") or {}),
                    "metadata": dict(args.get("metadata") or {}),
                    "session_id": args.get("session_id") or _session_id,
                    "run_id": args.get("run_id"),
                    "requested_by": "ws_bridge_tool",
                    "provider_override": args.get("provider_override"),
                })

            # --- Product object tools (2026-06-14) ---
            elif name == "study_create":
                payload = {"name": args["name"]}
                if args.get("description"): payload["description"] = args["description"]
                if args.get("tags"): payload["tags"] = args["tags"]
                return await _exec_api(base_url, "POST", "/api/v1/studies", json=payload)

            elif name == "study_attach_resource":
                return await _exec_api(
                    base_url, "POST",
                    f"/api/v1/studies/{args['study_id']}/artifacts",
                    json={"artifact_id": args["artifact_id"]},
                )

            elif name == "kb_create":
                payload = {"name": args["name"]}
                if args.get("kb_type"): payload["kb_type"] = args["kb_type"]
                if args.get("canonical_query"): payload["canonical_query"] = args["canonical_query"]
                if args.get("target_entities"): payload["target_entities"] = args["target_entities"]
                if args.get("target_topics"): payload["target_topics"] = args["target_topics"]
                return await _exec_api(base_url, "POST", "/api/v1/kbs", json=payload)

            elif name == "kb_semantic_query":
                params = f"mode={args.get('mode', 'semantic')}&limit={args.get('limit', 10)}"
                payload = {"query": args["query"]}
                return await _exec_api(
                    base_url, "POST",
                    f"/api/v1/kbs/{args['kb_id']}/search?{params}",
                    json=payload,
                )

            elif name == "artifact_create":
                artifact_type = args.get("artifact_type", "")
                display_name = args.get("display_name", "")
                if not artifact_type or not display_name:
                    return json.dumps({"error": "artifact_type and display_name are required"})
                payload = {"artifact_type": artifact_type, "display_name": display_name}
                if args.get("ref_url"): payload["ref_url"] = args["ref_url"]
                if args.get("source"): payload["source"] = args["source"]
                if args.get("mime_type"): payload["mime_type"] = args["mime_type"]
                return await _exec_api(base_url, "POST", "/api/v1/artifacts", json=payload)

            elif name == "artifact_signed_url":
                return await _exec_api(
                    base_url, "GET",
                    f"/api/v1/artifacts/{args['artifact_id']}/download",
                )

            elif name == "publish_agent_message":
                payload = {
                    "message_type": args["message_type"],
                    "summary": str(args.get("summary", ""))[:500],
                    "from_agent": "driver",
                    "to_agent": args.get("to_agent", "broadcast"),
                    "confidence": float(args.get("confidence", 0.0)),
                }
                if args.get("manifest_uri"): payload["manifest_uri"] = args["manifest_uri"]
                if args.get("snippet_uri"): payload["snippet_uri"] = args["snippet_uri"]
                if args.get("resource_refs"): payload["resource_refs"] = args["resource_refs"]
                return await _exec_api(base_url, "POST", "/api/v1/agent-messages", json=payload)

            elif name == "scroll_agent_feed":
                limit = args.get("limit", 20)
                topic = args.get("topic", "")
                return await _exec_api(
                    base_url, "GET",
                    f"/api/v1/agent-messages?limit={limit}&topic={topic}",
                )

            # --- GraphRAG product tools (2026-06-14) ---
            elif name == "graphrag_query":
                payload = {"query_text": args["query_text"], "limit": args.get("limit", 10)}
                if args.get("study_id"): payload["study_id"] = args["study_id"]
                if args.get("kb_id"): payload["kb_id"] = args["kb_id"]
                return await _exec_api(base_url, "POST", "/api/v1/graphrag/query", json=payload)

            elif name == "graphrag_hop1":
                payload = {"seed_nodes": args["seed_nodes"], "limit": args.get("limit", 50)}
                if args.get("study_id"): payload["study_id"] = args["study_id"]
                return await _exec_api(base_url, "POST", "/api/v1/graphrag/hop1", json=payload)

            elif name == "graphrag_write_claim":
                payload = {
                    "content": str(args["content"])[:2000],
                    "study_id": args.get("study_id", ""),
                    "kb_id": args.get("kb_id", ""),
                    "fact_type": args.get("fact_type", "finding"),
                    "entities": args.get("entities", []),
                    "source_doi": args.get("source_doi", ""),
                    "confidence": float(args.get("confidence", 1.0)),
                }
                return await _exec_api(base_url, "POST", "/api/v1/graphrag/claim", json=payload)

            elif name == "graphrag_write_lmp_graph":
                payload = {
                    "study_id": args["study_id"],
                    "lmp_file": args["lmp_file"],
                    "max_edges": args.get("max_edges", 500),
                }
                return await _exec_api(base_url, "POST", "/api/v1/graphrag/lmp", json=payload)

            elif name == "graphrag_export_decision_subgraph":
                payload = {"query_focus": args["query_focus"]}
                if args.get("study_id"): payload["study_id"] = args["study_id"]
                return await _exec_api(base_url, "POST", "/api/v1/graphrag/export-decision-subgraph", json=payload)

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            logger.warning("Tool %s failed: %s", name, exc)
            return json.dumps({"error": str(exc)})

    return executor


# ---------------------------------------------------------------------------
# WebSocket event streamer
# ---------------------------------------------------------------------------

async def stream_agentic_loop(
    websocket: Any,
    user_text: str,
    *,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: str = "agent",
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_executor: Optional[Callable] = None,
    config: Optional[LoopConfig] = None,
) -> Dict[str, Any]:
    """Run the AgenticLoop and stream events over the WebSocket.

    Returns a summary dict with ``finish_reason``, ``total_steps``, ``final_text``.
    """
    registry = await get_registry()

    # Resolve provider
    if not provider_id:
        prov_pref = (os.getenv("MICA_LLM_PROVIDER") or "auto").strip().lower()
        if prov_pref in ("openai", "oai") and registry.has_provider("openai"):
            provider_id = "openai"
        elif prov_pref in ("claude", "anthropic") and registry.has_provider("anthropic"):
            provider_id = "anthropic"
        elif prov_pref in ("vertex", "vertexai") and registry.has_provider("vertex"):
            provider_id = "vertex"
        elif prov_pref in ("gemini", "google") and registry.has_provider("google"):
            provider_id = "google"
        else:
            # Auto: first available
            for pid in ("vertex", "openai", "anthropic", "google"):
                if registry.has_provider(pid):
                    provider_id = pid
                    break
        if not provider_id:
            raise RuntimeError("No LLM provider configured")

    # Default tools and executor
    if tools is None:
        tools = MICA_TOOLS

    cfg = config or LoopConfig(
        max_iterations=15,
        temperature=0.4,
        max_output_tokens=4096,
    )
    loop = AgenticLoop(registry, cfg)

    default_system = (
        "You are MICA, an advanced molecular biology and bioinformatics research assistant. "
        "You have access to tools for protein search, structure analysis, literature search, "
        "visualization, workspace management, knowledge graphs, DLM scans, and report generation. "
        "Use tools proactively to answer questions thoroughly. When analyzing proteins, always "
        "search for structure AND literature. For deep research, use run_deep_research or "
        "run_dlm_scan. Be precise and cite sources."
    )

    # --- Resource injection (pre-populate context if MCP resources match) ---
    effective_text = user_text
    try:
        effective_text, res_count, res_chars = await _inject_resources(user_text)
        if res_count > 0:
            logger.info("Injected %d MCP resources (%d chars) into query", res_count, res_chars)
            # Notify the frontend
            try:
                await websocket.send_json({
                    "type": "STATE_UPDATE",
                    "payload": {
                        "status": "context_enriched",
                        "message": f"Injected {res_count} knowledge resources ({res_chars} chars)",
                    },
                })
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Resource injection skipped: %s", exc)

    # --- Tool selection via ToolKG router ---
    effective_tools = tools
    try:
        from mica.toolkg.schema import ToolRegistry
        from mica.toolkg.router import QueryIntentRouter
        from mica.toolkg.capability_inventory import InventoryBuilder, MCPToolDescriptor

        tools_by_server: Dict[str, List[Dict[str, Any]]] = {"mcp_ws_bridge": []}
        descriptors: List[MCPToolDescriptor] = []
        for t in tools:
            fn = t.get("function", t)
            name = fn.get("name", "")
            tools_by_server["mcp_ws_bridge"].append({
                "name": name,
                "description": fn.get("description", ""),
                "inputSchema": fn.get("parameters", {}),
            })
            descriptors.append(
                MCPToolDescriptor(
                    tool_id=f"mcp_ws_bridge.{name}",
                    server_id="mcp_ws_bridge",
                    name=name,
                    description=fn.get("description", ""),
                    input_schema=fn.get("parameters", {}) or {},
                    output_schema={},
                )
            )

        registry = ToolRegistry.from_mcp_list_tools(tools_by_server)
        registry = InventoryBuilder().merge_into_registry(registry, descriptors)

        global _ws_toolkg_smoke_checked
        if not _ws_toolkg_smoke_checked:
            try:
                from mica.toolkg.golden_fixtures import build_default_evaluator

                report = build_default_evaluator(registry).evaluate_by_tag("smoke")
                logger.info(
                    "WS ToolKG smoke fixtures: %d/%d passed (%.1f%%)",
                    report.passed,
                    report.total,
                    report.pass_rate * 100.0,
                )
            except Exception as eval_exc:
                logger.debug("WS ToolKG smoke eval skipped: %s", eval_exc)
            _ws_toolkg_smoke_checked = True

        router = QueryIntentRouter(registry)
        plan = router.route_with_fallback(user_text, available_artifacts=["query_string"])
        selected_names = {ptc.tool_name for ptc in plan.planned_tools}
        available_tool_names = {
            str(t.get("function", t).get("name") or "").strip()
            for t in tools
            if str(t.get("function", t).get("name") or "").strip()
        }
        intent_tags = {
            str(tag).strip().lower()
            for tag in list(getattr(plan, "intent_tags", []) or [])
            if str(tag).strip()
        }
        lowered_query = str(user_text or "").lower()
        selected_names |= set(
            infer_lmp_state_query_tool_names(
                user_text,
                available_tool_names=available_tool_names,
            )
        )
        scientific_lane = bool(
            {"protein_structure_download", "protein_interaction_network", "biological_pathway_analysis", "mechanistic_analysis", "critical_analysis"}
            & intent_tags
        ) or any(token in lowered_query for token in ("audit", "mechanistic", "benchmark", "structural"))
        required_closure_stages = ["evidence_acquisition", "critique", "vertical_synthesis"] if scientific_lane else []
        required_closure_tools = [
            name for name in ["consult_bibliotecario", "request_peer_review", "generate_vertical_report"]
            if name in available_tool_names
        ]
        no_tool_justification = ""

        if scientific_lane:
            selected_names |= set(required_closure_tools)
            if not selected_names and not required_closure_tools:
                no_tool_justification = (
                    "Scientific lane requires evidence acquisition, critique, and vertical synthesis, "
                    "but none of the closure tools are available in the active tool surface."
                )

        effective_tools = _apply_toolkg_selection(
            tools,
            selected_names=selected_names,
            scientific_lane=scientific_lane,
        )
        logger.info(
            "ToolKG routing: %d/%d tools selected (degraded=%s, intents=%s)",
            len(effective_tools),
            len(tools),
            plan.degraded,
            plan.intent_tags,
        )
    except Exception as exc:
        logger.debug("ToolKG routing skipped: %s", exc)

    # ── WI-12: Lane-aware tool filtering via capability registry ──
    try:
        _lane = "scientific_audit" if scientific_lane else "ws_runtime"
        _depth = depth_preset if "depth_preset" in locals() and depth_preset else "standard"
        effective_tools = filter_tools_for_lane(effective_tools, _lane, depth_preset_name=_depth)
    except Exception as _ftl_exc:
        logger.debug("filter_tools_for_lane skipped: %s", _ftl_exc)

    if tool_executor is None:
        routed_names = [
            str(tool.get("function", tool).get("name") or "").strip()
            for tool in effective_tools
            if str(tool.get("function", tool).get("name") or "").strip()
        ]
        runtime_authority = RuntimeAuthorityResolver().resolve(
            user_id=user_id,
            route_class="scientific_audit" if scientific_lane else "ws_runtime",
            role_id="driver",
            session_context={
                "route_card_id": f"ws::{session_id or 'ephemeral'}",
                "route_class": "scientific_audit" if scientific_lane else "ws_runtime",
                "authority": "mandatory",
                "selected_tool_names": list(routed_names),
                "visible_tool_names": list(routed_names),
                "internal_spawn_tools": [],
                "required_capabilities": list(plan.intent_tags or []) if "plan" in locals() else [],
                "required_closure_stages": list(required_closure_stages),
                "required_tool_names": list(required_closure_tools),
                "configured_providers": [provider_id] if provider_id else [],
                "provider_id": provider_id,
                "no_tool_justification": no_tool_justification,
            },
        ).to_runtime_authority_dict()
        tool_executor = await create_backend_executor(
            user_id=user_id,
            session_id=session_id,
            runtime_authority=runtime_authority,
        )

    messages: List[Dict[str, Any]] = [{"role": "user", "content": effective_text}]
    stream_id = uuid.uuid4().hex
    _step_counter = 0
    _event_log: List[Dict[str, Any]] = []

    final_text = ""
    # Collect structured artifacts from tool results for UI actions
    _pending_artifacts: List[Dict[str, Any]] = []
    result_summary: Dict[str, Any] = {
        "finish_reason": "unknown",
        "total_steps": 0,
        "final_text": "",
    }
    _effective_tool_names = [
        str(tool.get("function", tool).get("name") or "").strip()
        for tool in effective_tools
        if str(tool.get("function", tool).get("name") or "").strip()
    ]
    if not scientific_lane and not _effective_tool_names:
        protocol_manager: Any = _NoOpProtocolRuntimeManager()
    else:
        protocol_manager = ProtocolCueRuntimeManager(
            query=user_text,
            tool_names=_effective_tool_names,
            strictness="scientific_light",
            run_id=session_id or uuid.uuid4().hex,
            transport="ws",
        )

    async def _emit_protocol_events(events: List[Dict[str, Any]]) -> None:
        for event in events:
            try:
                await websocket.send_json(event)
                _event_log.append({
                    "type": event.get("type"),
                    "payload": event.get("payload", {}),
                })
            except Exception as exc:
                logger.debug("Protocol event send failed: %s", exc)

    async def _emit_workspace_action(action: str, data: Dict[str, Any]) -> None:
        """Send a WORKSPACE_ACTION event for autonomous UI control."""
        try:
            await websocket.send_json({
                "type": "WORKSPACE_ACTION",
                "payload": {"action": action, "data": data},
            })
        except Exception as exc:
            logger.debug("WORKSPACE_ACTION send failed: %s", exc)

    base_tool_executor = tool_executor
    contradiction_search_state: Dict[str, Any] = {
        "performed": False,
        "tool_name": "",
        "query": "",
        "result_excerpt": "",
    }

    def _build_protocol_closure_context() -> Dict[str, Any]:
        return {
            "query": user_text,
            "available_tool_names": list(_effective_tool_names),
            "contradicted_claims": [],
            "unsupported_critical_claims": [],
            "contradiction_search_performed": bool(contradiction_search_state.get("performed")),
            "contradiction_search_tool": str(contradiction_search_state.get("tool_name") or ""),
            "contradiction_search_query": str(contradiction_search_state.get("query") or ""),
            "contradiction_search_result_excerpt": str(contradiction_search_state.get("result_excerpt") or ""),
        }

    async def _run_forced_contradiction_search(interrupt: ScientificInterrupt) -> None:
        tool_name = str(interrupt.tool_name or "search_literature")
        tool_args = dict(interrupt.tool_args or {})
        contradiction_search_state["performed"] = True
        contradiction_search_state["tool_name"] = tool_name
        contradiction_search_state["query"] = str(tool_args.get("query") or tool_args.get("focus") or user_text)
        call_id = f"protocol-contradiction-{uuid.uuid4().hex[:10]}"
        result_text = await base_tool_executor(tool_name, call_id, tool_args)
        contradiction_search_state["result_excerpt"] = str(result_text or "")[:500]
        post_gate = protocol_manager.post_tool_gate(tool_name=tool_name, result_text=result_text, call_id=call_id)
        await _emit_protocol_events(list(post_gate.get("events") or []))

    async def _protocol_guarded_executor(name: str, call_id: str, args: Dict[str, Any]) -> str:
        pre_gate = protocol_manager.pre_tool_gate(tool_name=name, args=args, call_id=call_id)
        await _emit_protocol_events(list(pre_gate.get("events") or []))
        if pre_gate.get("blocked"):
            return json.dumps(
                {
                    "status": "unavailable",
                    "failure_reason": "PROTOCOL_CUE_BLOCKED",
                    "note": str(pre_gate.get("message") or f"Protocol cue blocked tool '{name}'."),
                    "cue_id": str(pre_gate.get("cue_id") or ""),
                    "fail_action": str(pre_gate.get("fail_action") or "warn"),
                    "tool_name": name,
                },
                ensure_ascii=False,
            )

        result_text = await base_tool_executor(name, call_id, args)
        post_gate = protocol_manager.post_tool_gate(tool_name=name, result_text=result_text, call_id=call_id)
        await _emit_protocol_events(list(post_gate.get("events") or []))
        if post_gate.get("blocked"):
            return json.dumps(
                {
                    "status": "unavailable",
                    "failure_reason": "PROTOCOL_CUE_BLOCKED",
                    "note": str(post_gate.get("message") or f"Protocol cue blocked tool '{name}' result."),
                    "cue_id": str(post_gate.get("cue_id") or ""),
                    "fail_action": str(post_gate.get("fail_action") or "warn"),
                    "tool_name": name,
                    "original_result": result_text[:500],
                },
                ensure_ascii=False,
            )
        return result_text

    tool_executor = _protocol_guarded_executor

    await _emit_protocol_events(list(protocol_manager.protocol_events))

    async for event in loop.run(
        messages=messages,
        tools=effective_tools,
        tool_executor=tool_executor,
        provider_id=provider_id,
        model_id=model_id,
        system_prompt=system_prompt or default_system,
    ):
        d = event.to_dict()
        etype = d["type"]

        try:
            if etype == "stream_start":
                _step_counter = d["step"]
                _event_log.append({"type": "WorkflowStarted", "step": _step_counter})
                # Send STREAM_START so frontend creates streaming bubble
                await websocket.send_json({
                    "type": "STREAM_START",
                    "payload": {
                        "id": stream_id,
                        "timestamp": __import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc
                        ).isoformat(),
                    },
                })
                await websocket.send_json({
                    "type": "STATE_UPDATE",
                    "payload": {
                        "status": "thinking",
                        "message": f"Step {d['step']}...",
                        "event_log": _event_log,
                    },
                })

            elif etype == "text_delta":
                delta = d["text"]
                final_text += delta
                # STREAM_CHUNK (aligned with frontend expectation)
                if delta:
                    await websocket.send_json({
                        "type": "STREAM_CHUNK",
                        "payload": {"id": stream_id, "text": delta},
                    })

            elif etype == "tool_call_start":
                step_entry = {
                    "type": "ToolCallStarted",
                    "step": _step_counter,
                    "tool": d["name"],
                    "call_id": d["call_id"],
                }
                _event_log.append(step_entry)
                # ACTION_STEP (aligned with frontend ActionStep type)
                await websocket.send_json({
                    "type": "ACTION_STEP",
                    "payload": {
                        "id": d["call_id"],
                        "kind": "tool_call",
                        "name": d["name"],
                        "details": json.dumps(d["args"])[:500] if d.get("args") else "",
                        "status": "running",
                        "step": _step_counter,
                    },
                })

            elif etype == "tool_call_end":
                _event_log.append({
                    "type": "ToolCallCompleted",
                    "step": _step_counter,
                    "tool": d["name"],
                    "call_id": d["call_id"],
                    "duration_ms": d["duration_ms"],
                })
                # ACTION_STEP completion
                await websocket.send_json({
                    "type": "ACTION_STEP",
                    "payload": {
                        "id": d["call_id"],
                        "kind": "tool_call",
                        "name": d["name"],
                        "details": d["result"][:300] if d.get("result") else "",
                        "status": "completed",
                        "duration_ms": d["duration_ms"],
                        "truncated": d.get("was_truncated", False),
                    },
                })
                # Detect structured results for WORKSPACE_ACTION
                _check_tool_result_for_ui_action(
                    d["name"], d.get("result", ""), _pending_artifacts, _emit_workspace_action
                )

            elif etype == "step_finish":
                _event_log.append({
                    "type": "NodeExecutionCompleted",
                    "step": d["step"],
                })
                # Close the text stream for this step
                await websocket.send_json({
                    "type": "STREAM_END",
                    "payload": {"id": stream_id},
                })
                await websocket.send_json({
                    "type": "STATE_UPDATE",
                    "payload": {
                        "status": "thinking",
                        "message": f"Step {d['step']} complete",
                        "usage": d["usage"],
                        "cost_usd": d["cost_usd"],
                        "event_log": _event_log,
                    },
                })
                # New stream_id for next step
                stream_id = uuid.uuid4().hex

            elif etype == "retry_wait":
                await websocket.send_json({
                    "type": "RetryWait",
                    "payload": {
                        "attempt": d.get("attempt", 1),
                        "delayMs": d.get("delay_ms", 0),
                        "error": d.get("error_message", ""),
                        "timestamp": __import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc
                        ).isoformat(),
                    },
                })

            elif etype == "resource_injected":
                await websocket.send_json({
                    "type": "STATE_UPDATE",
                    "payload": {
                        "status": "thinking",
                        "message": f"Resources injected: {d.get('count', 0)} ({d.get('total_chars', 0)} chars)",
                        "event_log": _event_log,
                    },
                })

            elif etype == "agent_turn":
                await websocket.send_json({
                    "type": "AgentTurn",
                    "payload": {
                        "agent": d.get("agent", ""),
                        "role": d.get("role", "thinking"),
                        "text": d.get("text", ""),
                        "sessionId": d.get("session_id", ""),
                        "timestamp": __import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc
                        ).isoformat(),
                    },
                })

            elif etype == "side_data":
                await websocket.send_json({
                    "type": "SIDE_DATA",
                    "payload": {
                        "channel": d.get("channel", ""),
                        "agent": d.get("agent", ""),
                        "data": d.get("payload", {}),
                        "timestamp": __import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc
                        ).isoformat(),
                    },
                })

            elif etype == "loop_finish":
                _now = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat()
                _event_log.append({
                    "type": "WorkflowCompleted",
                    "total_steps": d["total_steps"],
                    "finish_reason": d["finish_reason"],
                })
                result_summary = {
                    "finish_reason": d["finish_reason"],
                    "total_steps": d["total_steps"],
                    "total_cost_usd": d.get("total_cost_usd", 0),
                    "final_text": final_text,
                }
                while True:
                    try:
                        await _emit_protocol_events(
                            protocol_manager.finalize(
                                final_text=final_text,
                                total_steps=d["total_steps"],
                                closure_context=_build_protocol_closure_context(),
                            )
                        )
                        break
                    except ScientificInterrupt as interrupt:
                        await _emit_protocol_events(list(interrupt.events))
                        await _run_forced_contradiction_search(interrupt)
                # Emit LoopFinish so frontend can render cost/steps/tool log
                await websocket.send_json({
                    "type": "LoopFinish",
                    "payload": {
                        "reason": d["finish_reason"],
                        "steps": d["total_steps"],
                        "costUsd": d.get("total_cost_usd", 0),
                        "toolLog": _event_log,
                        "timestamp": _now,
                    },
                })

            elif etype == "error":
                _event_log.append({"type": "WorkflowFailed", "message": d["message"]})
                await websocket.send_json({
                    "type": "ERROR",
                    "payload": {"message": d["message"], "retryable": d.get("retryable", False)},
                })

            elif etype == "context_overflow":
                await websocket.send_json({
                    "type": "STATE_UPDATE",
                    "payload": {
                        "status": "warning",
                        "message": f"Context overflow: {d['prompt_tokens']}/{d['limit_tokens']} tokens",
                        "event_log": _event_log,
                    },
                })

            elif etype == "context_compacted":
                _event_log.append({"type": "ContextCompacted", "summary_chars": d["summary_chars"]})
                await websocket.send_json({
                    "type": "STATE_UPDATE",
                    "payload": {
                        "status": "compacted",
                        "message": (
                            f"Context compacted: {d['messages_before']}→{d['messages_after']} "
                            f"messages ({d['summary_chars']} chars summarised)"
                        ),
                        "event_log": _event_log,
                    },
                })

        except Exception as exc:
            logger.warning("Failed to send WS event %s: %s", etype, exc)

    # Build artifact from tool results if any structured data was collected
    artifact = None
    if _pending_artifacts:
        artifact = _pending_artifacts[0]  # Use first structured artifact

    # Send final text as TEXT_MESSAGE with optional artifact
    if final_text:
        try:
            payload: Dict[str, Any] = {
                "text": final_text,
                "timestamp": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
            }
            if artifact:
                payload["artifact"] = artifact
            await websocket.send_json({
                "type": "TEXT_MESSAGE",
                "payload": payload,
            })
        except Exception:
            pass

    # Final STATE_UPDATE with completed status and full event_log
    try:
        _protocol_payload = _build_ws_protocol_projection_state(protocol_manager.runtime_payload())
        await websocket.send_json({
            "type": "STATE_UPDATE",
            "payload": {
                "status": "idle",
                "message": "Completed",
                "event_log": _event_log,
                "protocol_runtime": _protocol_payload.get("protocol_runtime", protocol_manager.envelope.model_dump()),
                "protocol_events": _protocol_payload.get("protocol_events", protocol_manager.protocol_events),
                "scientific_protocol": _protocol_payload.get("scientific_protocol", {}),
                "prompt_protocol": _protocol_payload.get("prompt_protocol", {}),
                "unified_protocol_runtime": _protocol_payload.get("unified_protocol_runtime", {}),
                "protocol_runtime_projection": _protocol_payload.get("protocol_runtime_projection", {}),
                "total_cost_usd": result_summary.get("total_cost_usd", 0),
            },
        })
    except Exception:
        pass

    result_summary.update(_build_ws_protocol_projection_state(protocol_manager.runtime_payload()))
    return result_summary


def _check_tool_result_for_ui_action(
    tool_name: str,
    result: str,
    pending_artifacts: List[Dict[str, Any]],
    emit_fn: Callable[..., Any],
) -> None:
    """Detect structured tool results that should trigger WORKSPACE_ACTION events.

    Parses JSON tool results and queues UI actions for the agent to autonomously
    control the frontend (open viewers, push entities, update knowledge graph).
    """
    if not result:
        return
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return

    if not isinstance(data, dict):
        return

    # resolve_pdb / get_pdb_info → open 3D viewer
    if tool_name in ("resolve_pdb", "get_pdb_info") and data.get("pdb_id"):
        pdb_id = data["pdb_id"]
        pdb_url = data.get("pdb_url") or f"https://files.rcsb.org/download/{pdb_id}.pdb"
        gene = data.get("gene_name", pdb_id)
        asyncio.ensure_future(emit_fn("ADD_TOOL", {
            "toolType": "proteo_window",
            "title": f"{gene} ({pdb_id})",
            "config": {"pdbUrl": pdb_url, "pdbId": pdb_id},
        }))
        pending_artifacts.append({
            "type": "molecular_structure",
            "pdb_id": pdb_id,
            "title": gene,
        })

    # search_protein → push entities to CognitiveEngine
    elif tool_name == "search_protein" and (data.get("name") or data.get("gene_name")):
        entity = {
            "name": data.get("gene_name") or data.get("name", ""),
            "accession": data.get("accession", ""),
            "type": "PROTEIN",
            "pdbs": data.get("pdb_ids", []),
        }
        asyncio.ensure_future(emit_fn("PUSH_ENTITIES", {"entities": [entity]}))

    # get_knowledge_graph → update KG panel
    elif tool_name == "get_knowledge_graph" and data.get("nodes"):
        asyncio.ensure_future(emit_fn("UPDATE_KG", {"graphData": data}))

    # search_literature → collect as document_list artifact
    elif tool_name == "search_literature" and data.get("papers"):
        pending_artifacts.append({
            "type": "document_list",
            "title": f"Literature search ({len(data['papers'])} papers)",
            "papers": data["papers"][:20],  # Cap for context size
        })

    # get_domains / analyze_domains → push domain highlights
    elif tool_name in ("get_domains", "analyze_domains") and data.get("domains"):
        regions = []
        for dom in data["domains"][:20]:
            if dom.get("start") and dom.get("end"):
                regions.append({
                    "start_residue": dom["start"],
                    "end_residue": dom["end"],
                    "color": dom.get("color", "#ef4444"),
                    "chain_id": dom.get("chain", "A"),
                    "label": dom.get("name", "Domain"),
                })
        if regions:
            asyncio.ensure_future(emit_fn("HIGHLIGHT_RESIDUES", {"regions": regions}))


def _chunk_text(text: str, chunk_size: int = 20) -> List[str]:
    """Split text into chunks for smooth streaming UX."""
    if len(text) <= chunk_size:
        return [text] if text else []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

"""
Backend Command Manifest — canonical source of truth for all MICA backend commands.

ToolKG (MICA_TOOLS / tool_capability_registry) should be VALIDATED AGAINST this manifest,
never the other way around.

This manifest is importable by:
- backend CLI (tools/mica_backend_cli.py)
- shell (tools/mica_shell.py)
- AgenticDriver (src/mica/drivers/agentic_driver.py)
- ToolKG parity checker
- tests
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass(frozen=True)
class BackendCommandSpec:
    """Immutable specification for one backend command."""
    command_id: str                                    # "sys.health"
    family: str                                        # "sys"
    name: str                                          # "health"
    description: str
    method: Optional[str] = None                       # GET, POST, PATCH, DELETE
    route: Optional[str] = None                        # "/api/v1/health"
    transports: List[str] = field(default_factory=lambda: ["local_http", "railway_http"])
    auth_required: bool = False
    auth_modes: List[str] = field(default_factory=lambda: ["none"])
    input_schema_ref: Optional[str] = None
    output_schema_ref: Optional[str] = None
    input_artifact_types: List[str] = field(default_factory=list)
    output_artifact_types: List[str] = field(default_factory=list)
    resource_templates: List[str] = field(default_factory=list)
    risk_tier: str = "T0"                              # T0-T4
    supports_dry_run: bool = False
    product_proof_requires: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "family": self.family,
            "name": self.name,
            "description": self.description,
            "method": self.method,
            "route": self.route,
            "transports": self.transports,
            "auth_required": self.auth_required,
            "auth_modes": self.auth_modes,
            "risk_tier": self.risk_tier,
            "supports_dry_run": self.supports_dry_run,
            "blockers": self.blockers,
        }


# ── COMMAND INVENTORY ──────────────────────────────────────────────────────

COMMANDS: List[BackendCommandSpec] = [
    # ── sys ────────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="sys.health",
        family="sys", name="health",
        description="Backend health check",
        method="GET", route="/api/v1/health",
        transports=["local_http", "railway_http"],
        auth_required=False, auth_modes=["none"],
        risk_tier="T0", supports_dry_run=False,
    ),
    BackendCommandSpec(
        command_id="sys.routes",
        family="sys", name="routes",
        description="List registered backend routes",
        method="GET", route="/api/v1/runtime/routes",
        transports=["local_http"],
        auth_required=False, auth_modes=["none"],
        risk_tier="T0", supports_dry_run=False,
    ),
    BackendCommandSpec(
        command_id="sys.fingerprint",
        family="sys", name="fingerprint",
        description="Runtime system fingerprint",
        method="GET", route="/api/v1/runtime/fingerprint",
        transports=["local_http", "railway_http"],
        auth_required=False, auth_modes=["none"],
        risk_tier="T0", supports_dry_run=False,
    ),

    # ── routes ─────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="routes.list",
        family="routes", name="list",
        description="List all available backend command routes",
        method="GET", route="/api/v1/runtime/routes",
        transports=["local_http"],
        auth_required=False, auth_modes=["none"],
        risk_tier="T0", supports_dry_run=False,
    ),

    # ── workspace ──────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="workspace.status",
        family="workspace", name="status",
        description="GCS user workspace status",
        method="GET", route="/api/v1/user-bucket/status",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=True,
        product_proof_requires=["gcs_connectivity"],
    ),
    BackendCommandSpec(
        command_id="workspace.list",
        family="workspace", name="list",
        description="List objects in GCS user workspace",
        method="GET", route="/api/v1/user-bucket/list",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
        product_proof_requires=["gcs_connectivity"],
    ),

    # ── artifact ───────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="artifact.list",
        family="artifact", name="list",
        description="List artifacts for a session/run",
        method="GET", route="/api/v1/artifacts",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
        product_proof_requires=["gcs_backend_wired"],
        blockers=["artifact_writer_gcs_unwired"],
    ),
    BackendCommandSpec(
        command_id="artifact.read",
        family="artifact", name="read",
        description="Read artifact content by ID",
        method="GET", route="/api/v1/artifacts/{artifact_id}",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
        product_proof_requires=["gcs_backend_wired"],
    ),
    BackendCommandSpec(
        command_id="artifact.upload",
        family="artifact", name="upload",
        description="Upload artifact to workspace",
        method="POST", route="/api/v1/artifacts/upload",
        transports=["local_http", "railway_http", "gcs"],
        auth_required=True, auth_modes=["clerk", "service_token", "gcp"],
        risk_tier="T2", supports_dry_run=True,
        product_proof_requires=["gcs_backend_wired", "gcs_connectivity"],
    ),

    # ── resource ───────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="resource.list",
        family="resource", name="list",
        description="List MCP resource templates",
        transports=["resource", "local_http"],
        auth_required=False, auth_modes=["local_env"],
        risk_tier="T0", supports_dry_run=False,
    ),
    BackendCommandSpec(
        command_id="resource.read",
        family="resource", name="read",
        description="Read resource by mica:// URI",
        transports=["resource", "local_http"],
        auth_required=False, auth_modes=["local_env"],
        risk_tier="T0", supports_dry_run=False,
        resource_templates=[
            "mica://workspace/artifact/{artifact_id}",
            "mica://bio/sequence/{sequence_id}",
            "mica://bio/fasta/{fasta_id}",
            "mica://bio/structure/{structure_id}",
            "mica://pdb/{pdb_id}",
            "mica://afdb/{uniprot_id}",
            "mica://dlm/manifest/{query_hash}",
            "mica://lmp/portrait/{portrait_id}",
            "mica://compute/job/{job_id}/artifact/{artifact_name}",
        ],
    ),

    # ── literature ─────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="literature.search",
        family="literature", name="search",
        description="Search literature via Bibliotecario",
        method="POST", route="/api/v1/research/search",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T2", supports_dry_run=True,
        blockers=["semantic_scholar_key_not_passed_to_client"],
    ),

    # ── semantic_scholar ───────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="semantic_scholar.health",
        family="semantic_scholar", name="health",
        description="Semantic Scholar API health check (direct)",
        transports=["local_http"],
        auth_required=True, auth_modes=["local_env"],
        risk_tier="T0", supports_dry_run=False,
        product_proof_requires=["semantic_scholar_api_key_loaded"],
        blockers=["semantic_scholar_key_not_passed_to_client"],
    ),
    BackendCommandSpec(
        command_id="semantic_scholar.search_smoke",
        family="semantic_scholar", name="search_smoke",
        description="Minimal Semantic Scholar search smoke test",
        transports=["local_http"],
        auth_required=True, auth_modes=["local_env"],
        risk_tier="T1", supports_dry_run=True,
        product_proof_requires=["semantic_scholar_api_key_loaded"],
        blockers=["semantic_scholar_key_not_passed_to_client"],
    ),

    # ── bibliotecario ──────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="bibliotecario.scan",
        family="bibliotecario", name="scan",
        description="Run Bibliotecario literature scan",
        method="POST", route="/api/v1/research/bibliotecario/scan",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T3", supports_dry_run=True,
        product_proof_requires=["gcs_backend_wired"],
    ),

    # ── dlm ────────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="dlm.scan",
        family="dlm", name="scan",
        description="Run DLM document scan (redirected to bibliotecario)",
        method="POST", route="/api/v1/bibliotecario/scan",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T3", supports_dry_run=True,
    ),

    # ── lmp ────────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="lmp.compile_smoke",
        family="lmp", name="compile_smoke",
        description="Minimal LMP compile smoke test",
        transports=["local_http", "mcp_resource"],
        auth_required=False, auth_modes=["local_env"],
        risk_tier="T1", supports_dry_run=True,
        resource_templates=["mica://lmp/portrait/{portrait_id}"],
    ),
    BackendCommandSpec(
        command_id="lmp.generate",
        family="lmp", name="generate",
        description="Generate LMP molecular portrait",
        method="POST", route="/api/v1/lmp/generate",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T3", supports_dry_run=False,
        product_proof_requires=["gcs_backend_wired"],
        output_artifact_types=["lmp_portrait"],
    ),

    # ── model ──────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="model.list",
        family="model", name="list",
        description="List available serverless models",
        method="GET", route="/api/v1/serverless-models",
        transports=["local_http", "railway_http"],
        auth_required=False, auth_modes=["none"],
        risk_tier="T0", supports_dry_run=False,
    ),
    BackendCommandSpec(
        command_id="model.serverless_smoke",
        family="model", name="serverless_smoke",
        description="Minimal serverless model invocation smoke test",
        method="POST", route="/api/v1/serverless-models/invoke",
        transports=["local_http", "modal"],
        auth_required=True, auth_modes=["clerk", "service_token", "modal"],
        risk_tier="T3", supports_dry_run=True,
        blockers=["modal_esm3_descriptor_only", "modal_auth_unverified"],
    ),

    # ── compute ────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="compute.jobs.submit",
        family="compute", name="submit",
        description="Submit compute job (MD/CG)",
        method="POST", route="/api/v1/compute/jobs",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T3", supports_dry_run=True,
        product_proof_requires=["gcs_backend_wired", "job_receipt_durable"],
    ),
    BackendCommandSpec(
        command_id="compute.jobs.status",
        family="compute", name="status",
        description="Check compute job status",
        method="GET", route="/api/v1/compute/jobs/{job_id}",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
    ),

    # ── modal ──────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="modal.health",
        family="modal", name="health",
        description="Modal runtime health check",
        transports=["modal"],
        auth_required=True, auth_modes=["modal"],
        risk_tier="T0", supports_dry_run=False,
        blockers=["modal_auth_unverified", "modal_esm3_descriptor_only"],
    ),
    BackendCommandSpec(
        command_id="modal.model_smoke",
        family="modal", name="model_smoke",
        description="Minimal Modal model invocation smoke",
        transports=["modal"],
        auth_required=True, auth_modes=["modal"],
        risk_tier="T3", supports_dry_run=True,
        blockers=["modal_esm3_descriptor_only", "modal_cold_start_timeout"],
    ),

    # ── sandbox ────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="sandbox.health",
        family="sandbox", name="health",
        description="Sandbox execution health check",
        transports=["sandbox", "modal"],
        auth_required=True, auth_modes=["modal"],
        risk_tier="T0", supports_dry_run=False,
        blockers=["sandbox_unavailable", "sandbox_policy_blocked"],
    ),
    BackendCommandSpec(
        command_id="sandbox.python_smoke",
        family="sandbox", name="python_smoke",
        description="Minimal Python sandbox execution smoke",
        transports=["sandbox", "modal"],
        auth_required=True, auth_modes=["modal"],
        risk_tier="T2", supports_dry_run=True,
        blockers=["sandbox_unavailable"],
    ),

    # ── study ──────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="study.list",
        family="study", name="list",
        description="List studies",
        method="GET", route="/api/v1/studies",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
    ),

    # ── kb ─────────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="kb.list",
        family="kb", name="list",
        description="List knowledge bases",
        method="GET", route="/api/v1/kbs",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
    ),

    # ── working_set ────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="working_set.list",
        family="working_set", name="list",
        description="List working sets",
        method="GET", route="/api/v1/working-sets",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
    ),

    # ── protocol ───────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="protocol.validate",
        family="protocol", name="validate",
        description="Validate a scientific protocol",
        method="POST", route="/api/v1/protocols/validate",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=True,
    ),
    BackendCommandSpec(
        command_id="protocol.run",
        family="protocol", name="run",
        description="Execute a scientific protocol",
        method="POST", route="/api/v1/protocols/run",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T3", supports_dry_run=True,
        product_proof_requires=["gcs_backend_wired", "job_receipt_durable"],
    ),

    # ── jobs ───────────────────────────────────────────────────────────────
    BackendCommandSpec(
        command_id="jobs.list",
        family="jobs", name="list",
        description="List jobs",
        method="GET", route="/api/v1/jobs",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
    ),
    BackendCommandSpec(
        command_id="jobs.status",
        family="jobs", name="status",
        description="Check job status by ID",
        method="GET", route="/api/v1/jobs/{job_id}",
        transports=["local_http", "railway_http"],
        auth_required=True, auth_modes=["clerk", "service_token"],
        risk_tier="T1", supports_dry_run=False,
    ),
]


# ── INDEXES ────────────────────────────────────────────────────────────────

COMMAND_BY_ID: Dict[str, BackendCommandSpec] = {c.command_id: c for c in COMMANDS}
COMMANDS_BY_FAMILY: Dict[str, List[BackendCommandSpec]] = {}
for c in COMMANDS:
    COMMANDS_BY_FAMILY.setdefault(c.family, []).append(c)


def get_command(command_id: str) -> Optional[BackendCommandSpec]:
    """Look up a command by its canonical ID."""
    return COMMAND_BY_ID.get(command_id)


def get_commands_by_family(family: str) -> List[BackendCommandSpec]:
    """Get all commands in a family."""
    return COMMANDS_BY_FAMILY.get(family, [])


def list_families() -> List[str]:
    """List all command families."""
    return sorted(COMMANDS_BY_FAMILY.keys())


def list_commands() -> List[BackendCommandSpec]:
    """List all registered commands."""
    return list(COMMANDS)

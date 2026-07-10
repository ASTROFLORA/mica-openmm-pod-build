from typing import Any, Dict, List
from mica.sdk.shell_contracts import AgentSkillCard, AgentRuntimeCuePack, ShellCapability

CAPABILITIES = [
    ShellCapability(
        capability_id="sys:status",
        name="system",
        backend_authority="api_v1/health_probes.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="jobs:list",
        name="jobs",
        backend_authority="api_v1/routers/jobs.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="artifacts:ls",
        name="artifacts",
        backend_authority="api_v1/routers/user_bucket.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="protocol:run",
        name="protocols",
        backend_authority="mica/drivers/execution/protocol_executor.py",
        implemented=True,
        risk_tier="T3",
        dry_run_supported=True
    ),
    ShellCapability(
        capability_id="biodynamo:presets",
        name="biodynamo",
        backend_authority="api_v1/routers/biodynamo.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="biodynamo:compile",
        name="biodynamo",
        backend_authority="api_v1/routers/biodynamo.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="biodynamo:run",
        name="biodynamo",
        backend_authority="api_v1/routers/biodynamo.py",
        implemented=True,
        risk_tier="T1",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="mudo:show",
        name="mudo",
        backend_authority="api_v1/routers/mudo.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="mudo:codex",
        name="mudo",
        backend_authority="api_v1/routers/mudo.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="smic:metrics",
        name="smic/quetzal",
        backend_authority="api_v1/routers/smic.py",
        implemented=False,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="literature:quorum",
        name="literature",
        backend_authority="api_v1/routers/literature.py",
        implemented=False,
        risk_tier="T1",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="sandbox:run",
        name="sandbox",
        backend_authority="api_v1/routers/workspace.py",
        implemented=False,
        risk_tier="T2",
        dry_run_supported=True
    ),
    ShellCapability(
        capability_id="jobs:show",
        name="jobs",
        backend_authority="api_v1/routers/compute.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="jobs:timeline",
        name="jobs",
        backend_authority="api_v1/routers/compute.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="jobs:artifacts",
        name="jobs",
        backend_authority="api_v1/routers/compute.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="jobs:ui-state",
        name="jobs",
        backend_authority="api_v1/routers/compute.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="workspace:files",
        name="workspace",
        backend_authority="api_v1/routers/workspace.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="workspace:artifact:show",
        name="workspace",
        backend_authority="api_v1/routers/workspace.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="workspace:artifact:download-url",
        name="workspace",
        backend_authority="api_v1/routers/workspace.py",
        implemented=True,
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="study:list",
        name="study",
        backend_authority="api_v1/routers/studies.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="study:create",
        name="study",
        backend_authority="api_v1/routers/studies.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="study:summary",
        name="study",
        backend_authority="api_v1/routers/studies.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="kb:list",
        name="kb",
        backend_authority="api_v1/routers/knowledge_fabric.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="kb:create",
        name="kb",
        backend_authority="api_v1/routers/knowledge_fabric.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="kb:search",
        name="kb",
        backend_authority="api_v1/routers/knowledge_fabric.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="bibliotecario:presets",
        name="bibliotecario",
        backend_authority="api_v1/routers/bibliotecario.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="bibliotecario:scan",
        name="bibliotecario",
        backend_authority="api_v1/routers/bibliotecario.py",
        implemented=True,
        risk_tier="T1",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="bibliotecario:status",
        name="bibliotecario",
        backend_authority="api_v1/routers/bibliotecario.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="dlm:presets",
        name="dlm",
        backend_authority="api_v1/routers/dlm.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="dlm:scan",
        name="dlm",
        backend_authority="api_v1/routers/dlm.py",
        implemented=True,
        risk_tier="T1",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="gcs:list",
        name="gcs",
        backend_authority="api_v1/routers/user_bucket.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="gcs:upload",
        name="gcs",
        backend_authority="api_v1/routers/user_bucket.py",
        implemented=True,
        risk_tier="T1",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="gcs:read",
        name="gcs",
        backend_authority="api_v1/routers/user_bucket.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="working-set:create",
        name="working-set",
        backend_authority="api_v1/routers/working_sets.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="working-set:add",
        name="working-set",
        backend_authority="api_v1/routers/working_sets.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="alejandria-search:runs",
        name="alejandria-search",
        backend_authority="api_v1/routers/alejandria_search.py",
        implemented=True,
        risk_tier="T1",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="alejandria-search:show",
        name="alejandria-search",
        backend_authority="api_v1/routers/alejandria_search.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    ),
    ShellCapability(
        capability_id="alejandria-search:hits",
        name="alejandria-search",
        backend_authority="api_v1/routers/alejandria_search.py",
        implemented=True,
        risk_tier="T0",
        dry_run_supported=False
    )
]

RUNTIME_SKILL_CARDS = [
    AgentSkillCard(
        skill_id="sys:status",
        name="System Status Check",
        description="Inspects the runtime state and availability of core backend systems.",
        shell_verb="mica status",
        backend_route="/api/v1/health",
        is_async=False,
        receipt_required=False,
        next_actions=["sys:capabilities"],
        failure_recovery="Check network connectivity; check MICA_API_BASE_URL config."
    ),
    AgentSkillCard(
        skill_id="sys:capabilities",
        name="Capabilities Discovery",
        description="Lists all registered and implemented MICA capabilities.",
        shell_verb="mica capabilities",
        backend_route="/api/v1/capabilities",
        is_async=False,
        receipt_required=False,
        next_actions=["protocol:run"],
        failure_recovery="Inspect router registration files; rebuild capabilities registry."
    ),
    AgentSkillCard(
        skill_id="protocol:run",
        name="Protocol Runner",
        description="Executes a JSON-LD protocol workflow locally or on the API.",
        shell_verb="mica protocol run",
        backend_route="/api/v1/research/protocols/run",
        is_async=True,
        receipt_required=True,
        next_actions=["jobs:show"],
        failure_recovery="Run in --mock mode or check JSON-LD validation errors."
    ),
    AgentSkillCard(
        skill_id="study:list",
        name="Study Indexing",
        description="Lists all research studies currently stored in the system.",
        shell_verb="mica study list",
        backend_route="/api/v1/studies",
        is_async=False,
        receipt_required=True,
        next_actions=["study:create", "study:summary"],
        failure_recovery="Ensure TimescaleDB / Neon database connection is online."
    ),
    AgentSkillCard(
        skill_id="study:create",
        name="Study Initialization",
        description="Creates a new study to namespace literature search and working sets.",
        shell_verb="mica study create",
        backend_route="/api/v1/studies",
        is_async=False,
        receipt_required=True,
        next_actions=["working-set:create"],
        failure_recovery="Verify study name uniqueness."
    ),
    AgentSkillCard(
        skill_id="kb:search",
        name="Knowledge Base Search",
        description="Executes vector or hybrid search over KB documents.",
        shell_verb="mica kb search",
        backend_route="/api/v1/kbs/{kb_id}/search",
        is_async=False,
        receipt_required=True,
        next_actions=["working-set:add"],
        failure_recovery="If the backend reports database pending, classify the blocker explicitly or rerun in explicit fixture mode."
    ),
    AgentSkillCard(
        skill_id="bibliotecario:scan",
        name="Literature Ingestion & Scan",
        description="Enqueues literature retrieval, extraction, and synthesis jobs.",
        shell_verb="mica bibliotecario scan",
        backend_route="/api/v1/research/bibliotecario/scan",
        is_async=True,
        receipt_required=True,
        next_actions=["bibliotecario:status"],
        failure_recovery="Verify credentials; ensure redis is online."
    ),
    AgentSkillCard(
        skill_id="bibliotecario:status",
        name="Literature Scan Status",
        description="Retrieves the status of an ongoing literature scan job.",
        shell_verb="mica bibliotecario status",
        backend_route="/api/v1/research/bibliotecario/scan/{job_id}/status",
        is_async=False,
        receipt_required=False,
        next_actions=["working-set:add"],
        failure_recovery="Wait and retry polling; check worker logs."
    ),
    AgentSkillCard(
        skill_id="dlm:scan",
        name="Deep Language Memory Extraction",
        description="Scans a text file or GCS URI for protein entities and facts.",
        shell_verb="mica dlm scan",
        backend_route="/api/v1/research/bibliotecario/scan",
        is_async=True,
        receipt_required=True,
        next_actions=["workspace:files"],
        failure_recovery="Check if entity extractor models are loaded in memory."
    ),
    AgentSkillCard(
        skill_id="gcs:upload",
        name="Cloud Storage Uploader",
        description="Uploads local files to GCS user storage bucket.",
        shell_verb="mica gcs upload",
        backend_route="/api/v1/user-bucket/upload",
        is_async=False,
        receipt_required=True,
        next_actions=["workspace:files"],
        failure_recovery="If the backend storage route fails, surface the backend blocker or rerun in explicit fixture mode."
    ),
    AgentSkillCard(
        skill_id="gcs:list",
        name="Cloud Storage Indexer",
        description="Lists objects stored in GCS user storage bucket.",
        shell_verb="mica gcs list",
        backend_route="/api/v1/user-bucket/objects",
        is_async=False,
        receipt_required=False,
        next_actions=["gcs:read"],
        failure_recovery="If Railway target fails, surface the backend storage blocker; only local target mode should depend on local Google credentials."
    ),
    AgentSkillCard(
        skill_id="working-set:create",
        name="Working Set Creation",
        description="Creates a new working set to group literature, results, or data.",
        shell_verb="mica working-set create",
        backend_route="/api/v1/working-sets",
        is_async=False,
        receipt_required=True,
        next_actions=["working-set:add"],
        failure_recovery="Check study_id link validity."
    ),
    AgentSkillCard(
        skill_id="workspace:files",
        name="Workspace Artifacts Queries",
        description="Lists semantic artifacts available in the GCS_USER_WORKSPACE.",
        shell_verb="mica workspace files",
        backend_route="/api/v1/workspace/files",
        is_async=False,
        receipt_required=True,
        next_actions=["workspace:artifact show"],
        failure_recovery="Ensure API router workspace prefix is configured."
    ),
    AgentSkillCard(
        skill_id="biodynamo:run",
        name="BioDynamo Simulation Runner",
        description="Runs a multi-agent cell simulation using BioDynamo.",
        shell_verb="mica biodynamo run",
        backend_route="/api/v1/biodynamo/run",
        is_async=True,
        receipt_required=True,
        next_actions=["jobs:show"],
        failure_recovery="Verify biostate JSON-LD preset mapping."
    ),
    AgentSkillCard(
        skill_id="mudo:show",
        name="M-UDO Inspection",
        description="Accesses details of a specific M-UDO entity from the database.",
        shell_verb="mica mudo show",
        backend_route="/api/v1/mudo/{mudo_id}",
        is_async=False,
        receipt_required=True,
        next_actions=["kb:search"],
        failure_recovery="Check if mudo ID is correct and DB connection exists."
    ),
    AgentSkillCard(
        skill_id="alejandria-search:runs",
        name="Alejandria Search Runner",
        description="Runs literature search on Alejandria Search backend.",
        shell_verb="mica search runs",
        backend_route="/api/v1/alejandria-search/runs",
        is_async=False,
        receipt_required=True,
        next_actions=["alejandria-search:hits"],
        failure_recovery="Verify Alejandria search indexes are up."
    )
]

def build_agent_runtime_cue_pack(context: Dict[str, Any] = None) -> AgentRuntimeCuePack:
    context = context or {}
    mode = context.get("mode", "interactive")
    active_surface = context.get("active_surface", "mica_shell")
    
    return AgentRuntimeCuePack(
        mode=mode,
        active_surface=active_surface,
        available_tools=[
            "status", "capabilities", "protocol", "artifacts", "jobs", "mudo",
            "biodynamo", "workspace", "study", "kb", "bibliotecario", "dlm",
            "gcs", "working-set", "skills", "cues", "search"
        ],
        available_skills=RUNTIME_SKILL_CARDS,
        canonical_routes={
            "status": "/api/v1/health",
            "capabilities": "/api/v1/capabilities",
            "study_list": "/api/v1/studies",
            "kb_list": "/api/v1/kbs",
            "bibliotecario_scan": "/api/v1/research/bibliotecario/scan",
            "gcs_upload": "/api/v1/user-bucket/upload"
        },
        forbidden_routes=[
            "/api/v1/admin/*",
            "/api/v1/internal/*"
        ],
        recipes={
            "literature_to_working_set": [
                "1. Create a study using: mica study create --name 'MyStudy'",
                "2. Create a working set using: mica working-set create --study-id <study_id>",
                "3. Scan literature using: mica bibliotecario scan 'WNK1' --max-papers 5",
                "4. Check status using: mica bibliotecario status <job_id>",
                "5. Add results to working set: mica working-set add <ws_id> --ref-type 'paper' --ref-id <doi>"
            ]
        },
        current_state_summary="MICA shell client backed by shared runtime cues. Live, local, and fixture modes are explicitly separated in receipts.",
        next_action_suggestions=[
            "Run 'mica status' to check connection.",
            "Run 'mica study list' to see current studies.",
            "Run 'mica bibliotecario presets' to inspect scan presets."
        ],
        receipt_requirements=[
            "receipt_id", "command_id", "status", "actual_cost_usd"
        ],
        failure_recovery_playbooks={
            "DATABASE_PENDING": "Classify as kb_db_dsn_pending and keep product_proof=false until the backend is healthy.",
            "CREDENTIALS_MISSING": "Classify as gcs_credentials_pending only for local target mode; Railway mode must surface backend authority instead.",
            "GATEWAY_TIMEOUT": "Use async polling pattern with job status checker."
        }
    )

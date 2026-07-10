#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧠 MICA AgenticDriver SOTA - Hierarchical Multi-Agent Orchestration
====================================================================

**Based on State-of-the-Art Research**:
- HALO (Hou et al. 2025): Hierarchical autonomous logic-oriented orchestration
- RAG-KG-IL (Yu & McQuade 2025): Multi-agent hybrid with RAG + KG
- Agentic Lybic (Guo et al. 2025): FSM-based routing with quality gating
- MPI-UOS Phase 6: Meta-cognitive autonomous discovery

**Architecture**:

```
┌────────────────────────────────────────────────────────────┐
│  Layer 7: Meta-Cognitive Agent (Dr. Marcus Weber)          │
│  • Autonomous gap detection                                 │
│  • Proactive research initiation                            │
│  • Cross-driver knowledge synthesis                         │
└─────────────────────┬──────────────────────────────────────┘
                      ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 6: AgenticDriver (This Class)                       │
│  • Hierarchical task decomposition (HALO-inspired)         │
│  • FSM-based routing to specialist drivers                 │
│  • MCP tool orchestration (17 servers)                     │
│  • LangGraph state management + checkpointing              │
└─────────────────────┬──────────────────────────────────────┘
                      ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 5: Specialist Drivers                               │
│  • BioDynamoDriver (9 MD specialists)                      │
│  • AlchemistDriver (6 drug discovery specialists)          │
│  • SMICDriver (1 graph analysis specialist)                │
└─────────────────────┬──────────────────────────────────────┘
                      ↓
┌────────────────────────────────────────────────────────────┐
│  Layer 4: Quality & Peer Review                            │
│  • QualityEvaluator (Nature standards)                     │
│  • MSRPPressureEngine (peer feedback)                      │
│  • Iterative refinement (quality >= 85%)                   │
└────────────────────────────────────────────────────────────┘
```

**Key Features**:
1. **Hierarchical Planning**: HALO-inspired 3-tier reasoning (high/mid/low)
2. **MCP Integration**: 17 external servers (UniProt, PubMed, RDKit, etc.)
3. **Fault Tolerance**: LangGraph checkpointing for long-running workflows
4. **Quality Control**: Iterative peer review with Nature publication standards
5. **Proactive Discovery**: Phase 6 MPI-UOS autonomous research initiation

**Decision Logic** (Finite State Machine):
```
ANALYZE → ROUTE → DECOMPOSE → ASSIGN → EXECUTE → EVALUATE → SYNTHESIZE
```

**Example Usage**:
```python
from mica.drivers.agentic_driver import AgenticDriver

"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import unicodedata
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from typing_extensions import Protocol

# Security & governance (tool-use hardening)
try:
    from ..security import (
        SecurityAnalyzer,
        RiskLevel as _SecurityRiskLevel,
        ConfirmationPolicy,
        CostEstimate,
        GovernanceDecision,
    )
except Exception:  # pragma: no cover
    SecurityAnalyzer = None  # type: ignore
    _SecurityRiskLevel = None  # type: ignore
    ConfirmationPolicy = None  # type: ignore
    CostEstimate = None  # type: ignore
    GovernanceDecision = None  # type: ignore

# LangGraph and MCP are optional but expensive on cold import. Keep the module
# import cheap and load those stacks only when the active runtime path needs
# them.
LANGGRAPH_AVAILABLE = False
StateGraph = None  # type: ignore
END = None  # type: ignore
AsyncSqliteSaver = None  # type: ignore
MemorySaver = None  # type: ignore
_LANGGRAPH_RUNTIME_RESOLVED = False

ClientSession = None  # type: ignore
StdioServerParameters = None  # type: ignore
stdio_client = None  # type: ignore
FastMCPClient = None  # type: ignore
MCP_STDIO_AVAILABLE = False
FASTMCP_AVAILABLE = False
MCP_AVAILABLE = False
_MCP_RUNTIME_RESOLVED = False


def _ensure_langgraph_runtime() -> bool:
    global LANGGRAPH_AVAILABLE, StateGraph, END, AsyncSqliteSaver, _LANGGRAPH_RUNTIME_RESOLVED

    if _LANGGRAPH_RUNTIME_RESOLVED:
        return LANGGRAPH_AVAILABLE

    _LANGGRAPH_RUNTIME_RESOLVED = True
    try:
        from langgraph.graph import StateGraph as _StateGraph, END as _END

        StateGraph = _StateGraph  # type: ignore[assignment]
        END = _END  # type: ignore[assignment]
        LANGGRAPH_AVAILABLE = True
    except ImportError:
        LANGGRAPH_AVAILABLE = False
        logging.warning("⚠️ LangGraph not available - using fallback mode")
        return False

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver as _AsyncSqliteSaver

        AsyncSqliteSaver = _AsyncSqliteSaver  # type: ignore[assignment]
    except Exception:
        try:
            _lg_aiosqlite = importlib.import_module("langgraph.checkpoint.aiosqlite")
            AsyncSqliteSaver = getattr(_lg_aiosqlite, "AsyncSqliteSaver", None)
        except Exception:
            AsyncSqliteSaver = None  # type: ignore[assignment]
    return True


def _ensure_mcp_runtime() -> bool:
    global ClientSession, StdioServerParameters, stdio_client, FastMCPClient
    global MCP_STDIO_AVAILABLE, FASTMCP_AVAILABLE, MCP_AVAILABLE, _MCP_RUNTIME_RESOLVED

    if _MCP_RUNTIME_RESOLVED:
        return MCP_AVAILABLE

    _MCP_RUNTIME_RESOLVED = True

    try:
        from mcp import ClientSession as _ClientSession, StdioServerParameters as _StdioServerParameters
        from mcp.client.stdio import stdio_client as _stdio_client

        ClientSession = _ClientSession  # type: ignore[assignment]
        StdioServerParameters = _StdioServerParameters  # type: ignore[assignment]
        stdio_client = _stdio_client  # type: ignore[assignment]
        MCP_STDIO_AVAILABLE = True
    except ImportError:
        ClientSession = None  # type: ignore[assignment]
        StdioServerParameters = None  # type: ignore[assignment]
        stdio_client = None  # type: ignore[assignment]
        MCP_STDIO_AVAILABLE = False

    try:
        from fastmcp import Client as _FastMCPClient

        FastMCPClient = _FastMCPClient  # type: ignore[assignment]
        FASTMCP_AVAILABLE = True
    except ImportError:
        FastMCPClient = None  # type: ignore[assignment]
        FASTMCP_AVAILABLE = False

    MCP_AVAILABLE = MCP_STDIO_AVAILABLE or FASTMCP_AVAILABLE
    if not MCP_AVAILABLE:
        logging.warning("⚠️ MCP not available - MCP tools disabled")
    return MCP_AVAILABLE

# Specialist drivers
# BioDynamoDriver, AlchemistDriver, SMICDriver are imported lazily inside
# _initialize_specialist_drivers() — they pull in heavy ML/MD frameworks
# (torch, RDKit, MDAnalysis) that add 20-30s on cold import and are never
# needed when running in MCP meta-tool mode (mcp_enabled=False).
if TYPE_CHECKING:
    from .worker_driver import WorkerDriver, WorkerDriverConfig
else:
    WorkerDriver = Any  # type: ignore[misc,assignment]
    WorkerDriverConfig = Any  # type: ignore[misc,assignment]
from .cold_evidence import EpistemicFirewall, FirewallVerdict
from .driver_constants import (
    LOOP_SYSTEM_PROMPT, BIBLIOTECARIO_SYSTEM_PROMPT, SPAWN_TOOLS,
    BIBLIOTECARIO_TOOLS, EXPERT_BASE_TOOLS, EXPERT_POOL,
)
from .cognitive_layer import ACHArbiter, ContinuousCritic, validate_critic_verdict
from .role_context import (
    RoleSpec,
    RoleContext,
    RuptureBudget,
    InvariantCheck,
    EXPERT_INVARIANTS,
    is_embodiment_enabled,
    format_tombstone_warnings,
)
from .evidence_gate import EvidenceGate
from .final_artifact_renderer import FinalArtifactRenderer
from .runtime_skills import resolve_runtime_skills

# DLM-LMP Bridge for entity extraction and KB linking
BRIDGE_AVAILABLE = False
DLMLMPBridge = None  # type: ignore[assignment]
BridgeResult = None  # type: ignore[assignment]
get_bridge = None  # type: ignore[assignment]
_BRIDGE_RUNTIME_RESOLVED = False


def _ensure_bridge_runtime() -> bool:
    global BRIDGE_AVAILABLE, DLMLMPBridge, BridgeResult, get_bridge, _BRIDGE_RUNTIME_RESOLVED

    if _BRIDGE_RUNTIME_RESOLVED:
        return BRIDGE_AVAILABLE

    _BRIDGE_RUNTIME_RESOLVED = True
    try:
        from .dlm_lmp_bridge import DLMLMPBridge as _DLMLMPBridge, BridgeResult as _BridgeResult, get_bridge as _get_bridge

        DLMLMPBridge = _DLMLMPBridge  # type: ignore[assignment]
        BridgeResult = _BridgeResult  # type: ignore[assignment]
        get_bridge = _get_bridge  # type: ignore[assignment]
        BRIDGE_AVAILABLE = True
    except ImportError:
        BRIDGE_AVAILABLE = False
        DLMLMPBridge = None  # type: ignore[assignment]
        BridgeResult = None  # type: ignore[assignment]
        get_bridge = None  # type: ignore[assignment]
        logging.warning("⚠️ DLM-LMP Bridge not available - using fallback regex extraction")
    return BRIDGE_AVAILABLE

# Scientific workflow components
from ..scientific_workflow.quality_evaluator import QualityEvaluator
from ..scientific_workflow.peer_review import MSRPPressureEngine
from ..scientific_workflow.config import NatureStandards, ScientificWorkflowConfig
if TYPE_CHECKING:
    from ..memory.atom import ATOMMemorySystem, ATOMMemoryConfig
    from ..memory.atom.models import TemporalQuintuple
else:
    ATOMMemorySystem = Any  # type: ignore[misc,assignment]
    ATOMMemoryConfig = Any  # type: ignore[misc,assignment]
    TemporalQuintuple = Any  # type: ignore[misc,assignment]

TimescaleAtomPersistentStore = None  # type: ignore[assignment]
choose_timescale_database_url = None  # type: ignore[assignment]
_ATOM_RUNTIME_RESOLVED = False
_ATOM_STORE_RUNTIME_RESOLVED = False
_ATOM_STORE_AVAILABLE = False


def _ensure_atom_runtime() -> bool:
    global ATOMMemorySystem, ATOMMemoryConfig, _ATOM_RUNTIME_RESOLVED

    if _ATOM_RUNTIME_RESOLVED:
        return ATOMMemorySystem is not Any and ATOMMemoryConfig is not Any

    _ATOM_RUNTIME_RESOLVED = True
    try:
        from ..memory.atom import ATOMMemorySystem as _ATOMMemorySystem, ATOMMemoryConfig as _ATOMMemoryConfig

        ATOMMemorySystem = _ATOMMemorySystem  # type: ignore[assignment]
        ATOMMemoryConfig = _ATOMMemoryConfig  # type: ignore[assignment]
        return True
    except Exception:
        ATOMMemorySystem = Any  # type: ignore[misc,assignment]
        ATOMMemoryConfig = Any  # type: ignore[misc,assignment]
        return False


def _ensure_atom_store_runtime() -> bool:
    global TimescaleAtomPersistentStore, choose_timescale_database_url
    global _ATOM_STORE_RUNTIME_RESOLVED, _ATOM_STORE_AVAILABLE

    if _ATOM_STORE_RUNTIME_RESOLVED:
        return _ATOM_STORE_AVAILABLE

    _ATOM_STORE_RUNTIME_RESOLVED = True
    try:
        from ..memory.atom.persistence import TimescaleAtomPersistentStore as _TimescaleAtomPersistentStore
        from ..infrastructure.persistence.pg_async import choose_timescale_database_url as _choose_timescale_database_url

        TimescaleAtomPersistentStore = _TimescaleAtomPersistentStore  # type: ignore[assignment]
        choose_timescale_database_url = _choose_timescale_database_url  # type: ignore[assignment]
        _ATOM_STORE_AVAILABLE = True
    except Exception:  # pragma: no cover - optional infra
        TimescaleAtomPersistentStore = None  # type: ignore[assignment]
        choose_timescale_database_url = None  # type: ignore[assignment]
        _ATOM_STORE_AVAILABLE = False
    return _ATOM_STORE_AVAILABLE


def _build_atom_memory_runtime(config: Any) -> Tuple[Any, Optional[str]]:
    if not _ensure_atom_runtime():
        return None, "ATOM runtime unavailable"

    memory_config = getattr(config, "atom_memory_config", None) or ATOMMemoryConfig()
    atom_store = None
    if _ensure_atom_store_runtime() and getattr(config, "enable_atom_store", True):
        try:
            dsn = choose_timescale_database_url()  # type: ignore[misc]
            if dsn:
                atom_store = TimescaleAtomPersistentStore(dsn=dsn)  # type: ignore[misc]
                logger.info("ATOM store wired (TimescaleAtomPersistentStore) — W1/W2/W4 cascade active")
            else:
                logger.info(
                    "ATOM store not wired: no Timescale DSN resolvable (W1/W2/W4 remain dormant)"
                )
        except Exception as exc:  # pragma: no cover - best-effort infra
            logger.warning("ATOM store construction failed; continuing ephemeral: %s", exc)
            atom_store = None

    atom_memory = ATOMMemorySystem(config=memory_config, store=atom_store)
    logger.info(
        "ATOM memory enabled (chunk_size=%d, batch_size=%d, persistent=%s)",
        memory_config.chunk_size,
        memory_config.batch_size,
        atom_store is not None,
    )
    return atom_memory, None
from ..scientific_workflow.dynamic_dag import DynamicScientificDAG, ScientificDAGNode
from ..scientific_workflow.mudo_envelope import CognitiveAttractorState
from ..scientific_workflow.biorouter import BioRouter
from ..infrastructure.persistence import TimescaleEventStore
from ..infrastructure.event_store import SagaEvent

# ── NewDawn: Active enforcement layer ─────────────────────────────
from ..agentic.cue_evaluator import CueEvaluator, CueResult
from ..agentic.msrp_phase_dispatcher import MSRPPhaseDispatcher, DispatchResult
from ..agentic.decision_ledger import DecisionLedger, LedgerEntry
from ..agentic.depth_presets import DepthPreset, resolve_depth_preset
from ..agentic.session_audit_bundle import SessionAuditBundleBuilder
from ..agentic.protocol_cue_registry import REGISTRY as CUE_REGISTRY
from ..agentic.tool_capability_registry import filter_tools_for_lane
from ..scientific_workflow.paper_score_composite import paper_comparable_score

# Communication protocols
# NOTE: This repo includes `src/bsm/...` as a top-level package. Import it
# directly; avoid relative imports that can break when running as `mica.*`.
try:
    from bsm.communication.legacy_reports import (
        DiscussionSection,
        ExperimentMetadata,
        LabReport,
        MethodsSection,
        PeerFeedback,
        QualityScore,
        ResultsSection,
    )
    from bsm.communication.core import AgentPersona, Attachment
    from ..mcp.tool_context import ToolContextProtocol
    from .proactive.detectors import ProactiveSystem
except ImportError:
    # Minimal mocks to keep the driver importable in constrained environments.
    class LabReport:  # type: ignore
        pass

    class ExperimentMetadata:  # type: ignore
        pass

    class MethodsSection:  # type: ignore
        pass

    class ResultsSection:  # type: ignore
        pass

    class DiscussionSection:  # type: ignore
        pass

    class PeerFeedback:  # type: ignore
        pass

    class QualityScore:  # type: ignore
        pass

    class AgentPersona:  # type: ignore
        SYSTEM = "system"

    class Attachment:  # type: ignore
        pass

    class ToolContextProtocol:  # type: ignore
        def register_tool(self, t):
            return None

        def select_tools_for_task(self, t):
            return []

        def record_execution(self, t, s, l):
            return None

    class ProactiveSystem:  # type: ignore
        def scan(self, s):
            return []


# ============================================================================
# Phase 1 Extraction — re-exports from config / types / utils  (Rule 7)
# ============================================================================
from .config import AgenticDriverConfig, AgenticSession       # noqa: F401 — re-export
from mica.serverless_models.contracts import ModelInvocationRequest
from .types import (                                           # noqa: F401 — re-export
    MICAState,
    TaskType,
    WorkflowState,
    ToolExecutionHook,
)
from .utils import (                                           # noqa: F401 — re-export
    _emit_audit_event,
    _current_user_id_var,
    _current_bucket_var,
    _SECRET_PATTERNS,
    _truncate_text,
    _redact_text,
    _security_risk_to_dict,
    _risk_level_rank,
    _max_risk,
    _redact_obj,
    _DriverTransportShim,
    audit_logger,
)

logger = logging.getLogger(__name__)


def _schema_tool_name(schema: Dict[str, Any]) -> str:
    """Extract a tool name from an OpenAI function schema-like mapping."""
    fn = schema.get("function") if isinstance(schema, dict) else None
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip()
    return str(schema.get("name") if isinstance(schema, dict) else "").strip()


def _is_bio_driver_surface(config: Any) -> bool:
    """Best-effort mode classification for tool-surface filtering.

    Priority:
    1) Explicit driver_mode on config
    2) MICA_DRIVER_MODE env var
    3) mcp_config_path hint
    """
    cfg_mode = str(getattr(config, "driver_mode", "") or "").strip().lower()
    env_mode = str(os.getenv("MICA_DRIVER_MODE", "") or "").strip().lower()
    mode = cfg_mode or env_mode
    if mode in {"bio", "production", "prod"}:
        return True
    if mode in {"development", "dev"}:
        return False

    mcp_config_path = str(getattr(config, "mcp_config_path", "") or "").strip().lower()
    filename = Path(mcp_config_path).name if mcp_config_path else ""
    if "bio" in filename:
        return True
    if "dev" in filename:
        return False
    return False


def _filter_tools_for_driver_surface(tool_schemas: Sequence[Dict[str, Any]], config: Any) -> List[Dict[str, Any]]:
    """Return tool schemas allowed for the active driver surface.

    In bio mode, remove all tools not present in get_bio_tool_names().
    In development mode, keep the full tool surface.
    """
    if not _is_bio_driver_surface(config):
        return list(tool_schemas)

    try:
        from mica.agentic.tool_capability_registry import get_bio_tool_names

        bio_allowed = get_bio_tool_names()
    except Exception as exc:
        logger.warning("Bio tool-surface filtering unavailable (%s); using full surface", exc)
        return list(tool_schemas)

    filtered: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for schema in tool_schemas:
        name = _schema_tool_name(schema)
        if not name:
            continue
        if name in bio_allowed:
            filtered.append(schema)
        else:
            dropped.append(name)

    if dropped:
        logger.info(
            "Bio driver surface filter dropped %d tool(s): %s",
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )
    return filtered


def _sanitize_driver_probe_name(raw: str | None) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or "delegated_probe").strip()).strip("-._")
    return value or "delegated_probe"


def _coerce_checkpoint_flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw in (None, ""):
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


async def _run_subprocess_capture(command: Sequence[str], *, cwd: Path) -> Dict[str, Any]:
    proc = await asyncio.to_thread(
        subprocess.run,
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "command": list(command),
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _driver_file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_driver_target_relative_path(workspace_root: Path, raw_path: str | None) -> str:
    candidate = str(raw_path or "").strip().replace("\\", "/")
    if not candidate:
        return ""

    resolved = (workspace_root / candidate).resolve()
    if not resolved.is_relative_to(workspace_root):
        raise ValueError(f"target_relative_path escapes workspace_root: {candidate}")
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"target_relative_path does not exist as a file: {candidate}")
    return resolved.relative_to(workspace_root).as_posix()


def _load_driver_checkpoint_lineage_receipt(
    workspace_root: Path,
    upstream_checkpoint_result_path: str | None,
) -> tuple[Path | None, Dict[str, Any] | None]:
    raw_path = str(upstream_checkpoint_result_path or "").strip()
    if not raw_path:
        return None, None

    resolved_path = Path(raw_path).expanduser().resolve()
    if not resolved_path.exists() or not resolved_path.is_file():
        raise ValueError(f"upstream checkpoint result does not exist: {resolved_path}")

    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    apply_checkpoint = payload.get("apply_checkpoint") or {}
    approval_state = str(apply_checkpoint.get("approval_state") or "").strip()
    if approval_state != "applied_same_diff":
        raise ValueError(
            "upstream checkpoint must reach approval_state=applied_same_diff before staging lineage can continue"
        )

    artifact_root = Path(str(apply_checkpoint.get("artifact_root") or "")).resolve()
    candidate_file = Path(str(apply_checkpoint.get("candidate_file") or "")).resolve()
    workspace_target_file = Path(
        str(
            apply_checkpoint.get("workspace_target_file")
            or apply_checkpoint.get("workspace_probe_file")
            or ""
        )
    ).resolve()
    relative_path = str(apply_checkpoint.get("target_relative_path") or "").strip().replace("\\", "/")

    if not candidate_file.exists() or not candidate_file.is_file():
        raise ValueError(f"upstream candidate file missing: {candidate_file}")
    if not workspace_target_file.exists() or not workspace_target_file.is_file():
        raise ValueError(f"upstream applied workspace file missing: {workspace_target_file}")
    if not artifact_root.exists() or not artifact_root.is_dir():
        raise ValueError(f"upstream artifact root missing: {artifact_root}")

    if not relative_path:
        if candidate_file.is_relative_to(artifact_root):
            relative_path = candidate_file.relative_to(artifact_root).as_posix()
        elif workspace_target_file.is_relative_to(workspace_root):
            relative_path = workspace_target_file.relative_to(workspace_root).as_posix()
        else:
            raise ValueError("could not derive a target-relative path for staging lineage continuity")

    return resolved_path, {
        "checkpoint_result_path": str(resolved_path),
        "approval_state": approval_state,
        "artifact_root": str(artifact_root),
        "candidate_file": str(candidate_file),
        "workspace_target_file": str(workspace_target_file),
        "target_relative_path": relative_path,
        "apply_receipt_path": str(apply_checkpoint.get("apply_receipt_path") or ""),
        "closure_state": str(payload.get("closure_state") or ""),
    }


async def _run_driver_owned_delegated_checkpoint(
    *,
    workspace_root: Path,
    objective: str,
    probe_name: str,
    initial_value: int,
    updated_value: int,
    apply_same_diff: Any = False,
    target_relative_path: str | None = None,
    target_callable_name: str | None = None,
) -> Dict[str, Any]:
    from mica.drivers.ghp_session_executor import execute_delegated_task
    from mica.sdk.contracts import GhpDelegatedTask

    resolved_workspace = workspace_root.resolve()
    apply_requested = _coerce_checkpoint_flag(apply_same_diff)
    if not resolved_workspace.exists() or not resolved_workspace.is_dir():
        return {
            "status": "error",
            "failure_reason": "INVALID_WORKSPACE_ROOT",
            "note": f"workspace_root does not exist or is not a directory: {resolved_workspace}",
        }

    run_id = f"{_sanitize_driver_probe_name(probe_name)}-{uuid.uuid4().hex[:8]}"
    run_dir = resolved_workspace / "tmp" / "driver_owned_checkpoints" / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        target_rel = _resolve_driver_target_relative_path(resolved_workspace, target_relative_path)
    except ValueError as exc:
        return {
            "status": "error",
            "failure_reason": "INVALID_TARGET_RELATIVE_PATH",
            "note": str(exc),
        }

    target_callable = str(target_callable_name or "delegated_value").strip() or "delegated_value"
    if target_rel:
        probe_rel = target_rel
        probe_path = resolved_workspace / probe_rel
    else:
        probe_path = run_dir / "probe_module.py"
        probe_rel = probe_path.relative_to(resolved_workspace).as_posix()
        probe_source = textwrap.dedent(
            f"""\
            def {target_callable}() -> int:
                return {int(initial_value)}
            """
        )
        probe_path.write_text(probe_source, encoding="utf-8")

    runner_script = textwrap.dedent(
        f"""\
        from pathlib import Path

        target = Path({probe_rel!r})
        text = target.read_text(encoding="utf-8")
        old = "return {int(initial_value)}"
        new = "return {int(updated_value)}"
        if old not in text:
            raise SystemExit(f"expected marker '{{old}}' missing in {{target}}")
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"updated {{target}} from {{old}} to {{new}}")
        """
    )

    delegated = await execute_delegated_task(
        GhpDelegatedTask(
            task_id=run_id,
            objective=objective,
            file_allowlist=[probe_rel],
            forbidden_zones=["src/**", "tests/**", ".github/**"] if not target_rel else [".github/**"],
            must_produce=[
                f"Modify {probe_rel} so {target_callable} returns {int(updated_value)}",
                "Preserve a reviewable diff and touched-file summary",
                "Leave the candidate ready for focused validation",
            ],
        ),
        workspace_root=resolved_workspace,
        runner_script=runner_script,
        artifact_dir=run_dir / "candidate",
    )

    diff_path = run_dir / "candidate.diff"
    diff_path.write_text(delegated.diff or "", encoding="utf-8")

    validation: Dict[str, Any] = {}
    candidate_root = Path(delegated.artifact_root) if delegated.artifact_root else None
    candidate_file = candidate_root / probe_rel if candidate_root is not None else None

    if delegated.status == "completed" and candidate_file is not None and candidate_file.exists():
        validation["py_compile"] = await _run_subprocess_capture(
            [sys.executable, "-m", "py_compile", str(candidate_file)],
            cwd=candidate_root,
        )
        validation["python_assert"] = await _run_subprocess_capture(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""\
                    import importlib.util
                    from pathlib import Path

                    path = Path({str(candidate_file)!r})
                    spec = importlib.util.spec_from_file_location("delegated_probe", path)
                    module = importlib.util.module_from_spec(spec)
                    assert spec and spec.loader is not None
                    spec.loader.exec_module(module)
                    value = getattr(module, {target_callable!r})()
                    assert value == {int(updated_value)}, value
                    print(value)
                    """
                ),
            ],
            cwd=candidate_root,
        )
    else:
        validation["py_compile"] = {
            "command": [sys.executable, "-m", "py_compile"],
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "candidate artifact missing; compile was not run",
        }
        validation["python_assert"] = {
            "command": [sys.executable, "-c", "delegated candidate assertion"],
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "candidate artifact missing; assertion was not run",
        }

    all_valid = delegated.status == "completed" and all(item.get("ok") for item in validation.values())
    validation_path = run_dir / "validation.json"
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    apply_receipt: Dict[str, Any] = {
        "workspace_probe_file": str(probe_path),
        "workspace_target_file": str(probe_path),
        "artifact_root": delegated.artifact_root,
        "candidate_file": str(candidate_file) if candidate_file is not None else None,
        "diff_path": str(diff_path),
        "validation_path": str(validation_path),
        "target_relative_path": probe_rel,
        "target_callable_name": target_callable,
        "requested": apply_requested,
        "approval_state": "candidate_only",
        "status": "not_requested",
    }

    if apply_requested:
        apply_receipt["status"] = "apply_blocked_by_candidate_validation"
        apply_receipt["approval_state"] = "apply_blocked_by_candidate_validation"

        if all_valid and candidate_file is not None and candidate_file.exists() and delegated.diff:
            rollback_dir = run_dir / "rollback"
            rollback_dir.mkdir(parents=True, exist_ok=True)
            rollback_file = rollback_dir / "probe_module.before_apply.py"
            shutil.copy2(probe_path, rollback_file)

            apply_receipt["rollback_file"] = str(rollback_file)
            apply_receipt["rollback_ready"] = True
            apply_receipt["apply_check"] = await _run_subprocess_capture(
                ["git", "apply", "--check", "--unsafe-paths", str(diff_path)],
                cwd=resolved_workspace,
            )

            if apply_receipt["apply_check"].get("ok"):
                apply_receipt["apply_command"] = await _run_subprocess_capture(
                    ["git", "apply", "--unsafe-paths", str(diff_path)],
                    cwd=resolved_workspace,
                )

                if apply_receipt["apply_command"].get("ok"):
                    apply_validation = {
                        "py_compile": await _run_subprocess_capture(
                            [sys.executable, "-m", "py_compile", str(probe_path)],
                            cwd=resolved_workspace,
                        ),
                        "python_assert": await _run_subprocess_capture(
                            [
                                sys.executable,
                                "-c",
                                textwrap.dedent(
                                    f"""\
                                    import importlib.util
                                    from pathlib import Path

                                    path = Path({str(probe_path)!r})
                                    spec = importlib.util.spec_from_file_location("delegated_probe", path)
                                    module = importlib.util.module_from_spec(spec)
                                    assert spec and spec.loader is not None
                                    spec.loader.exec_module(module)
                                    value = getattr(module, {target_callable!r})()
                                    assert value == {int(updated_value)}, value
                                    print(value)
                                    """
                                ),
                            ],
                            cwd=resolved_workspace,
                        ),
                    }
                    apply_receipt["apply_validation"] = apply_validation

                    if all(item.get("ok") for item in apply_validation.values()):
                        apply_receipt["status"] = "applied"
                        apply_receipt["approval_state"] = "applied_same_diff"
                    else:
                        shutil.copy2(rollback_file, probe_path)
                        apply_receipt["status"] = "rolled_back_after_failed_apply_validation"
                        apply_receipt["approval_state"] = "rolled_back_after_failed_apply_validation"
                        apply_receipt["rollback_executed"] = True
                else:
                    shutil.copy2(rollback_file, probe_path)
                    apply_receipt["status"] = "rolled_back_after_apply_failure"
                    apply_receipt["approval_state"] = "rolled_back_after_apply_failure"
                    apply_receipt["rollback_executed"] = True
            else:
                apply_receipt["status"] = "apply_check_failed"
                apply_receipt["approval_state"] = "apply_check_failed"

    apply_receipt_path = run_dir / "apply_receipt.json"
    apply_receipt_path.write_text(
        json.dumps(apply_receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    actual_tools = ["execute_delegated_task", "py_compile", "python_assert"]
    if apply_requested:
        actual_tools.append("git_apply")
        if apply_receipt.get("status") == "applied":
            actual_tools.extend(["workspace_py_compile", "workspace_python_assert"])

    if apply_receipt.get("status") == "applied":
        post_critique_status = "completed"
        post_critique_note = (
            "Validated candidate diff was applied back to the workspace target via the preserved unified diff, "
            "and a rollback backup was captured before mutation."
        )
        closure_state = "same_diff_applied"
        overall_ok = True
    elif all_valid:
        post_critique_status = "completed"
        post_critique_note = (
            "Checkpoint closes as candidate-only; the original workspace target remains unchanged pending apply approval."
            if not apply_requested
            else "Validated candidate exists, but governed same-diff apply did not complete."
        )
        closure_state = "validated_candidate_pending_apply" if not apply_requested else "same_diff_apply_failed"
        overall_ok = not apply_requested
    else:
        post_critique_status = "needs_attention"
        post_critique_note = "Candidate validation failed before any apply decision could be accepted."
        closure_state = "candidate_failed_validation" if not apply_requested else "apply_blocked_by_candidate_validation"
        overall_ok = False

    result = {
        "status": "ok" if overall_ok else "failed",
        "route_card_id": "level5.slice_a.driver_owned_checkpoint",
        "lane_id": "driver_owned_delegated_checkpoint",
        "goal": objective,
        "fast_path_reason": "Use a bounded allowlisted target to prove delegated modify+test ownership and, when explicitly requested, apply that same preserved diff back to the workspace under rollback control.",
        "deep_path_reason": "Promote the same checkpoint shape from delegated candidate to governed apply and then into staging deployment lanes.",
        "why_not_deep_yet": "This slice stays on one bounded target so governed apply can be proven with rollback evidence before widening to broader repo mutations and staging deployment.",
        "considered_tools": ["execute_delegated_task", "py_compile", "python_assert", "git_apply"],
        "rejected_tools": ["manual_apply_patch", "external_shell_only_proof"],
        "planned_tools": ["execute_delegated_task", "py_compile", "python_assert"] + (["git_apply"] if apply_requested else []),
        "actual_tools": actual_tools,
        "checkpoint_ledger": [
            {
                "checkpoint": "post_plan",
                "status": "completed",
                "note": "Driver authored a bounded delegated task over one allowlisted target file.",
            },
            {
                "checkpoint": "post_acquisition",
                "status": delegated.status,
                "note": "Delegated executor returned diff, touched-file summary, and preserved candidate artifacts.",
            },
            {
                "checkpoint": "pre_synthesis",
                "status": "completed" if any(validation) else "missing",
                "note": "Focused validation ran on the preserved candidate before any apply decision.",
            },
            {
                "checkpoint": "post_critique",
                "status": post_critique_status,
                "note": post_critique_note,
            },
        ],
        "delegated_result": delegated.model_dump(),
        "validation": validation,
        "apply_checkpoint": {
            **apply_receipt,
            "apply_receipt_path": str(apply_receipt_path),
        },
        "closure_state": closure_state,
        "run_id": run_id,
        "checkpoint_result_path": str(run_dir / "checkpoint_result.json"),
    }
    # P1-7: Checkpoint size bound — refuse to write unbounded checkpoints.
    _max_bytes = int(os.environ.get("MICA_MAX_CHECKPOINT_BYTES", 2 * 1024 * 1024))
    _serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if len(_serialized.encode("utf-8")) > _max_bytes:
        logger.warning(
            "Checkpoint exceeds %d bytes limit (%d bytes), writing summary-only version",
            _max_bytes, len(_serialized.encode("utf-8")),
        )
        result["checkpoint_summary_only"] = True
        result["checkpoint_original_size"] = len(_serialized.encode("utf-8"))
        result["delegated_result"] = {"truncated": True, "note": "checkpoint size limit exceeded"}
        result["validation"] = {"truncated": True}
        _serialized = json.dumps(result, ensure_ascii=False, indent=2)
    Path(result["checkpoint_result_path"]).write_text(
        _serialized,
        encoding="utf-8",
    )
    return result


def _default_driver_staging_candidate_patterns(*, include_tests: bool) -> list[str]:
    patterns = [
        ".buildversion",
        ".env",
        "requirements_api.txt",
        "railway.toml",
        "Dockerfile.api",
        "src/**/*",
    ]
    if include_tests:
        patterns.append("tests/**/*")
    return patterns


def _prune_driver_staging_candidate_root(candidate_root: Path) -> list[str]:
    prune_patterns = [
        "src/**/__pycache__",
        "tests/**/__pycache__",
        "src/graphify-out",
        "src/bsm/DCTdomain_human_proteome",
        "src/mica/mcp_servers/nodejs_servers/**/node_modules",
        "src/mica/memory/dlm/api_cache",
        "src/mica/memory/dlm/generated_deltas",
        "src/mica/memory/atom/itext2kg_original/datasets",
        "src/mica/memory/dlm/*.bak.*",
    ]
    removed: set[str] = set()
    for pattern in prune_patterns:
        for target in candidate_root.glob(pattern):
            if not target.exists():
                continue
            rel_path = target.relative_to(candidate_root).as_posix()
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            removed.add(rel_path)
    return sorted(removed)


def _resolve_driver_candidate_commit_sha(
    workspace_root: Path,
    provided_commit_sha: str | None,
    fallback: str,
) -> str:
    commit_sha = (provided_commit_sha or "").strip()
    if commit_sha:
        return commit_sha[:64]

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        candidate = proc.stdout.strip()
        if proc.returncode == 0 and candidate:
            return candidate[:64]
    except Exception:
        pass

    return fallback[:64]


def _driver_http_smoke_probe(url: str, timeout_seconds: int = 30) -> bool:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except Exception:
        return False


async def _run_driver_owned_staging_deploy_checkpoint(
    *,
    workspace_root: Path,
    objective: str,
    candidate_name: str,
    project_id: str,
    environment_id: str,
    staging_service: str,
    public_base_url: str,
    readiness_url: str | None,
    py_compile_targets: Sequence[str] | None,
    pytest_args: Sequence[str] | None,
    deployment_patterns: Sequence[str] | None,
    commit_sha: str | None,
    max_wall_seconds: int,
    dry_run: bool,
    cli_bin: str | None,
    api_token: str | None,
    upstream_checkpoint_result_path: str | None = None,
) -> Dict[str, Any]:
    from mica.drivers.ghp_session_executor import execute_delegated_task
    from mica.drivers.railway_deploy_client import RailwayDeployClient
    from mica.sdk.contracts import GhpDelegatedTask, RailwayDeployCandidate

    resolved_workspace = workspace_root.resolve()
    if not resolved_workspace.exists() or not resolved_workspace.is_dir():
        return {
            "status": "error",
            "failure_reason": "INVALID_WORKSPACE_ROOT",
            "note": f"workspace_root does not exist or is not a directory: {resolved_workspace}",
        }
    if not project_id.strip() or not environment_id.strip():
        return {
            "status": "error",
            "failure_reason": "MISSING_RAILWAY_TARGET",
            "note": "project_id and environment_id are required for the driver-owned staging deploy checkpoint.",
        }

    try:
        upstream_checkpoint_path, upstream_lineage = _load_driver_checkpoint_lineage_receipt(
            resolved_workspace,
            upstream_checkpoint_result_path,
        )
    except ValueError as exc:
        return {
            "status": "error",
            "failure_reason": "INVALID_UPSTREAM_CHECKPOINT_LINEAGE",
            "note": str(exc),
        }

    include_tests = bool(pytest_args)
    allowlist = list(deployment_patterns or _default_driver_staging_candidate_patterns(include_tests=include_tests))
    run_id = f"{_sanitize_driver_probe_name(candidate_name)}-{uuid.uuid4().hex[:8]}"
    run_dir = Path(tempfile.gettempdir()) / "mica_driver_owned_checkpoints" / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    delegated = await execute_delegated_task(
        GhpDelegatedTask(
            task_id=run_id,
            objective=objective,
            file_allowlist=allowlist,
            forbidden_zones=[],
            must_produce=[
                "Export a deployable staging candidate workspace for validation and deployment.",
                "Preserve the exact candidate root that will be deployed if validation passes.",
                "Keep candidate continuity explicit for post-run audit.",
            ],
            max_wall_seconds=max(60, min(int(max_wall_seconds), 1800)),
        ),
        workspace_root=resolved_workspace,
        runner_script=None,
        artifact_dir=run_dir / "candidate",
    )

    candidate_root = Path(delegated.artifact_root) if delegated.artifact_root else None
    validation: Dict[str, Any] = {}
    py_compile_targets = list(py_compile_targets or [])
    pytest_args = list(pytest_args or [])
    pruned_candidate_paths: list[str] = []

    if delegated.status == "completed" and candidate_root is not None and candidate_root.exists():
        pruned_candidate_paths = _prune_driver_staging_candidate_root(candidate_root)
        for rel_path in py_compile_targets:
            candidate_file = candidate_root / rel_path
            validation[f"py_compile:{rel_path}"] = await _run_subprocess_capture(
                [sys.executable, "-m", "py_compile", str(candidate_file)],
                cwd=candidate_root,
            )
        if pytest_args:
            validation["pytest"] = await _run_subprocess_capture(
                [sys.executable, "-m", "pytest", *pytest_args],
                cwd=candidate_root,
            )
    else:
        validation["candidate_export"] = {
            "command": ["execute_delegated_task", "artifact_export"],
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "candidate artifact missing; validation was not run",
        }

    validations_ok = all(item.get("ok") for item in validation.values()) if validation else True

    lineage_receipt: Dict[str, Any] = {
        "requested": upstream_lineage is not None,
        "status": "not_requested",
        "continuity_ok": False,
    }
    lineage_ok = upstream_lineage is None

    if upstream_lineage is not None:
        lineage_receipt.update(
            {
                "status": "candidate_export_missing",
                "upstream_checkpoint_result_path": str(upstream_checkpoint_path),
                "upstream_candidate_file": upstream_lineage["candidate_file"],
                "upstream_workspace_target_file": upstream_lineage["workspace_target_file"],
                "target_relative_path": upstream_lineage["target_relative_path"],
            }
        )
        if candidate_root is not None and candidate_root.exists():
            staged_candidate_file = candidate_root / upstream_lineage["target_relative_path"]
            candidate_path = Path(upstream_lineage["candidate_file"])
            workspace_target_path = Path(upstream_lineage["workspace_target_file"])
            candidate_hash = _driver_file_sha256(candidate_path)
            workspace_hash = _driver_file_sha256(workspace_target_path)
            staged_hash = _driver_file_sha256(staged_candidate_file) if staged_candidate_file.exists() else None
            continuity_matrix = {
                "candidate_to_applied": candidate_hash == workspace_hash,
                "applied_to_staging": staged_hash == workspace_hash if staged_hash else False,
                "candidate_to_staging": staged_hash == candidate_hash if staged_hash else False,
            }
            lineage_ok = all(continuity_matrix.values())
            lineage_receipt.update(
                {
                    "status": "continuous" if lineage_ok else "lineage_broken",
                    "continuity_ok": lineage_ok,
                    "staging_candidate_file": str(staged_candidate_file),
                    "hashes": {
                        "candidate": candidate_hash,
                        "applied_workspace": workspace_hash,
                        "staging_candidate": staged_hash,
                    },
                    "continuity_matrix": continuity_matrix,
                }
            )

    lineage_receipt_path = run_dir / "lineage_receipt.json"
    lineage_receipt_path.write_text(
        json.dumps(lineage_receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    deploy_report = None
    if delegated.status == "completed" and candidate_root is not None and candidate_root.exists() and validations_ok and lineage_ok:
        public_base = public_base_url.rstrip("/")
        readiness_target = (readiness_url or "").strip() or f"{public_base}/api/v1/readiness"
        deploy_candidate = RailwayDeployCandidate(
            commit_sha=_resolve_driver_candidate_commit_sha(resolved_workspace, commit_sha, run_id),
            staging_service=staging_service,
            smoke_matrix=[f"{public_base}/health", f"{public_base}/api/v1/health"],
            max_wall_seconds=max(60, min(int(max_wall_seconds), 3600)),
            rollback_on_fail=True,
            readiness_url=readiness_target,
            readiness_required=True,
        )
        client = RailwayDeployClient(
            project_id=project_id,
            environment_id=environment_id,
            api_token=api_token,
            dry_run=dry_run,
            cli_bin=cli_bin,
            workdir=str(candidate_root),
        )
        deploy_report = await client.deploy(
            deploy_candidate,
            smoke_runner=lambda probe: _driver_http_smoke_probe(probe, timeout_seconds=30),
        )

    if delegated.status != "completed":
        closure_state = "candidate_export_failed"
    elif not validations_ok:
        closure_state = "candidate_failed_validation"
    elif not lineage_ok:
        closure_state = "validated_candidate_lineage_broken"
    elif deploy_report is None:
        closure_state = "validated_candidate_pending_deploy"
    elif deploy_report.status == "deployed" and deploy_report.security_controls.get("deploy_workdir_configured"):
        closure_state = (
            "validated_candidate_deployed_from_lineage_root"
            if upstream_lineage is not None
            else "validated_candidate_deployed_from_candidate_root"
        )
    else:
        closure_state = f"validated_candidate_deploy_{deploy_report.status}"

    result = {
        "status": "ok" if deploy_report and deploy_report.status == "deployed" and lineage_ok else "failed",
        "route_card_id": "level5.slice_b.driver_owned_candidate_deploy",
        "lane_id": "driver_owned_staging_deploy_checkpoint",
        "goal": objective,
        "fast_path_reason": "Snapshot the exact deploy slice into a preserved candidate root and, when requested, prove that root still carries the same delegated/apply lineage before deploy.",
        "deep_path_reason": "Use one checkpoint family to preserve candidate -> apply -> staging continuity over a bounded real repo target before widening to broader delegated mutations.",
        "why_not_deep_yet": "This slice still constrains continuity proof to one bounded lineage path before widening to larger real-file mutation families.",
        "considered_tools": ["execute_delegated_task", "py_compile", "pytest", "RailwayDeployClient", "lineage_receipt"],
        "rejected_tools": ["workspace_live_deploy_without_candidate_root", "railway_redeploy_without_candidate_continuity"],
        "planned_tools": ["execute_delegated_task", "py_compile", "pytest", "RailwayDeployClient"] + (["lineage_receipt"] if upstream_lineage is not None else []),
        "actual_tools": ["execute_delegated_task", *( ["py_compile"] if py_compile_targets else []), *( ["pytest"] if pytest_args else []), *( ["lineage_receipt"] if upstream_lineage is not None else []), *( ["RailwayDeployClient"] if deploy_report is not None else [])],
        "checkpoint_ledger": [
            {
                "checkpoint": "post_plan",
                "status": "completed",
                "note": "Driver selected a bounded deployment slice and exported it into a preserved candidate root.",
            },
            {
                "checkpoint": "post_acquisition",
                "status": delegated.status,
                "note": "Delegated executor exported the candidate root used for validation and deploy continuity.",
            },
            {
                "checkpoint": "pre_synthesis",
                "status": "completed" if validations_ok else "needs_attention",
                "note": "Focused validation ran against the preserved candidate root before deploy.",
            },
            {
                "checkpoint": "post_critique",
                "status": "completed" if deploy_report is not None and lineage_ok else ("needs_attention" if upstream_lineage is not None else "pending"),
                "note": (
                    "Deployment outcome records whether staging used the preserved candidate root and whether upstream delegated/apply lineage survived into that root."
                    if upstream_lineage is not None
                    else "Deployment outcome records whether staging used the preserved candidate root instead of the live workspace."
                ),
            },
        ],
        "delegated_result": delegated.model_dump(),
        "candidate_root": str(candidate_root) if candidate_root is not None else None,
        "candidate_pruned_paths": pruned_candidate_paths,
        "validation": validation,
        "lineage_receipt": lineage_receipt,
        "lineage_receipt_path": str(lineage_receipt_path),
        "deploy_report": deploy_report.model_dump() if deploy_report is not None else None,
        "closure_state": closure_state,
        "run_id": run_id,
        "upstream_checkpoint_result_path": str(upstream_checkpoint_path) if upstream_checkpoint_path is not None else None,
        "checkpoint_result_path": str(run_dir / "staging_deploy_checkpoint_result.json"),
    }
    # P1-7: Checkpoint size bound (same as delegated checkpoint).
    _max_bytes = int(os.environ.get("MICA_MAX_CHECKPOINT_BYTES", 2 * 1024 * 1024))
    _serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if len(_serialized.encode("utf-8")) > _max_bytes:
        logger.warning(
            "Staging deploy checkpoint exceeds %d bytes limit (%d bytes), writing summary-only",
            _max_bytes, len(_serialized.encode("utf-8")),
        )
        result["checkpoint_summary_only"] = True
        result["checkpoint_original_size"] = len(_serialized.encode("utf-8"))
        result["upstream_lineage"] = {"truncated": True}
        _serialized = json.dumps(result, ensure_ascii=False, indent=2)
    Path(result["checkpoint_result_path"]).write_text(
        _serialized,
        encoding="utf-8",
    )
    return result


# ============================================================================
# Phase 1 note: MICAState, TaskType, WorkflowState, AgenticDriverConfig,
# AgenticSession, ToolExecutionHook are now defined in:
#   .types   → MICAState, TaskType, WorkflowState, ToolExecutionHook
#   .config  → AgenticDriverConfig, AgenticSession
# They are re-exported at module level via the import block above.
# ============================================================================

# ── Phase 2: Evidence / Persistence / ATOM extraction ──────────────
from .evidence import (                                        # noqa: F401 — re-export
    official_link_from_identifiers,
    build_source_record_from_paper,
    format_bibliotecario_citation_entry,
    extract_sources_from_text,
    derive_claims_and_sources,
    extract_native_evidence_from_side_data,
    normalize_final_result_contract,
    build_minimal_lab_report,
)
from .atom_integration import (                                # noqa: F401 — re-export
    record_atom_entry,
    record_session_event_in_atom,
    record_lab_report_to_atom,
    record_quality_scores_to_atom,
    query_atom_for_gap_signals,
    maybe_run_proactive_gap_scan,
)
from .persistence import (                                     # noqa: F401 — re-export
    saga_log_path,
    append_saga_event,
    append_saga_event_timescale,
    get_timescale_store,
    best_effort_saga_mcp_metrics,
    run_manifest_dir,
    best_effort_git_info,
    best_effort_versions,
    write_run_manifest,
    write_report_card,
    snapshot_dir,
    sha256_file,
    save_session_snapshot as _save_session_snapshot_fn,
    restore_session_snapshot as _restore_session_snapshot_fn,
    conversation_log_path,
    safe_result_for_log,
    stringify_message_content,
    append_conversation_log,
    persist_communication_store as _persist_communication_store_fn,
    build_runtime_telemetry_emitter as _build_runtime_telemetry_emitter_fn,
    emit_runtime_status as _emit_runtime_status_fn,
    emit_runtime_error as _emit_runtime_error_fn,
    build_runtime_error_artifact_writer as _build_runtime_error_artifact_writer_fn,
    is_retryable_runtime_exception as _is_retryable_runtime_exception_fn,
    persist_runtime_error_artifact as _persist_runtime_error_artifact_fn,
    DriverArtifactSync,
)

# ── Phase 3: Identifiers extraction ───────────────────────────────
from .identifiers import (                                     # noqa: F401 — re-export
    PDB_FALSE_POSITIVES,
    extract_identifiers,
    merge_identifiers,
    best_protein_hint,
    extract_candidate_gene_symbols,
    extract_text_chunks_from_mcp,
    extract_uniprot_accessions_from_mcp_result,
    extract_pdb_ids_from_search_result,
)

# ── Phase 3: MCP format & tool selection ──────────────────────────
from .mcp import (                                             # noqa: F401 — re-export
    format_tools_for_claude,
    format_tools_for_openai,
    normalize_mcp_call_tool_result as _normalize_mcp_call_tool_result_fn,
    get_tool_schema as _get_tool_schema_fn,
    pick_tool_for_server as _pick_tool_for_server_fn,
    build_tool_args as _build_tool_args_fn,
    build_tool_args_fallback as _build_tool_args_fallback_fn,
)

# ── Phase 3: Structure analysis ──────────────────────────────────
from .structure import (                                       # noqa: F401 — re-export
    should_use_direct_structure_path as _should_use_direct_structure_path_fn,
    rank_pdb_structures as _rank_pdb_structures_fn,
    persist_structure_artifacts as _persist_structure_artifacts_fn,
    make_attachment as _make_attachment_fn,
)

# ── Phase 3: LangGraph nodes & routers ───────────────────────────
from .langgraph import (                                       # noqa: F401 — re-export
    node_route as _node_route_fn,
    node_decompose as _node_decompose_fn,
    node_analyze as _node_analyze_fn,
    node_synthesize as _node_synthesize_fn,
    node_proactive_monitor as _node_proactive_monitor_fn,
    router_quality_gate as _router_quality_gate_fn,
    router_proactive_monitor as _router_proactive_monitor_fn,
    # Phase 4a: additional node extractions
    node_initialize as _node_initialize_fn,
    node_thermostat as _node_thermostat_fn,
    node_assign as _node_assign_fn,
    # Phase 4b: heavy node extractions
    node_execute as _node_execute_fn,
    node_quality_gate as _node_quality_gate_fn,
)

# ── Phase 4a: MCP invocation pipeline helpers ────────────────────
from .mcp.invocation import (                                  # noqa: F401 — re-export
    normalize_call_args as _normalize_call_args_fn,
    inject_attribution as _inject_attribution_fn,
    build_blocked_payload as _build_blocked_payload_fn,
    build_confirmation_payload as _build_confirmation_payload_fn,
    build_success_payload as _build_success_payload_fn,
    build_error_payload as _build_error_payload_fn,
    build_saga_begin_event as _build_saga_begin_event_fn,
    build_saga_abort_event as _build_saga_abort_event_fn,
    build_saga_commit_event as _build_saga_commit_event_fn,
    build_saga_retry_event as _build_saga_retry_event_fn,
    run_security_gate as _run_security_gate_fn,
    run_governance_gate as _run_governance_gate_fn,
    check_circuit_breaker as _check_circuit_breaker_fn,
    circuit_breaker_on_success as _circuit_breaker_on_success_fn,
    circuit_breaker_on_failure as _circuit_breaker_on_failure_fn,
    RetryConfig as _RetryConfig,
    build_retry_config as _build_retry_config_fn,
    compute_backoff_sleep as _compute_backoff_sleep_fn,
)


# ============================================================================
# AGENTIC DRIVER CORE
# ============================================================================

class AgenticDriver:
    """
    🧠 Hierarchical Multi-Agent Orchestrator with MCP Integration
    
    Combines:
    - HALO-inspired hierarchical reasoning (high/mid/low-level planning)
    - FSM-based routing to specialist drivers
    - MCP tool orchestration (17 external servers)
    - LangGraph checkpointing for fault tolerance
    - Quality-driven iterative refinement
    - Meta-cognitive autonomous discovery (Phase 6)
    
    Args:
        config (AgenticDriverConfig): Driver configuration
        
    Attributes:
        specialist_drivers (Dict[str, WorkerDriver]): Registry of specialist drivers
        mcp_tools (List[Dict]): Available MCP tools from 17 servers
        quality_evaluator (QualityEvaluator): Nature standards evaluator
        pressure_engine (MSRPPressureEngine): Peer review feedback generator
        checkpointer (AsyncSqliteSaver | MemorySaver): LangGraph state persistence
        
    Example:
        >>> driver = AgenticDriver(config=AgenticDriverConfig(
        ...     checkpoint_dir="./.checkpoints",
        ...     quality_threshold=0.85
        ... ))
        >>> await driver.initialize_async()
        >>> result = await driver.process_agentic_prompt(
        ...     "Simulate p53 with RAMD sampling"
        ... )
    """

    async def _invoke_feed_tool(
        self,
        name: str,
        kwargs: Optional[Dict[str, Any]] = None,
        *,
        agent_id_fallback: str = "driver",
    ) -> Any:
        from mica.agentic.tools import agent_feed as _agent_feed

        feed_kwargs = dict(kwargs or {})
        if name in {"open_session_signature", "update_session_progress", "publish_cue"}:
            feed_kwargs.setdefault(
                "agent_id",
                getattr(self, "user_id", None) or agent_id_fallback,
            )
        handler = getattr(_agent_feed, name)
        return await handler(**feed_kwargs)
    
    def __init__(
        self,
        config: Optional[AgenticDriverConfig] = None,
        specialist_drivers: Optional[Dict[str, WorkerDriver]] = None
    ):
        """Initialize AgenticDriver with configuration and specialists."""
        
        self.config = config or AgenticDriverConfig.from_driver_config()
        self.final_artifact_renderer = FinalArtifactRenderer()
        
        # Specialist driver registry
        self.specialist_drivers: Dict[str, WorkerDriver] = specialist_drivers or {}

        # Transport layer (for DAGExecutor/LangGraph wrapper compatibility)
        self.registry = None
        self.transport = None
        
        # MCP integration
        self.mcp_tools: List[Dict[str, Any]] = []
        self.mcp_config: Dict[str, Any] = {}
        self.mcp_sessions: Dict[str, Any] = {}  # server_name → ClientSession
        self._mcp_exit_stack: Optional[AsyncExitStack] = None
        self._toolkg_registry: Optional[Any] = None
        self._toolkg_eval_summary: Dict[str, Any] = {}
        self._nru_gateway: Optional[Any] = None
        self.serverless_model_gateway: Optional[Any] = None
        
        # Quality control
        nature_standards = NatureStandards()
        self.quality_evaluator = QualityEvaluator(nature_standards)

        # Best-effort init of TransportLayer; if unavailable, we keep a small shim.
        try:
            from ..agent_transport import TransportLayer  # type: ignore
            from ..config.agent_registry import AgentRegistry  # type: ignore

            self.registry = AgentRegistry.load()
            self.transport = TransportLayer(
                self.registry,
                self._fallback_transport_execution,
                driver=self,
            )
        except Exception as exc:  # pragma: no cover - depends on env
            logger.warning("TransportLayer unavailable; DAGExecutor/LangGraph wrappers will use driver fallback: %s", exc)
            self.transport = _DriverTransportShim(self)

        self.pressure_engine = MSRPPressureEngine(nature_standards)

        # ── NewDawn: Active enforcement layer ─────────────────────────────
        self._depth_preset: DepthPreset = resolve_depth_preset(
            getattr(self.config, "depth_preset", None)
        )
        self._cue_evaluator = CueEvaluator(max_cue_depth=2)
        self._msrp_dispatcher = MSRPPhaseDispatcher(max_spawn_tokens=3000)
        self._decision_ledger = DecisionLedger(max_entries=500)
        self._audit_builder: Optional[SessionAuditBundleBuilder] = None
        self._active_cue_results: List[CueResult] = []

        # TEA Protocol (Tool Context)
        self.tool_context = ToolContextProtocol()

        # Timescale/Neon event sink (lazy init)
        self._timescale_store: Optional[TimescaleEventStore] = None
        self._timescale_store_failed: bool = False
        self._session_run_ids: Dict[str, str] = {}

        # S1.2: delegation session tracking per sub-agent invocation
        self._delegation_sessions: Dict[str, Any] = {}  # session_id → DelegationSession

        # UCS: per-role context windows (session-scoped, persist across embodiments)
        self._role_contexts: Dict[str, RoleContext] = {}

        # S2: ProgramEnvelope tracking — session_id → ProgramEnvelope
        self._program_envelopes: Dict[str, Any] = {}
        self._run_event_logs: Dict[str, Any] = {}
        self._evidence_ledgers: Dict[str, Any] = {}

        # TEA Protocol (Tool-Environment-Agent): optional execution tracing
        if getattr(self.config, "tea_tracing_enabled", False):
            try:
                from ..tea.trace import TEATraceHook  # type: ignore

                self.add_tool_hook(
                    TEATraceHook(
                        checkpoint_dir=str(getattr(self.config, "checkpoint_dir", "./.checkpoints")),
                        trace_dirname=str(getattr(self.config, "tea_trace_dirname", "tea_traces")),
                    )
                )
            except Exception:
                # Never fail driver init due to tracing.
                pass

        # Security & governance (best-effort, dependency-free)
        self.security_analyzer = None
        if getattr(self.config, "enable_tool_security", False) and SecurityAnalyzer is not None:
            try:
                self.security_analyzer = SecurityAnalyzer()
            except Exception:
                self.security_analyzer = None

        self.confirmation_policy = None
        # P2-6: default to True (matching config.py) so governance is active
        # even if config object lacks the attribute.
        _gov_enabled = getattr(self.config, "enable_tool_governance", True)
        # In production, governance is MANDATORY — override any opt-out.
        _env_mode = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower()
        if _env_mode in ("prod", "production"):
            _gov_enabled = True
        if _gov_enabled and ConfirmationPolicy is not None:
            try:
                self.confirmation_policy = ConfirmationPolicy(
                    cost_threshold=float(getattr(self.config, "governance_cost_threshold_usd", 100.0)),
                    auto_approve_threshold=float(getattr(self.config, "governance_auto_approve_threshold_usd", 10.0)),
                    autonomous_budget=float(getattr(self.config, "governance_autonomous_budget_usd_week", 500.0)),
                )
            except Exception:
                self.confirmation_policy = None

        # Proactive Monitoring System (Phase 6)
        self.proactive_system = ProactiveSystem()

        # Thermodynamic Cognition (BioRouter)
        self.biorouter: Optional[BioRouter] = None
        if self.config.enable_thermodynamic_cognition:
            self.biorouter = BioRouter()
            logger.info("🔥 BioRouter (Thermodynamic Cognition) initialized")

        # LangGraph checkpointing
        self.checkpointer: Optional[Any] = None
        # AsyncSqliteSaver.from_conn_string returns an async context manager.
        # Keep the handle so we can close it in cleanup().
        self._checkpointer_cm: Optional[Any] = None

        # LangGraph StateGraph (v3.0)
        self.graph: Optional[Any] = None  # StateGraph instance
        self.compiled_graph: Optional[Any] = None  # Compiled runnable

        # Active sessions (legacy FSM mode)
        self.active_sessions: Dict[str, AgenticSession] = {}

        # ATOM temporal knowledge graph memory
        self.atom_memory: Optional[ATOMMemorySystem] = None
        self._atom_memory_init_attempted = False
        self._atom_memory_init_error: Optional[str] = None
        if self.config.enable_atom_memory:
            logger.info("ATOM memory deferred until first runtime demand")

        # Memory retrieval backends (best-effort, lazily instantiated when needed)
        self.session_repository = None
        self.user_rag_store = None
        self.milvus_user_rag_store = None
        self.graph_store = None
        self._owned_memory_backends: Set[str] = set()

        self.agent_summary_store = None
        try:
            from mica.drivers.persistence.agent_summary_store import AgentSummaryStore

            self.agent_summary_store = AgentSummaryStore()
        except Exception as exc:
            logger.debug("AgentSummaryStore unavailable; continuing without typed summary persistence: %s", exc)

        # Initialization flag
        self._initialized = False

        # GCS artifact sync — lazy per-user cloud mirror for driver artifacts.
        # Actual user_id is set at request time via _ensure_gcs_sync().
        self._gcs_artifact_sync: Optional[DriverArtifactSync] = None

        # Conversation log lock (prevents concurrent writes)
        self._conversation_log_lock = asyncio.Lock()

        # Saga log lock (prevents concurrent writes)
        self._saga_log_lock = asyncio.Lock()

        # DLM-LMP Bridge for entity extraction
        self.bridge = None
        if self.config.enable_bridge:
            logger.info("DLM-LMP Bridge deferred until first runtime demand")

        # Optional event sink for real-time UI streaming (WebSocket/SSE).
        # ContextVar avoids polluting/checkpointing LangGraph state with non-serializable callables.
        self._event_sink_var: contextvars.ContextVar[Optional[Callable[[Dict[str, Any]], Any]]] = contextvars.ContextVar(
            "mica_agentic_event_sink",
            default=None,
        )

        # P2-4: ContextVar for workspace_id (asyncio-safe across concurrent WS connections).
        self._workspace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "mica_workspace_id",
            default="",
        )

        # Optional pre/post hooks around tool execution (Orchestral-inspired).
        # Empty by default => no behavior change.
        self._tool_hooks: List[ToolExecutionHook] = []

        # Step 4: MCP resiliency state (per server.tool)
        self._mcp_circuit_state: Dict[str, Dict[str, Any]] = {}
        self._mcp_circuit_lock = asyncio.Lock()

        logger.info("AgenticDriver initialized (not yet connected)")

    def add_tool_hook(self, hook: "ToolExecutionHook") -> None:
        """Register a tool-execution hook.

        Hooks are best-effort: hook failures are swallowed.
        """
        if hook not in self._tool_hooks:
            self._tool_hooks.append(hook)

    def clear_tool_hooks(self) -> None:
        """Remove all registered tool hooks."""
        self._tool_hooks.clear()

    async def _run_tool_hooks(self, method: str, **kwargs: Any) -> None:
        hooks = list(self._tool_hooks)
        if not hooks:
            return
        for hook in hooks:
            try:
                fn = getattr(hook, method, None)
                if fn is None:
                    continue
                res = fn(**kwargs)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                continue

    def _emit_event(
        self,
        *,
        event_type: str,
        node_id: str,
        workflow_id: Optional[str] = None,
        data: Any = None,
    ) -> None:
        sink = self._event_sink_var.get()
        if sink is None:
            return
        try:
            payload: Dict[str, Any] = {
                "event_type": str(event_type),
                "node_id": str(node_id),
                "workflow_id": str(workflow_id or ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": _redact_obj(data),
            }
            res = sink(payload)
            if inspect.isawaitable(res):
                task = asyncio.create_task(res)
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:
            return

    async def execute_worker(
        self,
        worker: str,
        prompt: str,
        session_id: str = "default",
    ) -> Dict[str, Any]:
        """Compatibility API used by `mica.dag_executor` and `mica.langgraph.nodes`.

        Returns a normalized dict with at least:
        - worker
        - status
        - backend_type (best-effort)
        - data (dict)
        """

        raw: Dict[str, Any]
        if self.transport is not None:
            try:
                raw = await self.transport.execute_worker(worker, prompt, session_id)
            except Exception as exc:
                raw = {
                    "status": "FAILED",
                    "worker": worker,
                    "backend_type": "transport",
                    "error": str(exc),
                    "errors": [str(exc)],
                    "data": {},
                }
        else:
            raw = await self._fallback_transport_execution(worker, prompt)

        # Normalize fields for downstream parsers.
        normalized: Dict[str, Any] = dict(raw or {})
        normalized.setdefault("worker", worker)

        if "backend_type" not in normalized and "backend" in normalized:
            normalized["backend_type"] = normalized.get("backend")
        normalized.setdefault("backend_type", "unknown")

        # Ensure `data` exists and is a dict.
        data = normalized.get("data")
        if not isinstance(data, dict):
            data = {}
        # Common transport payloads
        if "response" in normalized and "response" not in data:
            data["response"] = normalized.get("response")
        if "result" in normalized and "result" not in data:
            data["result"] = normalized.get("result")
        normalized["data"] = _redact_obj(data)

        # Production safety: never return raw prompts or tool args.
        normalized.pop("prompt", None)
        normalized.pop("args", None)
        if "tool_calls" in normalized:
            # Keep a redacted/truncated hint only.
            normalized["tool_calls"] = _truncate_text(_redact_text(str(normalized.get("tool_calls"))), max_len=2000)

        if "error" in normalized and "errors" not in normalized:
            normalized["errors"] = [str(normalized.get("error"))]

        return _redact_obj(normalized)

    async def _fallback_transport_execution(self, worker: str, prompt: str) -> Dict[str, Any]:
        """Fallback executor used by TransportLayer when no backend is configured.

        This intentionally avoids returning sensitive fields like tool args.
        """
        from .execution import run_fallback_transport_execution

        class _ThermoAdapter:
            def __init__(self, driver_obj: "AgenticDriver"):
                self._driver = driver_obj

            def get_thermodynamic_snapshot(self, prompt_text: str, *, biorouter: Any, config: Any) -> Optional[Dict[str, Any]]:
                return self._driver._get_thermodynamic_snapshot(prompt_text)

        return await run_fallback_transport_execution(
            worker=worker,
            prompt=prompt,
            specialist_drivers=self.specialist_drivers,
            bridge_obj=self.bridge,
            thermodynamic_routing_service_obj=_ThermoAdapter(self),
            biorouter_obj=self.biorouter,
            config_obj=self.config,
            execute_with_mcp_fn=self._execute_with_mcp,
            redact_text_fn=_redact_text,
            truncate_text_fn=_truncate_text,
            redact_obj_fn=_redact_obj,
            emit_event_fn=self._emit_event,
        )

    # ------------------------------------------------------------------
    # Unified AgenticLoop integration (v3.1)
    # Single execution path for CLI, WS, HTTP API — AgenticDriver routes,
    # AgenticLoop runs the LLM+tool iteration.
    # ------------------------------------------------------------------

    # -- Class-level constants (I09-A) - moved to driver_constants.py
    _LOOP_SYSTEM_PROMPT: str = LOOP_SYSTEM_PROMPT
    _BIBLIOTECARIO_SYSTEM_PROMPT: str = BIBLIOTECARIO_SYSTEM_PROMPT
    _SPAWN_TOOLS: list = SPAWN_TOOLS
    _BIBLIOTECARIO_TOOLS: list = BIBLIOTECARIO_TOOLS
    _EXPERT_BASE_TOOLS: list = EXPERT_BASE_TOOLS
    _EXPERT_POOL: dict = EXPERT_POOL

    async def _create_backend_native_executor(
        self,
        *,
        user_id: str,
        session_id: Optional[str],
    ) -> Callable[[str, str, Dict[str, Any]], Awaitable[str]]:
        """Construct the backend-native executor behind a driver-owned seam."""

        from mica.agentic.ws_bridge import create_backend_executor

        backend_url = str(getattr(self.config, "backend_url", "") or "").strip()
        kwargs: Dict[str, Any] = {
            "user_id": user_id,
            "session_id": session_id,
            "workspace_id": self._workspace_id_var.get("") or None,
        }
        if backend_url:
            kwargs["base_url"] = backend_url
        return await create_backend_executor(**kwargs)

    def _build_loop_executor(
        self,
        user_id: str = "agent",
        session_id: Optional[str] = None,
        pending: Optional[Any] = None,
        provider_id: str = "anthropic",
        model_id: Optional[str] = None,
        abort: Optional[asyncio.Event] = None,
        reinjection_packet: Optional[Dict[str, Any]] = None,
    ) -> Callable[[str, str, Dict[str, Any]], Awaitable[str]]:
        """Build a ToolExecutor that routes AgenticLoop tool calls to MICA's full stack.

        Research tools  → LiteratureSearchService / UniProt REST / ATOM
        Specialist work → self._fallback_transport_execution() → BioDynamo / Alchemist / SMIC
        Resto           → MCP sessions or informative fallback
        """
        from mica.agentic.tool_capability_registry import (
            get_tool_capability,
            registry_items,
            validate_tool_registry_coverage,
        )
        from mica.agentic.ws_bridge import MICA_TOOLS
        from .execution import (
            BACKEND_ONLY_TYPED_TOOLS,
            FEED_TOOL_NAMES,
            REPO_IDE_TOOL_NAMES,
            backend_dependency_state as _backend_dependency_state_service,
            build_loop_executor_bootstrap,
            build_loop_gated_executor,
            coerce_seed_entities as _coerce_seed_entities_service,
            degraded_tool_response as _degraded_tool_response_service,
            dependency_state_for_tool as _dependency_state_for_tool_service,
            is_backend_only_typed_tool,
            network_dependency_state as _network_dependency_state_service,
            normalize_tool_payload as _normalize_tool_payload_service,
            pre_dispatch_gate as _pre_dispatch_gate_service,
            prepare_tool_surface,
            probe_host_reachability as _probe_host_reachability_service,
            provider_dependency_state as _provider_dependency_state_service,
            run_bibliotecario_revision_cycle,
            run_loop_front_branch,
            run_loop_primary_branch,
            run_loop_tail_branch,
            sandbox_dependency_state as _sandbox_dependency_state_service,
            transport_payload_or_degraded as _transport_payload_or_degraded_service,
            unavailable_tool_response as _unavailable_tool_response_service,
        )
        _driver_config = getattr(self, "config", None) or AgenticDriverConfig.from_driver_config()

        _degraded_tool_response = _degraded_tool_response_service
        _unavailable_tool_response = _unavailable_tool_response_service
        _coerce_seed_entities = _coerce_seed_entities_service
        _transport_payload_or_degraded = _transport_payload_or_degraded_service
        _normalize_tool_payload = _normalize_tool_payload_service

        effective_mica_tools = _filter_tools_for_driver_surface(MICA_TOOLS, _driver_config)

        _bootstrap = build_loop_executor_bootstrap(
            driver_obj=self,
            driver_config=_driver_config,
            user_id=user_id,
            session_id=session_id,
            provider_id=provider_id,
            model_id=model_id,
            abort=abort,
            reinjection_packet=reinjection_packet,
            mcp_available=_ensure_mcp_runtime() if getattr(self.config, "mcp_enabled", False) else False,
            get_tool_capability_fn=get_tool_capability,
            validate_tool_registry_coverage_fn=validate_tool_registry_coverage,
            registry_items_fn=registry_items,
            prepare_tool_surface_fn=prepare_tool_surface,
            effective_mica_tools=effective_mica_tools,
            spawn_tools=self._SPAWN_TOOLS,
            provider_dependency_state_service_fn=_provider_dependency_state_service,
            backend_dependency_state_service_fn=_backend_dependency_state_service,
            network_dependency_state_service_fn=_network_dependency_state_service,
            sandbox_dependency_state_service_fn=_sandbox_dependency_state_service,
            dependency_state_for_tool_service_fn=_dependency_state_for_tool_service,
            pre_dispatch_gate_service_fn=_pre_dispatch_gate_service,
            unavailable_tool_response_fn=_unavailable_tool_response,
            degraded_tool_response_fn=_degraded_tool_response,
            run_bibliotecario_revision_cycle_service_fn=run_bibliotecario_revision_cycle,
        )
        _loop_context = _bootstrap.context
        _backend_native_tool_names = _bootstrap.backend_native_tool_names
        _get_backend_native_executor = _bootstrap.get_backend_native_executor_fn
        _literature_helpers = _bootstrap.literature_helpers
        _shorten_query = _literature_helpers.shorten_query
        _search_literature_records = _literature_helpers.search_literature_records
        _search_literature_result = _literature_helpers.search_literature_result
        _uniprot_search = _literature_helpers.uniprot_search
        _dependency_state_for_tool = _bootstrap.dependency_gates.dependency_state_for_tool
        _pre_dispatch_gate = _bootstrap.dependency_gates.pre_dispatch_gate
        public_tool_names = _bootstrap.public_tool_names
        spawn_tool_names = _bootstrap.spawn_tool_names
        _run_bibliotecario_revision_cycle = _bootstrap.run_bibliotecario_revision_cycle_fn
        _agent_memory = _loop_context.agent_memory
        _summary_store = _loop_context.summary_store
        _workspace_id = _loop_context.workspace_id
        _parent_run_id = _loop_context.parent_run_id
        _active_session_id = _loop_context.active_session_id
        _retrieval_planner = _loop_context.retrieval_planner
        _last_bibliotecario_state = _loop_context.last_bibliotecario_state
        _driver_literature_sources = _loop_context.driver_literature_sources

        async def executor(name: str, call_id: str, args: Dict[str, Any]) -> str:
            front_branch_result = await run_loop_front_branch(
                name=name,
                call_id=call_id,
                args=args,
                pending=pending,
                invoke_feed_tool_fn=self._invoke_feed_tool,
                feed_tool_names=FEED_TOOL_NAMES,
                repo_ide_tool_names=REPO_IDE_TOOL_NAMES,
                backend_native_tool_names=_backend_native_tool_names,
                get_backend_native_executor_fn=_get_backend_native_executor,
                shorten_query_fn=_shorten_query,
                search_literature_result_fn=_search_literature_result,
                driver_literature_sources=_driver_literature_sources,
                uniprot_search_fn=_uniprot_search,
            )
            if front_branch_result is not None:
                return front_branch_result

            primary_branch_result = await run_loop_primary_branch(
                name=name,
                args=args,
                driver_obj=self,
                executor_fn=executor,
                pending=pending,
                session_id=session_id,
                user_id=user_id,
                workspace_id=_workspace_id,
                parent_run_id=_parent_run_id,
                provider_id=provider_id,
                model_id=model_id,
                abort=abort,
                active_session_id=_active_session_id,
                agent_memory_obj=_agent_memory,
                summary_store_obj=_summary_store,
                retrieval_planner_obj=_retrieval_planner,
                driver_literature_sources=_driver_literature_sources,
                last_bibliotecario_state=_last_bibliotecario_state,
                run_bibliotecario_revision_cycle_fn=_run_bibliotecario_revision_cycle,
                shorten_query_fn=_shorten_query,
                search_literature_result_fn=_search_literature_result,
                search_literature_records_fn=_search_literature_records,
                coerce_seed_entities_fn=_coerce_seed_entities,
                degraded_tool_response_fn=_degraded_tool_response,
                transport_payload_or_degraded_fn=_transport_payload_or_degraded,
                fallback_transport_execution_fn=self._fallback_transport_execution,
                build_runtime_consumption_context_fn=self._build_runtime_consumption_context,
                run_driver_owned_delegated_checkpoint_fn=_run_driver_owned_delegated_checkpoint,
                run_driver_owned_staging_deploy_checkpoint_fn=_run_driver_owned_staging_deploy_checkpoint,
                is_backend_only_typed_tool_fn=is_backend_only_typed_tool,
                backend_only_typed_tools=BACKEND_ONLY_TYPED_TOOLS,
                specialist_pool_obj=getattr(self, "_specialist_pool", None),
            )
            if primary_branch_result is not None:
                return primary_branch_result

            return await run_loop_tail_branch(
                name=name,
                args=args,
                executor_obj=self,
                pending=pending,
                invoke_feed_tool_fn=self._invoke_feed_tool,
                search_literature_records_fn=_search_literature_records,
                retrieval_planner_obj=_retrieval_planner,
                driver_literature_sources=_driver_literature_sources,
                user_id=user_id,
                workspace_id=_workspace_id,
                parent_run_id=_parent_run_id,
                agent_memory_obj=_agent_memory,
                summary_store_obj=_summary_store,
                persist_summary_fn=self._persist_agent_summary,
                filter_tools_for_lane_fn=filter_tools_for_lane,
                degraded_tool_response_fn=_degraded_tool_response,
                provider_id=provider_id,
                model_id=model_id,
                last_bibliotecario_state=_last_bibliotecario_state,
                fallback_transport_execution_fn=self._fallback_transport_execution,
            )
        return build_loop_gated_executor(
            execute_fn=executor,
            invoke_feed_tool_fn=self._invoke_feed_tool,
            pre_dispatch_gate_fn=_pre_dispatch_gate,
            dependency_state_for_tool_fn=_dependency_state_for_tool,
            normalize_tool_payload_fn=_normalize_tool_payload,
            public_tool_names=public_tool_names,
            spawn_tool_names=spawn_tool_names,
            cleanup_literature_service_fn=_literature_helpers.close_literature_service,
        )

    async def run_streaming(
        self,
        query: str,
        provider_id: str = "anthropic",
        model_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: str = "agent",
        abort: Optional[asyncio.Event] = None,
        reinjection_packet: Optional[Dict[str, Any]] = None,
    ):
        """Unified streaming entry point — yields AnyLoopEvent from AgenticLoop.

        This is the SINGLE execution path for all MICA entry points:
        - CLI (tools/mica_agent.py)              → Rich streaming display
        - WS bridge (ws_agentic.py)              → stream events to frontend
        - HTTP POST /api/v1/agentic/prompt       → collect events into result dict

        AgenticLoop handles: LLM inference, tool dispatch, context compaction,
        retry logic, streaming text deltas, cost tracking.
        AgenticDriver provides: tool routing, specialist driver delegation, ATOM memory.
        """
        from mica.agentic.core import AgenticLoop, LoopConfig, ProviderRegistry, ToolTruncator
        from mica.agentic.ws_bridge import MICA_TOOLS
        from mica.agentic.events import Error as _Error, LoopFinish as _LoopFinish, ToolCallEnd as _ToolCallEnd, ToolCallStart as _ToolCallStart
        from .execution import build_run_streaming_prompt_plan

        registry = ProviderRegistry.from_env()
        loop_cfg = LoopConfig(
            max_iterations=int(getattr(self.config, "max_iterations", 25) or 25),
            temperature=float(getattr(self.config, "temperature", 0.4) or 0.4),
            enable_hot_loop_reinjection=bool(reinjection_packet),
            reinjection_packet=dict(reinjection_packet or {}) if isinstance(reinjection_packet, dict) else None,
            negative_memory_mode=str((reinjection_packet or {}).get("negative_memory_mode") or "full"),
            visible_tombstone_classes=tuple(
                str(value or "").strip().lower()
                for value in list((reinjection_packet or {}).get("visible_tombstone_classes") or [])
                if str(value or "").strip()
            ),
            allow_appeal_regime=bool(((reinjection_packet or {}).get("appeal_regime_state") or {}).get("appeal_regime_active")),
            rupture_energy_budget=float(
                sum(
                    float(event.get("released_energy") or 0.0)
                    for event in list((reinjection_packet or {}).get("rupture_energy_events") or [])
                    if isinstance(event, dict)
                )
            ),
        )
        loop = AgenticLoop(registry, loop_cfg, run_id=self._session_run_ids.get(session_id or "", ""))
        from collections import deque
        _pending: deque = deque()
        executor = self._build_loop_executor(
            user_id=user_id,
            session_id=session_id,
            pending=_pending,
            provider_id=provider_id,
            model_id=model_id,
            abort=abort,
            reinjection_packet=reinjection_packet,
        )
        _prompt_plan = build_run_streaming_prompt_plan(
            query=query,
            system_prompt=system_prompt,
            loop_system_prompt=self._LOOP_SYSTEM_PROMPT,
            session_id=session_id,
            spawn_tools=self._SPAWN_TOOLS,
            select_effective_tools_for_query_fn=self._select_effective_mica_tools_for_query,
            runtime_skill_overrides_fn=self._runtime_skill_overrides,
            resolve_runtime_skills_fn=resolve_runtime_skills,
            set_latest_runtime_skill_plan_fn=self._set_latest_runtime_skill_plan,
        )
        effective_system = _prompt_plan.effective_system
        effective_mica_tools = _prompt_plan.effective_mica_tools
        routing_meta = _prompt_plan.routing_meta
        runtime_skill_plan = _prompt_plan.runtime_skill_plan
        if str(routing_meta.get("routing_hint") or "").strip():
            logger.info(
                "ToolKG routed %d tools for query (degraded=%s, intents=%s)",
                len(routing_meta.get("planned_tool_names") or []),
                bool(routing_meta.get("degraded")),
                routing_meta.get("intent_tags") or [],
            )
        if runtime_skill_plan.prompt_block:
            logger.info(
                "Runtime skills activated for session %s: %s",
                session_id or "default",
                list(runtime_skill_plan.active_skill_ids),
            )
        if routing_meta.get("routed"):
            logger.info("ToolKG effective routing: %d/%d MICA tools", len(effective_mica_tools), len(MICA_TOOLS))

        all_tools = _prompt_plan.all_tools
        route_card = routing_meta.get("route_card") if isinstance(routing_meta.get("route_card"), dict) else {}
        route_card_tools = [str(item).strip() for item in list(route_card.get("planned_tools") or []) if str(item).strip()]
        route_required_tools = [
            str(item).strip()
            for item in list(route_card.get("required_tool_names") or [])
            if str(item).strip()
        ]
        if not route_required_tools and bool(route_card.get("required")):
            route_required_tools = list(route_card_tools)
        route_requires_execution = bool(route_card.get("required")) and bool(route_required_tools)
        required_tool_contract = self._resolve_required_tool_contract(
            query=query,
            required_tool_names=route_required_tools,
            visible_tool_names=[
                str(tool.get("function", tool).get("name") or "").strip()
                for tool in all_tools
                if str(tool.get("function", tool).get("name") or "").strip()
            ],
        )
        enforced_required_tools = [
            str(item).strip()
            for item in list(required_tool_contract.get("required_tool_names") or [])
            if str(item).strip()
        ]
        bootstrap_call = required_tool_contract.get("bootstrap_call") if isinstance(required_tool_contract.get("bootstrap_call"), dict) else None
        followup_guidance = str(required_tool_contract.get("followup_guidance") or "").strip()
        if route_requires_execution:
            effective_system = (
                f"{effective_system}\n\n"
                f"## REQUIRED TOOL EXECUTION CONTRACT\n"
                f"This route card is mandatory. Before writing narrative prose, call at least one visible required tool from: {', '.join(enforced_required_tools)}.\n"
                f"Do not reply with intention-only text such as 'I'll start by...' or 'First, I will...'.\n"
                f"If a required tool is unavailable, explicitly name the missing tool and why it cannot run."
            )
        if followup_guidance:
            effective_system = (
                f"{effective_system}\n\n"
                f"## POST-SCOUT FOLLOW-UP LIMIT\n"
                f"{followup_guidance}"
            )

        if route_requires_execution:
            loop.config.required_tool_names = tuple(enforced_required_tools)
            loop.config.required_tool_retry_limit = 1
        loop_messages = [{"role": "user", "content": query}]

        # ── R24 §8: watchdog guard. Abort total run after budget expires,
        # emit a tombstone to the agent feed, release resources cleanly. ─
        try:
            watchdog_s = int(os.environ.get("MICA_DRIVER_WATCHDOG_SECONDS", "1800"))
        except (TypeError, ValueError):
            watchdog_s = 1800

        try:
            async with asyncio.timeout(watchdog_s):
                if bootstrap_call is not None:
                    bootstrap_tc = {
                        "id": f"bootstrap_{bootstrap_call['name']}_{uuid.uuid4().hex[:8]}",
                        "name": str(bootstrap_call["name"]),
                        "args": dict(bootstrap_call.get("args") or {}),
                    }
                    yield _ToolCallStart(
                        run_id=loop.run_id,
                        program_id=loop.program_id,
                        call_id=bootstrap_tc["id"],
                        name=bootstrap_tc["name"],
                        args=dict(bootstrap_tc["args"]),
                    )
                    if abort and abort.is_set():
                        yield _Error(
                            run_id=loop.run_id,
                            program_id=loop.program_id,
                            message="Aborted by caller before bootstrap tool execution",
                            retryable=False,
                        )
                        yield _LoopFinish(
                            run_id=loop.run_id,
                            program_id=loop.program_id,
                            total_steps=0,
                            total_cost_usd=0.0,
                            finish_reason="aborted",
                            remediation_hint="Caller requested abort before required bootstrap tool execution.",
                            cumulative_tokens=0,
                        )
                        return
                    bootstrap_truncator = ToolTruncator(
                        max_output_chars=loop.config.truncation_limit_for(bootstrap_tc["name"])
                    )
                    bootstrap_result, bootstrap_duration_ms, bootstrap_truncated = await AgenticLoop._execute_one_tool(
                        bootstrap_tc,
                        executor,
                        bootstrap_truncator,
                    )
                    loop_messages.append(AgenticLoop._build_assistant_message(provider_id, "", [bootstrap_tc]))
                    AgenticLoop._append_tool_results(
                        provider_id,
                        loop_messages,
                        [(bootstrap_tc, bootstrap_result, bootstrap_duration_ms, bootstrap_truncated)],
                    )
                    while _pending:
                        yield _pending.popleft()
                    yield _ToolCallEnd(
                        run_id=loop.run_id,
                        program_id=loop.program_id,
                        call_id=bootstrap_tc["id"],
                        name=bootstrap_tc["name"],
                        result=bootstrap_result,
                        duration_ms=bootstrap_duration_ms,
                        was_truncated=bootstrap_truncated,
                    )
                async for event in loop.run(
                    messages=loop_messages,
                    tools=all_tools,
                    tool_executor=executor,
                    provider_id=provider_id,
                    model_id=model_id,
                    system_prompt=effective_system,
                    abort=abort,
                ):
                    # Drain sub-agent events before ToolCallEnd: thinking → speaking → done → result
                    if isinstance(event, _ToolCallEnd):
                        while _pending:
                            yield _pending.popleft()
                    yield event
        except (asyncio.TimeoutError, TimeoutError):
            logger.error(
                "AgenticDriver.run_streaming watchdog expired after %ds (session=%s, user=%s)",
                watchdog_s,
                session_id,
                user_id,
            )
            try:
                await self._invoke_feed_tool("publish_cue", {
                    "agent_id": user_id or "driver",
                    "post_type": "tombstone",
                    "topic": "errors",
                    "title": f"Driver watchdog timeout after {watchdog_s}s",
                    "body": f"run_streaming aborted: session={session_id or ''}, query_preview={(query or '')[:200]}",
                    "session_id": session_id,
                    "metadata": {
                        "watchdog_seconds": watchdog_s,
                        "session_id": session_id,
                        "user_id": user_id,
                        "provider_id": provider_id,
                        "model_id": model_id,
                    },
                })
            except Exception:
                logger.debug("watchdog tombstone publish failed", exc_info=True)
            raise
        finally:
            # ── Cleanup shared resources (DLM session, etc.) runs on both
            #    normal completion and timeout/abort paths. ──────────────
            if hasattr(executor, '_cleanup'):
                try:
                    await executor._cleanup()
                except Exception:
                    logger.debug("executor cleanup failed", exc_info=True)

    def _get_run_event_log(self, run_id: str) -> Optional[Any]:
        """Return a cached EventLog for *run_id* (best-effort)."""
        if not run_id:
            return None
        cache = getattr(self, "_run_event_logs", None)
        if cache is None:
            self._run_event_logs = {}
            cache = self._run_event_logs
        if run_id not in cache:
            from mica.infrastructure.event_log import EventLog
            cache[run_id] = EventLog(run_id=run_id)
        return cache[run_id]

    def _log_program_envelope_snapshot(
        self,
        run_id: str,
        envelope: Any,
        *,
        event_type: str,
        driver_id: str = "",
        phase: str = "",
    ) -> Optional[Any]:
        """Persist a ProgramEnvelope snapshot for the given run (best-effort)."""
        elog = self._get_run_event_log(run_id)
        if elog is None:
            return None
        try:
            return elog.log_envelope_snapshot(
                envelope,
                event_type=event_type,
                driver_id=driver_id,
                phase=phase,
            )
        except Exception:
            logger.debug("ProgramEnvelope snapshot logging failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # UCS: Embodied role execution (sole path for internal roles)
    # ------------------------------------------------------------------

    def _get_or_create_role_context(self, role_id: str) -> RoleContext:
        """Return existing RoleContext or create a fresh one (session-scoped)."""
        if role_id not in self._role_contexts:
            self._role_contexts[role_id] = RoleContext(role_id=role_id)
        return self._role_contexts[role_id]

    def _build_role_executor(
        self,
        role_ctx: RoleContext,
        parent_executor: Any,
    ) -> Callable:
        """Build a role-aware tool executor that tracks citations/gaps in RoleContext."""
        from .execution import build_role_executor as _build_role_executor_service

        return _build_role_executor_service(role_ctx=role_ctx, parent_executor=parent_executor)

    async def _embody_role(
        self,
        role_spec: RoleSpec,
        task_messages: List[Dict[str, Any]],
        *,
        provider_id: str,
        model_id: Optional[str] = None,
        pending_events: Any = None,
        abort: Optional[asyncio.Event] = None,
        inject_prior_context: bool = True,
        program_envelope: Optional[Any] = None,
        parent_executor: Optional[Any] = None,
        available_tools: Optional[List[Dict[str, Any]]] = None,
        inherited_tombstones: Optional[List[Dict[str, Any]]] = None,
        rupture_budget: Optional[RuptureBudget] = None,
    ) -> Tuple[str, Optional[str], RoleContext]:
        """Embody a role using the unified cognitive substrate.

        Returns: (synthesis_text, report_path, role_context)
        """
        from .execution.embody_role_executor import execute_role_embodiment
        return await execute_role_embodiment(
            role_spec=role_spec,
            task_messages=task_messages,
            driver_self=self,
            provider_id=provider_id,
            model_id=model_id,
            pending_events=pending_events,
            abort=abort,
            inject_prior_context=inject_prior_context,
            program_envelope=program_envelope,
            parent_executor=parent_executor,
            available_tools=available_tools,
            inherited_tombstones=inherited_tombstones,
            rupture_budget=rupture_budget,
        )

    # ------------------------------------------------------------------
    # Auto-export: writes a structured .md for every sub-agent execution
    # into MICAV4DOCS/reports/agent_reports/
    # ------------------------------------------------------------------
    @staticmethod
    def _export_agent_report(
        agent_name: str,
        session_id: str,
        messages: List[Dict[str, Any]],
        synthesis: str,
    ) -> Optional[str]:
        """Write a standalone Markdown report for a sub-agent execution.

        Location: ``src/mica/MICAV4DOCS/reports/agent_reports/``
        Filename: ``{agent_name}_{YYYYMMDD_HHMMSS}_{session_id}.md``
        """
        import datetime
        from pathlib import Path

        if not synthesis or len(synthesis) < 20:
            return None  # Skip trivial / empty outputs

        now = datetime.datetime.now()
        ts_file = now.strftime("%Y%m%d_%H%M%S")
        ts_human = now.strftime("%Y-%m-%d %H:%M:%S")

        # Resolve reports dir relative to this source file
        _this_dir = Path(__file__).resolve().parent          # src/mica/drivers/
        reports_dir = _this_dir.parent / "MICAV4DOCS" / "reports" / "agent_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        fname = f"{agent_name}_{ts_file}_{session_id}.md"

        # Extract query from messages (best-effort: first user message)
        query_text = ""
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str) and len(content) > 10:
                    query_text = content[:500]
                    break

        lines = [
            f"# Agent Report: {agent_name}",
            f"",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Agent | `{agent_name}` |",
            f"| Session | `{session_id}` |",
            f"| Timestamp | {ts_human} |",
            f"| Synthesis length | {len(synthesis)} chars |",
            f"",
            f"## Query / Input",
            f"",
            f"```",
            query_text or "(no user message captured)",
            f"```",
            f"",
            f"## Synthesis",
            f"",
            synthesis,
            f"",
            f"---",
            f"*Auto-generated by MICA AgenticDriver `_spawn_agent`*",
        ]

        path = reports_dir / fname
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Exported agent report: %s", fname)
        return str(path)

    def _derive_summary_workspace_id(self, user_id: str, explicit_workspace_id: str = "") -> str:
        # P2-4: Prefer explicit workspace_id from client over derived value.
        ws = explicit_workspace_id or self._workspace_id_var.get("")
        if ws and ws.strip():
            return ws.strip()
        return f"user:{(user_id or 'default').strip() or 'default'}"

    def _resolve_memory_backend(
        self,
        *,
        cache_attr: str,
        candidate_attrs: Sequence[str],
        factory: Optional[Callable[[], Any]],
        label: str,
    ) -> Any:
        current = getattr(self, cache_attr, None)
        if current is not None:
            return current

        for attr in candidate_attrs:
            value = getattr(self, attr, None)
            if value is not None:
                setattr(self, cache_attr, value)
                return value

        if factory is None:
            return None

        try:
            value = factory()
        except Exception as exc:
            logger.debug("%s unavailable; continuing without that retrieval backend: %s", label, exc)
            return None

        setattr(self, cache_attr, value)
        owned = set(getattr(self, "_owned_memory_backends", set()))
        owned.add(cache_attr)
        setattr(self, "_owned_memory_backends", owned)
        return value

    def _resolve_session_repository(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="session_repository",
            candidate_attrs=("_session_repository", "neon_session_repository", "session_repo"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.session_repository",
                fromlist=["NeonSessionRepository"],
            ).NeonSessionRepository(),
            label="NeonSessionRepository",
        )

    async def _persist_driver_session_start(
        self,
        *,
        session_id: str,
        user_query: str,
        mode: str,
        user_id: Optional[str],
        bucket: Optional[str],
        workspace_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        repo = self._resolve_session_repository()
        if repo is None:
            return

        metadata: Dict[str, Any] = {
            "source": "agentic_driver_direct",
            "status": "running",
        }
        if bucket:
            metadata["bucket"] = bucket
        if workspace_id:
            metadata["workspace_id"] = workspace_id
        if run_id:
            metadata["run_id"] = run_id

        message_metadata = dict(metadata)
        message_metadata["phase"] = "input"

        try:
            await repo.save_session(
                session_id=session_id,
                user_id=(user_id or "direct_driver").strip() or "direct_driver",
                conversation_history=[],
                mode=mode,
                metadata=metadata,
            )
            await repo.append_message(
                session_id=session_id,
                role="user",
                content=user_query,
                metadata=message_metadata,
            )
        except Exception as exc:
            logger.warning("Driver session start persistence failed for %s: %s", session_id, exc)

    async def _persist_driver_session_success(
        self,
        *,
        session_id: str,
        result: Dict[str, Any],
        bucket: Optional[str],
        run_id: Optional[str],
    ) -> None:
        repo = self._resolve_session_repository()
        if repo is None:
            return

        final_result = result.get("final_result") if isinstance(result, dict) else result
        try:
            content = (
                final_result
                if isinstance(final_result, str)
                else json.dumps(final_result, ensure_ascii=False, default=str)
            )
        except Exception:
            content = str(final_result)

        metadata: Dict[str, Any] = {
            "source": "agentic_driver_direct",
            "status": "completed",
            "phase": "output",
        }
        if bucket:
            metadata["bucket"] = bucket
        if run_id:
            metadata["run_id"] = run_id

        try:
            await repo.append_message(
                session_id=session_id,
                role="assistant",
                content=content,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("Driver session success persistence failed for %s: %s", session_id, exc)

    async def _persist_driver_session_failure(
        self,
        *,
        session_id: str,
        exc: Exception,
        bucket: Optional[str],
        run_id: Optional[str],
    ) -> None:
        repo = self._resolve_session_repository()
        if repo is None:
            return

        metadata: Dict[str, Any] = {
            "source": "agentic_driver_direct",
            "status": "failed",
            "phase": "error",
            "error_type": type(exc).__name__,
        }
        if bucket:
            metadata["bucket"] = bucket
        if run_id:
            metadata["run_id"] = run_id

        try:
            await repo.append_message(
                session_id=session_id,
                role="assistant",
                content=f"ERROR: {exc}",
                metadata=metadata,
            )
        except Exception as repo_exc:
            logger.warning("Driver session failure persistence failed for %s: %s", session_id, repo_exc)

    def _attach_epistemic_firewall_verdict(
        self,
        *,
        result: Dict[str, Any],
        verdict: FirewallVerdict,
    ) -> None:
        verdict_payload = verdict.to_dict()
        result["epistemic_firewall"] = verdict_payload
        runtime_state = result.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            result["runtime"] = runtime_state
        runtime_state["epistemic_firewall"] = verdict_payload
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["epistemic_firewall"] = verdict_payload
            dossier_envelope = final_result.get("dossier_envelope")
            if isinstance(dossier_envelope, dict):
                dossier_envelope["epistemic_firewall"] = verdict_payload

    def _build_pre_routing_firewall_result(
        self,
        *,
        session_id: str,
        run_id: str,
        user_query: str,
        verdict: FirewallVerdict,
    ) -> Dict[str, Any]:
        rationale = "; ".join(verdict.reasons) or "Query was blocked by the epistemic firewall before routing."
        return {
            "session_id": session_id,
            "run_id": run_id,
            "execution_path": "pre_routing_firewall",
            "runtime": {
                "transport_path": "pre_routing_firewall",
                "degradation_flags": ["pre_routing_firewall_block"],
                "fallbacks_used": [],
                "capabilities_unavailable": [],
                "epistemic_firewall": verdict.to_dict(),
            },
            "final_result": {
                "summary": "The request was halted before execution because it encoded an unsupported high-certainty premise.",
                "answer": rationale,
                "claims": [],
                "sources": [],
                "artifacts": [],
                "findings": [],
                "query": user_query,
            },
            "epistemic_firewall": verdict.to_dict(),
        }

    def _attach_cognitive_layer_verdicts(
        self,
        *,
        result: Dict[str, Any],
        ach_state: Dict[str, Any],
        critic_verdict: Dict[str, Any],
    ) -> None:
        critic_verdict = validate_critic_verdict(critic_verdict)
        payload = {
            "hypothesis_competition": ach_state,
            "critic_pass": critic_verdict,
        }
        result["cognitive_layer"] = payload
        runtime_state = result.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            result["runtime"] = runtime_state
        runtime_state["cognitive_layer"] = payload
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["hypothesis_competition"] = ach_state
            final_result["critic_pass"] = critic_verdict

    def _attach_thermodynamic_route(self, *, result: Dict[str, Any], route_state: Dict[str, Any]) -> None:
        result["thermodynamic_routing"] = route_state
        runtime_state = result.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            result["runtime"] = runtime_state
        runtime_state["thermodynamic_routing"] = route_state
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["thermodynamic_routing"] = route_state

    def _load_chaos_initial_result(
        self,
        *,
        session_id: str,
        run_id: str,
        user_query: str,
    ) -> Optional[Dict[str, Any]]:
        fixture_path = str(os.getenv("MICA_CHAOS_INITIAL_RESULT_JSON", "") or "").strip()
        if not fixture_path:
            return None

        path = Path(fixture_path).expanduser()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            payload = payload["result"]
        if not isinstance(payload, dict):
            raise ValueError("MICA_CHAOS_INITIAL_RESULT_JSON must point to a JSON object or an object with a 'result' mapping")

        seeded = dict(payload)
        seeded.setdefault("session_id", session_id)
        seeded.setdefault("run_id", run_id)
        seeded.setdefault("execution_path", "chaos_seeded_initial_result")
        seeded["user_query"] = user_query

        runtime_state = seeded.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            seeded["runtime"] = runtime_state
        runtime_state.setdefault("transport_path", "chaos_seeded_initial_result")
        runtime_state["chaos_initial_result_fixture"] = str(path.resolve())
        return seeded

    def _maybe_raise_retry_execution_fault(
        self,
        *,
        retry_execution_path: str,
        reinjection_packet: Optional[Dict[str, Any]],
    ) -> None:
        raw = str(os.getenv("MICA_CHAOS_FAIL_RETRY_EXECUTION", "") or "").strip().lower()
        if raw not in {"1", "true", "yes", "any", "agentic_loop", "langgraph"}:
            return
        if raw not in {"1", "true", "yes", "any"} and raw != str(retry_execution_path or "").strip().lower():
            return

        expected_packet_id = str(os.getenv("MICA_CHAOS_FAIL_RETRY_PACKET_ID", "") or "").strip()
        packet_id = str((reinjection_packet or {}).get("packet_id") or "")
        if expected_packet_id and expected_packet_id != packet_id:
            return

        raise RuntimeError("Simulated Network/LLM Failure")

    def _attach_hot_loop_reinjection(
        self,
        *,
        result: Dict[str, Any],
        packet: Optional[Dict[str, Any]],
    ) -> None:
        if not isinstance(packet, dict) or not packet:
            return
        from mica.agentic.core import build_negative_memory_summary, normalize_negative_memory_mode

        history = [dict(packet)]
        residual_inventory = list(packet.get("residual_tasks") or [])
        branch_tombstones = list(packet.get("branch_tombstones") or [])
        negative_memory_mode = normalize_negative_memory_mode(packet.get("negative_memory_mode") or "full")
        negative_memory_summary = dict(
            packet.get("negative_memory_summary")
            or build_negative_memory_summary(packet, negative_memory_mode=negative_memory_mode)
        )
        appeal_regime_state = dict(packet.get("appeal_regime_state") or {"appeal_regime_active": False, "policy": "normal"})
        rupture_energy_events = list(packet.get("rupture_energy_events") or [])

        result["reinjection_history"] = history
        result["residual_inventory"] = residual_inventory
        result["branch_tombstones"] = branch_tombstones
        result["negative_memory_summary"] = negative_memory_summary
        result["appeal_regime_state"] = appeal_regime_state
        result["rupture_energy_events"] = rupture_energy_events
        result["soft_repulsion_warnings"] = list(packet.get("soft_repulsion_warnings") or [])

        runtime_state = result.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            result["runtime"] = runtime_state
        runtime_state["reinjection_history"] = history
        runtime_state["residual_inventory"] = residual_inventory
        runtime_state["branch_tombstones"] = branch_tombstones
        runtime_state["negative_memory_summary"] = negative_memory_summary
        runtime_state["appeal_regime_state"] = appeal_regime_state
        runtime_state["rupture_energy_events"] = rupture_energy_events
        runtime_state["soft_repulsion_warnings"] = list(packet.get("soft_repulsion_warnings") or [])

        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["reinjection_history"] = history
            final_result["residual_inventory"] = residual_inventory
            final_result["branch_tombstones"] = branch_tombstones
            final_result["negative_memory_summary"] = negative_memory_summary
            final_result["appeal_regime_state"] = appeal_regime_state
            final_result["rupture_energy_events"] = rupture_energy_events
            final_result["soft_repulsion_warnings"] = list(packet.get("soft_repulsion_warnings") or [])

    def _extract_negative_memory_context(
        self,
        packet: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(packet, dict) or not packet:
            return {}

        from mica.agentic.core import build_negative_memory_summary, normalize_negative_memory_mode, normalize_tombstone_class

        negative_memory_mode = normalize_negative_memory_mode(packet.get("negative_memory_mode") or "full")
        tombstones = [
            dict(tombstone)
            for tombstone in list(packet.get("branch_tombstones") or [])
            if isinstance(tombstone, dict)
        ]
        operational_tombstones = [t for t in tombstones if normalize_tombstone_class(t) == "operational"]
        archaeological_tombstones = [t for t in tombstones if normalize_tombstone_class(t) == "archaeological"]
        heretical_tombstones = [t for t in tombstones if normalize_tombstone_class(t) == "heretical"]
        return {
            "negative_memory_mode": negative_memory_mode,
            "negative_memory_summary": dict(
                packet.get("negative_memory_summary")
                or build_negative_memory_summary(packet, negative_memory_mode=negative_memory_mode)
            ),
            "operational_tombstones": operational_tombstones,
            "archaeological_tombstones": archaeological_tombstones,
            "heretical_tombstones": heretical_tombstones,
            "soft_repulsion_warnings": list(packet.get("soft_repulsion_warnings") or []),
            "appeal_regime_state": dict(packet.get("appeal_regime_state") or {}),
            "rupture_energy_events": list(packet.get("rupture_energy_events") or []),
        }

    def _resolve_literature_retrieval_policy(
        self,
        *,
        query: str,
        max_papers: int,
        sources: Optional[Sequence[str]] = None,
        extra_queries: Optional[Sequence[str]] = None,
        negative_memory_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from mica.agentic.core import normalize_negative_memory_mode

        context = negative_memory_context or {}
        default_sources = list(
            getattr(self, "_driver_literature_sources", None)
            or ["semantic_scholar", "pubmed", "openalex"]
        )
        summary = dict(context.get("negative_memory_summary") or {})
        mode = normalize_negative_memory_mode(
            context.get("negative_memory_mode")
            or summary.get("negative_memory_mode")
            or "full"
        )
        requested_sources = [
            str(source or "").strip()
            for source in list(sources or default_sources)
            if str(source or "").strip()
        ]
        effective_sources = list(dict.fromkeys(requested_sources)) or list(default_sources)
        explicit_extra_queries = [
            str(value or "").strip()
            for value in list(extra_queries or [])
            if str(value or "").strip()
        ]
        appeal_state = dict(context.get("appeal_regime_state") or {})
        soft_repulsion_warnings = [
            warning for warning in list(context.get("soft_repulsion_warnings") or [])
            if isinstance(warning, dict)
        ]
        appeal_candidates: List[str] = []
        for value in list(appeal_state.get("appeal_candidates") or []):
            text = str(value or "").strip()
            if text:
                appeal_candidates.append(text)
        for warning in soft_repulsion_warnings:
            text = str(warning.get("target_id") or "").strip()
            if text:
                appeal_candidates.append(text)
        appeal_candidates = list(dict.fromkeys(appeal_candidates))

        effective_max_papers = max(1, int(max_papers or 1))
        effective_extra_queries = list(dict.fromkeys(explicit_extra_queries))
        applied_policy = [f"negative_memory_mode:{mode}"]

        if mode == "full":
            if appeal_state.get("appeal_regime_active") and appeal_candidates:
                effective_extra_queries = list(dict.fromkeys(explicit_extra_queries + appeal_candidates[:3]))
                applied_policy.append("appeal_candidate_expansion")
        elif mode == "semi_blind":
            effective_max_papers = max(5, min(effective_max_papers, int(round(effective_max_papers * 0.75)) or effective_max_papers))
            applied_policy.extend(["no_negative_memory_query_expansion", "reduced_retrieval_budget"])
        else:
            indexed_sources = [source for source in effective_sources if source in {"semantic_scholar", "pubmed"}]
            effective_sources = indexed_sources or ["semantic_scholar", "pubmed"]
            effective_extra_queries = []
            effective_max_papers = max(5, min(effective_max_papers, int(round(effective_max_papers * 0.5)) or effective_max_papers))
            applied_policy.extend(["indexed_sources_only", "query_expansion_disabled", "reduced_retrieval_budget"])

        return {
            "query": query,
            "max_papers": effective_max_papers,
            "sources": effective_sources,
            "extra_queries": effective_extra_queries,
            "policy": {
                "negative_memory_mode": mode,
                "appeal_regime_active": bool(appeal_state.get("appeal_regime_active")),
                "soft_repulsion_warning_count": len(soft_repulsion_warnings),
                "applied_policy": applied_policy,
            },
        }

    def _apply_negative_memory_to_critic_verdict(
        self,
        *,
        result: Dict[str, Any],
        critic_verdict: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return validate_critic_verdict(critic_verdict)

        final_result = result.get("final_result") if isinstance(result.get("final_result"), dict) else {}
        appeal_regime_state = dict(
            result.get("appeal_regime_state")
            or final_result.get("appeal_regime_state")
            or {}
        )
        soft_repulsion_warnings = [
            warning for warning in list(result.get("soft_repulsion_warnings") or final_result.get("soft_repulsion_warnings") or [])
            if isinstance(warning, dict)
        ]
        if not appeal_regime_state and not soft_repulsion_warnings:
            return validate_critic_verdict(critic_verdict)

        updated = dict(critic_verdict or {})
        rationale = [str(item) for item in list(updated.get("rationale") or []) if str(item)]
        if soft_repulsion_warnings:
            rationale.append(
                f"Soft-repulsion remains open for {len(soft_repulsion_warnings)} branch(es); anomaly-zone candidates require fresh evidence before reuse."
            )
        if appeal_regime_state.get("appeal_regime_active"):
            rationale.append("Appeal regime is active; anomaly candidates must be explicitly adjudicated instead of silently ignored.")
            updated["retry_recommended"] = True
            updated["escalate_critique"] = True
            if str(updated.get("status") or "accept").strip().lower() == "accept":
                updated["status"] = "critical"
        if soft_repulsion_warnings:
            guidance_suffix = "Search for materially new evidence around appeal candidates before reviving any soft-repelled branch."
            updated["retry_guidance"] = f"{str(updated.get('retry_guidance') or '').strip()} {guidance_suffix}".strip()
        updated["rationale"] = rationale
        updated["appeal_regime_state"] = appeal_regime_state
        updated["soft_repulsion_warnings"] = soft_repulsion_warnings
        updated["negative_memory_review"] = {
            "appeal_regime_active": bool(appeal_regime_state.get("appeal_regime_active")),
            "soft_repulsion_warning_count": len(soft_repulsion_warnings),
            "appeal_candidates": list(appeal_regime_state.get("appeal_candidates") or []),
        }
        return validate_critic_verdict(updated)

    def _apply_negative_memory_guidance(
        self,
        *,
        result: Dict[str, Any],
    ) -> None:
        if not isinstance(result, dict):
            return

        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return

        appeal_regime_state = dict(
            result.get("appeal_regime_state")
            or final_result.get("appeal_regime_state")
            or {}
        )
        soft_repulsion_warnings = [
            warning for warning in list(result.get("soft_repulsion_warnings") or final_result.get("soft_repulsion_warnings") or [])
            if isinstance(warning, dict)
        ]
        if not appeal_regime_state and not soft_repulsion_warnings:
            return

        guidance = {
            "appeal_regime_active": bool(appeal_regime_state.get("appeal_regime_active")),
            "soft_repulsion_warning_count": len(soft_repulsion_warnings),
            "appeal_candidates": list(appeal_regime_state.get("appeal_candidates") or []),
            "guidance": [],
        }
        if soft_repulsion_warnings:
            guidance["guidance"].append(
                f"{len(soft_repulsion_warnings)} soft-repulsion zone(s) remain open and should only be revisited with materially new evidence."
            )
        if appeal_regime_state.get("appeal_regime_active"):
            guidance["guidance"].append(
                "Appeal regime is active; anomaly candidates remain under investigative review rather than ordinary closure."
            )

        summary = str(final_result.get("summary") or "").strip()
        if guidance["guidance"]:
            guidance_sentence = " ".join(guidance["guidance"])
            if guidance_sentence not in summary:
                final_result["summary"] = f"{summary} {guidance_sentence}".strip()
            uncertainty = str(final_result.get("uncertainty_summary") or "").strip()
            if guidance_sentence not in uncertainty:
                final_result["uncertainty_summary"] = f"{uncertainty} {guidance_sentence}".strip()

        limitations = [str(item) for item in list(final_result.get("limitations") or []) if str(item)]
        next_steps = [str(item) for item in list(final_result.get("next_steps") or []) if str(item)]
        if soft_repulsion_warnings:
            limitation = "Soft-repelled branches remain unresolved and cannot be treated as ordinary closures."
            if limitation not in limitations:
                limitations.append(limitation)
            next_step = "Acquire materially new evidence before reviving soft-repelled branches."
            if next_step not in next_steps:
                next_steps.append(next_step)
        if appeal_regime_state.get("appeal_regime_active"):
            next_step = "Evaluate appeal candidates under anomaly-mode reasoning before declaring epistemic closure."
            if next_step not in next_steps:
                next_steps.append(next_step)
        final_result["limitations"] = limitations
        final_result["next_steps"] = next_steps
        final_result["negative_memory_guidance"] = guidance
        result["negative_memory_guidance"] = guidance
        runtime_state = result.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            result["runtime"] = runtime_state
        runtime_state["negative_memory_guidance"] = guidance

        paper = final_result.get("paper")
        if isinstance(paper, dict):
            paper_limitations = [str(item) for item in list(paper.get("limitations") or []) if str(item)]
            paper_next_steps = [str(item) for item in list(paper.get("next_steps") or []) if str(item)]
            for item in limitations:
                if item not in paper_limitations:
                    paper_limitations.append(item)
            for item in next_steps:
                if item not in paper_next_steps:
                    paper_next_steps.append(item)
            paper["limitations"] = paper_limitations
            paper["next_steps"] = paper_next_steps

    def _attach_pipeline_output(
        self,
        *,
        result: Dict[str, Any],
        pipeline_output: Any,
    ) -> None:
        if not isinstance(result, dict) or pipeline_output is None or not hasattr(pipeline_output, "to_dict"):
            return

        payload = pipeline_output.to_dict()
        result["pipeline_output"] = payload
        runtime_state = result.get("runtime")
        if not isinstance(runtime_state, dict):
            runtime_state = {}
            result["runtime"] = runtime_state
        runtime_state["pipeline_output"] = payload

        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["pipeline_output"] = payload

    def _build_hot_loop_reinjection_packet(
        self,
        *,
        user_query: str,
        session_id: str,
        run_id: str,
        result: Dict[str, Any],
        critic_verdict: Dict[str, Any],
        retry_plan: Dict[str, Any],
        current_execution_path: str,
    ) -> Dict[str, Any]:
        from mica.agentic.core import build_negative_memory_summary
        cognitive_layer = result.get("cognitive_layer") if isinstance(result.get("cognitive_layer"), dict) else {}
        from .execution import build_hot_loop_reinjection_packet
        return build_hot_loop_reinjection_packet(
            user_query=user_query,
            session_id=session_id,
            run_id=run_id,
            result=result,
            critic_verdict=critic_verdict,
            retry_plan=retry_plan,
            current_execution_path=current_execution_path,
        )
    def _get_thermodynamic_snapshot(self, query: str) -> Optional[Dict[str, Any]]:
        """Return a lightweight thermodynamic snapshot for specialist drivers.

        Uses BioRouter if available; returns None when thermodynamic cognition
        is disabled so that downstream code keeps its existing behaviour.
        """
        from .execution import build_thermodynamic_snapshot

        return build_thermodynamic_snapshot(
            query=query,
            thermodynamic_cognition_enabled=bool(
                getattr(getattr(self, "config", None), "enable_thermodynamic_cognition", False)
            ),
            biorouter_obj=self.biorouter,
        )

    def _build_thermodynamic_route_plan(
        self,
        *,
        query: str,
        session_id: str,
        requested_execution_path: str,
    ) -> Dict[str, Any]:
        from .execution import build_thermodynamic_route_plan

        return build_thermodynamic_route_plan(
            query=query,
            session_id=session_id,
            requested_execution_path=requested_execution_path,
            thermodynamic_cognition_enabled=bool(
                getattr(getattr(self, "config", None), "enable_thermodynamic_cognition", False)
            ),
            biorouter_obj=self.biorouter,
            compiled_graph_available=bool(self.compiled_graph),
        )

    async def _emit_thermodynamic_routing_telemetry(
        self,
        *,
        session_id: str,
        run_id: str,
        mode: str,
        route_plan: Dict[str, Any],
    ) -> None:
        from .execution import emit_thermodynamic_routing_telemetry

        await emit_thermodynamic_routing_telemetry(
            session_id=session_id,
            run_id=run_id,
            mode=mode,
            route_plan=route_plan,
            emit_runtime_status_telemetry_fn=self._emit_runtime_status_telemetry,
        )

    def _run_cognitive_layer(
        self,
        *,
        session_id: str,
        run_id: str,
        user_query: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return {
                "hypothesis_competition": {},
                "critic_pass": {},
            }

        self._record_final_result_evidence(session_id=session_id, run_id=run_id, result=result)
        ledger = self._get_or_create_evidence_ledger(session_id, run_id)
        if not hasattr(ledger, "get_entry") or not hasattr(ledger, "get_contradicted_claims"):
            empty_ach = {
                "schema_version": "mica.ach_arbiter.v0",
                "competition_open": False,
                "primary_hypothesis_id": "",
                "leading_hypothesis_ids": [],
                "rival_hypothesis_ids": [],
                "rejected_hypothesis_ids": [],
                "contradiction_pressure": 0.0,
                "entries": [],
                "note": "Cognitive layer skipped because no concrete evidence ledger was available in the current runtime context.",
            }
            empty_critic = {
                "schema_version": "mica.continuous_critic.v0",
                "status": "accept",
                "challenged_claim_ids": [],
                "contradicted_claim_ids": [],
                "unsupported_critical_claim_ids": [],
                "unresolved_rival_hypothesis_ids": [],
                "retry_recommended": False,
                "escalate_critique": False,
                "rationale": [],
                "retry_guidance": "",
                "note": "Continuous critic skipped because no concrete evidence ledger was available in the current runtime context.",
            }
            self._attach_cognitive_layer_verdicts(result=result, ach_state=empty_ach, critic_verdict=empty_critic)
            return {
                "hypothesis_competition": empty_ach,
                "critic_pass": empty_critic,
            }

        ach_state = ACHArbiter().arbitrate(
            query=user_query,
            final_result=final_result,
            ledger=ledger,
        ).to_dict()
        critic_verdict = ContinuousCritic().review(
            query=user_query,
            final_result=final_result,
            ledger=ledger,
            ach_state=ach_state,
        ).to_dict()
        critic_verdict = self._apply_negative_memory_to_critic_verdict(result=result, critic_verdict=critic_verdict)
        self._attach_cognitive_layer_verdicts(
            result=result,
            ach_state=ach_state,
            critic_verdict=critic_verdict,
        )
        return {
            "hypothesis_competition": ach_state,
            "critic_pass": critic_verdict,
        }

    def _build_thermodynamic_retry_plan(
        self,
        *,
        session_id: str,
        result: Dict[str, Any],
        critic_verdict: Dict[str, Any],
        route_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not route_plan.get("enabled") or self.biorouter is None:
            return {"retry": False, "note": "Thermodynamic retry disabled."}

        final_result = result.get("final_result") if isinstance(result.get("final_result"), dict) else {}
        quality = float(final_result.get("scientific_closure_score", result.get("quality_score", 0.0)) or 0.0)
        unsupported_assertion_rate = float(final_result.get("unsupported_assertion_rate", 0.0) or 0.0)
        contradiction_pressure = float(((result.get("cognitive_layer") or {}).get("hypothesis_competition", {}) or {}).get("contradiction_pressure", 0.0) or 0.0)
        unresolved_rivals = len(list(critic_verdict.get("unresolved_rival_hypothesis_ids") or []))
        appeal_regime_active = bool(((result.get("appeal_regime_state") or final_result.get("appeal_regime_state") or {}).get("appeal_regime_active")))
        soft_repulsion_warning_count = len(list(result.get("soft_repulsion_warnings") or final_result.get("soft_repulsion_warnings") or []))
        semantic_consistency = max(
            0.1,
            1.0 - unsupported_assertion_rate - (0.4 * contradiction_pressure) - (0.08 if appeal_regime_active else 0.0),
        )
        complexity_penalty = min(
            1.0,
            contradiction_pressure
            + min(unresolved_rivals / 3.0, 0.5)
            + (0.12 if appeal_regime_active else 0.0)
            + min(0.04 * soft_repulsion_warning_count, 0.16),
        )
        stagnation = min(
            1.0,
            contradiction_pressure
            + (0.35 if int((result.get("provenance") or {}).get("iterations", 0) or 0) <= 1 else 0.0)
            + (0.08 if appeal_regime_active else 0.0)
            + min(0.03 * soft_repulsion_warning_count, 0.12),
        )

        current_soul = CognitiveAttractorState.from_dict(route_plan.get("soul") or {"workflow_id": session_id})
        u_energy = self.biorouter.calculate_hamiltonian_from_scores(
            quality=quality,
            semantic_consistency=semantic_consistency,
            complexity_penalty=complexity_penalty,
        )
        regulated = self.biorouter.regulate_temperature(current_soul, u_energy, stagnation=stagnation)
        if bool(critic_verdict.get("retry_recommended")) and (stagnation >= 0.65 or contradiction_pressure >= 0.45):
            regulated.temperature = max(float(regulated.temperature), 0.62 if self.compiled_graph else 0.58)
            regulated.update_phase()
        retry_execution_path = "langgraph" if self.compiled_graph and regulated.temperature >= 0.55 else "agentic_loop"
        retry = bool(critic_verdict.get("retry_recommended")) and float(regulated.temperature) >= 0.58

        return {
            "retry": retry,
            "retry_execution_path": retry_execution_path,
            "temperature": round(float(regulated.temperature), 4),
            "energy": round(float(regulated.energy), 4),
            "phase": regulated.phase.value,
            "stagnation": round(float(stagnation), 4),
            "escalate_critique": bool(critic_verdict.get("escalate_critique")) or float(regulated.temperature) < 0.35,
            "note": "BioRouter evaluated contradiction pressure and stagnation before deciding whether to reopen execution.",
            "soul": regulated.to_dict(),
        }

    async def _maybe_retry_with_cognitive_critique(
        self,
        *,
        user_query: str,
        mode: str,
        session_id: str,
        run_id: str,
        result: Dict[str, Any],
        current_execution_path: str,
        firewall_verdict: FirewallVerdict,
        route_plan: Dict[str, Any],
    ) -> tuple[Dict[str, Any], str, Dict[str, Any]]:
        cognitive_layer = result.get("cognitive_layer") if isinstance(result.get("cognitive_layer"), dict) else {}
        critic_verdict = cognitive_layer.get("critic_pass") if isinstance(cognitive_layer.get("critic_pass"), dict) else {}
        retry_plan = self._build_thermodynamic_retry_plan(
            session_id=session_id,
            result=result,
            critic_verdict=critic_verdict,
            route_plan=route_plan,
        )

        updated_route_plan = {
            **route_plan,
            "retry_plan": retry_plan,
        }
        self._attach_thermodynamic_route(result=result, route_state=updated_route_plan)

        if not retry_plan.get("retry") or current_execution_path in {"pre_routing_firewall", "direct"}:
            return result, current_execution_path, updated_route_plan

        reinjection_packet = self._build_hot_loop_reinjection_packet(
            user_query=user_query,
            session_id=session_id,
            run_id=run_id,
            result=result,
            critic_verdict=critic_verdict,
            retry_plan=retry_plan,
            current_execution_path=current_execution_path,
        )
        staged_reinjection = self._stage_hot_loop_reinjection_packet(session_id=session_id, packet=reinjection_packet)

        retry_query = user_query
        retry_execution_path = str(retry_plan.get("retry_execution_path") or current_execution_path)
        await self._emit_runtime_status_telemetry(
            session_id=session_id,
            run_id=run_id,
            phase="continuous_critic",
            status="retry",
            details=str(retry_plan.get("note") or "Continuous critic requested one retry."),
            mode=mode,
            severity="warning",
            metrics={
                "temperature": float(retry_plan.get("temperature") or 0.0),
                "stagnation": float(retry_plan.get("stagnation") or 0.0),
            },
        )

        self._maybe_raise_retry_execution_fault(
            retry_execution_path=retry_execution_path,
            reinjection_packet=reinjection_packet,
        )

        if retry_execution_path == "langgraph" and self.compiled_graph:
            retried = await self._execute_with_langgraph(retry_query, mode, session_id, reinjection_packet=reinjection_packet)
        else:
            retry_execution_path = "agentic_loop"
            retried = await self._execute_with_agentic_loop(retry_query, mode, session_id, reinjection_packet=reinjection_packet)

        if isinstance(retried, dict):
            self._promote_staged_hot_loop_reinjection_packet(session_id=session_id, staged_paths=staged_reinjection)
            self._attach_epistemic_firewall_verdict(result=retried, verdict=firewall_verdict)
            retried.setdefault("execution_path", retry_execution_path)
            retried.setdefault("run_id", run_id)
            retried.setdefault("runtime", {})
            if isinstance(retried.get("runtime"), dict):
                retried["runtime"]["retry_of_execution_path"] = current_execution_path
                retried["runtime"]["retry_reason"] = critic_verdict.get("rationale") or []
                retried["runtime"]["hot_loop_reinjection_packet_id"] = reinjection_packet.get("packet_id")
                retried["runtime"]["forced_reinjection"] = True
            retried = self._normalize_final_result_contract(user_query=user_query, result=retried)
            self._attach_epistemic_firewall_verdict(result=retried, verdict=firewall_verdict)
            retried_runtime = retried.get("runtime")
            if not isinstance(retried_runtime, dict):
                retried_runtime = {}
                retried["runtime"] = retried_runtime
            retried_runtime["retry_of_execution_path"] = current_execution_path
            retried_runtime["retry_reason"] = critic_verdict.get("rationale") or []
            retried_runtime["hot_loop_reinjection_packet_id"] = reinjection_packet.get("packet_id")
            retried_runtime["forced_reinjection"] = True
            retried_cognitive = self._run_cognitive_layer(
                session_id=session_id,
                run_id=run_id,
                user_query=user_query,
                result=retried,
            )
            updated_route_plan = {
                **updated_route_plan,
                "preferred_execution_path": retry_execution_path,
                "retry_count": 1,
                "retry_plan": {
                    **retry_plan,
                    "retried": True,
                },
            }
            self._attach_cognitive_layer_verdicts(
                result=retried,
                ach_state=dict(retried_cognitive.get("hypothesis_competition") or {}),
                critic_verdict=dict(retried_cognitive.get("critic_pass") or {}),
            )
            self._attach_hot_loop_reinjection(result=retried, packet=reinjection_packet)
            self._attach_thermodynamic_route(result=retried, route_state=updated_route_plan)
            return retried, retry_execution_path, updated_route_plan

        return result, current_execution_path, updated_route_plan

    def _resolve_user_rag_store(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="user_rag_store",
            candidate_attrs=("_user_rag_store", "timescale_user_rag_store"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.timescale_user_rag_store",
                fromlist=["TimescaleUserRAGStore"],
            ).TimescaleUserRAGStore(),
            label="TimescaleUserRAGStore",
        )

    def _resolve_milvus_user_rag_store(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="milvus_user_rag_store",
            candidate_attrs=("_milvus_user_rag_store", "milvus_store"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.milvus_user_rag_store",
                fromlist=["MilvusUserRAGStore"],
            ).MilvusUserRAGStore(),
            label="MilvusUserRAGStore",
        )

    def _resolve_graph_store(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="graph_store",
            candidate_attrs=("_graph_store", "graphrag_store", "timescale_graphrag_store"),
            factory=lambda: __import__(
                "mica.infrastructure.persistence.timescale_graphrag_store",
                fromlist=["TimescaleGraphRAGStore"],
            ).TimescaleGraphRAGStore(),
            label="TimescaleGraphRAGStore",
        )

    def _resolve_mica_q_multisurface_service(self) -> Any:
        return self._resolve_memory_backend(
            cache_attr="mica_q_multisurface_service",
            candidate_attrs=("_mica_q_multisurface_service", "mica_q_service"),
            factory=lambda: __import__(
                "mica.memory.mica_q_multisurface",
                fromlist=["MICAQMultisurfaceService"],
            ).MICAQMultisurfaceService(
                graph_store=self._resolve_graph_store(),
            ),
            label="MICAQMultisurfaceService",
        )

    def _build_memory_retrieval_planner(self, *, agent_memory: Any, workspace_id: str):
        from mica.infrastructure.persistence.retrieval_planner import RetrievalPlanner

        return RetrievalPlanner(
            session_repository=self._resolve_session_repository(),
            agent_summary_store=getattr(self, "agent_summary_store", None),
            agent_memory=agent_memory,
            user_rag_store=self._resolve_user_rag_store(),
            milvus_user_rag_store=self._resolve_milvus_user_rag_store(),
            graph_store=self._resolve_graph_store(),
            atom_memory=self.atom_memory,
            mica_q_service=self._resolve_mica_q_multisurface_service(),
        )

    @staticmethod
    def _serialize_runtime_consumption_item(item: Any) -> Dict[str, Any]:
        payload: Any = item
        if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")):
            payload = item.to_dict()
        elif hasattr(item, "model_dump") and callable(getattr(item, "model_dump")):
            payload = item.model_dump(mode="json")
        elif hasattr(item, "__dataclass_fields__"):
            payload = asdict(item)
        elif isinstance(item, dict):
            payload = dict(item)
        elif hasattr(item, "__dict__"):
            payload = {
                key: value
                for key, value in vars(item).items()
                if not key.startswith("_")
            }
        else:
            payload = {"value": item}
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            return {"value": str(item)}

    @staticmethod
    def _runtime_consumption_snippet(item: Dict[str, Any]) -> str:
        if not isinstance(item, dict):
            return ""
        content = str(item.get("content") or "").strip()
        if content:
            return _truncate_text(content, max_len=220)
        triplet = " ".join(
            str(part).strip()
            for part in (item.get("subject"), item.get("predicate"), item.get("object"))
            if str(part).strip()
        ).strip()
        if triplet:
            return _truncate_text(triplet, max_len=220)
        edge = " ".join(
            str(part).strip()
            for part in (item.get("source_node"), item.get("relationship"), item.get("target_node"))
            if str(part).strip()
        ).strip()
        if edge:
            return _truncate_text(edge, max_len=220)
        return ""

    def _should_build_runtime_consumption_context(self, query: str) -> bool:
        normalized = " ".join(str(query or "").split())
        if len(normalized) < 8:
            return False
        lowered = normalized.lower()
        keywords = (
            "literature",
            "paper",
            "study",
            "evidence",
            "mechanism",
            "pathway",
            "protein",
            "gene",
            "mutation",
            "disease",
            "graph",
            "fact",
            "citation",
            "pubmed",
            "openalex",
        )
        return any(token in lowered for token in keywords) or bool(re.search(r"\b[A-Z0-9-]{3,10}\b", normalized))

    def _build_runtime_consumption_prompt_block(self, runtime_context: Dict[str, Any]) -> str:
        snippets: List[str] = []
        for bucket in ("atom_facts", "graph_facts", "edge_hits"):
            for item in list(runtime_context.get(bucket) or []):
                snippet = self._runtime_consumption_snippet(item)
                if snippet and snippet not in snippets:
                    snippets.append(snippet)
                if len(snippets) >= 3:
                    break
            if len(snippets) >= 3:
                break
        if not snippets:
            return ""
        counts = (
            f"graph_hits={int(runtime_context.get('graph_hit_count') or 0)}; "
            f"facts={int(runtime_context.get('fact_hit_count') or 0)}"
        )
        lines = [f"Persisted evidence summary ({counts})"]
        for snippet in snippets[:3]:
            lines.append(f"- {snippet}")
        return "\n".join(lines)

    async def _build_runtime_consumption_context(
        self,
        *,
        query: str,
        session_id: Optional[str],
        user_id: Optional[str],
        workspace_id: Optional[str],
        limit: int = 4,
        force: bool = False,
    ) -> Dict[str, Any]:
        from .execution import build_runtime_consumption_context

        return await build_runtime_consumption_context(
            self,
            query=query,
            session_id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
            limit=limit,
            force=force,
        )

    def _annotate_route_plan_with_runtime_consumption(
        self,
        *,
        route_plan: Dict[str, Any],
        runtime_context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        updated = dict(route_plan or {})
        if not isinstance(runtime_context, dict) or not runtime_context:
            return updated
        updated["runtime_consumption"] = {
            "state": str(runtime_context.get("state") or "skipped"),
            "graph_hit_count": int(runtime_context.get("graph_hit_count") or 0),
            "fact_hit_count": int(runtime_context.get("fact_hit_count") or 0),
            "degraded": list(runtime_context.get("degraded") or []),
        }
        if runtime_context.get("graph_hit_count") or runtime_context.get("fact_hit_count"):
            updated["consumed_persisted_signal"] = True
            note = f"Persisted signal consumed before execution: graph_hits={updated['runtime_consumption']['graph_hit_count']}, fact_hits={updated['runtime_consumption']['fact_hit_count']}."
            existing_note = str(updated.get("note") or "").strip()
            updated["note"] = f"{existing_note} {note}".strip()
        return updated

    def _attach_runtime_consumption_context(self, *, result: Dict[str, Any], runtime_context: Optional[Dict[str, Any]]) -> None:
        if not isinstance(result, dict) or not isinstance(runtime_context, dict) or not runtime_context:
            return
        payload = {
            key: value
            for key, value in runtime_context.items()
            if key != "prompt_block"
        }
        result["runtime_consumption"] = payload
        provenance = result.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
            result["provenance"] = provenance
        provenance["runtime_consumption"] = payload
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result.setdefault("consumption_closure", dict(payload.get("consumption_closure") or {}))
            final_result.setdefault("runtime_consumption", payload)

    def _persist_agent_summary(
        self,
        *,
        summary_store: Any,
        entry: Any,
        agent_name: str,
        query: str,
        user_id: str,
        workspace_id: str,
        session_id: Optional[str],
        run_id: Optional[str],
        artifact_path: Optional[str],
    ) -> None:
        if summary_store is None or entry is None:
            return
        try:
            import hashlib
            from mica.memory.contracts import SummaryMutationAction, SummaryMutationEvent

            normalized_query = " ".join((query or "").lower().split())
            digest = hashlib.sha256(
                f"{agent_name}|{user_id}|{workspace_id}|{run_id or ''}|{normalized_query}".encode("utf-8")
            ).hexdigest()[:16]
            summary_id = f"{agent_name}-{digest}"
            summary = summary_store.from_agent_memory_entry(
                entry,
                summary_id=summary_id,
                user_id=user_id,
                workspace_id=workspace_id,
                run_id=run_id or "ad-hoc",
                session_id=session_id,
                artifact_paths=[artifact_path] if artifact_path else [],
            )
            current = summary_store.get_summary(summary_id)
            action = SummaryMutationAction.UPDATE if current is not None else SummaryMutationAction.ADD
            event = SummaryMutationEvent(
                event_id=f"{summary_id}-{action.value.lower()}",
                summary_id=summary_id,
                action=action,
                actor_type="agent",
                actor_id=agent_name,
                scope={
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "run_id": run_id or "ad-hoc",
                },
                old_value=current.to_dict() if current is not None else None,
                new_value=summary.to_dict(),
                reason="subagent_completion",
            )
            summary_store.apply_mutation(summary, event)
        except Exception:
            logger.debug("Agent summary persistence failed for %s", agent_name, exc_info=True)

    async def initialize_async(self):
        """
        Asynchronous initialization of AgenticDriver.
        
        Steps:
        1. Load MCP configuration from JSON
        2. Connect to all enabled MCP servers
        3. List all available MCP tools
        4. Initialize specialist drivers
        5. Setup LangGraph checkpointer
        
        Raises:
            FileNotFoundError: If MCP config file not found
            ConnectionError: If MCP server connection fails
        """
        
        if self._initialized:
            logger.warning("AgenticDriver already initialized - skipping")
            return
        
        logger.info("🚀 Initializing AgenticDriver...")
        
        # Step 1: Load MCP configuration
        if self.config.mcp_enabled:
            await self._load_mcp_config()
        
        # Step 2: Connect to MCP servers
        if self.config.mcp_enabled and self.mcp_config:
            await self._connect_mcp_servers()
        
        # Step 3: List MCP tools
        if self.config.mcp_enabled and self.mcp_sessions:
            await self._list_mcp_tools()
        
        # Step 4: Initialize specialist drivers
        await self._initialize_specialist_drivers()

        # Step 4b: Initialize shared serverless model gateway
        await self._initialize_serverless_models()
        
        # Step 5: Setup checkpointer
        if self.config.use_checkpointing and _ensure_langgraph_runtime():
            await self._setup_checkpointer()
        
        # Step 6: Build LangGraph StateGraph (v3.0)
        if self.config.use_langgraph_stategraph and _ensure_langgraph_runtime():
            await self._build_langgraph_stategraph()
        
        self._initialized = True
        mode = "LangGraph v3.0" if self.compiled_graph else "FSM legacy"
        logger.info(f"✅ AgenticDriver initialized ({mode}): {len(self.mcp_tools)} MCP tools, {len(self.specialist_drivers)} specialist drivers")

    def _build_serverless_model_gateway(self):
        from mica.serverless_models import build_default_serverless_model_gateway

        artifact_root = Path(self.config.checkpoint_dir) / "serverless_model_artifacts"
        gateway = build_default_serverless_model_gateway(
            artifact_base_dir=str(artifact_root)
        )
        for _drv in self.specialist_drivers.values():
            try:
                _drv.serverless_model_gateway = gateway
            except Exception:
                pass
        return gateway

    async def _initialize_serverless_models(self, *, force: bool = False):
        """Initialize shared serverless model gateway for the driver and specialists."""

        if self.serverless_model_gateway is not None:
            return
        if not force and not bool(getattr(self.config, "enable_serverless_gateway_eager", True)):
            logger.info("ServerlessModelGateway eager init skipped by config")
            return

        try:
            self.serverless_model_gateway = self._build_serverless_model_gateway()
            logger.info(
                "✅ ServerlessModelGateway initialized with %d model descriptors",
                len(self.serverless_model_gateway.list_models()),
            )
        except Exception as exc:
            self.serverless_model_gateway = None
            logger.warning("ServerlessModelGateway init skipped (non-fatal): %s", exc)

    def _ensure_serverless_model_gateway(self):
        if self.serverless_model_gateway is None:
            self.serverless_model_gateway = self._build_serverless_model_gateway()
        return self.serverless_model_gateway

    def list_serverless_models(self) -> list[dict[str, Any]]:
        try:
            gateway = self._ensure_serverless_model_gateway()
        except Exception as exc:
            logger.warning("ServerlessModelGateway list bootstrap failed: %s", exc)
            return []
        return [asdict(descriptor) for descriptor in gateway.list_models()]

    async def invoke_serverless_model(
        self,
        *,
        model_id: str,
        inputs: Dict[str, Any],
        metadata: Dict[str, Any] | None = None,
        user_id: str = "agentic-driver",
        session_id: str | None = None,
        run_id: str | None = None,
        requested_by: str = "driver",
        provider_override: str | None = None,
    ):
        try:
            gateway = self._ensure_serverless_model_gateway()
        except Exception as exc:
            raise RuntimeError("ServerlessModelGateway is not initialized") from exc

        request_id = str(uuid.uuid4())
        invocation = ModelInvocationRequest(
            request_id=request_id,
            model_id=model_id,
            user_id=user_id,
            session_id=session_id or request_id,
            run_id=run_id or request_id,
            inputs=dict(inputs),
            metadata=dict(metadata or {}),
            requested_by=requested_by,
            provider_override=provider_override,
        )
        return await gateway.invoke(invocation)
    
    async def _load_mcp_config(self):
        """Load MCP server configuration from JSON file."""
        
        config_path = Path(self.config.mcp_config_path)
        if not config_path.exists():
            fallback_path = Path(__file__).resolve().parents[1] / "config" / "mcp_servers.json"
            if fallback_path.exists():
                logger.info(f"MCP config not found at {config_path}; using fallback {fallback_path}")
                config_path = fallback_path
            else:
                logger.warning(f"MCP config not found: {config_path} - MCP disabled")
                self.config.mcp_enabled = False
                return
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                full_config = json.load(f)
                self.mcp_config = full_config.get("mcpServers", {})
            
            # Filter enabled servers
            enabled_servers = {
                name: config 
                for name, config in self.mcp_config.items()
                if not name.startswith("_comment") and config.get("enabled", False)
            }

            allowlist = getattr(self.config, "mcp_server_allowlist", None)
            if allowlist is not None:
                allowed = {
                    str(name).strip()
                    for name in list(allowlist)
                    if str(name).strip()
                }
                enabled_servers = {
                    name: config
                    for name, config in enabled_servers.items()
                    if name in allowed
                }
            
            self.mcp_config = enabled_servers
            logger.info(f"📦 Loaded MCP config: {len(self.mcp_config)} enabled servers")
            
        except Exception as e:
            logger.error(f"Failed to load MCP config: {e}")
            self.config.mcp_enabled = False
    
    async def _connect_mcp_servers(self):
        """Connect to all enabled MCP servers."""
        
        if not _ensure_mcp_runtime():
            logger.warning("MCP library not available - skipping server connections")
            return
        
        if self._mcp_exit_stack is None:
            self._mcp_exit_stack = AsyncExitStack()
            await self._mcp_exit_stack.__aenter__()

        _CONNECT_TIMEOUT = float(getattr(self.config, "mcp_connect_timeout", 12))

        def _resolve_in_process_server(server_name: str, server_config: dict):
            target = None
            args = list(server_config.get("args") or [])
            if args:
                target = args[0]
            if not target or ":" not in str(target):
                raise ValueError(f"{server_name} missing module:attribute target for IN_PROCESS mode")
            module_name, attribute_name = str(target).split(":", 1)
            module = importlib.import_module(module_name)
            try:
                return getattr(module, attribute_name)
            except AttributeError as exc:
                raise AttributeError(f"{server_name} target '{target}' could not be resolved") from exc

        async def _connect_one(server_name: str, server_config: dict):
            try:
                mode = str(server_config.get("mode") or "").strip().lower()
                command = str(server_config.get("command") or "").strip()

                if mode == "in_process" or command.upper() == "IN_PROCESS":
                    if not FASTMCP_AVAILABLE or FastMCPClient is None:
                        raise RuntimeError("fastmcp is not installed; cannot connect IN_PROCESS MCP server")
                    server_object = _resolve_in_process_server(server_name, server_config)
                    session = await self._mcp_exit_stack.enter_async_context(FastMCPClient(server_object))
                    transport = "in_process"
                else:
                    if not MCP_STDIO_AVAILABLE or ClientSession is None or StdioServerParameters is None or stdio_client is None:
                        raise RuntimeError("stdio MCP client is not installed; cannot connect subprocess MCP server")
                    resolved_command = command
                    if command.lower() in {"python", "python3"}:
                        resolved_command = sys.executable
                    server_params = StdioServerParameters(
                        command=resolved_command,
                        args=server_config.get("args", []),
                        env=server_config.get("env", {})
                    )
                    read, write = await self._mcp_exit_stack.enter_async_context(
                        stdio_client(server_params)
                    )
                    session = await self._mcp_exit_stack.enter_async_context(
                        ClientSession(read, write)
                    )
                    await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT)
                    transport = "stdio"

                self.mcp_sessions[server_name] = {
                    "session": session,
                    "connected_at": datetime.now(timezone.utc),
                    "status": "connected",
                    "transport": transport,
                }
                logger.info(f"✅ Connected to MCP server: {server_name} ({transport})")
            except asyncio.TimeoutError:
                logger.warning(f"⏱ Timeout connecting to {server_name} (>{_CONNECT_TIMEOUT}s) — skipping")
                self.mcp_sessions[server_name] = {
                    "session": None, "connected_at": None,
                    "status": "timeout", "error": f"timeout >{_CONNECT_TIMEOUT}s"
                }
            except Exception as e:
                logger.error(f"❌ Failed to connect to {server_name}: {e}")
                self.mcp_sessions[server_name] = {
                    "session": None, "connected_at": None,
                    "status": "failed", "error": str(e)
                }

        # Keep connection setup in a single task. Some MCP stdio transports
        # rely on anyio cancel scopes that must be exited from the same task
        # that entered the async context manager; using concurrent gather here
        # makes AsyncExitStack cleanup run from a different task and can emit
        # noisy shutdown tracebacks during otherwise successful direct runs.
        for name, cfg in self.mcp_config.items():
            await _connect_one(name, cfg)
    
    async def _list_mcp_tools(self):
        """List all available tools from connected MCP servers."""
        
        all_tools = []
        
        for server_name, session_info in self.mcp_sessions.items():
            if session_info["status"] != "connected":
                continue
            
            try:
                session = session_info["session"]
                tools_result = await session.list_tools()

                # MCP python clients differ: some return an object with `.tools`,
                # some return a bare list.
                tools = getattr(tools_result, "tools", tools_result)
                
                def _tool_to_dict(raw: Any) -> Dict[str, Any]:
                    # Normalize Tool shapes across MCP client versions.
                    if isinstance(raw, dict):
                        name = raw.get("name") or raw.get("tool") or raw.get("id")
                        desc = raw.get("description") or ""
                        schema = raw.get("inputSchema") or raw.get("input_schema") or raw.get("schema") or {}
                        return {
                            "name": f"{server_name}_{name}",
                            "server": server_name,
                            "description": desc,
                            "input_schema": schema,
                        }

                    name = getattr(raw, "name", None)
                    desc = getattr(raw, "description", "")
                    schema = getattr(raw, "inputSchema", None) or getattr(raw, "input_schema", None) or {}

                    if name is None and isinstance(raw, (tuple, list)) and raw:
                        # Best effort: (name, description?, schema?)
                        name = raw[0]
                        if len(raw) > 1 and isinstance(raw[1], str):
                            desc = raw[1]
                        if len(raw) > 2 and isinstance(raw[2], dict):
                            schema = raw[2]

                    return {
                        "name": f"{server_name}_{str(name)}",
                        "server": server_name,
                        "description": str(desc or ""),
                        "input_schema": schema or {},
                    }

                # Add server prefix to tool names
                for tool in tools or []:
                    tool_dict = _tool_to_dict(tool)
                    all_tools.append(tool_dict)
                    
                    # Register with TEA Protocol
                    self.tool_context.register_tool(tool_dict)
                    _emit_audit_event(
                        "mcp_tool_registered",
                        server=server_name,
                        tool=tool_dict.get("name"),
                        has_schema=bool(tool_dict.get("input_schema")),
                    )
                
                logger.info(f"📋 Listed {len(tools or [])} tools from {server_name}")
                
            except Exception as e:
                logger.error(f"Failed to list tools from {server_name}: {e}")
        
        self.mcp_tools = all_tools
        logger.info(f"📊 Total MCP tools: {len(self.mcp_tools)}")
        _emit_audit_event(
            "mcp_tools_discovery_complete",
            total_tools=len(self.mcp_tools),
            servers=sorted({str(t.get("server")) for t in self.mcp_tools if t.get("server")}),
        )
        self._refresh_toolkg_runtime_state()

    def _refresh_toolkg_runtime_state(self) -> None:
        """Build ToolKG registry/inventory, smoke-evaluate routing, and bootstrap NRU gateway."""
        try:
            from mica.toolkg.schema import ToolRegistry
            from mica.toolkg.capability_inventory import InventoryBuilder, MCPToolDescriptor
            from mica.toolkg.golden_fixtures import build_default_evaluator
            from mica.toolkg.normalized_research_unit import NRUGateway

            tools_by_server: Dict[str, List[Dict[str, Any]]] = {}
            descriptors: List[MCPToolDescriptor] = []
            for t in self.mcp_tools:
                server = str(t.get("server", "unknown"))
                server_id = f"mcp_{server}"
                raw_name = str(t.get("name", ""))
                if raw_name.startswith(f"{server}_"):
                    raw_name = raw_name[len(server) + 1 :]
                tools_by_server.setdefault(server_id, []).append(
                    {
                        "name": raw_name,
                        "description": str(t.get("description", "")),
                        "inputSchema": t.get("input_schema", {}) or {},
                    }
                )
                descriptors.append(
                    MCPToolDescriptor(
                        tool_id=f"{server_id}.{raw_name}",
                        server_id=server_id,
                        name=raw_name,
                        description=str(t.get("description", "")),
                        input_schema=t.get("input_schema", {}) or {},
                        output_schema={},
                    )
                )

            registry = ToolRegistry.from_mcp_list_tools(tools_by_server)
            registry = InventoryBuilder().merge_into_registry(registry, descriptors)
            self._toolkg_registry = registry
            self._nru_gateway = NRUGateway(registry)

            # P3-1: Skip fixture smoke evaluation in production — fixtures are
            # test-time assets that should not execute in deployed environments.
            _env = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower()
            if _env in ("prod", "production"):
                self._toolkg_eval_summary = {"skipped": "production_env"}
                logger.info("ToolKG smoke fixtures skipped in production")
            else:
                report = build_default_evaluator(registry).evaluate_by_tag("smoke")
                self._toolkg_eval_summary = {
                    "total": report.total,
                    "passed": report.passed,
                    "failed": report.failed,
                    "pass_rate": report.pass_rate,
                }
                logger.info(
                    "ToolKG smoke fixtures: %d/%d passed (%.1f%%)",
                    report.passed,
                    report.total,
                    report.pass_rate * 100.0,
                )
        except Exception as exc:
            self._toolkg_registry = None
            self._nru_gateway = None
            self._toolkg_eval_summary = {"error": str(exc)}
            logger.debug("ToolKG runtime state refresh skipped: %s", exc)
    
    async def _initialize_specialist_drivers(self):
        """Initialize specialist drivers (BioDynamo, Alchemist, SMIC)."""
        
        if self.config.enable_biodynamo and "biodynamo" not in self.specialist_drivers:
            try:
                from .biodynamo_driver import BioDynamoDriver  # lazy — heavy ML/MD deps
                biodynamo = BioDynamoDriver(
                    config={"checkpoint_dir": self.config.checkpoint_dir}
                )
                self.specialist_drivers["biodynamo"] = biodynamo
                logger.info("✅ BioDynamoDriver initialized")
            except Exception as e:
                logger.error(f"Failed to initialize BioDynamoDriver: {e}")
        
        if self.config.enable_alchemist and "alchemist" not in self.specialist_drivers:
            try:
                from .alchemist_driver import AlchemistDriver  # lazy — heavy ML deps
                alchemist = AlchemistDriver(
                    config={"checkpoint_dir": self.config.checkpoint_dir}
                )
                self.specialist_drivers["alchemist"] = alchemist
                logger.info("✅ AlchemistDriver initialized")
            except Exception as e:
                logger.error(f"Failed to initialize AlchemistDriver: {e}")
        
        if self.config.enable_smic and "smic" not in self.specialist_drivers:
            try:
                from .smic_driver import SMICDriver  # lazy — graph/pocket deps
                smic = SMICDriver(
                    config={"checkpoint_dir": self.config.checkpoint_dir}
                )
                self.specialist_drivers["smic"] = smic
                logger.info("✅ SMICDriver initialized")
            except Exception as e:
                logger.error(f"Failed to initialize SMICDriver: {e}")

        # GAP-4 fix: build a SimpleAgentHub so every specialist can call siblings.
        try:
            from mica.drivers.agent_hub import SimpleAgentHub
            hub = SimpleAgentHub(drivers=self.specialist_drivers)
            for _drv in self.specialist_drivers.values():
                try:
                    _drv.agent_hub = hub
                except Exception:
                    pass  # read-only attrs on some stubs — non-fatal
            logger.info("✅ SimpleAgentHub injected into %d specialist drivers", len(self.specialist_drivers))
        except Exception as _hub_exc:
            logger.warning("AgentHub injection failed (non-fatal): %s", _hub_exc)

        # ── V2 Tier-2: Initialize ModalSpecialistPool ────────────────────
        try:
            from mica.sandbox.specialist_pool import ModalSpecialistPool
            self._specialist_pool = ModalSpecialistPool()
            logger.info("✅ ModalSpecialistPool initialized (Tier-2 V2 architecture)")
        except Exception as _pool_exc:
            self._specialist_pool = None  # type: ignore[assignment]
            logger.warning("ModalSpecialistPool init skipped (non-fatal): %s", _pool_exc)
    
    async def _setup_checkpointer(self):
        """Setup LangGraph checkpointer for fault-tolerant execution."""
        
        if not _ensure_langgraph_runtime():
            logger.warning("LangGraph not available - checkpointing disabled")
            return
        
        checkpoint_dir = Path(self.config.checkpoint_dir)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        
        use_sqlite = os.getenv("USE_SQLITE_CHECKPOINT", "0").lower() in {"1", "true", "yes"}

        if use_sqlite:
            # Block SQLite checkpointer in production — use Postgres-backed saver.
            _env = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower()
            if _env in ("prod", "production"):
                raise RuntimeError(
                    "USE_SQLITE_CHECKPOINT is FORBIDDEN in production. "
                    "Use a PostgreSQL-backed checkpointer instead."
                )
            # Prefer async sqlite saver when available.
            if AsyncSqliteSaver is not None:
                try:
                    db_path = checkpoint_dir / "agentic_driver.db"
                    cm = AsyncSqliteSaver.from_conn_string(str(db_path))
                    self._checkpointer_cm = cm
                    self.checkpointer = await cm.__aenter__()
                    logger.info(f"✅ AsyncSqliteSaver initialized: {db_path}")
                    return
                except Exception as e:
                    logger.warning(f"AsyncSqliteSaver failed: {e} - falling back")
                    self.checkpointer = None
                    self._checkpointer_cm = None

            logger.warning("USE_SQLITE_CHECKPOINT set but AsyncSqliteSaver is unavailable")

        # Keep checkpointing optional: run graph without persistence if no saver.
        self.checkpointer = None
    
    async def _build_langgraph_stategraph(self):
        """
        Build LangGraph StateGraph (v3.0 SOTA Architecture).
        
        Implements:
        - Stateless nodes (no self.attribute mutation)
        - Conditional edges (quality gating)
        - MSRP phase nodes
        - Proactive monitoring loop (Phase 6)
        
        Based on: AGENTICDRIVER_V3_BLUEPRINT.md §§285-372
        """
        
        if not _ensure_langgraph_runtime():
            logger.warning("LangGraph not available - StateGraph disabled")
            return
        
        logger.info("🔨 Building LangGraph StateGraph (v3.0)...")
        
        # Create StateGraph
        self.graph = StateGraph(MICAState)
        
        # ============================================
        # ENTRY NODE
        # ============================================
        self.graph.add_node("initialize", self._node_initialize)
        self.graph.set_entry_point("initialize")
        
        # ============================================
        # THERMODYNAMIC NODES
        # ============================================
        if self.config.enable_thermodynamic_cognition:
            self.graph.add_node("thermostat_init", self._node_thermostat)
            self.graph.add_node("thermostat_loop", self._node_thermostat)
        
        # ============================================
        # ANALYSIS & ROUTING NODES
        # ============================================
        self.graph.add_node("analyze", self._node_analyze)
        self.graph.add_node("route", self._node_route)
        self.graph.add_node("decompose", self._node_decompose)
        self.graph.add_node("assign", self._node_assign)
        
        # ============================================
        # EXECUTION NODES
        # ============================================
        self.graph.add_node("execute", self._node_execute)
        
        # ============================================
        # QUALITY GATE NODE (Critical Control Point)
        # ============================================
        self.graph.add_node("quality_gate", self._node_quality_gate)
        
        # ============================================
        # SYNTHESIS NODE
        # ============================================
        self.graph.add_node("synthesize", self._node_synthesize)
        
        # ============================================
        # PROACTIVE MONITORING NODE (Phase 6)
        # ============================================
        if self.config.enable_proactive_monitoring_node:
            self.graph.add_node("proactive_monitor", self._node_proactive_monitor)
        
        # ============================================
        # EDGES (LINEAR FLOW)
        # ============================================
        if self.config.enable_thermodynamic_cognition:
            self.graph.add_edge("initialize", "thermostat_init")
            self.graph.add_edge("thermostat_init", "analyze")
        else:
            self.graph.add_edge("initialize", "analyze")
            
        self.graph.add_edge("analyze", "route")
        self.graph.add_edge("route", "decompose")
        self.graph.add_edge("decompose", "assign")
        self.graph.add_edge("assign", "execute")
        self.graph.add_edge("execute", "quality_gate")
        
        # ============================================
        # CONDITIONAL EDGE: QUALITY GATE (v3.0 SOTA)
        # ============================================
        if self.config.enable_conditional_quality_gates:
            # Determine target for iteration based on thermodynamics
            iterate_target = "thermostat_loop" if self.config.enable_thermodynamic_cognition else "execute"
            
            self.graph.add_conditional_edges(
                "quality_gate",
                self._router_quality_gate,
                {
                    "continue": "synthesize",      # Quality sufficient
                    "iterate": iterate_target,      # Re-execute with feedback
                    "escalate": "synthesize"        # Max iterations, force completion
                }
            )
        else:
            # Fallback: always continue
            self.graph.add_edge("quality_gate", "synthesize")
            
        # Connect thermostat_loop back to execute
        if self.config.enable_thermodynamic_cognition:
            self.graph.add_edge("thermostat_loop", "execute")
        
        # ============================================
        # PROACTIVE MONITORING LOOP (Phase 6)
        # ============================================
        if self.config.enable_proactive_monitoring_node:
            self.graph.add_edge("synthesize", "proactive_monitor")
            
            # Conditional: spawn new research or terminate
            self.graph.add_conditional_edges(
                "proactive_monitor",
                self._router_proactive_monitor,
                {
                    "spawn_research": "initialize",  # Auto-generate new workflow
                    "end": END                        # Terminate
                }
            )
        else:
            # Direct termination
            self.graph.add_edge("synthesize", END)
        
        # ============================================
        # COMPILE GRAPH
        # ============================================
        if self.checkpointer:
            self.compiled_graph = self.graph.compile(checkpointer=self.checkpointer)
            logger.info("✅ LangGraph StateGraph compiled (with checkpointing)")
        else:
            self.compiled_graph = self.graph.compile()
            logger.info("✅ LangGraph StateGraph compiled (no checkpointing)")
    
    # ========================================================================
    # MCP TOOL INTEGRATION
    # ========================================================================

    def get_mcp_tools_for_claude(self) -> List[Dict[str, Any]]:
        """Convert MCP tools to Anthropic Claude format."""
        return format_tools_for_claude(self.mcp_tools)
    
    def get_mcp_tools_for_openai(self) -> List[Dict[str, Any]]:
        """Convert MCP tools to OpenAI function calling format."""
        return format_tools_for_openai(self.mcp_tools)
    
    async def call_mcp_tool(
        self, 
        server_name: str, 
        tool_name: str, 
        arguments: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Call an MCP tool on a specific server.
        
        Args:
            server_name: Name of MCP server (e.g., "uniprot", "pubmed")
            tool_name: Name of tool (without server prefix)
            arguments: Tool arguments as dict
            
        Returns:
            Tool execution result
            
        Raises:
            ValueError: If server not connected or tool not found
        """
        saga_session = session_id or "unknown"
        saga_event_id = str(uuid.uuid4())
        saga_started_at = datetime.now(timezone.utc)

        # ── 1. Normalize args + attribution ──────────────────────────
        call_args = _normalize_call_args_fn(arguments)
        _inject_attribution_fn(
            call_args, self.mcp_tools, server_name, tool_name, saga_session,
            user_id=_current_user_id_var.get(),
            bucket=_current_bucket_var.get(),
        )

        # ── 2. Before-tool hooks ─────────────────────────────────────
        await self._run_tool_hooks(
            "before_tool", server=server_name, tool=tool_name,
            arguments=call_args, session_id=saga_session,
        )

        # ── 3. Server connection check ───────────────────────────────
        if server_name not in self.mcp_sessions:
            raise ValueError(f"Server not connected: {server_name}")
        session_info = self.mcp_sessions[server_name]
        if session_info["status"] != "connected":
            raise ValueError(f"Server not connected: {server_name} (status: {session_info['status']})")

        # ── 4. Saga BEGIN ────────────────────────────────────────────
        try:
            args_preview = _truncate_text(
                _redact_text(json.dumps(call_args, ensure_ascii=False, default=str)),
                max_len=1000,
            )
            await self._append_saga_event(
                session_id=saga_session,
                event=_build_saga_begin_event_fn(saga_event_id, server_name, tool_name, args_preview, saga_started_at),
            )
        except Exception:
            pass

        # helper: emit abort saga + after-tool hooks + return payload
        async def _abort(payload, *, status="blocked", saga_extra=None):
            try:
                await self._append_saga_event(
                    session_id=saga_session,
                    event=_build_saga_abort_event_fn(
                        saga_event_id, server_name, tool_name, saga_started_at,
                        status=status, extra=saga_extra,
                    ),
                )
            except Exception:
                pass
            await self._run_tool_hooks(
                "after_tool", server=server_name, tool=tool_name,
                arguments=call_args, session_id=saga_session, result=payload,
            )
            return payload

        # ── 5. Security gate ─────────────────────────────────────────
        risk = None
        try:
            risk, should_block = _run_security_gate_fn(
                self.security_analyzer, server_name, tool_name, call_args,
                max_risk_fn=_max_risk,
            )
            if should_block:
                return await _abort(
                    _build_blocked_payload_fn(
                        server_name, tool_name,
                        error="Blocked MCP tool call by security policy",
                        extra={"security_risk": _security_risk_to_dict(risk)},
                    ),
                    saga_extra={"blocked_by": "security", "security_risk": _security_risk_to_dict(risk)},
                )
        except Exception:
            risk = None

        # ── 6. Governance gate ───────────────────────────────────────
        from .execution import execute_mcp_retry_loop, run_mcp_governance_circuit_precheck

        precheck_result = await run_mcp_governance_circuit_precheck(
            confirmation_policy=self.confirmation_policy,
            config_obj=self.config,
            server_name=server_name,
            tool_name=tool_name,
            risk=risk,
            governance_decision_cls=GovernanceDecision,
            cost_estimate_cls=CostEstimate,
            run_governance_gate_fn=_run_governance_gate_fn,
            security_risk_to_dict_fn=_security_risk_to_dict,
            check_circuit_breaker_fn=_check_circuit_breaker_fn,
            circuit_state=self._mcp_circuit_state,
            circuit_lock=self._mcp_circuit_lock,
            build_blocked_payload_fn=_build_blocked_payload_fn,
            build_confirmation_payload_fn=_build_confirmation_payload_fn,
            abort_fn=_abort,
            logger_obj=logger,
        )
        if precheck_result is not None:
            return precheck_result

        async def _after_tool_success(payload: Dict[str, Any]) -> None:
            await self._run_tool_hooks(
                "after_tool", server=server_name, tool=tool_name,
                arguments=call_args, session_id=saga_session, result=payload,
            )

        # ── 8. Retry loop ────────────────────────────────────────────
        return await execute_mcp_retry_loop(
            session_obj=session_info["session"],
            server_name=server_name,
            tool_name=tool_name,
            call_args=call_args,
            config_obj=self.config,
            saga_session=saga_session,
            saga_event_id=saga_event_id,
            saga_started_at=saga_started_at,
            risk=risk,
            normalize_mcp_call_tool_result_fn=self._normalize_mcp_call_tool_result,
            build_success_payload_fn=_build_success_payload_fn,
            build_error_payload_fn=_build_error_payload_fn,
            build_saga_commit_event_fn=_build_saga_commit_event_fn,
            build_saga_retry_event_fn=_build_saga_retry_event_fn,
            security_risk_to_dict_fn=_security_risk_to_dict,
            truncate_text_fn=_truncate_text,
            redact_text_fn=_redact_text,
            append_saga_event_fn=self._append_saga_event,
            after_tool_success_fn=_after_tool_success,
            abort_fn=_abort,
            build_retry_config_fn=_build_retry_config_fn,
            compute_backoff_sleep_fn=_compute_backoff_sleep_fn,
            circuit_breaker_on_success_fn=_circuit_breaker_on_success_fn,
            circuit_breaker_on_failure_fn=_circuit_breaker_on_failure_fn,
            circuit_state=self._mcp_circuit_state,
            circuit_lock=self._mcp_circuit_lock,
            nru_gateway_obj=self._nru_gateway,
            logger_obj=logger,
        )


    def _normalize_mcp_call_tool_result(self, raw: Any) -> Dict[str, Any]:
        """Normalize MCP tool results across client versions."""
        return _normalize_mcp_call_tool_result_fn(raw)
    
    # ========================================================================
    # HIERARCHICAL REASONING & ORCHESTRATION
    # ========================================================================
    
    async def process_agentic_prompt(
        self,
        user_query: str,
        mode: str = "production",
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        bucket: Optional[str] = None,
        workspace_id: Optional[str] = None,
        provider_id: str = "anthropic",
        model_id: Optional[str] = None,
        depth_preset: Optional[str] = None,
        execution_path_override: Optional[str] = None,
        output_contract: str = "default",
    ) -> Dict[str, Any]:
        """🧠 Main entry point for agentic workflow execution.
        
        v3.0: Uses LangGraph StateGraph if enabled
        v2.0 fallback: Uses FSM workflow
        """
        from .execution.agentic_prompt_executor import execute_agentic_prompt
        return await execute_agentic_prompt(
            driver_self=self,
            user_query=user_query,
            mode=mode,
            session_id=session_id,
            user_id=user_id,
            bucket=bucket,
            workspace_id=workspace_id,
            provider_id=provider_id,
            model_id=model_id,
            depth_preset=depth_preset,
            execution_path_override=execution_path_override,
            output_contract=output_contract,
        )

    async def inject_mcp_resources_into_query(self, user_query: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Materialize MCP resources and prepend them as controlled context.

        MVP goals:
        - Deterministic policy triggers from MCP config
        - Redaction + truncation applied in the fabric layer
        - Best-effort behavior (never blocks core workflow)
        """
        from .execution import inject_mcp_resources_into_query as _inject_mcp
        return await _inject_mcp(
            user_query,
            config_obj=self.config,
            mcp_sessions=self.mcp_sessions,
            mcp_config=self.mcp_config,
            bridge_obj=self.bridge,
            format_lmp_context_fn=self._format_lmp_context_for_injection,
            should_inject_institutional_fn=self._should_inject_institutional_memory,
            consult_institutional_fn=self._consult_institutional_memory,
            format_institutional_fn=self._format_institutional_memory_context,
        )

    def _format_lmp_context_for_injection(self, bio_ctx: Any, max_chars: int = 2000) -> str:
        """Format LMP biological context as compact text for pre-reasoning injection.

        Produces a dense block (Ôëñ max_chars) containing keywords, domains, PTMs,
        function summary, key interactions, PDB xrefs, and cited paper count ÔÇö
        everything the LLM needs to reason about this protein from token 0.
        """
        _name = getattr(bio_ctx, "protein_name", None) or getattr(bio_ctx, "uniprot_id", "?")
        _uid = getattr(bio_ctx, "uniprot_id", None) or ""
        parts: List[str] = [f"## {_name} ({_uid})" if _uid else f"## {_name}"]

        kw = getattr(bio_ctx, "keywords", None)
        if kw:
            parts.append(f"Keywords: {', '.join(str(k) for k in kw[:15])}")

        domains = getattr(bio_ctx, "domains", None)
        if domains:
            domain_strs = []
            for d in domains[:8]:
                domain_strs.append(d.name if hasattr(d, "name") else str(d))
            parts.append(f"Domains: {', '.join(domain_strs)}")

        ptms = getattr(bio_ctx, "ptms", None)
        if ptms:
            ptm_strs = []
            for p in ptms[:8]:
                ptm_strs.append(p.description if hasattr(p, "description") else str(p))
            parts.append(f"PTMs: {', '.join(ptm_strs)}")

        comments = getattr(bio_ctx, "comments", {}) or {}
        func_cmt = (comments.get("FUNCTION") or [""])[0]
        if func_cmt:
            parts.append(f"Function: {str(func_cmt)[:500]}")

        subunit_cmt = (comments.get("SUBUNIT") or [""])[0]
        if subunit_cmt:
            parts.append(f"Subunit: {str(subunit_cmt)[:200]}")

        # Extract PubMed ID count for awareness
        _pmid_re = re.compile(r"PubMed:(\d{6,9})")
        _all_pmids: set = set()
        for _cmt_list in comments.values():
            for _cmt_text in (_cmt_list or []):
                _all_pmids.update(_pmid_re.findall(str(_cmt_text)))
        if _all_pmids:
            parts.append(f"Cited papers: {len(_all_pmids)} PubMed IDs")

        # Cross-references (PDB structures)
        xrefs = getattr(bio_ctx, "cross_references", None) or getattr(bio_ctx, "xrefs", None)
        if isinstance(xrefs, dict):
            pdb_ids = xrefs.get("PDB", [])
            if pdb_ids:
                parts.append(f"PDB structures: {', '.join(str(x) for x in pdb_ids[:6])}")

        return "\n".join(parts)[:max_chars]


    def _should_inject_institutional_memory(self, user_query: str) -> bool:
        if not bool(getattr(self.config, "mcp_resources_nlp_enabled", False)):
            return False
        if str(os.getenv("MICA_ENABLE_INSTITUTIONAL_MEMORY_BRIDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
            return False
        from mica.agentic.tools.memory_search import should_auto_consult_institutional_memory

        return should_auto_consult_institutional_memory(user_query)

    def _format_institutional_memory_context(self, search_result: Dict[str, Any], *, query: str) -> str:
        context = str(search_result.get("context") or "").strip()
        if context:
            return context
        status = str(search_result.get("status") or "unknown")
        reason = str(search_result.get("reason") or "consultation returned no structured context")
        return (
            "[INSTITUTIONAL MEMORY]\n"
            f"query={query}\n"
            f"status={status}\n"
            f"reason={reason}"
        )

    async def _consult_institutional_memory(self, user_query: str, *, session_id: str = "") -> Dict[str, Any]:
        from mica.agentic.tools.memory_search import MicaInstitutionalMemorySearch

        memory_search = MicaInstitutionalMemorySearch()
        return await memory_search.search(query=user_query, limit=5, session_id=session_id or None)

    # ====================================================================
    # CONVERSATION LOGGING
    # ====================================================================

    def _saga_log_path(self, session_id: str) -> Path:
        return saga_log_path(self.config.checkpoint_dir, session_id, getattr(self.config, "saga_log_dirname", None))

    async def _append_saga_event(self, *, session_id: str, event: Dict[str, Any]) -> None:
        await append_saga_event(
            checkpoint_dir=self.config.checkpoint_dir,
            saga_log_dirname=getattr(self.config, "saga_log_dirname", None),
            saga_log_enabled=getattr(self.config, "saga_log_enabled", True),
            saga_log_max_bytes=int(getattr(self.config, "saga_log_max_bytes", 5_000_000) or 5_000_000),
            saga_log_lock=self._saga_log_lock,
            timescale_appender=self._append_saga_event_timescale,
            session_id=session_id,
            event=event,
        )

    async def _append_saga_event_timescale(self, *, session_id: str, event: Dict[str, Any]) -> None:
        store = await self._get_timescale_store()
        await append_saga_event_timescale(
            session_id=session_id,
            event=event,
            timescale_store=store,
            session_run_ids=self._session_run_ids,
        )

    async def _get_timescale_store(self) -> Optional[TimescaleEventStore]:
        store, failed = await get_timescale_store(self._timescale_store, self._timescale_store_failed)
        self._timescale_store = store
        self._timescale_store_failed = failed
        return store

    def _snapshot_dir(self, session_id: str) -> Path:
        return snapshot_dir(self.config.checkpoint_dir, session_id, getattr(self.config, "snapshots_dirname", None))

    def _sha256_file(self, path: Path) -> str:
        return sha256_file(path)

    def _run_manifest_dir(self, session_id: str) -> Path:
        return run_manifest_dir(self.config.checkpoint_dir, session_id, getattr(self.config, "run_manifest_dirname", None))

    def _best_effort_git_info(self) -> Dict[str, Any]:
        """Collect git commit/dirty status if repo metadata is available."""
        return best_effort_git_info()

    def _best_effort_versions(self) -> Dict[str, Any]:
        return best_effort_versions()

    def _write_run_manifest(
        self,
        *,
        session_id: str,
        mode: str,
        started_at: datetime,
        finished_at: datetime,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> Optional[Path]:
        """Write a minimal provenance manifest for this run."""
        manifest_path = write_run_manifest(
            checkpoint_dir=self.config.checkpoint_dir,
            run_manifest_dirname=getattr(self.config, "run_manifest_dirname", None),
            run_manifest_enabled=getattr(self.config, "run_manifest_enabled", True),
            session_id=session_id,
            mode=mode,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            error=error,
            session_run_ids=self._session_run_ids,
            conversation_log_path_fn=self._conversation_log_path,
            saga_log_path_fn=self._saga_log_path,
            sha256_file_fn=self._sha256_file,
            mcp_config_path=getattr(self.config, "mcp_config_path", "") or "",
            timescale_appender=self._append_saga_event_timescale,
        )
        self._sync_run_artifact_to_gcs(
            local_path=manifest_path,
            session_id=session_id,
            run_id=self._session_run_ids.get(session_id, session_id),
            filename="run_manifest.json",
        )
        return manifest_path

    def _best_effort_saga_mcp_metrics(self, session_id: str) -> Dict[str, Any]:
        """Summarize MCP execution outcomes by scanning the saga log (best-effort)."""
        return best_effort_saga_mcp_metrics(
            checkpoint_dir=self.config.checkpoint_dir,
            session_id=session_id,
            saga_log_dirname=getattr(self.config, "saga_log_dirname", None),
        )

    def _write_report_card(
        self,
        *,
        session_id: str,
        mode: str,
        started_at: datetime,
        finished_at: datetime,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> Optional[Path]:
        """Write a lightweight per-run evaluation report card."""
        card_path = write_report_card(
            checkpoint_dir=self.config.checkpoint_dir,
            run_manifest_dirname=getattr(self.config, "run_manifest_dirname", None),
            report_card_enabled=getattr(self.config, "report_card_enabled", True),
            session_id=session_id,
            mode=mode,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            error=error,
            conversation_log_path_fn=self._conversation_log_path,
            saga_log_path_fn=self._saga_log_path,
            sha256_file_fn=self._sha256_file,
            saga_mcp_metrics_fn=self._best_effort_saga_mcp_metrics,
            mcp_enabled=getattr(self.config, "mcp_enabled", False),
        )
        self._sync_run_artifact_to_gcs(
            local_path=card_path,
            session_id=session_id,
            run_id=self._session_run_ids.get(session_id, session_id),
            filename="report_card.json",
        )
        return card_path

    async def save_session_snapshot(self, *, session_id: str, label: str, overwrite: bool = False) -> Path:
        """Save a named snapshot of session artifacts (currently: conversation log)."""
        return await _save_session_snapshot_fn(
            snapshots_enabled=getattr(self.config, "snapshots_enabled", True),
            checkpoint_dir=self.config.checkpoint_dir,
            snapshots_dirname=getattr(self.config, "snapshots_dirname", None),
            session_id=session_id,
            label=label,
            overwrite=overwrite,
            conversation_log_path_fn=self._conversation_log_path,
            saga_log_path_fn=self._saga_log_path,
            append_saga_event_fn=self._append_saga_event,
        )

    async def restore_session_snapshot(self, *, session_id: str, label: str) -> Path:
        """Restore a named snapshot (currently: conversation log)."""
        return await _restore_session_snapshot_fn(
            snapshots_enabled=getattr(self.config, "snapshots_enabled", True),
            checkpoint_dir=self.config.checkpoint_dir,
            snapshots_dirname=getattr(self.config, "snapshots_dirname", None),
            session_id=session_id,
            label=label,
            conversation_log_path_fn=self._conversation_log_path,
            append_saga_event_fn=self._append_saga_event,
        )

    def _conversation_log_path(self, session_id: str) -> Path:
        return conversation_log_path(self.config.checkpoint_dir, session_id, self.config.conversation_log_dirname)

    def _conversation_artifact_dir(self, session_id: str) -> Path:
        artifact_dir = self._conversation_log_path(session_id).with_suffix("")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    # ── GCS artifact sync helpers ──────────────────────────────────

    def _ensure_gcs_sync(self, user_id: Optional[str] = None) -> Optional[DriverArtifactSync]:
        """Return (and lazily create) the GCS artifact sync for the current user."""
        uid = (user_id or _current_user_id_var.get(None) or "").strip()
        if not uid:
            return None
        sync = self._gcs_artifact_sync
        if sync is not None and sync._user_id == uid:
            return sync
        self._gcs_artifact_sync = DriverArtifactSync(user_id=uid)
        return self._gcs_artifact_sync

    def _sync_run_artifact_to_gcs(
        self,
        *,
        local_path: Optional[Path],
        session_id: str,
        run_id: str,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """Best-effort upload a local artifact to GCS (run-scoped)."""
        if local_path is None:
            return None
        sync = self._ensure_gcs_sync()
        if sync is None:
            return None
        try:
            return sync.sync_file(
                local_path=Path(local_path),
                session_id=session_id,
                run_id=run_id,
                filename=filename,
            )
        except Exception as exc:
            logger.debug("GCS sync failed for %s: %s", local_path, exc)
            return None

    def _sync_session_artifact_to_gcs(
        self,
        *,
        local_path: Optional[Path],
        session_id: str,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """Best-effort upload a local artifact to GCS (session-scoped)."""
        if local_path is None:
            return None
        sync = self._ensure_gcs_sync()
        if sync is None:
            return None
        try:
            return sync.sync_session_file(
                local_path=Path(local_path),
                session_id=session_id,
                filename=filename,
            )
        except Exception as exc:
            logger.debug("GCS session sync failed for %s: %s", local_path, exc)
            return None

    def _safe_result_for_log(self, result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return safe_result_for_log(result)

    def _stringify_message_content(self, content: Any) -> Optional[str]:
        return stringify_message_content(content)

    @staticmethod
    def _sanitize_runtime_label(value: Any) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:/-]+", "_", str(value or "").strip())
        return text[:160]

    def _ensure_atom_memory_ready(self) -> Optional[Any]:
        if self.atom_memory is not None:
            return self.atom_memory
        if not getattr(self.config, "enable_atom_memory", False):
            return None
        if self._atom_memory_init_attempted:
            return self.atom_memory

        self._atom_memory_init_attempted = True
        try:
            atom_memory, init_error = _build_atom_memory_runtime(self.config)
            self.atom_memory = atom_memory
            self._atom_memory_init_error = init_error
            if init_error:
                logger.warning("%s; continuing without ATOM memory", init_error)
        except Exception as exc:  # pragma: no cover - defensive lazy init
            self._atom_memory_init_error = str(exc)
            self.atom_memory = None
            logger.warning("Lazy ATOM initialization failed; continuing without ATOM memory: %s", exc)
        return self.atom_memory

    def _configured_provider_ids(self) -> Tuple[List[str], Optional[str]]:
        try:
            from mica.agentic.core import ProviderRegistry

            registry = ProviderRegistry.from_env()
            return sorted(set(registry.provider_ids or [])), None
        except Exception as exc:
            return [], str(exc)

    def _runtime_storage_snapshot(self) -> Dict[str, Any]:
        prod_env = (os.getenv("MICA_ENV") or os.getenv("ENVIRONMENT") or "").lower() in {
            "prod",
            "production",
        }
        bucket = self._sanitize_runtime_label(_current_bucket_var.get(""))
        workspace_id = self._sanitize_runtime_label(self._workspace_id_var.get(""))
        checkpoint_dir = str(Path(str(getattr(self.config, "checkpoint_dir", "./.checkpoints") or "./.checkpoints")).resolve())

        # Probe the GCS artifact sync for cloud backend status.
        gcs_sync = self._ensure_gcs_sync()
        if gcs_sync is not None and gcs_sync.is_cloud_ready:
            cloud_info = gcs_sync.storage_snapshot()
            return {
                "status": "ok",
                "artifact_backend": cloud_info.get("artifact_backend", "gcs_dual_write"),
                "cloud_backend_configured": True,
                "active": True,
                "bucket": cloud_info.get("bucket") or bucket or None,
                "workspace_id": workspace_id or None,
                "checkpoint_dir": checkpoint_dir,
                "gcs_object_prefix": cloud_info.get("object_prefix", "driver_runs/"),
                "fallback_flags": [],
                "notes": [
                    "Driver artifacts are dual-written: local checkpoint + GCS user bucket.",
                    "GCS layout: driver_runs/{session_id}/runs/{run_id}/{artifact}",
                ],
            }

        return {
            "status": "failed" if prod_env else "degraded",
            "artifact_backend": "local_filesystem",
            "cloud_backend_configured": False,
            "active": not prod_env,
            "bucket": bucket or None,
            "workspace_id": workspace_id or None,
            "checkpoint_dir": checkpoint_dir,
            "fallback_flags": ["artifact_storage_local_only"],
            "notes": [
                "Driver artifacts are persisted under the local checkpoint directory.",
                "GCS backend not available — cloud sync disabled.",
            ],
        }

    def _runtime_skill_overrides(self) -> Tuple[List[str], bool, bool]:
        explicit_ids = [
            item.strip()
            for item in str(os.getenv("MICA_ACTIVE_SKILLS", "") or "").split(",")
            if item.strip()
        ]
        include_tier_2 = str(os.getenv("MICA_INCLUDE_TIER2_SKILLS", "") or "").strip().lower() in {"1", "true", "yes", "on"}
        disable_auto = str(os.getenv("MICA_DISABLE_AUTO_SKILLS", "") or "").strip().lower() in {"1", "true", "yes", "on"}
        return explicit_ids, include_tier_2, disable_auto

    def _set_latest_runtime_skill_plan(self, session_id: str, payload: Dict[str, Any]) -> None:
        cache = getattr(self, "_latest_stream_runtime_skills", None)
        if cache is None:
            self._latest_stream_runtime_skills = {}
            cache = self._latest_stream_runtime_skills
        cache[str(session_id or "default")] = dict(payload or {})

    def _pop_latest_runtime_skill_plan(self, session_id: str) -> Optional[Dict[str, Any]]:
        cache = getattr(self, "_latest_stream_runtime_skills", None)
        if not isinstance(cache, dict):
            return None
        return cache.pop(str(session_id or "default"), None)

    def _attach_runtime_skills(self, *, result: Dict[str, Any], session_id: Optional[str]) -> None:
        if not isinstance(result, dict):
            return
        runtime_skills = self._pop_latest_runtime_skill_plan(str(session_id or ""))
        if not isinstance(runtime_skills, dict) or not runtime_skills:
            return
        result["runtime_skills"] = runtime_skills
        provenance = result.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
            result["provenance"] = provenance
        provenance["runtime_skills"] = runtime_skills
        final_result = result.get("final_result")
        if isinstance(final_result, dict):
            final_result["runtime_skills"] = runtime_skills
        runtime_payload = result.get("runtime")
        if not isinstance(runtime_payload, dict):
            runtime_payload = {}
            result["runtime"] = runtime_payload
        runtime_payload["runtime_skills"] = runtime_skills

    def _get_route_card_service(self):
        service = getattr(self, "_route_card_service", None)
        if service is None:
            from .routing import RouteCardService

            service = RouteCardService()
            self._route_card_service = service
        return service

    def _get_tool_selection_service(self):
        service = getattr(self, "_tool_selection_service", None)
        if service is None:
            from .routing import ToolSelectionService

            service = ToolSelectionService()
            self._tool_selection_service = service
        return service

    def _route_card_id_for_query(self, query: str) -> str:
        return self._get_route_card_service().route_card_id_for_query(query)

    def _query_requires_scientific_route_card(self, query: str, intent_tags: Sequence[str]) -> bool:
        return self._get_route_card_service().query_requires_scientific_route_card(query, intent_tags)

    def _query_declares_docs_authority(self, query: str) -> bool:
        return self._get_route_card_service().query_declares_docs_authority(query)

    def _query_requires_docs_authority(
        self,
        query: str,
        *,
        intent_tags: Sequence[str],
        planned_tool_names: Sequence[str],
        visible_tool_names: Sequence[str],
    ) -> tuple[bool, str, List[str]]:
        return self._get_route_card_service().query_requires_docs_authority(
            query,
            intent_tags=intent_tags,
            planned_tool_names=planned_tool_names,
            visible_tool_names=visible_tool_names,
        )

    def _query_prefers_federated_retrieve(
        self,
        query: str,
        *,
        intent_tags: Sequence[str],
        visible_tool_names: Sequence[str],
    ) -> tuple[bool, str]:
        return self._get_route_card_service().query_prefers_federated_retrieve(
            query,
            intent_tags=intent_tags,
            visible_tool_names=visible_tool_names,
        )

    def _extract_explicit_tool_mentions(
        self,
        query: str,
        *,
        visible_tool_names: Sequence[str],
    ) -> List[str]:
        return self._get_route_card_service().extract_explicit_tool_mentions(
            query,
            visible_tool_names=visible_tool_names,
        )

    def _build_route_card(self, *, query: str, routing_meta: Dict[str, Any]) -> Dict[str, Any]:
        return self._get_route_card_service().build_route_card(query=query, routing_meta=routing_meta)

    def _build_required_tool_bootstrap_call(
        self,
        *,
        query: str,
        required_tool_names: List[str],
        visible_tool_names: List[str],
    ) -> Optional[Dict[str, Any]]:
        required = [str(name).strip() for name in list(required_tool_names or []) if str(name).strip()]
        visible = {str(name).strip() for name in list(visible_tool_names or []) if str(name).strip()}
        lowered_query = str(query or "").strip().lower()
        protocol_first_declared = any(
            marker in lowered_query
            for marker in (
                "protocol-first",
                "gog-first",
                "mica.protocol.",
                "governed protocol",
                "not a literature task",
                "use only governed protocol tools",
            )
        )
        if (
            "consult_bibliotecario" not in required
            or "consult_bibliotecario" not in visible
            or protocol_first_declared
            or self._query_forbids_tool_name(query, "consult_bibliotecario")
        ):
            return None

        cleaned_query = re.sub(r"\([^)]{20,}\)", "", str(query or ""))
        cleaned_query = re.sub(r"[,;]+", " ", cleaned_query)
        cleaned_query = " ".join(cleaned_query.split())
        words = cleaned_query.split()
        short_query = " ".join(words[:6]) if len(words) > 6 else cleaned_query

        if "search_literature" in visible:
            return {
                "name": "search_literature",
                "args": {
                    "query": short_query or cleaned_query or str(query or "").strip(),
                    "max_papers": 12,
                    "sources": ["openalex"],
                    "persist_claim_atoms": False,
                },
            }

        return {
            "name": "consult_bibliotecario",
            "args": {
                "query": short_query or cleaned_query or str(query or "").strip(),
                "task": cleaned_query or str(query or "").strip(),
                "max_papers": 12,
            },
        }

    def _query_forbids_tool_name(self, query: str, tool_name: str) -> bool:
        lowered = str(query or "").strip().lower()
        normalized_tool = str(tool_name or "").strip().lower()
        if not lowered or not normalized_tool:
            return False

        negative_clause = re.compile(
            r"(?:do\s+not|don't|dont|never|avoid|without)\s+(?:call|use|invoke|run|select)?\s*([^.;:\n]+)",
            re.IGNORECASE,
        )
        for match in negative_clause.finditer(lowered):
            clause = str(match.group(1) or "").strip()
            if normalized_tool in clause:
                return True
        return False

    def _resolve_required_tool_contract(
        self,
        *,
        query: str,
        required_tool_names: List[str],
        visible_tool_names: List[str],
    ) -> Dict[str, Any]:
        forbidden_required_tool_names = {
            str(name).strip()
            for name in list(required_tool_names or [])
            if str(name).strip() and self._query_forbids_tool_name(query, str(name).strip())
        }
        filtered_required_tool_names = [
            str(name).strip()
            for name in list(required_tool_names or [])
            if str(name).strip() and str(name).strip() not in forbidden_required_tool_names
        ]
        bootstrap_call = self._build_required_tool_bootstrap_call(
            query=query,
            required_tool_names=filtered_required_tool_names,
            visible_tool_names=visible_tool_names,
        )
        enforced_tool_names = list(filtered_required_tool_names)
        followup_guidance = ""

        bootstrap_name = str((bootstrap_call or {}).get("name") or "").strip()
        if bootstrap_name and bootstrap_name not in enforced_tool_names:
            enforced_tool_names = [bootstrap_name]
            followup_guidance = (
                "The lightweight scout satisfies the mandatory first tool action for this turn. "
                "Use the scout output to decide whether the target is actually resolved before escalating. "
                "If the target term or alias remains ambiguous, do not escalate immediately to heavy planned tools "
                "such as consult_bibliotecario, request_peer_review, or generate_vertical_report. "
                "Instead, either run at most one additional lightweight search_literature refinement using the "
                "explicit resolved target, or stop with a calibrated hold that names next_required_evidence."
            )

        return {
            "required_tool_names": enforced_tool_names,
            "bootstrap_call": bootstrap_call,
            "followup_guidance": followup_guidance,
        }

    def _select_effective_mica_tools_for_query(self, query: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        from mica.agentic.ws_bridge import MICA_TOOLS

        effective_mica_tools, routing_meta, current_registry = self._get_tool_selection_service().select_effective_mica_tools_for_query(
            query=query,
            mica_tools=MICA_TOOLS,
            config=self.config,
            mcp_tools=self.mcp_tools,
            toolkg_registry=self._toolkg_registry,
            route_card_service=self._get_route_card_service(),
            filter_tools_fn=_filter_tools_for_driver_surface,
        )
        self._toolkg_registry = current_registry
        return effective_mica_tools, routing_meta

    def _runtime_capability_snapshot(self, transport_path: str, user_query: str = "") -> Dict[str, Any]:
        _, routing_meta = self._select_effective_mica_tools_for_query(user_query)
        configured_providers, provider_error = self._configured_provider_ids()
        storage = self._runtime_storage_snapshot()

        return self._get_tool_selection_service().compose_runtime_capability_snapshot(
            transport_path=transport_path,
            routing_meta=routing_meta,
            spawn_tools=self._SPAWN_TOOLS,
            configured_providers=configured_providers,
            provider_error=provider_error,
            storage_snapshot=storage,
            langgraph_available=bool(LANGGRAPH_AVAILABLE or _LANGGRAPH_RUNTIME_RESOLVED),
            graph_compiled=self.compiled_graph is not None,
            mcp_enabled=bool(self.config.mcp_enabled),
            mcp_available=bool(MCP_AVAILABLE or _MCP_RUNTIME_RESOLVED),
            bridge_available=bool(BRIDGE_AVAILABLE),
            bridge_present=self.bridge is not None,
            security_stack_available=not (SecurityAnalyzer is None or ConfirmationPolicy is None),
            use_checkpointing=bool(getattr(self.config, "use_checkpointing", False)),
            sqlite_checkpointer_available=AsyncSqliteSaver is not None,
        )

    def _official_link_from_identifiers(
        self,
        *,
        doi: Optional[str] = None,
        pmid: Optional[str] = None,
        pmcid: Optional[str] = None,
        uniprot_id: Optional[str] = None,
        pdb_id: Optional[str] = None,
        chembl_id: Optional[str] = None,
        open_access_url: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        return official_link_from_identifiers(
            doi=doi, pmid=pmid, pmcid=pmcid, uniprot_id=uniprot_id,
            pdb_id=pdb_id, chembl_id=chembl_id, open_access_url=open_access_url,
        )

    def _build_source_record_from_paper(self, paper: Dict[str, Any]) -> Dict[str, Any]:
        return build_source_record_from_paper(paper)

    def _format_bibliotecario_citation_entry(self, citation: Dict[str, Any], paper_by_id: Dict[str, Dict[str, Any]]) -> str:
        return format_bibliotecario_citation_entry(citation, paper_by_id)

    def _normalize_bibliotecario_citations(
        self,
        citations: List[Dict[str, Any]],
        paper_by_id: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for citation in citations or []:
            if not isinstance(citation, dict):
                continue
            paper_id = str(citation.get("paper_id") or "").strip()
            paper = paper_by_id.get(paper_id, {})
            source = self._build_source_record_from_paper(paper) if paper else {}
            enriched = dict(citation)
            if source:
                external_ids = paper.get("externalIds") or {}
                doi = source.get("doi") or external_ids.get("DOI") or paper.get("doi")
                pmid = source.get("pmid") or external_ids.get("PubMed") or paper.get("pmid")
                pmcid = source.get("pmcid") or external_ids.get("PubMedCentral") or paper.get("pmcid")
                canonical_source_id = (
                    (f"DOI:{doi}" if doi else "")
                    or (f"PMID:{pmid}" if pmid else "")
                    or (f"PMCID:{pmcid}" if pmcid else "")
                    or source.get("source_id")
                )
                enriched["source_id"] = canonical_source_id
                enriched.setdefault("display_citation", source.get("display_citation"))
                enriched.setdefault("official_url", source.get("official_url"))
                enriched["doi"] = enriched.get("doi") or doi
                enriched["pmid"] = enriched.get("pmid") or pmid
                enriched["pmcid"] = enriched.get("pmcid") or pmcid
                enriched.setdefault("title", source.get("title"))
            normalized.append(enriched)
        return normalized

    def _parse_peer_review_verdict(
        self,
        critique: str,
        review_issues: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        text = str(critique or "")
        upper = text.upper()
        decision = "UNKNOWN"

        # ── Priority 1: explicit verdict markers (most reliable) ──
        # Match patterns like **VERDICT: ACCEPT**, VERDICT: ACCEPT,
        # VEREDICTO: MAJOR_REVISION, etc.
        verdict_marker = re.search(
            r"\*{0,2}(?:VERDICT|VEREDICTO|DECISION)\s*:\s*"
            r"(REJECT|MAJOR_REVISION|MINOR_REVISION|ACCEPT)\b",
            upper,
        )
        if verdict_marker:
            decision = verdict_marker.group(1)
        else:
            # ── Priority 2: severity-ordered substring match (fallback) ──
            for candidate in ("REJECT", "MAJOR_REVISION", "MINOR_REVISION", "ACCEPT"):
                if candidate in upper:
                    decision = candidate
                    break

        recommended_queries: List[str] = []
        in_query_block = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                if in_query_block:
                    break
                continue
            if "CONCRETE ADDITIONAL SEARCHES" in line.upper() or "BÚSQUEDAS CONCRETAS" in line.upper():
                in_query_block = True
                continue
            if in_query_block and line.startswith(("-", "•", "*") ):
                candidate = line[1:].strip().strip("“”\"'")
                if candidate:
                    recommended_queries.append(candidate)
                continue
            if in_query_block:
                break

        if not recommended_queries:
            # Match quoted strings: standard, curly, guillemets
            _qpat = r'[\u201c\u201d\u201e\u201f\u00ab\u00bb"\u2018\u2019]'
            for match in re.findall(_qpat + r'([^"\u201c\u201d]{6,160})' + _qpat, text):
                candidate = str(match).strip()
                if len(candidate.split()) >= 3:
                    recommended_queries.append(candidate)

        deduped_queries: List[str] = []
        seen_queries: Set[str] = set()
        for query in recommended_queries:
            key = query.casefold()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            deduped_queries.append(query)

        unsupported_claims: List[str] = []
        for issue in review_issues or []:
            if not isinstance(issue, dict):
                continue
            claim = str(issue.get("claim") or "").strip()
            if claim:
                unsupported_claims.append(claim)

        if not unsupported_claims:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if "[UNVERIFIED]" in line.upper():
                    unsupported_claims.append(line)

        return {
            "decision": decision,
            "severity": (
                "critical" if decision == "REJECT"
                else "major" if decision == "MAJOR_REVISION"
                else "important" if decision == "MINOR_REVISION"
                else "normal"
            ),
            "recommended_queries": deduped_queries[:8],
            "unsupported_claims": unsupported_claims[:12],
            "should_revise": decision in {"MAJOR_REVISION", "REJECT"} and bool(deduped_queries),
        }

    def _serialize_legacy_model(self, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return dict(model_dump())
            except Exception:
                pass
        if isinstance(value, dict):
            return dict(value)
        return {"value": str(value)}

    def _build_quality_score_adapter(
        self,
        *,
        verdict: Dict[str, Any],
        review_issues: List[Dict[str, Any]],
        citation_count: int = 0,
    ) -> Any:
        critical_count = sum(1 for issue in review_issues if str(issue.get("severity") or "").lower() == "critical")
        major_count = sum(1 for issue in review_issues if str(issue.get("severity") or "").lower() == "major")
        minor_count = sum(1 for issue in review_issues if str(issue.get("severity") or "").lower() == "minor")

        methods = max(0.0, min(1.0, 0.92 - 0.34 * critical_count - 0.18 * major_count - 0.08 * minor_count))
        results = max(0.0, min(1.0, 0.88 - 0.36 * critical_count - 0.20 * major_count - 0.09 * minor_count))
        discussion = max(0.0, min(1.0, 0.84 - 0.20 * critical_count - 0.12 * major_count - 0.05 * minor_count))
        data = max(0.0, min(1.0, 0.30 + min(citation_count, 6) * 0.10 - 0.08 * critical_count))

        try:
            quality = QualityScore(
                methods_reproducibility=methods,
                results_rigor=results,
                discussion_depth=discussion,
                data_availability=data,
                overall_score=0.0,
                nature_compliance_checks={
                    "citations_present": citation_count > 0,
                    "major_revision_required": verdict.get("decision") in {"MAJOR_REVISION", "REJECT"},
                    "unverified_claims_flagged": bool(verdict.get("unsupported_claims")),
                },
            )
            if hasattr(quality, "calculate_overall"):
                quality.overall_score = float(quality.calculate_overall())
            return quality
        except Exception:
            overall = 0.30 * methods + 0.40 * results + 0.20 * discussion + 0.10 * data
            return {
                "methods_reproducibility": methods,
                "results_rigor": results,
                "discussion_depth": discussion,
                "data_availability": data,
                "overall_score": overall,
                "nature_compliance_checks": {
                    "citations_present": citation_count > 0,
                    "major_revision_required": verdict.get("decision") in {"MAJOR_REVISION", "REJECT"},
                    "unverified_claims_flagged": bool(verdict.get("unsupported_claims")),
                },
            }

    def _build_peer_feedback_adapter(
        self,
        *,
        focus: str,
        verdict: Dict[str, Any],
        review_issues: List[Dict[str, Any]],
        quality_score: Any,
    ) -> Any:
        recommendations = [
            str(issue.get("recommendation") or "").strip()
            for issue in review_issues
            if str(issue.get("recommendation") or "").strip()
        ]
        issue_texts = [
            str(issue.get("issue") or "").strip()
            for issue in review_issues
            if str(issue.get("issue") or "").strip()
        ]
        major_gaps = [
            text for issue, text in zip(review_issues, issue_texts)
            if str(issue.get("severity") or "").lower() in {"critical", "major"}
        ]
        assessment = (
            "ACCEPT" if verdict.get("decision") == "ACCEPT"
            else "REVISE_MINOR" if verdict.get("decision") == "MINOR_REVISION"
            else "REVISE_MAJOR"
        )
        reviewer_persona = getattr(AgentPersona, "DR_ARIS_THORNE", getattr(AgentPersona, "SYSTEM", "system"))

        try:
            return PeerFeedback(
                reviewer_persona=reviewer_persona,
                target_node_id="msrp_reviewer",
                target_report_version=1,
                methodological_concerns=major_gaps[:8],
                reproducibility_gaps=verdict.get("unsupported_claims", [])[:8],
                missing_evidence=verdict.get("unsupported_claims", [])[:8],
                insufficient_rigor=issue_texts[:8],
                nature_standard_violations=major_gaps[:8],
                publication_readiness_score=float(self._serialize_legacy_model(quality_score).get("overall_score", 0.0) or 0.0),
                specific_improvements=recommendations[:10],
                recommended_next_steps=list(verdict.get("recommended_queries", []))[:8],
                overall_assessment=assessment,
                quality_score=quality_score,
            )
        except Exception:
            return {
                "reviewer_persona": str(reviewer_persona),
                "target_node_id": "msrp_reviewer",
                "target_report_version": 1,
                "focus": focus,
                "methodological_concerns": major_gaps[:8],
                "reproducibility_gaps": verdict.get("unsupported_claims", [])[:8],
                "missing_evidence": verdict.get("unsupported_claims", [])[:8],
                "insufficient_rigor": issue_texts[:8],
                "nature_standard_violations": major_gaps[:8],
                "publication_readiness_score": float(self._serialize_legacy_model(quality_score).get("overall_score", 0.0) or 0.0),
                "specific_improvements": recommendations[:10],
                "recommended_next_steps": list(verdict.get("recommended_queries", []))[:8],
                "overall_assessment": assessment,
                "quality_score": self._serialize_legacy_model(quality_score),
            }

    def _get_or_create_communication_protocol(self):
        protocol = getattr(self, "_communication_protocol", None)
        if protocol is not None:
            return protocol
        try:
            from bsm.communication.core import CommunicationProtocol, MessageBus, MessageStore

            bus = MessageBus(store=MessageStore())
            protocol = CommunicationProtocol(bus=bus)
            self._communication_protocol = protocol
            self._communication_bus = bus
            return protocol
        except Exception:
            return None

    def _persist_communication_store(self, session_id: str) -> None:
        bus = getattr(self, "_communication_bus", None)
        checkpoint_dir = str(getattr(getattr(self, "config", None), "checkpoint_dir", "./.checkpoints") or "./.checkpoints")
        _persist_communication_store_fn(bus=bus, checkpoint_dir=checkpoint_dir, session_id=session_id)

    def _get_or_create_runtime_telemetry_emitter(self):
        emitter = getattr(self, "_runtime_telemetry_emitter", None)
        if emitter is not None:
            return emitter
        try:
            self._get_or_create_communication_protocol()
            emitter = _build_runtime_telemetry_emitter_fn(
                message_bus=getattr(self, "_communication_bus", None),
                persona=getattr(AgentPersona, "SYSTEM", AgentPersona.SYSTEM),
                roadmap_phase="runtime.process_agentic_prompt",
                goal="Project driver lifecycle telemetry onto the compatibility bus.",
                agent_name="driver",
                subsystem="runtime",
            )
            self._runtime_telemetry_emitter = emitter
            return emitter
        except Exception:
            return None

    async def _emit_runtime_status_telemetry(
        self,
        *,
        session_id: str,
        run_id: str,
        phase: str,
        status: str,
        details: Optional[str] = None,
        mode: Optional[str] = None,
        severity: str = "info",
        metrics: Optional[Dict[str, Any]] = None,
        artifact_refs: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
    ) -> None:
        emitter = self._get_or_create_runtime_telemetry_emitter()
        if emitter is None:
            return
        raw_status = str(status or "").strip().lower()
        normalized_status = raw_status
        if normalized_status not in {"started", "in_progress", "completed", "failed"}:
            if normalized_status in {"accept", "allow", "allowed", "success", "succeeded", "ok"}:
                normalized_status = "completed"
            elif normalized_status in {"block", "blocked", "error", "critical"}:
                normalized_status = "failed"
            else:
                normalized_status = "in_progress"
        normalized_metrics = dict(metrics or {})
        if raw_status and raw_status != normalized_status:
            normalized_metrics.setdefault("raw_status", raw_status)
        await _emit_runtime_status_fn(
            emitter=emitter,
            session_id=session_id,
            run_id=run_id,
            phase=phase,
            status=normalized_status,
            details=details,
            mode=mode,
            severity=severity,
            metrics=normalized_metrics,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            source_ids=source_ids,
        )
        self._persist_communication_store(session_id)

    async def _emit_runtime_error_telemetry(
        self,
        *,
        session_id: str,
        run_id: str,
        phase: str,
        error_type: str,
        message: str,
        traceback_text: Optional[str] = None,
        artifact_path: Optional[str] = None,
        rescue_suggestion: Optional[str] = None,
        mode: Optional[str] = None,
        retryable: Optional[bool] = None,
        artifact_refs: Optional[List[str]] = None,
        evidence_refs: Optional[List[str]] = None,
    ) -> None:
        emitter = self._get_or_create_runtime_telemetry_emitter()
        if emitter is None:
            return
        await _emit_runtime_error_fn(
            emitter=emitter,
            session_id=session_id,
            run_id=run_id,
            phase=phase,
            error_type=error_type,
            message=message,
            traceback_text=traceback_text,
            artifact_path=artifact_path,
            rescue_suggestion=rescue_suggestion,
            mode=mode,
            retryable=retryable,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
        )
        self._persist_communication_store(session_id)

    def _get_or_create_runtime_error_artifact_writer(self):
        writer = getattr(self, "_runtime_error_artifact_writer", None)
        if writer is not None:
            return writer
        try:
            checkpoint_dir = str(getattr(getattr(self, "config", None), "checkpoint_dir", "./.checkpoints") or "./.checkpoints")
            writer, manifest_path = _build_runtime_error_artifact_writer_fn(
                checkpoint_dir=checkpoint_dir,
                file_prefix="runtime_error",
            )
            self._runtime_error_artifact_writer = writer
            self._runtime_error_manifest_path = manifest_path
            return writer
        except Exception:
            return None

    @staticmethod
    def _is_retryable_runtime_exception(exc: Exception) -> bool:
        return _is_retryable_runtime_exception_fn(exc)

    def _persist_runtime_error_artifact(
        self,
        *,
        session_id: str,
        run_id: str,
        phase: str,
        exc: Exception,
        mode: Optional[str],
        user_query: Optional[str],
        evidence_refs: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
    ):
        writer = self._get_or_create_runtime_error_artifact_writer()
        return _persist_runtime_error_artifact_fn(
            writer=writer,
            manifest_path=getattr(self, "_runtime_error_manifest_path", None),
            session_id=session_id,
            run_id=run_id,
            phase=phase,
            exc=exc,
            mode=mode,
            user_query=user_query,
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
        )

    async def _publish_communication_review_projection(
        self,
        *,
        session_id: str,
        run_id: str,
        focus: str,
        critique: str,
        verdict: Dict[str, Any],
        review_issues: List[Dict[str, Any]],
        peer_feedback: Any,
        quality_score: Any,
        artifact_path: Optional[str],
    ) -> None:
        protocol = self._get_or_create_communication_protocol()
        if protocol is None:
            return
        publish_fn = getattr(protocol, "publish_review_projection", None)
        if not callable(publish_fn):
            return
        await publish_fn(
            session_id=session_id,
            run_id=run_id,
            agent_name="msrp_reviewer",
            focus=focus,
            critique=critique,
            verdict=verdict,
            review_issues=review_issues,
            peer_feedback=self._serialize_legacy_model(peer_feedback),
            quality_score=self._serialize_legacy_model(quality_score),
            artifact_path=artifact_path,
        )
        self._persist_communication_store(session_id)

    async def _publish_communication_artifact_announcement(
        self,
        *,
        session_id: str,
        run_id: str,
        result: Optional[Dict[str, Any]],
        artifact_path: Optional[Path],
    ) -> None:
        if artifact_path is None:
            return
        protocol = self._get_or_create_communication_protocol()
        if protocol is None:
            return
        publish_fn = getattr(protocol, "publish_artifact_announcement", None)
        if not callable(publish_fn):
            return
        final_result = (result or {}).get("final_result") if isinstance(result, dict) else {}
        if not isinstance(final_result, dict):
            final_result = {"summary": str(final_result or "")}
        await publish_fn(
            session_id=session_id,
            run_id=run_id,
            agent_name="driver",
            summary=str(final_result.get("summary") or "Artifact announcement generated."),
            artifact_path=str(artifact_path),
            source_ids=[
                str(source.get("source_id") or "")
                for source in final_result.get("sources", [])
                if isinstance(source, dict) and str(source.get("source_id") or "")
            ],
            evidence_refs=[
                str(claim.get("claim_id") or "")
                for claim in final_result.get("claims", [])
                if isinstance(claim, dict) and str(claim.get("claim_id") or "")
            ],
        )
        self._persist_communication_store(session_id)

    def _extract_sources_from_text(self, text: str) -> List[Dict[str, Any]]:
        return extract_sources_from_text(text)

    def _derive_claims_and_sources(
        self,
        *,
        summary: str,
        findings: Any,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return derive_claims_and_sources(summary=summary, findings=findings)

    def _extract_native_evidence_from_side_data(
        self,
        *,
        agent: str,
        channel: str,
        payload: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return extract_native_evidence_from_side_data(agent=agent, channel=channel, payload=payload)

    def _normalize_final_result_contract(
        self,
        *,
        user_query: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        transport_path = str((result.get("runtime") or {}).get("transport_path") or "unknown")
        runtime_state = self._runtime_capability_snapshot(transport_path, user_query)
        return normalize_final_result_contract(
            user_query=user_query,
            result=result,
            runtime_capability_snapshot=runtime_state,
            artifact_renderer=self.final_artifact_renderer,
        )

    def _get_or_create_evidence_ledger(self, session_id: str, run_id: str):
        from mica.drivers.evidence_ledger import EvidenceLedger

        sid = str(session_id or run_id or "default")
        rid = str(run_id or sid)
        cache = getattr(self, "_evidence_ledgers", None)
        if cache is None:
            self._evidence_ledgers = {}
            cache = self._evidence_ledgers
        ledger = cache.get(sid)
        if ledger is None or getattr(ledger, "run_id", "") != rid:
            ledger = EvidenceLedger(run_id=rid)
            cache[sid] = ledger
        self._evidence_ledger = ledger
        return ledger

    def _persist_evidence_ledger(self, session_id: str) -> Optional[Path]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        ledger = getattr(self, "_evidence_ledgers", {}).get(sid)
        if ledger is None:
            return None
        ledger_path = self._conversation_artifact_dir(sid) / "evidence_ledger.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(ledger.to_json(), encoding="utf-8")
        self._sync_session_artifact_to_gcs(local_path=ledger_path, session_id=sid, filename="evidence_ledger.json")
        return ledger_path

    def _persist_hot_loop_reinjection_packet(
        self,
        *,
        session_id: str,
        packet: Optional[Dict[str, Any]],
    ) -> Optional[Path]:
        sid = str(session_id or "").strip()
        if not sid or not isinstance(packet, dict) or not packet:
            return None
        artifact_dir = self._conversation_artifact_dir(sid)
        packet_path = artifact_dir / "hot_loop_reinjection.json"
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._sync_session_artifact_to_gcs(local_path=packet_path, session_id=sid, filename="hot_loop_reinjection.json")

        residual_path = artifact_dir / "residual_inventory.json"
        residual_payload = {
            "schema_version": "mica.residual_inventory.v0",
            "entries": list(packet.get("residual_tasks") or []),
        }
        residual_path.write_text(json.dumps(residual_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._sync_session_artifact_to_gcs(local_path=residual_path, session_id=sid, filename="residual_inventory.json")
        return packet_path

    def _stage_hot_loop_reinjection_packet(
        self,
        *,
        session_id: str,
        packet: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Path]]:
        sid = str(session_id or "").strip()
        if not sid or not isinstance(packet, dict) or not packet:
            return None
        artifact_dir = self._conversation_artifact_dir(sid)
        staged_packet_path = artifact_dir / "hot_loop_reinjection.staged.json"
        staged_residual_path = artifact_dir / "residual_inventory.staged.json"
        staged_packet_path.parent.mkdir(parents=True, exist_ok=True)
        staged_packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        residual_payload = {
            "schema_version": "mica.residual_inventory.v0",
            "entries": list(packet.get("residual_tasks") or []),
        }
        staged_residual_path.write_text(json.dumps(residual_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {
            "packet": staged_packet_path,
            "residual": staged_residual_path,
        }

    def _promote_staged_hot_loop_reinjection_packet(
        self,
        *,
        session_id: str,
        staged_paths: Optional[Dict[str, Path]],
    ) -> Optional[Path]:
        sid = str(session_id or "").strip()
        if not sid or not isinstance(staged_paths, dict) or not staged_paths:
            return None
        packet_stage = staged_paths.get("packet")
        residual_stage = staged_paths.get("residual")
        if packet_stage is None or residual_stage is None:
            raise ValueError("Missing staged hot-loop reinjection artifacts for promotion")
        if not packet_stage.exists() or not residual_stage.exists():
            raise FileNotFoundError("Staged hot-loop reinjection artifacts are missing and cannot be promoted")

        artifact_dir = self._conversation_artifact_dir(sid)
        packet_path = artifact_dir / "hot_loop_reinjection.json"
        residual_path = artifact_dir / "residual_inventory.json"
        packet_stage.replace(packet_path)
        residual_stage.replace(residual_path)
        self._sync_session_artifact_to_gcs(local_path=packet_path, session_id=sid, filename="hot_loop_reinjection.json")
        self._sync_session_artifact_to_gcs(local_path=residual_path, session_id=sid, filename="residual_inventory.json")
        return packet_path

    def _annotate_materialization_policy(
        self,
        *,
        session_id: str,
        run_id: str,
        result: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return {}

        policy = {
            "session_id": str(session_id or "").strip(),
            "run_id": str(run_id or session_id or "").strip(),
            "evidence_ledger_runtime_scope": "run_scoped",
            "evidence_ledger_persistence": "latest_run_snapshot_per_session_path",
            "final_artifact_persistence": "latest_run_overwrite_per_session_path",
            "conversation_log_persistence": "append_only_per_session",
            "retry_semantics": "artifacts_and_ledger_overwrite_latest_run; conversation_log_appends_turn_history",
        }
        result["materialization_policy"] = dict(policy)
        final_result["materialization_policy"] = dict(policy)
        return policy

    def _record_claim_dicts_in_evidence_ledger(
        self,
        *,
        session_id: str,
        run_id: str,
        claims: List[Dict[str, Any]],
        default_severity: str = "important",
        validation_route: str = "mixed",
        evidence_type: str = "review",
        verification_status: str = "verified",
    ) -> None:
        from mica.drivers.evidence_ledger import EvidenceEntry

        ledger = self._get_or_create_evidence_ledger(session_id, run_id)
        for claim in claims or []:
            text = str(claim.get("text") or "").strip()
            if not text:
                continue
            strength = str(claim.get("strength") or "").strip().lower()
            status = "supported" if strength == "supported" else "partial" if strength == "suggestive" else "unsupported"
            severity = str(claim.get("severity") or default_severity)
            source_relevance = {
                str(detail.get("source_id") or ""): {
                    "relevance_score": float(detail.get("relevance_score", 0.0) or 0.0),
                    "relevance_status": str(detail.get("relevance_status") or "unknown"),
                    "reasons": list(detail.get("reasons") or []),
                }
                for detail in (claim.get("source_relevance") or [])
                if isinstance(detail, dict) and str(detail.get("source_id") or "")
            }
            source_role_types = {
                str(source_id): str((claim.get("source_role_types") or {}).get(str(source_id)) or "")
                for source_id in list(claim.get("source_ids") or [])
                if str(source_id)
            }
            entry = EvidenceEntry(
                claim_id=str(claim.get("claim_id") or ""),
                claim_text=text,
                severity=severity,
                source_ids=list(claim.get("source_ids") or []),
                tool_call_ids=list(claim.get("tool_call_ids") or []),
                evidence_type=evidence_type,
                status=status,
                verification_status=verification_status,
                validation_route=validation_route,
                algorithmic_confidence=float(claim.get("algorithmic_confidence", 0.0) or 0.0),
                negative_result_refs=list(claim.get("counterevidence_ids") or []),
                source_role_types=source_role_types,
                source_relevance=source_relevance,
                relevant_source_ids=list(claim.get("relevant_source_ids") or []),
                weakly_relevant_source_ids=list(claim.get("weakly_relevant_source_ids") or []),
                irrelevant_source_ids=list(claim.get("irrelevant_source_ids") or []),
            )
            ledger.add_claim(entry)

    def _persist_final_result_artifact(
        self,
        *,
        session_id: str,
        result: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(result, dict):
            return None
        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return None
        text = str(final_result.get("paper_markdown") or final_result.get("answer") or final_result.get("summary") or "").strip()
        if not text:
            return None
        artifact_path = self._conversation_artifact_dir(session_id) / "final_result.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(text, encoding="utf-8")
        self._sync_session_artifact_to_gcs(local_path=artifact_path, session_id=session_id, filename="final_result.md")
        artifacts = final_result.setdefault("artifacts", [])
        if isinstance(artifacts, list) and not any(isinstance(a, dict) and a.get("path") == str(artifact_path) for a in artifacts):
            artifacts.append({
                "type": "final_synthesis_markdown",
                "path": str(artifact_path),
                "description": "Integrated final synthesis persisted from the driver result.",
            })
        return str(artifact_path)

    def _record_final_result_evidence(self, *, session_id: str, run_id: str, result: Optional[Dict[str, Any]]) -> None:
        if not isinstance(result, dict):
            return
        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return

        claims = [c for c in (final_result.get("claims") or []) if isinstance(c, dict)]
        source_role_types = {
            str(source.get("source_id") or ""): str((source.get("metadata") or {}).get("source_role_type") or "")
            for source in (final_result.get("sources") or [])
            if isinstance(source, dict) and str(source.get("source_id") or "")
        }
        for claim in claims:
            if isinstance(claim, dict):
                claim.setdefault("source_role_types", source_role_types)
        if claims:
            self._record_claim_dicts_in_evidence_ledger(
                session_id=session_id,
                run_id=run_id,
                claims=claims,
                default_severity="important",
                validation_route="mixed",
                evidence_type="review",
                verification_status="verified",
            )

        summary = str(final_result.get("summary") or final_result.get("answer") or "").strip()
        if summary:
            synthesis_confidence = float(
                final_result.get("scientific_closure_score", final_result.get("provenance_relevance_score", 0.0)) or 0.0
            )
            output_mode = str(final_result.get("output_mode") or "")
            synthesis_strength = "suggestive"
            verification_status = "unverified"
            if output_mode == "evidence_backed_answer" and claims:
                synthesis_strength = "supported"
                verification_status = "verified"
            elif output_mode in {"misleading_support_blocked", "calibrated_abstention", "failed"}:
                synthesis_strength = "unsupported"
            self._record_claim_dicts_in_evidence_ledger(
                session_id=session_id,
                run_id=run_id,
                claims=[{
                    "claim_id": f"final-synthesis-{session_id}",
                    "text": summary,
                    "strength": synthesis_strength,
                    "source_ids": sorted({sid for claim in claims for sid in (claim.get("source_ids") or [])}),
                    "severity": "critical",
                    "algorithmic_confidence": synthesis_confidence,
                }],
                default_severity="critical",
                validation_route="mixed",
                evidence_type="review",
                verification_status=verification_status,
            )

    def _evaluate_final_result_promotion(
        self,
        *,
        session_id: str,
        run_id: str,
        result: Optional[Dict[str, Any]],
    ):
        if not isinstance(result, dict):
            return None
        final_result = result.get("final_result")
        if not isinstance(final_result, dict):
            return None

        self._record_final_result_evidence(
            session_id=session_id,
            run_id=run_id,
            result=result,
        )
        ledger = self._get_or_create_evidence_ledger(session_id, run_id)
        verdict = EvidenceGate(ledger, final_result=final_result).evaluate()
        final_result["promotion_gate"] = verdict.to_dict()
        final_result["cold_evidence_spine"] = dict(verdict.cold_evidence_spine or {})
        final_result["publication_ready"] = bool(verdict.passed)
        final_result["promotion_blocked"] = not bool(verdict.passed)
        promotion_ledger = final_result.get("promotion_ledger") if isinstance(final_result.get("promotion_ledger"), dict) else {}
        promotion_ledger.update(
            {
                "publication_ready": bool(verdict.passed),
                "promotion_blocked": not bool(verdict.passed),
                "gate_passed": bool(verdict.passed),
                "block_reasons": list(verdict.promotion_block_reasons or []),
                "firewall_action": str(((verdict.cold_evidence_spine or {}).get("firewall") or {}).get("action") or "accept"),
                "invariant_passed": bool(((verdict.cold_evidence_spine or {}).get("invariants") or {}).get("passed", True)),
                "cold_evidence_spine": dict(verdict.cold_evidence_spine or {}),
            }
        )
        final_result["promotion_ledger"] = promotion_ledger
        dossier_envelope = final_result.get("dossier_envelope")
        if isinstance(dossier_envelope, dict):
            dossier_envelope["promotion_ledger"] = promotion_ledger
        result["promotion_gate"] = verdict.to_dict()
        result["cold_evidence_spine"] = dict(verdict.cold_evidence_spine or {})
        result["publication_ready"] = bool(verdict.passed)
        result["promotion_blocked"] = not bool(verdict.passed)
        result["promotion_ledger"] = promotion_ledger
        runtime_state = result.get("runtime")
        if isinstance(runtime_state, dict):
            runtime_state["promotion_ledger"] = promotion_ledger
        self._refresh_final_artifact_sections_after_promotion_gate(result=result)
        self._annotate_materialization_policy(session_id=session_id, run_id=run_id, result=result)
        return verdict

    def _refresh_final_artifact_sections_after_promotion_gate(self, *, result: Dict[str, Any]) -> None:
        final_result = result.get("final_result")
        if not isinstance(final_result, dict) or self.final_artifact_renderer is None:
            return

        rendered = self.final_artifact_renderer.render(
            query=str(final_result.get("query") or result.get("query") or ""),
            summary=str(final_result.get("summary") or ""),
            paper=final_result.get("paper") if isinstance(final_result.get("paper"), dict) else {},
            claims=list(final_result.get("claims") or []),
            sources=list(final_result.get("sources") or []),
            output_mode=str(final_result.get("output_mode") or "failed"),
            metrics={
                "raw_claim_to_source_coverage": float(final_result.get("claim_relevance_coverage", 0.0) or 0.0),
                "claim_relevance_coverage": float(final_result.get("claim_relevance_coverage", 0.0) or 0.0),
                "unsupported_assertion_rate": float(final_result.get("unsupported_assertion_rate", 0.0) or 0.0),
                "provenance_relevance_score": float(final_result.get("provenance_relevance_score", 0.0) or 0.0),
                "evidentiality_score": float(final_result.get("evidentiality_score", 0.0) or 0.0),
                "scientific_closure_score": float(final_result.get("scientific_closure_score", 0.0) or 0.0),
                "abstention_quality_score": float(final_result.get("abstention_quality_score", 0.0) or 0.0),
                "investigative_utility_score": float(final_result.get("investigative_utility_score", 0.0) or 0.0),
                "orchestration_coherence_score": float(final_result.get("orchestration_coherence_score", 0.0) or 0.0),
                "relevant_source_count": int(final_result.get("relevant_source_count", 0) or 0),
                "irrelevant_source_count": int(final_result.get("irrelevant_source_count", 0) or 0),
                "cognitive_layer": final_result.get("cognitive_layer") if isinstance(final_result.get("cognitive_layer"), dict) else {},
                "thermodynamic_routing": final_result.get("thermodynamic_routing") if isinstance(final_result.get("thermodynamic_routing"), dict) else {},
                "promotion_ledger": final_result.get("promotion_ledger") if isinstance(final_result.get("promotion_ledger"), dict) else {},
            },
            run_status=str(final_result.get("run_status") or "failed"),
            degradation_flags=list(final_result.get("degradation_flags") or []),
            capabilities_unavailable=list(final_result.get("capabilities_unavailable") or []),
            fallbacks_used=list(final_result.get("fallbacks_used") or []),
            failure_records=[
                dict(item)
                for item in list(final_result.get("failure_records") or result.get("failure_records") or [])
                if isinstance(item, dict)
            ],
            uncertainty_summary=str(final_result.get("uncertainty_summary") or ""),
        )
        final_result["paper_markdown"] = str(rendered.get("paper_markdown") or final_result.get("paper_markdown") or "")
        final_result["answer"] = str(rendered.get("paper_markdown") or final_result.get("answer") or "")

    async def _append_conversation_log(
        self,
        *,
        session_id: str,
        user_query: str,
        mode: str,
        result: Optional[Dict[str, Any]],
        started_at: datetime,
        finished_at: datetime,
        error: Optional[str],
    ) -> None:
        await append_conversation_log(
            conversation_log_enabled=getattr(self.config, "conversation_log_enabled", True),
            checkpoint_dir=self.config.checkpoint_dir,
            conversation_log_dirname=getattr(self.config, "conversation_log_dirname", None),
            conversation_log_max_entries=int(getattr(self.config, "conversation_log_max_entries", 250) or 250),
            conversation_log_lock=self._conversation_log_lock,
            session_id=session_id,
            user_query=user_query,
            mode=mode,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
            timescale_appender=self._append_saga_event_timescale,
            session_run_ids=self._session_run_ids,
        )
        # Mirror conversation log to GCS.
        log_path = self._conversation_log_path(session_id)
        if log_path.exists():
            self._sync_session_artifact_to_gcs(
                local_path=log_path,
                session_id=session_id,
                filename=log_path.name,
            )
    
    async def _execute_with_langgraph(
        self,
        user_query: str,
        mode: str,
        session_id: Optional[str],
        reinjection_packet: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute workflow using LangGraph StateGraph (v3.0).
        """
        
        safe_query_preview = _truncate_text(_redact_text(user_query), max_len=120)
        logger.info(f"🚀 [LangGraph v3.0] Processing query: {safe_query_preview}")
        
        # Create initial state
        workflow_id = session_id or str(uuid.uuid4())
        initial_state = MICAState(
            workflow_id=workflow_id,
            session_id=workflow_id,
            user_query=user_query,
            original_user_query=user_query,
            workflow_type="reactive" if mode == "production" else "proactive",
            msrp_current_phase=1,
            msrp_status={},
            iteration_count=0,
            max_iterations=self.config.max_iterations,
            quality_threshold=self.config.quality_threshold,
            intent={},
            subtasks=[],
            assigned_workers={},
            lab_reports=[],
            peer_feedback=[],
            quality_score=0.0,
            quality_metrics={},
            specialist_outputs={},
            mcp_tool_results=[],
            proactive_triggers=[],
            auto_generated_tasks=[],
            _proactive_spawn_count=0,
            final_result=None,
            converged=False,
            resolved_pdb_path="",
            created_at=datetime.now(timezone.utc).isoformat(),
            errors=[],
            logs=[],
            requires_approval=False,
            soul={}
        )
        if isinstance(reinjection_packet, dict) and reinjection_packet:
            initial_state["reinjection_packet"] = dict(reinjection_packet)
            initial_state["branch_tombstones"] = list(reinjection_packet.get("branch_tombstones") or [])
            initial_state["negative_memory_mode"] = str(reinjection_packet.get("negative_memory_mode") or "full")
            initial_state["negative_memory_summary"] = dict(reinjection_packet.get("negative_memory_summary") or {})
            initial_state["appeal_regime_state"] = dict(reinjection_packet.get("appeal_regime_state") or {})
            initial_state["soft_repulsion_warnings"] = list(reinjection_packet.get("soft_repulsion_warnings") or [])
            initial_state["rupture_energy_events"] = list(reinjection_packet.get("rupture_energy_events") or [])
            initial_state["logs"] = list(initial_state.get("logs") or []) + [
                {
                    "step": "hot_loop_reinjection",
                    "packet_id": reinjection_packet.get("packet_id"),
                    "required": bool(reinjection_packet.get("retry_required")),
                }
            ]
            initial_state["user_query"] = (
                f"{user_query}\n\n[Structured reinjection packet]\n"
                f"{json.dumps(reinjection_packet, ensure_ascii=False, indent=2, default=str)}"
            )
        
        # Run graph — default 25 is too low for quality-gate + proactive loops
        config = {"configurable": {"thread_id": workflow_id}, "recursion_limit": 100}
        final_state = await self.compiled_graph.ainvoke(initial_state, config)
        
        # Extract results
        return {
            "session_id": workflow_id,
            "final_result": final_state.get("final_result"),
            "lab_reports": final_state.get("lab_reports", []),
            "quality_score": final_state.get("quality_score", 0.0),
            "quality_metrics": final_state.get("quality_metrics", {}),
            "peer_feedback": final_state.get("peer_feedback", []),
            "provenance": {
                "iterations": final_state.get("iteration_count", 0),
                "converged": final_state.get("converged", False),
                "logs": final_state.get("logs", []),
                "errors": final_state.get("errors", [])
            },
            "runtime": {"transport_path": "langgraph"},
        }
    
    async def _execute_with_agentic_loop(
        self,
        user_query: str,
        mode: str,
        session_id: Optional[str],
        provider_id: str = "anthropic",
        model_id: Optional[str] = None,
        reinjection_packet: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from .execution import execute_with_agentic_loop

        return await execute_with_agentic_loop(
            self,
            user_query,
            mode,
            session_id,
            provider_id=provider_id,
            model_id=model_id,
            reinjection_packet=reinjection_packet,
        )

    def _should_use_direct_structure_path(self, user_query: str) -> bool:
        return _should_use_direct_structure_path_fn(user_query)

    async def _execute_direct_structure_request(self, *, user_query: str, session_id: str) -> Dict[str, Any]:
        from .execution import execute_direct_structure_request

        return await execute_direct_structure_request(
            self,
            user_query=user_query,
            session_id=session_id,
        )
    
    async def _execute_with_mcp(self, worker_type: str, session: AgenticSession) -> Dict[str, Any]:
        """Execute subtask using MCP tools with DLM-LMP Bridge integration."""
        
        # Extract server name from worker_type (e.g., "mcp_uniprot" → "uniprot")
        server_name = worker_type.replace("mcp_", "")
        server_aliases = {
            # Intent decomposition still emits the generic "literature" worker,
            # but the configured MCP server is named "dlm_literature".
            "literature": "dlm_literature",
        }
        server_name = server_aliases.get(server_name, server_name)
        
        logger.info(f"Executing with MCP server: {server_name}")
        if not self.config.mcp_enabled or not _ensure_mcp_runtime():
            return {"status": "failed", "server": server_name, "message": "MCP disabled/unavailable"}

        start_time = datetime.now(timezone.utc)

        tool_base = self._pick_tool_for_server(server_name, query=session.user_query)
        if tool_base is None:
            latency = (datetime.now(timezone.utc) - start_time).total_seconds()
            self.tool_context.record_execution(f"{server_name}_tool", False, latency)
            return {"status": "failed", "server": server_name, "message": "No tools available"}

        # Get identifiers (legacy) and schema
        identifiers = self._extract_identifiers(session.user_query)
        effective_identifiers = identifiers
        if server_name in {"uniprot", "pdb", "alphafold"}:
            resolved_uniprot = await self._resolve_uniprot_accessions(
                query=session.user_query,
                identifiers=identifiers,
            )
            if resolved_uniprot:
                effective_identifiers = self._merge_identifiers(
                    identifiers,
                    {"uniprot": resolved_uniprot, "pdb": []},
                )
        schema = self._get_tool_schema(server_name, tool_base)
        
        # Build args with bridge (returns args + optional BridgeResult)
        args, bridge_result = self._build_tool_args(schema, effective_identifiers, session.user_query, tool_type=server_name)
        
        # Handle pre-search if needed
        if bridge_result and bridge_result.needs_pre_search:
            logger.info(f"Pre-search required for {server_name}: {bridge_result.search_query}")

            # If we can do a structured pre-search (currently supported for PDB), do it.
            if server_name == "pdb" and getattr(bridge_result, "search_query", None):
                pre = await self._execute_with_pre_search(server_name, bridge_result.search_query, bridge_result)
                success = pre.get("status") == "success"
                latency = (datetime.now(timezone.utc) - start_time).total_seconds()
                self.tool_context.record_execution(f"{server_name}.pre_search", success, latency)

                artifacts: List[str] = []
                if success:
                    try:
                        protein_hint = self._best_protein_hint(effective_identifiers) or "unknown"
                        artifacts = self._persist_structure_artifacts(server_name, protein_hint, pre.get("structure"))
                    except Exception as exc:
                        logger.warning("Failed to persist artifacts from %s pre-search: %s", server_name, exc)

                # SECURITY: never return raw structure payloads; keep summary + artifact paths.
                pdb_id = pre.get("pdb_id") or (pre.get("selected") or {}).get("pdb_id")
                return {
                    "status": "success" if success else "failed",
                    "server": server_name,
                    "tool": "pre_search",
                    "needs_pre_search": True,
                    "search_query": bridge_result.search_query,
                    "pdb_id": pdb_id,
                    "fallback_source": pre.get("fallback_source"),
                    "selected": pre.get("selected"),
                    "message": _truncate_text(
                        _redact_text(pre.get("message") or ("Pre-search succeeded" if success else pre.get("error") or "Pre-search failed")),
                        max_len=2000,
                    ),
                    "artifacts": artifacts,
                    "latency": latency,
                    "bridge_confidence": bridge_result.confidence,
                    "telemetry": pre.get("telemetry"),
                }

            # Otherwise, if args are empty, we cannot proceed.
            if not args:
                latency = (datetime.now(timezone.utc) - start_time).total_seconds()
                self.tool_context.record_execution(f"{server_name}.{tool_base}", False, latency)
                return {
                    "status": "failed",
                    "server": server_name,
                    "message": f"Pre-search needed but unsupported for server: {server_name}",
                    "needs_pre_search": True,
                    "search_query": bridge_result.search_query,
                    "latency": latency,
                    "bridge_confidence": bridge_result.confidence,
                }

        # Execute MCP tool
        tool_result = await self.call_mcp_tool(server_name, tool_base, args, session_id=session.session_id)
        success = bool(tool_result.get("success"))
        latency = (datetime.now(timezone.utc) - start_time).total_seconds()
        self.tool_context.record_execution(f"{server_name}.{tool_base}", success, latency)

        # Persist artifacts
        artifacts: List[str] = []
        if success:
            try:
                protein_hint = self._best_protein_hint(effective_identifiers) or "unknown"
                artifacts = self._persist_structure_artifacts(server_name, protein_hint, tool_result.get("result"))
            except Exception as exc:
                logger.warning("Failed to persist artifacts from %s.%s: %s", server_name, tool_base, exc)

        return {
            "status": "success" if success else "failed",
            "server": server_name,
            "tool": tool_base,
            # SECURITY: never return raw tool args (may contain tokens/PII). Keep only keys.
            "arg_keys": sorted(list(args.keys())) if isinstance(args, dict) else [],
            "message": _truncate_text(
                _redact_text("MCP tool executed" if success else str(tool_result.get("error", "MCP tool failed"))),
                max_len=2000,
            ),
            "artifacts": artifacts,
            "latency": latency,
            "bridge_confidence": bridge_result.confidence if bridge_result else 0.0,
        }
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    async def cleanup(self):
        """Cleanup resources (MCP sessions, checkpointer)."""
        
        logger.info("Cleaning up AgenticDriver...")
        
        # Close MCP sessions
        if self._mcp_exit_stack is not None:
            try:
                await self._mcp_exit_stack.aclose()
            finally:
                self._mcp_exit_stack = None
        for server_name, info in self.mcp_sessions.items():
            info["status"] = "closed"
            info["session"] = None
        self._evidence_ledgers = {}
        self._evidence_ledger = None
        
        # Close checkpointer
        if self._checkpointer_cm is not None and hasattr(self._checkpointer_cm, '__aexit__'):
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            finally:
                self._checkpointer_cm = None
                self.checkpointer = None

        # Close Timescale/Neon store (best effort)
        if self._timescale_store is not None:
            try:
                await self._timescale_store.close()
            except Exception:
                pass
            finally:
                self._timescale_store = None

        # Close lazily created retrieval backends (best effort)
        for attr in sorted(getattr(self, "_owned_memory_backends", set())):
            backend = getattr(self, attr, None)
            if backend is None:
                continue
            close_fn = getattr(backend, "close", None)
            if callable(close_fn):
                try:
                    result = close_fn()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass
            setattr(self, attr, None)
        self._owned_memory_backends.clear()

        self._session_run_ids.clear()
        self._program_envelopes.clear()
        self._run_event_logs.clear()
        
        logger.info("AgenticDriver cleanup complete")

    # ====================================================================
    # MQA / STRUCTURE HELPERS
    # ====================================================================

    _PDB_FALSE_POSITIVES = PDB_FALSE_POSITIVES

    def _extract_identifiers(self, text: str) -> Dict[str, List[str]]:
        return extract_identifiers(text, pdb_false_positives=self._PDB_FALSE_POSITIVES)

    def _merge_identifiers(self, a: Dict[str, List[str]], b: Dict[str, List[str]]) -> Dict[str, List[str]]:
        return merge_identifiers(a, b)

    def _best_protein_hint(self, identifiers: Dict[str, List[str]]) -> Optional[str]:
        return best_protein_hint(identifiers)

    def _report_to_text(self, report: Any) -> str:
        if report is None:
            return ""
        to_md = getattr(report, "to_markdown", None)
        if callable(to_md):
            try:
                return to_md()
            except Exception:
                pass
        try:
            return str(report)
        except Exception:
            return ""

    def _get_tool_schema(self, server_name: str, tool_base: str) -> Dict[str, Any]:
        return _get_tool_schema_fn(self.mcp_tools, server_name, tool_base)

    def _pick_tool_for_server(self, server_name: str, query: Optional[str] = None) -> Optional[str]:
        return _pick_tool_for_server_fn(self.mcp_tools, self.tool_context, server_name, query)

    def _build_tool_args(self, schema: Dict[str, Any], identifiers: Dict[str, List[str]], query: str, tool_type: str = "unknown") -> Tuple[Dict[str, Any], Optional[object]]:
        """Build tool arguments using DLM-LMP Bridge or fallback regex."""
        bridge_available = _ensure_bridge_runtime() if self.config.enable_bridge else False
        return _build_tool_args_fn(
            bridge=self.bridge,
            bridge_available=bridge_available,
            schema=schema,
            identifiers=identifiers,
            query=query,
            tool_type=tool_type,
            gene_symbols_fn=self._extract_candidate_gene_symbols,
            protein_hint_fn=self._best_protein_hint,
        )
    
    def _build_tool_args_fallback(self, schema: Dict[str, Any], identifiers: Dict[str, List[str]], query: str) -> Dict[str, Any]:
        """Legacy regex-based tool argument building (fallback)."""
        return _build_tool_args_fallback_fn(
            schema=schema,
            identifiers=identifiers,
            query=query,
            gene_symbols_fn=self._extract_candidate_gene_symbols,
            protein_hint_fn=self._best_protein_hint,
        )

    def _extract_text_chunks_from_mcp(self, result_obj: Any) -> List[str]:
        return extract_text_chunks_from_mcp(result_obj)

    async def _resolve_uniprot_accessions(
        self,
        *,
        query: str,
        identifiers: Dict[str, List[str]],
    ) -> List[str]:
        existing = [str(x).strip().upper() for x in (identifiers.get("uniprot") or []) if str(x).strip()]
        if existing:
            return sorted(set(existing))

        try:
            schema = self._get_tool_schema("uniprot", "get_protein_info")
            args, _bridge_result = self._build_tool_args(schema, identifiers, query, tool_type="uniprot")
            accession = str((args or {}).get("accession") or "").strip().upper()
            if accession:
                return [accession]
        except Exception:
            pass

        gene_candidates = self._extract_candidate_gene_symbols(query, identifiers)
        for gene in gene_candidates:
            try:
                result = await self.call_mcp_tool("uniprot", "search_by_gene", {"gene": gene})
                if result.get("success"):
                    accessions = self._extract_uniprot_accessions_from_mcp_result(result.get("result"))
                    if accessions:
                        return accessions
            except Exception as exc:
                logger.warning("UniProt gene resolution failed for %s: %s", gene, exc)

        search_query = (gene_candidates[0] if gene_candidates else query).strip()
        if search_query:
            try:
                result = await self.call_mcp_tool("uniprot", "search_proteins", {"query": search_query})
                if result.get("success"):
                    accessions = self._extract_uniprot_accessions_from_mcp_result(result.get("result"))
                    if accessions:
                        return accessions
            except Exception as exc:
                logger.warning("UniProt search_proteins resolution failed for %s: %s", search_query, exc)

        return []

    def _extract_candidate_gene_symbols(self, query: str, identifiers: Dict[str, List[str]]) -> List[str]:
        return extract_candidate_gene_symbols(query, identifiers)

    def _extract_uniprot_accessions_from_mcp_result(self, result_obj: Any) -> List[str]:
        return extract_uniprot_accessions_from_mcp_result(result_obj)

    def _extract_pdb_ids_from_search_result(self, result_obj: Any) -> List[str]:
        return extract_pdb_ids_from_search_result(result_obj)

    def _persist_structure_artifacts(self, server_name: str, protein_hint: str, result_obj: Any) -> List[str]:
        return _persist_structure_artifacts_fn(
            server_name,
            protein_hint,
            result_obj,
            text_chunks_fn=self._extract_text_chunks_from_mcp,
            checkpoint_dir=self.config.checkpoint_dir,
        )

    async def _execute_with_pre_search(self, server_name: str, search_query: str, bridge_result: object) -> Dict[str, Any]:
        """Execute MCP tool with pre-search for structure discovery.
        
        Task #3: PDB Pre-Search Implementation
        Task #4: Includes fallback chain (PDB→AlphaFold)
        Task #8: Includes telemetry
        
        Args:
            server_name: MCP server (e.g., 'pdb')
            search_query: Structured search query from bridge (e.g., 'uniprot:P04637 domain:DNA-binding')
            bridge_result: Bridge result with extracted entities
        
        Returns:
            Dict with search results and selected structure
        """
        import time
        start_time = time.time()
        
        logger.info(f"🔍 Pre-search initiated: {server_name} | Query: {search_query}")
        
        try:
            # Task #4: Try primary source (PDB)
            if server_name == "pdb":
                result = await self._search_and_select_pdb(search_query, bridge_result)
                
                # Task #4: Fallback to AlphaFold if PDB fails
                if result["status"] == "failed" and bridge_result.extracted.has_explicit_ids():
                    logger.warning("PDB pre-search failed, falling back to AlphaFold")
                    result = await self._fallback_to_alphafold(bridge_result)
                    result["fallback_source"] = "alphafold"
                
                # Task #8: Telemetry
                execution_time = time.time() - start_time
                result["telemetry"] = {
                    "execution_time_ms": execution_time * 1000,
                    "primary_source": "pdb",
                    "fallback_used": "fallback_source" in result,
                    "confidence": bridge_result.confidence,
                    "search_query": search_query,
                }
                
                logger.info(f"✅ Pre-search completed in {execution_time:.2f}s")
                return result
            
            else:
                return {
                    "status": "not_supported",
                    "message": f"Pre-search not supported for server: {server_name}",
                    "server": server_name,
                }
                
        except Exception as e:
            logger.error(f"Pre-search failed: {e}")
            execution_time = time.time() - start_time
            return {
                "status": "failed",
                "error": str(e),
                "server": server_name,
                "telemetry": {
                    "execution_time_ms": execution_time * 1000,
                    "error": str(e),
                },
            }
    
    async def _search_and_select_pdb(self, search_query: str, bridge_result: object) -> Dict[str, Any]:
        """Search PDB and select best structure.
        
        Task #3: Core PDB search logic with ranking
        
        Args:
            search_query: Structured PDB search query
            bridge_result: Bridge result with entities
        
        Returns:
            Dict with selected structure or error
        """
        try:
            # Call PDB search tool
            search_result = await self.call_mcp_tool(
                "pdb",
                "search_structures",
                {"query": search_query, "limit": 10}
            )
            
            if not search_result.get("success"):
                return {
                    "status": "failed",
                    "message": "PDB search failed",
                    "error": search_result.get("error"),
                }
            
            # Extract search results
            results = search_result.get("result", {}).get("content", [])
            if not results:
                return {
                    "status": "failed",
                    "message": "No structures found in PDB",
                    "search_query": search_query,
                }
            
            # Task #3: Rank structures
            ranked = self._rank_pdb_structures(results, bridge_result)
            
            if not ranked:
                return {
                    "status": "failed",
                    "message": "No valid structures after ranking",
                }
            
            # Select best structure
            best = ranked[0]
            logger.info(f"🎯 Selected PDB structure: {best.get('pdb_id')} (score: {best.get('rank_score', 0):.2f})")
            
            # Download structure
            download_result = await self.call_mcp_tool(
                "pdb",
                "download_structure",
                {"pdb_id": best["pdb_id"]}
            )
            
            return {
                "status": "success",
                "pdb_id": best["pdb_id"],
                "structure": download_result.get("result"),
                "search_results": results,
                "selected": best,
                "alternatives": ranked[1:5],  # Top 5 alternatives
            }
            
        except Exception as e:
            logger.error(f"PDB search error: {e}")
            return {
                "status": "failed",
                "error": str(e),
            }
    
    def _rank_pdb_structures(self, structures: List[Dict], bridge_result: object) -> List[Dict]:
        """Rank PDB structures by quality metrics."""
        return _rank_pdb_structures_fn(structures, bridge_result)
    
    async def _fallback_to_alphafold(self, bridge_result: object) -> Dict[str, Any]:
        """Fallback to AlphaFold when PDB search fails.
        
        Task #4: Fallback chain implementation
        
        Args:
            bridge_result: Bridge result with extracted entities
        
        Returns:
            Dict with AlphaFold structure or error
        """
        logger.info("🔄 Attempting AlphaFold fallback...")
        
        # Get UniProt ID
        uniprot_id = None
        if bridge_result.extracted.uniprot_ids:
            uniprot_id = bridge_result.extracted.uniprot_ids[0]
        elif bridge_result.linked.uniprot_mappings:
            uniprot_id = bridge_result.linked.uniprot_mappings[0].kb_id
        
        if not uniprot_id:
            return {
                "status": "failed",
                "message": "No UniProt ID available for AlphaFold fallback",
            }
        
        try:
            result = await self.call_mcp_tool(
                "alphafold",
                "download_structure",
                {"uniprotId": uniprot_id}
            )
            
            if result.get("success"):
                return {
                    "status": "success",
                    "source": "alphafold",
                    "uniprot_id": uniprot_id,
                    "structure": result.get("result"),
                    "confidence_note": "AlphaFold prediction (not experimental structure)",
                }
            else:
                return {
                    "status": "failed",
                    "message": "AlphaFold fetch failed",
                    "error": result.get("error"),
                }
        
        except Exception as e:
            return {
                "status": "failed",
                "error": str(e),
            }
    
    async def _ensure_structures(self, identifiers: Dict[str, List[str]], query: str, use_pdb: bool, use_alphafold: bool) -> List[str]:
        if not self.config.mcp_enabled or not _ensure_mcp_runtime():
            return []

        resolved_uniprot = await self._resolve_uniprot_accessions(query=query, identifiers=identifiers)
        effective_identifiers = {
            "uniprot": sorted(set((identifiers.get("uniprot") or []) + resolved_uniprot)),
            "pdb": sorted(set(identifiers.get("pdb") or [])),
        }

        protein_hint = self._best_protein_hint(effective_identifiers) or "unknown"
        found: List[str] = []

        if use_pdb:
            pdb_ids = list(effective_identifiers.get("pdb") or [])
            if not pdb_ids and effective_identifiers.get("uniprot"):
                try:
                    search_res = await self.call_mcp_tool(
                        "pdb",
                        "search_by_uniprot",
                        {"uniprot_id": effective_identifiers["uniprot"][0], "limit": 5},
                    )
                    if search_res.get("success"):
                        pdb_ids = self._extract_pdb_ids_from_search_result(search_res.get("result"))
                except Exception as exc:
                    logger.warning("Failed PDB search by UniProt during structure ensure: %s", exc)

            if pdb_ids:
                try:
                    res = await self.call_mcp_tool("pdb", "download_structure", {"pdb_id": pdb_ids[0]})
                    if res.get("success"):
                        found.extend(self._persist_structure_artifacts("pdb", protein_hint, res.get("result")))
                except Exception as exc:
                    logger.warning("Failed PDB download during structure ensure: %s", exc)

        if use_alphafold and effective_identifiers.get("uniprot"):
            try:
                res = await self.call_mcp_tool(
                    "alphafold",
                    "download_structure",
                    {"uniprotId": effective_identifiers["uniprot"][0]},
                )
                if res.get("success"):
                    found.extend(self._persist_structure_artifacts("alphafold", protein_hint, res.get("result")))
            except Exception as exc:
                logger.warning("Failed AlphaFold download during structure ensure: %s", exc)

        return found

    def _make_attachment(self, file_path: str, description: str) -> Attachment:
        return _make_attachment_fn(file_path, description, attachment_cls=Attachment)

    def _build_minimal_lab_report(
        self,
        *,
        worker_name: str,
        query: str,
        findings_text: str,
        quantitative_metrics: Dict[str, float],
        raw_attachments: List[Any],
    ) -> Any:
        return build_minimal_lab_report(
            worker_name=worker_name,
            query=query,
            findings_text=findings_text,
            quantitative_metrics=quantitative_metrics,
            raw_attachments=raw_attachments,
        )

    # ====================================================================
    # ATOM MEMORY HELPERS
    # ====================================================================

    async def _record_atom_entry(
        self,
        text: str,
        observation_time: Optional[datetime] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        self._ensure_atom_memory_ready()
        await record_atom_entry(self.atom_memory, text, observation_time, metadata)

    async def _record_session_event_in_atom(
        self,
        session: AgenticSession,
        state: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._ensure_atom_memory_ready()
        await record_session_event_in_atom(self.atom_memory, session.session_id, state, payload)

    async def _record_lab_report_to_atom(
        self,
        subtask_id: str,
        lab_report: LabReport,
    ) -> None:
        self._ensure_atom_memory_ready()
        await record_lab_report_to_atom(self.atom_memory, subtask_id, lab_report, self._report_to_text)

    async def _record_quality_scores_to_atom(self, session: AgenticSession) -> None:
        self._ensure_atom_memory_ready()
        await record_quality_scores_to_atom(self.atom_memory, session.quality_scores)

    async def _maybe_run_proactive_gap_scan(self, session: AgenticSession) -> None:
        self._ensure_atom_memory_ready()
        async def _relay_event(state, payload):
            await self._record_session_event_in_atom(session, state, payload)
        await maybe_run_proactive_gap_scan(
            self.atom_memory,
            self.config.proactive_gap_detection,
            self.config.quality_threshold,
            session.logs,
            _relay_event,
        )

    async def _query_atom_for_gap_signals(
        self,
        recent_facts: Sequence[TemporalQuintuple],
    ) -> List[Dict[str, Any]]:
        self._ensure_atom_memory_ready()
        return await query_atom_for_gap_signals(self.atom_memory, recent_facts, self.config.quality_threshold)


    # ========================================================================
    # LANGGRAPH STATELESS NODES (v3.0)
    # ========================================================================
    
    def _node_initialize(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Initialize workflow — delegates to langgraph.nodes."""
        return _node_initialize_fn(
            state,
            enable_thermodynamic_cognition=bool(getattr(self.config, "enable_thermodynamic_cognition", False)),
            attractor_state_cls=CognitiveAttractorState,
            emit_event_fn=self._emit_event,
        )

    def _node_thermostat(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Thermodynamic Regulation — delegates to langgraph.nodes."""
        return _node_thermostat_fn(
            state,
            biorouter=self.biorouter,
            attractor_state_cls=CognitiveAttractorState,
        )

    def _node_analyze(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Analyze intent — delegates to langgraph.nodes."""
        return _node_analyze_fn(state, emit_event_fn=self._emit_event)
    
    def _node_route(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Route to specialists — delegates to langgraph.nodes.
        
        NewDawn: records routing decision in DecisionLedger (WI-07).
        """
        result = _node_route_fn(state)
        try:
            route_target = result.get("route_target", "unknown")
            self._decision_ledger.record(LedgerEntry(
                node="route",
                decision=f"routed → {route_target}",
                alternatives_considered=result.get("_route_alternatives", []),
                rejection_reasons=result.get("_route_rejection_reasons", []),
                evidence=result.get("_routing_evidence", "thermodynamic route plan"),
                quality_score=None,
                iteration=int(state.get("iteration_count", 0)),
            ))
        except Exception:
            pass
        return result
    
    def _node_decompose(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Decompose into subtasks — delegates to langgraph.nodes."""
        return _node_decompose_fn(state)
    
    def _node_assign(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Assign subtasks to workers — delegates to langgraph.nodes."""
        return _node_assign_fn(
            state,
            registry=self.registry,
            specialist_drivers=self.specialist_drivers,
        )
    
    async def _node_execute(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Execute assigned tasks — NewDawn enhanced.

        Enhancement over base node:
        1. Evaluates pre_tool cues before execution.
        2. Delegates to langgraph.nodes.node_execute for actual work.
        3. Evaluates post_tool cues after execution.
        4. Records all cue results in the DecisionLedger.
        5. Injects failpoints from previous iteration into execution context.
        """
        # ── Pre-tool cue evaluation ──
        if self._depth_preset.cue_mode != "off":
            try:
                cues = CUE_REGISTRY.load_cue_pack("default_scientific_light")
                cue_dicts = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in cues]
                pre_results = self._cue_evaluator.evaluate_batch(
                    cue_dicts, dict(state), phase_filter="pre_tool",
                )
                for cr in pre_results:
                    self._decision_ledger.record_cue_result(
                        node="execute",
                        cue_id=cr.cue_id,
                        passed=cr.passed,
                        evidence=cr.evidence,
                        action=cr.recommended_action,
                        msrp_phase=cr.phase_triggered,
                        tokens=cr.estimated_tokens,
                        iteration=int(state.get("iteration_count", 0)),
                    )
                    if self._audit_builder:
                        self._audit_builder.add_cue_result({
                            "cue_id": cr.cue_id, "passed": cr.passed,
                            "phase": "pre_tool", "action": cr.recommended_action,
                        })
                # In blocking mode, halt on blocking failures
                if (
                    self._depth_preset.cue_mode == "blocking"
                    and self._cue_evaluator.has_blocking_failure(pre_results)
                ):
                    logger.warning("[EXECUTE] Blocking pre-tool cue failure — injecting correction context")
                    # Don't abort; inject the failure as correction context for the LLM
                    failed = [r for r in pre_results if not r.passed]
                    state["_cue_correction_context"] = "; ".join(
                        f"[{r.cue_id}] {r.evidence}" for r in failed
                    )
            except Exception as exc:
                logger.warning("[EXECUTE] Pre-tool cue evaluation failed (non-fatal): %s", exc)

        # ── Inject peer review failpoints from previous iteration ──
        failpoints = state.get("_peer_review_failpoints")
        if failpoints:
            correction = state.get("_msrp_correction_prompt", "")
            if correction:
                state["_execution_guidance"] = (
                    f"PREVIOUS ITERATION FEEDBACK — address these specific issues:\n"
                    f"Failpoints: {'; '.join(failpoints[:5])}\n"
                    f"MSRP Correction: {correction}"
                )
            else:
                state["_execution_guidance"] = (
                    f"PREVIOUS ITERATION FEEDBACK — address these specific issues:\n"
                    f"{'; '.join(failpoints[:5])}"
                )

        # ── Record routing decision in ledger ──
        self._decision_ledger.record(LedgerEntry(
            node="execute",
            decision="execute_tasks",
            iteration=int(state.get("iteration_count", 0)),
            evidence=f"task_count={len(state.get('assigned_workers') or {})}",
            metadata={"depth_preset": self._depth_preset.name},
        ))

        # ── Delegate to base node ──
        result_state = await _node_execute_fn(
            state,
            emit_event_fn=self._emit_event,
            specialist_drivers=self.specialist_drivers,
            execute_worker_fn=self.execute_worker,
            execute_with_mcp_fn=self._execute_with_mcp,
            session_cls=AgenticSession,
        )

        # ── Post-tool cue evaluation ──
        if self._depth_preset.cue_mode != "off":
            try:
                cues = CUE_REGISTRY.load_cue_pack("default_scientific_light")
                cue_dicts = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in cues]
                post_results = self._cue_evaluator.evaluate_batch(
                    cue_dicts, dict(result_state), phase_filter="post_tool",
                )
                self._active_cue_results = post_results  # Save for quality gate
                for cr in post_results:
                    self._decision_ledger.record_cue_result(
                        node="execute",
                        cue_id=cr.cue_id,
                        passed=cr.passed,
                        evidence=cr.evidence,
                        action=cr.recommended_action,
                        msrp_phase=cr.phase_triggered,
                        tokens=cr.estimated_tokens,
                        iteration=int(result_state.get("iteration_count", 0)),
                    )
                    if self._audit_builder:
                        self._audit_builder.add_cue_result({
                            "cue_id": cr.cue_id, "passed": cr.passed,
                            "phase": "post_tool", "action": cr.recommended_action,
                        })
            except Exception as exc:
                logger.warning("[EXECUTE] Post-tool cue evaluation failed (non-fatal): %s", exc)

        return result_state

    async def _node_quality_gate(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Evaluate quality — NewDawn enhanced with promotion cues."""
        # ── Apply depth preset thresholds ──
        if "quality_threshold" not in state or state.get("_depth_preset_applied") is None:
            state["quality_threshold"] = self._depth_preset.quality_threshold
            state["max_iterations"] = self._depth_preset.max_iterations
            state["_depth_preset_applied"] = True

        result_state = await _node_quality_gate_fn(
            state,
            emit_event_fn=self._emit_event,
            quality_evaluator=self.quality_evaluator,
            config=self.config,
            extract_identifiers_fn=self._extract_identifiers,
            merge_identifiers_fn=self._merge_identifiers,
            ensure_structures_fn=self._ensure_structures,
            best_protein_hint_fn=self._best_protein_hint,
        )

        # ── Promotion cue evaluation (before quality decision is final) ──
        if self._depth_preset.cue_mode != "off":
            try:
                cues = CUE_REGISTRY.load_cue_pack("default_scientific_light")
                cue_dicts = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in cues]
                promotion_results = self._cue_evaluator.evaluate_batch(
                    cue_dicts, dict(result_state), phase_filter="promotion",
                )
                # Merge into active_cue_results for the router to process
                self._active_cue_results.extend(promotion_results)
                for cr in promotion_results:
                    self._decision_ledger.record_cue_result(
                        node="quality_gate",
                        cue_id=cr.cue_id,
                        passed=cr.passed,
                        evidence=cr.evidence,
                        action=cr.recommended_action,
                        msrp_phase=cr.phase_triggered,
                        tokens=cr.estimated_tokens,
                        iteration=int(result_state.get("iteration_count", 0)),
                    )
                    if self._audit_builder:
                        self._audit_builder.add_cue_result({
                            "cue_id": cr.cue_id, "passed": cr.passed,
                            "phase": "promotion", "action": cr.recommended_action,
                        })
            except Exception as exc:
                logger.warning("[QUALITY_GATE] Promotion cue evaluation failed (non-fatal): %s", exc)

        return result_state
    
    def _node_synthesize(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Synthesize final result — delegates to langgraph.nodes."""
        return _node_synthesize_fn(state, derive_claims_fn=self._derive_claims_and_sources)
    
    def _node_proactive_monitor(self, state: MICAState) -> MICAState:
        """STATELESS NODE: Proactive gap detection — delegates to langgraph.nodes."""
        return _node_proactive_monitor_fn(
            state,
            proactive_system=self.proactive_system,
            get_spawn_count=lambda: getattr(self, '_instance_spawn_count', 0),
            set_spawn_count=lambda v: setattr(self, '_instance_spawn_count', v),
        )
    
    # ========================================================================
    # CONDITIONAL EDGE ROUTERS (v3.0) — NewDawn Enhanced
    # ========================================================================
    
    def _router_quality_gate(self, state: MICAState) -> str:
        """CONTROL FLOW ROUTER: Quality gate with active enforcement.

        Enhancement over base router:
        1. Records quality gate decision in DecisionLedger.
        2. On iterate: extracts peer review failpoints via MSRPPressureEngine
           and injects them into state so the next execution pass addresses
           specific weaknesses instead of blindly retrying.
        3. Dispatches cue fail_actions to MSRP phases via MSRPPhaseDispatcher.
        4. Tracks quality trajectory in the audit builder.
        """
        decision = _router_quality_gate_fn(state)
        iteration = int(state.get("iteration_count", 0))
        quality_score = float(state.get("quality_score", 0.0))

        # ── Record in DecisionLedger ──
        alternatives = ["continue", "iterate", "escalate"]
        alternatives.remove(decision)
        self._decision_ledger.record_quality_gate(
            decision=decision,
            quality_score=quality_score,
            iteration=iteration,
            alternatives=alternatives,
            rejections=[
                f"quality {quality_score:.2f} {'<' if decision != 'continue' else '>='} "
                f"threshold {state.get('quality_threshold', 0.85)}"
            ],
        )

        # ── Compute PaperComparableScore (WI-13 consumer) ──
        try:
            cue_satisfied = sum(1 for c in self._active_cue_results if c.passed)
            cue_expected = len(self._active_cue_results) or None
            msrp_phases = list(state.get("_msrp_phases_completed", []))
            pcs = paper_comparable_score(
                nature_quality_score=quality_score if quality_score > 0 else None,
                msrp_phases_completed=msrp_phases or None,
                atom_mean_confidence=state.get("_atom_mean_confidence"),
                atom_facts_consumed=int(state.get("_atom_facts_consumed", 0)),
                atom_contradictions=int(state.get("_atom_contradictions", 0)),
                peer_assessment=state.get("_peer_assessment"),
                cue_satisfied=cue_satisfied,
                cue_expected=cue_expected,
            )
            state["_paper_comparable_score"] = pcs.composite_score
            state["_paper_comparable_tier"] = pcs.tier
            state["_paper_comparable_improvement"] = pcs.improvement_actions
        except Exception as exc:
            logger.debug("[QUALITY_GATE] PaperComparableScore computation skipped: %s", exc)

        # ── Track quality trajectory in audit builder ──
        if self._audit_builder is not None:
            self._audit_builder.add_quality_point(
                iteration=iteration,
                score=quality_score,
                converged=(decision == "continue"),
                feedback=state.get("_peer_feedback_summary", ""),
            )

        # ── On iterate: inject peer review failpoints into state ──
        if decision == "iterate" and self._depth_preset.peer_review_mode != "skip":
            try:
                lab_report_models = state.get("lab_report_models") or {}
                for _sub_id, report in lab_report_models.items():
                    feedback = self.pressure_engine.generate_peer_feedback(report)
                    if feedback:
                        failpoints = getattr(feedback, "issues", []) or []
                        failpoint_texts = [
                            str(getattr(fp, "description", fp)) for fp in failpoints
                        ]
                        if failpoint_texts:
                            state["_peer_review_failpoints"] = failpoint_texts
                            state["_peer_feedback_summary"] = "; ".join(failpoint_texts[:5])
                            logger.info(
                                "[QUALITY_GATE] Injected %d failpoints for next iteration",
                                len(failpoint_texts),
                            )
                    break  # one report is enough for feedback
            except Exception as exc:
                logger.warning("[QUALITY_GATE] Peer feedback extraction failed (non-fatal): %s", exc)

            # ── Dispatch cue fail_actions via MSRPPhaseDispatcher ──
            for cue_result in self._active_cue_results:
                if not cue_result.passed and cue_result.recommended_action != "warn":
                    dispatch = self._msrp_dispatcher.dispatch(
                        cue_id=cue_result.cue_id,
                        fail_action=cue_result.recommended_action,
                        state=dict(state),
                    )
                    if dispatch.msrp_phase:
                        state["_msrp_correction_prompt"] = dispatch.specialist_prompt
                        state["_msrp_correction_phase"] = dispatch.msrp_phase
                        self._decision_ledger.record(LedgerEntry(
                            node="quality_gate",
                            decision=f"msrp_dispatch:{dispatch.msrp_phase}",
                            cue_triggered=cue_result.cue_id,
                            cue_passed=False,
                            msrp_phase_activated=dispatch.msrp_phase,
                            tokens_spent=dispatch.estimated_tokens,
                            iteration=iteration,
                            evidence=dispatch.rationale,
                        ))
                        if self._audit_builder:
                            self._audit_builder.add_msrp_phase(dispatch.msrp_phase)
                        logger.info(
                            "[QUALITY_GATE] MSRP dispatch: %s → %s",
                            cue_result.cue_id, dispatch.msrp_phase,
                        )
                        break  # one correction per iteration to avoid overload

        return decision
    
    def _router_proactive_monitor(self, state: MICAState) -> str:
        """CONTROL FLOW ROUTER: Proactive monitoring — delegates to langgraph.nodes."""
        return _router_proactive_monitor_fn(
            state,
            spawn_count=getattr(self, '_instance_spawn_count', state.get("_proactive_spawn_count", 0)),
        )


# CONVENIENCE FUNCTIONS
# ============================================================================

async def create_agentic_driver(
    mcp_config_path: str = "./mcp_servers_17_complete.json",
    checkpoint_dir: str = "./.checkpoints",
    **kwargs
) -> AgenticDriver:
    """
    Convenience function to create and initialize AgenticDriver.
    
    Args:
        mcp_config_path: Path to MCP server configuration JSON
        checkpoint_dir: Directory for LangGraph checkpoints
        **kwargs: Additional configuration options
        
    Returns:
        Initialized AgenticDriver instance
        
    Example:
        >>> driver = await create_agentic_driver(
        ...     mcp_config_path="./mcp_servers.json",
        ...     checkpoint_dir="./.checkpoints"
        ... )
        >>> result = await driver.process_agentic_prompt("Simulate p53")
    """
    
    config = AgenticDriverConfig.from_driver_config(
        mcp_config_path=mcp_config_path,
        checkpoint_dir=checkpoint_dir,
        **kwargs,
    )
    
    driver = AgenticDriver(config=config)
    await driver.initialize_async()
    
    return driver

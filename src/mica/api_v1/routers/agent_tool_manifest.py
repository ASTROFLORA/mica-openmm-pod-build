"""agent_tool_manifest.py — Endpoint and model definitions for MICA Agent Tool Manifest."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from mica.api_v1.auth import user_dependency
from mica.agentic.backend_command_manifest import BACKEND_COMMAND_MANIFEST
from mica.tools_authority.tool_alias_registry import canonical_tool_name_for_command
from mica.tools_authority.tool_surface_exporter import (
    classify_manifest_entry as _classify_manifest_entry,
    effect_for_manifest_entry as _effect_for_manifest_entry,
    is_safe_for_agent as _is_safe_for_agent,
    lifecycle_state_for_manifest_entry as _lifecycle_state_for_manifest_entry,
    resolve_command_family as _resolve_command_family,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kernel", tags=["agent-tools"])
_BACKING_PROBE_TIMEOUT_SEC = float(os.getenv("MICA_AGENT_TOOL_BACKING_PROBE_TIMEOUT_SEC", "3.0"))


class AgentToolItem(BaseModel):
    tool_name: str
    command_name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    effect: str  # "read", "propose", "execute"
    lifecycle_state: str  # "production", "preview", "blocked", "deprecated"
    blocked: bool
    blocker_code: Optional[str] = None
    requires_approval: bool
    requires_scope: List[str]
    durability: str  # "durable", "non_durable"
    degradation_state: Dict[str, Any]
    safe_for_agent: bool
    classification: str  # "agent_safe_read", "agent_safe_propose", "agent_requires_approval", "internal_only", "blocked"
    examples: List[Dict[str, Any]]


def _get_classification(name: str, entry: Any) -> str:
    return _classify_manifest_entry(name, entry)


def _get_tool_name(command_name: str) -> str:
    return canonical_tool_name_for_command(command_name)


async def _resolve_backing_statuses() -> tuple[tuple[str, str, str, Optional[str]], tuple[str, str, str, Optional[str]], tuple[str, str, str, Optional[str]]]:
    from mica.agentic.commands.kb_commands import (
        _get_kb_service_with_backing,
        _get_graphrag_store_with_backing,
        _get_neon_status,
    )

    async def _bounded(awaitable: Any, fallback: tuple[str, str, str, Optional[str]], *, label: str):
        try:
            return await asyncio.wait_for(awaitable, timeout=_BACKING_PROBE_TIMEOUT_SEC)
        except Exception as exc:
            logger.warning("%s backing probe degraded: %s", label, exc)
            return fallback

    kb_result, gr_result, ne_result = await asyncio.gather(
        _bounded(
            _get_kb_service_with_backing(),
            ("unavailable", "non_durable", "degraded", f"Timed out or failed after {_BACKING_PROBE_TIMEOUT_SEC:.1f}s"),
            label="kb",
        ),
        _bounded(
            _get_graphrag_store_with_backing(),
            ("unavailable", "non_durable", "degraded", f"Timed out or failed after {_BACKING_PROBE_TIMEOUT_SEC:.1f}s"),
            label="graphrag",
        ),
        _bounded(
            _get_neon_status(),
            ("unavailable", "non_durable", "degraded", f"Timed out or failed after {_BACKING_PROBE_TIMEOUT_SEC:.1f}s"),
            label="artifact",
        ),
    )

    if len(kb_result) == 5:
        _, kb_backing, kb_durability, kb_trust, kb_reason = kb_result
    else:
        kb_backing, kb_durability, kb_trust, kb_reason = kb_result

    if len(gr_result) == 5:
        _, gr_backing, gr_durability, gr_trust, gr_reason = gr_result
    else:
        gr_backing, gr_durability, gr_trust, gr_reason = gr_result

    ne_backing, ne_durability, ne_trust, ne_reason = ne_result
    return (
        (kb_backing, kb_durability, kb_trust, kb_reason),
        (gr_backing, gr_durability, gr_trust, gr_reason),
        (ne_backing, ne_durability, ne_trust, ne_reason),
    )


@router.get("/agent-tools", response_model=List[AgentToolItem])
async def list_agent_tools(
    workspace_id: Optional[str] = Query(None, alias="workspace"),
    study_id: Optional[str] = Query(None, alias="study"),
    effect: Optional[str] = None,
    safe_for_agent: Optional[bool] = None,
    family: Optional[str] = None,
    user_id: str = Depends(user_dependency),
):
    """List agent-facing tools derived from the Command Kernel manifest."""
    # Dynamic backing stores evaluation, but bounded so the manifest stays usable.
    (kb_backing, kb_durability, kb_trust, kb_reason), (
        gr_backing,
        gr_durability,
        gr_trust,
        gr_reason,
    ), (
        ne_backing,
        ne_durability,
        ne_trust,
        ne_reason,
    ) = await _resolve_backing_statuses()

    tools: List[AgentToolItem] = []

    # 1. Map existing manifest commands
    for name, entry in BACKEND_COMMAND_MANIFEST.items():
        classification = _get_classification(name, entry)
        
        # Safe for agent criteria
        tool_safe = _is_safe_for_agent(name, entry)
        
        # requires_approval criteria
        requires_approval = classification in ("agent_requires_approval", "blocked") or bool(
            entry.requires_gate or entry.side_effects or entry.canonical_mutation
        )

        lifecycle_state = _lifecycle_state_for_manifest_entry(entry)

        # Backing store logic
        durability = "durable"
        degradation = {}
        
        if name.startswith("kb."):
            durability = kb_durability
            if kb_backing != "durable":
                degradation = {"backing": kb_backing, "reason": kb_reason, "trust_state": kb_trust}
        elif name.startswith("graphrag."):
            durability = gr_durability
            if gr_backing != "durable":
                degradation = {"backing": gr_backing, "reason": gr_reason, "trust_state": gr_trust}
        elif name in {"artifact.attach_to_study", "artifact.attach_to_working_set"}:
            durability = ne_durability
            if ne_backing != "durable":
                degradation = {"backing": ne_backing, "reason": ne_reason, "trust_state": ne_trust}

        tool_name = _get_tool_name(name)

        # Filters
        if effect and _effect_for_manifest_entry(entry) != effect:
            continue
        if safe_for_agent is not None and tool_safe != safe_for_agent:
            continue
        family_resolved = _resolve_command_family(name, entry)

        if family and family_resolved != family:
            continue

        tools.append(
            AgentToolItem(
                tool_name=tool_name,
                command_name=name,
                description=entry.description,
                input_schema=entry.input_schema,
                output_schema=entry.output_schema,
                effect=_effect_for_manifest_entry(entry),
                lifecycle_state=lifecycle_state,
                blocked=entry.implemented_status != "implemented",
                blocker_code=entry.implemented_status if entry.implemented_status != "implemented" else None,
                requires_approval=requires_approval,
                requires_scope=list(entry.required_scope),
                durability=durability,
                degradation_state=degradation,
                safe_for_agent=tool_safe,
                classification=classification,
                examples=[]
            )
        )

    # 2. Add synthesized/wrapper endpoints that represent essential agent surfaces
    # e.g., mica.command.run, mica.protocol.submit
    existing_command_names = {tool.command_name for tool in tools}
    synthesized = [
        {
            "tool_name": "mica.command.run",
            "command_name": "protocol.execute",
            "description": "Execute a permitted command through the unified entry point.",
            "effect": "execute",
            "lifecycle_state": "production",
            "blocked": False,
            "requires_approval": True,
            "requires_scope": [],
            "durability": "durable",
            "safe_for_agent": True,
            "classification": "agent_safe_propose"
        },
        {
            "tool_name": "mica.protocol.submit",
            "command_name": "protocol.submit",
            "description": "Submit a validated ProtocolJSONLDDocument to the background executor queue.",
            "effect": "execute",
            "lifecycle_state": "production",
            "blocked": False,
            "requires_approval": True,
            "requires_scope": ["workspace_id", "study_id"],
            "durability": "durable",
            "safe_for_agent": True,
            "classification": "agent_safe_propose"
        }
    ]

    for syn in synthesized:
        if syn["command_name"] in existing_command_names:
            continue
        # Match filters on synthesized tools
        if effect and syn["effect"] != effect:
            continue
        if safe_for_agent is not None and syn["safe_for_agent"] != safe_for_agent:
            continue
        if family and family != "protocol":
            continue

        tools.append(
            AgentToolItem(
                tool_name=syn["tool_name"],
                command_name=syn["command_name"],
                description=syn["description"],
                input_schema={},
                output_schema={},
                effect=syn["effect"],
                lifecycle_state=syn["lifecycle_state"],
                blocked=syn["blocked"],
                requires_approval=syn["requires_approval"],
                requires_scope=syn["requires_scope"],
                durability=syn["durability"],
                degradation_state={},
                safe_for_agent=syn["safe_for_agent"],
                classification=syn["classification"],
                examples=[]
            )
        )

    return tools

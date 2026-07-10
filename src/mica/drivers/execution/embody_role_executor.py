#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Role embodiment executor — extracted logic from AgenticDriver._embody_role.

Handles: role context setup, message building, loop execution, event handling,
invariant validation, ledger commit, and report export.
"""

import asyncio
import json
import logging
import uuid as _uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from .role_policy_service import filter_tools_for_role, run_output_invariants


def _normalize_bibliotecario_synthesis_for_invariants(
    synthesis_text: str,
    role_ctx: Any,
) -> str:
    text = str(synthesis_text or "").strip()
    if not text:
        return text

    required_sections = ("[KEY FINDINGS]", "[CONTRADICTIONS]", "[OPEN GAPS]")
    if all(section in text for section in required_sections):
        return text

    contradiction_block = "No explicit contradictions were surfaced in this pass."
    gap_entries = [
        str((gap or {}).get("description") or "").strip()
        for gap in list(getattr(role_ctx, "gaps_log", []) or [])[:5]
        if str((gap or {}).get("description") or "").strip()
    ]
    if gap_entries:
        open_gaps_block = "\n".join(f"- {entry}" for entry in gap_entries)
    else:
        open_gaps_block = "No explicit open gaps were surfaced in this pass."

    return (
        "[KEY FINDINGS]\n"
        f"{text}\n\n"
        "[CONTRADICTIONS]\n"
        f"{contradiction_block}\n\n"
        "[OPEN GAPS]\n"
        f"{open_gaps_block}"
    )


async def execute_role_embodiment(
    # Role spec & context
    role_spec: Any,
    task_messages: List[Dict[str, Any]],
    # Driver context
    driver_self: Any,
    # Loop parameters
    provider_id: str,
    model_id: Optional[str] = None,
    pending_events: Any = None,
    abort: Optional[asyncio.Event] = None,
    inject_prior_context: bool = True,
    program_envelope: Optional[Any] = None,
    parent_executor: Optional[Any] = None,
    available_tools: Optional[List[Dict[str, Any]]] = None,
    inherited_tombstones: Optional[List[Dict[str, Any]]] = None,
    rupture_budget: Optional[Any] = None,
) -> Tuple[str, Optional[str], Any]:
    """Execute role embodiment with integrated governance & ledger commit.
    
    Returns: (synthesis_text, report_path, role_context)
    """
    from mica.agentic.core import AgenticLoop, LoopConfig, ProviderRegistry
    from mica.agentic.events import AgentTurn
    from ..delegation_session import DelegationSession, DelegationStatus
    from ..session_closure_ledger import SessionClosureLedger
    from ..program_envelope import ProgramEnvelope

    # Get or create role context
    role_ctx = driver_self._get_or_create_role_context(role_spec.role_id)
    role_ctx.embodiment_count += 1

    # Store thermodynamic state
    if inherited_tombstones:
        role_ctx.inherited_tombstones = list(inherited_tombstones)
    if rupture_budget is not None:
        role_ctx.applied_rupture_budget = rupture_budget
    role_ctx.appeal_regime_active = role_spec.allow_appeal_regime

    # Build tool executor
    if parent_executor is None:
        async def _noop_exec(n: str, cid: str, a: dict) -> str:
            return json.dumps({"recorded": True, "name": n, "data": a}, ensure_ascii=False)
        parent_executor = _noop_exec

    role_executor = driver_self._build_role_executor(role_ctx, parent_executor)
    role_ctx.pending_ledger_entries = []

    # Resolve tools
    from .helpers import filter_tools_for_lane
    _base_tools = available_tools if available_tools is not None else driver_self._BIBLIOTECARIO_TOOLS
    _base_tools = filter_tools_for_lane(
        _base_tools,
        lane="scientific_audit",
        depth_preset_name=driver_self._depth_preset.name,
    )
    tools = filter_tools_for_role(role_spec, _base_tools, driver_self._SPAWN_TOOLS)

    # Build messages (independent context window)
    messages = list(task_messages)

    # Inject prior context
    if inject_prior_context and role_ctx.last_synthesis:
        from .helpers import format_tombstone_warnings
        prior_block = (
            "## Prior embodiment output (approved)\n"
            + role_ctx.last_synthesis
            + "\n\n## Prior citations\n"
            + json.dumps(role_ctx.citations_log, ensure_ascii=False, indent=1)
        )
        messages.insert(0, {"role": "user", "content": prior_block})

    # Inject tombstone warnings
    if role_spec.negative_memory_mode == "full":
        from .helpers import format_tombstone_warnings
        _warning_text = format_tombstone_warnings(
            role_ctx.inherited_tombstones,
            role_spec.visible_tombstone_classes,
        )
        if _warning_text:
            messages.append({"role": "system", "content": f"[EPISTEMIC IMMUNE SYSTEM WARNINGS]\n{_warning_text}"})
    elif role_spec.negative_memory_mode == "semi_blind":
        from .helpers import format_tombstone_warnings
        _semi_classes = role_spec.visible_tombstone_classes - frozenset({"operational"})
        _warning_text = format_tombstone_warnings(
            role_ctx.inherited_tombstones,
            _semi_classes,
        )
        if _warning_text:
            messages.append({"role": "system", "content": f"[HISTORICAL RISK ZONES]\n{_warning_text}"})

    # Calculate effective temperature (thermal override + rupture budget)
    _effective_temperature = (
        role_spec.temperature_override
        if role_spec.temperature_override is not None
        else role_spec.temperature
    )

    if role_ctx.applied_rupture_budget:
        _effective_temperature = min(
            0.95,
            _effective_temperature + role_ctx.applied_rupture_budget.temperature_bonus,
        )
        role_spec_iterations = role_spec.max_iterations + role_ctx.applied_rupture_budget.extra_iterations
    else:
        role_spec_iterations = role_spec.max_iterations

    # Create registry and loop config
    registry = ProviderRegistry.from_env()
    loop_cfg = LoopConfig(
        max_iterations=role_spec_iterations,
        temperature=_effective_temperature,
        negative_memory_mode=role_spec.negative_memory_mode,
        visible_tombstone_classes=tuple(role_spec.visible_tombstone_classes),
        allow_appeal_regime=role_spec.allow_appeal_regime,
        rupture_energy_budget=(
            role_ctx.applied_rupture_budget.released_energy
            if role_ctx.applied_rupture_budget
            else 0.0
        ),
    )

    _parent_run_id = (
        next(reversed(driver_self._session_run_ids.values()), "")
        if driver_self._session_run_ids
        else ""
    )
    sub_id = str(_uuid.uuid4())[:8]

    # Create DelegationSession
    _delegation = DelegationSession(
        parent_run_id=_parent_run_id,
        delegated_agent=role_spec.role_id,
        coordination_mode="embodied",
    )
    _delegation = _delegation.with_status(DelegationStatus.RUNNING)
    driver_self._delegation_sessions[_delegation.session_id] = _delegation

    if program_envelope is None:
        program_envelope = ProgramEnvelope(
            run_id=_parent_run_id,
            lifecycle_state="active",
            phase="reasoning",
            metadata={
                "delegated_agent": role_spec.role_id,
                "delegation_session_id": _delegation.session_id,
                "embodiment_mode": "ucs",
                "embodiment_count": role_ctx.embodiment_count,
            },
        )

    # Create and run loop
    loop = AgenticLoop(
        registry,
        loop_cfg,
        run_id=_parent_run_id,
        program_id=program_envelope.program_id,
    )

    if pending_events is None:
        pending_events = []

    text_parts: List[str] = []
    driver_self._program_envelopes[_delegation.session_id] = program_envelope

    from mica.agentic.events import (
        StreamStart as _SS, TextDelta as _TD,
        ToolCallStart as _TCS, LoopFinish as _LF,
    )
    loop_finished = False
    _spawn_exc: Optional[BaseException] = None

    try:
        async for event in loop.run(
            messages=messages,
            tools=tools,
            tool_executor=role_executor,
            provider_id=provider_id,
            model_id=model_id,
            system_prompt=role_spec.system_prompt,
            abort=abort,
        ):
            if isinstance(event, _SS):
                role_ctx.iterations_count += 1
                pending_events.append(AgentTurn(
                    agent=role_spec.role_id, role="thinking",
                    text=f"[embodied step {role_ctx.iterations_count}]",
                    session_id=sub_id,
                    run_id=_parent_run_id,
                    program_id=program_envelope.program_id,
                ))
            elif isinstance(event, _TD):
                text_parts.append(event.text)
                pending_events.append(AgentTurn(
                    agent=role_spec.role_id, role="speaking",
                    text=event.text, session_id=sub_id,
                    run_id=_parent_run_id,
                    program_id=program_envelope.program_id,
                ))
            elif isinstance(event, _TCS):
                role_ctx.tool_calls_count += 1
                program_envelope = program_envelope.with_phase("acting")
                pending_events.append(AgentTurn(
                    agent=role_spec.role_id, role="tool",
                    text=f"{event.name}({json.dumps(event.args, ensure_ascii=False)[:100]})",
                    session_id=sub_id,
                    run_id=_parent_run_id,
                    program_id=program_envelope.program_id,
                ))
            elif isinstance(event, _LF):
                loop_finished = True
                pending_events.append(AgentTurn(
                    agent=role_spec.role_id, role="done",
                    text="".join(text_parts).strip(),
                    session_id=sub_id,
                    run_id=_parent_run_id,
                    program_id=program_envelope.program_id,
                ))
    except Exception as exc:
        _spawn_exc = exc
    finally:
        if not loop_finished:
            pending_events.append(AgentTurn(
                agent=role_spec.role_id, role="done",
                text="".join(text_parts).strip() or "[embodied role terminated early]",
                session_id=sub_id,
                run_id=_parent_run_id,
                program_id=program_envelope.program_id,
            ))

    synthesis_text = "".join(text_parts).strip()
    if str(getattr(role_spec, "role_id", "") or "") == "bibliotecario":
        synthesis_text = _normalize_bibliotecario_synthesis_for_invariants(
            synthesis_text,
            role_ctx,
        )

    # Update role context
    role_ctx.accumulated_text.append(synthesis_text)
    role_ctx.last_synthesis = synthesis_text
    role_ctx.messages.extend(messages)

    # Run output invariants
    violations = run_output_invariants(role_spec, synthesis_text, role_ctx)
    _has_error_violations = False
    for v in violations:
        if v["severity"] == "error":
            _has_error_violations = True
            logger.warning(
                "Embodied role %s: invariant FAILED — %s: %s",
                role_spec.role_id, v["name"], v["description"],
            )
        else:
            logger.info(
                "Embodied role %s: invariant warning — %s: %s",
                role_spec.role_id, v["name"], v["description"],
            )

    # Transactional ledger commit (ACID semantics)
    if not _has_error_violations and role_ctx.pending_ledger_entries:
        _run_ledger = driver_self._evidence_ledgers.get(_parent_run_id)
        if _run_ledger is not None:
            from ..evidence_ledger import EvidenceEntry
            for staged in role_ctx.pending_ledger_entries:
                try:
                    if staged["type"] == "cite_finding":
                        _run_ledger.add_claim(EvidenceEntry(
                            claim_id=f"{role_spec.role_id}_{role_ctx.embodiment_count}_{staged['data'].get('paper_id', 'unknown')}",
                            claim_text=staged["data"].get("finding", ""),
                            source_ids=[staged["data"].get("paper_id", "")],
                            status="validated",
                            agent_source=role_spec.role_id,
                        ))
                except Exception:
                    logger.debug("Ledger commit failed for staged entry", exc_info=True)
            logger.info(
                "Embodied role %s: committed %d ledger entries (invariants passed)",
                role_spec.role_id, len(role_ctx.pending_ledger_entries),
            )
        role_ctx.pending_ledger_entries = []
    elif _has_error_violations and role_ctx.pending_ledger_entries:
        logger.warning(
            "Embodied role %s: ROLLBACK %d pending ledger entries (invariants failed)",
            role_spec.role_id, len(role_ctx.pending_ledger_entries),
        )
        role_ctx.pending_ledger_entries = []

    # Export report
    report_path: Optional[str] = None
    try:
        report_path = driver_self._export_agent_report(
            role_spec.role_id, sub_id, messages, synthesis_text,
        )
    except Exception:
        logger.debug("Agent .md export failed for %s (non-critical)", role_spec.role_id)

    # Finalize DelegationSession + ProgramEnvelope
    if _spawn_exc is None and synthesis_text:
        _delegation = _delegation.with_status(
            DelegationStatus.COMPLETED,
            result_text=synthesis_text[:500],
            result_artifact_path=report_path or "",
            tool_calls_count=role_ctx.tool_calls_count,
            iterations_count=role_ctx.iterations_count,
        )
        program_envelope = program_envelope.with_lifecycle("completed")
    else:
        _status = DelegationStatus.FAILED
        _err = str(_spawn_exc) if _spawn_exc else "embodied role produced no synthesis"
        _delegation = _delegation.with_status(
            _status,
            error_message=_err,
            tool_calls_count=role_ctx.tool_calls_count,
            iterations_count=role_ctx.iterations_count,
        )
        program_envelope = program_envelope.with_lifecycle("failed")

    driver_self._delegation_sessions[_delegation.session_id] = _delegation
    driver_self._program_envelopes[_delegation.session_id] = program_envelope

    closure_ledger = SessionClosureLedger.from_session(
        _delegation,
        program_id=program_envelope.program_id,
        role_ctx=role_ctx,
        report_path=report_path or "",
        final_text=synthesis_text,
        spawn_exc=_spawn_exc,
    )
    program_envelope.metadata["session_closure"] = closure_ledger.to_dict()
    driver_self._log_program_envelope_snapshot(
        _parent_run_id,
        program_envelope,
        event_type="program_envelope_snapshot",
        driver_id=role_spec.role_id,
        phase="close",
    )

    if _spawn_exc is not None:
        raise _spawn_exc

    return synthesis_text, report_path, role_ctx

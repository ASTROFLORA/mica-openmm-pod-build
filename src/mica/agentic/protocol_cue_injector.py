from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.analysis import (
    PromptNode,
    PromptProtocol,
    ProtocolCue,
    ProtocolCueResult,
    ProtocolRuntimeEnvelope,
    ProtocolType,
    ScientificProtocol,
)

from mica.infrastructure.event_log import EventLog
from mica.infrastructure.persistence.pg_async import choose_timescale_database_url

from .epistemic_firewall import evaluate_intake_cues
from .protocol_selector import build_protocol_runtime
from .protocol_transport import (
    PLAN_PROGRESS,
    PROMOTION_GATE_RESULT,
    PROTOCOL_CUE,
    PROTOCOL_CUE_RESULT,
    PROTOCOL_CUE_TELEMETRY,
    PROTOCOL_METADATA,
    PROTOCOL_PHASE,
    stable_hash,
    telemetry_event,
    transport_event,
)
from .tool_capability_registry import get_tool_capability


logger = logging.getLogger(__name__)


def _protocol_node_cues(node: Any) -> List[ProtocolCue]:
    cues: List[ProtocolCue] = []
    for item in list(getattr(node, "scientific_cues", []) or []):
        if hasattr(item, "model_dump") and callable(getattr(item, "model_dump")):
            payload = item.model_dump(mode="json")
        elif isinstance(item, dict):
            payload = dict(item)
        else:
            continue
        payload.setdefault("target_prompt_node_id", str(getattr(node, "node_id", "") or "") or None)
        cues.append(ProtocolCue(**payload))
    return cues


class ScientificInterrupt(RuntimeError):
    def __init__(
        self,
        *,
        interrupt_type: str,
        cue_id: str,
        message: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        events: List[Dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.interrupt_type = interrupt_type
        self.cue_id = cue_id
        self.message = message
        self.tool_name = tool_name
        self.tool_args = dict(tool_args or {})
        self.events = list(events or [])


class ProtocolCueRuntimeManager:
    def __init__(
        self,
        *,
        query: str,
        tool_names: List[str],
        strictness: str = "scientific_light",
        run_id: str = "",
        transport: str = "direct",
        protocol_runtime_override: Optional[tuple[ScientificProtocol, PromptProtocol, ProtocolRuntimeEnvelope, Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self.query = str(query or "")
        self.transport = str(transport or "direct")
        self.run_id = str(run_id or "")
        if protocol_runtime_override is None:
            self.scientific_protocol, self.prompt_protocol, self.envelope, self.benchmark_case = build_protocol_runtime(
                query=self.query,
                tool_names=tool_names,
                strictness=strictness,
            )
        else:
            self.scientific_protocol, self.prompt_protocol, self.envelope, self.benchmark_case = protocol_runtime_override
        self.protocol_events: List[Dict[str, Any]] = []
        self.event_log = EventLog(run_id=self.run_id or self.envelope.protocol_id)
        self._heavy_event_store: Optional[Any] = None
        self._heavy_event_store_init_attempted = False
        self._tool_to_node: Dict[str, str] = {node.tool_name: node.node_id for node in self.prompt_protocol.nodes}
        self._tool_gate_step = 0
        self._record_initial_state()

    def _heavy_event_store_enabled(self) -> bool:
        override = str(os.getenv("MICA_PROTOCOL_CUE_HEAVY_EVENT_STORE") or "").strip().lower()
        if override:
            return override in {"1", "true", "yes", "on"}
        return self.transport not in {"direct", "ws", "rest"}

    @classmethod
    def for_protocol_node(
        cls,
        *,
        node: Any,
        protocol_id: str,
        session_id: str,
        run_id: str = "",
        strictness: str = "scientific_light",
        transport: str = "protocol_executor",
    ) -> "ProtocolCueRuntimeManager":
        node_id = str(getattr(node, "node_id", "protocol-node") or "protocol-node")
        tool_name = str(getattr(node, "inputs", {}).get("tool_name") or getattr(node, "executor_id", "protocol_tool") or "protocol_tool")
        query = str(getattr(node, "inputs", {}).get("query") or getattr(node, "objective", node_id) or node_id)
        cues = _protocol_node_cues(node)
        prompt_protocol = PromptProtocol(
            protocol_type=ProtocolType.PROTEIN_FUNCTION_ANALYSIS,
            nodes=[
                PromptNode(
                    node_id=node_id,
                    tool_name=tool_name,
                    parameters=dict(getattr(node, "inputs", {}) or {}),
                    dependencies=list(getattr(node, "dependencies", []) or []),
                )
            ],
            created_by="protocol_node_scientific_policy",
            estimated_duration=max(60, len(cues) * 15 or 60),
        )
        envelope = ProtocolRuntimeEnvelope(
            protocol_id=protocol_id,
            protocol_label=str(getattr(node, "objective", node_id) or node_id),
            prompt_protocol_hash=stable_hash({"protocol_id": protocol_id, "node_id": node_id, "tool_name": tool_name}),
            study_type=str(getattr(node, "node_kind", "protocol_node") or "protocol_node"),
            cue_pack_id=f"protocol_node:{node_id}",
            strictness=str(strictness or "scientific_light"),
            cues=cues,
            cue_counts={
                "total": len(cues),
                "pending": len(cues),
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            },
        )
        scientific_protocol = ScientificProtocol(
            type="node",
            input={
                "query": query,
                "graph_protocol_id": protocol_id,
                "session_id": session_id,
                "node_id": node_id,
            },
            plan={
                "steps": [
                    {
                        "prompt_node_id": node_id,
                        "tool_name": tool_name,
                        "dependencies": list(getattr(node, "dependencies", []) or []),
                    }
                ],
                "dependencies": [list(getattr(node, "dependencies", []) or [])],
                "estimated_resources": {"tool_count": 1},
                "confidence": 0.5,
            },
            metadata={
                "version": "1.0.0",
                "contributors": ["mica-core", "protocol-node-scientific-policy"],
                "tags": [str(getattr(node, "node_kind", "protocol_node") or "protocol_node"), strictness],
                "citations": [],
                "protocol_runtime": {
                    "projection_only": True,
                    "graph_protocol_id": protocol_id,
                    "session_id": session_id,
                    "node_id": node_id,
                    "active_phase": "intake",
                    "strictness": strictness,
                    "source": "protocol_node",
                },
            },
        )
        return cls(
            query=query,
            tool_names=[tool_name],
            strictness=strictness,
            run_id=run_id or protocol_id,
            transport=transport,
            protocol_runtime_override=(scientific_protocol, prompt_protocol, envelope, None),
        )

    def _resolve_heavy_event_store(self) -> Optional[Any]:
        if self._heavy_event_store is not None or self._heavy_event_store_init_attempted:
            return self._heavy_event_store
        self._heavy_event_store_init_attempted = True
        if not self._heavy_event_store_enabled():
            return None

        connection_string = str(os.getenv("MICA_EVENT_STORE_DSN") or "").strip()
        if not connection_string:
            try:
                connection_string = str(choose_timescale_database_url() or "").strip()
            except Exception:
                connection_string = ""
        if not connection_string:
            return None

        try:
            from mica.memory.event_store import EventStore

            store = EventStore(connection_string=connection_string)
            store.initialize_schema(enable_timescaledb=True)
            self._heavy_event_store = store
        except Exception as exc:
            logger.warning("ProtocolCueRuntimeManager could not initialize EventStore: %s", exc)
            self._heavy_event_store = None
        return self._heavy_event_store

    def _persist_heavy_protocol_events(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        store = self._resolve_heavy_event_store()
        if store is None:
            return
        try:
            from mica.memory.events import Event, EventType

            payload_events = [
                Event(
                    event_type=EventType(str(item["event_type"])),
                    node_id=str(item["node_id"]),
                    timestamp=datetime.utcnow(),
                    data=dict(item.get("data") or {}),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in events
            ]
            if len(payload_events) == 1:
                store.append(payload_events[0])
            else:
                store.append_batch(payload_events)
        except Exception as exc:
            logger.warning("ProtocolCueRuntimeManager failed to persist heavy protocol events: %s", exc)

    def _heavy_event_metadata(self, *, phase: str, cue_id: str = "", target_prompt_node_id: str = "") -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "protocol_id": self.envelope.protocol_id,
            "run_id": self.run_id or self.envelope.protocol_id,
            "transport": self.transport,
            "phase": phase,
            "study_type": self.envelope.study_type,
            "strictness": self.envelope.strictness,
            "prompt_protocol_hash": self.envelope.prompt_protocol_hash,
            "benchmark_case_id": (self.benchmark_case or {}).get("id"),
        }
        if cue_id:
            metadata["cue_id"] = cue_id
        if target_prompt_node_id:
            metadata["target_prompt_node_id"] = target_prompt_node_id
        return metadata

    def _append_event(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        entry = transport_event(event_type, payload)
        self.protocol_events.append(entry)
        return entry

    def _append_telemetry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        entry = self._append_event(PROTOCOL_CUE_TELEMETRY, payload)
        try:
            self.event_log.append(
                event_type=str(payload.get("event_type") or "protocol_cue_event"),
                payload={"data": payload.get("data", {}), "metadata": payload.get("metadata", {})},
                phase=self.envelope.active_phase,
            )
        except Exception:
            pass
        return entry

    def _cue_status_counts(self) -> Dict[str, int]:
        counts = {"total": len(self.envelope.cues), "pending": 0, "passed": 0, "failed": 0, "skipped": 0}
        for cue in self.envelope.cues:
            result = self._latest_result(cue.cue_id)
            if result is None:
                counts["pending"] += 1
            else:
                counts[str(result.status)] += 1
        return counts

    def _latest_result(self, cue_id: str) -> Optional[ProtocolCueResult]:
        for result in reversed(self.envelope.cue_results):
            if result.cue_id == cue_id:
                return result
        return None

    def _record_result(self, cue: ProtocolCue, result: ProtocolCueResult, *, phase: str, telemetry_kind: str) -> None:
        self.envelope.cue_results.append(result)
        self.envelope.cue_counts = self._cue_status_counts()
        cue_result_payload = {
            "protocol_id": self.envelope.protocol_id,
            "cue_result": result.model_dump(),
            "fail_action_applied": cue.fail_action if result.status == "failed" else "warn",
        }
        self._append_event(
            PROTOCOL_CUE_RESULT,
            cue_result_payload,
        )
        telemetry_payload = telemetry_event(
            event_type=telemetry_kind,
            node_id=result.target_prompt_node_id or cue.target_prompt_node_id or "protocol",
            data={
                "protocol_id": self.envelope.protocol_id,
                "cue_id": cue.cue_id,
                "status": result.status,
                "note": result.note,
                "study_type": self.envelope.study_type,
                "capability_tags": list(cue.trigger_capabilities),
                "prompt_protocol_hash": self.envelope.prompt_protocol_hash,
                "execution_context_hash": result.execution_context_hash,
            },
            metadata={
                "strictness": self.envelope.strictness,
                "aft_cycle": self.envelope.aft_cycle,
                "transport": self.transport,
                "phase": phase,
            },
        )
        self._append_telemetry(telemetry_payload)
        target_node_id = result.target_prompt_node_id or cue.target_prompt_node_id or self.envelope.protocol_id
        heavy_metadata = self._heavy_event_metadata(phase=phase, cue_id=cue.cue_id, target_prompt_node_id=target_node_id)
        self._persist_heavy_protocol_events(
            [
                {
                    "event_type": "protocol_cue_result",
                    "node_id": target_node_id,
                    "data": cue_result_payload,
                    "metadata": heavy_metadata,
                },
                {
                    "event_type": "protocol_cue_telemetry",
                    "node_id": target_node_id,
                    "data": {
                        "event_type": telemetry_payload.get("event_type"),
                        "node_id": telemetry_payload.get("node_id"),
                        "data": telemetry_payload.get("data", {}),
                        "metadata": telemetry_payload.get("metadata", {}),
                    },
                    "metadata": heavy_metadata,
                },
            ]
        )

    def _record_initial_state(self) -> None:
        self._append_event(
            PROTOCOL_METADATA,
            {
                "protocol": self.envelope.model_dump(exclude={"cues", "cue_results"}),
                "scientific_protocol": {"id": self.scientific_protocol.id, "type": self.scientific_protocol.type},
                "benchmark_case_id": (self.benchmark_case or {}).get("id"),
            },
        )
        self.transition_phase("intake", reason="protocol initialized")
        for result in evaluate_intake_cues(self.query, self.envelope.cues):
            cue = next((item for item in self.envelope.cues if item.cue_id == result.cue_id), None)
            if cue is None:
                continue
            self._append_event(PROTOCOL_CUE, {"protocol_id": self.envelope.protocol_id, "cue": cue.model_dump(), "step": 0, "call_id": "intake"})
            self._record_result(cue, result, phase="intake", telemetry_kind=f"protocol_cue_{result.status}")
        self.transition_phase("planning", reason="intake complete")
        self.plan_progress(active_step_label="Initialize scientific protocol", steps_total=len(self.prompt_protocol.nodes), steps_completed=0)

    def transition_phase(self, phase: str, *, reason: str) -> Dict[str, Any]:
        previous = self.envelope.active_phase
        self.envelope.active_phase = phase
        self.scientific_protocol.metadata.setdefault("protocol_runtime", {})["active_phase"] = phase
        return self._append_event(
            PROTOCOL_PHASE,
            {
                "protocol_id": self.envelope.protocol_id,
                "previous_phase": previous,
                "active_phase": phase,
                "reason": reason,
                "timestamp": self.envelope.model_dump().get("cue_results", []) and self.envelope.cue_results[-1].timestamp or "",
            },
        )

    def plan_progress(
        self,
        *,
        active_step_label: str,
        steps_total: int,
        steps_completed: int,
        active_prompt_node_id: str = "",
        cue_fired: str = "",
        cue_result: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        return self._append_event(
            PLAN_PROGRESS,
            {
                "protocol_id": self.envelope.protocol_id,
                "plan": {
                    "steps_total": steps_total,
                    "steps_completed": steps_completed,
                    "active_step_label": active_step_label,
                    "active_prompt_node_id": active_prompt_node_id,
                },
                "cue_counts": dict(self.envelope.cue_counts),
                "cue_fired": cue_fired,
                "cue_result": cue_result,
                "message": message,
            },
        )

    def _next_tool_step(self) -> int:
        self._tool_gate_step += 1
        return self._tool_gate_step

    def _fail_action_decision(self, cue: ProtocolCue, result: ProtocolCueResult, *, step: int) -> Dict[str, Any]:
        blocked = result.status == "failed" and cue.fail_action in {"pause", "revise_plan", "request_review", "contradiction_search_required"}
        message = result.note or f"Protocol cue {cue.cue_id} returned {result.status}"
        self.plan_progress(
            active_step_label=f"Protocol cue {cue.label}",
            steps_total=max(1, len(self.prompt_protocol.nodes)),
            steps_completed=max(0, min(step, len(self.prompt_protocol.nodes))),
            active_prompt_node_id=result.target_prompt_node_id or cue.target_prompt_node_id or "",
            cue_fired=cue.cue_id,
            cue_result=result.status,
            message=message,
        )
        return {
            "blocked": blocked,
            "fail_action": cue.fail_action if result.status == "failed" else "warn",
            "cue_id": cue.cue_id,
            "message": message,
        }

    def _closure_claims(self, closure_context: Optional[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
        items = list((closure_context or {}).get(key) or [])
        return [item for item in items if isinstance(item, dict)]

    def _build_contradiction_query(self, closure_context: Optional[Dict[str, Any]]) -> str:
        claims = self._closure_claims(closure_context, "contradicted_claims") or self._closure_claims(
            closure_context,
            "unsupported_critical_claims",
        )
        fragments: List[str] = []
        for claim in claims[:2]:
            text = str(claim.get("claim_text") or claim.get("text") or "").strip()
            if text:
                fragments.append(" ".join(text.split()[:6]))
        if fragments:
            return "; ".join(fragments)[:120]
        fallback = " ".join(str(self.query or "").split()[:6]).strip()
        return fallback or "contradictory evidence"

    def _select_contradiction_tool(self, closure_context: Optional[Dict[str, Any]]) -> str:
        available = [str(item) for item in list((closure_context or {}).get("available_tool_names") or []) if str(item)]
        for candidate in ("search_literature", "consult_bibliotecario", "request_peer_review"):
            if candidate in available:
                return candidate
        return "search_literature"

    def _build_contradiction_tool_args(self, tool_name: str, closure_context: Optional[Dict[str, Any]], final_text: str) -> Dict[str, Any]:
        query = self._build_contradiction_query(closure_context)
        if tool_name == "consult_bibliotecario":
            return {
                "query": query,
                "task": "Analyze contradictions, rival hypotheses, and evidence gaps before allowing closure.",
                "max_papers": 12,
            }
        if tool_name == "request_peer_review":
            return {
                "content": str(final_text or "")[:4000],
                "focus": "Identify unresolved contradictions and unsupported critical claims before closure.",
            }
        return {"query": query, "max_papers": 8}

    def _evaluate_promotion_cue(
        self,
        cue: ProtocolCue,
        *,
        final_text: str,
        closure_context: Optional[Dict[str, Any]],
    ) -> ProtocolCueResult:
        contradicted_claims = self._closure_claims(closure_context, "contradicted_claims")
        unsupported_claims = self._closure_claims(closure_context, "unsupported_critical_claims")
        contradiction_search_performed = bool((closure_context or {}).get("contradiction_search_performed"))

        status = "passed"
        note = "promotion gate passed"
        artifacts: List[str] = []
        if cue.cue_id == "promotion_evidence_gate":
            unresolved_claim_ids = [
                str(item.get("claim_id") or "").strip()
                for item in contradicted_claims + unsupported_claims
                if str(item.get("claim_id") or "").strip()
            ]
            artifacts = unresolved_claim_ids[:12]
            unresolved_total = len(contradicted_claims) + len(unsupported_claims)
            if unresolved_total > 0:
                status = "failed"
                note = (
                    f"Promotion blocked: {len(contradicted_claims)} contradicted and "
                    f"{len(unsupported_claims)} unsupported critical claims remain"
                )
                if contradiction_search_performed:
                    note += " after forced contradiction search"
            elif not str(final_text or "").strip():
                status = "failed"
                note = "Promotion blocked: final synthesis text is empty"
        return ProtocolCueResult(
            cue_id=cue.cue_id,
            target_prompt_node_id=cue.target_prompt_node_id,
            status=status,
            note=note,
            artifacts=artifacts,
            execution_context_hash=stable_hash(
                {
                    "cue_id": cue.cue_id,
                    "phase": "promotion",
                    "final_text": str(final_text or "")[:500],
                    "closure_context": closure_context or {},
                }
            ),
        )

    def pre_tool_gate(self, *, tool_name: str, args: Dict[str, Any], call_id: str, step: Optional[int] = None) -> Dict[str, Any]:
        before = len(self.protocol_events)
        effective_step = int(step or self._next_tool_step())
        decision = {"blocked": False, "fail_action": "warn", "cue_id": "", "message": ""}
        node_id = self._tool_to_node.get(tool_name, "")
        self.transition_phase("pre_tool", reason=f"selected tool capability {tool_name}")
        self.plan_progress(
            active_step_label=f"Execute {tool_name}",
            steps_total=len(self.prompt_protocol.nodes),
            steps_completed=max(0, effective_step - 1),
            active_prompt_node_id=node_id,
        )
        for cue in self.envelope.cues:
            if cue.phase != "pre_tool" or not self._matches_tool(cue, tool_name):
                continue
            if node_id:
                cue.target_prompt_node_id = node_id
            self._append_event(PROTOCOL_CUE, {"protocol_id": self.envelope.protocol_id, "cue": cue.model_dump(), "step": effective_step, "call_id": call_id})
            ok = True
            if cue.cue_id == "structure_identity_check":
                ok = bool(args.get("pdb_id") or args.get("query"))
            elif cue.cue_id == "literature_objective_check":
                query = str(args.get("query") or "").strip()
                ok = len(query) >= 4 and query.lower() not in {"protein", "paper", "literature"}
            result = ProtocolCueResult(
                cue_id=cue.cue_id,
                target_prompt_node_id=node_id or cue.target_prompt_node_id,
                status="passed" if ok else "failed",
                note=f"pre-tool check for {tool_name}",
                execution_context_hash=stable_hash({"tool_name": tool_name, "args": args, "cue_id": cue.cue_id}),
            )
            self._record_result(cue, result, phase="pre_tool", telemetry_kind=f"protocol_cue_{result.status}")
            cue_decision = self._fail_action_decision(cue, result, step=effective_step)
            if cue_decision["blocked"] and not decision["blocked"]:
                decision = cue_decision
        return {"events": list(self.protocol_events[before:]), **decision}

    def post_tool_gate(self, *, tool_name: str, result_text: str, call_id: str, step: Optional[int] = None) -> Dict[str, Any]:
        before = len(self.protocol_events)
        effective_step = int(step or self._tool_gate_step or 1)
        decision = {"blocked": False, "fail_action": "warn", "cue_id": "", "message": ""}
        node_id = self._tool_to_node.get(tool_name, "")
        self.transition_phase("post_tool", reason=f"completed tool {tool_name}")
        for cue in self.envelope.cues:
            if cue.phase != "post_tool":
                continue
            if cue.trigger_capabilities and not self._matches_tool(cue, tool_name):
                continue
            if node_id:
                cue.target_prompt_node_id = node_id
            self._append_event(PROTOCOL_CUE, {"protocol_id": self.envelope.protocol_id, "cue": cue.model_dump(), "step": effective_step, "call_id": call_id})
            ok = bool(str(result_text or "").strip())
            note = f"post-tool check for {tool_name}"
            try:
                payload = json.loads(result_text)
                if cue.cue_id == "literature_primary_evidence_check":
                    papers_found = int(payload.get("papers_found") or payload.get("total_papers") or len(payload.get("results") or []))
                    ok = papers_found > 0
                    note = f"literature returned {papers_found} result(s)"
                elif cue.cue_id == "tool_output_capture_check":
                    ok = not payload.get("error")
                    note = "tool returned actionable payload" if ok else f"tool error: {payload.get('error')}"
            except Exception:
                pass
            result = ProtocolCueResult(
                cue_id=cue.cue_id,
                target_prompt_node_id=node_id or cue.target_prompt_node_id,
                status="passed" if ok else "failed",
                note=note,
                execution_context_hash=stable_hash({"tool_name": tool_name, "result": result_text[:500], "cue_id": cue.cue_id}),
            )
            self._record_result(cue, result, phase="post_tool", telemetry_kind=f"protocol_cue_{result.status}")
            cue_decision = self._fail_action_decision(cue, result, step=effective_step)
            if cue_decision["blocked"] and not decision["blocked"]:
                decision = cue_decision
        return {"events": list(self.protocol_events[before:]), **decision}

    def _matches_tool(self, cue: ProtocolCue, tool_name: str) -> bool:
        node_id = self._tool_to_node.get(tool_name, "")
        if cue.target_prompt_node_id and node_id and cue.target_prompt_node_id == node_id:
            if cue.trigger_study_types and self.envelope.study_type not in cue.trigger_study_types:
                return False
            return True
        if not cue.trigger_capabilities and not cue.trigger_study_types:
            return cue.phase in {"planning", "promotion", "post_tool"}
        try:
            capability = get_tool_capability(tool_name)
            tags = set(getattr(capability, "protocol_tags", ()) or ())
        except Exception:
            tags = set()
        if cue.trigger_study_types and self.envelope.study_type not in cue.trigger_study_types:
            return False
        if cue.trigger_capabilities and not (set(cue.trigger_capabilities) & tags):
            return False
        return True

    def on_tool_start(self, *, tool_name: str, args: Dict[str, Any], step: int, call_id: str) -> List[Dict[str, Any]]:
        return list(self.pre_tool_gate(tool_name=tool_name, args=args, call_id=call_id, step=step).get("events") or [])

    def on_tool_end(self, *, tool_name: str, result_text: str, step: int, call_id: str) -> List[Dict[str, Any]]:
        return list(self.post_tool_gate(tool_name=tool_name, result_text=result_text, call_id=call_id, step=step).get("events") or [])

    def finalize(
        self,
        *,
        final_text: str,
        total_steps: int,
        closure_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        before = len(self.protocol_events)
        self.transition_phase("promotion", reason="loop finished")
        for cue in self.envelope.cues:
            if cue.phase != "promotion":
                continue
            result = self._evaluate_promotion_cue(cue, final_text=final_text, closure_context=closure_context)
            self._record_result(cue, result, phase="promotion", telemetry_kind=f"protocol_cue_{result.status}")
            if result.status == "failed" and cue.fail_action == "contradiction_search_required" and not bool((closure_context or {}).get("contradiction_search_performed")):
                message = result.note or "Protocol cue requires contradiction search before promotion."
                self.plan_progress(
                    active_step_label="Forced contradiction search",
                    steps_total=max(1, len(self.prompt_protocol.nodes)),
                    steps_completed=total_steps,
                    active_prompt_node_id=result.target_prompt_node_id or cue.target_prompt_node_id or "",
                    cue_fired=cue.cue_id,
                    cue_result=result.status,
                    message=message,
                )
                self.scientific_protocol.metadata.setdefault("protocol_runtime", {})["allow_publication"] = False
                tool_name = self._select_contradiction_tool(closure_context)
                raise ScientificInterrupt(
                    interrupt_type="CONTRADICTION_SEARCH_REQUIRED",
                    cue_id=cue.cue_id,
                    message=message,
                    tool_name=tool_name,
                    tool_args=self._build_contradiction_tool_args(tool_name, closure_context, final_text),
                    events=list(self.protocol_events[before:]),
                )
        failed_critical = [r for r in self.envelope.cue_results if r.status == "failed" and any(c.cue_id == r.cue_id and c.priority == "critical" for c in self.envelope.cues)]
        failed_high = [r for r in self.envelope.cue_results if r.status == "failed" and any(c.cue_id == r.cue_id and c.priority == "high" for c in self.envelope.cues)]
        if failed_critical:
            verdict = "request_review"
        elif failed_high:
            verdict = "revise_plan"
        else:
            verdict = "ready"
        payload = {
            "protocol_id": self.envelope.protocol_id,
            "verdict": verdict,
            "strongest_contradiction": failed_critical[0].note if failed_critical else (failed_high[0].note if failed_high else "No critical protocol contradiction recorded"),
            "missing_control": "Protocol cues require explicit missing control surfacing before promotion" if verdict != "ready" else "None recorded",
            "next_required_evidence": "Protocol cue failure remediation" if verdict != "ready" else "Proceed to final synthesis",
            "escalate_to": "request_peer_review" if verdict == "request_review" else "",
        }
        self._append_event(PROMOTION_GATE_RESULT, payload)
        self._persist_heavy_protocol_events(
            [
                {
                    "event_type": "protocol_promotion_result",
                    "node_id": self.envelope.protocol_id,
                    "data": payload,
                    "metadata": self._heavy_event_metadata(phase="promotion"),
                }
            ]
        )
        self.scientific_protocol.conclusion.setdefault("next_steps", [])
        self.scientific_protocol.conclusion["summary"] = str(final_text or "")[:1000]
        self.scientific_protocol.conclusion["confidence"] = 0.75 if verdict == "ready" else 0.45
        self.envelope.protocol_confidence = float(self.scientific_protocol.conclusion["confidence"])
        self.scientific_protocol.metadata.setdefault("protocol_runtime", {})["allow_publication"] = verdict == "ready"
        self.plan_progress(active_step_label="Promotion gate", steps_total=max(1, len(self.prompt_protocol.nodes)), steps_completed=total_steps, active_prompt_node_id="")
        return list(self.protocol_events[before:])

    def runtime_payload(self) -> Dict[str, Any]:
        return {
            "protocol_runtime": self.envelope.model_dump(),
            "scientific_protocol": self.scientific_protocol.model_dump(),
            "prompt_protocol": self.prompt_protocol.model_dump(),
            "protocol_events": list(self.protocol_events),
        }

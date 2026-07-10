from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from mica.agentic.backend_command_manifest import (
    BackendCommandManifestEntry,
    canonical_backend_command_name,
    get_backend_command_manifest_entry,
)
from mica.mcp.resources_fabric import MCPResourceGateway
from mica.sdk.command_contracts import (
    BackendCommandBlocker,
    BackendCommandCostSnapshot,
    BackendCommandEnvelope,
    BackendCommandResult,
    BackendCommandTrace,
)

logger = logging.getLogger(__name__)


_SECTION_REF_RE = re.compile(r"^(?P<base>fixture://[^#]+?)(?:#section:(?P<section>.+))?$")
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)
_SNIPPET_WS_RE = re.compile(r"\s+")
_CEA_LOCAL_INDEX: dict[str, str] = {}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_fixture_root() -> Path:
    return _repo_root() / "tests" / "fixtures" / "unified_command_kernel"


def _slugify(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower())
    return normalized.strip("-") or "section"


def _bounded_summary(text: str, limit: int = 180) -> str:
    squashed = _SNIPPET_WS_RE.sub(" ", str(text or "").strip())
    if len(squashed) <= limit:
        return squashed
    return f"{squashed[: limit - 3]}..."


def _normalize_identity_key(value: str) -> str:
    return str(value or "").strip().lower()


class _KernelBlocked(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable


def _snippet_around(text: str, start: int, *, width: int = 160) -> str:
    lo = max(0, start - width // 2)
    hi = min(len(text), start + width // 2)
    return _bounded_summary(text[lo:hi], limit=width)


@dataclass(frozen=True)
class FixtureDocumentSection:
    title: str
    slug: str
    level: int
    text: str
    ref: str


@dataclass(frozen=True)
class FixtureDocument:
    fixture_name: str
    path: Path
    ref: str
    title: str
    text: str
    sections: tuple[FixtureDocumentSection, ...]


class LocalCommandFixtureStore:
    def __init__(self, fixture_root: Optional[Path] = None) -> None:
        self._fixture_root = Path(fixture_root or _default_fixture_root())

    def fixture_root(self) -> Path:
        return self._fixture_root

    def fixture_path(self, fixture_name: str) -> Path:
        path = self._fixture_root / str(fixture_name or "").strip()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"Fixture not found: {fixture_name}")
        return path

    def list_documents(self, fixture_name: str) -> list[FixtureDocument]:
        base = self.fixture_path(fixture_name)
        docs: list[FixtureDocument] = []
        for path in sorted(base.glob("*.md")):
            docs.append(self._load_document(fixture_name, path))
        for path in sorted(base.glob("*.txt")):
            docs.append(self._load_document(fixture_name, path))
        return docs

    def get_document(self, ref: str) -> FixtureDocument:
        fixture_name, doc_rel, _ = self._parse_ref(ref)
        path = self.fixture_path(fixture_name) / doc_rel
        if not path.exists():
            raise FileNotFoundError(f"Fixture document not found: {ref}")
        return self._load_document(fixture_name, path)

    def read_document(self, ref: str) -> dict[str, Any]:
        match = _SECTION_REF_RE.match(str(ref or "").strip())
        if not match:
            raise ValueError(f"Unsupported fixture ref: {ref}")
        base_ref = match.group("base")
        section_slug = match.group("section")
        doc = self.get_document(base_ref)
        if not section_slug:
            return {
                "ref": doc.ref,
                "title": doc.title,
                "text": doc.text,
                "path": str(doc.path),
                "sections": [{"title": s.title, "slug": s.slug, "ref": s.ref} for s in doc.sections],
            }
        for section in doc.sections:
            if section.slug == section_slug:
                return {
                    "ref": section.ref,
                    "title": section.title,
                    "text": section.text,
                    "path": str(doc.path),
                    "parent_ref": doc.ref,
                }
        raise KeyError(f"Section not found: {ref}")

    def _parse_ref(self, ref: str) -> tuple[str, str, Optional[str]]:
        normalized = str(ref or "").strip()
        if not normalized.startswith("fixture://"):
            raise ValueError(f"Unsupported fixture ref: {ref}")
        match = _SECTION_REF_RE.match(normalized)
        if not match:
            raise ValueError(f"Unsupported fixture ref: {ref}")
        base_ref = match.group("base")
        tail = base_ref[len("fixture://") :]
        fixture_name, _, doc_rel = tail.partition("/")
        if not fixture_name or not doc_rel:
            raise ValueError(f"Malformed fixture ref: {ref}")
        return fixture_name, doc_rel, match.group("section")

    def _load_document(self, fixture_name: str, path: Path) -> FixtureDocument:
        text = path.read_text(encoding="utf-8")
        title = self._extract_title(path, text)
        ref = f"fixture://{fixture_name}/{path.name}"
        sections = tuple(self._extract_sections(ref, text))
        return FixtureDocument(
            fixture_name=fixture_name,
            path=path,
            ref=ref,
            title=title,
            text=text,
            sections=sections,
        )

    def _extract_title(self, path: Path, text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return path.stem.replace("_", " ")

    def _extract_sections(self, base_ref: str, text: str) -> Iterable[FixtureDocumentSection]:
        matches = list(_HEADING_RE.finditer(text))
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title = match.group("title").strip()
            yield FixtureDocumentSection(
                title=title,
                slug=_slugify(title),
                level=len(match.group("level")),
                text=text[start:end].strip(),
                ref=f"{base_ref}#section:{_slugify(title)}",
            )


class UnifiedAgentCommandKernel:
    def __init__(
        self,
        *,
        user_id: Optional[str] = None,
        mcp_sessions: Optional[Dict[str, Any]] = None,
        mcp_config: Optional[Dict[str, Any]] = None,
        fixture_root: Optional[Path] = None,
    ) -> None:
        self._user_id = str(user_id or "").strip() or None
        self._fixture_store = LocalCommandFixtureStore(fixture_root=fixture_root)
        self._resource_gateway = MCPResourceGateway(
            mcp_sessions=dict(mcp_sessions or {}),
            mcp_config=dict(mcp_config or {}),
            user_id=self._user_id,
        )

    async def execute(self, envelope: BackendCommandEnvelope) -> BackendCommandResult:
        started = time.perf_counter()
        canonical_name = canonical_backend_command_name(envelope.command_name)
        try:
            entry = get_backend_command_manifest_entry(canonical_name)
        except KeyError:
            return self._blocked(
                command_name=canonical_name,
                binding_surface="manifest",
                backend_authority="src/mica/agentic/backend_command_manifest.py",
                blocker=BackendCommandBlocker(
                    code="unknown_command",
                    message=f"Backend command is not registered: {canonical_name}",
                ),
                started=started,
            )

        if entry.side_effects and not envelope.policy.allow_side_effects:
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="side_effects_not_allowed",
                    message=f"Command '{entry.command_name}' requires allow_side_effects=true.",
                ),
                started=started,
            )

        missing_scope = self._missing_scope(entry=entry, envelope=envelope)
        if missing_scope:
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="missing_scope",
                    message=f"Command '{entry.command_name}' requires scope: {', '.join(missing_scope)}",
                    details={"missing_scope": list(missing_scope)},
                ),
                started=started,
            )

        if entry.implemented_status != "implemented":
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="registered_but_blocked",
                    message=f"Command '{entry.command_name}' is registered but not yet implemented on this surface.",
                    details={"implemented_status": entry.implemented_status},
                ),
                started=started,
            )

        # ── CK1 — unlock-candidate & canonical-mutation guard ──
        if entry.unlock_candidate and entry.canonical_mutation:
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="canonical_mutation_not_allowed",
                    message=f"Unlock candidate command '{entry.command_name}' cannot perform canonical mutations.",
                ),
                started=started,
            )

        # ── CK2 — execute-capable gated check ──
        if getattr(entry, "execute_capable", False) and getattr(entry, "requires_gate", False):
            args = envelope.arguments or {}
            has_gate = args.get("quetzal_gate_ref") or args.get("quetzal-gate-ref")
            has_budget = args.get("budget_ref") or args.get("budget-ref")
            if not has_gate or not has_budget:
                return self._blocked(
                    command_name=entry.command_name,
                    binding_surface=entry.binding_surface,
                    backend_authority=entry.backend_authority,
                    blocker=BackendCommandBlocker(
                        code="command_gate_required",
                        message=f"Command '{entry.command_name}' is execute-capable gated and requires both quetzal_gate_ref and budget_ref references.",
                    ),
                    started=started,
                )

        # ── CK3 — Enforceable Dependency Guard (Ronda 3) ──
        if entry.family in ("hn", "provider"):
            from mica.agentic.closure_store import get_closure_store

            store = get_closure_store()
            store.load()

            hn_v1_ref = store.closure_ref_for("hn", "v1")
            hn_round1_ref = store.closure_ref_for("hn", 1)
            arc_v1_ref = store.closure_ref_for("arc", "v1")
            arc_round1_ref = store.closure_ref_for("arc", 1)

            hn_guard = store.is_closed_ref(hn_v1_ref) if store.get_closure(hn_v1_ref) is not None else store.is_closed_ref(hn_round1_ref)
            arc_guard = store.is_closed_ref(arc_v1_ref) if store.get_closure(arc_v1_ref) is not None else store.is_closed_ref(arc_round1_ref)
            if entry.family == "hn":
                if not hn_guard:
                    return self._blocked(
                        command_name=entry.command_name,
                        binding_surface=entry.binding_surface,
                        backend_authority=entry.backend_authority,
                        blocker=BackendCommandBlocker(
                            code="command_dependency_unmet",
                            message=f"Command '{entry.command_name}' is disabled because Harness Native closure receipt for v1 is missing or retracted.",
                            details={"required_closure_ref": hn_v1_ref, "fallback_closure_ref": hn_round1_ref},
                        ),
                        started=started,
                    )
            elif not hn_guard or not arc_guard:
                return self._blocked(
                    command_name=entry.command_name,
                    binding_surface=entry.binding_surface,
                    backend_authority=entry.backend_authority,
                    blocker=BackendCommandBlocker(
                        code="command_dependency_unmet",
                        message=f"Command '{entry.command_name}' is disabled because Harness Native v1 or ARC provider closure is missing or retracted.",
                        details={
                            "required_closure_refs": [hn_v1_ref, arc_v1_ref],
                            "fallback_hn_closure_ref": hn_round1_ref,
                            "fallback_arc_closure_ref": arc_round1_ref,
                        },
                    ),
                    started=started,
                )

        # Validate any closure URN passed in arguments
        args = envelope.arguments or {}
        phase_labels = {"p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "post_p6"}
        for val in args.values():
            if isinstance(val, str) and val.startswith("closure://"):
                if any(part in phase_labels for part in val.split("/")):
                    return self._blocked(
                        command_name=entry.command_name,
                        binding_surface=entry.binding_surface,
                        backend_authority=entry.backend_authority,
                        blocker=BackendCommandBlocker(
                            code="phase_label_in_closure_ref",
                            message=f"Closure reference URN '{val}' cannot contain phase labels.",
                        ),
                        started=started,
                    )

        # ── CK1 — naming-doctrine enforcement ──
        if entry.unlock_candidate or entry.command_name.startswith("protocol.reviews.") or entry.command_name.startswith("hn.") or entry.command_name.startswith("provider."):
            name_parts = entry.command_name.split(".")
            phase_labels = {"p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "post_p6"}
            if any(part in phase_labels for part in name_parts):
                return self._blocked(
                    command_name=entry.command_name,
                    binding_surface=entry.binding_surface,
                    backend_authority=entry.backend_authority,
                    blocker=BackendCommandBlocker(
                        code="phase_label_in_canonical_name",
                        message=f"Canonical command name '{entry.command_name}' cannot contain phase labels.",
                    ),
                    started=started,
                )

        try:
            payload = await self._dispatch(entry=entry, envelope=envelope)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return BackendCommandResult(
                success=True,
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                summary=_bounded_summary(str(payload.get("summary") or entry.description)),
                result=dict(payload.get("result") or {}),
                state_after=dict(payload.get("state_after") or {}),
                artifact_refs=list(payload.get("artifact_refs") or []),
                resource_refs=list(payload.get("resource_refs") or []),
                evidence_refs=list(payload.get("evidence_refs") or []),
                receipt_refs=list(payload.get("receipt_refs") or []),
                cost_snapshot=BackendCommandCostSnapshot(
                    usd=float(payload.get("usd") or 0.0),
                    tool_calls=int(payload.get("tool_calls") or 1),
                ),
                trace=BackendCommandTrace(
                    route_authority="shared",
                    backend_authority=entry.backend_authority,
                    duration_ms=duration_ms,
                ),
                status=payload.get("status") or "completed",
                blocker_code=payload.get("blocker_code"),
                warnings=list(payload.get("warnings") or []),
                degraded_reason=payload.get("degraded_reason"),
                runtime_backing=payload.get("runtime_backing"),
                durability=payload.get("durability"),
                trust_state=payload.get("trust_state"),
            )
        except _KernelBlocked as exc:
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                    details=exc.details,
                ),
                started=started,
            )
        except ValueError as exc:
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="invalid_arguments",
                    message=str(exc),
                    retryable=False,
                ),
                started=started,
            )
        except RuntimeError as exc:
            # RuntimeError is the canonical "domain validation failed" path used by
            # cg_martini_commands, insane, gauntlet, etc. Surface the full message
            # (which carries actionable context like "input_structure_ref not found")
            # instead of swallowing it under "unexpected_kernel_error".
            logger.warning("Kernel RuntimeError for %s: %s", entry.command_name, str(exc)[:200])
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="runtime_error",
                    message=f"{type(exc).__name__}: {str(exc)}",
                    retryable=False,
                ),
                started=started,
            )
        except Exception as exc:
            logger.exception("Unexpected kernel error for %s", entry.command_name)
            return self._blocked(
                command_name=entry.command_name,
                binding_surface=entry.binding_surface,
                backend_authority=entry.backend_authority,
                blocker=BackendCommandBlocker(
                    code="unexpected_kernel_error",
                    message=f"{type(exc).__name__}: Kernel command execution failed unexpectedly.",
                    retryable=False,
                ),
                started=started,
            )

    def _missing_scope(
        self,
        *,
        entry: BackendCommandManifestEntry,
        envelope: BackendCommandEnvelope,
    ) -> tuple[str, ...]:
        missing: list[str] = []
        for field_name in entry.required_scope:
            value = getattr(envelope, field_name, None)
            if value is None and field_name == "user_id":
                value = self._user_id
            if value in (None, ""):
                missing.append(field_name)
        return tuple(missing)

    async def _dispatch(
        self,
        *,
        entry: BackendCommandManifestEntry,
        envelope: BackendCommandEnvelope,
    ) -> Dict[str, Any]:
        args = dict(envelope.arguments or {})
        # ── CG/Martini lane nodes ─────────────────────────────────
        # These are the kernel-executable surfaces of the CG/Martini
        # pipeline (real martinize2 + real insane, NOT mocks). Any
        # protocol (P5/P6) can submit them via the canonical manifest
        # with command_name "cg.*" and family "cg_martini".
        if entry.command_name == "cg.martinize2.map":
            from mica.agentic.commands.cg_martini_commands import cg_martinize2_map
            return await cg_martinize2_map(self, args, envelope)
        if entry.command_name == "cg.insane.build":
            from mica.agentic.commands.cg_martini_commands import cg_insane_build
            return await cg_insane_build(self, args, envelope)
        if entry.command_name == "cg.gauntlet.clcn7":
            from mica.agentic.commands.cg_martini_commands import cg_gauntlet_clcn7
            return await cg_gauntlet_clcn7(self, args, envelope)
        if entry.command_name == "cg.preprocess.topology":
            from mica.agentic.commands.cg_martini_commands import cg_preprocess_topology
            return await cg_preprocess_topology(self, args, envelope)
        if entry.command_name == "cg.audit.geometry":
            from mica.agentic.commands.cg_martini_commands import cg_audit_geometry
            return await cg_audit_geometry(self, args, envelope)
        if entry.command_name == "cg.remediate.overlap":
            from mica.agentic.commands.cg_martini_commands import cg_remediate_overlap
            return await cg_remediate_overlap(self, args, envelope)
        if entry.command_name == "cg.railway.readiness":
            from mica.agentic.commands.cg_martini_commands import cg_railway_readiness
            return await cg_railway_readiness(self, args, envelope)
        if entry.command_name == "resource.ls":
            return await self._resource_ls(args)
        if entry.command_name == "resource.inspect":
            return await self._resource_inspect(args, envelope.resource_refs)
        if entry.command_name == "resource.resolve":
            return await self._resource_resolve(args, envelope.resource_refs)
        if entry.command_name == "resource.children":
            return await self._resource_children(args, envelope.resource_refs)
        if entry.command_name == "resource.search":
            return await self._resource_search(args)
        if entry.command_name == "document.read":
            return await self._document_read(args, envelope.resource_refs)
        if entry.command_name == "document.read_section":
            return await self._document_read_section(args, envelope.resource_refs)
        if entry.command_name == "corpus.grep":
            return await self._corpus_grep(args)
        if entry.command_name == "cea.resolve":
            return await self._cea_resolve(args, envelope)
        if entry.command_name == "cea.learn":
            return await self._cea_learn(args, envelope)
        if entry.command_name == "cea.mint":
            return await self._cea_mint(args, envelope)
        if entry.command_name == "quetzal.evaluate":
            from mica.agentic.commands.quetzal_commands import quetzal_evaluate
            return await quetzal_evaluate(self, args, envelope)
        if entry.command_name == "quetzal.validation_claim_gate":
            from mica.agentic.commands.quetzal_commands import quetzal_validation_claim_gate
            return await quetzal_validation_claim_gate(self, args, envelope)
        if entry.command_name == "quetzal.proactive_proposal_gate":
            from mica.agentic.commands.quetzal_commands import quetzal_proactive_proposal_gate
            return await quetzal_proactive_proposal_gate(self, args, envelope)
        if entry.command_name == "models.invoke":
            from mica.agentic.commands.models_commands import models_invoke
            return await models_invoke(self, args, envelope)
        if entry.command_name == "protocol.validate":
            from mica.agentic.commands.protocol_commands import protocol_validate
            return await protocol_validate(self, args, envelope)
        if entry.command_name == "protocol.run.status":
            from mica.agentic.commands.protocol_commands import protocol_run_status
            return await protocol_run_status(self, args, envelope)
        if entry.command_name == "protocol.p5.status":
            from mica.agentic.commands.protocol_commands import protocol_p5_status
            return await protocol_p5_status(self, args, envelope)
        if entry.command_name == "protocol.p6.status":
            from mica.agentic.commands.protocol_commands import protocol_p6_status
            return await protocol_p6_status(self, args, envelope)
        if entry.command_name == "protocol.p6.requests.project":
            from mica.agentic.commands.protocol_commands import protocol_p6_requests_project
            return await protocol_p6_requests_project(self, args, envelope)
        if entry.command_name == "protocol.p6.debate.artifacts":
            from mica.agentic.commands.protocol_commands import protocol_p6_debate_artifacts
            return await protocol_p6_debate_artifacts(self, args, envelope)
        if entry.command_name == "protocol.reviews.schedule":
            from mica.agentic.commands.protocol_commands import protocol_p6_reviews_schedule
            return await protocol_p6_reviews_schedule(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.claim":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_claim
            return await protocol_reviews_worker_claim(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.lineage.project":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_lineage_project
            return await protocol_reviews_worker_lineage_project(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.retry.transition.record":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_retry_transition_record
            return await protocol_reviews_worker_retry_transition_record(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.handoff.submit":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_submit
            return await protocol_reviews_worker_handoff_submit(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.handoff.binding":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_binding
            return await protocol_reviews_worker_handoff_binding(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.handoff.retry.status":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_retry_status
            return await protocol_reviews_worker_handoff_retry_status(self, args, envelope)
        if entry.command_name == "protocol.reviews.worker.handoff.deadletter":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_deadletter
            return await protocol_reviews_worker_handoff_deadletter(self, args, envelope)
        if entry.command_name == "protocol.msrp.self_improvement.design":
            from mica.agentic.commands.protocol_commands import protocol_p6_msrp_self_improvement_design
            return await protocol_p6_msrp_self_improvement_design(self, args, envelope)
        if entry.command_name == "protocol.p5.slices":
            from mica.agentic.commands.protocol_commands import protocol_p5_slices
            return await protocol_p5_slices(self, args, envelope)
        if entry.command_name == "protocol.p5.residuals":
            from mica.agentic.commands.protocol_commands import protocol_p5_residuals
            return await protocol_p5_residuals(self, args, envelope)
        if entry.command_name == "protocol.p5.refs":
            from mica.agentic.commands.protocol_commands import protocol_p5_refs
            return await protocol_p5_refs(self, args, envelope)
        if entry.command_name == "protocol.p5.cg.proposals":
            from mica.agentic.commands.protocol_commands import protocol_p5_cg_proposals
            return await protocol_p5_cg_proposals(self, args, envelope)
        if entry.command_name == "protocol.p5.ese_cg.proposals":
            from mica.agentic.commands.protocol_commands import protocol_p5_ese_cg_proposals
            return await protocol_p5_ese_cg_proposals(self, args, envelope)
        if entry.command_name == "protocol.p5.citations.consolidate":
            from mica.agentic.commands.protocol_commands import protocol_p5_citations_consolidate
            return await protocol_p5_citations_consolidate(self, args, envelope)
        if entry.command_name == "protocol.p5.scale.readiness":
            from mica.agentic.commands.protocol_commands import protocol_p5_scale_readiness
            return await protocol_p5_scale_readiness(self, args, envelope)
        if entry.command_name == "protocol.node.receipts":
            from mica.agentic.commands.protocol_commands import protocol_node_receipts
            return await protocol_node_receipts(self, args, envelope)
        # ── CK3 — protocol.* read adapters ──
        if entry.command_name == "protocol.list":
            from mica.agentic.commands.protocol_commands import protocol_list
            return await protocol_list(self, args, envelope)
        if entry.command_name == "protocol.inspect":
            from mica.agentic.commands.protocol_commands import protocol_inspect
            return await protocol_inspect(self, args, envelope)
        # ── CK4 — compute.* read adapters ──
        if entry.command_name == "compute.status":
            from mica.agentic.commands.compute_commands import compute_status
            return await compute_status(self, args, envelope)
        if entry.command_name == "compute.artifacts":
            from mica.agentic.commands.compute_commands import compute_artifacts
            return await compute_artifacts(self, args, envelope)
        # ── CK5 — artifact.inspect ──
        if entry.command_name == "artifact.inspect":
            from mica.agentic.commands.artifact_commands import artifact_inspect
            return await artifact_inspect(self, args, envelope)
        if entry.command_name == "artifact.attach_to_study":
            from mica.agentic.commands.kb_commands import artifact_attach_to_study
            return await artifact_attach_to_study(self, args, envelope)
        if entry.command_name == "artifact.attach_to_working_set":
            from mica.agentic.commands.kb_commands import artifact_attach_to_working_set
            return await artifact_attach_to_working_set(self, args, envelope)
        if entry.command_name == "kb.list":
            from mica.agentic.commands.kb_commands import kb_list
            return await kb_list(self, args, envelope)
        if entry.command_name == "kb.create":
            from mica.agentic.commands.kb_commands import kb_create
            return await kb_create(self, args, envelope)
        if entry.command_name == "kb.ingest":
            from mica.agentic.commands.kb_commands import kb_ingest
            return await kb_ingest(self, args, envelope)
        if entry.command_name == "kb.semantic_search":
            from mica.agentic.commands.kb_commands import kb_semantic_search
            return await kb_semantic_search(self, args, envelope)
        if entry.command_name == "graphrag.query":
            from mica.agentic.commands.kb_commands import graphrag_query
            return await graphrag_query(self, args, envelope)
        if entry.command_name == "graphrag.hop1":
            from mica.agentic.commands.kb_commands import graphrag_hop1
            return await graphrag_hop1(self, args, envelope)
        if entry.command_name == "graphrag.export_decision_subgraph":
            from mica.agentic.commands.kb_commands import graphrag_export_decision_subgraph
            return await graphrag_export_decision_subgraph(self, args, envelope)
        # ── CK6 — mudo.* read adapters ──
        if entry.command_name == "mudo.codex":
            from mica.agentic.commands.mudo_commands import mudo_codex
            return await mudo_codex(self, args, envelope)
        if entry.command_name == "mudo.stale_summary":
            from mica.agentic.commands.mudo_commands import mudo_stale_summary
            return await mudo_stale_summary(self, args, envelope)
        # ── CK7 — agent.feed.scroll ──
        if entry.command_name == "agent.feed.scroll":
            from mica.agentic.commands.agent_feed_commands import agent_feed_scroll
            return await agent_feed_scroll(self, args, envelope)
        # ── CK2 — hn.* and provider.* dispatch ──
        if entry.command_name == "hn.run.submit":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_submit
            return await protocol_reviews_worker_handoff_submit(self, args, envelope)
        if entry.command_name == "hn.run.binding":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_binding
            return await protocol_reviews_worker_handoff_binding(self, args, envelope)
        if entry.command_name == "hn.run.status":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_status
            return await protocol_reviews_worker_handoff_status(self, args, envelope)
        if entry.command_name == "hn.run.deadletter":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_deadletter
            return await protocol_reviews_worker_handoff_deadletter(self, args, envelope)
        if entry.command_name == "hn.run.retry.status":
            from mica.agentic.commands.protocol_commands import protocol_reviews_worker_handoff_retry_status
            return await protocol_reviews_worker_handoff_retry_status(self, args, envelope)
        if entry.command_name == "provider.matrix":
            from mica.agentic.commands.provider_commands import provider_matrix
            return await provider_matrix(self, args, envelope)
        if entry.command_name == "provider.select":
            from mica.agentic.commands.provider_commands import provider_select
            return await provider_select(self, args, envelope)
        if entry.command_name == "provider.run_job":
            from mica.agentic.commands.provider_commands import provider_run_job
            return await provider_run_job(self, args, envelope)
        if entry.command_name == "provider.job.status":
            from mica.agentic.commands.provider_commands import provider_job_status
            return await provider_job_status(self, args, envelope)
        if entry.command_name == "provider.endpoints":
            from mica.agentic.commands.provider_commands import provider_endpoints
            return await provider_endpoints(self, args, envelope)
        if entry.command_name == "provider.zombies":
            from mica.agentic.commands.provider_commands import provider_zombies
            return await provider_zombies(self, args, envelope)
        if entry.command_name == "provider.kill":
            from mica.agentic.commands.provider_commands import provider_kill
            return await provider_kill(self, args, envelope)
        if entry.command_name == "phy.execute":
            from mica.agentic.commands.phy_commands import phy_execute
            return await phy_execute(self, args, envelope)
        raise RuntimeError(f"No dispatcher branch exists for {entry.command_name}")

    async def _cea_resolve(self, args: Dict[str, Any], envelope: BackendCommandEnvelope) -> Dict[str, Any]:
        identifier = str(args.get("identifier") or args.get("query") or args.get("q") or "").strip()
        if not identifier:
            raise RuntimeError("cea.resolve requires a non-empty identifier.")

        cache_key = _normalize_identity_key(identifier)
        local_budo_id = _CEA_LOCAL_INDEX.get(cache_key)
        if local_budo_id:
            return self._cea_resolve_payload(
                identifier=identifier,
                identity_status="found",
                resolved_to=local_budo_id,
                matched_via="local_hash",
                confidence=0.6,
                workspace_id=envelope.workspace_id,
                identity_authority="local_hash_fallback",
                authority_status="fallback",
                fallback=True,
                source_ref=f"cea-local://{cache_key}",
            )

        try:
            from bsm.cea.cea_service import CEAService
            from bsm.cea.exceptions import CEANotFoundError
            from bsm.neo4j_integration import BSMNeo4jIntegration

            neo4j = BSMNeo4jIntegration()
            try:
                await neo4j.connect()
                service = CEAService(neo4j)
                entity = await service.resolve(identifier)
            finally:
                await neo4j.disconnect()
        except CEANotFoundError:
            return self._cea_resolve_payload(
                identifier=identifier,
                identity_status="unknown",
                resolved_to=None,
                matched_via=None,
                confidence=0.0,
                workspace_id=envelope.workspace_id,
                identity_authority="cea_service",
                authority_status="unresolved",
                fallback=False,
            )
        except Exception as exc:
            return self._cea_resolve_payload(
                identifier=identifier,
                identity_status="degraded",
                resolved_to=None,
                matched_via="cea_service_unavailable",
                confidence=0.0,
                workspace_id=envelope.workspace_id,
                identity_authority="cea_service",
                authority_status="degraded",
                fallback=False,
                error=str(exc),
            )

        entity_payload = entity.model_dump(mode="json")
        budo_id = str(entity_payload.get("budo_id") or "").strip()
        if budo_id:
            _CEA_LOCAL_INDEX[cache_key] = budo_id
            _CEA_LOCAL_INDEX[_normalize_identity_key(budo_id)] = budo_id

        return self._cea_resolve_payload(
            identifier=identifier,
            identity_status="found",
            resolved_to=budo_id or None,
            matched_via="cea_service",
            confidence=1.0,
            workspace_id=envelope.workspace_id,
            entity=entity_payload,
            identity_authority="cea_service",
            authority_status="canonical",
            fallback=False,
            source_ref=self._cea_identity_artifact_ref(budo_id) if budo_id else None,
        )

    async def _cea_learn(self, args: Dict[str, Any], envelope: BackendCommandEnvelope) -> Dict[str, Any]:
        identifier = str(args.get("identifier") or "").strip()
        budo_id = str(args.get("budo_id") or "").strip()
        if not identifier or not budo_id:
            raise RuntimeError("cea.learn requires identifier and budo_id.")

        _CEA_LOCAL_INDEX[_normalize_identity_key(identifier)] = budo_id
        _CEA_LOCAL_INDEX[_normalize_identity_key(budo_id)] = budo_id
        receipt = {
            "receipt_type": "CeaLearnReceipt",
            "identifier": identifier,
            "budo_id": budo_id,
            "workspace_id": envelope.workspace_id,
            "persistence": "process_local",
            "identity_authority": "local_hash_fallback",
            "authority_status": "fallback",
            "fallback": True,
            "canonical_authority": False,
            "claim_promotion_allowed": False,
            "claim_promotion_requires": "quetzal.validation_claim_gate",
        }
        return {
            "summary": f"Learned local CEA mapping for {identifier}.",
            "result": {
                "identifier": identifier,
                "budo_id": budo_id,
                "identity_status": "found",
                "matched_via": "local_hash",
                "identity_authority": "local_hash_fallback",
                "authority_status": "fallback",
                "fallback": True,
                "canonical_authority": False,
                "identity_can_support_claim_subject": False,
                "claim_promotion_allowed": False,
                "claim_promotion_requires": "quetzal.validation_claim_gate",
                "receipt": receipt,
            },
            "state_after": {
                "cea_local_index_size": len(_CEA_LOCAL_INDEX),
                "identity_authority": "local_hash_fallback",
                "authority_status": "fallback",
                "fallback": True,
            },
            "evidence_refs": [f"cea-local://{_normalize_identity_key(identifier)}"],
        }

    async def _cea_mint(self, args: Dict[str, Any], envelope: BackendCommandEnvelope) -> Dict[str, Any]:
        identifier = str(args.get("identifier") or args.get("query") or args.get("q") or "").strip()
        name = str(args.get("name") or "").strip()
        entity_type = str(args.get("entity_type") or "").strip()
        organism = str(args.get("organism") or "").strip() or None
        requested_budo_id = str(args.get("requested_budo_id") or "").strip()
        curator = str(args.get("curator") or self._user_id or "command_kernel").strip()
        evidence_refs = self._coerce_string_list(args.get("evidence_refs") or args.get("evidence_ref"))
        cross_references = self._coerce_mapping(args.get("cross_references"))
        tags = self._coerce_string_list(args.get("tags"))
        metadata = self._coerce_mapping(args.get("metadata"))

        if not identifier:
            raise _KernelBlocked(
                code="schema_validation_failed",
                message="cea.mint requires a non-empty identifier.",
            )
        if not evidence_refs:
            raise _KernelBlocked(
                code="missing_evidence_refs",
                message="cea.mint requires one or more durable evidence_refs.",
            )
        if entity_type not in {"Protein", "Complex", "SmallMolecule"}:
            raise _KernelBlocked(
                code="invalid_entity_type",
                message="cea.mint requires entity_type to be Protein, Complex, or SmallMolecule.",
                details={"entity_type": entity_type},
            )
        if not name:
            raise _KernelBlocked(
                code="schema_validation_failed",
                message="cea.mint requires a non-empty canonical name.",
            )

        try:
            from bsm.cea.id_generator import BudoIdGenerator
            from bsm.cea.exceptions import BudoIdError, CEADuplicateError, CEAError
            from bsm.schemas.cea import AuditTrail, CEAEntity, ExternalReferences
        except Exception as exc:
            raise _KernelBlocked(
                code="schema_validation_failed",
                message="CEA mint dependencies could not be imported.",
                details={"error": str(exc)},
            ) from exc

        generator = BudoIdGenerator()
        try:
            if requested_budo_id:
                budo_root = generator.parse_root(requested_budo_id)
            else:
                budo_root = generator.create_root_id(name, organism=organism, version=1)
        except BudoIdError as exc:
            raise _KernelBlocked(
                code="invalid_budo_id",
                message=str(exc),
                details={"requested_budo_id": requested_budo_id},
            ) from exc
        budo_id = budo_root.value

        if not cross_references and entity_type == "Protein":
            cross_references = {"uniprot": identifier}

        try:
            entity = CEAEntity(
                budo_id=budo_id,
                entity_type=entity_type,
                name=name,
                organism=organism,
                description=str(args.get("description") or "").strip() or None,
                cross_references=ExternalReferences(**cross_references),
                tags=tags,
                metadata={
                    **metadata,
                    "minted_by_command": "cea.mint",
                    "source_identifier": identifier,
                    "evidence_refs": evidence_refs,
                },
                audit=AuditTrail(curator=curator, pipeline="command_kernel.cea.mint"),
            )
        except Exception as exc:
            raise _KernelBlocked(
                code="schema_validation_failed",
                message=f"cea.mint payload did not validate as CEAEntity: {exc}",
                details={"identifier": identifier, "budo_id": budo_id},
            ) from exc

        try:
            from bsm.cea.cea_service import CEAService
            from bsm.neo4j_integration import BSMNeo4jIntegration

            neo4j = BSMNeo4jIntegration()
            await neo4j.connect()
        except Exception as exc:
            raise _KernelBlocked(
                code="neo4j_unavailable",
                message="CEA Neo4j backend is unavailable for cea.mint.",
                details={"error": str(exc)},
                retryable=True,
            ) from exc

        service = CEAService(neo4j)
        collision_candidates = self._cea_mint_collision_candidates(
            identifier=identifier,
            name=name,
            cross_references=cross_references,
        )
        try:
            if await service.exists(budo_id):
                raise _KernelBlocked(
                    code="identity_already_exists",
                    message=f"CEA identity already exists for BUDO root {budo_id}.",
                    details={"budo_id": budo_id, "matched_on": "budo_id"},
                )
            for candidate in collision_candidates:
                existing = await service.get_by_external_id(candidate)
                if existing is not None:
                    existing_payload = existing.model_dump(mode="json")
                    raise _KernelBlocked(
                        code="identity_already_exists",
                        message=f"CEA identity already exists for identifier {candidate}.",
                        details={
                            "matched_on": candidate,
                            "existing_budo_id": existing_payload.get("budo_id"),
                        },
                    )
            persisted = await service.create_entity(entity)
        except _KernelBlocked:
            raise
        except CEADuplicateError as exc:
            raise _KernelBlocked(
                code="identity_already_exists",
                message=str(exc),
                details={"budo_id": budo_id},
            ) from exc
        except CEAError as exc:
            raise _KernelBlocked(
                code="graph_write_failed",
                message=str(exc),
                details={"budo_id": budo_id},
            ) from exc
        except Exception as exc:
            raise _KernelBlocked(
                code="graph_write_failed",
                message=f"CEA graph write failed: {exc}",
                details={"budo_id": budo_id},
            ) from exc
        finally:
            await neo4j.disconnect()

        persisted_payload = persisted.model_dump(mode="json")
        persisted_budo_id = str(persisted_payload.get("budo_id") or budo_id)
        identity_artifact_ref = self._cea_identity_artifact_ref(persisted_budo_id)
        artifact_refs = [identity_artifact_ref] if identity_artifact_ref else []
        _CEA_LOCAL_INDEX[_normalize_identity_key(identifier)] = persisted_budo_id
        _CEA_LOCAL_INDEX[_normalize_identity_key(persisted_budo_id)] = persisted_budo_id

        receipt = {
            "receipt_type": "CeaMintReceipt",
            "command_name": "cea.mint",
            "workspace_id": envelope.workspace_id,
            "identifier": identifier,
            "entity_type": entity_type,
            "budo_id": persisted_budo_id,
            "identity_status": "minted",
            "matched_via": "cea_service",
            "evidence_refs": evidence_refs,
            "graph_backend": "neo4j",
            "collision_checks": {
                "budo_id": budo_id,
                "identifier_candidates": collision_candidates,
                "version_bump_policy": "block_existing_root",
            },
            "created_node_labels": ["CEA"],
            "created_relationship_types": ["HAS_XREF"] if cross_references else [],
            "curator": curator,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        mudo_publication_status, mudo_publication_blocker = await self._publish_cea_mint_mudo_receipt_ready(
            envelope=envelope,
            receipt=receipt,
            evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
        )
        receipt["artifact_refs"] = artifact_refs
        receipt["mudo_publication_status"] = mudo_publication_status
        if mudo_publication_blocker:
            receipt["mudo_publication_blocker"] = mudo_publication_blocker
        return {
            "summary": f"Minted CEA identity {persisted_budo_id} for {identifier}.",
            "result": {
                "identifier": identifier,
                "identity_status": "minted",
                "resolved_to": persisted_budo_id,
                "artifact_refs": artifact_refs,
                "entity": persisted_payload,
                "receipt": receipt,
                "evidence_refs": evidence_refs,
                "mudo_publication_status": mudo_publication_status,
                "mudo_publication_blocker": mudo_publication_blocker,
            },
            "state_after": {"identity_status": "minted", "resolved_to": persisted_budo_id},
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
        }

    @staticmethod
    def _cea_identity_artifact_ref(budo_id: str) -> str:
        normalized = str(budo_id or "").strip()
        if not normalized:
            return ""
        return f"cea://{normalized}"

    def _resolve_owner_user_id(self, args: Dict[str, Any], envelope: BackendCommandEnvelope) -> str:
        request_identity = self._coerce_mapping(envelope.request_identity)
        for candidate in (
            request_identity.get("owner_user_id"),
            request_identity.get("user_id"),
            args.get("owner_user_id"),
            args.get("user_id"),
            self._user_id,
        ):
            normalized = str(candidate or "").strip()
            if normalized:
                return normalized
        return ""

    async def _publish_cea_mint_mudo_receipt_ready(
        self,
        *,
        envelope: BackendCommandEnvelope,
        receipt: Dict[str, Any],
        evidence_refs: list[str],
        artifact_refs: list[str],
    ) -> tuple[str, str]:
        owner_user_id = self._resolve_owner_user_id(dict(envelope.arguments or {}), envelope)
        if not envelope.workspace_id:
            return "blocked", "missing_workspace_id"
        if not artifact_refs:
            return "blocked", "missing_identity_artifact_ref"
        if not owner_user_id:
            return "blocked", "missing_owner_user_id"

        try:
            from mica.agentic.event_bus import drain_mudo_subscriber_tasks, get_event_bus
            from mica.agentic.events import MUDOReceiptReady

            bus = get_event_bus()
            delivered = bus.publish(
                MUDOReceiptReady(
                    run_id=envelope.command_id,
                    program_id=envelope.command_name,
                    receipt_kind="CeaMintReceipt",
                    source_surface="command_kernel.cea.mint",
                    protocol_ref="",
                    study_id=str(envelope.study_id or ""),
                    workspace_id=str(envelope.workspace_id or ""),
                    owner_user_id=owner_user_id,
                    input_refs=list(evidence_refs),
                    artifact_refs=list(artifact_refs),
                    evidence_refs=list(evidence_refs),
                    receipt_payload=receipt,
                )
            )
            receipt["mudo_receipt_ready"] = {"published": True, "subscriber_count": delivered}
            drain_summary = await drain_mudo_subscriber_tasks(bus)
            task_errors = list(drain_summary.get("errors", []) or [])
            if task_errors:
                receipt["mudo_receipt_ready"]["subscriber_errors"] = task_errors
                return "not_configured", "mudo_subscriber_failed"
            return "submitted", ""
        except Exception as exc:  # noqa: BLE001
            receipt["mudo_receipt_ready"] = {"published": False, "error": str(exc)}
            return "not_configured", "event_bus_unavailable"

    def _cea_resolve_payload(
        self,
        *,
        identifier: str,
        identity_status: str,
        resolved_to: Optional[str],
        matched_via: Optional[str],
        confidence: float,
        workspace_id: Optional[str],
        identity_authority: str,
        authority_status: str,
        fallback: bool,
        source_ref: Optional[str] = None,
        entity: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        canonical_authority = authority_status == "canonical" and not fallback
        identity_can_support_claim_subject = identity_status == "found" and canonical_authority
        claim_promotion_allowed = False
        claim_promotion_requires = "quetzal.validation_claim_gate"
        receipt = {
            "receipt_type": "CeaResolveReceipt",
            "identifier": identifier,
            "identity_status": identity_status,
            "resolved_to": resolved_to,
            "matched_via": matched_via,
            "confidence": confidence,
            "workspace_id": workspace_id,
            "identity_authority": identity_authority,
            "authority_status": authority_status,
            "fallback": fallback,
            "canonical_authority": canonical_authority,
            "identity_can_support_claim_subject": identity_can_support_claim_subject,
            "claim_promotion_allowed": claim_promotion_allowed,
            "claim_promotion_requires": claim_promotion_requires,
            "no_local_identity_minting": True,
        }
        if source_ref:
            receipt["source_ref"] = source_ref
        if error:
            receipt["error"] = error
        result: Dict[str, Any] = {
            "identifier": identifier,
            "identity_status": identity_status,
            "resolved_to": resolved_to,
            "matched_via": matched_via,
            "confidence": confidence,
            "identity_authority": identity_authority,
            "authority_status": authority_status,
            "fallback": fallback,
            "canonical_authority": canonical_authority,
            "identity_can_support_claim_subject": identity_can_support_claim_subject,
            "claim_promotion_allowed": claim_promotion_allowed,
            "claim_promotion_requires": claim_promotion_requires,
            "receipt": receipt,
        }
        if source_ref:
            result["source_ref"] = source_ref
        if entity is not None:
            result["entity"] = entity
        return {
            "summary": f"CEA resolve returned {identity_status} for {identifier}.",
            "result": result,
            "state_after": {
                "identity_status": identity_status,
                "resolved_to": resolved_to,
                "identity_authority": identity_authority,
                "authority_status": authority_status,
                "fallback": fallback,
                "identity_can_support_claim_subject": identity_can_support_claim_subject,
                "claim_promotion_allowed": claim_promotion_allowed,
            },
        }

    @staticmethod
    def _coerce_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    @staticmethod
    def _coerce_mapping(value: Any) -> dict[str, Any]:
        if value is None or value == "":
            return {}
        if isinstance(value, Mapping):
            return {str(key): val for key, val in value.items()}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise _KernelBlocked(
                    code="schema_validation_failed",
                    message="Expected a valid JSON object string for mapping argument.",
                ) from exc
            if not isinstance(parsed, Mapping):
                raise _KernelBlocked(
                    code="schema_validation_failed",
                    message="Expected a JSON object for mapping argument.",
                )
            return {str(key): val for key, val in parsed.items()}
        raise _KernelBlocked(
            code="schema_validation_failed",
            message="Expected mapping argument to be an object or JSON object string.",
        )

    @staticmethod
    def _cea_mint_collision_candidates(
        *,
        identifier: str,
        name: str,
        cross_references: Mapping[str, Any],
    ) -> list[str]:
        candidates: list[str] = []
        for value in (identifier, name):
            text = str(value or "").strip()
            if text:
                candidates.append(text)
        for key, value in cross_references.items():
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    text = str(item or "").strip()
                    if text:
                        candidates.extend([text, f"{key}:{text}"])
            else:
                text = str(value or "").strip()
                if text:
                    candidates.extend([text, f"{key}:{text}"])
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = _normalize_identity_key(candidate)
            if key not in seen:
                seen.add(key)
                deduped.append(candidate)
        return deduped

    async def _resource_ls(self, args: Dict[str, Any]) -> Dict[str, Any]:
        fixture_name = str(args.get("fixture") or "").strip()
        if fixture_name:
            docs = self._fixture_store.list_documents(fixture_name)
            limit = max(1, min(int(args.get("limit", 20) or 20), 100))
            items = [
                {
                    "ref": doc.ref,
                    "title": doc.title,
                    "path": str(doc.path),
                    "section_count": len(doc.sections),
                }
                for doc in docs[:limit]
            ]
            return {
                "summary": f"Listed {len(items)} fixture resources from {fixture_name}.",
                "result": {"items": items, "count": len(items), "fixture": fixture_name},
                "resource_refs": [item["ref"] for item in items],
            }
        raise ValueError("resource.ls currently requires a local fixture argument in this slice.")

    async def _resource_inspect(self, args: Dict[str, Any], resource_refs: Sequence[str]) -> Dict[str, Any]:
        ref = self._extract_ref(args, resource_refs)
        if ref.startswith("fixture://"):
            doc = self._fixture_store.read_document(ref)
            result = {
                "ref": doc["ref"],
                "title": doc.get("title"),
                "path": doc.get("path"),
                "chars": len(str(doc.get("text") or "")),
                "section_count": len(doc.get("sections") or []),
            }
            if "parent_ref" in doc:
                result["parent_ref"] = doc["parent_ref"]
            return {
                "summary": f"Inspected {ref}.",
                "result": result,
                "resource_refs": [ref],
            }
        if ref.startswith("mica://"):
            text, mime_type = await self._resource_gateway.resolve_mica_uri(ref)
            return {
                "summary": f"Inspected {ref}.",
                "result": {
                    "ref": ref,
                    "mime_type": mime_type,
                    "chars": len(text),
                    "preview": _bounded_summary(text, limit=220),
                },
                "resource_refs": [ref],
            }
        raise RuntimeError(f"Unsupported resource ref: {ref}")

    async def _resource_resolve(self, args: Dict[str, Any], resource_refs: Sequence[str]) -> Dict[str, Any]:
        candidate = self._extract_ref(args, resource_refs, allow_missing=True)
        if candidate.startswith("fixture://") or candidate.startswith("mica://"):
            resolved = candidate
        else:
            fixture_name = str(args.get("fixture") or "").strip()
            if not fixture_name:
                raise RuntimeError("resource.resolve requires a canonical ref or a fixture name.")
            docs = self._fixture_store.list_documents(fixture_name)
            lowered = candidate.lower()
            match = next(
                (
                    doc.ref
                    for doc in docs
                    if lowered in doc.title.lower() or lowered in doc.path.stem.lower()
                ),
                None,
            )
            if not match:
                raise RuntimeError(f"No fixture resource matched alias '{candidate}'.")
            resolved = match
        return {
            "summary": f"Resolved resource '{candidate}' to '{resolved}'.",
            "result": {"input": candidate, "resolved_ref": resolved},
            "resource_refs": [resolved],
        }

    async def _resource_children(self, args: Dict[str, Any], resource_refs: Sequence[str]) -> Dict[str, Any]:
        ref = self._extract_ref(args, resource_refs)
        if ref.startswith("fixture://"):
            doc = self._fixture_store.get_document(ref)
            children = [
                {"ref": section.ref, "title": section.title, "level": section.level}
                for section in doc.sections
            ]
            return {
                "summary": f"Listed {len(children)} child sections for {ref}.",
                "result": {"items": children, "count": len(children)},
                "resource_refs": [item["ref"] for item in children],
            }
        raise RuntimeError(f"resource.children is only implemented for fixture refs in this slice: {ref}")

    async def _resource_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        fixture_name = str(args.get("fixture") or "").strip()
        query = str(args.get("query") or args.get("pattern") or "").strip()
        if not fixture_name or not query:
            raise RuntimeError("resource.search requires fixture and query in this slice.")
        docs = self._fixture_store.list_documents(fixture_name)
        matches: list[dict[str, Any]] = []
        lowered = query.lower()
        for doc in docs:
            haystacks = [doc.title.lower(), doc.text.lower()]
            if any(lowered in hay for hay in haystacks):
                index = doc.text.lower().find(lowered)
                matches.append(
                    {
                        "ref": doc.ref,
                        "title": doc.title,
                        "snippet": _snippet_around(doc.text, max(index, 0)),
                    }
                )
        limit = max(1, min(int(args.get("limit", 20) or 20), 100))
        matches = matches[:limit]
        return {
            "summary": f"Found {len(matches)} resource matches for '{query}'.",
            "result": {"items": matches, "count": len(matches), "query": query},
            "resource_refs": [item["ref"] for item in matches],
        }

    async def _document_read(self, args: Dict[str, Any], resource_refs: Sequence[str]) -> Dict[str, Any]:
        ref = self._extract_ref(args, resource_refs)
        max_chars = max(100, min(int(args.get("max_chars", 1500) or 1500), 10_000))
        payload = await self._read_ref_text(ref)
        text = str(payload["text"] or "")
        bounded = text[:max_chars]
        return {
            "summary": f"Read {len(bounded)} chars from {ref}.",
            "result": {
                "ref": ref,
                "title": payload.get("title"),
                "text": bounded,
                "truncated": len(text) > len(bounded),
                "total_chars": len(text),
            },
            "resource_refs": [ref],
        }

    async def _document_read_section(self, args: Dict[str, Any], resource_refs: Sequence[str]) -> Dict[str, Any]:
        ref = self._extract_ref(args, resource_refs)
        section = str(args.get("section") or "").strip()
        if section and ref.startswith("fixture://") and "#section:" not in ref:
            ref = f"{ref}#section:{_slugify(section)}"
        max_chars = max(100, min(int(args.get("max_chars", 1500) or 1500), 10_000))
        payload = await self._read_ref_text(ref)
        text = str(payload["text"] or "")
        bounded = text[:max_chars]
        return {
            "summary": f"Read section from {ref}.",
            "result": {
                "ref": ref,
                "title": payload.get("title"),
                "text": bounded,
                "truncated": len(text) > len(bounded),
                "total_chars": len(text),
            },
            "resource_refs": [ref],
        }

    async def _corpus_grep(self, args: Dict[str, Any]) -> Dict[str, Any]:
        corpus_ref = str(args.get("corpus_ref") or args.get("fixture") or "").strip()
        pattern = str(args.get("pattern") or "").strip()
        if not corpus_ref or not pattern:
            raise RuntimeError("corpus.grep requires corpus_ref and pattern.")
        limit = max(1, min(int(args.get("limit", 20) or 20), 100))
        case_sensitive = bool(args.get("case_sensitive", False))

        if corpus_ref.startswith("fixture://"):
            fixture_name = corpus_ref[len("fixture://") :]
        else:
            fixture_name = corpus_ref
        docs = self._fixture_store.list_documents(fixture_name)
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(re.escape(pattern), flags)
        matches: list[dict[str, Any]] = []
        for doc in docs:
            for match in regex.finditer(doc.text):
                matches.append(
                    {
                        "ref": doc.ref,
                        "title": doc.title,
                        "snippet": _snippet_around(doc.text, match.start()),
                    }
                )
                if len(matches) >= limit:
                    break
            if len(matches) >= limit:
                break
        return {
            "summary": f"Found {len(matches)} snippets for '{pattern}' in {fixture_name}.",
            "result": {
                "items": matches,
                "count": len(matches),
                "corpus_ref": corpus_ref,
                "pattern": pattern,
            },
            "resource_refs": [item["ref"] for item in matches],
        }

    async def _read_ref_text(self, ref: str) -> Dict[str, Any]:
        if ref.startswith("fixture://"):
            return self._fixture_store.read_document(ref)
        if ref.startswith("mica://"):
            text, mime_type = await self._resource_gateway.resolve_mica_uri(ref)
            return {"ref": ref, "title": ref, "text": text, "mime_type": mime_type}
        raise RuntimeError(f"Unsupported document ref: {ref}")

    def _extract_ref(
        self,
        args: Dict[str, Any],
        resource_refs: Sequence[str],
        *,
        allow_missing: bool = False,
    ) -> str:
        ref = str(args.get("ref") or args.get("resource_ref") or "").strip()
        if not ref and resource_refs:
            ref = str(resource_refs[0] or "").strip()
        if ref or allow_missing:
            return ref
        raise RuntimeError("Command requires a resource reference.")

    def _blocked(
        self,
        *,
        command_name: str,
        binding_surface: str,
        backend_authority: str,
        blocker: BackendCommandBlocker,
        started: float,
    ) -> BackendCommandResult:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return BackendCommandResult(
            success=False,
            command_name=command_name,
            binding_surface=binding_surface,
            summary=_bounded_summary(blocker.message),
            status="blocked",
            blocker_code=blocker.code,
            blockers=[blocker],
            cost_snapshot=BackendCommandCostSnapshot(usd=0.0, tool_calls=0),
            trace=BackendCommandTrace(
                route_authority="shared",
                backend_authority=backend_authority,
                duration_ms=duration_ms,
            ),
        )


def serialize_backend_command_result(result: BackendCommandResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str)

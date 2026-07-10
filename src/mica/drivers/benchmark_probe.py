"""Benchmark probe for harness change before/after measurement.

Captures four measurable dimensions at delegation closure:

  1. retrieval_calls  — tool_calls proxy (total; subdivide when event-log
                        filtering lands per BENCH-003)
  2. tokens_consumed  — BudgetState.consumed_tokens per delegation
  3. elapsed_s        — DelegationSession finished_at − created_at (wall clock)
  4. revalidation_loops — len(gaps_log) + len(review_issues) in RoleContext

Results are written as newline-delimited JSON to
  .mica/benchmarks/<harness_tag>/<YYYY-MM-DD_<run_id>.ndjson

and a companion human-readable TRACE entry is appended to
  .mica/programs/<program_id>/TRACE.md   (if that file exists).

Typical usage at delegation closure::

    from mica.drivers.benchmark_probe import BenchmarkProbe
    probe = BenchmarkProbe(program_id="POC_SUPERNOVA", harness_tag="before")
    probe.record(delegation=session, role_ctx=role_ctx, budget=budget_state)
    probe.flush(workspace_root=pathlib.Path("."))
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

# ---------------------------------------------------------------------------
# Packet schema
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "1.0"


@dataclass
class BenchmarkPacket:
    """Immutable measurement packet for one atomic delegation closure.

    Fields
    ------
    run_id
        Unique run identifier (delegation session_id or parent run_id).
    program_id
        .mica/programs/ owning program, e.g. ``POC_SUPERNOVA``.
    harness_tag
        Free label for the comparison leg, e.g. ``"before"``, ``"after"``,
        ``"v1"``, ``"v2"``.
    task_id
        Logical task name within the program, e.g. ``"dlm_scan"``.
    delegated_agent
        Agent name from DelegationSession.delegated_agent.
    closure_status
        Terminal status string: completed / failed / timed_out / cancelled.
    ts_start
        ISO-8601 UTC string from DelegationSession.created_at.
    ts_closure
        ISO-8601 UTC string from DelegationSession.finished_at.
    elapsed_s
        Wall-clock seconds from start to closure (−1.0 if not computable).
    retrieval_calls
        Total tool_calls_count at closure.  Proxy for retrieval calls until
        per-class event filtering is instrumented (BENCH-003).
    tokens_consumed
        BudgetState.consumed_tokens at delegation closure.
    revalidation_loops
        len(gaps_log) + len(review_issues) from RoleContext.  Each entry
        represents a gap/issue cycle that required a correction pass.
    delegation_iterations
        DelegationSession.iterations_count or RoleContext.iterations_count —
        whichever is non-zero (prefer delegation).
    schema_version
        Packet schema version for forward compatibility.
    notes
        Optional free-text annotation.
    """

    run_id: str
    program_id: str
    harness_tag: str
    task_id: str
    delegated_agent: str
    closure_status: str
    ts_start: str
    ts_closure: str
    elapsed_s: float
    retrieval_calls: int
    tokens_consumed: int
    revalidation_loops: int
    delegation_iterations: int
    schema_version: str = _SCHEMA_VERSION
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    # ------------------------------------------------------------------
    # Human summary for TRACE.md append
    # ------------------------------------------------------------------

    def trace_line(self) -> str:
        elapsed = f"{self.elapsed_s:.1f}s" if self.elapsed_s >= 0 else "n/a"
        return (
            f"| {self.ts_closure[:19]}Z "
            f"| {self.harness_tag:<8} "
            f"| {self.task_id:<28} "
            f"| {self.delegated_agent:<24} "
            f"| {self.closure_status:<10} "
            f"| {elapsed:>7} "
            f"| {self.retrieval_calls:>4} "
            f"| {self.tokens_consumed:>8} "
            f"| {self.revalidation_loops:>3} "
            f"| {self.delegation_iterations:>4} "
            f"|"
        )


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _elapsed_seconds(created_at: str, finished_at: Optional[str]) -> float:
    """Return wall-clock seconds or -1.0 if timestamps are absent/invalid."""
    if not finished_at:
        return -1.0
    try:
        t0 = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        return round((t1 - t0).total_seconds(), 3)
    except Exception:  # noqa: BLE001
        return -1.0


def extract_from_closure(
    *,
    delegation: Any,           # DelegationSession
    role_ctx: Any,             # RoleContext  (may be None)
    budget: Any,               # BudgetState  (may be None)
    program_id: str,
    harness_tag: str,
    task_id: str = "",
    notes: str = "",
) -> BenchmarkPacket:
    """Build a BenchmarkPacket from live driver objects at closure time.

    Parameters
    ----------
    delegation
        A ``DelegationSession`` instance (duck-typed; uses attribute access).
    role_ctx
        A ``RoleContext`` instance.  Pass ``None`` if unavailable.
    budget
        A ``BudgetState`` instance.  Pass ``None`` if unavailable.
    program_id
        Owning program name.
    harness_tag
        Label for this measurement leg (``"before"``, ``"after"``, etc.).
    task_id
        Logical task name.  Falls back to ``delegation.delegated_agent``.
    notes
        Free-text annotation.
    """
    # Core identity
    run_id = str(getattr(delegation, "session_id", "") or "")
    agent = str(getattr(delegation, "delegated_agent", "") or "")
    task_id = task_id or agent

    # Timestamps
    created_at = str(getattr(delegation, "created_at", "") or "")
    finished_at = getattr(delegation, "finished_at", None) or ""

    if not finished_at:
        finished_at = datetime.now(timezone.utc).isoformat()

    elapsed = _elapsed_seconds(created_at, finished_at)

    # Status
    raw_status = getattr(delegation, "status", None)
    if hasattr(raw_status, "value"):
        closure_status = raw_status.value
    else:
        closure_status = str(raw_status or "unknown")

    # tool_calls_count: prefer delegation, fall back to role_ctx
    retrieval_calls = int(
        getattr(delegation, "tool_calls_count", None)
        or getattr(role_ctx, "tool_calls_count", 0)
        or 0
    )

    # tokens_consumed: from BudgetState
    tokens_consumed = int(
        getattr(budget, "consumed_tokens", 0) or 0
    )

    # revalidation_loops: gaps_log + review_issues
    gaps = list(getattr(role_ctx, "gaps_log", []) or [])
    issues = list(getattr(role_ctx, "review_issues", []) or [])
    revalidation_loops = len(gaps) + len(issues)

    # delegation iterations
    delegation_iterations = int(
        getattr(delegation, "iterations_count", None)
        or getattr(role_ctx, "iterations_count", 0)
        or 0
    )

    return BenchmarkPacket(
        run_id=run_id,
        program_id=program_id,
        harness_tag=harness_tag,
        task_id=task_id,
        delegated_agent=agent,
        closure_status=closure_status,
        ts_start=created_at,
        ts_closure=finished_at,
        elapsed_s=elapsed,
        retrieval_calls=retrieval_calls,
        tokens_consumed=tokens_consumed,
        revalidation_loops=revalidation_loops,
        delegation_iterations=delegation_iterations,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Probe — accumulates packets and flushes to disk
# ---------------------------------------------------------------------------

_TRACE_HEADER = (
    "| ts_closure          | tag      | task_id                       "
    "| agent                    | status     | elapsed | retr | tokens   "
    "| reval | iters |\n"
    "|---------------------|----------|-------------------------------|"
    "--------------------------|------------|---------|------|----------|"
    "-------|-------|\n"
)


class BenchmarkProbe:
    """Stateful accumulator for one harness run.

    Usage::

        probe = BenchmarkProbe(program_id="POC_SUPERNOVA", harness_tag="before")
        # ... at each delegation closure:
        probe.record(delegation=ds, role_ctx=rc, budget=bs)
        # ... at end of session:
        probe.flush(workspace_root=pathlib.Path("c:/Users/busta/Downloads/MICA"))
    """

    def __init__(self, *, program_id: str, harness_tag: str) -> None:
        self.program_id = program_id
        self.harness_tag = harness_tag
        self._packets: List[BenchmarkPacket] = []

    def record(
        self,
        *,
        delegation: Any,
        role_ctx: Any = None,
        budget: Any = None,
        task_id: str = "",
        notes: str = "",
    ) -> BenchmarkPacket:
        """Extract metrics and append a packet.  Returns the packet."""
        packet = extract_from_closure(
            delegation=delegation,
            role_ctx=role_ctx,
            budget=budget,
            program_id=self.program_id,
            harness_tag=self.harness_tag,
            task_id=task_id,
            notes=notes,
        )
        self._packets.append(packet)
        return packet

    def flush(self, workspace_root: pathlib.Path) -> pathlib.Path:
        """Write packets to .mica/benchmarks/<tag>/ and update TRACE.md.

        Returns the path of the written .ndjson file.
        """
        if not self._packets:
            raise ValueError("No packets to flush")

        # ── Storage path ──────────────────────────────────────────────
        bench_dir = workspace_root / ".mica" / "benchmarks" / self.harness_tag
        bench_dir.mkdir(parents=True, exist_ok=True)

        date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Use first packet run_id prefix for filename
        run_prefix = self._packets[0].run_id[:8] if self._packets[0].run_id else "run"
        out_file = bench_dir / f"{date_tag}_{self.program_id}_{run_prefix}.ndjson"

        with out_file.open("a", encoding="utf-8") as fh:
            for pkt in self._packets:
                fh.write(pkt.to_json() + "\n")

        # ── TRACE.md append ───────────────────────────────────────────
        trace_path = (
            workspace_root
            / ".mica"
            / "programs"
            / self.program_id
            / "TRACE.md"
        )
        if trace_path.exists():
            self._append_trace(trace_path)

        self._packets.clear()
        return out_file

    def _append_trace(self, trace_path: pathlib.Path) -> None:
        """Append a benchmark section to an existing TRACE.md."""
        header_marker = "## Benchmark Packets"
        existing = trace_path.read_text(encoding="utf-8")
        lines = [pkt.trace_line() for pkt in self._packets]
        block = "\n".join(lines)

        if header_marker in existing:
            # Insert after the existing table header (two separator lines)
            insert_after = "-------|-------|\n"
            idx = existing.rfind(insert_after)
            if idx != -1:
                new_content = (
                    existing[: idx + len(insert_after)]
                    + block
                    + "\n"
                    + existing[idx + len(insert_after):]
                )
                trace_path.write_text(new_content, encoding="utf-8")
                return
        # First time: append a new section
        section = f"\n\n{header_marker}\n\n{_TRACE_HEADER}{block}\n"
        with trace_path.open("a", encoding="utf-8") as fh:
            fh.write(section)

"""
mica.drivers.workflow_dag
=========================
Driver-level WorkflowDAG: orchestrates parallel execution of independent
specialist drivers (BioDynamo, Alchemist, SMIC).

Anti-rigidity: a failing node does NOT stop independent sibling nodes.
Standard-library + asyncio only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from mica.drivers.program_envelope import ProgramEnvelope


# ---------------------------------------------------------------------------
# TaskStatus
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED   = "FAILED"
    SKIPPED  = "SKIPPED"


# ---------------------------------------------------------------------------
# DAGTask
# ---------------------------------------------------------------------------

@dataclass
class DAGTask:
    task_id:      str
    driver_id:    str
    description:  str
    status:       TaskStatus        = TaskStatus.PENDING
    dependencies: list[str]         = field(default_factory=list)
    result:       dict | None       = None
    phase_event:  Any | None        = None
    error:        str               = ""
    started_at:   str               = ""
    completed_at: str               = ""
    priority:     int               = 0
    program_id:   str               = ""  # S2.2: links to ProgramEnvelope.program_id

    # ------------------------------------------------------------------
    def is_ready(self, completed_ids: set[str]) -> bool:
        """True when all dependency task_ids are in *completed_ids*."""
        return all(dep in completed_ids for dep in self.dependencies)

    def is_terminal(self) -> bool:
        return self.status in (TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.SKIPPED)

    def elapsed_seconds(self) -> float:
        """Return wall-clock duration if both timestamps are set, else 0.0."""
        if not self.started_at or not self.completed_at:
            return 0.0
        try:
            fmt = "%Y-%m-%dT%H:%M:%S.%f"
            start = datetime.strptime(self.started_at,  fmt)
            end   = datetime.strptime(self.completed_at, fmt)
            return max(0.0, (end - start).total_seconds())
        except ValueError:
            return 0.0

    def to_dict(self) -> dict:
        d = {
            "task_id":      self.task_id,
            "driver_id":    self.driver_id,
            "description":  self.description,
            "status":       self.status.value,
            "dependencies": list(self.dependencies),
            "result":       self.result,
            "phase_event":  str(self.phase_event) if self.phase_event is not None else None,
            "error":        self.error,
            "started_at":   self.started_at,
            "completed_at": self.completed_at,
            "priority":     self.priority,
        }
        if self.program_id:
            d["program_id"] = self.program_id
        return d


# ---------------------------------------------------------------------------
# DAGResult
# ---------------------------------------------------------------------------

@dataclass
class DAGResult:
    workflow_id:           str
    completed_tasks:       list[DAGTask]
    failed_tasks:          list[DAGTask]
    skipped_tasks:         list[DAGTask]
    total_elapsed_seconds: float
    degraded:              bool

    # ------------------------------------------------------------------
    @property
    def success_count(self) -> int:
        return len(self.completed_tasks)

    @property
    def failure_count(self) -> int:
        return len(self.failed_tasks)

    @property
    def is_fully_successful(self) -> bool:
        return self.failure_count == 0

    def artifacts(self, task_id: str) -> list[dict]:
        """Return phase_event.artifacts for *task_id*, or [] if absent."""
        all_tasks = self.completed_tasks + self.failed_tasks + self.skipped_tasks
        for t in all_tasks:
            if t.task_id == task_id and t.phase_event is not None:
                raw = getattr(t.phase_event, "artifacts", [])
                return list(raw) if raw else []
        return []

    def to_dict(self) -> dict:
        return {
            "workflow_id":           self.workflow_id,
            "completed_tasks":       [t.to_dict() for t in self.completed_tasks],
            "failed_tasks":          [t.to_dict() for t in self.failed_tasks],
            "skipped_tasks":         [t.to_dict() for t in self.skipped_tasks],
            "total_elapsed_seconds": self.total_elapsed_seconds,
            "degraded":              self.degraded,
            "success_count":         self.success_count,
            "failure_count":         self.failure_count,
            "is_fully_successful":   self.is_fully_successful,
        }


# ---------------------------------------------------------------------------
# WorkflowDAG
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


class WorkflowDAG:
    """
    DAG that represents dependencies between specialist driver tasks and
    executes independent ones in parallel via asyncio.gather.
    """

    def __init__(self, workflow_id: str, *, program_envelope: Optional[ProgramEnvelope] = None) -> None:
        self.workflow_id:        str                          = workflow_id
        self._tasks:             dict[str, DAGTask]           = {}
        self._execution_log:     list[str]                    = []
        self._program_envelope:  Optional[ProgramEnvelope]    = program_envelope

    @property
    def program_envelope(self) -> Optional[ProgramEnvelope]:
        """Return the ProgramEnvelope attached to this DAG, if any."""
        return self._program_envelope

    def attach_program(self, envelope: ProgramEnvelope) -> None:
        """Attach or replace the ProgramEnvelope for this DAG."""
        self._program_envelope = envelope

    # ------------------------------------------------------------------
    # Building the graph
    # ------------------------------------------------------------------

    def add_task(self, task: DAGTask) -> "WorkflowDAG":
        """Fluent builder. Raises ValueError on duplicate task_id.

        If a ProgramEnvelope is attached and the task has no program_id,
        the envelope's program_id is propagated automatically.
        """
        if task.task_id in self._tasks:
            raise ValueError(f"Duplicate task_id '{task.task_id}'")
        if not task.program_id and self._program_envelope is not None:
            task.program_id = self._program_envelope.program_id
        self._tasks[task.task_id] = task
        return self

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """
        Return a (possibly empty) list of validation error strings:
        - Missing dependency references
        - Circular dependencies (DFS)
        """
        errors: list[str] = []
        all_ids = set(self._tasks)

        # 1. Missing deps
        for tid, task in self._tasks.items():
            for dep in task.dependencies:
                if dep not in all_ids:
                    errors.append(
                        f"Task '{tid}' depends on '{dep}' which does not exist."
                    )

        # 2. Cycle detection (DFS coloring: 0=white, 1=gray, 2=black)
        color: dict[str, int] = {tid: 0 for tid in all_ids}

        def dfs(node: str) -> bool:
            """Returns True if a cycle is detected."""
            color[node] = 1
            for dep in self._tasks[node].dependencies:
                if dep not in color:
                    continue  # already flagged as missing
                if color[dep] == 1:
                    return True
                if color[dep] == 0 and dfs(dep):
                    return True
            color[node] = 2
            return False

        for tid in all_ids:
            if color[tid] == 0:
                if dfs(tid):
                    errors.append(
                        f"Circular dependency detected involving task '{tid}'."
                    )

        return errors

    # ------------------------------------------------------------------
    # Topological ordering
    # ------------------------------------------------------------------

    def topological_order(self) -> list[list[str]]:
        """
        Returns list-of-levels: tasks in level N depend only on tasks in
        levels 0..N-1.  Tasks within one level are independent and can run
        in parallel.
        """
        remaining   = {tid: set(t.dependencies) for tid, t in self._tasks.items()}
        all_task_ids = set(self._tasks)
        # Filter deps that are not in the graph (already a validation concern)
        for tid in remaining:
            remaining[tid] &= all_task_ids

        levels: list[list[str]] = []
        completed: set[str] = set()

        while remaining:
            ready = sorted(
                [tid for tid, deps in remaining.items() if deps <= completed],
                key=lambda t: self._tasks[t].priority,
            )
            if not ready:
                # Cycle – return what we have (validate() catches it properly)
                break
            levels.append(ready)
            completed |= set(ready)
            for tid in ready:
                del remaining[tid]

        return levels

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    async def _run_task(
        self,
        task: DAGTask,
        executor: Callable[[DAGTask], Awaitable[dict]],
    ) -> None:
        """Execute one task, updating its status in-place."""
        task.status     = TaskStatus.RUNNING
        task.started_at = _now_iso()
        try:
            raw = await executor(task)
            if isinstance(raw, BaseException):
                raise raw
            if isinstance(raw, dict) and raw.get("status") == "error":
                task.status      = TaskStatus.FAILED
                task.error       = raw.get("message", str(raw))
            else:
                task.status = TaskStatus.COMPLETE
                task.result = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error  = str(exc)
        finally:
            task.completed_at = _now_iso()

    # ------------------------------------------------------------------
    # Public execution API
    # ------------------------------------------------------------------

    async def execute(
        self,
        executor: Callable[[DAGTask], Awaitable[dict]],
    ) -> DAGResult:
        """
        Execute the DAG level by level.  Within each level all PENDING tasks
        run concurrently via asyncio.gather.  A failed task does NOT block
        sibling tasks; downstream dependents are SKIPPED.
        """
        errors = self.validate()
        if errors:
            raise ValueError(errors)

        wall_start = datetime.now(timezone.utc)
        levels     = self.topological_order()
        failed_ids: set[str] = set()

        for level_ids in levels:
            # Determine which tasks in this level should actually run
            to_run: list[DAGTask] = []
            for tid in level_ids:
                task = self._tasks[tid]
                if task.is_terminal():
                    continue
                # Check if any dependency failed
                if any(dep in failed_ids for dep in task.dependencies):
                    task.status = TaskStatus.SKIPPED
                    self._execution_log.append(f"SKIP {tid} (dep failed)")
                else:
                    to_run.append(task)

            if not to_run:
                continue

            # Sort by priority
            to_run.sort(key=lambda t: t.priority)

            # Run all concurrently; capture exceptions via return_exceptions=True
            results = await asyncio.gather(
                *[self._run_task(t, executor) for t in to_run],
                return_exceptions=True,
            )

            # Collect exception results (asyncio.gather with return_exceptions
            # gives the coroutine return, but _run_task returns None; exceptions
            # appear here only if something outside the try block raises)
            for t, exc in zip(to_run, results):
                if isinstance(exc, BaseException):
                    t.status = TaskStatus.FAILED
                    t.error  = str(exc)
                    t.completed_at = t.completed_at or _now_iso()
                if t.status == TaskStatus.FAILED:
                    failed_ids.add(t.task_id)
                self._execution_log.append(f"{t.status.value} {t.task_id}")

            # Propagate SKIPs to tasks in later levels
            for later_task in self._tasks.values():
                if later_task.status == TaskStatus.PENDING:
                    if any(dep in failed_ids for dep in later_task.dependencies):
                        later_task.status = TaskStatus.SKIPPED
                        failed_ids.add(later_task.task_id)

        wall_end = datetime.now(timezone.utc)
        elapsed  = (wall_end - wall_start).total_seconds()

        all_tasks    = list(self._tasks.values())
        completed    = [t for t in all_tasks if t.status == TaskStatus.COMPLETE]
        failed       = [t for t in all_tasks if t.status == TaskStatus.FAILED]
        skipped      = [t for t in all_tasks if t.status == TaskStatus.SKIPPED]
        degraded     = len(failed) > 0 or len(skipped) > 0 or len(completed) < len(all_tasks)

        return DAGResult(
            workflow_id=self.workflow_id,
            completed_tasks=completed,
            failed_tasks=failed,
            skipped_tasks=skipped,
            total_elapsed_seconds=elapsed,
            degraded=degraded,
        )

    async def execute_with_timeout(
        self,
        executor: Callable[[DAGTask], Awaitable[dict]],
        timeout_seconds: float = 300.0,
    ) -> DAGResult:
        """
        Wraps execute() with asyncio.wait_for.  On timeout: mark RUNNING tasks
        as FAILED and return a partial DAGResult.
        """
        try:
            return await asyncio.wait_for(
                self.execute(executor), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            wall_end = datetime.now(timezone.utc)
            all_tasks = list(self._tasks.values())
            for t in all_tasks:
                if t.status == TaskStatus.RUNNING:
                    t.status      = TaskStatus.FAILED
                    t.error       = "Execution timed out"
                    t.completed_at = _now_iso()

            completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETE]
            failed    = [t for t in all_tasks if t.status == TaskStatus.FAILED]
            skipped   = [t for t in all_tasks if t.status == TaskStatus.SKIPPED]
            return DAGResult(
                workflow_id=self.workflow_id,
                completed_tasks=completed,
                failed_tasks=failed,
                skipped_tasks=skipped,
                total_elapsed_seconds=timeout_seconds,
                degraded=True,
            )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> DAGTask | None:
        return self._tasks.get(task_id)

    def summary(self) -> str:
        all_tasks = list(self._tasks.values())
        n         = len(all_tasks)
        complete  = sum(1 for t in all_tasks if t.status == TaskStatus.COMPLETE)
        failed    = sum(1 for t in all_tasks if t.status == TaskStatus.FAILED)
        skipped   = sum(1 for t in all_tasks if t.status == TaskStatus.SKIPPED)
        return (
            f"WorkflowDAG {self.workflow_id}: {n} tasks, "
            f"{complete} complete, {failed} failed, {skipped} skipped"
        )


# ---------------------------------------------------------------------------
# Factory: standard MICA 3-specialist workflow
# ---------------------------------------------------------------------------

def build_standard_workflow(
    workflow_id: str,
    available_drivers: list[str],
) -> WorkflowDAG:
    """
    Build the canonical MICA specialist workflow:

        Level 0 (parallel): literature_search, smiles_resolution, structure_download
        Level 1:            docking          (deps: smiles_resolution, structure_download)
        Level 2:            md_simulation    (dep: docking)
        Level 3:            analysis         (dep: md_simulation)

    Only tasks whose driver_id is in *available_drivers* are added.
    Tasks with unresolvable dependencies are excluded gracefully.
    """
    driver_set = set(available_drivers)

    # Full blueprint
    blueprint = [
        DAGTask(
            task_id="literature_search",
            driver_id="AlchemistDriver",
            description="Search literature for compound and target information",
            dependencies=[],
            priority=0,
        ),
        DAGTask(
            task_id="smiles_resolution",
            driver_id="AlchemistDriver",
            description="Resolve SMILES strings for candidate compounds",
            dependencies=[],
            priority=0,
        ),
        DAGTask(
            task_id="structure_download",
            driver_id="AlchemistDriver",
            description="Download protein structure from PDB",
            dependencies=[],
            priority=0,
        ),
        DAGTask(
            task_id="docking",
            driver_id="AlchemistDriver",
            description="Perform molecular docking",
            dependencies=["smiles_resolution", "structure_download"],
            priority=1,
        ),
        DAGTask(
            task_id="md_simulation",
            driver_id="BioDynamoDriver",
            description="Run molecular dynamics simulation",
            dependencies=["docking"],
            priority=2,
        ),
        DAGTask(
            task_id="analysis",
            driver_id="SMICDriver",
            description="Analyse simulation trajectories and binding free energy",
            dependencies=["md_simulation"],
            priority=3,
        ),
    ]

    # Filter by available drivers
    included_ids = {t.task_id for t in blueprint if t.driver_id in driver_set}

    dag = WorkflowDAG(workflow_id)
    for task in blueprint:
        if task.task_id not in included_ids:
            continue
        # Strip deps that were excluded
        task.dependencies = [
            dep for dep in task.dependencies if dep in included_ids
        ]
        dag.add_task(task)

    return dag

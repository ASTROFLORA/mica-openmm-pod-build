"""GHP Copilot subagent executor (Pillar 2, R28 W1).

Bridges ``GhpDelegatedTask`` -> ``GhpDelegatedResult``. The "GHP" path is a
GitHub-Pilot-style sub-agent: a fresh worker process is spawned with a fenced
file allowlist, given an objective + constraints, and its diff is captured.

This Rung-0 implementation is **process-fenced**, not LLM-fenced — it spawns
``sys.executable`` with a small Python script that the caller authors via
``task.objective`` + ``task.must_produce``. The point is to prove the
contract round-trip and the file-fence enforcement; Rung-1 will wire the
real LLM call (Anthropic / OpenAI) inside the same envelope.

Safety rails:
    - The subprocess writes only inside a tempdir copy of the allowed files.
    - File allowlist is matched against the workspace BEFORE any write.
    - Forbidden zones are checked AFTER the run; any touch is a hard reject.
    - Wall-clock timeout enforced via ``subprocess.run(timeout=...)``.
    - All exceptions are mapped onto ``GhpDelegatedResult`` — the executor
      never raises to the caller.

**Trust boundary (Rung-0)**:
    The subprocess is NOT chrooted or namespaced. A malicious ``runner_script``
    can read/write outside the tempdir (anywhere the calling user can). For
    Rung-0 we therefore require ``runner_script`` to be **orchestrator-authored**
    (i.e. constructed by the driver, not by the subagent). Rung-1+ will run
    the executor inside Modal/Cuenta-2-Railway sandboxes where the host
    filesystem is genuinely isolated.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path
from typing import Iterable

from mica.sdk.contracts import GhpDelegatedResult, GhpDelegatedTask


def _matches_any(rel_path: str, patterns: Iterable[str]) -> bool:
    rel_norm = rel_path.replace(os.sep, "/")
    return any(fnmatch.fnmatch(rel_norm, p) for p in patterns)


def _diff_dirs(before: Path, after: Path, files: Iterable[Path]) -> str:
    chunks: list[str] = []
    for f in files:
        rel = f.relative_to(after).as_posix()
        a = before / rel
        a_lines = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if a.exists() else []
        b_lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        for line in difflib.unified_diff(a_lines, b_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}"):
            chunks.append(line)
    return "".join(chunks)


def _copy_allowed(workspace_root: Path, dest: Path, allowlist: list[str]) -> list[Path]:
    copied: list[Path] = []
    for pat in allowlist:
        for src in workspace_root.glob(pat):
            if not src.is_file():
                continue
            rel = src.relative_to(workspace_root)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(target)
    return copied


def _classify_changes(before: Path, after: Path) -> tuple[list[str], list[str], list[str]]:
    before_files = {p.relative_to(before).as_posix(): p for p in before.rglob("*") if p.is_file()}
    after_files = {p.relative_to(after).as_posix(): p for p in after.rglob("*") if p.is_file()}

    added = sorted(set(after_files) - set(before_files))
    deleted = sorted(set(before_files) - set(after_files))
    modified = sorted(
        rel
        for rel in set(before_files) & set(after_files)
        if before_files[rel].read_bytes() != after_files[rel].read_bytes()
    )
    return added, modified, deleted


def _export_artifact_tree(source: Path, artifact_dir: Path | None) -> str | None:
    if artifact_dir is None:
        return None
    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir, ignore_errors=True)
    shutil.copytree(source, artifact_dir)
    return str(artifact_dir)


async def execute_delegated_task(
    task: GhpDelegatedTask,
    workspace_root: Path,
    runner_script: str | None = None,
    artifact_dir: Path | None = None,
) -> GhpDelegatedResult:
    """Execute a single delegated task in a fenced subprocess.

    ``runner_script`` is the python source the GHP subagent will run inside
    the fenced copy. For Rung-0 tests we accept it as input; Rung-1 will
    have the LLM author it.
    """

    started = time.perf_counter()
    sandbox = Path(tempfile.mkdtemp(prefix="ghp_session_"))
    before_dir = sandbox / "before"
    after_dir = sandbox / "after"
    before_dir.mkdir()
    after_dir.mkdir()

    if not task.file_allowlist:
        return GhpDelegatedResult(
            task_id=task.task_id,
            status="rejected",
            reason="empty file_allowlist — Rung-0 requires explicit fence",
            artifact_root=None,
        )

    try:
        copied_a = _copy_allowed(workspace_root, before_dir, task.file_allowlist)
        copied_b = _copy_allowed(workspace_root, after_dir, task.file_allowlist)

        if not copied_a and not copied_b:
            return GhpDelegatedResult(
                task_id=task.task_id,
                status="rejected",
                reason="file_allowlist matched zero files in workspace",
                artifact_root=None,
            )

        if runner_script is None:
            # No runner provided -> Rung-0 echo path: the contract round-trips
            # cleanly with status=completed and an empty diff. This is what
            # the Rung-0 acid test exercises.
            wall = time.perf_counter() - started
            return GhpDelegatedResult(
                task_id=task.task_id,
                status="completed",
                diff="",
                verdict_block=textwrap.dedent(
                    f"""\
                    Rung-0 echo executor: no runner_script supplied.
                    objective={task.objective[:200]}
                    files_in_fence={len(copied_b)}
                    wall_seconds={wall:.4f}
                    """
                ).strip(),
                files_added=[],
                files_modified=[],
                files_deleted=[],
                artifact_root=_export_artifact_tree(after_dir, artifact_dir),
            )

        # Run the subagent script with cwd=after_dir so any path-relative
        # writes land inside the fence.
        script_file = sandbox / "runner.py"
        script_file.write_text(runner_script, encoding="utf-8")

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(script_file)],
                cwd=str(after_dir),
                capture_output=True,
                text=True,
                timeout=max(5, task.max_wall_seconds),
            )
        except subprocess.TimeoutExpired:
            wall = time.perf_counter() - started
            return GhpDelegatedResult(
                task_id=task.task_id,
                status="timeout",
                reason=f"subprocess exceeded {task.max_wall_seconds}s",
                diff="",
                verdict_block=f"timeout after {wall:.2f}s",
                artifact_root=None,
            )

        added, modified, deleted = _classify_changes(before_dir, after_dir)
        exported_artifact_root = _export_artifact_tree(after_dir, artifact_dir)

        # Forbidden-zone enforcement: if anything touched matches a
        # forbidden pattern, hard-reject.
        all_touched = added + modified + deleted
        for path in all_touched:
            if task.forbidden_zones and _matches_any(path, task.forbidden_zones):
                return GhpDelegatedResult(
                    task_id=task.task_id,
                    status="rejected",
                    reason=f"touched forbidden zone: {path}",
                    files_added=added,
                    files_modified=modified,
                    files_deleted=deleted,
                    artifact_root=exported_artifact_root,
                )

        diff = _diff_dirs(before_dir, after_dir, [after_dir / p for p in modified + added])
        wall = time.perf_counter() - started

        if proc.returncode != 0:
            return GhpDelegatedResult(
                task_id=task.task_id,
                status="failed",
                reason=f"runner rc={proc.returncode} stderr={proc.stderr.strip()[:300]}",
                diff=diff,
                files_added=added,
                files_modified=modified,
                files_deleted=deleted,
                verdict_block=f"stdout={proc.stdout.strip()[:300]} wall={wall:.2f}s",
                artifact_root=exported_artifact_root,
            )

        return GhpDelegatedResult(
            task_id=task.task_id,
            status="completed",
            diff=diff,
            files_added=added,
            files_modified=modified,
            files_deleted=deleted,
            verdict_block=textwrap.dedent(
                f"""\
                rc=0 wall={wall:.2f}s
                stdout_head={proc.stdout.strip()[:300]}
                files_added={added}
                files_modified={modified}
                files_deleted={deleted}
                """
            ).strip(),
            artifact_root=exported_artifact_root,
        )

    except Exception as exc:  # noqa: BLE001 — never raise to caller
        wall = time.perf_counter() - started
        return GhpDelegatedResult(
            task_id=task.task_id,
            status="failed",
            reason=f"executor_exception: {type(exc).__name__}: {exc}",
            verdict_block=f"wall={wall:.2f}s",
            artifact_root=None,
        )
    finally:
        # Best-effort cleanup; on Windows tempfiles can be locked briefly.
        shutil.rmtree(sandbox, ignore_errors=True)


__all__ = ["execute_delegated_task"]

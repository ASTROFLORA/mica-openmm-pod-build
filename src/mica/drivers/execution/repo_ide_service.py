"""DD-CS-016AC: Repo read-only IDE primitives branch.

Handles repo_list_files, repo_grep, and repo_read tools.
Scope: MICA repo root. Read-only. No writes, no shell.

Blueprint: tools/r29_runs/_SLICE2_OPERATIONAL_BLINDNESS_BLUEPRINT.md §3
Extracted from _build_loop_executor (Slice-2 bootstrap).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

REPO_IDE_TOOL_NAMES: frozenset = frozenset({
    "repo_list_files",
    "repo_grep",
    "repo_read",
})

# Directories to skip when walking the repo tree.
_NOISY_DIRS: frozenset = frozenset({
    ".git", "__pycache__", ".venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build",
})


def _resolve_repo_root() -> Path:
    """Walk parents of this file until we find src/mica + tools/."""
    this_file = Path(__file__).resolve()
    repo_root = this_file
    for candidate in this_file.parents:
        if (candidate / "src" / "mica").is_dir() and (candidate / "tools").is_dir():
            repo_root = candidate
            break
    return repo_root


def _safe_rel(repo_root: Path, p: str) -> Path:
    """Resolve a user-supplied path relative to repo_root; raise PermissionError if it escapes."""
    target = (repo_root / (p or ".")).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError:
        raise PermissionError(f"path escapes repo root: {p!r}")
    return target


async def run_repo_ide_branch(
    *,
    name: str,
    args: Dict[str, Any],
) -> str:
    """Route repo_list_files / repo_grep / repo_read and return a JSON-encoded result."""
    repo_root = _resolve_repo_root()

    try:
        if name == "repo_list_files":
            base = _safe_rel(repo_root, str(args.get("path") or "."))
            glob_pat = str(args.get("glob") or "**/*")
            cap = int(args.get("max_results") or 200)
            if not base.is_dir():
                return json.dumps(
                    {"status": "error", "tool": name, "error": f"not_a_directory: {base}"},
                    ensure_ascii=False, default=str,
                )
            hits: list = []
            for p in base.rglob(glob_pat):
                if not p.is_file():
                    continue
                parts = set(p.parts)
                if parts & _NOISY_DIRS:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = -1
                hits.append({
                    "path": str(p.relative_to(repo_root)).replace("\\", "/"),
                    "size": size,
                })
                if len(hits) >= cap:
                    break
            return json.dumps(
                {
                    "status": "ok", "tool": name,
                    "repo_root": str(repo_root),
                    "count": len(hits), "capped": len(hits) >= cap,
                    "results": hits,
                },
                ensure_ascii=False, default=str,
            )

        if name == "repo_grep":
            pattern = str(args.get("pattern") or args.get("query") or "")
            if not pattern:
                return json.dumps(
                    {"status": "error", "tool": name, "error": "empty_pattern"},
                    ensure_ascii=False, default=str,
                )
            include_glob = str(args.get("include_glob") or "**/*.py")
            cap = int(args.get("max_results") or 120)
            scope = _safe_rel(repo_root, str(args.get("path") or "."))
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as rex:
                return json.dumps(
                    {"status": "error", "tool": name, "error": f"bad_regex: {rex}"},
                    ensure_ascii=False, default=str,
                )
            hits = []
            iter_base = scope if scope.is_dir() else repo_root
            for p in iter_base.rglob(include_glob):
                if not p.is_file():
                    continue
                parts = set(p.parts)
                if parts & _NOISY_DIRS:
                    continue
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as fh:
                        for idx, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append({
                                    "path": str(p.relative_to(repo_root)).replace("\\", "/"),
                                    "line_no": idx,
                                    "line": line.rstrip("\n")[:400],
                                })
                                if len(hits) >= cap:
                                    break
                except OSError:
                    continue
                if len(hits) >= cap:
                    break
            return json.dumps(
                {
                    "status": "ok", "tool": name,
                    "pattern": pattern, "count": len(hits),
                    "capped": len(hits) >= cap, "results": hits,
                },
                ensure_ascii=False, default=str,
            )

        if name == "repo_read":
            rel = str(args.get("path") or "")
            if not rel:
                return json.dumps(
                    {"status": "error", "tool": name, "error": "missing_path"},
                    ensure_ascii=False, default=str,
                )
            target = _safe_rel(repo_root, rel)
            if not target.is_file():
                return json.dumps(
                    {"status": "error", "tool": name, "error": f"not_a_file: {target}"},
                    ensure_ascii=False, default=str,
                )
            start_line = max(1, int(args.get("start_line") or 1))
            end_line = int(args.get("end_line") or 0)
            if end_line <= 0:
                end_line = start_line + 200
            try:
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as rex:
                return json.dumps(
                    {"status": "error", "tool": name, "error": f"read_failed: {rex}"},
                    ensure_ascii=False, default=str,
                )
            total = len(lines)
            end_line = min(end_line, total)
            # Bound content to protect tokens: 600 lines max.
            if end_line - start_line + 1 > 600:
                end_line = start_line + 599
            slice_lines = lines[start_line - 1:end_line]
            return json.dumps(
                {
                    "status": "ok", "tool": name,
                    "path": rel, "total_lines": total,
                    "start_line": start_line, "end_line": end_line,
                    "content": "\n".join(slice_lines)[:40000],
                },
                ensure_ascii=False, default=str,
            )

        # Should never reach here if called through REPO_IDE_TOOL_NAMES guard.
        return json.dumps(
            {"status": "error", "tool": name, "error": "unknown_repo_ide_tool"},
            ensure_ascii=False, default=str,
        )

    except PermissionError as perm:
        return json.dumps(
            {"status": "error", "tool": name, "error": "permission_denied", "detail": str(perm)},
            ensure_ascii=False, default=str,
        )
    except Exception as repo_exc:
        return json.dumps(
            {"status": "error", "tool": name, "error": str(repo_exc)},
            ensure_ascii=False, default=str,
        )

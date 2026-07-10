"""Role-aware tool executor builder for embodied role execution."""

from __future__ import annotations

import json
from typing import Any, Callable


def build_role_executor(role_ctx: Any, parent_executor: Any) -> Callable:
    """Build a role-aware executor that records role-specific ledger events."""

    async def _role_exec(name: str, call_id: str, args: dict) -> str:
        role_ctx.tool_calls_count += 1
        if name == "cite_finding":
            enriched = dict(args)
            enriched.setdefault("confidence", None)
            enriched.setdefault("source_chain", [])
            enriched.setdefault("reasoning_trace", "")
            enriched.setdefault("acquisition_type", "unknown")
            role_ctx.citations_log.append(enriched)
            role_ctx.pending_ledger_entries.append({
                "type": "cite_finding", "data": enriched,
            })
            return json.dumps({"recorded": True, "name": name, "data": enriched}, ensure_ascii=False)
        if name == "identify_gap":
            gap_entry = dict(args)
            role_ctx.gaps_log.append(gap_entry)
            role_ctx.pending_ledger_entries.append({
                "type": "identify_gap", "data": gap_entry,
            })
            return json.dumps({"recorded": True, "name": name, "data": args}, ensure_ascii=False)
        if name == "flag_issue":
            issue_entry = dict(args)
            role_ctx.review_issues.append(issue_entry)
            role_ctx.pending_ledger_entries.append({
                "type": "flag_issue", "data": issue_entry,
            })
            return json.dumps({"recorded": True, "name": name, "data": args}, ensure_ascii=False)
        if name == "flag_tombstone":
            tombstone_entry = dict(args)
            role_ctx.emitted_tombstones.append(tombstone_entry)
            role_ctx.pending_ledger_entries.append({
                "type": "flag_tombstone", "data": tombstone_entry,
            })
            return json.dumps({"recorded": True, "name": name, "data": args}, ensure_ascii=False)
        return await parent_executor(name, call_id, args)

    return _role_exec
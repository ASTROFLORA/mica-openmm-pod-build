"""Citation verification helpers extracted from AgenticDriver loop executor."""

import json
from typing import Any, Callable, Dict


async def run_verify_citations_branch(
    *,
    name: str,
    args: Dict[str, Any],
    degraded_tool_response_fn: Callable[..., str],
) -> str:
    try:
        from mica.memory.dlm.crossref_verifier import CrossRefVerifier

        text = args.get("text", "")
        explicit = args.get("dois", [])
        identifiers = list(explicit)
        if text:
            identifiers.extend(CrossRefVerifier.extract_identifiers(text))
        identifiers = list(dict.fromkeys(identifiers))
        if not identifiers:
            return json.dumps({"error": "No DOIs or PMIDs found in input"})
        async with CrossRefVerifier() as verifier:
            report = await verifier.verify_batch(identifiers)
        return json.dumps(
            {
                "integrity_score": round(report.integrity_score, 3),
                "total": report.total,
                "verified": report.verified,
                "not_found": report.not_found,
                "retracted": report.retracted,
                "summary": report.summary(),
                "results": [
                    {
                        "id": result.identifier,
                        "status": result.status.value,
                        "doi": result.resolved_doi,
                        "title": result.title,
                        "year": result.year,
                        "journal": result.journal,
                        "retracted": result.is_retracted,
                    }
                    for result in report.results
                ],
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as exc:
        return degraded_tool_response_fn(
            name,
            "Sandbox execution degraded instead of crashing.",
            args_payload=args,
            extra={"detail": str(exc)},
        )

#!/usr/bin/env python3
"""Generate a full-preset, isoform-aware LMP corpus for the SCFLR pilot.

This wrapper reads the 1000-protein pilot manifest, runs the existing LMP batch
scanner in ``full`` preset mode, and lets ``generator_v4`` auto-expand UniProt
alternative-product isoforms.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bsm.scripts.prepare_scflr_pilot_manifest import DEFAULT_CORPUS_ROOT, DEFAULT_MANIFEST_PATH, DEFAULT_REPORT_DIR
from bsm.lmp.scanner import LMPScanner


LOGGER = logging.getLogger(__name__)
DEFAULT_RESULTS_DIR = DEFAULT_CORPUS_ROOT / "_results"
DEFAULT_DATASET_NAME = "scflr_full_isoform_1000"
DEFAULT_LMP_CONFIG_PATH = Path(__file__).resolve().parents[1] / "lmp" / "lmp_config.yaml"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load_manifest_ids(manifest_path: Path, limit: Optional[int] = None) -> List[str]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    ids: List[str] = []
    seen: set[str] = set()
    with manifest_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            uniprot_id = str(record.get("uniprot_id") or record.get("protein_id") or "").strip()
            if not uniprot_id or uniprot_id in seen:
                continue
            seen.add(uniprot_id)
            ids.append(uniprot_id)
            if limit is not None and len(ids) >= limit:
                break

    return ids


def _summarize_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    status_counts = Counter()
    state_counts = Counter()
    file_count = 0
    error_samples: List[Dict[str, Any]] = []

    for result in results:
        if not isinstance(result, dict):
            continue

        status = str(result.get("status", "unknown"))
        status_counts[status] += 1

        states = result.get("states") or []
        if isinstance(states, list):
            state_counts.update(str(state) for state in states if state)

        files = result.get("files") or []
        if isinstance(files, list):
            file_count += len(files)

        if status == "ERROR" and len(error_samples) < 20:
            error_samples.append(
                {
                    "id": result.get("id"),
                    "kind": result.get("kind"),
                    "error": result.get("error"),
                }
            )

    return {
        "status_counts": dict(sorted(status_counts.items())),
        "state_counts": dict(sorted(state_counts.items())),
        "generated_files": file_count,
        "error_samples": error_samples,
    }


def _render_report(summary: Dict[str, Any]) -> str:
    lines = [
        "# SCFLR Full Isoform Corpus Run",
        "",
        f"Generated at: {summary.get('generated_at', '')}",
        f"Preset: {summary.get('preset', '')}",
        f"Isoform mode: {summary.get('isoform_mode', '')}",
        f"Manifest: {summary.get('input_manifest_path', '')}",
        f"Output root: {summary.get('output_root', '')}",
        f"Dataset dir: {summary.get('dataset_dir', '')}",
        f"Dataset manifest: {summary.get('dataset_manifest_path', '')}",
        "",
        "## Counts",
        "",
        f"- Requested IDs: {summary.get('requested_ids', 0)}",
        f"- Successful IDs: {summary.get('successful_ids', 0)}",
        f"- Failed IDs: {summary.get('failed_ids', 0)}",
        f"- Generated XML files: {summary.get('generated_files', 0)}",
        "",
        "## Status Breakdown",
        "",
    ]

    for status, count in summary.get("status_counts", {}).items():
        lines.append(f"- {status}: {count}")

    lines.extend([
        "",
        "## State Breakdown",
        "",
    ])

    for state_name, count in summary.get("state_counts", {}).items():
        lines.append(f"- {state_name}: {count}")

    lines.extend([
        "",
        "## Notes",
        "",
        "- The scanner runs with the LMP v4 `full` preset.",
        "- Isoforms are expanded automatically by generator_v4 when `isoforms=None`.",
        "- TrajectoryIFP blocks are only emitted when per-protein trajectory inputs are provided.",
    ])

    error_samples = summary.get("error_samples", [])
    if error_samples:
        lines.extend([
            "",
            "## Error Samples",
            "",
        ])
        for sample in error_samples:
            lines.append(f"- {sample.get('id', '')}: {sample.get('error', '')}")

    return "\n".join(lines) + "\n"


def run_corpus_generation(
    *,
    manifest_path: Path,
    output_root: Path,
    dataset_name: str,
    preset: str,
    limit: int,
    max_workers: int,
    request_min_interval_s: float,
    dry_run: bool,
) -> Dict[str, Any]:
    target_ids = _load_manifest_ids(manifest_path, limit=limit)
    if not target_ids:
        raise RuntimeError(f"No protein IDs found in manifest: {manifest_path}")

    output_root.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    scanner = LMPScanner(
        config_path=str(DEFAULT_LMP_CONFIG_PATH),
        output_dir=str(output_root),
        preset=preset,
        request_min_interval_s=request_min_interval_s,
    )

    context_tags = {
        "run_type": "scflr_full_isoform_corpus",
        "preset": preset,
        "isoform_mode": "auto",
        "source_manifest": str(manifest_path),
        "requested_limit": limit,
        "requested_ids": len(target_ids),
    }

    results = scanner.build_dataset(
        target_ids=target_ids,
        dataset_name=dataset_name,
        context_tags=context_tags,
        max_workers=max_workers,
        dry_run=dry_run,
    )

    dataset_dir = output_root / dataset_name
    dataset_manifest_path = dataset_dir / "dataset_manifest.jsonl"

    summary: Dict[str, Any] = {
        "generated_at": _utc_now(),
        "preset": preset,
        "isoform_mode": "auto",
        "input_manifest_path": str(manifest_path),
        "output_root": str(output_root),
        "dataset_name": dataset_name,
        "dataset_dir": str(dataset_dir),
        "dataset_manifest_path": str(dataset_manifest_path),
        "requested_limit": limit,
        "requested_ids": len(target_ids),
        "dry_run": dry_run,
        "max_workers": max_workers,
        "request_min_interval_s": request_min_interval_s,
        "target_ids": target_ids,
    }

    if results is None:
        summary.update(
            {
                "successful_ids": 0,
                "failed_ids": 0,
                "generated_files": 0,
                "status_counts": {},
                "state_counts": {},
                "error_samples": [],
            }
        )
    else:
        result_summary = _summarize_results(results)
        successful_ids = sum(1 for item in results if isinstance(item, dict) and item.get("status") == "OK")
        failed_ids = sum(1 for item in results if isinstance(item, dict) and item.get("status") == "ERROR")
        summary.update(
            {
                "successful_ids": successful_ids,
                "failed_ids": failed_ids,
                **result_summary,
            }
        )

    report_path = DEFAULT_REPORT_DIR / f"{dataset_name}_report.md"
    summary_path = DEFAULT_REPORT_DIR / f"{dataset_name}_summary.json"
    summary["report_path"] = str(report_path)
    summary["summary_path"] = str(summary_path)

    _write_json(summary_path, summary)
    _write_text(report_path, _render_report(summary))

    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a full-preset, isoform-aware SCFLR corpus")
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH, help="Input pilot manifest JSONL path")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RESULTS_DIR, help="Root directory for generated LMP outputs")
    parser.add_argument("--dataset-name", type=str, default=DEFAULT_DATASET_NAME, help="Dataset subdirectory name")
    parser.add_argument("--preset", type=str, default="full", help="LMP preset to use")
    parser.add_argument("--limit", type=int, default=1000, help="Maximum number of proteins to generate")
    parser.add_argument("--max-workers", type=int, default=4, help="Maximum number of worker threads")
    parser.add_argument("--request-min-interval-s", type=float, default=0.2, help="Minimum seconds between remote requests per host")
    parser.add_argument("--dry-run", action="store_true", help="Plan the run without generating XML files")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging verbosity")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    summary = run_corpus_generation(
        manifest_path=args.manifest_path,
        output_root=args.output_root,
        dataset_name=args.dataset_name,
        preset=args.preset,
        limit=args.limit,
        max_workers=args.max_workers,
        request_min_interval_s=args.request_min_interval_s,
        dry_run=args.dry_run,
    )

    LOGGER.info(
        "Corpus run prepared: preset=%s ids=%s files=%s status=%s",
        summary.get("preset"),
        summary.get("requested_ids"),
        summary.get("generated_files"),
        summary.get("status_counts", {}),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
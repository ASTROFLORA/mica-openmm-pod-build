#!/usr/bin/env python3
"""Generate and publish the full-only human LMP corpus.

This wrapper keeps the existing generation path intact:

- resolve the human reference proteome from UniProt,
- generate only `full`-preset XMLs through ``LMPScanner``,
- resume locally from the scanner's existing file checkpoints,
- publish newly generated XMLs into the public bucket prefix that production
  catalog/read surfaces already consume.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from google.cloud import storage

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bsm.lmp.scanner import LMPScanner
from bsm.scripts.prepare_scflr_pilot_manifest import DEFAULT_CORPUS_ROOT, DEFAULT_REPORT_DIR
from mica.config.dotenv_loader import seed_env_from_dotenv


LOGGER = logging.getLogger(__name__)
DEFAULT_RESULTS_DIR = DEFAULT_CORPUS_ROOT / "_results"
DEFAULT_DATASET_NAME = "human_proteome_full_isoform_public"
DEFAULT_LMP_CONFIG_PATH = Path(__file__).resolve().parents[1] / "lmp" / "lmp_config.yaml"
DEFAULT_XSD_PATH = Path(__file__).resolve().parents[1] / "lmp" / "lmp_v4_schema.xsd"
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
DEFAULT_QUERY = "proteome:UP000005640 AND reviewed:true"
DEFAULT_PUBLIC_BUCKET = "mica-public-lmp-v4"
DEFAULT_PUBLIC_PREFIX = "scflr_full_isoform_1000"
DEFAULT_UPLOAD_RECEIPTS_NAME = "public_upload_receipts.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _summarize_generation_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    status_counts = Counter()
    state_counts = Counter()
    generated_files = 0
    error_samples: List[Dict[str, Any]] = []

    for result in results:
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "unknown")
        status_counts[status] += 1
        files = result.get("files") or []
        if isinstance(files, list):
            generated_files += len(files)
        states = result.get("states") or []
        if isinstance(states, list):
            state_counts.update(str(state) for state in states if state)
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
        "generated_files": generated_files,
        "error_samples": error_samples,
    }


def _load_success_receipts(path: Path) -> Set[str]:
    if not path.exists():
        return set()

    uploaded: Set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(payload.get("status") or "") != "uploaded":
                continue
            object_path = str(payload.get("object_path") or "").strip()
            if object_path:
                uploaded.add(object_path)
    return uploaded


def _build_schema(xsd_path: Path):
    from lxml import etree  # type: ignore

    schema_doc = etree.parse(str(xsd_path))
    return etree.XMLSchema(schema_doc)


def _validate_xml_file(path: Path, schema) -> None:
    from lxml import etree  # type: ignore

    xml_doc = etree.parse(str(path))
    if schema.validate(xml_doc):
        return
    errors = [str(error) for error in schema.error_log]
    raise ValueError(f"LMP v4 XSD validation failed for {path.name}: {'; '.join(errors[:5])}")


def _public_object_path(prefix: str, filename: str) -> str:
    cleaned_prefix = str(prefix or "").strip().strip("/")
    if cleaned_prefix:
        return f"{cleaned_prefix}/{filename}"
    return filename


def _list_existing_public_objects(client: storage.Client, bucket_name: str, prefix: str) -> Set[str]:
    bucket = client.bucket(bucket_name)
    names: Set[str] = set()
    for blob in client.list_blobs(bucket, prefix=str(prefix or "").strip().strip("/") or None):
        name = str(getattr(blob, "name", "") or "").strip()
        if name:
            names.add(name)
    return names


def publish_dataset_to_public_prefix(
    *,
    dataset_dir: Path,
    receipts_path: Path,
    bucket_name: str,
    prefix: str,
    dry_run: bool,
    validate_xsd: bool,
    xsd_path: Path,
    skip_existing_public: bool,
    upload_limit: Optional[int],
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "bucket": bucket_name,
        "prefix": prefix,
        "receipts_path": str(receipts_path),
        "dataset_dir": str(dataset_dir),
        "dry_run": dry_run,
        "dataset_dir_exists": dataset_dir.is_dir(),
        "status_counts": {},
        "validated_files": 0,
        "error_samples": [],
    }

    if not dataset_dir.is_dir():
        return summary

    xml_paths = sorted(dataset_dir.glob("*.xml"))
    if upload_limit is not None:
        xml_paths = xml_paths[:upload_limit]
    summary["candidate_files"] = len(xml_paths)

    if not xml_paths:
        return summary

    schema = _build_schema(xsd_path) if validate_xsd else None
    uploaded_receipts = _load_success_receipts(receipts_path)
    status_counts = Counter()

    client = storage.Client()
    existing_public = _list_existing_public_objects(client, bucket_name, prefix) if skip_existing_public else set()
    bucket = client.bucket(bucket_name)

    for xml_path in xml_paths:
        object_path = _public_object_path(prefix, xml_path.name)
        receipt: Dict[str, Any] = {
            "timestamp": _utc_now(),
            "filename": xml_path.name,
            "object_path": object_path,
            "size_bytes": xml_path.stat().st_size,
        }
        try:
            if object_path in uploaded_receipts:
                receipt["status"] = "skipped_receipt"
                status_counts["skipped_receipt"] += 1
                _append_jsonl(receipts_path, receipt)
                continue

            if object_path in existing_public:
                receipt["status"] = "skipped_existing_public"
                status_counts["skipped_existing_public"] += 1
                _append_jsonl(receipts_path, receipt)
                continue

            if schema is not None:
                _validate_xml_file(xml_path, schema)
                summary["validated_files"] = int(summary.get("validated_files", 0)) + 1

            if dry_run:
                receipt["status"] = "planned_upload"
                status_counts["planned_upload"] += 1
                _append_jsonl(receipts_path, receipt)
                continue

            blob = bucket.blob(object_path)
            blob.upload_from_filename(str(xml_path), content_type="application/xml")
            receipt["status"] = "uploaded"
            status_counts["uploaded"] += 1
            uploaded_receipts.add(object_path)
            existing_public.add(object_path)
            _append_jsonl(receipts_path, receipt)
        except Exception as exc:
            receipt["status"] = "error"
            receipt["error"] = str(exc)
            status_counts["error"] += 1
            if len(summary["error_samples"]) < 20:
                summary["error_samples"].append({"filename": xml_path.name, "error": str(exc)})
            _append_jsonl(receipts_path, receipt)

    summary["status_counts"] = dict(sorted(status_counts.items()))
    return summary


def _render_report(summary: Dict[str, Any]) -> str:
    lines = [
        "# Human Full Public LMP Corpus Run",
        "",
        f"Generated at: {summary.get('generated_at', '')}",
        f"Query: {summary.get('query', '')}",
        f"Preset: {summary.get('preset', '')}",
        f"Output root: {summary.get('output_root', '')}",
        f"Dataset dir: {summary.get('dataset_dir', '')}",
        f"Dataset manifest: {summary.get('dataset_manifest_path', '')}",
        f"Public bucket: {summary.get('public_bucket', '')}",
        f"Public prefix: {summary.get('public_prefix', '')}",
        f"Validate XSD before upload: {summary.get('validate_xsd', False)}",
        "",
        "## Generation",
        "",
        f"- Requested IDs: {summary.get('requested_ids', 0)}",
        f"- Successful IDs: {summary.get('successful_ids', 0)}",
        f"- Failed IDs: {summary.get('failed_ids', 0)}",
        f"- Generated XML files: {summary.get('generated_files', 0)}",
        "",
        "## Generation Status Breakdown",
        "",
    ]

    for status, count in summary.get("status_counts", {}).items():
        lines.append(f"- {status}: {count}")

    lines.extend([
        "",
        "## Publication",
        "",
        f"- Publish enabled: {summary.get('publish_public', False)}",
        f"- Upload receipts: {summary.get('upload_receipts_path', '')}",
        f"- Candidate files: {summary.get('publication', {}).get('candidate_files', 0)}",
        f"- Validated XML files: {summary.get('publication', {}).get('validated_files', 0)}",
        "",
    ])

    for status, count in summary.get("publication", {}).get("status_counts", {}).items():
        lines.append(f"- {status}: {count}")

    lines.extend([
        "",
        "## Notes",
        "",
        "- This runner preserves the existing `LMPScanner` resume semantics by writing into a stable dataset directory.",
        "- Public publication targets the current production-readable prefix instead of creating a new versioned tree that the live catalog does not scan yet.",
        "- `full` is the only allowed preset in this slice.",
        "- XSD validation is opt-in here because the current generator emits XML that the checked schema still flags as drift, while the live public corpus is consumed directly from bucket objects.",
    ])

    generation_errors = summary.get("error_samples") or []
    publication_errors = summary.get("publication", {}).get("error_samples") or []
    if generation_errors or publication_errors:
        lines.extend(["", "## Error Samples", ""])
        for sample in generation_errors:
            lines.append(f"- generation {sample.get('id', '')}: {sample.get('error', '')}")
        for sample in publication_errors:
            lines.append(f"- publication {sample.get('filename', '')}: {sample.get('error', '')}")

    return "\n".join(lines) + "\n"


def run_human_full_public_corpus(
    *,
    query: str,
    output_root: Path,
    dataset_name: str,
    preset: str,
    limit: int,
    max_workers: int,
    request_min_interval_s: float,
    dry_run: bool,
    publish_public: bool,
    public_bucket: str,
    public_prefix: str,
    validate_xsd: bool,
    upload_limit: Optional[int],
    report_dir: Path,
) -> Dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    scanner = LMPScanner(
        config_path=str(DEFAULT_LMP_CONFIG_PATH),
        output_dir=str(output_root),
        preset=preset,
        request_min_interval_s=request_min_interval_s,
    )

    context_tags = {
        "run_type": "human_full_public_corpus",
        "preset": preset,
        "isoform_mode": "auto",
        "uniprot_query": query,
        "requested_limit": limit,
        "public_bucket": public_bucket,
        "public_prefix": public_prefix,
    }

    results = scanner.build_dataset_from_uniprot_query(
        query=query,
        dataset_name=dataset_name,
        limit=limit,
        context_tags=context_tags,
        max_workers=max_workers,
        dry_run=dry_run,
    )

    dataset_dir = output_root / dataset_name
    dataset_manifest_path = dataset_dir / "dataset_manifest.jsonl"
    upload_receipts_path = dataset_dir / DEFAULT_UPLOAD_RECEIPTS_NAME

    summary: Dict[str, Any] = {
        "generated_at": _utc_now(),
        "query": query,
        "preset": preset,
        "output_root": str(output_root),
        "dataset_name": dataset_name,
        "dataset_dir": str(dataset_dir),
        "dataset_manifest_path": str(dataset_manifest_path),
        "upload_receipts_path": str(upload_receipts_path),
        "requested_limit": limit,
        "requested_ids": 0,
        "dry_run": dry_run,
        "max_workers": max_workers,
        "request_min_interval_s": request_min_interval_s,
        "publish_public": publish_public,
        "public_bucket": public_bucket,
        "public_prefix": public_prefix,
        "validate_xsd": validate_xsd,
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
        result_summary = _summarize_generation_results(results)
        successful_ids = sum(1 for item in results if isinstance(item, dict) and item.get("status") == "OK")
        failed_ids = sum(1 for item in results if isinstance(item, dict) and item.get("status") == "ERROR")
        summary.update(
            {
                "requested_ids": len(results),
                "successful_ids": successful_ids,
                "failed_ids": failed_ids,
                **result_summary,
            }
        )

    publication_summary = {
        "bucket": public_bucket,
        "prefix": public_prefix,
        "status_counts": {},
        "validated_files": 0,
        "candidate_files": 0,
        "error_samples": [],
    }
    if publish_public:
        publication_summary = publish_dataset_to_public_prefix(
            dataset_dir=dataset_dir,
            receipts_path=upload_receipts_path,
            bucket_name=public_bucket,
            prefix=public_prefix,
            dry_run=dry_run,
            validate_xsd=validate_xsd,
            xsd_path=DEFAULT_XSD_PATH,
            skip_existing_public=True,
            upload_limit=upload_limit,
        )
    summary["publication"] = publication_summary

    report_path = report_dir / f"{dataset_name}_report.md"
    summary_path = report_dir / f"{dataset_name}_summary.json"
    summary["report_path"] = str(report_path)
    summary["summary_path"] = str(summary_path)

    _write_json(summary_path, summary)
    _write_text(report_path, _render_report(summary))
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and publish the human full-only LMP corpus")
    parser.add_argument("--env-path", type=Path, default=DEFAULT_ENV_PATH, help="Optional .env file to seed before execution")
    parser.add_argument("--query", type=str, default=DEFAULT_QUERY, help="UniProt query for the corpus run")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RESULTS_DIR, help="Root directory for generated LMP outputs")
    parser.add_argument("--dataset-name", type=str, default=DEFAULT_DATASET_NAME, help="Dataset subdirectory name")
    parser.add_argument("--preset", choices=["full"], default="full", help="Only the full preset is supported in this runner")
    parser.add_argument("--limit", type=int, default=50000, help="Maximum number of UniProt accessions to fetch from the query")
    parser.add_argument("--max-workers", type=int, default=4, help="Maximum number of worker threads")
    parser.add_argument("--request-min-interval-s", type=float, default=0.2, help="Minimum seconds between remote requests per host")
    parser.add_argument("--dry-run", action="store_true", help="Plan the run without writing XMLs or publishing uploads")
    parser.add_argument("--no-publish-public", action="store_true", help="Skip the public bucket publication step")
    parser.add_argument("--public-bucket", type=str, default=DEFAULT_PUBLIC_BUCKET, help="Public GCS bucket for the readable corpus")
    parser.add_argument("--public-prefix", type=str, default=DEFAULT_PUBLIC_PREFIX, help="Public GCS prefix that current catalog/read surfaces resolve")
    parser.add_argument("--upload-limit", type=int, default=None, help="Optional cap for the number of XML files considered for publication")
    parser.add_argument("--validate-xsd", action="store_true", help="Fail publication on per-file XSD validation errors before upload")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="Directory for summary/report outputs")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging verbosity")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.env_path:
        seed_env_from_dotenv(path=args.env_path)

    summary = run_human_full_public_corpus(
        query=args.query,
        output_root=args.output_root,
        dataset_name=args.dataset_name,
        preset=args.preset,
        limit=args.limit,
        max_workers=args.max_workers,
        request_min_interval_s=args.request_min_interval_s,
        dry_run=args.dry_run,
        publish_public=not args.no_publish_public,
        public_bucket=args.public_bucket,
        public_prefix=args.public_prefix,
        validate_xsd=args.validate_xsd,
        upload_limit=args.upload_limit,
        report_dir=args.report_dir,
    )

    LOGGER.info(
        "Human corpus run complete: ids=%s files=%s publication=%s",
        summary.get("requested_ids"),
        summary.get("generated_files"),
        summary.get("publication", {}).get("status_counts", {}),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
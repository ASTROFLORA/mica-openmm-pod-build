"""Phase 1.003 playbook to ingest WNK-OSR1/SPAK pathway kinases into the CEA.

This utility wraps :class:`~bsm.cea.ingestion.CEAPopulationIngestor` with the
curated datasets prepared for the first Canonical Entity Atlas population run.
It can be executed in dry-run mode (default) to verify payloads without writing
into Neo4j, or in commit mode when the BSM infrastructure is reachable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..ingestion import CEAPopulationIngestor
from ..cea_service import CEAService
from ...neo4j_integration import BSMNeo4jIntegration

logger = logging.getLogger(__name__)


@dataclass
class _NoopCEAService:
    """Minimal service stub used during dry-run executions."""

    async def create_entity(self, entity):  # type: ignore[no-untyped-def]
        logger.debug("Dry-run create for %s", entity.budo_id)
        return entity

    async def update_entity(self, entity):  # type: ignore[no-untyped-def]
        logger.debug("Dry-run update for %s", entity.budo_id)
        return entity


def _default_dataset_root() -> Path:
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / "EMBEDDINGORDERINGPLAN" / "TEAM_AI_COLLABORATION" / "AIUNIVERSITY" / "RESEARCH_LABS" / "ALEX_RODRIGUEZ_AI_SYSTEMS_ARCHITECTURE_LAB" / "RESEARCH_LINES" / "BSM-BUDO-CEA-PROGRAM" / "data" / "phase1_003_wnk_pathway"


def _build_parser() -> argparse.ArgumentParser:
    dataset_root = _default_dataset_root()
    parser = argparse.ArgumentParser(description="Phase 1.003 WNK pathway ingestion playbook")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=dataset_root / "protein_catalog.csv",
        help="Path to the curated protein catalog CSV.",
    )
    parser.add_argument(
        "--cross-references",
        dest="cross_references",
        type=Path,
        default=dataset_root / "cross_references.csv",
        help="Path to the curated cross-reference CSV.",
    )
    parser.add_argument(
        "--sequence-embeddings",
        dest="sequence_embeddings",
        type=Path,
        default=None,
        help="Optional sequence embedding export (CSV).",
    )
    parser.add_argument(
        "--network-embeddings",
        dest="network_embeddings",
        type=Path,
        default=None,
        help="Optional network embedding export (CSV).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="If provided, write the population summary JSON to this path.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist the entities into Neo4j (requires configured BSM infrastructure).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return parser


async def _build_service(write: bool) -> tuple[Any, Any]:
    if not write:
        return _NoopCEAService(), None

    neo4j = BSMNeo4jIntegration()
    await neo4j.connect()
    service = CEAService(neo4j)
    return service, neo4j


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    service, neo4j = await _build_service(args.write)
    try:
        ingestor = CEAPopulationIngestor(
            service,
            catalog_path=args.catalog,
            cross_references_path=args.cross_references,
            sequence_embeddings_path=args.sequence_embeddings,
            network_embeddings_path=args.network_embeddings,
            dry_run=not args.write,
            pipeline_tag="cea_initial_population_wnk_pathway",
            species_lookup={"9606": "Homo sapiens"},
        )

        summary = await ingestor.run()
        payload = summary.as_dict()
        logger.info("Processed %s records (created=%s updated=%s dry_run=%s)", payload["total"], payload["created"], payload["updated"], payload["dry_run"])

        if args.report:
            summary_path = Path(args.report)
            summary.write_report(summary_path)
            logger.info("Summary written to %s", summary_path)

        return payload
    finally:
        if neo4j is not None:
            await neo4j.disconnect()


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    if not args.catalog.exists():
        parser.error(f"Catalog not found: {args.catalog}")
    if not args.cross_references.exists():
        parser.error(f"Cross-reference CSV not found: {args.cross_references}")

    payload = asyncio.run(_run(args))
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

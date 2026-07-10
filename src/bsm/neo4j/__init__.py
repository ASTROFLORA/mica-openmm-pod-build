"""Neo4j schema and utilities for the BSM-BUDO-CEA program."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "graph_schema.cypher"
LITERATURE_INGEST_PATH = BASE_DIR / "literature_ingest.cypher"

__all__ = ["SCHEMA_PATH", "LITERATURE_INGEST_PATH"]

"""Utility script to export protein embeddings from Milvus/Zilliz into NPZ files."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
from pymilvus import Collection, connections

from bsm.config import get_bsm_config

logger = logging.getLogger(__name__)


def _connect(alias: str = "default") -> None:
    config = get_bsm_config().milvus
    if config.uri:
        connections.connect(
            alias=alias,
            uri=config.uri,
            token=config.token or None,
            timeout=config.timeout,
        )
    else:
        connections.connect(
            alias=alias,
            host=config.host,
            port=str(config.port),
            timeout=config.timeout,
        )
    logger.info("Connected to Milvus alias=%s", alias)


def _ensure_loaded(collection: Collection) -> None:
    if not collection.has_index():
        logger.warning("Collection %s has no index; queries may be slow", collection.name)
    collection.load()


def _iter_batches(
    collection: Collection,
    *,
    limit: int,
    offset: int,
    batch_size: int,
    expr: Optional[str],
    output_fields: Iterable[str],
    consistency_level: Any,
) -> Iterable[List[Dict]]:
    fetched = 0
    current_offset = offset
    # Milvus requires a boolean filter expression; when none provided, use a tautology on a likely string ID field.
    # Note: callers should pass a concrete expr for deterministic subsets; this fallback returns "all rows" semantics.
    expr_safe = expr if (expr is not None and str(expr).strip() != "") else None
    while fetched < limit:
        fetch_limit = min(batch_size, limit - fetched)
        query_kwargs: Dict = {
            "limit": fetch_limit,
            "offset": current_offset,
            "output_fields": list(output_fields),
            "consistency_level": consistency_level,
        }
        if expr_safe is None:
            # Try to infer a tautology using a common identifier field if present among output_fields
            fields_list = list(output_fields)
            id_like = None
            for cand in ("protein_id", "id", "mudo_id"):
                if cand in fields_list:
                    id_like = cand
                    break
            # Use a simple non-empty string check for string identifiers; for other types this will be ignored by Milvus
            query_kwargs["expr"] = f"{id_like} != ''" if id_like else ""
        else:
            query_kwargs["expr"] = expr_safe
        rows = collection.query(**query_kwargs)
        if not rows:
            break
        yield rows
        fetched += len(rows)
        current_offset += len(rows)
        if len(rows) < fetch_limit:
            break


def export_embeddings(
    *,
    collection_name: str,
    output_path: Path,
    vector_field: str,
    id_field: str,
    limit: int,
    offset: int,
    batch_size: int,
    consistency: str,
    expr: Optional[str],
    extra_fields: Optional[List[str]] = None,
) -> Dict[str, int]:
    _connect()
    collection = Collection(collection_name)
    _ensure_loaded(collection)

    field_names = {field.name for field in collection.schema.fields}
    if vector_field not in field_names:
        raise KeyError(f"Vector field '{vector_field}' not present in collection")
    if id_field not in field_names:
        raise KeyError(f"ID field '{id_field}' not present in collection")

    fields = [id_field, vector_field]
    if extra_fields:
        for field in extra_fields:
            if field not in field_names:
                raise KeyError(f"Extra field '{field}' not present in collection schema")
            fields.append(field)

    # Map provided consistency string to title case for pymilvus >=2.2 which accepts strings
    # Expected values: "Strong", "Bounded", "Session", "Eventually"
    if isinstance(consistency, str):
        cons_level = consistency.strip().title()
    else:
        cons_level = consistency

    embeddings: List[np.ndarray] = []
    ids: List[str] = []
    extras: Dict[str, List] = {field: [] for field in extra_fields or []}

    for batch in _iter_batches(
        collection,
        limit=limit,
        offset=offset,
        batch_size=batch_size,
        expr=expr,
        output_fields=fields,
        consistency_level=cons_level,
    ):
        for row in batch:
            ids.append(str(row[id_field]))
            embeddings.append(np.asarray(row[vector_field], dtype=np.float32))
            for field in extras:
                extras[field].append(row.get(field))

    if not embeddings:
        raise RuntimeError("Query returned no embeddings; check parameters")

    matrix = np.vstack(embeddings)
    data_dict = {"embeddings": matrix.astype(np.float32), "protein_ids": np.array(ids, dtype=object)}
    for field, values in extras.items():
        data_dict[field] = np.array(values)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(output_path, **data_dict)
    logger.info("Exported %d embeddings to %s", matrix.shape[0], output_path)
    return {"exported": matrix.shape[0], "dimensions": matrix.shape[1]}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export embeddings from Milvus/Zilliz to NPZ")
    parser.add_argument("--collection", type=str, required=True, help="Milvus collection name")
    parser.add_argument("--output", type=Path, required=True, help="Destination NPZ path")
    parser.add_argument("--vector-field", type=str, default="embedding", help="Vector field to export")
    parser.add_argument("--id-field", type=str, default="protein_id", help="Primary or ID field name")
    parser.add_argument("--limit", type=int, default=2000, help="Maximum records to export")
    parser.add_argument("--offset", type=int, default=0, help="Offset for sequential export")
    parser.add_argument("--batch-size", type=int, default=128, help="Query chunk size")
    parser.add_argument("--consistency", type=str, default="STRONG", help="Milvus consistency level")
    parser.add_argument("--expr", type=str, default=None, help="Optional boolean expression filter")
    parser.add_argument("--extra-field", action="append", dest="extra_fields", help="Additional fields to persist")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    summary = export_embeddings(
        collection_name=args.collection,
        output_path=args.output,
        vector_field=args.vector_field,
        id_field=args.id_field,
        limit=args.limit,
        offset=args.offset,
        batch_size=args.batch_size,
        consistency=args.consistency,
        expr=args.expr,
        extra_fields=args.extra_fields,
    )
    logger.info("Export summary: %s", summary)


if __name__ == "__main__":
    main()

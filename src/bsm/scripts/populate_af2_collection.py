#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""populate_af2_collection.py — AF2 384D DPEB single-vector ETL pipeline
==========================================================================

Builds ``protein_af2_rag_v1`` — a single-vector Milvus collection keyed on
UniProt accession, with AF2 DPEB 384D aggregated embeddings and rich metadata
joined from the DCT parquet (sequence, GO terms, functional_annotation, etc.).

Sources
-------
* ``D:\\STRING-DATABASE\\DPEB\\eppi_alphafold_aggregated_embeddings.csv``
  → af2_vec (384-D FLOAT_VECTOR), sequence (fasta column)
* DCT parquet (local, filtered to is_global=True rows for one row per protein)
  → uniprot, gene_symbol, preferred_name, sequence, functional_annotation,
     go_biological_process, go_molecular_function, go_cellular_component

Join strategy
-------------
AF2 CSV uses UniProt accessions as ``protein_id`` (e.g. "Q92519").
DCT parquet has a ``uniprot`` column — join AF2.protein_id == DCT.uniprot.
For proteins not found in DCT parquet, sequence comes from the AF2 fasta field
and all metadata VARCHAR fields are left as empty strings.

Usage
-----
    python src/bsm/scripts/populate_af2_collection.py --mode dry-run
    python src/bsm/scripts/populate_af2_collection.py --mode create-only
    python src/bsm/scripts/populate_af2_collection.py --mode populate --max-proteins 100
    python src/bsm/scripts/populate_af2_collection.py --mode populate --force-recreate
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import pandas as pd

    _PANDAS = True
except ImportError:
    _PANDAS = False

try:
    from pymilvus import Collection, connections, utility

    _MILVUS = True
except ImportError:
    _MILVUS = False

# Resolve src/ onto path so ``from bsm...`` works regardless of cwd
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bsm.milvus_integration import BSMMilvusSchema  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("populate_af2")

# ── constants ──────────────────────────────────────────────────────────────────
AF2_COLLECTION_NAME = "protein_af2_rag_v1"
AF2_MODEL_ID = "bsm-af2-protein-v1"
AF2_SCHEMA_VERSION = 1

AF2_CSV_PATHS: List[Path] = [
    Path(r"D:\STRING-DATABASE\DPEB\eppi_alphafold_aggregated_embeddings.csv"),
]
DCT_PARQUET_PATHS: List[Path] = [
    Path(r"C:\Users\busta\Downloads\DCTdomain_human_proteome\milvus_dct_collection.parquet"),
]
STRING_ALIASES_PATHS: List[Path] = [
    Path(r"D:\STRING-DATABASE\9606.protein.aliases.v12.0.txt\9606.protein.aliases.v12.0.txt"),
]

_ZERO_384: List[float] = [0.0] * 384


# ── helpers ────────────────────────────────────────────────────────────────────

def _stable_id(uniprot_id: str) -> str:
    """32-char deterministic primary key: SHA-256(uniprot_id + model_id)."""
    return hashlib.sha256(f"{uniprot_id}:{AF2_MODEL_ID}".encode()).hexdigest()[:32]


def _stringify(val: Any, maxlen: int = 2048) -> str:
    """Convert list or any value to a truncated string for Milvus VARCHAR."""
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        s = "; ".join(str(x) for x in val if x)
    else:
        s = str(val)
    if s == "nan":
        return ""
    return s[:maxlen]


def _load_env() -> Tuple[str, str]:
    """Load ZILLIZ_URI / ZILLIZ_TOKEN from a .env file walking up from script."""
    env_candidates = [
        Path(__file__).resolve().parents[4] / ".env",
        Path(os.getcwd()) / ".env",
    ]
    for ep in env_candidates:
        if ep.exists():
            for line in ep.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in ("ZILLIZ_URI", "ZILLIZ_TOKEN", "MILVUS_URI", "MILVUS_TOKEN"):
                    os.environ.setdefault(k, v)
    uri = os.environ.get("ZILLIZ_URI") or os.environ.get("MILVUS_URI") or ""
    tok = os.environ.get("ZILLIZ_TOKEN") or os.environ.get("MILVUS_TOKEN") or ""
    return uri, tok


def _load_af2_data() -> Tuple[Dict[str, List[float]], Dict[str, str]]:
    """Load AF2 DPEB CSV.

    Returns
    -------
    vectors   : {uniprot_id → [384 floats]}
    sequences : {uniprot_id → raw FASTA sequence string}
    """
    for p in AF2_CSV_PATHS:
        if not p.exists():
            continue
        if not _PANDAS:
            log.error("pandas is required but not installed")
            return {}, {}
        log.info("AF2: loading %s", p)
        df = pd.read_csv(p)
        required = {"protein_id", "fasta", "aggregated_features"}
        missing = required - set(df.columns)
        if missing:
            log.error("AF2 CSV missing columns: %s", missing)
            return {}, {}

        log.info("AF2: parsing %d aggregated_features strings (vectorized) ...", len(df))
        # Fast path: use pandas json parsing (JSON is a subset of Python list literals)
        import json

        vectors: Dict[str, List[float]] = {}
        sequences: Dict[str, str] = {}
        n_bad = 0

        pids = df["protein_id"].astype(str).str.strip().tolist()
        seqs = df["fasta"].astype(str).str.strip().tolist()
        feats = df["aggregated_features"].tolist()

        for pid, seq, raw in zip(pids, seqs, feats):
            if not pid or pid == "nan":
                continue
            try:
                if isinstance(raw, str):
                    # Replace Python-style list repr with JSON-compatible: no change needed for float lists
                    vec = json.loads(raw)
                else:
                    vec = list(raw)
                arr = np.asarray(vec, dtype=np.float32)
                if arr.ndim != 1 or len(arr) != 384:
                    n_bad += 1
                    continue
                # L2-normalize for COSINE metric (fixes score=1 saturation)
                _norm = np.linalg.norm(arr)
                if _norm > 0:
                    arr = arr / _norm
                vectors[pid] = arr.tolist()
                sequences[pid] = seq
            except Exception:
                # Fallback to ast for Python-specific list literals
                try:
                    vec = ast.literal_eval(str(raw))
                    arr = np.asarray(vec, dtype=np.float32)
                    if arr.ndim == 1 and len(arr) == 384:
                        _norm = np.linalg.norm(arr)
                        if _norm > 0:
                            arr = arr / _norm
                        vectors[pid] = arr.tolist()
                        sequences[pid] = seq
                    else:
                        n_bad += 1
                except Exception:
                    n_bad += 1

        log.info("AF2: %d proteins loaded (%d bad / skipped)", len(vectors), n_bad)
        return vectors, sequences

    log.warning("AF2 DPEB source not found at any path; af2_vec will be zero-filled")
    return {}, {}


def _load_dct_metadata() -> Dict[str, Dict]:
    """Build {uniprot_id → dct_row_dict} by joining aliases file + DCT parquet.

    Join path
    ---------
    1. Load ``STRING_ALIASES_PATHS`` → filter to ``source == 'UniProt_AC'``
       → build ``{uniprot_id: ensp_id}`` (1-to-1 mapping)
    2. Load ``DCT_PARQUET_PATHS`` → filter to ``is_global==True``
       → build ``{ensp_id: metadata_row}``
    3. Combine → ``{uniprot_id: metadata_row}``

    Falls back to empty dict if either source file is missing.
    """
    for parquet_path in DCT_PARQUET_PATHS:
        if not parquet_path.exists():
            continue

        aliases_path: Optional[Path] = None
        for ap in STRING_ALIASES_PATHS:
            if ap.exists():
                aliases_path = ap
                break

        if aliases_path is None:
            log.warning("STRING aliases file not found — DCT metadata will be empty")
            return {}

        # ── Step 1: build UniProt → ENSP map ──────────────────────────────────
        log.info("Aliases: loading from %s", aliases_path)
        aliases_df = pd.read_csv(aliases_path, sep="\t", usecols=["#string_protein_id", "alias", "source"])
        uid_df = (
            aliases_df[aliases_df["source"] == "UniProt_AC"][["#string_protein_id", "alias"]]
            .drop_duplicates(subset="alias", keep="first")
        )
        # {uniprot_accession → ENSP_id}
        uniprot_to_ensp: Dict[str, str] = dict(zip(uid_df["alias"].astype(str), uid_df["#string_protein_id"].astype(str)))
        log.info("Aliases: %d UniProt→ENSP mappings", len(uniprot_to_ensp))

        # ── Step 2: load DCT parquet global rows ──────────────────────────────
        log.info("DCT parquet: loading from %s", parquet_path)
        df = pd.read_parquet(parquet_path)
        if "is_global" in df.columns:
            global_df = df[df["is_global"] == True]  # noqa: E712
        else:
            global_df = df
        # Deduplicate by ENSP, keep first global row per protein
        dedup = global_df.drop_duplicates(subset="protein_id", keep="first")
        ensp_to_meta: Dict[str, Dict] = {str(row["protein_id"]): row.to_dict() for _, row in dedup.iterrows()}
        log.info(
            "DCT parquet: %d unique ENSP IDs (from %d global rows out of %d total)",
            len(ensp_to_meta),
            len(global_df),
            len(df),
        )

        # ── Step 3: combine → {uniprot → metadata_row} ───────────────────────
        meta: Dict[str, Dict] = {}
        for uniprot_id, ensp_id in uniprot_to_ensp.items():
            row_dict = ensp_to_meta.get(ensp_id)
            if row_dict is not None:
                meta[uniprot_id] = row_dict

        log.info("DCT metadata final: %d UniProt IDs with full metadata", len(meta))
        return meta

    log.warning("DCT parquet not found at any path — metadata will be minimal (fasta sequence only)")
    return {}


# ── collection lifecycle ───────────────────────────────────────────────────────

def _ensure_af2_collection(force_recreate: bool = False) -> "Collection":
    exists = utility.has_collection(AF2_COLLECTION_NAME)
    if exists and force_recreate:
        log.warning("Dropping %s (--force-recreate)", AF2_COLLECTION_NAME)
        utility.drop_collection(AF2_COLLECTION_NAME)
        exists = False

    if not exists:
        schema = BSMMilvusSchema.get_protein_af2_schema()
        coll = Collection(name=AF2_COLLECTION_NAME, schema=schema)
        log.info("Created collection %s", AF2_COLLECTION_NAME)
        for field_name, idx_params in BSMMilvusSchema.get_af2_index_params():
            coll.create_index(field_name=field_name, index_params=idx_params)
            log.info("  HNSW COSINE index created on field '%s'", field_name)
        coll.load()
        log.info("  Collection loaded and ready")
        return coll

    coll = Collection(AF2_COLLECTION_NAME)
    coll.load()
    log.info("Existing %s (%d entities)", AF2_COLLECTION_NAME, coll.num_entities)
    return coll


# ── main ETL ──────────────────────────────────────────────────────────────────

def run_etl(
    mode: str,
    batch_size: int = 200,
    max_proteins: Optional[int] = None,
    force_recreate: bool = False,
) -> None:
    print("=" * 60)
    print("BSM populate_af2_collection v1")
    print(f"  mode          : {mode}")
    print(f"  batch_size    : {batch_size}")
    print(f"  max_proteins  : {max_proteins or 'ALL'}")
    print(f"  force_recreate: {force_recreate}")
    print("=" * 60)

    # ── Phase 0: load local data ──────────────────────────────────────────────
    print("\nPhase 0: Loading AF2 vectors + DCT metadata ...")
    af2_vecs, af2_seqs = _load_af2_data()
    dct_meta = _load_dct_metadata()

    dct_hits = sum(1 for uid in af2_vecs if uid in dct_meta)
    print(f"  AF2 vectors   : {len(af2_vecs):,}")
    print(f"  AF2 sequences : {len(af2_seqs):,}")
    print(f"  DCT metadata  : {len(dct_meta):,}")
    print(f"  DCT meta hits : {dct_hits:,}/{len(af2_vecs):,} ({100*dct_hits//max(len(af2_vecs),1)}%)")

    if mode == "dry-run":
        print("\nDRY RUN complete — no Milvus writes performed.")
        return

    if not _MILVUS:
        log.error("pymilvus is not installed; run: pip install pymilvus")
        sys.exit(1)

    if not af2_vecs:
        log.error("No AF2 vectors loaded — aborting")
        sys.exit(1)

    uri, token = _load_env()
    if not uri:
        log.error("ZILLIZ_URI / MILVUS_URI not set in environment or .env file")
        sys.exit(1)

    # ── Phase 1: connect ──────────────────────────────────────────────────────
    print(f"\nPhase 1: Connecting to {uri[:60]} ...")
    try:
        connections.connect(alias="default", uri=uri, token=token, timeout=30)
        print("  Connected OK")
    except Exception as exc:
        print(f"  CONNECT FAILED: {exc}")
        raise

    # ── Phase 2: ensure collection ────────────────────────────────────────────
    print(f"\nPhase 2: Ensuring collection '{AF2_COLLECTION_NAME}' ...")
    target_coll = _ensure_af2_collection(force_recreate=force_recreate)
    print(f"  Collection ready: {AF2_COLLECTION_NAME}")

    if mode == "create-only":
        print("\ncreate-only: collection created/verified. Exiting without data load.")
        connections.disconnect("default")
        return

    # ── Phase 3: build protein universe ──────────────────────────────────────
    print("\nPhase 3: Building protein universe ...")
    all_uids = list(af2_vecs.keys())
    if max_proteins:
        all_uids = all_uids[:max_proteins]
    print(f"  Universe: {len(all_uids):,} proteins")

    # ── Phase 4: build rows and insert ───────────────────────────────────────
    print("\nPhase 4: Building rows and inserting ...")
    buffer: List[Dict[str, Any]] = []
    n_inserted = 0
    n_meta_hits = 0
    t0 = time.time()

    for uid in all_uids:
        meta = dct_meta.get(uid, {})
        if meta:
            n_meta_hits += 1

        seq_fallback = af2_seqs.get(uid, "")
        seq = str(meta.get("sequence") or seq_fallback)[:40000]
        seq_len = len(seq)

        row: Dict[str, Any] = {
            "id":                    _stable_id(uid),
            "uniprot_id":            uid[:32],
            "ensp_id":               str(meta.get("protein_id", ""))[:64],
            "gene_symbol":           str(meta.get("gene_symbol", ""))[:64],
            "canonical_name":        str(meta.get("preferred_name", uid))[:256],
            "organism":              "Homo sapiens",
            "sequence":              seq,
            "sequence_length":       seq_len,
            "functional_annotation": _stringify(meta.get("functional_annotation", ""), 4096),
            "go_biological_process": _stringify(meta.get("go_biological_process", ""), 2048),
            "go_molecular_function": _stringify(meta.get("go_molecular_function", ""), 2048),
            "go_cellular_component": _stringify(meta.get("go_cellular_component", ""), 2048),
            "model_id":              AF2_MODEL_ID,
            "schema_version":        AF2_SCHEMA_VERSION,
            "af2_vec":               af2_vecs[uid],
        }
        buffer.append(row)

        if len(buffer) >= batch_size:
            target_coll.insert(buffer)
            n_inserted += len(buffer)
            log.info(
                "  Batch inserted: %d rows  (cumulative: %d / %d)",
                len(buffer),
                n_inserted,
                len(all_uids),
            )
            buffer.clear()

    if buffer:
        target_coll.insert(buffer)
        n_inserted += len(buffer)

    target_coll.flush()
    elapsed = time.time() - t0
    throughput = n_inserted / max(elapsed, 0.001)

    print(f"\n  n_inserted    : {n_inserted:,}")
    print(f"  meta hits     : {n_meta_hits:,}")
    print(f"  elapsed       : {elapsed:.1f}s  ({throughput:.0f} rows/s)")
    log.info(
        "Phase 4 done: %d rows inserted in %.1fs (%.0f rows/s)",
        n_inserted,
        elapsed,
        throughput,
    )

    connections.disconnect("default")
    print("\n=== AF2 ETL complete ===")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Populate protein_af2_rag_v1 Milvus collection with AF2 DPEB 384D vectors",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--mode",
        choices=["dry-run", "create-only", "populate"],
        default="dry-run",
        help="dry-run: validate sources only; create-only: schema+index only; populate: full ETL",
    )
    p.add_argument("--batch-size", type=int, default=200, dest="batch_size", help="Rows per Milvus insert call")
    p.add_argument("--max-proteins", type=int, default=None, dest="max_proteins", help="Limit for testing (default: all)")
    p.add_argument("--force-recreate", action="store_true", dest="force_recreate", help="Drop and recreate collection before insert")
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    run_etl(
        mode=args.mode,
        batch_size=args.batch_size,
        max_proteins=args.max_proteins,
        force_recreate=args.force_recreate,
    )

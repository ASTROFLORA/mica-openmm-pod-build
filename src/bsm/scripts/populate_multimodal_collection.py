#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""populate_multimodal_collection.py — BSM schema-v3 ETL pipeline
=================================================================

Builds the unified ``protein_multimodal_rag_v1`` collection (schema v3,
5 dense + 1 sparse = 6 vector fields) from existing data sources.

ETL sources
-----------
1. ``protein_sequences_embeddings``  → prot_t5_vec (1024D)        | Zilliz
2. ``swissprot_esmc_v2``             → esm2_vec    (1280D)        | Zilliz
3. ``protein_networks_embeddings``   → node2vec_vec (512D)        | Zilliz
4. DCT parquet / NPZ                 → dct_vec     (480D)        | local
5. D:\\STRING-DATABASE\\DPEB\\...    → af2_vec     (384D, DPEB)  | D: drive (zero-fill if absent)
6. function_text from SwissProt row  → bm25_sparse               | inline TF

Identity bridge
---------------
ProtT5 and node2vec collections use STRING ENSP IDs or UniProt accessions.
ESM-C (swissprot_esmc_v2) uses UniProt accessions as primary key.
DCT uses ``9606.ENSP*`` ENSP IDs.

Join strategy:
  * Primary key for the unified collection: UniProt accession (``uniprot_id``)
  * ENSP ↔ UniProt bridge: loaded from DCT parquet ``sid`` column if available,
    or discovered dynamically from shared proteins in ProtT5 + ESM-C rows.

Usage
-----
    python -m bsm.scripts.populate_multimodal_collection \\
        --mode dry-run        # print stats only, no writes
        --mode create-only    # (re)create the collection + indexes, no data
        --mode populate       # full ETL, create + load + flush
        --batch-size 200      # insert batch size (default 200)
        --max-proteins 5000   # cap for dev/testing

Environment
-----------
    ZILLIZ_URI    e.g. https://in03-99a0c9d30ee3d44.serverless.aws-eu-central-1.cloud.zilliz.com
    ZILLIZ_TOKEN  your Zilliz API token (from .env or Railway secret)

The script is idempotent: if the collection already exists with schema v3
it will either skip (--mode populate) or drop+recreate (--force-recreate flag).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Normalize stdout / stderr to UTF-8 on Windows to avoid UnicodeEncodeError
# for non-ASCII characters (arrows, em-dashes, etc.) in log / print messages.
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

# ── optional heavy deps ──────────────────────────────────────────────────────
try:
    import pandas as pd
    _PANDAS = True
except ImportError:
    _PANDAS = False

try:
    from pymilvus import (
        MilvusClient,
        connections,
        Collection,
        utility,
        DataType,
    )
    _MILVUS = True
except ImportError:
    _MILVUS = False

# ── local imports ─────────────────────────────────────────────────────────────
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bsm.milvus_integration import BSMMilvusSchema
from bsm.milvus_writer import (
    COLLECTION_NAME,
    SCHEMA_VERSION,
    MODEL_ID,
    _vec_to_list,
    _ZERO_1024,
    _ZERO_1280,
    _ZERO_512,
    _ZERO_480,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("populate_multimodal")

# ── constants ─────────────────────────────────────────────────────────────────
SRC_PROTT5_COLL   = "protein_sequences_embeddings"   # 1024D ProtT5
SRC_ESM2_COLL     = "swissprot_esmc_v2"              # 1280D ESM-C (mapped as esm2)
SRC_NODE2VEC_COLL = "protein_networks_embeddings"    # 512D  node2vec

DCT_PARQUET_PATHS = [
    Path(r"C:\Users\busta\Downloads\DCTdomain_human_proteome\milvus_dct_collection.parquet"),
    Path("/mnt/user-data/uploads/milvus_dct_collection.parquet"),
]
DCT_NPZ_PATHS = [
    Path(r"C:\Users\busta\Downloads\DCTdomain_human_proteome\human_proteome-dct.npz"),
]
AF2_CSV_PATHS = [
    Path(r"D:\STRING-DATABASE\DPEB\eppi_alphafold_aggregated_embeddings.csv"),
]
STRING_ALIASES_PATHS = [
    Path(r"D:\STRING-DATABASE\9606.protein.aliases.v12.0.txt\9606.protein.aliases.v12.0.txt"),
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# ────────────────────────────────────────────────────────────────────────────
# Helper utilities
# ────────────────────────────────────────────────────────────────────────────

def _stable_id(uniprot_id: str) -> str:
    """Deterministic 32-char Milvus row ID from UniProt accession + MODEL_ID."""
    return hashlib.sha256(f"{uniprot_id}:{MODEL_ID}".encode()).hexdigest()[:32]


def _load_env() -> Tuple[str, str]:
    """Load ZILLIZ_URI and ZILLIZ_TOKEN from environment (or .env file)."""
    # Try dotenv
    env_paths = [
        Path(__file__).resolve().parents[4] / ".env",
        Path(os.getcwd()) / ".env",
    ]
    for ep in env_paths:
        if ep.exists():
            for line in ep.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in ("ZILLIZ_URI", "ZILLIZ_TOKEN", "MILVUS_URI", "MILVUS_TOKEN"):
                    os.environ.setdefault(k, v)

    uri = (
        os.environ.get("ZILLIZ_URI")
        or os.environ.get("MILVUS_URI")
        or ""
    )
    token = (
        os.environ.get("ZILLIZ_TOKEN")
        or os.environ.get("MILVUS_TOKEN")
        or ""
    )
    return uri, token


# ────────────────────────────────────────────────────────────────────────────
# Source loaders
# ────────────────────────────────────────────────────────────────────────────

def _load_dct_vectors() -> Dict[str, List[float]]:
    """Load DCT 480D vectors. Returns {ensp_id → [480 floats]}.

    Priority: pre-aggregated parquet > NPZ average-pool per protein.
    DCT keys are ``9606.ENSP*`` format — returned as-is so the caller
    can join on ENSP IDs.
    """
    # 1. Try parquet (fastest, pre-aggregated)
    for p in DCT_PARQUET_PATHS:
        if p.exists() and _PANDAS:
            log.info("DCT: loading from parquet %s", p)
            try:
                df = pd.read_parquet(p)
            except Exception as e:
                log.warning("DCT parquet unreadable (%s): %s — falling back to NPZ", p.name, e)
                continue
            # Expected columns: ensp_id (or sid), dct_vec (list of 480 floats)
            id_col = next((c for c in ("ensp_id", "sid", "protein_id") if c in df.columns), None)
            vec_col = next((c for c in ("dct_vec", "dct", "vector") if c in df.columns), None)
            if id_col and vec_col:
                result: Dict[str, List[float]] = {}
                for _, row in df.iterrows():
                    eid = str(row[id_col])
                    vec = row[vec_col]
                    if isinstance(vec, (list, np.ndarray)):
                        arr = np.asarray(vec, dtype=np.float32)
                        if len(arr) == 480:
                            result[eid] = arr.tolist()
                log.info("DCT parquet: %d proteins loaded", len(result))
                return result

    # 2. Try NPZ — average-pool per-domain vectors using CSR-style idx pointer
    for p in DCT_NPZ_PATHS:
        if p.exists():
            log.info("DCT: loading from NPZ %s", p)
            data = np.load(str(p), allow_pickle=True)
            sids  = data["sid"]   # shape (N_proteins,) string ENSP IDs
            idxs  = data["idx"]   # shape (N_proteins+1,) CSR pointer: protein i → dct[idx[i]:idx[i+1]]
            dcts  = data["dct"]   # shape (N_domains, 480) int8
            dcts_f = dcts.astype(np.float32) / 128.0   # normalise int8 [-128,127] → [-1,1]

            n_proteins = len(sids)
            result: Dict[str, List[float]] = {}
            for i in range(n_proteins):
                start = int(idxs[i])
                end   = int(idxs[i + 1])
                if end <= start:
                    continue
                domains = dcts_f[start:end]      # shape (n_domains, 480)
                agg = domains.mean(axis=0)        # avg-pool across domains
                result[str(sids[i])] = agg.tolist()
            log.info("DCT NPZ: %d proteins loaded", len(result))
            return result

    log.warning("DCT: no source found — dct_vec will be zero-filled everywhere")
    return {}


def _load_af2_vectors() -> Dict[str, List[float]]:
    """Load AF2 DPEB 384D vectors. Returns {string_id → [384 floats]}.

    Returns empty dict (zero-fill fallback) if D: drive is not accessible.
    """
    for p in AF2_CSV_PATHS:
        if p.exists() and _PANDAS:
            log.info("AF2: loading from %s", p)
            df = pd.read_csv(p, index_col=0)
            # Drop non-numeric columns (e.g. sequence strings) before iterating
            df = df.select_dtypes(include=[np.number])
            id_col = df.index.name or "protein_id"
            result: Dict[str, List[float]] = {}
            for pid, row in df.iterrows():
                try:
                    vec = row.values.astype(np.float32)
                except (ValueError, TypeError):
                    continue
                if len(vec) == 384:
                    result[str(pid)] = vec.tolist()
            log.info("AF2 DPEB: %d proteins loaded", len(result))
            return result

    log.warning("AF2 DPEB: D: drive not accessible — af2_vec will be zero-filled")
    return {}


def _fetch_ensp_uniprot_via_api(ensp_ids: List[str]) -> Dict[str, str]:
    """Map STRING ENSP IDs to UniProt accessions via the UniProt REST ID Mapping API.

    Processes in batches of 10 000 IDs per submission.  Each batch takes ~30-60 s.
    Requires the ``requests`` library (available because pymilvus depends on it).
    """
    try:
        import requests as _req
    except ImportError:
        log.warning("requests not installed -- cannot use UniProt API fallback")
        return {}

    BASE = "https://rest.uniprot.org/idmapping"
    BATCH = 10_000
    mapping: Dict[str, str] = {}

    for i in range(0, len(ensp_ids), BATCH):
        batch = ensp_ids[i : i + BATCH]
        log.info(
            "UniProt API: submitting batch %d-%d (%d IDs)...",
            i,
            i + len(batch),
            len(batch),
        )
        try:
            resp = _req.post(
                f"{BASE}/run",
                data={"ids": ",".join(batch), "from": "STRING", "to": "UniProtKB"},
                timeout=60,
            )
            resp.raise_for_status()
            job_id = resp.json()["jobId"]
        except Exception as exc:
            log.warning("UniProt API submit failed: %s", exc)
            continue

        # Poll until FINISHED (max ~10 min = 120 x 5 s)
        for _ in range(120):
            time.sleep(5)
            try:
                status_json = _req.get(f"{BASE}/status/{job_id}", timeout=30).json()
                js = status_json.get("jobStatus", "FINISHED")
                if js in (None, "FINISHED"):
                    break
                if js == "FAILURE":
                    log.warning("UniProt API job %s FAILED", job_id)
                    break
            except Exception:
                pass

        # Fetch ALL results via the /uniprotkb/results/stream endpoint (avoids pagination)
        try:
            r = _req.get(
                f"{BASE}/uniprotkb/results/stream/{job_id}",
                params={"format": "tsv"},
                timeout=300,
            )
            r.raise_for_status()
            for line in r.text.strip().split("\n")[1:]:   # skip "From\tTo" header
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0] and parts[1]:
                    mapping[parts[0]] = parts[1]          # STRING ID -> UniProt AC
        except Exception as exc:
            log.warning("UniProt API results failed: %s", exc)

    log.info("UniProt API: %d ENSP->UniProt mappings fetched", len(mapping))
    return mapping


def _load_ensp_to_uniprot_map() -> Dict[str, str]:
    """Build {ensp_id -> uniprot_id} from the STRING aliases file.

    Reads ``STRING_ALIASES_PATHS`` and extracts rows where ``source == 'UniProt_AC'``.
    Falls back to the UniProt REST ID Mapping API when the aliases file is absent.

    Example:
        "9606.ENSP00000000233" -> "P31946"
    """
    for ap in STRING_ALIASES_PATHS:
        if ap.exists() and _PANDAS:
            log.info("Aliases: loading ENSP->UniProt from %s", ap)
            aliases_df = pd.read_csv(
                ap,
                sep="\t",
                usecols=["#string_protein_id", "alias", "source"],
            )
            uid_df = (
                aliases_df[aliases_df["source"] == "UniProt_AC"][["#string_protein_id", "alias"]]
                .drop_duplicates(subset="#string_protein_id", keep="first")
            )
            result: Dict[str, str] = dict(
                zip(uid_df["#string_protein_id"].astype(str), uid_df["alias"].astype(str))
            )
            log.info("Aliases: %d ENSP->UniProt mappings loaded", len(result))
            return result

    # Fallback: resolve ENSP->UniProt via the UniProt REST ID Mapping API.
    log.warning("STRING aliases file not found -- trying UniProt REST API fallback")
    ensp_ids: List[str] = []
    for np_path in DCT_NPZ_PATHS:
        if np_path.exists():
            try:
                npz = np.load(str(np_path), allow_pickle=True)
                ensp_ids = [str(s) for s in npz["sid"]]
                log.info("Loaded %d ENSP IDs from NPZ for API mapping", len(ensp_ids))
            except Exception as exc:
                log.warning("Could not load NPZ for ENSP IDs: %s", exc)
            break

    if ensp_ids:
        return _fetch_ensp_uniprot_via_api(ensp_ids)

    log.warning("No ENSP IDs available -- ESM-C will be zero-filled")
    return {}


def _fetch_collection_rows(
    collection_name: str,
    output_fields: List[str],
    max_rows: Optional[int],
    batch_size: int = 400,
    expr: str = "id > 0",
) -> List[Dict[str, Any]]:
    """Iterate a Milvus collection using QueryIterator; avoids Zilliz Serverless
    ``offset+limit ≤ 16384`` hard limit for collections > 16K entities.

    Uses ``Collection.query_iterator(batch_size=400)`` which is safe for 1280D
    vectors: 400 × 1280 × 4 B = 2 MB < gRPC 4 MB limit.

    ``expr`` must be a valid filter string for the collection's PK type:
      - INT64 PK  (e.g. swissprot_esmc_v2):   ``id > 0``
      - VARCHAR PK (e.g. *_embeddings):        ``protein_id != ''``
    """
    if not _MILVUS:
        raise RuntimeError("pymilvus not installed")

    coll = Collection(collection_name)
    coll.load()
    total = coll.num_entities
    print(f"  {collection_name}: {total} entities total")

    results: List[Dict[str, Any]] = []

    iter_batch = min(batch_size, max_rows or batch_size)
    iterator = coll.query_iterator(
        batch_size=iter_batch,
        output_fields=output_fields,
        expr=expr,
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            results.extend(batch)
            if max_rows and len(results) >= max_rows:
                results = results[:max_rows]
                break
    finally:
        iterator.close()

    print(f"  {collection_name}: fetched {len(results)} rows")
    return results


def _fetch_esm2_by_ensembl_ids(
    collection_name: str,
    target_ensembl_ids: Set[str],
    output_fields: List[str],
    chunk_size: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch ESM-C rows only for specific ensembl_ids using chunked IN queries.

    Memory-efficient alternative to fetching all 573K rows: only transfers
    the ~20K rows that match our target proteins.

    chunk_size <= 200 keeps each query well within Zilliz Serverless limits.
    """
    if not _MILVUS:
        raise RuntimeError("pymilvus not installed")

    coll = Collection(collection_name)
    coll.load()
    total = coll.num_entities
    print(f"  {collection_name}: {total} entities total (targeted for {len(target_ensembl_ids)} IDs)")

    id_list = sorted(target_ensembl_ids)
    results: List[Dict[str, Any]] = []
    n_chunks = (len(id_list) + chunk_size - 1) // chunk_size

    for idx, start in enumerate(range(0, len(id_list), chunk_size)):
        chunk = id_list[start:start + chunk_size]
        quoted = ", ".join(f'"{i}"' for i in chunk)
        expr = f"ensembl_id in [{quoted}]"
        try:
            rows = coll.query(
                expr=expr,
                output_fields=output_fields,
                limit=chunk_size + 50,
            )
            results.extend(rows)
        except Exception as exc:
            log.warning("  ESM-C chunk %d/%d query failed: %s", idx + 1, n_chunks, exc)

    print(f"  {collection_name}: fetched {len(results)} matching rows")
    return results


def _fetch_esm2_by_uniprot_ids(
    collection_name: str,
    target_uniprot_ids: Set[str],
    output_fields: List[str],
    chunk_size: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch ESM-C rows by UniProt accession using chunked ``uniprot_id in [...]`` queries.

    ``swissprot_esmc_v2`` is UniProt-primary — the ``ensembl_id`` field is sparsely
    populated.  This function is the correct path for human proteome lookups
    where we have ENSP→UniProt mapping from the STRING aliases file.
    """
    if not _MILVUS:
        raise RuntimeError("pymilvus not installed")

    coll = Collection(collection_name)
    coll.load()
    total = coll.num_entities
    print(f"  {collection_name}: {total} entities total (targeted {len(target_uniprot_ids)} UniProt IDs)")

    id_list = sorted(target_uniprot_ids)
    results: List[Dict[str, Any]] = []
    n_chunks = (len(id_list) + chunk_size - 1) // chunk_size

    for idx, start in enumerate(range(0, len(id_list), chunk_size)):
        chunk = id_list[start:start + chunk_size]
        quoted = ", ".join(f'"{i}"' for i in chunk)
        expr = f"uniprot_id in [{quoted}]"
        try:
            rows = coll.query(
                expr=expr,
                output_fields=output_fields,
                limit=chunk_size + 50,
            )
            results.extend(rows)
        except Exception as exc:
            log.warning("  ESM-C uniprot chunk %d/%d query failed: %s", idx + 1, n_chunks, exc)

    print(f"  {collection_name}: fetched {len(results)} matching rows")
    return results


# ────────────────────────────────────────────────────────────────────────────
# Collection management
# ────────────────────────────────────────────────────────────────────────────

def _ensure_collection(force_recreate: bool = False) -> "Collection":
    """Create ``protein_multimodal_rag_v1`` with schema v3 if it does not exist.

    If ``force_recreate`` is True, drop existing and recreate.
    """
    if not _MILVUS:
        raise RuntimeError("pymilvus not installed; run: pip install pymilvus")

    exists = utility.has_collection(COLLECTION_NAME)

    if exists and force_recreate:
        log.warning("Dropping existing collection %s (--force-recreate)", COLLECTION_NAME)
        utility.drop_collection(COLLECTION_NAME)
        exists = False

    if not exists:
        schema = BSMMilvusSchema.get_protein_multimodal_schema()
        coll = Collection(name=COLLECTION_NAME, schema=schema)
        log.info("Created collection %s", COLLECTION_NAME)

        for field_name, idx_params in BSMMilvusSchema.get_multimodal_index_params():
            coll.create_index(field_name=field_name, index_params=idx_params)
            log.info("  Index created on %s", field_name)

        coll.load()
        log.info("Collection loaded (ready for search)")
        return coll
    else:
        coll = Collection(COLLECTION_NAME)
        coll.load()
        log.info("Using existing collection %s (%d entities)", COLLECTION_NAME, coll.num_entities)
        return coll


# ────────────────────────────────────────────────────────────────────────────
# Main ETL pipeline
# ────────────────────────────────────────────────────────────────────────────

def run_etl(
    mode: str,
    batch_size: int = 200,
    max_proteins: Optional[int] = None,
    force_recreate: bool = False,
) -> None:
    """Entry point for the ETL pipeline.

    Args:
        mode: 'dry-run' | 'create-only' | 'populate'
        batch_size: Milvus insert batch size.
        max_proteins: Cap on number of proteins to process (for dev/testing).
        force_recreate: Drop and recreate the collection before populating.
    """
    if not _MILVUS and mode != "dry-run":
        log.error("pymilvus is required for modes other than dry-run. pip install pymilvus")
        sys.exit(1)

    uri, token = _load_env()
    if not uri and mode != "dry-run":
        log.error("ZILLIZ_URI / MILVUS_URI not set. Cannot connect.")
        sys.exit(1)

    print("=== BSM populate_multimodal_collection v3 ===")
    print(f"Mode: {mode}  |  batch_size: {batch_size}  |  max_proteins: {max_proteins}")

    # ── Phase 0: load local sources ──────────────────────────────────────────
    print("Phase 0: Loading local DCT data ...")
    dct_map = _load_dct_vectors()
    print(f"  DCT vectors: {len(dct_map)}")
    ensp_to_uniprot = _load_ensp_to_uniprot_map()
    print(f"  ENSP→UniProt mappings: {len(ensp_to_uniprot)}")

    if mode == "dry-run":
        print("DRY RUN — reporting data availability only. No Milvus writes.")
        print(f"  DCT source      : {len(dct_map)} proteins")
        print(f"  ENSP→UniProt    : {len(ensp_to_uniprot)} mappings")
        print(f"  SCHEMA_VERSION  : {SCHEMA_VERSION}")
        print(f"  MODEL_ID        : {MODEL_ID}")
        print(f"  Collection name : {COLLECTION_NAME}")
        return

    # ── Phase 1: connect ──────────────────────────────────────────────────────
    print(f"Phase 1: Connecting to {uri[:60]} ...")
    try:
        connections.connect(
            alias="default",
            uri=uri,
            token=token,
            timeout=30,
        )
        print("  Connected OK")
    except Exception as exc:
        print(f"  CONNECT FAILED: {exc}")
        raise

    # ── Phase 2: ensure target collection ────────────────────────────────────
    print(f"Phase 2: Ensuring collection {COLLECTION_NAME} ...")
    target_coll = _ensure_collection(force_recreate=force_recreate)
    print(f"  Collection ready: {COLLECTION_NAME}")

    if mode == "create-only":
        print("create-only mode: collection ready. Exiting without data load.")
        connections.disconnect("default")
        return

    # ── Phase 3: fetch source vectors ────────────────────────────────────────
    print("Phase 3: Fetching source vectors from Milvus collections ...")

    # ProtT5 — 1024D  (protein_id=ENSP VARCHAR PK, vector field="embedding")
    print(f"  Fetching ProtT5 from {SRC_PROTT5_COLL} ...")
    try:
        prott5_rows = _fetch_collection_rows(
            SRC_PROTT5_COLL,
            output_fields=["protein_id", "embedding"],
            max_rows=max_proteins,
            expr="protein_id != ''",
        )
        # Key by ENSP protein_id  (e.g. "9606.ENSP00000000233")
        prott5_by_id: Dict[str, Dict] = {r["protein_id"]: r for r in prott5_rows}
    except Exception as exc:
        log.warning("  ProtT5 fetch failed: %s — will zero-fill prot_t5_vec", exc)
        prott5_by_id = {}

    # ESM-C — 1280D  (id=INT64 PK, uniprot_id primary key, ensembl_id sparsely populated)
    # swissprot_esmc_v2 is UniProt-primary.  Use the STRING aliases map (ENSP→UniProt)
    # to translate DCT ENSP IDs to UniProt accessions, then query by uniprot_id.
    # This avoids both the 2.94 GB MemoryError (fetching 573K rows) and the 0-row
    # issue from querying the empty ensembl_id field.
    _esm2_uniprot_ids: Set[str] = set()
    for _ensp in dct_map.keys():
        uid = ensp_to_uniprot.get(_ensp)
        if uid:
            _esm2_uniprot_ids.add(uid)
    # swissprot_esmc_v2 stores uniprot_id as "sp|P84085|ARF5_HUMAN" (FASTA header
    # format), NOT as a bare accession.  Query by suffix "%_HUMAN" to fetch all
    # human Swiss-Prot entries in one iterator pass (~20 k rows), then key the
    # result dict by the parsed bare accession (parts[1]).
    print(f"  Fetching ESM-C from {SRC_ESM2_COLL} (targeted: {len(_esm2_uniprot_ids)} UniProt IDs) ...")
    try:
        esm2_rows = _fetch_collection_rows(
            SRC_ESM2_COLL,
            output_fields=["id", "uniprot_id", "ensembl_id", "organism", "description", "embedding"],
            max_rows=None,
            expr='uniprot_id like "%_HUMAN"',
        )
        esm2_by_uniprot: Dict[str, Dict] = {}
        for r in esm2_rows:
            raw_uid = r.get("uniprot_id", "")
            if not raw_uid:
                continue
            uid_parts = raw_uid.split("|")
            acc = uid_parts[1] if len(uid_parts) >= 3 else raw_uid  # "P84085" from "sp|P84085|ARF5_HUMAN"
            if acc in _esm2_uniprot_ids:
                esm2_by_uniprot[acc] = r
        print(f"  {SRC_ESM2_COLL}: matched {len(esm2_by_uniprot)} rows after accession filter")
    except Exception as exc:
        log.warning("  ESM-C fetch failed: %s — will zero-fill esm2_vec", exc)
        esm2_by_uniprot = {}

    # node2vec — 512D  (protein_id=ENSP VARCHAR PK, vector field="embedding")
    print(f"  Fetching node2vec from {SRC_NODE2VEC_COLL} ...")
    try:
        node2vec_rows = _fetch_collection_rows(
            SRC_NODE2VEC_COLL,
            output_fields=["protein_id", "embedding"],
            max_rows=max_proteins,
            expr="protein_id != ''",
        )
        # Key by ENSP protein_id
        node2vec_by_id: Dict[str, Dict] = {r["protein_id"]: r for r in node2vec_rows}
    except Exception as exc:
        log.warning("  node2vec fetch failed: %s — will zero-fill node2vec_vec", exc)
        node2vec_by_id = {}

    # ── Phase 4: determine universe of proteins ───────────────────────────────
    # Driver = DCT map (19697 ENSP IDs). ProtT5 and node2vec share the same
    # ENSP ID space so hits will be high. ESM-C is keyed by ensembl_id.
    print("Phase 4: Computing protein universe ...")
    all_ids: set = set(dct_map.keys())
    # Extend with ProtT5 IDs that have no DCT entry (rare, keeps coverage)
    all_ids |= set(prott5_by_id.keys())
    if max_proteins:
        all_ids = set(list(all_ids)[:max_proteins])
    print(f"  Universe: {len(all_ids)} unique proteins")

    # ── Phase 5: build and insert rows ───────────────────────────────────────
    log.info("Phase 5: Building rows and inserting to %s ...", COLLECTION_NAME)

    buffer: List[Dict[str, Any]] = []
    n_inserted = 0
    t0 = time.time()

    for protein_id in all_ids:
        ensp_full = str(protein_id)   # e.g. "9606.ENSP00000000233"
        ensp_bare = ensp_full.split(".", 1)[-1]  # strip species prefix

        # --- ProtT5 (keyed by full ENSP ID) ---
        pt5_row = prott5_by_id.get(ensp_full, {})
        prot_t5_vec = _vec_to_list(
            np.asarray(pt5_row["embedding"], dtype=np.float32) if "embedding" in pt5_row else None,
            _ZERO_1024,
        )

        # --- ESM-C (keyed by uniprot_id — bridged via STRING aliases) ---
        _uid_for_esm = ensp_to_uniprot.get(ensp_full, "")
        esm2_row = esm2_by_uniprot.get(_uid_for_esm, {}) if _uid_for_esm else {}
        esm2_raw = esm2_row.get("embedding")
        esm2_vec = _vec_to_list(
            np.asarray(esm2_raw, dtype=np.float32) if esm2_raw is not None else None,
            _ZERO_1280,
        )

        n2v_row = node2vec_by_id.get(ensp_full, {})
        node2vec_vec = _vec_to_list(
            np.asarray(n2v_row["embedding"], dtype=np.float32) if "embedding" in n2v_row else None,
            _ZERO_512,
        )

        # Metadata best-effort (ESM-C has organism/description; ProtT5 has less)
        meta_src = esm2_row or pt5_row
        _raw_esm_uid = esm2_row.get("uniprot_id", "")
        _esm_uid_parts = _raw_esm_uid.split("|") if _raw_esm_uid else []
        uniprot_id = str(
            _esm_uid_parts[1] if len(_esm_uid_parts) >= 3
            else (_raw_esm_uid or _uid_for_esm or ensp_bare)
        )[:32]
        canonical_name = str(meta_src.get("protein_name") or meta_src.get("canonical_name") or ensp_full)[:256]
        organism = str(meta_src.get("organism", "Homo sapiens"))[:256]
        func_text = str(meta_src.get("description") or meta_src.get("function_text", ""))[:4096]
        ensp_id = ensp_full[:64]

        # DCT: check by protein_id or by ENSP variant
        dct_raw = dct_map.get(protein_id) or dct_map.get(ensp_id)
        dct_vec = _vec_to_list(
            np.asarray(dct_raw, dtype=np.float32) if dct_raw is not None else None,
            _ZERO_480,
        )

        row_id = _stable_id(uniprot_id)

        buffer.append({
            "id": row_id,
            "budo_id": f"budo:{uniprot_id.upper()}-D",
            "uniprot_id": uniprot_id,
            "canonical_name": canonical_name,
            "organism": organism,
            "function_text": func_text,
            "ensp_id": ensp_id[:64],
            "schema_version": SCHEMA_VERSION,
            "model_id": MODEL_ID,
            "prot_t5_vec": prot_t5_vec,
            "esm2_vec": esm2_vec,
            "node2vec_vec": node2vec_vec,
            "dct_vec": dct_vec,
        })

        if len(buffer) >= batch_size:
            target_coll.insert(buffer)
            n_inserted += len(buffer)
            log.info("  Inserted %d (total %d)", len(buffer), n_inserted)
            buffer.clear()

    # flush remaining
    if buffer:
        target_coll.insert(buffer)
        n_inserted += len(buffer)

    target_coll.flush()
    elapsed = time.time() - t0
    log.info("Phase 5 done: %d rows inserted in %.1fs  (%.0f rows/s)",
             n_inserted, elapsed, n_inserted / max(elapsed, 0.001))

    connections.disconnect("default")
    log.info("=== ETL complete ===")


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Populate protein_multimodal_rag_v1 (schema v3) from existing Milvus sources + DCT parquet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--mode",
        choices=["dry-run", "create-only", "populate"],
        default="dry-run",
        help="Operation mode (default: dry-run — only prints stats)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=200,
        metavar="N",
        help="Milvus insert batch size (default 200)",
    )
    p.add_argument(
        "--max-proteins",
        type=int,
        default=None,
        metavar="N",
        help="Cap on proteins to process; useful for development/testing",
    )
    p.add_argument(
        "--force-recreate",
        action="store_true",
        help="Drop and recreate the collection before populating (irreversible!)",
    )
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    run_etl(
        mode=args.mode,
        batch_size=args.batch_size,
        max_proteins=args.max_proteins,
        force_recreate=args.force_recreate,
    )

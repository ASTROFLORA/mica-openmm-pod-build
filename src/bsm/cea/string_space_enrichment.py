"""Utilities to enrich SPACE collections in Zilliz with STRING-derived metadata."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import h5py
import numpy as np
from pymilvus import Collection

from .milvus_utils import (
    collection_field_names,
    connect_default,
    disconnect_default,
    get_collection,
    get_field_attr,
    prune_to_schema,
    safe_upsert,
    supports_dynamic_field,
)

logger = logging.getLogger(__name__)


class StringMetadataLoader:
    """Loads metadata artifacts produced by ``process_string_database``."""

    def __init__(self, metadata_path: Path, neighbor_path: Optional[Path] = None) -> None:
        self.metadata_path = Path(metadata_path)
        self.neighbor_path = Path(neighbor_path) if neighbor_path else None

    def load_metadata(self) -> Dict[str, Dict[str, Any]]:
        if not self.metadata_path.exists():
            raise FileNotFoundError(f"Metadata file not found: {self.metadata_path}")

        raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        return {str(key): value for key, value in raw.items()}

    def load_neighbors(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.neighbor_path or not self.neighbor_path.exists():
            return {}

        raw = json.loads(self.neighbor_path.read_text(encoding="utf-8"))
        return {str(key): value for key, value in raw.items()}


class H5EmbeddingSource:
    """Random access utility for SPACE embedding H5 archives."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"Embedding file not found: {self.file_path}")

        with h5py.File(self.file_path, "r") as handle:
            proteins = handle["proteins"][:]
            self.proteins: List[str] = [protein.decode("utf-8") for protein in proteins]
            self.embeddings = handle["embeddings"][:]

        self.index = {protein: idx for idx, protein in enumerate(self.proteins)}
        logger.info("Embeddings loaded: %s (%s vectors)", self.file_path.name, len(self.proteins))

    def vector_for(self, protein_id: str) -> Optional[np.ndarray]:
        idx = self.index.get(protein_id)
        if idx is None:
            return None
        return self.embeddings[idx]

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.proteins)


@dataclass
class EnrichmentSummary:
    collection: str
    processed: int
    missing_metadata: int
    missing_vectors: int
    failed: int


class ZillizSpaceEnricher:
    """Handles metadata upserts into SPACE collections hosted in Zilliz Cloud."""

    def __init__(
        self,
        *,
        metadata: Dict[str, Dict[str, Any]],
        neighbor_context: Dict[str, List[Dict[str, Any]]],
        sequence_source: Optional[H5EmbeddingSource] = None,
        network_source: Optional[H5EmbeddingSource] = None,
        batch_size: int = 256,
        species_id: str = "9606",
        alias: str = "default",
    ) -> None:
        self.metadata = metadata
        self.neighbor_context = neighbor_context
        self.sequence_source = sequence_source
        self.network_source = network_source
        self.batch_size = batch_size
        self.species_id = species_id
        self._connected = False
        self._config = None
        self._alias = alias

    def connect(self) -> None:
        if self._connected:
            return

        config = connect_default(alias=self._alias)
        self._config = config

        self._connected = True
        logger.info("Conexión a Zilliz/Milvus establecida")

    def close(self) -> None:
        if not self._connected:
            return
        try:
            disconnect_default(alias=self._alias)
        finally:
            self._connected = False

    def enrich_sequence(self, collection_name: str = "protein_sequences_embeddings") -> EnrichmentSummary:
        if not self._connected:
            self.connect()
        if (
            self._config
            and getattr(self._config.milvus, "sequence_collection", None)
            and collection_name == "protein_sequences_embeddings"
        ):
            collection_name = getattr(self._config.milvus, "sequence_collection", collection_name)
        if not self.sequence_source:
            raise ValueError("Sequence embedding source no disponible")
        collection = self._resolve_collection(collection_name)
        return self._upsert_collection(
            collection=collection,
            embedding_source=self.sequence_source,
            embedding_type="sequence",
            source_label="STRING v12.0 SPACE sequence",
        )

    def enrich_network(self, collection_name: str = "protein_networks_embeddings") -> EnrichmentSummary:
        if not self._connected:
            self.connect()
        if (
            self._config
            and getattr(self._config.milvus, "network_collection", None)
            and collection_name == "protein_networks_embeddings"
        ):
            collection_name = getattr(self._config.milvus, "network_collection", collection_name)
        if not self.network_source:
            raise ValueError("Network embedding source no disponible")
        collection = self._resolve_collection(collection_name)
        return self._upsert_collection(
            collection=collection,
            embedding_source=self.network_source,
            embedding_type="network",
            source_label="STRING v12.0 SPACE network",
        )

    def _resolve_collection(self, name: str) -> Collection:
        collection = get_collection(name, alias=self._alias)
        if supports_dynamic_field(collection):
            logger.info("Dynamic field activo para %s", name)
        else:
            logger.warning("Dynamic field desactivado en %s, se usará columna metadata", name)
        return collection

    def _upsert_collection(
        self,
        *,
        collection: Collection,
        embedding_source: H5EmbeddingSource,
        embedding_type: str,
        source_label: str,
    ) -> EnrichmentSummary:
        processed = 0
        missing_metadata = 0
        missing_vectors = 0
        failed = 0

        dynamic_enabled = supports_dynamic_field(collection)
        timestamp = datetime.utcnow().isoformat()

        batch: List[Dict[str, Any]] = []

        allowed_fields: Optional[set[str]] = None
        if not dynamic_enabled:
            allowed_fields = set(collection_field_names(collection))
            logger.info("Campos permitidos en %s: %s", collection.name, sorted(allowed_fields))

        for protein_id in embedding_source.proteins:
            record = self.metadata.get(protein_id)
            if record is None:
                missing_metadata += 1
                continue

            vector = embedding_source.vector_for(protein_id)
            if vector is None:
                missing_vectors += 1
                continue

            meta_payload = dict(record)
            meta_payload.setdefault('species_id', self.species_id)
            if self.neighbor_context.get(protein_id):
                meta_payload.setdefault('interaction_neighbors', self.neighbor_context[protein_id])

            metadata_column = json.dumps(
                {
                    'gene_symbol': meta_payload.get('gene_symbol'),
                    'primary_uniprot': meta_payload.get('primary_uniprot'),
                    'cluster_id': meta_payload.get('cluster_id'),
                }
            )

            entity: Dict[str, Any] = {
                'protein_id': protein_id,
                'embedding': vector.astype(np.float32).tolist(),
                'species_id': meta_payload.get('species_id', self.species_id),
                'embedding_type': embedding_type,
                'source': source_label,
                'metadata': metadata_column,
                'upload_timestamp': timestamp,
            }

            if dynamic_enabled:
                entity['$meta'] = meta_payload
            elif allowed_fields is not None:
                if 'metadata' not in allowed_fields:
                    encoded = f"{source_label}|{metadata_column}"
                    max_len = get_field_attr(collection, 'source', 'max_length', 128)
                    if max_len and len(encoded) > max_len:
                        encoded = encoded[:max_len]
                    entity['source'] = encoded
                    entity.pop('metadata', None)
                entity = prune_to_schema(collection, entity)

            batch.append(entity)

            if len(batch) >= self.batch_size:
                if not self._commit_batch(collection, batch):
                    failed += len(batch)
                processed += len(batch)
                batch = []

        if batch:
            if not self._commit_batch(collection, batch):
                failed += len(batch)
            processed += len(batch)

        collection.flush()
        logger.info(
            "Colección %s actualizada (procesados=%s, faltante metadata=%s, faltante vector=%s, fallidos=%s)",
            collection.name,
            processed,
            missing_metadata,
            missing_vectors,
            failed,
        )

        return EnrichmentSummary(
            collection=collection.name,
            processed=processed,
            missing_metadata=missing_metadata,
            missing_vectors=missing_vectors,
            failed=failed,
        )

    def _commit_batch(self, collection: Collection, batch: List[Dict[str, Any]]) -> bool:
        if not batch:
            return True
        success = safe_upsert(collection, batch)
        if not success:
            logger.error("Fallo al upsert en %s", collection.name)
        return success


__all__ = [
    "StringMetadataLoader",
    "H5EmbeddingSource",
    "ZillizSpaceEnricher",
    "EnrichmentSummary",
]
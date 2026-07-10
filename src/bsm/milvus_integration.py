#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧮 BSM MILVUS INTEGRATION
Integración con Milvus/Zilliz para Biological Semantic Memory - Embeddings PubMedBERT
"""

import asyncio
import json
import logging
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass
import uuid

# Milvus/Zilliz imports
try:
    from pymilvus import (
        connections, Collection, FieldSchema, CollectionSchema, 
        DataType, utility, Index
    )
    MILVUS_AVAILABLE = True
except ImportError:
    MILVUS_AVAILABLE = False
    connections = None
    Collection = None
    FieldSchema = None
    CollectionSchema = None
    DataType = None
    utility = None
    Index = None

from .config import BSMConfig, get_bsm_config

logger = logging.getLogger(__name__)

# === MODELOS DE DATOS MILVUS ===

@dataclass
class ProteinEmbedding:
    """Embedding de proteína para Milvus"""
    protein_id: str
    name: str
    embedding: np.ndarray
    sequence: Optional[str] = None
    organism: Optional[str] = None
    function: Optional[str] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        # Asegurar que embedding sea numpy array
        if not isinstance(self.embedding, np.ndarray):
            self.embedding = np.array(self.embedding, dtype=np.float32)

@dataclass
class SimilarityResult:
    """Resultado de búsqueda por similitud"""
    protein_id: str
    name: str
    similarity_score: float
    embedding: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = None
    distance: float = 0.0

# === ESQUEMAS MILVUS BSM ===

class BSMMilvusSchema:
    """Esquemas para colecciones Milvus BSM"""
    
    @staticmethod
    def get_protein_collection_schema(dimension: int = 768) -> CollectionSchema:
        """
        Esquema para colección de proteínas con embeddings
        
        Args:
            dimension: Dimensión de los embeddings (768 para PubMedBERT)
            
        Returns:
            CollectionSchema para proteínas
        """
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=100, is_primary=True),
            FieldSchema(name="protein_id", dtype=DataType.VARCHAR, max_length=50),
            FieldSchema(name="name", dtype=DataType.VARCHAR, max_length=500),
            FieldSchema(name="sequence", dtype=DataType.VARCHAR, max_length=10000),
            FieldSchema(name="organism", dtype=DataType.VARCHAR, max_length=200),
            FieldSchema(name="function", dtype=DataType.VARCHAR, max_length=1000),
            FieldSchema(name="keywords", dtype=DataType.VARCHAR, max_length=2000),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dimension),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="embedding_model", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="created_at", dtype=DataType.VARCHAR, max_length=50),
            FieldSchema(name="updated_at", dtype=DataType.VARCHAR, max_length=50)
        ]
        
        return CollectionSchema(
            fields=fields,
            description="BSM Protein embeddings collection with PubMedBERT vectors"
        )
    
    @staticmethod
    def get_index_params() -> Dict[str, Any]:
        """Parámetros de índice optimizados para búsqueda semántica"""
        return {
            "metric_type": "IP",  # Inner Product para embeddings normalizados
            "index_type": "IVF_FLAT",
            "params": {"nlist": 1024}
        }
    
    @staticmethod
    def get_search_params() -> Dict[str, Any]:
        """Parámetros de búsqueda optimizados"""
        return {
            "metric_type": "IP",
            "params": {"nprobe": 10}
        }

    @staticmethod
    def get_dct_domain_schema() -> CollectionSchema:
        """Schema for DCTdomain 480-D fingerprint collection (GAP-5).

        Matches the ``dctdomain_embeddings`` collection structure plus the new
        ``cath_code`` field required for CATH-aware downstream queries.

        Use this schema when creating a NEW collection (``protein_domains_v2``)
        or migrating from the legacy ``dctdomain_embeddings`` collection.

        Returns:
            CollectionSchema with 480-D DSP vectors + CATH cross-reference fields.
        """
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="protein_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=480),
            FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="cath_code", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="is_global", dtype=DataType.BOOL),
            FieldSchema(name="gene_symbol", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="preferred_name", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="uniprot", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="sequence_length", dtype=DataType.INT64),
            FieldSchema(name="functional_annotation", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="sequence", dtype=DataType.VARCHAR, max_length=10000),
        ]
        return CollectionSchema(
            fields=fields,
            description="DCTdomain 480-D DSP fingerprints v2 with CATH codes",
            enable_dynamic_field=True,
        )

    @staticmethod
    def get_protein_multimodal_schema() -> CollectionSchema:
        """Schema for the protein-only 4-vector multimodal collection (GAP-4, schema v3).

        PINNED INVARIANTS — do NOT change dims/names without creating a new
        collection (schema_version bump required):

            prot_t5_vec  : FLOAT_VECTOR 1024D  — ProtT5-XL-U50 (sequence evolution)
            esm2_vec     : FLOAT_VECTOR 1280D  — ESM-C / ESM2-650M (language model)
            node2vec_vec : FLOAT_VECTOR  512D  — STRING/KG node2vec (network topology)
            dct_vec      : FLOAT_VECTOR  480D  — DCT domain fingerprint (int8→float32 avg-pool)
            schema_version : INT32 = 3         — v3: 4 dense, dropped af2+bm25+biolink+scibert
            model_id     : VARCHAR             — embedding run identifier

        Total vector fields: 4 dense (Zilliz Serverless hard cap = 4 vector fields).
        af2_vec (384D) and bm25_sparse deferred — af2 requires D: drive; bm25 requires
        a higher-tier cluster.  function_text scalar field retains text for re-indexing.

        Use with collection name ``protein_multimodal_rag_v1``.
        """
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="budo_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="uniprot_id", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="canonical_name", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="organism", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="function_text", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="ensp_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="schema_version", dtype=DataType.INT32),
            FieldSchema(name="model_id", dtype=DataType.VARCHAR, max_length=128),
            # === 4 dense vectors (PINNED dims) — Zilliz Serverless limit ===
            FieldSchema(name="prot_t5_vec", dtype=DataType.FLOAT_VECTOR, dim=1024),
            FieldSchema(name="esm2_vec", dtype=DataType.FLOAT_VECTOR, dim=1280),
            FieldSchema(name="node2vec_vec", dtype=DataType.FLOAT_VECTOR, dim=512),
            FieldSchema(name="dct_vec", dtype=DataType.FLOAT_VECTOR, dim=480),
        ]
        return CollectionSchema(
            fields=fields,
            description="4-vec protein RAG v3 (ProtT5+ESM-C+node2vec+DCT-480) — Zilliz-Serverless-4-field compliant",
            enable_dynamic_field=False,
        )

    @staticmethod
    def get_multimodal_index_params() -> List[Dict[str, Any]]:
        """Index params for every searchable field in protein_multimodal_rag_v1 (schema v3).

        Returns a list of (field_name, index_params) tuples for the caller to
        iterate and call ``collection.create_index(field, params)``.

        4 dense COSINE (HNSW) — Zilliz Serverless hard cap = 4 vector fields.
        af2_vec and bm25_sparse deferred to a higher-tier cluster.
        """
        dense_cosine = {"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 200}}
        return [
            ("prot_t5_vec", dense_cosine),
            ("esm2_vec", dense_cosine),
            ("node2vec_vec", dense_cosine),
            ("dct_vec", dense_cosine),
        ]

    @staticmethod
    def get_protein_af2_schema() -> "CollectionSchema":
        """Schema for the AF2 DPEB single-vector collection ``protein_af2_rag_v1``.

        Holds AlphaFold2 aggregated 384-D embeddings (from DPEB pipeline) with
        full protein metadata joined from the DCT parquet (sequence, gene symbol,
        preferred name, GO terms, functional annotation).

        PINNED INVARIANTS:
            af2_vec : FLOAT_VECTOR 384D  — AlphaFold2 DPEB aggregated embedding
            schema_version : INT32 = 1
            model_id       : "bsm-af2-protein-v1"

        One vector field — well within Zilliz Serverless 4-field cap.
        Collection name: ``protein_af2_rag_v1``.
        """
        fields = [
            FieldSchema(name="id",                     dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="uniprot_id",             dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="ensp_id",                dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="gene_symbol",            dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="canonical_name",         dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="organism",               dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="sequence",               dtype=DataType.VARCHAR, max_length=40000),
            FieldSchema(name="sequence_length",        dtype=DataType.INT64),
            FieldSchema(name="functional_annotation",  dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="go_biological_process",  dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="go_molecular_function",  dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="go_cellular_component",  dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="model_id",               dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="schema_version",         dtype=DataType.INT32),
            # === single 384D vector ===
            FieldSchema(name="af2_vec",                dtype=DataType.FLOAT_VECTOR, dim=384),
        ]
        return CollectionSchema(
            fields=fields,
            description="AF2 DPEB 384D protein collection v1 — single COSINE vector + full metadata",
            enable_dynamic_field=False,
        )

    @staticmethod
    def get_af2_index_params() -> List[Tuple[str, Dict[str, Any]]]:
        """Index params for ``protein_af2_rag_v1`` — single HNSW COSINE index on ``af2_vec``."""
        dense_cosine = {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200},
        }
        return [("af2_vec", dense_cosine)]

# === INTEGRACIÓN MILVUS BSM ===

class BSMMilvusIntegration:
    """Integración principal con Milvus/Zilliz para BSM"""
    
    def __init__(self, config: Optional[BSMConfig] = None):
        """
        Inicializa integración Milvus
        
        Args:
            config: Configuración BSM (opcional)
        """
        if not MILVUS_AVAILABLE:
            raise ImportError("Milvus/PyMilvus not available. Install with: pip install pymilvus")
        
        self.config = config or get_bsm_config()
        self.collection: Optional[Collection] = None
        self.collection_name = self.config.milvus.collection_name
        self.dimension = self.config.milvus.dimension
        self._connected = False
        
    async def initialize(self):
        """Inicializa conexión y colección"""
        await self.connect()
        await self.setup_collection()
        logger.info("🧮 BSM Milvus integration initialized successfully")

    async def initialize_existing(self):
        """Inicializa conexión y enlaza únicamente una colección existente."""
        await self.connect()
        await self.attach_existing_collection()
        logger.info("🧮 BSM Milvus integration attached to existing collection successfully")
    
    async def connect(self):
        """Establece conexión con Milvus/Zilliz"""
        try:
            milvus_config = self.config.milvus
            
            # Usar Zilliz Cloud si URI está configurada
            if milvus_config.uri and milvus_config.token:
                connections.connect(
                    alias="default",
                    uri=milvus_config.uri,
                    token=milvus_config.token
                )
                logger.info(f"✅ Connected to Zilliz Cloud: {milvus_config.uri}")
            else:
                # Usar Milvus local
                connections.connect(
                    alias="default",
                    host=milvus_config.host,
                    port=milvus_config.port
                )
                logger.info(f"✅ Connected to Milvus: {milvus_config.host}:{milvus_config.port}")
            
            self._connected = True
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to Milvus: {e}")
            raise
    
    async def disconnect(self):
        """Cierra conexión con Milvus"""
        if self._connected:
            connections.disconnect("default")
            self._connected = False
            logger.info("🔌 Milvus connection closed")
    
    async def setup_collection(self):
        """Configura o conecta a colección existente"""
        try:
            # Verificar si la colección existe
            if utility.has_collection(self.collection_name):
                logger.info(f"📂 Using existing collection: {self.collection_name}")
                self.collection = Collection(self.collection_name)
            else:
                # Crear nueva colección
                schema = BSMMilvusSchema.get_protein_collection_schema(self.dimension)
                self.collection = Collection(
                    name=self.collection_name,
                    schema=schema,
                    using='default'
                )
                logger.info(f"📂 Created new collection: {self.collection_name}")
            
            # Crear índice si no existe
            await self._setup_index()
            
            # Cargar colección en memoria
            self.collection.load()
            logger.info(f"💾 Collection {self.collection_name} loaded in memory")
            
        except Exception as e:
            logger.error(f"❌ Collection setup failed: {e}")
            raise

    async def attach_existing_collection(self):
        """Conecta a una colección existente sin crear recursos nuevos."""
        try:
            if not utility.has_collection(self.collection_name):
                raise ValueError(f"Milvus collection does not exist: {self.collection_name}")

            self.collection = Collection(self.collection_name)
            self.collection.load()
            vector_dim = self.get_vector_dimension()
            if vector_dim is not None:
                self.dimension = vector_dim
            logger.info(f"📂 Attached existing collection: {self.collection_name}")
        except Exception as e:
            logger.error(f"❌ Failed to attach existing collection {self.collection_name}: {e}")
            raise

    def get_vector_dimension(self) -> Optional[int]:
        """Obtiene la dimensión real del vector desde el schema de la colección."""
        if self.collection is None:
            return None
        try:
            for field in self.collection.schema.fields or []:
                if field.name in {"embedding", "vector"}:
                    params = getattr(field, "params", None) or {}
                    dim = params.get("dim")
                    return int(dim) if dim is not None else None
        except Exception as e:
            logger.warning(f"⚠️ Could not inspect vector dimension for {self.collection_name}: {e}")
        return None

    def get_metric_type(self) -> str:
        """Obtiene la métrica real del índice de la colección cuando está disponible."""
        if self.collection is None:
            return str(self.config.milvus.metric_type or "COSINE")
        try:
            indexes = getattr(self.collection, "indexes", None) or []
            for index in indexes:
                params = getattr(index, "params", None) or {}
                metric = params.get("metric_type")
                if metric:
                    return str(metric)
        except Exception as e:
            logger.warning(f"⚠️ Could not inspect metric type for {self.collection_name}: {e}")
        return str(self.config.milvus.metric_type or "COSINE")
    
    async def _setup_index(self):
        """Configura índice para búsqueda vectorial"""
        try:
            # Verificar si ya existe índice
            indexes = self.collection.indexes
            if indexes:
                logger.info("📇 Index already exists")
                return
            
            # Crear índice
            index_params = BSMMilvusSchema.get_index_params()
            self.collection.create_index(
                field_name="embedding",
                index_params=index_params
            )
            logger.info("📇 Vector index created successfully")
            
        except Exception as e:
            logger.warning(f"⚠️ Index setup warning: {e}")
    
    # === OPERACIONES CRUD ===
    
    async def insert_protein_embedding(self, protein_embedding: ProteinEmbedding) -> bool:
        """
        Inserta embedding de proteína
        
        Args:
            protein_embedding: Datos de proteína con embedding
            
        Returns:
            bool: True si se insertó exitosamente
        """
        try:
            # Normalizar embedding si es necesario
            embedding = protein_embedding.embedding
            if self.config.embedding.normalize:
                norm = np.linalg.norm(embedding)
                if norm > 0:
                    embedding = embedding / norm
            
            data = [{
                "id": str(uuid.uuid4()),
                "protein_id": protein_embedding.protein_id,
                "name": protein_embedding.name,
                "sequence": protein_embedding.sequence or "",
                "organism": protein_embedding.organism or "",
                "function": protein_embedding.function or "",
                "keywords": json.dumps(protein_embedding.metadata.get("keywords", [])),
                "user_id": protein_embedding.metadata.get("user_id", ""),
                "embedding": embedding.tolist(),
                "embedding_model": self.config.embedding.model_name,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }]
            
            result = self.collection.insert(data)
            
            if result.insert_count > 0:
                logger.debug(f"✅ Inserted protein embedding: {protein_embedding.protein_id}")
                return True
            return False
            
        except Exception as e:
            logger.error(f"❌ Error inserting protein embedding {protein_embedding.protein_id}: {e}")
            return False

    async def get_embeddings_by_ids(
        self,
        protein_ids: List[str],
        chunk: int = 800,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch embeddings and metadata for a list of protein_ids in batches.

        Returns a list of dicts with keys: protein_id, name, embedding, and any requested output_fields.
        """
        if self.collection is None:
            raise RuntimeError("Milvus collection not initialized. Call initialize().")
        if not protein_ids:
            return []
        # Determine available fields from schema and build a safe output_fields list
        available = set()
        try:
            available = {f.name for f in (self.collection.schema.fields or [])}
        except Exception:
            available = set()
        requested = set(output_fields or [])
        # Default desired minimal fields
        desired = {"protein_id", "name", "embedding"}
        fields = (requested or desired) & available
        # Handle alias for vector field if 'embedding' not present
        if "embedding" not in fields and "embedding" not in available and "vector" in available:
            fields.add("vector")
        # Ensure we always include an identifier if present
        if "protein_id" not in fields:
            for cand in ("mudo_id", "id"):
                if cand in available:
                    fields.add(cand)
                    break
        out: List[Dict[str, Any]] = []
        for i in range(0, len(protein_ids), chunk):
            batch = protein_ids[i : i + chunk]
            quoted = ", ".join([f'"{x}"' for x in batch])
            expr = f"protein_id in [{quoted}]"
            try:
                rows = self.collection.query(expr=expr, output_fields=list(fields))
                # rows is a list of dicts
                for r in rows:
                    try:
                        # Support either 'embedding' or 'vector' as vector field
                        vec_field = "embedding" if "embedding" in r else ("vector" if "vector" in r else None)
                        emb = np.array(r.get(vec_field, []), dtype=np.float32) if vec_field else np.array([], dtype=np.float32)
                        out.append({
                            "protein_id": str(r.get("protein_id", r.get("mudo_id", r.get("id", "")))),
                            "name": str(r.get("name", "")),
                            "embedding": emb,
                            **{k: r.get(k) for k in fields if k not in {"protein_id", "name", "embedding"}},
                        })
                    except Exception:
                        continue
            except Exception as e:
                logger.error(f"❌ Milvus query failed for batch {i}-{i+len(batch)}: {e}")
        return out

    async def get_all_embeddings(
        self,
        output_fields: Optional[List[str]] = None,
        limit: int = 256,
        max_total: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Best-effort fetch of all embeddings from the active collection.

        Note: Depending on Milvus version, full scans may require iterators. This method
        attempts paged queries and returns up to all rows if supported.
        """
        if self.collection is None:
            raise RuntimeError("Milvus collection not initialized. Call initialize().")
        # Build safe output_fields intersected with available schema
        available = set()
        try:
            available = {f.name for f in (self.collection.schema.fields or [])}
        except Exception:
            available = set()
        requested = set(output_fields or [])
        desired = {"protein_id", "name", "embedding"}
        fields = (requested or desired) & available
        # Add alias for vector field
        if "embedding" not in fields and "embedding" not in available and "vector" in available:
            fields.add("vector")
        # Ensure at least one identifier if protein_id missing
        if "protein_id" not in fields:
            for cand in ("mudo_id", "id"):
                if cand in available:
                    fields.add(cand)
                    break
        results: List[Dict[str, Any]] = []
        offset = 0
        while True:
            try:
                rows = self.collection.query(expr="", output_fields=list(fields), limit=limit, offset=offset)
            except TypeError:
                # Older PyMilvus may not support offset; try without offset once
                try:
                    rows = self.collection.query(expr="", output_fields=list(fields), limit=limit)
                except Exception as e:
                    logger.error(f"❌ Full scan unsupported on this Milvus version: {e}")
                    break
            except Exception as e:
                logger.error(f"❌ Query error during full scan: {e}")
                break
            if not rows:
                break
            for r in rows:
                try:
                    vec_field = "embedding" if "embedding" in r else ("vector" if "vector" in r else None)
                    emb = np.array(r.get(vec_field, []), dtype=np.float32) if vec_field else np.array([], dtype=np.float32)
                    results.append({
                        "protein_id": str(r.get("protein_id", r.get("mudo_id", r.get("id", "")))),
                        "name": str(r.get("name", "")),
                        "embedding": emb,
                        **{k: r.get(k) for k in fields if k not in {"protein_id", "name", "embedding"}},
                    })
                except Exception:
                    continue
            # Respect max_total cap if provided
            if max_total is not None and len(results) >= max_total:
                results = results[:max_total]
                break
            if len(rows) < limit:
                break
            offset += limit
        return results

    # === BÚSQUEDA DE SIMILARIDAD ===

    async def search_similar(
        self,
        query_embedding: np.ndarray,
        k: int = 10,
        expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[SimilarityResult]:
        """Busca los k más similares usando la colección actual.

        Args:
            query_embedding: Vector de consulta (dimensión = self.dimension)
            k: Número de resultados
            expr: Filtro booleano de Milvus (opcional)
            output_fields: Campos a retornar (protein_id, name, ...)

        Returns:
            Lista de SimilarityResult
        """
        try:
            if self.collection is None:
                raise RuntimeError("Milvus collection not initialized. Call initialize().")

            vec = np.asarray(query_embedding, dtype=np.float32)
            if self.config.embedding.normalize:
                n = np.linalg.norm(vec)
                if n > 0:
                    vec = vec / n

            params = dict(BSMMilvusSchema.get_search_params())
            params["metric_type"] = self.get_metric_type()
            available = set()
            try:
                available = {f.name for f in (self.collection.schema.fields or [])}
            except Exception:
                available = set()
            requested = list(output_fields or ["protein_id", "name", "embedding_model"])
            fields = [field for field in requested if field in available]
            if not fields and "protein_id" in available:
                fields = ["protein_id"]

            res = self.collection.search(
                data=[vec.tolist()],
                anns_field="embedding",
                param=params,
                limit=k,
                expr=expr,
                output_fields=fields,
            )
            out: List[SimilarityResult] = []
            for hits in res:
                for hit in hits:
                    out.append(
                        SimilarityResult(
                            protein_id=str(hit.get("protein_id", "")),
                            name=str(hit.get("name", "")),
                            similarity_score=float(hit.score),
                            metadata={k: hit.get(k) for k in fields if k in hit},
                            distance=0.0,
                        )
                    )
            return out
        except Exception as e:
            logger.error(f"❌ Milvus search failed: {e}")
            return []

    async def search_within_ids(
        self,
        query_embedding: np.ndarray,
        candidate_protein_ids: List[str],
        k: int = 10,
        chunk: int = 800,
    ) -> List[SimilarityResult]:
        """Busca restringiendo la consulta a un subconjunto de protein_id usando expr in-clauses por lotes.

        Nota: expr largos pueden exceder límites. Se divide en lotes y se combinan resultados.
        """
        if not candidate_protein_ids:
            return []
        all_hits: List[SimilarityResult] = []
        for i in range(0, len(candidate_protein_ids), chunk):
            batch = candidate_protein_ids[i : i + chunk]
            quoted = ", ".join([f'"{x}"' for x in batch])
            expr = f"protein_id in [{quoted}]"
            hits = await self.search_similar(query_embedding, k=k, expr=expr)
            all_hits.extend(hits)
        # Deduplicate by protein_id, keep best score
        best: Dict[str, SimilarityResult] = {}
        for h in all_hits:
            if h.protein_id not in best or h.similarity_score > best[h.protein_id].similarity_score:
                best[h.protein_id] = h
        # Return top-k overall
        return sorted(best.values(), key=lambda r: r.similarity_score, reverse=True)[:k]
    
    async def batch_insert_embeddings(self, 
                                    protein_embeddings: List[ProteinEmbedding],
                                    batch_size: int = 1000) -> Dict[str, Any]:
        """
        Inserta múltiples embeddings en lotes
        
        Args:
            protein_embeddings: Lista de embeddings de proteínas
            batch_size: Tamaño del lote
            
        Returns:
            Dict con estadísticas del procesamiento
        """
        success_count = 0
        error_count = 0
        errors = []
        
        try:
            # Procesar en lotes
            for i in range(0, len(protein_embeddings), batch_size):
                batch = protein_embeddings[i:i + batch_size]
                
                try:
                    # Preparar datos del lote
                    batch_data = []
                    for protein_emb in batch:
                        # Normalizar embedding
                        embedding = protein_emb.embedding
                        if self.config.embedding.normalize:
                            norm = np.linalg.norm(embedding)
                            if norm > 0:
                                embedding = embedding / norm
                        
                        batch_data.append({
                            "id": str(uuid.uuid4()),
                            "protein_id": protein_emb.protein_id,
                            "name": protein_emb.name,
                            "sequence": protein_emb.sequence or "",
                            "organism": protein_emb.organism or "",
                            "function": protein_emb.function or "",
                            "keywords": json.dumps(protein_emb.metadata.get("keywords", [])),
                            "embedding": embedding.tolist(),
                            "embedding_model": self.config.embedding.model_name,
                            "created_at": datetime.now().isoformat(),
                            "updated_at": datetime.now().isoformat()
                        })
                    
                    # Insertar lote
                    insert_result = self.collection.insert(batch_data)
                    success_count += getattr(insert_result, "insert_count", len(batch_data))
                    
                    logger.debug(f"📦 Batch inserted: {getattr(insert_result, 'insert_count', len(batch_data))} proteins")
                    
                except Exception as e:
                    error_count += len(batch)
                    error_msg = f"Batch {i//batch_size}: {str(e)}"
                    errors.append(error_msg)
                    logger.warning(f"⚠️ Batch insertion error: {e}")
            
            # Flush para asegurar persistencia
            self.collection.flush()
            
            logger.info(f"📦 Batch insertion completed: {success_count} success, {error_count} errors")
            
            return {
                "success_count": success_count,
                "error_count": error_count,
                "total_processed": len(protein_embeddings),
                "errors": errors[:10]  # Limitar errores mostrados
            }
            
        except Exception as e:
            logger.error(f"❌ Batch insertion failed: {e}")
            return {
                "success_count": 0,
                "error_count": len(protein_embeddings),
                "total_processed": len(protein_embeddings),
                "errors": [str(e)]
            }
    
    # === OPERACIONES DE BÚSQUEDA ===
    
    async def search_similar_proteins(self, 
                                    query_embedding: np.ndarray,
                                    top_k: int = 10,
                                    similarity_threshold: float = 0.7) -> List[SimilarityResult]:
        """
        Busca proteínas similares por embedding
        
        Args:
            query_embedding: Vector de consulta
            top_k: Número de resultados
            similarity_threshold: Umbral de similitud
            
        Returns:
            Lista de proteínas similares
        """
        try:
            # Normalizar query embedding
            if self.config.embedding.normalize:
                norm = np.linalg.norm(query_embedding)
                if norm > 0:
                    query_embedding = query_embedding / norm
            
            # Configurar búsqueda
            search_params = BSMMilvusSchema.get_search_params()
            
            # Realizar búsqueda
            results = self.collection.search(
                data=[query_embedding.tolist()],
                anns_field="embedding",
                param=search_params,
                limit=top_k,
                output_fields=["protein_id", "name", "organism", "function", "keywords"]
            )
            
            # Procesar resultados
            similar_proteins = []
            for result in results[0]:
                # Filtrar por umbral de similitud
                if result.score >= similarity_threshold:
                    similar_proteins.append(SimilarityResult(
                        protein_id=result.entity.get('protein_id'),
                        name=result.entity.get('name'),
                        similarity_score=result.score,
                        distance=result.distance,
                        metadata={
                            'organism': result.entity.get('organism'),
                            'function': result.entity.get('function'),
                            'keywords': json.loads(result.entity.get('keywords', '[]'))
                        }
                    ))
            
            logger.debug(f"🔍 Found {len(similar_proteins)} similar proteins (threshold: {similarity_threshold})")
            return similar_proteins
            
        except Exception as e:
            logger.error(f"❌ Error searching similar proteins: {e}")
            return []
    
    async def search_by_protein_id(self, protein_id: str) -> Optional[Dict[str, Any]]:
        """
        Busca proteína por ID
        
        Args:
            protein_id: ID de la proteína
            
        Returns:
            Datos de la proteína encontrada
        """
        try:
            results = self.collection.query(
                expr=f'protein_id == "{protein_id}"',
                output_fields=["*"]
            )
            
            if results:
                protein_data = results[0]
                # Deserializar keywords
                if 'keywords' in protein_data:
                    protein_data['keywords'] = json.loads(protein_data['keywords'])
                
                logger.debug(f"🔍 Found protein: {protein_id}")
                return protein_data
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Error searching protein by ID {protein_id}: {e}")
            return None

    async def search_by_stored_embedding(
        self,
        protein_id: str,
        top_k: int = 10,
        exclude_source: bool = True,
        output_fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Reutiliza el embedding ya almacenado para consultar la misma colección."""
        try:
            records = await self.get_embeddings_by_ids(
                [protein_id],
                output_fields=[
                    "protein_id",
                    "name",
                    "embedding",
                    "sequence",
                    "organism",
                    "function",
                    "embedding_model",
                ],
            )
            if not records:
                raise LookupError(f"Protein embedding not found in {self.collection_name}: {protein_id}")

            source_record = records[0]
            source_embedding = np.asarray(source_record.get("embedding", []), dtype=np.float32)
            if source_embedding.size == 0:
                raise ValueError(f"Stored embedding is empty for protein_id={protein_id}")

            expr = None
            if exclude_source:
                expr = f'protein_id != "{protein_id}"'

            results = await self.search_similar(
                query_embedding=source_embedding,
                k=top_k,
                expr=expr,
                output_fields=output_fields or ["protein_id", "name", "organism", "function", "embedding_model"],
            )

            return {
                "source_record": {
                    "protein_id": source_record.get("protein_id", protein_id),
                    "name": source_record.get("name", ""),
                    "sequence": source_record.get("sequence", ""),
                    "organism": source_record.get("organism", ""),
                    "function": source_record.get("function", ""),
                    "embedding_model": source_record.get("embedding_model", ""),
                    "embedding_dim": int(source_embedding.shape[0]),
                },
                "results": results,
            }
        except Exception as e:
            logger.error(f"❌ Error searching by stored embedding for {protein_id}: {e}")
            raise
    
    async def search_by_text(self, 
                           query: str,
                           fields: List[str] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        """
        Búsqueda por texto en campos específicos
        
        Args:
            query: Texto de búsqueda
            fields: Campos donde buscar (name, organism, function)
            limit: Límite de resultados
            
        Returns:
            Lista de proteínas encontradas
        """
        try:
            if fields is None:
                fields = ["name", "organism", "function"]
            
            # Construir expresión de búsqueda
            conditions = []
            for field in fields:
                conditions.append(f'{field} like "%{query}%"')
            
            expr = " or ".join(conditions)
            
            results = self.collection.query(
                expr=expr,
                output_fields=["protein_id", "name", "organism", "function", "keywords"],
                limit=limit
            )
            
            # Procesar resultados
            proteins = []
            for result in results:
                if 'keywords' in result:
                    result['keywords'] = json.loads(result['keywords'])
                proteins.append(result)
            
            logger.debug(f"🔍 Text search found {len(proteins)} proteins for: {query}")
            return proteins
            
        except Exception as e:
            logger.error(f"❌ Error in text search: {e}")
            return []
    
    # === OPERACIONES DE ANÁLISIS ===
    
    async def get_collection_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas de la colección
        
        Returns:
            Dict con estadísticas
        """
        try:
            stats = {
                "collection_name": self.collection_name,
                "total_entities": self.collection.num_entities,
                "dimension": self.dimension,
                "index_type": "IVF_FLAT",
                "metric_type": "COSINE"
            }
            
            # Obtener información adicional si está disponible
            try:
                collection_info = utility.describe_collection(self.collection_name)
                stats["schema_info"] = str(collection_info)
            except:
                pass
            
            logger.debug(f"📊 Collection stats: {stats['total_entities']} entities")
            return stats
            
        except Exception as e:
            logger.error(f"❌ Error getting collection stats: {e}")
            return {}
    
    async def get_protein_clusters(self, 
                                 sample_size: int = 1000,
                                 similarity_threshold: float = 0.8) -> Dict[str, Any]:
        """
        Identifica clusters de proteínas similares
        
        Args:
            sample_size: Tamaño de muestra para clustering
            similarity_threshold: Umbral para considerar similitud
            
        Returns:
            Información de clusters encontrados
        """
        try:
            # Obtener muestra aleatoria de proteínas
            sample_results = self.collection.query(
                expr="protein_id != ''",
                output_fields=["protein_id", "name", "embedding"],
                limit=sample_size
            )
            
            if not sample_results:
                return {"clusters": [], "total_proteins": 0}
            
            clusters = []
            processed_ids = set()
            
            for protein in sample_results:
                if protein['protein_id'] in processed_ids:
                    continue
                
                # Buscar proteínas similares
                embedding = np.array(protein['embedding'], dtype=np.float32)
                similar = await self.search_similar_proteins(
                    embedding, 
                    top_k=20, 
                    similarity_threshold=similarity_threshold
                )
                
                if len(similar) > 1:  # Al menos 2 proteínas para formar cluster
                    cluster = {
                        "cluster_id": len(clusters) + 1,
                        "representative": protein['protein_id'],
                        "representative_name": protein['name'],
                        "members": [s.protein_id for s in similar],
                        "size": len(similar),
                        "avg_similarity": np.mean([s.similarity_score for s in similar])
                    }
                    clusters.append(cluster)
                    
                    # Marcar como procesados
                    for similar_protein in similar:
                        processed_ids.add(similar_protein.protein_id)
            
            logger.info(f"🔬 Identified {len(clusters)} protein clusters")
            
            return {
                "clusters": clusters,
                "total_proteins": len(sample_results),
                "clustered_proteins": len(processed_ids),
                "clustering_threshold": similarity_threshold
            }
            
        except Exception as e:
            logger.error(f"❌ Error in protein clustering: {e}")
            return {"clusters": [], "total_proteins": 0}
    
    # === OPERACIONES DE MANTENIMIENTO ===
    
    async def optimize_collection(self):
        """Optimiza la colección para mejor rendimiento"""
        try:
            # Compactar colección
            self.collection.compact()
            logger.info("🔧 Collection compacted")
            
            # Recargar en memoria
            self.collection.load()
            logger.info("💾 Collection reloaded")
            
        except Exception as e:
            logger.warning(f"⚠️ Collection optimization warning: {e}")
    
    async def delete_protein_embedding(self, protein_id: str) -> bool:
        """
        Elimina embedding de proteína
        
        Args:
            protein_id: ID de la proteína a eliminar
            
        Returns:
            bool: True si se eliminó exitosamente
        """
        try:
            result = self.collection.delete(f'protein_id == "{protein_id}"')
            
            if result.delete_count > 0:
                logger.debug(f"🗑️ Deleted protein embedding: {protein_id}")
                return True
            return False
            
        except Exception as e:
            logger.error(f"❌ Error deleting protein embedding {protein_id}: {e}")
            return False

# === UTILIDADES ===

async def create_bsm_milvus_integration(config: Optional[BSMConfig] = None) -> BSMMilvusIntegration:
    """
    Crea e inicializa integración Milvus para BSM
    
    Args:
        config: Configuración BSM
        
    Returns:
        BSMMilvusIntegration inicializada
    """
    integration = BSMMilvusIntegration(config)
    await integration.initialize()
    return integration

async def migrate_existing_collection(old_collection_name: str, 
                                    new_collection_name: str,
                                    config: Optional[BSMConfig] = None) -> bool:
    """
    Migra datos de colección existente a nueva colección BSM
    
    Args:
        old_collection_name: Nombre de colección existente
        new_collection_name: Nombre de nueva colección BSM
        config: Configuración BSM
        
    Returns:
        bool: True si migración exitosa
    """
    try:
        # Implementar lógica de migración si es necesario
        logger.info(f"🔄 Migration from {old_collection_name} to {new_collection_name} started")
        # TODO: Implementar migración específica
        return True
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        return False

# === EXPORTACIONES ===

__all__ = [
    "BSMMilvusIntegration",
    "ProteinEmbedding",
    "SimilarityResult", 
    "BSMMilvusSchema",
    "create_bsm_milvus_integration",
    "migrate_existing_collection"
]
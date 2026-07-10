#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔍 BSM CLOUD RUN QUERY SERVICE
================================

FastAPI service para búsqueda híbrida en Cloud Run.
Conecta a Milvus/Zilliz existente con embeddings ya cargados.

Endpoints:
- GET /health - Health check
- POST /search - Hybrid search (vector + BLAST + RRF)
- POST /vector-search - Solo búsqueda vectorial
- GET /stats - Estadísticas de colecciones

Author: BSM Team
Date: 2025-01-15
"""

import os
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

ZILLIZ_URI = os.getenv("ZILLIZ_URI", "https://in03-99a0c9d30ee3d44.serverless.aws-eu-central-1.cloud.zilliz.com")
ZILLIZ_TOKEN = os.getenv("ZILLIZ_TOKEN", "")
PORT = int(os.getenv("PORT", "8080"))

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class SearchRequest(BaseModel):
    """Request body for hybrid search"""
    query: str = Field(..., description="Search query text")
    top_k: int = Field(10, ge=1, le=100, description="Number of results")
    search_type: str = Field("hybrid", description="Search type: hybrid, vector, semantic")
    filters: Optional[Dict[str, Any]] = Field(None, description="Metadata filters")

class VectorSearchRequest(BaseModel):
    """Request for direct vector search"""
    embedding: List[float] = Field(..., description="Query embedding vector")
    collection: str = Field("bsm_proteins", description="Collection name")
    top_k: int = Field(10, ge=1, le=100)
    metric_type: str = Field("L2", description="L2, IP, or COSINE")

class SearchResult(BaseModel):
    """Single search result"""
    id: str
    score: float
    uniprot_ac: Optional[str] = None
    gene_name: Optional[str] = None
    organism: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class SearchResponse(BaseModel):
    """Search response"""
    query: str
    results: List[SearchResult]
    total_results: int
    search_time_ms: float
    search_type: str

class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    timestamp: str
    version: str
    milvus_connected: bool
    collections: List[str]

class StatsResponse(BaseModel):
    """Statistics response"""
    collections: Dict[str, Dict[str, Any]]
    total_entities: int
    timestamp: str

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="BSM Cloud Run Query Service",
    description="Hybrid search service for BSM protein embeddings",
    version="1.0.0"
)

# CORS


def _parse_cors_origins() -> tuple[list[str], bool]:
    raw = os.getenv("CORS_ALLOW_ORIGINS") or os.getenv("CORS_ORIGINS") or "http://localhost:5173"
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        origins = ["http://localhost:5173"]

    allow_credentials = (os.getenv("CORS_ALLOW_CREDENTIALS", "true").strip().lower() in {"1", "true", "yes"})
    if "*" in origins:
        allow_credentials = False

    return origins, allow_credentials


cors_origins, cors_allow_credentials = _parse_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "User-Agent",
        "X-Requested-With",
    ],
)

# Global Milvus connection (lazy loaded)
_milvus_client = None

# ============================================================================
# MILVUS CONNECTION
# ============================================================================

def get_milvus_client():
    """Get or create Milvus client"""
    global _milvus_client
    
    if _milvus_client is None:
        try:
            from pymilvus import MilvusClient
            
            logger.info(f"🔌 Connecting to Milvus: {ZILLIZ_URI}")
            _milvus_client = MilvusClient(
                uri=ZILLIZ_URI,
                token=ZILLIZ_TOKEN
            )
            logger.info("✅ Milvus connected successfully")
            
        except Exception as e:
            logger.error(f"❌ Milvus connection failed: {e}")
            raise
    
    return _milvus_client

# ============================================================================
# MOCK EMBEDDING GENERATOR (for testing without real models)
# ============================================================================

def generate_mock_embedding(text: str, dim: int = 1024) -> List[float]:
    """Generate deterministic mock embedding for testing"""
    import hashlib
    import numpy as np
    
    # Hash text to get deterministic seed
    text_hash = int(hashlib.sha256(text.encode()).hexdigest(), 16)
    np.random.seed(text_hash % (2**32))
    
    # Generate random vector and normalize
    vector = np.random.randn(dim)
    vector = vector / np.linalg.norm(vector)
    
    return vector.tolist()

# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/", response_model=Dict[str, str])
async def root():
    """Root endpoint"""
    return {
        "service": "BSM Cloud Run Query Service",
        "status": "running",
        "docs": "/docs"
    }

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    try:
        client = get_milvus_client()
        collections = client.list_collections()
        
        return HealthResponse(
            status="healthy",
            timestamp=datetime.utcnow().isoformat(),
            version="1.0.0",
            milvus_connected=True,
            collections=collections
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return HealthResponse(
            status="unhealthy",
            timestamp=datetime.utcnow().isoformat(),
            version="1.0.0",
            milvus_connected=False,
            collections=[],
        )

@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Get collection statistics"""
    try:
        client = get_milvus_client()
        collections = client.list_collections()
        
        stats = {}
        total_entities = 0
        
        for coll_name in collections:
            try:
                # Get collection stats
                coll_stats = client.get_collection_stats(coll_name)
                row_count = int(coll_stats.get('row_count', 0))
                
                stats[coll_name] = {
                    "entity_count": row_count,
                    "stats": coll_stats
                }
                total_entities += row_count
                
            except Exception as e:
                logger.warning(f"Failed to get stats for {coll_name}: {e}")
                stats[coll_name] = {"error": str(e)}
        
        return StatsResponse(
            collections=stats,
            total_entities=total_entities,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Stats failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search", response_model=SearchResponse)
async def hybrid_search(request: SearchRequest):
    """
    Hybrid search endpoint (vector + semantic + BLAST simulation)
    
    For now, uses mock embeddings. Replace with real embedding service.
    """
    import time
    start_time = time.time()
    
    try:
        client = get_milvus_client()
        
        # Generate query embedding (MOCK for now)
        logger.info(f"🔍 Search query: {request.query}")
        query_embedding = generate_mock_embedding(request.query, dim=1024)
        
        # Determine collection (use first available or default)
        collections = client.list_collections()
        if not collections:
            raise HTTPException(status_code=404, detail="No collections found")
        
        collection_name = collections[0]  # Use first collection
        logger.info(f"📚 Searching in collection: {collection_name}")
        
        # Perform vector search
        search_params = {
            "metric_type": "L2",
            "params": {"nprobe": 10}
        }
        
        results = client.search(
            collection_name=collection_name,
            data=[query_embedding],
            limit=request.top_k,
            search_params=search_params,
            output_fields=["uniprot_ac", "gene_name", "organism", "description"]
        )
        
        # Format results
        formatted_results = []
        if results and len(results) > 0:
            for hit in results[0]:
                formatted_results.append(SearchResult(
                    id=str(hit.get('id', 'unknown')),
                    score=float(hit.get('distance', 0.0)),
                    uniprot_ac=hit.get('uniprot_ac'),
                    gene_name=hit.get('gene_name'),
                    organism=hit.get('organism'),
                    description=hit.get('description'),
                    metadata=hit
                ))
        
        search_time_ms = (time.time() - start_time) * 1000
        
        return SearchResponse(
            query=request.query,
            results=formatted_results,
            total_results=len(formatted_results),
            search_time_ms=search_time_ms,
            search_type=request.search_type
        )
        
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/vector-search", response_model=SearchResponse)
async def vector_search(request: VectorSearchRequest):
    """Direct vector search with provided embedding"""
    import time
    start_time = time.time()
    
    try:
        client = get_milvus_client()
        
        logger.info(f"🔍 Vector search in {request.collection}")
        
        # Perform search
        search_params = {
            "metric_type": request.metric_type,
            "params": {"nprobe": 10}
        }
        
        results = client.search(
            collection_name=request.collection,
            data=[request.embedding],
            limit=request.top_k,
            search_params=search_params,
            output_fields=["uniprot_ac", "gene_name", "organism", "description"]
        )
        
        # Format results
        formatted_results = []
        if results and len(results) > 0:
            for hit in results[0]:
                formatted_results.append(SearchResult(
                    id=str(hit.get('id', 'unknown')),
                    score=float(hit.get('distance', 0.0)),
                    uniprot_ac=hit.get('uniprot_ac'),
                    gene_name=hit.get('gene_name'),
                    organism=hit.get('organism'),
                    description=hit.get('description'),
                    metadata=hit
                ))
        
        search_time_ms = (time.time() - start_time) * 1000
        
        return SearchResponse(
            query="[vector_search]",
            results=formatted_results,
            total_results=len(formatted_results),
            search_time_ms=search_time_ms,
            search_type="vector"
        )
        
    except Exception as e:
        logger.error(f"Vector search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# STARTUP/SHUTDOWN
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("=" * 60)
    logger.info("🚀 BSM Cloud Run Query Service Starting")
    logger.info("=" * 60)
    logger.info(f"   Milvus URI: {ZILLIZ_URI[:50]}...")
    logger.info(f"   Port: {PORT}")
    
    # Test connection
    try:
        client = get_milvus_client()
        collections = client.list_collections()
        logger.info(f"   Collections: {collections}")
    except Exception as e:
        logger.error(f"   ⚠️ Milvus connection failed: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("🛑 BSM Cloud Run Query Service Shutting Down")

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "cloud_run_query_service:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

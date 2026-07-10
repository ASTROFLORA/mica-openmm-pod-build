#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🌉 BSM ROUTES EXTENSION - MOL★ INTEGRATION ENDPOINTS
Extensión de rutas BSM para integración con frontend Mol★

Author: Alex Rodriguez (AI Systems Architecture Lab)  
Date: October 10, 2025
Phase: 3.800 - BSM-Mol★ Integration
"""

import asyncio
import logging
from typing import Dict, List, Any, Optional

from fastapi import APIRouter, HTTPException, Query, Path as PathParam
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# Importar servicios
from .structure_service import (
    StructureService,
    StructureMetadata,
    GraphContext,
    SimilarStructure,
    create_structure_service
)
from .ese_service import (
    ESEService,
    ESECapsule,
    ESECapsuleMetadata,
    create_ese_service
)
from .chronosfold_api import (
    ChronosFoldAPI,
    ChronosFoldBootstrapStatus,
    ChronosFoldConfig,
    create_chronosfold_api
)

logger = logging.getLogger(__name__)

# === ROUTER PARA INTEGRACIÓN MOL★ ===

molstar_integration_router = APIRouter(
    prefix="/api/bsm",
    tags=["BSM - Mol★ Integration"]
)

# Cache global para servicios
_structure_service_cache: Optional[StructureService] = None
_ese_service_cache: Optional[ESEService] = None
_chronosfold_api_cache: Optional[ChronosFoldAPI] = None


async def get_structure_service() -> StructureService:
    """Obtiene StructureService (cached)"""
    global _structure_service_cache
    
    if _structure_service_cache is None:
        # TODO: Obtener clientes Neo4j y Milvus del contexto de aplicación
        _structure_service_cache = await create_structure_service()
        logger.info("🧬 StructureService initialized")
    
    return _structure_service_cache


async def get_ese_service() -> ESEService:
    """Obtiene ESEService (cached)"""
    global _ese_service_cache
    
    if _ese_service_cache is None:
        # TODO: Obtener clientes Neo4j y Milvus del contexto de aplicación
        _ese_service_cache = await create_ese_service()
        logger.info("🔮 ESEService initialized")
    
    return _ese_service_cache


async def get_chronosfold_api() -> ChronosFoldAPI:
    """Obtiene ChronosFoldAPI (cached)"""
    global _chronosfold_api_cache
    
    if _chronosfold_api_cache is None:
        _chronosfold_api_cache = await create_chronosfold_api()
        logger.info("⏰ ChronosFoldAPI initialized")
    
    return _chronosfold_api_cache


# === STRUCTURE ENDPOINTS ===

@molstar_integration_router.get(
    "/structures",
    response_model=StructureMetadata,
    summary="Get PDB structure metadata"
)
async def get_structure_metadata(
    pdb_id: str = Query(..., description="PDB ID (e.g., 1CRN)")
):
    """
    📊 Obtiene metadata de una estructura PDB
    
    Intenta Neo4j primero, fallback a RCSB PDB API
    
    **Ejemplo de uso:**
    ```
    GET /api/bsm/structures?pdb_id=1CRN
    ```
    """
    try:
        service = await get_structure_service()
        metadata = await service.get_structure_metadata(pdb_id)
        
        logger.info(f"✅ Structure metadata retrieved for {pdb_id}")
        return metadata
    
    except Exception as e:
        logger.error(f"❌ Error fetching structure metadata for {pdb_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@molstar_integration_router.get(
    "/structures/{pdb_id}/graph",
    response_model=GraphContext,
    summary="Get graph context for structure"
)
async def get_structure_graph_context(
    pdb_id: str = PathParam(..., description="PDB ID")
):
    """
    🕸️ Obtiene contexto de grafo para una estructura
    
    Incluye:
    - Interacciones proteína-proteína
    - Pathways asociados
    - GO terms
    - Enfermedades relacionadas
    - Drogas que interactúan
    
    **Ejemplo de uso:**
    ```
    GET /api/bsm/structures/1CRN/graph
    ```
    """
    try:
        service = await get_structure_service()
        graph_context = await service.get_graph_context(pdb_id)
        
        logger.info(
            f"✅ Graph context retrieved for {pdb_id}: "
            f"{graph_context.total_nodes} nodes, {graph_context.total_edges} edges"
        )
        return graph_context
    
    except Exception as e:
        logger.error(f"❌ Error fetching graph context for {pdb_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@molstar_integration_router.get(
    "/structures/{protein_id}/similar",
    response_model=List[SimilarStructure],
    summary="Find similar structures (vector search)"
)
async def find_similar_structures(
    protein_id: str = PathParam(..., description="Protein ID or PDB ID"),
    limit: int = Query(10, ge=1, le=100, description="Max number of results"),
    embedding_type: str = Query("ese", description="Embedding type (ese, sequence, structure)")
):
    """
    🔍 Busca estructuras similares usando Milvus vector search
    
    **Embedding types:**
    - `ese`: ESE (Essential State Embeddings) - blind dynamics
    - `sequence`: Sequence embeddings (PubMedBERT)
    - `structure`: Structural embeddings
    
    **Ejemplo de uso:**
    ```
    GET /api/bsm/structures/1CRN/similar?limit=5&embedding_type=ese
    ```
    """
    try:
        service = await get_structure_service()
        similar = await service.find_similar_structures(protein_id, limit, embedding_type)
        
        logger.info(f"✅ Found {len(similar)} similar structures for {protein_id}")
        return similar
    
    except Exception as e:
        logger.error(f"❌ Error finding similar structures for {protein_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === ESE CAPSULE ENDPOINTS ===

class ESECapsuleRequest(BaseModel):
    """Request para almacenar ESE capsule"""
    protein_id: str = Field(..., description="Protein identifier")
    pdb_id: Optional[str] = Field(None, description="PDB ID (optional)")
    ese_vectors: List[List[float]] = Field(..., description="ESE embedding vectors")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class ESECapsuleResponse(BaseModel):
    """Response de almacenamiento de ESE capsule"""
    success: bool
    capsule_id: str
    message: str


@molstar_integration_router.post(
    "/ese/capsule",
    response_model=ESECapsuleResponse,
    summary="Store ESE capsule"
)
async def store_ese_capsule(request: ESECapsuleRequest):
    """
    💾 Almacena un ESE capsule
    
    Flujo:
    1. Genera capsule_id único
    2. Almacena metadata en Neo4j
    3. Almacena vectors en Milvus
    4. Genera visualization hints
    
    **Ejemplo de uso:**
    ```json
    POST /api/bsm/ese/capsule
    {
      "protein_id": "P12345",
      "pdb_id": "1CRN",
      "ese_vectors": [[...], [...], ...],
      "metadata": {
        "blind": true,
        "source": "chronosfold_dynamo",
        "simulation": {...}
      }
    }
    ```
    """
    try:
        service = await get_ese_service()
        
        ese_data = {
            "protein_id": request.protein_id,
            "pdb_id": request.pdb_id,
            "ese_vectors": request.ese_vectors,
            "metadata": request.metadata
        }
        
        capsule_id = await service.store_capsule(ese_data)
        
        logger.info(f"✅ ESE capsule stored: {capsule_id}")
        
        return ESECapsuleResponse(
            success=True,
            capsule_id=capsule_id,
            message=f"ESE capsule stored successfully"
        )
    
    except Exception as e:
        logger.error(f"❌ Error storing ESE capsule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@molstar_integration_router.get(
    "/ese/capsule/{capsule_id}",
    response_model=ESECapsule,
    summary="Get ESE capsule"
)
async def get_ese_capsule(
    capsule_id: str = PathParam(..., description="Capsule ID")
):
    """
    📦 Recupera un ESE capsule completo
    
    Incluye:
    - Metadata (protein_id, timestamps, etc.)
    - ESE vectors
    - Visualization hints para Mol★
    
    **Ejemplo de uso:**
    ```
    GET /api/bsm/ese/capsule/ese_cap_abc123
    ```
    """
    try:
        service = await get_ese_service()
        capsule = await service.get_capsule(capsule_id)
        
        if capsule is None:
            raise HTTPException(
                status_code=404,
                detail=f"ESE capsule not found: {capsule_id}"
            )
        
        logger.info(f"✅ ESE capsule retrieved: {capsule_id}")
        return capsule
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error retrieving ESE capsule {capsule_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === CHRONOSFOLD ENDPOINTS ===

@molstar_integration_router.get(
    "/chronosfold/status",
    response_model=ChronosFoldBootstrapStatus,
    summary="Get ChronosFold infrastructure status"
)
async def get_chronosfold_status():
    """
    ⚙️ Obtiene estado del bootstrap de ChronosFold
    
    Verifica:
    - Estado general (ready/uninitialized/error)
    - Handles disponibles (Neo4j, Milvus, Object Storage)
    - Errores si existen
    
    **Ejemplo de uso:**
    ```
    GET /api/bsm/chronosfold/status
    ```
    """
    try:
        api = await get_chronosfold_api()
        status = await api.get_bootstrap_status()
        
        logger.info(f"✅ ChronosFold status: {status.status}")
        return status
    
    except Exception as e:
        logger.error(f"❌ Error getting ChronosFold status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@molstar_integration_router.post(
    "/chronosfold/bootstrap",
    response_model=ChronosFoldBootstrapStatus,
    summary="Trigger ChronosFold bootstrap"
)
async def trigger_chronosfold_bootstrap():
    """
    🚀 Trigger lazy bootstrap de ChronosFold infrastructure
    
    Ejecuta:
    1. Importa módulo de bootstrap
    2. Llama a bootstrap_infrastructure()
    3. Verifica handles creados
    
    **Ejemplo de uso:**
    ```
    POST /api/bsm/chronosfold/bootstrap
    ```
    """
    try:
        api = await get_chronosfold_api()
        status = await api.trigger_bootstrap()
        
        logger.info(f"✅ ChronosFold bootstrap triggered: {status.status}")
        return status
    
    except Exception as e:
        logger.error(f"❌ Error triggering ChronosFold bootstrap: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === VISUALIZATION ENDPOINTS ===

class MolstarVisualizationRequest(BaseModel):
    """Request para configuración de visualización Mol★"""
    structure_id: str = Field(..., description="Structure ID (PDB ID or protein ID)")
    include_graph_context: bool = Field(True, description="Include graph context overlay")
    include_ese_overlay: bool = Field(False, description="Include ESE capsule overlay")
    ese_capsule_id: Optional[str] = Field(None, description="ESE capsule ID (if overlay enabled)")
    representation: str = Field("cartoon", description="Mol★ representation type")
    color_scheme: str = Field("chain-id", description="Color scheme")


class MolstarVisualizationResponse(BaseModel):
    """Response con configuración Mol★"""
    success: bool
    structure_metadata: Optional[StructureMetadata] = None
    graph_context: Optional[GraphContext] = None
    ese_capsule: Optional[ESECapsule] = None
    molstar_config: Dict[str, Any] = Field(default_factory=dict)


@molstar_integration_router.post(
    "/visualization/molstar",
    response_model=MolstarVisualizationResponse,
    summary="Get Mol★ visualization configuration"
)
async def get_molstar_visualization_config(request: MolstarVisualizationRequest):
    """
    🎨 Obtiene configuración completa para visualización en Mol★
    
    Combina:
    - Structure metadata
    - Graph context (si habilitado)
    - ESE capsule overlay (si habilitado)
    - Configuración Mol★ pre-renderizada
    
    **Ejemplo de uso:**
    ```json
    POST /api/bsm/visualization/molstar
    {
      "structure_id": "1CRN",
      "include_graph_context": true,
      "include_ese_overlay": true,
      "ese_capsule_id": "ese_cap_abc123",
      "representation": "ball-and-stick",
      "color_scheme": "flexibility"
    }
    ```
    """
    try:
        struct_service = await get_structure_service()
        
        # 1. Obtener metadata
        metadata = await struct_service.get_structure_metadata(request.structure_id)
        
        # 2. Obtener graph context si está habilitado
        graph_context = None
        if request.include_graph_context:
            graph_context = await struct_service.get_graph_context(request.structure_id)
        
        # 3. Obtener ESE capsule si está habilitado
        ese_capsule = None
        if request.include_ese_overlay and request.ese_capsule_id:
            ese_service = await get_ese_service()
            ese_capsule = await ese_service.get_capsule(request.ese_capsule_id)
        
        # 4. Construir configuración Mol★
        molstar_config = {
            "representation": request.representation,
            "colorScheme": request.color_scheme,
            "pdbId": metadata.pdb_id,
            "graphOverlay": graph_context is not None,
            "eseOverlay": ese_capsule is not None
        }
        
        logger.info(f"✅ Mol★ visualization config generated for {request.structure_id}")
        
        return MolstarVisualizationResponse(
            success=True,
            structure_metadata=metadata,
            graph_context=graph_context,
            ese_capsule=ese_capsule,
            molstar_config=molstar_config
        )
    
    except Exception as e:
        logger.error(f"❌ Error generating Mol★ config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === EXPORTAR ROUTER ===

__all__ = ["molstar_integration_router"]

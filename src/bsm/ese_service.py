#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔮 BSM ESE SERVICE
Servicio para gestión de ESE (Essential State Embeddings) capsules

Author: Yuan Chen (Embedding Architecture Lab)
Date: October 10, 2025
Phase: 3.800 - BSM-Mol★ Integration
"""

import asyncio
import logging
import uuid
from typing import Dict, List, Any, Optional
from datetime import datetime

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# === PYDANTIC MODELS ===

class ESECapsuleMetadata(BaseModel):
    """Metadata de un ESE capsule"""
    capsule_id: str = Field(..., description="Unique capsule ID")
    protein_id: str = Field(..., description="Protein identifier")
    pdb_id: Optional[str] = None
    blind: bool = Field(True, description="Blind dynamics flag")
    source: str = Field("chronosfold_dynamo", description="Source pipeline")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    num_frames: Optional[int] = None
    num_residues: Optional[int] = None
    embedding_dimension: int = 512  # Default ESE dimension
    simulation_metadata: Dict[str, Any] = Field(default_factory=dict)


class ESECapsule(BaseModel):
    """ESE Capsule completo con metadata y vectores"""
    metadata: ESECapsuleMetadata
    ese_vectors: List[List[float]] = Field(..., description="ESE embedding vectors")
    visualization_hints: Dict[str, Any] = Field(default_factory=dict)


class ESEVisualizationHints(BaseModel):
    """Hints para visualización en Mol★"""
    color_map: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "gradient",
            "property": "flexibility",
            "scale": {"min": 0.0, "max": 1.0},
            "colors": ["#0000FF", "#00FF00", "#FFFF00", "#FF0000"]
        }
    )
    highlight_regions: List[Dict[str, Any]] = Field(default_factory=list)
    overlay_opacity: float = 0.7
    diagnostic_plots: List[str] = Field(
        default_factory=lambda: ["flexibility_heatmap", "rmsf_plot", "correlation_matrix"]
    )


# === ESE SERVICE ===

class ESEService:
    """
    Servicio para gestión de ESE capsules
    
    Proporciona:
    - Almacenamiento de ESE capsules (Milvus + Neo4j)
    - Recuperación de capsules
    - Generación de visualization hints para Mol★
    - Análisis de ESE vectors
    """
    
    def __init__(self, neo4j_client=None, milvus_client=None):
        """
        Inicializa el servicio
        
        Args:
            neo4j_client: Cliente Neo4j (opcional, lazy init)
            milvus_client: Cliente Milvus (opcional, lazy init)
        """
        self.neo4j = neo4j_client
        self.milvus = milvus_client
        logger.info("🔮 ESEService initialized")
    
    async def store_capsule(self, ese_data: Dict[str, Any]) -> str:
        """
        Almacena un ESE capsule
        
        Flujo:
        1. Generar capsule_id
        2. Validar ESE vectors
        3. Almacenar metadata en Neo4j
        4. Almacenar vectors en Milvus
        5. Generar visualization hints
        
        Args:
            ese_data: {
                protein_id: str,
                pdb_id: str (optional),
                ese_vectors: List[List[float]],
                metadata: Dict (optional)
            }
        
        Returns:
            capsule_id: Unique identifier for the stored capsule
        """
        try:
            # 1. Generar capsule_id
            capsule_id = f"ese_cap_{uuid.uuid4().hex[:12]}"
            
            # 2. Validar y extraer datos
            protein_id = ese_data.get("protein_id")
            ese_vectors = ese_data.get("ese_vectors", [])
            metadata_dict = ese_data.get("metadata", {})
            
            if not protein_id:
                raise ValueError("protein_id is required")
            if not ese_vectors:
                raise ValueError("ese_vectors cannot be empty")
            
            # Validar dimensiones
            vectors_array = np.array(ese_vectors)
            num_frames, embedding_dim = vectors_array.shape
            
            logger.info(
                f"📦 Storing ESE capsule {capsule_id}: "
                f"{num_frames} frames x {embedding_dim}D"
            )
            
            # 3. Crear metadata
            metadata = ESECapsuleMetadata(
                capsule_id=capsule_id,
                protein_id=protein_id,
                pdb_id=ese_data.get("pdb_id"),
                blind=metadata_dict.get("blind", True),
                source=metadata_dict.get("source", "chronosfold_dynamo"),
                num_frames=num_frames,
                embedding_dimension=embedding_dim,
                simulation_metadata=metadata_dict.get("simulation", {})
            )
            
            # 4. Almacenar metadata en Neo4j
            if self.neo4j:
                await self._store_metadata_in_neo4j(metadata)
            
            # 5. Almacenar vectors en Milvus
            if self.milvus:
                await self._store_vectors_in_milvus(capsule_id, ese_vectors, metadata)
            
            logger.info(f"✅ ESE capsule {capsule_id} stored successfully")
            return capsule_id
        
        except Exception as e:
            logger.error(f"❌ Error storing ESE capsule: {e}")
            raise
    
    async def _store_metadata_in_neo4j(self, metadata: ESECapsuleMetadata):
        """Almacena metadata del capsule en Neo4j"""
        try:
            query = """
            MERGE (p:Protein {id: $protein_id})
            ON MATCH SET p.last_updated = datetime()
            
            CREATE (cap:ESECapsule {
                id: $capsule_id,
                protein_id: $protein_id,
                pdb_id: $pdb_id,
                blind: $blind,
                source: $source,
                timestamp: $timestamp,
                num_frames: $num_frames,
                num_residues: $num_residues,
                embedding_dimension: $embedding_dimension
            })
            
            CREATE (p)-[:HAS_ESE_CAPSULE]->(cap)
            
            RETURN cap.id as capsule_id
            """
            
            await self.neo4j.execute_write(
                query,
                capsule_id=metadata.capsule_id,
                protein_id=metadata.protein_id,
                pdb_id=metadata.pdb_id,
                blind=metadata.blind,
                source=metadata.source,
                timestamp=metadata.timestamp,
                num_frames=metadata.num_frames,
                num_residues=metadata.num_residues,
                embedding_dimension=metadata.embedding_dimension
            )
            
            logger.info(f"✅ ESE capsule metadata stored in Neo4j")
        
        except Exception as e:
            logger.error(f"❌ Error storing metadata in Neo4j: {e}")
            # Non-critical, continue
    
    async def _store_vectors_in_milvus(
        self, 
        capsule_id: str, 
        ese_vectors: List[List[float]],
        metadata: ESECapsuleMetadata
    ):
        """Almacena ESE vectors en Milvus"""
        try:
            collection_name = "ese_capsules_v1"
            
            # Preparar datos para inserción
            # Almacenamos un vector "promedio" o representativo del capsule
            # Para búsquedas de similitud entre capsules
            vectors_array = np.array(ese_vectors)
            representative_vector = vectors_array.mean(axis=0).tolist()
            
            entities = [
                {
                    "capsule_id": capsule_id,
                    "protein_id": metadata.protein_id,
                    "pdb_id": metadata.pdb_id,
                    "embedding": representative_vector,
                    "num_frames": metadata.num_frames,
                    "timestamp": metadata.timestamp
                }
            ]
            
            await self.milvus.insert(
                collection_name=collection_name,
                data=entities
            )
            
            logger.info(f"✅ ESE capsule vectors stored in Milvus collection {collection_name}")
        
        except Exception as e:
            logger.error(f"❌ Error storing vectors in Milvus: {e}")
            # Non-critical, continue
    
    async def get_capsule(self, capsule_id: str) -> Optional[ESECapsule]:
        """
        Recupera un ESE capsule completo
        
        Args:
            capsule_id: Capsule identifier
        
        Returns:
            ESECapsule con metadata, vectors y visualization hints
        """
        try:
            # 1. Obtener metadata desde Neo4j
            metadata = await self._get_metadata_from_neo4j(capsule_id)
            if not metadata:
                logger.warning(f"⚠️ Capsule {capsule_id} not found")
                return None
            
            # 2. Obtener vectors desde Milvus
            ese_vectors = await self._get_vectors_from_milvus(capsule_id)
            if not ese_vectors:
                logger.warning(f"⚠️ No vectors found for capsule {capsule_id}")
                ese_vectors = []
            
            # 3. Generar visualization hints
            viz_hints = await self.generate_visualization_hints(capsule_id, ese_vectors, metadata)
            
            logger.info(f"✅ ESE capsule {capsule_id} retrieved")
            
            return ESECapsule(
                metadata=metadata,
                ese_vectors=ese_vectors,
                visualization_hints=viz_hints
            )
        
        except Exception as e:
            logger.error(f"❌ Error retrieving ESE capsule {capsule_id}: {e}")
            return None
    
    async def _get_metadata_from_neo4j(self, capsule_id: str) -> Optional[ESECapsuleMetadata]:
        """Recupera metadata desde Neo4j"""
        if not self.neo4j:
            return None
        
        try:
            query = """
            MATCH (cap:ESECapsule {id: $capsule_id})
            RETURN cap
            """
            
            result = await self.neo4j.execute_read(query, capsule_id=capsule_id)
            
            if not result:
                return None
            
            cap_node = result[0]["cap"]
            
            return ESECapsuleMetadata(
                capsule_id=capsule_id,
                protein_id=cap_node.get("protein_id"),
                pdb_id=cap_node.get("pdb_id"),
                blind=cap_node.get("blind", True),
                source=cap_node.get("source", "unknown"),
                timestamp=cap_node.get("timestamp"),
                num_frames=cap_node.get("num_frames"),
                num_residues=cap_node.get("num_residues"),
                embedding_dimension=cap_node.get("embedding_dimension", 512)
            )
        
        except Exception as e:
            logger.error(f"❌ Error fetching metadata from Neo4j: {e}")
            return None
    
    async def _get_vectors_from_milvus(self, capsule_id: str) -> List[List[float]]:
        """Recupera ESE vectors desde Milvus"""
        if not self.milvus:
            return []
        
        try:
            collection_name = "ese_capsules_v1"
            
            result = await self.milvus.query(
                collection_name=collection_name,
                expr=f'capsule_id == "{capsule_id}"',
                output_fields=["embedding"]
            )
            
            if result and len(result) > 0:
                # Retornar como lista de listas (compatible con frontend)
                return [result[0].get("embedding", [])]
            
            return []
        
        except Exception as e:
            logger.error(f"❌ Error fetching vectors from Milvus: {e}")
            return []
    
    async def generate_visualization_hints(
        self, 
        capsule_id: str,
        ese_vectors: List[List[float]],
        metadata: ESECapsuleMetadata
    ) -> Dict[str, Any]:
        """
        Genera visualization hints para Mol★
        
        Calcula:
        - Color map basado en flexibility scores
        - Regiones a destacar (alta/baja flexibilidad)
        - Configuración de overlay
        - Lista de gráficos de diagnóstico
        
        Args:
            capsule_id: Capsule ID
            ese_vectors: ESE embedding vectors
            metadata: Capsule metadata
        
        Returns:
            Dict con visualization hints para frontend
        """
        try:
            hints = ESEVisualizationHints()
            
            if ese_vectors and len(ese_vectors) > 0:
                # Calcular estadísticas de flexibility
                vectors_array = np.array(ese_vectors)
                
                # Norma L2 de cada frame como proxy de flexibility
                flexibility_scores = np.linalg.norm(vectors_array, axis=1)
                
                # Normalizar a [0, 1]
                flex_min = flexibility_scores.min()
                flex_max = flexibility_scores.max()
                normalized_flex = (flexibility_scores - flex_min) / (flex_max - flex_min + 1e-8)
                
                # Identificar regiones de alta y baja flexibilidad
                high_flex_threshold = 0.7
                low_flex_threshold = 0.3
                
                high_flex_regions = np.where(normalized_flex > high_flex_threshold)[0].tolist()
                low_flex_regions = np.where(normalized_flex < low_flex_threshold)[0].tolist()
                
                hints.highlight_regions = [
                    {
                        "type": "high_flexibility",
                        "frames": high_flex_regions[:10],  # Top 10
                        "color": "#FF0000",
                        "label": "High flexibility regions"
                    },
                    {
                        "type": "low_flexibility",
                        "frames": low_flex_regions[:10],  # Top 10
                        "color": "#0000FF",
                        "label": "Rigid regions"
                    }
                ]
                
                # Actualizar color_map con stats reales
                hints.color_map = {
                    "type": "gradient",
                    "property": "flexibility",
                    "scale": {
                        "min": float(flex_min),
                        "max": float(flex_max),
                        "mean": float(flexibility_scores.mean()),
                        "std": float(flexibility_scores.std())
                    },
                    "colors": ["#0000FF", "#00FF00", "#FFFF00", "#FF0000"]
                }
            
            logger.info(f"✅ Generated visualization hints for {capsule_id}")
            
            return hints.dict()
        
        except Exception as e:
            logger.error(f"❌ Error generating visualization hints: {e}")
            return ESEVisualizationHints().dict()


# === FACTORY FUNCTION ===

async def create_ese_service(neo4j_client=None, milvus_client=None) -> ESEService:
    """
    Factory function para crear ESEService
    
    Args:
        neo4j_client: Cliente Neo4j (opcional)
        milvus_client: Cliente Milvus (opcional)
    
    Returns:
        ESEService inicializado
    """
    service = ESEService(neo4j_client, milvus_client)
    logger.info("✅ ESEService created")
    return service

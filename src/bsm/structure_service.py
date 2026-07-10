#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM STRUCTURE SERVICE
Servicio para gestión de estructuras proteicas, contexto de grafo y búsquedas de similitud

Author: Alex Rodriguez (AI Systems Architecture Lab)
Date: October 10, 2025
Phase: 3.800 - BSM-Mol★ Integration
"""

import asyncio
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# === PYDANTIC MODELS ===

class StructureMetadata(BaseModel):
    """Metadata de estructura PDB"""
    pdb_id: str = Field(..., description="PDB ID")
    title: Optional[str] = None
    organism: Optional[str] = None
    method: Optional[str] = None  # X-ray, NMR, Cryo-EM, etc.
    resolution: Optional[float] = None
    release_date: Optional[str] = None
    protein_name: Optional[str] = None
    gene_names: List[str] = Field(default_factory=list)
    uniprot_ids: List[str] = Field(default_factory=list)
    source: str = "neo4j"  # neo4j or rcsb_api


class GraphContext(BaseModel):
    """Contexto de grafo para una estructura"""
    protein_id: str
    interactions: List[Dict[str, Any]] = Field(default_factory=list)
    pathways: List[Dict[str, Any]] = Field(default_factory=list)
    go_terms: List[Dict[str, Any]] = Field(default_factory=list)
    diseases: List[Dict[str, Any]] = Field(default_factory=list)
    drugs: List[Dict[str, Any]] = Field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0


class SimilarStructure(BaseModel):
    """Resultado de búsqueda de similitud"""
    protein_id: str
    pdb_id: Optional[str] = None
    similarity_score: float
    title: Optional[str] = None
    organism: Optional[str] = None
    thumbnail_url: Optional[str] = None
    embedding_type: str = "ese"  # ese, sequence, structure


# === STRUCTURE SERVICE ===

class StructureService:
    """
    Servicio para gestión de estructuras proteicas
    
    Proporciona:
    - Metadata de estructuras (Neo4j + RCSB PDB fallback)
    - Contexto de grafo (interacciones, pathways, GO terms)
    - Búsquedas de similitud (Milvus vector search)
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
        self.rcsb_base_url = "https://data.rcsb.org/rest/v1/core"
        logger.info("🧬 StructureService initialized")
    
    async def get_structure_metadata(self, pdb_id: str) -> StructureMetadata:
        """
        Obtiene metadata de una estructura
        
        Intenta Neo4j primero, fallback a RCSB PDB API
        
        Args:
            pdb_id: PDB ID (ej: "1CRN")
        
        Returns:
            StructureMetadata con información de la estructura
        """
        try:
            # Intentar Neo4j primero
            if self.neo4j:
                logger.info(f"📊 Fetching {pdb_id} metadata from Neo4j...")
                metadata = await self._get_metadata_from_neo4j(pdb_id)
                if metadata:
                    return metadata
            
            # Fallback a RCSB PDB API
            logger.info(f"📊 Fetching {pdb_id} metadata from RCSB PDB API...")
            metadata = await self._get_metadata_from_rcsb(pdb_id)
            return metadata
        
        except Exception as e:
            logger.error(f"❌ Error fetching metadata for {pdb_id}: {e}")
            # Retornar metadata mínima
            return StructureMetadata(
                pdb_id=pdb_id.upper(),
                title=f"Structure {pdb_id.upper()}",
                source="unknown"
            )
    
    async def _get_metadata_from_neo4j(self, pdb_id: str) -> Optional[StructureMetadata]:
        """Obtiene metadata desde Neo4j"""
        if not self.neo4j:
            return None
        
        try:
            query = """
            MATCH (p:Protein {pdb_id: $pdb_id})
            OPTIONAL MATCH (p)-[:HAS_GENE]->(g:Gene)
            OPTIONAL MATCH (p)-[:HAS_UNIPROT_ID]->(u:UniProtID)
            RETURN p, collect(DISTINCT g.name) as gene_names, collect(DISTINCT u.accession) as uniprot_ids
            """
            
            result = await self.neo4j.execute_read(query, pdb_id=pdb_id.upper())
            
            if not result:
                return None
            
            protein_node = result[0]["p"]
            gene_names = result[0].get("gene_names", [])
            uniprot_ids = result[0].get("uniprot_ids", [])
            
            return StructureMetadata(
                pdb_id=pdb_id.upper(),
                title=protein_node.get("title"),
                organism=protein_node.get("organism"),
                method=protein_node.get("experimental_method"),
                resolution=protein_node.get("resolution"),
                release_date=protein_node.get("release_date"),
                protein_name=protein_node.get("name"),
                gene_names=gene_names,
                uniprot_ids=uniprot_ids,
                source="neo4j"
            )
        
        except Exception as e:
            logger.warning(f"⚠️ Neo4j metadata fetch failed for {pdb_id}: {e}")
            return None
    
    async def _get_metadata_from_rcsb(self, pdb_id: str) -> StructureMetadata:
        """Obtiene metadata desde RCSB PDB REST API"""
        url = f"{self.rcsb_base_url}/entry/{pdb_id.upper()}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                
                # Extraer información relevante
                struct_data = data.get("struct", {})
                exptl_data = data.get("exptl", [{}])[0] if data.get("exptl") else {}
                refine_data = data.get("refine", [{}])[0] if data.get("refine") else {}
                
                return StructureMetadata(
                    pdb_id=pdb_id.upper(),
                    title=struct_data.get("title"),
                    organism=data.get("rcsb_entity_source_organism", [{}])[0].get("ncbi_scientific_name"),
                    method=exptl_data.get("method"),
                    resolution=refine_data.get("ls_d_res_high"),
                    release_date=data.get("rcsb_accession_info", {}).get("initial_release_date"),
                    source="rcsb_api"
                )
            
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.warning(f"⚠️ PDB {pdb_id} not found in RCSB")
                else:
                    logger.error(f"❌ RCSB API error for {pdb_id}: {e}")
                raise
    
    async def get_graph_context(self, pdb_id: str) -> GraphContext:
        """
        Obtiene contexto de grafo para una estructura
        
        Consulta Neo4j para obtener:
        - Interacciones proteína-proteína
        - Pathways asociados
        - GO terms
        - Enfermedades relacionadas
        - Drogas que interactúan
        
        Args:
            pdb_id: PDB ID
        
        Returns:
            GraphContext con información del grafo
        """
        if not self.neo4j:
            logger.warning("⚠️ Neo4j client not available, returning empty graph context")
            return GraphContext(protein_id=pdb_id)
        
        try:
            # Consulta Cypher para obtener contexto completo
            query = """
            MATCH (p:Protein {pdb_id: $pdb_id})
            OPTIONAL MATCH (p)-[r_int:INTERACTS_WITH]-(p2:Protein)
            OPTIONAL MATCH (p)-[:PARTICIPATES_IN]->(pw:Pathway)
            OPTIONAL MATCH (p)-[:HAS_GO_ANNOTATION]->(go:GOTerm)
            OPTIONAL MATCH (p)-[:ASSOCIATED_WITH_DISEASE]->(d:Disease)
            OPTIONAL MATCH (p)<-[:TARGETS]-(drug:Drug)
            
            RETURN 
                p.id as protein_id,
                collect(DISTINCT {
                    target_protein: p2.id,
                    target_pdb: p2.pdb_id,
                    interaction_type: type(r_int),
                    confidence: r_int.confidence,
                    residues: r_int.residues
                }) as interactions,
                collect(DISTINCT {
                    pathway_id: pw.id,
                    pathway_name: pw.name,
                    database: pw.database
                }) as pathways,
                collect(DISTINCT {
                    go_id: go.id,
                    go_term: go.name,
                    aspect: go.aspect,
                    evidence: go.evidence_code
                }) as go_terms,
                collect(DISTINCT {
                    disease_id: d.id,
                    disease_name: d.name,
                    association_type: d.association_type
                }) as diseases,
                collect(DISTINCT {
                    drug_id: drug.id,
                    drug_name: drug.name,
                    mechanism: drug.mechanism
                }) as drugs
            """
            
            result = await self.neo4j.execute_read(query, pdb_id=pdb_id.upper())
            
            if not result:
                return GraphContext(protein_id=pdb_id)
            
            data = result[0]
            
            # Filtrar nulls
            interactions = [i for i in data.get("interactions", []) if i.get("target_protein")]
            pathways = [pw for pw in data.get("pathways", []) if pw.get("pathway_id")]
            go_terms = [go for go in data.get("go_terms", []) if go.get("go_id")]
            diseases = [d for d in data.get("diseases", []) if d.get("disease_id")]
            drugs = [dr for dr in data.get("drugs", []) if dr.get("drug_id")]
            
            total_nodes = 1 + len(set(
                [i["target_protein"] for i in interactions] +
                [pw["pathway_id"] for pw in pathways] +
                [go["go_id"] for go in go_terms] +
                [d["disease_id"] for d in diseases] +
                [dr["drug_id"] for dr in drugs]
            ))
            
            total_edges = len(interactions) + len(pathways) + len(go_terms) + len(diseases) + len(drugs)
            
            logger.info(f"✅ Graph context for {pdb_id}: {total_nodes} nodes, {total_edges} edges")
            
            return GraphContext(
                protein_id=data.get("protein_id", pdb_id),
                interactions=interactions,
                pathways=pathways,
                go_terms=go_terms,
                diseases=diseases,
                drugs=drugs,
                total_nodes=total_nodes,
                total_edges=total_edges
            )
        
        except Exception as e:
            logger.error(f"❌ Error fetching graph context for {pdb_id}: {e}")
            return GraphContext(protein_id=pdb_id)
    
    async def find_similar_structures(
        self, 
        protein_id: str, 
        limit: int = 10,
        embedding_type: str = "ese"
    ) -> List[SimilarStructure]:
        """
        Busca estructuras similares usando Milvus vector search
        
        Args:
            protein_id: ID de proteína (PDB ID o internal ID)
            limit: Número máximo de resultados
            embedding_type: Tipo de embedding (ese, sequence, structure)
        
        Returns:
            Lista de estructuras similares ordenadas por score
        """
        if not self.milvus:
            logger.warning("⚠️ Milvus client not available, returning empty results")
            return []
        
        try:
            # 1. Obtener embedding de la proteína query
            embedding = await self._get_protein_embedding(protein_id, embedding_type)
            if embedding is None:
                logger.warning(f"⚠️ No embedding found for {protein_id}")
                return []
            
            # 2. Buscar en Milvus
            collection_name = f"{embedding_type}_embeddings_v1"
            logger.info(f"🔍 Searching {collection_name} for similar structures to {protein_id}...")
            
            search_params = {
                "metric_type": "COSINE",
                "params": {"nprobe": 16}
            }
            
            results = await self.milvus.search(
                collection_name=collection_name,
                vectors=[embedding],
                limit=limit + 1,  # +1 para excluir self-match
                search_params=search_params,
                output_fields=["protein_id", "pdb_id", "title", "organism"]
            )
            
            # 3. Procesar resultados
            similar_structures = []
            for hit in results[0]:  # results[0] porque solo enviamos 1 query vector
                # Excluir self-match
                if hit.entity.get("protein_id") == protein_id:
                    continue
                
                similar_structures.append(SimilarStructure(
                    protein_id=hit.entity.get("protein_id"),
                    pdb_id=hit.entity.get("pdb_id"),
                    similarity_score=float(hit.score),
                    title=hit.entity.get("title"),
                    organism=hit.entity.get("organism"),
                    thumbnail_url=self._get_thumbnail_url(hit.entity.get("pdb_id")),
                    embedding_type=embedding_type
                ))
            
            logger.info(f"✅ Found {len(similar_structures)} similar structures")
            return similar_structures[:limit]
        
        except Exception as e:
            logger.error(f"❌ Error searching similar structures for {protein_id}: {e}")
            return []
    
    async def _get_protein_embedding(self, protein_id: str, embedding_type: str) -> Optional[List[float]]:
        """Obtiene embedding de una proteína desde Milvus"""
        try:
            collection_name = f"{embedding_type}_embeddings_v1"
            
            # Query por protein_id o pdb_id
            expr = f'protein_id == "{protein_id}" or pdb_id == "{protein_id.upper()}"'
            
            result = await self.milvus.query(
                collection_name=collection_name,
                expr=expr,
                output_fields=["embedding"]
            )
            
            if result and len(result) > 0:
                return result[0].get("embedding")
            
            return None
        
        except Exception as e:
            logger.error(f"❌ Error fetching embedding for {protein_id}: {e}")
            return None
    
    def _get_thumbnail_url(self, pdb_id: Optional[str]) -> Optional[str]:
        """Genera URL de thumbnail RCSB PDB"""
        if not pdb_id:
            return None
        return f"https://cdn.rcsb.org/images/structures/{pdb_id.lower()}_assembly-1.jpeg"


# === FACTORY FUNCTION ===

async def create_structure_service(neo4j_client=None, milvus_client=None) -> StructureService:
    """
    Factory function para crear StructureService
    
    Args:
        neo4j_client: Cliente Neo4j (opcional)
        milvus_client: Cliente Milvus (opcional)
    
    Returns:
        StructureService inicializado
    """
    service = StructureService(neo4j_client, milvus_client)
    logger.info("✅ StructureService created")
    return service

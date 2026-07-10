#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM ROUTES - BIOLOGICAL SEMANTIC MEMORY ENDPOINTS
Rutas del servidor MCP para funcionalidades BSM
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Importar el transformer BSM
try:
    from src.bsm.bioschemas_transformer import (
        BioSchemasTransformer, 
        BSMBatchProcessor,
        BioSchemasConfig,
        create_bsm_transformer
    )
    from src.bsm.query_engine import (
        BSMQueryEngine,
        BSMQuery,
        QueryType,
        create_bsm_query_engine
    )
except ImportError:
    from bsm.bioschemas_transformer import (
        BioSchemasTransformer,
        BSMBatchProcessor, 
        BioSchemasConfig,
        create_bsm_transformer
    )
    from bsm.query_engine import (
        BSMQueryEngine,
        BSMQuery,
        QueryType,
        create_bsm_query_engine
    )

# Configuración de logging
logger = logging.getLogger(__name__)

# === MODELOS PYDANTIC PARA BSM ===

class BSMTransformRequest(BaseModel):
    """Request para transformación BSM individual"""
    protein_data: Dict[str, Any] = Field(..., description="Datos de proteína a transformar")
    include_embeddings: bool = Field(True, description="Incluir embeddings en output")
    validate_output: bool = Field(True, description="Validar BioSchemas compliance")

class BSMTransformResponse(BaseModel):
    """Response de transformación BSM"""
    success: bool
    bioschemas_data: Optional[Dict[str, Any]] = None
    validation_passed: bool = False
    processing_time: float = 0.0
    error: Optional[str] = None

class BSMBatchRequest(BaseModel):
    """Request para procesamiento en lote BSM"""
    source_directory: str = Field(..., description="Directorio con resultados PubMedBERT")
    output_directory: str = Field(..., description="Directorio de salida BSM")
    batch_size: int = Field(1000, description="Tamaño de lote para procesamiento")
    include_embeddings: bool = Field(True, description="Incluir embeddings")

class BSMBatchResponse(BaseModel):
    """Response de procesamiento en lote BSM"""
    success: bool
    task_id: str
    files_processed: List[str] = []
    total_proteins: int = 0
    processing_time: float = 0.0
    output_files: List[str] = []
    error: Optional[str] = None

class BSMStatusResponse(BaseModel):
    """Status del sistema BSM"""
    status: str
    version: str
    transformer_ready: bool
    batch_processor_ready: bool
    active_tasks: int
    stats: Dict[str, Any]

class BSMValidationRequest(BaseModel):
    """Request para validación BioSchemas"""
    bioschemas_data: Dict[str, Any] = Field(..., description="Datos BioSchemas a validar")

class BSMValidationResponse(BaseModel):
    """Response de validación BioSchemas"""
    valid: bool
    errors: List[str] = []
    warnings: List[str] = []
    profile_version: str = "0.11"

# === ROUTER BSM ===

bsm_router = APIRouter(prefix="/api/bsm", tags=["BSM - Biological Semantic Memory"])

# Cache global para transformers y procesadores
_transformer_cache: Optional[BioSchemasTransformer] = None
_query_engine_cache: Optional[BSMQueryEngine] = None
_active_batch_tasks: Dict[str, Dict[str, Any]] = {}

async def get_transformer() -> BioSchemasTransformer:
    """Obtiene transformer BSM (cached)"""
    global _transformer_cache
    
    if _transformer_cache is None:
        _transformer_cache = await create_bsm_transformer()
        logger.info("🧬 BSM Transformer initialized")
    
    return _transformer_cache

async def get_query_engine() -> BSMQueryEngine:
    """Obtiene query engine BSM (cached)"""
    global _query_engine_cache
    
    if _query_engine_cache is None:
        _query_engine_cache = await create_bsm_query_engine()
        logger.info("🔍 BSM Query Engine initialized")
    
    return _query_engine_cache

# === ENDPOINTS BSM ===

@bsm_router.get("/status", response_model=BSMStatusResponse)
async def bsm_status():
    """
    📊 Estado del sistema BSM
    
    Obtiene información sobre el estado actual del sistema 
    Biological Semantic Memory incluyendo estadísticas.
    """
    try:
        transformer = await get_transformer()
        stats = transformer.get_transformation_stats()
        
        return BSMStatusResponse(
            status="operational",
            version="1.0.0",
            transformer_ready=True,
            batch_processor_ready=True,
            active_tasks=len(_active_batch_tasks),
            stats=stats
        )
    
    except Exception as e:
        logger.error(f"Error getting BSM status: {e}")
        raise HTTPException(status_code=500, detail=f"BSM status error: {str(e)}")

@bsm_router.post("/transform", response_model=BSMTransformResponse)
async def transform_protein_to_bioschemas(request: BSMTransformRequest):
    """
    🔄 Transformar proteína individual a BioSchemas
    
    Convierte datos de una proteína individual desde formato 
    PubMedBERT a BioSchemas JSON-LD compatible.
    """
    start_time = datetime.now()
    
    try:
        # Crear configuración personalizada
        config = BioSchemasConfig(
            include_embeddings=request.include_embeddings,
            validate_output=request.validate_output
        )
        
        transformer = BioSchemasTransformer(config)
        
        # Transformar proteína
        bioschemas_data = transformer.transform_protein(request.protein_data)
        
        # Validar si está habilitado
        validation_passed = True
        if request.validate_output:
            validation_passed = transformer.validate_bioschemas_output(bioschemas_data)
        
        processing_time = (datetime.now() - start_time).total_seconds()
        
        logger.info(f"✅ Protein transformed successfully in {processing_time:.3f}s")
        
        return BSMTransformResponse(
            success=True,
            bioschemas_data=bioschemas_data,
            validation_passed=validation_passed,
            processing_time=processing_time
        )
    
    except Exception as e:
        logger.error(f"Error transforming protein: {e}")
        processing_time = (datetime.now() - start_time).total_seconds()
        
        return BSMTransformResponse(
            success=False,
            processing_time=processing_time,
            error=str(e)
        )

@bsm_router.post("/batch/start", response_model=BSMBatchResponse)
async def start_batch_processing(request: BSMBatchRequest, background_tasks: BackgroundTasks):
    """
    🚀 Iniciar procesamiento en lote BSM
    
    Inicia procesamiento asíncrono de todos los resultados PubMedBERT
    en un directorio a formato BioSchemas JSON-LD.
    """
    try:
        # Validar directorios
        input_dir = Path(request.source_directory)
        output_dir = Path(request.output_directory)
        
        if not input_dir.exists():
            raise HTTPException(status_code=400, detail=f"Source directory not found: {input_dir}")
        
        # Crear ID de tarea
        task_id = f"bsm_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Configuración para el procesador
        config = BioSchemasConfig(
            batch_size=request.batch_size,
            include_embeddings=request.include_embeddings
        )
        
        # Crear procesador
        processor = BSMBatchProcessor(input_dir, output_dir, config)
        
        # Registrar tarea
        _active_batch_tasks[task_id] = {
            "status": "starting",
            "start_time": datetime.now(),
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "config": config.__dict__
        }
        
        # Ejecutar en background
        background_tasks.add_task(
            _execute_batch_processing,
            task_id,
            processor
        )
        
        logger.info(f"🚀 Started BSM batch processing task: {task_id}")
        
        return BSMBatchResponse(
            success=True,
            task_id=task_id,
            files_processed=[],
            total_proteins=0,
            processing_time=0.0,
            output_files=[]
        )
    
    except Exception as e:
        logger.error(f"Error starting batch processing: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@bsm_router.get("/batch/{task_id}")
async def get_batch_status(task_id: str):
    """
    📊 Estado de tarea de procesamiento en lote
    
    Obtiene el estado actual de una tarea de procesamiento
    en lote BSM por su ID.
    """
    if task_id not in _active_batch_tasks:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    
    task_info = _active_batch_tasks[task_id]
    
    # Calcular tiempo transcurrido
    elapsed_time = (datetime.now() - task_info["start_time"]).total_seconds()
    task_info["elapsed_time"] = elapsed_time
    
    return JSONResponse(content=task_info)

@bsm_router.get("/batch")
async def list_batch_tasks():
    """
    📋 Listar todas las tareas de procesamiento en lote
    
    Obtiene lista de todas las tareas de procesamiento BSM,
    activas y completadas.
    """
    return JSONResponse(content={
        "active_tasks": len(_active_batch_tasks),
        "tasks": _active_batch_tasks
    })

@bsm_router.post("/validate", response_model=BSMValidationResponse)
async def validate_bioschemas_data(request: BSMValidationRequest):
    """
    ✅ Validar datos BioSchemas
    
    Valida que los datos proporcionados cumplan con el
    perfil BioSchemas Protein v0.11.
    """
    try:
        transformer = await get_transformer()
        
        # Validar datos
        is_valid = transformer.validate_bioschemas_output(request.bioschemas_data)
        
        # Lista de errores/warnings (básica por ahora)
        errors = []
        warnings = []
        
        if not is_valid:
            errors.append("BioSchemas validation failed - check required fields")
        
        # Verificaciones adicionales
        if "@context" not in request.bioschemas_data:
            errors.append("Missing @context field")
        
        if "@type" not in request.bioschemas_data:
            errors.append("Missing @type field")
        
        if request.bioschemas_data.get("@type") != "Protein":
            warnings.append("@type is not 'Protein' - may not follow Protein profile")
        
        return BSMValidationResponse(
            valid=is_valid and len(errors) == 0,
            errors=errors,
            warnings=warnings,
            profile_version="0.11"
        )
    
    except Exception as e:
        logger.error(f"Error validating BioSchemas data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@bsm_router.get("/examples")
async def get_bioschemas_examples():
    """
    📚 Ejemplos de formato BioSchemas
    
    Proporciona ejemplos de estructuras BioSchemas válidas
    para referencia y testing.
    """
    examples = {
        "basic_protein": {
            "@context": "https://bioschemas.org/",
            "@type": "Protein",
            "identifier": "uniprot:P69905",
            "name": "Hemoglobin subunit alpha",
            "sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF"
        },
        "protein_with_embeddings": {
            "@context": "https://bioschemas.org/",
            "@type": "Protein", 
            "identifier": "uniprot:P69905",
            "name": "Hemoglobin subunit alpha",
            "hasRepresentation": {
                "@type": "MolecularEntity",
                "identifier": "pubmedbert:P69905_768d",
                "encodingFormat": "application/x-pubmedbert-embedding",
                "embeddings": {
                    "model": "NeuML/pubmedbert-base-embeddings",
                    "dimension": 768,
                    "norm": 14.715084075927734
                }
            }
        },
        "protein_with_gene": {
            "@context": "https://bioschemas.org/",
            "@type": "Protein",
            "identifier": "uniprot:P69905", 
            "name": "Hemoglobin subunit alpha",
            "encodedBy": {
                "@type": "Gene",
                "identifier": "ensembl:ENSG00000206172",
                "name": "HBA1"
            }
        }
    }
    
    return JSONResponse(content={
        "description": "BioSchemas Protein Profile v0.11 examples for BSM",
        "examples": examples,
        "documentation": "https://bioschemas.org/profiles/Protein/0.11-RELEASE"
    })

@bsm_router.post("/convert/pubmedbert")
async def convert_pubmedbert_results(
    file: UploadFile = File(..., description="PubMedBERT results JSON file"),
    include_embeddings: bool = Query(True, description="Include embedding data"),
    validate_output: bool = Query(True, description="Validate BioSchemas output")
):
    """
    📁 Convertir archivo de resultados PubMedBERT
    
    Sube y convierte un archivo JSON de resultados PubMedBERT
    a formato BioSchemas JSON-LD.
    """
    try:
        # Leer archivo subido
        content = await file.read()
        data = json.loads(content.decode('utf-8'))
        
        # Crear configuración
        config = BioSchemasConfig(
            include_embeddings=include_embeddings,
            validate_output=validate_output
        )
        
        transformer = BioSchemasTransformer(config)
        
        # Extraer proteínas (lógica simplificada)
        proteins = []
        if "tests" in data and "protein_embedding_generation" in data["tests"]:
            test_proteins = data["tests"]["protein_embedding_generation"].get("test_proteins", {})
            for protein_id, protein_data in test_proteins.items():
                proteins.append({
                    "id": protein_id,
                    "name": f"Protein_{protein_id}",
                    **protein_data
                })
        
        # Transformar proteínas
        bioschemas_proteins = await transformer.transform_batch(proteins)
        
        # Crear documento de respuesta
        result = {
            "conversion_info": {
                "source_file": file.filename,
                "timestamp": datetime.now().isoformat(),
                "proteins_converted": len(bioschemas_proteins),
                "transformer_stats": transformer.get_transformation_stats()
            },
            "bioschemas_data": bioschemas_proteins
        }
        
        logger.info(f"✅ Converted {len(bioschemas_proteins)} proteins from {file.filename}")
        
        return JSONResponse(content=result)

@bsm_router.post("/query")
async def query_bsm_system(request: Dict[str, Any]):
    """
    🔍 Consultar sistema BSM
    
    Procesa consultas en lenguaje natural usando el motor
    dual-core que combina Neo4j y Milvus.
    """
    try:
        query_text = request.get("query", "")
        query_type = request.get("type", "hybrid_query")
        
        if not query_text:
            raise HTTPException(status_code=400, detail="Query text required")
        
        # Obtener query engine
        engine = await get_query_engine()
        
        # Crear consulta BSM
        bsm_query = BSMQuery(
            query_text=query_text,
            query_type=QueryType(query_type),
            parameters=request.get("parameters", {}),
            filters=request.get("filters", {})
        )
        
        # Procesar consulta
        result = await engine.process_query(bsm_query)
        
        # Formatear respuesta
        response = {
            "query_id": result.query_id,
            "query_text": query_text,
            "query_type": result.query_type.value,
            "execution_time": result.execution_time,
            "confidence_score": result.confidence_score,
            "results": {
                "semantic_results": [
                    {
                        "protein_id": r.protein_id,
                        "name": r.name,
                        "similarity_score": r.similarity_score,
                        "metadata": r.metadata
                    } for r in result.semantic_results
                ],
                "graph_results": result.graph_results,
                "combined_results": result.combined_results
            },
            "metadata": result.metadata
        }
        
        logger.info(f"🔍 BSM query processed: {query_text[:50]}...")
        
        return JSONResponse(content=response)
    
    except Exception as e:
        logger.error(f"Error processing BSM query: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@bsm_router.get("/query/suggestions")
async def get_query_suggestions(q: str = Query(..., description="Partial query text")):
    """
    💡 Obtener sugerencias de consulta
    
    Proporciona sugerencias de completación para consultas
    basadas en patrones comunes y el texto parcial.
    """
    try:
        engine = await get_query_engine()
        suggestions = await engine.get_query_suggestions(q)
        
        return JSONResponse(content={
            "partial_query": q,
            "suggestions": suggestions,
            "count": len(suggestions)
        })
    
    except Exception as e:
        logger.error(f"Error getting query suggestions: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@bsm_router.get("/insights/protein/{protein_id}")
async def get_protein_insights(protein_id: str):
    """
    🧬 Obtener insights completos de proteína
    
    Combina información de Neo4j y Milvus para proporcionar
    análisis completo de una proteína específica.
    """
    try:
        engine = await get_query_engine()
        
        # Crear consulta de análisis de proteína
        query = f"Analyze protein {protein_id}"
        result = await engine.process_query(query)
        
        # Obtener pathways relacionados
        pathways = await engine.discover_protein_pathways(protein_id)
        
        response = {
            "protein_id": protein_id,
            "analysis_results": result.combined_results,
            "pathways": pathways,
            "confidence_score": result.confidence_score,
            "execution_time": result.execution_time
        }
        
        return JSONResponse(content=response)
    
    except Exception as e:
        logger.error(f"Error getting protein insights for {protein_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@bsm_router.get("/system/health")
async def bsm_system_health():
    """
    🏥 Estado de salud del sistema BSM
    
    Verifica el estado de todos los componentes del sistema
    BSM incluyendo Neo4j, Milvus y el motor de consultas.
    """
    try:
        health_status = {
            "timestamp": datetime.now().isoformat(),
            "version": "1.0.0",
            "components": {}
        }
        
        # Verificar transformer
        try:
            transformer = await get_transformer()
            stats = transformer.get_transformation_stats()
            health_status["components"]["transformer"] = {
                "status": "healthy",
                "stats": stats
            }
        except Exception as e:
            health_status["components"]["transformer"] = {
                "status": "error",
                "error": str(e)
            }
        
        # Verificar query engine
        try:
            engine = await get_query_engine()
            health_status["components"]["query_engine"] = {
                "status": "healthy",
                "cache_initialized": _query_engine_cache is not None
            }
        except Exception as e:
            health_status["components"]["query_engine"] = {
                "status": "error", 
                "error": str(e)
            }
        
        # Estado general
        all_healthy = all(
            comp.get("status") == "healthy" 
            for comp in health_status["components"].values()
        )
        
        health_status["overall_status"] = "healthy" if all_healthy else "degraded"
        
        return JSONResponse(content=health_status)
    
    except Exception as e:
        logger.error(f"Error checking BSM system health: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# === FUNCIONES AUXILIARES ===

async def _execute_batch_processing(task_id: str, processor: BSMBatchProcessor):
    """Ejecuta procesamiento en lote en background"""
    try:
        # Actualizar estado
        _active_batch_tasks[task_id]["status"] = "processing"
        
        # Ejecutar procesamiento
        results = await processor.process_pubmedbert_results()
        
        # Actualizar estado final
        _active_batch_tasks[task_id].update({
            "status": "completed",
            "end_time": datetime.now(),
            "results": results,
            "files_processed": list(results.keys()),
            "total_proteins": sum(r.get("proteins_count", 0) for r in results.values())
        })
        
        logger.info(f"✅ Batch processing completed: {task_id}")
    
    except Exception as e:
        logger.error(f"Error in batch processing {task_id}: {e}")
        _active_batch_tasks[task_id].update({
            "status": "failed",
            "end_time": datetime.now(),
            "error": str(e)
        })

# === EXPORTAR ROUTER ===

__all__ = ["bsm_router"]
    
    except Exception as e:
        logger.error(f"Error converting file {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# === FUNCIONES AUXILIARES ===

async def _execute_batch_processing(task_id: str, processor: BSMBatchProcessor):
    """Ejecuta procesamiento en lote en background"""
    try:
        # Actualizar estado
        _active_batch_tasks[task_id]["status"] = "processing"
        
        # Ejecutar procesamiento
        results = await processor.process_pubmedbert_results()
        
        # Actualizar estado final
        _active_batch_tasks[task_id].update({
            "status": "completed",
            "end_time": datetime.now(),
            "results": results,
            "files_processed": list(results.keys()),
            "total_proteins": sum(r.get("proteins_count", 0) for r in results.values())
        })
        
        logger.info(f"✅ Batch processing completed: {task_id}")
    
    except Exception as e:
        logger.error(f"Error in batch processing {task_id}: {e}")
        _active_batch_tasks[task_id].update({
            "status": "failed",
            "end_time": datetime.now(),
            "error": str(e)
        })

# === EXPORTAR ROUTER ===

__all__ = ["bsm_router"]
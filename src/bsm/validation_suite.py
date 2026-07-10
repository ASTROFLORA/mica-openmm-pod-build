#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
✅ BSM VALIDATION SUITE
Suite completa de validación y testing para Biological Semantic Memory
"""

import asyncio
import json
import logging
import time
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path
import unittest
from unittest.mock import Mock, AsyncMock

from .config import BSMConfig, get_testing_config
from .bioschemas_transformer import BioSchemasTransformer, create_bsm_transformer
from .neo4j_integration import BSMNeo4jIntegration, ProteinNode, BioSchemasNode
from .milvus_integration import BSMMilvusIntegration, ProteinEmbedding
from .query_engine import BSMQueryEngine, BSMQuery, QueryType

logger = logging.getLogger(__name__)

# === MODELOS DE VALIDACIÓN ===

@dataclass
class ValidationResult:
    """Resultado de validación"""
    test_name: str
    passed: bool
    execution_time: float
    error_message: Optional[str] = None
    details: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}

@dataclass
class BenchmarkResult:
    """Resultado de benchmark"""
    operation: str
    iterations: int
    total_time: float
    avg_time: float
    min_time: float
    max_time: float
    throughput: float
    success_rate: float

# === VALIDADORES INDIVIDUALES ===

class BioSchemasValidator:
    """Validador para transformaciones BioSchemas"""
    
    @staticmethod
    async def validate_transformer() -> ValidationResult:
        """Valida funcionamiento básico del transformer"""
        start_time = time.time()
        
        try:
            # Crear transformer
            transformer = await create_bsm_transformer()
            
            # Datos de prueba
            test_protein = {
                "id": "test_protein_001",
                "name": "Test Protein Alpha",
                "sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF",
                "organism": "Homo sapiens",
                "function": "Test enzymatic activity",
                "embeddings": np.random.rand(768).tolist()
            }
            
            # Transformar
            bioschemas_data = transformer.transform_protein(test_protein)
            
            # Validaciones
            assert "@context" in bioschemas_data
            assert "@type" in bioschemas_data
            assert bioschemas_data["@type"] == "Protein"
            assert "identifier" in bioschemas_data
            assert "name" in bioschemas_data
            
            # Validar compliance
            is_valid = transformer.validate_bioschemas_output(bioschemas_data)
            assert is_valid, "BioSchemas validation failed"
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="bioschemas_transformer",
                passed=True,
                execution_time=execution_time,
                details={
                    "transformed_fields": len(bioschemas_data),
                    "context": bioschemas_data["@context"],
                    "type": bioschemas_data["@type"]
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="bioschemas_transformer",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )
    
    @staticmethod
    async def validate_batch_processing() -> ValidationResult:
        """Valida procesamiento en lote"""
        start_time = time.time()
        
        try:
            transformer = await create_bsm_transformer()
            
            # Crear lote de prueba
            test_proteins = []
            for i in range(100):
                test_proteins.append({
                    "id": f"test_protein_{i:03d}",
                    "name": f"Test Protein {i}",
                    "sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF",
                    "embeddings": np.random.rand(768).tolist()
                })
            
            # Procesar en lote
            results = await transformer.transform_batch(test_proteins)
            
            # Validaciones
            assert len(results) == len(test_proteins)
            
            for result in results:
                assert "@context" in result
                assert "@type" in result
                assert result["@type"] == "Protein"
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="batch_processing",
                passed=True,
                execution_time=execution_time,
                details={
                    "batch_size": len(test_proteins),
                    "processed_count": len(results),
                    "avg_processing_time": execution_time / len(test_proteins)
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="batch_processing",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )

class Neo4jValidator:
    """Validador para integración Neo4j"""
    
    @staticmethod
    async def validate_connection() -> ValidationResult:
        """Valida conexión a Neo4j"""
        start_time = time.time()
        
        try:
            config = get_testing_config()
            integration = BSMNeo4jIntegration(config)
            
            # Intentar conexión
            await integration.connect()
            
            # Verificar schema
            await integration.setup_schema()
            
            # Cerrar conexión
            await integration.disconnect()
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="neo4j_connection",
                passed=True,
                execution_time=execution_time,
                details={
                    "uri": config.neo4j.uri,
                    "database": config.neo4j.database
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="neo4j_connection",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )
    
    @staticmethod
    async def validate_crud_operations() -> ValidationResult:
        """Valida operaciones CRUD"""
        start_time = time.time()
        
        try:
            config = get_testing_config()
            integration = BSMNeo4jIntegration(config)
            await integration.initialize()
            
            # Crear proteína de prueba
            test_protein = ProteinNode(
                identifier="test_crud_protein",
                name="Test CRUD Protein",
                sequence="TESTSEQUENCE",
                organism="Test organism",
                function="Test function"
            )
            
            # CREATE
            created = await integration.create_protein_node(test_protein)
            assert created, "Failed to create protein node"
            
            # READ
            retrieved = await integration.get_protein_by_id("test_crud_protein")
            assert retrieved is not None, "Failed to retrieve protein"
            assert retrieved["name"] == "Test CRUD Protein"
            
            # SEARCH
            search_results = await integration.search_proteins("Test CRUD", limit=10)
            assert len(search_results) > 0, "Search failed"
            
            await integration.disconnect()
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="neo4j_crud",
                passed=True,
                execution_time=execution_time,
                details={
                    "operations_tested": ["create", "read", "search"],
                    "protein_created": test_protein.identifier
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="neo4j_crud",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )

class MilvusValidator:
    """Validador para integración Milvus"""
    
    @staticmethod
    async def validate_connection() -> ValidationResult:
        """Valida conexión a Milvus"""
        start_time = time.time()
        
        try:
            config = get_testing_config()
            config.milvus.collection_name = "test_bsm_validation"
            
            integration = BSMMilvusIntegration(config)
            await integration.initialize()
            
            # Verificar estadísticas
            stats = await integration.get_collection_stats()
            assert "collection_name" in stats
            
            await integration.disconnect()
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="milvus_connection",
                passed=True,
                execution_time=execution_time,
                details={
                    "collection": config.milvus.collection_name,
                    "dimension": config.milvus.dimension,
                    "stats": stats
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="milvus_connection",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )
    
    @staticmethod
    async def validate_embedding_operations() -> ValidationResult:
        """Valida operaciones con embeddings"""
        start_time = time.time()
        
        try:
            config = get_testing_config()
            config.milvus.collection_name = "test_bsm_embeddings"
            
            integration = BSMMilvusIntegration(config)
            await integration.initialize()
            
            # Embedding de prueba
            test_embedding = ProteinEmbedding(
                protein_id="test_embedding_protein",
                name="Test Embedding Protein",
                embedding=np.random.rand(768),
                sequence="TESTEMBEDDINGSEQUENCE",
                metadata={"test": True}
            )
            
            # INSERT
            inserted = await integration.insert_protein_embedding(test_embedding)
            assert inserted, "Failed to insert embedding"
            
            # SEARCH by ID
            retrieved = await integration.search_by_protein_id("test_embedding_protein")
            assert retrieved is not None, "Failed to retrieve by ID"
            
            # SEARCH by similarity
            similar = await integration.search_similar_proteins(
                test_embedding.embedding,
                top_k=5,
                similarity_threshold=0.5
            )
            assert len(similar) > 0, "Similarity search failed"
            
            await integration.disconnect()
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="milvus_embeddings",
                passed=True,
                execution_time=execution_time,
                details={
                    "embedding_inserted": test_embedding.protein_id,
                    "similar_found": len(similar),
                    "embedding_dimension": len(test_embedding.embedding)
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="milvus_embeddings",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )

class QueryEngineValidator:
    """Validador para motor de consultas"""
    
    @staticmethod
    async def validate_query_processing() -> ValidationResult:
        """Valida procesamiento de consultas"""
        start_time = time.time()
        
        try:
            # Usar mocks para evitar dependencias externas
            mock_neo4j = Mock()
            mock_neo4j.search_proteins = AsyncMock(return_value=[
                {"identifier": "test_protein", "name": "Test Protein"}
            ])
            mock_neo4j.get_protein_relationships = AsyncMock(return_value=[])
            
            mock_milvus = Mock()
            mock_milvus.search_by_text = AsyncMock(return_value=[
                {"protein_id": "test_protein", "name": "Test Protein", "embedding": np.random.rand(768).tolist()}
            ])
            mock_milvus.search_similar_proteins = AsyncMock(return_value=[])
            
            config = get_testing_config()
            engine = BSMQueryEngine(mock_neo4j, mock_milvus, config)
            
            # Test diferentes tipos de consultas
            test_queries = [
                "Find proteins similar to hemoglobin",
                "What proteins interact with BRCA1",
                "Analyze protein P53",
                "What is the function of insulin"
            ]
            
            results = []
            for query_text in test_queries:
                result = await engine.process_query(query_text)
                results.append(result)
                assert result.query_id is not None
                assert result.execution_time > 0
            
            execution_time = time.time() - start_time
            
            return ValidationResult(
                test_name="query_processing",
                passed=True,
                execution_time=execution_time,
                details={
                    "queries_tested": len(test_queries),
                    "results_generated": len(results),
                    "avg_query_time": execution_time / len(test_queries)
                }
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return ValidationResult(
                test_name="query_processing",
                passed=False,
                execution_time=execution_time,
                error_message=str(e)
            )

# === SUITE DE VALIDACIÓN PRINCIPAL ===

class BSMValidationSuite:
    """Suite principal de validación BSM"""
    
    def __init__(self, config: Optional[BSMConfig] = None):
        """
        Inicializa suite de validación
        
        Args:
            config: Configuración BSM (opcional)
        """
        self.config = config or get_testing_config()
        self.results: List[ValidationResult] = []
        
    async def run_all_validations(self) -> Dict[str, Any]:
        """
        Ejecuta todas las validaciones
        
        Returns:
            Resumen de resultados
        """
        logger.info("🧪 Starting BSM validation suite...")
        start_time = time.time()
        
        # Lista de validaciones a ejecutar
        validations = [
            ("BioSchemas Transformer", BioSchemasValidator.validate_transformer),
            ("BioSchemas Batch Processing", BioSchemasValidator.validate_batch_processing),
            ("Neo4j Connection", Neo4jValidator.validate_connection),
            ("Neo4j CRUD Operations", Neo4jValidator.validate_crud_operations),
            ("Milvus Connection", MilvusValidator.validate_connection),
            ("Milvus Embeddings", MilvusValidator.validate_embedding_operations),
            ("Query Engine", QueryEngineValidator.validate_query_processing)
        ]
        
        # Ejecutar validaciones
        for validation_name, validation_func in validations:
            logger.info(f"🔍 Running validation: {validation_name}")
            
            try:
                result = await validation_func()
                self.results.append(result)
                
                if result.passed:
                    logger.info(f"✅ {validation_name}: PASSED ({result.execution_time:.3f}s)")
                else:
                    logger.error(f"❌ {validation_name}: FAILED - {result.error_message}")
                    
            except Exception as e:
                logger.error(f"💥 {validation_name}: EXCEPTION - {e}")
                self.results.append(ValidationResult(
                    test_name=validation_name.lower().replace(" ", "_"),
                    passed=False,
                    execution_time=0.0,
                    error_message=str(e)
                ))
        
        total_time = time.time() - start_time
        
        # Generar resumen
        summary = self._generate_summary(total_time)
        
        logger.info(f"🧪 Validation suite completed in {total_time:.3f}s")
        logger.info(f"📊 Results: {summary['passed_count']}/{summary['total_count']} passed")
        
        return summary
    
    def _generate_summary(self, total_time: float) -> Dict[str, Any]:
        """Genera resumen de validaciones"""
        passed_count = sum(1 for r in self.results if r.passed)
        failed_count = len(self.results) - passed_count
        
        return {
            "total_count": len(self.results),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "success_rate": (passed_count / len(self.results)) * 100 if self.results else 0,
            "total_execution_time": total_time,
            "avg_test_time": total_time / len(self.results) if self.results else 0,
            "results": [
                {
                    "test_name": r.test_name,
                    "passed": r.passed,
                    "execution_time": r.execution_time,
                    "error_message": r.error_message,
                    "details": r.details
                }
                for r in self.results
            ],
            "timestamp": datetime.now().isoformat()
        }
    
    async def run_performance_benchmarks(self) -> Dict[str, BenchmarkResult]:
        """
        Ejecuta benchmarks de rendimiento
        
        Returns:
            Resultados de benchmarks
        """
        logger.info("⚡ Starting performance benchmarks...")
        
        benchmarks = {}
        
        # Benchmark de transformación BioSchemas
        try:
            transformer = await create_bsm_transformer()
            
            test_protein = {
                "id": "benchmark_protein",
                "name": "Benchmark Protein",
                "sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF",
                "embeddings": np.random.rand(768).tolist()
            }
            
            times = []
            success_count = 0
            iterations = 100
            
            for i in range(iterations):
                start = time.time()
                try:
                    result = transformer.transform_protein(test_protein)
                    success_count += 1
                except:
                    pass
                times.append(time.time() - start)
            
            total_time = sum(times)
            benchmarks["bioschemas_transformation"] = BenchmarkResult(
                operation="bioschemas_transformation",
                iterations=iterations,
                total_time=total_time,
                avg_time=total_time / iterations,
                min_time=min(times),
                max_time=max(times),
                throughput=iterations / total_time,
                success_rate=(success_count / iterations) * 100
            )
            
        except Exception as e:
            logger.warning(f"⚠️ BioSchemas benchmark failed: {e}")
        
        return benchmarks
    
    def save_results(self, output_path: str):
        """
        Guarda resultados a archivo
        
        Args:
            output_path: Ruta del archivo de salida
        """
        try:
            summary = self._generate_summary(0.0)  # Total time ya calculado
            
            with open(output_path, 'w') as f:
                json.dump(summary, f, indent=2, default=str)
            
            logger.info(f"💾 Validation results saved to: {output_path}")
            
        except Exception as e:
            logger.error(f"❌ Failed to save results: {e}")

# === UTILIDADES DE TESTING ===

class BSMTestRunner:
    """Runner para tests automatizados"""
    
    @staticmethod
    async def run_quick_validation() -> bool:
        """
        Ejecuta validación rápida
        
        Returns:
            bool: True si todas las validaciones básicas pasan
        """
        suite = BSMValidationSuite()
        
        # Solo validaciones básicas
        basic_validations = [
            BioSchemasValidator.validate_transformer,
        ]
        
        for validation in basic_validations:
            result = await validation()
            if not result.passed:
                return False
        
        return True
    
    @staticmethod
    async def run_integration_tests() -> Dict[str, Any]:
        """
        Ejecuta tests de integración completos
        
        Returns:
            Resultados detallados
        """
        suite = BSMValidationSuite()
        return await suite.run_all_validations()

# === EXPORTACIONES ===

__all__ = [
    "BSMValidationSuite",
    "ValidationResult",
    "BenchmarkResult",
    "BSMTestRunner",
    "BioSchemasValidator",
    "Neo4jValidator", 
    "MilvusValidator",
    "QueryEngineValidator"
]

# === MAIN PARA TESTING ===

async def main():
    """Función principal para ejecutar validaciones"""
    suite = BSMValidationSuite()
    
    # Ejecutar validaciones
    results = await suite.run_all_validations()
    
    # Ejecutar benchmarks
    benchmarks = await suite.run_performance_benchmarks()
    
    # Guardar resultados
    suite.save_results("bsm_validation_results.json")
    
    # Mostrar resumen
    print(f"\n🧪 BSM VALIDATION SUITE RESULTS")
    print(f"{'='*50}")
    print(f"Total Tests: {results['total_count']}")
    print(f"Passed: {results['passed_count']}")
    print(f"Failed: {results['failed_count']}")
    print(f"Success Rate: {results['success_rate']:.1f}%")
    print(f"Total Time: {results['total_execution_time']:.3f}s")
    
    if benchmarks:
        print(f"\n⚡ PERFORMANCE BENCHMARKS")
        print(f"{'='*50}")
        for name, benchmark in benchmarks.items():
            print(f"{name}: {benchmark.throughput:.1f} ops/sec")

if __name__ == "__main__":
    asyncio.run(main())
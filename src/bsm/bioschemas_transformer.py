#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM - BIOSCHEMAS TRANSFORMER MODULE
Transformador de resultados PubMedBERT a formato BioSchemas JSON-LD
Biological Semantic Memory Implementation
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Union
from pathlib import Path
import asyncio
from dataclasses import dataclass

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class BioSchemasConfig:
    """Configuración para transformación BioSchemas"""
    version: str = "0.11"
    context_url: str = "https://bioschemas.org/"
    include_embeddings: bool = True
    validate_output: bool = True
    batch_size: int = 1000

class BioSchemasTransformer:
    """
    🔬 Transformador principal PubMedBERT → BioSchemas JSON-LD
    
    Convierte resultados de validación PubMedBERT a formato semántico
    compatible con BioSchemas Profile v0.11 para Biological Semantic Memory
    """
    
    def __init__(self, config: Optional[BioSchemasConfig] = None):
        self.config = config or BioSchemasConfig()
        self.context = self._create_jsonld_context()
        self.stats = {
            "proteins_processed": 0,
            "transformation_time": 0.0,
            "validation_errors": 0,
            "start_time": None
        }
        logger.info("🧬 BioSchemasTransformer initialized")
    
    def _create_jsonld_context(self) -> Dict[str, Any]:
        """Crea contexto JSON-LD optimizado para BSM"""
        return {
            "@context": {
                "@version": 1.1,
                "@vocab": "https://schema.org/",
                "bioschemas": "https://bioschemas.org/",
                "uniprot": "https://www.uniprot.org/uniprot/",
                "pdb": "https://www.rcsb.org/structure/",
                "pubmedbert": "https://huggingface.co/NeuML/pubmedbert-base-embeddings/",
                "mesh": "https://meshb.nlm.nih.gov/record/ui?ui=",
                "ensembl": "https://www.ensembl.org/id/",
                
                # BioSchemas types
                "Protein": "bioschemas:Protein",
                "Gene": "bioschemas:Gene",
                "MolecularEntity": "bioschemas:MolecularEntity",
                "SequenceAnnotation": "bioschemas:SequenceAnnotation",
                
                # Properties
                "identifier": "@id",
                "hasRepresentation": "bioschemas:hasRepresentation",
                "encodedBy": "bioschemas:encodedBy",
                "associatedDisease": "bioschemas:associatedDisease",
                "hasBioChemEntityPart": "bioschemas:hasBioChemEntityPart",
                
                # Embeddings context
                "embeddings": {
                    "@type": "@json",
                    "@context": {
                        "model": "schema:model",
                        "dimension": "schema:dimension",
                        "norm": "schema:norm",
                        "generatedAt": "schema:dateCreated",
                        "similarity_cluster": "schema:category"
                    }
                }
            }
        }
    
    def transform_protein(self, protein_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        🔄 Transforma datos de proteína individual a BioSchemas
        
        Args:
            protein_data: Datos de proteína desde PubMedBERT results
            
        Returns:
            Dict con formato BioSchemas JSON-LD
        """
        try:
            # Extraer información básica
            protein_id = protein_data.get('id', protein_data.get('identifier', 'unknown'))
            protein_name = protein_data.get('name', f"Protein {protein_id}")
            
            # Estructura base BioSchemas
            bioschemas_protein = {
                **self.context,
                "@type": "Protein",
                "identifier": f"uniprot:{protein_id}",
                "name": protein_name,
                "dateModified": datetime.now().isoformat(),
            }
            
            # Añadir secuencia si disponible
            if 'sequence' in protein_data:
                bioschemas_protein["sequence"] = protein_data['sequence']
            
            # Añadir nombres alternativos
            if 'alternateName' in protein_data or 'alternate_names' in protein_data:
                alt_names = protein_data.get('alternateName', protein_data.get('alternate_names', []))
                if isinstance(alt_names, str):
                    alt_names = [alt_names]
                bioschemas_protein["alternateName"] = alt_names
            
            # Añadir representación de embeddings
            if self.config.include_embeddings and 'embedding' in protein_data:
                bioschemas_protein["hasRepresentation"] = self._create_embedding_representation(
                    protein_data, protein_id
                )
            
            # Añadir propiedades de clustering si disponibles
            if 'similarity_cluster' in protein_data:
                bioschemas_protein["additionalProperty"] = self._create_clustering_properties(protein_data)
            
            # Añadir gene information si disponible
            if 'gene_id' in protein_data or 'encodedBy' in protein_data:
                bioschemas_protein["encodedBy"] = self._create_gene_reference(protein_data)
            
            # Añadir structural information si disponible
            if 'structure_id' in protein_data or 'pdb_id' in protein_data:
                bioschemas_protein["hasStructure"] = self._create_structure_reference(protein_data)
            
            self.stats["proteins_processed"] += 1
            return bioschemas_protein
            
        except Exception as e:
            logger.error(f"Error transforming protein {protein_id}: {e}")
            self.stats["validation_errors"] += 1
            raise
    
    def _create_embedding_representation(self, protein_data: Dict[str, Any], protein_id: str) -> Dict[str, Any]:
        """Crea representación de embeddings siguiendo MolecularEntity profile"""
        embedding_data = protein_data.get('embedding', {})
        
        return {
            "@type": "MolecularEntity", 
            "identifier": f"pubmedbert:{protein_id}_768d",
            "name": f"PubMedBERT embedding for {protein_id}",
            "encodingFormat": "application/x-pubmedbert-embedding",
            "contentSize": "768",
            "embeddings": {
                "model": "NeuML/pubmedbert-base-embeddings",
                "dimension": 768,
                "norm": protein_data.get('embedding_norm', 0.0),
                "generatedAt": datetime.now().isoformat(),
                "similarity_cluster": protein_data.get('similarity_cluster', 'unknown')
            },
            "additionalProperty": [
                {
                    "@type": "PropertyValue",
                    "name": "embedding_time",
                    "value": protein_data.get('embedding_time', 0.0),
                    "unitText": "seconds"
                },
                {
                    "@type": "PropertyValue",
                    "name": "sequence_length", 
                    "value": protein_data.get('sequence_length', len(protein_data.get('sequence', ''))),
                    "unitText": "amino_acids"
                }
            ]
        }
    
    def _create_clustering_properties(self, protein_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Crea propiedades de clustering para la proteína"""
        properties = []
        
        if 'similarity_cluster' in protein_data:
            properties.append({
                "@type": "PropertyValue",
                "name": "functional_cluster",
                "value": protein_data['similarity_cluster'],
                "description": "Biological functional cluster determined by PubMedBERT embeddings"
            })
        
        if 'clustering_similarity' in protein_data:
            properties.append({
                "@type": "PropertyValue", 
                "name": "cluster_similarity_score",
                "value": protein_data['clustering_similarity'],
                "description": "Cosine similarity within functional cluster"
            })
        
        return properties
    
    def _create_gene_reference(self, protein_data: Dict[str, Any]) -> Dict[str, Any]:
        """Crea referencia a gen siguiendo Gene profile"""
        gene_id = protein_data.get('gene_id', protein_data.get('encodedBy'))
        
        return {
            "@type": "Gene",
            "identifier": f"ensembl:{gene_id}" if gene_id else "unknown:gene",
            "name": protein_data.get('gene_name', f"Gene for {protein_data.get('id', 'unknown')}")
        }
    
    def _create_structure_reference(self, protein_data: Dict[str, Any]) -> Dict[str, Any]:
        """Crea referencia a estructura siguiendo ProteinStructure profile"""
        structure_id = protein_data.get('structure_id', protein_data.get('pdb_id'))
        
        return {
            "@type": "ProteinStructure",
            "identifier": f"pdb:{structure_id}" if structure_id else "predicted:structure",
            "name": f"Structure for {protein_data.get('id', 'unknown')}",
            "additionalProperty": [
                {
                    "@type": "PropertyValue",
                    "name": "structure_source",
                    "value": "experimental" if structure_id else "predicted"
                }
            ]
        }
    
    async def transform_batch(self, protein_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        🔄 Transforma lote de proteínas de manera asíncrona
        
        Args:
            protein_list: Lista de datos de proteínas
            
        Returns:
            Lista de proteínas transformadas a BioSchemas
        """
        start_time = datetime.now()
        self.stats["start_time"] = start_time
        
        logger.info(f"🔄 Starting batch transformation of {len(protein_list)} proteins")
        
        # Procesar en lotes para evitar sobrecarga de memoria
        batch_size = self.config.batch_size
        transformed_proteins = []
        
        for i in range(0, len(protein_list), batch_size):
            batch = protein_list[i:i + batch_size]
            logger.info(f"📊 Processing batch {i//batch_size + 1}/{(len(protein_list)-1)//batch_size + 1}")
            
            # Transformar batch actual
            batch_results = []
            for protein_data in batch:
                try:
                    transformed = self.transform_protein(protein_data)
                    batch_results.append(transformed)
                except Exception as e:
                    logger.error(f"Error in batch processing: {e}")
                    continue
            
            transformed_proteins.extend(batch_results)
            
            # Pequeña pausa para no saturar
            await asyncio.sleep(0.1)
        
        end_time = datetime.now()
        self.stats["transformation_time"] = (end_time - start_time).total_seconds()
        
        logger.info(f"✅ Batch transformation completed: {len(transformed_proteins)} proteins in {self.stats['transformation_time']:.2f}s")
        
        return transformed_proteins
    
    def validate_bioschemas_output(self, bioschemas_data: Dict[str, Any]) -> bool:
        """
        ✅ Valida que el output cumple con BioSchemas Profile v0.11
        
        Args:
            bioschemas_data: Datos en formato BioSchemas
            
        Returns:
            bool: True si es válido
        """
        try:
            # Validaciones obligatorias Profile v0.11
            required_fields = ["@context", "@type", "identifier", "name"]
            
            for field in required_fields:
                if field not in bioschemas_data:
                    logger.error(f"Missing required field: {field}")
                    return False
            
            # Validar tipo
            if bioschemas_data["@type"] != "Protein":
                logger.error(f"Invalid @type: {bioschemas_data['@type']}")
                return False
            
            # Validar identifier format
            identifier = bioschemas_data["identifier"]
            if not isinstance(identifier, str) or not (":" in identifier):
                logger.error(f"Invalid identifier format: {identifier}")
                return False
            
            # Validar embeddings si están presentes
            if "hasRepresentation" in bioschemas_data:
                embedding_rep = bioschemas_data["hasRepresentation"]
                if embedding_rep.get("@type") != "MolecularEntity":
                    logger.error("Invalid embedding representation type")
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return False
    
    def get_transformation_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas de transformación"""
        stats_copy = dict(self.stats)
        start_time = stats_copy.get("start_time")
        if isinstance(start_time, datetime):
            stats_copy["start_time"] = start_time.isoformat()

        return {
            **stats_copy,
            "processing_rate": (
                stats_copy["proteins_processed"] / stats_copy["transformation_time"]
                if stats_copy["transformation_time"] > 0 else 0
            ),
            "success_rate": (
                (stats_copy["proteins_processed"] - stats_copy["validation_errors"]) /
                max(stats_copy["proteins_processed"], 1)
            ) * 100
        }

class BSMBatchProcessor:
    """
    📦 Procesador de lotes para transformación masiva
    Maneja la transformación de todos los resultados PubMedBERT a BSM
    """
    
    def __init__(self, input_dir: Path, output_dir: Path, config: Optional[BioSchemasConfig] = None):
        self.input_dir = Path(input_dir) 
        self.output_dir = Path(output_dir)
        self.config = config or BioSchemasConfig()
        self.transformer = BioSchemasTransformer(self.config)
        
        # Crear directorio de salida
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📦 BSMBatchProcessor initialized")
        logger.info(f"📁 Input: {self.input_dir}")
        logger.info(f"📁 Output: {self.output_dir}")
    
    async def process_pubmedbert_results(self):
        """
        🚀 Procesa todos los resultados PubMedBERT a formato BSM
        """
        logger.info("🚀 Starting PubMedBERT → BSM batch processing")
        
        # Archivos de resultados PubMedBERT a procesar
        result_files = [
            "pubmedbert_rag_integration_results.json",
            "pubmedbert_clustering_validation_results.json", 
            "scientific_validation_results.json",
            "pubmedbert_integration_plan.json"
        ]
        
        all_results = {}
        
        for file_name in result_files:
            input_path = self.input_dir / file_name
            
            if not input_path.exists():
                logger.warning(f"⚠️ File not found: {input_path}")
                continue
                
            logger.info(f"📖 Processing {file_name}")
            
            # Cargar datos
            with open(input_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Extraer información de proteínas
            proteins = self._extract_proteins_from_results(data, file_name)
            
            if proteins:
                # Transformar a BioSchemas
                bioschemas_proteins = await self.transformer.transform_batch(proteins)
                
                # Guardar resultados transformados
                output_path = self.output_dir / f"bioschemas_{file_name}"
                await self._save_bioschemas_results(bioschemas_proteins, output_path)
                
                all_results[file_name] = {
                    "source_file": str(input_path),
                    "output_file": str(output_path),
                    "proteins_count": len(proteins),
                    "bioschemas_count": len(bioschemas_proteins),
                    "processing_time": self.transformer.get_transformation_stats()["transformation_time"]
                }
        
        # Guardar resumen completo
        summary_path = self.output_dir / "bsm_transformation_summary.json"
        await self._save_processing_summary(all_results, summary_path)
        
        logger.info("✅ BSM batch processing completed successfully")
        return all_results
    
    def _extract_proteins_from_results(self, data: Dict[str, Any], file_name: str) -> List[Dict[str, Any]]:
        """Extrae información de proteínas de diferentes tipos de resultados"""
        proteins = []
        
        try:
            if "test_proteins" in data.get("tests", {}).get("protein_embedding_generation", {}):
                # Formato: pubmedbert_rag_integration_results.json
                test_proteins = data["tests"]["protein_embedding_generation"]["test_proteins"]
                for protein_id, protein_data in test_proteins.items():
                    proteins.append({
                        "id": protein_id,
                        "name": f"Protein_{protein_id}",
                        "embedding_dimension": protein_data.get("embedding_dimension", 768),
                        "embedding_time": protein_data.get("embedding_time", 0.0),
                        "embedding_norm": protein_data.get("embedding_norm", 0.0),
                        "sequence_length": protein_data.get("sequence_length", 0)
                    })
            
            elif "protein_groups" in data.get("tests", {}).get("functional_clustering", {}):
                # Formato: pubmedbert_clustering_validation_results.json
                protein_groups = data["tests"]["functional_clustering"]["protein_groups"]
                for cluster_name, cluster_data in protein_groups.items():
                    for protein in cluster_data.get("proteins", []):
                        proteins.append({
                            "id": protein["id"],
                            "name": protein["name"],
                            "sequence_length": protein.get("length", 0),
                            "embedding_norm": protein.get("embedding_norm", 0.0),
                            "similarity_cluster": cluster_name,
                            "clustering_similarity": cluster_data.get("average_similarity", 0.0)
                        })
            
            else:
                logger.warning(f"⚠️ Unknown format for {file_name}")
        
        except Exception as e:
            logger.error(f"Error extracting proteins from {file_name}: {e}")
        
        logger.info(f"📊 Extracted {len(proteins)} proteins from {file_name}")
        return proteins
    
    async def _save_bioschemas_results(self, bioschemas_data: List[Dict[str, Any]], output_path: Path):
        """Guarda resultados BioSchemas transformados"""
        
        # Crear documento de colección BioSchemas
        collection_document = {
            "@context": "https://bioschemas.org/",
            "@type": "Dataset",
            "name": "BSM PubMedBERT Protein Collection",
            "description": "Biological Semantic Memory protein dataset with PubMedBERT embeddings",
            "dateCreated": datetime.now().isoformat(),
            "version": self.config.version,
            "distribution": {
                "@type": "DataDownload",
                "encodingFormat": "application/ld+json",
                "contentUrl": str(output_path)
            },
            "hasPart": bioschemas_data
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(collection_document, f, indent=2, ensure_ascii=False)
        
        logger.info(f"💾 Saved {len(bioschemas_data)} BioSchemas proteins to {output_path}")
    
    async def _save_processing_summary(self, results: Dict[str, Any], summary_path: Path):
        """Guarda resumen del procesamiento"""
        
        total_stats = self.transformer.get_transformation_stats()
        
        summary = {
            "processing_date": datetime.now().isoformat(),
            "bsm_version": "1.0.0",
            "bioschemas_version": self.config.version,
            "files_processed": list(results.keys()),
            "total_stats": total_stats,
            "file_results": results,
            "configuration": {
                "batch_size": self.config.batch_size,
                "include_embeddings": self.config.include_embeddings,
                "validate_output": self.config.validate_output
            }
        }
        
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        logger.info(f"📋 Processing summary saved to {summary_path}")

# === FUNCIONES DE UTILIDAD ===

async def create_bsm_transformer(config_dict: Optional[Dict[str, Any]] = None) -> BioSchemasTransformer:
    """Factory function para crear transformer con configuración"""
    if config_dict:
        config = BioSchemasConfig(**config_dict)
    else:
        config = BioSchemasConfig()
    
    return BioSchemasTransformer(config)

async def process_single_protein_to_bsm(protein_data: Dict[str, Any]) -> Dict[str, Any]:
    """Procesa una sola proteína a formato BSM"""
    transformer = await create_bsm_transformer()
    return transformer.transform_protein(protein_data)

# === FUNCIÓN PRINCIPAL PARA TESTING ===

async def main():
    """Función principal para testing del transformer"""
    logger.info("🧪 Testing BioSchemas Transformer")
    
    # Datos de prueba
    test_protein = {
        "id": "P69905",
        "name": "HBA_HUMAN", 
        "sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF",
        "embedding_norm": 14.715084075927734,
        "embedding_time": 0.296,
        "sequence_length": 142,
        "similarity_cluster": "hemoglobins"
    }
    
    # Crear transformer
    transformer = BioSchemasTransformer()
    
    # Transformar proteína
    bioschemas_protein = transformer.transform_protein(test_protein)
    
    # Validar resultado
    is_valid = transformer.validate_bioschemas_output(bioschemas_protein)
    
    # Mostrar resultados
    logger.info("🔍 Transformation Results:")
    logger.info(f"✅ Valid BioSchemas: {is_valid}")
    logger.info(f"📊 Stats: {transformer.get_transformation_stats()}")
    
    print("\n🧬 BioSchemas Output:")
    print(json.dumps(bioschemas_protein, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
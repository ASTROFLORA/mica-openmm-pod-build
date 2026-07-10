#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚙️ BSM CONFIGURATION MANAGER
Gestión centralizada de configuración para Biological Semantic Memory
"""

import os
import json
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass, field, asdict
from enum import Enum
import logging
from urllib.parse import urlparse

# Load environment variables from .env file
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
    logging.info(f"✅ Loaded environment from {env_path}")
else:
    logging.warning(f"⚠️ .env file not found at {env_path}, using system environment variables")

logger = logging.getLogger(__name__)

# === ENUMS DE CONFIGURACIÓN ===

class BSMMode(Enum):
    """Modos de operación BSM"""
    DEVELOPMENT = "development"
    PRODUCTION = "production" 
    TESTING = "testing"
    RESEARCH = "research"

class StorageBackend(Enum):
    """Backends de almacenamiento disponibles"""
    NEO4J = "neo4j"
    MILVUS = "milvus"
    ZILLIZ = "zilliz"
    LOCAL_FILE = "local_file"

class EmbeddingModel(Enum):
    """Modelos de embeddings soportados"""
    PUBMEDBERT = "NeuML/pubmedbert-base-embeddings"
    BIOBERT = "dmis-lab/biobert-base-cased-v1.1"
    CLINICALBERT = "emilyalsentzer/Bio_ClinicalBERT"


@dataclass
class TreeConfig:
    """Configuración para árboles derivados de embeddings"""
    default_method: str = "nj"  # nj, bionj, fastme, rapidnj
    rapidnj_path: str = ""       # ruta binaria RapidNJ si aplica
    artifacts_dir: str = "./data/tree_artifacts"
    enable_tree_guided_retrieval: bool = False
    subtree_max_size: int = 2000
    csls_k: int = 10
    depth_penalty: float = 0.0
    kinases_collection_name: str = "protein_sequences_embeddings"

# === CONFIGURACIONES BASE ===

@dataclass
class Neo4jConfig:
    """Configuración para Neo4j"""
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = ""
    database: str = "neo4j"
    max_connection_lifetime: int = 3600
    max_connection_pool_size: int = 50
    connection_timeout: int = 30
    encrypted: bool = True
    trust: str = "TRUST_ALL_CERTIFICATES"
    
    def __post_init__(self):
        # Cargar desde variables de entorno si están disponibles
        self.uri = os.getenv("NEO4J_URI", self.uri)
        self.username = os.getenv("NEO4J_USERNAME", self.username)
        self.password = os.getenv("NEO4J_PASSWORD", self.password)
        self.database = os.getenv("NEO4J_DATABASE") or self._database_from_query_api() or self.database

    @staticmethod
    def _database_from_query_api() -> Optional[str]:
        query_api = os.getenv("NEO4J_QUERY_API", "")
        if not query_api:
            return None

        parts = [part for part in urlparse(query_api).path.split("/") if part]
        try:
            db_index = parts.index("db")
        except ValueError:
            return None

        if db_index + 1 >= len(parts):
            return None
        return parts[db_index + 1] or None

@dataclass
class MilvusConfig:
    """Configuración para Milvus/Zilliz"""
    host: str = "localhost"
    port: int = 19530
    uri: str = ""
    token: str = ""
    collection_name: str = "bsm_proteins"
    dimension: int = 768
    metric_type: str = "IP"  # Inner Product for normalized embeddings
    index_type: str = "IVF_FLAT"
    nlist: int = 1024
    nprobe: int = 10
    timeout: int = 60
    
    def __post_init__(self):
        # Cargar desde variables de entorno
        self.host = os.getenv("MILVUS_HOST", self.host)
        self.port = int(os.getenv("MILVUS_PORT", str(self.port)))
        self.uri = os.getenv("ZILLIZ_URI", self.uri)
        self.token = os.getenv("ZILLIZ_TOKEN", self.token)
        self.collection_name = os.getenv("BSM_COLLECTION_NAME", self.collection_name)

@dataclass
class BioSchemasConfig:
    """Configuración para transformaciones BioSchemas"""
    profile_version: str = "0.11"
    base_context: str = "https://bioschemas.org/"
    include_embeddings: bool = True
    validate_output: bool = True
    batch_size: int = 1000
    max_memory_mb: int = 2048
    compression_enabled: bool = True
    output_format: str = "jsonld"  # jsonld, turtle, nquads
    
    # Configuración de campos obligatorios
    required_fields: List[str] = field(default_factory=lambda: [
        "@context", "@type", "identifier", "name"
    ])
    
    # Configuración de validación
    strict_validation: bool = False
    allow_extensions: bool = True

@dataclass
class EmbeddingConfig:
    """Configuración para generación de embeddings"""
    model_name: str = "NeuML/pubmedbert-base-embeddings"
    dimension: int = 768
    batch_size: int = 32
    max_length: int = 512
    normalize: bool = True
    device: str = "auto"  # auto, cpu, cuda
    cache_embeddings: bool = True
    cache_dir: str = "./cache/embeddings"
    
    def __post_init__(self):
        # Auto-detectar device si es necesario
        if self.device == "auto":
            try:
                import torch
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                self.device = "cpu"

@dataclass
class APIConfig:
    """Configuración para API BSM"""
    host: str = "127.0.0.1"
    port: int = 8001
    workers: int = 1
    log_level: str = "info"
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    max_request_size: int = 100 * 1024 * 1024  # 100MB
    timeout: int = 300
    rate_limit_per_minute: int = 100
    
    # Configuración de autenticación
    auth_enabled: bool = False
    api_key_header: str = "X-BSM-API-Key"
    allowed_api_keys: List[str] = field(default_factory=list)

@dataclass
class DataConfig:
    """Configuración para manejo de datos"""
    input_directory: str = "./data/pubmedbert_results"
    output_directory: str = "./data/bsm_output"
    backup_directory: str = "./data/bsm_backups"
    temp_directory: str = "./tmp/bsm"
    log_directory: str = "./logs/bsm"
    
    # Configuración de archivos
    file_patterns: List[str] = field(default_factory=lambda: [
        "*.json", "*.jsonl", "*.pkl", "*.h5"
    ])
    max_file_size_mb: int = 1024
    compression_format: str = "gzip"  # gzip, bz2, xz
    
    # Configuración de procesamiento
    parallel_workers: int = 4
    chunk_size: int = 10000
    memory_limit_gb: int = 8

# === CONFIGURACIÓN PRINCIPAL BSM ===

@dataclass
class BSMConfig:
    """Configuración principal del sistema BSM"""
    
    # Configuración general
    mode: BSMMode = BSMMode.DEVELOPMENT
    version: str = "1.0.0"
    debug: bool = False
    verbose: bool = True
    
    # Configuraciones de componentes
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    milvus: MilvusConfig = field(default_factory=MilvusConfig)
    bioschemas: BioSchemasConfig = field(default_factory=BioSchemasConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    api: APIConfig = field(default_factory=APIConfig)
    data: DataConfig = field(default_factory=DataConfig)
    tree: TreeConfig = field(default_factory=TreeConfig)
    
    # Configuración de backends activos
    active_backends: List[StorageBackend] = field(default_factory=lambda: [
        StorageBackend.NEO4J, StorageBackend.MILVUS
    ])
    
    # Configuración de features
    features: Dict[str, bool] = field(default_factory=lambda: {
        "dual_core_queries": True,
        "semantic_search": True,
        "graph_traversal": True,
        "embedding_generation": True,
        "batch_processing": True,
        "real_time_updates": False,
        "backup_sync": True
    })
    
    # Configuración de métricas y observabilidad
    metrics_enabled: bool = True
    telemetry_endpoint: str = ""
    log_queries: bool = True
    profiling_enabled: bool = False

# === GESTOR DE CONFIGURACIÓN ===

class BSMConfigManager:
    """Gestor centralizado de configuración BSM"""
    
    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        """
        Inicializa el gestor de configuración
        
        Args:
            config_path: Ruta al archivo de configuración (opcional)
        """
        self.config_path = Path(config_path) if config_path else None
        self._config: Optional[BSMConfig] = None
        self._config_cache: Dict[str, Any] = {}
    
    def load_config(self, config_path: Optional[Union[str, Path]] = None) -> BSMConfig:
        """
        Carga configuración desde archivo o variables de entorno
        
        Args:
            config_path: Ruta al archivo de configuración
            
        Returns:
            BSMConfig: Configuración cargada
        """
        if config_path:
            self.config_path = Path(config_path)
        
        # Cargar configuración base
        config = BSMConfig()
        
        # Cargar desde archivo si existe
        if self.config_path and self.config_path.exists():
            config = self._load_from_file(self.config_path)
            logger.info(f"📁 BSM config loaded from: {self.config_path}")
        
        # Sobrescribir con variables de entorno
        config = self._load_from_env(config)
        
        # Validar configuración
        self._validate_config(config)
        
        # Crear directorios necesarios
        self._create_directories(config)
        
        self._config = config
        logger.info("⚙️ BSM configuration initialized successfully")
        
        return config
    
    def _load_from_file(self, config_path: Path) -> BSMConfig:
        """Carga configuración desde archivo YAML/JSON"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                if config_path.suffix.lower() in ['.yml', '.yaml']:
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)
            
            # Convertir dict a BSMConfig usando dataclass
            return self._dict_to_config(data)
        
        except Exception as e:
            logger.warning(f"⚠️ Error loading config from file: {e}")
            return BSMConfig()
    
    def _load_from_env(self, config: BSMConfig) -> BSMConfig:
        """Sobrescribe configuración con variables de entorno"""
        
        # Variables de entorno generales
        if os.getenv("BSM_MODE"):
            try:
                config.mode = BSMMode(os.getenv("BSM_MODE"))
            except ValueError:
                logger.warning(f"⚠️ Invalid BSM_MODE: {os.getenv('BSM_MODE')}")
        
        config.debug = os.getenv("BSM_DEBUG", "false").lower() == "true"
        config.verbose = os.getenv("BSM_VERBOSE", "true").lower() == "true"
        
        # Features
        if os.getenv("BSM_FEATURES"):
            try:
                features = json.loads(os.getenv("BSM_FEATURES"))
                config.features.update(features)
            except json.JSONDecodeError:
                logger.warning("⚠️ Invalid BSM_FEATURES JSON")

        # Árbol (overrides)
        config.tree.default_method = os.getenv("BSM_TREE_METHOD", config.tree.default_method)
        config.tree.rapidnj_path = os.getenv("BSM_RAPIDNJ_PATH", config.tree.rapidnj_path)
        config.tree.artifacts_dir = os.getenv("BSM_TREE_ARTIFACTS_DIR", config.tree.artifacts_dir)
        config.tree.enable_tree_guided_retrieval = os.getenv("BSM_TREE_RETRIEVAL", str(config.tree.enable_tree_guided_retrieval)).lower() == "true"
        config.tree.subtree_max_size = int(os.getenv("BSM_TREE_SUBTREE_MAX", str(config.tree.subtree_max_size)))
        config.tree.csls_k = int(os.getenv("BSM_TREE_CSLS_K", str(config.tree.csls_k)))
        config.tree.depth_penalty = float(os.getenv("BSM_TREE_DEPTH_PENALTY", str(config.tree.depth_penalty)))
        config.tree.kinases_collection_name = os.getenv("BSM_KINASES_COLLECTION", config.tree.kinases_collection_name)
        
        return config
    
    def _dict_to_config(self, data: Dict[str, Any]) -> BSMConfig:
        """Convierte diccionario a BSMConfig"""
        # Esta es una implementación simplificada
        # En producción usaríamos una librería como pydantic o cattrs
        config = BSMConfig()
        
        if 'mode' in data:
            config.mode = BSMMode(data['mode'])
        if 'debug' in data:
            config.debug = data['debug']
        if 'verbose' in data:
            config.verbose = data['verbose']
        
        # Cargar configuraciones anidadas
        if 'neo4j' in data:
            neo4j_data = data['neo4j']
            config.neo4j.uri = neo4j_data.get('uri', config.neo4j.uri)
            config.neo4j.username = neo4j_data.get('username', config.neo4j.username)
            config.neo4j.password = neo4j_data.get('password', config.neo4j.password)
        
        if 'milvus' in data:
            milvus_data = data['milvus']
            config.milvus.host = milvus_data.get('host', config.milvus.host)
            config.milvus.port = milvus_data.get('port', config.milvus.port)
        
        # Más configuraciones...
        
        return config
    
    def _validate_config(self, config: BSMConfig):
        """Valida la configuración cargada"""
        errors = []
        
        # Validar backends activos
        if StorageBackend.NEO4J in config.active_backends:
            if not config.neo4j.password and config.mode == BSMMode.PRODUCTION:
                errors.append("Neo4j password required in production mode")
        
        if StorageBackend.MILVUS in config.active_backends or StorageBackend.ZILLIZ in config.active_backends:
            if config.milvus.dimension <= 0:
                errors.append("Milvus dimension must be positive")
        
        # Validar configuración de embeddings
        if config.embedding.dimension != config.milvus.dimension:
            logger.warning("⚠️ Embedding dimension mismatch with Milvus config")
        
        # Validar directorios
        for directory in [config.data.input_directory, config.data.output_directory]:
            try:
                Path(directory).parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"Cannot create directory {directory}: {e}")
        
        if errors:
            raise ValueError(f"Configuration validation failed: {'; '.join(errors)}")
    
    def _create_directories(self, config: BSMConfig):
        """Crea directorios necesarios"""
        directories = [
            config.data.input_directory,
            config.data.output_directory,
            config.data.backup_directory,
            config.data.temp_directory,
            config.data.log_directory,
            config.embedding.cache_dir,
            config.tree.artifacts_dir
        ]
        
        for directory in directories:
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"⚠️ Could not create directory {directory}: {e}")
    
    def save_config(self, config_path: Optional[Union[str, Path]] = None):
        """Guarda configuración actual a archivo"""
        if not self._config:
            raise ValueError("No configuration loaded")
        
        save_path = Path(config_path) if config_path else self.config_path
        if not save_path:
            raise ValueError("No config path specified")
        
        # Convertir configuración a diccionario
        config_dict = asdict(self._config)
        
        # Serializar enums
        config_dict['mode'] = self._config.mode.value
        config_dict['active_backends'] = [b.value for b in self._config.active_backends]
        
        # Guardar archivo
        with open(save_path, 'w', encoding='utf-8') as f:
            if save_path.suffix.lower() in ['.yml', '.yaml']:
                yaml.dump(config_dict, f, default_flow_style=False)
            else:
                json.dump(config_dict, f, indent=2)
        
        logger.info(f"💾 BSM config saved to: {save_path}")
    
    def get_config(self) -> BSMConfig:
        """Obtiene configuración actual"""
        if not self._config:
            return self.load_config()
        return self._config
    
    def update_config(self, updates: Dict[str, Any]):
        """Actualiza configuración dinámicamente"""
        if not self._config:
            self.load_config()
        
        # Implementar actualización de campos
        # En producción usaríamos una librería más robusta
        logger.info(f"⚙️ BSM config updated: {list(updates.keys())}")
    
    def get_connection_string(self, backend: StorageBackend) -> str:
        """Obtiene string de conexión para un backend"""
        if not self._config:
            self.load_config()
        
        if backend == StorageBackend.NEO4J:
            return f"{self._config.neo4j.uri}"
        elif backend == StorageBackend.MILVUS:
            if self._config.milvus.uri:
                return self._config.milvus.uri
            return f"{self._config.milvus.host}:{self._config.milvus.port}"
        else:
            raise ValueError(f"Unsupported backend: {backend}")

# === CONFIGURACIÓN GLOBAL ===

# Instancia global del gestor de configuración
_global_config_manager: Optional[BSMConfigManager] = None

def get_config_manager() -> BSMConfigManager:
    """Obtiene el gestor de configuración global"""
    global _global_config_manager
    
    if _global_config_manager is None:
        _global_config_manager = BSMConfigManager()
    
    return _global_config_manager

def get_bsm_config() -> BSMConfig:
    """Obtiene configuración BSM global"""
    return get_config_manager().get_config()

def load_bsm_config(config_path: Optional[Union[str, Path]] = None) -> BSMConfig:
    """Carga configuración BSM desde archivo"""
    return get_config_manager().load_config(config_path)

# === CONFIGURACIONES PREDEFINIDAS ===

def get_development_config() -> BSMConfig:
    """Configuración para desarrollo"""
    config = BSMConfig()
    config.mode = BSMMode.DEVELOPMENT
    config.debug = True
    config.neo4j.password = "development"
    config.features["real_time_updates"] = True
    config.features["profiling_enabled"] = True
    return config

def get_production_config() -> BSMConfig:
    """Configuración para producción"""
    config = BSMConfig()
    config.mode = BSMMode.PRODUCTION
    config.debug = False
    config.verbose = False
    config.api.workers = 4
    config.features["profiling_enabled"] = False
    config.data.parallel_workers = 8
    return config

def get_testing_config() -> BSMConfig:
    """Configuración para testing"""
    config = BSMConfig()
    config.mode = BSMMode.TESTING
    config.debug = True
    config.neo4j.database = "test_bsm"
    config.milvus.collection_name = "test_bsm_proteins"
    config.data.input_directory = "./test_data/input"
    config.data.output_directory = "./test_data/output"
    return config

# === UTILIDADES ===

def create_config_template(output_path: Union[str, Path]):
    """Crea archivo de configuración template"""
    template_config = BSMConfig()
    manager = BSMConfigManager()
    manager._config = template_config
    manager.save_config(output_path)
    logger.info(f"📄 BSM config template created: {output_path}")

if __name__ == "__main__":
    # Crear configuración de ejemplo
    create_config_template("bsm_config.yaml")
    
    # Testear configuración
    config = load_bsm_config("bsm_config.yaml")
    print(f"✅ BSM Config loaded successfully: {config.mode.value}")

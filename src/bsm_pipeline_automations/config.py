"""
Configuration for BSM Pipeline Automations.

Handles connection settings for Milvus and Neo4j, sourcing credentials
from environment variables for security.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class MilvusConfig:
    """Connection details for Milvus/Zilliz."""
    URI: str = os.environ.get("ZILLIZ_URI", "http://localhost:19530")
    TOKEN: str = os.environ.get("ZILLIZ_TOKEN", "")


@dataclass(frozen=True)
class Neo4jConfig:
    """Connection details for Neo4j."""
    URI: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    USERNAME: str = os.environ.get("NEO4J_USERNAME", "neo4j")
    PASSWORD: str = os.environ.get("NEO4J_PASSWORD", "password")


@dataclass(frozen=True)
class BSMConfig:
    """All BSM pipeline configurations."""
    COLLECTION_NAME: str = "protein_pubmedbert_embeddings"
    EMBEDDING_DIM: int = 768
    milvus: MilvusConfig = MilvusConfig()
    neo4j: Neo4jConfig = Neo4jConfig()


def get_bsm_config() -> BSMConfig:
    """Returns a BSMConfig instance."""
    return BSMConfig()


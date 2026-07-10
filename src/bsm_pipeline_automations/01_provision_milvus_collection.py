"""
BSM Pipeline Automation: 01 - Provision Milvus Collection

This script connects to Milvus and creates the collection required for storing
PubMedBERT embeddings, as defined in the Unified Master Plan.
"""

import logging
from pymilvus import (
    connections,
    utility,
    Collection,
    CollectionSchema,
    FieldSchema,
    DataType,
)
from config import get_bsm_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    """
    Provisions the Milvus collection for PubMedBERT embeddings.
    """
    config = get_bsm_config()
    collection_name = config.COLLECTION_NAME
    dim = config.EMBEDDING_DIM

    try:
        logging.info(f"Connecting to Milvus at {config.milvus.URI}")
        connections.connect("default", uri=config.milvus.URI, token=config.milvus.TOKEN)

        if utility.has_collection(collection_name):
            logging.warning(f"Collection '{collection_name}' already exists. Skipping creation.")
            return

        logging.info(f"Creating collection '{collection_name}' with dimension {dim}.")

        # Define fields
        protein_id = FieldSchema(
            name="protein_id",
            dtype=DataType.VARCHAR,
            is_primary=True,
            auto_id=False,
            max_length=100,
            description="Unique protein identifier (e.g., UniProt ID)",
        )
        embedding = FieldSchema(
            name="embedding",
            dtype=DataType.FLOAT_VECTOR,
            dim=dim,
            description="PubMedBERT embedding vector",
        )

        # Create schema
        schema = CollectionSchema(
            fields=[protein_id, embedding],
            description="Collection for PubMedBERT protein embeddings",
            enable_dynamic_field=True,
        )

        # Create collection
        collection = Collection(
            name=collection_name,
            schema=schema,
            using='default',
            consistency_level="Strong"
        )

        logging.info(f"Collection '{collection_name}' created successfully.")

        # Create index
        logging.info("Creating IVF_FLAT index...")
        index_params = {
            "metric_type": "L2",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 1024},
        }
        collection.create_index(
            field_name="embedding",
            index_params=index_params
        )
        logging.info("Index created successfully.")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        connections.disconnect("default")
        logging.info("Disconnected from Milvus.")

if __name__ == "__main__":
    main()

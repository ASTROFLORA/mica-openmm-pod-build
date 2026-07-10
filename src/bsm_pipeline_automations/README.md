# BSM Pipeline Automations

This directory contains the scripts for automating the BSM (Biological Semantic Memory) data ingestion pipeline, as outlined in the [Unified Master Plan](file:///c:/Users/busta/Downloads/MICA/astroflora-core-feature-spectra-worker-integration-1/EMBEDDINGORDERINGPLAN/TEAM_AI_COLLABORATION/AIUNIVERSITY/STRATEGY/UNIFIED_MASTER_PLAN_20250929.md).

The scripts are numbered to be run in sequence.

### Scripts

1.  **`01_provision_milvus_collection.py`**: Creates the necessary Milvus collection for the PubMedBERT embeddings.
2.  **`02_backfill_pubmedbert_embeddings.py`**: Generates PubMedBERT embeddings for the entire proteome and ingests them into the new Milvus collection.
3.  **`03_update_neo4j_graph.py`**: Updates the Neo4j graph to link the new PubMedBERT embeddings to the existing protein nodes.

### Configuration

Connection settings for Milvus and Neo4j are managed in `config.py` and sourced from environment variables.

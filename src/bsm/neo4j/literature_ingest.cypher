// ============================================================================
// Phase 2.004 - Neo4j Literature Ingestion Script (SIMPLIFIED)
// ----------------------------------------------------------------------------
// Loads Literature, Embedding nodes and relationships from staging CSVs.
// Aligns with schema in `src/bsm/neo4j/graph_schema.cypher`.
//
// Prerequisites:
// 1. Run `python scripts/phase2_generate_neo4j_staging.py` to create CSVs
// 2. Place CSVs in Neo4j import directory or use absolute paths
// 3. Run constraints/indexes from graph_schema.cypher first
//
// Usage (Neo4j Browser):
//   :use neo4j
//   :begin
//   <paste STEP 1-3 below>
//   :commit
//
// Usage (cypher-shell):
//   cypher-shell -u neo4j -p <password> < literature_ingest.cypher
// ============================================================================

// ----------------------------------------------------------------------------
// STEP 1: Load Literature nodes
// ----------------------------------------------------------------------------
LOAD CSV WITH HEADERS FROM 'file:///literature_nodes.csv' AS row
MERGE (l:Literature {literature_id: row.literature_id})
ON CREATE SET
  l.doi = row.doi,
  l.title = row.title,
  l.abstract_hash = row.abstract_hash,
  l.publication_year = toInteger(row.publication_year),
  l.venue = row.venue,
  l.source = row.source,
  l.url = row.url,
  l.tags = CASE row.tags WHEN '' THEN [] ELSE split(row.tags, '|') END,
  l.authors = CASE row.authors WHEN '' THEN [] ELSE split(row.authors, '|') END,
  l.created_at = datetime(row.retrieved_at)
ON MATCH SET
  l.updated_at = datetime();

// ----------------------------------------------------------------------------
// STEP 2: Load Embedding nodes
// ----------------------------------------------------------------------------
LOAD CSV WITH HEADERS FROM 'file:///literature_embeddings.csv' AS row
MERGE (e:Embedding {embedding_id: row.embedding_id})
ON CREATE SET
  e.kind = row.kind,
  e.dimensionality = toInteger(row.dimensionality),
  e.milvus_collection = row.milvus_collection,
  e.milvus_vector_id = row.milvus_vector_id,
  e.created_at = datetime(row.created_at)
ON MATCH SET
  e.updated_at = datetime();

// ----------------------------------------------------------------------------
// STEP 3: Create HAS_EMBEDDING relationships
// ----------------------------------------------------------------------------
LOAD CSV WITH HEADERS FROM 'file:///literature_has_embedding.csv' AS row
MATCH (l:Literature {literature_id: row.source_literature_id})
MATCH (e:Embedding {embedding_id: row.target_embedding_id})
MERGE (l)-[:HAS_EMBEDDING]->(e);

// ============================================================================
// VALIDATION QUERIES (Optional - run after ingestion)
// ============================================================================

// Count loaded nodes
// MATCH (l:Literature) RETURN count(l) AS literature_count;
// MATCH (e:Embedding {kind: 'pubmedbert'}) RETURN count(e) AS embedding_count;

// Verify relationships
// MATCH (l:Literature)-[:HAS_EMBEDDING]->(e:Embedding)
// RETURN l.title AS title, e.embedding_id AS embedding_id
// LIMIT 5;

// Check tags distribution
// MATCH (l:Literature)
// UNWIND l.tags AS tag
// RETURN tag, count(*) AS paper_count
// ORDER BY paper_count DESC;

// ============================================================================
// Notes:
// - CSV files must be in Neo4j's import directory (default: /var/lib/neo4j/import/)
// - Windows: C:\Users\<user>\.Neo4jDesktop\relate-data\dbmss\<dbms-id>\import\
// - For absolute paths, enable: apoc.import.file.enabled=true in neo4j.conf
// - Run constraints/indexes from graph_schema.cypher BEFORE this script
// ============================================================================

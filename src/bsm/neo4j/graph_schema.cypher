// ============================================================================
// BSM-BUDO-CEA PHASE 2 NEO4J GRAPH SCHEMA
// ----------------------------------------------------------------------------
// Roadmap Alignment: Phase 2.004 (Neo4j Graph Modeling)
// Author: Alex Rodriguez (AI Systems Architecture Lab)
// Date: 2025-10-08
// Description:
//   Unified schema for BUDO V3, BioSite V3, and Literature enrichment layers.
//   Applies constraints, indexes, and seed relationship patterns used by the
//   `BudoIngestionPipeline` and literature ingestion stages.
// ============================================================================

// ----------------------------------------------------------------------------
// SAFETY: Wrap in an explicit transaction block when using from Python:
//   :begin
//   :use neo4j
//   <paste schema>
//   :commit
// ----------------------------------------------------------------------------

// ============================================================================
// 1. CONSTRAINTS (UNIQUENESS + EXISTENCE)
// ============================================================================

CREATE CONSTRAINT budo_identity_unique IF NOT EXISTS
FOR (b:Budo)
REQUIRE b.budo_id IS UNIQUE;

CREATE CONSTRAINT budo_version_exists IF NOT EXISTS
FOR (b:Budo)
REQUIRE b.version IS NOT NULL;

CREATE CONSTRAINT biosite_identity_unique IF NOT EXISTS
FOR (s:BioSite)
REQUIRE s.biosite_id IS UNIQUE;

CREATE CONSTRAINT literature_identity_unique IF NOT EXISTS
FOR (l:Literature)
REQUIRE l.literature_id IS UNIQUE;

CREATE CONSTRAINT literature_chunk_unique IF NOT EXISTS
FOR (c:LiteratureChunk)
REQUIRE c.chunk_id IS UNIQUE;

CREATE CONSTRAINT embedding_identity_unique IF NOT EXISTS
FOR (e:Embedding)
REQUIRE e.embedding_id IS UNIQUE;

// Composite ID (Paper + Section) for provenance relationships
CREATE CONSTRAINT provenance_edge_unique IF NOT EXISTS
FOR ()-[r:SUPPORTS]->()
REQUIRE (r.source_id, r.chunk_id) IS UNIQUE;

// ============================================================================
// 2. INDEXES (QUERY PERFORMANCE)
// ============================================================================

// BUDO V3 search surfaces
CREATE INDEX budo_name_idx IF NOT EXISTS
FOR (b:Budo)
ON (b.canonical_name);

CREATE INDEX budo_taxonomy_idx IF NOT EXISTS
FOR (b:Budo)
ON (b.taxonomy_id);

CREATE INDEX budo_modality_idx IF NOT EXISTS
FOR (b:Budo)
ON (b.modality_suffix);

// BioSite lookup by parent + type
CREATE INDEX biosite_parent_idx IF NOT EXISTS
FOR (s:BioSite)
ON (s.parent_budo_id, s.site_type);

// Literature quick lookup by Paper metadata
CREATE INDEX literature_year_idx IF NOT EXISTS
FOR (l:Literature)
ON (l.publication_year);

CREATE INDEX literature_source_idx IF NOT EXISTS
FOR (l:Literature)
ON (l.source);

CREATE FULLTEXT INDEX literature_text_ft IF NOT EXISTS
FOR (l:Literature)
ON EACH [l.title, l.abstract];

// Embedding metadata (enables vector-hops)
CREATE INDEX embedding_type_idx IF NOT EXISTS
FOR (e:Embedding)
ON (e.kind);

CREATE INDEX embedding_vector_id_idx IF NOT EXISTS
FOR (e:Embedding)
ON (e.milvus_vector_id);

// ============================================================================
// 3. NODE TEMPLATE PROJECTIONS (DOCUMENTATION ONLY)
// ============================================================================
// Label: Budo
//   budo_id: string (composite ID, e.g., "budo:WNK1_HUMAN_v1-L")
//   canonical_name: string
//   recommended_name: string
//   organism: string
//   taxonomy_id: string
//   modality_suffix: string (S/D/L/Q/F)
//   sequence_length: integer
//   ese_signature_ids: array<string>
//   pubmedbert_embedding_id: string (FK -> Embedding)
//   provenance: map
//
// Label: BioSite
//   biosite_id: string (e.g., "biosite:WNK1_HUMAN_v1-L-001")
//   parent_budo_id: string
//   site_name: string
//   site_type: string
//   start_position: integer
//   end_position: integer
//   conformational_state: string
//   ese_signature_id: string (FK -> Embedding)
//   metadata: map
//
// Label: Literature
//   literature_id: string (Semantic Scholar paperId)
//   doi: string
//   title: string
//   abstract: string
//   publication_year: integer
//   venue: string
//   source: string (SemanticScholar, PubMed, etc.)
//   url: string
//   embedding_id: string (FK -> Embedding)
//   tags: array<string>
//
// Label: LiteratureChunk
//   chunk_id: string (e.g., "97476cd3_secAbstract_para1")
//   literature_id: string
//   section: string
//   order: integer
//   text: string
//   created_at: datetime
//
// Label: Embedding
//   embedding_id: string
//   kind: string (pubmedbert, ese, multimodal)
//   dimensionality: integer
//   milvus_collection: string
//   milvus_vector_id: string
//   created_at: datetime
// ============================================================================

// ============================================================================
// 4. RELATIONSHIP PATTERNS
// ============================================================================

// BUDO structural model
// (b:Budo)-[:HAS_DOMAIN]->(d:Domain)
// (b:Budo)-[:HAS_VARIANT]->(v:Variant)
// (b:Budo)-[:HAS_SITE]->(s:BioSite)
// (b:Budo)-[:HAS_EMBEDDING {modality:"pubmedbert"}]->(e:Embedding)
// (b:Budo)-[:HAS_EMBEDDING {modality:"ese"}]->(e:Embedding)

// Literature enrichment
// (b:Budo)-[r:SUPPORTED_BY]->(l:Literature)
//   r.chunk_ids: array<string>
//   r.evidence_type: string ("literature", "clinical", etc.)
//   r.confidence: float
//
// (s:BioSite)-[:MENTIONED_IN]->(l:Literature)
// (l:Literature)-[:HAS_CHUNK]->(c:LiteratureChunk)
// (c:LiteratureChunk)-[:EMBEDDED_AS]->(e:Embedding)

// CEA cross-reference bridge (Phase 1 tie-in)
// (b:Budo)-[:DERIVES_ID]->(cea:CEA {budo_id: ...})

// ============================================================================
// 5. SEED QUERIES (OPTIONAL)
// ============================================================================

// Example: Merge a WNK1 literature node backed by PubMedBERT embedding
//
// MERGE (b:Budo {budo_id: "budo:WNK1_HUMAN_v1-L"})
//   ON CREATE SET b.canonical_name = "WNK1_HUMAN",
//                 b.recommended_name = "Serine/threonine-protein kinase WNK1",
//                 b.organism = "Homo sapiens",
//                 b.taxonomy_id = "9606",
//                 b.modality_suffix = "L",
//                 b.created_at = datetime(),
//                 b.updated_at = datetime();
//
// MERGE (l:Literature {literature_id: "97476cd3-204c-400c-488b-5eae6f98426b"})
//   ON CREATE SET l.doi = "10.1038/s41440-020-0401-8",
//                 l.title = "The WNK-SPAK/OSR1 pathway in hypertension",
//                 l.abstract = "The WNK-SPAK/OSR1 pathway regulates sodium...",
//                 l.publication_year = 2020,
//                 l.venue = "Acta Pharmacologica Sinica",
//                 l.source = "SemanticScholar",
//                 l.url = "https://www.nature.com/articles/s41440-020-0401-8",
//                 l.created_at = datetime(),
//                 l.updated_at = datetime();
//
// MERGE (e:Embedding {embedding_id: "emb:pubmedbert:97476cd3"})
//   ON CREATE SET e.kind = "pubmedbert",
//                 e.dimensionality = 768,
//                 e.milvus_collection = "bsm_pubmedbert_v1",
//                 e.milvus_vector_id = "vector_97476cd3",
//                 e.created_at = datetime();
//
// MERGE (l)-[:HAS_EMBEDDING]->(e);
// MERGE (b)-[:SUPPORTED_BY {
//           evidence_type: "literature",
//           chunk_ids: ["97476cd3_secAbstract_para1"],
//           confidence: 0.85,
//           source_id: "phase2_generate_wnk_literature_embeddings.py"
//       }]->(l);
//
// ============================================================================
// 6. MAINTENANCE QUERIES
// ============================================================================
// -- List constraints
// CALL db.constraints();
//
// -- List indexes
// CALL db.indexes();
//
// -- Inspect literature evidence for a BUDO entity
// MATCH (b:Budo {budo_id: $budo_id})-[r:SUPPORTED_BY]->(l:Literature)
// RETURN l.literature_id AS literature_id, l.title AS title, r.chunk_ids AS chunk_ids;
//
// -- Validate BioSite to Budo linkage
// MATCH (s:BioSite)
// WHERE NOT EXISTS {
//   MATCH (b:Budo {budo_id: s.parent_budo_id})
// }
// RETURN s.biosite_id;
//
// -- Extract embedding usage statistics
// MATCH (e:Embedding)<-[:HAS_EMBEDDING]-(n)
// RETURN e.embedding_id AS embedding_id,
//        e.kind AS kind,
//        collect(DISTINCT labels(n)) AS node_labels,
//        count(n) AS usage_count
// ORDER BY usage_count DESC;
// ============================================================================

"""
GraphRAG Engine - Dr. Priya Sharma
==================================

Graph Retrieval-Augmented Generation engine for protein knowledge graphs.
Provides intelligent knowledge retrieval and reasoning for MICA-Lineage system.

Phase 5 Implementation: Chronoracle Integration (5 weeks)
Lead: Dr. Priya Sharma + Alex Rodriguez
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Union, Any, Set
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import json

import numpy as np
import networkx as nx
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.decomposition import PCA
from sklearn.cluster import DBSCAN
import torch
import torch.nn.functional as F

from bsm.config import get_bsm_config
from bsm.neo4j_integration import Neo4jClient

logger = logging.getLogger(__name__)


@dataclass
class GraphEntity:
    """Represents an entity in the protein knowledge graph"""
    entity_id: str
    entity_type: str  # 'protein', 'function', 'organism', 'pathway', 'structure'
    properties: Dict[str, Any]
    embeddings: Optional[np.ndarray] = None
    confidence: float = 1.0


@dataclass
class GraphRelation:
    """Represents a relationship between entities"""
    relation_id: str
    source_entity: str
    target_entity: str
    relation_type: str  # 'homologous_to', 'functions_in', 'expressed_by', etc.
    properties: Dict[str, Any]
    weight: float = 1.0
    confidence: float = 1.0


@dataclass
class GraphPath:
    """Represents a reasoning path through the knowledge graph"""
    path_id: str
    entities: List[str]
    relations: List[str]
    path_length: int
    semantic_score: float
    confidence: float
    reasoning_type: str  # 'functional', 'evolutionary', 'structural'


@dataclass
class RAGQuery:
    """Query structure for GraphRAG system"""
    query_id: str
    query_text: str
    query_type: str  # 'similarity', 'functional', 'pathway', 'evolutionary'
    context_entities: List[str]
    constraints: Dict[str, Any]
    max_results: int = 10
    confidence_threshold: float = 0.6


@dataclass
class RAGResponse:
    """Response from GraphRAG system"""
    response_id: str
    query: RAGQuery
    retrieved_entities: List[GraphEntity]
    reasoning_paths: List[GraphPath]
    generated_insights: str
    confidence_score: float
    retrieval_time: float
    reasoning_time: float


class GraphRAGEngine:
    """
    Graph Retrieval-Augmented Generation Engine for protein knowledge.
    
    Capabilities:
    - Multi-modal knowledge graph construction and management
    - Semantic entity retrieval using embeddings
    - Graph-based reasoning path discovery
    - Context-aware insight generation
    - Integration with Neo4j knowledge base
    - Real-time graph updates and learning
    """
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or get_bsm_config()
        
        # Graph components
        self.knowledge_graph = nx.MultiDiGraph()
        self.entity_embeddings = {}
        self.relation_embeddings = {}
        self.neo4j_client = None
        
        # RAG components
        self.retrieval_cache = {}
        self.reasoning_patterns = {}
        self.insight_templates = {}
        
        # Performance tracking
        self.query_history = []
        self.performance_metrics = defaultdict(list)
        
        # Initialize system
        self._initialize_graph_engine()
        self._load_reasoning_patterns()
        self._setup_insight_generation()
        
        logger.info("GraphRAG Engine initialized - Dr. Priya Sharma implementation")
    
    def _initialize_graph_engine(self):
        """Initialize the graph engine components"""
        
        try:
            # Neo4j connection
            neo4j_config = self.config.get('neo4j', {})
            if neo4j_config:
                self.neo4j_client = Neo4jClient(neo4j_config)
                logger.info("Connected to Neo4j knowledge base")
            
            # Load existing graph if available
            self._load_knowledge_graph()
            
            # Initialize embedding spaces
            self._initialize_embedding_spaces()
            
            logger.info("Graph engine components initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize graph engine: {e}")
            self._initialize_fallback_graph()
    
    def _initialize_fallback_graph(self):
        """Initialize minimal fallback graph"""
        logger.warning("Using fallback graph initialization")
        
        # Create basic protein knowledge graph
        self._create_basic_protein_graph()
    
    def _load_knowledge_graph(self):
        """Load knowledge graph from Neo4j or local storage"""
        
        if self.neo4j_client:
            # Load from Neo4j
            self._load_from_neo4j()
        else:
            # Load from local files
            self._load_from_local_files()
    
    def _load_from_neo4j(self):
        """Load graph data from Neo4j database"""
        
        try:
            # Query all protein entities
            protein_query = """
            MATCH (p:Protein)
            RETURN p.protein_id as id, p.sequence as sequence, 
                   p.organism as organism, p.function as function
            LIMIT 1000
            """
            
            proteins = self.neo4j_client.execute_query(protein_query)
            
            for protein in proteins:
                entity = GraphEntity(
                    entity_id=protein['id'],
                    entity_type='protein',
                    properties={
                        'sequence': protein.get('sequence', ''),
                        'organism': protein.get('organism', ''),
                        'function': protein.get('function', '')
                    }
                )
                self._add_entity_to_graph(entity)
            
            # Query relationships
            relation_query = """
            MATCH (p1:Protein)-[r]->(p2:Protein)
            RETURN p1.protein_id as source, p2.protein_id as target,
                   type(r) as relation_type, properties(r) as props
            LIMIT 5000
            """
            
            relations = self.neo4j_client.execute_query(relation_query)
            
            for relation in relations:
                graph_relation = GraphRelation(
                    relation_id=f"{relation['source']}_{relation['target']}",
                    source_entity=relation['source'],
                    target_entity=relation['target'],
                    relation_type=relation['relation_type'],
                    properties=relation.get('props', {})
                )
                self._add_relation_to_graph(graph_relation)
            
            logger.info(f"Loaded {len(proteins)} proteins and {len(relations)} relations from Neo4j")
            
        except Exception as e:
            logger.error(f"Failed to load from Neo4j: {e}")
            self._create_basic_protein_graph()
    
    def _load_from_local_files(self):
        """Load graph from local JSON files"""
        
        # Mock graph loading - in practice would load from serialized files
        self._create_basic_protein_graph()
    
    def _create_basic_protein_graph(self):
        """Create a basic protein knowledge graph for testing"""
        
        # Sample protein entities
        proteins = [
            {
                'id': 'P12345', 'sequence': 'MKTVRQERLKSIVRILERSKEPVSGAQLAEELSVSRQVIVQDIAYLRSLGYNIVATPRGYVLAGG',
                'organism': 'E. coli', 'function': 'ATP synthase subunit'
            },
            {
                'id': 'Q67890', 'sequence': 'MAEGEITTFTALTEKFNLPPGNYKKPKLLYCSNGGHFLRILPDGTVDGTRDRSDQHIQLQLSAESVGEVYIKSTETGQYLAMDTSGLLYGSQTPNEECLFLERLEENHYNTYTSKKHAEKNWFVGLKKNGSCKRGPRTHYGQKAILFLPLPV',
                'organism': 'S. cerevisiae', 'function': 'Heat shock protein'
            },
            {
                'id': 'R54321', 'sequence': 'MSQCHWDLKDKKVVVVGAGGVGKSALTIQLIQNHFVDEYDPTIEFESYRQYQLDAPDGQIIDGDGQVNYEEFVQMMTAK',
                'organism': 'H. sapiens', 'function': 'GTPase'
            }
        ]
        
        # Add entities
        for protein_data in proteins:
            entity = GraphEntity(
                entity_id=protein_data['id'],
                entity_type='protein',
                properties={
                    'sequence': protein_data['sequence'],
                    'organism': protein_data['organism'],
                    'function': protein_data['function']
                }
            )
            self._add_entity_to_graph(entity)
        
        # Add sample relationships
        relationships = [
            ('P12345', 'Q67890', 'interacts_with', {'confidence': 0.8}),
            ('Q67890', 'R54321', 'regulates', {'confidence': 0.7}),
            ('P12345', 'R54321', 'homologous_to', {'similarity': 0.6})
        ]
        
        for source, target, rel_type, props in relationships:
            relation = GraphRelation(
                relation_id=f"{source}_{target}",
                source_entity=source,
                target_entity=target,
                relation_type=rel_type,
                properties=props
            )
            self._add_relation_to_graph(relation)
        
        logger.info("Created basic protein knowledge graph")
    
    def _add_entity_to_graph(self, entity: GraphEntity):
        """Add entity to the knowledge graph"""
        
        self.knowledge_graph.add_node(
            entity.entity_id,
            entity_type=entity.entity_type,
            properties=entity.properties,
            confidence=entity.confidence
        )
        
        # Store entity embeddings if available
        if entity.embeddings is not None:
            self.entity_embeddings[entity.entity_id] = entity.embeddings
    
    def _add_relation_to_graph(self, relation: GraphRelation):
        """Add relation to the knowledge graph"""
        
        self.knowledge_graph.add_edge(
            relation.source_entity,
            relation.target_entity,
            key=relation.relation_id,
            relation_type=relation.relation_type,
            properties=relation.properties,
            weight=relation.weight,
            confidence=relation.confidence
        )
    
    def _initialize_embedding_spaces(self):
        """Initialize embedding spaces for entities and relations"""
        
        # Generate embeddings for entities without them
        for node_id in self.knowledge_graph.nodes():
            if node_id not in self.entity_embeddings:
                self.entity_embeddings[node_id] = self._generate_entity_embedding(node_id)
        
        # Generate relation embeddings
        self._generate_relation_embeddings()
        
        logger.info(f"Initialized embeddings for {len(self.entity_embeddings)} entities")
    
    def _generate_entity_embedding(self, entity_id: str) -> np.ndarray:
        """Generate embedding for an entity"""
        
        node_data = self.knowledge_graph.nodes[entity_id]
        properties = node_data.get('properties', {})
        
        # Simple feature-based embedding (in practice would use sophisticated methods)
        features = []
        
        # Sequence-based features for proteins
        if node_data.get('entity_type') == 'protein':
            sequence = properties.get('sequence', '')
            if sequence:
                features.extend([
                    len(sequence) / 1000.0,  # Normalized length
                    sequence.count('C') / len(sequence) if sequence else 0,  # Cysteine content
                    sequence.count('G') / len(sequence) if sequence else 0,  # Glycine content
                ])
            else:
                features.extend([0.0, 0.0, 0.0])
        
        # Organism encoding
        organism = properties.get('organism', '')
        organism_encoding = hash(organism) % 1000 / 1000.0 if organism else 0.0
        features.append(organism_encoding)
        
        # Function encoding
        function = properties.get('function', '')
        function_encoding = hash(function) % 1000 / 1000.0 if function else 0.0
        features.append(function_encoding)
        
        # Graph topology features
        degree = self.knowledge_graph.degree(entity_id)
        features.append(degree / 10.0)  # Normalized degree
        
        # Pad to standard embedding size (256D)
        while len(features) < 256:
            features.append(np.random.normal(0, 0.1))
        
        return np.array(features[:256])
    
    def _generate_relation_embeddings(self):
        """Generate embeddings for relations"""
        
        for source, target, edge_data in self.knowledge_graph.edges(data=True):
            relation_id = edge_data.get('key', f"{source}_{target}")
            
            # Combine source and target embeddings
            source_emb = self.entity_embeddings.get(source, np.zeros(256))
            target_emb = self.entity_embeddings.get(target, np.zeros(256))
            
            # Relation-specific transformation
            relation_type = edge_data.get('relation_type', 'unknown')
            type_encoding = hash(relation_type) % 100 / 100.0
            
            # Create relation embedding
            relation_emb = np.concatenate([
                source_emb * 0.4,
                target_emb * 0.4,
                np.array([type_encoding] * 51)[:51]  # Type encoding
            ])[:256]
            
            self.relation_embeddings[relation_id] = relation_emb
    
    def _load_reasoning_patterns(self):
        """Load predefined reasoning patterns for different query types"""
        
        self.reasoning_patterns = {
            'functional_similarity': {
                'path_types': ['homologous_to', 'similar_function', 'same_pathway'],
                'max_depth': 3,
                'weight_factors': {'homologous_to': 0.9, 'similar_function': 0.8, 'same_pathway': 0.7}
            },
            
            'evolutionary_relation': {
                'path_types': ['homologous_to', 'orthologous_to', 'paralogous_to', 'diverged_from'],
                'max_depth': 4,
                'weight_factors': {'homologous_to': 0.9, 'orthologous_to': 0.85, 'paralogous_to': 0.8}
            },
            
            'interaction_network': {
                'path_types': ['interacts_with', 'regulates', 'activates', 'inhibits', 'binds_to'],
                'max_depth': 2,
                'weight_factors': {'interacts_with': 0.8, 'regulates': 0.9, 'activates': 0.85}
            },
            
            'structural_similarity': {
                'path_types': ['similar_structure', 'same_fold', 'homologous_to'],
                'max_depth': 3,
                'weight_factors': {'similar_structure': 0.9, 'same_fold': 0.95, 'homologous_to': 0.7}
            }
        }
        
        logger.info(f"Loaded {len(self.reasoning_patterns)} reasoning patterns")
    
    def _setup_insight_generation(self):
        """Setup templates for insight generation"""
        
        self.insight_templates = {
            'functional_analysis': """
            Based on graph analysis, the protein {protein_id} shows {similarity_score:.2f} functional similarity 
            to {similar_proteins}. Key functional indicators include:
            - {functional_evidence}
            - Graph reasoning suggests {primary_function}
            - Confidence: {confidence:.2f}
            """,
            
            'evolutionary_analysis': """
            Evolutionary analysis reveals {protein_id} has {evolutionary_distance} relationship 
            with {related_proteins}. Evidence includes:
            - {evolutionary_evidence}
            - Phylogenetic position suggests {evolutionary_context}
            - Divergence time estimate: {divergence_time}
            """,
            
            'interaction_analysis': """
            Interaction network analysis shows {protein_id} participates in {interaction_count} 
            known interactions. Network properties:
            - {network_properties}
            - Central role in {pathways}
            - Predicted interactions: {predicted_interactions}
            """,
            
            'structural_analysis': """
            Structural analysis indicates {protein_id} belongs to {structural_family} 
            with {structural_confidence:.2f} confidence. Structural features:
            - {structural_features}
            - Fold similarity to {similar_structures}
            - Functional implications: {structural_function_link}
            """
        }
    
    async def process_rag_query(self, query: RAGQuery) -> RAGResponse:
        """
        Main interface for processing GraphRAG queries.
        
        Args:
            query: RAGQuery object with query details
            
        Returns:
            RAGResponse with retrieved entities, reasoning paths, and insights
        """
        
        start_time = datetime.now()
        logger.info(f"Processing GraphRAG query: {query.query_type}")
        
        # Stage 1: Entity retrieval
        retrieval_start = datetime.now()
        retrieved_entities = await self._retrieve_entities(query)
        retrieval_time = (datetime.now() - retrieval_start).total_seconds()
        
        # Stage 2: Reasoning path discovery
        reasoning_start = datetime.now()
        reasoning_paths = await self._discover_reasoning_paths(query, retrieved_entities)
        reasoning_time = (datetime.now() - reasoning_start).total_seconds()
        
        # Stage 3: Insight generation
        insights = await self._generate_insights(query, retrieved_entities, reasoning_paths)
        
        # Stage 4: Confidence assessment
        confidence = self._assess_response_confidence(retrieved_entities, reasoning_paths)
        
        # Create response
        response = RAGResponse(
            response_id=f"RAG_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            query=query,
            retrieved_entities=retrieved_entities,
            reasoning_paths=reasoning_paths,
            generated_insights=insights,
            confidence_score=confidence,
            retrieval_time=retrieval_time,
            reasoning_time=reasoning_time
        )
        
        # Cache and track
        self._cache_response(response)
        self._track_performance(response)
        
        total_time = (datetime.now() - start_time).total_seconds()
        logger.info(f"GraphRAG query processed in {total_time:.3f}s with confidence {confidence:.3f}")
        
        return response
    
    async def _retrieve_entities(self, query: RAGQuery) -> List[GraphEntity]:
        """Retrieve relevant entities using semantic similarity"""
        
        # Check cache first
        cache_key = f"{query.query_text}_{query.query_type}"
        if cache_key in self.retrieval_cache:
            logger.debug("Using cached retrieval results")
            return self.retrieval_cache[cache_key]
        
        # Generate query embedding
        query_embedding = self._generate_query_embedding(query)
        
        # Compute similarities
        entity_similarities = []
        
        for entity_id, entity_emb in self.entity_embeddings.items():
            similarity = cosine_similarity(
                query_embedding.reshape(1, -1),
                entity_emb.reshape(1, -1)
            )[0, 0]
            
            entity_similarities.append((entity_id, similarity))
        
        # Sort by similarity and filter by threshold
        entity_similarities.sort(key=lambda x: x[1], reverse=True)
        
        retrieved_entities = []
        for entity_id, similarity in entity_similarities[:query.max_results]:
            if similarity >= query.confidence_threshold:
                node_data = self.knowledge_graph.nodes[entity_id]
                entity = GraphEntity(
                    entity_id=entity_id,
                    entity_type=node_data.get('entity_type', 'unknown'),
                    properties=node_data.get('properties', {}),
                    embeddings=self.entity_embeddings[entity_id],
                    confidence=similarity
                )
                retrieved_entities.append(entity)
        
        # Cache results
        self.retrieval_cache[cache_key] = retrieved_entities
        
        logger.debug(f"Retrieved {len(retrieved_entities)} entities above threshold {query.confidence_threshold}")
        return retrieved_entities
    
    def _generate_query_embedding(self, query: RAGQuery) -> np.ndarray:
        """Generate embedding for the input query"""
        
        # Simple bag-of-words + context embedding
        # In practice would use sophisticated NLP models
        
        query_words = query.query_text.lower().split()
        
        # Word-based features
        features = []
        
        # Functional keywords
        functional_keywords = ['function', 'activity', 'enzyme', 'binding', 'catalysis']
        functional_score = sum(1 for word in query_words if word in functional_keywords) / len(query_words)
        features.append(functional_score)
        
        # Structural keywords
        structural_keywords = ['structure', 'fold', 'domain', 'conformation', 'binding']
        structural_score = sum(1 for word in query_words if word in structural_keywords) / len(query_words)
        features.append(structural_score)
        
        # Evolutionary keywords
        evolutionary_keywords = ['evolution', 'homolog', 'ancestor', 'divergence', 'phylogeny']
        evolutionary_score = sum(1 for word in query_words if word in evolutionary_keywords) / len(query_words)
        features.append(evolutionary_score)
        
        # Context entity embeddings
        if query.context_entities:
            context_embeddings = [
                self.entity_embeddings.get(entity_id, np.zeros(256))
                for entity_id in query.context_entities
            ]
            if context_embeddings:
                avg_context_emb = np.mean(context_embeddings, axis=0)
                features.extend(avg_context_emb[:253].tolist())  # Use first 253 dims
        
        # Pad to 256 dimensions
        while len(features) < 256:
            features.append(0.0)
        
        return np.array(features[:256])
    
    async def _discover_reasoning_paths(
        self, 
        query: RAGQuery, 
        entities: List[GraphEntity]
    ) -> List[GraphPath]:
        """Discover reasoning paths between query context and retrieved entities"""
        
        reasoning_paths = []
        
        # Get reasoning pattern for query type
        pattern = self.reasoning_patterns.get(query.query_type, self.reasoning_patterns['functional_similarity'])
        
        # Find paths from context entities to retrieved entities
        if query.context_entities:
            for context_entity in query.context_entities:
                for target_entity in entities[:5]:  # Limit for performance
                    paths = self._find_graph_paths(
                        context_entity, 
                        target_entity.entity_id,
                        pattern['path_types'],
                        pattern['max_depth']
                    )
                    
                    for path in paths:
                        graph_path = self._create_graph_path(path, query.query_type, pattern)
                        reasoning_paths.append(graph_path)
        
        # Sort by semantic score
        reasoning_paths.sort(key=lambda x: x.semantic_score, reverse=True)
        
        # Return top paths
        return reasoning_paths[:10]
    
    def _find_graph_paths(
        self, 
        source: str, 
        target: str, 
        allowed_relations: List[str], 
        max_depth: int
    ) -> List[List[str]]:
        """Find paths between source and target nodes"""
        
        paths = []
        
        try:
            # Use NetworkX to find all simple paths
            all_paths = nx.all_simple_paths(
                self.knowledge_graph, 
                source, 
                target, 
                cutoff=max_depth
            )
            
            for path in all_paths:
                # Check if path uses allowed relation types
                valid_path = True
                for i in range(len(path) - 1):
                    edge_data = self.knowledge_graph.get_edge_data(path[i], path[i+1])
                    if edge_data:
                        # Check any edge between the nodes
                        relations = [data.get('relation_type') for data in edge_data.values()]
                        if not any(rel in allowed_relations for rel in relations):
                            valid_path = False
                            break
                    else:
                        valid_path = False
                        break
                
                if valid_path:
                    paths.append(path)
                    
                # Limit number of paths for performance
                if len(paths) >= 20:
                    break
            
        except nx.NetworkXNoPath:
            # No path exists
            pass
        except Exception as e:
            logger.warning(f"Path finding error: {e}")
        
        return paths
    
    def _create_graph_path(self, path: List[str], reasoning_type: str, pattern: Dict) -> GraphPath:
        """Create GraphPath object from node path"""
        
        # Extract relations along the path
        relations = []
        total_weight = 1.0
        
        for i in range(len(path) - 1):
            edge_data = self.knowledge_graph.get_edge_data(path[i], path[i+1])
            if edge_data:
                # Use first available relation
                first_relation = next(iter(edge_data.values()))
                relation_type = first_relation.get('relation_type', 'unknown')
                relations.append(relation_type)
                
                # Apply pattern weights
                weight_factor = pattern.get('weight_factors', {}).get(relation_type, 0.5)
                total_weight *= weight_factor
        
        # Calculate semantic score
        semantic_score = total_weight * (1.0 / len(path))  # Shorter paths get higher scores
        
        # Calculate confidence
        confidence = total_weight * 0.8  # Base confidence from path weights
        
        return GraphPath(
            path_id=f"path_{'_'.join(path)}",
            entities=path,
            relations=relations,
            path_length=len(path),
            semantic_score=semantic_score,
            confidence=confidence,
            reasoning_type=reasoning_type
        )
    
    async def _generate_insights(
        self, 
        query: RAGQuery, 
        entities: List[GraphEntity], 
        paths: List[GraphPath]
    ) -> str:
        """Generate natural language insights from graph analysis"""
        
        # Select appropriate template
        template_key = f"{query.query_type}_analysis"
        template = self.insight_templates.get(template_key, self.insight_templates['functional_analysis'])
        
        # Extract key information
        insights_data = self._extract_insights_data(query, entities, paths)
        
        # Generate insights using template
        try:
            insights = template.format(**insights_data)
        except KeyError as e:
            logger.warning(f"Template formatting error: {e}")
            insights = self._generate_fallback_insights(query, entities, paths)
        
        return insights.strip()
    
    def _extract_insights_data(
        self, 
        query: RAGQuery, 
        entities: List[GraphEntity], 
        paths: List[GraphPath]
    ) -> Dict[str, Any]:
        """Extract structured data for insight generation"""
        
        data = {}
        
        # Primary protein/entity
        if query.context_entities:
            data['protein_id'] = query.context_entities[0]
        elif entities:
            data['protein_id'] = entities[0].entity_id
        else:
            data['protein_id'] = 'unknown'
        
        # Similar proteins/entities
        if entities:
            similar_proteins = [e.entity_id for e in entities[:3]]
            data['similar_proteins'] = ', '.join(similar_proteins)
            data['similarity_score'] = np.mean([e.confidence for e in entities[:3]])
        else:
            data['similar_proteins'] = 'none found'
            data['similarity_score'] = 0.0
        
        # Functional evidence
        functional_evidence = []
        for entity in entities[:3]:
            function = entity.properties.get('function', '')
            if function:
                functional_evidence.append(f"Similar to {entity.entity_id}: {function}")
        
        data['functional_evidence'] = '; '.join(functional_evidence) if functional_evidence else 'Limited functional evidence'
        
        # Primary function prediction
        if functional_evidence:
            # Extract most common functional terms
            all_functions = ' '.join([entity.properties.get('function', '') for entity in entities])
            function_words = all_functions.lower().split()
            if function_words:
                # Simple frequency-based prediction
                from collections import Counter
                word_counts = Counter(function_words)
                common_words = [word for word, count in word_counts.most_common(3) if len(word) > 3]
                data['primary_function'] = ' '.join(common_words) if common_words else 'unknown function'
            else:
                data['primary_function'] = 'unknown function'
        else:
            data['primary_function'] = 'unknown function'
        
        # Evolutionary context
        data['evolutionary_distance'] = self._assess_evolutionary_distance(paths)
        data['related_proteins'] = data['similar_proteins']  # Same for now
        
        evolutionary_evidence = []
        for path in paths[:3]:
            if 'homologous' in ' '.join(path.relations):
                evolutionary_evidence.append(f"Homology path via {' -> '.join(path.entities[1:-1])}")
        
        data['evolutionary_evidence'] = '; '.join(evolutionary_evidence) if evolutionary_evidence else 'Limited evolutionary evidence'
        
        # Evolutionary context
        if evolutionary_evidence:
            data['evolutionary_context'] = 'conserved protein family with ancient origin'
        else:
            data['evolutionary_context'] = 'potentially novel or poorly characterized lineage'
        
        data['divergence_time'] = 'unknown without phylogenetic analysis'
        
        # Interaction analysis
        interaction_count = sum(1 for path in paths if any('interact' in rel for rel in path.relations))
        data['interaction_count'] = interaction_count
        
        # Network properties
        if interaction_count > 0:
            data['network_properties'] = f"Hub protein with {interaction_count} documented interactions"
        else:
            data['network_properties'] = "Peripheral protein with few known interactions"
        
        # Pathways
        pathway_mentions = []
        for entity in entities:
            function = entity.properties.get('function', '').lower()
            if 'pathway' in function or 'metabolism' in function:
                pathway_mentions.append(function.split()[0])
        
        data['pathways'] = ', '.join(set(pathway_mentions)) if pathway_mentions else 'unknown pathways'
        
        # Predicted interactions
        data['predicted_interactions'] = f"{len(entities)} potential interaction partners identified"
        
        # Structural analysis
        data['structural_family'] = 'unknown fold family'  # Would need structure prediction
        data['structural_confidence'] = 0.7  # Mock confidence
        data['structural_features'] = 'standard globular protein features predicted'
        data['similar_structures'] = 'no specific structural homologs identified'
        data['structural_function_link'] = 'structure-function relationship requires experimental validation'
        
        # Overall confidence
        if entities and paths:
            data['confidence'] = np.mean([e.confidence for e in entities] + [p.confidence for p in paths])
        elif entities:
            data['confidence'] = np.mean([e.confidence for e in entities])
        else:
            data['confidence'] = 0.3
        
        return data
    
    def _assess_evolutionary_distance(self, paths: List[GraphPath]) -> str:
        """Assess evolutionary distance based on reasoning paths"""
        
        if not paths:
            return 'unknown'
        
        # Look for evolutionary relation types
        evolutionary_relations = ['homologous_to', 'orthologous_to', 'paralogous_to']
        
        for path in paths:
            for relation in path.relations:
                if relation in evolutionary_relations:
                    if path.path_length <= 2:
                        return 'close evolutionary'
                    elif path.path_length <= 4:
                        return 'moderate evolutionary'
                    else:
                        return 'distant evolutionary'
        
        return 'unclear evolutionary'
    
    def _generate_fallback_insights(
        self, 
        query: RAGQuery, 
        entities: List[GraphEntity], 
        paths: List[GraphPath]
    ) -> str:
        """Generate fallback insights when template fails"""
        
        insights = f"Graph analysis for query '{query.query_text}' identified:\n"
        
        if entities:
            insights += f"- {len(entities)} relevant entities with average confidence {np.mean([e.confidence for e in entities]):.2f}\n"
            insights += f"- Top match: {entities[0].entity_id} (confidence: {entities[0].confidence:.2f})\n"
        else:
            insights += "- No entities found above confidence threshold\n"
        
        if paths:
            insights += f"- {len(paths)} reasoning paths discovered\n"
            insights += f"- Best reasoning path: {' -> '.join(paths[0].entities)} (score: {paths[0].semantic_score:.2f})\n"
        else:
            insights += "- No clear reasoning paths identified\n"
        
        insights += f"- Query type: {query.query_type}\n"
        insights += "- Recommendation: Consider expanding search criteria or adding more context"
        
        return insights
    
    def _assess_response_confidence(
        self, 
        entities: List[GraphEntity], 
        paths: List[GraphPath]
    ) -> float:
        """Assess overall confidence in the RAG response"""
        
        confidence_factors = []
        
        # Entity retrieval confidence
        if entities:
            entity_confidence = np.mean([e.confidence for e in entities])
            confidence_factors.append(entity_confidence)
        else:
            confidence_factors.append(0.1)  # Low confidence for no entities
        
        # Reasoning path confidence
        if paths:
            path_confidence = np.mean([p.confidence for p in paths])
            confidence_factors.append(path_confidence)
        else:
            confidence_factors.append(0.2)  # Low confidence for no paths
        
        # Graph connectivity (more connections = higher confidence)
        if entities:
            avg_degree = np.mean([self.knowledge_graph.degree(e.entity_id) for e in entities])
            connectivity_confidence = min(avg_degree / 10.0, 1.0)  # Normalize
            confidence_factors.append(connectivity_confidence)
        
        # Overall confidence (weighted average)
        weights = [0.4, 0.4, 0.2]
        if len(confidence_factors) == 2:
            weights = [0.6, 0.4]
        
        overall_confidence = sum(w * c for w, c in zip(weights[:len(confidence_factors)], confidence_factors))
        
        return min(max(overall_confidence, 0.1), 0.95)  # Clamp to reasonable range
    
    def _cache_response(self, response: RAGResponse):
        """Cache RAG response for future queries"""
        
        # Simple cache by query text
        cache_key = response.query.query_text
        self.retrieval_cache[cache_key] = response.retrieved_entities
        
        # Add to query history
        self.query_history.append(response)
        
        # Limit cache size
        if len(self.query_history) > 100:
            # Remove oldest entries
            self.query_history = self.query_history[-100:]
    
    def _track_performance(self, response: RAGResponse):
        """Track performance metrics"""
        
        self.performance_metrics['retrieval_time'].append(response.retrieval_time)
        self.performance_metrics['reasoning_time'].append(response.reasoning_time)
        self.performance_metrics['confidence_score'].append(response.confidence_score)
        self.performance_metrics['num_entities'].append(len(response.retrieved_entities))
        self.performance_metrics['num_paths'].append(len(response.reasoning_paths))
        
        # Keep only recent metrics
        for key in self.performance_metrics:
            self.performance_metrics[key] = self.performance_metrics[key][-100:]
    
    async def add_entity(self, entity: GraphEntity) -> bool:
        """Add new entity to the knowledge graph"""
        
        try:
            self._add_entity_to_graph(entity)
            
            # Update Neo4j if available
            if self.neo4j_client:
                await self._add_entity_to_neo4j(entity)
            
            logger.info(f"Added entity {entity.entity_id} to knowledge graph")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add entity {entity.entity_id}: {e}")
            return False
    
    async def _add_entity_to_neo4j(self, entity: GraphEntity):
        """Add entity to Neo4j database"""
        
        # Create Cypher query based on entity type
        if entity.entity_type == 'protein':
            query = """
            CREATE (p:Protein {
                protein_id: $entity_id,
                sequence: $sequence,
                organism: $organism,
                function: $function
            })
            """
            parameters = {
                'entity_id': entity.entity_id,
                'sequence': entity.properties.get('sequence', ''),
                'organism': entity.properties.get('organism', ''),
                'function': entity.properties.get('function', '')
            }
        else:
            # Generic entity
            query = """
            CREATE (e:Entity {
                entity_id: $entity_id,
                entity_type: $entity_type,
                properties: $properties
            })
            """
            parameters = {
                'entity_id': entity.entity_id,
                'entity_type': entity.entity_type,
                'properties': json.dumps(entity.properties)
            }
        
        await self.neo4j_client.execute_query(query, parameters)
    
    async def add_relation(self, relation: GraphRelation) -> bool:
        """Add new relation to the knowledge graph"""
        
        try:
            self._add_relation_to_graph(relation)
            
            # Update Neo4j if available
            if self.neo4j_client:
                await self._add_relation_to_neo4j(relation)
            
            logger.info(f"Added relation {relation.relation_id} to knowledge graph")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add relation {relation.relation_id}: {e}")
            return False
    
    async def _add_relation_to_neo4j(self, relation: GraphRelation):
        """Add relation to Neo4j database"""
        
        # Create relationship query
        query = f"""
        MATCH (source {{entity_id: $source_id}})
        MATCH (target {{entity_id: $target_id}})
        CREATE (source)-[r:{relation.relation_type.upper()} {{
            weight: $weight,
            confidence: $confidence,
            properties: $properties
        }}]->(target)
        """
        
        parameters = {
            'source_id': relation.source_entity,
            'target_id': relation.target_entity,
            'weight': relation.weight,
            'confidence': relation.confidence,
            'properties': json.dumps(relation.properties)
        }
        
        await self.neo4j_client.execute_query(query, parameters)
    
    def get_graph_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics about the knowledge graph"""
        
        stats = {
            'nodes': self.knowledge_graph.number_of_nodes(),
            'edges': self.knowledge_graph.number_of_edges(),
            'density': nx.density(self.knowledge_graph),
            'connected_components': nx.number_connected_components(self.knowledge_graph.to_undirected()),
        }
        
        # Entity type distribution
        entity_types = defaultdict(int)
        for node_id in self.knowledge_graph.nodes():
            node_data = self.knowledge_graph.nodes[node_id]
            entity_type = node_data.get('entity_type', 'unknown')
            entity_types[entity_type] += 1
        
        stats['entity_types'] = dict(entity_types)
        
        # Relation type distribution
        relation_types = defaultdict(int)
        for source, target, edge_data in self.knowledge_graph.edges(data=True):
            relation_type = edge_data.get('relation_type', 'unknown')
            relation_types[relation_type] += 1
        
        stats['relation_types'] = dict(relation_types)
        
        # Performance metrics
        if self.performance_metrics:
            stats['performance'] = {
                'avg_retrieval_time': np.mean(self.performance_metrics.get('retrieval_time', [0])),
                'avg_reasoning_time': np.mean(self.performance_metrics.get('reasoning_time', [0])),
                'avg_confidence': np.mean(self.performance_metrics.get('confidence_score', [0.5])),
                'total_queries': len(self.query_history)
            }
        
        return stats
    
    def export_graph(self, filepath: str, format: str = 'graphml'):
        """Export knowledge graph to file"""
        
        if format == 'graphml':
            nx.write_graphml(self.knowledge_graph, filepath)
        elif format == 'gexf':
            nx.write_gexf(self.knowledge_graph, filepath)
        elif format == 'json':
            graph_data = {
                'nodes': [
                    {
                        'id': node_id,
                        **self.knowledge_graph.nodes[node_id]
                    }
                    for node_id in self.knowledge_graph.nodes()
                ],
                'edges': [
                    {
                        'source': source,
                        'target': target,
                        **edge_data
                    }
                    for source, target, edge_data in self.knowledge_graph.edges(data=True)
                ]
            }
            
            with open(filepath, 'w') as f:
                json.dump(graph_data, f, indent=2, default=str)
        
        logger.info(f"Graph exported to {filepath} ({format} format)")
    
    async def optimize_graph(self):
        """Optimize graph structure and embeddings"""
        
        logger.info("Starting graph optimization")
        
        # Remove low-confidence nodes and edges
        self._prune_low_confidence_elements()
        
        # Update embeddings based on graph structure
        self._update_structural_embeddings()
        
        # Cluster similar entities
        self._cluster_similar_entities()
        
        # Update reasoning patterns based on successful queries
        self._update_reasoning_patterns()
        
        logger.info("Graph optimization completed")
    
    def _prune_low_confidence_elements(self):
        """Remove nodes and edges with low confidence scores"""
        
        # Remove low-confidence edges
        edges_to_remove = []
        for source, target, edge_data in self.knowledge_graph.edges(data=True):
            if edge_data.get('confidence', 1.0) < 0.3:
                edges_to_remove.append((source, target))
        
        for source, target in edges_to_remove:
            self.knowledge_graph.remove_edge(source, target)
        
        # Remove isolated nodes
        isolated_nodes = list(nx.isolates(self.knowledge_graph))
        self.knowledge_graph.remove_nodes_from(isolated_nodes)
        
        logger.info(f"Pruned {len(edges_to_remove)} edges and {len(isolated_nodes)} isolated nodes")
    
    def _update_structural_embeddings(self):
        """Update entity embeddings based on graph structure"""
        
        # Use node2vec or similar for structural embeddings
        # For now, simple degree-based update
        
        for node_id in self.knowledge_graph.nodes():
            if node_id in self.entity_embeddings:
                # Get structural features
                degree = self.knowledge_graph.degree(node_id)
                clustering = nx.clustering(self.knowledge_graph, node_id)
                
                # Update last dimensions with structural info
                self.entity_embeddings[node_id][-2] = degree / 10.0
                self.entity_embeddings[node_id][-1] = clustering
    
    def _cluster_similar_entities(self):
        """Cluster entities with similar embeddings"""
        
        if len(self.entity_embeddings) < 3:
            return
        
        # Get all embeddings
        entity_ids = list(self.entity_embeddings.keys())
        embeddings = np.array(list(self.entity_embeddings.values()))
        
        # Cluster using DBSCAN
        clustering = DBSCAN(eps=0.3, min_samples=2)
        cluster_labels = clustering.fit_predict(embeddings)
        
        # Add cluster information to graph
        for i, entity_id in enumerate(entity_ids):
            cluster_id = cluster_labels[i]
            if cluster_id != -1:  # Not noise
                self.knowledge_graph.nodes[entity_id]['cluster'] = int(cluster_id)
        
        num_clusters = len(set(cluster_labels)) - (1 if -1 in cluster_labels else 0)
        logger.info(f"Identified {num_clusters} entity clusters")
    
    def _update_reasoning_patterns(self):
        """Update reasoning patterns based on successful queries"""
        
        # Analyze successful query patterns
        successful_queries = [q for q in self.query_history if q.confidence_score > 0.7]
        
        if not successful_queries:
            return
        
        # Count successful relation types by query type
        pattern_updates = defaultdict(lambda: defaultdict(int))
        
        for query_response in successful_queries:
            query_type = query_response.query.query_type
            
            for path in query_response.reasoning_paths:
                for relation in path.relations:
                    pattern_updates[query_type][relation] += 1
        
        # Update pattern weights based on success rates
        for query_type, relation_counts in pattern_updates.items():
            if query_type in self.reasoning_patterns:
                total_relations = sum(relation_counts.values())
                
                for relation, count in relation_counts.items():
                    success_rate = count / total_relations
                    
                    # Update weight factor
                    if relation in self.reasoning_patterns[query_type]['weight_factors']:
                        old_weight = self.reasoning_patterns[query_type]['weight_factors'][relation]
                        new_weight = (old_weight * 0.7) + (success_rate * 0.3)  # Weighted average
                        self.reasoning_patterns[query_type]['weight_factors'][relation] = new_weight
        
        logger.info("Updated reasoning patterns based on query success rates")
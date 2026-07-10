"""
Knowledge Graph Tool Planner for BSM-BUDO-CEA
==============================================

Implements SciToolAgent-inspired KG-driven tool orchestration using MICA's existing:
- Neo4j graph database (workers, capabilities, relationships)
- Zilliz embeddings (tool descriptions, functionality)
- MessageBus communication protocol

Architecture:
- Graph stores tool nodes with inputs/outputs/functionality relations
- Embedding-based retrieval finds relevant tools for queries
- BFS expansion discovers tool chains
- Structured output with confidence scores

References:
- SciToolAgent custom_kg_retrievers.py (BFS traversal)
- kg_integration_strategy.md (MICA adaptation plan)
- Research on LangGraph, LlamaIndex KnowledgeGraphIndex patterns
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ToolCapability(BaseModel):
    """
    Describes what a tool can do
    """
    tool_id: str = Field(..., description="Unique tool identifier (e.g., 'worker.dynamo')")
    tool_name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="What the tool does")
    inputs: List[str] = Field(default_factory=list, description="Required input types")
    outputs: List[str] = Field(default_factory=list, description="Output types produced")
    categories: List[str] = Field(default_factory=list, description="Tool categories")
    confidence_score: Optional[float] = Field(None, ge=0, le=1, description="Reliability score")
    
    def matches_input(self, required_input: str) -> bool:
        """Check if tool accepts a required input type"""
        return required_input.lower() in [inp.lower() for inp in self.inputs]
    
    def produces_output(self, desired_output: str) -> bool:
        """Check if tool produces a desired output"""
        return desired_output.lower() in [out.lower() for out in self.outputs]


class ToolChain(BaseModel):
    """
    Sequence of tools to achieve a goal
    """
    chain_id: str = Field(..., description="Chain identifier")
    tools: List[ToolCapability] = Field(..., description="Ordered tools in chain")
    confidence: float = Field(..., ge=0, le=1, description="Overall confidence score")
    reasoning: str = Field(..., description="Why this chain was selected")
    estimated_duration: Optional[str] = Field(None, description="Estimated execution time")
    
    def to_execution_plan(self) -> Dict[str, Any]:
        """
        Convert to execution plan for MessageBus
        """
        return {
            'chain_id': self.chain_id,
            'steps': [
                {
                    'tool_id': tool.tool_id,
                    'tool_name': tool.tool_name,
                    'inputs': tool.inputs,
                    'outputs': tool.outputs
                }
                for tool in self.tools
            ],
            'confidence': self.confidence,
            'reasoning': self.reasoning
        }


@dataclass
class KGNode:
    """
    Node in the tool knowledge graph
    """
    node_id: str
    node_type: str  # 'tool', 'input_type', 'output_type', 'functionality'
    properties: Dict[str, Any]
    
    def __hash__(self):
        return hash(self.node_id)
    
    def __eq__(self, other):
        if not isinstance(other, KGNode):
            return False
        return self.node_id == other.node_id


@dataclass
class KGEdge:
    """
    Edge in the tool knowledge graph
    """
    source: KGNode
    target: KGNode
    relation: str  # 'requires_input', 'produces_output', 'has_functionality'
    properties: Dict[str, Any]


class ToolKnowledgeGraph:
    """
    In-memory tool knowledge graph with Neo4j backend sync
    
    Stores tools and their relationships for fast traversal
    """
    
    def __init__(self):
        self.nodes: Dict[str, KGNode] = {}
        self.edges: List[KGEdge] = []
        self.tool_capabilities: Dict[str, ToolCapability] = {}
        
        # Index for fast lookup
        self.edges_from_node: Dict[str, List[KGEdge]] = {}
        self.edges_to_node: Dict[str, List[KGEdge]] = {}
    
    def add_tool(self, capability: ToolCapability) -> None:
        """
        Add tool to knowledge graph
        
        Creates:
        - Tool node
        - Input type nodes + edges
        - Output type nodes + edges
        - Functionality nodes + edges
        """
        # Create tool node
        tool_node = KGNode(
            node_id=f"tool:{capability.tool_id}",
            node_type="tool",
            properties={
                'name': capability.tool_name,
                'description': capability.description,
                'confidence': capability.confidence_score or 0.8
            }
        )
        self.nodes[tool_node.node_id] = tool_node
        self.tool_capabilities[capability.tool_id] = capability
        
        # Create input nodes and edges
        for input_type in capability.inputs:
            input_node_id = f"input:{input_type}"
            if input_node_id not in self.nodes:
                input_node = KGNode(
                    node_id=input_node_id,
                    node_type="input_type",
                    properties={'type_name': input_type}
                )
                self.nodes[input_node_id] = input_node
            
            edge = KGEdge(
                source=self.nodes[input_node_id],
                target=tool_node,
                relation="required_by",
                properties={}
            )
            self._add_edge(edge)
        
        # Create output nodes and edges
        for output_type in capability.outputs:
            output_node_id = f"output:{output_type}"
            if output_node_id not in self.nodes:
                output_node = KGNode(
                    node_id=output_node_id,
                    node_type="output_type",
                    properties={'type_name': output_type}
                )
                self.nodes[output_node_id] = output_node
            
            edge = KGEdge(
                source=tool_node,
                target=self.nodes[output_node_id],
                relation="produces",
                properties={}
            )
            self._add_edge(edge)
        
        logger.info(f"Added tool to KG: {capability.tool_id}")
    
    def _add_edge(self, edge: KGEdge) -> None:
        """Add edge and update indexes"""
        self.edges.append(edge)
        
        source_id = edge.source.node_id
        target_id = edge.target.node_id
        
        if source_id not in self.edges_from_node:
            self.edges_from_node[source_id] = []
        self.edges_from_node[source_id].append(edge)
        
        if target_id not in self.edges_to_node:
            self.edges_to_node[target_id] = []
        self.edges_to_node[target_id].append(edge)
    
    def get_tools_with_input(self, input_type: str) -> List[ToolCapability]:
        """Find tools that accept an input type"""
        input_node_id = f"input:{input_type}"
        if input_node_id not in self.edges_from_node:
            return []
        
        tool_nodes = [
            edge.target for edge in self.edges_from_node[input_node_id]
            if edge.relation == "required_by"
        ]
        
        tool_ids = [node.node_id.replace("tool:", "") for node in tool_nodes]
        return [self.tool_capabilities[tid] for tid in tool_ids if tid in self.tool_capabilities]
    
    def get_tools_with_output(self, output_type: str) -> List[ToolCapability]:
        """Find tools that produce an output type"""
        output_node_id = f"output:{output_type}"
        if output_node_id not in self.edges_to_node:
            return []
        
        tool_nodes = [
            edge.source for edge in self.edges_to_node[output_node_id]
            if edge.relation == "produces"
        ]
        
        tool_ids = [node.node_id.replace("tool:", "") for node in tool_nodes]
        return [self.tool_capabilities[tid] for tid in tool_ids if tid in self.tool_capabilities]
    
    def bfs_tool_chain(self, 
                       start_input: str, 
                       target_output: str, 
                       max_depth: int = 5) -> List[List[ToolCapability]]:
        """
        Find tool chains from input to output using BFS
        
        Inspired by SciToolAgent custom_kg_retrievers.py
        
        Args:
            start_input: Initial input type available
            target_output: Desired output type
            max_depth: Maximum chain length
        
        Returns:
            List of tool chains (each chain is a list of tools)
        """
        # Find tools that accept start_input
        start_tools = self.get_tools_with_input(start_input)
        if not start_tools:
            logger.warning(f"No tools accept input type: {start_input}")
            return []
        
        # BFS queue: (current_tool, chain_so_far, available_outputs)
        queue: List[Tuple[ToolCapability, List[ToolCapability], Set[str]]] = []
        for tool in start_tools:
            queue.append((tool, [tool], set(tool.outputs)))
        
        found_chains = []
        visited_states = set()
        
        while queue and len(found_chains) < 10:  # Limit to 10 chains
            current_tool, chain, available_outputs = queue.pop(0)
            
            # Check if we've reached target
            if target_output in available_outputs:
                found_chains.append(chain)
                continue
            
            # Don't expand if chain too long
            if len(chain) >= max_depth:
                continue
            
            # Avoid revisiting same state
            state_key = (tuple(t.tool_id for t in chain), frozenset(available_outputs))
            if state_key in visited_states:
                continue
            visited_states.add(state_key)
            
            # Expand: find tools that can consume available outputs
            for output_type in available_outputs:
                next_tools = self.get_tools_with_input(output_type)
                for next_tool in next_tools:
                    # Avoid cycles
                    if next_tool.tool_id in [t.tool_id for t in chain]:
                        continue
                    
                    new_chain = chain + [next_tool]
                    new_outputs = available_outputs | set(next_tool.outputs)
                    queue.append((next_tool, new_chain, new_outputs))
        
        logger.info(f"Found {len(found_chains)} chains from {start_input} to {target_output}")
        return found_chains


class KGToolPlanner:
    """
    Knowledge graph-based tool planner for MICA
    
    Orchestrates tool selection using:
    1. Embedding-based retrieval (semantic matching)
    2. Graph traversal (BFS tool chains)
    3. Confidence scoring
    """
    
    def __init__(self, 
                 neo4j_client=None, 
                 zilliz_client=None,
                 embedding_model=None):
        """
        Initialize planner
        
        Args:
            neo4j_client: Optional Neo4j client for persistent graph
            zilliz_client: Optional Zilliz client for embedding search
            embedding_model: Optional model for encoding queries
        """
        self.kg = ToolKnowledgeGraph()
        self.neo4j_client = neo4j_client
        self.zilliz_client = zilliz_client
        self.embedding_model = embedding_model
        
        logger.info("KGToolPlanner initialized")
    
    def register_worker(self, 
                       worker_id: str,
                       name: str,
                       description: str,
                       inputs: List[str],
                       outputs: List[str],
                       categories: List[str] = None,
                       confidence: float = 0.8) -> None:
        """
        Register a worker as a tool in the knowledge graph
        
        Example:
            planner.register_worker(
                worker_id='worker.dynamo',
                name='Dynamo MD Simulation',
                description='Executes molecular dynamics simulations using OpenMM',
                inputs=['pdb_file', 'simulation_params'],
                outputs=['trajectory', 'energy_profile'],
                categories=['molecular_dynamics', 'simulation'],
                confidence=0.95
            )
        """
        capability = ToolCapability(
            tool_id=worker_id,
            tool_name=name,
            description=description,
            inputs=inputs,
            outputs=outputs,
            categories=categories or [],
            confidence_score=confidence
        )
        
        self.kg.add_tool(capability)
        
        # Optionally sync to Neo4j
        if self.neo4j_client:
            asyncio.create_task(self._sync_to_neo4j(capability))
    
    async def _sync_to_neo4j(self, capability: ToolCapability) -> None:
        """Sync tool capability to Neo4j graph database"""
        try:
            # Create tool node in Neo4j
            query = """
            MERGE (t:Tool {tool_id: $tool_id})
            SET t.name = $name,
                t.description = $description,
                t.confidence = $confidence,
                t.categories = $categories
            """
            await self.neo4j_client.run_query(
                query,
                tool_id=capability.tool_id,
                name=capability.tool_name,
                description=capability.description,
                confidence=capability.confidence_score or 0.8,
                categories=capability.categories
            )
            
            logger.debug(f"Synced {capability.tool_id} to Neo4j")
        except Exception as e:
            logger.error(f"Failed to sync to Neo4j: {e}")
    
    async def plan(self, 
                  goal: str,
                  available_inputs: List[str],
                  desired_outputs: List[str],
                  max_chain_length: int = 5,
                  top_k: int = 3) -> List[ToolChain]:
        """
        Plan tool chains to achieve goal
        
        Args:
            goal: Natural language description of what to achieve
            available_inputs: Input types currently available
            desired_outputs: Output types needed
            max_chain_length: Maximum tools in chain
            top_k: Number of chains to return
        
        Returns:
            List of ToolChain objects ranked by confidence
        """
        logger.info(f"Planning for goal: {goal}")
        
        # Step 1: Embedding-based tool retrieval (if available)
        semantic_candidates = []
        if self.zilliz_client and self.embedding_model:
            semantic_candidates = await self._semantic_tool_search(goal, top_k=10)
        
        # Step 2: Graph-based chain finding
        all_chains = []
        for input_type in available_inputs:
            for output_type in desired_outputs:
                chains = self.kg.bfs_tool_chain(
                    start_input=input_type,
                    target_output=output_type,
                    max_depth=max_chain_length
                )
                all_chains.extend(chains)
        
        if not all_chains:
            logger.warning("No tool chains found")
            return []
        
        # Step 3: Score and rank chains
        ranked_chains = self._score_chains(
            chains=all_chains,
            goal=goal,
            semantic_candidates=semantic_candidates
        )
        
        # Step 4: Convert to ToolChain objects
        tool_chains = []
        for i, (chain, score) in enumerate(ranked_chains[:top_k]):
            reasoning = self._generate_chain_reasoning(chain, goal, available_inputs, desired_outputs)
            
            tool_chain = ToolChain(
                chain_id=f"chain_{i+1}",
                tools=chain,
                confidence=score,
                reasoning=reasoning,
                estimated_duration=self._estimate_duration(chain)
            )
            tool_chains.append(tool_chain)
        
        logger.info(f"Planned {len(tool_chains)} tool chains")
        return tool_chains
    
    async def _semantic_tool_search(self, query: str, top_k: int = 10) -> List[ToolCapability]:
        """
        Search for tools using semantic similarity
        
        Uses Zilliz embeddings if available
        """
        # Placeholder for embedding search
        # In practice, would encode query and search Zilliz collection
        logger.debug(f"Semantic search for: {query} (top_k={top_k})")
        return []
    
    def _score_chains(self, 
                     chains: List[List[ToolCapability]],
                     goal: str,
                     semantic_candidates: List[ToolCapability]) -> List[Tuple[List[ToolCapability], float]]:
        """
        Score and rank tool chains
        
        Scoring factors:
        - Chain length (shorter is better)
        - Tool confidence scores
        - Presence in semantic candidates
        """
        scored_chains = []
        
        for chain in chains:
            # Base score: average tool confidence
            tool_scores = [t.confidence_score or 0.8 for t in chain]
            avg_confidence = sum(tool_scores) / len(tool_scores)
            
            # Length penalty (prefer shorter chains)
            length_penalty = 1.0 / (1.0 + 0.1 * len(chain))
            
            # Semantic bonus (if tools in semantic candidates)
            semantic_bonus = 0.0
            if semantic_candidates:
                for tool in chain:
                    if any(t.tool_id == tool.tool_id for t in semantic_candidates):
                        semantic_bonus += 0.1
            
            final_score = avg_confidence * length_penalty + semantic_bonus
            final_score = min(1.0, final_score)  # Cap at 1.0
            
            scored_chains.append((chain, final_score))
        
        # Sort by score descending
        scored_chains.sort(key=lambda x: x[1], reverse=True)
        return scored_chains
    
    def _generate_chain_reasoning(self, 
                                  chain: List[ToolCapability],
                                  goal: str,
                                  inputs: List[str],
                                  outputs: List[str]) -> str:
        """Generate human-readable reasoning for chain selection"""
        tool_names = [t.tool_name for t in chain]
        chain_desc = " → ".join(tool_names)
        
        reasoning = (
            f"To achieve '{goal}', execute: {chain_desc}. "
            f"This chain transforms available inputs {inputs} "
            f"into desired outputs {outputs} through {len(chain)} steps."
        )
        return reasoning
    
    def _estimate_duration(self, chain: List[ToolCapability]) -> str:
        """Estimate execution time for chain"""
        # Placeholder: in practice, query historical execution times
        if len(chain) == 1:
            return "~1 hour"
        elif len(chain) == 2:
            return "~2-3 hours"
        else:
            return f"~{len(chain)}-{len(chain)*2} hours"


# Demo function for testing
async def demo_kg_planner():
    """
    Demonstrate KG planner with example tools
    """
    planner = KGToolPlanner()
    
    # Register example tools
    planner.register_worker(
        worker_id='worker.dynamo',
        name='Dynamo MD Simulation',
        description='Executes molecular dynamics simulations using OpenMM',
        inputs=['pdb_file', 'simulation_params'],
        outputs=['trajectory', 'energy_profile'],
        categories=['molecular_dynamics'],
        confidence=0.95
    )
    
    planner.register_worker(
        worker_id='worker.chronosfold',
        name='Chronosfold ESE Extractor',
        description='Extracts Evolutionary Structure Embeddings from trajectories',
        inputs=['trajectory'],
        outputs=['ese_embeddings', 'feature_vectors'],
        categories=['embeddings', 'analysis'],
        confidence=0.90
    )
    
    planner.register_worker(
        worker_id='worker.vizier',
        name='Vizier Visualization',
        description='Creates 3D visualizations and plots from MD data',
        inputs=['trajectory', 'feature_vectors'],
        outputs=['visualization', 'plots'],
        categories=['visualization'],
        confidence=0.85
    )
    
    # Plan a workflow
    chains = await planner.plan(
        goal="Run MD simulation, extract embeddings, and visualize results",
        available_inputs=['pdb_file', 'simulation_params'],
        desired_outputs=['visualization', 'ese_embeddings'],
        max_chain_length=5,
        top_k=3
    )
    
    # Display results
    for chain in chains:
        print(f"\n{chain.chain_id} (confidence: {chain.confidence:.2f})")
        print(f"  {chain.reasoning}")
        print(f"  Tools: {' → '.join([t.tool_name for t in chain.tools])}")
        print(f"  Estimated duration: {chain.estimated_duration}")
    
    return chains


if __name__ == "__main__":
    # Run demo
    asyncio.run(demo_kg_planner())

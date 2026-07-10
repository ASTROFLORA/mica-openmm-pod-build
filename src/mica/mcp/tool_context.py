#☕ TEA Protocol: Tool Context Protocol (TCP)
#===========================================

#Implements the Tool Context Protocol from the AgentOrchestra architecture.
#Provides intelligent tool selection, context injection, and usage tracking.

#Key Features:
#- Tool Registry with metadata
#- Context injection for tool calls
#- Usage tracking and analytics


from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

@dataclass
class ToolMetadata:
    """Metadata for an MCP tool."""
    name: str
    server: str
    description: str
    input_schema: Dict[str, Any]
    capabilities: List[str] = field(default_factory=list)
    usage_count: int = 0
    avg_latency: float = 0.0
    success_rate: float = 1.0

class ToolContextProtocol:
    """
    Manages tool context, selection, and execution tracking.
    """
    
    def __init__(self):
        self.registry: Dict[str, ToolMetadata] = {}
        self.execution_log: List[Dict[str, Any]] = []
        
    def register_tool(self, tool_info: Dict[str, Any]):
        """Register a tool with the protocol."""
        tool_id = tool_info["name"]
        
        # Extract capabilities from description (simple heuristic)
        description = tool_info.get("description", "").lower()
        capabilities = []
        if "protein" in description: capabilities.append("protein_analysis")
        if "gene" in description: capabilities.append("genomics")
        if "drug" in description or "molecule" in description: capabilities.append("chemistry")
        if "paper" in description or "search" in description: capabilities.append("literature")
        
        metadata = ToolMetadata(
            name=tool_id,
            server=tool_info.get("server", "unknown"),
            description=tool_info.get("description", ""),
            input_schema=tool_info.get("input_schema", {}),
            capabilities=capabilities
        )
        
        self.registry[tool_id] = metadata
        logger.debug(f"Registered tool: {tool_id} (capabilities: {capabilities})")
        
    def select_tools_for_task(self, task_description: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Select relevant tools for a given task.
        
        In a full implementation, this would use vector embeddings.
        Here we use keyword matching as a robust fallback.
        """
        task_desc = task_description.lower()
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "by",
            "for",
            "from",
            "i",
            "in",
            "is",
            "it",
            "need",
            "of",
            "on",
            "or",
            "that",
            "the",
            "this",
            "to",
            "we",
            "with",
            # Common generic verbs that cause false positives
            "analyze",
            "analysis",
            "check",
            "compute",
            "calculate",
            "get",
            "run",
            "use",
        }
        scored_tools = []
        
        for tool_id, metadata in self.registry.items():
            score = 0
            
            # Keyword matching in description
            if metadata.description:
                desc_words = {w for w in metadata.description.lower().split() if w and w not in stopwords}
                task_words = {w for w in task_desc.split() if w and w not in stopwords}
                overlap = len(desc_words.intersection(task_words))
                score += overlap * 2
            
            # Capability matching
            for cap in metadata.capabilities:
                if cap in task_desc:
                    score += 5
            
            # Usage bonus (prefer proven tools)
            score += min(metadata.usage_count * 0.1, 2.0)
            
            if score > 0:
                scored_tools.append((score, metadata))
        
        # Sort by score desc
        scored_tools.sort(key=lambda x: x[0], reverse=True)
        
        # Return top N tools in MCP format
        return [
            {
                "name": m.name,
                "description": m.description,
                "input_schema": m.input_schema
            }
            for _, m in scored_tools[:limit]
        ]
    
    def record_execution(self, tool_name: str, success: bool, latency: float):
        """Record tool execution metrics."""
        if tool_name in self.registry:
            meta = self.registry[tool_name]
            meta.usage_count += 1
            
            # Update moving averages
            alpha = 0.1
            meta.avg_latency = (meta.avg_latency * (1 - alpha)) + (latency * alpha)
            
            # Update success rate
            current_success = 1.0 if success else 0.0
            meta.success_rate = (meta.success_rate * (1 - alpha)) + (current_success * alpha)
            
        self.execution_log.append({
            "tool": tool_name,
            "success": success,
            "latency": latency,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })


"""Agent registry definitions for Morpheus workers and external backends.

This module centralises metadata about every worker that the agentic driver
can route work to. It is designed to support hybrid execution across local
FastAPI endpoints, MCP subprocesses, and remote Vertex AI Agent Engine
instances (via the A2A protocol).

The default registry keeps all workers in simulated mode so that the system
continues to operate while Vertex connectivity is being provisioned. Once
real endpoints are available, update the configuration file or set the
``MICA_AGENT_REGISTRY`` environment variable to point at a JSON document with
live settings.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from mica.model_runtime.backends import DEFAULT_VERTEX_DRIVER_MODEL, resolve_vertex_location


class BackendType(str, Enum):
    """Supported execution backends for a worker."""

    SIMULATED = "simulated"
    LOCAL_HTTP = "local_http"
    LOCAL_MCP = "local_mcp"
    MCP_STDIO = "mcp_stdio"              # 🆕 FastMCP stdio servers (Phase 1)
    VERTEX_AGENT = "vertex_agent"
    OPENAI_NATIVE = "openai_native"      # Native OpenAI API
    CLAUDE_NATIVE = "claude_native"      # Native Anthropic Claude API
    GEMINI_NATIVE = "gemini_native"      # Native Google Gemini API
    ROUTER_NATIVE = "router_native"      # 🎯 FASE 1: Multi-LLM Router with intelligent cost optimization


@dataclass
class AgentEndpointConfig:
    """Configuration metadata for a single worker endpoint."""

    name: str
    backend: BackendType = BackendType.SIMULATED
    description: str = ""
    local_endpoint: Optional[str] = None
    mcp_command: Optional[str] = None
    agent_card_url: Optional[str] = None
    memory_scope: Optional[str] = None
    llm_model: Optional[str] = None          # For native LLM backends
    system_prompt: Optional[str] = None      # For native LLM backends
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_remote(self) -> bool:
        return self.backend == BackendType.VERTEX_AGENT

    @property
    def is_local_http(self) -> bool:
        return self.backend == BackendType.LOCAL_HTTP and self.local_endpoint is not None

    @property
    def is_local_mcp(self) -> bool:
        return self.backend == BackendType.LOCAL_MCP and self.mcp_command is not None
    
    @property
    def is_native_llm(self) -> bool:
        return self.backend in (
            BackendType.OPENAI_NATIVE, 
            BackendType.CLAUDE_NATIVE,
            BackendType.GEMINI_NATIVE,
            BackendType.ROUTER_NATIVE  # 🎯 FASE 1: Router también es LLM nativo
        )


class AgentRegistry:
    """Container for agent endpoint metadata with optional disk overrides."""

    def __init__(self, agents: Dict[str, AgentEndpointConfig]):
        self._agents = agents

    def get(self, worker_name: str) -> Optional[AgentEndpointConfig]:
        return self._agents.get(worker_name)

    def list_agents(self) -> Dict[str, AgentEndpointConfig]:
        return dict(self._agents)

    @classmethod
    def load(cls, override_path: Optional[str] = None) -> "AgentRegistry":
        """Load registry data from JSON file or return defaults."""

        path = cls._resolve_override_path(override_path)
        if path:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            agents = {
                name: AgentEndpointConfig(
                    name=name,
                    backend=BackendType(config.get("backend", BackendType.SIMULATED)),
                    description=config.get("description", ""),
                    local_endpoint=config.get("local_endpoint"),
                    mcp_command=config.get("mcp_command"),
                    agent_card_url=config.get("agent_card_url"),
                    memory_scope=config.get("memory_scope"),
                    llm_model=config.get("llm_model"),
                    system_prompt=config.get("system_prompt"),
                    extras=config.get("extras", {}),
                )
                for name, config in payload.items()
            }
            return cls(agents)

        return cls(_default_agents())

    @staticmethod
    def _resolve_override_path(user_path: Optional[str]) -> Optional[Path]:
        candidate = user_path or os.getenv("MICA_AGENT_REGISTRY")
        if not candidate:
            return None
        path = Path(candidate)
        return path if path.exists() else None


def _default_agents() -> Dict[str, AgentEndpointConfig]:
    """Baseline registry with simulated workers so tests keep running."""

    descriptions = {
        "dynamo": "Molecular dynamics / OpenMM orchestration",
        "spectra": "Protein spectral & embedding analysis",
        "embedding": "Vector embeddings (ESM-2)",
        "ncbi": "NCBI data retrieval",
        "uniprot": "UniProt integration",
        "pdb": "Protein structure analysis",
        "networks_rag": "Protein networks RAG queries",
        "sequences_rag": "Protein sequences RAG queries",
        "biological_explainer": "Biological explainer (GraphRAG)",
        "bsm": "BioSchemas transformation",
        "biodynamo_nlp": "BioDynamo natural language parser",
        "biodynamo_scaffold": "BioDynamo scaffold generator",
        "biodynamo_executor": "BioDynamo simulation executor",
        "biodynamo_driver": "Vertex orchestrator for BioDynamo workflows",
        "sampling_orchestrator": "Sampling strategy agent (RAMD, metadynamics, WE)",
        "free_energy": "Free-energy evaluation agent (MM/PBSA, TI)",
        "potential_benchmark": "Force-field benchmarking agent",
        "pharmaco_analytics": "Pharmacology analytics agent",
    }

    agents = {
        name: AgentEndpointConfig(name=name, description=desc)
        for name, desc in descriptions.items()
    }

    # Canonical multi-agent orchestration workers (LangGraph heart)
    agents["router_native"] = AgentEndpointConfig(
        name="router_native",
        backend=BackendType.ROUTER_NATIVE,
        description="Primary orchestrator agent with cost-aware model routing",
        llm_model=(os.getenv("MICA_VERTEX_MODEL") or os.getenv("VERTEX_MODEL") or DEFAULT_VERTEX_DRIVER_MODEL),
        system_prompt=(
            "You are MICA's principal orchestrator. "
            "Plan tasks, route to specialists when needed, and use available tools safely."
        ),
        extras={
            "provider_family": "vertex_gemini",
            "capability_profile": "router",
            "router_primary_provider": "vertex",
        },
    )
    agents["openai_native"] = AgentEndpointConfig(
        name="openai_native",
        backend=BackendType.OPENAI_NATIVE,
        description="OpenAI-native specialist for tool-using scientific reasoning",
        llm_model="gpt-4o",
        system_prompt=(
            "You are a scientific specialist in molecular biology and bioinformatics. "
            "Use tools when needed and cite evidence from retrieved artifacts."
        ),
        extras={"provider_family": "openai_chat", "capability_profile": "scientific_reasoning"},
    )
    agents["claude_native"] = AgentEndpointConfig(
        name="claude_native",
        backend=BackendType.CLAUDE_NATIVE,
        description="Claude-native specialist for deep analysis and synthesis",
        llm_model="claude-sonnet-4-20250514",
        system_prompt=(
            "You are an analytical research specialist. "
            "Decompose complex tasks, call tools deliberately, and return concise validated conclusions."
        ),
        extras={"provider_family": "anthropic_native", "capability_profile": "deep_analysis"},
    )
    agents["gemini_native"] = AgentEndpointConfig(
        name="gemini_native",
        backend=BackendType.GEMINI_NATIVE,
        description="Gemini-native specialist for general multimodal scientific assistance",
        llm_model="gemini-2.5-flash",
        system_prompt=(
            "You are a scientific assistant optimized for fast structured synthesis. "
            "Prefer concise, grounded outputs and preserve explicit uncertainty."
        ),
        extras={"provider_family": "google_gemini_api", "capability_profile": "fast_generalist"},
    )
    agents["vertex_gemini_native"] = AgentEndpointConfig(
        name="vertex_gemini_native",
        backend=BackendType.GEMINI_NATIVE,
        description="Gemini on Vertex AI using the canonical managed Vertex path",
        llm_model=(os.getenv("MICA_VERTEX_MODEL") or os.getenv("VERTEX_MODEL") or DEFAULT_VERTEX_DRIVER_MODEL),
        system_prompt=(
            "You are a Vertex-hosted Gemini specialist for scientific orchestration. "
            "Respond precisely and preserve operational constraints."
        ),
        extras={
            "provider_family": "vertex_gemini",
            "location": resolve_vertex_location(
                os.getenv("MICA_VERTEX_MODEL") or os.getenv("VERTEX_MODEL") or DEFAULT_VERTEX_DRIVER_MODEL,
                os.getenv("MICA_VERTEX_LOCATION") or os.getenv("VERTEX_LOCATION") or os.getenv("GCP_REGION") or os.getenv("GOOGLE_CLOUD_LOCATION"),
            ),
            "capability_profile": "vertex_gemini",
        },
    )
    agents["vertex_claude_native"] = AgentEndpointConfig(
        name="vertex_claude_native",
        backend=BackendType.CLAUDE_NATIVE,
        description="Claude on Vertex AI using Anthropic's Vertex client",
        llm_model=(os.getenv("MICA_VERTEX_CLAUDE_MODEL") or os.getenv("VERTEX_CLAUDE_MODEL") or "claude-sonnet-4-20250514"),
        system_prompt=(
            "You are a Vertex-hosted Claude specialist for deep analytical synthesis. "
            "Work carefully, use tools deliberately, and keep conclusions auditable."
        ),
        extras={
            "provider_family": "vertex_claude",
            "location": (os.getenv("MICA_VERTEX_LOCATION") or os.getenv("VERTEX_LOCATION") or os.getenv("GCP_REGION") or "us-central1"),
            "project_id": (os.getenv("MICA_VERTEX_PROJECT_ID") or os.getenv("VERTEX_PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")),
            "capability_profile": "vertex_claude",
        },
    )

    return agents

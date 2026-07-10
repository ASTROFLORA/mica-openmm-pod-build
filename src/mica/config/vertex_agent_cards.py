"""Prototype Vertex AI Agent Engine card specifications for BioDynamo agents.

This module defines production-leaning card templates that describe how the
BioDynamo multi-agent ecosystem should be exposed through the A2A protocol.
They provide a consistent place to track agent capabilities, versions, and
preferred transports while keeping the codebase free from hard dependencies on
the Google Agent Development Kit (ADK).  The call sites can attempt to build a
real ``AgentCard`` only when the A2A SDK is present; otherwise they can fall
back to the plain data structures defined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class VertexAgentCardSpec:
    """Portable description of an agent card.

    The ``build_card`` helper converts the spec into an ``AgentCard`` instance
    when the ``a2a-sdk`` dependency is installed.  Downstream callers can
    serialise the dictionary payload or inject the fully realised card into the
    Vertex AI Agent Engine without touching this module.
    """

    agent_id: str
    title: str
    description: str
    version: str = "0.1.0"
    capabilities: Dict[str, Any] = field(default_factory=dict)
    preferred_transport: str = "http_json"
    metadata: Optional[Dict[str, Any]] = None

    def build_card(self):  # pragma: no cover - requires optional dependency
        """Return an ``AgentCard`` instance when the A2A SDK is available."""

        try:
            from a2a.server.agent_card import AgentCard, TransportProtocol  # type: ignore
        except Exception as exc:  # pragma: no cover - env specific
            raise RuntimeError(
                "a2a-sdk is required to build AgentCard objects"
            ) from exc

        transport = getattr(TransportProtocol, self.preferred_transport, None)
        if transport is None:
            transport = TransportProtocol.http_json

        return AgentCard(
            id=self.agent_id,
            title=self.title,
            description=self.description,
            agent_version=self.version,
            preferred_transport=transport,
            capabilities=self.capabilities,
            metadata=self.metadata,
        )


BIO_DYNAMO_AGENT_SPECS: Dict[str, VertexAgentCardSpec] = {
    "biodynamo_driver": VertexAgentCardSpec(
        agent_id="biodynamo_driver",
        title="BioDynamo Vertex Orchestrator",
        description=(
            "Coordinates BioDynamo sampling, iMMD, free-energy, and reporting "
            "agents using Memory Bank state and TransportLayer routing."
        ),
        version="0.1.0",
        capabilities={
            "workflows": {
                "supported": [
                    "sampling_optimization",
                    "immd_cycle",
                    "free_energy_eval",
                    "report_generation",
                ],
                "memory_scopes": [
                    "sampling_state",
                    "immd_state",
                    "free_energy_cache",
                ],
            }
        },
    ),
    "sampling_orchestrator": VertexAgentCardSpec(
        agent_id="sampling_orchestrator",
        title="Sampling Strategy Orchestrator",
        description=(
            "Optimises exploration vs. refinement by combining MDGraphEmb "
            "uncertainty, RAMD statistics, and trajectory lineage metadata."
        ),
        capabilities={
            "sampling": {
                "methods": [
                    "ramd",
                    "metadynamics",
                    "weighted_ensemble",
                ],
                "inputs": [
                    "state_histogram",
                    "uncertainty_map",
                    "residence_time_targets",
                ],
                "outputs": [
                    "schedule",
                    "recommended_method",
                    "confidence",
                ],
            }
        },
    ),
    "free_energy": VertexAgentCardSpec(
        agent_id="free_energy",
        title="Free Energy Evaluation Agent",
        description=(
            "Runs MM/PBSA and thermodynamic integration on trajectory bundles "
            "and persists delta-G metrics into the Artifact Registry."
        ),
        capabilities={
            "calculations": {
                "techniques": ["mm_pbsa", "ti"],
                "trajectory_formats": ["dcd", "xtc"],
                "ensemble_support": True,
            }
        },
    ),
    "potential_benchmark": VertexAgentCardSpec(
        agent_id="potential_benchmark",
        title="Potential Benchmark Agent",
        description=(
            "Benchmarks learned force fields (TorchMD-Net, ATOM operator) "
            "against classical references and reports drift statistics."
        ),
        capabilities={
            "benchmark": {
                "models": ["torchmd_net", "atom_operator", "amber"],
                "metrics": ["rmsd", "energy_drift", "force_rmse"],
            }
        },
    ),
    "pharmaco_analytics": VertexAgentCardSpec(
        agent_id="pharmaco_analytics",
        title="Pharmaco Analytics Agent",
        description=(
            "Translates MD observables into medicinal chemistry insights, "
            "linking binding kinetics with ADME/Tox predictions."
        ),
        capabilities={
            "analysis": {
                "reports": ["sar_summary", "adme_profile", "tox_alerts"],
                "tools": ["admet_mcp", "metabolite_predictor"],
            }
        },
    ),
}


def get_agent_spec(agent_name: str) -> Optional[VertexAgentCardSpec]:
    """Return a card specification by name if it exists."""

    return BIO_DYNAMO_AGENT_SPECS.get(agent_name)


def list_agent_specs() -> Dict[str, VertexAgentCardSpec]:
    """Return a shallow copy of all registered card specifications."""

    return dict(BIO_DYNAMO_AGENT_SPECS)

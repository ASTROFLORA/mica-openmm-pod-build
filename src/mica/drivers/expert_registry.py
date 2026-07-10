"""
S1.5 — Formal ExpertRegistry.

Replaces the ``_EXPERT_POOL`` class-level dict in ``agentic_driver.py``
with a typed, introspectable registry.

Design goals
~~~~~~~~~~~~
* Drop-in backward compatibility:  ``registry[name]`` returns the same
  ``{"description": ..., "system": ...}`` dict shape that the
  ``consult_expert`` handler already expects.
* Typed ``ExpertDefinition`` — validated, serialisable, extensible.
* Capability-based filtering for future coordination_mode routing.
* Distinct from ``config/agent_registry.py`` (transport-layer endpoints).

NOTE: This module is *pure data* — no I/O, no async, no imports beyond
stdlib + typing.  Tests run in < 0.1 s.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ────────────────────────────────────────────────────────────────────
# Expert definition
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExpertDefinition:
    """Immutable configuration of a single specialist expert."""

    name: str
    description: str
    system_prompt: str
    capabilities: Tuple[str, ...] = ()
    max_iterations: int = 4
    max_response_tokens: int = 400

    # ── Backward compat ────────────────────────────────────────────
    def to_pool_entry(self) -> dict:
        """Return the legacy ``{"description": ..., "system": ...}`` dict."""
        return {
            "description": self.description,
            "system": self.system_prompt,
        }

    # ── Serialisation ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "capabilities": list(self.capabilities),
            "max_iterations": self.max_iterations,
            "max_response_tokens": self.max_response_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExpertDefinition":
        return cls(
            name=d["name"],
            description=d["description"],
            system_prompt=d["system_prompt"],
            capabilities=tuple(d.get("capabilities", ())),
            max_iterations=d.get("max_iterations", 4),
            max_response_tokens=d.get("max_response_tokens", 400),
        )


# ────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────

class ExpertRegistry:
    """
    Typed, introspectable registry of specialist experts.

    Supports direct item access (``registry[name]``) which returns the
    *legacy pool entry* for backward compatibility with the
    ``consult_expert`` handler.
    """

    def __init__(self, experts: Optional[Iterable[ExpertDefinition]] = None):
        self._experts: Dict[str, ExpertDefinition] = {}
        for exp in (experts or []):
            self.register(exp)

    # ── Mutation ───────────────────────────────────────────────────
    def register(self, expert: ExpertDefinition) -> None:
        """Add or replace an expert in the registry."""
        self._experts[expert.name] = expert

    def unregister(self, name: str) -> bool:
        """Remove an expert.  Returns True if it existed."""
        return self._experts.pop(name, None) is not None

    # ── Lookup ─────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[ExpertDefinition]:
        """Return expert by name, or None."""
        return self._experts.get(name)

    def __getitem__(self, name: str) -> dict:
        """Legacy dict-style access: ``registry["biophysics_idp"]``."""
        exp = self._experts.get(name)
        if exp is None:
            raise KeyError(name)
        return exp.to_pool_entry()

    def __contains__(self, name: str) -> bool:
        return name in self._experts

    def __len__(self) -> int:
        return len(self._experts)

    def __iter__(self):
        return iter(self._experts)

    # ── Queries ────────────────────────────────────────────────────
    def list_all(self) -> List[ExpertDefinition]:
        """All registered experts in insertion order."""
        return list(self._experts.values())

    def list_names(self) -> List[str]:
        """All expert names."""
        return list(self._experts.keys())

    def filter_by_capability(self, capability: str) -> List[ExpertDefinition]:
        """Return experts whose capabilities include *capability*."""
        return [
            e for e in self._experts.values()
            if capability in e.capabilities
        ]

    # ── Serialisation ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "experts": [e.to_dict() for e in self._experts.values()],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExpertRegistry":
        experts = [ExpertDefinition.from_dict(entry) for entry in d.get("experts", [])]
        return cls(experts)

    # ── Legacy compat ──────────────────────────────────────────────
    def as_pool_dict(self) -> Dict[str, dict]:
        """Return full ``_EXPERT_POOL``-shaped dict for backward compat."""
        return {name: exp.to_pool_entry() for name, exp in self._experts.items()}


# ────────────────────────────────────────────────────────────────────
# Default pool — the 4 original experts from agentic_driver.py
# ────────────────────────────────────────────────────────────────────

_DEFAULT_EXPERTS: Sequence[ExpertDefinition] = (
    ExpertDefinition(
        name="biophysics_idp",
        description="Expert in intrinsically disordered proteins (IDPs/IDRs) in kinases",
        system_prompt=(
            "You are an expert in biophysics of intrinsically disordered regions (IDPs/IDRs).\n"
            "Your specialty: how disordered regions couple with catalytic domains in kinases.\n"
            "\n"
            "WHEN THE DRIVER CONSULTS YOU:\n"
            "1. Respond directly from your domain knowledge.\n"
            "2. If you need specific evidence, use search_literature with ultra-short queries.\n"
            "3. Register key findings with cite_finding; gaps with identify_gap.\n"
            "4. Explicitly connect your response with the context the driver gave you.\n"
            "\n"
            "FORMAT: Dense response, max 400 tokens. No generic lists.\n"
            "Speak in first person as a scientific colleague to the driver, not as an assistant.\n"
        ),
        capabilities=("idp", "kinase", "disorder", "biophysics"),
    ),
    ExpertDefinition(
        name="structural_biology",
        description="Expert in protein structure, allosteric sites, PDB, cryo-EM",
        system_prompt=(
            "You are an expert in structural biology with focus on allosteric sites and cryptic pockets.\n"
            "Your specialty: how PDB/cryo-EM data reveal mechanisms that sequence alone cannot.\n"
            "\n"
            "WHEN THE DRIVER CONSULTS YOU:\n"
            "1. Interpret the structural context that the driver gives you.\n"
            "2. Use search_literature to find relevant structures or allosteric mechanisms.\n"
            "3. Point out pharmacological implications of the sites you describe.\n"
            "4. Be specific: name residues, domains, conformations when possible.\n"
            "\n"
            "FORMAT: Max 400 tokens. No generic introductions.\n"
        ),
        capabilities=("structure", "allosteric", "pdb", "cryo-em"),
    ),
    ExpertDefinition(
        name="pharmacology",
        description="Expert in translational pharmacology, ADMET, clinical therapeutic window",
        system_prompt=(
            "You are an expert in translational pharmacology — from molecular mechanism to patient.\n"
            "Your specialty: therapeutic window, ADMET, selectivity, on/off-target effects, clinical trials.\n"
            "\n"
            "WHEN THE DRIVER CONSULTS YOU:\n"
            "1. Contextualize the target or compound in real clinical terms.\n"
            "2. Use search_literature to find relevant clinical/preclinical evidence.\n"
            "3. Point out pharmacological risks that pure mechanistic reasoning misses.\n"
            "4. Respond from the perspective of a pharmacologist who has seen similar compounds fail.\n"
            "\n"
            "FORMAT: Max 400 tokens. Concrete data, not generalities.\n"
        ),
        capabilities=("admet", "clinical", "pharmacology", "selectivity"),
    ),
    ExpertDefinition(
        name="bioinformatics",
        description="Expert in domain architecture, evolutionary conservation, splicing, variants",
        system_prompt=(
            "You are an expert in protein bioinformatics: domain architecture, conservation,\n"
            "alternative splicing, pathogenic variants, and contextualized sequence analysis.\n"
            "\n"
            "WHEN THE DRIVER CONSULTS YOU:\n"
            "1. Interpret the domain architecture or sequence the driver describes.\n"
            "2. Use search_literature to find conservation or variant data.\n"
            "3. Connect sequence patterns with known function or pathology.\n"
            "4. Indicate which computational analyses would resolve the driver's question.\n"
            "\n"
            "FORMAT: Max 400 tokens. Explicit naming of residues and domains.\n"
        ),
        capabilities=("domain", "conservation", "splicing", "variants"),
    ),
)


def build_default_registry() -> ExpertRegistry:
    """Factory: return an ExpertRegistry pre-loaded with the 4 default experts."""
    return ExpertRegistry(_DEFAULT_EXPERTS)

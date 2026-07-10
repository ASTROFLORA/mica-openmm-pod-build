"""Specialist LLM Runtime — governed single-turn execution for specialist sub-agents.

SPEC-LLMU Wave 0 implementation. This module provides:

* :class:`SpecialistLLMConfig` – per-call LLM configuration
* :class:`SpecialistLLMResult` – raw LLM result with provenance
* :class:`SpecialistExecutionResult` – typed specialist result (replaces Dict[str, Any])
* :class:`SpecialistTaskEnvelope` – A2A-like internal task state model
* :class:`EvidenceFusionVerdict` – structured debate/fusion output
* :class:`SpecialistLLMRuntime` – governed provider-routed LLM runtime

Consumes :class:`ProviderRegistry` from ``agentic/core.py`` and
:class:`ModelAdapterFactory` from ``model_runtime/factory.py``.
Does NOT depend on any driver.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from mica.env_aliases import bootstrap_runtime_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wave 0 data contracts
# ---------------------------------------------------------------------------


@dataclass
class SpecialistLLMConfig:
    """Lightweight LLM config for specialist sub-agent calls."""

    provider_id: str = ""  # empty → env default
    model_id: Optional[str] = None  # None → provider default model
    max_tokens: int = 2000
    temperature: Optional[float] = None  # None → provider default
    budget_max_tokens: int = 8000  # per-specialist context budget
    enable_tools: bool = False
    enable_event_stream: bool = False
    model_tier: Literal["small", "medium", "frontier"] = "medium"
    cost_regime: Literal["budget", "balanced", "premium"] = "balanced"


@dataclass
class SpecialistLLMResult:
    """Raw result from a single specialist LLM completion."""

    text: str
    provider_id: str
    model_id: str
    latency_s: float
    tokens_used: int
    cost_usd: float
    ok: bool
    error: Optional[str] = None


@dataclass
class SpecialistExecutionResult:
    """Typed result contract — replaces raw ``Dict[str, Any]`` everywhere.

    All 7 provenance fields (provider_id … cost_usd) are MANDATORY
    per §11.1 provenance chain.
    """

    specialist_id: str
    answer: str
    status: Literal["SUCCESS", "PARTIAL", "FAILED", "STUB", "DEGRADED"]

    # Provider traceability (AP-003)
    provider_id: str
    model_id: str
    backend_used: Literal["governed", "legacy_fallback", "stub"]
    latency_s: float

    # Token + cost tracking
    tokens_prompt: int
    tokens_completion: int
    cost_usd: float

    # MSRP chain preservation (AP-002)
    msrp_chain: Optional[Dict[str, Any]] = None
    confidence: float = 0.5
    literature_consulted: Optional[List[str]] = None
    execution_time_ms: int = 0

    # Error context (AP-001)
    error: Optional[str] = None
    error_source: Optional[Literal["llm", "enrichment", "tool", "system"]] = None

    # Quality gate metadata (bridge to AGENT-B)
    quality_score: Optional[float] = None
    quality_tier: Optional[str] = None

    # Capability authority (Convergence §5.3)
    capability_bundle: Optional[List[str]] = None
    required_capabilities_missing: Optional[List[str]] = None
    degraded: bool = False
    degrade_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for legacy callers that still expect dict."""
        d: Dict[str, Any] = {
            "specialist_id": self.specialist_id,
            "answer": self.answer,
            "status": self.status,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "backend_used": self.backend_used,
            "latency_s": self.latency_s,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "cost_usd": self.cost_usd,
            "confidence": self.confidence,
            "execution_time_ms": self.execution_time_ms,
            "degraded": self.degraded,
        }
        if self.msrp_chain is not None:
            d["msrp_chain"] = self.msrp_chain
        if self.literature_consulted is not None:
            d["literature_consulted"] = self.literature_consulted
        if self.error is not None:
            d["error"] = self.error
            d["error_source"] = self.error_source
        if self.quality_score is not None:
            d["quality_score"] = self.quality_score
            d["quality_tier"] = self.quality_tier
        if self.degrade_reason is not None:
            d["degrade_reason"] = self.degrade_reason
        if self.capability_bundle is not None:
            d["capability_bundle"] = self.capability_bundle
        if self.required_capabilities_missing:
            d["required_capabilities_missing"] = self.required_capabilities_missing
        return d


@dataclass
class SpecialistTaskEnvelope:
    """A2A-like internal task state model (Convergence §5.2).

    Gives successor agents a typed task state without HTTP transport.
    """

    task_id: str = ""
    specialist_id: str = ""
    capability_bundle: Optional[List[str]] = None
    required_capabilities: Optional[List[str]] = None
    optional_capabilities: Optional[List[str]] = None
    status: Literal[
        "submitted", "working", "input_required",
        "completed", "failed", "degraded",
    ] = "submitted"
    backend_used: Literal["governed", "legacy_fallback", "stub"] = "governed"
    downgrade_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = uuid.uuid4().hex[:12]


@dataclass
class EvidenceFusionVerdict:
    """Structured debate/fusion output (Convergence §5.4).

    Replaces boolean consensus with routing-actionable fusion.
    """

    supporting_specialists: List[str] = field(default_factory=list)
    contradicting_specialists: List[str] = field(default_factory=list)
    weighted_confidence: float = 0.0
    unresolved_conflict: bool = False
    recommended_action: Literal[
        "accept", "retry", "route_alt_specialist", "degrade",
    ] = "accept"


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

# Rough token estimation (4 chars ≈ 1 token for English text)
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Cheap token estimate for budget gating (AP-007)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


class SpecialistLLMRuntime:
    """Governed LLM runtime for specialist sub-agent execution.

    Consumes ``ProviderRegistry`` from ``agentic/core.py`` and
    ``ModelAdapterFactory`` from ``model_runtime/factory.py``.

    This is stateless and does NOT depend on any driver.
    """

    def __init__(
        self,
        registry: Optional[Any] = None,
        *,
        default_provider: Optional[str] = None,
    ) -> None:
        bootstrap_runtime_env()
        # Lazy import to avoid circular dependency
        if registry is None:
            from mica.agentic.core import ProviderRegistry
            registry = ProviderRegistry.from_env()
        self._registry = registry
        self._default_provider = (
            default_provider
            or os.getenv("MICA_SPECIALIST_PROVIDER", "")
            or "deepinfra"
        )

    # ------------------------------------------------------------------
    # Provider / model resolution
    # ------------------------------------------------------------------

    def _resolve_provider_id(self, config: SpecialistLLMConfig) -> str:
        """Resolve the effective provider id from config → env → registry."""
        if config.provider_id:
            return config.provider_id
        if self._default_provider:
            return self._default_provider
        # Pick first available provider in registry
        providers = getattr(self._registry, "_providers", {})
        if providers:
            # Prefer deepinfra > fireworks > openai > anything
            for pref in ("deepinfra", "fireworks", "openai", "anthropic", "google"):
                if pref in providers:
                    return pref
            return next(iter(providers))
        return "deepinfra"  # final fallback

    def _resolve_model_id(
        self, provider_id: str, config: SpecialistLLMConfig,
    ) -> str:
        """Resolve model id honoring tier selection (§11.3)."""
        if config.model_id:
            return config.model_id
        # Tier-based resolution
        if config.model_tier == "small" and config.cost_regime == "budget":
            tier_models = {
                "fireworks": "accounts/fireworks/models/llama-v3p1-8b-instruct",
                "openai": "gpt-4o-mini",
                "anthropic": "claude-3-5-haiku-20241022",
                "google": "gemini-2.0-flash-lite",
            }
            if provider_id in tier_models:
                return tier_models[provider_id]
        if config.model_tier == "frontier" or config.cost_regime == "premium":
            tier_models = {
                "fireworks": "accounts/fireworks/models/llama-v3p1-405b-instruct",
                "openai": "gpt-4o",
                "anthropic": "claude-sonnet-4-20250514",
                "google": "gemini-2.5-pro",
            }
            if provider_id in tier_models:
                return tier_models[provider_id]
        # Default: use registry's default for provider
        try:
            providers = getattr(self._registry, "_providers", {})
            cfg = providers.get(provider_id)
            if cfg:
                return cfg.default_model
        except Exception:
            pass
        return "gpt-4o-mini"

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def complete(
        self,
        query: str,
        system_prompt: str,
        config: Optional[SpecialistLLMConfig] = None,
    ) -> SpecialistLLMResult:
        """Single-turn governed completion.

        Resolves provider → builds descriptor → invokes backend → returns
        typed result with full provenance.
        """
        cfg = config or SpecialistLLMConfig()
        provider_id = self._resolve_provider_id(cfg)
        model_id = self._resolve_model_id(provider_id, cfg)

        # Budget gate (AP-007)
        prompt_tokens_est = estimate_tokens(system_prompt) + estimate_tokens(query)
        if prompt_tokens_est > cfg.budget_max_tokens:
            logger.warning(
                "Specialist prompt (%d est. tokens) exceeds budget (%d). "
                "Truncating query.",
                prompt_tokens_est,
                cfg.budget_max_tokens,
            )
            # Truncate query to fit budget, preserving system prompt
            sys_tokens = estimate_tokens(system_prompt)
            available = max(200, cfg.budget_max_tokens - sys_tokens)
            query = query[: available * _CHARS_PER_TOKEN]

        t0 = time.monotonic()

        try:
            # Build descriptor and backend
            descriptor = self._registry.get_descriptor(provider_id, model_id)
            from mica.model_runtime.factory import ModelAdapterFactory
            backend = ModelAdapterFactory.build(descriptor)

            # Invoke
            raw = await backend.invoke(
                prompt=query,
                system_prompt=system_prompt,
                metadata={
                    "specialist_runtime": True,
                    "max_tokens": cfg.max_tokens,
                    "temperature": cfg.temperature,
                },
            )

            elapsed = time.monotonic() - t0
            ok = raw.get("status") == "SUCCESS"
            usage = raw.get("usage") or {}
            tokens = usage.get("total_tokens", 0)

            return SpecialistLLMResult(
                text=raw.get("response", "") if ok else "",
                provider_id=provider_id,
                model_id=raw.get("model", model_id),
                latency_s=round(elapsed, 3),
                tokens_used=tokens,
                cost_usd=0.0,  # real cost estimation deferred to Wave 2
                ok=ok,
                error=raw.get("error") if not ok else None,
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error(
                "SpecialistLLMRuntime.complete() failed provider=%s model=%s: %s",
                provider_id, model_id, exc,
            )
            return SpecialistLLMResult(
                text="",
                provider_id=provider_id,
                model_id=model_id,
                latency_s=round(elapsed, 3),
                tokens_used=0,
                cost_usd=0.0,
                ok=False,
                error=str(exc),
            )

    async def batch_complete(
        self,
        queries: List[Tuple[str, str, Optional[SpecialistLLMConfig]]],
        total_budget_usd: Optional[float] = None,
        max_concurrent: int = 5,
    ) -> List[SpecialistLLMResult]:
        """Execute multiple specialist queries in parallel (§11.2).

        Each specialist gets independent config (provider, model, budget).
        *max_concurrent* caps simultaneous LLM calls (F-4 cost guardrail).
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _limited(q: str, sp: str, cfg: Optional[SpecialistLLMConfig]) -> SpecialistLLMResult:
            async with semaphore:
                return await self.complete(q, sp, cfg)

        tasks = [
            _limited(q, sys_p, cfg)
            for q, sys_p, cfg in queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[SpecialistLLMResult] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(SpecialistLLMResult(
                    text="", provider_id="unknown", model_id="unknown",
                    latency_s=0.0, tokens_used=0, cost_usd=0.0,
                    ok=False, error=str(r),
                ))
            else:
                out.append(r)
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def available_providers(self) -> List[str]:
        """List provider ids available in the registry."""
        providers = getattr(self._registry, "_providers", {})
        return list(providers.keys())

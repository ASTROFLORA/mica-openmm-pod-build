"""Unified agentic core for the MICA platform.

This module provides:

* :class:`ProviderConfig` / :class:`ProviderRegistry` – a single registry of
  all configured LLM providers, lazily initialised from environment variables.
* :class:`ModelHandle` – lightweight reference to a specific provider + model.
* :class:`LoopConfig` – tuning knobs for the agentic loop.
* :class:`AgenticLoop` – the streaming *while-true* loop that calls the model,
  executes tool calls, and re-submits results until the model produces a final
  text response, the iteration cap is hit, or an abort signal is raised.

Architecture is inspired by `OpenCode's SessionProcessor
<https://github.com/nichochar/opencode>`_ and designed to replace the
single-shot helpers in ``mica.llm_backends`` with a production-grade iterative
agent runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from .events import (
    AnyLoopEvent,
    ContextCompacted,
    ContextOverflow,
    Error,
    LoopFinish,
    RetryWait,
    StepFinish,
    StreamStart,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)
from mica.env_aliases import bootstrap_runtime_env
from .retry import RetryPolicy
from .safety import ContextTracker, DoomLoopDetector, SpendLedger, ToolTruncator, compact_messages, llm_compact_messages
from mica.model_runtime import ModelDescriptor
from mica.model_runtime.backends import (
    DEFAULT_DEEPINFRA_DRIVER_MODEL,
    DEFAULT_FIREWORKS_DRIVER_MODEL,
    DEFAULT_VERTEX_DRIVER_MODEL,
    is_vertex_saturation_error,
    resolve_optional_vertex_fallback_model,
    resolve_optional_vertex_global_location_retry,
    resolve_vertex_location,
)

logger = logging.getLogger(__name__)


def _resolve_openai_compat_timeout_seconds(provider_id: str) -> float:
    base_timeout = float(os.getenv("MICA_OPENAI_COMPAT_TIMEOUT_SEC", "30") or "30")
    if provider_id == "deepinfra":
        raw = os.getenv("MICA_DEEPINFRA_TIMEOUT_SEC")
        if raw not in (None, ""):
            return float(raw)
        return max(base_timeout, 120.0)
    return base_timeout

# Type alias for tool executor callbacks supplied by the caller.
ToolExecutor = Callable[[str, str, Dict[str, Any]], Awaitable[str]]


# ---------------------------------------------------------------------------
# G2: Remediation hints for LoopFinish termination events
# ---------------------------------------------------------------------------

_REMEDIATION_HINTS: Dict[str, str] = {
    "max_iterations": (
        "The task required more steps than allowed. Try: "
        "(1) breaking the task into smaller sub-questions, "
        "(2) using 'deep' depth preset for exhaustive research, or "
        "(3) using SMIC tools for structural/pocket analysis tasks."
    ),
    "doom_loop": (
        "The agent repeated the same tool call without progress. This usually means "
        "the task requires a capability not available in the current toolset. Try: "
        "(1) rephrasing your question with more specific constraints, "
        "(2) requesting SMIC structural analysis directly, or "
        "(3) checking if required data (PDB, sequences) is in your workspace."
    ),
    "budget_exceeded": (
        "The cost or token budget for this run was exhausted. Try: "
        "(1) narrowing the scope of your question, "
        "(2) increasing the budget limit if the task is critical, or "
        "(3) using 'fast' depth preset for a quicker analysis."
    ),
    "context_overflow": (
        "The conversation history exceeded the context window. Try: "
        "(1) starting a new session for the next question, "
        "(2) asking a more focused question, or "
        "(3) referencing specific proteins/papers instead of broad topics."
    ),
}


def _remediation_hint(
    finish_reason: str, total_tokens: int = 0, total_cost: float = 0.0
) -> str:
    """Return a user-facing remediation hint for a given finish_reason."""
    base = _REMEDIATION_HINTS.get(finish_reason, "")
    if not base:
        return ""
    suffix_parts: list[str] = []
    if total_tokens > 0:
        suffix_parts.append(f"tokens_used={total_tokens}")
    if total_cost > 0:
        suffix_parts.append(f"cost=${total_cost:.4f}")
    if suffix_parts:
        return f"{base} [{', '.join(suffix_parts)}]"
    return base

_NEGATIVE_MEMORY_MODES = {"full", "semi_blind", "blind"}
_TOMBSTONE_CLASSES = {"operational", "archaeological", "heretical"}


def normalize_negative_memory_mode(mode: Any) -> str:
    normalized = str(mode or "full").strip().lower()
    if normalized in _NEGATIVE_MEMORY_MODES:
        return normalized
    return "full"


def normalize_tombstone_class(tombstone: Dict[str, Any]) -> str:
    raw = str(
        tombstone.get("tombstone_class")
        or tombstone.get("class")
        or "operational"
    ).strip().lower()
    if raw in _TOMBSTONE_CLASSES:
        return raw
    return "operational"


def visible_tombstone_classes(
    packet: Optional[Dict[str, Any]],
    *,
    negative_memory_mode: Any = "full",
    override: Optional[Tuple[str, ...]] = None,
) -> Tuple[str, ...]:
    if override:
        filtered = tuple(
            value for value in override
            if str(value or "").strip().lower() in _TOMBSTONE_CLASSES
        )
        if filtered:
            return filtered

    mode = normalize_negative_memory_mode(negative_memory_mode)
    packet_values = tuple(
        str(value or "").strip().lower()
        for value in list((packet or {}).get("visible_tombstone_classes") or [])
        if str(value or "").strip().lower() in _TOMBSTONE_CLASSES
    )
    if packet_values:
        return packet_values
    if mode == "full":
        return ("operational", "archaeological", "heretical")
    if mode == "semi_blind":
        return ("archaeological", "heretical")
    return tuple()


def tombstone_prunes_in_mode(
    tombstone: Dict[str, Any],
    *,
    negative_memory_mode: Any = "full",
    visible_classes: Optional[Tuple[str, ...]] = None,
    packet: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True only when the active mode should hard-prune this tombstone.

    Phase 3A intentionally limits hard pruning to operational tombstones.
    Archaeological and heretical classes become soft-repulsion / appeal surfaces
    in later phases once warning injection and anomaly-mode loops exist.
    """
    mode = normalize_negative_memory_mode(negative_memory_mode)
    allowed = visible_tombstone_classes(packet, negative_memory_mode=mode, override=visible_classes)
    tombstone_class = normalize_tombstone_class(tombstone)
    if tombstone_class != "operational":
        return False
    if tombstone_class not in allowed:
        return False
    return str(tombstone.get("action") or "prune_context").strip().lower() == "prune_context"


def build_negative_memory_summary(
    packet: Optional[Dict[str, Any]],
    *,
    negative_memory_mode: Any = "full",
    visible_classes: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    packet_dict = packet or {}
    tombstones = [
        tombstone
        for tombstone in list(packet_dict.get("branch_tombstones") or [])
        if isinstance(tombstone, dict)
    ]
    classes = visible_tombstone_classes(packet_dict, negative_memory_mode=negative_memory_mode, override=visible_classes)
    operational = [t for t in tombstones if normalize_tombstone_class(t) == "operational"]
    archaeological = [t for t in tombstones if normalize_tombstone_class(t) == "archaeological"]
    heretical = [t for t in tombstones if normalize_tombstone_class(t) == "heretical"]
    return {
        "negative_memory_mode": normalize_negative_memory_mode(negative_memory_mode),
        "visible_tombstone_classes": list(classes),
        "total_tombstones": len(tombstones),
        "operational_tombstones": len(operational),
        "archaeological_tombstones": len(archaeological),
        "heretical_tombstones": len(heretical),
        "visible_tombstones": sum(1 for t in tombstones if normalize_tombstone_class(t) in classes),
        "residual_tasks": len(list(packet_dict.get("residual_tasks") or [])),
        "soft_repulsion_warnings": len(list(packet_dict.get("soft_repulsion_warnings") or [])),
        "contradiction_pressure": float(packet_dict.get("contradiction_pressure", 0.0) or 0.0),
        "appeal_regime_active": bool(((packet_dict.get("appeal_regime_state") or {}).get("appeal_regime_active"))),
        "rupture_energy_events": len(list(packet_dict.get("rupture_energy_events") or [])),
    }


def build_reinjection_packet_for_mode(
    packet: Dict[str, Any],
    *,
    negative_memory_mode: Any = "full",
    visible_classes: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    mode = normalize_negative_memory_mode(negative_memory_mode)
    summary = build_negative_memory_summary(packet, negative_memory_mode=mode, visible_classes=visible_classes)
    sanitized = dict(packet)
    sanitized["negative_memory_mode"] = mode
    sanitized["visible_tombstone_classes"] = list(summary.get("visible_tombstone_classes") or [])
    sanitized["negative_memory_summary"] = summary

    if mode == "full":
        return sanitized

    sanitized.pop("branch_tombstones", None)
    sanitized.pop("residual_tasks", None)
    sanitized["challenged_claim_ids"] = []
    sanitized["rejected_hypothesis_ids"] = []
    sanitized["unresolved_rival_hypothesis_ids"] = []
    return sanitized


def tombstone_match_strings(tombstone: Dict[str, Any]) -> List[str]:
    raw_values: List[str] = []
    for key in (
        "target_id",
        "claim_id",
        "hypothesis_id",
        "origin_claim_id",
        "origin_hypothesis_id",
    ):
        value = str(tombstone.get(key) or "").strip()
        if value:
            raw_values.append(value)
    for key in ("match_strings", "text_markers"):
        for value in list(tombstone.get(key) or []):
            text = str(value or "").strip()
            if text:
                raw_values.append(text)

    deduped: List[str] = []
    seen: set[str] = set()
    for value in raw_values:
        normalized = re.sub(r"\s+", " ", value).strip()
        if len(normalized) < 6:
            continue
        folded = normalized.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        deduped.append(normalized)
    return deduped


def message_matches_tombstones(
    message: Dict[str, Any],
    tombstones: List[Dict[str, Any]],
) -> bool:
    role = str(message.get("role") or "").strip().lower()
    if role not in {"assistant", "tool"}:
        return False

    content = message.get("content")
    if isinstance(content, str):
        haystack = content
    else:
        haystack = json.dumps(content, ensure_ascii=False, default=str)
    haystack_folded = haystack.casefold()

    for tombstone in tombstones:
        if not tombstone_prunes_in_mode(tombstone):
            continue
        for needle in tombstone_match_strings(tombstone):
            if needle.casefold() in haystack_folded:
                return True
    return False


def prune_messages_for_tombstones(
    messages: List[Dict[str, Any]],
    packet: Dict[str, Any],
    *,
    negative_memory_mode: Any = "full",
    visible_classes: Optional[Tuple[str, ...]] = None,
) -> List[Dict[str, Any]]:
    tombstones = [
        tombstone
        for tombstone in list(packet.get("branch_tombstones") or [])
        if isinstance(tombstone, dict)
        and tombstone_prunes_in_mode(
            tombstone,
            negative_memory_mode=negative_memory_mode,
            visible_classes=visible_classes,
            packet=packet,
        )
    ]
    if not tombstones:
        return list(messages)

    return [
        message
        for message in messages
        if not message_matches_tombstones(message, tombstones)
    ]


# =========================================================================
# Provider Configuration
# =========================================================================

@dataclass
class ProviderConfig:
    """Static configuration for a single LLM provider."""

    provider_id: str
    api_key: str
    base_url: Optional[str] = None
    project_id: Optional[str] = None
    location: Optional[str] = None
    credential_path: Optional[str] = None
    provider_family: str = ""
    deployment_kind: str = "managed_api"
    auth_mode: str = "env"
    default_model: str = ""
    max_output_tokens: int = 16384
    supports_streaming: bool = True
    supports_tools: bool = True
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelHandle:
    """Lightweight reference to *provider + model*."""

    provider_id: str
    model_id: str


# =========================================================================
# Provider Registry
# =========================================================================

class ProviderRegistry:
    """Singleton-style registry of configured LLM providers.

    Call :meth:`from_env` to auto-discover providers from environment
    variables.  Individual SDK clients are lazily instantiated on first use
    via :meth:`get_client`.
    """

    def __init__(self, providers: Optional[Dict[str, ProviderConfig]] = None) -> None:
        self._providers: Dict[str, ProviderConfig] = providers or {}
        self._clients: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> ProviderRegistry:
        """Auto-detect all configured providers from environment variables.

        Recognised variables (``MICA_`` prefixed variants take precedence):

        * ``OPENAI_API_KEY`` / ``MICA_OPENAI_API_KEY``
        * ``ANTHROPIC_API_KEY`` / ``MICA_ANTHROPIC_API_KEY``
        * ``GOOGLE_API_KEY`` / ``MICA_GOOGLE_API_KEY``
        * ``GOOGLE_APPLICATION_CREDENTIALS`` + ``GCP_PROJECT_ID`` (Vertex)
        * ``FIREWORKS_API_KEY`` / ``MICA_FIREWORKS_API_KEY``
        * ``OPENROUTER_API_KEY`` / ``MICA_OPENROUTER_API_KEY``

        Base-URL overrides: ``MICA_<PROVIDER>_BASE_URL`` or
        ``<PROVIDER>_BASE_URL``.
        """
        bootstrap_runtime_env()

        def _provider_max_output_tokens(provider_name: str, default: int) -> int:
            for env_name in (
                f"MICA_{provider_name}_MAX_OUTPUT_TOKENS",
                f"{provider_name}_MAX_OUTPUT_TOKENS",
            ):
                raw_value = os.getenv(env_name)
                if raw_value in (None, ""):
                    continue
                try:
                    return int(raw_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid %s=%r; expected integer max_output_tokens",
                        env_name,
                        raw_value,
                    )
            return default

        providers: Dict[str, ProviderConfig] = {}

        # --- OpenAI --------------------------------------------------
        oai_key = os.getenv("MICA_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        oai_model = os.getenv("MICA_OPENAI_MODEL") or "gpt-4o"
        if oai_key:
            providers["openai"] = ProviderConfig(
                provider_id="openai",
                api_key=oai_key,
                base_url=os.getenv("MICA_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
                provider_family="openai_chat",
                default_model=oai_model,
                max_output_tokens=_provider_max_output_tokens("OPENAI", 16384),
            )

        # --- Anthropic ------------------------------------------------
        anth_key = os.getenv("MICA_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if anth_key:
            providers["anthropic"] = ProviderConfig(
                provider_id="anthropic",
                api_key=anth_key,
                base_url=os.getenv("MICA_ANTHROPIC_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL"),
                provider_family="anthropic_native",
                default_model="claude-sonnet-4-20250514",
                max_output_tokens=_provider_max_output_tokens("ANTHROPIC", 16384),
            )

        # --- Google (Gemini) ------------------------------------------
        goog_key = os.getenv("MICA_GOOGLE_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if goog_key:
            providers["google"] = ProviderConfig(
                provider_id="google",
                api_key=goog_key,
                base_url=os.getenv("MICA_GOOGLE_BASE_URL") or os.getenv("GOOGLE_BASE_URL"),
                provider_family="google_gemini_api",
                default_model="gemini-2.5-flash",
                max_output_tokens=_provider_max_output_tokens("GOOGLE", 8192),
            )

        # --- Fireworks -------------------------------------------------
        fireworks_key = os.getenv("MICA_FIREWORKS_API_KEY") or os.getenv("FIREWORKS_API_KEY")
        if fireworks_key:
            providers["fireworks"] = ProviderConfig(
                provider_id="fireworks",
                api_key=fireworks_key,
                base_url=os.getenv("MICA_FIREWORKS_BASE_URL") or os.getenv("FIREWORKS_BASE_URL"),
                provider_family="openai_chat",
                default_model=(
                    os.getenv("MICA_FIREWORKS_MODEL")
                    or os.getenv("FIREWORKS_MODEL")
                    or DEFAULT_FIREWORKS_DRIVER_MODEL
                ),
                max_output_tokens=_provider_max_output_tokens("FIREWORKS", 16384),
            )

        # --- DeepInfra (official, default after latency benchmark vs Fireworks) ---
        deepinfra_key = os.getenv("MICA_DEEPINFRA_API_KEY") or os.getenv("DEEPINFRA_API_KEY") or os.getenv("DEEPINFRA_API_KEY_DEV")
        if deepinfra_key:
            providers["deepinfra"] = ProviderConfig(
                provider_id="deepinfra",
                api_key=deepinfra_key,
                base_url=os.getenv("MICA_DEEPINFRA_BASE_URL") or os.getenv("DEEPINFRA_BASE_URL") or "https://api.deepinfra.com/v1/openai",
                provider_family="openai_chat",
                default_model=(
                    os.getenv("MICA_DEEPINFRA_MODEL")
                    or os.getenv("DEEPINFRA_MODEL")
                    or DEFAULT_DEEPINFRA_DRIVER_MODEL
                ),
                max_output_tokens=_provider_max_output_tokens("DEEPINFRA", 16384),
            )

        # --- Vertex AI (Gemini on Vertex) ----------------------------
        vertex_credentials = (
            os.getenv("MICA_GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )
        vertex_project = (
            os.getenv("MICA_VERTEX_PROJECT_ID")
            or os.getenv("VERTEX_PROJECT_ID")
            or os.getenv("GCP_PROJECT_ID")
            or os.getenv("GCP_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
        )
        vertex_model = (
            os.getenv("MICA_VERTEX_MODEL")
            or os.getenv("VERTEX_MODEL")
            or DEFAULT_VERTEX_DRIVER_MODEL
        )
        vertex_location = resolve_vertex_location(
            vertex_model,
            os.getenv("MICA_VERTEX_LOCATION")
            or os.getenv("VERTEX_LOCATION")
            or os.getenv("GCP_REGION")
            or os.getenv("GOOGLE_CLOUD_LOCATION"),
        )
        if vertex_project and vertex_credentials:
            providers["vertex"] = ProviderConfig(
                provider_id="vertex",
                api_key="",
                project_id=vertex_project,
                location=vertex_location,
                credential_path=vertex_credentials,
                provider_family="vertex_gemini",
                auth_mode="service_account",
                default_model=vertex_model,
                max_output_tokens=_provider_max_output_tokens("VERTEX", 8192),
            )

        vertex_maas_base_url = (
            os.getenv("MICA_VERTEX_MAAS_BASE_URL")
            or os.getenv("VERTEX_MAAS_BASE_URL")
        )
        vertex_maas_api_key = (
            os.getenv("MICA_VERTEX_MAAS_API_KEY")
            or os.getenv("VERTEX_MAAS_API_KEY")
        )
        if vertex_maas_base_url:
            providers["vertex_maas"] = ProviderConfig(
                provider_id="vertex_maas",
                api_key=vertex_maas_api_key or "vertex-maas-token-required",
                base_url=vertex_maas_base_url,
                provider_family="vertex_maas_openai_compatible",
                deployment_kind="maas_openai_compatible",
                auth_mode="bearer_token",
                default_model=(
                    os.getenv("MICA_VERTEX_MAAS_MODEL")
                    or os.getenv("VERTEX_MAAS_MODEL")
                    or "meta/llama-3.1-70b-instruct-maas"
                ),
                max_output_tokens=8192,
            )

        # --- OpenRouter -----------------------------------------------
        or_key = os.getenv("MICA_OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")
        if or_key:
            providers["openrouter"] = ProviderConfig(
                provider_id="openrouter",
                api_key=or_key,
                base_url=os.getenv("MICA_OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
                provider_family="openrouter_chat",
                default_model="openai/gpt-4o",
                max_output_tokens=16384,
                headers={"HTTP-Referer": "https://mica.dev", "X-Title": "MICA"},
            )

        # --- NVIDIA NIM (OpenAI-compatible) ----------------------------
        nv_key = os.getenv("NVIDIA_API_KEY")
        nv_base = os.getenv("MICA_NVIDIA_BASE_URL") or os.getenv("NVIDIA_BASE_URL")
        if nv_key and nv_base:
            providers["nvidia"] = ProviderConfig(
                provider_id="nvidia",
                api_key=nv_key,
                base_url=nv_base,
                provider_family="nvidia_openai_compatible",
                default_model="meta/llama-3.1-70b-instruct",
                max_output_tokens=4096,
                supports_streaming=True,
                supports_tools=True,
            )

        preferred_order = (
            "deepinfra",
            "fireworks",
            "openai",
            "anthropic",
            "google",
            "openrouter",
            "nvidia",
            "vertex_maas",
            "vertex",
        )
        ordered_providers: Dict[str, ProviderConfig] = {}
        for provider_id in preferred_order:
            if provider_id in providers:
                ordered_providers[provider_id] = providers[provider_id]
        for provider_id, provider_cfg in providers.items():
            if provider_id not in ordered_providers:
                ordered_providers[provider_id] = provider_cfg

        logger.info(
            "ProviderRegistry: discovered %d provider(s): %s",
            len(ordered_providers),
            ", ".join(ordered_providers.keys()) or "(none)",
        )
        return cls(ordered_providers)

    # ------------------------------------------------------------------
    # Client access
    # ------------------------------------------------------------------

    @property
    def provider_ids(self) -> List[str]:
        return list(self._providers.keys())

    def has_provider(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def get_config(self, provider_id: str) -> ProviderConfig:
        """Return the :class:`ProviderConfig` or raise ``KeyError``."""
        return self._providers[provider_id]

    def get_client(self, provider_id: str) -> Any:
        """Return the lazily-created SDK client for *provider_id*.

        * ``openai`` / ``openrouter`` / ``nvidia`` → ``openai.OpenAI``
        * ``fireworks`` → ``fireworks.Fireworks``
        * ``anthropic`` → ``anthropic.Anthropic``
        * ``google`` → ``google.generativeai`` module (configured)
        * ``vertex`` → ``google.genai.Client`` for Vertex AI
        """
        cfg = self._providers[provider_id]
        cache_key = self._client_cache_key(provider_id, cfg)

        if cache_key in self._clients:
            return self._clients[cache_key]

        client: Any

        if provider_id in ("openai", "openrouter", "nvidia", "vertex_maas", "deepinfra"):
            try:
                import openai
                import httpx
            except Exception:
                from openai import OpenAI as _OpenAI
                import httpx

            kwargs: Dict[str, Any] = {"api_key": cfg.api_key}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            if cfg.headers:
                kwargs["default_headers"] = cfg.headers
            kwargs["http_client"] = httpx.Client(
                timeout=httpx.Timeout(
                    _resolve_openai_compat_timeout_seconds(provider_id),
                    connect=10.0,
                )
            )
            client = (openai.OpenAI if "openai" in locals() else _OpenAI)(**kwargs)

        elif provider_id == "fireworks":
            try:
                from fireworks.client import Fireworks
                import httpx
            except Exception:
                from fireworks import Fireworks
                import httpx

            kwargs = {"api_key": cfg.api_key}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            if cfg.headers:
                kwargs["default_headers"] = cfg.headers
            kwargs["http_client"] = httpx.Client(
                timeout=httpx.Timeout(
                    _resolve_openai_compat_timeout_seconds(provider_id),
                    connect=10.0,
                )
            )
            client = Fireworks(**kwargs)

        elif provider_id == "anthropic":
            import anthropic
            import httpx

            kwargs = {"api_key": cfg.api_key}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            # Anthropic 0.8.x forwards `proxies=` into its internal httpx wrapper,
            # which breaks against httpx 0.28 where the kwarg became `proxy=`.
            # Provide the base client explicitly so provider construction remains
            # stable without downgrading the shared httpx stack.
            kwargs["http_client"] = httpx.Client()
            client = anthropic.Anthropic(**kwargs)

        elif provider_id == "google":
            import google.generativeai as genai

            genai.configure(api_key=cfg.api_key)
            client = genai

        elif provider_id == "vertex":
            from google import genai
            from google.genai.types import HttpOptions

            if cfg.credential_path and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cfg.credential_path
            client = genai.Client(
                vertexai=True,
                project=cfg.project_id,
                location=cfg.location,
                http_options=HttpOptions(api_version="v1"),
            )

        else:
            raise ValueError(f"Unknown provider: {provider_id}")

        self._clients[cache_key] = client
        return client

    @staticmethod
    def _client_cache_key(provider_id: str, cfg: ProviderConfig) -> str:
        if provider_id == "vertex":
            return f"vertex:{cfg.project_id or ''}:{cfg.location or ''}"
        return provider_id

    def close_client(self, provider_id: str) -> None:
        cfg = self._providers.get(provider_id)
        if cfg is None:
            return
        cache_key = self._client_cache_key(provider_id, cfg)
        client = self._clients.pop(cache_key, None)
        if client is None:
            return
        self._close_client_sync(client)

    async def close_client_async(self, provider_id: str) -> None:
        cfg = self._providers.get(provider_id)
        if cfg is None:
            return
        cache_key = self._client_cache_key(provider_id, cfg)
        client = self._clients.pop(cache_key, None)
        if client is None:
            return
        await self._close_client_async(client)

    @classmethod
    def _iter_nested_clients(cls, client: Any) -> List[Any]:
        nested: List[Any] = []
        for attr_name in ("_client_v1", "_image_client_v1"):
            nested_client = getattr(client, attr_name, None)
            if nested_client is not None:
                nested.append(nested_client)
        return nested

    @classmethod
    def _close_client_sync(cls, client: Any) -> None:
        for nested_client in cls._iter_nested_clients(client):
            cls._close_client_sync(nested_client)
        close = getattr(client, "close", None)
        if callable(close):
            close()

    @classmethod
    async def _close_client_async(cls, client: Any) -> None:
        for nested_client in cls._iter_nested_clients(client):
            await cls._close_client_async(nested_client)

        close = getattr(client, "close", None)
        if callable(close):
            close()

        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            result = aclose()
            if inspect.isawaitable(result):
                await result

    def get_model(self, provider_id: str, model_id: Optional[str] = None) -> ModelHandle:
        """Create a :class:`ModelHandle` for *provider_id* and *model_id*."""
        cfg = self._providers[provider_id]
        return ModelHandle(
            provider_id=provider_id,
            model_id=model_id or cfg.default_model,
        )

    def get_descriptor(self, provider_id: str, model_id: Optional[str] = None) -> ModelDescriptor:
        cfg = self._providers[provider_id]
        return ModelDescriptor(
            provider_id=provider_id,
            provider_family=cfg.provider_family or provider_id,
            model_id=model_id or cfg.default_model,
            base_url=cfg.base_url,
            location=cfg.location,
            project_id=cfg.project_id,
            deployment_kind=cfg.deployment_kind,
            auth_mode=cfg.auth_mode,
            extras={
                "supports_tools": cfg.supports_tools,
                "supports_streaming": cfg.supports_streaming,
                "api_key": cfg.api_key,
            },
        )


# =========================================================================
# Loop Configuration
# =========================================================================

@dataclass
class LoopConfig:
    """Tuning knobs for :class:`AgenticLoop`.

    ``code_mode_tools`` — set of tool names whose payloads are
    treated as *large-output* (code artifacts, PDB dumps, …).
    When a tool call's name is in this set, the loop may apply
    aggressive truncation (``code_mode_truncation_chars``) and
    skip streaming chunks that exceed the threshold.  Inspired by
    the Anthropic / Cloudflare *Code Mode* pattern (SOTA §6.3).
    """

    max_iterations: int = 25
    max_output_tokens: int = 16384
    temperature: float = 0.7
    tool_truncation_chars: int = 50_000
    doom_loop_threshold: int = 3
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    enable_streaming: bool = True
    context_limit: int = 128_000
    context_output_reserve: int = 4096

    # S2.5 — Budget enforcement (optional caps per run)
    budget_max_usd: float = 0.0   # 0.0 = no cap
    budget_max_tokens: int = 0    # 0 = no cap

    # S2.7 — Code Mode pilot
    code_mode_tools: frozenset = field(default_factory=frozenset)
    code_mode_truncation_chars: int = 120_000

    # Route-card execution enforcement
    required_tool_names: Tuple[str, ...] = field(default_factory=tuple)
    required_tool_retry_limit: int = 1

    # Phase 1 — structured hot-loop reinjection
    enable_hot_loop_reinjection: bool = False
    reinjection_packet: Optional[Dict[str, Any]] = None

    # Phase 3A — stratified negative memory baseline
    negative_memory_mode: str = "full"
    visible_tombstone_classes: Tuple[str, ...] = field(default_factory=tuple)
    allow_appeal_regime: bool = False
    rupture_energy_budget: float = 0.0

    @property
    def has_budget(self) -> bool:
        """True if at least one budget cap is set."""
        return self.budget_max_usd > 0.0 or self.budget_max_tokens > 0

    def is_budget_exceeded(self, consumed_usd: float, consumed_tokens: int) -> bool:
        """Check whether either cap has been breached."""
        if self.budget_max_usd > 0.0 and consumed_usd >= self.budget_max_usd:
            return True
        if self.budget_max_tokens > 0 and consumed_tokens >= self.budget_max_tokens:
            return True
        return False

    def is_code_mode_tool(self, tool_name: str) -> bool:
        """Return True if *tool_name* should use code-mode truncation."""
        return bool(self.code_mode_tools) and tool_name in self.code_mode_tools

    def truncation_limit_for(self, tool_name: str) -> int:
        """Return the truncation char limit for *tool_name*."""
        if self.is_code_mode_tool(tool_name):
            return self.code_mode_truncation_chars
        return self.tool_truncation_chars


# =========================================================================
# Agentic Loop
# =========================================================================

class AgenticLoop:
    """Core iterative agentic loop: stream → tool calls → re-stream → done.

    Inspired by OpenCode's ``SessionProcessor``.  The loop continues until the
    model produces a final text response with no tool calls, the iteration cap
    is reached, the abort signal fires, or a context overflow is detected.

    Usage::

        registry = ProviderRegistry.from_env()
        loop = AgenticLoop(registry, LoopConfig())

        async for event in loop.run(
            messages=[{"role": "user", "content": "Hello"}],
            tools=[...],
            tool_executor=my_executor,
            provider_id="openai",
            model_id="gpt-4o",
        ):
            handle(event)
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        config: Optional[LoopConfig] = None,
        *,
        run_id: str = "",
        program_id: str = "",
    ) -> None:
        self.registry = registry
        self.config = config or LoopConfig()
        self.run_id = run_id  # S0.3: canonical run identifier for all events
        self.program_id = program_id  # S2: canonical program identifier for all events

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def _format_reinjection_packet(self, packet: Dict[str, Any]) -> str:
        payload = json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        return (
            "[Structured reinjection packet]\n"
            "The previous iteration did not reach acceptable epistemic closure. "
            "You must resolve or explicitly abandon the challenged claims and hypotheses before finalizing.\n\n"
            "Required behavior:\n"
            "1. Address challenged claims directly.\n"
            "2. Do not restate rejected hypotheses as valid without new evidence.\n"
            "3. Update the status of rival hypotheses explicitly.\n"
            "4. Treat soft-repulsion warnings as high-cost anomaly zones, not as automatic bans.\n"
            "5. If you cannot resolve a challenged item, abandon it explicitly instead of hiding it.\n\n"
            f"Packet JSON:\n{payload}"
        )

    def _format_required_tool_retry_message(self, required_tool_names: Tuple[str, ...]) -> str:
        return (
            "[Required tool execution gate]\n"
            "Your previous turn did not call a required tool.\n"
            f"Before any further narrative, call at least one of these visible required tools: {', '.join(required_tool_names)}.\n"
            "If none of them are visible or invocable, explicitly name the missing tool and why it cannot run."
        )

    def _tombstone_match_strings(self, tombstone: Dict[str, Any]) -> List[str]:
        return tombstone_match_strings(tombstone)

    @staticmethod
    def _message_contains_required_tool_call(
        message: Dict[str, Any],
        required_tool_names: Tuple[str, ...],
    ) -> bool:
        if not required_tool_names or not isinstance(message, dict):
            return False

        for tc in list(message.get("_tool_calls") or []):
            if str(tc.get("name") or "").strip() in required_tool_names:
                return True

        for tc in list(message.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            function_payload = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(function_payload.get("name") or tc.get("name") or "").strip()
            if name in required_tool_names:
                return True

        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if str(block.get("type") or "").strip() == "tool_use" and str(block.get("name") or "").strip() in required_tool_names:
                    return True

        return False

    def _message_matches_tombstones(
        self,
        message: Dict[str, Any],
        tombstones: List[Dict[str, Any]],
    ) -> bool:
        return message_matches_tombstones(message, tombstones)

    def _prune_messages_for_tombstones(
        self,
        messages: List[Dict[str, Any]],
        packet: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        return prune_messages_for_tombstones(
            messages,
            packet,
            negative_memory_mode=self.config.negative_memory_mode,
            visible_classes=self.config.visible_tombstone_classes,
        )

    def _build_reinjection_messages(self, packet: Dict[str, Any]) -> List[Dict[str, Any]]:
        reinjection_packet = build_reinjection_packet_for_mode(
            packet,
            negative_memory_mode=self.config.negative_memory_mode,
            visible_classes=self.config.visible_tombstone_classes,
        )
        return [{"role": "user", "content": self._format_reinjection_packet(reinjection_packet)}]

    @staticmethod
    def _surface_tool_names(tools: List[Dict[str, Any]]) -> Tuple[str, ...]:
        names: List[str] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function_payload = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            name = str(function_payload.get("name") or tool.get("name") or "").strip()
            if name:
                names.append(name)
        return tuple(dict.fromkeys(names))

    @staticmethod
    def _build_blocked_tool_result(
        tool_name: str,
        visible_tool_names: Tuple[str, ...],
    ) -> str:
        return json.dumps(
            {
                "status": "blocked",
                "blocked_reason": "tool_not_visible_in_runtime_surface",
                "tool_name": tool_name,
                "visible_tool_names": list(visible_tool_names),
                "summary": (
                    f"Tool '{tool_name}' was proposed by the model but is not part of the "
                    "advertised runtime surface for this run."
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        )

    async def run(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_executor: ToolExecutor,
        provider_id: str,
        model_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        abort: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[AnyLoopEvent]:
        """Run the full agentic loop, yielding :class:`LoopEvent` instances.

        Parameters
        ----------
        messages:
            Initial message history (OpenAI chat format).
        tools:
            Tool definitions in OpenAI function-calling schema.
        tool_executor:
            ``async (name, call_id, args) -> result_text`` callback.
        provider_id:
            Which provider to use (``"openai"``, ``"anthropic"``, …).
        model_id:
            Model name override (falls back to provider default).
        system_prompt:
            Optional system prompt prepended to the conversation.
        abort:
            If set, the loop yields an ``Error`` and stops.
        """
        cfg = self.config
        handle = self.registry.get_model(provider_id, model_id)
        prov_cfg = self.registry.get_config(provider_id)

        doom = DoomLoopDetector(threshold=cfg.doom_loop_threshold)
        truncator = ToolTruncator(max_output_chars=cfg.tool_truncation_chars)
        ctx_tracker = ContextTracker(
            context_limit=cfg.context_limit,
            output_reserve=cfg.context_output_reserve,
        )

        # Work on a *copy* so the caller's list is not mutated.
        msgs = list(messages)
        if cfg.enable_hot_loop_reinjection and isinstance(cfg.reinjection_packet, dict) and cfg.reinjection_packet:
            msgs = self._prune_messages_for_tombstones(msgs, cfg.reinjection_packet)
            msgs.extend(self._build_reinjection_messages(cfg.reinjection_packet))
        total_cost = 0.0
        total_tokens = 0  # S2.5 — cumulative token counter for budget enforcement
        _already_compacted = False
        _event_base = {"run_id": self.run_id, "program_id": self.program_id}
        required_tool_names = tuple(
            str(name).strip()
            for name in tuple(cfg.required_tool_names or ())
            if str(name).strip()
        )
        required_tool_retry_count = 0
        required_tool_satisfied = not required_tool_names or any(
            self._message_contains_required_tool_call(message, required_tool_names)
            for message in msgs
        )
        visible_tool_names = self._surface_tool_names(tools)
        visible_tool_name_set = set(visible_tool_names)

        for step in range(1, cfg.max_iterations + 1):
            # --- abort check -----------------------------------------
            if abort and abort.is_set():
                yield Error(message="Aborted by caller", retryable=False, **_event_base)
                return

            # --- pre-step compaction (at 75% capacity) ---------------
            if not _already_compacted and ctx_tracker.approaching_limit(0.75):
                before_len = len(msgs)
                # Try LLM-driven compaction first, fall back to heuristic
                try:
                    msgs, summary = await llm_compact_messages(
                        msgs, system_prompt=system_prompt,
                        keep_last_n=4, provider_id=provider_id,
                        model_id=handle.model_id,
                    )
                except Exception:
                    msgs, summary = compact_messages(
                        msgs, system_prompt=system_prompt,
                        keep_last_n=4, provider_id=provider_id,
                    )
                if summary:
                    _already_compacted = True
                    ctx_tracker.reset()
                    yield ContextCompacted(
                        messages_before=before_len,
                        messages_after=len(msgs),
                        summary_chars=len(summary),
                        **_event_base,
                    )

            yield StreamStart(step=step, **_event_base)

            # --- streaming callback for real-time text deltas --------
            _streaming_deltas: List[str] = []

            async def _on_text_delta(chunk: str) -> None:
                _streaming_deltas.append(chunk)

            # --- inference (with retry) ------------------------------
            try:
                text, tool_calls, usage = await self._infer_with_retry(
                    handle=handle,
                    prov_cfg=prov_cfg,
                    msgs=msgs,
                    tools=tools,
                    system_prompt=system_prompt,
                    yield_event=None,
                    on_text_delta=_on_text_delta,
                )
            except Exception as exc:
                yield Error(message=str(exc), retryable=False, **_event_base)
                return

            # --- yield text deltas (streaming chunks or full text) ---
            if _streaming_deltas:
                for chunk in _streaming_deltas:
                    yield TextDelta(text=chunk, **_event_base)
            elif text:
                yield TextDelta(text=text, **_event_base)

            # --- usage tracking --------------------------------------
            prompt_tok = usage.get("prompt_tokens", 0)
            compl_tok = usage.get("completion_tokens", 0)
            step_cost = _estimate_cost(handle, prompt_tok, compl_tok)
            total_cost += step_cost
            total_tokens += prompt_tok + compl_tok

            yield StepFinish(step=step, usage=usage, cost_usd=step_cost, **_event_base)

            # --- S2.5 budget enforcement -----------------------------
            if cfg.has_budget and cfg.is_budget_exceeded(total_cost, total_tokens):
                yield Error(
                    message=(
                        f"Budget exceeded: ${total_cost:.4f}/{cfg.budget_max_usd:.4f} USD, "
                        f"{total_tokens}/{cfg.budget_max_tokens} tokens"
                    ),
                    retryable=False,
                    **_event_base,
                )
                yield LoopFinish(
                    total_steps=step,
                    total_cost_usd=total_cost,
                    finish_reason="budget_exceeded",
                    remediation_hint=_remediation_hint("budget_exceeded", total_tokens, total_cost),
                    cumulative_tokens=total_tokens,
                    **_event_base,
                )
                return

            # --- context overflow ------------------------------------
            if ctx_tracker.add_usage(prompt_tok, compl_tok):
                yield ContextOverflow(
                    prompt_tokens=ctx_tracker.total_prompt_tokens,
                    limit_tokens=ctx_tracker.effective_limit,
                    **_event_base,
                )
                yield LoopFinish(
                    total_steps=step,
                    total_cost_usd=total_cost,
                    finish_reason="context_overflow",
                    remediation_hint=_remediation_hint("context_overflow", total_tokens, total_cost),
                    cumulative_tokens=total_tokens,
                    **_event_base,
                )
                return

            # --- no tool calls → finished ----------------------------
            if not tool_calls:
                if not required_tool_satisfied and required_tool_retry_count < max(0, int(cfg.required_tool_retry_limit)):
                    required_tool_retry_count += 1
                    if text:
                        msgs.append(self._build_assistant_message(provider_id, text, tool_calls))
                    msgs.append(
                        {
                            "role": "user",
                            "content": self._format_required_tool_retry_message(required_tool_names),
                        }
                    )
                    continue
                yield LoopFinish(
                    total_steps=step,
                    total_cost_usd=total_cost,
                    finish_reason="end_turn",
                    cumulative_tokens=total_tokens,
                    **_event_base,
                )
                return

            if not required_tool_satisfied:
                required_tool_satisfied = any(
                    str(tc.get("name") or "").strip() in required_tool_names
                    for tc in tool_calls
                )

            # --- doom loop detection ---------------------------------
            doom.record(tool_calls)
            if doom.check():
                yield Error(
                    message=(
                        f"Doom loop detected: identical tool calls repeated "
                        f"{cfg.doom_loop_threshold} times"
                    ),
                    retryable=False,
                    **_event_base,
                )
                yield LoopFinish(
                    total_steps=step,
                    total_cost_usd=total_cost,
                    finish_reason="doom_loop",
                    remediation_hint=_remediation_hint("doom_loop", total_tokens, total_cost),
                    cumulative_tokens=total_tokens,
                    **_event_base,
                )
                return

            # --- execute tools (parallel) ----------------------------
            # Append assistant message with tool calls to history.
            msgs.append(
                self._build_assistant_message(provider_id, text, tool_calls)
            )

            tool_results: List[Tuple[Dict[str, Any], str, int, bool]] = []
            tool_exec_plans: List[Tuple[Dict[str, Any], ToolTruncator]] = []
            blocked_tool_results: Dict[str, Tuple[str, int, bool]] = {}
            for tc in tool_calls:
                if abort and abort.is_set():
                    yield Error(message="Aborted by caller", retryable=False, **_event_base)
                    yield LoopFinish(
                        total_steps=step,
                        total_cost_usd=total_cost,
                        finish_reason="aborted",
                        remediation_hint=_remediation_hint("aborted", total_tokens, total_cost),
                        cumulative_tokens=total_tokens,
                        **_event_base,
                    )
                    return
                tool_name = str(tc.get("name") or "").strip()
                yield ToolCallStart(
                    call_id=tc.get("id", ""),
                    name=tool_name,
                    args=tc.get("args", {}),
                    **_event_base,
                )
                if abort and abort.is_set():
                    yield Error(message="Aborted by caller", retryable=False, **_event_base)
                    yield LoopFinish(
                        total_steps=step,
                        total_cost_usd=total_cost,
                        finish_reason="aborted",
                        remediation_hint=_remediation_hint("aborted", total_tokens, total_cost),
                        cumulative_tokens=total_tokens,
                        **_event_base,
                    )
                    return
                if tool_name not in visible_tool_name_set:
                    blocked_text = self._build_blocked_tool_result(tool_name, visible_tool_names)
                    blocked_tool_results[str(tc.get("id") or "")] = (blocked_text, 0, False)
                    yield Error(
                        message=(
                            f"Blocked tool call outside advertised runtime surface: "
                            f"{tool_name or '<empty>'}"
                        ),
                        retryable=False,
                        **_event_base,
                    )
                    continue
                # S2.7: code_mode tools use a higher truncation limit
                _name = tool_name
                if cfg.code_mode_tools and _name in cfg.code_mode_tools:
                    _trunc = ToolTruncator(max_output_chars=cfg.code_mode_truncation_chars)
                else:
                    _trunc = truncator
                tool_exec_plans.append((tc, _trunc))
            coros = [
                self._execute_one_tool(tc, tool_executor, _trunc)
                for tc, _trunc in tool_exec_plans
            ]
            results = await asyncio.gather(*coros, return_exceptions=True)

            allowed_results_by_call_id: Dict[str, Tuple[str, int, bool]] = {}
            for tc, res in zip((tc for tc, _trunc in tool_exec_plans), results):
                if isinstance(res, BaseException):
                    allowed_results_by_call_id[str(tc.get("id") or "")] = (f"Tool error: {res}", 0, False)
                else:
                    allowed_results_by_call_id[str(tc.get("id") or "")] = res  # type: ignore[assignment]

            for tc in tool_calls:
                call_id = str(tc.get("id") or "")
                result_text, duration, trunc = blocked_tool_results.get(
                    call_id,
                    allowed_results_by_call_id.get(call_id, ("Tool error: missing tool result", 0, False)),
                )
                yield ToolCallEnd(
                    call_id=call_id,
                    name=tc.get("name", ""),
                    result=result_text[:500],  # events carry a preview only
                    duration_ms=duration,
                    was_truncated=trunc,
                    **_event_base,
                )
                tool_results.append((tc, result_text, duration, trunc))

            # Append tool results to history.
            self._append_tool_results(provider_id, msgs, tool_results)

            # --- post-tool compaction: estimate msg chars vs context ---
            if not _already_compacted:
                total_chars = sum(
                    len(str(m.get("content", ""))) for m in msgs
                )
                # ~4 chars per token is a rough estimate
                est_tokens = total_chars // 4
                if est_tokens > int(ctx_tracker.effective_limit * 0.65):
                    before_len = len(msgs)
                    msgs, summary = compact_messages(
                        msgs, system_prompt=system_prompt,
                        keep_last_n=4, provider_id=provider_id,
                    )
                    if summary:
                        _already_compacted = True
                        ctx_tracker.reset()
                        yield ContextCompacted(
                            messages_before=before_len,
                            messages_after=len(msgs),
                            summary_chars=len(summary),
                            **_event_base,
                        )

        # --- max iterations exceeded ---------------------------------
        yield Error(
            message=f"Max iterations ({cfg.max_iterations}) reached",
            retryable=False,
            **_event_base,
        )
        yield LoopFinish(
            total_steps=cfg.max_iterations,
            total_cost_usd=total_cost,
            finish_reason="max_iterations",
            remediation_hint=_remediation_hint("max_iterations", total_tokens, total_cost),
            cumulative_tokens=total_tokens,
            **_event_base,
        )

    # ------------------------------------------------------------------
    # Inference with retry wrapper
    # ------------------------------------------------------------------

    # Cross-provider fallback order when a primary provider is saturated.
    _FALLBACK_CHAIN = ("deepinfra", "fireworks", "openai", "anthropic", "google", "openrouter")

    async def _infer_with_retry(
        self,
        handle: ModelHandle,
        prov_cfg: ProviderConfig,
        msgs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str],
        yield_event: Any,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
        """Call the model, retrying on transient failures.

        After all retry attempts are exhausted on a Vertex saturation error
        (429 / RESOURCE_EXHAUSTED), falls back to the first available
        alternative provider in ``_FALLBACK_CHAIN``.
        """
        policy = self.config.retry
        last_exc: Optional[Exception] = None

        for attempt in range(policy.max_attempts):
            try:
                return await self._infer(handle, prov_cfg, msgs, tools, system_prompt, on_text_delta)
            except Exception as exc:
                last_exc = exc
                if not policy.is_retryable(exc) or attempt == policy.max_attempts - 1:
                    break
                delay = max(0, int(policy.delay_ms(attempt) or 0))
                logger.warning(
                    "Retryable error (attempt %d/%d), waiting %dms: %s",
                    attempt + 1,
                    policy.max_attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay / 1000.0)

        # ── Cross-provider failover on Vertex saturation ──────────
        assert last_exc is not None  # satisfy type checker
        if handle.provider_id == "vertex" and is_vertex_saturation_error(last_exc):
            for fallback_pid in self._FALLBACK_CHAIN:
                if fallback_pid == handle.provider_id:
                    continue
                if not self.registry.has_provider(fallback_pid):
                    continue
                try:
                    fb_handle = self.registry.get_model(fallback_pid)
                    fb_cfg = self.registry.get_config(fallback_pid)
                    logger.warning(
                        "Vertex saturated (%s); falling back to %s/%s",
                        last_exc,
                        fallback_pid,
                        fb_handle.model_id,
                    )
                    text, tool_calls, usage = await self._infer(
                        fb_handle, fb_cfg, msgs, tools, system_prompt, on_text_delta,
                    )
                    usage["provider_fallback_used"] = True  # type: ignore[assignment]
                    usage["original_provider"] = handle.provider_id  # type: ignore[assignment]
                    usage["fallback_provider"] = fallback_pid  # type: ignore[assignment]
                    return text, tool_calls, usage
                except Exception as fb_exc:
                    logger.warning(
                        "Fallback provider %s also failed: %s", fallback_pid, fb_exc,
                    )
                    continue

        raise last_exc

    # ------------------------------------------------------------------
    # Provider-specific inference
    # ------------------------------------------------------------------

    async def _infer(
        self,
        handle: ModelHandle,
        prov_cfg: ProviderConfig,
        msgs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str],
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
        """Dispatch to the correct provider backend."""
        pid = handle.provider_id
        if pid in ("openai", "openrouter", "nvidia", "vertex_maas", "fireworks", "deepinfra"):
            return await self._infer_openai(handle, prov_cfg, msgs, tools, system_prompt, on_text_delta)
        elif pid == "anthropic":
            return await self._infer_anthropic(handle, prov_cfg, msgs, tools, system_prompt)
        elif pid == "google":
            return await self._infer_google(handle, prov_cfg, msgs, tools, system_prompt)
        elif pid == "vertex":
            return await self._infer_vertex(handle, prov_cfg, msgs, tools, system_prompt)
        else:
            raise ValueError(f"Unsupported provider: {pid}")

    # --- OpenAI / OpenAI-compatible -----------------------------------

    async def _infer_openai(
        self,
        handle: ModelHandle,
        prov_cfg: ProviderConfig,
        msgs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str],
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
        """Perform an OpenAI chat completion with optional streaming.

        When ``self.config.enable_streaming`` is True and *on_text_delta* is
        provided, text tokens are streamed via the callback.  Otherwise falls
        back to a single non-streaming request.
        """
        client = self.registry.get_client(handle.provider_id)

        try:
            api_msgs: List[Dict[str, Any]] = []
            if system_prompt:
                api_msgs.append({"role": "system", "content": system_prompt})
            api_msgs.extend(msgs)

            # Newer models (gpt-5.x, o-series) require max_completion_tokens.
            _new_style = any(
                handle.model_id.startswith(p) for p in ("gpt-5", "gpt-4.1", "o1", "o3", "o4")
            )
            token_key = "max_completion_tokens" if _new_style else "max_tokens"

            kwargs: Dict[str, Any] = {
                "model": handle.model_id,
                "messages": api_msgs,
                "temperature": self.config.temperature,
                token_key: self.config.max_output_tokens,
            }
            if tools:
                kwargs["tools"] = [
                    {"type": "function", "function": t} if "type" not in t else t
                    for t in tools
                ]

            use_streaming = self.config.enable_streaming and on_text_delta is not None
            # OpenAI-compatible custom gateways are invoked through a sync
            # iterator. Iterating that stream inside the event loop can block
            # WS progress and defeat timeout/cancellation. Keep streaming only
            # for the standard OpenAI API surface and use non-streaming for
            # custom providers such as DeepInfra/Fireworks/OpenRouter.
            if handle.provider_id != "openai" or not _is_standard_openai:
                use_streaming = False

            # --- Streaming path ---
            if use_streaming:
                kwargs["stream"] = True
                # Only standard OpenAI API supports stream_options; skip for custom base URLs
                _is_standard_openai = not prov_cfg.base_url or "api.openai.com" in (prov_cfg.base_url or "")
                if _is_standard_openai:
                    kwargs["stream_options"] = {"include_usage": True}

                try:
                    stream = await asyncio.to_thread(client.chat.completions.create, **kwargs)
                except Exception as stream_exc:
                    # If streaming fails (e.g. unsupported by provider), fall back to non-streaming
                    logger.warning("Streaming failed, falling back to non-streaming: %s", stream_exc)
                    kwargs.pop("stream", None)
                    kwargs.pop("stream_options", None)
                    response = await asyncio.to_thread(client.chat.completions.create, **kwargs)
                    choice = response.choices[0]
                    text = choice.message.content or ""
                    tool_calls_out: List[Dict[str, Any]] = []
                    if choice.message.tool_calls:
                        for tc in choice.message.tool_calls:
                            try:
                                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                            except json.JSONDecodeError:
                                args = {"_raw": tc.function.arguments}
                            tool_calls_out.append({"id": tc.id, "name": tc.function.name, "args": args})
                    usage_out: Dict[str, int] = {}
                    if response.usage:
                        usage_out = {"prompt_tokens": response.usage.prompt_tokens, "completion_tokens": response.usage.completion_tokens, "total_tokens": response.usage.total_tokens}
                    if text and on_text_delta:
                        try:
                            await on_text_delta(text)
                        except Exception:
                            pass
                    return text, tool_calls_out, usage_out

                text_parts: List[str] = []
                tc_accum: Dict[int, Dict[str, Any]] = {}  # index → {id, name, args_str}
                usage: Dict[str, int] = {}

                for chunk in stream:
                    if not chunk.choices and hasattr(chunk, "usage") and chunk.usage:
                        usage = {
                            "prompt_tokens": chunk.usage.prompt_tokens or 0,
                            "completion_tokens": chunk.usage.completion_tokens or 0,
                            "total_tokens": chunk.usage.total_tokens or 0,
                        }
                        continue

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    # Text streaming
                    if delta.content:
                        text_parts.append(delta.content)
                        try:
                            await on_text_delta(delta.content)
                        except Exception:
                            pass

                    # Tool call accumulation
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tc_accum:
                                tc_accum[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "args_str": "",
                                }
                            if tc_delta.id:
                                tc_accum[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tc_accum[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tc_accum[idx]["args_str"] += tc_delta.function.arguments

                text = "".join(text_parts)
                tool_calls: List[Dict[str, Any]] = []
                for idx in sorted(tc_accum.keys()):
                    tc = tc_accum[idx]
                    try:
                        args = json.loads(tc["args_str"]) if tc["args_str"] else {}
                    except json.JSONDecodeError:
                        args = {"_raw": tc["args_str"]}
                    tool_calls.append({"id": tc["id"], "name": tc["name"], "args": args})

                return text, tool_calls, usage

            # --- Non-streaming fallback ---
            response = await asyncio.to_thread(client.chat.completions.create, **kwargs)

            choice = response.choices[0]
            text = choice.message.content or ""
            tool_calls = []

            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {"_raw": tc.function.arguments}
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "args": args,
                    })

            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return text, tool_calls, usage
        except Exception as e:
            # Let caller handle provider-specific errors
            raise

    # --- Anthropic (Messages API) -------------------------------------

    async def _infer_anthropic(
        self,
        handle: ModelHandle,
        prov_cfg: ProviderConfig,
        msgs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
        """Perform a single Anthropic Messages API call via thread."""
        client = self.registry.get_client(handle.provider_id)

        # Convert OpenAI-style messages to Anthropic format.
        anth_msgs = _openai_msgs_to_anthropic(msgs)

        # Convert tool definitions to Anthropic schema.
        anth_tools = _openai_tools_to_anthropic(tools) if tools else []

        kwargs: Dict[str, Any] = {
            "model": handle.model_id,
            "messages": anth_msgs,
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if anth_tools:
            kwargs["tools"] = anth_tools

        response = await asyncio.to_thread(client.messages.create, **kwargs)

        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "args": block.input if isinstance(block.input, dict) else {},
                })

        usage: Dict[str, int] = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "input_tokens", 0),
                "completion_tokens": getattr(response.usage, "output_tokens", 0),
                "total_tokens": (
                    getattr(response.usage, "input_tokens", 0)
                    + getattr(response.usage, "output_tokens", 0)
                ),
            }

        return "\n".join(text_parts), tool_calls, usage

    # --- Google (Gemini) ----------------------------------------------

    async def _infer_google(
        self,
        handle: ModelHandle,
        prov_cfg: ProviderConfig,
        msgs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
        """Perform a Gemini GenerativeModel call via thread."""
        genai = self.registry.get_client("google")

        contents = _openai_msgs_to_gemini(msgs, system_prompt=system_prompt)

        try:
            model = genai.GenerativeModel(
                model_name=handle.model_id,
                system_instruction=system_prompt or None,
            )
        except TypeError as exc:
            if "system_instruction" not in str(exc):
                raise
            model = genai.GenerativeModel(model_name=handle.model_id)

        # Gemini tool declarations.
        gemini_tools = None
        if tools:
            gemini_tools = [genai.types.Tool(function_declarations=[
                _openai_tool_to_gemini_decl(t) for t in tools
            ])]

        gen_config = genai.types.GenerationConfig(
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
        )

        response = await asyncio.to_thread(
            model.generate_content,
            contents,
            tools=gemini_tools,
            generation_config=gen_config,
        )

        text = ""
        tool_calls: List[Dict[str, Any]] = []

        for part in response.parts:
            if hasattr(part, "text") and part.text:
                text += part.text
            if hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                tool_calls.append({
                    "id": f"gemini_{fc.name}_{int(time.time()*1000)}",
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                })

        usage: Dict[str, int] = {}
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            usage = {
                "prompt_tokens": getattr(um, "prompt_token_count", 0),
                "completion_tokens": getattr(um, "candidates_token_count", 0),
                "total_tokens": getattr(um, "total_token_count", 0),
            }

        return text, tool_calls, usage

    async def _infer_vertex(
        self,
        handle: ModelHandle,
        prov_cfg: ProviderConfig,
        msgs: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str],
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
        """Perform a Vertex AI call via the google-genai SDK."""
        from google.genai import types

        client = self.registry.get_client("vertex")
        contents = _openai_msgs_to_gemini(msgs, system_prompt=None)

        vertex_tools = None
        if tools:
            vertex_tools = [
                types.Tool(
                    functionDeclarations=[
                        types.FunctionDeclaration(**_openai_tool_to_google_genai_decl(t))
                        for t in tools
                    ]
                )
            ]

        gen_config = types.GenerateContentConfig(
            systemInstruction=system_prompt or None,
            temperature=self.config.temperature,
            maxOutputTokens=self.config.max_output_tokens,
            tools=vertex_tools,
        )

        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=handle.model_id,
                contents=contents,
                config=gen_config,
            )
            resolved_model_id = handle.model_id
        except Exception as exc:
            retry_location = resolve_optional_vertex_global_location_retry(
                handle.model_id,
                exc,
                current_location=prov_cfg.location,
            )
            if retry_location:
                logger.warning(
                    "Vertex model %s unavailable in location %s; retrying direct core inference with location %s",
                    handle.model_id,
                    prov_cfg.location,
                    retry_location,
                )
                prov_cfg.location = retry_location
                client = self.registry.get_client("vertex")
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=handle.model_id,
                    contents=contents,
                    config=gen_config,
                )
                resolved_model_id = handle.model_id
                exc = None
            if exc is None:
                pass
            else:
                fallback_model = resolve_optional_vertex_fallback_model(
                    handle.model_id,
                    exc,
                    enabled=str(os.getenv("MICA_VERTEX_ALLOW_MODEL_FALLBACK") or os.getenv("VERTEX_ALLOW_MODEL_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"},
                    fallback_model=(os.getenv("MICA_VERTEX_DIRECT_FALLBACK_MODEL") or os.getenv("VERTEX_DIRECT_FALLBACK_MODEL") or DEFAULT_VERTEX_DRIVER_MODEL),
                )
                if not fallback_model:
                    raise
                logger.warning(
                    "Vertex model %s unavailable in this project; retrying direct core inference with fallback model %s",
                    handle.model_id,
                    fallback_model,
                )
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=fallback_model,
                    contents=contents,
                    config=gen_config,
                )
                resolved_model_id = fallback_model

        text = ""
        tool_calls: List[Dict[str, Any]] = []
        parts: List[Any] = []

        candidates = getattr(response, "candidates", None) or []
        if candidates:
            for candidate in candidates:
                content = getattr(candidate, "content", None)
                candidate_parts = getattr(content, "parts", None) or []
                parts.extend(candidate_parts)
        else:
            parts.extend(getattr(response, "parts", None) or [])

        for part in parts:
            part_text = getattr(part, "text", None)
            if part_text:
                text += part_text

            function_call = getattr(part, "function_call", None)
            if not function_call:
                continue

            args: Dict[str, Any] = {}
            raw_args = getattr(function_call, "args", None)
            if raw_args:
                try:
                    args = dict(raw_args)
                except Exception:
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}

            tool_calls.append({
                "id": f"vertex_{function_call.name}_{int(time.time()*1000)}",
                "name": function_call.name,
                "args": args,
                "thought_signature": getattr(part, "thought_signature", None),
            })

        usage: Dict[str, int] = {}
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata:
            usage = {
                "prompt_tokens": getattr(usage_metadata, "prompt_token_count", 0),
                "completion_tokens": getattr(usage_metadata, "candidates_token_count", 0),
                "total_tokens": getattr(usage_metadata, "total_token_count", 0),
            }

        if resolved_model_id != handle.model_id:
            usage["resolved_model_id"] = resolved_model_id

        return text, tool_calls, usage

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    @staticmethod
    async def _execute_one_tool(
        tc: Dict[str, Any],
        executor: ToolExecutor,
        truncator: ToolTruncator,
    ) -> Tuple[str, int, bool]:
        """Execute a single tool call, returning (result, duration_ms, truncated)."""
        t0 = time.monotonic()
        raw_result = await executor(
            tc.get("name", ""),
            tc.get("id", ""),
            tc.get("args", {}),
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        result, was_truncated = truncator.truncate(raw_result)
        return result, duration_ms, was_truncated

    # ------------------------------------------------------------------
    # Message formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_assistant_message(
        provider_id: str,
        text: str,
        tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build the assistant message that records model output + tool calls."""
        if provider_id in ("openai", "openrouter", "nvidia", "vertex_maas", "fireworks", "deepinfra"):
            msg: Dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"]),
                        },
                    }
                    for tc in tool_calls
                ]
            return msg

        elif provider_id == "anthropic":
            content: List[Dict[str, Any]] = []
            if text:
                content.append({"type": "text", "text": text})
            for tc in tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["args"],
                })
            return {"role": "assistant", "content": content}

        elif provider_id in ("google", "vertex"):
            # Gemini messages handled separately; store in an interchange format.
            return {"role": "assistant", "content": text, "_tool_calls": tool_calls}

        return {"role": "assistant", "content": text}

    @staticmethod
    def _append_tool_results(
        provider_id: str,
        msgs: List[Dict[str, Any]],
        results: List[Tuple[Dict[str, Any], str, int, bool]],
    ) -> None:
        """Append tool results to the message history in provider-native format."""
        if provider_id in ("openai", "openrouter", "nvidia", "vertex_maas", "fireworks", "deepinfra"):
            for tc, result_text, _dur, _trunc in results:
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })

        elif provider_id == "anthropic":
            tool_result_blocks: List[Dict[str, Any]] = []
            for tc, result_text, _dur, _trunc in results:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": result_text,
                })
            msgs.append({"role": "user", "content": tool_result_blocks})

        elif provider_id in ("google", "vertex"):
            # Store as interchange; the converter reads ``_tool_results``.
            msgs.append({
                "role": "tool",
                "_tool_results": [
                    {"id": tc["id"], "name": tc["name"], "result": res}
                    for tc, res, _d, _t in results
                ],
            })


# =========================================================================
# Format conversion helpers (private)
# =========================================================================

def _openai_msgs_to_anthropic(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI-style messages to Anthropic Messages API format.

    System messages are stripped (passed separately via ``system=``).
    Tool-role messages are folded into the preceding ``user`` turn as
    ``tool_result`` content blocks.
    """
    result: List[Dict[str, Any]] = []

    for msg in msgs:
        role = msg.get("role", "")

        if role == "system":
            continue  # handled outside

        if role == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                # Already in Anthropic content-block format.
                result.append({"role": "assistant", "content": content})
            elif isinstance(content, str) and content:
                result.append({"role": "assistant", "content": content})
            elif msg.get("tool_calls"):
                # OpenAI-format assistant with tool_calls: convert.
                blocks: List[Dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                result.append({"role": "assistant", "content": blocks})
            else:
                # Empty assistant message — Anthropic requires non-empty.
                result.append({"role": "assistant", "content": "(no response)"})

        elif role == "tool":
            # Fold into the last user message or create a new one.
            block: Dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": msg.get("content", ""),
            }
            if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                result[-1]["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})

        elif role == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                result.append({"role": "user", "content": content})
            else:
                result.append({"role": "user", "content": str(content)})

    return result


def _openai_tools_to_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI function-calling tool defs to Anthropic tool schema."""
    anth: List[Dict[str, Any]] = []
    for t in tools:
        fn = t.get("function", t)
        anth.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return anth


def _openai_msgs_to_gemini(msgs: List[Dict[str, Any]], system_prompt: Optional[str] = None) -> List[Any]:
    """Convert OpenAI messages to simple Gemini ``contents`` list.

    This uses the dictionary-based content format accepted by
    ``GenerativeModel.generate_content``.
    """
    contents: List[Dict[str, Any]] = []

    if system_prompt:
        contents.append({
            "role": "user",
            "parts": [{"text": f"[SYSTEM INSTRUCTION]\n{system_prompt}"}],
        })

    for msg in msgs:
        role = msg.get("role", "user")
        if role == "system":
            continue

        gemini_role = "model" if role == "assistant" else "user"
        content = msg.get("content", "")

        # Tool result messages.
        if role == "tool" and "_tool_results" in msg:
            response_parts: List[Dict[str, Any]] = []
            for tr in msg["_tool_results"]:
                response_parts.append({
                    "function_response": {
                        "name": tr["name"],
                        "response": {
                            "result": tr["result"],
                            "tool_call_id": tr.get("id", ""),
                        },
                    }
                })
            if response_parts:
                contents.append({"role": "user", "parts": response_parts})
            continue

        if role == "assistant" and msg.get("_tool_calls"):
            assistant_parts: List[Dict[str, Any]] = []
            if isinstance(content, str) and content:
                assistant_parts.append({"text": content})
            for tc in list(msg.get("_tool_calls") or []):
                part: Dict[str, Any] = {
                    "function_call": {
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}) or {},
                    }
                }
                if tc.get("thought_signature") is not None:
                    part["thought_signature"] = tc.get("thought_signature")
                assistant_parts.append(part)
            if assistant_parts:
                contents.append({"role": "model", "parts": assistant_parts})
            continue

        if role == "assistant" and msg.get("tool_calls"):
            assistant_parts = []
            if isinstance(content, str) and content:
                assistant_parts.append({"text": content})
            for tc in list(msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                try:
                    fn_args = json.loads(fn.get("arguments", "{}")) if fn.get("arguments") else {}
                except json.JSONDecodeError:
                    fn_args = {}
                assistant_parts.append({
                    "function_call": {
                        "name": fn.get("name", ""),
                        "args": fn_args,
                    }
                })
            if assistant_parts:
                contents.append({"role": "model", "parts": assistant_parts})
            continue

        if isinstance(content, str):
            contents.append({"role": gemini_role, "parts": [{"text": content or " "}]})
        elif isinstance(content, list):
            # Content blocks — extract text.
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            contents.append({"role": gemini_role, "parts": [{"text": text or " "}]})

    return contents


_GEMINI_UNSUPPORTED_SCHEMA_KEYS = {"default"}


def _sanitize_gemini_schema(value: Any) -> Any:
    """Strip JSON Schema fields rejected by Gemini tool declarations."""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, nested_value in value.items():
            if key in _GEMINI_UNSUPPORTED_SCHEMA_KEYS:
                continue
            cleaned[key] = _sanitize_gemini_schema(nested_value)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_gemini_schema(item) for item in value]
    return value


def _openai_tool_to_gemini_decl(t: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one OpenAI tool def to a Gemini ``FunctionDeclaration`` dict."""
    fn = t.get("function", t)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "parameters": _sanitize_gemini_schema(
            fn.get("parameters", {"type": "object", "properties": {}})
        ),
    }


def _openai_tool_to_google_genai_decl(t: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one OpenAI tool def to google-genai ``FunctionDeclaration`` kwargs."""
    fn = t.get("function", t)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "parametersJsonSchema": _sanitize_gemini_schema(
            fn.get("parameters", {"type": "object", "properties": {}})
        ),
    }


# =========================================================================
# Cost estimation (rough, provider-dependent)
# =========================================================================

# Prices per 1 M tokens (input, output) in USD — approximate mid-2025 rates.
_COST_TABLE: Dict[str, Tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "gpt-5.2": (2.50, 10.00),
    "gpt-5.1": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-3.1-flash-lite-preview": (0.15, 0.60),
    "gemini-1.5-pro": (1.25, 5.00),
}


def _estimate_cost(handle: ModelHandle, prompt_tokens: int, completion_tokens: int) -> float:
    """Rough cost estimate in USD; returns 0.0 for unknown models."""
    rates = _COST_TABLE.get(handle.model_id)
    if not rates:
        return 0.0
    input_cost = (prompt_tokens / 1_000_000) * rates[0]
    output_cost = (completion_tokens / 1_000_000) * rates[1]
    return round(input_cost + output_cost, 6)

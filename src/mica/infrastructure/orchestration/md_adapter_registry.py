from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable, Optional

from .md_provider_adapter import MDProviderAdapter


class MDAdapterRegistry:
    def __init__(self, adapters: Iterable[MDProviderAdapter] | None = None):
        self._adapters: list[MDProviderAdapter] = list(adapters or [])

    def register(self, adapter: MDProviderAdapter) -> None:
        self._adapters.append(adapter)

    def resolve_provider_alias(
        self,
        provider_name: str | None,
        available_provider_names: Iterable[str] | None = None,
    ) -> str:
        normalized = str(provider_name or "").strip().lower()
        if not normalized:
            return ""

        available = {str(name).strip().lower() for name in (available_provider_names or [])}
        for adapter in self._adapters:
            aliases = tuple(alias.lower() for alias in adapter.provider_aliases)
            if normalized not in aliases:
                continue
            if not available:
                return normalized
            for alias in aliases:
                if alias in available:
                    return alias
        return normalized

    def get_for_provider(self, provider_name: str | None) -> Optional[MDProviderAdapter]:
        normalized = str(provider_name or "").strip().lower()
        for adapter in self._adapters:
            if normalized in tuple(alias.lower() for alias in adapter.provider_aliases):
                return adapter
        return None

    def get_for_config(self, cfg: Any, provider_name: str | None = None) -> Optional[MDProviderAdapter]:
        normalized_provider = str(provider_name or "").strip().lower()
        config_matches = [adapter for adapter in self._adapters if adapter.supports_config(cfg)]

        if config_matches:
            if normalized_provider:
                for adapter in config_matches:
                    if normalized_provider in tuple(alias.lower() for alias in adapter.provider_aliases):
                        return adapter
            return config_matches[0]

        provider_adapter = self.get_for_provider(provider_name)
        if provider_adapter is not None:
            return provider_adapter
        return None

    def create_execution(self, cfg: Any, provider: Any, on_event: Any = None):
        provider_name = str(getattr(provider, "PROVIDER_NAME", "") or "").lower()
        adapter = self.get_for_config(cfg, provider_name=provider_name)
        if adapter is None:
            raise ValueError(f"No MD adapter registered for provider '{provider_name or 'unknown'}'")
        return adapter.create_execution(cfg, provider, on_event=on_event)


@lru_cache(maxsize=1)
def build_default_md_adapter_registry() -> MDAdapterRegistry:
    from .adapters import RunPodMDAdapter, SaladGCSAdapter, VastGCSAdapter, VastMDAdapter

    return MDAdapterRegistry(
        adapters=[
            SaladGCSAdapter(),
            VastMDAdapter(),
            VastGCSAdapter(),
            RunPodMDAdapter(),
        ]
    )
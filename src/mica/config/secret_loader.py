"""Secret loader for api_keys.yaml with environment seeding.
Cascade: existing os.environ values are preserved.
"""
from __future__ import annotations
import os, yaml, logging
from typing import Dict, Any

log = logging.getLogger(__name__)
DEFAULT_FILE = os.getenv("MICA_KEYS_FILE", "api_keys.yaml")

PROVIDER_ENV_MAP = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "nemotron": ("NEMOTRON_API_KEY",),
    "bionemo": ("BIONEMO_API_KEY",),
    "llama": ("LLAMA_API_KEY",),
}

VECTOR_ENV_KEYS = {
    "ZILLIZ_URI": "ZILLIZ_URI",
    "ZILLIZ_TOKEN": "ZILLIZ_TOKEN",
    "MICA_VECTOR_COLLECTION": "MICA_VECTOR_COLLECTION",
    "MICA_EMBED_DIM": "MICA_EMBED_DIM",
}


def _safe_set(key: str, value: str):
    if key not in os.environ and value:
        os.environ[key] = value
        log.info(f"[secret_loader] set {key} from file")


def seed_env_from_file(path: str | None = None) -> Dict[str, Any]:
    path = path or DEFAULT_FILE
    if not os.path.exists(path):
        log.info("[secret_loader] api_keys file not found, skipping")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning(f"[secret_loader] failed to read {path}: {e}")
        return {}

    providers = data.get("providers", {})
    for provider, models in providers.items():
        env_keys = PROVIDER_ENV_MAP.get(provider)
        if not env_keys:
            continue
        # pick first model api_key
        first_key = None
        if isinstance(models, dict):
            for _model_name, spec in models.items():
                if isinstance(spec, dict) and spec.get("api_key"):
                    first_key = spec.get("api_key")
                    break
        if first_key:
            for envk in env_keys:
                _safe_set(envk, first_key)
    # vector store direct keys at root
    for yaml_key, env_key in VECTOR_ENV_KEYS.items():
        if yaml_key in data and data[yaml_key]:
            _safe_set(env_key, str(data[yaml_key]))

    return data

__all__ = ["seed_env_from_file"]

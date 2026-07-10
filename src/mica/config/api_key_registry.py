"""API Key Registry Loader

Permite cargar un archivo `api_keys.yaml` (estructura jerárquica por proveedor->modelo)
y exponer helpers para obtener claves y hacer seed de variables de entorno
sin sobreescribir valores ya presentes.

Formato esperado (ver `api_keys.example.yaml`):
providers:
  provider_name:
    model_name:
      api_key: "..."
      api_url/base_url: "..." (opcional)

Dependencias: intenta usar PyYAML si está instalado; si no, aplica un parser
lineal muy simple (requiere sangrías consistentes con 2 espacios).
"""
from __future__ import annotations
import os, re
from typing import Dict, Any, Optional

try:  # preferible si disponible
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

_CACHE: Dict[str, Dict[str, Any]] = {}


def _simple_yaml_parse(text: str) -> Dict[str, Any]:  # fallback minimalista
    data: Dict[str, Any] = {"providers": {}}
    cur_provider: Optional[str] = None
    cur_model: Optional[str] = None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        if re.match(r'^\s{0,2}[a-zA-Z0-9_]+:\s*$', line):  # toplevel or provider
            key = line.split(':')[0].strip()
            if key == 'providers':
                continue
            if cur_provider is None or key != cur_provider:
                cur_provider = key
                data['providers'].setdefault(cur_provider, {})
                cur_model = None
            continue
        # model line (two-space indent)
        m = re.match(r'^\s{2}([a-zA-Z0-9_.\-]+):\s*$', line)
        if m and cur_provider:
            cur_model = m.group(1)
            data['providers'][cur_provider][cur_model] = {}
            continue
        # key inside model (4-space indent)
        m2 = re.match(r'^\s{4}([a-zA-Z0-9_]+):\s*"?(.*?)"?\s*$', line)
        if m2 and cur_provider and cur_model:
            k, v = m2.group(1), m2.group(2)
            data['providers'][cur_provider][cur_model][k] = v
    return data


def load_registry(path: str = 'api_keys.yaml', *, force: bool = False) -> Dict[str, Any]:
    if not force and path in _CACHE:
        return _CACHE[path]
    if not os.path.exists(path):
        _CACHE[path] = {"providers": {}}
        return _CACHE[path]
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    if yaml:
        try:
            parsed = yaml.safe_load(text) or {}
        except Exception:
            parsed = {}
    else:
        parsed = _simple_yaml_parse(text)
    if 'providers' not in parsed:
        parsed = {"providers": {}}
    _CACHE[path] = parsed
    return parsed


def get_api_key(provider: str, model: Optional[str] = None, *, path: str = 'api_keys.yaml') -> Optional[str]:
    reg = load_registry(path)
    prov = reg.get('providers', {}).get(provider, {})
    if not prov:
        return None
    if model and model in prov:
        return prov[model].get('api_key')
    # fallback first model
    for _, meta in prov.items():  # type: ignore
        if isinstance(meta, dict) and meta.get('api_key'):
            return meta['api_key']
    return None


def seed_env_from_registry(reg: Dict[str, Any]):
    providers = reg.get('providers', {})

    def _seed(env_key: str, value: Optional[str]):
        if value and not os.getenv(env_key):
            os.environ[env_key] = value

    # OpenAI
    _seed('OPENAI_API_KEY', get_api_key('openai'))
    _seed('ANTHROPIC_API_KEY', get_api_key('anthropic'))
    _seed('GEMINI_API_KEY', get_api_key('gemini'))
    # DeepSeek
    ds = providers.get('deepseek', {})
    if ds:
        first = next(iter(ds.values())) if isinstance(ds, dict) else {}
        _seed('DEEPSEEK_API_KEY', first.get('api_key'))
        _seed('DEEPSEEK_API_URL', first.get('api_url'))
    # Nemotron
    nem = providers.get('nemotron', {})
    if nem:
        first = next(iter(nem.values())) if isinstance(nem, dict) else {}
        _seed('NEMOTRON_API_KEY', first.get('api_key'))
        _seed('NEMOTRON_API_URL', first.get('api_url'))
    # BioNeMo (use evo2 as base)
    bio = providers.get('bionemo', {})
    if bio:
        first = bio.get('evo2-40b') or (next(iter(bio.values())) if isinstance(bio, dict) else {})
        _seed('BIONEMO_API_KEY', first.get('api_key'))
        _seed('BIONEMO_BASE_URL', first.get('base_url'))
    # Llama
    ll = providers.get('llama', {})
    if ll:
        first = next(iter(ll.values())) if isinstance(ll, dict) else {}
        _seed('LLAMA_API_KEY', first.get('api_key'))
        _seed('LLAMA_API_URL', first.get('api_url'))
    # DeepInfra / Mixtral
    di = providers.get('deepinfra', {})
    if di:
        first = next(iter(di.values())) if isinstance(di, dict) else {}
        _seed('MIXTRAL_API_KEY', first.get('api_key'))
        _seed('MIXTRAL_API_URL', first.get('api_url'))
    # Phi multimodal
    phi = providers.get('phi', {})
    if phi:
        first = next(iter(phi.values())) if isinstance(phi, dict) else {}
        _seed('PHI_API_KEY', first.get('api_key'))
        _seed('PHI_API_URL', first.get('api_url'))


def seed_env_from_file(path: str = 'api_keys.yaml'):
    reg = load_registry(path)
    seed_env_from_registry(reg)
    return True


__all__ = [
    'load_registry',
    'get_api_key',
    'seed_env_from_file',
    'seed_env_from_registry',
]

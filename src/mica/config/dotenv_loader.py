from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional


_REFERENCE_PREFIXES = ("$", "${", "env:", "secret:", "sm://", "gcp-secret:")


def _normalize_env_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    if text.startswith('"'):
        end = text.find('"', 1)
        if end != -1:
            return text[1:end].strip()
    elif text.startswith("'"):
        end = text.find("'", 1)
        if end != -1:
            return text[1:end].strip()

    comment_idx = text.find(" #")
    if comment_idx != -1:
        text = text[:comment_idx].rstrip()

    return text.strip()


def normalize_env_value(value: str | None) -> str | None:
    normalized = _normalize_env_text(value or "")
    return normalized or None


def resolve_env_value(*keys: str) -> str | None:
    for key in keys:
        normalized = normalize_env_value(os.getenv(key))
        if normalized:
            return normalized
    return None


def _looks_like_reference(value: str | None) -> bool:
    text = str(value or "").strip()
    return text.startswith(_REFERENCE_PREFIXES)


def _read_gcp_secret_reference(reference: str) -> str | None:
    ref = normalize_env_value(reference)
    if not ref:
        return None

    raw = ref
    if raw.startswith("gcp-secret:"):
        raw = raw[len("gcp-secret:"):]
    elif raw.startswith("sm://"):
        raw = raw[len("sm://"):]
    raw = raw.lstrip("/")
    if not raw:
        return None

    if raw.startswith("projects/"):
        resource_name = raw
    else:
        project = resolve_env_value("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT") or ""
        secret_name = ""
        version = "latest"

        if "/" in raw and not ":" in raw:
            parts = [part for part in raw.split("/") if part]
            if len(parts) >= 2:
                project = parts[0] or project
                secret_name = parts[1]
                if len(parts) >= 3:
                    version = parts[2] or "latest"
        else:
            secret_name, _, raw_version = raw.partition(":")
            version = raw_version or "latest"

        if not project or not secret_name:
            return None
        resource_name = f"projects/{project}/secrets/{secret_name}/versions/{version}"

    try:
        from google.cloud import secretmanager  # type: ignore

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": resource_name})
        payload = response.payload.data.decode("utf-8")
        return normalize_env_value(payload)
    except Exception:
        return None


def resolve_external_secret_reference(value: str | None) -> str | None:
    normalized = normalize_env_value(value)
    if not normalized:
        return None

    if normalized.startswith("${") and normalized.endswith("}"):
        return normalize_env_value(os.getenv(normalized[2:-1].strip()))
    if normalized.startswith("$") and not normalized.startswith("${"):
        return normalize_env_value(os.getenv(normalized[1:].strip()))
    if normalized.startswith(("env:", "secret:")):
        return normalize_env_value(os.getenv(normalized.split(":", 1)[1].strip()))
    if normalized.startswith(("sm://", "gcp-secret:")):
        return _read_gcp_secret_reference(normalized)
    return normalized


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    if s.startswith("export "):
        s = s[len("export "):].lstrip()

    if "=" not in s:
        return None

    key, value = s.split("=", 1)
    key = key.strip()
    value = value.strip()

    if not key:
        return None

    return key, _normalize_env_text(value)


def find_dotenv(start: Path | None = None, filename: str = ".env", max_depth: int = 12) -> Optional[Path]:
    """Search for `.env` by walking up parent directories."""
    candidates = find_dotenv_chain(start=start, filename=filename, max_depth=max_depth)
    return candidates[-1] if candidates else None


def find_dotenv_chain(start: Path | None = None, filename: str = ".env", max_depth: int = 12) -> list[Path]:
    """Return all `.env` files found while walking up parent directories."""
    cur = (start or Path.cwd()).resolve()
    found: list[Path] = []
    for _ in range(max_depth + 1):
        candidate = cur / filename
        if candidate.exists() and candidate.is_file():
            found.append(candidate)
        if cur.parent == cur:
            break
        cur = cur.parent
    return list(reversed(found))


def _iter_dotenv_paths(path: str | Path | None = None) -> Iterable[Path]:
    if path is None:
        yield from find_dotenv_chain(start=Path(__file__).resolve())
        return

    dotenv_path = Path(path).expanduser().resolve()
    if dotenv_path.exists() and dotenv_path.is_file():
        yield dotenv_path


def materialize_google_credentials_from_env() -> Optional[Path]:
    """Bridge inline Railway credentials JSON to GOOGLE_APPLICATION_CREDENTIALS."""

    direct_inline_json = resolve_external_secret_reference(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        or os.getenv("MICA_GOOGLE_APPLICATION_CREDENTIALS_JSON")
    )
    referenced_inline_json = resolve_external_secret_reference(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS_REF")
        or os.getenv("MICA_GOOGLE_APPLICATION_CREDENTIALS_REF")
    )
    inline_json = direct_inline_json or referenced_inline_json
    existing_path = resolve_external_secret_reference(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or os.getenv("MICA_GOOGLE_APPLICATION_CREDENTIALS")
    )

    if not inline_json and existing_path and not str(existing_path).lstrip().startswith("{"):
        existing_file = Path(existing_path).expanduser()
        if existing_file.exists() and existing_file.is_file():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(existing_file)
            return existing_file

    if not inline_json:
        return None

    try:
        parsed = json.loads(inline_json)
    except Exception:
        try:
            decoded_text = base64.b64decode(inline_json).decode("utf-8")
            parsed = json.loads(decoded_text)
        except Exception:
            return None

    target = Path(tempfile.gettempdir()) / "mica_google_application_credentials.json"
    payload = json.dumps(parsed, ensure_ascii=False, indent=2)
    if not target.exists() or target.read_text(encoding="utf-8") != payload:
        target.write_text(payload, encoding="utf-8")

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(target)
    return target


def seed_env_from_dotenv(path: str | Path | None = None, *, override: bool = False) -> Dict[str, str]:
    """Load key/value pairs from `.env` into os.environ.

    - Does NOT require python-dotenv.
    - By default does NOT override existing env vars.
    """
    loaded: Dict[str, str] = {}
    seeded_in_call: set[str] = set()
    for dotenv_path in _iter_dotenv_paths(path):
        try:
            lines = dotenv_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        for line in lines:
            parsed = _parse_dotenv_line(line)
            if not parsed:
                continue
            key, value = parsed
            loaded[key] = value
            current_value = os.environ.get(key)
            caller_locked = normalize_env_value(current_value) and key not in seeded_in_call
            if override or not caller_locked:
                os.environ[key] = value
                seeded_in_call.add(key)

    materialize_google_credentials_from_env()
    return loaded

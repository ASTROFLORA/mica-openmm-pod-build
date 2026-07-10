from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional


PROVIDER_REQUIRED_ENV: Dict[str, List[str]] = {
    "salad": ["SALAD_CLOUD_API_KEY", "SALAD_ORG_NAME"],
    "vast": ["VAST_API_KEY"],
    "runpod": ["RUNPOD_API_KEY"],
}

PROVIDER_OPTIONAL_ENV: Dict[str, List[str]] = {
    "salad": ["SALAD_PROJECT_NAME"],
    "vast": [],
    "runpod": [],
}


@dataclass
class ProviderPreflightResult:
    provider: str
    ok: bool
    missing_required: List[str]
    present_required: List[str]
    optional_present: List[str]
    optional_missing: List[str]
    message: str
    remediation: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def run_provider_preflight(
    provider: str,
    env: Optional[Dict[str, str]] = None,
) -> ProviderPreflightResult:
    env_map = env or os.environ
    provider_name = provider.strip().lower()

    required = PROVIDER_REQUIRED_ENV.get(provider_name, [])
    optional = PROVIDER_OPTIONAL_ENV.get(provider_name, [])

    present_required: List[str] = []
    missing_required: List[str] = []
    for key in required:
        val = str(env_map.get(key, "") or "").strip().strip('"').strip("'")
        if val:
            present_required.append(key)
        else:
            missing_required.append(key)

    optional_present: List[str] = []
    optional_missing: List[str] = []
    for key in optional:
        val = str(env_map.get(key, "") or "").strip().strip('"').strip("'")
        if val:
            optional_present.append(key)
        else:
            optional_missing.append(key)

    if missing_required:
        msg = f"provider_preflight_failed:{provider_name}"
        remediation = (
            "Set missing required env vars and retry. "
            f"Missing={','.join(missing_required)}"
        )
        return ProviderPreflightResult(
            provider=provider_name,
            ok=False,
            missing_required=missing_required,
            present_required=present_required,
            optional_present=optional_present,
            optional_missing=optional_missing,
            message=msg,
            remediation=remediation,
        )

    return ProviderPreflightResult(
        provider=provider_name,
        ok=True,
        missing_required=[],
        present_required=present_required,
        optional_present=optional_present,
        optional_missing=optional_missing,
        message=f"provider_preflight_ok:{provider_name}",
        remediation="none",
    )

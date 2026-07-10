"""
Harness profiles — explicit operational profiles for the MICA command harness.

Profiles control which targets are allowed, whether fixtures are permitted,
and what product proof is required.

Env vars:
  MICA_HARNESS_PROFILE=internal_dev
  MICA_HARNESS_TARGETS=local,railway,sandbox,modal,resource
  MICA_HARNESS_ALLOW_FIXTURE=false
  MICA_HARNESS_ALLOW_GITHUB_WRITE=false
  MICA_HARNESS_ALLOW_MODAL=true
  MICA_HARNESS_REQUIRE_GCS_PRODUCT_PROOF=true
  MICA_HARNESS_MAX_OUTPUT_BYTES=8192
  MICA_HARNESS_TIMEOUT_SECONDS=60
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import os


@dataclass(frozen=True)
class HarnessProfile:
    name: str
    description: str
    targets_allowed: List[str] = field(default_factory=lambda: ["local"])
    allow_fixture: bool = False
    allow_github_write: bool = False
    allow_modal: bool = False
    require_gcs_product_proof: bool = False
    strict_live_default: bool = False
    max_output_bytes: int = 8192
    timeout_seconds: int = 60

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "targets_allowed": self.targets_allowed,
            "allow_fixture": self.allow_fixture,
            "allow_github_write": self.allow_github_write,
            "allow_modal": self.allow_modal,
            "require_gcs_product_proof": self.require_gcs_product_proof,
            "strict_live_default": self.strict_live_default,
            "max_output_bytes": self.max_output_bytes,
            "timeout_seconds": self.timeout_seconds,
        }


PROFILES: Dict[str, HarnessProfile] = {
    "local_dev": HarnessProfile(
        name="local_dev",
        description="Local development — local backend, sandbox, fixtures allowed",
        targets_allowed=["local", "sandbox", "resource", "gcs"],
        allow_fixture=True,
        allow_modal=False,
        require_gcs_product_proof=False,
        strict_live_default=False,
    ),
    "railway_prod": HarnessProfile(
        name="railway_prod",
        description="Railway production — remote only, no local SDKs, no fixtures",
        targets_allowed=["railway"],
        allow_fixture=False,
        allow_github_write=False,
        allow_modal=False,
        require_gcs_product_proof=True,
        strict_live_default=True,
    ),
    "internal_dev": HarnessProfile(
        name="internal_dev",
        description="Internal development — all targets, no GitHub write, requires product proof",
        targets_allowed=["local", "railway", "sandbox", "modal", "resource", "gcs"],
        allow_fixture=False,
        allow_github_write=False,
        allow_modal=True,
        require_gcs_product_proof=True,
        strict_live_default=False,
    ),
    "agent_service": HarnessProfile(
        name="agent_service",
        description="Agent service token — no human session, protected routes allowed",
        targets_allowed=["railway", "modal", "sandbox", "gcs"],
        allow_fixture=False,
        allow_github_write=False,
        allow_modal=True,
        require_gcs_product_proof=True,
        strict_live_default=True,
    ),
    "readonly_audit": HarnessProfile(
        name="readonly_audit",
        description="Read-only audit — no writes, no compute, no self-modification",
        targets_allowed=["local", "railway"],
        allow_fixture=False,
        allow_github_write=False,
        allow_modal=False,
        require_gcs_product_proof=False,
        strict_live_default=True,
    ),
}


def resolve_profile(profile_name: Optional[str] = None) -> HarnessProfile:
    """Resolve the active harness profile from env or explicit name."""
    name = profile_name or os.getenv("MICA_HARNESS_PROFILE", "internal_dev")
    return PROFILES.get(name, PROFILES["internal_dev"])


def get_profile(name: str) -> Optional[HarnessProfile]:
    """Get a specific profile by name."""
    return PROFILES.get(name)


def list_profiles() -> List[str]:
    """List all available profile names."""
    return sorted(PROFILES.keys())

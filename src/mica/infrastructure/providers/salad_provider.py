"""
salad_provider.py — SaladCloud Provider Implementation

Implements the CloudProvider interface for SaladCloud Container Groups.

Unlike Vast.ai (SSH pod access), Salad Cloud exposes a Container Group API:
  - No SSH; workloads run inside Docker containers.
  - Container Groups (CG) map to "instances" in the MICA provider model.
  - GPU classes are Salad-specific string IDs resolved at runtime via API.
  - All data persistence MUST use external storage (GCS or S3-compatible).

Key architectural differences from VastProvider:
  - create_instance()  → creates + starts a Container Group (SRCG)
  - destroy_instance() → stops + deletes the Container Group
  - instance_id        → container_group_name (slug, not UUID)
  - No SSH host/port fields are populated (access via Salad portal/VS Code tunnel)

SRCG = Single-Replica Container Group: the recommended pattern for large MD.

SDK: salad-cloud-sdk 0.9.0a17+
  pip install salad-cloud-sdk

Auth: SALAD_CLOUD_API_KEY environment variable
      Header: Salad-Api-Key

Reference:
  https://docs.salad.com/reference/saladcloud-api/container-groups
  .mica/external_docs/salad/container-engine/how-to-guides/molecular-dynamics-simulation/openmm-srcg.mdx
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from ..compute_image_contract import ghcr_basic_auth, is_ghcr_image
from .base_provider import (
    CloudProvider,
    GPUOffer,
    GPUType,
    Instance,
    InstanceStatus,
    ProvisionRequest,
    ProvisionResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GPU class name → GPUType mapping (Salad names from list_gpu_classes API)
# These are display names returned by the API; GPU class IDs are UUIDs.
# ---------------------------------------------------------------------------
_SALAD_GPU_NAME_TO_TYPE: Dict[str, GPUType] = {
    # RTX 5xxx series
    "RTX 5090": GPUType.RTX_5090,
    "NVIDIA GeForce RTX 5090": GPUType.RTX_5090,
    "GeForce RTX 5090": GPUType.RTX_5090,
    "RTX 5080": GPUType.RTX_5080,
    "NVIDIA GeForce RTX 5080": GPUType.RTX_5080,
    # RTX 4xxx
    "RTX 4090": GPUType.RTX_4090,
    "NVIDIA GeForce RTX 4090": GPUType.RTX_4090,
    "RTX 4080": GPUType.RTX_4080,
    # RTX 3xxx
    "RTX 3090": GPUType.RTX_3090,
    "RTX 3080": GPUType.RTX_3080,
    # Data center
    "A100 SXM 80GB": GPUType.A100_80GB,
    "A100 PCIe 80GB": GPUType.A100_80GB,
    "A100 80GB": GPUType.A100_80GB,
    "A100 SXM 40GB": GPUType.A100_40GB,
    "A100 PCIe 40GB": GPUType.A100_40GB,
    "A100 40GB": GPUType.A100_40GB,
    "A40": GPUType.A40,
    "A10": GPUType.A10,
    "L40S": GPUType.L40S,
    "L40": GPUType.L40,
    "H100 SXM 80GB": GPUType.H100_SXM,
    "H100 PCIe 80GB": GPUType.H100_80GB,
    "H100 80GB": GPUType.H100_80GB,
}

# GPUType → search keyword in Salad GPU class name
_GPU_TYPE_SEARCH_KEYWORD: Dict[GPUType, str] = {
    GPUType.RTX_5090: "5090",
    GPUType.RTX_5080: "5080",
    GPUType.RTX_4090: "4090",
    GPUType.RTX_4080: "4080",
    GPUType.RTX_3090: "3090",
    GPUType.RTX_3080: "3080",
    GPUType.A100_80GB: "A100",
    GPUType.A100_40GB: "A100",
    GPUType.A40: "A40",
    GPUType.A10: "A10",
    GPUType.L40S: "L40S",
    GPUType.L40: "L40",
    GPUType.H100_SXM: "H100",
    GPUType.H100_80GB: "H100",
    GPUType.V100_16GB: "V100",
    GPUType.V100_32GB: "V100",
}

# Default Salad resource parameters for MD workloads
_DEFAULT_CPU_CORES = 4
_DEFAULT_RAM_MB = 16_384  # 16 GB
_DEFAULT_STORAGE_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB in bytes (SDK requires bytes, min 1 GiB)
_DEFAULT_IMAGE_CACHING = os.getenv("SALAD_IMAGE_CACHING", "true").strip().lower() not in {"0", "false", "no", "off"}

# Salad Container Group name constraints: lowercase alphanumeric + hyphens, max 63 chars
_CG_NAME_RE = re.compile(r"[^a-z0-9\-]")
_DISPLAY_NAME_RE = re.compile(r"[^ ,.0-9A-Za-z\-]")
_SAFE_CG_ENV_KEYS = {
    "GCS_BUCKET",
    "GCS_PREFIX",
    "OUTPUT_GCS_PREFIX",
    "COMPLETED_MARKER_OBJECT",
    "MICA_JOB_ID",
    "SIMULATION_MODE",
    "MICA_WORKER_MODE",
    "MICA_MD_PROCESSOR",
    "PRODUCTION_NS",
}


def _price_to_float(price_obj: Any) -> Optional[float]:
    """Extract numeric price from SDK float/string/object payloads."""
    if price_obj is None:
        return None
    if isinstance(price_obj, (int, float, str)):
        try:
            return float(price_obj)
        except (TypeError, ValueError):
            return None

    # salad_cloud_sdk.models.gpu_class_price.GpuClassPrice exposes `.price`
    raw_price = getattr(price_obj, "price", None)
    if raw_price is None:
        return None
    try:
        return float(raw_price)
    except (TypeError, ValueError):
        return None


def _make_cg_name(job_id: str) -> str:
    """Generate a valid Container Group name from a job ID."""
    safe = _CG_NAME_RE.sub("-", job_id.lower())
    safe = safe.strip("-")[:60]
    return f"mica-{safe}"


def _make_display_name(job_id: str, fallback: str) -> str:
    """Generate a Salad SDK-safe display name from a job ID."""
    raw = f"MICA MD {job_id or fallback}".strip()
    safe = _DISPLAY_NAME_RE.sub("-", raw)
    safe = re.sub(r"\s+", " ", safe).strip(" -")
    if not safe:
        safe = f"MICA MD {fallback}"
    return safe[:120]


def _salad_status_to_mica(status_str: str) -> InstanceStatus:
    """Map Salad ContainerGroupStatus → MICA InstanceStatus."""
    mapping = {
        "pending": InstanceStatus.PENDING,
        "deploying": InstanceStatus.PROVISIONING,
        "running": InstanceStatus.RUNNING,
        "stopped": InstanceStatus.STOPPED,
        "succeeded": InstanceStatus.STOPPED,
        "failed": InstanceStatus.ERROR,
    }
    return mapping.get(status_str, InstanceStatus.PENDING)


def _event_name_to_mica_status(event_name: str) -> tuple[InstanceStatus, str]:
    normalized = str(event_name or "").strip()
    lowered = normalized.lower()
    if lowered.startswith("instance exited:") or "(error)" in lowered:
        return InstanceStatus.ERROR, normalized
    if lowered == "container group stopped":
        return InstanceStatus.STOPPED, normalized
    if lowered == "instance running":
        return InstanceStatus.RUNNING, normalized
    if lowered.startswith("instance ") and any(
        token in lowered for token in ("allocated", "downloading", "starting", "ready", "startup probe", "interrupted")
    ):
        return InstanceStatus.PROVISIONING, normalized
    return InstanceStatus.PENDING, normalized


def _project_safe_container_environment(cg: Any) -> Dict[str, str]:
    container = getattr(cg, "container", None)
    env_map = getattr(container, "environment_variables", None)
    if not isinstance(env_map, dict):
        return {}
    projected: Dict[str, str] = {}
    for key in _SAFE_CG_ENV_KEYS:
        value = env_map.get(key)
        if value is None:
            continue
        projected[key] = str(value)
    return projected


def _latest_system_log_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {}
    latest = items[0]
    return {
        "latest_system_event": latest.get("event_name", ""),
        "latest_system_event_time": latest.get("event_time", ""),
        "latest_container_group_instance_id": latest.get("instance_id", ""),
        "latest_machine_id": latest.get("machine_id", ""),
        "recent_events": [
            {
                "event_name": item.get("event_name", ""),
                "event_time": item.get("event_time", ""),
                "instance_id": item.get("instance_id", ""),
            }
            for item in items[:5]
        ],
    }


def _value_or_raw(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _summarize_container_group_instances(collection: Any) -> Dict[str, Any]:
    items = list(getattr(collection, "items", None) or getattr(collection, "instances", None) or [])
    if not items:
        return {}

    first = items[0]
    state = str(_value_or_raw(getattr(first, "state", "")) or "")
    instance_id = str(
        getattr(first, "id_", "")
        or getattr(first, "id", "")
        or getattr(first, "instance_id", "")
        or ""
    )
    pulling_progress = getattr(first, "pulling_progress", None)
    try:
        pulling_progress = float(pulling_progress) if pulling_progress is not None else None
    except (TypeError, ValueError):
        pulling_progress = None
    return {
        "instance_state": state.lower(),
        "container_group_instance_id": instance_id,
        "machine_id": str(getattr(first, "machine_id", "") or ""),
        "pulling_progress": pulling_progress,
        "instance_update_time": str(getattr(first, "update_time", "") or ""),
        "instance_ready": bool(getattr(first, "ready", False)),
        "instance_started": bool(getattr(first, "started", False)),
        "instance_count": len(items),
    }


class SaladProvider(CloudProvider):
    """
    MICA CloudProvider for SaladCloud Container Groups.

    Uses the salad-cloud-sdk async client to manage SRCG (Single-Replica
    Container Group) lifecycle for MD workloads.

    Environment variables:
        SALAD_CLOUD_API_KEY       — API key (required)
        SALAD_ORG_NAME            — Organization name (required, e.g. "acme-corp")
        SALAD_PROJECT_NAME        — Project name (default: "mica-compute")

    The "instance_id" in MICA maps to the Container Group name (slug).
    Container Groups are opaque to SSH; access is via Salad portal or
    VS Code tunnel running inside the container.
    """

    PROVIDER_NAME: str = "salad"

    def __init__(
        self,
        api_key: Optional[str] = None,
        org_name: Optional[str] = None,
        project_name: str = "mica-compute",
        timeout_ms: int = 30_000,
    ):
        super().__init__(api_key=api_key)
        self._api_key = api_key or os.environ.get("SALAD_CLOUD_API_KEY", "").strip().strip('"')
        self._org_name = org_name or os.environ.get("SALAD_ORG_NAME", "")
        self._project_name = project_name or os.environ.get("SALAD_PROJECT_NAME", "mica-compute")
        self._timeout_ms = timeout_ms

        if not self._api_key:
            raise ValueError("SALAD_CLOUD_API_KEY is required for SaladProvider")
        if not self._org_name:
            raise ValueError("SALAD_ORG_NAME is required for SaladProvider")

        # Lazy import — only available after pip install salad-cloud-sdk
        from salad_cloud_sdk import SaladCloudSdkAsync
        self._sdk = SaladCloudSdkAsync(
            api_key=self._api_key,
            timeout=self._timeout_ms,
        )
        # SDK 0.9.0a17 quota endpoints can miss constructor auth headers.
        # Force header registration explicitly for reliable authenticated calls.
        self._sdk.set_api_key(self._api_key)

    @classmethod
    def from_env(cls) -> "SaladProvider":
        """Build from environment variables."""
        return cls(
            api_key=os.environ.get("SALAD_CLOUD_API_KEY", "").strip().strip('"'),
            org_name=os.environ.get("SALAD_ORG_NAME", ""),
            project_name=os.environ.get("SALAD_PROJECT_NAME", "mica-compute"),
        )

    # ── GPU class resolution ──────────────────────────────────────────────

    async def list_gpu_classes(self) -> List[Dict[str, Any]]:
        """
        Return all available GPU classes for this organization.

        Returns list of dicts: {id, name, gpu_type, gpu_count, price_min, is_high_demand}
        """
        result = await self._sdk.organization_data.list_gpu_classes(
            organization_name=self._org_name
        )
        classes = []
        for gpu_class in (result.items or []):
            gpu_type = _SALAD_GPU_NAME_TO_TYPE.get(gpu_class.name)
            min_price = None
            if gpu_class.prices:
                prices = [p for p in (_price_to_float(x) for x in gpu_class.prices) if p is not None]
                min_price = min(prices) if prices else None
            classes.append({
                "id": gpu_class.id_,
                "name": gpu_class.name,
                "gpu_type": gpu_type,
                "gpu_count": getattr(gpu_class, "gpu_count", None),
                "price_min": min_price,
                "is_high_demand": gpu_class.is_high_demand,
            })
        return classes

    async def resolve_gpu_class_id(self, gpu_type: GPUType) -> Optional[str]:
        """
        Resolve a GPUType to the Salad GPU class UUID.

        For RTX_5090, searches GPU classes for "5090" in the name.
        Returns first match, or None if not found.
        """
        keyword = _GPU_TYPE_SEARCH_KEYWORD.get(gpu_type)
        if not keyword:
            return None
        classes = await self.list_gpu_classes()
        for cls in classes:
            if keyword.lower() in cls["name"].lower():
                return cls["id"]
        return None

    async def get_quotas(self) -> Dict[str, Any]:
        """Return current quota information for this organization."""
        result = await self._sdk.quotas.get_quotas(organization_name=self._org_name)
        quotas_dict: Dict[str, Any] = {}
        if hasattr(result, "container_groups_quotas") and result.container_groups_quotas:
            cg = result.container_groups_quotas
            quotas_dict["max_created_container_groups"] = getattr(cg, "max_created_container_groups", None)
            quotas_dict["max_replicas_per_container_group"] = getattr(cg, "max_replicas_per_container_group", None)
            quotas_dict["max_gpu_classes"] = getattr(cg, "max_gpu_classes", None)
        return quotas_dict

    # ── CloudProvider interface ───────────────────────────────────────────

    async def search_offers(
        self,
        gpu_type: GPUType,
        max_price: Optional[float] = None,
        min_gpu_count: int = 1,
        prefer_spot: bool = True,
        region: Optional[str] = None,
    ) -> List[GPUOffer]:
        """
        Search available GPU classes on Salad matching the requested GPU type.

        Salad's pricing model is per-hour and set at Container Group creation time
        (using gpu_classes IDs). This method resolves GPU class availability.
        """
        classes = await self.list_gpu_classes()
        keyword = _GPU_TYPE_SEARCH_KEYWORD.get(gpu_type, "")
        offers = []
        for cls in classes:
            if keyword and keyword.lower() not in cls["name"].lower():
                continue
            if cls["gpu_count"] and cls["gpu_count"] < min_gpu_count:
                continue
            price = cls.get("price_min") or 0.0
            if max_price is not None and price > max_price:
                continue
            offer = GPUOffer(
                provider=self.PROVIDER_NAME,
                offer_id=cls["id"],
                gpu_type=cls.get("gpu_type") or gpu_type,
                gpu_count=cls.get("gpu_count") or 1,
                gpu_memory_gb=24.0,  # Salad API doesn't expose VRAM per class directly
                cpu_cores=_DEFAULT_CPU_CORES,
                ram_gb=_DEFAULT_RAM_MB / 1024,
                disk_gb=_DEFAULT_STORAGE_BYTES / (1024 * 1024 * 1024),
                disk_type="nvme",
                price_per_hour=price,
                is_spot=True,  # Salad is inherently interruptible
                raw_data=cls,
            )
            offers.append(offer)
        return sorted(offers, key=lambda o: o.price_per_hour)

    async def create_instance(self, offer, docker_image, docker_command=None, env_vars=None, job_id=None, gpu_class_ids=None):
        """
        Create and start a Single-Replica Container Group on Salad.

        The Container Group name (slug) is used as the MICA instance_id.
        The CG is started immediately after creation (autostart_policy=True).

        Args:
            offer:          GPUOffer from search_offers() — offer_id is the GPU class UUID
            docker_image:   Docker image to run (must be publicly accessible or in authed registry)
            docker_command: Optional command override (list passed as-is or None for image default)
            env_vars:       Environment variables injected at container runtime
            job_id:         MICA job ID for tracking (used to derive CG name)
        """
        from salad_cloud_sdk.models import (
            ContainerGroupCreationRequest,
            ContainerRestartPolicy,
        )

        cg_name = _make_cg_name(job_id or str(uuid.uuid4()))
        start_ts = datetime.now(timezone.utc)

        # The salad-cloud-sdk expects plain dicts for nested models.
        # Passing SDK model instances causes _unmap to fail (expects dict, not object).
        env_dict = env_vars or {}
        gpu_classes = gpu_class_ids if gpu_class_ids else [offer.offer_id]
        container_dict = {
            "image": docker_image,
            "image_caching": _DEFAULT_IMAGE_CACHING,
            "priority": "high",
            "resources": {
                "cpu": _DEFAULT_CPU_CORES,
                "memory": _DEFAULT_RAM_MB,
                "gpu_classes": gpu_classes,
                "storage_amount": _DEFAULT_STORAGE_BYTES,
            },
            "environment_variables": env_dict,
        }
        if docker_command:
            container_dict["command"] = shlex.split(docker_command)

        # Inject GHCR (or generic) basic registry auth if env vars are present.
        # Required when the image is hosted in a private GHCR package.
        # Set GHCR_USERNAME (GitHub actor) and GHCR_TOKEN (PAT with read:packages).
        ghcr_auth = ghcr_basic_auth() if is_ghcr_image(docker_image) else None
        if ghcr_auth:
            container_dict["registry_authentication"] = {
                "basic": ghcr_auth
            }

        # Build creation request (SRCG: 1 replica, never restart on success)
        request = ContainerGroupCreationRequest(
            name=cg_name,
            display_name=_make_display_name(str(job_id or ""), cg_name),
            container=container_dict,
            replicas=1,
            restart_policy=ContainerRestartPolicy.NEVER,
            autostart_policy=True,
        )

        try:
            cg = await self._sdk.container_groups.create_container_group(
                request_body=request,
                organization_name=self._org_name,
                project_name=self._project_name,
            )
            instance = Instance(
                provider=self.PROVIDER_NAME,
                instance_id=cg.name,  # slug — used for all subsequent API calls
                status=InstanceStatus.PROVISIONING,
                gpu_type=offer.gpu_type,
                gpu_count=1,
                price_per_hour=offer.price_per_hour,
                started_at=start_ts,
                job_id=job_id,
                raw_data={"cg_id": cg.id_, "cg_name": cg.name, "org": self._org_name, "project": self._project_name},
            )
            self._instances[cg.name] = instance
            provision_secs = (datetime.now(timezone.utc) - start_ts).total_seconds()
            logger.info("SaladProvider: Container Group %s created (job=%s)", cg.name, job_id)
            return ProvisionResult(
                success=True,
                instance=instance,
                offer_used=offer,
                provision_time_seconds=provision_secs,
            )
        except Exception as exc:
            logger.error("SaladProvider: create_instance failed: %s", exc)
            error_message = str(exc)
            project_path = f"/organizations/{self._org_name}/projects/{self._project_name}/containers"
            if "404 error" in error_message and project_path in error_message:
                error_message = (
                    f"Salad project '{self._project_name}' was not found in organization '{self._org_name}'. "
                    f"Create the project in the Salad portal or set SALAD_PROJECT_NAME to an existing project. "
                    f"Original error: {exc}"
                )
            return ProvisionResult(success=False, error_message=error_message)

    async def destroy_instance(self, instance_id: str) -> bool:
        """
        Stop then delete the Container Group.

        instance_id = container group name (slug).
        """
        try:
            # Stop first (idempotent — OK if already stopped)
            try:
                await self._sdk.container_groups.stop_container_group(
                    organization_name=self._org_name,
                    project_name=self._project_name,
                    container_group_name=instance_id,
                )
            except Exception as stop_err:
                logger.warning("SaladProvider: stop failed (continuing): %s", stop_err)

            # Delete
            await self._sdk.container_groups.delete_container_group(
                organization_name=self._org_name,
                project_name=self._project_name,
                container_group_name=instance_id,
            )
            self._instances.pop(instance_id, None)
            logger.info("SaladProvider: Container Group %s deleted", instance_id)
            return True
        except Exception as exc:
            logger.error("SaladProvider: destroy_instance(%s) failed: %s", instance_id, exc)
            return False

    async def inspect_container_group(self, instance_id: str) -> Dict[str, Any]:
        """Collect a best-effort status/log snapshot before teardown or forensics."""
        inspection: Dict[str, Any] = {
            "cg_name": instance_id,
            "provider": self.PROVIDER_NAME,
            "inspected_at": datetime.now(timezone.utc).isoformat(),
            "status_probe": {},
            "system_log_probe": {},
            "inspection_errors": [],
        }
        try:
            instance = await self.get_instance_status(instance_id)
            inspection["status_probe"] = {
                "status": getattr(getattr(instance, "status", None), "value", None)
                or str(getattr(instance, "status", "")),
                "error_message": str(getattr(instance, "error_message", "") or ""),
                "raw_data": dict(getattr(instance, "raw_data", {}) or {}),
            }
        except Exception as exc:
            inspection["inspection_errors"].append(f"status_probe_failed: {exc}")

        try:
            items = self._fetch_system_logs(instance_id)
            inspection["system_log_probe"] = {
                "log_count": len(items),
                "latest_summary": _latest_system_log_summary(items),
                "recent_items": items[:20],
            }
        except Exception as exc:
            inspection["inspection_errors"].append(f"system_log_probe_failed: {exc}")
        return inspection

    async def get_instance_status(self, instance_id: str) -> Instance:
        """
        Get current Container Group status.

        Maps Salad CG status → MICA InstanceStatus:
          pending   → PENDING
          deploying → PROVISIONING
          running   → RUNNING
          stopped/succeeded → STOPPED
          failed    → ERROR
        """
        try:
            cg = await self._sdk.container_groups.get_container_group(
                organization_name=self._org_name,
                project_name=self._project_name,
                container_group_name=instance_id,
            )
            cached = self._instances.get(instance_id)

            # Extract status from current_state
            status_str = "pending"
            if cg.current_state and cg.current_state.status:
                status_str = str(cg.current_state.status.value if hasattr(cg.current_state.status, "value") else cg.current_state.status)

            mica_status = _salad_status_to_mica(status_str)
            instance = Instance(
                provider=self.PROVIDER_NAME,
                instance_id=instance_id,
                status=mica_status,
                gpu_type=cached.gpu_type if cached else None,
                gpu_count=1,
                price_per_hour=cached.price_per_hour if cached else 0.0,
                started_at=cached.started_at if cached else None,
                job_id=cached.job_id if cached else None,
                raw_data={
                    "status_str": status_str,
                    "cg_name": instance_id,
                    "replicas": cg.replicas,
                    "version": cg.version,
                    "safe_environment": _project_safe_container_environment(cg),
                },
            )
            safe_environment = instance.raw_data.get("safe_environment") or {}
            if isinstance(safe_environment, dict):
                if safe_environment.get("OUTPUT_GCS_PREFIX"):
                    output_prefix = str(safe_environment.get("OUTPUT_GCS_PREFIX") or "").rstrip("/")
                    if output_prefix.endswith("/output"):
                        output_prefix = output_prefix[: -len("/output")]
                    instance.raw_data["output_gcs_prefix"] = output_prefix
                if safe_environment.get("MICA_JOB_ID"):
                    instance.raw_data["provider_job_id"] = str(safe_environment.get("MICA_JOB_ID") or "")
            if status_str == "deploying":
                try:
                    instance.raw_data.update(_latest_system_log_summary(self._fetch_system_logs(instance_id)))
                except Exception as log_exc:
                    logger.debug(
                        "SaladProvider: live system-log enrichment failed for %s: %s",
                        instance_id,
                        log_exc,
                    )
            if status_str in {"deploying", "running"}:
                try:
                    instances = self._sdk.container_groups.list_container_group_instances(
                        organization_name=self._org_name,
                        project_name=self._project_name,
                        container_group_name=instance_id,
                    )
                    if inspect.isawaitable(instances):
                        instances = await instances
                    instance.raw_data.update(_summarize_container_group_instances(instances))
                except Exception as instances_exc:
                    logger.debug(
                        "SaladProvider: live instance-state enrichment failed for %s: %s",
                        instance_id,
                        instances_exc,
                    )
            self._instances[instance_id] = instance
            return instance
        except Exception as exc:
            fallback = self._instance_from_system_logs(instance_id)
            if fallback is not None:
                if not fallback.error_message:
                    fallback.error_message = str(exc)
                self._instances[instance_id] = fallback
                return fallback
            logger.error("SaladProvider: get_instance_status(%s) failed: %s", instance_id, exc)
            return Instance(
                provider=self.PROVIDER_NAME,
                instance_id=instance_id,
                status=InstanceStatus.ERROR,
                error_message=str(exc),
            )

    def _system_logs_base_url(self) -> str:
        service = getattr(self._sdk, "system_logs", None)
        base_url = getattr(service, "base_url", None)
        return str(base_url or "https://api.salad.com/api/public").rstrip("/")

    def _fetch_system_logs(self, instance_id: str) -> list[dict[str, Any]]:
        url = (
            f"{self._system_logs_base_url()}/organizations/{self._org_name}"
            f"/projects/{self._project_name}/containers/{instance_id}/system-logs"
        )
        response = requests.get(
            url,
            headers={"Salad-Api-Key": self._api_key},
            timeout=max(int(self._timeout_ms / 1000), 5),
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def _instance_from_system_logs(self, instance_id: str) -> Optional[Instance]:
        try:
            items = self._fetch_system_logs(instance_id)
        except Exception as log_exc:
            logger.debug(
                "SaladProvider: system-log fallback failed for %s: %s",
                instance_id,
                log_exc,
            )
            return None

        if not items:
            return None

        latest = items[0]
        status, latest_event = _event_name_to_mica_status(latest.get("event_name", ""))
        if status == InstanceStatus.STOPPED:
            for item in items:
                candidate_status, candidate_event = _event_name_to_mica_status(item.get("event_name", ""))
                if candidate_status == InstanceStatus.ERROR:
                    status = candidate_status
                    latest_event = candidate_event
                    break
        cached = self._instances.get(instance_id)
        recent_events = [
            {
                "event_name": item.get("event_name", ""),
                "event_time": item.get("event_time", ""),
                "instance_id": item.get("instance_id", ""),
            }
            for item in items[:5]
        ]
        error_message = latest_event if status == InstanceStatus.ERROR else None
        return Instance(
            provider=self.PROVIDER_NAME,
            instance_id=instance_id,
            status=status,
            gpu_type=cached.gpu_type if cached else None,
            gpu_count=1,
            price_per_hour=cached.price_per_hour if cached else 0.0,
            started_at=cached.started_at if cached else None,
            job_id=cached.job_id if cached else None,
            error_message=error_message,
            raw_data={
                "status_source": "system_logs",
                "status_str": latest_event.lower(),
                "cg_name": instance_id,
                **_latest_system_log_summary(items),
            },
        )

    async def reallocate_container_group_instance(self, instance_id: str, container_group_instance_id: str) -> bool:
        try:
            await self._sdk.container_groups.reallocate_container_group_instance(
                organization_name=self._org_name,
                project_name=self._project_name,
                container_group_name=instance_id,
                container_group_instance_id=container_group_instance_id,
            )
            logger.info(
                "SaladProvider: reallocate requested for %s instance %s",
                instance_id,
                container_group_instance_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "SaladProvider: reallocate failed for %s instance %s: %s",
                instance_id,
                container_group_instance_id,
                exc,
            )
            return False

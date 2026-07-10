from __future__ import annotations

import os


_DEFAULT_SALAD_COMMAND = "python /app/main_gcs.py"


def _default_ghcr_owner() -> str:
    return os.getenv("MICA_GHCR_OWNER", "juaness38").strip() or "juaness38"


def _default_image_repository() -> str:
    return f"ghcr.io/{_default_ghcr_owner()}/mica-openmm-pod"


def _default_image_tag() -> str:
    return os.getenv("MICA_MD_DOCKER_IMAGE_TAG", "latest").strip() or "latest"


def _configured_image_digest() -> str:
    digest = (
        os.getenv("MICA_MD_DOCKER_IMAGE_DIGEST", "").strip()
        or os.getenv("MICA_OPENMM_POD_IMAGE_DIGEST", "").strip()
    )
    if digest and not digest.startswith("sha256:"):
        digest = f"sha256:{digest}"
    return digest


def canonical_md_worker_image() -> str:
    explicit_image = (
        os.getenv("MICA_MD_DOCKER_IMAGE", "").strip()
        or os.getenv("MICA_OPENMM_POD_IMAGE", "").strip()
    )
    if explicit_image:
        return explicit_image

    image_repository = _default_image_repository()
    image_digest = _configured_image_digest()
    if image_digest:
        return f"{image_repository}@{image_digest}"

    return f"{image_repository}:{_default_image_tag()}"


def default_salad_worker_command() -> str:
    return os.getenv("MICA_SALAD_WORKER_COMMAND") or _DEFAULT_SALAD_COMMAND


def is_ghcr_image(image: str) -> bool:
    return image.strip().startswith("ghcr.io/")


def ghcr_basic_auth() -> dict[str, str] | None:
    username = os.getenv("GHCR_USERNAME", "").strip()
    password = os.getenv("GHCR_TOKEN", "").strip()
    if not username or not password:
        return None
    return {"username": username, "password": password}

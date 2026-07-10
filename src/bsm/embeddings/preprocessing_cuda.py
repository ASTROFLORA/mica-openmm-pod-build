"""CUDA-accelerated preprocessing utilities for embedding pipelines.

These helpers centralize the anisotropy correction and ZCA whitening steps
required by the Phase 3 NJ rebuild. They operate on torch tensors so the
entire workflow can stay on GPU until we need numpy outputs for legacy code.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np

try:
    import torch
    import torch.nn.functional as F
except ImportError as exc:  # pragma: no cover - torch is mandatory for CUDA path
    raise ImportError("Torch is required for CUDA preprocessing") from exc

logger = logging.getLogger(__name__)


def _resolve_device(user_device: Optional[str]) -> torch.device:
    if user_device is not None:
        return torch.device(user_device)
    if torch.cuda.is_available():
        return torch.device("cuda")
    logger.warning("CUDA not available; falling back to CPU")
    return torch.device("cpu")


def _to_tensor(data: np.ndarray, device: torch.device) -> torch.Tensor:
    if not isinstance(data, np.ndarray):
        raise TypeError("preprocess_embeddings expects a numpy array input")
    tensor = torch.from_numpy(data.astype(np.float32, copy=False))
    return tensor.to(device)


def _center(tensor: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    mean = tensor.mean(dim=0, keepdim=True)
    centered = tensor - mean
    return centered, mean.squeeze(0)


def _remove_anisotropy(tensor: torch.Tensor, components: int, eps: float) -> torch.Tensor:
    if components <= 0:
        return tensor
    cov = torch.matmul(tensor.T, tensor) / max(1, tensor.shape[0] - 1)
    cov = cov + eps * torch.eye(cov.shape[0], device=tensor.device, dtype=tensor.dtype)
    eigenvalues, eigenvectors = torch.linalg.eigh(cov)
    order = torch.argsort(eigenvalues, descending=True)
    top_vectors = eigenvectors[:, order[:components]]
    projection = torch.matmul(torch.matmul(tensor, top_vectors), top_vectors.T)
    return tensor - projection


def _zca_whiten(tensor: torch.Tensor, eps: float) -> torch.Tensor:
    cov = torch.matmul(tensor.T, tensor) / max(1, tensor.shape[0] - 1)
    cov = cov + eps * torch.eye(cov.shape[0], device=tensor.device, dtype=tensor.dtype)
    eigenvalues, eigenvectors = torch.linalg.eigh(cov)
    whitening = eigenvectors @ torch.diag(torch.rsqrt(eigenvalues + eps)) @ eigenvectors.T
    whitened = torch.matmul(tensor, whitening)
    return whitened


def preprocess_embeddings(
    embeddings: np.ndarray,
    *,
    device: Optional[str] = None,
    anisotropy_components: int = 1,
    apply_zca: bool = True,
    eps: float = 1e-5,
) -> np.ndarray:
    """Run centering, anisotropy correction, and optional ZCA whitening on GPU."""
    target_device = _resolve_device(device)
    tensor = _to_tensor(embeddings, target_device)
    centered, _ = _center(tensor)
    corrected = _remove_anisotropy(centered, anisotropy_components, eps)
    if apply_zca:
        corrected = _zca_whiten(corrected, eps)
    normalized = F.normalize(corrected, p=2.0, dim=1, eps=eps)
    return normalized.to("cpu", non_blocking=True).numpy()


def cosine_distance_condensed_cuda(
    tensor: torch.Tensor,
    *,
    batch_size: int = 2048,
    eps: float = 1e-8,
) -> np.ndarray:
    """Compute condensed cosine distances entirely on GPU with chunking."""
    if tensor.dim() != 2:
        raise ValueError("Input tensor must be 2D (num_samples x embedding_dim)")
    tensor = F.normalize(tensor, p=2.0, dim=1, eps=eps)
    n = tensor.shape[0]
    m = n * (n - 1) // 2
    out = torch.empty(m, device="cpu", dtype=torch.float32)
    position = 0
    for i in range(n - 1):
        anchor = tensor[i]
        for start in range(i + 1, n, batch_size):
            end = min(n, start + batch_size)
            chunk = tensor[start:end]
            sims = torch.matmul(chunk, anchor)
            dists = (1.0 - sims).to(dtype=torch.float32).cpu()
            length = end - start
            out[position : position + length] = dists
            position += length
    return out.numpy()

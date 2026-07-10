"""
GPU Routing Intelligence — W4.

Architecture-tiered scoring, DCEM, VRAM checks, and cascade generation.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from mica.infrastructure.providers.base_provider import GPUOffer, GPUType

logger = logging.getLogger("mica.infrastructure.gpu_scorer")


# Tier 5 = Blackwell, Tier 4 = Ada/Lovelace, Tier 3 = Ampere, Tier 2 = Hopper, Tier 1 = Legacy
GPU_TIERS: Dict[GPUType, int] = {
    GPUType.RTX_5090: 5,
    GPUType.RTX_5080: 5,
    GPUType.RTX_5070Ti: 5,
    GPUType.RTX_4090: 4,
    GPUType.RTX_4080: 4,
    GPUType.L40S: 4,
    GPUType.L40: 4,
    GPUType.RTX_3090: 3,
    GPUType.RTX_3080: 3,
    GPUType.A100_80GB: 3,
    GPUType.A100_40GB: 3,
    GPUType.A40: 3,
    GPUType.A10: 3,
    GPUType.H100_80GB: 2,
    GPUType.H100_SXM: 2,
    GPUType.V100_16GB: 1,
    GPUType.V100_32GB: 1,
    GPUType.T4: 1,
}

GPU_VRAM: Dict[GPUType, float] = {
    GPUType.RTX_5090: 32.0,
    GPUType.RTX_5080: 16.0,
    GPUType.RTX_5070Ti: 16.0,
    GPUType.RTX_4090: 24.0,
    GPUType.RTX_4080: 16.0,
    GPUType.L40S: 48.0,
    GPUType.L40: 48.0,
    GPUType.RTX_3090: 24.0,
    GPUType.RTX_3080: 10.0,
    GPUType.A100_80GB: 80.0,
    GPUType.A100_40GB: 40.0,
    GPUType.A40: 48.0,
    GPUType.A10: 24.0,
    GPUType.H100_80GB: 80.0,
    GPUType.H100_SXM: 80.0,
    GPUType.V100_16GB: 16.0,
    GPUType.V100_32GB: 32.0,
    GPUType.T4: 16.0,
}

# Estimated ns/day for ~50k-atom all-atom MD (OpenMM, PME, 2fs timestep)
# Seeded from empirical RTX_5080 data (~350 ns/day) then scaled by relative perf
GPU_NS_PER_DAY: Dict[GPUType, float] = {
    GPUType.RTX_5090: 420.0,
    GPUType.RTX_5080: 350.0,
    GPUType.RTX_5070Ti: 280.0,
    GPUType.RTX_4090: 300.0,
    GPUType.RTX_4080: 220.0,
    GPUType.L40S: 260.0,
    GPUType.L40: 240.0,
    GPUType.RTX_3090: 180.0,
    GPUType.RTX_3080: 140.0,
    GPUType.A100_80GB: 280.0,
    GPUType.A100_40GB: 260.0,
    GPUType.A40: 180.0,
    GPUType.A10: 100.0,
    GPUType.H100_80GB: 380.0,
    GPUType.H100_SXM: 400.0,
    GPUType.V100_16GB: 80.0,
    GPUType.V100_32GB: 85.0,
    GPUType.T4: 40.0,
}

# Pre-built fallback cascades: preferred GPU → ordered fallbacks
GPU_CASCADES: Dict[GPUType, List[GPUType]] = {
    GPUType.RTX_5090: [GPUType.RTX_5080, GPUType.H100_SXM, GPUType.RTX_4090, GPUType.A100_80GB, GPUType.L40S],
    GPUType.RTX_5080: [GPUType.RTX_4090, GPUType.H100_80GB, GPUType.A100_80GB, GPUType.L40S, GPUType.RTX_3090],
    GPUType.RTX_4090: [GPUType.RTX_5080, GPUType.A100_80GB, GPUType.L40S, GPUType.RTX_3090, GPUType.A100_40GB],
    GPUType.H100_80GB: [GPUType.H100_SXM, GPUType.A100_80GB, GPUType.RTX_5090, GPUType.RTX_5080, GPUType.L40S],
    GPUType.A100_80GB: [GPUType.A100_40GB, GPUType.L40S, GPUType.RTX_4090, GPUType.RTX_5080, GPUType.A40],
    GPUType.L40S: [GPUType.L40, GPUType.A100_40GB, GPUType.RTX_4090, GPUType.A40, GPUType.RTX_3090],
}


class GPUScorer:
    """GPU intelligence: tiering, VRAM check, DCEM, cascade generation."""

    def score_gpu(self, gpu_type: GPUType, price_per_hour: float) -> float:
        """Score = tier_weight × (ns_per_day / price). Higher is better."""
        tier = GPU_TIERS.get(gpu_type, 1)
        ns_day = GPU_NS_PER_DAY.get(gpu_type, 40.0)
        if price_per_hour <= 0:
            return float("inf")
        return tier * (ns_day / price_per_hour)

    def dcem(self, gpu_type: GPUType, price_per_hour: float, system_atoms: int = 50_000) -> float:
        """Dynamic Cost-Efficiency Metric = $/ns (lower is better).

        Tries empirical benchmark first, falls back to GPU_NS_PER_DAY table.
        """
        ns_day = GPU_NS_PER_DAY.get(gpu_type, 0.0)
        # Try empirical benchmark if available
        try:
            from mica.infrastructure.costing import get_empirical_md_benchmark
            empirical = get_empirical_md_benchmark(system_atoms, gpu_type=gpu_type)
            if empirical is not None and float(empirical.get("ns_day") or 0.0) > 0:
                ns_day = float(empirical["ns_day"])
        except (ImportError, Exception):
            pass

        if ns_day <= 0:
            return float("inf")
        ns_per_hour = ns_day / 24.0
        if ns_per_hour <= 0:
            return float("inf")
        return price_per_hour / ns_per_hour

    def check_vram(self, gpu_type: GPUType, atom_count: int) -> bool:
        """Check if GPU has sufficient VRAM for atom_count.

        Rule: ~1 GB per 15k atoms + 2 GB CUDA context overhead.
        """
        if atom_count <= 0:
            return True
        required_gb = atom_count / 15_000 + 2.0
        available = GPU_VRAM.get(gpu_type, 0.0)
        return available >= required_gb

    def get_cascade(self, preferred_gpu: GPUType) -> List[GPUType]:
        """Get fallback GPU cascade starting from preferred.

        Uses pre-built cascades if available, otherwise generates dynamically
        by sorting remaining GPUs by tier (desc) then ns/day (desc).
        """
        if preferred_gpu in GPU_CASCADES:
            return [preferred_gpu] + GPU_CASCADES[preferred_gpu]

        # Dynamic: same VRAM or higher, sorted by tier desc then ns/day desc
        preferred_vram = GPU_VRAM.get(preferred_gpu, 0)
        candidates = [
            g for g in GPUType
            if g != preferred_gpu and GPU_VRAM.get(g, 0) >= preferred_vram
        ]
        candidates.sort(
            key=lambda g: (GPU_TIERS.get(g, 0), GPU_NS_PER_DAY.get(g, 0)),
            reverse=True,
        )
        return [preferred_gpu] + candidates

    def select_best(self, offers: List[GPUOffer], atom_count: int = 0) -> Optional[GPUOffer]:
        """Select best offer by DCEM, filtering by VRAM if atom_count > 0."""
        valid = offers
        if atom_count > 0:
            valid = [o for o in valid if self.check_vram(o.gpu_type, atom_count)]
        if not valid:
            return None
        # Sort by DCEM ascending (lower = cheaper per ns)
        valid.sort(key=lambda o: self.dcem(o.gpu_type, o.price_per_hour, atom_count))
        return valid[0]

    def as_scorer(self, atom_count: int = 50_000) -> Callable[[GPUOffer], float]:
        """Return a scorer callable compatible with CloudOrchestrator.set_scorer()."""
        def _score(offer: GPUOffer) -> float:
            return self.dcem(offer.gpu_type, offer.price_per_hour, atom_count)
        return _score

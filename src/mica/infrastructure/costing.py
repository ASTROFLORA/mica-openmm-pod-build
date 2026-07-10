from __future__ import annotations

import inspect
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from .cloud_orchestrator import CloudOrchestrator
from .providers.base_provider import GPUOffer, GPUType

logger = logging.getLogger(__name__)

_PROVIDER_DISPLAY_NAMES = {
    "vast": "Vast.ai",
    "runpod_pods": "RunPod",
    "runpod": "RunPod",
    "gcp": "GCP",
}

_GPU_TYPE_ALIASES = {
    "3090": GPUType.RTX_3090,
    "rtx3090": GPUType.RTX_3090,
    "rtx_3090": GPUType.RTX_3090,
    "4090": GPUType.RTX_4090,
    "rtx4090": GPUType.RTX_4090,
    "rtx_4090": GPUType.RTX_4090,
    "5080": GPUType.RTX_5080,
    "rtx5080": GPUType.RTX_5080,
    "rtx_5080": GPUType.RTX_5080,
    "5090": GPUType.RTX_5090,
    "rtx5090": GPUType.RTX_5090,
    "rtx_5090": GPUType.RTX_5090,
    "l40s": GPUType.L40S,
    "l40": GPUType.L40,
    "a40": GPUType.A40,
    "a10": GPUType.A10,
    "a100": GPUType.A100_80GB,
    "a100_40gb": GPUType.A100_40GB,
    "a100_80gb": GPUType.A100_80GB,
    "h100": GPUType.H100_80GB,
    "h100_80gb": GPUType.H100_80GB,
    "h100_sxm": GPUType.H100_SXM,
    "v100": GPUType.V100_32GB,
    "t4": GPUType.T4,
}

_RTX_5080_EMPIRICAL_SOURCE = "inventory_rtx5080_all_atom_2026_05_10"
_RTX_5090_EMPIRICAL_SOURCE = "inventory_rtx5090_all_atom_2026_05_10"
_USD_PER_NS_FORMULA = "(price_per_hour_usd * 24) / ns_day"
_COST_FOR_200NS_FORMULA = "(price_per_hour_usd * 24 * 200) / ns_day"


@dataclass(frozen=True)
class EmpiricalMDObservation:
    system_name: str
    gpu_type: GPUType
    system_atoms: int
    ns_day: float
    workload_type: str = "all_atom"
    include_in_default_model: bool = True
    box_angstrom: tuple[float, float, float] | None = None
    source: str = _RTX_5080_EMPIRICAL_SOURCE
    timestep_fs: float | None = 4.0
    production_ns: float | None = 200.0
    hours_per_200ns: float | None = None
    steps_per_sec: float | None = None
    normalized_ns_day_per_10k_atoms: float | None = None


_EMPIRICAL_MD_OBSERVATIONS: tuple[EmpiricalMDObservation, ...] = (
    EmpiricalMDObservation(
        system_name="Control_8_OSR1-WNK1_extended",
        gpu_type=GPUType.RTX_5080,
        system_atoms=70799,
        ns_day=211.614708854855,
        hours_per_200ns=None,
        steps_per_sec=612.3110788624276,
        normalized_ns_day_per_10k_atoms=29.892622820964796,
    ),
    EmpiricalMDObservation(
        system_name="control_6_apo_SPAK",
        gpu_type=GPUType.RTX_5080,
        system_atoms=41216,
        ns_day=350.493301086414,
        hours_per_200ns=13.695,
        steps_per_sec=1014.1588573102258,
        normalized_ns_day_per_10k_atoms=85.04558515654344,
    ),
    EmpiricalMDObservation(
        system_name="control_5_negativo_SPAK_taylormutant",
        gpu_type=GPUType.RTX_5080,
        system_atoms=51270,
        ns_day=468.7517881461645,
        hours_per_200ns=10.24,
        steps_per_sec=1356.3419795895963,
        normalized_ns_day_per_10k_atoms=91.42801871550452,
    ),
    EmpiricalMDObservation(
        system_name="OSR1-WNK1_from_6FBK_20aa",
        gpu_type=GPUType.RTX_5080,
        system_atoms=22541,
        ns_day=492.2,
        normalized_ns_day_per_10k_atoms=218.35765937624774,
    ),
    EmpiricalMDObservation(
        system_name="control_4_SPAK_WNK1_peptide",
        gpu_type=GPUType.RTX_5080,
        system_atoms=47760,
        ns_day=542.2,
        normalized_ns_day_per_10k_atoms=113.52596314907873,
    ),
    EmpiricalMDObservation(
        system_name="control_7_negativo_OSR1_taylormutant",
        gpu_type=GPUType.RTX_5080,
        system_atoms=21890,
        ns_day=844.0,
        include_in_default_model=False,
        normalized_ns_day_per_10k_atoms=398.2392095316807,
    ),
    EmpiricalMDObservation(
        system_name="NRBP1_replica1",
        gpu_type=GPUType.RTX_5080,
        system_atoms=23189,
        ns_day=1027.8372591006425,
        include_in_default_model=False,
        hours_per_200ns=4.67,
        steps_per_sec=2974.0661432310253,
        normalized_ns_day_per_10k_atoms=443.2434598734929,
    ),
    EmpiricalMDObservation(
        system_name="NRBP1_replica2",
        gpu_type=GPUType.RTX_5080,
        system_atoms=23189,
        ns_day=956.1752988047808,
        include_in_default_model=False,
        hours_per_200ns=5.02,
        steps_per_sec=2766.7109340416114,
        normalized_ns_day_per_10k_atoms=412.3400313962572,
    ),
    EmpiricalMDObservation(
        system_name="novel_ccts_and_WNK4_erendira",
        gpu_type=GPUType.RTX_5080,
        system_atoms=101706,
        ns_day=518.8333333333334,
        include_in_default_model=False,
        normalized_ns_day_per_10k_atoms=142.38586603339328,
    ),
    EmpiricalMDObservation(
        system_name="Control_9_bound_SPAK_WNK4",
        gpu_type=GPUType.RTX_5090,
        system_atoms=27549,
        ns_day=709.0118866837723,
        include_in_default_model=False,
        source=_RTX_5090_EMPIRICAL_SOURCE,
        hours_per_200ns=6.77,
        steps_per_sec=2051.539023969249,
        normalized_ns_day_per_10k_atoms=257.3639285214608,
    ),
)


def _select_empirical_observations(
    *,
    gpu_type: GPUType | None = None,
    workload_type: str | None = None,
    include_excluded: bool = True,
) -> list[EmpiricalMDObservation]:
    observations: list[EmpiricalMDObservation] = []
    for obs in _EMPIRICAL_MD_OBSERVATIONS:
        if gpu_type is not None and obs.gpu_type != gpu_type:
            continue
        if workload_type is not None and obs.workload_type != workload_type:
            continue
        if not include_excluded and not obs.include_in_default_model:
            continue
        observations.append(obs)
    observations.sort(key=lambda obs: (obs.gpu_type.value, obs.system_atoms, obs.system_name))
    return observations


def list_empirical_md_observations(
    *,
    gpu_type: GPUType | str | None = None,
    workload_type: str | None = None,
    include_excluded: bool = True,
) -> list[Dict[str, Any]]:
    resolved_gpu = normalize_gpu_type(gpu_type) if gpu_type is not None else None
    rows: list[Dict[str, Any]] = []
    for obs in _select_empirical_observations(
        gpu_type=resolved_gpu,
        workload_type=workload_type,
        include_excluded=include_excluded,
    ):
        rows.append(
            {
                "system_name": obs.system_name,
                "gpu_type": obs.gpu_type.value,
                "system_atoms": obs.system_atoms,
                "ns_day": obs.ns_day,
                "hours_per_200ns": obs.hours_per_200ns,
                "hours_per_200ns_derived": (24.0 * 200.0) / obs.ns_day,
                "steps_per_sec": obs.steps_per_sec,
                "normalized_ns_day_per_10k_atoms": (
                    obs.normalized_ns_day_per_10k_atoms
                    if obs.normalized_ns_day_per_10k_atoms is not None
                    else (obs.ns_day * 10000.0 / max(obs.system_atoms, 1))
                ),
                "workload_type": obs.workload_type,
                "include_in_default_model": obs.include_in_default_model,
                "source": obs.source,
                "timestep_fs": obs.timestep_fs,
                "production_ns": obs.production_ns,
            }
        )
    return rows


def build_empirical_md_heuristic_table(
    *,
    prices_by_gpu: Optional[Mapping[GPUType | str, float]] = None,
    workload_type: str = "all_atom",
    include_excluded: bool = True,
) -> list[Dict[str, Any]]:
    normalized_prices: Dict[str, float] = {}
    for gpu_key, price in (prices_by_gpu or {}).items():
        normalized_prices[normalize_gpu_type(gpu_key).value] = float(price)

    table: list[Dict[str, Any]] = []
    for row in list_empirical_md_observations(
        workload_type=workload_type,
        include_excluded=include_excluded,
    ):
        price = normalized_prices.get(str(row["gpu_type"]))
        row["usd_per_ns_formula"] = _USD_PER_NS_FORMULA
        row["cost_for_200ns_formula"] = _COST_FOR_200NS_FORMULA
        if price is not None:
            row["price_per_hour_usd"] = price
            row["usd_per_ns"] = (price * 24.0) / float(row["ns_day"])
            row["cost_for_200ns_usd"] = (price * 24.0 * 200.0) / float(row["ns_day"])
        table.append(row)
    return table


@dataclass
class ProviderPriceSnapshot:
    provider_id: str
    provider_name: str
    gpu_type: str
    price_per_hour: float
    offer_id: str
    gpu_count: int
    gpu_memory_gb: float
    is_spot: bool
    region: str = ""
    datacenter: str = ""
    offer_count: int = 1
    sampled_at: str = ""
    source: str = "live_market"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


async def _maybe_close_provider(provider: Any) -> None:
    close = getattr(provider, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("Provider close failed", exc_info=True)


def normalize_gpu_type(gpu_type: GPUType | str | None, default: GPUType = GPUType.RTX_5080) -> GPUType:
    if gpu_type is None:
        return default
    if isinstance(gpu_type, GPUType):
        return gpu_type
    raw = str(gpu_type).strip()
    if not raw:
        return default
    candidate = raw.upper().replace("-", "_").replace(" ", "_")
    try:
        return GPUType[candidate]
    except KeyError:
        pass
    try:
        return GPUType(candidate)
    except ValueError:
        pass
    alias = raw.lower().replace("-", "").replace("_", "").replace(" ", "")
    return _GPU_TYPE_ALIASES.get(alias, default)


def _pool_adjacent_violators_decreasing(values: Iterable[float]) -> list[float]:
    blocks: list[dict[str, float | int]] = []
    for value in values:
        blocks.append({"sum": float(value), "count": 1})
        while len(blocks) >= 2:
            prev_avg = float(blocks[-2]["sum"]) / int(blocks[-2]["count"])
            curr_avg = float(blocks[-1]["sum"]) / int(blocks[-1]["count"])
            if prev_avg >= curr_avg:
                break
            merged = {
                "sum": float(blocks[-2]["sum"]) + float(blocks[-1]["sum"]),
                "count": int(blocks[-2]["count"]) + int(blocks[-1]["count"]),
            }
            blocks[-2:] = [merged]

    smoothed: list[float] = []
    for block in blocks:
        avg = float(block["sum"]) / int(block["count"])
        smoothed.extend([avg] * int(block["count"]))
    return smoothed


def _interpolate_on_log_atoms(target_atoms: int, atoms: list[int], ns_day: list[float]) -> float:
    if not atoms or not ns_day or len(atoms) != len(ns_day):
        raise ValueError("atoms and ns_day must be non-empty and aligned")
    target = max(int(target_atoms or 0), 1)
    if target <= atoms[0]:
        return float(ns_day[0])
    if target >= atoms[-1]:
        return float(ns_day[-1])

    target_log = math.log(target)
    for idx in range(1, len(atoms)):
        if target <= atoms[idx]:
            left_atoms = atoms[idx - 1]
            right_atoms = atoms[idx]
            left_log = math.log(left_atoms)
            right_log = math.log(right_atoms)
            if math.isclose(left_log, right_log):
                return float(ns_day[idx])
            ratio = (target_log - left_log) / (right_log - left_log)
            return float(ns_day[idx - 1] + ratio * (ns_day[idx] - ns_day[idx - 1]))
    return float(ns_day[-1])


def get_empirical_md_benchmark(
    system_atoms: int,
    *,
    gpu_type: GPUType | str | None = None,
) -> Optional[Dict[str, Any]]:
    resolved_gpu = normalize_gpu_type(gpu_type)
    included = _select_empirical_observations(
        gpu_type=resolved_gpu,
        workload_type="all_atom",
        include_excluded=False,
    )
    excluded = _select_empirical_observations(
        gpu_type=resolved_gpu,
        workload_type="all_atom",
        include_excluded=True,
    )
    excluded = [obs for obs in excluded if not obs.include_in_default_model]
    if not included:
        return None

    included = sorted(included, key=lambda obs: obs.system_atoms)
    atoms = [obs.system_atoms for obs in included]
    raw_values = [obs.ns_day for obs in included]
    smoothed_values = _pool_adjacent_violators_decreasing(raw_values)
    predicted = _interpolate_on_log_atoms(int(system_atoms), atoms, smoothed_values)
    nearest = min(included, key=lambda obs: abs(obs.system_atoms - int(system_atoms)))

    return {
        "ns_day": predicted,
        "source": str(nearest.source),
        "gpu_type": resolved_gpu.value,
        "workload_type": "all_atom",
        "observation_count": len(included),
        "excluded_observation_count": len(excluded),
        "nearest_system": nearest.system_name,
        "nearest_system_atoms": nearest.system_atoms,
        "smoothed_reference_points": [
            {"system_name": obs.system_name, "system_atoms": obs.system_atoms, "ns_day": value}
            for obs, value in zip(included, smoothed_values)
        ],
    }


async def collect_live_gpu_price_snapshots(
    gpu_type: GPUType | str,
    *,
    max_price_per_hour: Optional[float] = None,
    providers: Optional[Iterable[str]] = None,
    prefer_spot: bool = True,
) -> Dict[str, ProviderPriceSnapshot]:
    resolved_gpu = normalize_gpu_type(gpu_type)
    orchestrator = CloudOrchestrator()
    provider_objects: list[Any] = []
    requested = {str(p).lower() for p in providers} if providers else None

    try:
        from .providers.vast_provider import VastProvider

        if requested is None or "vast" in requested or "vast.ai" in requested:
            try:
                provider = VastProvider()
                orchestrator.register_provider(provider)
                provider_objects.append(provider)
            except Exception as exc:
                logger.info("Skipping Vast live pricing: %s", exc)
    except Exception as exc:
        logger.debug("Vast provider import failed: %s", exc)

    try:
        from .providers.runpod_pods_provider import RunPodPodsProvider

        if requested is None or "runpod" in requested or "runpod_pods" in requested:
            try:
                provider = RunPodPodsProvider(prefer_spot=prefer_spot)
                orchestrator.register_provider(provider)
                provider_objects.append(provider)
            except Exception as exc:
                logger.info("Skipping RunPod live pricing: %s", exc)
    except Exception as exc:
        logger.debug("RunPod provider import failed: %s", exc)

        if not orchestrator.providers:
            return {}

        offers = await orchestrator.search_all_offers(
            gpu_type=resolved_gpu,
            max_price=max_price_per_hour,
            prefer_spot=prefer_spot,
        )
        sampled_at = datetime.now(timezone.utc).isoformat()
        counts: Dict[str, int] = {}
        for offer in offers:
            counts[offer.provider] = counts.get(offer.provider, 0) + 1

        snapshots: Dict[str, ProviderPriceSnapshot] = {}
        for offer in offers:
            provider_id = offer.provider
            current = snapshots.get(provider_id)
            if current and current.price_per_hour <= offer.price_per_hour:
                continue
            snapshots[provider_id] = snapshot_from_offer(offer, offer_count=counts.get(provider_id, 1), sampled_at=sampled_at)

        return snapshots
    finally:
        for provider in provider_objects:
            await _maybe_close_provider(provider)


def snapshot_from_offer(offer: GPUOffer, *, offer_count: int = 1, sampled_at: Optional[str] = None) -> ProviderPriceSnapshot:
    return ProviderPriceSnapshot(
        provider_id=offer.provider,
        provider_name=_PROVIDER_DISPLAY_NAMES.get(offer.provider, offer.provider),
        gpu_type=offer.gpu_type.value,
        price_per_hour=offer.price_per_hour,
        offer_id=offer.offer_id,
        gpu_count=offer.gpu_count,
        gpu_memory_gb=offer.gpu_memory_gb,
        is_spot=offer.is_spot,
        region=offer.region or "",
        datacenter=offer.datacenter or "",
        offer_count=offer_count,
        sampled_at=sampled_at or datetime.now(timezone.utc).isoformat(),
    )


def estimate_md_performance_ns_day(
    system_atoms: int,
    *,
    optimizations: Optional[Iterable[str]] = None,
    benchmark_ns_day: Optional[float] = None,
    gpu_type: GPUType | str | None = None,
) -> float:
    if benchmark_ns_day and benchmark_ns_day > 0:
        return float(benchmark_ns_day)
    empirical = get_empirical_md_benchmark(system_atoms, gpu_type=gpu_type)
    if empirical is not None:
        return float(empirical["ns_day"])
    atoms = max(int(system_atoms or 0), 1)
    opts = {str(opt).strip().upper() for opt in (optimizations or [])}
    base_performance = 350.0 * (100000.0 / atoms)
    if "HMR" in opts:
        base_performance *= 2.0
    if "PME_GPU" in opts:
        base_performance *= 1.15
    if "MIXED_PRECISION" in opts:
        base_performance *= 1.05
    return max(base_performance, 1e-6)


def resolve_provider_prices(
    *,
    explicit_provider_prices: Optional[Mapping[str, float]] = None,
    fallback_provider_prices: Optional[Mapping[str, float]] = None,
    live_price_snapshots: Optional[Mapping[str, ProviderPriceSnapshot]] = None,
) -> tuple[Dict[str, float], Dict[str, str], Dict[str, Dict[str, Any]]]:
    prices: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    snapshots: Dict[str, Dict[str, Any]] = {}

    for provider, price in (fallback_provider_prices or {}).items():
        prices[str(provider)] = float(price)
        sources[str(provider)] = "fallback"

    for snapshot in (live_price_snapshots or {}).values():
        prices[snapshot.provider_name] = float(snapshot.price_per_hour)
        sources[snapshot.provider_name] = snapshot.source
        snapshots[snapshot.provider_name] = snapshot.to_dict()

    for provider, price in (explicit_provider_prices or {}).items():
        prices[str(provider)] = float(price)
        sources[str(provider)] = "explicit"

    return prices, sources, snapshots


def compute_dcem_scores(
    *,
    provider_prices: Mapping[str, float],
    system_atoms: int,
    optimizations: Optional[Iterable[str]] = None,
    pue: float = 1.0,
    benchmark_ns_day: Optional[float] = None,
    provider_benchmark_ns_day: Optional[Mapping[str, float]] = None,
    price_sources: Optional[Mapping[str, str]] = None,
    gpu_type: GPUType | str | None = None,
) -> Dict[str, Any]:
    if not provider_prices:
        raise ValueError("provider_prices must not be empty")

    benchmark_map = {str(k): float(v) for k, v in (provider_benchmark_ns_day or {}).items()}

    def _lookup_provider_benchmark(name: str) -> Optional[float]:
        if name in benchmark_map:
            return benchmark_map[name]
        lowered = name.lower()
        for key, value in benchmark_map.items():
            if key.lower() == lowered:
                return value
        aliases = {
            "Vast.ai": ["vast", "vast.ai"],
            "RunPod": ["runpod", "runpod_pods"],
            "GCP": ["gcp", "google cloud"],
        }
        for alias in aliases.get(name, []):
            if alias in benchmark_map:
                return benchmark_map[alias]
            for key, value in benchmark_map.items():
                if key.lower() == alias.lower():
                    return value
        return None

    empirical_benchmark = None if benchmark_ns_day else get_empirical_md_benchmark(system_atoms, gpu_type=gpu_type)
    base_performance = estimate_md_performance_ns_day(
        system_atoms,
        optimizations=optimizations,
        benchmark_ns_day=benchmark_ns_day,
        gpu_type=gpu_type,
    )
    base_performance_source = (
        "benchmark"
        if benchmark_ns_day
        else (str(empirical_benchmark["source"]) if empirical_benchmark else "fallback_model")
    )
    pue = float(pue or 1.0)
    dcem_scores: Dict[str, float] = {}
    provider_details: Dict[str, Dict[str, Any]] = {}

    for provider, price_hr in provider_prices.items():
        provider_perf = _lookup_provider_benchmark(provider)
        perf = float(provider_perf or base_performance)
        dcem = (float(price_hr) * pue) / (perf / 24.0)
        dcem_scores[provider] = dcem
        provider_details[provider] = {
            "price_per_hour": float(price_hr),
            "predicted_performance_ns_day": perf,
            "dcem_score": dcem,
            "price_source": (price_sources or {}).get(provider, "unknown"),
            "performance_source": "provider_benchmark" if provider_perf else base_performance_source,
        }

    best_provider = min(dcem_scores, key=dcem_scores.get)
    result = {
        "best_provider": best_provider,
        "selected_provider": best_provider,
        "best_dcem_score": dcem_scores[best_provider],
        "cost_per_hour": provider_details[best_provider]["price_per_hour"],
        "dcem_scores": dcem_scores,
        "provider_details": provider_details,
        "predicted_performance_ns_day": provider_details[best_provider]["predicted_performance_ns_day"],
        "base_performance_ns_day": base_performance,
        "pue_applied": pue,
        "price_source": provider_details[best_provider]["price_source"],
        "performance_source": provider_details[best_provider]["performance_source"],
    }
    if empirical_benchmark is not None:
        result["empirical_benchmark"] = empirical_benchmark
    return result

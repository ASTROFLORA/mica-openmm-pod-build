"""Ligero registro de métricas en memoria (placeholders Prometheus) - Copilot B.

Soporta:
  - Counters: inc(name, value=1, **labels)
  - Histograms (min/max/count/sum)

Se evita dependencia externa; permite exponer /api/metrics.
"""
from __future__ import annotations

import threading
import time
import os
from collections import defaultdict
from typing import Dict, Any, Tuple


class _MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = defaultdict(float)
        self.histograms: Dict[str, Dict[str, float]] = {}
        self.flags_snapshot: Dict[str, Any] = {}
        # optional raw samples for select histograms (rolling window)
        self._histogram_samples: Dict[str, list] = {}
        self._sample_max = int(os.getenv('METRICS_HISTOGRAM_SAMPLE_MAX', '500'))
        default_sampled = {'fusion_efficiency_ratio','fusion_pca_variance_explained'}
        extra = os.getenv('METRICS_SAMPLE_HISTOGRAMS','')
        if extra:
            default_sampled.update({x.strip() for x in extra.split(',') if x.strip()})
        self._sampled_names = default_sampled

    def inc(self, name: str, value: float = 1.0, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] += value

    # Added Sprint 4: explicit additive method for clarity in KPI accumulation
    def inc_by(self, name: str, value: float, **labels):  # type: ignore
        self.inc(name, value=value, **labels)

    def observe_histogram(self, name: str, value: float):
        with self._lock:
            h = self.histograms.setdefault(name, {"count": 0, "sum": 0.0, "min": value, "max": value})
            h["count"] += 1
            h["sum"] += value
            if value < h["min"]:
                h["min"] = value
            if value > h["max"]:
                h["max"] = value
            if name in getattr(self, '_sampled_names', set()):
                samples = self._histogram_samples.setdefault(name, [])
                samples.append(value)
                if len(samples) > self._sample_max:
                    # drop oldest half to keep amortized O(1)
                    del samples[: len(samples)//2]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters_out = {}
            for (name, labels), val in self.counters.items():
                label_key = ",".join(f"{k}={v}" for k, v in labels) if labels else "_"
                counters_out.setdefault(name, {})[label_key] = val
            # shallow copy of samples (avoid heavy clone)
            samples_out = {k: v[-50:] for k, v in self._histogram_samples.items()}  # last 50 for debugging
            return {
                "counters": counters_out,
                "histograms": self.histograms.copy(),
                "histogram_samples": samples_out,
                "flags": self.flags_snapshot.copy(),
                "timestamp": time.time(),
            }

    def set_flags(self, flags: Dict[str, Any]):
        with self._lock:
            self.flags_snapshot = flags


metrics = _MetricsRegistry()

__all__ = ["metrics"]

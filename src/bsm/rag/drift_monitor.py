"""Drift monitoring stubs for tests."""
from __future__ import annotations

from typing import List


class DriftReference:
	def __init__(self):
		self._vectors: list[list[float]] = []
	def fit(self, vecs: List[list[float]]):  # type: ignore
		self._vectors.extend(vecs)
	def score(self, vec: list[float]) -> float:
		# Trivial placeholder drift score
		return float(len(vec))


__all__ = ["DriftReference"]
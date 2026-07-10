"""
SMIC Module Dependency Graph and Topological Sorting.

Encodes the known dependency relationships between SMIC analysis modules
so that bundle execution respects inter-module data dependencies.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List

# Edges: key depends on values (must run AFTER them)
SMIC_DEPENDENCY_GRAPH: Dict[str, List[str]] = {
    "rmsd": [],
    "rmsd_pairwise": ["rmsd"],
    "clustering": ["rmsd"],
    "pca": ["rmsd"],
    "tica": ["pca"],
    "binding": [],
    "contacts": [],
    "contact_density": ["contacts"],
    "convergence": [],
    "dccm": [],
    "dssp": [],
    "ifp": [],
    "interactions": [],
    "interactions_general": [],
    "interactions_plip": [],
    "network": ["contacts", "dccm"],
    "prs": ["dccm", "network"],
    "water": [],
    "pocket_volume": ["pocket_detection"],
    "pocket_detection": [],
    "allosteric_pathways": ["dccm", "network", "prs"],
}


def topological_sort(requested: List[str]) -> List[str]:
    """Return *requested* modules in dependency-safe order (Kahn's algorithm).

    Only the subgraph induced by *requested* is considered.
    Modules not in ``SMIC_DEPENDENCY_GRAPH`` are appended at the end.
    """
    requested_set = {a.lower().strip() for a in requested}

    # Build in-degree map restricted to requested modules
    in_degree: Dict[str, int] = {m: 0 for m in requested_set}
    adj: Dict[str, List[str]] = {m: [] for m in requested_set}

    for module in requested_set:
        deps = SMIC_DEPENDENCY_GRAPH.get(module, [])
        for dep in deps:
            if dep in requested_set:
                adj[dep].append(module)
                in_degree[module] += 1

    queue: deque[str] = deque(m for m, d in in_degree.items() if d == 0)
    result: List[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for neighbor in adj.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # Append any modules not in the graph (unknown deps)
    remaining = [m for m in requested_set if m not in result]
    result.extend(remaining)

    return result

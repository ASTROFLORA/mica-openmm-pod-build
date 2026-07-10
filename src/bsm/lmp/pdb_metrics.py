"""
Minimal PDB structural metrics for LMP v4 (MVP).

Best-effort metrics using MDAnalysis (optional dependency):
- Per-chain center of mass (COM)
- Per-chain radius of gyration (Rg)
- Per-chain bounding box
- Pairwise COM distances between chains
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple


def compute_chain_geometry_metrics(pdb_path: Path) -> Dict[str, Any]:
    """Compute basic per-chain geometry metrics from a PDB file.

    Returns:
        {
          "chains": {
             "A": {
               "n_atoms": 1234,
               "com": [x, y, z],
               "rg": 21.3,
               "bbox": {"min": [...], "max": [...]}
             },
             ...
          },
          "com_distances": [
             {"chain_a": "A", "chain_b": "B", "distance": 12.4},
             ...
          ]
        }
    """
    try:
        import MDAnalysis as mda  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return {}

    pdb_path = Path(pdb_path)
    if not pdb_path.exists():
        return {}

    u = mda.Universe(str(pdb_path))
    result: Dict[str, Any] = {"chains": {}, "com_distances": []}

    # Collect chain IDs from protein atoms only.
    protein = u.select_atoms("protein")
    if len(protein) == 0:
        return {}

    chain_ids: List[str] = []
    try:
        chain_ids = sorted({str(x).strip() for x in getattr(protein, "chainIDs") if str(x).strip()})
    except Exception:
        chain_ids = []
    if not chain_ids:
        try:
            chain_ids = sorted({str(x).strip() for x in getattr(protein, "segids") if str(x).strip()})
        except Exception:
            chain_ids = []

    def _select_chain(cid: str):
        for kw in ("chainid", "segid"):
            try:
                sel = u.select_atoms(f"protein and {kw} {cid}")
                if len(sel) > 0:
                    return sel
            except Exception:
                continue
        return None

    com_map: Dict[str, Tuple[float, float, float]] = {}

    for cid in chain_ids:
        sel = _select_chain(cid)
        if sel is None or len(sel) == 0:
            continue
        try:
            com = sel.center_of_mass()
            rg = float(sel.radius_of_gyration())
            pos = np.asarray(sel.positions, dtype=float)
            bbox_min = pos.min(axis=0)
            bbox_max = pos.max(axis=0)
        except Exception:
            continue

        com_list = [round(float(x), 3) for x in com]
        bbox_min_list = [round(float(x), 3) for x in bbox_min]
        bbox_max_list = [round(float(x), 3) for x in bbox_max]

        result["chains"][cid] = {
            "n_atoms": int(len(sel)),
            "com": com_list,
            "rg": round(float(rg), 3),
            "bbox": {"min": bbox_min_list, "max": bbox_max_list},
        }
        com_map[cid] = (com_list[0], com_list[1], com_list[2])

    # Pairwise COM distances
    cids = sorted(com_map.keys())
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            a = cids[i]
            b = cids[j]
            ax, ay, az = com_map[a]
            bx, by, bz = com_map[b]
            try:
                dist = float(((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5)
            except Exception:
                continue
            result["com_distances"].append({
                "chain_a": a,
                "chain_b": b,
                "distance": round(dist, 3),
            })

    return result
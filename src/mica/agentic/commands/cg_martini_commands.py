"""cg_martini_commands.py — Command Kernel nodes for the CG/Martini lane.

Authority:
  Lane I (Command Kernel) — exposes Lane CG/Martini as kernel-executable nodes
  Lane CG/Martini          — SLICES CG-P0.2 / P0.3 / P0.4 / P0.5 / P1.1

Scope:
  Six canonical command nodes that any protocol (P5/P6) can submit:

    1. cg.martinize2.map       — AA → CG (real vermouth 0.15.x martinize2 binary)
    2. cg.insane.build         — membrane + solvate + ions (real insane 1.2.0)
    3. cg.gauntlet.clcn7       — full E2E chain (martinize2 → INSANE → audit)
    4. cg.preprocess.topology  — P0.3 topology preprocessor
    5. cg.audit.geometry       — P0.4 geometry audit
    6. cg.remediate.overlap    — P0.4 overlap remediation

  Plus one read-only diagnostic:
    7. cg.railway.readiness    — verify all stack components are importable
                                 + martinize2 CLI on PATH

Receipts:
  Every command returns a dict with at least `status` and `receipt_id` (when
  a ReceiptCore was emitted). ReceiptCore itself is returned inside the
  `result` envelope so the protocol layer can persist it for receipts.jsonl
  / graphrag ingestion.

Doctrina:
  D1: todo receipt hereda ReceiptCore (receipts.py:37)
  D2: no edge input→output sin MUDODependencyEdge real
  D3: builder_policy por geometry_class (en INSANEAdapter, ya cerrado)
  D4: failure_domain = lane (cg_martini)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

from mica.provenance.receipts import ReceiptCore

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _envelope_to_dict(result: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a sub-result dict into the kernel envelope shape.

    Every command returns:
        {
            "summary": "<one-liner>",
            "result":  {...sub-result...},
            "route_authority": "command_kernel",
            "route_backed":    True,
        }
    """
    return {
        "summary": str(result.get("status", "completed")),
        "result": dict(result),
        "route_authority": "command_kernel",
        "route_backed": True,
    }


def _validate_required(args: Dict[str, Any], *keys: str) -> Optional[str]:
    """Return None if all required keys are present, else an error message."""
    missing = [k for k in keys if not args.get(k)]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}"
    return None


def _receipt_to_status_block(receipt: ReceiptCore) -> Dict[str, Any]:
    """Extract a compact status block from a ReceiptCore."""
    payload = receipt.payload or {}
    if not isinstance(payload, dict):
        payload = payload.model_dump() if hasattr(payload, "model_dump") else {}
    return {
        "status": receipt.status,
        "receipt_id": receipt.receipt_id,
        "kind": receipt.kind,
        "operation_name": receipt.operation_name,
        "started_at": receipt.started_at,
        "ended_at": receipt.ended_at,
        "validation_errors": payload.get("validation_errors", []),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Node 1: cg.martinize2.map
# ═══════════════════════════════════════════════════════════════════════════


async def cg_martinize2_map(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Map an AA structure to Martini 3 CG via real martinize2.

    Args (via envelope.arguments):
        input_structure_ref: path to input .pdb/.gro
        output_dir:          output directory
        ss_policy:           "dssp" | "provided" | "custom" | "none" (default "dssp")
        en_policy:           "elnedyn" | "go_martini" | "none" (default "elnedyn")
        maxwarn:             int (default 200)
    """
    err = _validate_required(args, "input_structure_ref", "output_dir")
    if err:
        raise RuntimeError(f"cg.martinize2.map: {err}")
    if not os.path.isfile(args["input_structure_ref"]):
        raise RuntimeError(
            f"cg.martinize2.map: input_structure_ref not found: "
            f"{args['input_structure_ref']}"
        )

    from mica.sim.cg_martini.martinize2_adapter import Martinize2Adapter

    adapter = Martinize2Adapter()
    receipt = adapter.map_protein(
        input_structure_ref=args["input_structure_ref"],
        output_dir=args["output_dir"],
        ss_policy=args.get("ss_policy", "dssp"),
        en_policy=args.get("en_policy", "elnedyn"),
        maxwarn=int(args.get("maxwarn", 200)),
    )

    payload = receipt.payload or {}
    return _envelope_to_dict({
        "status": receipt.status,
        "bead_count": payload.get("bead_count_output", 0),
        "residue_count": payload.get("residue_count_input", 0),
        "output_cg_gro_ref": payload.get("output_cg_gro_ref", ""),
        "output_cg_itp_ref": payload.get("output_cg_itp_ref", ""),
        "validation_errors": payload.get("validation_errors", []),
        **_receipt_to_status_block(receipt),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Node 2: cg.insane.build
# ═══════════════════════════════════════════════════════════════════════════


async def cg_insane_build(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Build a CG POPC membrane around a protein via real INSANE 1.2.0.

    Args (via envelope.arguments):
        protein_gro_ref:     path to protein .gro (from cg.martinize2.map)
        output_dir:          output directory
        lipid_composition:   default "POPC:1"
        solvent:             default "PW"
        salt_concentration:  default 0.15
        center_protein:      default True
    """
    err = _validate_required(args, "protein_gro_ref", "output_dir")
    if err:
        raise RuntimeError(f"cg.insane.build: {err}")
    if not os.path.isfile(args["protein_gro_ref"]):
        raise RuntimeError(
            f"cg.insane.build: protein_gro_ref not found: {args['protein_gro_ref']}"
        )

    from mica.sim.cg_martini.insane_adapter import INSANEAdapter

    adapter = INSANEAdapter()
    # Preflight (don't block on failure — build will surface the error)
    pf = adapter.preflight(
        builder="insane",
        builder_version="1.2.0",
        geometry_class="flat_bilayer",
        protein_gro_ref=args["protein_gro_ref"],
    )
    if pf.status != "passed":
        return _envelope_to_dict({
            "status": "failed",
            "stage": "preflight",
            **_receipt_to_status_block(pf),
        })

    receipt = adapter.build(
        protein_gro_ref=args["protein_gro_ref"],
        output_dir=args["output_dir"],
        lipid_composition=args.get("lipid_composition", "POPC:1"),
        solvent=args.get("solvent", "PW"),
        salt_concentration=float(args.get("salt_concentration", 0.15)),
        center_protein=bool(args.get("center_protein", True)),
    )

    payload = receipt.payload or {}
    return _envelope_to_dict({
        "status": receipt.status,
        "counts": payload.get("counts", {}),
        "box_nm": payload.get("box_nm", {}),
        "output_membrane_gro_ref": payload.get("outputs", {}).get("gro_ref", ""),
        "output_membrane_top_ref": payload.get("outputs", {}).get("top_ref", ""),
        "validation_errors": payload.get("validation_errors", []),
        **_receipt_to_status_block(receipt),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Node 3: cg.gauntlet.clcn7 — full E2E chain
# ═══════════════════════════════════════════════════════════════════════════


async def cg_gauntlet_clcn7(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Run the CLCN7 E2E gauntlet: martinize2 → INSANE → audit.

    Args (via envelope.arguments):
        input_pdb:        path to AA input .pdb
        work_dir:         working directory
        membrane_builder: "insane" (default) | "ts2cg" (legacy mock)
    """
    err = _validate_required(args, "input_pdb", "work_dir")
    if err:
        raise RuntimeError(f"cg.gauntlet.clcn7: {err}")
    if not os.path.isfile(args["input_pdb"]):
        raise RuntimeError(
            f"cg.gauntlet.clcn7: input_pdb not found: {args['input_pdb']}"
        )

    # Resolve the martinize2 binary via the same env-var chain as the adapter
    m2_binary = os.environ.get("MICA_MARTINIZE2_BINARY") or shutil.which("martinize2") or "martinize2"

    from mica.sim.cg_martini.gauntlet_clcn7 import CLCN7Gauntlet

    gauntlet = CLCN7Gauntlet(
        martinize2_binary=m2_binary,
        membrane_builder=args.get("membrane_builder", "insane"),
        input_pdb=args["input_pdb"],
    )
    receipt = gauntlet.run(work_dir=args["work_dir"])

    payload = receipt.payload or {}
    return _envelope_to_dict({
        "decision": payload.get("decision", "unknown"),
        "nodes_executed": payload.get("nodes_executed", []),
        "contracts_promoted": payload.get("contracts_promoted", []),
        "contracts_still_draft": payload.get("contracts_still_draft", []),
        "manual_interventions": payload.get("manual_interventions", []),
        "receipts_count": len(gauntlet.receipts or []),
        **_receipt_to_status_block(receipt),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Node 4: cg.preprocess.topology
# ═══════════════════════════════════════════════════════════════════════════


async def cg_preprocess_topology(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Preprocess a CG topology file. P0.3 of the CG/Martini lane.

    Args (via envelope.arguments):
        input_topology:  path to input .top/.itp
        output_topology: path to output .top
    """
    err = _validate_required(args, "input_topology", "output_topology")
    if err:
        raise RuntimeError(f"cg.preprocess.topology: {err}")
    if not os.path.isfile(args["input_topology"]):
        raise RuntimeError(
            f"cg.preprocess.topology: input_topology not found: {args['input_topology']}"
        )

    from mica.sim.cg_martini.topology_preprocessor import TopologyPreprocessor

    pre = TopologyPreprocessor(workspace_id="cg_martini")
    receipt = pre.preprocess(args["input_topology"], args["output_topology"])
    return _envelope_to_dict({
        **_receipt_to_status_block(receipt),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Node 5: cg.audit.geometry
# ═══════════════════════════════════════════════════════════════════════════


async def cg_audit_geometry(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Run geometry audit on a CG system. P0.4 of the CG/Martini lane.

    Args (via envelope.arguments):
        input_gro:       path to input .gro
        input_topology: path to input .top
    """
    err = _validate_required(args, "input_gro", "input_topology")
    if err:
        raise RuntimeError(f"cg.audit.geometry: {err}")
    if not os.path.isfile(args["input_gro"]):
        raise RuntimeError(
            f"cg.audit.geometry: input_gro not found: {args['input_gro']}"
        )

    from mica.sim.cg_martini.geometry_audit import GeometryAudit

    audit = GeometryAudit(workspace_id="cg_martini")
    receipt = audit.audit(args["input_gro"], args["input_topology"])
    payload = receipt.payload or {}
    return _envelope_to_dict({
        "validation_errors": payload.get("validation_errors", []),
        **_receipt_to_status_block(receipt),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Node 6: cg.remediate.overlap
# ═══════════════════════════════════════════════════════════════════════════


async def cg_remediate_overlap(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Run overlap remediation on a CG system. P0.4 of the CG/Martini lane.

    Args (via envelope.arguments):
        input_gro:      path to input .gro
        output_gro:     path to output .gro
        max_iterations: int (default 1000)
    """
    err = _validate_required(args, "input_gro", "output_gro")
    if err:
        raise RuntimeError(f"cg.remediate.overlap: {err}")
    if not os.path.isfile(args["input_gro"]):
        raise RuntimeError(
            f"cg.remediate.overlap: input_gro not found: {args['input_gro']}"
        )

    from mica.sim.cg_martini.overlap_remediation import OverlapRemediation

    rem = OverlapRemediation(workspace_id="cg_martini")
    receipt = rem.remediate(
        args["input_gro"],
        args["output_gro"],
        max_iterations=int(args.get("max_iterations", 1000)),
    )
    payload = receipt.payload or {}
    return _envelope_to_dict({
        "iterations_run": payload.get("iterations_run", 0),
        "remaining_overlaps": payload.get("remaining_overlaps", 0),
        **_receipt_to_status_block(receipt),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Node 7: cg.railway.readiness — read-only diagnostic
# ═══════════════════════════════════════════════════════════════════════════


async def cg_railway_readiness(
    kernel: Any,
    args: Dict[str, Any],
    envelope: Any,
) -> Dict[str, Any]:
    """Verify the CG/Martini stack is Railway-ready.

    Checks:
      1. vermouth importable
      2. insane importable
      3. mdtraj importable
      4. biopython importable
      5. numpy importable
      6. scipy importable
      7. martinize2 CLI on PATH
      8. martinize2 --version exits 0
      9. Martinize2Adapter + INSANEAdapter construct without error
    """
    checks: list[Dict[str, Any]] = []

    def import_ok(name: str, import_as: Optional[str] = None) -> Dict[str, Any]:
        try:
            mod = __import__(import_as or name)
            ver = getattr(mod, "__version__", "unknown")
            return {"name": name, "ok": True, "detail": f"version {ver}"}
        except ImportError as e:
            return {"name": name, "ok": False, "detail": str(e)}

    checks.append(import_ok("vermouth"))
    checks.append(import_ok("insane"))
    checks.append(import_ok("mdtraj"))
    checks.append(import_ok("biopython", "Bio"))
    checks.append(import_ok("numpy"))
    checks.append(import_ok("scipy"))

    # martinize2 CLI on PATH
    which = shutil.which("martinize2")
    if which:
        checks.append({"name": "martinize2 CLI on PATH", "ok": True, "detail": which})
        # martinize2 --version
        try:
            r = subprocess.run(
                [which, "--version"], capture_output=True, text=True, timeout=60,
            )
            ok = r.returncode == 0
            ver_line = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
            checks.append({"name": "martinize2 --version exits 0", "ok": ok, "detail": ver_line})
        except Exception as e:
            checks.append({"name": "martinize2 --version exits 0", "ok": False, "detail": repr(e)})
    else:
        checks.append({
            "name": "martinize2 CLI on PATH", "ok": False,
            "detail": "not on PATH (vermouth install did not register the script)",
        })
        checks.append({
            "name": "martinize2 --version exits 0", "ok": False,
            "detail": "no binary to test",
        })

    # Adapters construct
    try:
        from mica.sim.cg_martini.martinize2_adapter import Martinize2Adapter
        from mica.sim.cg_martini.insane_adapter import INSANEAdapter
        m2 = Martinize2Adapter()
        ia = INSANEAdapter()
        if not ia._insane_available:
            checks.append({
                "name": "INSANEAdapter construct", "ok": False,
                "detail": f"insane not importable: {ia._insane_import_error}",
            })
        else:
            checks.append({
                "name": "INSANEAdapter construct", "ok": True,
                "detail": f"martinize2_binary={m2.martinize2_binary}",
            })
    except Exception as e:
        checks.append({
            "name": "INSANEAdapter construct", "ok": False,
            "detail": repr(e),
        })

    all_ok = all(c["ok"] for c in checks)
    return _envelope_to_dict({
        "all_ok": all_ok,
        "checks": checks,
    })

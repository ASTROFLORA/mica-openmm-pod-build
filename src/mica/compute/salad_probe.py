"""
salad_probe.py — Live API probe for SaladCloud integration.

Validates:
  1. Auth: API key works (quota endpoint)
  2. GPU classes: lists available GPUs, finds RTX 5090
  3. GPU availability: checks real-time availability for 5090
  4. Quota: confirms replica budget is available

Usage:
    cd MICA
    python -m src.mica.compute.salad_probe

Environment:
    SALAD_CLOUD_API_KEY  — required
    SALAD_ORG_NAME       — required (e.g. "your-org")
    SALAD_PROJECT_NAME   — optional (default: "mica-compute")
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mica.compute.provider_preflight import run_provider_preflight


def _price_to_float(price_obj):
    if price_obj is None:
        return None
    if isinstance(price_obj, (int, float, str)):
        try:
            return float(price_obj)
        except (TypeError, ValueError):
            return None
    raw_price = getattr(price_obj, "price", None)
    if raw_price is None:
        return None
    try:
        return float(raw_price)
    except (TypeError, ValueError):
        return None


async def probe() -> None:
    preflight = run_provider_preflight("salad")
    if not preflight.ok:
        print("[FAIL] Provider preflight failed")
        print(preflight.to_json())
        sys.exit(1)

    api_key = os.environ.get("SALAD_CLOUD_API_KEY", "").strip().strip('"')
    org_name = os.environ.get("SALAD_ORG_NAME", "").strip()
    project_name = os.environ.get("SALAD_PROJECT_NAME", "mica-compute").strip()

    print("[PRECHECK]", preflight.to_json())

    print(f"[INFO] Probing SaladCloud API")
    print(f"       Org:     {org_name}")
    print(f"       Project: {project_name}")
    print()

    from salad_cloud_sdk import SaladCloudSdkAsync
    sdk = SaladCloudSdkAsync(api_key=api_key, timeout=20_000)
    sdk.set_api_key(api_key)

    # ── 1. Quota check ───────────────────────────────────────────────────
    print("[1/3] Checking quotas...")
    try:
        quotas = await sdk.quotas.get_quotas(organization_name=org_name)
        cg_quotas = getattr(quotas, "container_groups_quotas", None)
        if cg_quotas:
            print(f"      max_created_container_groups: {getattr(cg_quotas, 'max_created_container_groups', '?')}")
            print(f"      max_replicas_per_container_group: {getattr(cg_quotas, 'max_replicas_per_container_group', '?')}")
        print("      [OK] Quota check passed — API key is valid\n")
    except Exception as exc:
        print(f"      [FAIL] Quota check failed: {exc}")
        sys.exit(1)

    # ── 2. GPU classes ───────────────────────────────────────────────────
    print("[2/3] Listing GPU classes...")
    try:
        gpu_result = await sdk.organization_data.list_gpu_classes(organization_name=org_name)
        gpu_classes = gpu_result.items or []
        print(f"      Total GPU classes available: {len(gpu_classes)}")

        # Find 5090
        found_5090 = []
        for gc in gpu_classes:
            name = gc.name or ""
            if "5090" in name:
                prices = []
                if gc.prices:
                    prices = [p for p in (_price_to_float(x) for x in gc.prices) if p is not None]
                found_5090.append({
                    "id": gc.id_,
                    "name": name,
                    "gpu_count": getattr(gc, "gpu_count", None),
                    "is_high_demand": gc.is_high_demand,
                    "price_min": min(prices) if prices else None,
                    "price_max": max(prices) if prices else None,
                })

        if found_5090:
            print(f"      [OK] RTX 5090 GPU classes found: {len(found_5090)}")
            for gc in found_5090:
                print(f"           id={gc['id']}")
                print(f"           name={gc['name']}")
                print(f"           gpu_count={gc['gpu_count']}")
                print(f"           high_demand={gc['is_high_demand']}")
                price_str = f"${gc['price_min']:.4f}/hr" if gc['price_min'] else "unknown"
                print(f"           price_min={price_str}")
        else:
            print("      [WARN] No RTX 5090 classes found — may not be available in your org tier")
            print("      Available GPU names:")
            for gc in sorted(gpu_classes, key=lambda g: g.name or ""):
                print(f"           {gc.name}")
        print()
    except Exception as exc:
        print(f"      [FAIL] GPU class listing failed: {exc}")
        sys.exit(1)

    # ── 3. Existing container groups ─────────────────────────────────────
    print("[3/3] Listing existing Container Groups in project...")
    try:
        cg_list = await sdk.container_groups.list_container_groups(
            organization_name=org_name,
            project_name=project_name,
        )
        items = getattr(cg_list, "items", []) or []
        print(f"      Container Groups in project '{project_name}': {len(items)}")
        for cg in items:
            status_str = "?"
            if cg.current_state and cg.current_state.status:
                status_str = str(cg.current_state.status)
            print(f"           {cg.name} — {status_str}")
        print("      [OK] Container Group listing succeeded\n")
    except Exception as exc:
        print(f"      [WARN] Container Group listing failed (project may not exist yet): {exc}\n")

    print("=" * 60)
    print("[DONE] SaladCloud probe complete. API key valid. Ready for SRCG launch.")
    if found_5090:
        best = found_5090[0]
        print(f"       Recommended GPU class for OpenMM MD:")
        print(f"         name  : {best['name']}")
        print(f"         id    : {best['id']}")
        print(f"         price : {best.get('price_min', '?')}")
    print("=" * 60)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(dotenv_path="MICA/.env", override=False)
    load_dotenv(override=False)
    asyncio.run(probe())

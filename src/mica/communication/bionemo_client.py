"""BioNeMo client (placeholder) for Evo2 40B, DiffDock, and Embedding models.

This lightweight module standardizes call signatures so workers can wrap
NVIDIA BioNeMo (or NIM) endpoints. Real HTTP integration can replace the
placeholders by implementing the `_post_json` helper.

Env vars (expected):
  BIONEMO_API_KEY            -> API key (Bearer)
  BIONEMO_BASE_URL           -> Base URL (e.g. https://api.nvidia.com/v1)
  BIONEMO_EVO2_MODEL         -> (optional) default evo2 model name (default: evo2-40b)
  BIONEMO_DIFFDOCK_MODEL     -> (optional) diffdock variant (default: bionemo-diffdock)
  BIONEMO_EMBED_MISTRAL_MODEL-> (optional) default high-dim embedding model (default: nv-embedqa-mistral-7b-v2)
  BIONEMO_EMBED_E5_MODEL     -> (optional) default low-dim embedding model (default: nv-embedqa-e5-v5)

Returns dicts with common fields: ok, provider, latency_s, and either
answer / embedding / error / pose.
"""
from __future__ import annotations
import os, time, random
from typing import Dict, Any, List

try:  # optional dependency
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def _missing(reason: str, provider: str):
    return {"ok": False, "error": reason, "provider": provider}


def _post_json(path: str, payload: Dict[str, Any], provider: str) -> Dict[str, Any]:
    base = os.getenv("BIONEMO_BASE_URL")
    key = os.getenv("BIONEMO_API_KEY") or os.getenv("NEMOTRON_API_KEY")  # fallback
    if not base or not key or requests is None:
        return _missing("missing_credentials", provider)
    url = base.rstrip("/") + path
    try:
        t0 = time.time()
        r = requests.post(url, json=payload, timeout=90, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        lat = round(time.time() - t0, 3)
        if r.status_code >= 400:
            return {"ok": False, "error": f"http_{r.status_code}", "provider": provider, "latency_s": lat}
        data = r.json()
        data.setdefault("provider", provider)
        data.setdefault("latency_s", lat)
        data.setdefault("ok", True)
        return data
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "provider": provider}


# --- Evo2 (sequence reasoning / generation) ---
def call_bionemo_evo2(prompt: str, *, system: str = "You are a bio reasoning assistant.") -> Dict[str, Any]:
    model = os.getenv("BIONEMO_EVO2_MODEL", "evo2-40b")
    provider = "bionemo_evo2"
    if not os.getenv("BIONEMO_BASE_URL"):
        # Offline placeholder
        t0 = time.time(); time.sleep(0.01)
        return {"ok": True, "answer": f"[offline-evo2:{model}] {prompt[:80]}", "provider": provider, "latency_s": round(time.time()-t0,3), "offline": True}
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "temperature": 0.2}
    return _post_json("/bionemo/evo2/chat", payload, provider)


# --- DiffDock (pose prediction) ---
def call_bionemo_diffdock(*, protein_pdb: str | None, ligand_smiles: str | None) -> Dict[str, Any]:
    provider = "bionemo_diffdock"
    if not protein_pdb or not ligand_smiles:
        return {"ok": False, "error": "missing_inputs", "provider": provider, "needs": ["protein_pdb", "ligand_smiles"]}
    model = os.getenv("BIONEMO_DIFFDOCK_MODEL", "bionemo-diffdock")
    if not os.getenv("BIONEMO_BASE_URL"):
        # Deterministic-ish offline stub
        t0 = time.time(); random.seed(hash(ligand_smiles) % (2**32))
        pose_id = f"pose_{abs(hash(ligand_smiles)) % 10_000}"; score = round(random.uniform(-10, -6), 3)
        return {"ok": True, "provider": provider, "model": model, "pose_id": pose_id, "dock_score": score, "latency_s": round(time.time()-t0,3), "offline": True}
    payload = {"model": model, "protein_pdb": protein_pdb, "ligand_smiles": ligand_smiles, "num_poses": 1}
    return _post_json("/bionemo/diffdock/predict", payload, provider)


# --- Embeddings (two models) ---
def call_bionemo_embedding(texts: List[str], *, model: str) -> Dict[str, Any]:
    provider = f"bionemo_embed_{model}".replace('-', '_')
    if not texts:
        return {"ok": False, "error": "empty_texts", "provider": provider}
    if not os.getenv("BIONEMO_BASE_URL"):
        # Offline synthetic embedding (hash-based stable) preserving expected dim
        dim = 4096 if "mistral" in model else 1024
        t0 = time.time()
        out_vecs: List[List[float]] = []
        for t in texts:
            seed = abs(hash((model, t))) % (2**32)
            rnd = random.Random(seed)
            out_vecs.append([rnd.uniform(-0.5, 0.5) for _ in range(dim)])
        return {"ok": True, "provider": provider, "model": model, "embeddings": out_vecs, "dim": dim, "count": len(out_vecs), "latency_s": round(time.time()-t0,3), "offline": True}
    payload = {"model": model, "inputs": texts}
    return _post_json("/bionemo/embeddings", payload, provider)


def detect_bionemo_services() -> Dict[str, bool]:
    services: Dict[str, bool] = {}
    if os.getenv("BIONEMO_BASE_URL") and (os.getenv("BIONEMO_API_KEY") or os.getenv("NEMOTRON_API_KEY")):
        services["evo2"] = True
        services["diffdock"] = True
        services["embed_mistral"] = True
        services["embed_e5"] = True
    return services

__all__ = [
    "call_bionemo_evo2",
    "call_bionemo_diffdock",
    "call_bionemo_embedding",
    "detect_bionemo_services",
]

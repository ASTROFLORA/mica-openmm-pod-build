"""
ArtifactResolver — Multi-stage pipeline to auto-resolve missing compound/protein inputs.

Stage 0 — Intent classification: protein-only? complex? screening?
Stage 1 — Inventory: what do we have?
Stage 2 — SMILES resolution (compound name / IUPAC / CAS / CID / InChI → SMILES)
Stage 3 — PDB resolution (PDB ID / UniProt ID / gene name → .pdb file path)
Stage 4 — Decide mode: if ligand completely unresolvable but protein present → protein_only_md
Stage 5 — If protein also unresolvable → raise DriverFailureEvent

Anti-rigidity: ligand absence is NOT an error when protein-only MD makes sense.
Every degradation produces a degradation_notice in the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy dependencies — fail gracefully
# ---------------------------------------------------------------------------

try:
    from rdkit import Chem
    from rdkit.Chem import inchi as rdkit_inchi

    _RDKIT_AVAILABLE = True
except ImportError:
    _RDKIT_AVAILABLE = False
    Chem = None  # type: ignore[assignment]
    rdkit_inchi = None  # type: ignore[assignment]

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False

# Optional: DriverFailureEvent from contracts (P0-02 may not be merged yet)
try:
    from mica.drivers.contracts import DriverFailureEvent
except ImportError:
    DriverFailureEvent = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

MdMode = Literal["complex", "protein_only", "screening_only"]

_HTTP_TIMEOUT = 10  # seconds


@dataclass
class ResolverResult:
    """Full description of what the resolver was able to obtain."""

    smiles: str = ""
    protein_pdb: str = ""
    md_mode: MdMode = "complex"
    degradation_notice: str = ""
    resolution_log: list[str] = field(default_factory=list)
    quality_score: float = 1.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log(result: ResolverResult, message: str) -> None:
    result.resolution_log.append(message)
    logger.debug("[ArtifactResolver] %s", message)


def _sync_http_get(url: str, timeout: int = _HTTP_TIMEOUT) -> bytes:
    """Synchronous HTTP GET using urllib (always available)."""
    req = urllib.request.Request(url, headers={"User-Agent": "MICA-ArtifactResolver/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


async def _async_http_get(url: str, timeout: int = _HTTP_TIMEOUT) -> bytes:
    """Async HTTP GET — uses httpx if available, falls back to sync urllib in executor."""
    if _HTTPX_AVAILABLE:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    else:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_http_get, url, timeout)


def _is_4char_pdb_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{4}", value.strip()))


def _validate_smiles_rdkit(smiles: str) -> tuple[bool, str]:
    """
    Returns (is_valid, canonical_smiles_or_original).
    If RDKit not available, accepts the string as-is (degraded quality).
    """
    if not _RDKIT_AVAILABLE:
        return True, smiles  # soft-accept, no validation

    mol = Chem.MolFromSmiles(smiles)
    if mol is not None:
        canonical = Chem.MolToSmiles(mol)
        return True, canonical

    # Try sanitization rescue
    try:
        mol2 = Chem.MolFromSmiles(smiles, sanitize=False)
        if mol2 is not None:
            Chem.SanitizeMol(mol2)
            canonical = Chem.MolToSmiles(mol2)
            return True, canonical
    except Exception:
        pass

    return False, smiles


# ---------------------------------------------------------------------------
# ArtifactResolver
# ---------------------------------------------------------------------------


class ArtifactResolver:
    """
    Multi-stage resolver for SMILES and PDB inputs.

    Usage::

        result = await ArtifactResolver().resolve(context)
    """

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def resolve(self, context: dict[str, Any]) -> ResolverResult:
        result = ResolverResult()
        _log(result, "Stage 0 — Intent classification and inventory")

        # Stage 2 — SMILES
        await self._resolve_smiles(context, result)

        # Stage 3 — PDB
        await self._resolve_pdb(context, result)

        # Stage 4 — Decide mode
        self._decide_mode(result)

        # Stage 5 — Hard failure if protein also unresolvable
        if not result.protein_pdb:
            _log(result, "Stage 5 — protein unresolvable; raising DriverFailureEvent")
            self._raise_protein_failure(context, result)

        # Final quality score
        result.quality_score = self._compute_quality(result)

        _log(
            result,
            f"Resolution complete: mode={result.md_mode}, "
            f"smiles={'set' if result.smiles else 'empty'}, "
            f"pdb={'set' if result.protein_pdb else 'empty'}, "
            f"quality={result.quality_score}",
        )
        return result

    # ------------------------------------------------------------------
    # Stage 2 — SMILES resolution
    # ------------------------------------------------------------------

    async def _resolve_smiles(self, ctx: dict[str, Any], result: ResolverResult) -> None:
        _log(result, "Stage 2 — SMILES resolution")

        # 2a: literal SMILES in context
        raw = ctx.get("ligand_smiles") or ctx.get("smiles") or ""
        if raw:
            valid, canonical = _validate_smiles_rdkit(raw)
            if valid:
                result.smiles = canonical
                if not _RDKIT_AVAILABLE:
                    _log(result, "2a: SMILES accepted (RDKit unavailable — no validation)")
                    result.degradation_notice = _append_notice(
                        result.degradation_notice,
                        "RDKit not installed; SMILES passed through without structural validation.",
                    )
                else:
                    _log(result, f"2a: SMILES validated and canonicalized: {canonical[:60]}")
                return
            else:
                _log(result, f"2a: SMILES present but invalid after sanitization attempt: {raw[:60]}")

        # 2b: compound/ligand name → PubChem
        name = ctx.get("compound_name") or ctx.get("ligand_name") or ""
        if name:
            smiles = await self._fetch_smiles_by_name(name)
            if smiles:
                result.smiles = smiles
                _log(result, f"2b: SMILES fetched from PubChem by name '{name}'")
                return
            _log(result, f"2b: PubChem name lookup failed for '{name}'")

        # 2c: PubChem CID
        cid = ctx.get("pubchem_cid") or ctx.get("cid") or ""
        if cid:
            smiles = await self._fetch_smiles_by_cid(str(cid))
            if smiles:
                result.smiles = smiles
                _log(result, f"2c: SMILES fetched from PubChem CID={cid}")
                return
            _log(result, f"2c: PubChem CID lookup failed for CID={cid}")

        # 2d: InChI → SMILES via RDKit
        inchi_val = ctx.get("inchi") or ""
        if inchi_val:
            smiles = self._inchi_to_smiles(inchi_val)
            if smiles:
                result.smiles = smiles
                _log(result, "2d: SMILES converted from InChI via RDKit")
                return
            _log(result, "2d: InChI → SMILES conversion failed")

        # 2e: SDF file
        sdf_path = ctx.get("sdf_path") or ""
        if sdf_path and Path(sdf_path).is_file():
            smiles = self._smiles_from_sdf(sdf_path)
            if smiles:
                result.smiles = smiles
                _log(result, f"2e: SMILES extracted from SDF: {sdf_path}")
                return
            _log(result, f"2e: SDF extraction failed: {sdf_path}")

        # All SMILES stages failed — not a hard error
        _log(result, "Stage 2 complete: SMILES unresolvable — will downgrade to protein_only_md")
        result.smiles = ""
        result.degradation_notice = _append_notice(
            result.degradation_notice,
            "Ligand SMILES could not be resolved from any source; "
            "downgrading to protein-only MD simulation.",
        )

    # ------------------------------------------------------------------
    # Stage 3 — PDB resolution
    # ------------------------------------------------------------------

    async def _resolve_pdb(self, ctx: dict[str, Any], result: ResolverResult) -> None:
        _log(result, "Stage 3 — PDB resolution")

        pdb_val = ctx.get("protein_pdb") or ctx.get("pdb_path") or ""

        # 3a: local file
        if pdb_val and Path(str(pdb_val)).is_file():
            result.protein_pdb = str(pdb_val)
            _log(result, f"3a: Using existing local PDB: {pdb_val}")
            return

        # 3b: 4-char PDB ID → download from RCSB
        if pdb_val and _is_4char_pdb_id(str(pdb_val)):
            pdb_id = str(pdb_val).strip().upper()
            path = await self._download_rcsb(pdb_id)
            if path:
                result.protein_pdb = path
                _log(result, f"3b: PDB downloaded from RCSB: {pdb_id} → {path}")
                result.degradation_notice = _append_notice(
                    result.degradation_notice,
                    f"PDB structure {pdb_id} was automatically downloaded from RCSB.",
                )
                return
            _log(result, f"3b: RCSB download failed for {pdb_id}")

        # 3c: UniProt ID → AlphaFold
        uid = ctx.get("uniprot_id") or ""
        if uid:
            path = await self._download_alphafold(str(uid))
            if path:
                result.protein_pdb = path
                _log(result, f"3c: AlphaFold structure downloaded for UniProt {uid}")
                result.degradation_notice = _append_notice(
                    result.degradation_notice,
                    f"Protein structure obtained from AlphaFold for UniProt ID {uid}.",
                )
                return
            _log(result, f"3c: AlphaFold download failed for UniProt {uid}")

        # 3d: gene name → UniProt → AlphaFold
        gene = ctx.get("gene_name") or ""
        if gene:
            uid_from_gene = await self._gene_to_uniprot(str(gene))
            if uid_from_gene:
                path = await self._download_alphafold(uid_from_gene)
                if path:
                    result.protein_pdb = path
                    _log(result, f"3d: AlphaFold structure via gene '{gene}' → UniProt {uid_from_gene}")
                    result.degradation_notice = _append_notice(
                        result.degradation_notice,
                        f"Protein structure obtained from AlphaFold via gene name '{gene}' "
                        f"(UniProt: {uid_from_gene}).",
                    )
                    return
            _log(result, f"3d: Gene→UniProt→AlphaFold pipeline failed for gene '{gene}'")

        # All PDB stages failed
        result.protein_pdb = ""
        _log(result, "Stage 3 complete: PDB unresolvable")

    # ------------------------------------------------------------------
    # Stage 4 — mode decision
    # ------------------------------------------------------------------

    def _decide_mode(self, result: ResolverResult) -> None:
        if result.smiles and result.protein_pdb:
            result.md_mode = "complex"
            _log(result, "Stage 4: mode=complex (both smiles and pdb resolved)")
        elif result.protein_pdb and not result.smiles:
            result.md_mode = "protein_only"
            _log(result, "Stage 4: mode=protein_only (ligand absent)")
        elif result.smiles and not result.protein_pdb:
            result.md_mode = "screening_only"
            _log(result, "Stage 4: mode=screening_only (no protein)")
        else:
            result.md_mode = "protein_only"  # will fail at stage 5

    # ------------------------------------------------------------------
    # Stage 5 — hard failure
    # ------------------------------------------------------------------

    def _raise_protein_failure(self, ctx: dict[str, Any], result: ResolverResult) -> None:
        msg = (
            "Protein PDB could not be resolved from any source "
            "(local file, RCSB 4-char ID, UniProt, or gene name). "
            "MD simulation cannot proceed."
        )
        result.quality_score = 0.0
        result.degradation_notice = _append_notice(result.degradation_notice, msg)

        if DriverFailureEvent is not None:
            from datetime import datetime

            evt = DriverFailureEvent(
                driver_id="ArtifactResolver",
                failure_type="protein_unresolvable",  # type: ignore[arg-type]
                query=str(ctx.get("query", "")),
                attempted_steps=list(result.resolution_log),
                timestamp=datetime.utcnow(),
                retryable=False,
            )
            raise RuntimeError(f"DriverFailureEvent: {evt}")
        else:
            raise RuntimeError(f"DriverFailureEvent(protein_unresolvable): {msg}")

    # ------------------------------------------------------------------
    # Quality scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_quality(result: ResolverResult) -> float:
        if result.md_mode == "complex" and not result.degradation_notice:
            return 1.0
        if result.md_mode == "complex" and result.degradation_notice:
            return 0.8
        if result.md_mode in ("protein_only", "screening_only"):
            return 0.5
        return 0.0

    # ------------------------------------------------------------------
    # Sub-resolvers (extracted for mocking in tests)
    # ------------------------------------------------------------------

    async def _fetch_smiles_by_name(self, name: str) -> str:
        """Query PubChem by compound name → IsomericSMILES."""
        encoded = urllib.parse.quote(name) if hasattr(urllib, "parse") else name.replace(" ", "%20")
        url = (
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
            f"{encoded}/property/IsomericSMILES/JSON"
        )
        try:
            data = await _async_http_get(url)
            payload = json.loads(data)
            return payload["PropertyTable"]["Properties"][0]["IsomericSMILES"]
        except Exception as exc:
            logger.debug("PubChem name lookup failed: %s", exc)
            return ""

    async def _fetch_smiles_by_cid(self, cid: str) -> str:
        """Query PubChem by CID → IsomericSMILES."""
        url = (
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
            f"{cid}/property/IsomericSMILES/JSON"
        )
        try:
            data = await _async_http_get(url)
            payload = json.loads(data)
            return payload["PropertyTable"]["Properties"][0]["IsomericSMILES"]
        except Exception as exc:
            logger.debug("PubChem CID lookup failed: %s", exc)
            return ""

    @staticmethod
    def _inchi_to_smiles(inchi_val: str) -> str:
        if not _RDKIT_AVAILABLE:
            return ""
        try:
            mol = rdkit_inchi.MolFromInchi(inchi_val)
            if mol is None:
                return ""
            return Chem.MolToSmiles(mol)
        except Exception:
            return ""

    @staticmethod
    def _smiles_from_sdf(sdf_path: str) -> str:
        if not _RDKIT_AVAILABLE:
            return ""
        try:
            supplier = Chem.SDMolSupplier(sdf_path)
            for mol in supplier:
                if mol is not None:
                    return Chem.MolToSmiles(mol)
        except Exception:
            pass
        return ""

    async def _download_rcsb(self, pdb_id: str) -> str:
        """Download PDB from RCSB; return local file path or '' on failure."""
        url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
        try:
            data = await _async_http_get(url)
            tmp_dir = tempfile.mkdtemp(prefix="mica_pdb_")
            out_path = os.path.join(tmp_dir, f"{pdb_id}.pdb")
            with open(out_path, "wb") as fh:
                fh.write(data)
            return out_path
        except Exception as exc:
            logger.debug("RCSB download failed for %s: %s", pdb_id, exc)
            return ""

    async def _download_alphafold(self, uid: str) -> str:
        """Download AlphaFold model; return local file path or '' on failure.

        Tries current (v6) first, then v4 fallback. AF EBI bumped v4 → v6 in 2025.
        Override with ALPHAFOLD_MODEL_VERSION env var if it bumps again.
        """
        preferred = os.environ.get("ALPHAFOLD_MODEL_VERSION", "v6").strip() or "v6"
        versions = [preferred] + [v for v in ("v6", "v5", "v4") if v != preferred]
        for ver in versions:
            url = f"https://alphafold.ebi.ac.uk/files/AF-{uid}-F1-model_{ver}.pdb"
            try:
                data = await _async_http_get(url)
                tmp_dir = tempfile.mkdtemp(prefix="mica_af_")
                out_path = os.path.join(tmp_dir, f"AF-{uid}-F1-model_{ver}.pdb")
                with open(out_path, "wb") as fh:
                    fh.write(data)
                return out_path
            except Exception as exc:
                logger.debug("AlphaFold %s download failed for %s: %s", ver, uid, exc)
                continue
        return ""

    async def _gene_to_uniprot(self, gene: str) -> str:
        """Query UniProt REST to get the canonical UniProt accession for a gene name."""
        url = (
            f"https://rest.uniprot.org/uniprotkb/search"
            f"?query=gene:{urllib.parse.quote(gene)}&format=json&size=1"
        )
        try:
            data = await _async_http_get(url)
            payload = json.loads(data)
            results = payload.get("results", [])
            if results:
                return results[0]["primaryAccession"]
        except Exception as exc:
            logger.debug("UniProt gene lookup failed for '%s': %s", gene, exc)
        return ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _append_notice(existing: str, new_notice: str) -> str:
    """Append a degradation notice; avoid duplicates."""
    if not existing:
        return new_notice
    if new_notice in existing:
        return existing
    return f"{existing}; {new_notice}"


# ---------------------------------------------------------------------------
# urllib.parse fix — make sure it's importable at module level
# ---------------------------------------------------------------------------
import urllib.parse  # noqa: E402  (already in stdlib, always present)

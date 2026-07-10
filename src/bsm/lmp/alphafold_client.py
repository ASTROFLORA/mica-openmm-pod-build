"""
AlphaFold DB API Client for LMP structural enrichment.

Fetches: metadata, PDB/mmCIF files, PAE matrices, per-residue pLDDT.
Cache-first: all downloads cached locally with configurable TTL.
Graceful degradation: if AlphaFold is down, LMP generates without structure.

Endpoints used (documented at https://alphafold.ebi.ac.uk/api-docs):
  - GET /api/prediction/{uniprot_accession}  -> model metadata + file URLs
  - File downloads: pdbUrl, cifUrl, paeDocUrl from prediction response
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_UNIPROT_RE = re.compile(r"^[A-Z0-9]{4,12}$", re.IGNORECASE)


@dataclass
class AlphaFoldModelMeta:
    """Metadata for a single AlphaFold model fragment."""

    entry_id: str  # "AF-P00520-F1"
    uniprot_accession: str  # "P00520"
    gene: Optional[str] = None  # "ABL1"
    organism: Optional[str] = None  # "Mus musculus"
    model_version: int = 4
    confidence_avg_plddt: float = 0.0
    confidence_version: int = 4
    uniprot_start: int = 1
    uniprot_end: int = 0
    pdb_url: str = ""
    cif_url: str = ""
    pae_url: Optional[str] = None
    model_created_date: str = ""


@dataclass
class AlphaFoldStructure:
    """Resolved AlphaFold structure with local files and extracted metrics."""

    meta: AlphaFoldModelMeta
    pdb_path: Optional[Path] = None
    cif_path: Optional[Path] = None
    pae_matrix: Optional[List[List[float]]] = None
    plddt_per_residue: Optional[List[Tuple[int, str, float]]] = None
    mean_pae: Optional[float] = None
    max_pae: Optional[float] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AlphaFoldClient:
    """Cache-first AlphaFold DB API client.

    Parameters
    ----------
    cache_dir : Path
        Root directory for cached downloads. Will be created if absent.
    ttl_seconds : int
        Cache time-to-live. Default 30 days.
    timeout : int
        HTTP request timeout in seconds. Default 30.
    """

    BASE_URL = "https://alphafold.ebi.ac.uk/api"

    def __init__(
        self,
        cache_dir: Path,
        *,
        ttl_seconds: int = 86400 * 30,
        timeout: int = 30,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self._session: Optional[requests.Session] = None

    # -- Session management -------------------------------------------------

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Accept": "application/json"})
        return self._session

    # -- Cache helpers -------------------------------------------------------

    def _cache_path(self, *parts: str) -> Path:
        p = self.cache_dir.joinpath("alphafold", *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _is_cache_fresh(self, path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        age = time.time() - path.stat().st_mtime
        return age < self.ttl_seconds

    def _read_json_cache(self, path: Path) -> Optional[Any]:
        if not self._is_cache_fresh(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_json_cache(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    # -- API methods ---------------------------------------------------------

    def fetch_prediction(self, uniprot_accession: str) -> List[AlphaFoldModelMeta]:
        """GET /prediction/{accession} -> list of AlphaFoldModelMeta.

        Returns empty list on any failure (graceful degradation).
        """
        accession = (uniprot_accession or "").strip().upper()
        if not accession or not _UNIPROT_RE.match(accession):
            logger.warning("Invalid UniProt accession: %r", uniprot_accession)
            return []

        cache_file = self._cache_path(accession, "prediction.json")
        cached = self._read_json_cache(cache_file)
        if cached is not None:
            logger.debug("AlphaFold prediction cache hit for %s", accession)
            return self._parse_prediction_response(cached)

        url = f"{self.BASE_URL}/prediction/{accession}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 404:
                logger.info("No AlphaFold model for %s (404)", accession)
                # Cache the 404 as empty list to avoid re-hitting
                self._write_json_cache(cache_file, [])
                return []
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("AlphaFold API request failed for %s: %s", accession, exc)
            return []
        except (ValueError, KeyError) as exc:
            logger.warning("AlphaFold API parse error for %s: %s", accession, exc)
            return []

        self._write_json_cache(cache_file, data)
        return self._parse_prediction_response(data)

    def _parse_prediction_response(self, data: Any) -> List[AlphaFoldModelMeta]:
        if not isinstance(data, list):
            return []
        results: List[AlphaFoldModelMeta] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("entryId", "")
            if not entry_id:
                continue
            results.append(
                AlphaFoldModelMeta(
                    entry_id=str(entry_id),
                    uniprot_accession=str(entry.get("uniprotAccession", "")),
                    gene=entry.get("gene"),
                    organism=entry.get("organismScientificName"),
                    model_version=int(entry.get("latestVersion", 4)),
                    confidence_avg_plddt=float(entry.get("globalMetricValue") or entry.get("confidenceAvgLocalScore") or 0.0),
                    confidence_version=int(entry.get("confidenceVersion", 4)),
                    uniprot_start=int(entry.get("uniprotStart", 1)),
                    uniprot_end=int(entry.get("uniprotEnd", 0)),
                    pdb_url=str(entry.get("pdbUrl", "")),
                    cif_url=str(entry.get("cifUrl", "")),
                    pae_url=entry.get("paeDocUrl"),
                    model_created_date=str(entry.get("modelCreatedDate", "")),
                )
            )
        return results

    # -- Structure download --------------------------------------------------

    def download_structure(
        self,
        meta: AlphaFoldModelMeta,
        *,
        download_pdb: bool = True,
        download_pae: bool = True,
        download_cif: bool = False,
    ) -> AlphaFoldStructure:
        """Download PDB + PAE + extract pLDDT from B-factor column.

        Returns AlphaFoldStructure with local paths. Missing files are None.
        If download_cif=True, also downloads the .cif file (needed for FlatProt).
        """
        accession = meta.uniprot_accession.upper()
        result = AlphaFoldStructure(meta=meta)

        # PDB download
        if download_pdb and meta.pdb_url:
            pdb_cache = self._cache_path(accession, f"{meta.entry_id}.pdb")
            if self._is_cache_fresh(pdb_cache):
                result.pdb_path = pdb_cache
            else:
                try:
                    resp = self.session.get(meta.pdb_url, timeout=self.timeout)
                    resp.raise_for_status()
                    pdb_cache.write_bytes(resp.content)
                    result.pdb_path = pdb_cache
                except requests.RequestException as exc:
                    logger.warning("Failed to download AlphaFold PDB for %s: %s", accession, exc)

            # Extract pLDDT from B-factor
            if result.pdb_path and result.pdb_path.exists():
                try:
                    result.plddt_per_residue = self.extract_plddt_from_pdb(result.pdb_path)
                except Exception as exc:
                    logger.warning("Failed to extract pLDDT for %s: %s", accession, exc)

        # CIF download (optional; needed for FlatProt secondary structure extraction)
        if download_cif and meta.cif_url:
            cif_cache = self._cache_path(accession, f"{meta.entry_id}.cif")
            if self._is_cache_fresh(cif_cache):
                result.cif_path = cif_cache
            else:
                try:
                    resp = self.session.get(meta.cif_url, timeout=self.timeout)
                    resp.raise_for_status()
                    cif_cache.write_bytes(resp.content)
                    result.cif_path = cif_cache
                except requests.RequestException as exc:
                    logger.warning("Failed to download AlphaFold CIF for %s: %s", accession, exc)

        # PAE download
        if download_pae and meta.pae_url:
            pae_cache = self._cache_path(accession, f"{meta.entry_id}_pae.json")
            if self._is_cache_fresh(pae_cache):
                pae_raw = pae_cache
            else:
                pae_raw = None
                try:
                    resp = self.session.get(meta.pae_url, timeout=self.timeout)
                    resp.raise_for_status()
                    pae_cache.write_bytes(resp.content)
                    pae_raw = pae_cache
                except requests.RequestException as exc:
                    logger.warning("Failed to download AlphaFold PAE for %s: %s", accession, exc)

            if pae_raw and pae_raw.exists():
                try:
                    matrix, max_pae = self.parse_pae_json(pae_raw)
                    result.pae_matrix = matrix
                    result.max_pae = max_pae
                    if matrix:
                        flat = [v for row in matrix for v in row if v is not None]
                        result.mean_pae = sum(flat) / len(flat) if flat else None
                except Exception as exc:
                    logger.warning("Failed to parse PAE for %s: %s", accession, exc)

        return result

    def get_structure_for_accession(
        self,
        accession: str,
        *,
        download_pdb: bool = True,
        download_pae: bool = True,
        download_cif: bool = False,
    ) -> Optional[AlphaFoldStructure]:
        """Full pipeline: fetch meta -> download best model -> extract metrics.

        Returns None if no AlphaFold model exists or API is unreachable.
        """
        models = self.fetch_prediction(accession)
        if not models:
            return None

        # Pick the model with highest average pLDDT (typically only one)
        best = max(models, key=lambda m: m.confidence_avg_plddt)
        return self.download_structure(
            best,
            download_pdb=download_pdb,
            download_pae=download_pae,
            download_cif=download_cif,
        )

    # -- pLDDT extraction ----------------------------------------------------

    @staticmethod
    def extract_plddt_from_pdb(pdb_path: Path) -> List[Tuple[int, str, float]]:
        """Parse B-factor column from AlphaFold PDB (B-factor = pLDDT).

        Returns list of (residue_number, residue_name, pLDDT_score) tuples,
        one per residue (uses CA atom only to deduplicate).
        """
        plddt: List[Tuple[int, str, float]] = []
        seen: set = set()
        with open(pdb_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("ATOM"):
                    continue
                atom_name = line[12:16].strip()
                if atom_name != "CA":
                    continue
                try:
                    resid = int(line[22:26].strip())
                except (ValueError, IndexError):
                    continue
                if resid in seen:
                    continue
                seen.add(resid)
                resname = line[17:20].strip()
                try:
                    bfactor = float(line[60:66].strip())
                except (ValueError, IndexError):
                    continue
                plddt.append((resid, resname, bfactor))
        return plddt

    @staticmethod
    def plddt_confidence_class(score: float) -> str:
        """Classify a pLDDT score into AlphaFold confidence tiers."""
        if score >= 90:
            return "very_high"
        if score >= 70:
            return "confident"
        if score >= 50:
            return "low"
        return "very_low"

    # -- PAE parsing ---------------------------------------------------------

    @staticmethod
    def parse_pae_json(pae_path: Path) -> Tuple[List[List[float]], float]:
        """Parse PAE JSON into NxN matrix.

        Returns (matrix, max_predicted_aligned_error).
        AlphaFold v4 format: [{"predicted_aligned_error": [[...]], "max_predicted_aligned_error": 31.75}]
        """
        with open(pae_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Handle both list-wrapped and bare dict formats
        if isinstance(data, list) and len(data) > 0:
            obj = data[0]
        elif isinstance(data, dict):
            obj = data
        else:
            raise ValueError(f"Unexpected PAE format: {type(data)}")

        matrix = obj.get("predicted_aligned_error")
        if not isinstance(matrix, list):
            raise ValueError("PAE JSON missing 'predicted_aligned_error' key")

        max_pae = float(obj.get("max_predicted_aligned_error", 31.75))
        return matrix, max_pae

    @staticmethod
    def compute_domain_pae(
        pae_matrix: List[List[float]],
        domains: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Compute mean inter-domain PAE for domain pairs.

        Parameters
        ----------
        pae_matrix : NxN PAE matrix
        domains : list of dicts with keys 'name', 'start', 'end' (1-indexed)

        Returns
        -------
        List of dicts with domain_a, domain_b, mean_pae, confident_interface.
        """
        n = len(pae_matrix)
        pairs: List[Dict[str, Any]] = []

        for i, dom_a in enumerate(domains):
            for dom_b in domains[i + 1:]:
                a_start = max(0, int(dom_a.get("start", 1)) - 1)
                a_end = min(n, int(dom_a.get("end", 0)))
                b_start = max(0, int(dom_b.get("start", 1)) - 1)
                b_end = min(n, int(dom_b.get("end", 0)))

                if a_start >= a_end or b_start >= b_end:
                    continue

                values = []
                for ri in range(a_start, a_end):
                    if ri >= n:
                        break
                    row = pae_matrix[ri]
                    for ci in range(b_start, b_end):
                        if ci < len(row):
                            values.append(row[ci])

                # Also add reverse direction
                for ri in range(b_start, b_end):
                    if ri >= n:
                        break
                    row = pae_matrix[ri]
                    for ci in range(a_start, a_end):
                        if ci < len(row):
                            values.append(row[ci])

                if not values:
                    continue

                mean_pae = sum(values) / len(values)
                pairs.append({
                    "domain_a": str(dom_a.get("name", "?")),
                    "domain_b": str(dom_b.get("name", "?")),
                    "mean_pae": round(mean_pae, 2),
                    "confident_interface": mean_pae < 10.0,
                })

        return pairs

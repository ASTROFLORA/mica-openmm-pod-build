"""
LMP v2.0 XML Generator with NeSy Integration
==============================================

Generate LMP v2.0 XML documents from external biological databases:
- UniProt (PTM annotations, domains, binding sites)
- PDB (conformational states)
- PhosphoSitePlus (phosphorylation data)
- Inferred states from structural and functional data

NOW INCLUDES:
- Full NeSy (Neuro-Symbolic) sequence encoding from ANEXO
- Hierarchical markers: [DOM], [TMD], [MOT], (ATP), (CAT), <PPI>
- Enhanced PTMs with enzymes: {S-P:PKA}, {K-Ac:p300}
- State markers: *ACTIVE*, *DFG-IN*, *DFG-OUT*

Supports semi-automated generation with manual curation options.
"""

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

# Import NeSy encoder
from .nesy_encoder import LMPNeSyEncoder, NeSyAnnotation
from .pas_annotators import get_pas_annotator


# Decorator for retry logic with exponential backoff
def with_retry_and_backoff(max_retries=3, backoff_factor=2, exceptions=(Exception,), fallback=None):
    """
    Retry decorator with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts
        backoff_factor: Multiplier for wait time between retries
        exceptions: Tuple of exception types to catch
        fallback: Optional callable invoked after exhausting retries. Signature:
            fallback(last_exception, *func_args, **func_kwargs)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries - 1:
                        break
                    
                    wait_time = backoff_factor ** attempt
                    logger = logging.getLogger(__name__)
                    logger.warning(
                        f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)

            if fallback is not None:
                return fallback(last_exception, *args, **kwargs)
            if last_exception is not None:
                raise last_exception
            return None  # Should not reach here
        return wrapper
    return decorator


def _uniprot_retry_fallback(last_exception: Exception, *call_args, **call_kwargs):
    """Gracefully degrade UniProt fetches after exhausting retries."""
    instance = call_args[0] if call_args else None
    if instance is None:
        raise last_exception

    uniprot_id = call_kwargs.get("uniprot_id")
    if uniprot_id is None and len(call_args) > 1:
        uniprot_id = call_args[1]

    if uniprot_id is None:
        raise last_exception

    instance.logger.warning(
        "Retries exhausted for UniProt %s (%s). Returning minimal payload for graceful degradation.",
        uniprot_id,
        last_exception,
    )
    instance._log_uniprot_call(
        uniprot_id,
        source="network",
        status="fallback",
        latency_ms=0.0,
        extra={"reason": str(last_exception)},
    )
    return instance._create_minimal_uniprot_data(uniprot_id)


class LMPGenerator:
    """
    Generator for LMP v2.0 XML documents
    
    Strategy:
    1. Query UniProt for PTM annotations
    2. Query PDB for structural states
    3. Infer functional states from PTM patterns + domain knowledge
    4. Generate multiple LMP documents per protein (one per state)
    
    Example Usage:
    ```python
    generator = LMPGenerator()
    
    # Generate LMP for c-Src kinase
    lmp_documents = generator.generate_multi_state(
        uniprot_id="P12931",
        gene_name="SRC",
        states=["Inactive", "Active"]
    )
    
    # Save to disk
    for state, xml_str in lmp_documents.items():
        Path(f"P12931_{state}.xml").write_text(xml_str)
    ```
    """
    
    UNIPROT_API = "https://rest.uniprot.org/uniprotkb"
    PDB_API = "https://data.rcsb.org/rest/v1/core/entry"
    PUBCHEM_API_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    
    # Controlled vocabularies
    PTM_TYPES = {
        "phosphorylation": ["pS", "pT", "pY"],
        "acetylation": ["acK"],
        "ubiquitination": ["ubK"],
        "methylation": ["meK", "meR"],
        "sumoylation": ["suK"],
    }
    
    LIGAND_EFFECTS = ["activation", "inhibition", "catalysis", "allosteric_modulation"]
    
    # PTM-Residue Compatibility Matrix (Bug #3 fix)
    PTM_RESIDUE_COMPATIBILITY = {
        "phosphorylation": {"S", "T", "Y"},
        "acetylation": {"K"},
        "methylation": {"K", "R", "H"},
        "ubiquitination": {"K"},
        "sumoylation": {"K"},
        "O-glycosylation": {"S", "T"},
        "N-glycosylation": {"N"},
        "hydroxylation": {"P", "K"},
        "nitrosylation": {"C"},
        "palmitoylation": {"C"},
        "n_terminal_myristoylation": {"G"},  # N-terminal only
        "n_terminal_acetylation": {"M", "A", "S", "P", "V", "T"},  # N-terminal amino acids
    }
    
    STATE_KEYWORDS = {
        "active": ["active", "open", "agonist", "phosphorylated"],
        "inactive": ["inactive", "closed", "antagonist", "autoinhibited"],
        "transition": ["intermediate", "transition", "partially"],
    }
    
    # Cache configuration
    CACHE_TTL_DAYS = 30  # Cache expiration time
    MAX_CACHE_SIZE_MB = 1000  # Maximum cache size

    _CACHE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")
    
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        rate_limit: float = 0.5,
        config_path: Optional[Path] = None,
    ):
        """
        Initialize LMP generator
        
        Args:
            cache_dir: Directory for caching API responses
            rate_limit: Minimum seconds between API requests
        """
        self.logger = logging.getLogger(self.__class__.__name__)

        self.config: Dict[str, Any] = self._load_config(config_path)
        generator_cfg: Dict[str, Any] = self.config.get("generator", {}) if isinstance(self.config, dict) else {}

        effective_cache_dir = cache_dir
        if effective_cache_dir is None:
            cfg_cache_dir = generator_cfg.get("cache_dir")
            if isinstance(cfg_cache_dir, str) and cfg_cache_dir.strip():
                effective_cache_dir = Path(cfg_cache_dir)

        self.cache_dir = Path(effective_cache_dir) if effective_cache_dir else Path("lmp_cache")
        self.cache_dir.mkdir(exist_ok=True)

        cfg_rate_limit = generator_cfg.get("rate_limit")
        self.rate_limit = float(cfg_rate_limit) if cfg_rate_limit is not None else rate_limit
        self._last_request_time = 0.0

        # PubChem enrichment configuration (best-effort)
        pubchem_cfg: Dict[str, Any] = generator_cfg.get("pubchem", {}) if isinstance(generator_cfg.get("pubchem"), dict) else {}
        self.pubchem_enabled: bool = bool(pubchem_cfg.get("enabled", False))
        self.pubchem_api_base: str = str(pubchem_cfg.get("api_base", self.PUBCHEM_API_BASE)).rstrip("/")
        self.pubchem_timeout_seconds: float = float(pubchem_cfg.get("timeout_seconds", 10))
        self.pubchem_rate_limit: float = float(pubchem_cfg.get("rate_limit", 0.25))
        self.pubchem_max_ligands_per_pdb: int = int(pubchem_cfg.get("max_ligands_per_pdb", 20))
        self.pubchem_include_synonyms: bool = bool(pubchem_cfg.get("include_synonyms", False))
        # PubChem dynamic throttling: https://pubchem.ncbi.nlm.nih.gov/docs/dynamic-request-throttling
        self.pubchem_dynamic_throttling: bool = bool(pubchem_cfg.get("dynamic_throttling", True))
        self.pubchem_max_retries: int = int(pubchem_cfg.get("max_retries", 3))
        self.pubchem_backoff_base_seconds: float = float(pubchem_cfg.get("backoff_base_seconds", 1.0))
        self.pubchem_jitter_seconds: float = float(pubchem_cfg.get("jitter_seconds", 0.25))
        self.pubchem_property_fields: List[str] = list(pubchem_cfg.get(
            "property_fields",
            ["CanonicalSMILES", "IsomericSMILES", "InChI", "InChIKey", "MolecularFormula", "MolecularWeight", "IUPACName"],
        ))
        self.pubchem_write_sidecar_json: bool = bool(pubchem_cfg.get("write_sidecar_json", True))
        self.pubchem_sidecar_prefix: str = str(pubchem_cfg.get("sidecar_prefix", "pubchem_enrichment"))
        self.pubchem_xml_include_cid: bool = bool(pubchem_cfg.get("xml_include_pubchem_cid", False))
        self._pubchem_last_request_time: float = 0.0
        
        # Circuit breaker state for API failures
        self._circuit_breaker = {
            'failures': 0,
            'last_failure_time': None,
            'state': 'closed',  # closed, open, half_open
            'failure_threshold': 5,
            'reset_timeout': 60  # seconds
        }
        
        # Initialize cache cleanup
        self._cleanup_cache()

    def _load_config(self, config_path: Optional[Path]) -> Dict[str, Any]:
        """Load LMP configuration from YAML file (best-effort)."""
        if config_path is None:
            config_path = Path(__file__).parent / "lmp_config.yaml"

        config_path = Path(config_path)
        if not config_path.exists():
            return {}

        if yaml is None:
            self.logger.warning("PyYAML not available; skipping LMP config load (%s)", config_path)
            return {}

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config if isinstance(config, dict) else {}
        except Exception as e:
            self.logger.warning("Failed to load LMP config from %s: %s", config_path, e)
            return {}

    @classmethod
    def _cache_key(cls, raw: str, *, max_len: int = 128) -> str:
        """Create a filesystem-safe cache key from an arbitrary identifier."""
        if not isinstance(raw, str):
            raw = str(raw)
        safe = cls._CACHE_KEY_RE.sub("_", raw).strip("._-")
        if not safe:
            safe = "unknown"
        return safe[:max_len]
    
    def _get_cache_path(self, cache_file: Path) -> Optional[Path]:
        """
        Get cache file path if valid, None if expired
        
        Args:
            cache_file: Path to cache file
            
        Returns:
            Path if cache is valid, None if expired or missing
        """
        if not cache_file.exists():
            return None
            
        # Check TTL
        file_age_days = (time.time() - cache_file.stat().st_mtime) / (24 * 3600)
        if file_age_days > self.CACHE_TTL_DAYS:
            try:
                cache_file.unlink()
                self.logger.info(f"Expired cache removed: {cache_file.name}")
            except Exception as e:
                self.logger.warning(f"Failed to remove expired cache {cache_file.name}: {e}")
            return None
            
        return cache_file
    
    def _cleanup_cache(self):
        """
        Clean up expired and oversized cache entries
        Runs on initialization to maintain cache health
        """
        try:
            # Remove expired files
            for cache_file in self.cache_dir.glob("*.json"):
                self._get_cache_path(cache_file)  # Will remove if expired
            
            # Check total cache size
            total_size = sum(f.stat().st_size for f in self.cache_dir.glob("*.json"))
            max_size_bytes = self.MAX_CACHE_SIZE_MB * 1024 * 1024
            
            if total_size > max_size_bytes:
                # Remove oldest files until under limit
                files = sorted(
                    self.cache_dir.glob("*.json"),
                    key=lambda f: f.stat().st_mtime
                )
                
                for cache_file in files:
                    if total_size <= max_size_bytes:
                        break
                    try:
                        file_size = cache_file.stat().st_size
                        cache_file.unlink()
                        total_size -= file_size
                        self.logger.info(f"Removed cache file to free space: {cache_file.name}")
                    except Exception as e:
                        self.logger.warning(f"Failed to remove cache file {cache_file.name}: {e}")
                        
        except Exception as e:
            self.logger.warning(f"Cache cleanup failed: {e}")

    def _log_uniprot_call(
        self,
        uniprot_id: str,
        *,
        source: str,
        status: str,
        latency_ms: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit structured log lines for UniProt monitoring."""
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uniprot_id": uniprot_id,
            "source": source,
            "status": status,
            "latency_ms": round(latency_ms, 4),
        }
        if extra:
            payload.update(extra)
        self.logger.info("UNIPROT_CALL %s", json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _elapsed_ms(start_time: Optional[float]) -> float:
        if start_time is None:
            return 0.0
        return max((time.perf_counter() - start_time) * 1000, 0.0)
    
    def _validate_uniprot_response(self, data: Dict[str, Any], uniprot_id: str) -> bool:
        """
        Validate UniProt API response structure
        
        Args:
            data: Response data from UniProt API
            uniprot_id: Expected UniProt ID
            
        Returns:
            True if response is valid, False otherwise
        """
        if not isinstance(data, dict):
            self.logger.error(f"Invalid UniProt response for {uniprot_id}: not a dict")
            return False

        # The generator supports two shapes:
        # 1) Raw UniProt REST JSON: has `primaryAccession`, `organism`, etc.
        # 2) Normalized cached summary used by this repo: has `uniprot_id`, `gene_name`,
        #    `organism` (string), `sequence`, and `features`.
        if 'uniprot_id' in data:
            required_fields = ['uniprot_id', 'organism', 'sequence']
            for field in required_fields:
                if field not in data:
                    self.logger.warning(
                        f"Missing required field '{field}' in UniProt cache for {uniprot_id}"
                    )
                    return False

            if data.get('uniprot_id') != uniprot_id:
                self.logger.warning(
                    f"Accession mismatch: requested {uniprot_id}, got {data.get('uniprot_id')}"
                )

            return True
            
        # Raw UniProt REST payload
        required_fields = ['primaryAccession', 'organism']
        for field in required_fields:
            if field not in data:
                self.logger.warning(f"Missing required field '{field}' in UniProt response for {uniprot_id}")
                return False
        
        # Validate accession matches
        if data.get('primaryAccession') != uniprot_id:
            self.logger.warning(f"Accession mismatch: requested {uniprot_id}, got {data.get('primaryAccession')}")
            # This is acceptable for ID mapping, just log
        
        return True
    
    def _create_minimal_uniprot_data(self, uniprot_id: str) -> Dict[str, Any]:
        """
        Create minimal UniProt data structure for graceful degradation
        
        Args:
            uniprot_id: UniProt accession
            
        Returns:
            Minimal valid data structure
        """
        return {
            "uniprot_id": uniprot_id,
            "gene_name": uniprot_id,
            "organism": "Unknown",
            "taxonomy_id": 0,
            "sequence": "",
            "features": [],
            "comments": [],
            "dbReferences": []
        }
    
    
    def generate_multi_state(
        self,
        uniprot_id: str,
        gene_name: str,
        organism: str = "Homo sapiens",
        states: Optional[List[str]] = None,
        pdb_ids: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """
        Generate multiple LMP XML documents for different states
        
        Args:
            uniprot_id: UniProt accession (e.g., "P12931")
            gene_name: Gene name (e.g., "SRC")
            organism: Organism name
            states: List of state names to generate (default: infer from data)
            pdb_ids: List of PDB IDs to include (optional)
        
        Returns:
            Dictionary mapping state_name → LMP XML string
        """
        # Fetch UniProt data
        uniprot_data = self._fetch_uniprot(uniprot_id)
        
        # Extract PTMs
        ptms = self._extract_ptms(uniprot_data)
        
        # Extract domains
        domains = self._extract_domains(uniprot_data)
        
        # Extract binding sites
        binding_sites = self._extract_binding_sites(uniprot_data)
        
        # Extract motifs
        motifs = self._extract_motifs(uniprot_data)
        
        # Enrich with PAS (Protein Annotation Specificity)
        domains = self._enrich_with_pas(
            domains=domains,
            sequence=uniprot_data.get("sequence", ""),
            ptms=ptms,
            uniprot_data=uniprot_data
        )
        
        # Fetch PDB structures if provided
        pdb_data = {}
        if pdb_ids:
            for pdb_id in pdb_ids:
                pdb_data[pdb_id] = self._fetch_pdb(pdb_id)
        
        # Infer states if not provided
        if states is None:
            states = self._infer_states(ptms, domains)
        
        # Generate LMP document for each state
        lmp_documents = {}
        for state_name in states:
            xml_str = self._generate_lmp_xml(
                uniprot_id=uniprot_id,
                gene_name=gene_name,
                organism=organism,
                state_name=state_name,
                sequence=uniprot_data.get("sequence", ""),
                domains=domains,
                ptms=ptms,
                binding_sites=binding_sites,
                motifs=motifs,
                pdb_data=pdb_data,
            )
            lmp_documents[state_name] = xml_str
        
        return lmp_documents
    
    def generate_from_mcsa(
        self,
        uniprot_id: str,
        catalytic_residues: List[int],
        output_dir: Path,
    ) -> List[Path]:
        """
        Generate LMP documents for M-CSA protein with catalytic states
        
        Args:
            uniprot_id: UniProt accession
            catalytic_residues: List of catalytic residue positions
            output_dir: Output directory for LMP XML files
        
        Returns:
            List of generated file paths
        
        States generated:
        - Apo/Inactive: No substrate, catalytic site unoccupied
        - Substrate-bound/Active: Substrate present, catalytic residues engaged
        - Inhibitor-bound (optional): If inhibitor data available
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Fetch UniProt data
        uniprot_data = self._fetch_uniprot(uniprot_id)
        gene_name = uniprot_data.get("gene_name", uniprot_id)
        organism = uniprot_data.get("organism", "Unknown")
        
        # Generate states
        states_to_generate = ["Apo_Inactive", "Substrate_bound_Active"]
        
        lmp_documents = self.generate_multi_state(
            uniprot_id=uniprot_id,
            gene_name=gene_name,
            organism=organism,
            states=states_to_generate,
        )
        
        # Annotate catalytic residues in each state
        for state_name, xml_str in lmp_documents.items():
            xml_str = self._annotate_catalytic_residues(
                xml_str, catalytic_residues, state_name
            )
            
            # Save to file
            output_path = output_dir / f"{uniprot_id}_{state_name}.xml"
            output_path.write_text(xml_str, encoding="utf-8")
        
        return list(output_dir.glob(f"{uniprot_id}_*.xml"))
    
    @with_retry_and_backoff(max_retries=3, backoff_factor=2, fallback=_uniprot_retry_fallback)
    def _fetch_uniprot(self, uniprot_id: str) -> Dict[str, Any]:
        """Fetch UniProt entry data with validation and TTL-aware caching"""
        cache_file = self.cache_dir / f"{self._cache_key(uniprot_id)}_uniprot.json"
        
        # Check cache with TTL validation
        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                cached = json.loads(valid_cache.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and self._validate_uniprot_response(cached, uniprot_id):
                    self._log_uniprot_call(
                        uniprot_id,
                        source="cache",
                        status="success",
                        latency_ms=0.0,
                        extra={"cache_file": str(valid_cache)},
                    )
                    return cached
                else:
                    # Corrupt or invalid cache — remove and refetch
                    valid_cache.unlink()
                    self.logger.warning(f"Invalid cache for {uniprot_id} removed")
            except Exception as e:
                self.logger.warning(f"Failed to read cache for {uniprot_id}: {e}")
                try:
                    cache_file.unlink()
                except:
                    pass
        
        # Rate limit
        self._rate_limit_wait()

        # Fetch from API
        request_start = time.perf_counter()
        url = f"{self.UNIPROT_API}/{uniprot_id}.json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Validate response structure
            if not self._validate_uniprot_response(data, uniprot_id):
                self.logger.error(f"Invalid response structure for {uniprot_id}")
                # Return minimal structure for graceful degradation
                self._log_uniprot_call(
                    uniprot_id,
                    source="network",
                    status="invalid_response",
                    latency_ms=self._elapsed_ms(request_start),
                    extra={"reason": "validation_failed"},
                )
                return self._create_minimal_uniprot_data(uniprot_id)
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try ID mapping (Bug #2 fix: obsolete IDs)
                self.logger.warning(f"UniProt {uniprot_id} not found (404), trying ID mapping...")
                new_id = self._try_uniprot_id_mapping(uniprot_id)
                if new_id:
                    self.logger.info(f"Mapped {uniprot_id} → {new_id}")
                    return self._fetch_uniprot(new_id)  # Recursive call with new ID
                else:
                    self.logger.error(f"ID mapping failed for {uniprot_id}")
                    self._log_uniprot_call(
                        uniprot_id,
                        source="network",
                        status="not_found",
                        latency_ms=self._elapsed_ms(request_start),
                        extra={"http_status": 404},
                    )
                    return self._create_minimal_uniprot_data(uniprot_id)
            else:
                self.logger.warning(f"HTTP error for {uniprot_id}: {e}")
                # Will be retried by decorator, or return minimal data after retries exhausted
                raise
        except Exception as e:
            # Network or API error — will be retried by decorator
            self.logger.warning(f"Failed to fetch UniProt {uniprot_id}: {e}")
            raise
        
        # Extract relevant fields
        uniprot_data = {
            "uniprot_id": uniprot_id,
            "gene_name": data.get("genes", [{}])[0].get("geneName", {}).get("value", ""),
            "organism": data.get("organism", {}).get("scientificName", "Unknown"),
            "taxonomy_id": data.get("organism", {}).get("taxonId", 0),
            "sequence": data.get("sequence", {}).get("value", ""),
            "features": data.get("features", []),
            "dbReferences": data.get("uniProtKBCrossReferences", []),
        }

        # Fetch InterPro Data for real domain coordinates
        try:
            # Use the InterPro API to get domain coordinates
            interpro_url = f"https://www.ebi.ac.uk/interpro/api/entry/interpro/protein/uniprot/{uniprot_id}"
            # Use a slightly longer timeout for InterPro as it can be slow
            ip_response = requests.get(interpro_url, timeout=15)
            
            if ip_response.status_code == 200:
                ip_data = ip_response.json()
                uniprot_data["interpro_matches"] = ip_data.get("results", [])
                self.logger.info(f"Fetched {len(uniprot_data['interpro_matches'])} InterPro entries for {uniprot_id}")
            else:
                self.logger.warning(f"InterPro API returned {ip_response.status_code} for {uniprot_id}")
                uniprot_data["interpro_matches"] = []
        except Exception as e:
            self.logger.warning(f"Failed to fetch InterPro data for {uniprot_id}: {e}")
            uniprot_data["interpro_matches"] = []
        
        # Cache
        try:
            cache_file.write_text(json.dumps(uniprot_data, indent=2), encoding="utf-8")
        except Exception:
            # Non-fatal: if cache write fails, continue
            pass

        self._log_uniprot_call(
            uniprot_id,
            source="network",
            status="success",
            latency_ms=self._elapsed_ms(request_start),
            extra={"cache_file": str(cache_file)},
        )
        
        return uniprot_data
    
    def _try_uniprot_id_mapping(self, obsolete_id: str) -> Optional[str]:
        """
        Try to map obsolete UniProt ID to current ID (Bug #2 fix)
        
        Uses UniProt ID Mapping service
        """
        url = "https://rest.uniprot.org/idmapping/run"
        try:
            # Submit mapping job
            response = requests.post(
                url,
                data={"from": "UniProtKB_AC-ID", "to": "UniProtKB", "ids": obsolete_id},
                timeout=10
            )
            response.raise_for_status()
            job_id = response.json()["jobId"]
            
            # Poll for results
            status_url = f"https://rest.uniprot.org/idmapping/status/{job_id}"
            for _ in range(10):  # Max 10 attempts
                time.sleep(1)
                status_response = requests.get(status_url, timeout=10)
                status_response.raise_for_status()
                status_data = status_response.json()
                
                if "results" in status_data:
                    results = status_data["results"]
                    if results and len(results) > 0:
                        new_id = results[0]["to"]
                        return new_id
                    else:
                        return None
            
            return None
        except Exception as e:
            self.logger.warning(f"ID mapping failed for {obsolete_id}: {e}")
            return None
    
    def _fetch_pdb(self, pdb_id: str) -> Dict[str, Any]:
        """Fetch PDB structure metadata and chain sequences"""
        cache_file = self.cache_dir / f"{self._cache_key(pdb_id)}_pdb.json"

        # Check cache (TTL-aware)
        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                return json.loads(valid_cache.read_text(encoding="utf-8"))
            except Exception as e:
                self.logger.warning(f"Failed to read cache for PDB {pdb_id}: {e}")
                try:
                    valid_cache.unlink()
                except Exception:
                    pass
        
        # Rate limit
        self._rate_limit_wait()
        
        # Fetch metadata from API
        url = f"{self.PDB_API}/{pdb_id}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        # Fetch FASTA sequences
        fasta_url = f"https://www.rcsb.org/fasta/entry/{pdb_id}"
        try:
            fasta_response = requests.get(fasta_url, timeout=10)
            if fasta_response.ok:
                data["fasta_raw"] = fasta_response.text
                data["chains"] = self._parse_pdb_fasta(fasta_response.text)
        except Exception as e:
            self.logger.warning(f"Could not fetch FASTA for {pdb_id}: {e}")
            data["chains"] = {}
        
        # NEW: Fetch UniProt mappings from polymer entities
        try:
            polymer_entity_ids = data.get("rcsb_entry_container_identifiers", {}).get("polymer_entity_ids", [])
            entity_uniprot_map = {}  # Maps entity_id -> uniprot_id
            
            for entity_id in polymer_entity_ids:
                entity_url = f"https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}"
                self._rate_limit_wait()
                entity_resp = requests.get(entity_url, timeout=10)
                
                if entity_resp.ok:
                    entity_data = entity_resp.json()
                    
                    # Get UniProt references
                    refs = entity_data.get("rcsb_polymer_entity_container_identifiers", {}).get("reference_sequence_identifiers", [])
                    
                    for ref in refs:
                        if ref.get("database_name") == "UniProt":
                            uniprot_id = ref.get("database_accession")
                            if uniprot_id:
                                entity_uniprot_map[entity_id] = uniprot_id
                                self.logger.info(f"Found UniProt {uniprot_id} for entity {entity_id}")
                                break
            
            # Map entities to chains and add uniprot_id
            for chain_id, chain_data in data.get("chains", {}).items():
                # Extract entity number from header (e.g., "6FBK_1|Chain A|..." -> entity_id="1")
                header = chain_data.get("header", "")
                import re
                entity_match = re.match(r'(\w+)_(\d+)\|', header)
                if entity_match:
                    entity_id = entity_match.group(2)
                    if entity_id in entity_uniprot_map:
                        chain_data["uniprot_id"] = entity_uniprot_map[entity_id]
                        self.logger.info(f"Chain {chain_id} -> UniProt {chain_data['uniprot_id']}")
        
        except Exception as e:
            self.logger.warning(f"Could not fetch entity UniProt mappings for {pdb_id}: {e}")
        
        # Cache
        try:
            cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
        
        return data
    
    def _parse_pdb_fasta(self, fasta_text: str) -> Dict[str, Dict[str, Any]]:
        """
        Parse PDB FASTA format to extract chain sequences
        
        Example input:
            >6FBK_1|Chain A|Serine/threonine-protein kinase WNK2|Homo sapiens (9606)
            SMAEDTGVRVELAEEDHGRKSTIALRLWVEDPKKLKGKPKDNGAIEFTFDLEKETPDEV
            AQEMIESGFFHESDVKIVAKSIRDRVALIQWRRERIWPA
            >6FBK_2|Chain B[auth P]|WNK1|Homo sapiens (9606)
            LTQVVHSAGRRFIVSPVPESRLR
        
        Returns:
            {
                "A": {
                    "sequence": "SMAEDTG...",
                    "protein": "WNK2",
                    "organism": "Homo sapiens",
                    "length": 95,
                    "header": "6FBK_1|Chain A|..."
                },
                "B": { ... }
            }
        """
        import re
        
        chains = {}
        current_chain_id = None
        current_auth_chain_id = None
        current_auth_chain_ids = []  # NEW: List of all author chain IDs for multi-unit structures
        current_sequence = []
        current_header = None
        
        for line in fasta_text.strip().split('\n'):
            if line.startswith('>'):
                # Save previous chain if exists
                if current_chain_id and current_sequence:
                    chains[current_chain_id] = {
                        "sequence": ''.join(current_sequence),
                        "length": len(''.join(current_sequence)),
                        "header": current_header,
                        "protein": chains[current_chain_id].get("protein", ""),
                        "organism": chains[current_chain_id].get("organism", ""),
                        "auth_chain_id": current_auth_chain_id or current_chain_id,
                        "auth_chain_ids": current_auth_chain_ids if current_auth_chain_ids else [current_auth_chain_id or current_chain_id]
                    }
                
                # Parse header: >6FBK_1|Chain A|Protein name|Organism (taxid)
                # OR >1FIN_1|Chains A, C|Protein name|...  (multi-unit)
                current_header = line[1:]  # Remove '>'
                parts = current_header.split('|')
                
                # Extract chain ID - handle multiple formats:
                # Format 1: "Chain A[auth E]" → label=A, auth=E
                # Format 2: "Chains A, C" → label=1, auth=[A, C] (multi-unit)
                # Format 3: "Chain A" → label=A, auth=A
                
                # First check for [auth X] notation
                auth_match = re.search(r'Chain\s+([A-Z])\s*\[auth ([A-Z])\]', current_header)
                if auth_match:
                    # Format 1: Single chain with explicit auth ID
                    current_chain_id = auth_match.group(1)
                    current_auth_chain_id = auth_match.group(2)
                    current_auth_chain_ids = [current_auth_chain_id]
                else:
                    # Check for "Chains A, C" (multi-unit) or "Chain A" (single)
                    chain_match = re.search(r'Chains?\s+([A-Z, ]+)', current_header)
                    if chain_match:
                        chain_str = chain_match.group(1)
                        all_chains = [ch.strip() for ch in chain_str.split(',')]
                        
                        if len(all_chains) > 1:
                            # Format 2: Multi-unit structure
                            # Use PDB_N format for label chain ID (e.g., "1", "2")
                            current_chain_id = parts[0].split('_')[-1] if '_' in parts[0] else parts[0]
                            current_auth_chain_id = all_chains[0]  # Primary author chain
                            current_auth_chain_ids = all_chains  # All author chains
                        else:
                            # Format 3: Single chain, no auth notation
                            current_chain_id = all_chains[0]
                            current_auth_chain_id = all_chains[0]
                            current_auth_chain_ids = [all_chains[0]]
                    else:
                        # Fallback: use PDB_N format
                        current_chain_id = parts[0].split('_')[-1] if '_' in parts[0] else parts[0]
                        current_auth_chain_id = current_chain_id
                        current_auth_chain_ids = [current_chain_id]
                
                # Extract protein name and organism if available
                protein_name = parts[2].strip() if len(parts) > 2 else ""
                organism_match = re.search(r'(.*?)\s*\((\d+)\)', parts[-1]) if len(parts) > 3 else None
                organism = organism_match.group(1).strip() if organism_match else ""
                
                # Initialize chain entry
                chains[current_chain_id] = {
                    "protein": protein_name,
                    "organism": organism,
                    "header": current_header,
                    "auth_chain_id": current_auth_chain_id,  # For PDBe API mapping (primary)
                    "auth_chain_ids": current_auth_chain_ids if current_auth_chain_ids else [current_auth_chain_id]  # All units
                }
                
                current_sequence = []
            else:
                # Accumulate sequence
                current_sequence.append(line.strip())
        
        # Save last chain
        if current_chain_id and current_sequence:
            chains[current_chain_id] = {
                "sequence": ''.join(current_sequence),
                "length": len(''.join(current_sequence)),
                "header": current_header,
                "protein": chains[current_chain_id].get("protein", ""),
                "organism": chains[current_chain_id].get("organism", ""),
                "auth_chain_id": current_auth_chain_id or current_chain_id,
                "auth_chain_ids": current_auth_chain_ids if current_auth_chain_ids else [current_auth_chain_id or current_chain_id]
            }
        
        return chains
    def _fuzzy_align(
        self,
        query_seq: str,
        target_seq: str,
        threshold: float = 0.90
    ) -> Tuple[int, float]:
        """
        Fuzzy alignment using sliding window.
        
        Handles PDB constructs that don't exact-match UniProt:
        - Truncations (N/C-terminal)
        - Expression tags (His, GST, MBP)
        - Point mutations for crystallization
        - Missing loops
        
        Args:
            query_seq: PDB sequence (shorter, fragment)
            target_seq: UniProt sequence (longer, full protein)
            threshold: Minimum identity (default 90%)
        
        Returns:
            (position, identity_percentage) or (-1, 0.0) if no match
        """
        best_pos = -1
        best_identity = 0.0
        query_len = len(query_seq)

        if query_len == 0:
            self.logger.warning("Fuzzy alignment called with empty query sequence")
            return (-1, 0.0)
        
        if query_len > len(target_seq):
            self.logger.warning(
                f"Query longer than target ({query_len} vs {len(target_seq)})"
            )
            return (-1, 0.0)
        
        # Sliding window search
        for i in range(len(target_seq) - query_len + 1):
            window = target_seq[i:i + query_len]
            matches = sum(a == b for a, b in zip(query_seq, window))
            identity = matches / query_len
            
            if identity > best_identity:
                best_identity = identity
                best_pos = i
        
        identity_pct = best_identity * 100.0
        
        if best_identity >= threshold:
            self.logger.info(
                f"Fuzzy alignment: {identity_pct:.1f}% identity at position {best_pos}"
            )
            return (best_pos, identity_pct)
        else:
            self.logger.warning(
                f"Best alignment only {identity_pct:.1f}% identity (threshold {threshold*100}%)"
            )
            return (-1, 0.0)
    
    def _fetch_pdb_ligands(self, pdb_id: str) -> List[Dict[str, Any]]:
        """
        Extract non-polymer entities (ligands, ions, cofactors) from PDB
        
        Returns list of ligands with ChEBI IDs when available:
        [
            {
                "ligand_id": "ATP",
                "name": "ADENOSINE-5'-TRIPHOSPHATE",
                "formula": "C10 H16 N5 O13 P3",
                "chebi_id": "CHEBI:15422",
                "type": "non-polymer"
            },
            ...
        ]
        """
        cache_file = self.cache_dir / f"pdb_ligands_{self._cache_key(pdb_id)}.json"

        # Check cache (TTL-aware)
        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                return json.loads(valid_cache.read_text(encoding="utf-8"))
            except Exception as e:
                self.logger.warning(f"Failed to read ligand cache for {pdb_id}: {e}")
                try:
                    valid_cache.unlink()
                except Exception:
                    pass
        
        ligands = []
        
        try:
            # Get entry info to find number of non-polymer entities
            url = f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            entry_data = response.json()
            
            # Get actual non-polymer entity IDs (not sequential!)
            entity_ids = entry_data.get("rcsb_entry_container_identifiers", {}).get("non_polymer_entity_ids", [])
            
            if not entity_ids:
                self.logger.info(f"No ligands found in {pdb_id}")
                return []
            
            self.logger.info(f"Found {len(entity_ids)} non-polymer entities in {pdb_id}: {entity_ids}")
            
            # Fetch each non-polymer entity
            for entity_id in entity_ids:
                entity_url = f"https://data.rcsb.org/rest/v1/core/nonpolymer_entity/{pdb_id}/{entity_id}"
                try:
                    resp = requests.get(entity_url, timeout=10)
                    if resp.status_code == 200:
                        entity_data = resp.json()
                        
                        # Extract basic info
                        ligand_id = entity_data.get("pdbx_entity_nonpoly", {}).get("comp_id")
                        if not ligand_id:
                            continue
                        
                        name = entity_data.get("pdbx_entity_nonpoly", {}).get("name", "")
                        formula = entity_data.get("chem_comp", {}).get("formula", "")
                        
                        # Map to ChEBI
                        chebi_id = self._map_ligand_to_chebi(ligand_id)

                        # Try to extract chemical descriptors from CCD (InChIKey/SMILES) for downstream enrichment
                        descriptors = self._extract_ccd_descriptors(ligand_id)

                        ligand_obj: Dict[str, Any] = {
                            "ligand_id": ligand_id,
                            "name": name,
                            "formula": formula,
                            "chebi_id": chebi_id,
                            "type": "non-polymer",
                        }
                        if descriptors:
                            ligand_obj.update(descriptors)
                        
                        # Optional PubChem enrichment (best-effort, cached)
                        if self.pubchem_enabled and len(ligands) < self.pubchem_max_ligands_per_pdb:
                            pubchem = self._enrich_ligand_with_pubchem(
                                ligand_id=ligand_id,
                                ligand_name=name,
                                inchi_key=descriptors.get("inchi_key") if isinstance(descriptors, dict) else None,
                                smiles=descriptors.get("smiles") if isinstance(descriptors, dict) else None,
                                inchi=descriptors.get("inchi") if isinstance(descriptors, dict) else None,
                            )
                            if pubchem:
                                ligand_obj["pubchem"] = pubchem
                                if pubchem.get("cid") is not None:
                                    ligand_obj["pubchem_cid"] = str(pubchem["cid"])

                        ligands.append(ligand_obj)
                        
                        self.logger.info(f"  Ligand {entity_id}: {ligand_id} ({name})")
                        if chebi_id:
                            self.logger.info(f"    ChEBI: {chebi_id}")
                        
                except Exception as e:
                    self.logger.warning(f"Failed to fetch entity {entity_id}: {e}")
                    continue
            
            # Cache results
            try:
                cache_file.write_text(json.dumps(ligands, indent=2), encoding="utf-8")
            except Exception:
                pass

            # Optional sidecar for PubChem enrichment details
            if self.pubchem_enabled and self.pubchem_write_sidecar_json:
                sidecar_name = f"{self.pubchem_sidecar_prefix}_{self._cache_key(pdb_id)}.json"
                sidecar_file = self.cache_dir / sidecar_name
                try:
                    sidecar_file.write_text(json.dumps(ligands, indent=2), encoding="utf-8")
                except Exception:
                    pass
            
        except Exception as e:
            self.logger.error(f"Failed to fetch ligands for {pdb_id}: {e}")
        
        return ligands
    
    def _map_ligand_to_chebi(self, ligand_id: str) -> Optional[str]:
        """
        Map PDB ligand ID to ChEBI accession
        
        Uses PDB Chemical Component Dictionary (CCD) which includes
        ChEBI cross-references for most common ligands.
        
        Returns:
            "CHEBI:15422" (for ATP) or None if not found
        """
        # Pre-mapped common ligands (for speed)
        COMMON_LIGANDS = {
            "ATP": "CHEBI:15422",
            "ADP": "CHEBI:456216",
            "GTP": "CHEBI:37565",
            "GDP": "CHEBI:17552",
            "MG": "CHEBI:18420",   # Magnesium ion
            "CA": "CHEBI:29108",   # Calcium ion
            "ZN": "CHEBI:29105",   # Zinc ion
            "FE": "CHEBI:18248",   # Iron ion
            "MN": "CHEBI:29035",   # Manganese ion
            "NAD": "CHEBI:57540",  # NAD+
            "FAD": "CHEBI:57692",  # FAD
            "HEM": "CHEBI:17627",  # Heme
            "PO4": "CHEBI:18367",  # Phosphate
        }
        
        # Check pre-mapped first
        if ligand_id in COMMON_LIGANDS:
            return COMMON_LIGANDS[ligand_id]
        
        # Query CCD for ChEBI cross-reference
        cache_file = self.cache_dir / f"ccd_{self._cache_key(ligand_id)}.json"
        
        try:
            ccd_data = self._fetch_ccd(ligand_id, cache_file=cache_file)
            if not ccd_data:
                return None
            
            # Look for ChEBI in pdbx_reference_molecule
            if "pdbx_reference_molecule" in ccd_data:
                for ref in ccd_data["pdbx_reference_molecule"]:
                    if ref.get("resource_name") == "ChEBI":
                        chebi_code = ref.get("resource_accession_code", "")
                        # Format: CHEBI:15422
                        if chebi_code and not chebi_code.startswith("CHEBI:"):
                            chebi_code = f"CHEBI:{chebi_code}"
                        return chebi_code
            
            return None
            
        except Exception as e:
            self.logger.warning(f"Failed to map {ligand_id} to ChEBI: {e}")
            return None

    def _fetch_ccd(self, ligand_id: str, *, cache_file: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        """Fetch PDB CCD entry for a ligand (cached, best-effort)."""
        if cache_file is None:
            cache_file = self.cache_dir / f"ccd_{self._cache_key(ligand_id)}.json"

        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                return json.loads(valid_cache.read_text(encoding="utf-8"))
            except Exception:
                try:
                    valid_cache.unlink()
                except Exception:
                    pass

        url = f"https://data.rcsb.org/rest/v1/core/chemcomp/{ligand_id}"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                return None
            ccd_data = response.json()
            try:
                cache_file.write_text(json.dumps(ccd_data, indent=2), encoding="utf-8")
            except Exception:
                pass
            return ccd_data
        except Exception as e:
            self.logger.warning("Failed to fetch CCD for %s: %s", ligand_id, e)
            return None

    @staticmethod
    def _pick_first_str(value: Any) -> Optional[str]:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _extract_ccd_descriptors(self, ligand_id: str) -> Dict[str, str]:
        """Extract InChI / InChIKey / SMILES from CCD if present."""
        ccd = self._fetch_ccd(ligand_id)
        if not isinstance(ccd, dict):
            return {}

        desc = ccd.get("rcsb_chem_comp_descriptor")
        if not isinstance(desc, dict):
            desc = {}

        # Try multiple key spellings; CCD payloads vary.
        inchi_key = (
            self._pick_first_str(desc.get("InChIKey"))
            or self._pick_first_str(desc.get("inchi_key"))
            or self._pick_first_str(desc.get("inchiKey"))
        )
        inchi = (
            self._pick_first_str(desc.get("InChI"))
            or self._pick_first_str(desc.get("inchi"))
        )
        smiles = (
            self._pick_first_str(desc.get("SMILES"))
            or self._pick_first_str(desc.get("smiles"))
            or self._pick_first_str(desc.get("SMILES_CANONICAL"))
            or self._pick_first_str(desc.get("canonical_smiles"))
        )

        out: Dict[str, str] = {}
        if inchi_key:
            out["inchi_key"] = inchi_key
        if inchi:
            out["inchi"] = inchi
        if smiles:
            out["smiles"] = smiles
        return out

    def _pubchem_rate_limit_wait(self):
        elapsed = time.time() - self._pubchem_last_request_time
        if elapsed < self.pubchem_rate_limit:
            time.sleep(self.pubchem_rate_limit - elapsed)
        self._pubchem_last_request_time = time.time()

    @staticmethod
    def _pubchem_parse_throttling_control(header_value: str) -> Dict[str, str]:
        """Parse PubChem's X-Throttling-Control header (best-effort)."""
        if not isinstance(header_value, str) or not header_value.strip():
            return {}
        # Example:
        # X-Throttling-Control: Request Count status: Green (0%), Request Time status: Green (0%), Service status: Green (20%)
        statuses: Dict[str, str] = {}
        patterns = {
            "request_count": r"Request Count status:\s*([A-Za-z]+)",
            "request_time": r"Request Time status:\s*([A-Za-z]+)",
            "service": r"Service status:\s*([A-Za-z]+)",
        }
        for key, pat in patterns.items():
            match = re.search(pat, header_value)
            if match:
                statuses[key] = match.group(1).strip().lower()
        return statuses

    def _pubchem_suggest_extra_delay(self, throttling_statuses: Dict[str, str]) -> float:
        """Return additional delay based on PubChem throttling/service status.

        Conservative policy:
        - If any status is yellow/red, slow down a bit.
        - If any status is black, disable PubChem for this generator instance.
        """
        if not self.pubchem_dynamic_throttling or not throttling_statuses:
            return 0.0

        values = set(v for v in throttling_statuses.values() if isinstance(v, str))
        if "black" in values:
            # Avoid repeated calls when PubChem is actively blocking this client/IP.
            self.logger.warning(
                "PubChem throttling status BLACK (blocked). Disabling PubChem enrichment for this run. Header=%s",
                throttling_statuses,
            )
            self.pubchem_enabled = False
            return 0.0

        # Base delay derived from our configured baseline.
        delay = 0.0
        if "red" in values:
            delay = max(delay, self.pubchem_rate_limit * 2.0)
        elif "yellow" in values:
            delay = max(delay, self.pubchem_rate_limit * 1.0)

        # If service is busy/overloaded, add a bit more.
        service = throttling_statuses.get("service")
        if service in {"red", "black"}:
            delay = max(delay, self.pubchem_rate_limit * 2.0)
        elif service == "yellow":
            delay = max(delay, self.pubchem_rate_limit * 1.0)

        return float(delay)

    def _pubchem_retry_sleep(self, attempt: int, *, retry_after: Optional[str] = None) -> None:
        wait_s: Optional[float] = None
        if retry_after:
            try:
                wait_s = float(retry_after)
            except Exception:
                wait_s = None
        if wait_s is None:
            wait_s = self.pubchem_backoff_base_seconds * (2 ** max(attempt, 0))
        wait_s = float(wait_s) + random.uniform(0, max(self.pubchem_jitter_seconds, 0.0))
        time.sleep(wait_s)

    def _pubchem_get_json(self, url: str, *, cache_name: str) -> Optional[Dict[str, Any]]:
        cache_file = self.cache_dir / f"pubchem_{self._cache_key(cache_name)}.json"
        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                return json.loads(valid_cache.read_text(encoding="utf-8"))
            except Exception:
                try:
                    valid_cache.unlink()
                except Exception:
                    pass

        if not self.pubchem_enabled:
            return None

        last_error: Optional[str] = None
        for attempt in range(max(self.pubchem_max_retries, 1)):
            try:
                self._pubchem_rate_limit_wait()
                resp = requests.get(url, timeout=self.pubchem_timeout_seconds)

                throttling = self._pubchem_parse_throttling_control(resp.headers.get("X-Throttling-Control", ""))
                extra_delay = self._pubchem_suggest_extra_delay(throttling)
                if extra_delay > 0:
                    time.sleep(extra_delay)

                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                    return data

                # PubChem signals overload/too many requests with 503; be polite and retry.
                if resp.status_code in (429, 503, 504):
                    last_error = f"HTTP {resp.status_code}"
                    self.logger.warning("PubChem %s for %s (attempt %s/%s)", resp.status_code, url, attempt + 1, self.pubchem_max_retries)
                    self._pubchem_retry_sleep(attempt, retry_after=resp.headers.get("Retry-After"))
                    continue

                # Other errors are treated as non-retryable (best-effort).
                last_error = f"HTTP {resp.status_code}"
                return None
            except Exception as e:
                last_error = str(e)
                if attempt == max(self.pubchem_max_retries, 1) - 1:
                    break
                self.logger.warning("PubChem request error (%s): %s (attempt %s/%s)", url, e, attempt + 1, self.pubchem_max_retries)
                self._pubchem_retry_sleep(attempt)

        self.logger.warning("PubChem request failed after retries (%s): %s", url, last_error)
        return None

    def _pubchem_resolve_cid(
        self,
        *,
        inchi_key: Optional[str],
        inchi: Optional[str],
        smiles: Optional[str],
        name: Optional[str],
    ) -> Optional[int]:
        """Resolve a PubChem CID from the best available identifier."""
        base = self.pubchem_api_base

        def parse_cid(payload: Optional[Dict[str, Any]]) -> Optional[int]:
            if not payload:
                return None
            ident = payload.get("IdentifierList") if isinstance(payload, dict) else None
            if not isinstance(ident, dict):
                return None
            cids = ident.get("CID")
            if isinstance(cids, list) and cids:
                try:
                    return int(cids[0])
                except Exception:
                    return None
            return None

        if inchi_key:
            url = f"{base}/compound/inchikey/{quote(inchi_key)}/cids/JSON"
            return parse_cid(self._pubchem_get_json(url, cache_name=f"cid_inchikey_{inchi_key}"))

        if inchi:
            url = f"{base}/compound/inchi/{quote(inchi)}/cids/JSON"
            return parse_cid(self._pubchem_get_json(url, cache_name=f"cid_inchi_{inchi[:32]}"))

        if smiles:
            url = f"{base}/compound/smiles/{quote(smiles)}/cids/JSON"
            return parse_cid(self._pubchem_get_json(url, cache_name=f"cid_smiles_{smiles[:32]}"))

        if name:
            url = f"{base}/compound/name/{quote(name)}/cids/JSON"
            return parse_cid(self._pubchem_get_json(url, cache_name=f"cid_name_{name[:64]}"))

        return None

    def _pubchem_fetch_properties(self, cid: int) -> Dict[str, Any]:
        base = self.pubchem_api_base
        fields = [f for f in self.pubchem_property_fields if isinstance(f, str) and f.strip()]
        if not fields:
            return {}

        url = f"{base}/compound/cid/{cid}/property/{quote(','.join(fields))}/JSON"
        payload = self._pubchem_get_json(url, cache_name=f"props_{cid}_{'_'.join(fields)}")
        if not payload:
            return {}
        prop_table = payload.get("PropertyTable")
        if not isinstance(prop_table, dict):
            return {}
        props = prop_table.get("Properties")
        if isinstance(props, list) and props:
            return props[0] if isinstance(props[0], dict) else {}
        return {}

    def _pubchem_fetch_synonyms(self, cid: int) -> List[str]:
        if not self.pubchem_include_synonyms:
            return []
        base = self.pubchem_api_base
        url = f"{base}/compound/cid/{cid}/synonyms/JSON"
        payload = self._pubchem_get_json(url, cache_name=f"syn_{cid}")
        if not payload:
            return []
        info = payload.get("InformationList")
        if not isinstance(info, dict):
            return []
        infos = info.get("Information")
        if isinstance(infos, list) and infos:
            syns = infos[0].get("Synonym") if isinstance(infos[0], dict) else None
            if isinstance(syns, list):
                return [s for s in syns if isinstance(s, str) and s.strip()]
        return []

    def _enrich_ligand_with_pubchem(
        self,
        *,
        ligand_id: str,
        ligand_name: str,
        inchi_key: Optional[str],
        smiles: Optional[str],
        inchi: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Best-effort PubChem enrichment (CID + selected properties)."""
        try:
            cid = self._pubchem_resolve_cid(
                inchi_key=inchi_key,
                inchi=inchi,
                smiles=smiles,
                name=ligand_name or ligand_id,
            )
            if cid is None:
                return None

            props = self._pubchem_fetch_properties(cid)
            synonyms = self._pubchem_fetch_synonyms(cid)
            out: Dict[str, Any] = {
                "cid": cid,
                "properties": props,
            }
            if synonyms:
                out["synonyms"] = synonyms
            return out
        except Exception as e:
            self.logger.warning("PubChem enrichment failed for %s: %s", ligand_id, e)
            return None
    
    def _fetch_secondary_structure(self, pdb_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Extract secondary structure elements (helices, sheets) from PDB
        
        Uses PDBe REST API for secondary structure assignments.
        
        Returns:
            {
                "A": [
                    {"type": "helix", "start": 10, "end": 25},
                    {"type": "sheet", "start": 50, "end": 58, "strand_id": 1}
                ],
                "B": [...]
            }
        """
        cache_file = self.cache_dir / f"pdb_secstruct_{pdb_id}.json"
        
        # Check cache
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
        
        secondary_structure = {}
        
        try:
            # PDBe secondary structure API
            url = f"https://www.ebi.ac.uk/pdbe/api/pdb/entry/secondary_structure/{pdb_id.lower()}"
            response = requests.get(url, timeout=15)
            
            if response.status_code != 200:
                self.logger.warning(f"Secondary structure API returned {response.status_code}")
                return {}
            
            data = response.json()
            pdb_data = data.get(pdb_id.lower(), {})
            
            if not pdb_data:
                return {}
            
            # Parse molecules
            for molecule in pdb_data.get("molecules", []):
                for chain_data in molecule.get("chains", []):
                    chain_id = chain_data.get("chain_id")
                    if not chain_id:
                        continue
                    
                    elements = []
                    
                    # Extract helices
                    sec_struct = chain_data.get("secondary_structure", {})
                    for helix in sec_struct.get("helices", []):
                        start = helix.get("start", {}).get("residue_number")
                        end = helix.get("end", {}).get("residue_number")
                        if start and end:
                            elements.append({
                                "type": "helix",
                                "start": start,
                                "end": end
                            })
                    
                    # Extract strands (beta sheets)
                    for strand in sec_struct.get("strands", []):
                        start = strand.get("start", {}).get("residue_number")
                        end = strand.get("end", {}).get("residue_number")
                        strand_id = strand.get("sheet_id", 1)
                        if start and end:
                            elements.append({
                                "type": "sheet",
                                "start": start,
                                "end": end,
                                "strand_id": strand_id
                            })
                    
                    if elements:
                        secondary_structure[chain_id] = elements
                        self.logger.info(
                            f"  Chain {chain_id}: {len([e for e in elements if e['type']=='helix'])} helices, "
                            f"{len([e for e in elements if e['type']=='sheet'])} strands"
                        )
            
            # Cache results
            with open(cache_file, 'w') as f:
                json.dump(secondary_structure, f, indent=2)
            
        except Exception as e:
            self.logger.error(f"Failed to fetch secondary structure for {pdb_id}: {e}")
        
        return secondary_structure
    
    def _fetch_binding_sites(self, pdb_id: str) -> Dict[str, Any]:
        """
        Extract binding sites from PDBe API
        
        Returns dict with ligand binding sites and potential PPI interfaces.
        Each site contains residue-level contact information.
        
        Returns:
            {
                "ligand_sites": [
                    {
                        "site_id": "AC1",
                        "details": "ATP binding site",
                        "residues": [10, 15, 20, 25]
                    }
                ],
                "interface_sites": [...]  # Potential protein-protein interfaces
            }
        """
        cache_file = self.cache_dir / f"pdb_binding_{pdb_id}.json"
        
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
        
        binding_data = {"ligand_sites": [], "interface_sites": []}
        
        try:
            url = f"https://www.ebi.ac.uk/pdbe/api/pdb/entry/binding_sites/{pdb_id.lower()}"
            response = requests.get(url, timeout=15)
            
            if response.status_code != 200:
                self.logger.warning(f"Binding sites API returned {response.status_code}")
                return binding_data
            
            data = response.json()
            pdb_data = data.get(pdb_id.lower(), {})
            
            if not pdb_data:
                return binding_data
            
            # Process binding sites
            for site in pdb_data:
                site_id = site.get("site_id", "unknown")
                details = site.get("details", "")
                evidence = site.get("evidence_code", "")
                
                # Extract residue numbers per chain
                residues_by_chain = {}
                for residue in site.get("site_residues", []):
                    chain_id = residue.get("author_insertion_code") or residue.get("chain_id")
                    res_num = residue.get("author_residue_number") or residue.get("residue_number")
                    
                    if chain_id and res_num:
                        if chain_id not in residues_by_chain:
                            residues_by_chain[chain_id] = []
                        residues_by_chain[chain_id].append(res_num)
                
                # Classify as ligand site or potential PPI interface
                site_info = {
                    "site_id": site_id,
                    "details": details,
                    "evidence": evidence,
                    "residues_by_chain": residues_by_chain
                }
                
                # If multiple chains involved, likely PPI interface
                if len(residues_by_chain) > 1:
                    binding_data["interface_sites"].append(site_info)
                else:
                    binding_data["ligand_sites"].append(site_info)
            
            # Cache results
            with open(cache_file, 'w') as f:
                json.dump(binding_data, f, indent=2)
            
            self.logger.info(
                f"  Binding sites: {len(binding_data['ligand_sites'])} ligand, "
                f"{len(binding_data['interface_sites'])} interface"
            )
            
        except Exception as e:
            self.logger.error(f"Failed to fetch binding sites for {pdb_id}: {e}")
        
        return binding_data
    
    def _detect_ppi_interfaces(self, chains: Dict[str, Dict], binding_data: Dict) -> List[Dict[str, Any]]:
        """
        Detect protein-protein interfaces from chain data and binding sites
        
        Strategy:
        1. Check if multiple chains present (simplest PPI indicator)
        2. Use binding site data to identify interface residues
        3. Return list of interface definitions
        
        Returns:
            [
                {
                    "chains": ["A", "B"],
                    "type": "hetero-dimer",
                    "binding_site_id": "AC1",
                    "residues": {"A": [10, 15, 20], "B": [5, 8, 12]}
                }
            ]
        """
        interfaces = []
        
        # Build mapping: auth_chain_id -> label_chain_id
        # Support multi-unit structures where one label maps to multiple auth chains
        auth_to_label = {}
        for label_id, chain_data in chains.items():
            # Get all author chain IDs (supports multi-unit structures)
            auth_ids = chain_data.get("auth_chain_ids", [chain_data.get("auth_chain_id", label_id)])
            for auth_id in auth_ids:
                auth_to_label[auth_id] = label_id
        
        # Simple case: multi-chain structure
        if len(chains) > 1:
            chain_ids = sorted(chains.keys())
            
            # Check for interface sites from binding data
            for site in binding_data.get("interface_sites", []):
                auth_chains = sorted(site["residues_by_chain"].keys())
                
                # Map author chain IDs back to label chain IDs
                label_chains = [auth_to_label.get(auth_id, auth_id) for auth_id in auth_chains]
                mapped_residues = {
                    auth_to_label.get(auth_id, auth_id): residues
                    for auth_id, residues in site["residues_by_chain"].items()
                }
                
                if len(label_chains) >= 2:
                    interface = {
                        "chains": sorted(set(label_chains)),
                        "type": "hetero-oligomer" if len(set(label_chains)) > 1 else "homo-oligomer",
                        "binding_site_id": site["site_id"],
                        "details": site.get("details", ""),
                        "residues": mapped_residues
                    }
                    interfaces.append(interface)
            
            # If no explicit interface sites but multiple chains, create basic interface
            if not interfaces and len(chain_ids) >= 2:
                interfaces.append({
                    "chains": chain_ids,
                    "type": "multi-chain complex",
                    "binding_site_id": None,
                    "details": f"Multi-chain structure with {len(chain_ids)} chains",
                    "residues": {}
                })
        
        return interfaces
    
    def _classify_kinase_state(
        self,
        sequence: str,
        ligands: List[Dict[str, Any]],
        ptms: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Classify kinase conformational state using structural heuristics
        
        Heuristics:
        1. ATP/ADP/Analog binding → likely active/competent state
        2. DFG motif presence → structural competence
        3. Activation loop phosphorylation → active state marker
        4. Inhibitor binding → inactive/inhibited state
        
        Returns:
            {
                "state": "active" | "inactive" | "inhibited" | "apo" | "unknown",
                "confidence": "high" | "medium" | "low",
                "atp_bound": bool,
                "dfg_motif": bool,
                "activation_phosphorylation": bool,
                "inhibitor_bound": bool
            }
        """
        # Common ATP/ADP analogs and inhibitors
        ATP_LIKE = {"ATP", "ADP", "ANP", "AMP", "ACP", "AGS", "AMP-PNP", "AMPPNP"}
        KNOWN_INHIBITORS = {
            "STU", "STA", "BI2", "KIN", "ANP",  # Generic kinase inhibitors
            "WNK", "463", "5FJ",  # WNK-specific (5FJ is WNK463)
            "MEK", "PD0", "AZD", "GSK",  # Named inhibitors
        }
        
        # Check ATP/analog binding
        atp_bound = False
        inhibitor_bound = False
        
        for lig in ligands:
            lig_id = lig.get("ligand_id", "")
            lig_name = lig.get("name", "").upper()
            
            if lig_id in ATP_LIKE:
                atp_bound = True
            
            # Check if ligand is an inhibitor
            if any(inhib in lig_id or inhib in lig_name for inhib in KNOWN_INHIBITORS):
                inhibitor_bound = True
        
        # Check DFG motif (Asp-Phe-Gly in activation segment)
        # Typical location: around 30% into kinase domain
        has_dfg_motif = "DFG" in sequence
        dfg_position = sequence.find("DFG") if has_dfg_motif else -1
        
        # Check activation loop phosphorylation
        # Typically in region 160-180 for many kinases
        activation_ptms = [
            ptm for ptm in ptms
            if ptm.get("type") == "phosphorylation" and
               ptm.get("position", 0) > 0  # Has valid position
        ]
        
        # Classify state based on heuristics
        if inhibitor_bound:
            state = "inhibited"
            confidence = "high" if atp_bound else "medium"
        elif atp_bound and has_dfg_motif and activation_ptms:
            state = "active"
            confidence = "high"
        elif atp_bound and has_dfg_motif:
            state = "active"
            confidence = "medium"
        elif atp_bound:
            state = "active"
            confidence = "low"
        elif has_dfg_motif and activation_ptms:
            state = "inactive_dfg_in"
            confidence = "medium"
        elif len(ligands) == 0:
            state = "apo"
            confidence = "medium"
        else:
            state = "unknown"
            confidence = "low"
        
        self.logger.info(
            f"Conformational state: {state} (confidence: {confidence}) - "
            f"ATP: {atp_bound}, DFG: {has_dfg_motif}, Phospho: {len(activation_ptms)}, "
            f"Inhibitor: {inhibitor_bound}"
        )
        
        return {
            "state": state,
            "confidence": confidence,
            "atp_bound": atp_bound,
            "dfg_motif": has_dfg_motif,
            "dfg_position": dfg_position,
            "activation_phosphorylation": len(activation_ptms) > 0,
            "phosphorylation_count": len(activation_ptms),
            "inhibitor_bound": inhibitor_bound
        }
    
    def _map_pdb_to_uniprot(
        self,
        pdb_sequence: str,
        protein_name: str,
        organism: str = "Homo sapiens",
        uniprot_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Map PDB fragment to UniProt and extract relevant annotations
        
        Strategy:
        1. If uniprot_id provided (from PDB API), use it directly
        2. Otherwise, search UniProt for protein by name + organism
        3. Align PDB sequence to UniProt sequence
        4. Extract domains/PTMs that overlap with PDB fragment
        
        Args:
            pdb_sequence: Amino acid sequence from PDB
            protein_name: Protein name from PDB
            organism: Organism name
            uniprot_id: Optional UniProt accession from PDB API (preferred)
        
        Returns:
            {
                "uniprot_id": "Q9H4A3",
                "gene_name": "WNK1",
                "full_sequence": "MSGGAA...",
                "alignment": {
                    "pdb_start": 1,
                    "pdb_end": 23,
                    "uniprot_start": 1795,
                    "uniprot_end": 1817
                },
                "domains": [...],  # Domains overlapping PDB region
                "ptms": [...],     # PTMs in PDB region
                "binding_sites": [...],
                "motifs": [...]
            }
        """
        try:
            # NEW: If UniProt ID provided from PDB API, use it directly
            if uniprot_id:
                self.logger.info(f"Using UniProt ID {uniprot_id} from PDB API")
                gene_name = ""
                
                # Fetch full UniProt data
                uniprot_data = self._fetch_uniprot(uniprot_id)
                
                # Extract gene name
                genes = uniprot_data.get("genes", [])
                if genes and isinstance(genes, list) and len(genes) > 0:
                    gene_obj = genes[0]
                    if isinstance(gene_obj, dict):
                        gene_name_obj = gene_obj.get("geneName", {})
                        if isinstance(gene_name_obj, dict):
                            gene_name = gene_name_obj.get("value", "")
                        elif isinstance(gene_name_obj, str):
                            gene_name = gene_name_obj
                    elif isinstance(gene_obj, str):
                        gene_name = gene_obj
            else:
                # Fallback: Search UniProt by protein name
                self._rate_limit_wait()
                search_url = f"{self.UNIPROT_API}/search"
                params = {
                    "query": f'(protein_name:"{protein_name}") AND (organism_name:"{organism}")',
                    "format": "json",
                    "size": 1
                }
                
                response = requests.get(search_url, params=params)
                if not response.ok:
                    self.logger.warning(f"UniProt search failed for {protein_name}")
                    return None
                
                results = response.json().get("results", [])
                if not results:
                    self.logger.warning(f"No UniProt match for {protein_name}")
                    return None
                
                # Get first result
                entry = results[0]
                uniprot_id = entry.get("primaryAccession")
                
                # Handle different gene name formats
                genes = entry.get("genes", [])
                if genes and isinstance(genes, list) and len(genes) > 0:
                    gene_obj = genes[0]
                    if isinstance(gene_obj, dict):
                        gene_name_obj = gene_obj.get("geneName", {})
                        if isinstance(gene_name_obj, dict):
                            gene_name = gene_name_obj.get("value", "")
                        elif isinstance(gene_name_obj, str):
                            gene_name = gene_name_obj
                        else:
                            gene_name = ""
                    elif isinstance(gene_obj, str):
                        gene_name = gene_obj
                    else:
                        gene_name = ""
                else:
                    gene_name = ""
                
                self.logger.info(f"Matched PDB fragment to UniProt {uniprot_id} ({gene_name})")
                
                # Fetch full UniProt data
                uniprot_data = self._fetch_uniprot(uniprot_id)
            
            # Extract the full UniProt sequence (handle both dict and string formats)
            seq_field = uniprot_data.get("sequence", "")
            if isinstance(seq_field, dict):
                full_sequence = seq_field.get("value", "")
            elif isinstance(seq_field, str):
                full_sequence = seq_field
            else:
                full_sequence = ""
            
            if not full_sequence:
                self.logger.warning(f"No sequence found in UniProt {uniprot_id}")
                return None
            
            # Align PDB sequence to UniProt sequence
            pdb_seq = pdb_sequence.upper()
            full_seq = full_sequence.upper()
            
            # Try exact match first (fastest)
            alignment_start = full_seq.find(pdb_seq)
            alignment_identity = 100.0  # Exact match
            
            if alignment_start == -1:
                # Try fuzzy alignment with sliding window
                self.logger.info(f"Exact match failed, trying fuzzy alignment...")
                alignment_start, alignment_identity = self._fuzzy_align(pdb_seq, full_seq)
                
                if alignment_start == -1:
                    self.logger.warning(
                        f"No alignment found for {protein_name} "
                        f"(PDB: {len(pdb_seq)} aa, UniProt: {len(full_seq)} aa)"
                    )
                    return None
                
                self.logger.info(
                    f"Fuzzy alignment: {alignment_identity:.1f}% identity at position {alignment_start + 1}"
                )
            
            alignment_end = alignment_start + len(pdb_seq)
            
            self.logger.info(
                f"Aligned PDB fragment: UniProt positions {alignment_start + 1}-{alignment_end}"
            )
            
            # Extract domains that overlap with PDB region
            all_domains = self._extract_domains(uniprot_data)

            def _domain_overlaps_fragment(dom: Dict[str, Any]) -> bool:
                """Conservatively keep domains that truly sit in the fragment."""
                overlap_start = max(dom["start"], alignment_start + 1)
                overlap_end = min(dom["end"], alignment_end)
                if overlap_start > overlap_end:
                    return False

                overlap_len = overlap_end - overlap_start + 1
                domain_len = dom["end"] - dom["start"] + 1
                fragment_len = len(pdb_seq)

                # Require meaningful overlap: at least 20 aa and ~20% of the domain/fragment,
                # or keep if the domain center lies inside the fragment (helps CCT-sized hits).
                min_overlap = max(20, int(0.2 * domain_len), int(0.2 * fragment_len))
                center_in_fragment = alignment_start + 1 <= (dom["start"] + dom["end"]) / 2 <= alignment_end
                return overlap_len >= min_overlap or center_in_fragment

            overlapping_domains = [d for d in all_domains if _domain_overlaps_fragment(d)]
            
            # Extract PTMs in PDB region
            all_ptms = self._extract_ptms(uniprot_data)
            overlapping_ptms = [
                ptm for ptm in all_ptms
                if alignment_start + 1 <= ptm["position"] <= alignment_end
            ]
            
            # Adjust positions to PDB coordinates
            for domain in overlapping_domains:
                domain["pdb_start"] = max(1, domain["start"] - alignment_start)
                domain["pdb_end"] = min(len(pdb_seq), domain["end"] - alignment_start)
            
            for ptm in overlapping_ptms:
                ptm["pdb_position"] = ptm["position"] - alignment_start
            
            # Extract binding sites
            binding_sites = self._extract_binding_sites(uniprot_data)
            overlapping_binding = [
                site for site in binding_sites
                if any(alignment_start + 1 <= r <= alignment_end for r in site.get("residues", []))
            ]
            
            # Adjust binding site positions
            for site in overlapping_binding:
                site["pdb_residues"] = [
                    r - alignment_start for r in site.get("residues", [])
                    if alignment_start + 1 <= r <= alignment_end
                ]
            
            # Extract motifs
            motifs = self._extract_motifs(uniprot_data)
            overlapping_motifs = [
                m for m in motifs
                if not (m["end"] < alignment_start + 1 or m["start"] > alignment_end)
            ]
            
            for motif in overlapping_motifs:
                motif["pdb_start"] = max(1, motif["start"] - alignment_start)
                motif["pdb_end"] = min(len(pdb_seq), motif["end"] - alignment_start)
            
            # NEW: Return BOTH full protein domains AND fragment-specific annotations
            return {
                "uniprot_id": uniprot_id,
                "gene_name": gene_name,
                "full_sequence": full_sequence,
                "full_protein_length": len(full_sequence),
                "alignment": {
                    "pdb_start": 1,
                    "pdb_end": len(pdb_seq),
                    "uniprot_start": alignment_start + 1,
                    "uniprot_end": alignment_end
                },
                # All domains from full protein (UniProt coords)
                "all_domains": all_domains,
                "all_ptms": all_ptms,
                # Only annotations overlapping with PDB fragment (PDB coords)
                "domains": overlapping_domains,
                "ptms": overlapping_ptms,
                "binding_sites": overlapping_binding,
                "motifs": overlapping_motifs
            }
            
        except Exception as e:
            self.logger.error(f"Error mapping PDB to UniProt: {e}")
            return None
    
    def _extract_ptms(self, uniprot_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract PTMs from UniProt features"""
        ptms = []
        for feature in uniprot_data.get("features", []):
            # Some UniProt entries may have non-dict features (corrupt cache or unexpected API shape)
            if not isinstance(feature, dict):
                continue

            feature_type = feature.get("type", "")

            if feature_type in ["Modified residue", "Cross-link", "Glycosylation", "Lipidation"]:
                # Extract position
                location = feature.get("location", {})
                # location may be nested dicts in different schemas
                start = None
                if isinstance(location, dict):
                    start = location.get("start", {})
                position = None
                if isinstance(start, dict):
                    position = start.get("value")

                if position is None:
                    continue

                # Extract modification type
                description = feature.get("description", "")
                ptm_type = self._infer_ptm_type(description)

                # Extract residue
                residue = ""
                seq = uniprot_data.get("sequence", "") or ""
                if isinstance(position, int) and position <= len(seq):
                    residue = seq[position - 1]

                evidence = ""
                evidences = feature.get("evidences") if isinstance(feature.get("evidences"), list) else []
                if evidences:
                    src = evidences[0].get("source", {}) if isinstance(evidences[0], dict) else {}
                    evidence = src.get("name", "") if isinstance(src, dict) else ""

                # VALIDATE PTM-residue compatibility (Bug #3 fix)
                if not self._validate_ptm(ptm_type, residue):
                    continue  # Skip invalid PTM

                ptms.append({
                    "ptm_id": f"ptm_{position}",
                    "ptm_type": ptm_type,
                    "residue": residue,
                    "position": position,
                    "status": "present",
                    "description": description,
                    "evidence": evidence,
                })
        
        # Deduplicate PTMs by position (keep first occurrence)
        seen_positions = set()
        unique_ptms = []
        for ptm in ptms:
            if ptm["position"] not in seen_positions:
                unique_ptms.append(ptm)
                seen_positions.add(ptm["position"])
        
        return unique_ptms
    
    def _extract_domains(self, uniprot_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract domains from UniProt features
        
        Includes:
        - Domain: structural/functional domains
        - Region: functional regions
        - Repeat: repeat sequences
        - Motif: short functional motifs (e.g., RFXV motif)
        """
        domains = []
        for feature in uniprot_data.get("features", []):
            if not isinstance(feature, dict):
                continue

            feature_type = feature.get("type", "")

            # Include Domain/Region/Repeat (Motifs are handled separately by _extract_motifs)
            # Also include other structural features
            if feature_type in ["Domain", "Region", "Repeat", "Coiled coil", "Zinc finger", "Compositional bias"]:
                location = feature.get("location", {})
                start = 1
                end = len(uniprot_data.get("sequence", "") or "")
                if isinstance(location, dict):
                    s = location.get("start", {})
                    e = location.get("end", {})
                    if isinstance(s, dict):
                        start = s.get("value", start)
                    if isinstance(e, dict):
                        end = e.get("value", end)

                domains.append({
                    "domain_id": feature.get("description", "unknown"),
                    "domain_name": feature.get("description", "unknown"),
                    "domain_type": feature_type.lower(),
                    "start": start,
                    "end": end,
                })

        # Process InterPro Matches (Real coordinates from InterPro API)
        # This replaces the previous logic that used full sequence length
        for match in uniprot_data.get("interpro_matches", []):
            metadata = match.get("metadata", {})
            ipr_id = metadata.get("accession")
            ipr_name = metadata.get("name")
            ipr_type = metadata.get("type", "domain")
            
            # Get coordinates
            for protein in match.get("proteins", []):
                # The API returns matches for the requested protein
                for location in protein.get("entry_protein_locations", []):
                    for fragment in location.get("fragments", []):
                        start = fragment.get("start")
                        end = fragment.get("end")
                        
                        if start and end:
                            domains.append({
                                "domain_id": ipr_id,
                                "domain_name": f"{ipr_name} (InterPro)",
                                "domain_type": ipr_type,
                                "start": start,
                                "end": end,
                            })
        
        return domains
    
    def _extract_binding_sites(self, uniprot_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract binding sites from UniProt features with FULL experimental evidence
        
        Maps UniProt feature types to NeSy site types:
        - "Binding site" → infer from description (ATP, DNA, etc.)
        - "Active site" → "catalytic"
        - "Metal binding" → "ion-binding" with ion_type
        - "Nucleotide binding" → "ATP-binding" or "GTP-binding"
        - "DNA binding" → "DNA-binding"
        
        Evidence captured:
        - PubMed IDs (experimental validation)
        - PDB IDs (structural evidence)
        - ChEBI IDs (ligand identity)
        - ECO codes (evidence type)
        """
        binding_sites = []
        
        # Group binding residues by site type
        site_groups = {}  # {site_key: {type, residues, params, evidence}}
        
        for feature in uniprot_data.get("features", []):
            if not isinstance(feature, dict):
                continue
            
            feature_type = feature.get("type", "")
            description = feature.get("description", "").lower()
            
            # Extract position
            location = feature.get("location", {})
            position = None
            if isinstance(location, dict):
                start = location.get("start", {})
                if isinstance(start, dict):
                    position = start.get("value")
            
            if position is None:
                continue
            
            # Extract experimental evidence
            evidence = self._extract_evidence(feature)
            
            # Extract ligand from feature (ChEBI)
            ligand = feature.get("ligand", {})
            ligand_name = ligand.get("name", "").upper() if isinstance(ligand, dict) else ""
            ligand_id = ligand.get("id") if isinstance(ligand, dict) else None
            
            if ligand_id:
                evidence['chebi_id'] = ligand_id
            
            # Map to NeSy site type
            site_type = None
            site_params = {}
            
            if feature_type == "Active site":
                site_type = "catalytic"
            
            elif feature_type == "Metal binding":
                site_type = "ion-binding"
                # Extract ion type from description
                if "zinc" in description or "zn" in description:
                    site_params["ion_type"] = "Zn"
                elif "calcium" in description or "ca" in description:
                    site_params["ion_type"] = "Ca"
                elif "magnesium" in description or "mg" in description:
                    site_params["ion_type"] = "Mg"
                elif "iron" in description or "fe" in description:
                    site_params["ion_type"] = "Fe"
                else:
                    site_params["ion_type"] = "Metal"
            
            elif feature_type == "Nucleotide binding" or ligand_name in ["ATP", "GTP", "ADP", "GDP"]:
                # Prefer ligand name over description inference
                if ligand_name in ["ATP", "ADP"] or "atp" in description:
                    site_type = "ATP-binding"
                elif ligand_name in ["GTP", "GDP"] or "gtp" in description:
                    site_type = "GTP-binding"
                else:
                    site_type = "nucleotide-binding"
            
            elif feature_type == "DNA binding":
                site_type = "DNA-binding"
                # Try to infer groove specificity
                if "major" in description:
                    site_params["groove"] = "Major"
                elif "minor" in description:
                    site_params["groove"] = "Minor"
            
            elif feature_type == "RNA binding":
                site_type = "RNA-binding"
            
            elif feature_type == "Binding site":
                # Infer from ligand name first, then description
                if ligand_name and ligand_name not in ["ATP", "GTP", "ADP", "GDP"]:
                    site_type = f"{ligand_name}-binding"
                elif "atp" in description:
                    site_type = "ATP-binding"
                elif "gtp" in description:
                    site_type = "GTP-binding"
                elif "dna" in description:
                    site_type = "DNA-binding"
                elif "rna" in description:
                    site_type = "RNA-binding"
                elif "substrate" in description:
                    site_type = "substrate"
                else:
                    site_type = "binding"
            
            # Group by site type + params
            if site_type:
                site_key = site_type
                if site_params:
                    # Create unique key for parameterized sites
                    param_str = "_".join(f"{k}:{v}" for k, v in sorted(site_params.items()))
                    site_key = f"{site_type}_{param_str}"
                
                if site_key not in site_groups:
                    site_groups[site_key] = {
                        "type": site_type,
                        "residues": [],
                        "evidence": {
                            "pubmed_ids": set(),
                            "pdb_ids": set(),
                            "eco_codes": set(),
                            "chebi_id": None
                        },
                        **site_params
                    }
                
                site_groups[site_key]["residues"].append(position)
                
                # Merge evidence
                ev = site_groups[site_key]["evidence"]
                ev["pubmed_ids"].update(evidence.get("pubmed_ids", []))
                ev["pdb_ids"].update(evidence.get("pdb_ids", []))
                ev["eco_codes"].update(evidence.get("eco_codes", []))
                if evidence.get("chebi_id"):
                    ev["chebi_id"] = evidence["chebi_id"]
        
        # Convert groups to list and format evidence
        for site_data in site_groups.values():
            # Convert sets to sorted lists
            ev = site_data["evidence"]
            if ev["pubmed_ids"] or ev["pdb_ids"] or ev["eco_codes"] or ev["chebi_id"]:
                site_data["evidence"] = {
                    "pubmed_ids": sorted(list(ev["pubmed_ids"])),
                    "pdb_ids": sorted(list(ev["pdb_ids"])),
                    "eco_codes": sorted(list(ev["eco_codes"]))
                }
                if ev["chebi_id"]:
                    site_data["evidence"]["chebi_id"] = ev["chebi_id"]
            else:
                # Remove empty evidence dict
                del site_data["evidence"]
            
            binding_sites.append(site_data)
        
        return binding_sites
    
    def _extract_evidence(self, feature: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract experimental evidence from UniProt feature
        
        Returns:
            dict: {pubmed_ids: [...], pdb_ids: [...], eco_codes: [...], chebi_id: None}
        """
        evidences = feature.get("evidences", [])
        if not isinstance(evidences, list):
            return {"pubmed_ids": [], "pdb_ids": [], "eco_codes": []}
        
        pubmed_ids = []
        pdb_ids = []
        eco_codes = []
        
        for ev in evidences:
            if not isinstance(ev, dict):
                continue
            
            eco_code = ev.get("evidenceCode", "")
            source = ev.get("source", "")
            ev_id = ev.get("id", "")
            
            if eco_code:
                eco_codes.append(eco_code)
            
            if source == "PubMed" and ev_id:
                pubmed_ids.append(ev_id)
            elif source == "PDB" and ev_id:
                pdb_ids.append(ev_id)
        
        return {
            "pubmed_ids": pubmed_ids,
            "pdb_ids": pdb_ids,
            "eco_codes": eco_codes
        }
    
    def _extract_motifs(self, uniprot_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract motifs from UniProt features
        
        Maps UniProt feature types:
        - "Motif" → generic motif
        - "Short sequence motif" → specific motif
        - "Compositionally biased region" → excluded (too generic)
        """
        motifs = []
        
        for feature in uniprot_data.get("features", []):
            if not isinstance(feature, dict):
                continue
            
            feature_type = feature.get("type", "")
            
            if feature_type in ["Motif", "Short sequence motif"]:
                location = feature.get("location", {})
                start = 1
                end = 1
                
                if isinstance(location, dict):
                    s = location.get("start", {})
                    e = location.get("end", {})
                    if isinstance(s, dict):
                        start = s.get("value", start)
                    if isinstance(e, dict):
                        end = e.get("value", end)
                
                description = feature.get("description", "Unknown motif")
                
                # Clean up description for NeSy marker
                motif_name = description
                # Common abbreviations
                if "nuclear localization signal" in description.lower():
                    motif_name = "NLS"
                elif "nuclear export signal" in description.lower():
                    motif_name = "NES"
                elif "dfg" in description.lower():
                    motif_name = "DFG"
                elif "hinge" in description.lower():
                    motif_name = "Hinge"
                
                motifs.append({
                    "name": motif_name,
                    "type": feature_type.lower(),
                    "start": start,
                    "end": end,
                    "description": description
                })
        
        return motifs
    
    def _infer_states(self, ptms: List[Dict], domains: List[Dict]) -> List[str]:
        """
        Infer functional states from PTM patterns and domain types
        
        Strategy:
        - Kinases: Apo_Inactive, Phosphorylated_Active
        - GPCRs: Apo_Inactive, Agonist_Active
        - Enzymes: Apo, Substrate_bound
        - Default: Inactive, Active
        """
        states = set()
        
        # Detect protein family from domains
        is_kinase = any("kinase" in d.get("domain_name", "").lower() for d in domains)
        is_gpcr = any("7tm" in d.get("domain_name", "").lower() or 
                     "gpcr" in d.get("domain_name", "").lower() for d in domains)
        is_enzyme = any("catalytic" in d.get("domain_name", "").lower() or
                       "enzyme" in d.get("domain_name", "").lower() for d in domains)
        
        # Kinase-specific states
        if is_kinase:
            states.add("Apo_Inactive")
            has_phosphorylation = any(ptm["ptm_type"] == "phosphorylation" for ptm in ptms)
            if has_phosphorylation:
                states.add("Phosphorylated_Active")
            else:
                states.add("Active")  # Generic active state
        
        # GPCR-specific states
        elif is_gpcr:
            states.add("Apo_Inactive")
            states.add("Agonist_Active")
        
        # Enzyme-specific states
        elif is_enzyme:
            states.add("Apo")
            states.add("Substrate_bound")
        
        # Default states
        else:
            states.add("Inactive")
            if len(ptms) > 0:
                states.add("Modified_Active")
            else:
                states.add("Active")
        
        return sorted(states)
    
    def _infer_state_regions(
        self, 
        domains: List[Dict], 
        motifs: List[Dict], 
        state_name: str
    ) -> List[Dict[str, Any]]:
        """
        Infer state-specific regions (e.g., DFG-in/out for kinases)
        
        Returns list of state region annotations for punctual markers
        """
        state_regions = []
        state_lower = state_name.lower()
        
        # Check for DFG motif in kinases
        for motif in motifs:
            motif_name = motif.get("name", "").lower()
            
            # DFG motif in kinases
            if "dfg" in motif_name:
                # Determine DFG state from overall state
                if "inactive" in state_lower or "apo" in state_lower:
                    dfg_state = "DFG-OUT"
                else:
                    dfg_state = "DFG-IN"
                
                # Add point marker at DFG motif start
                state_regions.append({
                    "type": "point",
                    "position": motif["start"],
                    "state_name": dfg_state
                })
        
        return state_regions
    
    def _generate_lmp_xml(
        self,
        uniprot_id: str,
        gene_name: str,
        organism: str,
        state_name: str,
        sequence: str,
        domains: List[Dict],
        ptms: List[Dict],
        binding_sites: List[Dict],
        motifs: List[Dict],
        pdb_data: Dict[str, Any],
    ) -> str:
        """Generate LMP v2.0 XML document for a single state"""
        # Create root element
        root = ET.Element("PML_Protein")
        root.set("uniprot_id", uniprot_id)
        root.set("gene_name", gene_name)
        root.set("organism", organism)
        root.set("state", state_name)
        
        # Metadata
        metadata = ET.SubElement(root, "Metadata")
        source = ET.SubElement(metadata, "Source")
        source.set("type", "UniProt")
        source.set("ref", uniprot_id)
        
        # Chain with modified sequence (PTMs encoded in FULL LMP v2.0 NeSy language)
        chain = ET.SubElement(root, "Chain")
        chain.set("id", "A")
        
        # NEW: Use NeSy encoder for complete hierarchical sequence
        state_ptms = [ptm for ptm in ptms if self._get_ptm_status_for_state(ptm, state_name) == "present"]
        nesy_sequence = self._encode_nesy_sequence(
            sequence=sequence,
            domains=domains,
            ptms=state_ptms,
            binding_sites=binding_sites,
            motifs=motifs,
            state_name=state_name
        )
        chain.set("sequence", nesy_sequence)
        
        # Add GLOBAL PTMs section (NEW: includes ALL PTMs, not just those in domains)
        # This ensures comprehensive annotation coverage
        ptm_section = ET.SubElement(chain, "PTMs")
        for ptm in ptms:
            ptm_elem = ET.SubElement(ptm_section, "PTM")
            ptm_elem.set("id", ptm["ptm_id"])
            ptm_elem.set("type", ptm["ptm_type"])
            ptm_elem.set("residue", ptm["residue"])
            ptm_elem.set("position", str(ptm["position"]))
            ptm_elem.set("status", self._get_ptm_status_for_state(ptm, state_name))
            if ptm.get("evidence"):
                ptm_elem.set("evidence", ptm["evidence"])
            if ptm.get("description"):
                ptm_elem.set("description", ptm["description"])
        
        # Add domains
        for domain_data in domains:
            domain_elem = ET.SubElement(chain, "Domain")
            domain_elem.set("name", domain_data["domain_name"])
            domain_elem.set("type", domain_data["domain_type"])
            domain_elem.set("start", str(domain_data["start"]))
            domain_elem.set("end", str(domain_data["end"]))
            
            # NEW: Add PAS Annotations if present
            if "pas_annotations" in domain_data:
                pas_data = domain_data["pas_annotations"]
                pas_elem = ET.SubElement(domain_elem, "PAS_Annotations")
                pas_elem.set("family", pas_data.get("family", "Unknown"))
                
                # Add Motifs
                if pas_data.get("motifs"):
                    motifs_elem = ET.SubElement(pas_elem, "Motifs")
                    for name, info in pas_data["motifs"].items():
                        m_elem = ET.SubElement(motifs_elem, "Motif")
                        m_elem.set("name", name)
                        m_elem.set("sequence", info.get("sequence", ""))
                        m_elem.set("start", str(info.get("start_absolute", "")))
                        m_elem.set("end", str(info.get("end_absolute", "")))
                        if "state_inference" in info:
                            m_elem.set("inference", info["state_inference"])

                # Add Regions (Activation Loop)
                if pas_data.get("regions"):
                    regions_elem = ET.SubElement(pas_elem, "Regions")
                    for name, info in pas_data["regions"].items():
                        r_elem = ET.SubElement(regions_elem, "Region")
                        r_elem.set("name", name)
                        r_elem.set("start", str(info.get("start", "")))
                        r_elem.set("end", str(info.get("end", "")))
                        if "description" in info:
                            r_elem.set("description", info["description"])

                # Add Sites (Catalytic)
                if pas_data.get("sites"):
                    sites_elem = ET.SubElement(pas_elem, "Sites")
                    for name, info_list in pas_data["sites"].items():
                        # Handle both list and single dict for robustness
                        items = info_list if isinstance(info_list, list) else [info_list]
                        for info in items:
                            s_elem = ET.SubElement(sites_elem, "Site")
                            s_elem.set("type", name)
                            s_elem.set("position", str(info.get("position", "")))
                            s_elem.set("role", info.get("role", ""))

            # NOTE: PTMs are now in global <PTMs> section only (lines 831-842)
            # Removed duplicate PTM addition within domains to prevent ID conflicts
            
            # Add conformation for this state
            conf_elem = ET.SubElement(domain_elem, "Conformation")
            conf_elem.set("state_name", state_name)
            
            # Determine trigger (first PTM in state)
            trigger_ptm = next(
                (ptm for ptm in ptms if domain_data["start"] <= ptm["position"] <= domain_data["end"]),
                None
            )
            if trigger_ptm and state_name != "Apo_Inactive":
                conf_elem.set("trigger", trigger_ptm["ptm_id"])
            
            # Add feature states (domain-specific, simplified)
            if "kinase" in domain_data["domain_name"].lower():
                self._add_kinase_feature_states(conf_elem, state_name)
            elif "GPCR" in domain_data["domain_name"].lower():
                self._add_gpcr_feature_states(conf_elem, state_name)
        
        # Add motifs (NEW: Explicitly added from motifs list)
        for motif_data in motifs:
            # Avoid duplicates if they were already in domains (though we removed "Motif" from _extract_domains)
            # But just in case, check if a domain with same start/end/type exists
            is_duplicate = any(
                d["start"] == motif_data["start"] and 
                d["end"] == motif_data["end"] and 
                d["domain_type"] == motif_data["type"]
                for d in domains
            )
            if is_duplicate:
                continue

            domain_elem = ET.SubElement(chain, "Domain")
            domain_elem.set("name", motif_data["name"])
            domain_elem.set("type", motif_data["type"]) # e.g. "motif", "short sequence motif"
            domain_elem.set("start", str(motif_data["start"]))
            domain_elem.set("end", str(motif_data["end"]))
            if motif_data.get("description"):
                domain_elem.set("description", motif_data["description"])
            
            # Add conformation for this state (simple)
            conf_elem = ET.SubElement(domain_elem, "Conformation")
            conf_elem.set("state_name", state_name)

        # Pretty print XML
        xml_str = self._prettify_xml(root)
        
        return xml_str
    
    def _get_ptm_status_for_state(self, ptm: Dict, state_name: str) -> str:
        """
        Determine PTM status (present/absent/transient) for given state
        
        Rules:
        - Constitutive PTMs (glycosylation, etc.): ALWAYS "present"
        - Dynamic PTMs (phosphorylation, acetylation): state-dependent
        
        Example:
        - Apo_Inactive: phosphorylation is "absent", glycosylation is "present"
        - Phosphorylated_Active: both phosphorylation and glycosylation are "present"
        """
        ptm_type = ptm.get("ptm_type", "").lower()
        
        # Constitutive PTMs are ALWAYS present (structural modifications)
        CONSTITUTIVE_PTMS = {
            "n-glycosylation",
            "o-glycosylation",
            "hydroxylation",
            "palmitoylation",
            "nitrosylation",
            "n_terminal_myristoylation",  # Lipid anchor - permanent
            "n_terminal_acetylation",     # N-terminal processing - permanent
        }
        
        if ptm_type in CONSTITUTIVE_PTMS:
            return "present"  # Always present regardless of state
        
        # Dynamic PTMs are state-dependent (regulatory modifications)
        state_lower = state_name.lower()
        
        if "apo" in state_lower or "inactive" in state_lower:
            return "absent"
        elif "active" in state_lower or "phosphorylated" in state_lower:
            return "present"
        else:
            return "transient"
    
    def _add_kinase_feature_states(self, conf_elem: ET.Element, state_name: str):
        """Add kinase-specific feature states"""
        state_lower = state_name.lower()
        
        # Activation loop
        feature_activation_loop = ET.SubElement(conf_elem, "FeatureState")
        feature_activation_loop.set("feature_name", "ActivationLoop")
        if "active" in state_lower:
            feature_activation_loop.set("state", "Substrate-accessible")
        else:
            feature_activation_loop.set("state", "Blocked")
        
        # ATP binding site
        feature_atp = ET.SubElement(conf_elem, "FeatureState")
        feature_atp.set("feature_name", "ATP_BindingSite")
        if "active" in state_lower:
            feature_atp.set("state", "Competent")
        else:
            feature_atp.set("state", "Distorted")
    
    def _add_gpcr_feature_states(self, conf_elem: ET.Element, state_name: str):
        """Add GPCR-specific feature states"""
        state_lower = state_name.lower()
        
        # Transmembrane helices
        feature_tm = ET.SubElement(conf_elem, "FeatureState")
        feature_tm.set("feature_name", "TransmembraneHelices")
        if "active" in state_lower or "agonist" in state_lower:
            feature_tm.set("state", "Outward")
        else:
            feature_tm.set("state", "Inward")
    
    def _annotate_catalytic_residues(
        self, xml_str: str, catalytic_residues: List[int], state_name: str
    ) -> str:
        """Annotate catalytic residues in LMP XML"""
        # Parse XML
        root = ET.fromstring(xml_str)
        
        # Find first domain and add catalytic site annotation
        chain = root.find("Chain")
        if chain is not None:
            domain = chain.find("Domain")
            if domain is not None:
                # Add binding site for catalytic residues
                binding_site = ET.SubElement(domain, "BindingSite")
                binding_site.set("type", "catalytic")
                binding_site.set("residues", ",".join(map(str, catalytic_residues)))
                
                # Add substrate ligand for Active state
                if "active" in state_name.lower():
                    ligand = ET.SubElement(binding_site, "Ligand")
                    ligand.set("name", "Substrate")
                    ligand.set("type", "substrate")
                    ligand.set("effect", "catalysis")
        
        return self._prettify_xml(root)
    
    def _encode_lmp_sequence(self, sequence: str, ptms: List[Dict[str, Any]]) -> str:
        """
        Encode sequence with PTM modifications in LMP language
        
        Example:
        - Position 57: S + phosphorylation → pS
        - Position 102: K + acetylation → acK
        
        Returns modified sequence string with PTM codes
        """
        seq_list = list(sequence)
        
        # Sort PTMs by position (reverse) to avoid index shifts
        sorted_ptms = sorted(ptms, key=lambda p: p["position"], reverse=True)
        
        for ptm in sorted_ptms:
            position = ptm["position"]
            ptm_type = ptm["ptm_type"]
            
            if position < 1 or position > len(seq_list):
                continue
            
            # Get modification prefix
            prefix = ""
            if ptm_type == "phosphorylation":
                prefix = "p"
            elif ptm_type == "acetylation":
                prefix = "ac"
            elif ptm_type == "methylation":
                prefix = "me"
            elif ptm_type == "ubiquitination":
                prefix = "ub"
            elif ptm_type == "sumoylation":
                prefix = "su"
            else:
                prefix = "mod"
            
            # Replace residue with modified version
            idx = position - 1  # 0-based index
            original_residue = seq_list[idx]
            seq_list[idx] = f"{prefix}{original_residue}"
        
        return "".join(seq_list)
    
    def _encode_nesy_sequence(
        self,
        sequence: str,
        domains: List[Dict[str, Any]],
        ptms: List[Dict[str, Any]],
        binding_sites: List[Dict[str, Any]],
        motifs: List[Dict[str, Any]],
        state_name: str
    ) -> str:
        """
        Encode sequence with FULL LMP v2.0 NeSy syntax (ANEXO implementation)
        
        Uses the complete hierarchical NeSy encoder with:
        - Domain markers: [DOM:name], [TMD], [DBD], [RBD]
        - Binding sites: (ATP), (CAT), (DNA), (ION:Zn)
        - Motifs: [MOT:NLS], [MOT:DFG]
        - PTMs with enzymes: {S-P:PKA}, {K-Ac:p300}
        - PPI interfaces: <PPI:partner>
        - State markers: *ACTIVE*, *DFG-IN*
        
        Args:
            sequence: Canonical amino acid sequence
            domains: List of domain annotations
            ptms: List of PTM annotations (state-filtered)
            binding_sites: List of binding site annotations
            motifs: List of motif annotations
            state_name: Current conformational state
        
        Returns:
            NeSy-encoded sequence string with full hierarchical markers
        """
        # Build NeSyAnnotation from current data
        annotation = NeSyAnnotation(
            sequence=sequence,
            domains=[
                {
                    "name": d["domain_name"],
                    "type": d.get("domain_type", "unknown"),
                    "start": d["start"],
                    "end": d["end"]
                }
                for d in domains
            ],
            motifs=[
                {
                    "name": m["name"],
                    "type": m.get("type", "motif"),
                    "start": m["start"],
                    "end": m["end"]
                }
                for m in motifs
            ],
            ptms=[
                {
                    "position": ptm["position"],
                    "ptm_type": ptm["ptm_type"],
                    "residue": ptm["residue"],
                    "enzyme": ptm.get("enzyme", None)  # Will be None for now
                }
                for ptm in ptms
            ],
            binding_sites=binding_sites,  # Now populated from _extract_binding_sites()
            ppi_interfaces=[],  # TODO: Extract from STRING/ProtCID (external data)
            conformational_state=state_name,
            state_regions=self._infer_state_regions(domains, motifs, state_name)
        )
        
        # Use NeSy encoder
        encoder = LMPNeSyEncoder()
        nesy_seq = encoder.encode(annotation)
        
        return nesy_seq
    
    def _infer_ptm_type(self, description: str) -> str:
        """Infer PTM type from description"""
        description_lower = description.lower()
        
        # N-terminal modifications (must check BEFORE generic acyl checks)
        if "n-myristoyl" in description_lower or "n-myristoy" in description_lower:
            return "n_terminal_myristoylation"
        elif "n-acetyl" in description_lower and any(aa in description_lower for aa in ['alanine', 'methionine', 'serine', 'proline', 'valine']):
            return "n_terminal_acetylation"
        # Regular PTMs
        elif "phospho" in description_lower:
            return "phosphorylation"
        elif "acetyl" in description_lower:
            return "acetylation"
        elif "methyl" in description_lower:
            return "methylation"
        elif "ubiquitin" in description_lower:
            return "ubiquitination"
        elif "sumo" in description_lower:
            return "sumoylation"
        elif "n-linked" in description_lower or "o-linked" in description_lower:
            # N-linked (GlcNAc...) asparagine → N-glycosylation
            # O-linked (...) serine/threonine → O-glycosylation
            if "n-linked" in description_lower:
                return "N-glycosylation"
            else:
                return "O-glycosylation"
        elif "hydroxy" in description_lower:
            return "hydroxylation"
        elif "nitroso" in description_lower or "s-nitroso" in description_lower:
            return "nitrosylation"
        elif "palmito" in description_lower:
            return "palmitoylation"
        else:
            return "modification"
    
    def _validate_ptm(self, ptm_type: str, residue: str) -> bool:
        """
        Validate PTM-residue compatibility (Bug #3 fix)
        
        Returns True if PTM can occur on this residue type
        """
        valid_residues = self.PTM_RESIDUE_COMPATIBILITY.get(ptm_type, set())
        
        if not valid_residues:
            # Unknown PTM type — allow (conservative)
            return True
        
        if residue not in valid_residues:
            self.logger.warning(
                f"Invalid PTM: {ptm_type} cannot occur on {residue} "
                f"(valid: {valid_residues})"
            )
            return False
        
        return True
    
    def _prettify_xml(self, elem: ET.Element) -> str:
        """Return pretty-printed XML string"""
        rough_string = ET.tostring(elem, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        return reparsed.toprettyxml(indent="  ")
    
    def _rate_limit_wait(self):
        """Enforce rate limit between API requests"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.time()
    
    def generate_from_pdb(
        self,
        pdb_id: str,
        chain_id: Optional[str] = None,
        state_name: Optional[str] = None
    ) -> str:
        """
        Generate LMP XML from PDB structure with actual PDB sequences
        
        CRITICAL: Uses existing NeSy encoder (LMPNeSyEncoder)
        CRITICAL: Uses existing marker system [DOM:xxx], [BIND:xxx], etc.
        
        Args:
            pdb_id: PDB identifier (e.g., "6FBK")
            chain_id: Specific chain to process (e.g., "A"). 
                     If None, includes ALL chains in a single XML
            state_name: Custom state name (default: f"{pdb_id}" or f"{pdb_id}_Chain{chain_id}")
        
        Returns:
            Single XML string with one or multiple chains
        
        Example:
            >>> gen = LMPGenerator()
            >>> xml = gen.generate_from_pdb("6FBK")  # All chains in one XML
            >>> # Returns XML with Chain A (98 aa) AND Chain B (23 aa)
            
            >>> xml = gen.generate_from_pdb("6FBK", chain_id="B")  # Single chain
            >>> # Returns XML with only Chain B (23 aa WNK1 CCT fragment)
        """
        self.logger.info(f"Generating LMP from PDB {pdb_id} (chain: {chain_id or 'all'})")
        
        # Fetch PDB data (metadata + FASTA chains)
        pdb_data = self._fetch_pdb(pdb_id)
        chains = pdb_data.get("chains", {})
        
        if not chains:
            raise ValueError(f"No chains found in PDB {pdb_id}")
        
        # Fetch ligands for this PDB
        self.logger.info(f"Fetching ligands for {pdb_id}...")
        ligands = self._fetch_pdb_ligands(pdb_id)
        
        # Fetch secondary structure
        self.logger.info(f"Fetching secondary structure for {pdb_id}...")
        secondary_structure = self._fetch_secondary_structure(pdb_id)
        
        # Fetch binding sites for interface detection
        self.logger.info(f"Fetching binding sites for {pdb_id}...")
        binding_data = self._fetch_binding_sites(pdb_id)
        
        # Detect protein-protein interfaces
        ppi_interfaces = self._detect_ppi_interfaces(chains, binding_data)
        if ppi_interfaces:
            self.logger.info(f"Detected {len(ppi_interfaces)} protein-protein interface(s)")
        
        # Filter to specific chain if requested
        if chain_id:
            if chain_id not in chains:
                available = ', '.join(chains.keys())
                raise ValueError(f"Chain {chain_id} not found in {pdb_id}. Available: {available}")
            chains_to_process = {chain_id: chains[chain_id]}
            state = state_name or f"{pdb_id}_Chain{chain_id}"
        else:
            chains_to_process = chains
            state = state_name or pdb_id
        
        # Create root XML element
        root = ET.Element("PML_Protein")
        root.set("pdb_id", pdb_id)
        if chain_id:
            root.set("chain_id", chain_id)
        else:
            root.set("chains", ",".join(sorted(chains_to_process.keys())))
        root.set("state", state)
        
        # Get first chain for organism info
        first_chain_data = list(chains_to_process.values())[0]
        organism = first_chain_data.get("organism", "Unknown")
        root.set("organism", organism)
        
        # Metadata
        metadata = ET.SubElement(root, "Metadata")
        source = ET.SubElement(metadata, "Source")
        source.set("type", "PDB")
        source.set("ref", pdb_id)
        
        # Add PDB metadata (method, resolution, etc.)
        struct = pdb_data.get("struct", {})
        if struct:
            pdb_meta = ET.SubElement(metadata, "PDB_Info")
            pdb_meta.set("method", struct.get("title", "X-RAY DIFFRACTION"))
            
            # Resolution (if available)
            exptl = pdb_data.get("rcsb_entry_info", {})
            resolution = exptl.get("resolution_combined", [None])[0]
            if resolution:
                pdb_meta.set("resolution", f"{resolution:.2f}")
        
        # Process each chain and add to XML
        for chain_idx, (cid, chain_data) in enumerate(sorted(chains_to_process.items())):
            sequence = chain_data["sequence"]
            protein_name = chain_data.get("protein", f"PDB_{pdb_id}_Chain{cid}")
            
            self.logger.info(
                f"Processing chain {cid}: {protein_name} "
                f"({len(sequence)} aa)"
            )
            
            # Map PDB fragment to UniProt to get domain/PTM annotations
            self.logger.info(f"Mapping PDB fragment to UniProt...")
            
            # NEW: Get uniprot_id from chain_data if available (from PDB API)
            pdb_uniprot_id = chain_data.get("uniprot_id")
            
            uniprot_mapping = self._map_pdb_to_uniprot(
                pdb_sequence=sequence,
                protein_name=protein_name,
                organism=organism,
                uniprot_id=pdb_uniprot_id
            )
            
            if uniprot_mapping:
                # Use mapped annotations from UniProt
                domains = uniprot_mapping["domains"]
                ptms = uniprot_mapping["ptms"]
                binding_sites = uniprot_mapping["binding_sites"]
                motifs = uniprot_mapping["motifs"]
                
                # NEW: Get full protein domains for complete annotation
                all_domains_full = uniprot_mapping.get("all_domains", [])
                all_ptms_full = uniprot_mapping.get("all_ptms", [])
                full_protein_length = uniprot_mapping.get("full_protein_length", 0)
                
                # Add UniProt reference to metadata if not already present
                if not root.get("uniprot_id"):
                    root.set("uniprot_id", uniprot_mapping["uniprot_id"])
                    root.set("gene_name", uniprot_mapping["gene_name"])
                    root.set("full_protein_length", str(full_protein_length))
                    
                    # Add UniProt source
                    uniprot_source = ET.SubElement(metadata, "Source")
                    uniprot_source.set("type", "UniProt")
                    uniprot_source.set("ref", uniprot_mapping["uniprot_id"])
                    
                    # Add alignment info (PDB fragment → UniProt region)
                    alignment_elem = ET.SubElement(metadata, "Alignment")
                    alignment_elem.set("pdb_region", f"1-{len(sequence)}")
                    alignment_elem.set(
                        "uniprot_region",
                        f"{uniprot_mapping['alignment']['uniprot_start']}-"
                        f"{uniprot_mapping['alignment']['uniprot_end']}"
                    )
                    alignment_elem.set("note", "PDB fragment corresponds to this UniProt region")
                    
                    # NEW: Add full protein domain annotations
                    if all_domains_full:
                        full_domains_elem = ET.SubElement(metadata, "FullProteinDomains")
                        full_domains_elem.set("count", str(len(all_domains_full)))
                        for dom in all_domains_full:
                            dom_elem = ET.SubElement(full_domains_elem, "Domain")
                            dom_elem.set("name", dom.get("domain_name", "Unknown"))
                            dom_elem.set("type", dom.get("domain_type", "domain"))
                            dom_elem.set("start", str(dom.get("start", 0)))
                            dom_elem.set("end", str(dom.get("end", 0)))
                            # Mark if this domain overlaps with PDB fragment
                            dom_start = dom.get("start", 0)
                            dom_end = dom.get("end", 0)
                            frag_start = uniprot_mapping['alignment']['uniprot_start']
                            frag_end = uniprot_mapping['alignment']['uniprot_end']
                            overlaps = not (dom_end < frag_start or dom_start > frag_end)
                            dom_elem.set("overlaps_pdb_fragment", str(overlaps).lower())
                
                self.logger.info(
                    f"✓ Mapped to {uniprot_mapping['uniprot_id']}: "
                    f"{len(all_domains_full)} total domains ({len(domains)} in PDB fragment), "
                    f"{len(ptms)} PTMs, {len(binding_sites)} binding sites"
                )
            else:
                # No UniProt mapping - use empty annotations
                domains = []
                ptms = []
                binding_sites = []
                motifs = []
                self.logger.warning(f"No UniProt mapping found for {protein_name}")
            
            # Extract ligand binding sites for this chain from binding_data
            chain_ligand_bindings = []
            
            # Support multi-unit structures: iterate over ALL author chain IDs
            auth_chain_ids = chain_data.get("auth_chain_ids", [chain_data.get("auth_chain_id", cid)])
            seen_binding_residues = set()  # Track residues to avoid duplicates across units
            
            for site in binding_data.get("ligand_sites", []):
                # Check if this chain is involved in the binding site
                site_residues_by_chain = site.get("residues_by_chain", {})
                site_id = site.get("site_id", "UNKNOWN")
                
                # Search in ALL author chain IDs (handles multi-unit structures like 1FIN)
                for auth_cid in auth_chain_ids:
                    if auth_cid not in site_residues_by_chain:
                        continue
                    
                    residues = site_residues_by_chain[auth_cid]
                    
                    # Deduplicate residues across multiple crystallographic units
                    unique_residues = []
                    for res_num in residues:
                        res_key = (site_id, res_num)
                        if res_key not in seen_binding_residues:
                            unique_residues.append(res_num)
                            seen_binding_residues.add(res_key)
                    
                    if not unique_residues:
                        continue  # All residues already seen in another unit
                    
                    # Try to match site to a specific ligand
                    matching_ligand = None
                    for lig in ligands:
                        # Check if ligand ID appears in site details
                        if site.get("details") and lig["ligand_id"] in site["details"]:
                            matching_ligand = lig
                            break
                    
                    if matching_ligand:
                        # Determine binding site type based on ligand
                        lig_id = matching_ligand["ligand_id"]
                        lig_name = matching_ligand["name"].upper()
                        
                        # Classify binding site type and ligand role
                        if lig_id in ["ATP", "ADP", "ANP", "AMP", "ACP", "AGS"]:
                            site_type = "ATP-binding"
                            ligand_role = "agonist"  # ATP is activating
                        elif lig_id in ["GTP", "GDP", "GNP"]:
                            site_type = "GTP-binding"
                            ligand_role = "agonist"  # GTP is activating
                        elif "MANGANESE" in lig_name or "ZINC" in lig_name or "CALCIUM" in lig_name or "MAGNESIUM" in lig_name:
                            # Extract ion type
                            ion_type = lig_id  # Use ligand ID (MN, ZN, CA, MG)
                            site_type = "ion-binding"
                            chain_ligand_bindings.append({
                                "type": site_type,
                                "residues": unique_residues,
                                "ion_type": ion_type,
                                "binding_site_id": site_id
                            })
                            continue
                        else:
                            site_type = "substrate"  # Generic binding site
                            ligand_role = "substrate"  # Generic substrate
                        
                        # Format ligand in structure expected by NeSy encoder
                        # Encoder expects: {"ligand": {"type": "agonist/inhibitor", "name": "ATP"}}
                        chain_ligand_bindings.append({
                            "type": site_type,
                            "residues": unique_residues,
                            "ligand": {
                                "type": ligand_role,
                                "name": matching_ligand["ligand_id"]
                            },
                            "binding_site_id": site_id
                        })
            
            # Add ligand binding sites to binding_sites list
            binding_sites.extend(chain_ligand_bindings)
            
            # Normalize domains/motifs to NeSyAnnotation format
            # NeSyAnnotation expects: {name, type, start, end}
            # We have: {domain_name, domain_type, pdb_start, pdb_end, ...}
            normalized_domains = []
            for d in domains:
                normalized_domains.append({
                    "name": d.get("domain_name", d.get("name", "unknown")),
                    "type": d.get("domain_type", d.get("type", "domain")),
                    "start": d.get("pdb_start", d.get("start", 1)),
                    "end": d.get("pdb_end", d.get("end", len(sequence)))
                })
            
            normalized_motifs = []
            for m in motifs:
                normalized_motifs.append({
                    "name": m.get("motif_name", m.get("name", "unknown")),
                    "type": m.get("motif_type", m.get("type", "motif")),
                    "start": m.get("pdb_start", m.get("start", 1)),
                    "end": m.get("pdb_end", m.get("end", len(sequence)))
                })
            
            # Normalize PTMs: use pdb_position if available
            normalized_ptms = []
            for p in ptms:
                normalized_ptms.append({
                    "position": p.get("pdb_position", p.get("position", 1)),
                    "type": p.get("ptm_type", p.get("type", "modification")),
                    "residue": p.get("residue", "X"),
                    "enzyme": p.get("enzyme")
                })
            
            # Extract PPI interfaces for this chain
            chain_ppi_interfaces = []
            for interface in ppi_interfaces:
                # Check if this chain is involved in the interface
                if cid in interface.get("chains", []):
                    # Get residues for this specific chain
                    chain_residues = interface.get("residues", {}).get(cid, [])
                    if chain_residues:
                        # Get partner chains (all chains except current)
                        partner_chains = [c for c in interface["chains"] if c != cid]
                        partner_id = ",".join(partner_chains) if partner_chains else "Unknown"
                        
                        # Get binding site ID if available
                        binding_site_id = interface.get("binding_site_id", None)
                        
                        chain_ppi_interfaces.append({
                            "partner_id": partner_id,
                            "residues": chain_residues,
                            "binding_site_id": binding_site_id,
                            "type": interface.get("type", "protein-protein")
                        })
            
            # Encode sequence using NeSy encoder with mapped annotations
            encoder = LMPNeSyEncoder()
            
            annotation = NeSyAnnotation(
                sequence=sequence,
                domains=normalized_domains,
                motifs=normalized_motifs,
                ptms=normalized_ptms,
                binding_sites=binding_sites,
                ppi_interfaces=chain_ppi_interfaces,
                conformational_state=state,
                state_regions=[]
            )
            
            # Encode
            encoded_seq = encoder.encode(annotation)
            
            # Add Chain element
            chain = ET.SubElement(root, "Chain")
            chain.set("id", cid)
            chain.set("protein", protein_name)
            chain.set("sequence", encoded_seq)
            chain.set("raw_sequence", sequence)
            chain.set("length", str(len(sequence)))
            
            # Add PTMs section if present
            if ptms:
                ptm_section = ET.SubElement(chain, "PTMs")
                for ptm in ptms:
                    ptm_elem = ET.SubElement(ptm_section, "PTM")
                    ptm_elem.set("id", ptm.get("ptm_id", f"ptm_{ptm['position']}"))
                    ptm_elem.set("type", ptm["ptm_type"])
                    ptm_elem.set("residue", ptm["residue"])
                    ptm_elem.set("position", str(ptm.get("pdb_position", ptm["position"])))
                    ptm_elem.set("uniprot_position", str(ptm["position"]))
                    if ptm.get("description"):
                        ptm_elem.set("description", ptm["description"])
            
            # Add Domains section if present
            if domains:
                for domain in domains:
                    domain_elem = ET.SubElement(chain, "Domain")
                    # Handle both "name" and "domain_name" keys
                    domain_name = domain.get("name") or domain.get("domain_name", "unknown")
                    domain_type = domain.get("type") or domain.get("domain_type", "domain")
                    domain_elem.set("name", domain_name)
                    domain_elem.set("type", domain_type)
                    domain_elem.set("pdb_start", str(domain.get("pdb_start", domain["start"])))
                    domain_elem.set("pdb_end", str(domain.get("pdb_end", domain["end"])))
                    domain_elem.set("uniprot_start", str(domain["start"]))
                    domain_elem.set("uniprot_end", str(domain["end"]))
            
            # Add Conformational State classification (for first chain only)
            # Can classify even without UniProt mapping using ligands and sequence
            if chain_idx == 0:
                conformational_state = self._classify_kinase_state(
                    sequence=sequence,
                    ligands=ligands,
                    ptms=ptms if uniprot_mapping else []
                )
                
                # Add to chain metadata
                conf_state_elem = ET.SubElement(chain, "ConformationalState")
                conf_state_elem.set("state", conformational_state["state"])
                conf_state_elem.set("confidence", conformational_state["confidence"])
                conf_state_elem.set("atp_bound", str(conformational_state["atp_bound"]).lower())
                conf_state_elem.set("dfg_motif", str(conformational_state["dfg_motif"]).lower())
                if conformational_state["dfg_position"] >= 0:
                    conf_state_elem.set("dfg_position", str(conformational_state["dfg_position"]))
                conf_state_elem.set("activation_phosphorylation", str(conformational_state["activation_phosphorylation"]).lower())
                conf_state_elem.set("inhibitor_bound", str(conformational_state["inhibitor_bound"]).lower())
            
            # Add SecondaryStructure section if present for this chain
            # Use auth_chain_id for PDBe API mapping (e.g., "E" instead of "A")
            auth_cid = chain_data.get("auth_chain_id", cid)
            if auth_cid in secondary_structure:
                sec_struct_section = ET.SubElement(chain, "SecondaryStructure")
                for element in secondary_structure[auth_cid]:
                    if element["type"] == "helix":
                        helix_elem = ET.SubElement(sec_struct_section, "Helix")
                        helix_elem.set("start", str(element["start"]))
                        helix_elem.set("end", str(element["end"]))
                    elif element["type"] == "sheet":
                        strand_elem = ET.SubElement(sec_struct_section, "Strand")
                        strand_elem.set("start", str(element["start"]))
                        strand_elem.set("end", str(element["end"]))
                        if "strand_id" in element:
                            strand_elem.set("sheet_id", str(element["strand_id"]))
        
        # Add Ligands section (PDB-level, not chain-specific)
        # NOTE: Ligands are typically shared across chains in same PDB structure
        if ligands:
            ligands_section = ET.SubElement(root, "Ligands")
            for lig in ligands:
                ligand_elem = ET.SubElement(ligands_section, "Ligand")
                ligand_elem.set("id", lig["ligand_id"])
                ligand_elem.set("name", lig["name"])
                if lig.get("formula"):
                    ligand_elem.set("formula", lig["formula"])
                if lig.get("chebi_id"):
                    ligand_elem.set("chebi_id", lig["chebi_id"])
                if self.pubchem_xml_include_cid and lig.get("pubchem_cid"):
                    ligand_elem.set("pubchem_cid", str(lig["pubchem_cid"]))
                ligand_elem.set("type", lig.get("type", "non-polymer"))
                
                # Add binding site reference if available
                ligand_binding_sites = [
                    site for site in binding_data.get("ligand_sites", [])
                    if site.get("details") and lig["ligand_id"] in site["details"]
                ]
                if ligand_binding_sites:
                    ligand_elem.set("binding_site", ligand_binding_sites[0]["site_id"])
            
            self.logger.info(f"✓ Added {len(ligands)} ligand(s) to PDB structure")
        
        # Add Interfaces section if protein-protein interfaces detected
        if ppi_interfaces:
            interfaces_section = ET.SubElement(root, "Interfaces")
            for iface in ppi_interfaces:
                interface_elem = ET.SubElement(interfaces_section, "Interface")
                interface_elem.set("chains", ",".join(iface["chains"]))
                interface_elem.set("type", iface["type"])
                if iface.get("binding_site_id"):
                    interface_elem.set("binding_site", iface["binding_site_id"])
                if iface.get("details"):
                    interface_elem.set("details", iface["details"])
                
                # Add residue-level contact information
                if iface.get("residues"):
                    for chain_id, residues in iface["residues"].items():
                        if residues:
                            residues_elem = ET.SubElement(interface_elem, "ChainResidues")
                            residues_elem.set("chain", chain_id)
                            residues_elem.set("residues", ",".join(map(str, sorted(residues))))
            
            self.logger.info(f"✓ Added {len(ppi_interfaces)} interface(s) to PDB structure")
        
        # Log chain summary
        for cid in sorted(chains_to_process.keys()):
            self.logger.info(
                f"✓ Chain {cid}: "
                f"{len(chains_to_process[cid]['sequence'])} aa"
            )

        
        # Convert to pretty XML string
        xml_str = self._prettify_xml(root)
        
        chain_summary = f"{len(chains_to_process)} chain(s): {', '.join(sorted(chains_to_process.keys()))}"
        self.logger.info(f"✓ Generated complete PDB XML with {chain_summary}")
        
        return xml_str
    
    def _enrich_with_pas(
        self,
        domains: List[Dict[str, Any]],
        sequence: str,
        ptms: List[Dict[str, Any]],
        uniprot_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Enrich domains with PAS (Protein Annotation Specificity) data.
        Uses expert rules to add biological context (e.g. activation loops).
        """
        enriched_domains = []
        for domain in domains:
            # Get appropriate annotator for this domain type
            annotator = get_pas_annotator(domain.get("domain_type", ""))
            
            if annotator:
                try:
                    # Apply expert rules
                    enriched_domain = annotator.annotate(
                        domain=domain,
                        sequence=sequence,
                        ptms=ptms,
                        uniprot_data=uniprot_data
                    )
                    enriched_domains.append(enriched_domain)
                except Exception as e:
                    self.logger.warning(f"PAS annotation failed for {domain.get('domain_type')}: {e}")
                    enriched_domains.append(domain)
            else:
                enriched_domains.append(domain)
                
        return enriched_domains

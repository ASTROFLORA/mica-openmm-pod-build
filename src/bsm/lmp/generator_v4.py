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

import itertools
import json
import base64
import math
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom
import sys
import gzip
import tempfile
import os
import subprocess

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

# Import NeSy encoder
from .nesy_encoder import LMPNeSyEncoder, NeSyAnnotation
from .dynamic_statistics_importer import normalize_dynamic_statistics_receipt
from .pas_annotators import get_pas_annotator

# Import presets for v4 unified mode
try:
    from .presets import LMPPreset, get_preset
    PRESETS_AVAILABLE = True
except ImportError:
    PRESETS_AVAILABLE = False
    LMPPreset = Any  # type: ignore

    def get_preset(name: str):  # type: ignore
        raise ImportError("LMP presets are not available in this environment")

# Domain semantic classification (single source of truth)
from bsm.schemas.domain_ontology import DomainClass, classify_domain

# Structural enrichment (v4.1) — AlphaFold + DSSP/contacts/network
try:
    from .alphafold_client import AlphaFoldClient, AlphaFoldStructure
    ALPHAFOLD_CLIENT_AVAILABLE = True
except ImportError:
    ALPHAFOLD_CLIENT_AVAILABLE = False

try:
    from .structural_metrics import StructuralMetricsComputer, StructuralMetrics
    STRUCTURAL_METRICS_AVAILABLE = True
except ImportError:
    STRUCTURAL_METRICS_AVAILABLE = False


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
    STRING_API_BASE = "https://version-12-0.string-db.org/api"
    OPENTARGETS_API_BASE = "https://api.platform.opentargets.org/api/v4"
    CHEMBL_API_BASE = "https://www.ebi.ac.uk/chembl/api/data"
    KEGG_API_BASE = "https://rest.kegg.jp"
    REACTOME_API_BASE = "https://reactome.org/ContentService"
    PROTEIN_ATLAS_API_BASE = "https://www.proteinatlas.org"
    ENSEMBL_API_BASE = "https://rest.ensembl.org"
    GO_API_BASE = "https://api.geneontology.org/api"
    HPO_API_BASE = "https://ontology.jax.org/api/hp"
    GTEX_API_BASE = "https://gtexportal.org/api/v2"
    
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
        "n_terminal_myristoylation": {"G", "M"},  # N-terminal only (M = pre-cleavage init-Met)
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
        preset: Optional[str] = None,
        offline_mode: bool = False,
    ):
        """
        Initialize LMP generator
        
        Args:
            cache_dir: Directory for caching API responses
            rate_limit: Minimum seconds between API requests
            config_path: Path to YAML config file
            preset: Preset name for v4 unified mode (e.g., "nesy-core", "semantic")
            offline_mode: If True, disable all network fetching (use local data only)
        """
        self.logger = logging.getLogger(self.__class__.__name__)

        # v4 unified mode support
        self.preset = None
        if preset and PRESETS_AVAILABLE:
            self.preset = get_preset(preset)
        # NOTE: presets.py does not encode network requirements; offline_mode is explicit.
        self.offline_mode = bool(offline_mode)

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

        # ── Per-API rate-limit tracking (v4.3) ──
        # Each API has its own last-request-time and minimum interval
        self._api_rate_limits: Dict[str, float] = {
            "uniprot": 0.35,    # UniProt: ~3 req/s
            "pdb": 0.25,        # RCSB: generous
            "pubchem": 0.25,    # PubChem: 5 req/s documented
            "alphafold": 0.25,  # AlphaFold: generous
            "string": 1.0,      # STRING-DB: 1 req/s recommended
            "opentargets": 0.2, # OpenTargets GraphQL: generous
            "chembl": 0.35,     # ChEMBL: ~3 req/s
            "kegg": 0.35,       # KEGG: ~3 req/s (undocumented)
            "reactome": 0.25,   # Reactome: generous
            "protein_atlas": 0.5,  # HPA: conservative
            "ensembl": 0.07,    # Ensembl: 15 req/s
            "go": 0.2,          # QuickGO: generous
            "hpo": 0.5,         # JAX HPO: conservative
            "gtex": 0.5,        # GTEx: conservative
        }
        self._api_last_request: Dict[str, float] = {k: 0.0 for k in self._api_rate_limits}

        # ── Circuit breaker per API (v4.3) ──
        # If an API fails N consecutive times, skip it for a cooldown window
        self._api_fail_count: Dict[str, int] = {k: 0 for k in self._api_rate_limits}
        self._api_circuit_open_until: Dict[str, float] = {k: 0.0 for k in self._api_rate_limits}
        self._circuit_breaker_threshold: int = 3  # consecutive failures before tripping
        self._circuit_breaker_cooldown: float = 120.0  # seconds to wait before retrying

        # PubChem enrichment configuration (best-effort)
        pubchem_cfg: Dict[str, Any] = generator_cfg.get("pubchem", {}) if isinstance(generator_cfg.get("pubchem"), dict) else {}
        self.pubchem_enabled: bool = bool(pubchem_cfg.get("enabled", True))
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

        # ── Multi-API enrichment configuration (v4.2) ──
        # STRING-DB
        string_cfg = generator_cfg.get("string_db", {}) if isinstance(generator_cfg.get("string_db"), dict) else {}
        self.string_enabled: bool = bool(string_cfg.get("enabled", True))
        self.string_api_base: str = str(string_cfg.get("api_base", self.STRING_API_BASE)).rstrip("/")
        self.string_species: int = int(string_cfg.get("species", 9606))
        self.string_min_score: int = int(string_cfg.get("min_score", 400))
        self.string_max_partners: int = int(string_cfg.get("max_partners", 25))
        self.string_timeout: float = float(string_cfg.get("timeout_seconds", 15))

        # OpenTargets
        ot_cfg = generator_cfg.get("opentargets", {}) if isinstance(generator_cfg.get("opentargets"), dict) else {}
        self.opentargets_enabled: bool = bool(ot_cfg.get("enabled", True))
        self.opentargets_timeout: float = float(ot_cfg.get("timeout_seconds", 15))
        self.opentargets_max_diseases: int = int(ot_cfg.get("max_diseases", 20))

        # ChEMBL
        chembl_cfg = generator_cfg.get("chembl", {}) if isinstance(generator_cfg.get("chembl"), dict) else {}
        self.chembl_enabled: bool = bool(chembl_cfg.get("enabled", True))
        self.chembl_timeout: float = float(chembl_cfg.get("timeout_seconds", 15))
        self.chembl_max_activities: int = int(chembl_cfg.get("max_activities", 50))

        # KEGG
        kegg_cfg = generator_cfg.get("kegg", {}) if isinstance(generator_cfg.get("kegg"), dict) else {}
        self.kegg_enabled: bool = bool(kegg_cfg.get("enabled", True))
        self.kegg_timeout: float = float(kegg_cfg.get("timeout_seconds", 15))

        # Reactome
        reactome_cfg = generator_cfg.get("reactome", {}) if isinstance(generator_cfg.get("reactome"), dict) else {}
        self.reactome_enabled: bool = bool(reactome_cfg.get("enabled", True))
        self.reactome_timeout: float = float(reactome_cfg.get("timeout_seconds", 15))
        self.reactome_max_pathways: int = int(reactome_cfg.get("max_pathways", 30))

        # ProteinAtlas
        pa_cfg = generator_cfg.get("protein_atlas", {}) if isinstance(generator_cfg.get("protein_atlas"), dict) else {}
        self.protein_atlas_enabled: bool = bool(pa_cfg.get("enabled", True))
        self.protein_atlas_timeout: float = float(pa_cfg.get("timeout_seconds", 15))

        # Ensembl
        ensembl_cfg = generator_cfg.get("ensembl", {}) if isinstance(generator_cfg.get("ensembl"), dict) else {}
        self.ensembl_enabled: bool = bool(ensembl_cfg.get("enabled", True))
        self.ensembl_timeout: float = float(ensembl_cfg.get("timeout_seconds", 15))

        # GO enrichment
        go_cfg = generator_cfg.get("gene_ontology", {}) if isinstance(generator_cfg.get("gene_ontology"), dict) else {}
        self.go_enabled: bool = bool(go_cfg.get("enabled", True))
        self.go_timeout: float = float(go_cfg.get("timeout_seconds", 15))

        # HPO
        hpo_cfg = generator_cfg.get("hpo", {}) if isinstance(generator_cfg.get("hpo"), dict) else {}
        self.hpo_enabled: bool = bool(hpo_cfg.get("enabled", True))
        self.hpo_timeout: float = float(hpo_cfg.get("timeout_seconds", 15))

        # GTEx
        gtex_cfg = generator_cfg.get("gtex", {}) if isinstance(generator_cfg.get("gtex"), dict) else {}
        self.gtex_enabled: bool = bool(gtex_cfg.get("enabled", True))
        self.gtex_timeout: float = float(gtex_cfg.get("timeout_seconds", 15))

    def _ensure_smic_core_on_path(self) -> Optional[Path]:
        """Make `smic_core` importable (best-effort).

        In this repo layout, SMIC lives at `workers/smic/python/smic_core`, but
        `workers/smic/python` is not a Python package by default.
        """
        try:
            repo_root = Path(__file__).resolve().parents[3]
        except Exception:
            return None

        smic_python = repo_root / "workers" / "smic" / "python"
        if not smic_python.exists():
            return None

        smic_python_str = str(smic_python)
        if smic_python_str not in sys.path:
            sys.path.insert(0, smic_python_str)
        return smic_python

    # ------------------------------------------------------------------
    # PLIP subprocess isolation
    # ------------------------------------------------------------------
    def _run_plip_in_subprocess(
        self,
        *,
        smic_python: Path,
        pdb_path: Path,
        peptide_chain_spec: str,
        plip_overrides: Optional[Dict[str, Any]],
        pdb_id: str,
    ) -> Optional[Any]:
        """Run PLIP analysis in a child process to isolate C-level crashes.

        OpenBabel (used by PLIP internally) can call C-level ``exit(1)`` on
        certain PDB files (e.g. short peptide chains), which kills the entire
        Python process — no ``except``, no ``finally``, no ``faulthandler``
        can catch it.  Running in a subprocess ensures the *parent* survives.

        Returns a ``types.SimpleNamespace(interactions=[...])`` whose elements
        have the same attributes that ``analyze_pdb_file_with_plip`` returns
        (``chain_protein``, ``chain_ligand``, ``interaction_type``,
        ``residue_protein``, ``residue_ligand``), or ``None`` on failure.
        """
        import types as _types

        overrides_json = json.dumps(plip_overrides or {})

        # The child script tries the static-PDB PLIP path first, then the
        # MDTraj fallback — exactly mirroring the two paths that used to
        # live in the parent process.
        #
        # CRITICAL: we pre-populate ``sys.modules['smic_core']`` with a
        # minimal placeholder *before* importing the PLIP module so that
        # Python skips ``smic_core/__init__.py`` (which eagerly imports
        # heavy deps like matplotlib/visualization that can crash).
        child_script = (
            "import sys, json, types\n"
            "from pathlib import Path\n"
            "\n"
            "# Prevent smic_core/__init__.py from running (heavy deps)\n"
            f"_smic_python = Path({str(smic_python)!r})\n"
            "sys.path.insert(0, str(_smic_python))\n"
            "if 'smic_core' not in sys.modules:\n"
            "    _pkg = types.ModuleType('smic_core')\n"
            "    _pkg.__path__ = [str(_smic_python / 'smic_core')]\n"
            "    _pkg.__package__ = 'smic_core'\n"
            "    sys.modules['smic_core'] = _pkg\n"
            "if 'smic_core.md_analisys' not in sys.modules:\n"
            "    _sub = types.ModuleType('smic_core.md_analisys')\n"
            "    _sub.__path__ = [str(_smic_python / 'smic_core' / 'md_analisys')]\n"
            "    _sub.__package__ = 'smic_core.md_analisys'\n"
            "    sys.modules['smic_core.md_analisys'] = _sub\n"
            "\n"
            "use_mdtraj = False\n"
            "try:\n"
            "    from smic_core.md_analisys.analysis_interactions_plip import (\n"
            "        analyze_pdb_file_with_plip,\n"
            "    )\n"
            "except ImportError as exc:\n"
            "    use_mdtraj = True\n"
            "\n"
            "result = None\n"
            "if not use_mdtraj:\n"
            f"    overrides = json.loads({overrides_json!r})\n"
            "    try:\n"
            f"        result = analyze_pdb_file_with_plip(\n"
            f"            Path({str(pdb_path)!r}),\n"
            f"            peptide_chain_id={peptide_chain_spec!r},\n"
            "            config_overrides=(overrides or None),\n"
            "        )\n"
            "    except Exception as exc:\n"
            '        print(json.dumps({"error": str(exc), "interactions": []}))\n'
            "        sys.exit(0)\n"
            "else:\n"
            "    try:\n"
            "        import mdtraj as md\n"
            "        from smic_core.md_analisys.analysis_interactions_plip import (\n"
            "            analyze_frame_with_plip,\n"
            "        )\n"
            f"        traj = md.load_pdb({str(pdb_path)!r})\n"
            f"        result = analyze_frame_with_plip(traj, 0, peptide_chain_id={peptide_chain_spec!r})\n"
            "    except Exception as exc:\n"
            '        print(json.dumps({"error": str(exc), "interactions": []}))\n'
            "        sys.exit(0)\n"
            "\n"
            "interactions = []\n"
            "for rec in getattr(result, 'interactions', []) or []:\n"
            "    interactions.append({\n"
            '        "chain_protein": str(getattr(rec, "chain_protein", "") or ""),\n'
            '        "chain_ligand": str(getattr(rec, "chain_ligand", "") or ""),\n'
            '        "interaction_type": str(getattr(rec, "interaction_type", "") or ""),\n'
            '        "residue_protein": str(getattr(rec, "residue_protein", "") or ""),\n'
            '        "residue_ligand": str(getattr(rec, "residue_ligand", "") or ""),\n'
            "    })\n"
            'print(json.dumps({"interactions": interactions}))\n'
        )

        try:
            proc = subprocess.run(
                [sys.executable, "-c", child_script],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(Path(pdb_path).parent),
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("PLIP subprocess timed out for %s", pdb_id)
            return None
        except Exception as e:
            self.logger.warning("PLIP subprocess error for %s: %s", pdb_id, e)
            return None

        if proc.returncode != 0:
            stderr_snip = (proc.stderr or "").strip()[:300]
            self.logger.warning(
                "PLIP subprocess crashed (exit %d) for %s: %s",
                proc.returncode, pdb_id, stderr_snip,
            )
            return None

        try:
            data = json.loads(proc.stdout.strip())
        except (json.JSONDecodeError, ValueError) as e:
            self.logger.warning("PLIP subprocess bad JSON for %s: %s", pdb_id, e)
            return None

        if data.get("error"):
            self.logger.warning("PLIP analysis error for %s: %s", pdb_id, data["error"])

        interactions = [
            _types.SimpleNamespace(**rec_dict)
            for rec_dict in data.get("interactions", [])
        ]
        return _types.SimpleNamespace(interactions=interactions)

    # ------------------------------------------------------------------

    def _compute_plip_interchain_interfaces(
        self,
        *,
        pdb_id: str,
        pdb_path: Path,
        chains: Dict[str, Dict[str, Any]],
        peptide_max_len: int = 60,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Compute inter-chain interfaces using SMIC's PLIP integration.

        Returns mapping: chain_id -> list of Interface attribute dicts.
        Interface residues are PDB author residue numbers (digits-only) as
        required by LMP v4 XSD.
        """
        pdb_id_u = str(pdb_id).upper()
        out: Dict[str, List[Dict[str, Any]]] = {}

        smic_python = self._ensure_smic_core_on_path()
        if smic_python is None:
            return out

        # NOTE: PLIP imports moved to subprocess to avoid OpenBabel C-level exit(1) crashes.

        # Prefer explicit peptide candidates based on chain lengths (we have sequences from RCSB FASTA).
        peptide_candidates: List[str] = []
        for cid, cinfo in (chains or {}).items():
            seq = str((cinfo or {}).get("sequence") or "")
            if not seq:
                continue
            if 3 <= len(seq) <= int(peptide_max_len):
                peptide_candidates.append(str(cid).strip().upper())
        peptide_candidates = sorted({c for c in peptide_candidates if c})
        if not peptide_candidates:
            return out

        peptide_chain_spec = ",".join(peptide_candidates)

        # Optional PLIP config overrides (opt-in) to align with alternative detectors (e.g., RING4).
        plip_overrides: Dict[str, Any] = {}
        try:
            plip_cfg = self.config.get("plip", {}) if isinstance(self.config, dict) else {}
            if isinstance(plip_cfg, dict):
                preset = str(plip_cfg.get("preset", "") or "").strip().lower()
                if preset in {"ring4_relaxed", "ring4", "ring-like", "ring_like"}:
                    plip_overrides.update(
                        {
                            "PISTACK_DIST_MAX": 6.5,
                            "PISTACK_OFFSET_MAX": 3.5,
                            "PISTACK_ANG_DEV": 45,
                        }
                    )
                extra = plip_cfg.get("config_overrides_interchain")
                if isinstance(extra, dict):
                    plip_overrides.update({str(k): v for k, v in extra.items()})
        except Exception:
            plip_overrides = {}

        # --- SUBPROCESS ISOLATION ---
        # OpenBabel (used by PLIP) can call C-level exit(1) on certain PDBs,
        # killing the entire Python process.  Run in a subprocess so the parent survives.
        frame_res = self._run_plip_in_subprocess(
            smic_python=smic_python,
            pdb_path=pdb_path,
            peptide_chain_spec=peptide_chain_spec,
            plip_overrides=plip_overrides,
            pdb_id=pdb_id_u,
        )
        if frame_res is None:
            return out

        def _parse_reslabel(label: str) -> Tuple[Optional[str], Optional[int], str]:
            """Parse residue label like "PHE452" or "PHE 452" into (resname, resnum, raw_label)."""
            raw = str(label or "").strip()
            if not raw:
                return None, None, ""
            m = re.match(r"^\s*([A-Za-z]{3})\s*-?\s*(\d+)", raw)
            if not m:
                m = re.search(r"([A-Za-z]{3}).*?(\d+)", raw)
            if not m:
                return None, None, raw
            try:
                resname = str(m.group(1)).upper()
                resnum = int(m.group(2))
            except Exception:
                return None, None, raw
            if resnum <= 0:
                return resname or None, None, raw
            return resname or None, resnum, raw

        # Aggregate residues by (chain, partner_chain, interaction_type)
        residues: Dict[Tuple[str, str, str], Set[int]] = {}
        residue_meta: Dict[Tuple[str, str, str], Dict[Tuple[str, int], Dict[str, str]]] = {}
        for rec in getattr(frame_res, "interactions", []) or []:
            try:
                c_prot = str(getattr(rec, "chain_protein", "") or "").strip().upper()
                c_lig = str(getattr(rec, "chain_ligand", "") or "").strip().upper()
                itype = str(getattr(rec, "interaction_type", "") or "").strip().lower()
                rn_prot, r_prot, raw_prot = _parse_reslabel(getattr(rec, "residue_protein", "") or "")
                rn_lig, r_lig, raw_lig = _parse_reslabel(getattr(rec, "residue_ligand", "") or "")
            except Exception:
                continue

            if not itype:
                itype = "interchain"

            if c_prot and c_lig and r_prot is not None:
                k = (c_prot, c_lig, itype)
                residues.setdefault(k, set()).add(int(r_prot))
                residue_meta.setdefault(k, {})[("self", int(r_prot))] = {
                    "side": "self",
                    "chain": c_prot,
                    "resname": str(rn_prot or ""),
                    "resnum": str(int(r_prot)),
                    "label": str(raw_prot or (f"{rn_prot or ''}{int(r_prot)}")),
                }
            if c_prot and c_lig and r_lig is not None:
                k = (c_prot, c_lig, itype)
                residue_meta.setdefault(k, {})[("partner", int(r_lig))] = {
                    "side": "partner",
                    "chain": c_lig,
                    "resname": str(rn_lig or ""),
                    "resnum": str(int(r_lig)),
                    "label": str(raw_lig or (f"{rn_lig or ''}{int(r_lig)}")),
                }

            if c_lig and c_prot and r_lig is not None:
                k = (c_lig, c_prot, itype)
                residues.setdefault(k, set()).add(int(r_lig))
                residue_meta.setdefault(k, {})[("self", int(r_lig))] = {
                    "side": "self",
                    "chain": c_lig,
                    "resname": str(rn_lig or ""),
                    "resnum": str(int(r_lig)),
                    "label": str(raw_lig or (f"{rn_lig or ''}{int(r_lig)}")),
                }
            if c_lig and c_prot and r_prot is not None:
                k = (c_lig, c_prot, itype)
                residue_meta.setdefault(k, {})[("partner", int(r_prot))] = {
                    "side": "partner",
                    "chain": c_prot,
                    "resname": str(rn_prot or ""),
                    "resnum": str(int(r_prot)),
                    "label": str(raw_prot or (f"{rn_prot or ''}{int(r_prot)}")),
                }

        for (chain_id, partner_chain, itype), rset in residues.items():
            if not rset:
                continue
            res_list = ",".join(str(x) for x in sorted(rset))
            base = f"plip:{itype}" if itype else "plip:interchain"
            interaction_type = base + (":protein-peptide" if partner_chain in peptide_candidates else "")
            attrs = {
                "partner_protein": f"pdb:{pdb_id_u}",
                "partner_chain": str(partner_chain),
                "interface_residues": res_list,
                # Schema-controlled interface category
                "type": "protein-protein",
                # Detector-specific subtype + source
                "interaction_source": "plip",
                "interaction_type": interaction_type,
                # Optional structured residue metadata (schema allows child <Residue/>)
                "residue_elems": list((residue_meta.get((chain_id, partner_chain, itype), {}) or {}).values()),
            }
            out.setdefault(chain_id, []).append(attrs)

        return out

    def _compute_dssp_segments_smic(
        self,
        *,
        pdb_path: Path,
    ) -> Optional[Dict[str, Any]]:
        """Run DSSP via SMIC module (best-effort).

        Returns dict chain_id -> list[DSSPSegment] (as dataclasses) or None.

        IMPORTANT: we use ``importlib`` to load ``analysis_dssp.py`` directly
        instead of ``from smic_core.md_analisys.analysis_dssp import ...``
        because importing the ``smic_core`` *package* triggers its heavy
        ``__init__.py`` (visualization, mm_gbsa, etc.), some of whose
        C-level dependencies can call ``exit(1)`` and crash the host process.
        """
        smic_python = self._ensure_smic_core_on_path()
        if smic_python is None:
            return None
        try:
            import importlib.util as _ilu

            dssp_module_path = (
                Path(smic_python) / "smic_core" / "md_analisys" / "analysis_dssp.py"
            )
            if not dssp_module_path.exists():
                return None

            spec = _ilu.spec_from_file_location("_lmp_analysis_dssp", str(dssp_module_path))
            if spec is None or spec.loader is None:
                return None
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            run_dssp_segments = mod.run_dssp_segments  # type: ignore[attr-defined]

            smic_root = Path(smic_python) / "smic_core"
            return run_dssp_segments(Path(pdb_path), smic_root=smic_root)
        except Exception:
            return None

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

    @staticmethod
    def _looks_like_pdb_id(value: Optional[str]) -> bool:
        """Return True if value looks like a 4-char PDB ID (starts with digit)."""
        if not value:
            return False
        v = str(value).strip()
        if len(v) != 4:
            return False
        return bool(re.match(r"^[0-9][A-Za-z0-9]{3}$", v))
    
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
    
    # =============== v4 Unified Mode Helper Methods ===============

    def _preset_bool(self, primary_attr: str, *, fallback_attr: Optional[str] = None, default: bool = True) -> bool:
        """Compatibility shim for differing preset field names across versions."""
        if self.preset is None:
            return default
        if hasattr(self.preset, primary_attr):
            return bool(getattr(self.preset, primary_attr))
        if fallback_attr and hasattr(self.preset, fallback_attr):
            return bool(getattr(self.preset, fallback_attr))
        return default
    
    def _should_include_nesy(self) -> bool:
        """Check if NeSy annotations should be included based on preset"""
        if self.preset is None:
            return True  # v2-compat: always include
        return self._preset_bool("include_nesy_grammar", fallback_attr="include_nesy", default=True)
    
    def _should_include_structure(self) -> bool:
        """Check if structural data should be fetched/included"""
        if self.preset is None:
            return True  # v2-compat: always include
        return self._preset_bool("include_geometry", fallback_attr="include_structure", default=True)
    
    def _should_include_pubchem(self) -> bool:
        """Check if PubChem ligand enrichment should be done"""
        if self.preset is None:
            return True  # v2-compat: always include
        # v4 presets: include_features implies ligand/feature enrichment.
        return self._preset_bool("include_features", fallback_attr="include_ligands", default=True)
    
    def _can_fetch_network(self) -> bool:
        """Check if network fetching is allowed"""
        return not self.offline_mode

    # =============== v4 XML (LMP) Generation ===============

    LMP_V4_NS = "http://ai-university.edu/lmp/v4.0"

    def _lmp_tag(self, local: str) -> str:
        return f"{{{self.LMP_V4_NS}}}{local}"

    def _gzip_write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _download_pdb_topology(self, pdb_id: str, *, fmt: str = "pdb") -> Path:
        """Download a coordinate file suitable as a topology for SMIC IFP.

        Returns a local path (cached). No mocks.
        """
        pdb_id = (pdb_id or "").strip().upper()
        if not pdb_id:
            raise ValueError("pdb_id is required")

        fmt = fmt.lower().strip()
        if fmt not in {"pdb", "cif", "mmcif"}:
            raise ValueError(f"Unsupported topology format: {fmt}")
        ext = "pdb" if fmt == "pdb" else "cif"

        target_dir = self.cache_dir / "pdb_files"
        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / f"{pdb_id}.{ext}"
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path

        if not self._can_fetch_network():
            raise RuntimeError(f"Offline mode: cannot download PDB topology for {pdb_id}")

        # RCSB endpoints (retry/backoff handled by requests adapter at caller level)
        urls: List[str]
        if ext == "pdb":
            urls = [
                f"https://files.rcsb.org/download/{pdb_id}.pdb",
                f"https://files.rcsb.org/view/{pdb_id}.pdb",
            ]
        else:
            urls = [
                f"https://files.rcsb.org/download/{pdb_id}.cif",
                f"https://files.rcsb.org/view/{pdb_id}.cif",
            ]

        last_exc: Optional[Exception] = None
        for url in urls:
            for attempt in range(3):
                try:
                    self._rate_limit_wait("pdb")
                    resp = requests.get(url, timeout=20)
                    resp.raise_for_status()
                    out_path.write_bytes(resp.content)
                    if out_path.stat().st_size == 0:
                        raise RuntimeError(f"Downloaded empty file from {url}")
                    return out_path
                except Exception as e:
                    last_exc = e
                    wait_s = (2 ** attempt)
                    self.logger.warning("PDB download failed (%s) attempt %s/3: %s", url, attempt + 1, e)
                    time.sleep(wait_s)

        raise RuntimeError(f"Failed to download {pdb_id} topology ({ext}): {last_exc}")

    def _download_pdb_topology_temp(self, pdb_id: str, *, fmt: str = "pdb") -> Path:
        """Download a PDB/mmCIF to a temporary file (no cache)."""
        pdb_id = (pdb_id or "").strip().upper()
        if not pdb_id:
            raise ValueError("pdb_id is required")

        fmt = fmt.lower().strip()
        if fmt not in {"pdb", "cif", "mmcif"}:
            raise ValueError(f"Unsupported topology format: {fmt}")
        ext = "pdb" if fmt == "pdb" else "cif"

        if not self._can_fetch_network():
            raise RuntimeError(f"Offline mode: cannot download PDB topology for {pdb_id}")

        urls: List[str]
        if ext == "pdb":
            urls = [
                f"https://files.rcsb.org/download/{pdb_id}.pdb",
                f"https://files.rcsb.org/view/{pdb_id}.pdb",
            ]
        else:
            urls = [
                f"https://files.rcsb.org/download/{pdb_id}.cif",
                f"https://files.rcsb.org/view/{pdb_id}.cif",
            ]

        last_exc: Optional[Exception] = None
        for url in urls:
            for attempt in range(3):
                try:
                    self._rate_limit_wait("pdb")
                    resp = requests.get(url, timeout=20)
                    resp.raise_for_status()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tf:
                        tf.write(resp.content)
                        temp_path = Path(tf.name)
                    if temp_path.stat().st_size == 0:
                        raise RuntimeError(f"Downloaded empty file from {url}")
                    return temp_path
                except Exception as e:
                    last_exc = e
                    wait_s = (2 ** attempt)
                    self.logger.warning("PDB download failed (%s) attempt %s/3: %s", url, attempt + 1, e)
                    time.sleep(wait_s)

        raise RuntimeError(f"Failed to download {pdb_id} topology ({ext}): {last_exc}")

    def _import_smic_ifp_engine(self):
        """Import SMIC IFPEngine from workers tree (no mocks)."""
        repo_root = Path(__file__).resolve().parents[3]
        smic_python = repo_root / "workers" / "smic" / "python"
        if smic_python.exists() and str(smic_python) not in sys.path:
            sys.path.insert(0, str(smic_python))
        # Historical layout note: some branches keep the engine under
        # `smic_core/ifp_engine.py`, while this repo currently ships it under
        # `smic_core/md_analisys/ifp_engine.py`.
        try:
            from smic_core.ifp_engine import IFPEngine  # type: ignore
            return IFPEngine
        except Exception:
            from smic_core.md_analisys.ifp_engine import IFPEngine  # type: ignore
            return IFPEngine

    _WATER_RESNAMES = {"WAT", "HOH", "TIP3", "SOL"}
    _COMMON_IONS = {
        "NA",
        "CL",
        "K",
        "CA",
        "MG",
        "ZN",
        "MN",
        "FE",
        "CU",
        "CO",
        "NI",
        "CD",
        "HG",
    }

    def _discover_ligand_resnames(self, *, topology_path: Path, trajectory_path: Optional[Path]) -> List[str]:
        """Best-effort ligand discovery from a topology/trajectory.

        Returns a ranked list of candidate residue names (excluding protein/water/common ions).
        """
        # Fast path: parse topology PDB directly to avoid heavy MDAnalysis/SciPy imports.
        try:
            if topology_path.exists() and topology_path.suffix.lower() in {".pdb", ".ent"}:
                counts: Dict[str, int] = {}
                with topology_path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        # PDB fixed-width: resname at columns 18-20 (0-based 17:20)
                        if not line.startswith("HETATM"):
                            continue
                        rn = (line[17:20] or "").strip().upper()
                        if not rn or rn in self._WATER_RESNAMES or rn in self._COMMON_IONS:
                            continue
                        counts[rn] = counts.get(rn, 0) + 1

                if counts:
                    resnames = [rn for rn in counts.keys() if rn]
                    resnames.sort(key=lambda rn: counts.get(rn, 0), reverse=True)
                    return resnames
        except Exception:
            # Continue to MDAnalysis fallback.
            pass

        # MDAnalysis fallback (trajectory-aware)
        try:
            import MDAnalysis as mda  # type: ignore
        except Exception:
            return []

        try:
            if trajectory_path is not None:
                u = mda.Universe(str(topology_path), str(trajectory_path))
            else:
                u = mda.Universe(str(topology_path))

            candidates = u.select_atoms("not protein and not nucleic")
            if len(candidates) == 0:
                return []

            # Rank by total atoms per resname without repeated selections.
            counts = {}
            try:
                for r in candidates.residues:
                    rn = (getattr(r, "resname", "") or "").strip().upper()
                    if not rn or rn in self._WATER_RESNAMES or rn in self._COMMON_IONS:
                        continue
                    try:
                        counts[rn] = counts.get(rn, 0) + int(getattr(r.atoms, "n_atoms", len(r.atoms)))
                    except Exception:
                        counts[rn] = counts.get(rn, 0) + len(r.atoms)
            except Exception:
                # Fallback: still avoid O(n) selections per resname.
                for rn in sorted({str(x).strip().upper() for x in candidates.resnames if x}):
                    if rn and rn not in self._WATER_RESNAMES and rn not in self._COMMON_IONS:
                        counts[rn] = counts.get(rn, 0) + 1

            resnames = [rn for rn in counts.keys() if rn]
            resnames.sort(key=lambda rn: counts.get(rn, 0), reverse=True)
            return resnames
        except Exception:
            return []

    def _discover_ligand_chain_ids(self, *, topology_path: Path, trajectory_path: Optional[Path]) -> List[str]:
        """Best-effort complex ligand discovery via protein chains.

        For complexes without small-molecule ligands, treat a *different* protein chain
        (often a short peptide) as the ligand candidate.

        Returns chain/seg IDs ranked by increasing residue count (shorter first).
        """
        try:
            import MDAnalysis as mda  # type: ignore
        except Exception:
            return []

        try:
            if trajectory_path is not None:
                u = mda.Universe(str(topology_path), str(trajectory_path))
            else:
                u = mda.Universe(str(topology_path))

            protein = u.select_atoms("protein")
            if len(protein) == 0:
                return []

            chain_ids: List[str] = []
            try:
                chain_ids = sorted({str(x).strip() for x in getattr(protein, "chainIDs") if str(x).strip()})
            except Exception:
                chain_ids = []
            attr = "chainid"
            if not chain_ids:
                try:
                    chain_ids = sorted({str(x).strip() for x in getattr(protein, "segids") if str(x).strip()})
                except Exception:
                    chain_ids = []
                attr = "segid"

            if len(chain_ids) < 2:
                return []

            sizes: Dict[str, int] = {}
            for cid in chain_ids:
                try:
                    atoms = u.select_atoms(f"protein and {attr} {cid}")
                    sizes[cid] = len(getattr(atoms, "residues", []))
                except Exception:
                    sizes[cid] = sizes.get(cid, 0)

            # Prefer shorter chains first (peptides are usually the smallest partner)
            ranked = [cid for cid in chain_ids if cid]
            ranked.sort(key=lambda cid: sizes.get(cid, 10**9))
            return ranked
        except Exception:
            return []

    def _auto_select_receptor_chain(
        self,
        *,
        topology_path: Path,
        trajectory_path: Optional[Path],
        ligand_sel: str,
        default_sel: str = "protein",
    ) -> Tuple[str, Optional[str]]:
        """Pick a protein chain/segid closest to the ligand selection (first frame).

        Returns (receptor_sel, receptor_chain_id).
        """
        try:
            import MDAnalysis as mda  # type: ignore
            import numpy as np  # type: ignore
        except Exception:
            return default_sel, None

        try:
            if trajectory_path is not None:
                u = mda.Universe(str(topology_path), str(trajectory_path))
            else:
                u = mda.Universe(str(topology_path))

            ligand = u.select_atoms(ligand_sel)
            if len(ligand) == 0:
                return default_sel, None

            ligand_chain_ids: Set[str] = set()
            try:
                ligand_chain_ids = {str(x).strip() for x in getattr(ligand, "chainIDs") if str(x).strip()}
            except Exception:
                ligand_chain_ids = set()
            if not ligand_chain_ids:
                try:
                    ligand_chain_ids = {str(x).strip() for x in getattr(ligand, "segids") if str(x).strip()}
                except Exception:
                    ligand_chain_ids = set()

            def _sel_for(cid: str) -> Optional[str]:
                for kw in ("chainid", "segid"):
                    try:
                        test = u.select_atoms(f"protein and {kw} {cid}")
                        if len(test) > 0:
                            return f"protein and {kw} {cid}"
                    except Exception:
                        continue
                return None

            # Prefer the chain with the most close atom-atom contacts to the ligand selection (frame 0).
            # This is more reliable than centroid distance for multi-chain complexes.
            try:
                from MDAnalysis.lib.distances import capped_distance  # type: ignore

                ligand_pos = np.asarray(ligand.positions, dtype=float)
                if ligand_pos.size == 0:
                    return default_sel, None

                box = None
                try:
                    dims = getattr(u, "dimensions", None)
                    if dims is not None and len(dims) >= 3:
                        arr = np.asarray(dims, dtype=float)
                        if np.all(np.isfinite(arr[:3])) and np.any(arr[:3] > 0):
                            box = arr
                except Exception:
                    box = None

                protein = u.select_atoms("protein")
                if len(protein) == 0:
                    return default_sel, None

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
                if not chain_ids:
                    return default_sel, None

                best_sel: str = default_sel
                best_chain: Optional[str] = None
                best_pairs = -1
                best_min_d = float("inf")

                for cid in chain_ids:
                    if cid in ligand_chain_ids:
                        continue
                    sel = _sel_for(cid)
                    if sel is None:
                        continue
                    chain_atoms = u.select_atoms(sel)
                    if len(chain_atoms) == 0:
                        continue
                    chain_pos = np.asarray(chain_atoms.positions, dtype=float)
                    if chain_pos.size == 0:
                        continue

                    try:
                        pairs, dists = capped_distance(
                            chain_pos,
                            ligand_pos,
                            max_cutoff=6.0,
                            box=box,
                            return_distances=True,
                        )
                        pair_count = int(len(pairs))
                        min_d = float(np.min(dists)) if len(dists) else float("inf")
                    except Exception:
                        pair_count = 0
                        min_d = float("inf")

                    if pair_count > best_pairs or (pair_count == best_pairs and min_d < best_min_d):
                        best_pairs = pair_count
                        best_min_d = min_d
                        best_sel = sel
                        best_chain = cid

                if best_chain is not None:
                    return best_sel, best_chain
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

            # Fallback: centroid distance to avoid building a full distance matrix.
            try:
                ligand_center = np.asarray(ligand.positions, dtype=float).mean(axis=0)
            except Exception:
                return default_sel, None

            protein = u.select_atoms("protein")
            if len(protein) == 0:
                return default_sel, None

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
            if not chain_ids:
                return default_sel, None

            best_sel: str = default_sel
            best_chain: Optional[str] = None
            best_dist = float("inf")

            for cid in chain_ids:
                if cid in ligand_chain_ids:
                    continue
                sel = _sel_for(cid)
                if sel is None:
                    continue
                chain_atoms = u.select_atoms(sel)
                if len(chain_atoms) == 0:
                    continue
                try:
                    pos = np.asarray(chain_atoms.positions, dtype=float)
                    if pos.size == 0:
                        continue
                    # Min distance from chain atoms to ligand center.
                    min_d = float(np.linalg.norm(pos - ligand_center, axis=1).min())
                except Exception:
                    continue
                if math.isfinite(min_d) and min_d < best_dist:
                    best_dist = min_d
                    best_sel = sel
                    best_chain = cid

            return best_sel, best_chain
        except Exception:
            return default_sel, None

    def _compute_trajectory_ifp_smic(
        self,
        *,
        topology_path: Path,
        trajectory_path: Path,
        ligand_resname: Optional[str],
        stride: int = 1,
        max_frames: Optional[int] = None,
        receptor_sel: str = "protein",
        auto_ligand: bool = True,
        auto_chain: bool = True,
        detect_metals: bool = False,
        pdb_id: Optional[str] = None,
    ):
        """Compute trajectory IFP using SMIC (MDAnalysis-backed)."""
        if not Path(topology_path).exists():
            raise FileNotFoundError(f"Topology not found: {topology_path}")
        if not Path(trajectory_path).exists():
            raise FileNotFoundError(f"Trajectory not found: {trajectory_path}")
        raw_ligand = (ligand_resname or "").strip()
        ligand_sel_used: Optional[str] = None
        ligand_label: Optional[str] = None
        _chain_sel_cache: Dict[str, Optional[str]] = {}

        def _sel_for_chain(cid: str) -> Optional[str]:
            chain_id = (cid or "").strip()
            if not chain_id:
                return None
            cached = _chain_sel_cache.get(chain_id)
            if cached is not None:
                return cached

            candidates = [
                f"protein and chainid {chain_id}",
                f"protein and segid {chain_id}",
            ]

            try:
                import MDAnalysis as mda  # type: ignore

                try:
                    u = mda.Universe(str(topology_path), str(trajectory_path))
                except Exception:
                    u = mda.Universe(str(topology_path))

                for sel in candidates:
                    try:
                        atoms = u.select_atoms(sel)
                        if len(atoms) > 0:
                            _chain_sel_cache[chain_id] = sel
                            return sel
                    except Exception:
                        continue
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

            fallback = candidates[0]
            _chain_sel_cache[chain_id] = fallback
            return fallback

        # Allow passing complex ligand specs via the ligand_resname string:
        # - "CHAIN:P"  -> ligand selection is the protein chain P
        # - "SEL:<mda selection>" -> advanced selection
        raw_upper = raw_ligand.upper()
        if raw_upper.startswith("SEL:"):
            ligand_sel_used = raw_ligand[4:].strip()
            ligand_label = ligand_sel_used
        elif raw_upper.startswith("CHAIN:"):
            cid = raw_ligand.split(":", 1)[1].strip()
            ligand_sel_used = _sel_for_chain(cid)
            ligand_label = f"CHAIN:{cid}"
        elif raw_ligand:
            ligand_label = raw_upper
            ligand_sel_used = f"resname {raw_upper}"

        if ligand_sel_used is None and auto_ligand:
            # 1) Prefer true (non-protein) ligands
            candidates = self._discover_ligand_resnames(
                topology_path=Path(topology_path),
                trajectory_path=Path(trajectory_path),
            )
            if candidates:
                ligand_label = candidates[0]
                ligand_sel_used = f"resname {candidates[0]}"
            else:
                # 2) Fallback: treat a partner protein chain as the "ligand" (complex IFP)
                chain_candidates = self._discover_ligand_chain_ids(
                    topology_path=Path(topology_path),
                    trajectory_path=Path(trajectory_path),
                )
                if chain_candidates:
                    cid = chain_candidates[0]
                    ligand_sel_used = _sel_for_chain(cid)
                    ligand_label = f"CHAIN:{cid}"

        if not ligand_sel_used or not ligand_label:
            raise ValueError(
                "TrajectoryIFP requested but no ligand was specified and auto-discovery found no candidates "
                "(neither non-protein ligands nor partner protein chains)."
            )

        IFPEngine = self._import_smic_ifp_engine()
        engine = IFPEngine()

        # Apply max_frames by converting to end_frame and clamping to actual trajectory length.
        stride_i = max(1, int(stride))
        desired_end_frame: Optional[int] = None
        if max_frames is not None and int(max_frames) > 0:
            desired_end_frame = int(max_frames) * stride_i

        end_frame: Optional[int] = desired_end_frame
        try:
            import MDAnalysis as mda  # type: ignore

            u = mda.Universe(str(topology_path), str(trajectory_path))
            n_total = int(getattr(u.trajectory, "n_frames", len(u.trajectory)))
            if desired_end_frame is None:
                end_frame = n_total
            else:
                end_frame = min(desired_end_frame, n_total)
        except Exception:
            # Best-effort: if we can't introspect frames, rely on SMIC engine defaults.
            end_frame = desired_end_frame

        receptor_chain: Optional[str] = None
        receptor_sel_used = receptor_sel
        if auto_chain:
            receptor_sel_used, receptor_chain = self._auto_select_receptor_chain(
                topology_path=Path(topology_path),
                trajectory_path=Path(trajectory_path),
                ligand_sel=ligand_sel_used,
                default_sel=receptor_sel,
            )

        ifp_result = engine.generate_ifp(
            topology=str(topology_path),
            trajectory=str(trajectory_path),
            receptor_sel=receptor_sel_used,
            ligand_sel=ligand_sel_used,
            stride=stride_i,
            end_frame=end_frame,
            verbose=True,
            detect_metals=bool(detect_metals),
        )

        try:
            setattr(
                ifp_result,
                "mica_ifp_context",
                {
                    "engine": "smic_core.ifp_engine:IFPEngine",
                    "pdb_id": (pdb_id or "").upper() if pdb_id else None,
                    "topology": str(topology_path),
                    "trajectory": str(trajectory_path),
                    "ligand": ligand_label,
                    "ligand_sel": ligand_sel_used,
                    "receptor_sel": receptor_sel_used,
                    "receptor_chain": receptor_chain,
                    "stride": stride_i,
                    "end_frame": end_frame,
                    "detect_metals": bool(detect_metals),
                },
            )
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)

        return ifp_result, ligand_label

    def _map_smic_ifp_type_to_schema(self, ifp_type: str) -> str:
        mapping = {
            "HY": "Hydrophobic",
            "HD": "H-Bond",
            "HA": "H-Bond",
            "WB": "Water-Bridge",
            "IP": "Salt-Bridge",
            "IN": "Salt-Bridge",
            "IO": "Metal-Coordination",
            "HL": "Halogen-Bond",
            "AR": "Pi-Stacking",
        }
        return mapping.get((ifp_type or "").strip().upper(), "Hydrophobic")

    def _ifp_fingerprint_bits(self, mapped_types: Set[str]) -> str:
        order = [
            "H-Bond",
            "Hydrophobic",
            "Pi-Stacking",
            "Pi-Cation",
            "Salt-Bridge",
            "Water-Bridge",
            "Halogen-Bond",
            "Metal-Coordination",
        ]
        return "".join("1" if t in mapped_types else "0" for t in order)

    # =============== Structural Enrichment Blocks (v4.1) ===============

    def _get_alphafold_client(self) -> Optional["AlphaFoldClient"]:
        """Lazy-init AlphaFold client (reuses generator cache_dir)."""
        if not ALPHAFOLD_CLIENT_AVAILABLE:
            return None
        if not hasattr(self, "_alphafold_client"):
            self._alphafold_client = AlphaFoldClient(cache_dir=self.cache_dir / "alphafold")
        return self._alphafold_client

    def _get_structural_metrics_computer(self) -> Optional["StructuralMetricsComputer"]:
        """Lazy-init StructuralMetricsComputer."""
        if not STRUCTURAL_METRICS_AVAILABLE:
            return None
        if not hasattr(self, "_structural_metrics_computer"):
            self._structural_metrics_computer = StructuralMetricsComputer()
        return self._structural_metrics_computer

    def _add_alphafold_model_v4(
        self,
        geometry_elem: ET.Element,
        *,
        accession: str,
        domains: List[Dict[str, Any]],
    ) -> None:
        """Emit <AlphaFoldModel> with per-residue pLDDT and PAE summary."""
        client = self._get_alphafold_client()
        if client is None:
            self.logger.debug("AlphaFold client not available — skipping AlphaFoldModel block")
            return

        try:
            structure = client.get_structure_for_accession(accession)
        except Exception as e:
            self.logger.warning("AlphaFold fetch failed for %s: %s", accession, e)
            return
        if structure is None:
            return

        af_elem = ET.SubElement(geometry_elem, self._lmp_tag("AlphaFoldModel"))
        af_elem.set("entry_id", structure.meta.entry_id)
        af_elem.set("version", str(structure.meta.model_version))
        if structure.meta.confidence_avg_plddt is not None:
            af_elem.set("avg_plddt", f"{structure.meta.confidence_avg_plddt:.2f}")
        if structure.meta.model_created_date:
            af_elem.set("model_date", structure.meta.model_created_date)
        if structure.meta.uniprot_start is not None:
            af_elem.set("uniprot_start", str(structure.meta.uniprot_start))
        if structure.meta.uniprot_end is not None:
            af_elem.set("uniprot_end", str(structure.meta.uniprot_end))

        # ConfidencePerResidue
        if structure.plddt_per_residue:
            conf_elem = ET.SubElement(af_elem, self._lmp_tag("ConfidencePerResidue"))
            for resid, resname, plddt in structure.plddt_per_residue:
                res_elem = ET.SubElement(conf_elem, self._lmp_tag("Residue"))
                res_elem.set("id", str(resid))
                if resname:
                    res_elem.set("name", str(resname))
                res_elem.set("pLDDT", f"{plddt:.1f}")
                res_elem.set("confidence_class", client.plddt_confidence_class(plddt))

        # PAESummary
        if structure.pae_matrix is not None:
            pae_elem = ET.SubElement(af_elem, self._lmp_tag("PAESummary"))
            import numpy as np
            mean_pae = float(np.mean(structure.pae_matrix))
            max_pae = float(structure.max_pae) if structure.max_pae is not None else float(np.max(structure.pae_matrix))
            pae_elem.set("mean_pae", f"{mean_pae:.2f}")
            pae_elem.set("max_pae", f"{max_pae:.2f}")

            # Domain-pair PAE if domains are available
            if domains:
                domain_list = []
                for d in domains:
                    dname = d.get("name") or d.get("domain_name") or d.get("domain_id")
                    dstart = d.get("start")
                    dend = d.get("end")
                    if dname and dstart is not None and dend is not None:
                        try:
                            domain_list.append({
                                "name": str(dname),
                                "start": int(dstart),
                                "end": int(dend),
                            })
                        except (ValueError, TypeError):
                            continue
                if domain_list:
                    try:
                        dpae = client.compute_domain_pae(structure.pae_matrix, domain_list)
                        for pair in dpae:
                            dp_elem = ET.SubElement(pae_elem, self._lmp_tag("DomainPair"))
                            dp_elem.set("domain_a", pair["domain_a"])
                            dp_elem.set("domain_b", pair["domain_b"])
                            dp_elem.set("mean_pae", f"{pair['mean_pae']:.2f}")
                            dp_elem.set("confident_interface", "true" if pair["mean_pae"] < 10.0 else "false")
                    except Exception as e:
                        self.logger.debug("Domain PAE computation failed: %s", e)

    def _add_secondary_structure_block_v4(
        self,
        geometry_elem: ET.Element,
        *,
        pdb_path: str,
        source: str = "experimental",
    ) -> None:
        """Emit <SecondaryStructure> from DSSP via StructuralMetricsComputer."""
        computer = self._get_structural_metrics_computer()
        if computer is None:
            self.logger.debug("StructuralMetricsComputer not available — skipping SecondaryStructure block")
            return

        try:
            metrics = computer.compute_all(
                pdb_path,
                source=source,
                prefer_smic_static=self._preset_bool("prefer_smic_static", default=True),
            )
        except Exception as e:
            self.logger.warning("Structural metrics computation failed for %s: %s", pdb_path, e)
            return
        if metrics is None or metrics.secondary_structure is None:
            return

        dssp = metrics.secondary_structure
        ss_elem = ET.SubElement(geometry_elem, self._lmp_tag("SecondaryStructure"))
        ss_elem.set("method", str(getattr(dssp, "method", "dssp") or "dssp"))
        if dssp.composition:
            _frac_map = {"helix": "helix_fraction", "strand": "strand_fraction", "coil": "coil_fraction"}
            for key, attr_name in _frac_map.items():
                val = dssp.composition.get(key)
                if val is not None:
                    ss_elem.set(attr_name, f"{val:.3f}")

        for seg in dssp.segments:
            seg_elem = ET.SubElement(ss_elem, self._lmp_tag("Segment"))
            seg_elem.set("type", seg.ss_type)
            seg_elem.set("start", str(seg.start))
            seg_elem.set("end", str(seg.end))
            if seg.chain:
                seg_elem.set("chain", seg.chain)
            seg_elem.set("length", str(seg.end - seg.start + 1))

        # Stash metrics for downstream blocks (quality + network)
        self._last_structural_metrics = metrics
        metrics._source_path = pdb_path

    def _add_structural_quality_v4(
        self,
        geometry_elem: ET.Element,
        *,
        pdb_path: str,
        source: str = "experimental",
    ) -> None:
        """Emit <StructuralQuality> with Rg, Ramachandran, and ContactDensity."""
        # Reuse cached metrics from SecondaryStructure if available
        metrics = getattr(self, "_last_structural_metrics", None)
        if metrics is None or getattr(metrics, "_source_path", None) != pdb_path:
            computer = self._get_structural_metrics_computer()
            if computer is None:
                return
            try:
                metrics = computer.compute_all(
                    pdb_path,
                    source=source,
                    prefer_smic_static=self._preset_bool("prefer_smic_static", default=True),
                )
            except Exception as e:
                self.logger.warning("StructuralQuality computation failed: %s", e)
                return
        if metrics is None or metrics.quality is None:
            return

        q = metrics.quality
        sq_elem = ET.SubElement(geometry_elem, self._lmp_tag("StructuralQuality"))
        sq_elem.set("source", source)

        if q.rg is not None:
            rg_elem = ET.SubElement(sq_elem, self._lmp_tag("Rg"))
            rg_elem.set("value", f"{q.rg:.2f}")
            rg_elem.set("unit", "angstrom")

        if q.ramachandran_favored is not None:
            rama_elem = ET.SubElement(sq_elem, self._lmp_tag("Ramachandran"))
            rama_elem.set("favored", str(q.ramachandran_favored))
            rama_elem.set("allowed", str(q.ramachandran_allowed or 0))
            rama_elem.set("outlier", str(q.ramachandran_outlier or 0))
            total = (q.ramachandran_favored or 0) + (q.ramachandran_allowed or 0) + (q.ramachandran_outlier or 0)
            if total > 0:
                rama_elem.set("favored_pct", f"{(q.ramachandran_favored / total) * 100:.1f}")

        if q.contacts_per_residue is not None:
            cd_elem = ET.SubElement(sq_elem, self._lmp_tag("ContactDensity"))
            if metrics.contacts:
                cd_elem.set("total_contacts", str(len(metrics.contacts)))
            cd_elem.set("contacts_per_residue", f"{q.contacts_per_residue:.2f}")

    def _add_network_annotation_v4(
        self,
        geometry_elem: ET.Element,
        *,
        pdb_path: str,
        source: str = "experimental",
    ) -> None:
        """Emit <NetworkAnnotation> with hub residues from contact network centrality."""
        metrics = getattr(self, "_last_structural_metrics", None)
        if metrics is None or getattr(metrics, "_source_path", None) != pdb_path:
            computer = self._get_structural_metrics_computer()
            if computer is None:
                return
            try:
                metrics = computer.compute_all(
                    pdb_path,
                    source=source,
                    prefer_smic_static=self._preset_bool("prefer_smic_static", default=True),
                )
            except Exception as e:
                self.logger.warning("NetworkAnnotation computation failed: %s", e)
                return
        if metrics is None or not metrics.hub_residues:
            return

        na_elem = ET.SubElement(geometry_elem, self._lmp_tag("NetworkAnnotation"))
        for hub in metrics.hub_residues:
            hub_elem = ET.SubElement(na_elem, self._lmp_tag("Hub"))
            hub_elem.set("residue_id", str(hub.residue_id))
            if hub.chain:
                hub_elem.set("chain", hub.chain)
            hub_elem.set("betweenness", f"{hub.betweenness:.4f}")
            if hub.degree is not None:
                hub_elem.set("degree", f"{hub.degree:.4f}")
            hub_elem.set("allosteric_candidate", "true" if hub.allosteric_candidate else "false")

    def _add_pocket_sites_v4(
        self,
        geometry_elem: ET.Element,
        *,
        pdb_path: str,
        source: str = "experimental",
    ) -> None:
        """Emit <PocketSites> from SMIC-first static structural metrics."""
        metrics = getattr(self, "_last_structural_metrics", None)
        if metrics is None or getattr(metrics, "_source_path", None) != pdb_path:
            computer = self._get_structural_metrics_computer()
            if computer is None:
                return
            try:
                metrics = computer.compute_all(
                    pdb_path,
                    source=source,
                    prefer_smic_static=self._preset_bool("prefer_smic_static", default=True),
                )
            except Exception as e:
                self.logger.warning("PocketSites computation failed: %s", e)
                return
        if metrics is None or not getattr(metrics, "pocket_sites", None):
            return

        pockets_elem = ET.SubElement(geometry_elem, self._lmp_tag("PocketSites"))
        for pocket in metrics.pocket_sites:
            pocket_elem = ET.SubElement(pockets_elem, self._lmp_tag("PocketSite"))
            pocket_elem.set("id", str(getattr(pocket, "pocket_id", "pocket") or "pocket"))
            pocket_elem.set("rank", str(int(getattr(pocket, "rank", 0) or 0)))
            if getattr(pocket, "engine", None):
                pocket_elem.set("engine", str(pocket.engine))
            if getattr(pocket, "source", None):
                pocket_elem.set("source", str(pocket.source))
            pocket_elem.set("score", f"{float(getattr(pocket, 'score', 0.0) or 0.0):.3f}")
            pocket_elem.set("volume", f"{float(getattr(pocket, 'volume', 0.0) or 0.0):.3f}")
            pocket_elem.set("center_x", f"{float(getattr(pocket, 'center_x', 0.0) or 0.0):.3f}")
            pocket_elem.set("center_y", f"{float(getattr(pocket, 'center_y', 0.0) or 0.0):.3f}")
            pocket_elem.set("center_z", f"{float(getattr(pocket, 'center_z', 0.0) or 0.0):.3f}")
            pocket_elem.set("point_count", str(int(getattr(pocket, "point_count", 0) or 0)))
            pocket_elem.set("residue_count", str(int(getattr(pocket, "residue_count", 0) or 0)))
            pocket_elem.set("static", "true" if bool(getattr(pocket, "static", True)) else "false")

            for residue in getattr(pocket, "residues", []) or []:
                residue_elem = ET.SubElement(pocket_elem, self._lmp_tag("Residue"))
                residue_elem.set("residue_id", str(int(getattr(residue, "residue_id", 0) or 0)))
                if getattr(residue, "chain", None):
                    residue_elem.set("chain", str(residue.chain))
                if getattr(residue, "residue_name", None):
                    residue_elem.set("residue_name", str(residue.residue_name))

    def _resolve_structural_pdb_path(
        self,
        *,
        accession: str,
        pdb_ids: Optional[List[str]],
        prefer_cif: bool = False,
    ) -> Optional[str]:
        """Resolve a structural file path for downstream consumers.

        Priority:
        1. AlphaFold downloaded model (CIF if prefer_cif=True and available, else PDB)
        2. First experimental PDB from cache

        FlatProt needs CIF (for secondary structure extraction without DSSP).
        Other callers typically want PDB.
        """
        # Try AlphaFold first if available
        if self._preset_bool("alphafold_download_pdb", default=False):
            client = self._get_alphafold_client()
            if client is not None:
                try:
                    structure = client.get_structure_for_accession(
                        accession,
                        download_cif=prefer_cif,
                    )
                    if structure:
                        if prefer_cif and structure.cif_path:
                            return str(structure.cif_path)
                        if structure.pdb_path:
                            return str(structure.pdb_path)
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)

        # Fallback: experimental PDB
        if pdb_ids:
            try:
                pdb_path = self._download_pdb_topology(str(pdb_ids[0]).upper(), fmt="pdb")
                return str(pdb_path)
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

        return None

    def _coerce_structure_generation_receipt(self, raw: Any) -> Dict[str, Any]:
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _plddt_confidence_class(self, score: float) -> str:
        if score >= 90.0:
            return "very_high"
        if score >= 70.0:
            return "confident"
        if score >= 50.0:
            return "low"
        return "very_low"

    def _normalize_receipt_coverage_segments(
        self,
        raw_segments: Any,
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for segment in raw_segments or []:
            if not isinstance(segment, Mapping):
                continue
            try:
                start = int(segment.get("start") or 0)
                end = int(segment.get("end") or 0)
            except (TypeError, ValueError):
                continue
            if start <= 0 or end <= 0 or end < start:
                continue
            payload: Dict[str, Any] = {
                "start": start,
                "end": end,
            }
            for attr in (
                "chain_id",
                "auth_chain_id",
                "label_chain_id",
                "entity_id",
                "entity_type",
            ):
                value = segment.get(attr)
                if value not in {None, ""}:
                    payload[attr] = str(value)
            for attr in ("structure_start", "structure_end", "auth_start", "auth_end"):
                value = segment.get(attr)
                if value in {None, ""}:
                    continue
                try:
                    payload[attr] = int(value)
                except (TypeError, ValueError):
                    continue
            for attr in ("identity", "coverage"):
                value = segment.get(attr)
                if value in {None, ""}:
                    continue
                try:
                    payload[attr] = float(value)
                except (TypeError, ValueError):
                    continue
            normalized.append(payload)
        return normalized

    def _extract_uniprot_coverage_segments_from_sifts_payload(
        self,
        *,
        pdb_id: str,
        payload: Any,
        accession_filter: str = "",
    ) -> List[Dict[str, Any]]:
        pdb_lower = str(pdb_id or "").strip().lower()
        if not pdb_lower or not isinstance(payload, Mapping):
            return []

        root = payload.get(pdb_lower, {})
        if not isinstance(root, Mapping):
            return []
        uniprot_root = root.get("UniProt", root)
        if not isinstance(uniprot_root, Mapping):
            return []

        accession_candidates: Set[str] = set()
        normalized_accession = str(accession_filter or "").strip().upper()
        if normalized_accession:
            accession_candidates.add(normalized_accession)
            accession_candidates.add(normalized_accession.split("-")[0])

        segments: List[Dict[str, Any]] = []
        seen: Set[Tuple[Any, ...]] = set()
        for accession, entry in uniprot_root.items():
            accession_value = str(accession or "").strip().upper()
            if accession_candidates:
                accession_base = accession_value.split("-")[0]
                if accession_value not in accession_candidates and accession_base not in accession_candidates:
                    continue
            if not isinstance(entry, Mapping):
                continue
            for mapping in entry.get("mappings") or []:
                if not isinstance(mapping, Mapping):
                    continue
                try:
                    start = int(mapping.get("unp_start") or 0)
                    end = int(mapping.get("unp_end") or 0)
                except (TypeError, ValueError):
                    continue
                if start <= 0 or end <= 0 or end < start:
                    continue
                start_payload = mapping.get("start") if isinstance(mapping.get("start"), Mapping) else {}
                end_payload = mapping.get("end") if isinstance(mapping.get("end"), Mapping) else {}
                segment = {
                    "start": start,
                    "end": end,
                    "chain_id": str(mapping.get("struct_asym_id") or mapping.get("chain_id") or ""),
                    "auth_chain_id": str(mapping.get("chain_id") or mapping.get("struct_asym_id") or ""),
                    "label_chain_id": str(mapping.get("struct_asym_id") or mapping.get("chain_id") or ""),
                    "entity_id": str(mapping.get("entity_id") or ""),
                    "entity_type": "protein",
                }
                for attr, source in (
                    ("structure_start", start_payload.get("residue_number")),
                    ("structure_end", end_payload.get("residue_number")),
                    ("auth_start", start_payload.get("author_residue_number")),
                    ("auth_end", end_payload.get("author_residue_number")),
                ):
                    if source in {None, ""}:
                        continue
                    try:
                        segment[attr] = int(source)
                    except (TypeError, ValueError):
                        continue
                for attr in ("identity", "coverage"):
                    value = mapping.get(attr)
                    if value in {None, ""}:
                        continue
                    try:
                        segment[attr] = float(value)
                    except (TypeError, ValueError):
                        continue
                dedupe_key = (
                    accession_value,
                    segment.get("chain_id"),
                    segment["start"],
                    segment["end"],
                    segment.get("structure_start"),
                    segment.get("structure_end"),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                segments.append(segment)
        return segments

    def _fetch_uniprot_coverage_segments_for_pdb(
        self,
        pdb_id: str,
        *,
        accession_filter: str = "",
    ) -> List[Dict[str, Any]]:
        pdb_lower = str(pdb_id or "").strip().lower()
        if not pdb_lower:
            return []

        cache_file = self.cache_dir / f"pdbe_sifts_uniprot_{pdb_lower}.json"
        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                payload = json.loads(valid_cache.read_text(encoding="utf-8"))
                return self._extract_uniprot_coverage_segments_from_sifts_payload(
                    pdb_id=pdb_lower,
                    payload=payload,
                    accession_filter=accession_filter,
                )
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

        if not self._can_fetch_network():
            return []

        url = f"https://www.ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb_lower}"
        try:
            self._rate_limit_wait("pdb")
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                return []
            payload = resp.json()
            try:
                cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)
            return self._extract_uniprot_coverage_segments_from_sifts_payload(
                pdb_id=pdb_lower,
                payload=payload,
                accession_filter=accession_filter,
            )
        except Exception as exc:
            self.logger.warning("PDBe SIFTS UniProt coverage fetch failed for %s: %s", pdb_id, exc)
            return []

    def _receipt_residue_confidence_lookup(
        self,
        structure_generation_receipt: Optional[Mapping[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        receipt = self._coerce_structure_generation_receipt(structure_generation_receipt)
        lookup: Dict[int, Dict[str, Any]] = {}
        for raw_record in receipt.get("residue_confidence") or []:
            if not isinstance(raw_record, Mapping):
                continue
            try:
                position = int(raw_record.get("position") or 0)
            except (TypeError, ValueError):
                continue
            if position <= 0:
                continue
            payload: Dict[str, Any] = {}
            chain_id = raw_record.get("chain_id") or raw_record.get("chain")
            if chain_id not in {None, ""}:
                payload["chain"] = str(chain_id)
            plddt = raw_record.get("plddt")
            if plddt not in {None, ""}:
                try:
                    payload["confidence"] = float(plddt)
                    payload["confidence_source"] = str(raw_record.get("confidence_source") or "plddt")
                    payload["confidence_class"] = str(
                        raw_record.get("confidence_class")
                        or self._plddt_confidence_class(float(plddt))
                    )
                except (TypeError, ValueError):
                    pass
            mean_pae = raw_record.get("mean_pae")
            if mean_pae not in {None, ""}:
                try:
                    payload["mean_pae"] = float(mean_pae)
                except (TypeError, ValueError):
                    pass
            if payload:
                lookup[position] = payload
        return lookup

    def _build_structure_catalog_entries(
        self,
        *,
        primary_accession: str,
        sequence: str,
        pdb_ids: Optional[List[str]],
        structure_generation_receipt: Optional[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        receipt = self._coerce_structure_generation_receipt(structure_generation_receipt)

        if receipt:
            structure_name = str(
                receipt.get("primary_structure_name")
                or receipt.get("structure_name")
                or receipt.get("structure_name")
                or "serverless_structure_model"
            )
            seed_payload = self._coerce_structure_generation_receipt(
                receipt.get("biostate_seed_payload")
            )
            structure_ref = str(
                seed_payload.get("preferred_structure_ref")
                or receipt.get("structure_ref")
                or f"serverless:{re.sub(r'[^A-Za-z0-9_.:-]+', '_', structure_name)}"
            )
            chain_map = self._coerce_structure_generation_receipt(receipt.get("chain_map"))
            coverage_segments = self._normalize_receipt_coverage_segments(
                receipt.get("coverage_segments")
            )
            if not coverage_segments:
                for chain_id, chain_info in chain_map.items():
                    chain_payload = dict(chain_info) if isinstance(chain_info, Mapping) else {}
                    try:
                        sequence_length = int(chain_payload.get("sequence_length") or len(sequence))
                    except (TypeError, ValueError):
                        sequence_length = len(sequence)
                    if sequence_length <= 0:
                        continue
                    coverage_segments.append(
                        {
                            "start": 1,
                            "end": sequence_length,
                            "chain_id": str(chain_id),
                            "auth_chain_id": str(chain_payload.get("source_chain_id") or chain_id),
                            "label_chain_id": str(chain_payload.get("label_chain_id") or chain_id),
                            "entity_id": str(chain_payload.get("entity_id") or ""),
                            "entity_type": str(chain_payload.get("entity_type") or "protein"),
                        }
                    )
            if not coverage_segments and sequence:
                coverage_segments.append(
                    {
                        "start": 1,
                        "end": len(sequence),
                        "chain_id": "A",
                        "auth_chain_id": "A",
                        "label_chain_id": "A",
                        "entity_id": "",
                        "entity_type": "protein",
                    }
                )

            entries.append(
                {
                    "structure_ref": structure_ref,
                    "source_kind": "predicted_model",
                    "provider": str(receipt.get("provider") or "serverless"),
                    "provider_native_id": str(receipt.get("provider_native_id") or receipt.get("model_id") or ""),
                    "coordinate_accession_ref": str(
                        receipt.get("isoform_accession")
                        or receipt.get("canonical_accession")
                        or primary_accession
                    ),
                    "artifact_uri": str(receipt.get("structure_artifact_uri") or ""),
                    "format": str(receipt.get("structure_format") or ""),
                    "display_name": structure_name,
                    "representative": True,
                    "coverage_segments": coverage_segments,
                }
            )

        if primary_accession and self._preset_bool("include_alphafold", default=False):
            af_version = os.environ.get("ALPHAFOLD_MODEL_VERSION", "v6").strip() or "v6"
            entries.append(
                {
                    "structure_ref": f"alphafold:{primary_accession}",
                    "source_kind": "predicted_model",
                    "provider": "alphafold",
                    "provider_native_id": str(primary_accession),
                    "coordinate_accession_ref": str(primary_accession),
                    "artifact_uri": f"https://alphafold.ebi.ac.uk/files/AF-{primary_accession}-F1-model_{af_version}.pdb",
                    "format": "pdb",
                    "display_name": f"AF-{primary_accession}",
                    "representative": not entries,
                    "coverage_segments": [
                        {
                            "start": 1,
                            "end": len(sequence),
                            "chain_id": "A",
                            "auth_chain_id": "A",
                            "label_chain_id": "A",
                            "entity_id": "",
                            "entity_type": "protein",
                        }
                    ] if sequence else [],
                }
            )

        seen_pdb: Set[str] = set()
        for pdb_id in pdb_ids or []:
            normalized = str(pdb_id or "").strip().upper()
            if not normalized or normalized in seen_pdb:
                continue
            seen_pdb.add(normalized)
            entries.append(
                {
                    "structure_ref": f"pdb:{normalized}",
                    "source_kind": "experimental_pdb",
                    "provider": "pdbe",
                    "provider_native_id": normalized,
                    "coordinate_accession_ref": str(primary_accession),
                    "artifact_uri": f"https://files.rcsb.org/download/{normalized}.cif",
                    "format": "mmcif",
                    "display_name": normalized,
                    "representative": not entries,
                    "coverage_segments": self._fetch_uniprot_coverage_segments_for_pdb(
                        normalized,
                        accession_filter=primary_accession,
                    ),
                }
            )

        if entries and not any(bool(entry.get("representative")) for entry in entries):
            entries[0]["representative"] = True
        return entries

    def _add_structure_catalog_v4(
        self,
        geometry_elem: ET.Element,
        *,
        primary_accession: str,
        sequence: str,
        pdb_ids: Optional[List[str]],
        structure_generation_receipt: Optional[Mapping[str, Any]],
    ) -> str:
        entries = self._build_structure_catalog_entries(
            primary_accession=primary_accession,
            sequence=sequence,
            pdb_ids=pdb_ids,
            structure_generation_receipt=structure_generation_receipt,
        )
        if not entries:
            return ""

        catalog_elem = ET.SubElement(geometry_elem, self._lmp_tag("StructureCatalog"))
        structure_set = ET.SubElement(geometry_elem, self._lmp_tag("StructureSet"))
        representative_ref = ""
        coordinate_accession_ref = str(primary_accession or "")

        for entry in entries:
            structure_elem = ET.SubElement(catalog_elem, self._lmp_tag("Structure"))
            for attr in (
                "structure_ref",
                "source_kind",
                "provider",
                "provider_native_id",
                "coordinate_accession_ref",
                "artifact_uri",
                "format",
                "display_name",
            ):
                value = entry.get(attr)
                if value not in {None, ""}:
                    structure_elem.set(attr, str(value))
            if "representative" in entry:
                structure_elem.set("representative", "true" if bool(entry.get("representative")) else "false")
            if bool(entry.get("representative")) and not representative_ref:
                representative_ref = str(entry.get("structure_ref") or "")
            if entry.get("coordinate_accession_ref"):
                coordinate_accession_ref = str(entry.get("coordinate_accession_ref"))
            for segment in entry.get("coverage_segments") or []:
                if not isinstance(segment, Mapping):
                    continue
                try:
                    start = int(segment.get("start") or 0)
                    end = int(segment.get("end") or 0)
                except (TypeError, ValueError):
                    continue
                if start <= 0 or end <= 0 or end < start:
                    continue
                segment_elem = ET.SubElement(structure_elem, self._lmp_tag("CoverageSegment"))
                segment_elem.set("start", str(start))
                segment_elem.set("end", str(end))
                for attr in (
                    "chain_id",
                    "auth_chain_id",
                    "label_chain_id",
                    "entity_id",
                    "entity_type",
                    "structure_start",
                    "structure_end",
                    "auth_start",
                    "auth_end",
                    "identity",
                    "coverage",
                ):
                    value = segment.get(attr)
                    if value not in {None, ""}:
                        segment_elem.set(attr, str(value))

            member_elem = ET.SubElement(structure_set, self._lmp_tag("StructureMember"))
            member_elem.set("structure_ref", str(entry.get("structure_ref") or ""))
            member_elem.set("role", "representative" if bool(entry.get("representative")) else "supporting")

        if not representative_ref and entries:
            representative_ref = str(entries[0].get("structure_ref") or "")
        if representative_ref:
            structure_set.set("representative_structure_ref", representative_ref)
        if coordinate_accession_ref:
            structure_set.set("coordinate_accession_ref", coordinate_accession_ref)
        return representative_ref

    def _secondary_structure_lookup_for_statistics(self) -> Dict[int, str]:
        lookup: Dict[int, str] = {}
        metrics = getattr(self, "_last_structural_metrics", None)
        secondary_structure = getattr(metrics, "secondary_structure", None) if metrics is not None else None
        for segment in getattr(secondary_structure, "segments", []) or []:
            try:
                start = int(getattr(segment, "start", 0) or 0)
                end = int(getattr(segment, "end", 0) or 0)
            except (TypeError, ValueError):
                continue
            if start <= 0 or end <= 0 or end < start:
                continue
            ss_type = str(getattr(segment, "ss_type", "") or "")
            for position in range(start, end + 1):
                lookup[position] = ss_type
        return lookup

    def _add_residue_statistics_v4(
        self,
        geometry_elem: ET.Element,
        *,
        sequence: str,
        structure_ref: str,
        structure_generation_receipt: Optional[Mapping[str, Any]],
    ) -> None:
        if not sequence or not structure_ref:
            return

        receipt = self._coerce_structure_generation_receipt(structure_generation_receipt)
        chain_map = self._coerce_structure_generation_receipt(receipt.get("chain_map"))
        default_chain = next(iter(chain_map.keys()), "A") if chain_map else "A"
        confidence_summary = self._coerce_structure_generation_receipt(receipt.get("confidence_summary"))
        average_confidence = confidence_summary.get("avg_plddt") or confidence_summary.get("average_confidence")
        try:
            normalized_confidence = float(average_confidence) if average_confidence is not None else None
        except (TypeError, ValueError):
            normalized_confidence = None
        residue_confidence_lookup = self._receipt_residue_confidence_lookup(
            structure_generation_receipt
        )

        secondary_structure_lookup = self._secondary_structure_lookup_for_statistics()
        hub_lookup: Dict[int, Any] = {}
        metrics = getattr(self, "_last_structural_metrics", None)
        if metrics is not None:
            for hub in getattr(metrics, "hub_residues", []) or []:
                try:
                    hub_lookup[int(getattr(hub, "residue_id", 0) or 0)] = hub
                except (TypeError, ValueError):
                    continue

        stats_elem = ET.SubElement(geometry_elem, self._lmp_tag("ResidueStatistics"))
        for index, amino_acid in enumerate(sequence, start=1):
            stat_elem = ET.SubElement(stats_elem, self._lmp_tag("ResidueStat"))
            stat_elem.set("position", str(index))
            stat_elem.set("amino_acid", str(amino_acid))
            stat_elem.set("structure_ref", structure_ref)
            residue_confidence = residue_confidence_lookup.get(index, {})
            stat_elem.set("chain", str(residue_confidence.get("chain") or default_chain))
            ss_type = secondary_structure_lookup.get(index)
            if ss_type:
                stat_elem.set("secondary_structure", ss_type)
            residue_specific_confidence = residue_confidence.get("confidence")
            if residue_specific_confidence is not None:
                stat_elem.set("confidence", f"{float(residue_specific_confidence):.2f}")
                if residue_confidence.get("confidence_source"):
                    stat_elem.set("confidence_source", str(residue_confidence.get("confidence_source")))
                if residue_confidence.get("confidence_class"):
                    stat_elem.set("confidence_class", str(residue_confidence.get("confidence_class")))
            elif normalized_confidence is not None:
                stat_elem.set("confidence", f"{normalized_confidence:.2f}")
            residue_mean_pae = residue_confidence.get("mean_pae")
            if residue_mean_pae is not None:
                stat_elem.set("mean_pae", f"{float(residue_mean_pae):.2f}")
            hub = hub_lookup.get(index)
            if hub is not None:
                betweenness = getattr(hub, "betweenness", None)
                degree = getattr(hub, "degree", None)
                if betweenness is not None:
                    stat_elem.set("hub_score", f"{float(betweenness):.4f}")
                if degree is not None:
                    stat_elem.set("contact_degree", f"{float(degree):.4f}")

    def _add_dynamics_statistics_v4(
        self,
        geometry_elem: ET.Element,
        *,
        dynamic_statistics_receipt: Optional[Mapping[str, Any]],
    ) -> None:
        payload = normalize_dynamic_statistics_receipt(dynamic_statistics_receipt)
        if not payload:
            return

        runs = payload.get("run_metadata") or payload.get("runs") or []
        dataset_refs = payload.get("dataset_refs") or payload.get("datasets") or []
        residue_stats = payload.get("residue_stats") or payload.get("residue_dynamic_stats") or []
        pair_stats = payload.get("pair_stats") or payload.get("pair_dynamic_stats") or []
        if not any([runs, dataset_refs, residue_stats, pair_stats]):
            return

        dyn_elem = ET.SubElement(geometry_elem, self._lmp_tag("DynamicsStatistics"))
        if payload.get("source_kind"):
            dyn_elem.set("source_kind", str(payload.get("source_kind")))

        run_records = [runs] if isinstance(runs, Mapping) else [r for r in runs if isinstance(r, Mapping)]
        for run in run_records:
            run_elem = ET.SubElement(dyn_elem, self._lmp_tag("RunMetadata"))
            for key in (
                "run_id",
                "engine",
                "topology_ref",
                "trajectory_ref",
                "replica_id",
                "replica_count",
                "ensemble_id",
                "force_field",
                "solvent_model",
                "n_frames",
                "stride",
                "time_step_ps",
                "duration_ns",
                "temperature_k",
            ):
                value = run.get(key)
                if value not in {None, ""}:
                    run_elem.set(key, str(value))

        dataset_records = [dataset_refs] if isinstance(dataset_refs, Mapping) else [d for d in dataset_refs if isinstance(d, Mapping)]
        for dataset_ref in dataset_records:
            dataset = dataset_ref.get("dataset")
            if not dataset:
                continue
            dataset_elem = ET.SubElement(dyn_elem, self._lmp_tag("DatasetReference"))
            dataset_elem.set("dataset", str(dataset))
            for key in ("record_id", "split", "source_uri"):
                value = dataset_ref.get(key)
                if value not in {None, ""}:
                    dataset_elem.set(key, str(value))

        residue_records = [residue_stats] if isinstance(residue_stats, Mapping) else [r for r in residue_stats if isinstance(r, Mapping)]
        for residue_stat in residue_records:
            position = residue_stat.get("position")
            if position in {None, ""}:
                continue
            residue_elem = ET.SubElement(dyn_elem, self._lmp_tag("ResidueDynamicStat"))
            residue_elem.set("position", str(position))
            for key in (
                "chain",
                "rmsf",
                "sasa_mean",
                "sasa_std",
                "secondary_structure",
                "normal_mode_low",
                "normal_mode_mid",
                "normal_mode_high",
            ):
                value = residue_stat.get(key)
                if value not in {None, ""}:
                    residue_elem.set(key, str(value))

        pair_records = [pair_stats] if isinstance(pair_stats, Mapping) else [p for p in pair_stats if isinstance(p, Mapping)]
        for pair_stat in pair_records:
            pos_i = pair_stat.get("position_i")
            pos_j = pair_stat.get("position_j")
            if pos_i in {None, ""} or pos_j in {None, ""}:
                continue
            pair_elem = ET.SubElement(dyn_elem, self._lmp_tag("PairDynamicStat"))
            pair_elem.set("position_i", str(pos_i))
            pair_elem.set("position_j", str(pos_j))
            for key in (
                "chain_i",
                "chain_j",
                "vdw",
                "hbbb",
                "hbsb",
                "hbss",
                "hydrophobic",
                "salt_bridge",
                "pi_cation",
                "pi_stacking",
                "t_stacking",
                "motion_correlation",
                "normal_mode_low",
                "normal_mode_mid",
                "normal_mode_high",
            ):
                value = pair_stat.get(key)
                if value not in {None, ""}:
                    pair_elem.set(key, str(value))

    def _extract_feature_span(self, feature: Mapping[str, Any]) -> Tuple[int, int]:
        location = feature.get("location") if isinstance(feature, Mapping) else {}
        start = 0
        end = 0
        if isinstance(location, Mapping):
            start_raw = location.get("start", {})
            end_raw = location.get("end", {})
            if isinstance(start_raw, Mapping):
                start_raw = start_raw.get("value")
            if isinstance(end_raw, Mapping):
                end_raw = end_raw.get("value")
            try:
                start = int(start_raw or 0)
                end = int(end_raw or 0)
            except (TypeError, ValueError):
                start = 0
                end = 0
        return start, end

    def _infer_membrane_segments(self, uniprot_data: Mapping[str, Any]) -> List[Dict[str, Any]]:
        segments: List[Dict[str, Any]] = []
        for feature in uniprot_data.get("features", []) or []:
            if not isinstance(feature, Mapping):
                continue
            feature_type = str(feature.get("type") or "").strip().lower()
            description = str(feature.get("description") or "")
            if "transmembrane" not in feature_type and "transmembrane" not in description.lower():
                continue
            start, end = self._extract_feature_span(feature)
            if start <= 0 or end <= 0 or end < start:
                continue
            orientation = ""
            lowered = description.lower()
            if "cytoplasm" in lowered or "cytosolic" in lowered:
                orientation = "cytosolic"
            elif "extracellular" in lowered or "luminal" in lowered:
                orientation = "extracellular"
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "kind": "transmembrane_segment",
                    "orientation": orientation,
                    "evidence_source": "uniprot_feature",
                    "confidence": "reported",
                }
            )
        return segments

    def _collect_cell_context_hints(
        self,
        *,
        uniprot_data: Mapping[str, Any],
        membrane_segments: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        hints: List[Dict[str, str]] = []

        subcellular_raw = str(uniprot_data.get("Subcellular location") or "").strip()
        if subcellular_raw:
            hints.append(
                {
                    "type": "subcellular_location",
                    "value": subcellular_raw,
                    "source": "uniprot_payload",
                }
            )

        for comment in uniprot_data.get("comments", []) or []:
            if not isinstance(comment, Mapping):
                continue
            if str(comment.get("commentType") or "").upper() != "SUBCELLULAR LOCATION":
                continue
            texts = comment.get("texts") or []
            if not isinstance(texts, list):
                continue
            for item in texts:
                if not isinstance(item, Mapping):
                    continue
                value = str(item.get("value") or "").strip()
                if value:
                    hints.append(
                        {
                            "type": "subcellular_location",
                            "value": value,
                            "source": "uniprot_comment",
                        }
                    )

        for keyword in uniprot_data.get("keywords", []) or []:
            keyword_value = str(keyword or "").strip()
            if keyword_value and "membrane" in keyword_value.lower():
                hints.append(
                    {
                        "type": "keyword",
                        "value": keyword_value,
                        "source": "uniprot_keyword",
                    }
                )

        if membrane_segments:
            hints.append(
                {
                    "type": "membrane_required",
                    "value": "true",
                    "source": "membrane_topology",
                }
            )

        deduped: List[Dict[str, str]] = []
        seen: Set[Tuple[str, str, str]] = set()
        for hint in hints:
            key = (hint.get("type", ""), hint.get("value", ""), hint.get("source", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(hint)
        return deduped

    def _add_membrane_topology_v4(
        self,
        geometry_elem: ET.Element,
        *,
        structure_ref: str,
        membrane_segments: List[Dict[str, Any]],
    ) -> None:
        if not membrane_segments:
            return
        topology_elem = ET.SubElement(geometry_elem, self._lmp_tag("MembraneTopology"))
        for segment in membrane_segments:
            segment_elem = ET.SubElement(topology_elem, self._lmp_tag("Segment"))
            segment_elem.set("structure_ref", structure_ref)
            segment_elem.set("start", str(segment["start"]))
            segment_elem.set("end", str(segment["end"]))
            for attr in ("kind", "orientation", "evidence_source", "confidence"):
                value = segment.get(attr)
                if value:
                    segment_elem.set(attr, str(value))

    def _add_cell_context_hints_v4(
        self,
        geometry_elem: ET.Element,
        *,
        uniprot_data: Mapping[str, Any],
        membrane_segments: List[Dict[str, Any]],
    ) -> None:
        hints = self._collect_cell_context_hints(
            uniprot_data=uniprot_data,
            membrane_segments=membrane_segments,
        )
        if not hints:
            return
        hints_elem = ET.SubElement(geometry_elem, self._lmp_tag("CellContextHints"))
        for hint in hints:
            hint_elem = ET.SubElement(hints_elem, self._lmp_tag("Hint"))
            hint_elem.set("type", hint["type"])
            hint_elem.set("value", hint["value"])
            if hint.get("source"):
                hint_elem.set("source", hint["source"])

    def _add_topology_hooks_v4(
        self,
        geometry_elem: ET.Element,
        *,
        preferred_structure_ref: str,
        primary_accession: str,
        membrane_segments: List[Dict[str, Any]],
        structure_generation_receipt: Optional[Mapping[str, Any]],
    ) -> None:
        if not preferred_structure_ref:
            return

        receipt = self._coerce_structure_generation_receipt(structure_generation_receipt)
        chain_map = self._coerce_structure_generation_receipt(receipt.get("chain_map"))
        ligand_context = self._coerce_structure_generation_receipt(receipt.get("ligand_context"))
        hooks_elem = ET.SubElement(geometry_elem, self._lmp_tag("TopologyHooks"))
        membrane_required = bool(membrane_segments)

        hook_elem = ET.SubElement(hooks_elem, self._lmp_tag("Hook"))
        hook_elem.set("preferred_structure_ref", preferred_structure_ref)
        hook_elem.set("component_id", f"protein:{primary_accession}")
        hook_elem.set("entity_type", "protein")
        hook_elem.set("membrane_required", "true" if membrane_required else "false")
        if ligand_context:
            hook_elem.set("ligand_parameterization_target", "input_context")

        for chain_id, chain_info in list(chain_map.items())[:8]:
            chain_payload = dict(chain_info) if isinstance(chain_info, Mapping) else {}
            chain_hook = ET.SubElement(hooks_elem, self._lmp_tag("Hook"))
            chain_hook.set("preferred_structure_ref", preferred_structure_ref)
            chain_hook.set("component_id", f"protein:{primary_accession}")
            chain_hook.set("chain_id", str(chain_id))
            chain_hook.set("entity_type", str(chain_payload.get("entity_type") or "protein"))
            chain_hook.set("membrane_required", "true" if membrane_required else "false")

        for segment in membrane_segments:
            segment_hook = ET.SubElement(hooks_elem, self._lmp_tag("Hook"))
            segment_hook.set("preferred_structure_ref", preferred_structure_ref)
            segment_hook.set("component_id", f"protein:{primary_accession}")
            segment_hook.set("entity_type", "protein")
            segment_hook.set("membrane_required", "true")
            segment_hook.set(
                "topology_region_id",
                f"membrane:{segment['start']}-{segment['end']}",
            )

    def _add_visuals_v4(
        self,
        geometry_elem: ET.Element,
        *,
        accession: str,
        pdb_ids: Optional[List[str]] = None,
    ) -> None:
        """Emit <Visuals> with AlphaFold + PDB + InterPro URLs for the FE protein card.

        Contract (consumed by ``LMPv4ContextExtractor._extract_geometry`` and
        the FE ``StructureTab``):

            <lmp:Visuals>
              <lmp:Visual kind="af_cif" source="alphafold" url="..."/>
              <lmp:Visual kind="af_pdb" source="alphafold" url="..."/>
              <lmp:Visual kind="af_pae_png" source="alphafold" preview_url="..." avg_plddt="87.3"/>
              <lmp:Visual kind="af_entry_page" source="alphafold" url="https://alphafold.ebi.ac.uk/entry/{acc}"/>
              <lmp:Visual kind="pdb_assembly_jpeg" source="rcsb" pdb_id="4PWN" preview_url="..."/>
              <lmp:Visual kind="pdb_front_image" source="pdbe" pdb_id="4PWN" preview_url="..."/>
              <lmp:Visual kind="pdb_bcif_proxy" source="mica" pdb_id="4PWN" url="/api/v1/structure/bcif?pdb_id=4PWN"/>
              <lmp:Visual kind="interpro_domain_graphic" source="interpro" url="..."/>
              <lmp:Visual kind="flatprot_svg" source="flatprot" url="..."/>
            </lmp:Visuals>

        All URLs are deterministic (no network calls). AlphaFold URLs require a
        valid UniProt accession; PDB URLs require a 4-char PDB id. FlatProt is
        only emitted if the binary is available locally AND a structure file
        was already downloaded (reuses the AlphaFold cache path).
        """
        acc = (accession or "").strip().upper()
        acc_is_uniprot = bool(re.match(r"^[A-Z0-9]{4,12}$", acc)) if acc else False

        visuals_elem = ET.SubElement(geometry_elem, self._lmp_tag("Visuals"))
        emitted_any = False

        # -- AlphaFold (deterministic URL pattern) ---------------------------
        if acc_is_uniprot:
            # AF EBI bumped v4 -> v6 in 2025. Override via env if it bumps again.
            af_version = os.environ.get("ALPHAFOLD_MODEL_VERSION", "v6").strip() or "v6"
            af_stem = f"AF-{acc}-F1-model_{af_version}"
            af_entry_id = f"AF-{acc}-F1"

            # Try to pull real metadata (avg_plddt + live URLs) if AF client is enabled;
            # graceful: if unavailable we still emit deterministic URLs at af_version.
            avg_plddt: Optional[float] = None
            real_cif_url: Optional[str] = None
            real_pdb_url: Optional[str] = None
            try:
                client = self._get_alphafold_client()
                if client is not None:
                    meta_list = client.fetch_prediction(acc)
                    if meta_list:
                        m = meta_list[0]
                        if m.confidence_avg_plddt:
                            avg_plddt = float(m.confidence_avg_plddt)
                        real_cif_url = (m.cif_url or "").strip() or None
                        real_pdb_url = (m.pdb_url or "").strip() or None
                        if m.model_version and int(m.model_version) > 0:
                            af_stem = f"AF-{acc}-F1-model_v{int(m.model_version)}"
                            af_version = f"v{int(m.model_version)}"
            except Exception as _exc:  # noqa: BLE001 — graceful
                self.logger.debug("AF meta fetch during Visuals failed: %s", _exc)

            cif_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            cif_v.set("kind", "af_cif")
            cif_v.set("source", "alphafold")
            cif_v.set("entry_id", af_entry_id)
            cif_v.set("url", real_cif_url or f"https://alphafold.ebi.ac.uk/files/{af_stem}.cif")

            pdb_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            pdb_v.set("kind", "af_pdb")
            pdb_v.set("source", "alphafold")
            pdb_v.set("entry_id", af_entry_id)
            pdb_v.set("url", real_pdb_url or f"https://alphafold.ebi.ac.uk/files/{af_stem}.pdb")

            pae_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            pae_v.set("kind", "af_pae_png")
            pae_v.set("source", "alphafold")
            pae_v.set("entry_id", af_entry_id)
            pae_v.set(
                "preview_url",
                f"https://alphafold.ebi.ac.uk/files/AF-{acc}-F1-predicted_aligned_error_{af_version}.png",
            )
            if avg_plddt is not None:
                pae_v.set("avg_plddt", f"{avg_plddt:.2f}")

            page_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            page_v.set("kind", "af_entry_page")
            page_v.set("source", "alphafold")
            page_v.set("url", f"https://alphafold.ebi.ac.uk/entry/{acc}")

            emitted_any = True

        # -- PDB experimental structures ------------------------------------
        seen_pdb: set[str] = set()
        for raw_pdb in (pdb_ids or [])[:8]:  # budget: first 8 entries
            if not isinstance(raw_pdb, str):
                continue
            pdb_id = raw_pdb.strip().upper()
            if not re.match(r"^[A-Z0-9]{4}$", pdb_id) or pdb_id in seen_pdb:
                continue
            seen_pdb.add(pdb_id)
            pdb_lower = pdb_id.lower()

            rcsb_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            rcsb_v.set("kind", "pdb_assembly_jpeg")
            rcsb_v.set("source", "rcsb")
            rcsb_v.set("pdb_id", pdb_id)
            rcsb_v.set(
                "preview_url",
                f"https://cdn.rcsb.org/images/structures/{pdb_lower}_assembly-1.jpeg",
            )
            rcsb_v.set(
                "url",
                f"https://files.rcsb.org/download/{pdb_id}.cif",
            )

            pdbe_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            pdbe_v.set("kind", "pdb_front_image")
            pdbe_v.set("source", "pdbe")
            pdbe_v.set("pdb_id", pdb_id)
            pdbe_v.set(
                "preview_url",
                f"https://www.ebi.ac.uk/pdbe/static/entry/{pdb_lower}_deposited_chain_front_image-800x800.png",
            )

            bcif_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            bcif_v.set("kind", "pdb_bcif_proxy")
            bcif_v.set("source", "mica")
            bcif_v.set("pdb_id", pdb_id)
            bcif_v.set("url", f"/api/v1/structure/bcif?pdb_id={pdb_id}")

            emitted_any = True

        # -- InterPro domain graphic (SVG-capable) --------------------------
        if acc_is_uniprot:
            ipr_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
            ipr_v.set("kind", "interpro_domain_graphic")
            ipr_v.set("source", "interpro")
            ipr_v.set(
                "url",
                f"https://www.ebi.ac.uk/interpro/api/entry/InterPro/protein/UniProt/{acc}/?format=json",
            )
            emitted_any = True

        # -- FlatProt (only if binary + structure available locally) --------
        if self._preset_bool("flatprot_enabled", default=False):
            try:
                from bsm.lmp.flatprot_client import FlatProtClient  # local import: graceful

                fp_client = FlatProtClient(cache_dir=self.cache_dir)
                if fp_client.is_available():
                    struct_path = self._resolve_structural_pdb_path(
                        accession=acc,
                        pdb_ids=pdb_ids,
                        prefer_cif=True,
                    )
                    if struct_path:
                        out = fp_client.render_svg(Path(struct_path), output_name=acc or None)
                        if out is not None:
                            fp_v = ET.SubElement(visuals_elem, self._lmp_tag("Visual"))
                            fp_v.set("kind", "flatprot_svg")
                            fp_v.set("source", "flatprot")
                            fp_v.set("local_path", str(out.svg_path))
                            # url stays empty until an uploader fills it;
                            # FE treats empty url as "pending"
                            fp_v.set("url", "")
                            emitted_any = True
            except Exception as _exc:  # noqa: BLE001 — graceful
                self.logger.debug("FlatProt Visual emission skipped: %s", _exc)

        if not emitted_any:
            # Remove the empty <Visuals> to keep XML minimal
            geometry_elem.remove(visuals_elem)

    def _add_trajectory_ifp_v4(self, geometry_elem: ET.Element, *, pdb_id: str, ligand: str, ifp_result, stride: int) -> None:
        """Serialize SMIC IFPTrajectoryResult into v4 <TrajectoryIFP>."""
        traj_elem = ET.SubElement(geometry_elem, self._lmp_tag("TrajectoryIFP"))
        traj_elem.set("pdb_id", pdb_id)
        traj_elem.set("ligand", ligand)
        traj_elem.set("total_frames", str(int(getattr(ifp_result, "n_frames", 0) or 0)))
        traj_elem.set("stride", str(max(1, int(stride))))

        # Controls (preset-configurable; schema stays unchanged)
        try:
            min_occupancy = float(getattr(self.preset, "ifp_min_occupancy", 0.10)) if self.preset else 0.10
        except Exception:
            min_occupancy = 0.10
        try:
            max_key_interactions = int(getattr(self.preset, "ifp_max_key_interactions", 25)) if self.preset else 25
        except Exception:
            max_key_interactions = 25

        # Schema-safe extra context: embed as XML comment (ignored by XSD).
        try:
            ctx = getattr(ifp_result, "mica_ifp_context", None)
            if isinstance(ctx, dict) and ctx:
                payload = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
                payload = payload.replace("--", "- -").strip()
                if payload.endswith("-"):
                    payload += " "
                traj_elem.insert(0, ET.Comment(f"mica:smic_context {payload}"))
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)

        time_ps = getattr(ifp_result, "time_ps", None)
        if time_ps is not None and hasattr(time_ps, "__len__") and len(time_ps) >= 2:
            try:
                dt_ps = float(time_ps[1] - time_ps[0])
                if dt_ps > 0:
                    traj_elem.set("time_step_ps", str(dt_ps))
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

        contact_occupancy = getattr(ifp_result, "contact_occupancy", {}) or {}
        frame_results = getattr(ifp_result, "frame_results", []) or []
        ifp_matrix = getattr(ifp_result, "ifp_matrix", None)
        ifp_col_occupancies = getattr(ifp_result, "occupancies", {}) or {}

        def _parse_col(col_name: str) -> Optional[Tuple[str, str, Optional[int]]]:
            try:
                raw = str(col_name or "")
                if "_" not in raw:
                    return None
                code, residue = raw.split("_", 1)
                code = code.strip().upper()
                residue = residue.strip()
                if not residue:
                    return None
                m = re.match(r"^(.*?)(-?\d+)$", residue)
                resid = int(m.group(2)) if m else None
                return code, residue, resid
            except Exception:
                return None

        fallback_codes = {"HD", "HA", "WB"}
        fallback_by_frame: Dict[int, List[Tuple[str, str, Optional[int], float]]] = {}
        fallback_occ_by_residue_type: Dict[Tuple[str, str], float] = {}

        try:
            if ifp_matrix is not None and hasattr(ifp_matrix, "columns") and hasattr(ifp_matrix, "iterrows"):
                candidate_cols: List[str] = []
                for c in list(ifp_matrix.columns):
                    if c in ("frame", "time"):
                        continue
                    parsed = _parse_col(str(c))
                    if parsed and parsed[0] in fallback_codes:
                        candidate_cols.append(str(c))

                if candidate_cols:
                    for _, row in ifp_matrix.iterrows():
                        try:
                            frame_no = int(row["frame"]) if "frame" in row else int(_)
                        except Exception:
                            continue

                        for col in candidate_cols:
                            try:
                                v = float(row[col])
                            except Exception:
                                continue
                            if v <= 0:
                                continue

                            parsed = _parse_col(col)
                            if not parsed:
                                continue
                            code, residue_label, resid = parsed

                            try:
                                occ = float(ifp_col_occupancies.get(col, 0.0))
                            except Exception:
                                occ = 0.0

                            bucket = fallback_by_frame.setdefault(frame_no, [])
                            bucket.append((code, residue_label, resid, occ))

                            schema_t = self._map_smic_ifp_type_to_schema(code)
                            k = (residue_label, schema_t)
                            prev = fallback_occ_by_residue_type.get(k, 0.0)
                            if occ > prev:
                                fallback_occ_by_residue_type[k] = occ
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)

        # Enriched summary stats (match SMIC bridge behavior): collect per-key distances/angles and residue names.
        distances_by_key: Dict[tuple, List[float]] = {}
        angles_by_key: Dict[tuple, List[float]] = {}
        resname_by_resid: Dict[int, str] = {}

        total_interactions = 0
        for fr in frame_results:
            frame_elem = ET.SubElement(traj_elem, self._lmp_tag("Frame"))
            frame_elem.set("number", str(int(getattr(fr, "frame", 0) or 0)))
            try:
                t_ns = float(getattr(fr, "time_ps", 0.0) or 0.0) / 1000.0
                frame_elem.set("time_ns", f"{t_ns:.3f}")
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

            mapped_types: Set[str] = set()
            contacts = getattr(fr, "contacts", []) or []
            seen_residue_type: Set[Tuple[str, str]] = set()

            for c in contacts:
                ifp_type = str(getattr(c, "ifp_type", "") or "")
                schema_type = self._map_smic_ifp_type_to_schema(ifp_type)
                resname = (getattr(c, "receptor_resname", "") or "").strip()
                resid = getattr(c, "receptor_resid", None)
                lig_resid = getattr(c, "ligand_resid", None)
                if isinstance(resid, int) and resname:
                    resname_by_resid.setdefault(resid, resname)
                residue_label = f"{resname}{resid}" if (resname and resid is not None) else str(resid or "UNK")

                dedupe_key = (residue_label, schema_type)
                if dedupe_key in seen_residue_type:
                    continue
                seen_residue_type.add(dedupe_key)

                mapped_types.add(schema_type)

                interaction = ET.SubElement(frame_elem, self._lmp_tag("Interaction"))
                interaction.set("type", schema_type)
                interaction.set("residue", residue_label)
                total_interactions += 1

                dist_val: Optional[float] = None
                try:
                    dist_val = float(getattr(c, "distance", 0.0) or 0.0)
                    interaction.set("distance", f"{dist_val:.3f}")
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)

                try:
                    key = None
                    if resid is not None and lig_resid is not None:
                        key = (int(resid), int(lig_resid), ifp_type)
                        occ = contact_occupancy.get(key)
                        if occ is not None:
                            interaction.set("occupancy", f"{float(occ):.6f}")

                    if key is not None:
                        if dist_val is not None:
                            distances_by_key.setdefault(key, []).append(float(dist_val))

                        # Optional angles from engine metadata (HBond DHA angle, pi stacking angle, etc.)
                        md = getattr(c, "metadata", None)
                        if isinstance(md, dict):
                            ang = md.get("angle") or md.get("dha_angle") or md.get("pi_angle")
                            if ang is not None:
                                angf = float(ang)
                                angles_by_key.setdefault(key, []).append(float(angf))
                                interaction.set("angle", f"{angf:.2f}")
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)

            frame_no = int(getattr(fr, "frame", 0) or 0)
            for code, residue_label, resid, occ in fallback_by_frame.get(frame_no, []):
                schema_type = self._map_smic_ifp_type_to_schema(code)
                k = (residue_label, schema_type)
                if k in seen_residue_type:
                    continue
                seen_residue_type.add(k)

                interaction = ET.SubElement(frame_elem, self._lmp_tag("Interaction"))
                interaction.set("type", schema_type)
                interaction.set("residue", residue_label)

                if resid is not None and re.match(r"^[A-Za-z]+", residue_label):
                    resname_match = re.match(r"^([A-Za-z]+)", residue_label)
                    if resname_match:
                        resname_by_resid.setdefault(int(resid), str(resname_match.group(1)))

                if occ > 0.0:
                    interaction.set("occupancy", f"{float(occ):.6f}")

                mapped_types.add(schema_type)
                total_interactions += 1

            fp_elem = ET.SubElement(frame_elem, self._lmp_tag("Fingerprint"))
            fp_elem.text = self._ifp_fingerprint_bits(mapped_types)

        # Summary
        summary = ET.SubElement(traj_elem, self._lmp_tag("Summary"))
        summary.set("total_interactions", str(int(total_interactions)))
        if getattr(ifp_result, "n_frames", 0):
            summary.set(
                "average_interactions_per_frame",
                f"{(float(total_interactions) / float(ifp_result.n_frames)):.3f}",
            )

        # Key interactions (top occupancy)
        try:
            ranked = sorted(contact_occupancy.items(), key=lambda kv: kv[1], reverse=True)
            emitted = 0
            emitted_keys: Set[Tuple[str, str]] = set()
            for (receptor_resid, lig_resid, ifp_type), occ in ranked:
                if float(occ) < float(min_occupancy):
                    break

                key_elem = ET.SubElement(summary, self._lmp_tag("KeyInteraction"))
                resname = resname_by_resid.get(int(receptor_resid), "")
                residue_label = f"{resname}{int(receptor_resid)}" if resname else str(int(receptor_resid))
                schema_type = self._map_smic_ifp_type_to_schema(str(ifp_type))
                key_elem.set("residue", residue_label)
                key_elem.set("type", schema_type)
                key_elem.set("occupancy", f"{float(occ):.6f}")
                emitted_keys.add((residue_label, schema_type))

                key = (int(receptor_resid), int(lig_resid), str(ifp_type))
                dvals = distances_by_key.get(key) or []
                if dvals:
                    key_elem.set("avg_distance", f"{(float(np.mean(dvals))):.3f}")
                avals = angles_by_key.get(key) or []
                if avals:
                    key_elem.set("avg_angle", f"{(float(np.mean(avals))):.2f}")

                emitted += 1
                if emitted >= max_key_interactions:
                    break

            if emitted < max_key_interactions and fallback_occ_by_residue_type:
                extra_ranked = sorted(fallback_occ_by_residue_type.items(), key=lambda kv: kv[1], reverse=True)
                for (residue_label, schema_type), occ in extra_ranked:
                    if emitted >= max_key_interactions:
                        break
                    if float(occ) < float(min_occupancy):
                        continue
                    if (residue_label, schema_type) in emitted_keys:
                        continue

                    key_elem = ET.SubElement(summary, self._lmp_tag("KeyInteraction"))
                    key_elem.set("residue", residue_label)
                    key_elem.set("type", schema_type)
                    key_elem.set("occupancy", f"{float(occ):.6f}")
                    emitted_keys.add((residue_label, schema_type))
                    emitted += 1
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)

    def _safe_xs_id(self, value: str) -> str:
        """Make a best-effort xs:ID-safe token (letters/digits/_), starting with a letter."""
        raw = (value or "").strip()
        raw = re.sub(r"[^A-Za-z0-9_]+", "_", raw)
        raw = re.sub(r"_+", "_", raw).strip("_")
        if not raw:
            raw = "id"
        if not re.match(r"^[A-Za-z_]", raw):
            raw = f"id_{raw}"
        return raw

    def _infer_ligand_type_effect(self, *, name: str, site_type: str) -> tuple[str, str]:
        """Infer required schema enums for Ligand@type and Ligand@effect."""
        hay = f"{name} {site_type}".lower()
        if any(k in hay for k in ["inhib", "antagon", "block"]):
            return ("inhibitor", "inhibition")
        if "agon" in hay:
            return ("agonist", "activation")
        if any(k in hay for k in ["alloster", "modulator"]):
            return ("allosteric_modulator", "allosteric_modulation")
        if "cofactor" in hay:
            return ("cofactor", "stabilization")
        return ("substrate", "catalysis")

    def _add_ligand_v4(
        self,
        parent_binding_site: ET.Element,
        *,
        ligand_name: str,
        ligand_obj: Optional[Dict[str, Any]] = None,
        site_type: str,
        stable_id_hint: Optional[str] = None,
    ) -> None:
        lig_type, lig_effect = self._infer_ligand_type_effect(name=ligand_name, site_type=site_type)
        lig_elem = ET.SubElement(parent_binding_site, self._lmp_tag("Ligand"))
        lig_elem.set("name", str(ligand_name))
        lig_elem.set("type", lig_type)
        lig_elem.set("effect", lig_effect)

        if stable_id_hint:
            lig_elem.set("id", self._safe_xs_id(stable_id_hint))

        if isinstance(ligand_obj, dict):
            if ligand_obj.get("pubchem_cid"):
                lig_elem.set("pubchem_cid", str(ligand_obj.get("pubchem_cid")))
            if ligand_obj.get("smiles"):
                lig_elem.set("smiles", str(ligand_obj.get("smiles")))
            if ligand_obj.get("inchi"):
                lig_elem.set("inchi", str(ligand_obj.get("inchi")))

            pubchem = ligand_obj.get("pubchem")
            if isinstance(pubchem, dict) and pubchem.get("cid") is not None:
                pc = ET.SubElement(lig_elem, self._lmp_tag("PubChemData"))
                pc.set("cid", str(pubchem.get("cid")))
                props = pubchem.get("properties")
                if isinstance(props, dict):
                    for k, v in list(props.items())[:50]:
                        p = ET.SubElement(pc, self._lmp_tag("Property"))
                        p.set("name", str(k))
                        p.text = "" if v is None else str(v)
                syns = pubchem.get("synonyms")
                if isinstance(syns, list) and syns:
                    p = ET.SubElement(pc, self._lmp_tag("Property"))
                    p.set("name", "synonyms")
                    p.text = "; ".join(str(s) for s in syns[:20] if s)

    def _collect_pdb_binding_sites(self, pdb_ids: List[str]) -> List[Dict[str, Any]]:
        """Best-effort: PDBe binding sites + RCSB ligands for v4 BindingSite/Ligand embedding."""
        out: List[Dict[str, Any]] = []
        for pid in pdb_ids:
            try:
                pid_u = str(pid).upper()
                bs_payload = self._fetch_binding_sites(pid_u)
                ligands = self._fetch_pdb_ligands(pid_u)

                ligand_sites = bs_payload.get("ligand_sites") if isinstance(bs_payload, dict) else None
                if not isinstance(ligand_sites, list):
                    ligand_sites = []

                any_site = False

                # For each site, attach matched ligands by simple substring heuristic.
                for site in ligand_sites[:25]:
                    if not isinstance(site, dict):
                        continue
                    residues: List[int] = []
                    # PDBe binding_sites returns per-chain residue lists.
                    residues_by_chain = site.get("residues_by_chain")
                    if isinstance(residues_by_chain, dict) and len(residues_by_chain) == 1:
                        only_chain = next(iter(residues_by_chain.keys()))
                        raw_res = residues_by_chain.get(only_chain)
                        if isinstance(raw_res, list):
                            for r in raw_res:
                                try:
                                    ri = int(r)
                                    if ri > 0:
                                        residues.append(ri)
                                except Exception:
                                    continue
                    else:
                        raw_res = site.get("residues")
                        if isinstance(raw_res, list):
                            for r in raw_res:
                                try:
                                    ri = int(r)
                                    if ri > 0:
                                        residues.append(ri)
                                except Exception:
                                    continue

                    residues = sorted(set(residues))
                    if not residues:
                        continue

                    details = str(site.get("details") or site.get("site_id") or "binding_site")
                    details_upper = details.upper()
                    matched: List[Dict[str, Any]] = []
                    if isinstance(ligands, list):
                        for lig in ligands:
                            if not isinstance(lig, dict):
                                continue
                            lig_id = str(lig.get("ligand_id") or "").upper()
                            lig_name = str(lig.get("name") or "").upper()
                            if lig_id and (lig_id in details_upper or lig_name in details_upper):
                                matched.append(lig)

                    if not matched and isinstance(ligands, list) and ligands:
                        # Attach first ligand as best-effort if details do not match.
                        first = ligands[0]
                        if isinstance(first, dict):
                            matched = [first]

                    out.append(
                        {
                            "type": details,
                            "residues": residues,
                            "pdb_id": pid_u,
                            "ligands": matched,
                        }
                    )
                    any_site = True

                # Fallback: if PDBe binding sites unavailable (404) but we do have ligands,
                # attach them to a synthetic site with a valid residue list.
                if not any_site and isinstance(ligands, list) and ligands:
                    out.append(
                        {
                            "type": f"PDB:{pid_u}:ligands",
                            "residues": [1],
                            "pdb_id": pid_u,
                            "ligands": [lig for lig in ligands if isinstance(lig, dict)][:10],
                        }
                    )
            except Exception as e:
                self.logger.warning("Failed to collect PDBe binding sites for %s: %s", pid, e)
        return out

    def _add_knowledge_graph_v4(
        self,
        root: ET.Element,
        *,
        uniprot_data: Dict[str, Any],
        source_id: Optional[str] = None,
        pdb_sifts_domains: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        pdb_entity_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Schema-compliant <KnowledgeGraph> block (CrossReference/Reference/Edge).

        Note: `source_id` allows upstream callers to use a canonical entity ID
        (e.g., a stable BUDO/CEA-like identifier) rather than overloading the
        source with transient state labels.
        """
        kg = ET.SubElement(root, self._lmp_tag("KnowledgeGraph"))

        accession = uniprot_data.get("primaryAccession") or uniprot_data.get("primary_accession")
        effective_source = source_id or accession

        def _xref_category(db_name: str) -> str:
            d = (db_name or "").strip().upper()
            if d in {"PDB", "EMDB", "ALPHAFOLDDB"}:
                return "structure"
            if d in {"GO"}:
                return "function"
            if d in {"REACTOME", "KEGG", "WIKIPATHWAYS"}:
                return "pathway"
            if d in {"CHEMBL", "DRUGBANK", "PUBCHEM", "CHEBI"}:
                return "chemistry"
            if d in {"OMIM", "ORPHANET", "DISGENET"}:
                return "disease"
            if d in {"PUBMED", "DOI"}:
                return "literature"
            return "other"

        def _xref_relation(db_name: str) -> Optional[str]:
            d = (db_name or "").strip().upper()
            if d == "PDB":
                return "HAS_STRUCTURE"
            if d == "GO":
                return "HAS_FUNCTION"
            if d in {"DRUGBANK", "CHEMBL", "KEGG", "REACTOME"}:
                return "HAS_KNOWLEDGE"
            return None

        # Cross references
        xrefs = uniprot_data.get("dbReferences")
        if isinstance(xrefs, list):
            for xref in xrefs[:500]:
                if not isinstance(xref, dict):
                    continue
                db = xref.get("database") or xref.get("type")
                xid = xref.get("id")
                if not db or not xid:
                    continue
                cr = ET.SubElement(kg, self._lmp_tag("CrossReference"))
                cr.set("db", str(db))
                cr.set("id", str(xid))
                if xref.get("isoformId"):
                    cr.set("isoformId", str(xref.get("isoformId")))

                # Derived semantics (schema-safe: stored as <Property>)
                cat = _xref_category(str(db))
                rel = _xref_relation(str(db))
                p_cat = ET.SubElement(cr, self._lmp_tag("Property"))
                p_cat.set("name", "category")
                p_cat.text = cat
                if rel:
                    p_rel = ET.SubElement(cr, self._lmp_tag("Property"))
                    p_rel.set("name", "relation")
                    p_rel.text = rel
                p_src = ET.SubElement(cr, self._lmp_tag("Property"))
                p_src.set("name", "source")
                p_src.text = "uniprot"

                props = xref.get("properties")
                if isinstance(props, list):
                    for prop in props[:100]:
                        if not isinstance(prop, dict):
                            continue
                        key = prop.get("key")
                        val = prop.get("value")
                        if not key:
                            continue
                        p = ET.SubElement(cr, self._lmp_tag("Property"))
                        p.set("name", str(key))
                        if val is not None:
                            p.text = str(val)

        # References (citations)
        refs = uniprot_data.get("references")
        if isinstance(refs, list):
            for i, ref in enumerate(refs[:200], 1):
                if not isinstance(ref, dict):
                    continue
                r = ET.SubElement(kg, self._lmp_tag("Reference"))
                r.set("number", str(i))
                citation = ref.get("citation")
                if isinstance(citation, dict):
                    title = citation.get("title")
                    ctype = citation.get("type")
                    if title:
                        c = ET.SubElement(r, self._lmp_tag("Citation"))
                        if ctype:
                            c.set("type", str(ctype))
                        c.text = str(title)
                    cxrefs = citation.get("citationCrossReferences")
                    if isinstance(cxrefs, list):
                        for cxref in cxrefs[:50]:
                            if not isinstance(cxref, dict):
                                continue
                            db = cxref.get("database")
                            cid = cxref.get("id")
                            if not db or not cid:
                                continue
                            cx = ET.SubElement(r, self._lmp_tag("CitationXref"))
                            cx.set("name", str(db))
                            cx.text = str(cid)

        # Edges (lightweight relationships derived from xrefs)
        if isinstance(xrefs, list):
            for xref in xrefs:
                if not isinstance(xref, dict):
                    continue
                db = str(xref.get("database") or "")
                xid = xref.get("id")
                if not db or not xid:
                    continue
                edge_type = _xref_relation(db)
                if edge_type:
                    e = ET.SubElement(kg, self._lmp_tag("Edge"))
                    e.set("type", edge_type)
                    e.set("db", db)
                    e.set("id", str(xid))
                    if effective_source:
                        e.set("source", str(effective_source))
                    e.set("target", f"{db}:{xid}")

        # ---- PDBe SIFTS domain cross-references (InterPro/Pfam/CATH) ----
        if isinstance(pdb_sifts_domains, dict):
            for chain_id, dom_list in pdb_sifts_domains.items():
                for sdom in (dom_list or []):
                    if not isinstance(sdom, dict):
                        continue
                    src = str(sdom.get("source", "")).strip()
                    sid = str(sdom.get("id", "")).strip()
                    sname = str(sdom.get("name", "")).strip()
                    if not sid:
                        continue
                    cr = ET.SubElement(kg, self._lmp_tag("CrossReference"))
                    cr.set("db", src)
                    cr.set("id", sid)
                    p = ET.SubElement(cr, self._lmp_tag("Property"))
                    p.set("name", "category")
                    p.text = "domain"
                    p2 = ET.SubElement(cr, self._lmp_tag("Property"))
                    p2.set("name", "chain")
                    p2.text = str(chain_id)
                    if sname:
                        p3 = ET.SubElement(cr, self._lmp_tag("Property"))
                        p3.set("name", "name")
                        p3.text = sname
                    p4 = ET.SubElement(cr, self._lmp_tag("Property"))
                    p4.set("name", "source")
                    p4.text = "pdbe_sifts"
                    # Edge
                    e = ET.SubElement(kg, self._lmp_tag("Edge"))
                    e.set("type", "HAS_DOMAIN")
                    e.set("db", src)
                    e.set("id", sid)
                    if effective_source:
                        e.set("source", str(effective_source))
                    e.set("target", f"{src}:{sid}")

        # ---- Entity-level GO terms / EC numbers from PDB enrichment ----
        if isinstance(pdb_entity_metadata, dict):
            for eid, emeta in pdb_entity_metadata.items():
                if not isinstance(emeta, dict):
                    continue
                for go in (emeta.get("go_terms") or []):
                    if isinstance(go, dict) and go.get("id"):
                        cr = ET.SubElement(kg, self._lmp_tag("CrossReference"))
                        cr.set("db", "GO")
                        cr.set("id", str(go["id"]))
                        p = ET.SubElement(cr, self._lmp_tag("Property"))
                        p.set("name", "category")
                        p.text = "function"
                        if go.get("name"):
                            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
                            p2.set("name", "name")
                            p2.text = str(go["name"])
                        p3 = ET.SubElement(cr, self._lmp_tag("Property"))
                        p3.set("name", "source")
                        p3.text = "rcsb_entity"
                        e = ET.SubElement(kg, self._lmp_tag("Edge"))
                        e.set("type", "HAS_FUNCTION")
                        e.set("db", "GO")
                        e.set("id", str(go["id"]))
                        if effective_source:
                            e.set("source", str(effective_source))
                        e.set("target", f"GO:{go['id']}")

        # ---- Multi-API Enrichment (v4.2) ----
        gene_name = (
            (uniprot_data.get("gene_name") or "").strip()
            or (uniprot_data.get("genes", [{}])[0].get("geneName", {}).get("value", "")).strip()
            if isinstance(uniprot_data.get("genes"), list) and uniprot_data.get("genes")
            else (uniprot_data.get("gene_name") or "").strip()
        )
        try:
            if gene_name:
                if self._preset_bool("include_string_interactions", default=False):
                    self._add_string_interactions_v4(kg, gene_name=gene_name, species=self.string_species)
                if self._preset_bool("include_opentargets", default=False):
                    self._add_opentargets_v4(kg, gene_name=gene_name)
                if accession and self._preset_bool("include_chembl_bioactivity", default=False):
                    self._add_chembl_bioactivity_v4(kg, uniprot_id=accession, gene_name=gene_name)
                if self._preset_bool("include_kegg_pathways", default=False):
                    self._add_kegg_pathways_v4(kg, gene_name=gene_name)
                if accession and self._preset_bool("include_reactome_pathways", default=False):
                    self._add_reactome_pathways_v4(kg, uniprot_id=accession, gene_name=gene_name)
                if self._preset_bool("include_protein_atlas", default=False):
                    self._add_protein_atlas_v4(kg, gene_name=gene_name)
                if accession and self._preset_bool("include_go_enrichment", default=False):
                    self._add_go_enrichment_v4(kg, uniprot_id=accession, gene_name=gene_name)
                if self._preset_bool("include_ensembl", default=False):
                    self._add_ensembl_v4(kg, gene_name=gene_name)
                if self._preset_bool("include_hpo_phenotypes", default=False):
                    self._add_hpo_phenotypes_v4(kg, gene_name=gene_name)
                if self._preset_bool("include_gtex_expression", default=False):
                    self._add_gtex_expression_v4(kg, gene_name=gene_name)
        except Exception as _exc:
            self.logger.debug("Multi-API enrichment error: %s", _exc)

        # ---- XSD order enforcement: CrossReference* → Reference* → Edge* ----
        _ORDER = {"CrossReference": 0, "Reference": 1, "Edge": 2}
        children = list(kg)
        children.sort(key=lambda c: _ORDER.get(c.tag.split("}")[-1] if "}" in c.tag else c.tag, 9))
        for c in children:
            kg.remove(c)
        for c in children:
            kg.append(c)

    def _add_provenance_v4(
        self,
        root: ET.Element,
        *,
        uniprot_data: Dict[str, Any],
        ground_truth_meta: Optional[Dict[str, Any]] = None,
        structure_generation_receipt: Optional[Mapping[str, Any]] = None,
    ) -> None:
        prov = ET.SubElement(root, self._lmp_tag("Provenance"))

        audit = uniprot_data.get("entryAudit")
        if isinstance(audit, dict) and audit:
            ea = ET.SubElement(prov, self._lmp_tag("EntryAudit"))
            if audit.get("sequenceVersion") is not None:
                ET.SubElement(ea, self._lmp_tag("SequenceVersion")).text = str(audit.get("sequenceVersion"))
            if audit.get("entryVersion") is not None:
                ET.SubElement(ea, self._lmp_tag("EntryVersion")).text = str(audit.get("entryVersion"))
            if audit.get("firstPublicDate"):
                ET.SubElement(ea, self._lmp_tag("FirstPublicDate")).text = str(audit.get("firstPublicDate"))
            if audit.get("lastAnnotationUpdateDate"):
                ET.SubElement(ea, self._lmp_tag("LastAnnotationUpdateDate")).text = str(audit.get("lastAnnotationUpdateDate"))
            if audit.get("lastSequenceUpdateDate"):
                ET.SubElement(ea, self._lmp_tag("LastSequenceUpdateDate")).text = str(audit.get("lastSequenceUpdateDate"))

        if self._preset_bool("embed_ground_truth", default=False):
            try:
                gt = ET.SubElement(prov, self._lmp_tag("GroundTruthEntry"))
                gt.set("contentType", "application/json")
                gt.set("encoding", "base64")
                gt.text = base64.b64encode(json.dumps(uniprot_data, ensure_ascii=False).encode("utf-8")).decode("ascii")
                if ground_truth_meta:
                    gtm = ET.SubElement(prov, self._lmp_tag("GroundTruthMeta"))
                    gtm.set("contentType", "application/json")
                    gtm.set("encoding", "base64")
                    gtm.text = base64.b64encode(json.dumps(ground_truth_meta, ensure_ascii=False).encode("utf-8")).decode("ascii")
            except Exception as e:
                self.logger.warning("Failed to embed ground truth: %s", e)

        # Generation info must appear before any InternalLine entries to satisfy
        # the ProvenanceType sequence in lmp_v4_schema.xsd.
        gen = ET.SubElement(prov, self._lmp_tag("GenerationInfo"))
        ET.SubElement(gen, self._lmp_tag("Generator")).text = "src.bsm.lmp.generator_v4:LMPGenerator"
        ET.SubElement(gen, self._lmp_tag("GeneratorVersion")).text = "2026-01-22"
        if self.preset is not None:
            ET.SubElement(gen, self._lmp_tag("Preset")).text = getattr(self.preset, "name", "")
        ET.SubElement(gen, self._lmp_tag("Timestamp")).text = datetime.now(timezone.utc).isoformat()
        # UniProt data provenance: release number (from HTTP header, live fetches
        # only) and per-entry version (always available, increments on any change).
        _ur = uniprot_data.get("_uniprot_release", "")
        if _ur:
            ET.SubElement(gen, self._lmp_tag("UniProtRelease")).text = _ur
        _ea = uniprot_data.get("entryAudit") or {}
        if _ea.get("entryVersion") is not None:
            ET.SubElement(gen, self._lmp_tag("UniProtEntryVersion")).text = str(_ea["entryVersion"])
        if _ea.get("lastAnnotationUpdateDate"):
            ET.SubElement(gen, self._lmp_tag("UniProtLastAnnotated")).text = str(_ea["lastAnnotationUpdateDate"])

        # Compact CrossReference snapshot for downstream semantic enrichment.
        # Keeps schema stability by using Provenance/InternalLine.
        try:
            xrefs = uniprot_data.get("dbReferences")
            if isinstance(xrefs, list) and xrefs:
                counts: Dict[str, int] = {}
                examples: Dict[str, List[str]] = {}
                for x in xrefs:
                    if not isinstance(x, dict):
                        continue
                    db = str(x.get("database") or x.get("type") or "").strip()
                    xid = x.get("id")
                    if not db or not xid:
                        continue
                    counts[db] = counts.get(db, 0) + 1
                    if db not in examples:
                        examples[db] = []
                    if len(examples[db]) < 8:
                        examples[db].append(str(xid))

                snapshot = {
                    "primaryAccession": uniprot_data.get("primaryAccession"),
                    "xref_total": sum(counts.values()),
                    "xref_db_counts": dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:25]),
                    "xref_examples": examples,
                    "generated_at": datetime.utcnow().isoformat(),
                }
                line = ET.SubElement(prov, self._lmp_tag("InternalLine"))
                line.set("type", "xref_snapshot")
                line.text = json.dumps(snapshot, ensure_ascii=False)
        except Exception as e:
            self.logger.debug("xref snapshot generation failed: %s", e)

        try:
            receipt = self._coerce_structure_generation_receipt(structure_generation_receipt)
            model_context = receipt.get("model_asset_context") or receipt.get("structure_asset_context")
            if isinstance(model_context, Mapping) and model_context:
                line = ET.SubElement(prov, self._lmp_tag("InternalLine"))
                line.set("type", "structure_asset_context")
                line.text = json.dumps(model_context, ensure_ascii=False)
        except Exception as e:
            self.logger.debug("structure asset context provenance failed: %s", e)

    # ====================================================================
    # Multi-API Enrichment Methods (v4.2)
    # ====================================================================

    def _is_circuit_open(self, api_name: str) -> bool:
        """Check if the circuit breaker is tripped for an API."""
        if self._api_fail_count.get(api_name, 0) >= self._circuit_breaker_threshold:
            if time.time() < self._api_circuit_open_until.get(api_name, 0.0):
                return True
            # Cooldown elapsed — half-open: reset counter, allow one attempt
            self._api_fail_count[api_name] = 0
        return False

    def _record_api_success(self, api_name: str):
        """Reset failure counter on success."""
        self._api_fail_count[api_name] = 0

    def _record_api_failure(self, api_name: str):
        """Record a failure; trip circuit breaker if threshold reached."""
        self._api_fail_count[api_name] = self._api_fail_count.get(api_name, 0) + 1
        if self._api_fail_count[api_name] >= self._circuit_breaker_threshold:
            self._api_circuit_open_until[api_name] = time.time() + self._circuit_breaker_cooldown
            self.logger.warning(
                "Circuit breaker OPEN for %s (fails=%d). Cooling down %.0fs.",
                api_name, self._api_fail_count[api_name], self._circuit_breaker_cooldown,
            )

    def _safe_api_get(
        self,
        url: str,
        *,
        timeout: float = 15,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        api_name: str = "generic",
        max_retries: int = 3,
    ) -> Optional[Dict]:
        """Best-effort API GET with per-API rate limit, HTTP 429 retry, and circuit breaker.

        Returns parsed JSON on success, None on failure.
        """
        if self.offline_mode or not self._can_fetch_network():
            return None
        if self._is_circuit_open(api_name):
            self.logger.debug("Circuit open for %s — skipping %s", api_name, url[:80])
            return None

        for attempt in range(max_retries):
            try:
                self._rate_limit_wait(api_name)
                resp = requests.get(url, timeout=timeout, headers=headers or {}, params=params or {})

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                    self.logger.warning("429 from %s — backing off %.1fs (attempt %d/%d)", api_name, retry_after, attempt + 1, max_retries)
                    time.sleep(min(retry_after, 30))
                    continue

                # 4xx client errors (except 429) mean "no data" — don't retry or count as failure
                if 400 <= resp.status_code < 500:
                    self.logger.debug("HTTP %d from %s %s — no data (not a failure)", resp.status_code, api_name, url[:80])
                    self._record_api_success(api_name)  # API is alive, just no data
                    return None

                resp.raise_for_status()
                self._record_api_success(api_name)
                return resp.json()

            except requests.exceptions.HTTPError as e:
                # 5xx server errors — retry with backoff
                self.logger.debug("HTTP error %s %s (attempt %d): %s", api_name, url[:80], attempt + 1, e)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            except Exception as e:
                self.logger.debug("API call failed %s %s (attempt %d): %s", api_name, url[:80], attempt + 1, e)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

        self._record_api_failure(api_name)
        return None

    def _safe_api_post(
        self,
        url: str,
        *,
        timeout: float = 15,
        json_data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        api_name: str = "generic",
        max_retries: int = 3,
    ) -> Optional[Dict]:
        """Best-effort API POST with per-API rate limit, HTTP 429 retry, and circuit breaker."""
        if self.offline_mode or not self._can_fetch_network():
            return None
        if self._is_circuit_open(api_name):
            self.logger.debug("Circuit open for %s — skipping POST %s", api_name, url[:80])
            return None

        for attempt in range(max_retries):
            try:
                self._rate_limit_wait(api_name)
                resp = requests.post(
                    url,
                    timeout=timeout,
                    json=json_data,
                    headers=headers or {"Content-Type": "application/json", "Accept": "application/json"},
                )

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                    self.logger.warning("429 from %s POST — backing off %.1fs", api_name, retry_after)
                    time.sleep(min(retry_after, 30))
                    continue

                # 4xx client errors (except 429) — no data, not a failure
                if 400 <= resp.status_code < 500:
                    self.logger.debug("HTTP %d from %s POST — no data", resp.status_code, api_name)
                    self._record_api_success(api_name)
                    return None

                resp.raise_for_status()
                self._record_api_success(api_name)
                return resp.json()

            except Exception as e:
                self.logger.debug("API POST failed %s %s (attempt %d): %s", api_name, url[:80], attempt + 1, e)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

        self._record_api_failure(api_name)
        return None

    def _safe_api_get_text(
        self,
        url: str,
        *,
        timeout: float = 15,
        api_name: str = "generic",
        max_retries: int = 3,
    ) -> Optional[str]:
        """Like _safe_api_get but returns raw text (for KEGG text/plain API)."""
        if self.offline_mode or not self._can_fetch_network():
            return None
        if self._is_circuit_open(api_name):
            return None

        for attempt in range(max_retries):
            try:
                self._rate_limit_wait(api_name)
                resp = requests.get(url, timeout=timeout)

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                    self.logger.warning("429 from %s text — backing off %.1fs", api_name, retry_after)
                    time.sleep(min(retry_after, 30))
                    continue

                # 4xx — no data, not a failure
                if 400 <= resp.status_code < 500:
                    self.logger.debug("HTTP %d from %s text — no data", resp.status_code, api_name)
                    self._record_api_success(api_name)
                    return None

                resp.raise_for_status()
                self._record_api_success(api_name)
                return resp.text

            except Exception as e:
                self.logger.debug("API text call failed %s %s (attempt %d): %s", api_name, url[:80], attempt + 1, e)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

        self._record_api_failure(api_name)
        return None

    # ── STRING-DB ──

    def _fetch_string_interactions(self, gene_name: str, species: int = 9606) -> Optional[List[Dict]]:
        """Fetch protein interaction partners from STRING-DB."""
        if not self.string_enabled:
            return None
        url = f"{self.string_api_base}/json/interaction_partners"
        params = {
            "identifiers": gene_name,
            "species": species,
            "limit": self.string_max_partners,
            "required_score": self.string_min_score,
            "caller_identity": "mica_lmp_generator",
        }
        data = self._safe_api_get(url, timeout=self.string_timeout, params=params, api_name="string")
        if isinstance(data, list) and data:
            return data
        return None

    def _fetch_string_enrichment(self, gene_name: str, species: int = 9606) -> Optional[List[Dict]]:
        """Fetch functional enrichment from STRING-DB."""
        if not self.string_enabled:
            return None
        url = f"{self.string_api_base}/json/enrichment"
        params = {
            "identifiers": gene_name,
            "species": species,
            "caller_identity": "mica_lmp_generator",
        }
        return self._safe_api_get(url, timeout=self.string_timeout, params=params, api_name="string")

    def _add_string_interactions_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
        species: int = 9606,
    ) -> int:
        """Add STRING-DB interaction partners as CrossReference + Edge elements.
        Returns number of interactions added."""
        interactions = self._fetch_string_interactions(gene_name, species)
        if not interactions:
            return 0

        count = 0
        for ix in interactions:
            partner = ix.get("preferredName_B") or ix.get("stringId_B", "")
            score = ix.get("score", 0)
            if not partner:
                continue

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "STRING")
            cr.set("id", str(ix.get("stringId_B", partner)))
            cr.set("category", "interaction")

            for prop_name, prop_key in [
                ("partner_name", "preferredName_B"),
                ("combined_score", "score"),
                ("experimental_score", "escore"),
                ("database_score", "dscore"),
                ("textmining_score", "tscore"),
                ("coexpression_score", "ascore"),
            ]:
                val = ix.get(prop_key)
                if val is not None:
                    p = ET.SubElement(cr, self._lmp_tag("Property"))
                    p.set("name", prop_name)
                    p.text = str(val)

            p_src = ET.SubElement(cr, self._lmp_tag("Property"))
            p_src.set("name", "source")
            p_src.text = "string_db_v12"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "INTERACTS_WITH")
            e.set("db", "STRING")
            e.set("id", str(ix.get("stringId_B", partner)))
            e.set("source", gene_name)
            e.set("target", partner)
            e.set("score", str(score))

            count += 1

        self.logger.info("STRING-DB: added %d interaction partners for %s", count, gene_name)
        return count

    # ── OpenTargets ──

    def _fetch_opentargets_associations(self, gene_name: str) -> Optional[Dict]:
        """Fetch disease associations from OpenTargets GraphQL API."""
        if not self.opentargets_enabled:
            return None
        query = """
        query targetAssociations($ensemblId: String!) {
            target(ensemblId: $ensemblId) {
                id
                approvedSymbol
                associatedDiseases(page: {index: 0, size: %d}) {
                    rows {
                        disease { id name }
                        score
                        datatypeScores { id score }
                    }
                }
            }
        }
        """ % self.opentargets_max_diseases
        # First resolve gene name to Ensembl ID via search
        search_url = f"{self.OPENTARGETS_API_BASE}/graphql"
        search_query = """
        query searchTarget($q: String!) {
            search(queryString: $q, entityNames: ["target"], page: {index: 0, size: 1}) {
                hits { id }
            }
        }
        """
        result = self._safe_api_post(search_url, json_data={"query": search_query, "variables": {"q": gene_name}}, timeout=self.opentargets_timeout, api_name="opentargets")
        if not result:
            return None
        hits = (result.get("data") or {}).get("search", {}).get("hits", [])
        if not hits:
            return None
        ensembl_id = hits[0].get("id", "")
        if not ensembl_id:
            return None

        assoc_result = self._safe_api_post(search_url, json_data={"query": query, "variables": {"ensemblId": ensembl_id}}, timeout=self.opentargets_timeout, api_name="opentargets")
        if not assoc_result:
            return None
        target_data = (assoc_result.get("data") or {}).get("target")
        if target_data:
            target_data["_ensemblId"] = ensembl_id
        return target_data

    def _add_opentargets_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
    ) -> int:
        """Add OpenTargets disease associations as CrossReference + Edge elements."""
        target_data = self._fetch_opentargets_associations(gene_name)
        if not target_data:
            return 0

        count = 0
        rows = (target_data.get("associatedDiseases") or {}).get("rows", [])
        ensembl_id = target_data.get("_ensemblId", "")

        # Add Ensembl cross-reference
        if ensembl_id:
            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "Ensembl")
            cr.set("id", ensembl_id)
            cr.set("category", "function")
            p = ET.SubElement(cr, self._lmp_tag("Property"))
            p.set("name", "source")
            p.text = "opentargets"

        for row in rows:
            disease = row.get("disease") or {}
            disease_id = disease.get("id", "")
            disease_name = disease.get("name", "")
            score = row.get("score", 0)
            if not disease_id:
                continue

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "OpenTargets")
            cr.set("id", disease_id)
            cr.set("category", "disease")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "disease_name")
            p1.text = disease_name
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "association_score")
            p2.text = f"{score:.4f}"
            p3 = ET.SubElement(cr, self._lmp_tag("Property"))
            p3.set("name", "source")
            p3.text = "opentargets_v4"

            # Datatype scores
            for ds in (row.get("datatypeScores") or []):
                if ds.get("score", 0) > 0:
                    pd = ET.SubElement(cr, self._lmp_tag("Property"))
                    pd.set("name", f"score_{ds['id']}")
                    pd.text = f"{ds['score']:.4f}"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "ASSOCIATED_WITH_DISEASE")
            e.set("db", "OpenTargets")
            e.set("id", disease_id)
            e.set("source", gene_name)
            e.set("target", disease_name)
            e.set("score", f"{score:.4f}")

            count += 1

        self.logger.info("OpenTargets: added %d disease associations for %s", count, gene_name)
        return count

    # ── ChEMBL ──

    def _fetch_chembl_target(self, uniprot_id: str) -> Optional[str]:
        """Resolve UniProt ID to ChEMBL target ID."""
        if not self.chembl_enabled:
            return None
        url = f"{self.CHEMBL_API_BASE}/target.json"
        params = {"target_components__accession": uniprot_id, "limit": 1, "format": "json"}
        data = self._safe_api_get(url, timeout=self.chembl_timeout, params=params, api_name="chembl")
        if data and data.get("targets"):
            return data["targets"][0].get("target_chembl_id")
        return None

    def _fetch_chembl_bioactivities(self, target_chembl_id: str) -> Optional[List[Dict]]:
        """Fetch bioactivity data for a ChEMBL target."""
        url = f"{self.CHEMBL_API_BASE}/activity.json"
        params = {
            "target_chembl_id": target_chembl_id,
            "limit": self.chembl_max_activities,
            "format": "json",
            "pchembl_value__isnull": "false",
        }
        data = self._safe_api_get(url, timeout=self.chembl_timeout, params=params, api_name="chembl")
        if data and data.get("activities"):
            return data["activities"]
        return None

    def _add_chembl_bioactivity_v4(
        self,
        kg_elem: ET.Element,
        *,
        uniprot_id: str,
        gene_name: str,
    ) -> int:
        """Add ChEMBL bioactivity data as CrossReference + Edge elements."""
        target_id = self._fetch_chembl_target(uniprot_id)
        if not target_id:
            return 0

        activities = self._fetch_chembl_bioactivities(target_id)
        if not activities:
            return 0

        # Add ChEMBL target cross-reference
        cr_target = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
        cr_target.set("db", "ChEMBL")
        cr_target.set("id", target_id)
        cr_target.set("category", "chemistry")
        p = ET.SubElement(cr_target, self._lmp_tag("Property"))
        p.set("name", "source")
        p.text = "chembl_api"

        count = 0
        seen_molecules = set()
        for act in activities:
            mol_id = act.get("molecule_chembl_id", "")
            if not mol_id or mol_id in seen_molecules:
                continue
            seen_molecules.add(mol_id)

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "ChEMBL")
            cr.set("id", mol_id)
            cr.set("category", "chemistry")

            for prop_name, prop_key in [
                ("molecule_name", "molecule_pref_name"),
                ("activity_type", "standard_type"),
                ("activity_value", "standard_value"),
                ("activity_units", "standard_units"),
                ("pchembl_value", "pchembl_value"),
                ("assay_type", "assay_type"),
            ]:
                val = act.get(prop_key)
                if val is not None:
                    p = ET.SubElement(cr, self._lmp_tag("Property"))
                    p.set("name", prop_name)
                    p.text = str(val)

            p_src = ET.SubElement(cr, self._lmp_tag("Property"))
            p_src.set("name", "source")
            p_src.text = "chembl_bioactivity"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "HAS_BIOACTIVITY")
            e.set("db", "ChEMBL")
            e.set("id", mol_id)
            e.set("source", gene_name)
            e.set("target", mol_id)

            count += 1

        self.logger.info("ChEMBL: added %d bioactivities for %s (%s)", count, gene_name, target_id)
        return count

    # ── KEGG ──

    def _fetch_kegg_pathways(self, gene_name: str) -> Optional[List[Dict]]:
        """Fetch KEGG pathway annotations for a human gene."""
        if not self.kegg_enabled:
            return None
        # First find KEGG gene ID
        url = f"{self.KEGG_API_BASE}/find/hsa/{gene_name}"
        try:
            text = self._safe_api_get_text(url, timeout=self.kegg_timeout, api_name="kegg")
            if not text:
                return None
            lines = text.strip().split("\n")
            if not lines or not lines[0].strip():
                return None
            # Find exact gene name match — avoid partial hits like DESR1 matching ESR1
            kegg_gene_id = None
            gene_upper = gene_name.upper()
            for line in lines:
                cols = line.split("\t")
                if len(cols) >= 2:
                    # Gene symbols are comma-separated after the ID, with optional descriptions after ;
                    symbols_part = cols[1].split(";")[0]
                    symbols = [s.strip().upper() for s in symbols_part.split(",")]
                    if gene_upper in symbols:
                        kegg_gene_id = cols[0].strip()
                        break
            if not kegg_gene_id:
                # Fallback to first result if no exact match found
                kegg_gene_id = lines[0].split("\t")[0].strip()

            # Get gene info with pathway links
            url2 = f"{self.KEGG_API_BASE}/get/{kegg_gene_id}"
            text2 = self._safe_api_get_text(url2, timeout=self.kegg_timeout, api_name="kegg")
            if not text2:
                return None

            pathways = []
            in_pathway = False
            for line in text2.split("\n"):
                if line.startswith("PATHWAY"):
                    in_pathway = True
                    parts = line.replace("PATHWAY", "").strip().split("  ", 1)
                    if len(parts) == 2:
                        pathways.append({"id": parts[0].strip(), "name": parts[1].strip()})
                elif in_pathway:
                    if line.startswith(" "):
                        parts = line.strip().split("  ", 1)
                        if len(parts) == 2:
                            pathways.append({"id": parts[0].strip(), "name": parts[1].strip()})
                    else:
                        in_pathway = False

            return pathways if pathways else None
        except Exception as e:
            self.logger.debug("KEGG lookup failed for %s: %s", gene_name, e)
            return None

    def _add_kegg_pathways_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
    ) -> int:
        """Add KEGG metabolic pathway annotations."""
        pathways = self._fetch_kegg_pathways(gene_name)
        if not pathways:
            return 0

        count = 0
        for pw in pathways:
            pw_id = pw.get("id", "")
            pw_name = pw.get("name", "")
            if not pw_id:
                continue

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "KEGG")
            cr.set("id", pw_id)
            cr.set("category", "pathway")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "pathway_name")
            p1.text = pw_name
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "source")
            p2.text = "kegg_api"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "IN_PATHWAY")
            e.set("db", "KEGG")
            e.set("id", pw_id)
            e.set("source", gene_name)
            e.set("target", f"KEGG:{pw_id}")

            count += 1

        self.logger.info("KEGG: added %d pathway annotations for %s", count, gene_name)
        return count

    # ── Reactome ──

    def _fetch_reactome_pathways(self, uniprot_id: str) -> Optional[List[Dict]]:
        """Fetch Reactome pathway data for a UniProt ID."""
        if not self.reactome_enabled:
            return None
        url = f"{self.REACTOME_API_BASE}/data/mapping/UniProt/{uniprot_id}/pathways"
        headers = {"Accept": "application/json"}
        data = self._safe_api_get(url, timeout=self.reactome_timeout, headers=headers, api_name="reactome")
        if isinstance(data, list):
            return data[:self.reactome_max_pathways]
        return None

    def _add_reactome_pathways_v4(
        self,
        kg_elem: ET.Element,
        *,
        uniprot_id: str,
        gene_name: str,
    ) -> int:
        """Add Reactome pathway details."""
        pathways = self._fetch_reactome_pathways(uniprot_id)
        if not pathways:
            return 0

        count = 0
        for pw in pathways:
            pw_id = pw.get("stId", "")
            pw_name = pw.get("displayName", "")
            species = pw.get("speciesName", "")
            if not pw_id:
                continue

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "Reactome")
            cr.set("id", pw_id)
            cr.set("category", "pathway")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "pathway_name")
            p1.text = pw_name
            if species:
                p2 = ET.SubElement(cr, self._lmp_tag("Property"))
                p2.set("name", "species")
                p2.text = species
            p3 = ET.SubElement(cr, self._lmp_tag("Property"))
            p3.set("name", "source")
            p3.text = "reactome_content_service"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "IN_PATHWAY")
            e.set("db", "Reactome")
            e.set("id", pw_id)
            e.set("source", gene_name)
            e.set("target", f"Reactome:{pw_id}")

            count += 1

        self.logger.info("Reactome: added %d pathways for %s", count, gene_name)
        return count

    # ── ProteinAtlas ──

    def _fetch_protein_atlas(self, gene_name: str) -> Optional[Dict]:
        """Fetch tissue expression data from Human Protein Atlas search API."""
        if not self.protein_atlas_enabled:
            return None
        if self.offline_mode or not self._can_fetch_network():
            return None
        if self._is_circuit_open("protein_atlas"):
            return None
        try:
            self._rate_limit_wait("protein_atlas")
            url = f"{self.PROTEIN_ATLAS_API_BASE}/api/search_download.php"
            params = {
                "search": gene_name,
                "format": "json",
                "columns": "g,t,rnats,sc,up",
            }
            resp = requests.get(url, timeout=self.protein_atlas_timeout, params=params)
            if resp.status_code != 200:
                self.logger.debug("ProteinAtlas HTTP %d for %s", resp.status_code, gene_name)
                if 400 <= resp.status_code < 500:
                    self._record_api_success("protein_atlas")
                return None
            # Response is gzip-compressed even when Accept-Encoding not set
            import gzip as _gzip
            try:
                text = _gzip.decompress(resp.content).decode("utf-8")
            except Exception:
                text = resp.text
            data = json.loads(text)
            self._record_api_success("protein_atlas")
            if isinstance(data, list):
                # Find exact gene match
                for entry in data:
                    if entry.get("Gene") == gene_name:
                        return entry
                # Fallback: return first result
                return data[0] if data else None
            return data if isinstance(data, dict) else None
        except Exception as e:
            self.logger.debug("ProteinAtlas failed for %s: %s", gene_name, e)
            self._record_api_failure("protein_atlas")
            return None

    def _add_protein_atlas_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
    ) -> int:
        """Add ProteinAtlas tissue expression data (search API format)."""
        data = self._fetch_protein_atlas(gene_name)
        if not data:
            return 0

        count = 0

        # RNA tissue specificity (from search_download API)
        rna_raw = data.get("RNA tissue specificity", "")
        rna_spec = rna_raw[0] if isinstance(rna_raw, list) and rna_raw else str(rna_raw or "")
        if rna_spec:
            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "ProteinAtlas")
            cr.set("id", f"{gene_name}_rna_specificity")
            cr.set("category", "function")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "rna_tissue_specificity")
            p1.text = rna_spec
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "source")
            p2.text = "protein_atlas"
            count += 1

        # Subcellular location
        subcell_raw = data.get("Subcellular location", "")
        subcell = subcell_raw[0] if isinstance(subcell_raw, list) and subcell_raw else str(subcell_raw or "")
        if subcell:
            for loc_name in [s.strip() for s in subcell.split(";") if s.strip()][:10]:
                cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
                cr.set("db", "ProteinAtlas")
                cr.set("id", f"{gene_name}_subcell_{loc_name}")
                cr.set("category", "function")
                p1 = ET.SubElement(cr, self._lmp_tag("Property"))
                p1.set("name", "subcellular_location")
                p1.text = loc_name
                p2 = ET.SubElement(cr, self._lmp_tag("Property"))
                p2.set("name", "source")
                p2.text = "protein_atlas"
                count += 1

        # Tissue expression (if available from full response)
        rna_tissues = data.get("rna_tissue") or []
        for tissue in rna_tissues[:30]:
            tissue_name = tissue.get("tissue", "")
            value = tissue.get("value")
            unit = tissue.get("unit", "nTPM")
            if not tissue_name or value is None:
                continue
            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "ProteinAtlas")
            cr.set("id", f"{gene_name}_{tissue_name}")
            cr.set("category", "function")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "tissue")
            p1.text = tissue_name
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "expression_value")
            p2.text = str(value)
            p3 = ET.SubElement(cr, self._lmp_tag("Property"))
            p3.set("name", "unit")
            p3.text = unit
            p4 = ET.SubElement(cr, self._lmp_tag("Property"))
            p4.set("name", "source")
            p4.text = "protein_atlas"
            count += 1

        # UniProt cross-ref
        uniprot_raw = data.get("Uniprot", "")
        uniprot_id = uniprot_raw[0] if isinstance(uniprot_raw, list) and uniprot_raw else str(uniprot_raw)
        if uniprot_id:
            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "ProteinAtlas")
            cr.set("id", f"{gene_name}_uniprot")
            cr.set("category", "identity")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "uniprot_id")
            p1.text = uniprot_id
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "source")
            p2.text = "protein_atlas"
            count += 1

        self.logger.info("ProteinAtlas: added %d entries for %s", count, gene_name)
        return count

    # ── GeneOntology ──

    def _fetch_go_annotations(self, gene_name: str) -> Optional[List[Dict]]:
        """Fetch GO annotations via QuickGO API."""
        if not self.go_enabled:
            return None
        url = "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
        params = {
            "geneProductId": gene_name,
            "taxonId": "9606",
            "limit": 100,
        }
        headers = {"Accept": "application/json"}
        data = self._safe_api_get(url, timeout=self.go_timeout, headers=headers, params=params, api_name="go")
        if data and data.get("results"):
            return data["results"]
        return None

    def _add_go_enrichment_v4(
        self,
        kg_elem: ET.Element,
        *,
        uniprot_id: str,
        gene_name: str,
    ) -> int:
        """Add Gene Ontology annotations."""
        annotations = self._fetch_go_annotations(uniprot_id)
        if not annotations:
            return 0

        count = 0
        seen_terms = set()
        for ann in annotations:
            go_id = ann.get("goId", "")
            go_name = ann.get("goName", "")
            go_aspect = ann.get("goAspect", "")
            evidence = ann.get("goEvidence", "")
            if not go_id or go_id in seen_terms:
                continue
            seen_terms.add(go_id)

            aspect_map = {
                "biological_process": "function",
                "molecular_function": "function",
                "cellular_component": "function",
            }

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "GO")
            cr.set("id", go_id)
            cr.set("category", aspect_map.get(go_aspect, "function"))
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "name")
            p1.text = go_name
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "aspect")
            p2.text = go_aspect
            if evidence:
                p3 = ET.SubElement(cr, self._lmp_tag("Property"))
                p3.set("name", "evidence_code")
                p3.text = evidence
            p4 = ET.SubElement(cr, self._lmp_tag("Property"))
            p4.set("name", "source")
            p4.text = "quickgo_api"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "HAS_FUNCTION")
            e.set("db", "GO")
            e.set("id", go_id)
            e.set("source", gene_name)
            e.set("target", f"GO:{go_id}")

            count += 1

        self.logger.info("GO: added %d annotations for %s", count, gene_name)
        return count

    # ── Ensembl ──

    def _fetch_ensembl_data(self, gene_name: str) -> Optional[Dict]:
        """Fetch gene/transcript info from Ensembl REST API."""
        if not self.ensembl_enabled:
            return None
        url = f"{self.ENSEMBL_API_BASE}/lookup/symbol/homo_sapiens/{gene_name}"
        headers = {"Content-Type": "application/json"}
        params = {"expand": 1}
        return self._safe_api_get(url, timeout=self.ensembl_timeout, headers=headers, params=params, api_name="ensembl")

    def _add_ensembl_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
    ) -> int:
        """Add Ensembl gene and transcript data."""
        data = self._fetch_ensembl_data(gene_name)
        if not data:
            return 0

        count = 0
        ensembl_id = data.get("id", "")
        if ensembl_id:
            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "Ensembl")
            cr.set("id", ensembl_id)
            cr.set("category", "function")
            for prop_name, prop_key in [
                ("biotype", "biotype"),
                ("description", "description"),
                ("strand", "strand"),
                ("seq_region_name", "seq_region_name"),
                ("start", "start"),
                ("end", "end"),
            ]:
                val = data.get(prop_key)
                if val is not None:
                    p = ET.SubElement(cr, self._lmp_tag("Property"))
                    p.set("name", prop_name)
                    p.text = str(val)
            p_src = ET.SubElement(cr, self._lmp_tag("Property"))
            p_src.set("name", "source")
            p_src.text = "ensembl_rest"
            count += 1

        # Transcripts
        for tx in (data.get("Transcript") or [])[:10]:
            tx_id = tx.get("id", "")
            if not tx_id:
                continue
            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "Ensembl")
            cr.set("id", tx_id)
            cr.set("category", "function")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "type")
            p1.text = "transcript"
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "biotype")
            p2.text = tx.get("biotype", "")
            p3 = ET.SubElement(cr, self._lmp_tag("Property"))
            p3.set("name", "source")
            p3.text = "ensembl_rest"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "HAS_TRANSCRIPT")
            e.set("db", "Ensembl")
            e.set("id", tx_id)
            e.set("source", gene_name)
            e.set("target", tx_id)
            count += 1

        self.logger.info("Ensembl: added %d entries for %s", count, gene_name)
        return count

    # ── HPO (Human Phenotype Ontology) ──

    def _fetch_hpo_phenotypes(self, gene_name: str) -> Optional[List[Dict]]:
        """Fetch HPO phenotypes associated with a gene.

        Two-step: search gene → get disease annotations using NCBIGene: prefix.
        """
        if not self.hpo_enabled:
            return None
        # Step 1: Find the exact NCBI gene ID
        search_url = f"{self.HPO_API_BASE.replace('/hp', '')}/network/search/gene"
        search_data = self._safe_api_get(
            search_url, timeout=self.hpo_timeout,
            params={"q": gene_name}, api_name="hpo",
        )
        if not isinstance(search_data, dict) or not search_data.get("results"):
            return None
        # Find exact match
        gene_full_id = None
        for res in search_data["results"]:
            if res.get("name") == gene_name:
                gene_full_id = res.get("id", "")  # e.g. "NCBIGene:6714"
                break
        if not gene_full_id:
            return None
        # Step 2: Get disease annotations using full ID (NCBIGene:XXXX)
        annot_url = f"{self.HPO_API_BASE.replace('/hp', '')}/network/annotation/{gene_full_id}"
        annot_data = self._safe_api_get(annot_url, timeout=self.hpo_timeout, api_name="hpo")
        if isinstance(annot_data, dict) and annot_data.get("diseases"):
            return [
                {"id": d.get("id", ""), "name": d.get("name", "")}
                for d in annot_data["diseases"][:20]
            ]
        return None

    def _add_hpo_phenotypes_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
    ) -> int:
        """Add HPO phenotype associations."""
        phenotypes = self._fetch_hpo_phenotypes(gene_name)
        if not phenotypes:
            return 0

        count = 0
        for ph in phenotypes:
            ph_id = ph.get("id") or ph.get("ontologyId", "")
            ph_name = ph.get("name", "")
            if not ph_id:
                continue

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "HPO")
            cr.set("id", ph_id)
            cr.set("category", "disease")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "phenotype_name")
            p1.text = ph_name
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "source")
            p2.text = "hpo_api"

            e = ET.SubElement(kg_elem, self._lmp_tag("Edge"))
            e.set("type", "HAS_PHENOTYPE")
            e.set("db", "HPO")
            e.set("id", ph_id)
            e.set("source", gene_name)
            e.set("target", f"HPO:{ph_id}")

            count += 1

        self.logger.info("HPO: added %d phenotypes for %s", count, gene_name)
        return count

    # ── GTEx ──

    def _fetch_gtex_expression(self, gene_name: str) -> Optional[List[Dict]]:
        """Fetch GTEx tissue expression data.

        Two-step: lookup gencodeId from gene symbol, then query expression.
        """
        if not self.gtex_enabled:
            return None
        # Step 1: Lookup gencodeId
        ref_url = f"{self.GTEX_API_BASE}/reference/gene"
        ref_data = self._safe_api_get(
            ref_url, timeout=self.gtex_timeout,
            params={"geneId": gene_name, "format": "json"}, api_name="gtex",
        )
        if not isinstance(ref_data, dict) or not ref_data.get("data"):
            return None
        gencode_id = ref_data["data"][0].get("gencodeId")
        if not gencode_id:
            return None
        # Step 2: Query median tissue expression with gencodeId
        expr_url = f"{self.GTEX_API_BASE}/expression/medianGeneExpression"
        expr_data = self._safe_api_get(
            expr_url, timeout=self.gtex_timeout,
            params={"gencodeId": gencode_id, "datasetId": "gtex_v8"}, api_name="gtex",
        )
        if isinstance(expr_data, dict) and expr_data.get("data"):
            return expr_data["data"][:30]
        return None

    def _add_gtex_expression_v4(
        self,
        kg_elem: ET.Element,
        *,
        gene_name: str,
    ) -> int:
        """Add GTEx tissue expression as CrossReference elements."""
        expressions = self._fetch_gtex_expression(gene_name)
        if not expressions:
            return 0

        count = 0
        for expr in expressions:
            tissue = expr.get("tissueSiteDetailId", "")
            median_tpm = expr.get("median", 0)
            if not tissue:
                continue

            cr = ET.SubElement(kg_elem, self._lmp_tag("CrossReference"))
            cr.set("db", "GTEx")
            cr.set("id", f"{gene_name}_{tissue}")
            cr.set("category", "function")
            p1 = ET.SubElement(cr, self._lmp_tag("Property"))
            p1.set("name", "tissue")
            p1.text = tissue
            p2 = ET.SubElement(cr, self._lmp_tag("Property"))
            p2.set("name", "median_tpm")
            p2.text = f"{median_tpm:.2f}"
            p3 = ET.SubElement(cr, self._lmp_tag("Property"))
            p3.set("name", "source")
            p3.text = "gtex_v8"

            count += 1

        self.logger.info("GTEx: added %d tissue expressions for %s", count, gene_name)
        return count

    def generate_lmp_v4_multi_state(
        self,
        *,
        uniprot_id: str,
        gene_name: str,
        organism: str = "Homo sapiens",
        states: Optional[List[str]] = None,
        isoforms: Optional[List[str]] = None,
        pdb_ids: Optional[List[str]] = None,
        pdb_id_for_ifp: Optional[str] = None,
        trajectory_path: Optional[Path] = None,
        ligand_resname: Optional[str] = None,
        require_ifp: bool = False,
        structure_generation_receipt: Optional[Mapping[str, Any]] = None,
        dynamic_statistics_receipt: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, str]:
        """Generate LMP v4.0 XML documents (one per state, optionally per isoform).

        If *isoforms* is ``None`` and the UniProt entry contains alternative-product
        isoform accessions, per-isoform documents are generated automatically
        (isoform × state Cartesian product).  Pass ``isoforms=[]`` to suppress
        isoform expansion.

        If the selected preset includes TrajectoryIFP, you must provide
        pdb_id_for_ifp + trajectory_path. ligand_resname is optional if auto-discovery is enabled.
        """
        # Fetch UniProt data.
        # Fast-path: if caller is doing PDB-centric generation (passing a PDB id as `uniprot_id`),
        # skip UniProt network calls and rely on PDB for sequences/chains.
        if pdb_ids and self._looks_like_pdb_id(uniprot_id):
            uniprot_data = self._create_minimal_uniprot_data(uniprot_id)
        else:
            uniprot_data = self._fetch_uniprot(uniprot_id)
        ptms = self._extract_ptms(uniprot_data)
        domains = self._extract_domains(uniprot_data)
        binding_sites = self._extract_binding_sites(uniprot_data)
        motifs = self._extract_motifs(uniprot_data)

        sequence_override: Optional[str] = None
        if not uniprot_data.get("sequence") and pdb_ids:
            # Use a sensible default sequence when user passes a PDB id as --uniprot.
            # Prefer the *longest* polymer chain sequence (avoid picking a short peptide first).
            try:
                pdb_fallback = self._fetch_pdb(str(pdb_ids[0]))
                chains = pdb_fallback.get("chains", {}) if isinstance(pdb_fallback, dict) else {}
                if isinstance(chains, dict) and chains:
                    best_chain = None
                    best_len = -1
                    for c in chains.values():
                        if not isinstance(c, dict):
                            continue
                        seq = c.get("sequence")
                        if not isinstance(seq, str) or not seq:
                            continue
                        if len(seq) > best_len:
                            best_len = len(seq)
                            best_chain = c
                    if best_chain and isinstance(best_chain.get("sequence"), str):
                        sequence_override = str(best_chain.get("sequence"))
                        if not gene_name and best_chain.get("protein"):
                            gene_name = str(best_chain.get("protein"))
            except Exception:
                sequence_override = None

        domains = self._enrich_with_pas(
            domains=domains,
            sequence=uniprot_data.get("sequence", ""),
            ptms=ptms,
            uniprot_data=uniprot_data,
        )

        pdb_data = {}
        if pdb_ids:
            for pid in pdb_ids:
                pdb_data[pid] = self._fetch_pdb(pid)

        pdb_mode = bool(pdb_ids) and (self._looks_like_pdb_id(uniprot_id) or not uniprot_data.get("sequence"))
        pdb_mode_chains: Dict[str, Dict[str, Any]] = {}
        if pdb_mode and pdb_ids:
            try:
                primary_pdb = str(pdb_ids[0]).upper()
                pdb_entry = pdb_data.get(primary_pdb)
                if isinstance(pdb_entry, dict) and isinstance(pdb_entry.get("chains"), dict):
                    pdb_mode_chains = pdb_entry.get("chains", {})
            except Exception:
                pdb_mode_chains = {}

        pdb_binding_sites: List[Dict[str, Any]] = []
        # PDB-centric mode: avoid extra network calls (binding-sites endpoints can be flaky)
        # and we already compute structural annotations (PLIP/DSSP) directly from the PDB.
        if pdb_ids and self._preset_bool("include_features", default=True) and not pdb_mode:
            pdb_binding_sites = self._collect_pdb_binding_sites([str(x).upper() for x in pdb_ids])

        if states is None:
            states = self._infer_states(ptms, domains)

        # ── WI-29: Isoform-aware state expansion ────────────────────────
        # Extract isoform accessions from UniProt alternative-products when
        # caller did not explicitly pass ``isoforms``.
        _isoform_ids: List[str] = []
        if isoforms is None:
            try:
                for comment in (uniprot_data.get("comments") or []):
                    if not isinstance(comment, dict):
                        continue
                    if comment.get("commentType", "").upper() == "ALTERNATIVE PRODUCTS":
                        for iso in (comment.get("isoforms") or []):
                            iso_id = (iso.get("isoformIds") or [None])[0] if isinstance(iso.get("isoformIds"), list) else None
                            iso_name = iso.get("name", {}).get("value", "") if isinstance(iso.get("name"), dict) else ""
                            if iso_id:
                                _isoform_ids.append(str(iso_id))
                            elif iso_name:
                                _isoform_ids.append(str(iso_name))
            except Exception as _iso_exc:
                self.logger.debug("Isoform extraction failed (continuing without): %s", _iso_exc)
        elif isoforms:
            _isoform_ids = list(isoforms)
        # if isoforms==[] explicitly, _isoform_ids stays empty → no expansion

        # Optional IFP compute (no mocks)
        ifp_result = None
        used_ligand_resname: Optional[str] = None
        receptor_chain_id: Optional[str] = None
        if self.preset is not None and self._preset_bool("include_trajectory_ifp", default=False):
            if not (pdb_id_for_ifp and trajectory_path):
                if require_ifp:
                    raise ValueError("TrajectoryIFP requested by preset, but missing pdb_id_for_ifp/trajectory_path")
            else:
                topo_path = self._download_pdb_topology(pdb_id_for_ifp, fmt="pdb")
                try:
                    ifp_result, used_ligand_resname = self._compute_trajectory_ifp_smic(
                        topology_path=topo_path,
                        trajectory_path=Path(trajectory_path),
                        ligand_resname=str(ligand_resname) if ligand_resname else None,
                        stride=getattr(self.preset, "ifp_stride", 1) if self.preset else 1,
                        max_frames=getattr(self.preset, "max_ifp_frames", None) if self.preset else None,
                        receptor_sel=(getattr(self.preset, "ifp_receptor_sel", None) or "protein") if self.preset else "protein",
                        auto_ligand=bool(getattr(self.preset, "ifp_auto_ligand", True)) if self.preset else True,
                        auto_chain=bool(getattr(self.preset, "ifp_auto_chain", True)) if self.preset else True,
                        detect_metals=bool(getattr(self.preset, "ifp_detect_metals", False)) if self.preset else False,
                        pdb_id=str(pdb_id_for_ifp),
                    )
                    try:
                        ctx = getattr(ifp_result, "mica_ifp_context", None)
                        if isinstance(ctx, dict):
                            receptor_chain_id = ctx.get("receptor_chain")
                    except Exception:
                        receptor_chain_id = None
                except Exception as e:
                    if require_ifp:
                        raise
                    self.logger.warning(
                        "TrajectoryIFP compute failed for pdb_id_for_ifp=%s trajectory=%s (continuing without IFP): %s",
                        pdb_id_for_ifp,
                        trajectory_path,
                        e,
                    )
                    ifp_result = None
                    used_ligand_resname = None
                    receptor_chain_id = None

        # Register namespace once
        ET.register_namespace("lmp", self.LMP_V4_NS)

        lmp_documents: Dict[str, str] = {}

        # ── WI-29: Cartesian product isoform × state ────────────────────
        # When isoforms are discovered, each document is keyed
        # ``"{isoform_id}_{state}"``; otherwise just ``"{state}"``.
        _loop_isoforms: List[Optional[str]] = (
            [str(i) for i in _isoform_ids] if _isoform_ids else [None]
        )
        for _current_isoform, state_name in itertools.product(_loop_isoforms, states):
            doc_key = (
                f"{_current_isoform}_{state_name}" if _current_isoform else state_name
            )
            # Use isoform-specific data (sequence, protein_name) when iterating an isoform
            _active_uniprot_data = (
                self._resolve_isoform_uniprot_data(
                    base_uniprot_data=uniprot_data,
                    isoform_accession=_current_isoform,
                )
                if _current_isoform else uniprot_data
            )
            state_ptms = [ptm for ptm in ptms if self._get_ptm_status_for_state(ptm, state_name) == "present"]
            sequence = sequence_override or _active_uniprot_data.get("sequence", "")
            if self._should_include_nesy():
                nesy_sequence = self._encode_nesy_sequence(
                    sequence=sequence,
                    domains=domains,
                    ptms=state_ptms,
                    binding_sites=binding_sites,
                    motifs=motifs,
                    state_name=state_name,
                )
            else:
                nesy_sequence = sequence

            root = ET.Element(self._lmp_tag("LMP"))
            root.set("version", "4.0")
            if self.preset is not None:
                root.set("preset", getattr(self.preset, "name", ""))

            # Identity
            identity = ET.SubElement(root, self._lmp_tag("Identity"))

            # Canonical identity (protein-centric):
            # - PrimaryAccession stays as the accession (e.g., Q9Y3S1)
            # - UniProtKBId prefers UniProt's entry name when present (e.g., WNK2_HUMAN)
            # - BudoID is stable/canonical and does NOT include PDB/state labels
            primary_accession = (
                uniprot_data.get("primaryAccession")
                or uniprot_data.get("primary_accession")
                or uniprot_data.get("uniprot_id")
                or uniprot_id
            )
            uniprot_kb_id = (
                uniprot_data.get("uniProtkbId")
                or uniprot_data.get("uniProtKBId")
                or uniprot_data.get("uniprot_kb_id")
                or None
            )
            budo_id = self._compute_budo_root_id(uniprot_data=uniprot_data, uniprot_id=uniprot_id)

            ET.SubElement(identity, self._lmp_tag("BudoID")).text = budo_id
            ET.SubElement(identity, self._lmp_tag("PrimaryAccession")).text = str(primary_accession)

            # ── WI-29: Isoform annotation in Identity ─────────────
            if _current_isoform:
                ET.SubElement(identity, self._lmp_tag("IsoformAccession")).text = str(_current_isoform)
            if uniprot_kb_id:
                ET.SubElement(identity, self._lmp_tag("UniProtKBId")).text = str(uniprot_kb_id)
            else:
                ET.SubElement(identity, self._lmp_tag("UniProtKBId")).text = str(primary_accession)

            if uniprot_data.get("entryType") is not None:
                ET.SubElement(identity, self._lmp_tag("EntryType")).text = str(uniprot_data.get("entryType"))
            if uniprot_data.get("active") is not None:
                try:
                    ET.SubElement(identity, self._lmp_tag("Active")).text = "true" if bool(uniprot_data.get("active")) else "false"
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)
            if uniprot_data.get("proteinExistence"):
                ET.SubElement(identity, self._lmp_tag("ProteinExistence")).text = str(uniprot_data.get("proteinExistence"))
            org_elem = ET.SubElement(identity, self._lmp_tag("Organism"))
            org_elem.text = organism
            taxid = uniprot_data.get("taxonomy_id")
            if taxid:
                org_elem.set("id", str(taxid))

            lineages = uniprot_data.get("lineages")
            if isinstance(lineages, list) and lineages:
                lins = ET.SubElement(identity, self._lmp_tag("Lineages"))
                for lin in lineages[:200]:
                    if lin:
                        ET.SubElement(lins, self._lmp_tag("Lineage")).text = str(lin)

            secondary = uniprot_data.get("secondaryAccessions")
            if isinstance(secondary, list) and secondary:
                sec = ET.SubElement(identity, self._lmp_tag("SecondaryAccessions"))
                for acc in secondary[:200]:
                    if acc:
                        ET.SubElement(sec, self._lmp_tag("Value")).text = str(acc)

            # Semantics
            include_semantics = self._preset_bool("include_semantics", default=True)
            include_nesy_grammar = self._preset_bool("include_nesy_grammar", fallback_attr="include_nesy", default=True)
            if include_semantics or include_nesy_grammar:
                semantics = ET.SubElement(root, self._lmp_tag("Semantics"))
                if include_semantics:
                    protein_name = _active_uniprot_data.get("protein_name") or _active_uniprot_data.get("proteinName")
                    if protein_name:
                        ET.SubElement(semantics, self._lmp_tag("ProteinName")).text = str(protein_name)

                    genes: List[str] = []
                    if gene_name:
                        genes.append(str(gene_name))
                    if uniprot_data.get("gene_name") and str(uniprot_data.get("gene_name")) not in genes:
                        genes.append(str(uniprot_data.get("gene_name")))
                    if genes:
                        genes_elem = ET.SubElement(semantics, self._lmp_tag("Genes"))
                        for g in genes:
                            ET.SubElement(genes_elem, self._lmp_tag("Value")).text = g

                    keywords = uniprot_data.get("keywords")
                    if isinstance(keywords, (list, tuple)) and keywords:
                        kw_elem = ET.SubElement(semantics, self._lmp_tag("Keywords"))
                        for kw in keywords[:100]:
                            if kw:
                                ET.SubElement(kw_elem, self._lmp_tag("Value")).text = str(kw)

                if include_nesy_grammar:
                    nesy = ET.SubElement(semantics, self._lmp_tag("NeSyGrammar"))
                    nesy.set("version", "2.0")
                    nesy.set("length", str(len(nesy_sequence)))
                    nesy.text = nesy_sequence

                # UniProt comments (best-effort)
                # XSD order in SemanticsType requires Comment after NeSyGrammar.
                comments = uniprot_data.get("comments")
                if isinstance(comments, list):
                    for c in comments[:50]:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("commentType")
                        texts = c.get("texts")
                        if isinstance(texts, list) and texts:
                            joined = "\n".join(
                                str(t.get("value"))
                                for t in texts
                                if isinstance(t, dict) and t.get("value")
                            ).strip()
                            if joined:
                                ce = ET.SubElement(semantics, self._lmp_tag("Comment"))
                                if ctype:
                                    ce.set("type", str(ctype))
                                ce.text = joined

            # Pre-init variables needed by KnowledgeGraph (populated by Geometry block if active)
            _kg_sifts_domains: Dict[str, List[Dict[str, Any]]] = {}
            _kg_entity_metadata: Optional[Dict[str, Dict[str, Any]]] = None
            structure_generation_receipt_payload = self._coerce_structure_generation_receipt(
                structure_generation_receipt
            )

            # Geometry
            if self._preset_bool("include_geometry", fallback_attr="include_structure", default=True):
                geometry = ET.SubElement(root, self._lmp_tag("Geometry"))

                # Optional v3-style sequence + simple features (must appear before <Chain> per XSD)
                try:
                    seq_elem = ET.SubElement(geometry, self._lmp_tag("Sequence"))
                    seq_elem.set("length", str(len(sequence)))
                    seq_elem.text = str(sequence)
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)

                # Best-effort secondary structure (helices/strands) via PDBe API.
                # Schema note: LMP v4 XSD does not have a dedicated SecondaryStructure block;
                # we encode segments as <Feature type=... start=... end=... description=.../>.
                include_structural_features = self._preset_bool("include_features", default=False)
                if include_structural_features and pdb_ids and self._can_fetch_network():
                    for pdb_id in [str(x).upper() for x in (pdb_ids or [])][:3]:
                        try:
                            sec = self._fetch_secondary_structure(pdb_id)
                        except Exception:
                            sec = {}
                        if not isinstance(sec, dict) or not sec:
                            continue

                        for chain_id, elems in list(sec.items())[:50]:
                            if not isinstance(elems, list) or not elems:
                                continue
                            for el in elems[:500]:
                                if not isinstance(el, dict):
                                    continue
                                etype = str(el.get("type") or "").strip().lower()
                                start_raw = el.get("start")
                                end_raw = el.get("end")
                                if not isinstance(start_raw, (int, str)) or not isinstance(end_raw, (int, str)):
                                    continue
                                try:
                                    start_i = int(start_raw)
                                    end_i = int(end_raw)
                                except Exception:
                                    continue
                                if start_i <= 0 or end_i <= 0 or end_i < start_i:
                                    continue

                                seg_len = (end_i - start_i) + 1
                                # Filter out extremely short segments (PDBe may emit 1-residue strands/turns).
                                # Helices shorter than 4 residues and strands shorter than 2 residues are usually noise.
                                if etype in {"helix", "alpha_helix"} and seg_len < 4:
                                    continue
                                if etype in {"sheet", "strand", "beta_sheet"} and seg_len < 2:
                                    continue

                                # Normalize to schema-friendly types
                                if etype in {"helix", "alpha_helix"}:
                                    ftype = "secondary_structure:helix"
                                elif etype in {"sheet", "strand", "beta_sheet"}:
                                    ftype = "secondary_structure:strand"
                                else:
                                    ftype = f"secondary_structure:{etype or 'segment'}"

                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", ftype)
                                # IMPORTANT: PDBe reports PDB residue numbers (author numbering), which is NOT
                                # guaranteed to match the LMP v4 <Sequence> indexing (and in pdb_mode we often
                                # emit multiple <Chain> entries). To avoid generating nonsense coordinates,
                                # store residue ranges in description instead of start/end attributes.
                                desc_bits = [
                                    f"pdb={pdb_id}",
                                    f"chain={chain_id}",
                                    f"res_start={start_i}",
                                    f"res_end={end_i}",
                                    f"len={seg_len}",
                                ]
                                if el.get("strand_id") is not None:
                                    desc_bits.append(f"strand_id={el.get('strand_id')}")
                                feat.set("description", " ".join(desc_bits))
                                feat.text = ""

                    # PDB entry metadata (cell, resolution, citation) as Features (schema-safe)
                    try:
                        primary_pdb = str(pdb_ids[0]).upper()
                        pdb_entry = pdb_data.get(primary_pdb)
                        if isinstance(pdb_entry, dict):
                            cell = pdb_entry.get("cell")
                            if isinstance(cell, dict):
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "pdb:cell")
                                desc = (
                                    f"pdb={primary_pdb} "
                                    f"a={cell.get('length_a')} b={cell.get('length_b')} c={cell.get('length_c')} "
                                    f"alpha={cell.get('angle_alpha')} beta={cell.get('angle_beta')} gamma={cell.get('angle_gamma')}"
                                )
                                feat.set("description", desc)

                            rinfo = pdb_entry.get("rcsb_entry_info", {})
                            if isinstance(rinfo, dict):
                                res = None
                                try:
                                    res_list = rinfo.get("resolution_combined")
                                    if isinstance(res_list, list) and res_list:
                                        res = res_list[0]
                                except Exception:
                                    res = None
                                if res is not None:
                                    feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                    feat.set("type", "pdb:resolution")
                                    feat.set("description", f"pdb={primary_pdb} resolution={res}")

                            citation = pdb_entry.get("rcsb_primary_citation")
                            if isinstance(citation, dict):
                                doi = citation.get("pdbx_database_id_doi")
                                pmid = citation.get("pdbx_database_id_pub_med")
                                title = citation.get("title")
                                if doi or pmid or title:
                                    feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                    feat.set("type", "pdb:citation")
                                    desc_bits = [f"pdb={primary_pdb}"]
                                    if doi:
                                        desc_bits.append(f"doi={doi}")
                                    if pmid:
                                        desc_bits.append(f"pmid={pmid}")
                                    if title:
                                        desc_bits.append(f"title={str(title)[:120]}")
                                    feat.set("description", " ".join(desc_bits))
                    except Exception as _exc:
                        self.logger.debug("Suppressed: %s", _exc)

                    # MVP: structural metrics from a temporary PDB (COM/Rg/BBox + COM distances)
                    try:
                        from .pdb_metrics import compute_chain_geometry_metrics

                        metrics_pdb_id = str(pdb_ids[0]).upper() if pdb_ids else None
                        if metrics_pdb_id:
                            temp_pdb_path: Optional[Path] = None
                            try:
                                temp_pdb_path = self._download_pdb_topology_temp(metrics_pdb_id, fmt="pdb")
                                metrics = compute_chain_geometry_metrics(temp_pdb_path)
                            finally:
                                if temp_pdb_path and temp_pdb_path.exists():
                                    try:
                                        temp_pdb_path.unlink()
                                    except Exception as _exc:
                                        self.logger.debug("Suppressed: %s", _exc)

                            if isinstance(metrics, dict) and metrics.get("chains"):
                                for cid, cdata in metrics.get("chains", {}).items():
                                    if not isinstance(cdata, dict):
                                        continue
                                    # COM feature
                                    com = cdata.get("com")
                                    if isinstance(com, list) and len(com) == 3:
                                        feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                        feat.set("type", "geometry:com")
                                        feat.set("description", f"pdb={metrics_pdb_id} chain={cid} x={com[0]} y={com[1]} z={com[2]}")

                                    # Rg feature
                                    rg = cdata.get("rg")
                                    if isinstance(rg, (int, float)):
                                        feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                        feat.set("type", "geometry:rg")
                                        feat.set("description", f"pdb={metrics_pdb_id} chain={cid} rg={rg}")

                                    # Bounding box feature
                                    bbox = cdata.get("bbox")
                                    if isinstance(bbox, dict):
                                        bmin = bbox.get("min")
                                        bmax = bbox.get("max")
                                        if isinstance(bmin, list) and isinstance(bmax, list) and len(bmin) == 3 and len(bmax) == 3:
                                            feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                            feat.set("type", "geometry:bbox")
                                            feat.set("description", f"pdb={metrics_pdb_id} chain={cid} min={bmin} max={bmax}")

                                for dist in metrics.get("com_distances", []) or []:
                                    if not isinstance(dist, dict):
                                        continue
                                    ca = dist.get("chain_a")
                                    cb = dist.get("chain_b")
                                    dval = dist.get("distance")
                                    if ca and cb and isinstance(dval, (int, float)):
                                        feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                        feat.set("type", "geometry:chain_distance")
                                        feat.set("description", f"pdb={metrics_pdb_id} chain_a={ca} chain_b={cb} com_distance={dval}")
                    except BaseException as e:
                        # Metrics are a best-effort MVP; on some Windows envs MDAnalysis/SciPy
                        # imports can be slow or problematic. Never let this block PDB-centric
                        # generation (PLIP/DSSP/features).
                        self.logger.warning("Failed to compute structural metrics MVP: %s", e)

                if pdb_mode and pdb_mode_chains:
                    # Normalize to author chain IDs when possible.
                    # RCSB FASTA sometimes keys chains by entity index ("1", "2"...),
                    # but the downloaded PDB uses author chain IDs ("A", "B"...).
                    # PLIP relies on matching those PDB chain IDs when marking peptide
                    # chains as ligands, so we build an auth-keyed view here.
                    pdb_mode_chains_auth: Dict[str, Dict[str, Any]] = {}
                    for _cid, _cinfo in (pdb_mode_chains or {}).items():
                        if not isinstance(_cinfo, dict):
                            continue
                        auth_cid = str(_cinfo.get("auth_chain_id") or _cid).strip()
                        if not auth_cid:
                            continue
                        # Avoid collisions just in case.
                        key = auth_cid
                        if key in pdb_mode_chains_auth:
                            key = str(_cid).strip() or auth_cid
                        pdb_mode_chains_auth[key] = _cinfo

                    # PDB-centric: emit peptide-ligand features BEFORE Chain (XSD order)
                    try:
                        primary_pdb = str(pdb_ids[0]).upper() if pdb_ids else ""
                        for cid, cinfo in sorted(pdb_mode_chains_auth.items()):
                            seq = str(cinfo.get("sequence") or "")
                            if not seq:
                                continue
                            if len(seq) <= 10:
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "ligand:peptide")
                                feat.set("description", f"pdb={primary_pdb} chain={cid} seq={seq}")
                                m = re.search(r"RF.V", seq)
                                if m:
                                    start = m.start() + 1
                                    end = m.end()
                                    feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                    feat.set("type", "ligand:RFxV")
                                    feat.set("start", str(start))
                                    feat.set("end", str(end))
                                    feat.set("description", f"pdb={primary_pdb} chain={cid} seq={seq}")
                    except Exception as _exc:
                        self.logger.debug("Suppressed: %s", _exc)

                    # Best-effort: compute PLIP inter-chain interfaces and DSSP segments once.
                    # IMPORTANT: DSSP is emitted as <Feature> and MUST appear before <Chain> (XSD order).
                    interchain_ifaces: Dict[str, List[Dict[str, Any]]] = {}
                    try:
                        primary_pdb = str(pdb_ids[0]).upper() if pdb_ids else ""
                        if primary_pdb:
                            temp_pdb_path: Optional[Path] = None
                            try:
                                temp_pdb_path = self._download_pdb_topology_temp(primary_pdb, fmt="pdb")
                                interchain_ifaces = self._compute_plip_interchain_interfaces(
                                    pdb_id=primary_pdb,
                                    pdb_path=temp_pdb_path,
                                    chains=pdb_mode_chains_auth,
                                )

                                dssp_segments = self._compute_dssp_segments_smic(pdb_path=temp_pdb_path)
                                if isinstance(dssp_segments, dict):
                                    for chain_id, segs in dssp_segments.items():
                                        if not isinstance(segs, list):
                                            continue
                                        for seg in segs:
                                            try:
                                                ss_type = str(getattr(seg, "ss_type", "") or "")
                                                rs = int(getattr(seg, "res_start", 0) or 0)
                                                re_ = int(getattr(seg, "res_end", 0) or 0)
                                            except Exception:
                                                continue
                                            if rs <= 0 or re_ <= 0 or re_ < rs:
                                                continue
                                            feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                            feat.set("type", f"secondary_structure:dssp:{ss_type or 'segment'}")
                                            feat.set(
                                                "description",
                                                f"pdb={primary_pdb} chain={chain_id} res_start={rs} res_end={re_} len={re_ - rs + 1}",
                                            )
                            finally:
                                if temp_pdb_path and temp_pdb_path.exists():
                                    try:
                                        temp_pdb_path.unlink()
                                    except Exception as _exc:
                                        self.logger.debug("Suppressed: %s", _exc)
                    except BaseException as exc:
                        # CRITICAL: catch BaseException (not just Exception) because PLIP/OpenBabel
                        # C extensions can raise SystemExit on internal errors.  C-level exit(1)
                        # still cannot be caught here — see subprocess isolation TODO.
                        self.logger.warning("PLIP/DSSP computation failed (BaseException): %s", exc)
                        interchain_ifaces = {}

                    # ---- PDBe SIFTS domain mapping (InterPro/Pfam/CATH) per chain ----
                    sifts_domains: Dict[str, List[Dict[str, Any]]] = {}
                    try:
                        primary_pdb = str(pdb_ids[0]).upper() if pdb_ids else ""
                        if primary_pdb and self._can_fetch_network():
                            sifts_domains = self._fetch_interpro_for_pdb(primary_pdb)
                            if sifts_domains:
                                total_d = sum(len(v) for v in sifts_domains.values())
                                self.logger.info("PDBe SIFTS domains for %s: %d domains across %d chains",
                                                 primary_pdb, total_d, len(sifts_domains))
                    except Exception as exc:
                        self.logger.warning("Failed to fetch PDBe SIFTS domains: %s", exc)
                    # Export for KG
                    _kg_sifts_domains = sifts_domains

                    # ---- Enriched PDB metadata as Features ----
                    try:
                        primary_pdb = str(pdb_ids[0]).upper() if pdb_ids else ""
                        pdb_entry = pdb_data.get(primary_pdb, {}) if isinstance(pdb_data, dict) else {}
                        enriched = pdb_entry.get("enriched_meta")
                        # Export entity metadata for KG
                        _kg_entity_metadata = pdb_entry.get("entity_metadata")
                        if isinstance(enriched, dict):
                            # Experimental method
                            if enriched.get("experimental_method"):
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "pdb:experimental_method")
                                feat.set("description", f"pdb={primary_pdb} method={enriched['experimental_method']}")
                            # Assembly count
                            if enriched.get("assembly_count") is not None:
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "pdb:assembly")
                                feat.set("description", f"pdb={primary_pdb} assembly_count={enriched['assembly_count']}")
                            # Polymer/nonpolymer counts
                            if enriched.get("deposited_polymer_entity_count") is not None:
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "pdb:entity_count")
                                feat.set("description", (
                                    f"pdb={primary_pdb} "
                                    f"polymer={enriched.get('deposited_polymer_entity_count')} "
                                    f"nonpolymer={enriched.get('deposited_nonpolymer_entity_count')}"
                                ))
                            # Validation metrics
                            vmetrics = enriched.get("validation")
                            if isinstance(vmetrics, dict):
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "pdb:validation")
                                vbits = [f"pdb={primary_pdb}"]
                                if vmetrics.get("clashscore") is not None:
                                    vbits.append(f"clashscore={vmetrics['clashscore']}")
                                if vmetrics.get("ramachandran_outliers_percent") is not None:
                                    vbits.append(f"rama_outliers={vmetrics['ramachandran_outliers_percent']}")
                                if vmetrics.get("rfree") is not None:
                                    vbits.append(f"rfree={vmetrics['rfree']}")
                                feat.set("description", " ".join(vbits))
                            # Symmetry
                            sym = enriched.get("symmetry")
                            if isinstance(sym, dict) and sym.get("symbol"):
                                feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                feat.set("type", "pdb:symmetry")
                                feat.set("description", (
                                    f"pdb={primary_pdb} symbol={sym.get('symbol')} "
                                    f"type={sym.get('type')} stoichiometry={sym.get('stoichiometry')}"
                                ))
                        # Entity-level metadata (GO terms, EC numbers)
                        entity_meta_map = pdb_entry.get("entity_metadata")
                        if isinstance(entity_meta_map, dict):
                            for eid, emeta in entity_meta_map.items():
                                if not isinstance(emeta, dict):
                                    continue
                                # GO terms
                                for go in (emeta.get("go_terms") or []):
                                    if isinstance(go, dict) and go.get("id"):
                                        feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                        feat.set("type", "annotation:go")
                                        feat.set("description", (
                                            f"pdb={primary_pdb} entity={eid} "
                                            f"go_id={go['id']} go_name={go.get('name', '')} "
                                            f"go_type={go.get('type', '')}"
                                        ))
                                # EC numbers
                                ec_nums = emeta.get("ec_numbers")
                                if isinstance(ec_nums, list) and ec_nums:
                                    for ec in ec_nums:
                                        ec_str = ec.get("id") if isinstance(ec, dict) else str(ec)
                                        if ec_str:
                                            feat = ET.SubElement(geometry, self._lmp_tag("Feature"))
                                            feat.set("type", "annotation:ec")
                                            feat.set("description", f"pdb={primary_pdb} entity={eid} ec={ec_str}")
                    except Exception as _exc:
                        self.logger.debug("Suppressed: %s", _exc)

                    # Emit one Chain per PDB chain, avoid UniProt-domain labeling
                    for cid, cinfo in sorted(pdb_mode_chains_auth.items()):
                        seq = str(cinfo.get("sequence") or "")
                        if not seq:
                            continue
                        chain = ET.SubElement(geometry, self._lmp_tag("Chain"))
                        chain.set("id", str(cid))
                        chain.set("sequence", seq)
                        chain.set("state", state_name)

                        # PDBe SIFTS Domain elements (XSD: Domain before Interface)
                        cid_upper = str(cid).strip().upper()
                        for sdom in sifts_domains.get(cid_upper, []):
                            if not isinstance(sdom, dict):
                                continue
                            try:
                                ds = int(sdom.get("start", 0))
                                de = int(sdom.get("end", 0))
                            except (ValueError, TypeError):
                                continue
                            if ds <= 0 or de <= 0 or de < ds:
                                continue
                            dom_elem = ET.SubElement(chain, self._lmp_tag("Domain"))
                            dom_elem.set("name", str(sdom.get("name") or sdom.get("id") or "Domain"))
                            dom_elem.set("type", str(sdom.get("source", "domain")).lower())
                            dom_elem.set("start", str(ds))
                            dom_elem.set("end", str(de))
                            src = str(sdom.get("source", "")).strip()
                            sid = str(sdom.get("id", "")).strip()
                            if src == "InterPro" and sid.startswith("IPR"):
                                dom_elem.set("interpro_id", sid)
                            elif src == "Pfam" and sid.startswith("PF"):
                                dom_elem.set("pfam_id", sid)
                            elif src == "CATH":
                                dom_elem.set("type", "cath")
                                # Store CATH id in interpro_id field for now (XSD allows optional string)
                                dom_elem.set("interpro_id", f"CATH:{sid}")
                            dom_elem.set("class", classify_domain(src, str(sdom.get("name") or sid or "")).value)

                        # Emit Interface elements for this chain (XSD: ChainType allows Interface after Domain).
                        for attrs in interchain_ifaces.get(str(cid).strip().upper(), []) or []:
                            try:
                                iface = ET.SubElement(chain, self._lmp_tag("Interface"))
                                iface.set("partner_protein", str(attrs.get("partner_protein") or f"pdb:{str(pdb_ids[0]).upper()}"))
                                iface.set("partner_chain", str(attrs.get("partner_chain") or "?"))
                                iface.set("interface_residues", str(attrs.get("interface_residues") or ""))
                                iface.set("type", str(attrs.get("type") or "protein-protein"))

                                if attrs.get("interaction_source"):
                                    iface.set("interaction_source", str(attrs.get("interaction_source")))
                                if attrs.get("interaction_type"):
                                    iface.set("interaction_type", str(attrs.get("interaction_type")))

                                for r in (attrs.get("residue_elems") or []) if isinstance(attrs, dict) else []:
                                    if not isinstance(r, dict):
                                        continue
                                    try:
                                        relem = ET.SubElement(iface, self._lmp_tag("Residue"))
                                        if r.get("side"):
                                            relem.set("side", str(r.get("side")))
                                        if r.get("chain"):
                                            relem.set("chain", str(r.get("chain")))
                                        if r.get("resname"):
                                            relem.set("resname", str(r.get("resname")))
                                        if r.get("resnum") is not None:
                                            relem.set("resnum", str(r.get("resnum")))
                                        if r.get("label"):
                                            relem.set("label", str(r.get("label")))
                                    except Exception as _exc:
                                        self.logger.debug("Suppressed: %s", _exc)
                            except Exception as _exc:
                                self.logger.debug("Suppressed: %s", _exc)
                else:
                    chain = ET.SubElement(geometry, self._lmp_tag("Chain"))
                    chain.set("id", str(receptor_chain_id or "A"))
                    chain.set("sequence", nesy_sequence)
                    chain.set("state", state_name)

                # ---- PDBe SIFTS fetch for UniProt mode (fallback domain IDs) ----
                # When not in PDB mode but PDB IDs are available, fetch SIFTS data
                # so we can enrich UniProt domains with InterPro/Pfam IDs.
                if not (pdb_mode and pdb_mode_chains) and pdb_ids and not _kg_sifts_domains:
                    try:
                        primary_pdb = str(pdb_ids[0]).upper()
                        if primary_pdb and self._can_fetch_network():
                            _sifts_result = self._fetch_interpro_for_pdb(primary_pdb)
                            if _sifts_result:
                                _kg_sifts_domains = _sifts_result
                                total_d = sum(len(v) for v in _sifts_result.values())
                                self.logger.info(
                                    "PDBe SIFTS domains (UniProt mode) for %s: %d domains across %d chains",
                                    primary_pdb, total_d, len(_sifts_result),
                                )
                    except Exception as exc:
                        self.logger.warning("Failed to fetch PDBe SIFTS domains (UniProt mode): %s", exc)

                # UniProt-derived domain segmentation (v2-style detailed blocks)
                # Keep this separate from the GlobalFeatures domain (used as a container for PTMs/motifs/sites).
                if not (pdb_mode and pdb_mode_chains) and self._preset_bool("include_features", default=False):
                    for d in domains:
                        if not isinstance(d, dict):
                            continue
                        try:
                            start_i = int(d.get("start") or 1)
                            end_i = int(d.get("end") or 1)
                        except Exception:
                            continue
                        if start_i <= 0 or end_i <= 0 or end_i < start_i:
                            continue
                        if len(sequence) > 0:
                            start_i = max(1, min(start_i, len(sequence)))
                            end_i = max(1, min(end_i, len(sequence)))
                            if end_i < start_i:
                                continue

                        dom_elem = ET.SubElement(chain, self._lmp_tag("Domain"))
                        dom_elem.set("name", str(d.get("domain_name") or d.get("domain_id") or "Domain"))
                        if d.get("domain_type"):
                            dom_elem.set("type", str(d.get("domain_type")))
                        dom_elem.set("start", str(start_i))
                        dom_elem.set("end", str(end_i))
                        interpro_id = d.get("interpro_id")
                        if not interpro_id:
                            # Back-compat: some domain dicts store InterPro accessions under domain_id.
                            dom_id = d.get("domain_id")
                            if isinstance(dom_id, str) and dom_id.startswith("IPR"):
                                interpro_id = dom_id
                        if interpro_id:
                            dom_elem.set("interpro_id", str(interpro_id))
                        if d.get("pfam_id"):
                            dom_elem.set("pfam_id", str(d.get("pfam_id")))
                        dom_elem.set("class", d.get("domain_class", DomainClass.UNKNOWN.value))

                    # ---- SIFTS domain fallback for UniProt mode ----
                    # When InterPro-by-UniProt API failed (no domains have interpro_id/pfam_id),
                    # emit PDBe SIFTS domains (InterPro/Pfam/CATH) from the PDB chain mapping.
                    # This ensures cross-reference IDs are always available when we have PDB data.
                    has_ipr_pfam = any(
                        isinstance(d, dict) and (d.get("interpro_id") or d.get("pfam_id"))
                        for d in domains
                    )
                    if not has_ipr_pfam and _kg_sifts_domains:
                        _emitted_sifts = set()  # deduplicate by (source, id, start, end)
                        for _chain_sifts in _kg_sifts_domains.values():
                            for sdom in _chain_sifts:
                                if not isinstance(sdom, dict):
                                    continue
                                _skey = (sdom.get("source"), sdom.get("id"), sdom.get("start"), sdom.get("end"))
                                if _skey in _emitted_sifts:
                                    continue
                                _emitted_sifts.add(_skey)
                                try:
                                    ds = int(sdom.get("start", 0))
                                    de = int(sdom.get("end", 0))
                                except (ValueError, TypeError):
                                    continue
                                if ds <= 0 or de <= 0 or de < ds:
                                    continue
                                dom_elem = ET.SubElement(chain, self._lmp_tag("Domain"))
                                dom_elem.set("name", str(sdom.get("name") or sdom.get("id") or "Domain"))
                                dom_elem.set("type", str(sdom.get("source", "domain")).lower())
                                dom_elem.set("start", str(ds))
                                dom_elem.set("end", str(de))
                                src = str(sdom.get("source", "")).strip()
                                sid = str(sdom.get("id", "")).strip()
                                if src == "InterPro" and sid.startswith("IPR"):
                                    dom_elem.set("interpro_id", sid)
                                elif src == "Pfam" and sid.startswith("PF"):
                                    dom_elem.set("pfam_id", sid)
                                elif src == "CATH":
                                    dom_elem.set("type", "cath")
                                    dom_elem.set("interpro_id", f"CATH:{sid}")
                                dom_elem.set("class", classify_domain(src, str(sdom.get("name") or sid or "")).value)

                # Global domain for PTMs/motifs/binding-sites (schema-compliant)
                if not (pdb_mode and pdb_mode_chains) and self._preset_bool("include_features", default=True):
                    global_domain = ET.SubElement(chain, self._lmp_tag("Domain"))
                    global_domain.set("name", "GlobalFeatures")
                    global_domain.set("type", "global")
                    global_domain.set("class", DomainClass.GLOBAL.value)
                    global_domain.set("start", "1")
                    global_domain.set("end", str(max(1, len(sequence))))

                    for motif in motifs:
                        motif_elem = ET.SubElement(global_domain, self._lmp_tag("Motif"))
                        motif_elem.set("name", motif.get("name", "Motif"))
                        motif_elem.set("start", str(motif.get("start", 1)))
                        motif_elem.set("end", str(motif.get("end", 1)))
                        if motif.get("type"):
                            motif_elem.set("type", str(motif.get("type")))

                    for ptm in ptms:
                        ptm_elem = ET.SubElement(global_domain, self._lmp_tag("PTM"))
                        ptm_elem.set("id", ptm["ptm_id"])
                        ptm_elem.set("type", ptm["ptm_type"])
                        ptm_elem.set("residue", ptm["residue"])
                        ptm_elem.set("position", str(ptm["position"]))
                        ptm_elem.set("status", self._get_ptm_status_for_state(ptm, state_name))
                        if ptm.get("evidence"):
                            ptm_elem.set("evidence", str(ptm["evidence"]))

                    for bs in binding_sites:
                        bs_type = bs.get("type") or bs.get("site_type") or "binding"
                        residues = bs.get("residues")
                        if isinstance(residues, (list, tuple)):
                            residues_str = ",".join(str(x) for x in residues)
                        else:
                            residues_str = str(residues or "")
                        if not residues_str:
                            continue

                        bs_elem = ET.SubElement(global_domain, self._lmp_tag("BindingSite"))
                        bs_elem.set("type", str(bs_type))
                        bs_elem.set("residues", residues_str)

                        # Optional UniProt ligand hint (schema supports Ligand under BindingSite)
                        ligand_name_hint: Optional[str] = None
                        try:
                            if isinstance(bs_type, str) and "-binding" in bs_type:
                                ligand_name_hint = bs_type.split("-binding", 1)[0].strip().upper()
                        except Exception:
                            ligand_name_hint = None

                        if ligand_name_hint:
                            self._add_ligand_v4(
                                bs_elem,
                                ligand_name=ligand_name_hint,
                                ligand_obj=None,
                                site_type=str(bs_type),
                                stable_id_hint=f"lig_{ligand_name_hint}",
                            )

                    # PDB/PDBe-derived binding sites + ligands (best-effort)
                    for pbs in pdb_binding_sites:
                        if not isinstance(pbs, dict):
                            continue
                        residues = pbs.get("residues")
                        if not isinstance(residues, list) or not residues:
                            continue
                        bs_type = pbs.get("type") or f"PDB:{pbs.get('pdb_id', '')}:binding_site"
                        bs_elem = ET.SubElement(global_domain, self._lmp_tag("BindingSite"))
                        bs_elem.set("type", str(bs_type))
                        bs_elem.set("residues", ",".join(str(int(x)) for x in residues if isinstance(x, int) and x > 0))
                        ligs = pbs.get("ligands")
                        if isinstance(ligs, list):
                            for lig in ligs[:10]:
                                if not isinstance(lig, dict):
                                    continue
                                lig_id = str(lig.get("ligand_id") or lig.get("name") or "LIG").strip()
                                stable_id = f"lig_{pbs.get('pdb_id','')}_{lig_id}"
                                self._add_ligand_v4(
                                    bs_elem,
                                    ligand_name=lig_id,
                                    ligand_obj=lig,
                                    site_type=str(bs_type),
                                    stable_id_hint=stable_id,
                                )

                # ---- Structural Enrichment Blocks (v4.1) ----
                # AlphaFoldModel
                if self._preset_bool("include_alphafold", default=False):
                    self._add_alphafold_model_v4(
                        geometry,
                        accession=str(primary_accession),
                        domains=domains,
                    )

                # Resolve PDB path for DSSP / quality / network blocks
                _structural_pdb_path: Optional[str] = None
                _structural_source = "experimental"
                needs_structural = (
                    self._preset_bool("include_secondary_structure", default=False)
                    or self._preset_bool("include_structural_quality", default=False)
                    or self._preset_bool("include_network_annotation", default=False)
                    or self._preset_bool("include_pocket_sites", default=False)
                )
                if needs_structural:
                    _structural_pdb_path = self._resolve_structural_pdb_path(
                        accession=str(primary_accession),
                        pdb_ids=pdb_ids,
                    )
                    if _structural_pdb_path and "alphafold" in str(_structural_pdb_path).lower():
                        _structural_source = "alphafold"
                    # Clear stashed metrics from previous state iteration
                    self._last_structural_metrics = None

                # SecondaryStructure
                if _structural_pdb_path and self._preset_bool("include_secondary_structure", default=False):
                    self._add_secondary_structure_block_v4(
                        geometry,
                        pdb_path=_structural_pdb_path,
                        source=_structural_source,
                    )

                # StructuralQuality
                if _structural_pdb_path and self._preset_bool("include_structural_quality", default=False):
                    self._add_structural_quality_v4(
                        geometry,
                        pdb_path=_structural_pdb_path,
                        source=_structural_source,
                    )

                # NetworkAnnotation
                if _structural_pdb_path and self._preset_bool("include_network_annotation", default=False):
                    self._add_network_annotation_v4(
                        geometry,
                        pdb_path=_structural_pdb_path,
                        source=_structural_source,
                    )

                if _structural_pdb_path and self._preset_bool("include_pocket_sites", default=False):
                    self._add_pocket_sites_v4(
                        geometry,
                        pdb_path=_structural_pdb_path,
                        source=_structural_source,
                    )

                representative_structure_ref = ""
                if self._preset_bool("include_structure_catalog", default=False):
                    representative_structure_ref = self._add_structure_catalog_v4(
                        geometry,
                        primary_accession=str(primary_accession),
                        sequence=str(sequence),
                        pdb_ids=pdb_ids,
                        structure_generation_receipt=structure_generation_receipt_payload,
                    )

                membrane_segments = self._infer_membrane_segments(_active_uniprot_data)

                if self._preset_bool("include_residue_statistics", default=False):
                    self._add_residue_statistics_v4(
                        geometry,
                        sequence=str(sequence),
                        structure_ref=representative_structure_ref,
                        structure_generation_receipt=structure_generation_receipt_payload,
                    )

                if self._preset_bool("include_dynamic_statistics", default=False):
                    self._add_dynamics_statistics_v4(
                        geometry,
                        dynamic_statistics_receipt=dynamic_statistics_receipt,
                    )

                if self._preset_bool("include_membrane_topology", default=False):
                    self._add_membrane_topology_v4(
                        geometry,
                        structure_ref=representative_structure_ref,
                        membrane_segments=membrane_segments,
                    )

                if self._preset_bool("include_cell_context_hints", default=False):
                    self._add_cell_context_hints_v4(
                        geometry,
                        uniprot_data=_active_uniprot_data,
                        membrane_segments=membrane_segments,
                    )

                if self._preset_bool("include_topology_hooks", default=False):
                    self._add_topology_hooks_v4(
                        geometry,
                        preferred_structure_ref=representative_structure_ref,
                        primary_accession=str(primary_accession),
                        membrane_segments=membrane_segments,
                        structure_generation_receipt=structure_generation_receipt_payload,
                    )

                # Visuals (v4.3) — AF2/PDB/InterPro/FlatProt image URLs for FE protein card
                if self._preset_bool("include_structural_visuals", default=False):
                    self._add_visuals_v4(
                        geometry,
                        accession=str(primary_accession),
                        pdb_ids=pdb_ids,
                    )

                # TrajectoryIFP
                if ifp_result is not None and pdb_id_for_ifp and (used_ligand_resname or ligand_resname):
                    self._add_trajectory_ifp_v4(
                        geometry,
                        pdb_id=str(pdb_id_for_ifp).upper(),
                        ligand=str(used_ligand_resname or ligand_resname),
                        ifp_result=ifp_result,
                        stride=getattr(self.preset, "ifp_stride", 1) if self.preset else 1,
                    )

            # Provenance
            if self._preset_bool("include_knowledge_graph", default=False):
                self._add_knowledge_graph_v4(
                    root,
                    uniprot_data=uniprot_data,
                    source_id=budo_id,
                    pdb_sifts_domains=_kg_sifts_domains,
                    pdb_entity_metadata=_kg_entity_metadata,
                )

            if self._preset_bool("include_provenance", default=True):
                self._add_provenance_v4(
                    root,
                    uniprot_data=uniprot_data,
                    ground_truth_meta={
                        "pdb_ids": [str(x).upper() for x in (pdb_ids or [])],
                        "pdb_id_for_ifp": str(pdb_id_for_ifp).upper() if pdb_id_for_ifp else None,
                        "state": state_name,
                    },
                    structure_generation_receipt=structure_generation_receipt_payload,
                )

            lmp_documents[doc_key] = self._serialize_xml_v4(root)

        return lmp_documents

    def _organism_to_uniprot_mnemonic(self, organism: Optional[str]) -> Optional[str]:
        if not organism:
            return None
        org = str(organism).strip().replace("_", " ").lower()
        # Minimal, high-value mapping (expand as needed).
        mapping = {
            "homo sapiens": "HUMAN",
            "mus musculus": "MOUSE",
            "rattus norvegicus": "RAT",
            "saccharomyces cerevisiae": "YEAST",
            "escherichia coli": "ECOLI",
        }
        for k, v in mapping.items():
            if org == k or org.startswith(k + " "):
                return v
        return None

    def _resolve_isoform_uniprot_data(
        self,
        *,
        base_uniprot_data: Dict[str, Any],
        isoform_accession: str,
    ) -> Dict[str, Any]:
        """Return merged UniProt data for an isoform.

        Fetches the isoform entry and overlays isoform-specific fields (sequence,
        protein_name, primaryAccession) on the canonical base data.  Functional
        annotations (features, comments, keywords, lineages) are inherited from
        the canonical entry when the isoform record lacks them.  Adds
        ``_canonical_primaryAccession`` to track the originating canonical entry.
        """
        try:
            isoform_data: Dict[str, Any] = self._fetch_uniprot(isoform_accession)
        except Exception:
            isoform_data = {}

        merged = dict(base_uniprot_data)
        merged["_canonical_primaryAccession"] = (
            base_uniprot_data.get("primaryAccession")
            or base_uniprot_data.get("primary_accession")
            or base_uniprot_data.get("uniprot_id")
            or ""
        )

        # Override with isoform-specific values when present
        for key in ("primaryAccession", "primary_accession", "sequence", "protein_name"):
            val = isoform_data.get(key)
            if val:
                merged[key] = val

        # Functional annotations: fall back to canonical when isoform entry is empty
        for key in ("features", "comments", "keywords", "lineages", "secondaryAccessions"):
            iso_val = isoform_data.get(key)
            base_val = base_uniprot_data.get(key)
            if isinstance(iso_val, list) and not iso_val and isinstance(base_val, list) and base_val:
                merged[key] = base_val
            elif iso_val is not None:
                merged[key] = iso_val

        return merged

    def _compute_budo_root_id(self, *, uniprot_data: Dict[str, Any], uniprot_id: str) -> str:
        """Compute the canonical/root BUDO identity (CEA-style).

        CEA/BUDO V3 convention expects a stable root identity like:
          budo:ABL1_HUMAN_v1

        and modality-specific IDs derive from it (e.g. -S, -D). LMP v4 uses the
        root identity for Identity/BudoID.
        """
        entry_audit = uniprot_data.get("entryAudit")
        seq_version = None
        if isinstance(entry_audit, dict):
            seq_version = entry_audit.get("sequenceVersion")
        try:
            seq_version_i = int(seq_version) if seq_version is not None else 1
        except Exception:
            seq_version_i = 1

        # Prefer UniProt entry name when available (e.g., WNK2_HUMAN).
        uniprot_entry_name = (
            uniprot_data.get("uniProtkbId")
            or uniprot_data.get("uniProtKBId")
            or uniprot_data.get("uniprot_kb_id")
            or None
        )
        if uniprot_entry_name:
            canonical_name = str(uniprot_entry_name).strip()
        else:
            gene_name = (uniprot_data.get("gene_name") or "").strip()
            mnemonic = self._organism_to_uniprot_mnemonic(uniprot_data.get("organism"))
            if gene_name and mnemonic:
                canonical_name = f"{gene_name}_{mnemonic}"
            else:
                canonical_name = str(uniprot_id).strip()

        canonical_name = canonical_name.upper()
        canonical_name_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", canonical_name) or str(uniprot_id)
        return f"budo:{canonical_name_safe}_v{seq_version_i}"
    
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
        Create minimal UniProt data structure for graceful degradation.
        For PDB IDs, attempts to fetch sequence from RCSB FASTA before returning empty.
        
        Args:
            uniprot_id: UniProt accession or PDB ID
            
        Returns:
            Minimal valid data structure
        """
        sequence = ""
        gene_name = uniprot_id
        organism = "Unknown"
        taxonomy_id = 0

        # Best-effort: if this looks like a PDB ID, try fetching FASTA for metadata
        if self._looks_like_pdb_id(uniprot_id) and self._can_fetch_network():
            try:
                fasta_url = f"https://www.rcsb.org/fasta/entry/{uniprot_id.upper()}"
                resp = requests.get(fasta_url, timeout=8)
                if resp.ok and resp.text.strip():
                    chains = self._parse_pdb_fasta(resp.text)
                    if chains:
                        best = max(chains.values(), key=lambda c: len(c.get("sequence", "")), default={})
                        sequence = best.get("sequence", "")
                        gene_name = best.get("protein") or uniprot_id
                        organism = best.get("organism") or "Unknown"
            except Exception as exc:
                self.logger.debug("Minimal-data PDB FASTA fallback failed for %s: %s", uniprot_id, exc)

        return {
            "uniprot_id": uniprot_id,
            "gene_name": gene_name,
            "organism": organism,
            "taxonomy_id": taxonomy_id,
            "sequence": sequence,
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
        states_to_generate = ["Reference", "Substrate_bound"]
        
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
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)
        
        # Offline mode: return minimal data if no valid cache found
        if not self._can_fetch_network():
            self.logger.warning(f"Offline mode: no cache for {uniprot_id}, returning minimal data")
            return self._create_minimal_uniprot_data(uniprot_id)
        
        # Rate limit
        self._rate_limit_wait("uniprot")

        # Fetch from API
        request_start = time.perf_counter()
        url = f"{self.UNIPROT_API}/{uniprot_id}.json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            # Capture the UniProt release version from the response header for
            # downstream provenance tracking (header: "x-uniprot-release",
            # e.g. "2024_06"). Available on live fetches; absent from cache.
            _uniprot_release = response.headers.get("x-uniprot-release", "")
            
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
        protein_name = ""
        try:
            protein_desc = data.get("proteinDescription") or {}
            rec = (protein_desc.get("recommendedName") or {}).get("fullName") or {}
            protein_name = rec.get("value") or ""
        except Exception:
            protein_name = ""

        keywords: List[str] = []
        try:
            raw_keywords = data.get("keywords") or []
            if isinstance(raw_keywords, list):
                for kw in raw_keywords:
                    if isinstance(kw, dict):
                        # UniProt REST API uses "name" (not "value") for keyword text
                        val = kw.get("name") or kw.get("value")
                        if val:
                            keywords.append(str(val))
                    elif isinstance(kw, str) and kw:
                        keywords.append(kw)
        except Exception:
            keywords = []

        lineages: List[str] = []
        try:
            raw_lineages = (data.get("organism") or {}).get("lineage")
            if isinstance(raw_lineages, list):
                lineages = [str(x) for x in raw_lineages if x]
        except Exception:
            lineages = []

        uniprot_data = {
            "uniprot_id": uniprot_id,
            # UniProt release captured from HTTP response header (empty when loaded
            # from cache or when the header is absent).
            "_uniprot_release": _uniprot_release,
            # Keep key UniProt REST identifiers around (useful for CEA/BUDO naming).
            "primaryAccession": data.get("primaryAccession"),
            "uniProtkbId": data.get("uniProtkbId"),
            "gene_name": data.get("genes", [{}])[0].get("geneName", {}).get("value", ""),
            "organism": data.get("organism", {}).get("scientificName", "Unknown"),
            "taxonomy_id": data.get("organism", {}).get("taxonId", 0),
            "sequence": data.get("sequence", {}).get("value", ""),
            "protein_name": protein_name,
            "keywords": keywords,
            "lineages": lineages,
            "features": data.get("features", []),
            "dbReferences": data.get("uniProtKBCrossReferences", []),
            # Extra UniProt fields required for LMP v4 schema completeness
            "comments": data.get("comments", []),
            "references": data.get("references", []),
            "entryAudit": data.get("entryAudit", {}),
            "secondaryAccessions": data.get("secondaryAccessions", []),
            "entryType": data.get("entryType"),
            "active": data.get("active"),
            "proteinExistence": (data.get("proteinExistence") or {}).get("evidenceCode")
            if isinstance(data.get("proteinExistence"), dict)
            else data.get("proteinExistence"),
        }

        # Fetch InterPro Data for real domain coordinates
        try:
            # Use the InterPro API to get domain coordinates
            interpro_url = f"https://www.ebi.ac.uk/interpro/api/entry/interpro/protein/uniprot/{uniprot_id}"
            # Use a generous timeout for InterPro as this endpoint is often slow
            ip_response = requests.get(interpro_url, timeout=30)
            
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
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)
        
        # Offline mode or structure disabled: return empty structure
        if not self._can_fetch_network() or not self._should_include_structure():
            self.logger.warning(f"Offline mode or structure disabled: returning empty PDB data for {pdb_id}")
            return {"chains": {}, "metadata": {}}
        
        # Rate limit
        self._rate_limit_wait("pdb")
        
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
                self._rate_limit_wait("pdb")
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

                    # --- Enriched entity metadata (GO, EC, description, organism) ---
                    entity_meta: Dict[str, Any] = {}
                    # Entity description
                    rcsb_entity = entity_data.get("rcsb_polymer_entity", {})
                    if isinstance(rcsb_entity, dict):
                        entity_meta["description"] = rcsb_entity.get("pdbx_description")
                        entity_meta["organism_scientific"] = (
                            rcsb_entity.get("rcsb_source_organism", [{}])[0].get("ncbi_scientific_name")
                            if isinstance(rcsb_entity.get("rcsb_source_organism"), list) and rcsb_entity.get("rcsb_source_organism")
                            else None
                        )
                        entity_meta["ec_numbers"] = rcsb_entity.get("rcsb_ec_lineage")

                    # Polymer entity annotations (GO terms, Pfam, InterPro from RCSB)
                    annots = entity_data.get("rcsb_polymer_entity_annotation", [])
                    if isinstance(annots, list):
                        go_terms = []
                        pfam_ids = []
                        interpro_ids = []
                        for annot in annots:
                            if not isinstance(annot, dict):
                                continue
                            atype = annot.get("type") or ""
                            aid = annot.get("annotation_id") or ""
                            aname = annot.get("name") or ""
                            if "GO" in atype.upper():
                                go_terms.append({"id": aid, "name": aname, "type": atype})
                            elif "Pfam" in atype:
                                pfam_ids.append({"id": aid, "name": aname})
                            elif "InterPro" in atype:
                                interpro_ids.append({"id": aid, "name": aname})
                        if go_terms:
                            entity_meta["go_terms"] = go_terms
                        if pfam_ids:
                            entity_meta["pfam_annotations"] = pfam_ids
                        if interpro_ids:
                            entity_meta["interpro_annotations"] = interpro_ids

                    if entity_meta:
                        data.setdefault("entity_metadata", {})[entity_id] = entity_meta
            
            # Map entities to chains and add uniprot_id + entity metadata
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
                    # Attach entity metadata to chain
                    emeta = data.get("entity_metadata", {}).get(entity_id)
                    if isinstance(emeta, dict):
                        chain_data["entity_meta"] = emeta

        except Exception as e:
            self.logger.warning(f"Could not fetch entity UniProt mappings for {pdb_id}: {e}")

        # ---- Enriched entry-level PDB metadata ----
        try:
            entry_info = data.get("rcsb_entry_info", {})
            if isinstance(entry_info, dict):
                data["enriched_meta"] = {
                    "experimental_method": entry_info.get("experimental_method"),
                    "resolution": (
                        entry_info.get("resolution_combined", [None])[0]
                        if isinstance(entry_info.get("resolution_combined"), list) and entry_info.get("resolution_combined")
                        else None
                    ),
                    "molecular_weight": entry_info.get("molecular_weight"),
                    "deposited_polymer_entity_count": entry_info.get("deposited_polymer_entity_instance_count"),
                    "deposited_nonpolymer_entity_count": entry_info.get("deposited_nonpolymer_entity_instance_count"),
                    "assembly_count": entry_info.get("assembly_count"),
                    "diffrn_resolution_high": entry_info.get("diffrn_resolution_high", {}).get("value")
                    if isinstance(entry_info.get("diffrn_resolution_high"), dict)
                    else entry_info.get("diffrn_resolution_high"),
                }
            # Validation metrics from RCSB (if present in /core/entry response)
            # API may return a list (take first element) or a dict
            vrpt_raw = data.get("pdbx_vrpt_summary_geometry") or data.get("rcsb_entry_info", {}).get("pdbx_vrpt_summary_geometry")
            vrpt: Any = None
            if isinstance(vrpt_raw, list) and vrpt_raw and isinstance(vrpt_raw[0], dict):
                vrpt = vrpt_raw[0]
            elif isinstance(vrpt_raw, dict):
                vrpt = vrpt_raw
            if isinstance(vrpt, dict):
                data.setdefault("enriched_meta", {})["validation"] = {
                    "clashscore": vrpt.get("clashscore"),
                    "ramachandran_outliers_percent": vrpt.get("percent_ramachandran_outliers"),
                    "rotamer_outliers_percent": vrpt.get("percent_rotamer_outliers"),
                    "rfree": vrpt.get("R_free"),
                }
            # Assembly info
            struct_sym = data.get("rcsb_struct_symmetry")
            if isinstance(struct_sym, list) and struct_sym:
                sym0 = struct_sym[0] if isinstance(struct_sym[0], dict) else {}
                data.setdefault("enriched_meta", {})["symmetry"] = {
                    "symbol": sym0.get("symbol"),
                    "type": sym0.get("type"),
                    "stoichiometry": sym0.get("stoichiometry"),
                }
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)
        
        # Cache
        try:
            cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)
        
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

        chains: Dict[str, Dict[str, Any]] = {}

        fasta_text = (fasta_text or "").strip()
        if not fasta_text:
            return chains

        # Robust record-based parsing.
        # IMPORTANT: PDB FASTA may contain records like "Chains A, B" meaning the same sequence
        # applies to multiple chain IDs. We must NOT collapse them into a single numeric key.
        records = []
        current_header: Optional[str] = None
        current_seq_lines: List[str] = []

        for raw_line in fasta_text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if current_header is not None:
                    records.append((current_header, "".join(current_seq_lines)))
                current_header = line[1:].strip()
                current_seq_lines = []
            else:
                current_seq_lines.append(line)

        if current_header is not None:
            records.append((current_header, "".join(current_seq_lines)))

        for header, seq in records:
            if not seq:
                continue
            parts = header.split("|")
            protein_name = parts[2].strip() if len(parts) > 2 else ""
            organism_match = re.search(r"(.*?)\s*\((\d+)\)", parts[-1]) if len(parts) > 3 else None
            organism = organism_match.group(1).strip() if organism_match else ""

            # Extract entity id from prefix like "2V3S_1|..."
            entity_id: Optional[str] = None
            try:
                m_ent = re.match(r"(\w+)_(\d+)\|", header)
                if m_ent:
                    entity_id = m_ent.group(2)
            except Exception:
                entity_id = None

            # Parse chain IDs
            # Case 1: "Chain B[auth P]" -> label=B, auth=P
            auth_match = re.search(r"Chain\s+([A-Z0-9])\s*\[auth\s+([A-Z0-9])\]", header)
            if auth_match:
                label_cid = auth_match.group(1)
                auth_cid = auth_match.group(2)
                chain_ids = [(auth_cid, label_cid)]
            else:
                # Case 2/3: "Chains A, B" or "Chain A"
                chain_match = re.search(r"Chains?\s+([A-Z0-9, ]+)", header)
                if chain_match:
                    chain_str = chain_match.group(1)
                    all_chains = [ch.strip() for ch in chain_str.split(",") if ch.strip()]
                    chain_ids = [(cid, cid) for cid in all_chains]
                else:
                    # Fallback to entity id when no chain IDs are present
                    fallback = entity_id or (parts[0].split("_")[-1] if parts and "_" in parts[0] else parts[0])
                    chain_ids = [(fallback, fallback)]

            for auth_cid, label_cid in chain_ids:
                chains[str(auth_cid)] = {
                    "sequence": seq,
                    "length": len(seq),
                    "header": header,
                    "protein": protein_name,
                    "organism": organism,
                    "auth_chain_id": str(auth_cid),
                    "label_chain_id": str(label_cid),
                    "entity_id": entity_id,
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
        # Skip ligands if preset excludes them or offline mode
        if not self._should_include_pubchem() or not self._can_fetch_network():
            self.logger.info(f"Ligands disabled by preset or offline mode: returning empty list for {pdb_id}")
            return []
        
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
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)
        
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
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

            # Optional sidecar for PubChem enrichment details
            if self.pubchem_enabled and self.pubchem_write_sidecar_json:
                sidecar_name = f"{self.pubchem_sidecar_prefix}_{self._cache_key(pdb_id)}.json"
                sidecar_file = self.cache_dir / sidecar_name
                try:
                    sidecar_file.write_text(json.dumps(ligands, indent=2), encoding="utf-8")
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)
            
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
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)

        url = f"https://data.rcsb.org/rest/v1/core/chemcomp/{ligand_id}"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                return None
            ccd_data = response.json()
            try:
                cache_file.write_text(json.dumps(ccd_data, indent=2), encoding="utf-8")
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)
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
                except Exception as _exc:
                    self.logger.debug("Suppressed: %s", _exc)

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
                    except Exception as _exc:
                        self.logger.debug("Suppressed: %s", _exc)
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
    
    # ------------------------------------------------------------------
    # InterPro / PDBe SIFTS domain mapping for PDB-centric entries
    # ------------------------------------------------------------------
    def _fetch_interpro_for_pdb(self, pdb_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch InterPro / Pfam / CATH domain mappings per chain via PDBe SIFTS.

        Returns:
            {
                "A": [
                    {"source": "InterPro", "id": "IPR000719", "name": "Protein kinase domain",
                     "start": 57, "end": 290},
                    {"source": "Pfam", "id": "PF00069", "name": "Pkinase", "start": 60, "end": 285},
                    {"source": "CATH", "id": "3.30.200.20", "name": "Phosphorylase Kinase ...",
                     "start": 57, "end": 290},
                ],
                ...
            }
        """
        pdb_lower = str(pdb_id).strip().lower()
        cache_file = self.cache_dir / f"pdbe_sifts_domains_{pdb_lower}.json"
        valid_cache = self._get_cache_path(cache_file)
        if valid_cache:
            try:
                return json.loads(valid_cache.read_text(encoding="utf-8"))
            except Exception as _exc:
                self.logger.debug("Suppressed: %s", _exc)

        result: Dict[str, List[Dict[str, Any]]] = {}
        if not self._can_fetch_network():
            return result

        # PDBe SIFTS endpoints: InterPro, Pfam, CATH
        endpoints = {
            "InterPro": f"https://www.ebi.ac.uk/pdbe/api/mappings/interpro/{pdb_lower}",
            "Pfam": f"https://www.ebi.ac.uk/pdbe/api/mappings/pfam/{pdb_lower}",
            "CATH": f"https://www.ebi.ac.uk/pdbe/api/mappings/cath/{pdb_lower}",
        }

        for source_name, url in endpoints.items():
            try:
                self._rate_limit_wait("pdb")
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    continue
                raw = resp.json().get(pdb_lower, {})
                # PDBe wraps results in an extra source-name level:
                #   {"6fbk": {"InterPro": {"IPR000719": {...}, ...}}}
                # We unwrap source_name -> {accession: entry} before iterating.
                data = raw.get(source_name, raw)
                # Now data = { "IPR000719": { "identifier": "...", "mappings": [...] }, ... }
                for db_id, entry in data.items():
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("identifier") or entry.get("name") or db_id
                    for mapping in (entry.get("mappings") or []):
                        if not isinstance(mapping, dict):
                            continue
                        chain_id = mapping.get("chain_id") or mapping.get("struct_asym_id")
                        if not chain_id:
                            continue
                        start_raw = mapping.get("start", {})
                        end_raw = mapping.get("end", {})
                        start_num = start_raw.get("residue_number") if isinstance(start_raw, dict) else start_raw
                        end_num = end_raw.get("residue_number") if isinstance(end_raw, dict) else end_raw
                        if start_num is None or end_num is None:
                            continue
                        try:
                            s = int(start_num)
                            e = int(end_num)
                        except (ValueError, TypeError):
                            continue
                        if s <= 0 or e <= 0 or e < s:
                            continue
                        result.setdefault(str(chain_id).strip().upper(), []).append({
                            "source": source_name,
                            "id": str(db_id),
                            "name": str(name),
                            "start": s,
                            "end": e,
                        })
                self.logger.info("PDBe SIFTS %s for %s: fetched %d mappings",
                                 source_name, pdb_id.upper(),
                                 sum(len(v) for v in result.values()))
            except Exception as exc:
                self.logger.warning("PDBe SIFTS %s fetch failed for %s: %s", source_name, pdb_id, exc)

        # Cache
        try:
            cache_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)

        return result

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
                self._rate_limit_wait("uniprot")
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

                # v4 schema requires residue to be a canonical AA letter
                if not residue or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]", residue):
                    continue

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
                    "domain_class": classify_domain(feature_type, feature.get("description", "")).value,
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

            # --- Parse member_databases (Pfam, SMART, CDD, etc.) ---
            # The InterPro response contains member_databases with their own IDs.
            pfam_id: Optional[str] = None
            member_dbs = metadata.get("member_databases") or {}
            if isinstance(member_dbs, dict):
                pfam_entries = member_dbs.get("pfam") or member_dbs.get("Pfam") or {}
                if isinstance(pfam_entries, dict) and pfam_entries:
                    pfam_id = next(iter(pfam_entries.keys()), None)
            
            # Get coordinates
            for protein in match.get("proteins", []):
                # The API returns matches for the requested protein
                for location in protein.get("entry_protein_locations", []):
                    for fragment in location.get("fragments", []):
                        start = fragment.get("start")
                        end = fragment.get("end")
                        
                        if start and end:
                            dom_entry: Dict[str, Any] = {
                                "domain_id": ipr_id,
                                "interpro_id": ipr_id,
                                "domain_name": f"{ipr_name} (InterPro)",
                                "domain_type": ipr_type,
                                "domain_class": classify_domain(ipr_type, f"{ipr_name} (InterPro)", ipr_type).value,
                                "start": start,
                                "end": end,
                            }
                            if pfam_id:
                                dom_entry["pfam_id"] = pfam_id
                            domains.append(dom_entry)
        
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
        Conservative state inference — backed by PTM annotation evidence only.

        Never emits ``*_Active`` or ``*_Inactive`` states; never infers functional
        activity from protein-family membership alone.

        Taxonomy:
        - "Reference"     : neutral apo/unmodified baseline — always emitted.
        - "Phosphorylated": kinase with annotated phosphorylation sites.
        - "Modified"      : any protein with non-phospho PTM annotations,
                            OR non-kinase protein with phosphorylation.
        """
        states: set = {"Reference"}

        is_kinase = any(
            "kinase" in d.get("domain_name", "").lower() for d in domains
        )
        has_phosphorylation = any(
            ptm.get("ptm_type", "").lower() == "phosphorylation" for ptm in ptms
        )
        has_other_ptms = any(
            ptm.get("ptm_type", "").lower() not in ("", "phosphorylation")
            for ptm in ptms
        )

        if is_kinase and has_phosphorylation:
            states.add("Phosphorylated")
        elif has_phosphorylation or has_other_ptms:
            states.add("Modified")

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
                dfg_state = "DFG-UNKNOWN"
                
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
        
        # NEW: Use NeSy encoder for complete hierarchical sequence (if preset includes it)
        state_ptms = [ptm for ptm in ptms if self._get_ptm_status_for_state(ptm, state_name) == "present"]
        if self._should_include_nesy():
            nesy_sequence = self._encode_nesy_sequence(
                sequence=sequence,
                domains=domains,
                ptms=state_ptms,
                binding_sites=binding_sites,
                motifs=motifs,
                state_name=state_name
            )
        else:
            # Preset excludes NeSy: use canonical sequence
            nesy_sequence = sequence
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
        
        if "reference" in state_lower or "apo" in state_lower or "inactive" in state_lower:
            return "absent"
        elif "phosphorylated" in state_lower or "modified" in state_lower or "active" in state_lower:
            return "present"
        else:
            return "transient"
    
    def _add_kinase_feature_states(self, conf_elem: ET.Element, state_name: str):
        """Add kinase-specific feature states"""
        state_lower = state_name.lower()
        
        # Activation loop — conformation not inferred from state name alone
        feature_activation_loop = ET.SubElement(conf_elem, "FeatureState")
        feature_activation_loop.set("feature_name", "ActivationLoop")
        feature_activation_loop.set("state", "Undetermined")

        # ATP binding site — conformation not inferred from state name alone
        feature_atp = ET.SubElement(conf_elem, "FeatureState")
        feature_atp.set("feature_name", "ATP_BindingSite")
        feature_atp.set("state", "Undetermined")
    
    def _add_gpcr_feature_states(self, conf_elem: ET.Element, state_name: str):
        """Add GPCR-specific feature states"""
        state_lower = state_name.lower()
        
        # Transmembrane helices — conformation not inferred from state name alone
        feature_tm = ET.SubElement(conf_elem, "FeatureState")
        feature_tm.set("feature_name", "TransmembraneHelices")
        feature_tm.set("state", "Undetermined")
    
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
            # Normalize to schema enum: glycosylation
            return "glycosylation"
        elif "hydroxy" in description_lower:
            return "hydroxylation"
        elif "nitroso" in description_lower or "s-nitroso" in description_lower:
            return "nitrosylation"
        elif "palmito" in description_lower:
            return "palmitoylation"
        else:
            return "other"
    
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

    def _serialize_xml_v4(self, elem: ET.Element) -> str:
        """Serialize XML with preset-controlled formatting options."""
        pretty_print = bool(getattr(self.preset, "pretty_print", True)) if self.preset is not None else True
        include_decl = bool(getattr(self.preset, "include_xml_declaration", True)) if self.preset is not None else True

        if pretty_print:
            xml_str = self._prettify_xml(elem)
        else:
            xml_str = ET.tostring(elem, encoding="unicode")
            if include_decl:
                xml_str = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" + xml_str

        if not include_decl:
            lines = xml_str.splitlines(True)
            if lines and lines[0].lstrip().startswith("<?xml"):
                xml_str = "".join(lines[1:])
        return xml_str
    
    def _rate_limit_wait(self, api_name: Optional[str] = None):
        """Enforce per-API rate limit between requests.
        
        Args:
            api_name: API identifier for per-API tracking (e.g. 'string', 'kegg').
                      Falls back to global rate limit if None or unknown.
        """
        if api_name and api_name in self._api_rate_limits:
            limit = self._api_rate_limits[api_name]
            elapsed = time.time() - self._api_last_request.get(api_name, 0.0)
            if elapsed < limit:
                time.sleep(limit - elapsed)
            self._api_last_request[api_name] = time.time()
        else:
            # Global fallback
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


def _parse_csv_list(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    parts = [p.strip() for p in str(value).split(",")]
    parts = [p for p in parts if p]
    return parts or None


def _parse_path_list(values: Optional[List[str]]) -> Optional[List[Path]]:
    if not values:
        return None
    paths: List[Path] = []
    for value in values:
        for part in str(value).split(","):
            item = part.strip()
            if item:
                paths.append(Path(item))
    return paths or None


def _first_model_identity(model_context: Mapping[str, Any]) -> Dict[str, Any]:
    identity_resolution = model_context.get("identity_resolution") or {}
    if not isinstance(identity_resolution, Mapping):
        return {}
    for resolution in identity_resolution.get("chain_resolutions") or []:
        if not isinstance(resolution, Mapping):
            continue
        accepted = resolution.get("accepted_identity")
        if isinstance(accepted, Mapping) and accepted:
            return dict(accepted)
    return {}


def _first_model_gene(identity: Mapping[str, Any], fallback: Optional[str]) -> Optional[str]:
    if fallback:
        return str(fallback)
    for gene in identity.get("genes") or []:
        text = str(gene or "").strip()
        if text and re.fullmatch(r"[A-Za-z0-9_.-]+", text):
            return text
    entry_name = str(identity.get("uniprot_entry_name") or "").strip()
    if "_" in entry_name:
        return entry_name.split("_", 1)[0]
    return entry_name or None


def _compact_model_context_for_provenance(model_context: Mapping[str, Any]) -> Dict[str, Any]:
    asset = model_context.get("asset") or {}
    identity_resolution = model_context.get("identity_resolution") or {}
    physical_context = model_context.get("physical_context") or {}
    multimer = model_context.get("multimer") or {}

    chain_summaries = []
    for chain in model_context.get("chains") or []:
        if not isinstance(chain, Mapping):
            continue
        chain_summaries.append({
            "chain_id": chain.get("chain_id"),
            "sequence_length": chain.get("sequence_length"),
            "sequence_sha256": chain.get("sequence_sha256"),
            "residue_count": chain.get("residue_count"),
            "atom_count": chain.get("atom_count"),
            "sequence_source": chain.get("sequence_source"),
            "numbering_gaps": chain.get("numbering_gaps") or [],
        })

    resolved_chains = []
    for resolution in identity_resolution.get("chain_resolutions") or []:
        if not isinstance(resolution, Mapping):
            continue
        accepted = resolution.get("accepted_identity") or {}
        domain_context = resolution.get("domain_context") or {}
        resolved_chains.append({
            "chain_id": resolution.get("chain_id"),
            "status": resolution.get("status"),
            "identity_source": accepted.get("identity_source") if isinstance(accepted, Mapping) else None,
            "uniprot_accession": accepted.get("uniprot_accession") if isinstance(accepted, Mapping) else None,
            "isoform_accession": accepted.get("isoform_accession") if isinstance(accepted, Mapping) else None,
            "uniprot_entry_name": accepted.get("uniprot_entry_name") if isinstance(accepted, Mapping) else None,
            "confidence": accepted.get("confidence") if isinstance(accepted, Mapping) else None,
            "local_lmp_match_count": len(resolution.get("local_lmp_matches") or []),
            "domain_feature_count": domain_context.get("feature_count") if isinstance(domain_context, Mapping) else None,
        })

    contact_pairs = []
    for pair in physical_context.get("chain_pairs") or []:
        if not isinstance(pair, Mapping):
            continue
        heavy = pair.get("heavy_atom_contacts") or {}
        ca_contacts = pair.get("ca_contacts") or {}
        contact_pairs.append({
            "chain_a": pair.get("chain_a"),
            "chain_b": pair.get("chain_b"),
            "heavy_atom_contact_count": heavy.get("atom_contact_count") if isinstance(heavy, Mapping) else None,
            "heavy_unique_residue_pair_count": heavy.get("unique_residue_pair_count") if isinstance(heavy, Mapping) else None,
            "heavy_minimum_distance_angstrom": heavy.get("minimum_distance_angstrom") if isinstance(heavy, Mapping) else None,
            "ca_contact_count": ca_contacts.get("atom_contact_count") if isinstance(ca_contacts, Mapping) else None,
            "ca_unique_residue_pair_count": ca_contacts.get("unique_residue_pair_count") if isinstance(ca_contacts, Mapping) else None,
        })

    return {
        "context_schema": "mica.lmp.model_asset_context.compact.v1",
        "asset": {
            "asset_id": asset.get("asset_id") if isinstance(asset, Mapping) else None,
            "source_kind": asset.get("source_kind") if isinstance(asset, Mapping) else None,
            "structure_format": asset.get("structure_format") if isinstance(asset, Mapping) else None,
            "local_path": asset.get("local_path") if isinstance(asset, Mapping) else None,
            "sha256": asset.get("sha256") if isinstance(asset, Mapping) else None,
            "size_bytes": asset.get("size_bytes") if isinstance(asset, Mapping) else None,
            "chain_ids": asset.get("chain_ids") if isinstance(asset, Mapping) else [],
        },
        "chains": chain_summaries,
        "identity": {
            "status": identity_resolution.get("status") if isinstance(identity_resolution, Mapping) else None,
            "resolved_chain_count": identity_resolution.get("resolved_chain_count") if isinstance(identity_resolution, Mapping) else None,
            "chain_resolutions": resolved_chains,
        },
        "multimer": {
            "assembly_classification": multimer.get("assembly_classification") if isinstance(multimer, Mapping) else None,
            "unique_sequence_count": multimer.get("unique_sequence_count") if isinstance(multimer, Mapping) else None,
        },
        "physical_context": {
            "status": physical_context.get("status") if isinstance(physical_context, Mapping) else None,
            "analysis_kind": physical_context.get("analysis_kind") if isinstance(physical_context, Mapping) else None,
            "trajectory_executed": physical_context.get("trajectory_executed") if isinstance(physical_context, Mapping) else None,
            "chain_pairs": contact_pairs,
        },
        "smic_handoff": model_context.get("smic_handoff") or {},
        "warnings": model_context.get("warnings") or [],
    }


def _model_context_to_structure_receipt(
    model_context: Mapping[str, Any],
    *,
    primary_accession: str,
) -> Dict[str, Any]:
    asset = model_context.get("asset") or {}
    asset_id = str(asset.get("asset_id") or "structure_asset:local_model") if isinstance(asset, Mapping) else "structure_asset:local_model"
    local_path = str(asset.get("local_path") or "") if isinstance(asset, Mapping) else ""
    structure_format = str(asset.get("structure_format") or Path(local_path).suffix.lstrip(".") or "pdb")
    display_name = Path(local_path).name if local_path else asset_id
    accepted_identity = _first_model_identity(model_context)
    accession = str(accepted_identity.get("uniprot_accession") or primary_accession or "")
    isoform_accession = str(accepted_identity.get("isoform_accession") or "")

    chain_map: Dict[str, Dict[str, Any]] = {}
    coverage_segments: List[Dict[str, Any]] = []
    for chain in model_context.get("chains") or []:
        if not isinstance(chain, Mapping):
            continue
        chain_id = str(chain.get("chain_id") or "").strip()
        if not chain_id:
            continue
        sequence_length = int(chain.get("sequence_length") or len(str(chain.get("sequence") or "")) or 0)
        chain_map[chain_id] = {
            "source_chain_id": chain_id,
            "label_chain_id": chain_id,
            "entity_type": "protein",
            "sequence_length": sequence_length,
            "sequence_sha256": chain.get("sequence_sha256") or "",
            "sequence_source": chain.get("sequence_source") or "atom_records",
            "residue_count": chain.get("residue_count") or sequence_length,
            "atom_count": chain.get("atom_count") or 0,
        }
        if sequence_length > 0:
            coverage_segments.append({
                "start": 1,
                "end": sequence_length,
                "chain_id": chain_id,
                "auth_chain_id": chain_id,
                "label_chain_id": chain_id,
                "entity_id": chain_id,
                "entity_type": "protein",
                "structure_start": chain.get("first_residue_number") or 1,
                "structure_end": chain.get("last_residue_number") or sequence_length,
                "auth_start": chain.get("first_residue_number") or 1,
                "auth_end": chain.get("last_residue_number") or sequence_length,
                "identity": 1.0 if accession else "",
                "coverage": 1.0,
            })

    return {
        "schema": "mica.lmp.local_model_structure_receipt.v1",
        "source_kind": "imported_user_asset",
        "provider": "imported_user_asset",
        "provider_native_id": asset_id,
        "model_id": asset_id,
        "primary_structure_name": display_name,
        "structure_ref": asset_id,
        "structure_artifact_uri": local_path,
        "structure_format": structure_format,
        "canonical_accession": accession,
        "isoform_accession": isoform_accession,
        "chain_map": chain_map,
        "coverage_segments": coverage_segments,
        "model_asset_context": _compact_model_context_for_provenance(model_context),
    }


def _validate_lmp_v4_xml(xml_str: str, xsd_path: Path) -> Tuple[bool, str]:
    """Validate a generated v4 LMP XML string against an XSD."""
    try:
        from lxml import etree  # type: ignore
    except Exception as e:
        return False, f"lxml is required for XSD validation but is not available: {e}"

    try:
        schema_doc = etree.parse(str(xsd_path))
        schema = etree.XMLSchema(schema_doc)
        doc = etree.fromstring(xml_str.encode("utf-8"))
        ok = bool(schema.validate(doc))
        if ok:
            return True, "ok"
        # Return first error for quick debugging
        try:
            err = schema.error_log.last_error
            if err is not None:
                return False, f"{err.message} (line {err.line})"
        except Exception as _exc:
            self.logger.debug("Suppressed: %s", _exc)
        return False, "schema validation failed"
    except Exception as e:
        return False, str(e)


def _build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(description="Generate LMP v4 XML (optionally with TrajectoryIFP via SMIC).")
    p.add_argument("--uniprot", default=None, help="UniProt accession (e.g., P12931); optional when --pdb-model resolves identity")
    p.add_argument("--gene", default=None, help="Gene symbol/name; optional when --pdb-model resolves identity")
    p.add_argument("--organism", default="Homo sapiens", help="Organism name")
    p.add_argument("--preset", default=None, help="Preset name from src/bsm/lmp/presets.py")
    p.add_argument("--offline", action="store_true", help="Disable all network calls")
    p.add_argument("--config", default=None, help="Path to YAML config")
    p.add_argument("--cache-dir", default=None, help="Cache directory")
    p.add_argument("--states", default=None, help="Comma-separated states (optional; otherwise inferred)")
    p.add_argument("--isoforms", default=None, help="Comma-separated isoform accessions; omitted keeps UniProt auto-expansion")
    p.add_argument("--no-isoforms", action="store_true", help="Suppress UniProt isoform auto-expansion")
    p.add_argument("--pdb-ids", default=None, help="Comma-separated PDB IDs to include as context")
    p.add_argument("--pdb-model", default=None, help="Local PDB/mmCIF model path to scan and attach as imported structure context")
    p.add_argument("--local-lmp-root", action="append", default=None, help="Local LMP XML root for exact sequence identity matching; repeat or comma-separate")
    p.add_argument("--remote-sequence-search", action="store_true", help="Allow RCSB/UniProt remote sequence identity lookup for --pdb-model")
    p.add_argument("--remote-mmseqs2", action="store_true", help="Allow MMseqs2 serverless sequence identity search during --pdb-model lookup")
    p.add_argument("--remote-blast", action="store_true", help="Deprecated alias for --remote-mmseqs2 (Allow NCBI BLAST during --pdb-model identity lookup)")
    p.add_argument("--model-context-json", default=None, help="Optional path to write the imported model context JSON sidecar")

    # Trajectory IFP inputs (only needed if preset includes include_trajectory_ifp)
    p.add_argument("--pdb-ifp", default=None, help="PDB ID used for topology for TrajectoryIFP")
    p.add_argument("--traj", default=None, help="Trajectory file path (e.g., .dcd)")
    p.add_argument("--ligand", default=None, help="Ligand residue name (e.g., ATP)")
    p.add_argument("--require-ifp", action="store_true", help="Fail if TrajectoryIFP cannot be computed")

    p.add_argument("--out-dir", default=None, help="Output directory; if omitted prints to stdout")
    p.add_argument("--validate", action="store_true", help="Validate output against lmp_v4_schema.xsd")
    p.add_argument("--dynamic-stats-json", default=None, help="Optional JSON receipt for DynamicsStatistics")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    config_path = Path(args.config) if args.config else None

    gen = LMPGenerator(
        cache_dir=cache_dir,
        config_path=config_path,
        preset=args.preset,
        offline_mode=bool(args.offline),
    )

    states = _parse_csv_list(args.states)
    isoforms = _parse_csv_list(args.isoforms)
    pdb_ids = _parse_csv_list(args.pdb_ids)
    traj_path = Path(args.traj) if args.traj else None
    dynamic_statistics_receipt = None
    if args.dynamic_stats_json:
        dynamic_statistics_receipt = json.loads(Path(args.dynamic_stats_json).read_text(encoding="utf-8"))
    structure_generation_receipt = None
    model_context_data = None
    if args.pdb_model:
        from .structure_asset_context import detect_pdb_structure_context

        model_context = detect_pdb_structure_context(
            Path(args.pdb_model),
            local_lmp_roots=_parse_path_list(args.local_lmp_root),
            enable_remote_sequence_search=bool(args.remote_sequence_search),
            enable_remote_mmseqs2=bool(args.remote_mmseqs2 or args.remote_blast),
        )
        model_context_data = model_context.to_dict()
        accepted_identity = _first_model_identity(model_context_data)
        if not args.uniprot:
            args.uniprot = accepted_identity.get("uniprot_accession")
        args.gene = _first_model_gene(accepted_identity, args.gene)
        if not args.no_isoforms and args.isoforms is None:
            isoform_accession = str(accepted_identity.get("isoform_accession") or "").strip()
            isoforms = [isoform_accession] if isoform_accession else []
        if args.model_context_json:
            context_path = Path(args.model_context_json)
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text(json.dumps(model_context_data, indent=2), encoding="utf-8")

    if not args.uniprot:
        parser.error("--uniprot is required unless --pdb-model resolves a UniProt accession")
    if not args.gene:
        parser.error("--gene is required unless --pdb-model resolves a gene or entry name")
    if args.no_isoforms:
        isoforms = []
    if model_context_data is not None:
        structure_generation_receipt = _model_context_to_structure_receipt(
            model_context_data,
            primary_accession=str(args.uniprot),
        )

    docs = gen.generate_lmp_v4_multi_state(
        uniprot_id=str(args.uniprot),
        gene_name=str(args.gene),
        organism=str(args.organism),
        states=states,
        isoforms=isoforms,
        pdb_ids=pdb_ids,
        pdb_id_for_ifp=args.pdb_ifp,
        trajectory_path=traj_path,
        ligand_resname=args.ligand,
        require_ifp=bool(args.require_ifp),
        structure_generation_receipt=structure_generation_receipt,
        dynamic_statistics_receipt=dynamic_statistics_receipt,
    )

    xsd_path = Path(__file__).with_name("lmp_v4_schema.xsd")
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    ok_all = True
    for state, xml_str in docs.items():
        if args.validate:
            ok, msg = _validate_lmp_v4_xml(xml_str, xsd_path)
            if not ok:
                ok_all = False
                logging.getLogger(__name__).error("XSD validation failed for state '%s': %s", state, msg)

        if out_dir is not None:
            safe_state = re.sub(r"[^A-Za-z0-9._-]+", "_", state)
            out_path = out_dir / f"{args.uniprot}_{safe_state}.lmp_v4.xml"
            out_path.write_text(xml_str, encoding="utf-8")
        else:
            sys.stdout.write(xml_str)
            if not xml_str.endswith("\n"):
                sys.stdout.write("\n")

    return 0 if ok_all else 2


if __name__ == "__main__":
    raise SystemExit(main())

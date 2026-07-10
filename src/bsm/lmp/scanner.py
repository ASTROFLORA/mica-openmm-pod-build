"""
LMP Scanner (Orchestrator)
==========================

Módulo de orquestación para la generación masiva de datasets LMP.
Actúa como una capa superior al LMPGenerator, gestionando:
1. Discovery (Búsqueda en UniProt/PDB)
2. Batch Processing (Ejecución paralela)
3. Robustez (Checkpointing, Rate Limiting, Manifest)

Uso:
    scanner = LMPScanner()
    ids = scanner.scan_uniprot("family:kinase AND organism_id:9606")
    scanner.build_dataset(ids, output_dir="./datasets/human_kinome")
"""

import os
import json
import time
import random
import logging
import re
import threading
from urllib.parse import urlparse
from typing import List, Dict, Optional, Any, Tuple, Callable
import concurrent.futures
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

# Intentar importar tqdm para barra de progreso
try:
    from tqdm import tqdm
except ImportError:
    # Fallback simple si no está instalado
    def tqdm(iterable, **kwargs):
        return iterable

from .generator import LMPGenerator

# Configurar logger
logger = logging.getLogger("LMPScanner")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class SemanticQueryBuilder:
    """Translates semantic protein queries to UniProt query syntax + post-filters.
    
    Supports natural-language-style queries like:
    - "kinases with IDR regions" → UniProt query + AlphaFold pLDDT post-filter
    - "GPCRs with medical targets" → GPCR family query + disease annotation filter
    - "human phosphatases with known inhibitors" → keyword query + ligand check
    """

    # Pre-built semantic templates: keyword → (uniprot_query_fragment, post_filter_key)
    SEMANTIC_TEMPLATES = {
        # Protein families
        "kinase": ("(family:kinase OR keyword:KW-0418)", None),
        "kinases": ("(family:kinase OR keyword:KW-0418)", None),
        "gpcr": ("(keyword:KW-0297)", None),  # G-protein coupled receptor
        "gpcrs": ("(keyword:KW-0297)", None),
        "phosphatase": ("(keyword:KW-0904)", None),
        "phosphatases": ("(keyword:KW-0904)", None),
        "protease": ("(keyword:KW-0645)", None),
        "proteases": ("(keyword:KW-0645)", None),
        "nuclear receptor": ("(keyword:KW-0804)", None),
        "nuclear receptors": ("(keyword:KW-0804)", None),
        "ion channel": ("(keyword:KW-0407)", None),
        "ion channels": ("(keyword:KW-0407)", None),
        "transporter": ("(keyword:KW-0813)", None),
        "transporters": ("(keyword:KW-0813)", None),
        
        # Structural features
        "idr": (None, "disorder_filter"),
        "idr regions": (None, "disorder_filter"),
        "intrinsically disordered": (None, "disorder_filter"),
        "disordered regions": (None, "disorder_filter"),
        "idp": (None, "disorder_filter"),
        
        # Medical/disease
        "medical targets": ("(keyword:KW-0621 OR annotation:(type:disease))", None),  # Pharmaceutical
        "drug targets": ("(keyword:KW-0621)", None),
        "disease associated": ("(annotation:(type:disease))", None),
        "therapeutic targets": ("(keyword:KW-0621 OR annotation:(type:disease))", None),
        
        # Organism shortcuts  
        "human": ("(organism_id:9606)", None),
        "mouse": ("(organism_id:10090)", None),
        "rat": ("(organism_id:10116)", None),
        "yeast": ("(organism_id:559292)", None),
        "e. coli": ("(organism_id:83333)", None),
        
        # Quality filters
        "reviewed": ("(reviewed:true)", None),
        "swissprot": ("(reviewed:true)", None),
        
        # Functional annotations
        "known inhibitors": (None, "ligand_inhibitor_filter"),
        "with inhibitors": (None, "ligand_inhibitor_filter"),
        "allosteric": ("(annotation:(type:binding AND allosteric))", None),
        "membrane": ("(keyword:KW-0472)", None),
        "secreted": ("(keyword:KW-0964)", None),
    }

    # Post-filter registry
    POST_FILTERS = {
        "disorder_filter": {
            "description": "Filter proteins with significant intrinsically disordered regions (pLDDT < 50)",
            "requires": "alphafold",
            "threshold": 0.15,  # At least 15% of residues with pLDDT < 50
        },
        "ligand_inhibitor_filter": {
            "description": "Filter proteins with known inhibitor ligands in PDB structures",
            "requires": "pdb_check",
        },
    }

    @classmethod
    def parse(cls, semantic_query: str) -> dict:
        """Parse a semantic query into UniProt query parts and post-filters.
        
        Args:
            semantic_query: Natural language query like "kinases with IDR regions"
            
        Returns:
            dict with keys:
                - uniprot_query: str (combined UniProt query)
                - post_filters: list of post-filter keys to apply
                - unresolved: list of tokens that didn't match any template
        """
        query = semantic_query.lower().strip()
        
        # Split on common connectors
        import re as _re
        tokens = _re.split(r'\s+(?:with|and|that have|having|containing|y|con|que tienen)\s+', query)
        tokens = [t.strip() for t in tokens if t.strip()]
        
        uniprot_parts = []
        post_filters = []
        unresolved = []
        
        for token in tokens:
            matched = False
            # Try exact match first
            if token in cls.SEMANTIC_TEMPLATES:
                uq, pf = cls.SEMANTIC_TEMPLATES[token]
                if uq:
                    uniprot_parts.append(uq)
                if pf:
                    post_filters.append(pf)
                matched = True
            else:
                # Try substring match for multi-word templates
                for key, (uq, pf) in cls.SEMANTIC_TEMPLATES.items():
                    if key in token or token in key:
                        if uq:
                            uniprot_parts.append(uq)
                        if pf:
                            post_filters.append(pf)
                        matched = True
                        break
            
            if not matched:
                # Could be a raw UniProt query fragment
                unresolved.append(token)
        
        # Combine UniProt parts with AND
        combined_query = " AND ".join(uniprot_parts) if uniprot_parts else ""
        
        # If there are unresolved tokens, append them as-is (user knows UniProt syntax)
        if unresolved:
            extra = " AND ".join(f"({u})" for u in unresolved)
            combined_query = f"{combined_query} AND {extra}" if combined_query else extra
        
        return {
            "uniprot_query": combined_query,
            "post_filters": post_filters,
            "unresolved": unresolved,
        }


class LMPScanner:
    def __init__(
        self,
        config_path: str = "src/bsm/lmp/lmp_config.yaml",
        output_dir: str = "./output",
        *,
        preset: Optional[str] = None,
        request_min_interval_s: float = 0.2,
        request_min_interval_by_host: Optional[Dict[str, float]] = None,
        worker_jitter_s: Tuple[float, float] = (0.5, 2.0),
    ):
        """
        Inicializa el Scanner y el Generador subyacente.
        
        Args:
            config_path: Ruta al archivo de configuración YAML.
            output_dir: Directorio base para la salida de datasets.
            preset: Preset LMP v4 a usar al generar XML (por ejemplo, "full").
            request_min_interval_s: Intervalo mínimo (segundos) entre requests por host (compartido entre hilos).
            request_min_interval_by_host: Overrides por host (netloc), ej: {"rest.uniprot.org": 0.25}.
            worker_jitter_s: Rango (min,max) de sleep aleatorio por target en workers para comportamiento cortés.
        """
        self.preset = preset
        self.generator = LMPGenerator(config_path=Path(config_path), preset=preset)
        if preset and getattr(self.generator, "preset", None) is None:
            raise ValueError(f"Requested LMP preset is not available: {preset}")
        self.output_dir = output_dir

        self.request_min_interval_s = max(0.0, float(request_min_interval_s))
        self.request_min_interval_by_host = request_min_interval_by_host or {}
        self.worker_jitter_s = worker_jitter_s

        # Shared per-host throttling state across threads
        self._host_next_allowed_at: Dict[str, float] = {}
        self._host_throttle_lock = threading.Lock()

    def _host_min_interval(self, url: str) -> float:
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if host and host in self.request_min_interval_by_host:
            try:
                return max(0.0, float(self.request_min_interval_by_host[host]))
            except Exception:
                return self.request_min_interval_s
        return self.request_min_interval_s

    def _throttle_url(self, url: str, sleep_fn: Callable[[float], None]) -> None:
        """Shared per-host min-interval throttle (prevents bursts when max_workers>1)."""
        min_interval_s = self._host_min_interval(url)
        if min_interval_s <= 0:
            return

        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if not host:
            return

        now = time.time()
        wait_s = 0.0
        with self._host_throttle_lock:
            next_allowed = float(self._host_next_allowed_at.get(host, 0.0))
            if now < next_allowed:
                wait_s = next_allowed - now
                # Reserve the next slot after the current reservation.
                self._host_next_allowed_at[host] = next_allowed + min_interval_s
            else:
                self._host_next_allowed_at[host] = now + min_interval_s

        if wait_s > 0:
            sleep_fn(wait_s)

    def _safe_mkdir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def _safe_uid_stem(self, uid: str) -> str:
        """Return a filesystem-safe stem for generated files and checkpoint matching."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", uid or "")
        return safe.strip("._-") or "unknown"

    def _budo_stem_from_xml(self, xml_str: str) -> Optional[str]:
        """Extract a filesystem-safe BudoID stem from a generated LMP XML document.

        Returns the part after ``budo:`` (e.g. ``WNK1_HUMAN_v2``) normalized for
        filesystem use, or *None* if the element is absent or unparseable.
        """
        try:
            root = ET.fromstring(xml_str)
            for el in root.iter():
                tag = el.tag if isinstance(el.tag, str) else ""
                if tag == "BudoID" or tag.endswith("}BudoID"):
                    raw = (el.text or "").strip()
                    if raw:
                        stem = raw.split(":", 1)[-1] if ":" in raw else raw
                        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
                        return stem or None
        except Exception:
            pass
        return None

    def _now_epoch_ms(self) -> int:
        return int(time.time() * 1000)

    def _looks_like_pdb_id(self, value: str) -> bool:
        # PDB IDs are 4 chars and almost always start with a digit.
        return bool(re.fullmatch(r"[0-9][A-Za-z0-9]{3}", value or ""))

    def _request_with_backoff(
        self,
        method: str,
        url: str,
        *,
        max_retries: int = 3,
        base_sleep_s: float = 0.5,
        jitter_s: float = 0.25,
        timeout: int = 30,
        sleep_fn: Optional[Callable[[float], None]] = None,
        **kwargs,
    ) -> requests.Response:
        if sleep_fn is None:
            sleep_fn = time.sleep
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                self._throttle_url(url, sleep_fn)
                resp = requests.request(method, url, timeout=timeout, **kwargs)
                if resp.status_code in (429, 503):
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        retry_s = float(retry_after) if retry_after is not None else None
                    except Exception:
                        retry_s = None
                    wait_s = retry_s if retry_s is not None else (base_sleep_s * (2 ** attempt))
                    wait_s = wait_s + random.uniform(0, jitter_s)
                    logger.warning("HTTP %s for %s; backing off %.2fs (attempt %s/%s)", resp.status_code, url, wait_s, attempt + 1, max_retries)
                    sleep_fn(wait_s)
                    continue

                resp.raise_for_status()
                return resp
            except Exception as e:
                last_exc = e
                if attempt == max_retries - 1:
                    break
                wait_s = (base_sleep_s * (2 ** attempt)) + random.uniform(0, jitter_s)
                logger.warning("Request error for %s %s: %s; retrying in %.2fs (attempt %s/%s)", method, url, e, wait_s, attempt + 1, max_retries)
                sleep_fn(wait_s)

        raise last_exc if last_exc is not None else RuntimeError("request failed")
        
    def _setup_output(self, subdir: str) -> Tuple[str, str]:
        """Prepara directorios y manifest para un job específico."""
        target_dir = os.path.join(self.output_dir, subdir)
        self._safe_mkdir(target_dir)
        manifest_path = os.path.join(target_dir, "dataset_manifest.jsonl")
        return target_dir, manifest_path

    def _is_processed(self, uid: str, target_dir: str) -> bool:
        """Verify whether LMP files already exist for this UID.

        Checks both legacy accession-prefixed files (``{uid}_*.xml``) and the
        manifest JSONL for an OK record.  The manifest check supports the new
        budoID-named files written by the current naming scheme.
        """
        safe_uid = self._safe_uid_stem(uid)
        # 1. Legacy check: accession-prefixed files (pre-migration)
        try:
            for fname in os.listdir(target_dir):
                if fname.startswith(f"{safe_uid}_") and fname.endswith(".xml"):
                    return True
        except OSError:
            pass
        # 2. Manifest check: covers budoID-named files from new naming scheme
        manifest_path = os.path.join(target_dir, "dataset_manifest.jsonl")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as _mf:
                    for _line in _mf:
                        try:
                            rec = json.loads(_line.strip())
                            if rec.get("id") == uid and rec.get("status") == "OK":
                                return True
                        except Exception:
                            pass
            except OSError:
                pass
        return False

    def _save_to_manifest(self, manifest_path: str, record: Dict[str, Any]) -> None:
        """Escribe el resultado en el log maestro en tiempo real."""
        manifest_parent = os.path.dirname(manifest_path)
        if manifest_parent:
            self._safe_mkdir(manifest_parent)
        with open(manifest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _find_first_child_by_localname(self, parent: ET.Element, local_name: str) -> Optional[ET.Element]:
        for child in list(parent):
            tag = child.tag
            if isinstance(tag, str):
                if tag == local_name or tag.endswith("}" + local_name):
                    return child
        return None

    def _append_child_with_localname(self, parent: ET.Element, local_name: str) -> ET.Element:
        # Preserve namespace of parent if present
        tag = parent.tag
        if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
            ns = tag.split("}", 1)[0] + "}"
            return ET.SubElement(parent, f"{ns}{local_name}")
        return ET.SubElement(parent, local_name)

    def _inject_metadata(self, xml_string: str, metadata: Dict) -> str:
        """
        Inyecta metadatos del scanner en el XML generado.
        Añade un bloque <ScannerMetadata> dentro del root o como comentario si es más seguro.
        Aquí intentamos inyectarlo como elementos dentro de <Metadata> si existe, o al final.
        """
        try:
            root = ET.fromstring(xml_string)
            
            # Buscar o crear nodo Metadata
            meta_node = self._find_first_child_by_localname(root, "Metadata")
            if meta_node is None:
                meta_node = self._append_child_with_localname(root, "Metadata")
            
            # Crear nodo ScannerInfo
            scanner_node = self._append_child_with_localname(meta_node, "ScannerInfo")
            for k, v in (metadata or {}).items():
                elem = self._append_child_with_localname(scanner_node, "Tag")
                elem.set("key", str(k))
                elem.text = str(v)
                
            # Serializar de nuevo (sin pretty print complejo para no romper nada, o usando minidom si se prefiere)
            # Usamos la codificación por defecto de tostring (bytes) y decodificamos
            return ET.tostring(root, encoding="unicode")
        except Exception as e:
            logger.warning(f"No se pudo inyectar metadata en XML: {e}")
            return xml_string

    def _worker_wrapper(self, uid: str, target_dir: str, context_tags: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Envuelve al generador con manejo de errores, rate limiting y guardado.
        """
        # 1. Rate Limiting "Cortés" (Jitter)
        try:
            jmin, jmax = self.worker_jitter_s
        except Exception:
            jmin, jmax = (0.5, 2.0)
        time.sleep(random.uniform(float(jmin), float(jmax)))

        context_tags = context_tags or {}
        kind = "pdb" if self._looks_like_pdb_id(uid) else "uniprot"
        safe_uid = self._safe_uid_stem(uid)
        start_ms = self._now_epoch_ms()
        
        generated_files = []
        
        try:
            if kind == "pdb":
                xml_content = self.generator.generate_from_pdb(uid)
                if context_tags:
                    xml_content = self._inject_metadata(xml_content, context_tags)

                filename = f"{safe_uid}_pdb.xml"
                filepath = os.path.join(target_dir, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(xml_content)
                generated_files.append(filename)

                elapsed_ms = self._now_epoch_ms() - start_ms
                return {
                    "id": uid,
                    "kind": kind,
                    "status": "OK",
                    "files": generated_files,
                    "elapsed_ms": elapsed_ms,
                    **context_tags,
                }

            # UniProt mode — use v4 entry point; resolve gene_name from UniProt first
            uniprot_data = self.generator._fetch_uniprot(uid)
            gene_name = (
                uniprot_data.get("gene_name")
                or uniprot_data.get("gene")
                or (uniprot_data.get("genes") or [{}])[0].get("value", uid)
                or uid
            )
            organism = uniprot_data.get("organism", "Homo sapiens")
            docs = self.generator.generate_lmp_v4_multi_state(
                uniprot_id=uid,
                gene_name=gene_name,
                organism=organism,
            )
            if not docs:
                elapsed_ms = self._now_epoch_ms() - start_ms
                return {"id": uid, "kind": kind, "status": "SKIPPED", "reason": "No states generated", "elapsed_ms": elapsed_ms, **context_tags}

            # Extract canonical budo_stem from first doc for all filename writes
            _first_xml = next(iter(docs.values()), None)
            budo_stem = (self._budo_stem_from_xml(_first_xml) if _first_xml else None) or safe_uid

            for state_name, xml_content in docs.items():
                if context_tags:
                    xml_content = self._inject_metadata(xml_content, context_tags)

                filename = f"{budo_stem}_{state_name}.xml"
                filepath = os.path.join(target_dir, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(xml_content)
                generated_files.append(filename)

            elapsed_ms = self._now_epoch_ms() - start_ms
            return {
                "id": uid,
                "kind": kind,
                "status": "OK",
                "files": generated_files,
                "states": list(docs.keys()),
                "elapsed_ms": elapsed_ms,
                **context_tags,
            }
            
        except Exception as e:
            logger.error(f"Failed {uid}: {e}")
            elapsed_ms = self._now_epoch_ms() - start_ms
            return {"id": uid, "kind": kind, "status": "ERROR", "error": str(e), "elapsed_ms": elapsed_ms, **context_tags}

    def _parse_link_next(self, link_header: Optional[str]) -> Optional[str]:
        if not link_header:
            return None
        # Example: <https://.../search?...&cursor=XYZ>; rel="next"
        for part in link_header.split(","):
            part = part.strip()
            if "rel=\"next\"" not in part and "rel=next" not in part:
                continue
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
        return None

    def scan_uniprot(self, query: str, limit: int = 100) -> List[str]:
        """
        Busca en UniProtKB y retorna una lista de Accession IDs.
        
        Args:
            query: Query string estilo UniProt (ej: "family:kinase AND organism_id:9606")
            limit: Número máximo de resultados.
        """
        logger.info(f"Scanning UniProt for: '{query}' (limit={limit})")

        if limit <= 0:
            return []
        
        api_url = "https://rest.uniprot.org/uniprotkb/search"
        page_size = min(500, int(limit))
        first_params = {
            "query": query,
            "format": "json",
            "size": page_size,
            "fields": "accession",
        }

        ids: List[str] = []
        seen: set = set()
        next_url: Optional[str] = api_url

        try:
            while next_url and len(ids) < limit:
                if next_url == api_url:
                    resp = self._request_with_backoff("GET", next_url, params=first_params, timeout=30)
                else:
                    # UniProt provides a fully-qualified next URL via Link: rel="next".
                    resp = self._request_with_backoff("GET", next_url, timeout=30)

                data = resp.json() or {}
                for entry in data.get("results", []) or []:
                    acc = entry.get("primaryAccession")
                    if not acc or acc in seen:
                        continue
                    seen.add(acc)
                    ids.append(acc)
                    if len(ids) >= limit:
                        break

                next_url = self._parse_link_next(resp.headers.get("Link") or resp.headers.get("link"))

            logger.info(f"Found {len(ids)} IDs in UniProt.")
            return ids
        except Exception as e:
            logger.error(f"UniProt search failed: {e}")
            return []

    def build_dataset_from_uniprot_query(
        self,
        query: str,
        dataset_name: str,
        *,
        limit: int = 100,
        context_tags: Optional[Dict[str, Any]] = None,
        max_workers: int = 5,
        dry_run: bool = False,
    ):
        """Convenience API: UniProt query → IDs → build_dataset."""
        tags = dict(context_tags or {})
        tags.setdefault("source", "uniprot_query")
        tags.setdefault("uniprot_query", query)
        tags.setdefault("uniprot_limit", int(limit))

        ids = self.scan_uniprot(query, limit=limit)
        return self.build_dataset(
            target_ids=ids,
            dataset_name=dataset_name,
            context_tags=tags,
            max_workers=max_workers,
            dry_run=dry_run,
        )

    def scan_semantic(
        self,
        query: str,
        *,
        limit: int = 100,
        apply_post_filters: bool = True,
    ) -> Dict[str, Any]:
        """Scan using semantic natural-language queries.
        
        Examples:
            scanner.scan_semantic("kinases with IDR regions")
            scanner.scan_semantic("GPCRs with medical targets")
            scanner.scan_semantic("human phosphatases with known inhibitors")
            
        Args:
            query: Natural language query
            limit: Max results
            apply_post_filters: Whether to apply post-fetch filters (e.g., AlphaFold pLDDT)
            
        Returns:
            Dict with 'ids', 'query_info', and 'post_filter_results'
        """
        parsed = SemanticQueryBuilder.parse(query)
        logger.info(
            "Semantic query '%s' → UniProt: '%s', post_filters: %s",
            query, parsed["uniprot_query"], parsed["post_filters"],
        )
        
        if not parsed["uniprot_query"]:
            logger.warning("Semantic query produced empty UniProt query. Check input: %s", query)
            return {"ids": [], "query_info": parsed, "post_filter_results": {}}
        
        # Fetch from UniProt
        ids = self.scan_uniprot(parsed["uniprot_query"], limit=limit)
        
        result = {
            "ids": ids,
            "query_info": parsed,
            "post_filter_results": {},
        }
        
        # Apply post-filters if requested
        if apply_post_filters and parsed["post_filters"]:
            for pf_key in parsed["post_filters"]:
                pf_spec = SemanticQueryBuilder.POST_FILTERS.get(pf_key, {})
                
                if pf_key == "disorder_filter":
                    result["post_filter_results"]["disorder_filter"] = {
                        "status": "available",
                        "description": pf_spec.get("description", ""),
                        "note": "Apply AlphaFold pLDDT < 50 threshold on fetched proteins. "
                                "Use AlphaFoldClient.extract_plddt_from_pdb() per protein "
                                "and filter where disorder_fraction >= 0.15",
                        "threshold_disorder_fraction": pf_spec.get("threshold", 0.15),
                    }
                elif pf_key == "ligand_inhibitor_filter":
                    result["post_filter_results"]["ligand_inhibitor_filter"] = {
                        "status": "available",
                        "description": pf_spec.get("description", ""),
                        "note": "Cross-reference protein accessions with PDB ligand search "
                                "to identify those with known inhibitor co-crystals.",
                    }
                else:
                    result["post_filter_results"][pf_key] = {"status": "unknown_filter"}
        
        return result

    def scan_pdb_by_ligand(self, ligand_code: str, limit: int = 100) -> List[str]:
        """
        Busca estructuras en PDB que contengan un ligando específico.
        Retorna lista de PDB IDs.
        """
        logger.info(f"Scanning PDB for ligand: '{ligand_code}'")
        
        search_url = "https://search.rcsb.org/rcsbsearch/v2/query"
        
        query_json = {
            "query": {
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
                    "operator": "exact_match",
                    "value": ligand_code
                }
            },
            "return_type": "entry",
            "request_options": {
                "paginate": {
                    "start": 0,
                    "rows": limit
                }
            }
        }
        
        try:
            resp = self._request_with_backoff("POST", search_url, json=query_json, timeout=30)
            data = resp.json()
            ids = [entry.get("identifier") for entry in data.get("result_set", []) if entry.get("identifier")]
            logger.info(f"Found {len(ids)} structures with ligand {ligand_code}.")
            return ids
        except Exception as e:
            logger.error(f"PDB search failed: {e}")
            return []

    def build_dataset(self, target_ids: List[str], dataset_name: str, context_tags: Optional[Dict[str, Any]] = None, max_workers: int = 5, dry_run: bool = False):
        """
        Orquesta la generación masiva de datasets.
        
        Args:
            target_ids: Lista de IDs (UniProt o PDB) a procesar.
            dataset_name: Nombre del subdirectorio de salida (ej: "human_kinome").
            context_tags: Metadatos a inyectar (ej: {"source": "uniprot_query_kinase"}).
            max_workers: Número de hilos paralelos.
            dry_run: Si es True, solo simula el proceso.
        """
        if dry_run:
            logger.info(f"[DRY RUN] Se procesarían {len(target_ids)} IDs.")
            logger.info(f"[DRY RUN] Salida: {os.path.join(self.output_dir, dataset_name)}")
            logger.info(f"[DRY RUN] Tags: {context_tags or {}}")
            return None

        if not target_ids:
            logger.info("Nada que procesar: target_ids vacío.")
            return []

        target_dir, manifest_path = self._setup_output(dataset_name)

        context_tags = context_tags or {}

        # 1. Filtrar lo que ya existe (Checkpointing)
        # Nota: Esto asume que target_ids son UniProt IDs para generate_multi_state.
        # Si fueran PDB IDs para generate_from_pdb, la lógica de _is_processed debería adaptarse.
        # Por ahora asumimos flujo UniProt -> generate_multi_state.
        
        pending_ids = [uid for uid in target_ids if not self._is_processed(uid, target_dir)]
        skipped_count = len(target_ids) - len(pending_ids)
        
        logger.info(f"Procesando {len(pending_ids)} IDs ({skipped_count} ya existían).")
        
        if not pending_ids:
            logger.info("Nada nuevo que procesar.")
            return []

        results = []
        
        # 2. Ejecución Paralela con Barra de Progreso
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Enviar trabajos
            future_to_uid = {
                executor.submit(self._worker_wrapper, uid, target_dir, context_tags): uid 
                for uid in pending_ids
            }
            
            # Procesar a medida que terminan
            with tqdm(total=len(pending_ids), desc=f"Generando {dataset_name}") as pbar:
                for future in concurrent.futures.as_completed(future_to_uid):
                    res = future.result()
                    res.setdefault("dataset", dataset_name)
                    self._save_to_manifest(manifest_path, res) # Guardar en log maestro
                    results.append(res)
                    pbar.update(1)
        
        logger.info(f"Dataset '{dataset_name}' completado. Resultados en {manifest_path}")
        return results

"""LMP v4 Unified Generator.

This generator combines:
- v3's deterministic Ground-Truth approach (local JSON snapshots)
- v2's rich NeSy grammar encoding
- SMIC IFP integration for molecular dynamics trajectories
- Preset-based configuration for different consumers (PLM, LLM, MD)

Usage:
    from src.bsm.lmp.generator_unified import LMPUnifiedGenerator
    from src.bsm.lmp.presets import get_preset

    gen = LMPUnifiedGenerator(preset=get_preset("full"))
    xml_str = gen.generate(input_data)

Author: MICA Team
Version: 4.0
"""

from __future__ import annotations

import argparse
import gzip
import json
import base64
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import requests
import xml.etree.ElementTree as ET


# Type aliases
JsonDict = Dict[str, Any]

# Default namespace
LMP_V4_NS = "http://ai-university.edu/lmp/v4.0"


@dataclass
class LMPInput:
    """Input data container for LMP v4 generation.
    
    Attributes:
        accession: UniProt accession (e.g., P00533)
        entry: UniProtKB entry JSON dict (the ground truth)
        meta: Optional metadata from snapshot
        pdb_id: Optional PDB ID for structural data
        pdb_features: Optional PDB-derived features
        trajectory_ifp: Optional trajectory IFP data from SMIC
        embeddings: Optional dict of model->vector embeddings
        user_id: Optional user ID for GCS bucket routing
    """
    accession: str
    entry: JsonDict
    meta: Optional[JsonDict] = None
    pdb_id: Optional[str] = None
    pdb_features: Optional[List[JsonDict]] = None
    trajectory_ifp: Optional[JsonDict] = None
    embeddings: Optional[Dict[str, List[float]]] = None
    user_id: Optional[str] = None


@dataclass(frozen=True)
class LMPPreset:
    """Preset configuration for LMP generation.
    
    Imported from presets.py but duplicated here for type hints.
    """
    name: str
    description: str = ""
    include_identity: bool = True
    include_embeddings: bool = False
    include_nesy_grammar: bool = False
    include_semantics: bool = True
    include_geometry: bool = True
    include_sequence: bool = True
    include_features: bool = True
    include_trajectory_ifp: bool = False
    include_knowledge_graph: bool = True
    include_provenance: bool = True
    embed_ground_truth: bool = False
    validate: bool = True


class LMPUnifiedGenerator:
    """Unified LMP v4 Generator.
    
    Combines deterministic ground-truth inflation (v3) with rich NeSy
    grammar encoding (v2) and optional trajectory IFP integration.
    
    Key features:
    - Preset-based configuration for different consumers
    - Optional NeSy grammar generation (offline via nesy_offline_mapper)
    - Optional trajectory IFP embedding
    - Optional vector embeddings (ESM-2, ProtT5, etc.)
    - XSD validation
    """
    
    VERSION = "4.0"
    
    def __init__(
        self,
        *,
        preset: Optional[LMPPreset] = None,
        xsd_path: Optional[Path] = None,
        validate: Optional[bool] = None,
    ):
        """Initialize the unified generator.
        
        Args:
            preset: LMPPreset configuration (default: full preset)
            xsd_path: Path to XSD schema (default: lmp_v4_schema.xsd)
            validate: Override preset's validate setting
        """
        # Import preset if not provided
        if preset is None:
            from src.bsm.lmp.presets import get_preset
            preset = get_preset("full")
        
        self.preset = preset
        self.xsd_path = xsd_path or (Path(__file__).parent / "lmp_v4_schema.xsd")
        self.validate = validate if validate is not None else preset.validate
        
        # Lazy-load XSD schema
        self._xsd_schema = None
        
        # Lazy-load NeSy encoder
        self._nesy_encoder = None
        
    def _load_xsd_schema(self):
        """Load XSD schema for validation."""
        if self._xsd_schema is not None:
            return self._xsd_schema
            
        try:
            from lxml import etree as lxml_etree
        except ImportError:
            return None
        
        if not self.xsd_path.exists():
            return None
        
        with open(self.xsd_path, "rb") as f:
            xsd_doc = lxml_etree.parse(f)
        self._xsd_schema = lxml_etree.XMLSchema(xsd_doc)
        return self._xsd_schema

    # =========================================================================
    # Optional: Real data helpers (network + SMIC)
    # =========================================================================

    UNIPROT_ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{accession}?format=json"
    RCSB_PDB_DOWNLOAD_URLS = (
        "https://files.rcsb.org/download/{pdb_id}.pdb",
        "https://files.rcsb.org/view/{pdb_id}.pdb",
    )

    def _http_get_text(self, url: str, *, timeout_seconds: float = 30.0, max_retries: int = 3) -> str:
        last_exc: Optional[Exception] = None
        headers = {"User-Agent": "MICA-LMP/4.0 (requests)"}
        for attempt in range(max(1, int(max_retries))):
            try:
                resp = requests.get(url, timeout=timeout_seconds, headers=headers)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc or RuntimeError(f"HTTP GET failed: {url}")

    def _http_get_json(self, url: str, *, timeout_seconds: float = 30.0, max_retries: int = 3) -> JsonDict:
        last_exc: Optional[Exception] = None
        headers = {"User-Agent": "MICA-LMP/4.0 (requests)"}
        for attempt in range(max(1, int(max_retries))):
            try:
                resp = requests.get(url, timeout=timeout_seconds, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc or RuntimeError(f"HTTP GET failed: {url}")

    def _cache_root(self) -> Path:
        # Reuse the repo-local cache folder if present; otherwise default to cwd
        repo_cache = Path("lmp_cache")
        return repo_cache

    def _fetch_uniprot_entry(self, accession: str, *, cache_dir: Optional[Path] = None) -> Tuple[JsonDict, Optional[JsonDict]]:
        """Fetch UniProt JSON (ground truth) with on-disk caching.

        Returns:
            (entry_json, meta_json)
        """
        accession = accession.strip().upper()
        cache_dir = Path(cache_dir) if cache_dir is not None else self._cache_root() / "uniprot" / accession
        cache_dir.mkdir(parents=True, exist_ok=True)

        entry_path = cache_dir / "entry.json.gz"
        meta_path = cache_dir / "meta.json"

        if entry_path.exists():
            entry = self._read_gz_json(entry_path)
            meta = self._read_json(meta_path) if meta_path.exists() else None
            return entry, meta

        url = self.UNIPROT_ENTRY_URL.format(accession=accession)
        entry = self._http_get_json(url)

        meta: JsonDict = {
            "accession": accession,
            "source": "uniprot",
            "url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        self._write_gz_json(entry_path, entry)
        self._write_json(meta_path, meta)
        return entry, meta

    def _download_pdb(self, pdb_id: str, *, cache_dir: Optional[Path] = None) -> Path:
        pdb_id = pdb_id.strip().upper()
        cache_dir = Path(cache_dir) if cache_dir is not None else self._cache_root() / "pdb"
        cache_dir.mkdir(parents=True, exist_ok=True)

        out_path = cache_dir / f"{pdb_id}.pdb"
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path

        last_exc: Optional[Exception] = None
        for tmpl in self.RCSB_PDB_DOWNLOAD_URLS:
            url = tmpl.format(pdb_id=pdb_id)
            try:
                pdb_text = self._http_get_text(url)
                out_path.write_text(pdb_text, encoding="utf-8")
                return out_path
            except Exception as e:
                last_exc = e
                continue

        raise last_exc or RuntimeError(f"Failed to download PDB {pdb_id}")

    def _compute_trajectory_ifp_smic(
        self,
        *,
        topology_path: Path,
        ligand_resname: str,
        trajectory_path: Optional[Path] = None,
        stride: int = 1,
        max_frames: int = 500,
    ) -> JsonDict:
        """Compute TrajectoryIFP using SMIC IFPEngine (MDAnalysis required).

        This is a real pipeline (no mocks). If MDAnalysis/SMIC is unavailable,
        this will raise.
        """
        # Local import so the module remains importable without heavy deps
        from src.bsm.lmp.smic_bridge import SMIC_AVAILABLE, MDANALYSIS_AVAILABLE, IFPEngine

        if not SMIC_AVAILABLE:
            raise RuntimeError("SMIC IFPEngine is not available (workers/smic/python missing or not importable)")
        if not MDANALYSIS_AVAILABLE:
            raise RuntimeError("MDAnalysis is not available; install it to run SMIC IFP")

        if IFPEngine is None:
            raise RuntimeError("SMIC IFPEngine import failed")

        engine = IFPEngine()
        traj_in = trajectory_path if trajectory_path is not None else topology_path
        result = engine.generate_ifp(
            topology=str(topology_path),
            trajectory=str(traj_in),
            receptor_sel="protein",
            ligand_sel=f"resname {ligand_resname}",
            stride=max(1, int(stride)),
            verbose=False,
            low_memory=False,
        )

        # Convert IFPTrajectoryResult -> dict compatible with _add_trajectory_ifp
        frames: List[JsonDict] = []
        for frame_res in result.frame_results[: max(1, int(max_frames))]:
            contacts: List[JsonDict] = []
            active_types: List[str] = []
            for c in frame_res.contacts:
                rec = f"{c.receptor_resname}{c.receptor_resid}" if c.receptor_resname else str(c.receptor_resid)
                lig = f"{c.ligand_resname}{c.ligand_resid}" if c.ligand_resname else str(c.ligand_resid)
                contacts.append(
                    {
                        "type": str(c.ifp_type),
                        "receptor": rec,
                        "ligand": lig,
                        "distance": float(c.distance),
                    }
                )
                if str(c.ifp_type) and str(c.ifp_type) not in active_types:
                    active_types.append(str(c.ifp_type))
            frames.append(
                {
                    "index": int(frame_res.frame),
                    "time_ps": float(frame_res.time_ps),
                    "contacts": contacts,
                    "active_types": active_types,
                }
            )

        occupancy: Dict[str, float] = {}
        for (rec_resid, lig_resid, ifp_type), occ in (result.contact_occupancy or {}).items():
            key = f"{ifp_type}:{rec_resid}:{lig_resid}"
            occupancy[key] = float(occ)

        return {
            "receptor": topology_path.stem.upper(),
            "ligand": ligand_resname,
            "total_frames": int(result.n_frames),
            "stride": int(stride),
            "dt_ps": None,
            "topology": str(topology_path),
            "trajectory": str(traj_in),
            "frames": frames,
            "occupancy": occupancy,
        }
    
    def _validate_xml(self, xml_bytes: bytes) -> None:
        """Validate XML against XSD schema."""
        if not self.validate:
            return
            
        schema = self._load_xsd_schema()
        if schema is None:
            return
            
        from lxml import etree as lxml_etree
        doc = lxml_etree.fromstring(xml_bytes)
        schema.assertValid(doc)
    
    def _get_nesy_encoder(self):
        """Get or create NeSy encoder instance."""
        if self._nesy_encoder is None:
            from src.bsm.lmp.nesy_encoder import LMPNeSyEncoder
            self._nesy_encoder = LMPNeSyEncoder()
        return self._nesy_encoder
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def load_snapshot_dir(
        self, 
        snapshot_dir: Path, 
        *, 
        accession: Optional[str] = None
    ) -> LMPInput:
        """Load LMPInput from a snapshot directory.
        
        Args:
            snapshot_dir: Path to directory containing entry.json.gz and meta.json
            accession: Override accession (optional)
            
        Returns:
            LMPInput ready for generation
        """
        snapshot_dir = Path(snapshot_dir)
        entry_path = snapshot_dir / "entry.json.gz"
        meta_path = snapshot_dir / "meta.json"
        
        entry = self._read_gz_json(entry_path)
        meta = self._read_json(meta_path) if meta_path.exists() else None
        
        acc = accession or (meta or {}).get("accession") or entry.get("primaryAccession")
        if not acc:
            raise ValueError("Could not determine accession from snapshot")
        
        return LMPInput(accession=str(acc), entry=entry, meta=meta)
    
    def generate(self, input_data: LMPInput) -> str:
        """Generate LMP v4 XML from input data.
        
        Args:
            input_data: LMPInput containing all source data
            
        Returns:
            LMP v4 XML string
        """
        ET.register_namespace("", LMP_V4_NS)
        
        root = ET.Element(f"{{{LMP_V4_NS}}}LMP")
        root.set("version", self.VERSION)
        root.set("preset", self.preset.name)
        
        # Build sections based on preset
        if self.preset.include_identity:
            self._add_identity(root, input_data)
        
        if self.preset.include_embeddings and input_data.embeddings:
            self._add_embeddings(root, input_data)
        
        if self.preset.include_semantics:
            self._add_semantics(root, input_data)
        
        if self.preset.include_geometry:
            self._add_geometry(root, input_data)
        
        if self.preset.include_knowledge_graph:
            self._add_knowledge_graph(root, input_data)
        
        if self.preset.include_provenance:
            self._add_provenance(root, input_data)
        
        # Serialize and validate
        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        self._validate_xml(xml_bytes)
        
        return xml_bytes.decode("utf-8")
    def generate_to_gcs(
        self, 
        input_data: LMPInput, 
        user_id: Optional[str] = None
    ) -> str:
        """Generate LMP XML and upload to user's GCS bucket.
        
        Args:
            input_data: LMPInput containing all source data
            user_id: User ID for bucket routing (or from input_data)
            
        Returns:
            GCS URI where file was uploaded
        """
        user_id = user_id or input_data.user_id
        if not user_id:
            raise ValueError("user_id required for GCS upload")
        
        xml_str = self.generate(input_data)
        
        # Import storage manager
        from src.mica.infrastructure.storage.user_storage_manager import UserStorageManager
        
        manager = UserStorageManager(user_id=user_id)
        
        # Generate filename
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"lmp/{input_data.accession}_{self.preset.name}_{timestamp}.xml"
        
        # Upload
        gcs_uri = manager.upload_content(
            content=xml_str.encode("utf-8"),
            filename=filename,
            content_type="application/xml"
        )
        
        return gcs_uri
    
    # =========================================================================
    # Private: File I/O
    # =========================================================================
    
    def _read_json(self, path: Path) -> JsonDict:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"JSON is not an object: {path}")
        return data
    
    def _read_gz_json(self, path: Path) -> JsonDict:
        with gzip.open(path, "rb") as f:
            raw = f.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"GZ JSON is not an object: {path}")
        return data

    def _write_json(self, path: Path, data: JsonDict) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _write_gz_json(self, path: Path, data: JsonDict) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(data, ensure_ascii=False)
        with gzip.open(path, "wb") as f:
            f.write(raw.encode("utf-8"))
    
    # =========================================================================
    # Private: Identity Section
    # =========================================================================
    
    def _add_identity(self, root: ET.Element, input_data: LMPInput) -> None:
        ns = LMP_V4_NS
        entry = input_data.entry
        
        ident = ET.SubElement(root, f"{{{ns}}}Identity")
        
        # BudoID
        primary = entry.get("primaryAccession") or input_data.accession
        uniprot_id = entry.get("uniProtkbId")
        budo_id = self._make_budo_id(uniprot_id or primary)
        ET.SubElement(ident, f"{{{ns}}}BudoID").text = budo_id
        
        # Primary accession
        if primary:
            ET.SubElement(ident, f"{{{ns}}}PrimaryAccession").text = str(primary)
        
        # UniProtKB ID
        if uniprot_id:
            ET.SubElement(ident, f"{{{ns}}}UniProtKBId").text = str(uniprot_id)
        
        # Entry type
        if entry.get("entryType"):
            ET.SubElement(ident, f"{{{ns}}}EntryType").text = str(entry["entryType"])
        
        # Active
        if entry.get("active") is not None:
            ET.SubElement(ident, f"{{{ns}}}Active").text = "true" if entry["active"] else "false"
        
        # Protein existence
        if entry.get("proteinExistence"):
            ET.SubElement(ident, f"{{{ns}}}ProteinExistence").text = str(entry["proteinExistence"])
        
        # Organism
        organism = entry.get("organism")
        if isinstance(organism, dict):
            org_elem = ET.SubElement(ident, f"{{{ns}}}Organism")
            if organism.get("taxonId"):
                org_elem.set("id", str(organism["taxonId"]))
            org_elem.text = str(organism.get("scientificName") or organism.get("commonName") or "")
        
        # Lineages
        lineages = entry.get("lineages")
        if isinstance(lineages, list) and lineages:
            lin_elem = ET.SubElement(ident, f"{{{ns}}}Lineages")
            for lin in lineages:
                if isinstance(lin, dict):
                    name = lin.get("scientificName") or lin.get("commonName")
                    if name:
                        ET.SubElement(lin_elem, f"{{{ns}}}Lineage").text = str(name)
        
        # Secondary accessions
        secondary = entry.get("secondaryAccessions")
        if isinstance(secondary, list) and secondary:
            sec_elem = ET.SubElement(ident, f"{{{ns}}}SecondaryAccessions")
            for val in secondary:
                if val:
                    ET.SubElement(sec_elem, f"{{{ns}}}Value").text = str(val)
    
    def _make_budo_id(self, value: str) -> str:
        value = str(value).strip()
        if not value:
            return "budo:UNKNOWN-S"
        if value.startswith("budo:"):
            return value
        return f"budo:{value}-S"
    
    # =========================================================================
    # Private: Embeddings Section
    # =========================================================================
    
    def _add_embeddings(self, root: ET.Element, input_data: LMPInput) -> None:
        ns = LMP_V4_NS
        
        if not input_data.embeddings:
            return
        
        emb_elem = ET.SubElement(root, f"{{{ns}}}Embeddings")
        
        for model, vector in input_data.embeddings.items():
            e = ET.SubElement(emb_elem, f"{{{ns}}}Embedding")
            e.set("model", model)
            e.set("dimension", str(len(vector)))
            e.set("encoding", "base64")
            
            # Encode vector as base64
            import struct
            packed = struct.pack(f"{len(vector)}f", *vector)
            e.text = base64.b64encode(packed).decode("ascii")
    
    # =========================================================================
    # Private: Semantics Section (includes NeSy Grammar)
    # =========================================================================
    
    def _add_semantics(self, root: ET.Element, input_data: LMPInput) -> None:
        ns = LMP_V4_NS
        entry = input_data.entry
        
        sem = ET.SubElement(root, f"{{{ns}}}Semantics")
        
        # Protein name
        protein_name = self._extract_protein_name(entry.get("proteinDescription"))
        if protein_name:
            ET.SubElement(sem, f"{{{ns}}}ProteinName").text = protein_name
        
        # Genes
        genes = list(self._extract_gene_names(entry.get("genes")))
        if genes:
            genes_elem = ET.SubElement(sem, f"{{{ns}}}Genes")
            for g in genes:
                ET.SubElement(genes_elem, f"{{{ns}}}Value").text = g
        
        # Keywords
        keywords = list(self._extract_keyword_names(entry.get("keywords")))
        if keywords:
            kw_elem = ET.SubElement(sem, f"{{{ns}}}Keywords")
            for k in keywords:
                ET.SubElement(kw_elem, f"{{{ns}}}Value").text = k
        
        # NeSy Grammar (if enabled)
        if self.preset.include_nesy_grammar:
            nesy_grammar = self._generate_nesy_grammar(input_data)
            if nesy_grammar:
                nesy_elem = ET.SubElement(sem, f"{{{ns}}}NeSyGrammar")
                nesy_elem.set("version", "2.0")
                nesy_elem.set("length", str(len(input_data.entry.get("sequence", {}).get("value", ""))))
                nesy_elem.text = nesy_grammar
        
        # Comments (JSON-encoded)
        comments = entry.get("comments")
        if isinstance(comments, list):
            for c in comments:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("commentType")
                ce = ET.SubElement(sem, f"{{{ns}}}Comment")
                if ctype:
                    ce.set("type", str(ctype))
                ce.text = json.dumps(c, ensure_ascii=False)
    
    def _generate_nesy_grammar(self, input_data: LMPInput) -> Optional[str]:
        """Generate NeSy grammar from UniProt entry using offline mapper.
        
        Returns:
            NeSy grammar string or None if generation fails
        """
        try:
            # Import offline mapper and encoder
            from src.bsm.lmp.nesy_offline_mapper import map_uniprot_to_nesy
            
            # Map UniProt JSON to NeSyAnnotation
            annotation = map_uniprot_to_nesy(input_data.entry)
            
            # Encode using LMPNeSyEncoder
            encoder = self._get_nesy_encoder()
            nesy_str = encoder.encode(annotation)
            
            return nesy_str
            
        except Exception as e:
            # Log but don't fail generation
            import logging
            logging.warning(f"NeSy grammar generation failed: {e}")
            return None
    
    def _extract_protein_name(self, protein_desc: Any) -> Optional[str]:
        if not isinstance(protein_desc, dict):
            return None
        rec = protein_desc.get("recommendedName")
        if isinstance(rec, dict):
            full = rec.get("fullName")
            if isinstance(full, dict):
                val = full.get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
        subs = protein_desc.get("submissionNames")
        if isinstance(subs, list) and subs:
            first = subs[0]
            if isinstance(first, dict):
                full = first.get("fullName")
                if isinstance(full, dict):
                    val = full.get("value")
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        return None
    
    def _extract_gene_names(self, genes: Any) -> Iterable[str]:
        if not isinstance(genes, list):
            return
        for g in genes:
            if not isinstance(g, dict):
                continue
            gene_name = g.get("geneName")
            if isinstance(gene_name, dict):
                val = gene_name.get("value")
                if isinstance(val, str) and val.strip():
                    yield val.strip()
    
    def _extract_keyword_names(self, keywords: Any) -> Iterable[str]:
        if not isinstance(keywords, list):
            return
        for k in keywords:
            if not isinstance(k, dict):
                continue
            name = k.get("name")
            if isinstance(name, str) and name.strip():
                yield name.strip()
    
    # =========================================================================
    # Private: Geometry Section (Sequence, Features, TrajectoryIFP)
    # =========================================================================
    
    def _add_geometry(self, root: ET.Element, input_data: LMPInput) -> None:
        ns = LMP_V4_NS
        entry = input_data.entry
        
        geom = ET.SubElement(root, f"{{{ns}}}Geometry")
        
        # Sequence
        if self.preset.include_sequence:
            seq_data = entry.get("sequence")
            if isinstance(seq_data, dict):
                seq_elem = ET.SubElement(geom, f"{{{ns}}}Sequence")
                seq_elem.text = seq_data.get("value", "")
                if seq_data.get("length"):
                    seq_elem.set("length", str(seq_data["length"]))
                if seq_data.get("crc64"):
                    seq_elem.set("checksum", str(seq_data["crc64"]))
        
        # Features
        if self.preset.include_features:
            features = entry.get("features")
            if isinstance(features, list):
                for feat in features:
                    if not isinstance(feat, dict):
                        continue
                    self._add_feature(geom, feat)
        
        # PDB features (if provided)
        if input_data.pdb_features:
            for feat in input_data.pdb_features:
                self._add_feature(geom, feat)
        
        # Trajectory IFP (if enabled and provided)
        if self.preset.include_trajectory_ifp and input_data.trajectory_ifp:
            self._add_trajectory_ifp(geom, input_data.trajectory_ifp)
    
    def _add_feature(self, parent: ET.Element, feat: JsonDict) -> None:
        ns = LMP_V4_NS
        
        ftype = feat.get("type")
        if not ftype:
            return
        
        f = ET.SubElement(parent, f"{{{ns}}}Feature")
        f.set("type", str(ftype))
        
        # Location
        loc = feat.get("location")
        if isinstance(loc, dict):
            start = loc.get("start", {}).get("value")
            end = loc.get("end", {}).get("value")
            if start is not None:
                f.set("start", str(start))
            if end is not None:
                f.set("end", str(end))
        
        # Description
        desc = feat.get("description")
        if desc:
            f.set("description", str(desc))
            f.text = str(desc)
    
    def _add_trajectory_ifp(self, parent: ET.Element, ifp_data: JsonDict) -> None:
        """Add schema-compliant TrajectoryIFP element (LMP v4).

        Accepts legacy unified IFP dicts but emits only v4 XSD shape:
        TrajectoryIFP -> Frame* + Summary.
        """
        ns = LMP_V4_NS

        def _map_ifp_type(value: Any) -> str:
            raw = str(value or "").strip().upper()
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
                "H-BOND": "H-Bond",
                "HYDROPHOBIC": "Hydrophobic",
                "PI-STACKING": "Pi-Stacking",
                "PI-CATION": "Pi-Cation",
                "SALT-BRIDGE": "Salt-Bridge",
                "WATER-BRIDGE": "Water-Bridge",
                "HALOGEN-BOND": "Halogen-Bond",
                "METAL-COORDINATION": "Metal-Coordination",
            }
            return mapping.get(raw, "Hydrophobic")

        def _fingerprint_bits(mapped_types: Iterable[str]) -> str:
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
            present = set(mapped_types)
            return "".join("1" if t in present else "0" for t in order)

        def _safe_float(v: Any) -> Optional[float]:
            try:
                f = float(v)
                if f != f:
                    return None
                return f
            except Exception:
                return None

        def _clamp01(v: Any) -> Optional[float]:
            fv = _safe_float(v)
            if fv is None:
                return None
            if fv < 0.0:
                return 0.0
            if fv > 1.0:
                return 1.0
            return fv

        traj = ET.SubElement(parent, f"{{{ns}}}TrajectoryIFP")
        pdb_id = str(ifp_data.get("receptor") or ifp_data.get("pdb_id") or "UNK")
        ligand = str(ifp_data.get("ligand") or "UNK")
        frames = ifp_data.get("frames") if isinstance(ifp_data.get("frames"), list) else []

        total_frames_raw = ifp_data.get("total_frames")
        try:
            total_frames = int(total_frames_raw) if total_frames_raw is not None else len(frames)
        except Exception:
            total_frames = len(frames)
        total_frames = max(0, total_frames)

        stride_raw = ifp_data.get("stride", 1)
        try:
            stride = max(1, int(stride_raw))
        except Exception:
            stride = 1

        traj.set("pdb_id", pdb_id)
        traj.set("ligand", ligand)
        traj.set("total_frames", str(total_frames))
        traj.set("stride", str(stride))

        dt_ps = _safe_float(ifp_data.get("dt_ps"))
        if dt_ps is not None and dt_ps > 0.0:
            traj.set("time_step_ps", f"{dt_ps:.6f}")

        occupancy_raw = ifp_data.get("occupancy")
        occupancy_map: Dict[str, float] = {}
        if isinstance(occupancy_raw, dict):
            for key, val in occupancy_raw.items():
                occ = _clamp01(val)
                if occ is not None:
                    occupancy_map[str(key)] = occ

        total_interactions = 0

        for frame in frames[:100]:
            total_interactions += self._add_ifp_frame(
                traj,
                frame,
                occupancy_map=occupancy_map,
                map_ifp_type=_map_ifp_type,
                fingerprint_builder=_fingerprint_bits,
            )

        summary = ET.SubElement(traj, f"{{{ns}}}Summary")
        summary.set("total_interactions", str(int(total_interactions)))
        denom = total_frames if total_frames > 0 else max(1, len(frames[:100]))
        summary.set("average_interactions_per_frame", f"{(float(total_interactions) / float(denom)):.3f}")

        if occupancy_map:
            ranked = sorted(occupancy_map.items(), key=lambda kv: kv[1], reverse=True)
            for key, occ in ranked[:25]:
                parts = str(key).split(":")
                if len(parts) < 2:
                    continue
                key_elem = ET.SubElement(summary, f"{{{ns}}}KeyInteraction")
                key_elem.set("type", _map_ifp_type(parts[0]))
                key_elem.set("residue", str(parts[1] or "UNK"))
                key_elem.set("occupancy", f"{float(occ):.6f}")
    
    def _add_ifp_frame(
        self,
        parent: ET.Element,
        frame: JsonDict,
        *,
        occupancy_map: Dict[str, float],
        map_ifp_type,
        fingerprint_builder,
    ) -> int:
        ns = LMP_V4_NS

        f = ET.SubElement(parent, f"{{{ns}}}Frame")
        try:
            f.set("number", str(max(0, int(frame.get("index", 0)))))
        except Exception:
            f.set("number", "0")

        try:
            time_ps = float(frame.get("time_ps", 0.0) or 0.0)
            f.set("time_ns", f"{(time_ps / 1000.0):.3f}")
        except Exception:
            pass

        contacts = frame.get("contacts")
        emitted = 0
        mapped_types: List[str] = []

        if isinstance(contacts, list):
            for contact in contacts:
                c = ET.SubElement(f, f"{{{ns}}}Interaction")
                raw_type = str(contact.get("type", "") or "")
                mapped_type = map_ifp_type(raw_type)
                c.set("type", mapped_type)
                mapped_types.append(mapped_type)

                residue = str(contact.get("receptor", "") or "UNK")
                c.set("residue", residue)

                dist = None
                try:
                    dist = float(contact.get("distance", 0.0) or 0.0)
                    c.set("distance", f"{dist:.3f}")
                except Exception:
                    pass

                try:
                    angle = contact.get("angle")
                    if angle is not None:
                        c.set("angle", f"{float(angle):.2f}")
                except Exception:
                    pass

                key = f"{raw_type}:{residue}:{str(contact.get('ligand', '') or '')}"
                occ = occupancy_map.get(key)
                if occ is not None:
                    c.set("occupancy", f"{float(occ):.6f}")

                emitted += 1

        fp = ET.SubElement(f, f"{{{ns}}}Fingerprint")
        active_types = frame.get("active_types") if isinstance(frame.get("active_types"), list) else []
        if active_types:
            mapped = [map_ifp_type(t) for t in active_types]
            fp.text = fingerprint_builder(mapped)
        else:
            fp.text = fingerprint_builder(mapped_types)

        return emitted
    
    # =========================================================================
    # Private: KnowledgeGraph Section
    # =========================================================================
    
    def _add_knowledge_graph(self, root: ET.Element, input_data: LMPInput) -> None:
        ns = LMP_V4_NS
        entry = input_data.entry
        
        kg = ET.SubElement(root, f"{{{ns}}}KnowledgeGraph")
        
        # Cross-references
        xrefs = entry.get("uniProtKBCrossReferences")
        if isinstance(xrefs, list):
            for xref in xrefs:
                if not isinstance(xref, dict):
                    continue
                self._add_cross_reference(kg, xref)
        
        # References (citations)
        refs = entry.get("references")
        if isinstance(refs, list):
            for i, ref in enumerate(refs, 1):
                if not isinstance(ref, dict):
                    continue
                self._add_reference(kg, ref, i)
        
        # Edges (relationships derived from features)
        self._add_edges(kg, input_data)
    
    def _add_cross_reference(self, parent: ET.Element, xref: JsonDict) -> None:
        ns = LMP_V4_NS
        
        db = xref.get("database")
        xid = xref.get("id")
        if not db or not xid:
            return
        
        cr = ET.SubElement(parent, f"{{{ns}}}CrossReference")
        cr.set("db", str(db))
        cr.set("id", str(xid))
        
        if xref.get("isoformId"):
            cr.set("isoformId", str(xref["isoformId"]))
        
        # Properties
        props = xref.get("properties")
        if isinstance(props, list):
            for prop in props:
                if isinstance(prop, dict) and prop.get("key"):
                    p = ET.SubElement(cr, f"{{{ns}}}Property")
                    p.set("key", str(prop["key"]))
                    if prop.get("value"):
                        p.set("value", str(prop["value"]))
    
    def _add_reference(self, parent: ET.Element, ref: JsonDict, number: int) -> None:
        ns = LMP_V4_NS
        
        r = ET.SubElement(parent, f"{{{ns}}}Reference")
        r.set("number", str(number))
        
        citation = ref.get("citation")
        if isinstance(citation, dict):
            ctype = citation.get("type")
            title = citation.get("title")
            
            if title:
                c = ET.SubElement(r, f"{{{ns}}}Citation")
                if ctype:
                    c.set("type", str(ctype))
                c.text = str(title)
            
            # Citation cross-refs (PubMed, DOI)
            cxrefs = citation.get("citationCrossReferences")
            if isinstance(cxrefs, list):
                for cxref in cxrefs:
                    if isinstance(cxref, dict):
                        cx = ET.SubElement(r, f"{{{ns}}}CitationXref")
                        cx.set("key", str(cxref.get("database", "")))
                        if cxref.get("id"):
                            cx.set("value", str(cxref["id"]))
    
    def _add_edges(self, parent: ET.Element, input_data: LMPInput) -> None:
        """Add knowledge graph edges from features and xrefs."""
        ns = LMP_V4_NS
        entry = input_data.entry
        
        # PDB edges
        xrefs = entry.get("uniProtKBCrossReferences")
        if isinstance(xrefs, list):
            for xref in xrefs:
                if isinstance(xref, dict) and xref.get("database") == "PDB":
                    e = ET.SubElement(parent, f"{{{ns}}}Edge")
                    e.set("type", "HAS_STRUCTURE")
                    e.set("db", "PDB")
                    e.set("id", str(xref.get("id", "")))
        
        # GO edges
        if isinstance(xrefs, list):
            for xref in xrefs:
                if isinstance(xref, dict) and xref.get("database") == "GO":
                    e = ET.SubElement(parent, f"{{{ns}}}Edge")
                    e.set("type", "HAS_FUNCTION")
                    e.set("db", "GO")
                    e.set("id", str(xref.get("id", "")))
    
    # =========================================================================
    # Private: Provenance Section
    # =========================================================================
    
    def _add_provenance(self, root: ET.Element, input_data: LMPInput) -> None:
        ns = LMP_V4_NS
        entry = input_data.entry
        
        prov = ET.SubElement(root, f"{{{ns}}}Provenance")
        
        # Entry audit
        audit = entry.get("entryAudit")
        if isinstance(audit, dict):
            ea = ET.SubElement(prov, f"{{{ns}}}EntryAudit")
            
            if audit.get("sequenceVersion"):
                ET.SubElement(ea, f"{{{ns}}}SequenceVersion").text = str(audit["sequenceVersion"])
            if audit.get("entryVersion"):
                ET.SubElement(ea, f"{{{ns}}}EntryVersion").text = str(audit["entryVersion"])
            if audit.get("firstPublicDate"):
                ET.SubElement(ea, f"{{{ns}}}FirstPublicDate").text = str(audit["firstPublicDate"])
            if audit.get("lastAnnotationUpdateDate"):
                ET.SubElement(ea, f"{{{ns}}}LastAnnotationUpdateDate").text = str(audit["lastAnnotationUpdateDate"])
            if audit.get("lastSequenceUpdateDate"):
                ET.SubElement(ea, f"{{{ns}}}LastSequenceUpdateDate").text = str(audit["lastSequenceUpdateDate"])
        
        # Ground truth (optional)
        if self.preset.embed_ground_truth:
            gt = ET.SubElement(prov, f"{{{ns}}}GroundTruthEntry")
            gt.set("contentType", "application/json")
            gt.set("encoding", "base64")
            gt.text = base64.b64encode(
                json.dumps(input_data.entry, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            
            if input_data.meta:
                gtm = ET.SubElement(prov, f"{{{ns}}}GroundTruthMeta")
                gtm.set("contentType", "application/json")
                gtm.set("encoding", "base64")
                gtm.text = base64.b64encode(
                    json.dumps(input_data.meta, ensure_ascii=False).encode("utf-8")
                ).decode("ascii")
        
        # Generation info
        gen = ET.SubElement(prov, f"{{{ns}}}GenerationInfo")
        ET.SubElement(gen, f"{{{ns}}}Generator").text = "LMPUnifiedGenerator"
        ET.SubElement(gen, f"{{{ns}}}GeneratorVersion").text = self.VERSION
        ET.SubElement(gen, f"{{{ns}}}Preset").text = self.preset.name
        ET.SubElement(gen, f"{{{ns}}}Timestamp").text = datetime.now(timezone.utc).isoformat()


# =============================================================================
# Convenience functions
# =============================================================================

def generate_lmp_v4(
    entry: JsonDict,
    *,
    preset_name: str = "full",
    trajectory_ifp: Optional[JsonDict] = None,
    embeddings: Optional[Dict[str, List[float]]] = None,
) -> str:
    """Generate LMP v4 XML from a UniProt entry dict.
    
    Args:
        entry: UniProtKB entry JSON dict
        preset_name: Name of preset to use (default: "full")
        trajectory_ifp: Optional SMIC IFP data
        embeddings: Optional dict of model->vector embeddings
        
    Returns:
        LMP v4 XML string
    """
    from src.bsm.lmp.presets import get_preset
    
    preset = get_preset(preset_name)
    gen = LMPUnifiedGenerator(preset=preset)
    
    accession = entry.get("primaryAccession", "UNKNOWN")
    input_data = LMPInput(
        accession=accession,
        entry=entry,
        trajectory_ifp=trajectory_ifp,
        embeddings=embeddings,
    )
    
    return gen.generate(input_data)


# =============================================================================
# CLI
# =============================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate LMP v4 XML (unified generator)")
    p.add_argument("--accession", required=True, help="UniProt accession (e.g., P00519)")
    p.add_argument("--preset", default="full", help="Preset name (e.g., full, md-ifp, semantic)")
    p.add_argument("--all-presets", action="store_true", help="Generate for all presets")
    p.add_argument("--out", default="lmp_v4_out", help="Output directory")
    p.add_argument("--cache-dir", default=None, help="Optional cache directory root")

    p.add_argument("--pdb-id", default=None, help="PDB ID to download/use (e.g., 1IEP)")
    p.add_argument("--pdb-path", default=None, help="Path to a local PDB file (overrides --pdb-id)")
    p.add_argument("--trajectory", default=None, help="Trajectory path (.dcd/.xtc). If omitted, uses PDB as single-frame")
    p.add_argument("--ligand", default=None, help="Ligand residue name in the structure (e.g., STI)")

    p.add_argument("--require-ifp", action="store_true", help="Fail if preset needs IFP but IFP cannot be computed")
    p.add_argument("--ifp-stride", type=int, default=None, help="IFP stride override")
    p.add_argument("--max-ifp-frames", type=int, default=None, help="Max IFP frames to embed in XML")

    p.add_argument("--no-validate", action="store_true", help="Disable XSD validation")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    from src.bsm.lmp.presets import PRESET_REGISTRY, get_preset

    cache_root = Path(args.cache_dir) if args.cache_dir else None
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    preset_names = list(PRESET_REGISTRY.keys()) if args.all_presets else [args.preset]

    # Resolve PDB path if requested
    pdb_path: Optional[Path] = Path(args.pdb_path) if args.pdb_path else None
    if pdb_path is None and args.pdb_id:
        tmp_gen = LMPUnifiedGenerator(preset=get_preset("full"), validate=False)
        pdb_path = tmp_gen._download_pdb(args.pdb_id, cache_dir=(cache_root / "pdb") if cache_root else None)

    traj_path: Optional[Path] = Path(args.trajectory) if args.trajectory else None

    # Fetch ground truth UniProt entry
    tmp_gen = LMPUnifiedGenerator(preset=get_preset("full"), validate=False)
    entry, meta = tmp_gen._fetch_uniprot_entry(
        args.accession,
        cache_dir=(cache_root / "uniprot" / str(args.accession).upper()) if cache_root else None,
    )

    for preset_name in preset_names:
        preset = get_preset(preset_name)
        validate = False if args.no_validate else preset.validate
        gen = LMPUnifiedGenerator(preset=preset, validate=validate)

        # Optional: compute IFP if needed
        trajectory_ifp = None
        if preset.include_trajectory_ifp:
            if pdb_path is None or not args.ligand:
                if args.require_ifp:
                    raise SystemExit("Preset requires IFP, but --pdb-id/--pdb-path and --ligand were not provided")
            else:
                stride = args.ifp_stride if args.ifp_stride is not None else preset.ifp_stride
                max_frames = args.max_ifp_frames if args.max_ifp_frames is not None else preset.max_ifp_frames
                try:
                    trajectory_ifp = gen._compute_trajectory_ifp_smic(
                        topology_path=pdb_path,
                        ligand_resname=str(args.ligand),
                        trajectory_path=traj_path,
                        stride=stride,
                        max_frames=max_frames,
                    )
                except Exception as e:
                    if args.require_ifp:
                        raise
                    print(f"[warn] IFP failed for preset={preset_name}: {e}")
                    trajectory_ifp = None

        input_data = LMPInput(
            accession=str(args.accession).upper(),
            entry=entry,
            meta=meta,
            pdb_id=str(args.pdb_id).upper() if args.pdb_id else None,
            pdb_features=None,
            trajectory_ifp=trajectory_ifp,
            embeddings=None,
            user_id=None,
        )

        xml_str = gen.generate(input_data)
        out_path = out_dir / f"{str(args.accession).upper()}_{preset_name}.xml"
        out_path.write_text(xml_str, encoding="utf-8")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

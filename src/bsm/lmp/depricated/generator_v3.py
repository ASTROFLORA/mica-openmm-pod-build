"""LMP v3 Generator (Ground-Truth → XML).

This generator inflates a compact, reproducible LMP v3 XML document from a
locally stored UniProtKBEntry JSON snapshot ("ground truth").

Design goals:
- Deterministic output from local evidence.
- Preserve provenance and references/xrefs first (see LMP_V3_MASSIVE_EXPANSION_PLAN.md).
- Keep the schema intentionally permissive for forward expansion.

Note: v2 and v3 are intentionally decoupled; v3 does not inherit v2 schema.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import xml.etree.ElementTree as ET


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class LmpV3Snapshot:
    accession: str
    entry: JsonDict
    meta: Optional[JsonDict]


class LmpV3Generator:
    def __init__(
        self,
        *,
        xsd_path: Optional[Path] = None,
        validate: bool = True,
        embed_ground_truth_json: bool = False,
    ):
        self.xsd_path = xsd_path or (Path(__file__).parent / "lmp_v3_schema.xsd")
        self.validate = bool(validate)
        self.embed_ground_truth_json = bool(embed_ground_truth_json)

        self._xsd_schema = None
        if self.validate:
            self._xsd_schema = self._load_xsd_schema()

    def _load_xsd_schema(self):
        try:
            from lxml import etree as lxml_etree
        except Exception:
            return None

        if not self.xsd_path.exists():
            return None

        with open(self.xsd_path, "rb") as f:
            xsd_doc = lxml_etree.parse(f)
        return lxml_etree.XMLSchema(xsd_doc)

    def _validate_xml(self, xml_bytes: bytes) -> None:
        if not self.validate or self._xsd_schema is None:
            return
        from lxml import etree as lxml_etree

        doc = lxml_etree.fromstring(xml_bytes)
        self._xsd_schema.assertValid(doc)

    def load_snapshot_dir(self, snapshot_dir: Path, *, accession: Optional[str] = None) -> LmpV3Snapshot:
        snapshot_dir = Path(snapshot_dir)
        entry_path = snapshot_dir / "entry.json.gz"
        meta_path = snapshot_dir / "meta.json"

        entry = self._read_gz_json(entry_path)
        meta = self._read_json(meta_path) if meta_path.exists() else None

        acc = accession or (meta or {}).get("accession") or entry.get("primaryAccession")
        if not acc:
            raise ValueError("Could not determine accession from snapshot")

        return LmpV3Snapshot(accession=str(acc), entry=entry, meta=meta)

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

    def generate_xml(self, snapshot: LmpV3Snapshot) -> str:
        ns = "http://ai-university.edu/lmp/v3.0"
        ET.register_namespace("", ns)

        root = ET.Element(f"{{{ns}}}LMP")
        root.set("version", "3.0")

        self._add_identity(root, snapshot)
        self._add_semantics(root, snapshot)
        self._add_geometry(root, snapshot)
        self._add_knowledge_graph(root, snapshot)
        self._add_provenance(root, snapshot)

        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        self._validate_xml(xml_bytes)
        return xml_bytes.decode("utf-8")

    def _add_identity(self, root: ET.Element, snapshot: LmpV3Snapshot) -> None:
        ns = "http://ai-university.edu/lmp/v3.0"
        entry = snapshot.entry

        ident = ET.SubElement(root, f"{{{ns}}}Identity")

        primary = entry.get("primaryAccession") or snapshot.accession
        uniprot_id = entry.get("uniProtkbId")

        budo_id = self._default_budo_id(uniprot_id or primary)

        ET.SubElement(ident, f"{{{ns}}}BudoID").text = budo_id
        if primary:
            ET.SubElement(ident, f"{{{ns}}}PrimaryAccession").text = str(primary)
        if uniprot_id:
            ET.SubElement(ident, f"{{{ns}}}UniProtKBId").text = str(uniprot_id)

        if entry.get("entryType") is not None:
            ET.SubElement(ident, f"{{{ns}}}EntryType").text = str(entry.get("entryType"))
        if entry.get("active") is not None:
            ET.SubElement(ident, f"{{{ns}}}Active").text = "true" if bool(entry.get("active")) else "false"
        if entry.get("proteinExistence") is not None:
            ET.SubElement(ident, f"{{{ns}}}ProteinExistence").text = str(entry.get("proteinExistence"))

        organism = entry.get("organism")
        if isinstance(organism, dict):
            o = ET.SubElement(ident, f"{{{ns}}}Organism")
            if organism.get("taxonId") is not None:
                o.set("id", str(organism.get("taxonId")))
            o.text = str(organism.get("scientificName") or organism.get("commonName") or "")

        lineages = entry.get("lineages")
        if isinstance(lineages, list) and lineages:
            ls = ET.SubElement(ident, f"{{{ns}}}Lineages")
            for lin in lineages:
                if isinstance(lin, dict):
                    name = lin.get("scientificName") or lin.get("commonName")
                    if name:
                        ET.SubElement(ls, f"{{{ns}}}Lineage").text = str(name)

        secondary = entry.get("secondaryAccessions")
        if isinstance(secondary, list) and secondary:
            sa = ET.SubElement(ident, f"{{{ns}}}SecondaryAccessions")
            for val in secondary:
                if val:
                    ET.SubElement(sa, f"{{{ns}}}Value").text = str(val)

    def _default_budo_id(self, value: str) -> str:
        value = str(value).strip()
        if not value:
            return "budo:UNKNOWN-S"
        # Convention: budo:{UNIPROT_ID}-S (state-aware suffix)
        if value.startswith("budo:"):
            return value
        return f"budo:{value}-S"

    def _add_semantics(self, root: ET.Element, snapshot: LmpV3Snapshot) -> None:
        ns = "http://ai-university.edu/lmp/v3.0"
        entry = snapshot.entry

        sem = ET.SubElement(root, f"{{{ns}}}Semantics")

        protein_desc = entry.get("proteinDescription")
        protein_name = self._extract_protein_name(protein_desc)
        if protein_name:
            ET.SubElement(sem, f"{{{ns}}}ProteinName").text = protein_name

        genes = entry.get("genes")
        gene_values = list(self._extract_gene_names(genes))
        if gene_values:
            gs = ET.SubElement(sem, f"{{{ns}}}Genes")
            for g in gene_values:
                ET.SubElement(gs, f"{{{ns}}}Value").text = g

        keywords = entry.get("keywords")
        kw_values = list(self._extract_keyword_names(keywords))
        if kw_values:
            ks = ET.SubElement(sem, f"{{{ns}}}Keywords")
            for k in kw_values:
                ET.SubElement(ks, f"{{{ns}}}Value").text = k

        comments = entry.get("comments")
        if isinstance(comments, list):
            for c in comments:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("commentType")
                text = json.dumps(c, ensure_ascii=False)
                ce = ET.SubElement(sem, f"{{{ns}}}Comment")
                if ctype:
                    ce.set("type", str(ctype))
                ce.text = text

    def _extract_protein_name(self, protein_desc: Any) -> Optional[str]:
        if not isinstance(protein_desc, dict):
            return None
        # Prefer recommendedName.fullName.value
        rec = protein_desc.get("recommendedName")
        if isinstance(rec, dict):
            full = rec.get("fullName")
            if isinstance(full, dict):
                val = full.get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
        # Fallback: submissionNames[0].fullName.value
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
            return []
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
            return []
        for k in keywords:
            if not isinstance(k, dict):
                continue
            name = k.get("name")
            if isinstance(name, str) and name.strip():
                yield name.strip()

    def _add_geometry(self, root: ET.Element, snapshot: LmpV3Snapshot) -> None:
        ns = "http://ai-university.edu/lmp/v3.0"
        entry = snapshot.entry

        geom = ET.SubElement(root, f"{{{ns}}}Geometry")

        seq = entry.get("sequence")
        if isinstance(seq, dict):
            value = seq.get("value")
            if isinstance(value, str) and value:
                se = ET.SubElement(geom, f"{{{ns}}}Sequence")
                se.text = value
                if seq.get("length") is not None:
                    try:
                        se.set("length", str(int(seq.get("length"))))
                    except Exception:
                        pass

        features = entry.get("features")
        if isinstance(features, list):
            for feat in features:
                if not isinstance(feat, dict):
                    continue
                ftype = feat.get("type")
                if not ftype:
                    continue
                fe = ET.SubElement(geom, f"{{{ns}}}Feature")
                fe.set("type", str(ftype))

                loc = feat.get("location")
                if isinstance(loc, dict):
                    start = (loc.get("start") or {}).get("value") if isinstance(loc.get("start"), dict) else None
                    end = (loc.get("end") or {}).get("value") if isinstance(loc.get("end"), dict) else None
                    if start is not None:
                        try:
                            fe.set("start", str(int(start)))
                        except Exception:
                            pass
                    if end is not None:
                        try:
                            fe.set("end", str(int(end)))
                        except Exception:
                            pass

                desc = feat.get("description")
                if isinstance(desc, str) and desc.strip():
                    fe.set("description", desc.strip())
                # Store full feature JSON as text for lossless round-trip.
                fe.text = json.dumps(feat, ensure_ascii=False)

    def _add_knowledge_graph(self, root: ET.Element, snapshot: LmpV3Snapshot) -> None:
        ns = "http://ai-university.edu/lmp/v3.0"
        entry = snapshot.entry

        kg = ET.SubElement(root, f"{{{ns}}}KnowledgeGraph")

        xrefs = entry.get("uniProtKBCrossReferences")
        if isinstance(xrefs, list):
            for x in xrefs:
                if not isinstance(x, dict):
                    continue
                db = x.get("database")
                xid = x.get("id")
                if not db or not xid:
                    continue
                xe = ET.SubElement(kg, f"{{{ns}}}CrossReference")
                xe.set("db", str(db))
                xe.set("id", str(xid))
                if x.get("isoformId"):
                    xe.set("isoformId", str(x.get("isoformId")))

                props = x.get("properties")
                if isinstance(props, list):
                    for p in props:
                        if not isinstance(p, dict):
                            continue
                        key = p.get("key")
                        val = p.get("value")
                        if not key:
                            continue
                        pe = ET.SubElement(xe, f"{{{ns}}}Property")
                        pe.set("key", str(key))
                        if val is not None:
                            pe.set("value", str(val))

        refs = entry.get("references")
        if isinstance(refs, list):
            for r in refs:
                if not isinstance(r, dict):
                    continue
                re = ET.SubElement(kg, f"{{{ns}}}Reference")
                if r.get("referenceNumber") is not None:
                    try:
                        re.set("number", str(int(r.get("referenceNumber"))))
                    except Exception:
                        pass

                citation = r.get("citation")
                if isinstance(citation, dict):
                    title = citation.get("title")
                    if isinstance(title, str) and title.strip():
                        ET.SubElement(re, f"{{{ns}}}Citation").text = title.strip()

                    cx = citation.get("citationCrossReferences")
                    if isinstance(cx, list):
                        for cxi in cx:
                            if not isinstance(cxi, dict):
                                continue
                            db = cxi.get("database")
                            cid = cxi.get("id")
                            if not db or not cid:
                                continue
                            xe = ET.SubElement(re, f"{{{ns}}}CitationXref")
                            xe.set("key", str(db))
                            xe.set("value", str(cid))

    def _add_provenance(self, root: ET.Element, snapshot: LmpV3Snapshot) -> None:
        ns = "http://ai-university.edu/lmp/v3.0"
        entry = snapshot.entry

        prov = ET.SubElement(root, f"{{{ns}}}Provenance")

        audit = entry.get("entryAudit")
        if isinstance(audit, dict) and audit:
            ea = ET.SubElement(prov, f"{{{ns}}}EntryAudit")
            if audit.get("sequenceVersion") is not None:
                ET.SubElement(ea, f"{{{ns}}}SequenceVersion").text = str(audit.get("sequenceVersion"))
            if audit.get("entryVersion") is not None:
                ET.SubElement(ea, f"{{{ns}}}EntryVersion").text = str(audit.get("entryVersion"))
            if audit.get("firstPublicDate") is not None:
                ET.SubElement(ea, f"{{{ns}}}FirstPublicDate").text = str(audit.get("firstPublicDate"))
            if audit.get("lastAnnotationUpdateDate") is not None:
                ET.SubElement(ea, f"{{{ns}}}LastAnnotationUpdateDate").text = str(audit.get("lastAnnotationUpdateDate"))
            if audit.get("lastSequenceUpdateDate") is not None:
                ET.SubElement(ea, f"{{{ns}}}LastSequenceUpdateDate").text = str(audit.get("lastSequenceUpdateDate"))

        if self.embed_ground_truth_json:
            ge = ET.SubElement(prov, f"{{{ns}}}GroundTruthEntry")
            ge.set("contentType", "application/json")
            ge.set("encoding", "utf-8")
            ge.text = json.dumps(entry, ensure_ascii=False, sort_keys=True)

            if snapshot.meta is not None:
                gm = ET.SubElement(prov, f"{{{ns}}}GroundTruthMeta")
                gm.set("contentType", "application/json")
                gm.set("encoding", "utf-8")
                gm.text = json.dumps(snapshot.meta, ensure_ascii=False, sort_keys=True)

        internal = entry.get("internalSection")
        if isinstance(internal, dict):
            internal_lines = internal.get("internalLines")
            if isinstance(internal_lines, list):
                for line in internal_lines:
                    if not isinstance(line, dict):
                        continue
                    t = line.get("type")
                    v = line.get("value")
                    if v is None:
                        continue
                    il = ET.SubElement(prov, f"{{{ns}}}InternalLine")
                    if t:
                        il.set("type", str(t))
                    il.text = str(v)


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

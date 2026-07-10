from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

# Public LMP v4 scanner (GCS-backed fallback when .tmp_lmp_v4 is empty).
# See src/mica/storage/lmp_v4_public_scanner.py for ownership notes (fence F5).
from ...storage import lmp_v4_public_scanner as _lmp_v4_scanner


router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


def _repo_root() -> Path:
    # src/mica/api_v1/routers/graph.py -> repo root (parents[4])
    return Path(__file__).resolve().parents[4]


def _lmp_v4_dir() -> Path:
    # Configurable for deployments; default matches the repo convention.
    # Note: keep this read-only (no writes) to avoid security foot-guns.
    import os

    override = os.getenv("MICA_LMP_V4_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (_repo_root() / ".tmp_lmp_v4").resolve()


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="file is required")
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid file name")
    if name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid file name")
    if not name.lower().endswith(".xml"):
        raise HTTPException(status_code=400, detail="file must end with .xml")
    return name


def _resolve_xml_path(filename: str) -> Path:
    base = _lmp_v4_dir()
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=500, detail=f"LMP v4 dir not found: {base}")

    safe = _safe_filename(filename)
    path = (base / safe).resolve()
    # Prevent path traversal
    try:
        path.relative_to(base)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {safe}")
    return path


def _resolve_xml_bytes(filename: str) -> Tuple[bytes, str]:
    """Return ``(xml_bytes, source)`` for an LMP v4 file.

    Tries the local ``.tmp_lmp_v4`` dir first, then falls back to the public
    GCS bucket via :mod:`mica.storage.lmp_v4_public_scanner`. Raises 404 if
    neither substrate has the file, 400 if the filename is unsafe.
    """
    safe = _safe_filename(filename)
    base = _lmp_v4_dir()

    # Local first
    if base.exists() and base.is_dir():
        try:
            candidate = (base / safe).resolve()
            candidate.relative_to(base)
            if candidate.is_file():
                return (candidate.read_bytes(), "local")
        except Exception:
            # Path traversal or stat failure — fall through to GCS.
            pass

    data = _lmp_v4_scanner.load_xml_bytes(safe, local_dir=None)
    if data is not None:
        return (data, "gcs")

    raise HTTPException(status_code=404, detail=f"File not found: {safe}")


def _xml_namespace(tag: str) -> str:
    # "{namespace}LocalName" or "LocalName"
    if tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return ""


def _make_tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}" if ns else local


def _split_node_id(node_id: str) -> Tuple[str, str]:
    node_id = (node_id or "").strip()
    if ":" in node_id:
        prefix, rest = node_id.split(":", 1)
        return prefix, rest
    return "", node_id


def _node_kind(node_id: str) -> str:
    prefix, _ = _split_node_id(node_id)
    p = prefix.lower()
    if p == "budo":
        return "protein"
    if p in {"pdb", "pdbsum", "alphafolddb", "smr", "bmrb", "sasbdb"}:
        return "structure"
    if p in {"go", "pan-go"}:
        return "go"
    if p in {"reactome", "pathwaycommons", "signalink", "signor", "biocyc"}:
        return "pathway"
    if p in {"chembl", "kegg", "drugbank", "drugcentral", "guidetopharmacology", "pharmgkb", "pharos"}:
        return "pharmacology"
    if p in {
        "interpro", "pfam", "smart", "prosite", "supfam", "cdd",
        "gene3d", "funfam", "panther", "elm", "cd-code",
    }:
        return "domain"
    if p in {"embl", "refseq", "ccds", "ensembl", "mane-select", "ucsc"}:
        return "sequence"
    if p in {"biogrid", "intact", "dip", "mint", "string", "corum", "funcoup"}:
        return "interaction"
    if p in {"disgenet", "malacards", "mim", "orphanet", "opentargets", "clinpgx"}:
        return "disease"
    if p in {"bgee", "hpa", "expressionatlas", "proteomicsdb", "paxdb", "cptac", "massivе"}:
        return "expression"
    if p in {"hgnc", "genecards", "geneid", "genewiki", "genetree", "nextprot"}:
        return "gene"
    return "entity"


def _node_label(node_id: str) -> str:
    prefix, rest = _split_node_id(node_id)
    if not prefix:
        return node_id
    if prefix.lower() == "go" and rest.startswith("GO:"):
        # Some generators produce target="GO:GO:000xxxx".
        return rest
    return f"{prefix}:{rest}"


def _parse_lmp_v4_graph(xml_source: "Path | bytes | str", *, filename: Optional[str] = None) -> Dict[str, Any]:
    try:
        if isinstance(xml_source, (bytes, bytearray)):
            root = ET.fromstring(bytes(xml_source))
            display_name = filename or "<bytes>"
        elif isinstance(xml_source, Path):
            tree = ET.parse(str(xml_source))
            root = tree.getroot()
            display_name = filename or xml_source.name
        else:
            # Legacy str path
            tree = ET.parse(str(xml_source))
            root = tree.getroot()
            display_name = filename or Path(str(xml_source)).name
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid XML: {exc}")

    ns = _xml_namespace(root.tag)
    t = lambda local: _make_tag(ns, local)

    identity = root.find(t("Identity"))
    budo_id = None
    primary_accession = None
    uniprot_id = None
    organism = None
    if identity is not None:
        budo_id = (identity.findtext(t("BudoID")) or "").strip() or None
        primary_accession = (identity.findtext(t("PrimaryAccession")) or "").strip() or None
        uniprot_id = (identity.findtext(t("UniProtKBId")) or "").strip() or None
        org_el = identity.find(t("Organism"))
        if org_el is not None:
            organism = (org_el.text or "").strip() or None

    kg = root.find(t("KnowledgeGraph"))

    edges: List[Dict[str, Any]] = []
    node_ids: set[str] = set()
    node_props: Dict[str, Dict[str, str]] = {}

    def _normalize_ref_node_id(db: str, raw_id: str) -> str:
        db = (db or "").strip()
        raw_id = (raw_id or "").strip()
        if not db:
            return raw_id
        if raw_id.startswith(f"{db}:"):
            return raw_id
        return f"{db}:{raw_id}"

    def _get_property(el: ET.Element, name: str) -> Optional[str]:
        for p in el.findall(t("Property")):
            if (p.get("name") or "").strip() == name:
                val = (p.text or "").strip()
                return val or None
        return None

    def _collect_properties(el: ET.Element) -> Dict[str, str]:
        props: Dict[str, str] = {}
        for p in el.findall(t("Property")):
            key = (p.get("name") or "").strip()
            if not key:
                continue
            val = (p.text or "").strip()
            if val:
                props[key] = val
        return props

    if kg is not None:
        # Preferred: explicit Edge elements.
        for e in kg.findall(t("Edge")):
            src = (e.get("source") or "").strip()
            dst = (e.get("target") or "").strip()
            if not src or not dst:
                continue
            node_ids.add(src)
            node_ids.add(dst)
            edges.append(
                {
                    "source": src,
                    "target": dst,
                    "type": (e.get("type") or "").strip() or None,
                    "db": (e.get("db") or "").strip() or None,
                    "id": (e.get("id") or "").strip() or None,
                    "props": _collect_properties(e) or None,
                }
            )

        # Fallback/compat: UniProt-style CrossReference entries.
        # Many LMP v4 files represent the knowledge graph as cross-references
        # with an implicit relation to the protein identity.
        if budo_id:
            for cr in kg.findall(t("CrossReference")):
                db = (cr.get("db") or "").strip()
                rid = (cr.get("id") or "").strip()
                if not db or not rid:
                    continue
                target = _normalize_ref_node_id(db, rid)
                rel = _get_property(cr, "relation") or _get_property(cr, "Relation")
                edge_type = rel or "HAS_KNOWLEDGE"

                # Preserve xref properties for downstream filtering/UI.
                cr_props = _collect_properties(cr)
                if cr_props:
                    existing = node_props.get(target)
                    if existing:
                        # Do not overwrite existing keys.
                        for k, v in cr_props.items():
                            existing.setdefault(k, v)
                    else:
                        node_props[target] = dict(cr_props)

                node_ids.add(budo_id)
                node_ids.add(target)
                edges.append(
                    {
                        "source": budo_id,
                        "target": target,
                        "type": edge_type,
                        "db": db or None,
                        "id": rid or None,
                        "props": cr_props or None,
                    }
                )

    # Ensure identity node is present even if the KG is empty.
    if budo_id:
        node_ids.add(budo_id)

    nodes: List[Dict[str, Any]] = []
    for nid in sorted(node_ids):
        node: Dict[str, Any] = {
            "id": nid,
            "label": _node_label(nid),
            "kind": _node_kind(nid),
        }
        props = node_props.get(nid)
        if props:
            node["props"] = props
        nodes.append(node)

    return {
        "meta": {
            "file": display_name,
            "budo_id": budo_id,
            "primary_accession": primary_accession,
            "uniprot_id": uniprot_id,
            "organism": organism,
        },
        # Force-graph expects `nodes` + `links`.
        "nodes": nodes,
        "links": edges,
        "counts": {"nodes": len(nodes), "links": len(edges)},
    }


@router.get("/lmp_v4/files")
async def list_lmp_v4_files() -> Dict[str, Any]:
    base = _lmp_v4_dir()
    # Prefer local directory when populated; otherwise fall back to the public
    # GCS bucket so deploy targets without a mounted corpus (e.g., Railway
    # containers) still serve a non-empty file list to Alejandría.
    source, files = _lmp_v4_scanner.scan_xml_files(local_dir=base)
    return {
        "dir": str(base),
        "files": files,
        "count": len(files),
        "source": source,
    }


@router.get("/lmp_v4/graph")
async def get_lmp_v4_graph(file: str = Query(..., description="Filename under .tmp_lmp_v4")) -> Dict[str, Any]:
    xml_bytes, _source = _resolve_xml_bytes(file)
    return _parse_lmp_v4_graph(xml_bytes, filename=file)


# Domain color palette for the visualiser agent tool.
_DOMAIN_COLORS: Dict[str, str] = {
    "domain": "#e6a023",
    "protein": "#4fc3f7",
    "structure": "#81c784",
    "go": "#ba68c8",
    "pathway": "#ff8a65",
    "pharmacology": "#ef5350",
    "sequence": "#90a4ae",
    "interaction": "#aed581",
    "disease": "#f06292",
    "expression": "#fff176",
    "gene": "#7986cb",
    "entity": "#bdbdbd",
}


@router.get("/lmp_v4/domains")
async def get_lmp_v4_domains(
    file: str = Query(..., description="Filename under .tmp_lmp_v4"),
) -> Dict[str, Any]:
    """Return only domain/feature nodes with colour assignments.

    Useful for the visualiser agent tool that needs a compact list of
    domain-class nodes without the full graph payload.
    """
    xml_bytes, _source = _resolve_xml_bytes(file)
    graph = _parse_lmp_v4_graph(xml_bytes, filename=file)

    domain_kinds = {"domain", "go", "pathway", "pharmacology", "disease"}
    domain_nodes: List[Dict[str, Any]] = []
    for node in graph["nodes"]:
        kind = node.get("kind", "entity")
        if kind in domain_kinds:
            domain_nodes.append({
                **node,
                "color": _DOMAIN_COLORS.get(kind, _DOMAIN_COLORS["entity"]),
            })

    return {
        "meta": graph["meta"],
        "domain_nodes": domain_nodes,
        "count": len(domain_nodes),
        "color_legend": {k: v for k, v in _DOMAIN_COLORS.items() if k in domain_kinds},
    }


@router.get("/lmp_v4/viewer", response_class=HTMLResponse)
async def lmp_v4_viewer(file: Optional[str] = Query(default=None, description="Optional .xml file to load")) -> HTMLResponse:
    # Served over HTTP so the viewer can fetch JSON without `file://` CORS issues.
    initial_file = html.escape(file or "")
    # Minimal viewer using force-graph from CDN. If CDN is unavailable, user still can use the JSON endpoint.
    page = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>LMP v4 KnowledgeGraph Viewer</title>
    <style>
            :root { color-scheme: dark; }
            body { margin: 0; font-family: ui-sans-serif, system-ui, Segoe UI, Roboto, Arial; background: #0b0f17; color: #e6edf3; }
            header { padding: 10px 12px; display: flex; gap: 10px; align-items: center; border-bottom: 1px solid #1f2a44; position: sticky; top: 0; background: #0b0f17; z-index: 10; }
            select, button { background: #111a2e; color: #e6edf3; border: 1px solid #2a3a63; border-radius: 6px; padding: 6px 10px; }
            #meta { opacity: 0.9; font-size: 12px; }
            #graph { width: 100vw; height: calc(100vh - 56px); }
            #fallback { white-space: pre-wrap; padding: 12px; display: none; }
            a { color: #7fb4ff; }
    </style>
    <script src=\"https://cdn.jsdelivr.net/npm/force-graph\"></script>
  </head>
  <body>
    <header>
      <strong>KG Viewer</strong>
      <label>File:</label>
      <select id=\"fileSelect\"><option value=\"\">(loading...)</option></select>
      <button id=\"reloadBtn\">Reload</button>
      <span id=\"meta\"></span>
      <span style=\"margin-left:auto; opacity:0.85\">JSON: <a id=\"jsonLink\" href=\"#\">/api/v1/graph/lmp_v4/graph</a></span>
    </header>
    <div id=\"graph\"></div>
    <div id=\"fallback\"></div>

        <script>
            const INITIAL_FILE = __INITIAL_FILE__;

      const sel = document.getElementById('fileSelect');
      const meta = document.getElementById('meta');
      const jsonLink = document.getElementById('jsonLink');
      const fallback = document.getElementById('fallback');
      const reloadBtn = document.getElementById('reloadBtn');

      async function fetchJson(url) {{
        const r = await fetch(url);
        if (!r.ok) throw new Error(`HTTP ${{r.status}}: ${{await r.text()}}`);
        return r.json();
      }}

      function showFallback(msg, obj) {{
        fallback.style.display = 'block';
        fallback.textContent = msg + '\n\n' + (obj ? JSON.stringify(obj, null, 2) : '');
      }}

      async function loadFileList() {{
        const data = await fetchJson('/api/v1/graph/lmp_v4/files');
        const files = data.files || [];
        sel.innerHTML = '';
        if (!files.length) {{
          sel.innerHTML = '<option value="">(no .xml files found)</option>';
          return;
        }}
        for (const f of files) {{
          const opt = document.createElement('option');
          opt.value = f;
          opt.textContent = f;
          sel.appendChild(opt);
        }}
        if (INITIAL_FILE && files.includes(INITIAL_FILE)) {{
          sel.value = INITIAL_FILE;
        }}
      }}

      let Graph = null;
      function ensureGraph() {{
        if (Graph) return Graph;
        if (!window.ForceGraph) throw new Error('force-graph library not available (CDN blocked?)');
        Graph = ForceGraph()(document.getElementById('graph'))
          .nodeId('id')
          .nodeLabel(n => `${{n.kind}}\n${{n.label}}`)
          .nodeAutoColorBy('kind')
          .linkLabel(l => l.type || '')
          .linkDirectionalParticles(1)
          .linkDirectionalParticleWidth(1)
          .linkDirectionalParticleSpeed(0.005)
          .backgroundColor('#0b0f17');
        return Graph;
      }

      async function loadGraph() {{
        const f = sel.value;
        if (!f) return;
        const url = `/api/v1/graph/lmp_v4/graph?file=${{encodeURIComponent(f)}}`;
        jsonLink.href = url;
        jsonLink.textContent = url;

        try {{
          const data = await fetchJson(url);
          meta.textContent = `${{data.counts.nodes}} nodes, ${{data.counts.links}} edges` + (data.meta?.budo_id ? ` | ${{data.meta.budo_id}}` : '');
          fallback.style.display = 'none';
          ensureGraph().graphData({{ nodes: data.nodes, links: data.links }});
        }} catch (e) {{
          meta.textContent = 'Error loading graph';
          showFallback(String(e), null);
        }}
      }

      reloadBtn.addEventListener('click', loadGraph);
      sel.addEventListener('change', loadGraph);

      (async () => {{
        try {{
          await loadFileList();
          await loadGraph();
        }} catch (e) {{
          showFallback('Viewer failed to initialize. You can still call /api/v1/graph/lmp_v4/graph directly.', {{ error: String(e) }});
        }}
      }})();
    </script>
  </body>
</html>"""
    page = page.replace("__INITIAL_FILE__", json.dumps(initial_file))
    return HTMLResponse(content=page)


# ─────────────────────────────────────────────────────────────────
# B-GRAPH-01: Public aggregated graph stats
# ─────────────────────────────────────────────────────────────────

async def _timescale_graph_counts() -> Optional[Dict[str, int]]:
    """Count rows in ``{schema}.atom_graph_nodes`` / ``atom_graph_edges``.

    Returns ``None`` if Timescale is not reachable — callers must fall
    back to the filesystem aggregate so the landing page never 500s.
    """
    import os

    try:
        import asyncpg  # type: ignore
        from mica.infrastructure.persistence.pg_async import (
            asyncpg_connection_kwargs_for_database_url,
            choose_timescale_database_url,
            validate_ident,
        )
    except Exception:
        return None

    dsn = choose_timescale_database_url()
    if not dsn:
        return None

    schema = validate_ident(os.getenv("GRAPHRAG_SCHEMA") or "graphrag")

    kwargs = asyncpg_connection_kwargs_for_database_url(dsn)
    try:
        conn = await asyncpg.connect(dsn=dsn, **kwargs, timeout=5.0)
    except Exception:
        return None

    try:
        node_count = await conn.fetchval(
            f"SELECT COUNT(*)::bigint FROM {schema}.atom_graph_nodes"
        )
        edge_count = await conn.fetchval(
            f"SELECT COUNT(*)::bigint FROM {schema}.atom_graph_edges"
        )
    except Exception:
        await conn.close()
        return None
    finally:
        try:
            await conn.close()
        except Exception:
            pass

    return {
        "nodes": int(node_count or 0),
        "edges": int(edge_count or 0),
        "schema": schema,
    }


@router.get("/stats/public")
async def graph_stats_public() -> Dict[str, Any]:
    """Lightweight, unauthenticated stats for the landing page / health panel.

    Two substrates are aggregated:
      * ``graph``       — TimescaleDB ``{schema}.atom_graph_nodes / atom_graph_edges``
                          (authoritative knowledge graph, STRING + BUDO).
      * ``lmp_corpus``  — local ``.tmp_lmp_v4`` directory scan
                          (filesystem mirror of the shared LMP prefix).

    Both surfaces degrade independently; the endpoint never 500s.
    """
    base = _lmp_v4_dir()
    xml_count = 0
    total_nodes_fs = 0
    total_edges_fs = 0
    corpus_source = "empty"

    if base.exists() and base.is_dir():
        local_xmls = [p for p in base.glob("*.xml") if p.is_file()]
        if local_xmls:
            corpus_source = "local"
            for xml_path in local_xmls:
                xml_count += 1
                try:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    ns = _xml_namespace(root.tag)
                    nodes_el = root.find(_make_tag(ns, "nodes"))
                    edges_el = root.find(_make_tag(ns, "edges"))
                    if nodes_el is not None:
                        total_nodes_fs += len(list(nodes_el))
                    if edges_el is not None:
                        total_edges_fs += len(list(edges_el))
                except Exception:
                    pass

    if xml_count == 0:
        # Fall back to GCS-backed scan so the corpus block is not silently
        # empty on deploy targets without a mounted .tmp_lmp_v4 directory.
        # Per-file parse is skipped here to avoid a 19k-blob round trip per
        # request — the TimescaleDB `graph` block is the authoritative node
        # and edge count, so `lmp_corpus.total_nodes/edges` stays at 0 with
        # `source: "gcs"` advertised explicitly.
        gcs_source, gcs_files = _lmp_v4_scanner.scan_xml_files(local_dir=None)
        if gcs_files:
            xml_count = len(gcs_files)
            corpus_source = gcs_source

    graph_block: Dict[str, Any] = {"status": "unavailable", "nodes": 0, "edges": 0}
    ts = await _timescale_graph_counts()
    if ts is not None:
        graph_block = {
            "status": "ok",
            "nodes": ts["nodes"],
            "edges": ts["edges"],
            "schema": ts["schema"],
        }

    return {
        "graph": graph_block,
        "lmp_corpus": {
            "xml_files": xml_count,
            "total_nodes": total_nodes_fs,
            "total_edges": total_edges_fs,
            "source": corpus_source,
        },
        # Back-compat flat keys for existing consumers (Alejandría status bar).
        "xml_files": xml_count,
        "total_nodes": graph_block["nodes"] or total_nodes_fs,
        "total_edges": graph_block["edges"] or total_edges_fs,
    }

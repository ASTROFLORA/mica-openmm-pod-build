from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("mica.audit")


# ── mica:// URI routing table ────────────────────────────────────────

_MICA_LMP_RE = re.compile(
    r"^mica://lmp/(?P<uniprot>[A-Za-z0-9_]+)/(?P<preset>[A-Za-z0-9_-]+)$"
)
_MICA_LMP_ENRICHED_RE = re.compile(
    r"^mica://lmp/(?P<uniprot>[A-Za-z0-9_]+)/enriched/(?P<preset>[A-Za-z0-9_-]+)$"
)
_MICA_DLM_MANIFEST_RE = re.compile(
    r"^mica://dlm/manifest/(?P<qhash>[A-Za-z0-9_-]+)$"
)
_MICA_DLM_DOC_RE = re.compile(
    r"^mica://dlm/doc/(?P<paper_id>[A-Za-z0-9._-]+)"
    r"/sec/(?P<section>[A-Za-z0-9_-]+)"
    r"/span/(?P<start>\d+)-(?P<end>\d+)$"
)
_MICA_BUCKET_SEARCH_RE = re.compile(
    r"^mica://bucket/search/(?P<entity_type>[A-Za-z0-9_-]+)/(?P<term>[^/]+)$"
)
_MICA_BIO_ARTIFACT_RE = re.compile(
    r"^mica://bio/(?P<artifact_type>[A-Za-z0-9_-]+)/(?P<artifact_id>[A-Za-z0-9._-]+)$"
)
_MICA_WORKSPACE_RE = re.compile(
    r"^mica://workspace/(?P<user_id>[A-Za-z0-9_-]+)/(?P<object_path>.+)$"
)
_MICA_ATOM_GRAPH_RE = re.compile(
    r"^mica://atom/graph/(?P<user_id>[A-Za-z0-9_-]+)/(?P<snapshot_id>[A-Za-z0-9_-]+)$"
)

# ── Bridge suggestion scanner ──────────────────────────────────────
# Regex patterns for entity types commonly found in DLM snippet text.
# These drive the bridge_suggestions[] attached to MaterializedResource.

_UNIPROT_RE = re.compile(r"\b[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}\b")
_PDB_RE = re.compile(r"\b[0-9][A-Za-z0-9]{3}\b")
_GENE_NAME_RE = re.compile(r"\b[A-Z]{2,6}(?:\d[A-Z]?)?\b")
# Common biological domain/keyword markers that trigger LMP suggestions
_DOMAIN_MARKERS = [
    "domain", "motif", "binding", "active site", "catalytic", 
    "phosphorylation", "acetylation", "ubiquitination",
    "transmembrane", "kinase", "receptor", "ligand",
]
# Known high-value genes to avoid false positives on 3-letter acronyms
_HIGH_VALUE_GENES = {
    "TP53", "EGFR", "MAPK", "AKT", "SRC", "RAS", "MYC", "BRAF",
    "WNK1", "WNK2", "WNK3", "WNK4", "SPAK", "OSR1", "CCC",
    "KCC1", "KCC2", "KCC3", "KCC4", "NKCC1", "NKCC2",
    "CFTR", "PTEN", "PIK3", "mTOR", "AMPK", "PKA", "PKC",
}


def _scan_bridge_suggestions(text: str, max_suggestions: int = 3) -> Tuple[BridgeSuggestion, ...]:
    """Scan snippet text for biological entities and generate bridge suggestions.

    Matches UniProt accessions, PDB IDs, and known high-value gene names,
    then produces structured BridgeSuggestion objects for downstream tools
    (LMP compile, PDB fetch, literature search).

    Returns up to ``max_suggestions``, ordered by confidence descending.
    """
    if not text or not isinstance(text, str):
        return ()

    suggestions: List[BridgeSuggestion] = []
    seen_tools: Set[str] = set()

    # 1. UniProt accessions → LMP compile suggestion
    for match in _UNIPROT_RE.finditer(text):
        uniprot = match.group()
        # Validate: must be 6 or 10 chars (standard UniProt format)
        if len(uniprot) not in (6, 10):
            continue
        if "LMP" not in seen_tools:
            suggestions.append(BridgeSuggestion(
                suggested_tool="lmp_compile",
                rationale=f"Snippet mentions UniProt accession {uniprot} — suggest LMP compilation",
                suggested_resource_uri=f"mica://lmp/{uniprot}/default",
                confidence=0.85,
            ))
            seen_tools.add("LMP")
        if len(suggestions) >= max_suggestions:
            break

    # 2. PDB IDs → structure fetch suggestion
    if len(suggestions) < max_suggestions:
        for match in _PDB_RE.finditer(text):
            pdb = match.group().upper()
            if pdb in seen_tools:
                continue
            # Basic validation: PDB IDs are 4-char alphanumeric, not all digits
            if pdb.isdigit() or len(pdb) != 4:
                continue
            suggestions.append(BridgeSuggestion(
                suggested_tool="pdb_fetch",
                rationale=f"Snippet mentions PDB ID {pdb} — suggest structure fetch",
                suggested_resource_uri=f"mica://bio/pdb/{pdb}",
                confidence=0.80,
            ))
            seen_tools.add(pdb)
            if len(suggestions) >= max_suggestions:
                break

    # 3. High-value gene names → literature search suggestion
    if len(suggestions) < max_suggestions:
        words = set(w.upper() for w in text.split())
        found_genes = words & _HIGH_VALUE_GENES
        for gene in sorted(found_genes):
            if gene in seen_tools:
                continue
            suggestions.append(BridgeSuggestion(
                suggested_tool="search_literature",
                rationale=f"Snippet references gene {gene} — suggest targeted literature search",
                confidence=0.75,
            ))
            seen_tools.add(gene)
            if len(suggestions) >= max_suggestions:
                break

    # 4. Domain/keyword markers → domain analysis suggestion
    if len(suggestions) < max_suggestions:
        text_lower = text.lower()
        for marker in _DOMAIN_MARKERS:
            if marker in text_lower and marker.upper() not in seen_tools:
                suggestions.append(BridgeSuggestion(
                    suggested_tool="domain_analysis",
                    rationale=f"Snippet mentions '{marker}' — suggest structural domain analysis",
                    confidence=0.70,
                ))
                seen_tools.add(marker.upper())
                if len(suggestions) >= max_suggestions:
                    break

    return tuple(suggestions[:max_suggestions])


# ── URI ALLOWLIST ────────────────────────────────────────────────────
# Only URIs matching these patterns are permitted for resolution.
# Everything else — including file://, absolute paths, .env, secrets,
# unbounded PDFs — is blocked before any read is attempted.

_ALLOWED_URI_PATTERNS: List[re.Pattern] = [
    re.compile(r"^mica://dlm/.*$"),
    re.compile(r"^mica://bio/.*$"),
    re.compile(r"^mica://lmp/.*$"),
    re.compile(r"^mica://workspace/.*$"),
    re.compile(r"^mica://bucket/.*$"),
    re.compile(r"^mica://atom/.*$"),
    # MCP resource URIs from known servers (validated per-server)
    re.compile(r"^resource://[A-Za-z0-9_-]+/.*$"),
    re.compile(r"^text://[A-Za-z0-9_-]+/.*$"),
]

_FORBIDDEN_URI_PATTERNS: List[re.Pattern] = [
    re.compile(r"^file://", re.IGNORECASE),
    re.compile(r"^(/|[A-Za-z]:\\)",),  # absolute paths (Unix & Windows)
    re.compile(r"\.env", re.IGNORECASE),  # env files
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"\.pdf\b", re.IGNORECASE),  # unbounded PDFs
    re.compile(r"^https?://", re.IGNORECASE),  # external URLs not via MICA gateways
]

# Governance caps
MAX_BYTES_PER_READ: int = 8192
MAX_BYTES_PER_QUERY: int = 65536


def _check_uri_allowlist(uri: str) -> Optional[str]:
    """Validate URI against allowlist and forbidden patterns.

    Returns None if URI is allowed, or an error string if blocked.
    """
    if not uri or not isinstance(uri, str):
        return "BLOCKED: empty or invalid URI"

    # Forbidden patterns take priority
    for pat in _FORBIDDEN_URI_PATTERNS:
        if pat.search(uri):
            return f"BLOCKED: URI matches forbidden pattern '{pat.pattern}'"

    # Must match at least one allowed pattern
    for pat in _ALLOWED_URI_PATTERNS:
        if pat.match(uri):
            return None  # allowed

    return f"BLOCKED: URI does not match any allowed pattern. Allowed prefixes: mica://dlm/, mica://bio/, mica://lmp/, mica://workspace/, mica://bucket/"


_SECRET_PATTERNS: List[re.Pattern] = [
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?key|secret|password|passwd|pwd|token|refresh[_-]?token)\b\s*[:=]\s*([^\s'\"`]{6,})"
    ),
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*(bearer\s+[^\s'\"`]{10,})"),
    re.compile(r"\b(sk-[A-Za-z0-9]{16,})\b"),
    re.compile(r"\b(hf_[A-Za-z0-9]{16,})\b"),
]


def _redact_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    return redacted


def _truncate_text(text: str, *, max_len: int) -> str:
    if not isinstance(text, str):
        return ""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 12] + "…(truncated)"


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _normalize_for_match(text: str) -> str:
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return norm.lower()


@dataclass(frozen=True)
class ResourcePlanItem:
    server: str
    uri: str
    reason: str


@dataclass(frozen=True)
class BridgeSuggestion:
    """A tool suggestion derived from entities found in a resource snippet."""
    suggested_tool: str
    rationale: str
    suggested_resource_uri: Optional[str] = None
    confidence: float = 0.0


@dataclass(frozen=True)
class MaterializedResource:
    server: str
    uri: str
    mime_type: str
    text: str
    sha256: str
    size_bytes: int
    reason: str
    fetched_at: str
    error: Optional[str] = None
    bridge_suggestions: Tuple[BridgeSuggestion, ...] = ()
    atom_snapshot_id: Optional[str] = None


class MCPResourceGateway:
    """Small host-side gateway over MCP ClientSession resources APIs.

    MVP contract:
    - Best-effort compatibility across MCP python client versions
    - Read-only; safe-by-default redaction + truncation
    - Resolves ``mica://`` URIs from user GCS bucket
    """

    def __init__(
        self,
        *,
        mcp_sessions: Dict[str, Any],
        mcp_config: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self._mcp_sessions = mcp_sessions
        self._mcp_config = mcp_config or {}
        self._user_id = user_id
        self._discovered_templates: Optional[List[Dict[str, Any]]] = None

    def _audit(self, event: str, **fields: Any) -> None:
        try:
            payload = {
                "component": "mcp_resources_fabric",
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(),
                **fields,
            }
            audit_logger.info(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            pass

    def _get_session(self, server_name: str) -> Any:
        info = (self._mcp_sessions or {}).get(server_name)
        if not isinstance(info, dict) or info.get("status") != "connected":
            return None
        return info.get("session")

    async def list_resources(self, server_name: str) -> List[Dict[str, Any]]:
        session = self._get_session(server_name)
        if session is None:
            raise ValueError(f"MCP server not connected: {server_name}")
        fn = getattr(session, "list_resources", None)
        if not callable(fn):
            raise RuntimeError("MCP client does not support list_resources")
        res = await fn()
        resources = getattr(res, "resources", res)
        out: List[Dict[str, Any]] = []
        for r in resources or []:
            if isinstance(r, dict):
                uri = r.get("uri") or r.get("resourceUri") or r.get("resource_uri") or r.get("url")
                if uri is not None:
                    uri = str(uri)
                out.append(
                    {
                        "uri": uri,
                        "name": r.get("name"),
                        "description": r.get("description"),
                        "mimeType": r.get("mimeType") or r.get("mime_type"),
                        "tags": r.get("tags"),
                        "annotations": r.get("annotations"),
                        "meta": r.get("_meta") or r.get("meta"),
                        "raw": r,
                    }
                )
            else:
                out.append(
                    {
                        "uri": getattr(r, "uri", None),
                        "name": getattr(r, "name", None),
                        "description": getattr(r, "description", None),
                        "mimeType": getattr(r, "mimeType", None) or getattr(r, "mime_type", None),
                        "tags": getattr(r, "tags", None),
                        "annotations": getattr(r, "annotations", None),
                        "meta": getattr(r, "_meta", None) or getattr(r, "meta", None),
                    }
                )
        return out

    async def list_resource_templates(self, server_name: str) -> List[Dict[str, Any]]:
        session = self._get_session(server_name)
        if session is None:
            raise ValueError(f"MCP server not connected: {server_name}")
        fn = getattr(session, "list_resource_templates", None)
        if not callable(fn):
            # Some clients expose list_resource_templates as list_resource_templates()
            fn = getattr(session, "list_resources_templates", None)
        if not callable(fn):
            raise RuntimeError("MCP client does not support list_resource_templates")
        res = await fn()
        templates = getattr(res, "resourceTemplates", None) or getattr(res, "resource_templates", None) or getattr(res, "templates", None) or res
        out: List[Dict[str, Any]] = []
        for t in templates or []:
            if isinstance(t, dict):
                uri_t = (
                    t.get("uriTemplate")
                    or t.get("uri_template")
                    or t.get("urlTemplate")
                    or t.get("url_template")
                    or t.get("template")
                )
                if uri_t is not None:
                    uri_t = str(uri_t)
                out.append(
                    {
                        "uriTemplate": uri_t,
                        "name": t.get("name"),
                        "description": t.get("description"),
                        "mimeType": t.get("mimeType") or t.get("mime_type"),
                        "tags": t.get("tags"),
                        "annotations": t.get("annotations"),
                        "meta": t.get("_meta") or t.get("meta"),
                        "raw": t,
                    }
                )
            else:
                out.append(
                    {
                        "uriTemplate": getattr(t, "uriTemplate", None) or getattr(t, "uri_template", None),
                        "name": getattr(t, "name", None),
                        "description": getattr(t, "description", None),
                        "mimeType": getattr(t, "mimeType", None) or getattr(t, "mime_type", None),
                        "tags": getattr(t, "tags", None),
                        "annotations": getattr(t, "annotations", None),
                        "meta": getattr(t, "_meta", None) or getattr(t, "meta", None),
                    }
                )
        return out

    async def discover_and_merge_templates(self) -> List[Dict[str, Any]]:
        """Call ``list_resource_templates`` on ALL connected MCP servers and merge
        discovered templates into the local registry.

        Servers that advertise ``resourceTemplates`` (e.g. PubChem, PDB, UniProt
        MCP servers) are auto-introspected. Templates are cached for the session
        duration in ``self._discovered_templates``.

        This closes the gap where only config-provided ``resources.nlp_templates``
        worked and server-advertised templates required manual proxy.
        """
        if self._discovered_templates is not None:
            return self._discovered_templates

        merged: List[Dict[str, Any]] = []
        for server_name, info in (self._mcp_sessions or {}).items():
            if not isinstance(info, dict) or info.get("status") != "connected":
                continue
            try:
                templates = await self.list_resource_templates(server_name)
                for t in templates:
                    t["_source_server"] = server_name
                    merged.append(t)
                self._audit(
                    "discover_templates",
                    server=server_name,
                    template_count=len(templates),
                )
            except Exception as exc:
                self._audit(
                    "discover_templates_error",
                    server=server_name,
                    error=str(exc),
                )

        self._discovered_templates = merged
        return merged

    async def read_resource_text(self, server_name: str, uri: str) -> Tuple[str, str]:
        session = self._get_session(server_name)
        if session is None:
            raise ValueError(f"MCP server not connected: {server_name}")
        fn = getattr(session, "read_resource", None)
        if not callable(fn):
            raise RuntimeError("MCP client does not support read_resource")

        res = await fn(uri)

        # Normalization across MCP client versions
        contents = None
        if isinstance(res, dict):
            contents = res.get("contents")
            mime_type = res.get("mimeType") or res.get("mime_type") or "text/plain"
        else:
            contents = getattr(res, "contents", None)
            mime_type = getattr(res, "mimeType", None) or getattr(res, "mime_type", None) or "text/plain"

        text_parts: List[str] = []
        for c in contents or []:
            if isinstance(c, dict):
                txt = c.get("text")
            else:
                txt = getattr(c, "text", None)
            if txt:
                text_parts.append(str(txt))

        return "\n\n".join(text_parts).strip(), str(mime_type or "text/plain")

    # ── mica:// URI resolution ───────────────────────────────────

    async def resolve_mica_uri(
        self, uri: str, *, max_span_bytes: int = 4096
    ) -> Tuple[str, str]:
        """Resolve a ``mica://`` URI to ``(text, mime_type)`` from the user bucket.

        Supported schemes:
        - ``mica://lmp/{uniprot}/{preset}``
        - ``mica://lmp/{uniprot}/enriched/{preset}``
        - ``mica://dlm/manifest/{query_hash}``  (compact manifest, ~2-5 KB)
        - ``mica://dlm/doc/{paper_id}/sec/{section}/span/{start}-{end}``  (bounded snippet, max 4096 bytes)
        - ``mica://bio/{artifact_type}/{artifact_id}``  (biological artifact handle)
        - ``mica://workspace/{user_id}/{object_path}``  (workspace object)
        - ``mica://bucket/search/{entity_type}/{term}``

        All URIs are validated against the allowlist before resolution.
        Forbidden patterns (file://, absolute paths, .env, secrets, PDFs) are
        blocked at the gate.
        """
        # ── URI allowlist enforcement ─────────────────────────────
        block_reason = _check_uri_allowlist(uri)
        if block_reason is not None:
            self._audit("uri_blocked", uri=uri, reason=block_reason)
            raise ValueError(f"[RESOURCE_GOVERNANCE] {block_reason}")

        if not self._user_id:
            raise ValueError("[RESOURCE_GOVERNANCE] resolve_mica_uri requires user_id on gateway")

        from mica.storage.gcs_user_storage import get_storage_manager

        storage = get_storage_manager()

        # mica://lmp/{uniprot}/enriched/{preset}  (check BEFORE plain lmp)
        m = _MICA_LMP_ENRICHED_RE.match(uri)
        if m:
            obj_path = f"convergence/{m.group('uniprot')}_enriched_{m.group('preset')}.xml"
            _res = storage.read_text_best_effort(user_id=self._user_id, object_path=obj_path)
            text = _res.get("text") if _res else None
            if not text:
                raise FileNotFoundError(f"[RESOURCE_MISSING] No enriched LMP at {obj_path}")
            self._audit("resolve_mica_uri", uri=uri, object=obj_path, chars=len(text),
                        sha256=_sha256_text(text))
            return text, "application/xml"

        # mica://lmp/{uniprot}/{preset}
        m = _MICA_LMP_RE.match(uri)
        if m:
            obj_path = f"lmp_v4/{m.group('uniprot')}/{m.group('preset')}.xml"
            _res = storage.read_text_best_effort(user_id=self._user_id, object_path=obj_path)
            text = _res.get("text") if _res else None
            if not text:
                raise FileNotFoundError(f"[RESOURCE_MISSING] No LMP at {obj_path}")
            self._audit("resolve_mica_uri", uri=uri, object=obj_path, chars=len(text),
                        sha256=_sha256_text(text))
            return text, "application/xml"

        # mica://dlm/manifest/{query_hash}
        m = _MICA_DLM_MANIFEST_RE.match(uri)
        if m:
            obj_path = f"dlm/manifests/{m.group('qhash')}.json"
            _res = storage.read_text_best_effort(user_id=self._user_id, object_path=obj_path)
            text = _res.get("text") if _res else None
            if not text:
                raise FileNotFoundError(f"[RESOURCE_MISSING] No DLM manifest at {obj_path}")
            # Verify it's valid JSON and within size limits
            if len(text.encode("utf-8")) > MAX_BYTES_PER_READ:
                raise ValueError(
                    f"[RESOURCE_TOO_LARGE] DLM manifest at {obj_path} exceeds "
                    f"{MAX_BYTES_PER_READ} bytes"
                )
            self._audit("resolve_mica_uri", uri=uri, object=obj_path, chars=len(text),
                        sha256=_sha256_text(text))
            return text, "application/json"

        # mica://dlm/doc/{paper_id}/sec/{section}/span/{start}-{end}
        m = _MICA_DLM_DOC_RE.match(uri)
        if m:
            paper_id = m.group("paper_id")
            section = m.group("section")
            start = int(m.group("start"))
            end = min(int(m.group("end")), start + max_span_bytes)
            from mica.memory.dlm.snippet_resolver import resolve_snippet

            text = await resolve_snippet(
                storage, self._user_id, paper_id, section, start, end,
            )
            if text.startswith("[snippet_resolver]"):
                raise FileNotFoundError(f"[RESOURCE_MISSING] {text}")
            self._audit("resolve_mica_uri", uri=uri, chars=len(text),
                        sha256=_sha256_text(text), paper_id=paper_id, section=section)
            return text, "text/plain"

        # mica://bio/{artifact_type}/{artifact_id}
        m = _MICA_BIO_ARTIFACT_RE.match(uri)
        if m:
            artifact_type = m.group("artifact_type")
            artifact_id = m.group("artifact_id")
            obj_path = f"bio_artifacts/{artifact_type}/{artifact_id}.json"
            _res = storage.read_text_best_effort(user_id=self._user_id, object_path=obj_path)
            text = _res.get("text") if _res else None
            if not text:
                raise FileNotFoundError(
                    f"[RESOURCE_MISSING] No bio artifact at {obj_path}"
                )
            if len(text.encode("utf-8")) > MAX_BYTES_PER_READ:
                raise ValueError(
                    f"[RESOURCE_TOO_LARGE] Bio artifact at {obj_path} exceeds "
                    f"{MAX_BYTES_PER_READ} bytes"
                )
            self._audit("resolve_mica_uri", uri=uri, object=obj_path, chars=len(text),
                        sha256=_sha256_text(text))
            return text, "application/json"

        # mica://workspace/{user_id}/{object_path}
        m = _MICA_WORKSPACE_RE.match(uri)
        if m:
            ws_user_id = m.group("user_id")
            obj_path = m.group("object_path")
            _res = storage.read_text_best_effort(user_id=ws_user_id, object_path=obj_path)
            text = _res.get("text") if _res else None
            if not text:
                raise FileNotFoundError(
                    f"[RESOURCE_MISSING] No workspace object at {obj_path} for user {ws_user_id}"
                )
            if len(text.encode("utf-8")) > MAX_BYTES_PER_READ:
                raise ValueError(
                    f"[RESOURCE_TOO_LARGE] Workspace object at {obj_path} exceeds "
                    f"{MAX_BYTES_PER_READ} bytes"
                )
            self._audit("resolve_mica_uri", uri=uri, object=obj_path, chars=len(text),
                        sha256=_sha256_text(text))
            return text, "text/plain"

        # mica://atom/graph/{user_id}/{snapshot_id}
        m = _MICA_ATOM_GRAPH_RE.match(uri)
        if m:
            return await self._resolve_atom_graph_uri(
                user_id=m.group("user_id"),
                snapshot_id=m.group("snapshot_id"),
            )

        # mica://bucket/search/{entity_type}/{term}
        m = _MICA_BUCKET_SEARCH_RE.match(uri)
        if m:
            from mica.storage.bucket_search import AhoCorasickBucketScanner

            scanner = AhoCorasickBucketScanner(storage)
            results = scanner.scan(
                user_id=self._user_id,
                terms=[m.group("term")],
                prefix=m.group("entity_type"),
                max_results=10,
            )
            text = json.dumps(results, indent=2, default=str)
            self._audit("resolve_mica_uri", uri=uri, hits=len(results),
                        sha256=_sha256_text(text))
            return text, "application/json"

        raise ValueError(f"[RESOURCE_UNSUPPORTED] Unsupported mica:// URI scheme: {uri}")

    async def _resolve_atom_graph_uri(
        self, user_id: str, snapshot_id: str
    ) -> Tuple[str, str]:
        """Resolve ``mica://atom/graph/{user_id}/{snapshot_id}`` to a compact
        JSON-LD graph of facts/citations from that ATOM snapshot.

        Reads from TimescaleDB ``atom_snapshots`` table. Returns valid JSON-LD
        with ``@context``, ``@graph`` of quintuples, and provenance metadata.
        """
        from mica.memory.atom.persistence import TimescaleAtomPersistentStore

        try:
            store = TimescaleAtomPersistentStore()
            state = await store.load_state(user_id=user_id)
        except Exception as exc:
            self._audit("atom_graph_error", user_id=user_id,
                        snapshot_id=snapshot_id, error=str(exc))
            raise FileNotFoundError(
                f"[RESOURCE_MISSING] ATOM state not available for user {user_id}: {exc}"
            )

        if state is None:
            raise FileNotFoundError(
                f"[RESOURCE_MISSING] No ATOM state for user {user_id}"
            )

        current_snapshot, history = state

        # Find the requested snapshot from history
        target_snapshot = None
        if current_snapshot.metadata.get("snapshot_id") == snapshot_id:
            target_snapshot = current_snapshot
        else:
            for snap in history:
                if snap.metadata.get("snapshot_id") == snapshot_id:
                    target_snapshot = snap
                    break

        if target_snapshot is None:
            # Fallback: return current snapshot as best-effort
            target_snapshot = current_snapshot

        # Build compact JSON-LD graph from quintuples
        graph_nodes = []
        for q in target_snapshot.quintuples:
            node = {
                "@type": "TemporalQuintuple",
                "subject": q.subject,
                "predicate": q.predicate,
                "object": q.obj,
                "observation_time": q.observation_time.isoformat() if q.observation_time else None,
                "confidence": q.extraction_confidence,
            }
            if q.source_fact and q.source_fact.source_uri:
                node["source_uri"] = q.source_fact.source_uri
            graph_nodes.append(node)

        jsonld = {
            "@context": {
                "mica": "https://mica.science/ns#",
                "schema": "https://schema.org/",
                "TemporalQuintuple": "mica:TemporalQuintuple",
                "subject": "mica:subject",
                "predicate": "mica:predicate",
                "object": "mica:object",
                "observation_time": "mica:observationTime",
                "confidence": "mica:extractionConfidence",
                "source_uri": "schema:citation",
            },
            "@id": f"mica://atom/graph/{user_id}/{snapshot_id}",
            "@type": "mica:AtomicTKGGraph",
            "user_id": user_id,
            "snapshot_id": target_snapshot.metadata.get("snapshot_id", snapshot_id),
            "created_at": target_snapshot.metadata.get("created_at", ""),
            "quintuple_count": len(graph_nodes),
            "@graph": graph_nodes,
        }

        text = json.dumps(jsonld, indent=2, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) > MAX_BYTES_PER_READ:
            # Truncate graph if too large: keep first 128 quintuples
            graph_nodes = graph_nodes[:128]
            jsonld["@graph"] = graph_nodes
            jsonld["quintuple_count"] = len(graph_nodes)
            jsonld["truncated"] = True
            text = json.dumps(jsonld, indent=2, ensure_ascii=False, default=str)

        self._audit("resolve_mica_uri", uri=f"mica://atom/graph/{user_id}/{snapshot_id}",
                    chars=len(text), quintuples=len(graph_nodes),
                    sha256=_sha256_text(text))
        return text, "application/ld+json"

    def plan_for_query(self, query: str) -> List[ResourcePlanItem]:
        q = _normalize_for_match(query)
        plan: List[ResourcePlanItem] = []

        for server_name, cfg in (self._mcp_config or {}).items():
            if not isinstance(cfg, dict):
                continue

            auto_cfg = None
            if isinstance(cfg.get("resources"), dict):
                auto_cfg = (cfg.get("resources") or {}).get("auto_inject")
            if auto_cfg is None:
                auto_cfg = cfg.get("auto_inject_resources")
            if not isinstance(auto_cfg, dict):
                continue

            if not auto_cfg.get("enabled", False):
                continue

            triggers = auto_cfg.get("triggers") or auto_cfg.get("keywords") or []
            if triggers:
                triggers_norm = [_normalize_for_match(str(t)) for t in triggers if str(t).strip()]
                if not any(t in q for t in triggers_norm):
                    continue

            uris = auto_cfg.get("uris") or []
            if not isinstance(uris, list) or not uris:
                continue

            max_resources = int(auto_cfg.get("max_resources", 3) or 3)

            for uri in uris[: max_resources if max_resources > 0 else len(uris)]:
                if not uri:
                    continue
                plan.append(
                    ResourcePlanItem(
                        server=server_name,
                        uri=str(uri),
                        reason=f"triggered_by={triggers}" if triggers else "enabled=true",
                    )
                )

            self._audit(
                "plan_for_query",
                query_preview=_truncate_text(_redact_text(str(query or "")), max_len=300),
                planned_count=len(plan),
                servers=sorted({p.server for p in plan}),
            )

        return plan

    async def materialize(
        self,
        plan: Sequence[ResourcePlanItem],
        *,
        max_chars_per_resource: int = MAX_BYTES_PER_READ,
        max_total_chars: int = MAX_BYTES_PER_QUERY,
    ) -> List[MaterializedResource]:
        """Materialize a resource plan into bounded, redacted, audited resources.

        Governance caps (enforced):
        - max_chars_per_resource: capped at MAX_BYTES_PER_READ (8192)
        - max_total_chars: capped at MAX_BYTES_PER_QUERY (65536)
        - Every resource includes sha256 provenance.
        - Failed resources are recorded with typed blocker errors.
        """
        # Enforce hard governance caps
        max_chars_per_resource = min(max_chars_per_resource, MAX_BYTES_PER_READ)
        max_total_chars = min(max_total_chars, MAX_BYTES_PER_QUERY)

        out: List[MaterializedResource] = []
        self._audit(
            "materialize_start",
            requested_count=len(list(plan)) if not isinstance(plan, list) else len(plan),
            max_chars_per_resource=max_chars_per_resource,
            max_total_chars=max_total_chars,
        )
        # Normalize to a list so requested_count and iteration are consistent.
        if not isinstance(plan, list):
            plan = list(plan)
        remaining = max_total_chars
        for item in plan:
            if remaining <= 0:
                break

            try:
                uri_str = str(item.uri or "")
                # ── governance: validate URI before any read ──────
                if uri_str.startswith("mica://"):
                    raw_text, mime_type = await self.resolve_mica_uri(uri_str)
                else:
                    block_reason = _check_uri_allowlist(uri_str)
                    if block_reason is not None:
                        raise ValueError(f"[RESOURCE_GOVERNANCE] {block_reason}")
                    raw_text, mime_type = await self.read_resource_text(item.server, item.uri)
                safe_text = _redact_text(raw_text)
                safe_text = _truncate_text(safe_text, max_len=min(max_chars_per_resource, max(0, remaining)))
                remaining -= len(safe_text)

                # Scan DLM doc snippets for bridge suggestions (UniProt, PDB, gene names)
                bridge_suggestions: Tuple[BridgeSuggestion, ...] = ()
                atom_snapshot_id: Optional[str] = None
                if uri_str.startswith("mica://dlm/doc/"):
                    bridge_suggestions = _scan_bridge_suggestions(safe_text)
                elif uri_str.startswith("mica://dlm/manifest/"):
                    # Extract atom_snapshot_id from the manifest JSON if present
                    try:
                        manifest_data = json.loads(safe_text)
                        atom_snapshot_id = manifest_data.get("atom_snapshot_id")
                    except Exception:
                        pass

                out.append(
                    MaterializedResource(
                        server=item.server,
                        uri=item.uri,
                        mime_type=mime_type,
                        text=safe_text,
                        sha256=_sha256_text(safe_text),
                        size_bytes=len(safe_text.encode("utf-8")),
                        reason=item.reason,
                        fetched_at=datetime.now(timezone.utc).isoformat(),
                        error=None,
                        bridge_suggestions=bridge_suggestions,
                        atom_snapshot_id=atom_snapshot_id,
                    )
                )
                self._audit(
                    "materialize_item",
                    server=item.server,
                    uri=item.uri,
                    ok=True,
                    text_chars=len(safe_text),
                    mime_type=mime_type,
                )
            except Exception as exc:
                out.append(
                    MaterializedResource(
                        server=item.server,
                        uri=item.uri,
                        mime_type="text/plain",
                        text="",
                        sha256=_sha256_text(""),
                        size_bytes=0,
                        reason=item.reason,
                        fetched_at=datetime.now(timezone.utc).isoformat(),
                        error=str(exc),
                    )
                )
                self._audit(
                    "materialize_item",
                    server=item.server,
                    uri=item.uri,
                    ok=False,
                    error=str(exc),
                )

        total_bytes = sum(r.size_bytes for r in out)
        error_count = sum(1 for r in out if r.error)
        success_count = len(out) - error_count

        # ── Resource audit receipt ────────────────────────────────
        self._audit(
            "materialize_done",
            materialized_count=len(out),
            success_count=success_count,
            error_count=error_count,
            total_bytes=total_bytes,
            remaining_bytes=remaining,
            max_bytes_per_query=MAX_BYTES_PER_QUERY,
            errors=[r.error for r in out if r.error],
            uris=[r.uri for r in out],
            sha256s=[r.sha256 for r in out if r.sha256 and r.sha256 != _sha256_text("")],
        )

        return out


def format_resource_context(resources: Iterable[MaterializedResource]) -> str:
    blocks: List[str] = []
    for r in resources:
        header = f"[MCP Resource] server={r.server} uri={r.uri} sha256={r.sha256}"
        if r.error:
            blocks.append(f"{header}\nERROR: {r.error}")
            continue
        blocks.append(f"{header}\n{r.text}")
    return "\n\n".join(blocks).strip()

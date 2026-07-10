from __future__ import annotations

import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import httpx

from mica.infrastructure.runpod_client import RunPodClient


_DEFAULT_EMBED_PROVIDER = "openai"
_DEFAULT_ENDPOINT_ID = "ggiglw5fxhmg68"
_DEFAULT_COLLECTION_NAME = "mpal_67419fb20de0"
_DEFAULT_USER_ID = "mica:dev:driver"
_DEFAULT_EMBED_DIM = 384
_DEFAULT_CHUTES_EMBED_DIM = 384
_DEFAULT_OPENAI_EMBED_MODEL = "text-embedding-3-small"
_DOTENV_LOADED = False
_MEMPALACE_BOOTSTRAPPED = False
_MEMPALACE_WRAPPER_MODULE: Any = None

_AUDIT_QUERY_MARKERS = (
    "r20",
    "main dashboard",
    "multiagent_supernova",
    "multiagent_supernova.md",
    "supernova",
    "tea protocol",
    "atom-budo",
    "architecture review",
    "architectural review",
    "institutional memory",
    "memory bridge",
)
_EXPLICIT_SOURCE_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)+", re.IGNORECASE)


def should_auto_consult_institutional_memory(query: str) -> bool:
    folded = str(query or "").casefold()
    return any(marker in folded for marker in _AUDIT_QUERY_MARKERS)


def _seed_env() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from mica.config.dotenv_loader import seed_env_from_dotenv

        seed_env_from_dotenv()
    except Exception:
        pass
    _DOTENV_LOADED = True


def _resolve_endpoint_id(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("RUNPOD_MEMPALACE_EMBEDDING_ENDPOINT_ID"),
        os.getenv("RUNPOD_MEMPALACE_ENDPOINT_ID"),
        os.getenv("RUNPOD_MEMPALACE_RUNPOD_ENDPOINT_ID"),
        _DEFAULT_ENDPOINT_ID,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return _DEFAULT_ENDPOINT_ID


def _resolve_embed_provider(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_INSTITUTIONAL_MEMORY_EMBED_PROVIDER"),
        os.getenv("MICA_INSTITUTIONAL_MEMORY_PROVIDER"),
        _DEFAULT_EMBED_PROVIDER,
    ):
        normalized = str(value or "").strip().lower()
        if normalized:
            return normalized
    return _DEFAULT_EMBED_PROVIDER


def _resolve_openai_embed_model(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_INSTITUTIONAL_MEMORY_OPENAI_EMBED_MODEL"),
        os.getenv("MICA_OPENAI_EMBED_MODEL"),
        _DEFAULT_OPENAI_EMBED_MODEL,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return _DEFAULT_OPENAI_EMBED_MODEL


def _resolve_openai_api_key(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_OPENAI_API_KEY"),
        os.getenv("OPENAI_API_KEY"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _resolve_openai_base_url() -> str:
    for value in (
        os.getenv("MICA_OPENAI_BASE_URL"),
        os.getenv("OPENAI_BASE_URL"),
        "https://api.openai.com/v1",
    ):
        normalized = str(value or "").strip().rstrip("/")
        if normalized:
            return normalized
    return "https://api.openai.com/v1"


def _resolve_chutes_api_key(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_CHUTES_API_KEY"),
        os.getenv("CHUTES_AI_API_KEY"),
        os.getenv("CHUTES_API_KEY"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _resolve_chutes_embed_url(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_CHUTES_EMBED_URL"),
        os.getenv("CHUTES_EMBED_URL"),
        os.getenv("CHUTES_EMBED_ENDPOINT_URL"),
        os.getenv("CHUTES_API_URL"),
    ):
        normalized = str(value or "").strip().rstrip("/")
        if normalized:
            return normalized
    return ""


def _resolve_chutes_embed_dim(explicit: Optional[int] = None) -> int:
    for value in (
        explicit,
        os.getenv("MICA_CHUTES_EMBED_DIM"),
        os.getenv("CHUTES_EMBED_DIM"),
        _DEFAULT_CHUTES_EMBED_DIM,
    ):
        normalized = str(value or "").strip()
        if not normalized:
            continue
        try:
            return max(1, int(normalized))
        except ValueError:
            continue
    return _DEFAULT_CHUTES_EMBED_DIM


def _resolve_collection_name(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_INSTITUTIONAL_MEMORY_COLLECTION"),
        os.getenv("MICA_USER_RAG_MILVUS_COLLECTION"),
        _DEFAULT_COLLECTION_NAME,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return _DEFAULT_COLLECTION_NAME


def _resolve_user_id(explicit: Optional[str] = None) -> str:
    for value in (
        explicit,
        os.getenv("MICA_INSTITUTIONAL_MEMORY_USER_ID"),
        _DEFAULT_USER_ID,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return _DEFAULT_USER_ID


def _resolve_enabled(explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    value = str(os.getenv("MICA_ENABLE_INSTITUTIONAL_MEMORY_BRIDGE") or "").strip().lower()
    if value:
        return value not in {"0", "false", "no", "off"}
    return False


@dataclass(frozen=True)
class InstitutionalMemoryHit:
    score: float
    content: str
    doc_key: str
    chunk_index: int
    source: str
    metadata: Dict[str, Any]


class MicaInstitutionalMemorySearch:
    def __init__(
        self,
        *,
        embed_provider: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        collection_name: Optional[str] = None,
        user_id: Optional[str] = None,
        embed_dim: Optional[int] = None,
        openai_api_key: Optional[str] = None,
        openai_model: Optional[str] = None,
        chutes_api_key: Optional[str] = None,
        chutes_embed_url: Optional[str] = None,
        chutes_embed_dim: Optional[int] = None,
        enabled: Optional[bool] = None,
        rag_store: Optional[MilvusUserRAGStore] = None,
        runpod_client: Optional[Any] = None,
    ) -> None:
        _seed_env()
        self.embed_provider = _resolve_embed_provider(embed_provider)
        self.endpoint_id = _resolve_endpoint_id(endpoint_id)
        self.collection_name = _resolve_collection_name(collection_name)
        self.user_id = _resolve_user_id(user_id)
        self.embed_dim = int(embed_dim or _DEFAULT_EMBED_DIM)
        self.openai_api_key = _resolve_openai_api_key(openai_api_key)
        self.openai_model = _resolve_openai_embed_model(openai_model)
        self.chutes_api_key = _resolve_chutes_api_key(chutes_api_key)
        self.chutes_embed_url = _resolve_chutes_embed_url(chutes_embed_url)
        self.chutes_embed_dim = _resolve_chutes_embed_dim(chutes_embed_dim)
        self.enabled = _resolve_enabled(enabled)
        self._runpod_client = runpod_client

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if not self.collection_name or not self.user_id:
            return False
        if self.embed_provider == "openai":
            return bool(self.openai_api_key)
        if self.embed_provider == "chutes":
            return bool(self.chutes_api_key and self.chutes_embed_url)
        if self.embed_provider == "runpod":
            return bool(self.endpoint_id and os.getenv("RUNPOD_API_KEY"))
        return True

    async def _embed_query_openai(self, text: str) -> List[float]:
        if not self.openai_api_key:
            raise RuntimeError("OpenAI institutional embedding requires OPENAI_API_KEY or MICA_OPENAI_API_KEY")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{_resolve_openai_base_url()}/embeddings",
                headers={"Authorization": f"Bearer {self.openai_api_key}"},
                json={
                    "input": text,
                    "model": self.openai_model,
                    "dimensions": self.embed_dim,
                },
            )
            response.raise_for_status()
            payload = response.json()

        embeddings = payload.get("data") or []
        if not embeddings:
            raise RuntimeError("OpenAI institutional embedding returned no data")
        query_embedding = embeddings[0].get("embedding") or []
        if not isinstance(query_embedding, list) or not query_embedding:
            raise RuntimeError("OpenAI institutional embedding returned an invalid embedding vector")
        if len(query_embedding) != self.embed_dim:
            raise RuntimeError(
                f"OpenAI institutional embedding dimension mismatch: expected {self.embed_dim}, got {len(query_embedding)}"
            )
        return [float(value) for value in query_embedding]

    async def _embed_query_chutes(self, text: str) -> List[float]:
        if not self.chutes_api_key:
            raise RuntimeError("Chutes institutional embedding requires CHUTES_AI_API_KEY or MICA_CHUTES_API_KEY")
        if not self.chutes_embed_url:
            raise RuntimeError("Chutes institutional embedding requires CHUTES_EMBED_URL or MICA_CHUTES_EMBED_URL")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.chutes_embed_url,
                headers={
                    "Authorization": self.chutes_api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"texts": [text]},
            )
            response.raise_for_status()
            payload = response.json()

        if isinstance(payload, dict) and isinstance(payload.get("output"), dict):
            payload = payload["output"]
        if not isinstance(payload, dict):
            raise RuntimeError("Chutes institutional embedding returned an unexpected payload")

        embeddings = payload.get("embeddings")
        if embeddings is None:
            data = payload.get("data")
            if isinstance(data, list):
                embeddings = [item.get("embedding") for item in data if isinstance(item, dict)]
        if embeddings is None and isinstance(payload.get("embedding"), list):
            embeddings = payload.get("embedding")

        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], (int, float)):
            embeddings = [embeddings]
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError("Chutes institutional embedding returned no embeddings")

        query_embedding = embeddings[0]
        if not isinstance(query_embedding, list) or not query_embedding:
            raise RuntimeError("Chutes institutional embedding returned an invalid embedding vector")
        if len(query_embedding) != self.chutes_embed_dim:
            raise RuntimeError(
                f"Chutes institutional embedding dimension mismatch: expected {self.chutes_embed_dim}, got {len(query_embedding)}"
            )
        return [float(value) for value in query_embedding]

    async def _embed_query(self, query: str) -> Optional[List[float]]:
        text = str(query or "").strip()
        if not text:
            return None

        if self.embed_provider == "openai":
            return await self._embed_query_openai(text)
        if self.embed_provider == "chutes":
            return await self._embed_query_chutes(text)
        if self.embed_provider not in {"runpod", "openai", "chutes"}:
            raise RuntimeError(f"Unsupported institutional embedding provider: {self.embed_provider}")

        client = self._runpod_client
        if client is None:
            client = RunPodClient(api_key=os.getenv("RUNPOD_API_KEY"), endpoint_id=self.endpoint_id)

        try:
            if hasattr(client, "submit_sync_job"):
                job = await client.submit_sync_job({"texts": [text]})
                payload = getattr(job, "output", None) or {}
            elif hasattr(client, "embed_texts"):
                payload = await client.embed_texts([text])
            else:
                payload = await client({"texts": [text]})  # type: ignore[operator]
        except Exception as exc:
            raise RuntimeError(f"RunPod institutional embedding failed: {exc}") from exc

        if isinstance(payload, dict) and isinstance(payload.get("output"), dict):
            payload = payload["output"]
        if not isinstance(payload, dict):
            raise RuntimeError("RunPod institutional embedding returned an unexpected payload")

        embeddings = payload.get("embeddings") or payload.get("embedding") or []
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError("RunPod institutional embedding returned no embeddings")

        query_embedding = embeddings[0]
        if not isinstance(query_embedding, list) or not query_embedding:
            raise RuntimeError("RunPod institutional embedding returned an invalid embedding vector")
        if len(query_embedding) != self.embed_dim:
            raise RuntimeError(
                f"RunPod institutional embedding dimension mismatch: expected {self.embed_dim}, got {len(query_embedding)}"
            )
        return [float(value) for value in query_embedding]

    def _bootstrap_mempalace_runtime(self) -> str:
        global _MEMPALACE_BOOTSTRAPPED, _MEMPALACE_WRAPPER_MODULE

        os.environ.setdefault("MEMPALACE_BACKEND", "zilliz")
        os.environ.setdefault("MEMPALACE_EMBED_PROVIDER", self.embed_provider)
        if self.endpoint_id:
            os.environ.setdefault("RUNPOD_EMBED_ENDPOINT_ID", self.endpoint_id)
        if self.embed_provider == "chutes":
            if self.chutes_api_key:
                os.environ.setdefault("CHUTES_AI_API_KEY", self.chutes_api_key)
                os.environ.setdefault("CHUTES_API_KEY", self.chutes_api_key)
            if self.chutes_embed_url:
                os.environ.setdefault("CHUTES_EMBED_URL", self.chutes_embed_url)
                os.environ.setdefault("CHUTES_EMBED_ENDPOINT_URL", self.chutes_embed_url)
            if self.chutes_embed_dim:
                os.environ.setdefault("CHUTES_EMBED_DIM", str(self.chutes_embed_dim))

        if not _MEMPALACE_BOOTSTRAPPED:
            if _MEMPALACE_WRAPPER_MODULE is None:
                import importlib.util

                repo_root = Path(__file__).resolve().parents[5]
                wrapper_path = repo_root / "tools" / "mica_memory_mcp_server.py"
                spec = importlib.util.spec_from_file_location(
                    "mica_memory_mcp_server_runtime",
                    wrapper_path,
                )
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"Could not load MemPalace wrapper from {wrapper_path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _MEMPALACE_WRAPPER_MODULE = module

            _MEMPALACE_WRAPPER_MODULE.bootstrap_mempalace_runtime()
            _MEMPALACE_WRAPPER_MODULE._maybe_activate_zilliz_backend()
            _MEMPALACE_BOOTSTRAPPED = True

        palace_path = str(os.getenv("MEMPALACE_PALACE_PATH") or "").strip()
        if not palace_path:
            raise RuntimeError("MemPalace runtime did not resolve MEMPALACE_PALACE_PATH")
        return palace_path

    def _search_via_mempalace(
        self,
        *,
        query: str,
        limit: int,
    ) -> List[InstitutionalMemoryHit]:
        from mempalace.searcher import search_memories

        palace_path = self._bootstrap_mempalace_runtime()
        payload = search_memories(
            query,
            palace_path=palace_path,
            n_results=max(1, min(int(limit or 5), 10)),
            max_distance=1.5,
        )
        if payload.get("error"):
            raise RuntimeError(str(payload.get("error")))

        formatted_hits: List[InstitutionalMemoryHit] = []
        for raw_hit in payload.get("results") or []:
            if not isinstance(raw_hit, dict):
                continue
            wing = str(raw_hit.get("wing") or "unknown")
            room = str(raw_hit.get("room") or "unknown")
            source_file = str(raw_hit.get("source_file") or "").strip()
            doc_key = source_file or f"{wing}/{room}"
            metadata: Dict[str, Any] = {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "created_at": raw_hit.get("created_at"),
                "matched_via": raw_hit.get("matched_via"),
                "distance": raw_hit.get("distance"),
                "effective_distance": raw_hit.get("effective_distance"),
                "closet_boost": raw_hit.get("closet_boost"),
                "collection_name": self.collection_name,
            }
            if "drawer_index" in raw_hit:
                metadata["drawer_index"] = raw_hit.get("drawer_index")
            if "total_drawers" in raw_hit:
                metadata["total_drawers"] = raw_hit.get("total_drawers")
            formatted_hits.append(
                InstitutionalMemoryHit(
                    score=float(raw_hit.get("similarity") or 0.0),
                    content=str(raw_hit.get("text") or ""),
                    doc_key=doc_key,
                    chunk_index=int(raw_hit.get("drawer_index") or 0),
                    source=str(source_file or "mempalace"),
                    metadata=metadata,
                )
            )
        return formatted_hits

    def _extract_source_candidates(self, query: str) -> List[str]:
        folded = str(query or "").casefold()
        candidates = {
            token.strip("_-")
            for token in _EXPLICIT_SOURCE_TOKEN_RE.findall(folded)
            if len(token.strip("_-")) >= 6
        }
        if "main dashboard" in folded:
            candidates.add("main_dashboard")
            candidates.add("00_main_dashboard")
        return sorted(candidates, key=len, reverse=True)

    def _source_match_score(self, source_file: str, candidates: Sequence[str]) -> float:
        if not source_file:
            return 0.0
        basename = Path(source_file).name.casefold()
        stem = Path(source_file).stem.casefold()
        for candidate in candidates:
            if not candidate:
                continue
            if stem == candidate or basename == candidate:
                return 1.0
            if candidate in stem or candidate in basename:
                return 0.95
        return 0.0

    def _search_via_source_fallback(
        self,
        *,
        query: str,
        limit: int,
    ) -> List[InstitutionalMemoryHit]:
        candidates = self._extract_source_candidates(query)
        if not candidates:
            return []

        from mempalace.palace import get_collection

        palace_path = self._bootstrap_mempalace_runtime()
        collection = get_collection(palace_path, create=False)
        page_size = 2000
        total = int(collection.count() or 0)
        max_query_window = 16_384
        scan_total = min(total, max_query_window)
        query_terms = [token for token in re.findall(r"\w{3,}", str(query or "").casefold()) if token]
        ranked: List[InstitutionalMemoryHit] = []

        for offset in range(0, scan_total, page_size):
            page_limit = min(page_size, scan_total - offset)
            page = collection.get(include=["documents", "metadatas"], limit=page_limit, offset=offset)
            documents = list(getattr(page, "documents", []) or page.get("documents") or [])
            metadatas = list(getattr(page, "metadatas", []) or page.get("metadatas") or [])
            for document, metadata in zip(documents, metadatas):
                metadata = metadata or {}
                source_file = str(metadata.get("source_file") or "").strip()
                filename_score = self._source_match_score(source_file, candidates)
                if filename_score <= 0.0:
                    continue

                content = str(document or "")
                content_lower = content.casefold()
                overlap = sum(1 for token in query_terms if token in content_lower)
                score = min(1.0, filename_score + (0.02 * overlap))
                ranked.append(
                    InstitutionalMemoryHit(
                        score=score,
                        content=content,
                        doc_key=source_file or f"{metadata.get('wing') or 'unknown'}/{metadata.get('room') or 'unknown'}",
                        chunk_index=int(metadata.get("chunk_index") or 0),
                        source=source_file or "mempalace-fallback",
                        metadata=dict(metadata),
                    )
                )

        ranked.sort(key=lambda hit: (-hit.score, hit.doc_key, hit.chunk_index))
        return ranked[: max(1, min(int(limit or 5), 10))]

    def _format_hit(self, hit: Any) -> InstitutionalMemoryHit:
        metadata = getattr(hit, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        return InstitutionalMemoryHit(
            score=float(getattr(hit, "score", 0.0) or 0.0),
            content=str(getattr(hit, "content", "") or ""),
            doc_key=str(getattr(hit, "doc_key", "") or ""),
            chunk_index=int(getattr(hit, "chunk_index", 0) or 0),
            source=str(metadata.get("source") or "mempalace"),
            metadata=metadata,
        )

    def _format_query_context(self, hits: Sequence[InstitutionalMemoryHit], *, query: str) -> str:
        if not hits:
            return (
                "[INSTITUTIONAL MEMORY]\n"
                f"query={query}\n"
                "status=no_hits\n"
                "reason=The internal memory index was consulted but returned no matches."
            )

        lines: List[str] = ["[INSTITUTIONAL MEMORY]"]
        lines.append(f"query={query}")
        lines.append(f"hits={len(hits)}")
        for idx, hit in enumerate(hits[:5], 1):
            lines.append(
                f"{idx}. score={hit.score:.4f} doc={hit.doc_key} chunk={hit.chunk_index} source={hit.source}"
            )
            snippet = re.sub(r"\s+", " ", hit.content).strip()
            if snippet:
                lines.append(f"   {snippet[:500]}")
        return "\n".join(lines)

    async def search(
        self,
        *,
        query: str,
        limit: int = 5,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.is_available():
            return {
                "status": "disabled",
                "query": query,
                "results": [],
                "context": self._format_query_context([], query=query),
                "reason": "Institutional memory bridge is disabled",
            }

        try:
            formatted_hits = self._search_via_mempalace(query=query, limit=limit)
        except Exception as exc:
            fallback_hits = self._search_via_source_fallback(query=query, limit=limit)
            if fallback_hits:
                return {
                    "status": "ok",
                    "query": query,
                    "embed_provider": self.embed_provider,
                    "endpoint_id": self.endpoint_id,
                    "collection_name": self.collection_name,
                    "user_id": self.user_id,
                    "results": [
                        {
                            "score": hit.score,
                            "content": hit.content,
                            "doc_key": hit.doc_key,
                            "chunk_index": hit.chunk_index,
                            "source": hit.source,
                            "metadata": hit.metadata,
                        }
                        for hit in fallback_hits
                    ],
                    "context": self._format_query_context(fallback_hits, query=query),
                    "fallback_reason": str(exc),
                    "retrieval_mode": "source_file_fallback",
                }
            return {
                "status": "degraded",
                "query": query,
                "results": [],
                "context": self._format_query_context([], query=query),
                "reason": str(exc),
            }
        return {
            "status": "ok",
            "query": query,
            "embed_provider": self.embed_provider,
            "endpoint_id": self.endpoint_id,
            "collection_name": self.collection_name,
            "user_id": self.user_id,
            "results": [
                {
                    "score": hit.score,
                    "content": hit.content,
                    "doc_key": hit.doc_key,
                    "chunk_index": hit.chunk_index,
                    "source": hit.source,
                    "metadata": hit.metadata,
                }
                for hit in formatted_hits[: max(1, min(int(limit or 5), 10))]
            ],
            "context": self._format_query_context(formatted_hits, query=query),
        }


class InstitutionalMemorySearch(MicaInstitutionalMemorySearch):
    """Backward-compatible alias for existing imports."""


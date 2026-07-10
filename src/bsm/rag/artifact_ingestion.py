"""Artifact ingestion utilities for the unified BSM RAG subsystem."""
from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Iterable, List, Optional

from .semantic_store import GLOBAL_SEMANTIC_INDEX, BSMSemanticStore

try:
    from embedding.active import embed_sequence  # type: ignore
except Exception:  # pragma: no cover - fallback for test envs
    try:
        from esm2_provider import embed_sequence  # type: ignore
    except Exception:  # pragma: no cover
        def embed_sequence(_: str) -> List[float]:  # type: ignore
            return [0.0]


@dataclass
class ArtifactRecord:
    """Canonical representation of artifacts eligible for ingestion."""

    artifact_id: str
    summary: str
    sequence: str = ""
    source: str = "generic"
    metadata: Dict[str, Any] = field(default_factory=dict)


class BSMArtifactExtractor:
    """Enumerates candidate artifacts from multiple sources."""

    STATIC_ARTIFACTS: List[ArtifactRecord] = [
        ArtifactRecord(
            artifact_id="prot_kinase_1",
            sequence="MSTNPKPQRIT...",
            summary="Kinase domain variant study",
            source="analysis",
        ),
        ArtifactRecord(
            artifact_id="dock_res_1",
            sequence="PEPTIDESEQAAA",
            summary="Docking pose evaluation peptide A",
            source="docking",
        ),
    ]

    def __init__(self) -> None:
        self._eligible_cache: Optional[List[ArtifactRecord]] = None

    # ------------------------------------------------------------------
    # Data sources
    # ------------------------------------------------------------------
    def iter_static_examples(self) -> Iterable[ArtifactRecord]:
        yield from self.STATIC_ARTIFACTS

    def iter_local_pdb(self, limit: int = 3) -> Iterable[ArtifactRecord]:
        if os.getenv("RAG_SCAN_LOCAL", "0") != "1":
            return

        root = pathlib.Path(os.getenv("RAG_SCAN_PATH", "."))
        count = 0
        for pdb_path in root.glob("*.pdb"):
            if count >= limit:
                break
            try:
                _ = pdb_path.read_text(errors="ignore")
            except Exception:
                continue

            yield ArtifactRecord(
                artifact_id=f"pdb_{pdb_path.stem}",
                summary=f"Local PDB file {pdb_path.name} (truncated)",
                source="scaffold",
            )
            count += 1

    def iter_extra_environment(self) -> Iterable[ArtifactRecord]:
        raw = os.getenv("RAG_EXTRA_ARTIFACTS")
        if not raw:
            return

        for chunk in raw.split("|"):
            if not chunk.strip():
                continue
            parts = chunk.split(":", 1)
            if len(parts) != 2:
                continue
            artifact_id, description = parts
            yield ArtifactRecord(
                artifact_id=artifact_id.strip(),
                summary=description.strip(),
                source="extra",
            )

    def iter_notebooks(self, limit: int = 4) -> Iterable[ArtifactRecord]:
        raw_paths = os.getenv("RAG_NOTEBOOK_PATHS", "").strip()
        configured: List[str] = []
        if raw_paths:
            for chunk in raw_paths.replace(";", "|").split("|"):
                chunk = chunk.strip()
                if chunk:
                    configured.append(chunk)

        defaults = [
            "astroflora_8.0_agentic_research.ipynb",
            "MICA_AstroFlora_Research_Implementation.ipynb",
        ]
        abs_hint = pathlib.Path(r"d:\\DESCARGASHASTA070825\\MICA_AstroFlora_Research_Implementation.ipynb")
        try:
            if abs_hint.exists():
                defaults.append(str(abs_hint))
        except Exception:
            pass

        candidates = configured + [d for d in defaults if d not in configured]
        emitted: set[str] = set()
        count = 0

        for path_entry in candidates:
            if count >= limit:
                break
            try:
                p = pathlib.Path(path_entry)
                if not p.exists():
                    relative = pathlib.Path(".") / path_entry
                    if relative.exists():
                        p = relative
                    else:
                        continue
                resolved = str(p.resolve())
                if resolved in emitted:
                    continue

                data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                cells = data.get("cells", [])
                markdown_fragments: List[str] = []
                markdown_count = 0
                for cell in cells:
                    if cell.get("cell_type") != "markdown":
                        continue
                    src = cell.get("source", [])
                    if isinstance(src, list):
                        markdown_fragments.extend([str(item) for item in src])
                    else:
                        markdown_fragments.append(str(src))
                    markdown_fragments.append("\n\n")
                    markdown_count += 1

                text = ("".join(markdown_fragments)).strip()
                if not text:
                    text = f"Notebook {p.name} sin celdas markdown legibles"

                yield ArtifactRecord(
                    artifact_id=f"nb_{p.stem}",
                    summary=f"Notebook: {p.name} | markdown_cells={markdown_count}\n\n{text}",
                    source="notebook",
                )
                emitted.add(resolved)
                count += 1
            except Exception:
                continue

    # ------------------------------------------------------------------
    def list_artifacts(self, force_refresh: bool = False) -> List[ArtifactRecord]:
        """Return the deduplicated list of artifacts eligible for ingestion."""

        if self._eligible_cache is not None and not force_refresh:
            return list(self._eligible_cache)

        collected: List[ArtifactRecord] = []
        seen: set[str] = set()

        for iterator in (
            self.iter_static_examples(),
            self.iter_local_pdb(),
            self.iter_extra_environment(),
            self.iter_notebooks(),
        ):
            for record in iterator:
                if record.artifact_id in seen:
                    continue
                seen.add(record.artifact_id)
                collected.append(record)

        self._eligible_cache = collected
        return list(collected)


def build_document_text(record: ArtifactRecord) -> str:
    """Compose the text payload stored in the semantic index."""

    sequence_snippet = record.sequence[:200]
    return f"SUMMARY: {record.summary}\nSEQUENCE:{sequence_snippet}"


class BSMArtifactIngestionService:
    """Service responsible for ingesting artifacts into the semantic store."""

    def __init__(self, store: Optional[BSMSemanticStore] = None) -> None:
        self.store = store or GLOBAL_SEMANTIC_INDEX
        self.extractor = BSMArtifactExtractor()
        self._last_ingest_ts: float = 0.0

    # ------------------------------------------------------------------
    def eligible_artifacts(self, *, force_refresh: bool = False) -> List[ArtifactRecord]:
        return self.extractor.list_artifacts(force_refresh=force_refresh)

    def _normalize_vector(self, vec: List[float]) -> List[float]:
        """Pad or truncate vectors so they remain dimensionally consistent."""

        base_dim = self.store.dimension
        if base_dim is None:
            return vec
        if len(vec) == base_dim:
            return vec
        if len(vec) < base_dim:
            return vec + [0.0] * (base_dim - len(vec))
        return vec[:base_dim]

    def ingest_once(self, *, force_refresh: bool = False) -> int:
        """Ingest eligible artifacts a single time; returns number of documents added."""

        artifacts = self.eligible_artifacts(force_refresh=force_refresh)
        ingested = 0
        for record in artifacts:
            vector = embed_sequence(record.sequence)
            vector = self._normalize_vector(vector)
            self.store.add_or_update(
                record.artifact_id,
                build_document_text(record),
                vector,
                metadata={"source": record.source, **record.metadata},
            )
            ingested += 1

        self._last_ingest_ts = time.time()
        return ingested

    async def maybe_background_ingest(self, interval_s: float = 300.0) -> None:
        now = time.time()
        if now - self._last_ingest_ts < interval_s:
            return
        self.ingest_once()

    def status(self) -> Dict[str, Any]:
        eligible = self.eligible_artifacts()
        docs = self.store.size
        coverage = 0.0 if not eligible else round(docs / len(eligible), 4)
        target = float(os.getenv("RAG_COVERAGE_TARGET", "0.6"))

        age = None if self._last_ingest_ts == 0.0 else round(time.time() - self._last_ingest_ts, 2)
        return {
            "documents": docs,
            "eligible": len(eligible),
            "coverage": coverage,
            "coverage_target": target,
            "coverage_target_met": coverage >= target,
            "last_ingest_age_s": age,
            "last_ingest_at": datetime.fromtimestamp(self._last_ingest_ts).isoformat()
            if self._last_ingest_ts
            else None,
        }


__all__ = [
    "ArtifactRecord",
    "BSMArtifactExtractor",
    "BSMArtifactIngestionService",
    "build_document_text",
]

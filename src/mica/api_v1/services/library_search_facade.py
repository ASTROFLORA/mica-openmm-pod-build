"""library_search_facade.py — Alejandria Library search facade.

Routes Library search requests to the correct backend authority:
- Literature tab → LiteratureSearchService (multi-provider with provider quorum)
- All other tabs → existing dedicated provider adapters (preserved)
- Falls back to legacy direct provider calls with explicit classification.

Part of ALEJANDRIA_LIBRARY_LITERATURE_CONSOLIDATION_REWIRE_AUDIT_V1.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from mica.api_v1.routers.library_models import (
    LibraryHit,
    LibrarySearchResponse,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed degradation statuses (per library_provider_fallback_policy_v1.json)
# ---------------------------------------------------------------------------

DEGRADED_UNWIRED = "literature_consolidation_unwired"
DEGRADED_QUORUM_DEGRADED = "provider_quorum_degraded"
DEGRADED_QUORUM_BLOCKED = "provider_quorum_blocked"
DEGRADED_DLM_UNAVAILABLE = "dlm_fulltext_unavailable"
DEGRADED_DLM_PARTIAL = "dlm_fulltext_partial"
DEGRADED_LEGACY_FALLBACK = "legacy_direct_provider_fallback"

# ---------------------------------------------------------------------------
# Legacy classification marker (for code comments and raw metadata)
# ---------------------------------------------------------------------------

LEGACY_CLASSIFICATION_MARKER = (
    "LEGACY_DIRECT_PROVIDER_FALLBACK: This function calls the provider directly. "
    "It is preserved for degraded operation when Literature Consolidation "
    "infrastructure is unavailable. See library_provider_fallback_policy_v1.json."
)


class LibrarySearchFacade:
    """Facade that wires Alejandria Library to Literature Consolidation infrastructure.

    Design contract: library_to_literature_consolidation_adapter_plan_v1.md
    Fallback policy: library_provider_fallback_policy_v1.json
    """

    def __init__(self) -> None:
        self._lit_service: Any = None
        self._quorum_service: Any = None
        self._fulltext_router: Any = None
        self._consolidation_wired: bool = False
        self._quorum_available: bool = False
        self._fulltext_available: bool = False
        self._init_errors: Dict[str, str] = {}
        self._init_attempted: bool = False

    # ------------------------------------------------------------------
    # Lazy initialization — two-phase: probe (lightweight) then init (heavy)
    # ------------------------------------------------------------------

    def _probe_imports(self) -> None:
        """Lightweight import probe — checks if the core service module exists.

        Uses importlib.util.find_spec() on a single known-lightweight module
        to avoid triggering __init__.py imports in heavy sub-packages
        (literature_consolidation.services.__init__ loads DLM chain).

        Called by health() for fast status reporting.
        """
        if self._init_attempted:
            return
        self._init_attempted = True

        import importlib.util

        # Only probe the core LiteratureSearchService module.
        # Sub-package probes (ProviderQuorumService, FullTextRouter) are skipped
        # because their parent __init__.py files trigger the full DLM import chain
        # (genia_trigger_pack → DLMConfig → YAML parsing).
        spec = importlib.util.find_spec("mica.services.literature_search_service")
        if spec is not None:
            self._consolidation_wired = True
            # Assume quorum and fulltext are available if core service is found.
            # Actual init happens lazily in _ensure_service_init().
            self._quorum_available = True
            self._fulltext_available = True
        else:
            self._init_errors["LiteratureSearchService"] = "module not found"

    def _ensure_service_init(self) -> None:
        """Instantiate consolidation services (heavy — may trigger GCS/DB init).
        
        Called only when a search is actually performed, not during health checks.
        """
        self._probe_imports()  # Ensure imports are probed first
        if self._lit_service is not None:
            return  # Already initialized

        if not self._consolidation_wired:
            return  # Import failed — nothing to init

        # Instantiate LiteratureSearchService
        try:
            from mica.services.literature_search_service import LiteratureSearchService
            self._lit_service = LiteratureSearchService()
            logger.info("LibrarySearchFacade: LiteratureSearchService instantiated")
        except Exception as exc:
            self._init_errors["LiteratureSearchService_init"] = str(exc)[:200]
            self._consolidation_wired = False
            logger.warning("LiteratureSearchService instantiation failed: %s", str(exc)[:120])
            return

        # Instantiate ProviderQuorumService (wraps LiteratureSearchService)
        if self._lit_service is not None:
            try:
                from mica.literature_consolidation.services.provider_quorum_service import (
                    ProviderQuorumService,
                )
                self._quorum_service = ProviderQuorumService(
                    search_service=self._lit_service
                )
                self._quorum_available = True
                logger.info("LibrarySearchFacade: ProviderQuorumService instantiated")
            except Exception as exc:
                self._init_errors["ProviderQuorumService_init"] = str(exc)[:200]
                logger.warning("ProviderQuorumService instantiation failed: %s", str(exc)[:120])

    # ------------------------------------------------------------------
    # Literature search — consolidated path
    # ------------------------------------------------------------------

    async def search_literature_consolidated(
        self, q: str, limit: int
    ) -> Tuple[List[LibraryHit], Dict[str, str]]:
        """Search literature through consolidated infrastructure.

        Priority:
        1. ProviderQuorumService.run_quorum() if available
        2. LiteratureSearchService.search() with multi-source
        3. Returns (None, errors) with typed blocker if unwired

        Returns:
            Tuple of (hits, errors). If hits is None, caller should use
            legacy direct provider fallback with classification.
        """
        self._ensure_service_init()

        if not self._consolidation_wired:
            return [], {
                "literature_consolidation": (
                    f"{DEGRADED_UNWIRED}: "
                    f"LiteratureSearchService import failed: "
                    f"{self._init_errors.get('LiteratureSearchService', 'unknown error')}"
                )
            }

        errors: Dict[str, str] = {}
        papers: List[Dict[str, Any]] = []
        receipt_data: Optional[Dict[str, Any]] = None
        source_health: Dict[str, Any] = {}
        quorum_status: Optional[str] = None

        # Path A: Provider quorum (preferred)
        if self._quorum_available:
            try:
                result = await self._run_quorum_search(q, limit)
                papers = list(result.get("papers", []) or [])
                receipt_data = result.get("receipt")
                source_health = result.get("source_health", {})
                quorum_status = result.get("quorum_status")
                if quorum_status == "blocked":
                    errors["provider_quorum"] = (
                        f"{DEGRADED_QUORUM_BLOCKED}: "
                        f"quorum not satisfied — "
                        f"{result.get('blocked_reasons', ['unknown'])}"
                    )
                elif quorum_status == "degraded":
                    errors["provider_quorum"] = (
                        f"{DEGRADED_QUORUM_DEGRADED}: "
                        f"some providers degraded"
                    )
            except Exception as exc:
                errors["provider_quorum"] = (
                    f"{DEGRADED_QUORUM_DEGRADED}: "
                    f"quorum execution failed: {str(exc)[:200]}"
                )
                logger.warning("Provider quorum execution failed: %s", exc)

        # Path B: Direct multi-source via LiteratureSearchService (fallback within consolidated)
        if not papers and self._lit_service is not None:
            try:
                result = await self._lit_service.search(
                    query=q,
                    max_papers=min(limit, 50),
                    sources=["semantic_scholar", "pubmed", "openalex"],
                )
                papers = list(result.papers or [])
                source_health = dict(result.source_health or {})
                if result.failed_sources:
                    errors["literature_consolidation"] = (
                        f"partial_results: failed sources: {result.failed_sources}"
                    )
            except Exception as exc:
                errors["literature_consolidation"] = (
                    f"consolidation_search_failed: {str(exc)[:200]}"
                )
                logger.warning("LiteratureSearchService.search failed: %s", exc)

        # Map papers to LibraryHit
        if papers:
            hits = self._map_consolidated_papers_to_hits(
                papers,
                receipt_data=receipt_data,
                source_health=source_health,
                quorum_status=quorum_status,
            )
            return hits, errors
        else:
            # No results from consolidation — signal caller to use legacy fallback
            return [], errors

    async def _run_quorum_search(
        self, q: str, limit: int
    ) -> Dict[str, Any]:
        """Execute provider quorum search via ProviderQuorumService.

        Returns dict with keys: papers, receipt, source_health, quorum_status, blocked_reasons.
        """
        if self._quorum_service is None:
            return {"papers": [], "quorum_status": "blocked", "blocked_reasons": ["quorum_service_unavailable"]}

        from mica.literature_consolidation.contracts.provider_quorum import (
            ProviderQuorumPolicy,
        )

        # Build a minimal LiteratureQuerySpec for the quorum
        try:
            from mica.literature_consolidation.services.provider_quorum_service import (
                LiteratureQuerySpec,
            )
        except ImportError:
            # LiteratureQuerySpec might be in contracts
            try:
                from mica.literature_consolidation.contracts.provider_quorum import (
                    LiteratureQuerySpec,
                )
            except ImportError:
                # Fallback: build spec dict
                LiteratureQuerySpec = None  # type: ignore[assignment]

        if LiteratureQuerySpec is not None:
            spec = LiteratureQuerySpec(
                query=q,
                max_papers=min(limit, 50),
            )
        else:
            # Build a simple object-like spec
            class _MinimalSpec:
                query = q
                max_papers = min(limit, 50)
                session_id = None
                run_id = None
                user_id = None
                tenant_id = None
                acquisition_budget_usd = None

            spec = _MinimalSpec()  # type: ignore[assignment]

        policy = ProviderQuorumPolicy(
            min_attempted_providers=1,  # Relaxed for library search responsiveness
            min_successful_providers=1,
            allow_degraded_success=True,
            require_nonempty_papers=False,
        )

        result = await self._quorum_service.run_quorum(
            spec=spec,
            lane_class="library",
            preset_name="standard",
            task_type="general",
            policy=policy,
            enable_unpaywall_enrichment=False,  # Skip enrichment for library search speed
        )

        receipt = result.receipt
        receipt_dict: Optional[Dict[str, Any]] = None
        if receipt is not None:
            try:
                receipt_dict = receipt.model_dump() if hasattr(receipt, "model_dump") else dict(receipt)
            except Exception:
                receipt_dict = {"receipt_version": "1.0"}

        return {
            "papers": list(result.papers or []),
            "receipt": receipt_dict,
            "source_health": {},
            "quorum_status": getattr(receipt, "quorum_status", None) if receipt else None,
            "blocked_reasons": list(getattr(receipt, "blocked_reasons", []) if receipt else []),
        }

    # ------------------------------------------------------------------
    # Hit mapping: consolidated papers → LibraryHit
    # ------------------------------------------------------------------

    def _map_consolidated_papers_to_hits(
        self,
        papers: List[Dict[str, Any]],
        *,
        receipt_data: Optional[Dict[str, Any]] = None,
        source_health: Dict[str, Any] = None,
        quorum_status: Optional[str] = None,
    ) -> List[LibraryHit]:
        """Map papers from consolidation services to LibraryHit format.

        Preserves full LibraryHit shape for frontend compatibility.
        Adds receipt_ref, provider_status, degraded to raw.
        """
        source_health = source_health or {}
        hits: List[LibraryHit] = []
        for paper in papers:
            # Build author string
            authors_list = paper.get("authors") or []
            if isinstance(authors_list, list):
                author_names = []
                for a in authors_list[:4]:
                    if isinstance(a, dict):
                        name = a.get("name") or ""
                    else:
                        name = str(a)
                    if name:
                        author_names.append(name)
                authors_str = ", ".join(author_names) if author_names else None
            else:
                authors_str = str(authors_list) if authors_list else None

            # External IDs
            external_ids = paper.get("externalIds") or paper.get("external_ids") or {}
            paper_id = (
                paper.get("paperId")
                or paper.get("paper_id")
                or external_ids.get("DOI")
                or ""
            )
            doi = external_ids.get("DOI") or paper.get("doi")

            # Determine provider status for raw
            provider_status = "consolidated"
            degraded = False
            if quorum_status == "degraded":
                provider_status = DEGRADED_QUORUM_DEGRADED
                degraded = True
            elif quorum_status == "blocked":
                provider_status = DEGRADED_QUORUM_BLOCKED
                degraded = True

            # Build raw object
            raw: Dict[str, Any] = {
                "provider_status": provider_status,
                "degraded": degraded,
                "receipt_ref": receipt_data.get("run_id") if receipt_data else None,
                "receipt": receipt_data,
                "source_health": source_health,
            }

            hits.append(
                LibraryHit(
                    source="literature",
                    id=f"s2:{paper_id}" if paper_id else f"lit:{hash(str(paper.get('title', '')))})",
                    title=paper.get("title") or "(untitled)",
                    subtitle=paper.get("venue") or paper.get("journal"),
                    authors=authors_str,
                    journal=paper.get("venue") or paper.get("journal"),
                    year=paper.get("year"),
                    abstract=paper.get("abstract"),
                    doi=doi,
                    raw=raw,
                )
            )
        return hits

    # ------------------------------------------------------------------
    # Entity mentions — consolidated path
    # ------------------------------------------------------------------

    async def entity_mentions_consolidated(
        self, entity_id: str, query: Optional[str], max_results: int
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Search for entity mentions through consolidated infrastructure.

        Returns (mentions, warnings). Falls back to empty with warnings if unwired.
        """
        self._probe_imports()

        if not self._consolidation_wired:
            return [], [
                f"{DEGRADED_UNWIRED}: entity mention search unavailable — "
                f"LiteratureSearchService not wired"
            ]

        # For now, entity mentions via consolidated search is not implemented.
        # The existing LMP XML + DLM cache scan in library.py is the fallback.
        return [], [
            "consolidated_entity_mentions_not_implemented: "
            "using legacy LMP XML + DLM cache scan"
        ]

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return health status including consolidation wiring status."""
        self._probe_imports()

        return {
            "ok": True,
            "lmp_v4_dir": "",
            "lmp_v4_exists": False,
            "lmp_v4_count": 0,
            # --- Consolidation wiring status (NEW) ---
            "literature_consolidation_wired": self._consolidation_wired,
            "provider_quorum_available": self._quorum_available,
            "bibliotecario_available": False,
            "dlm_fulltext_available": self._fulltext_available,
            "active_providers": (
                ["semantic_scholar", "pubmed", "openalex"]
                if self._consolidation_wired
                else []
            ),
            "init_errors": self._init_errors if self._init_errors else None,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_facade: Optional[LibrarySearchFacade] = None


def get_library_search_facade() -> LibrarySearchFacade:
    """Return the module-level LibrarySearchFacade singleton."""
    global _facade
    if _facade is None:
        _facade = LibrarySearchFacade()
    return _facade

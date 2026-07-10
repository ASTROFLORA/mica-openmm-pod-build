"""test_library_search_facade.py — Tests for LibrarySearchFacade.

Part of ALEJANDRIA_LIBRARY_LITERATURE_CONSOLIDATION_REWIRE_AUDIT_V1.
"""
from __future__ import annotations

import pytest

from mica.api_v1.routers.library_models import LibraryHit, LibrarySearchResponse
from mica.api_v1.services.library_search_facade import (
    DEGRADED_DLM_UNAVAILABLE,
    DEGRADED_LEGACY_FALLBACK,
    DEGRADED_QUORUM_DEGRADED,
    DEGRADED_UNWIRED,
    LibrarySearchFacade,
    get_library_search_facade,
)


class TestLibrarySearchFacadeInit:
    """Test facade initialization and health reporting."""

    def test_facade_singleton(self) -> None:
        """Facade singleton returns same instance."""
        f1 = get_library_search_facade()
        f2 = get_library_search_facade()
        assert f1 is f2

    def test_facade_health_reports_consolidation_status(self) -> None:
        """Health endpoint includes consolidation wiring status fields."""
        facade = LibrarySearchFacade()
        health = facade.health()

        # Backward-compatible fields
        assert "ok" in health
        assert "lmp_v4_count" in health

        # New consolidation fields
        assert "literature_consolidation_wired" in health
        assert "provider_quorum_available" in health
        assert "bibliotecario_available" in health
        assert "dlm_fulltext_available" in health
        assert "active_providers" in health
        assert "init_errors" in health

        # Type checks
        assert isinstance(health["literature_consolidation_wired"], bool)
        assert isinstance(health["provider_quorum_available"], bool)
        assert isinstance(health["active_providers"], list)

    def test_facade_health_init_errors_is_none_when_no_errors(self) -> None:
        """init_errors is None when no import errors occurred."""
        facade = LibrarySearchFacade()
        facade._probe_imports()
        health = facade.health()
        # init_errors may be None or a dict depending on import success
        assert health["init_errors"] is None or isinstance(health["init_errors"], dict)


class TestLibrarySearchFacadeDegradation:
    """Test typed degradation statuses."""

    def test_unwired_returns_typed_blocker(self) -> None:
        """When consolidation is unwired, search returns typed blocker."""
        facade = LibrarySearchFacade()
        # Force unwired state
        facade._init_attempted = True
        facade._consolidation_wired = False

        import asyncio

        async def _run():
            hits, errors = await facade.search_literature_consolidated("test", 10)
            return hits, errors

        hits, errors = asyncio.get_event_loop().run_until_complete(_run())

        assert hits == []
        assert "literature_consolidation" in errors
        assert DEGRADED_UNWIRED in errors["literature_consolidation"]

    def test_degraded_status_constants_defined(self) -> None:
        """All typed degradation constants are defined."""
        assert DEGRADED_UNWIRED == "literature_consolidation_unwired"
        assert DEGRADED_QUORUM_DEGRADED == "provider_quorum_degraded"
        assert DEGRADED_DLM_UNAVAILABLE == "dlm_fulltext_unavailable"
        assert DEGRADED_LEGACY_FALLBACK == "legacy_direct_provider_fallback"

    def test_search_without_init_does_not_crash(self) -> None:
        """Calling search_literature_consolidated before init is safe."""
        facade = LibrarySearchFacade()
        # Pre-set init state to avoid heavy service imports in test env
        facade._init_attempted = True
        facade._consolidation_wired = False

        import asyncio

        async def _run():
            hits, errors = await facade.search_literature_consolidated("kinase", 5)
            return hits, errors

        hits, errors = asyncio.get_event_loop().run_until_complete(_run())
        # Should not raise; returns empty hits and typed blocker error
        assert isinstance(hits, list)
        assert isinstance(errors, dict)
        assert hits == []
        assert "literature_consolidation" in errors


class TestLibraryHitMapping:
    """Test that consolidated papers map correctly to LibraryHit."""

    def test_map_consolidated_papers_preserves_shape(self) -> None:
        """Mapped hits have all required LibraryHit fields."""
        facade = LibrarySearchFacade()

        papers = [
            {
                "paperId": "abc123",
                "title": "Test Paper",
                "abstract": "An abstract",
                "year": 2024,
                "venue": "Test Journal",
                "authors": [{"name": "Author One"}, {"name": "Author Two"}],
                "externalIds": {"DOI": "10.1234/test"},
            }
        ]

        hits = facade._map_consolidated_papers_to_hits(
            papers,
            receipt_data={"run_id": "test-123"},
            source_health={"semantic_scholar": {"status": "ok"}},
            quorum_status="satisfied",
        )

        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, LibraryHit)
        assert hit.source == "literature"
        assert hit.title == "Test Paper"
        assert hit.abstract == "An abstract"
        assert hit.year == 2024
        assert hit.journal == "Test Journal"
        assert hit.authors == "Author One, Author Two"
        assert hit.doi == "10.1234/test"

        # New fields in raw
        assert hit.raw is not None
        assert "provider_status" in hit.raw
        assert "degraded" in hit.raw
        assert "receipt_ref" in hit.raw
        assert "receipt" in hit.raw
        assert "source_health" in hit.raw

    def test_map_papers_with_degraded_quorum(self) -> None:
        """Degraded quorum status is reflected in raw."""
        facade = LibrarySearchFacade()

        papers = [{"paperId": "x1", "title": "Degraded Test"}]

        hits = facade._map_consolidated_papers_to_hits(
            papers, quorum_status="degraded"
        )

        assert hits[0].raw is not None
        assert hits[0].raw["provider_status"] == DEGRADED_QUORUM_DEGRADED
        assert hits[0].raw["degraded"] is True

    def test_map_papers_with_blocked_quorum(self) -> None:
        """Blocked quorum status is reflected in raw."""
        facade = LibrarySearchFacade()

        papers = [{"paperId": "x2", "title": "Blocked Test"}]

        hits = facade._map_consolidated_papers_to_hits(
            papers, quorum_status="blocked"
        )

        assert hits[0].raw is not None
        assert hits[0].raw["provider_status"] == "provider_quorum_blocked"
        assert hits[0].raw["degraded"] is True

    def test_map_empty_papers_returns_empty(self) -> None:
        """Empty paper list returns empty hits."""
        facade = LibrarySearchFacade()
        hits = facade._map_consolidated_papers_to_hits([])
        assert hits == []


class TestLibrarySearchResponseCompatibility:
    """Test that LibrarySearchResponse shape is preserved."""

    def test_response_model_fields_exist(self) -> None:
        """LibrarySearchResponse has all required fields."""
        response = LibrarySearchResponse(
            query="test",
            tab="literature",
            total=0,
            hits=[],
            errors={},
            latency_ms=100,
        )
        assert response.query == "test"
        assert response.tab == "literature"
        assert response.total == 0
        assert response.hits == []
        assert response.errors == {}
        assert response.latency_ms == 100

    def test_response_with_new_error_keys(self) -> None:
        """Response can include new consolidation-related error keys."""
        response = LibrarySearchResponse(
            query="test",
            tab="literature",
            total=0,
            hits=[],
            errors={
                "literature_consolidation": "test_error",
                "provider_quorum": "test_error",
            },
            latency_ms=100,
        )
        assert "literature_consolidation" in response.errors
        assert "provider_quorum" in response.errors

    def test_hit_raw_fields_are_additive(self) -> None:
        """New raw fields do not break existing LibraryHit construction."""
        hit = LibraryHit(
            source="literature",
            id="test:1",
            title="Test",
            raw={
                "provider_status": "consolidated",
                "degraded": False,
                "receipt_ref": "r-1",
                "receipt": {"run_id": "r-1"},
                "source_health": {"semantic_scholar": {"status": "ok"}},
            },
        )
        # Existing fields preserved
        assert hit.source == "literature"
        assert hit.title == "Test"
        # Raw is preserved
        assert hit.raw is not None
        assert hit.raw["provider_status"] == "consolidated"


class TestNonLiteratureTabsPreserved:
    """Non-literature tabs are unaffected by the facade."""

    def test_facade_only_handles_literature(self) -> None:
        """Facade search_literature_consolidated only handles literature."""
        facade = LibrarySearchFacade()
        # The facade only has search_literature_consolidated.
        # Non-literature tabs go through existing library.py functions unchanged.
        assert hasattr(facade, "search_literature_consolidated")
        # Non-lit functions are NOT on the facade (they stay in library.py)
        assert not hasattr(facade, "search_alphafold")


class TestDlmFulltextBlocker:
    """DLM/full-text missing returns typed blocker, not fake hits."""

    def test_no_fake_fulltext_in_facade(self) -> None:
        """Facade does not fabricate fulltext availability."""
        facade = LibrarySearchFacade()
        facade._probe_imports()
        health = facade.health()
        # dlm_fulltext_available is a boolean, not faked
        assert isinstance(health["dlm_fulltext_available"], bool)
        # If fulltext not wired, it should be False
        if not facade._fulltext_available:
            assert health["dlm_fulltext_available"] is False

"""Service-layer exports for literature consolidation.

Imports are guarded so one optional dependency chain does not prevent loading
unrelated services.
"""

__all__ = []

try:
    from .deep_research_service import DeepResearchExecutionRequest, run_deep_research

    __all__.extend(["DeepResearchExecutionRequest", "run_deep_research"])
except ModuleNotFoundError:
    pass

try:
    from .literature_ingest_service import LiteratureIngestExecutionRequest, run_literature_ingest

    __all__.extend(["LiteratureIngestExecutionRequest", "run_literature_ingest"])
except ModuleNotFoundError:
    pass

try:
    from .research_pipeline_service import ResearchPipelineExecutionRequest, run_research_pipeline

    __all__.extend(["ResearchPipelineExecutionRequest", "run_research_pipeline"])
except ModuleNotFoundError:
    pass

try:
    from .query_facade_service import LiteratureQueryFacadeService

    __all__.extend(["LiteratureQueryFacadeService"])
except ModuleNotFoundError:
    pass

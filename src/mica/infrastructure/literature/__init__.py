__all__ = [
    "UserLiteratureIngestionConfig",
    "UserLiteratureIngestionService",
    "UserLiteratureIngestionSummary",
]


def __getattr__(name: str):
    if name in __all__:
        from .user_literature_ingestion import (
            UserLiteratureIngestionConfig,
            UserLiteratureIngestionService,
            UserLiteratureIngestionSummary,
        )

        exports = {
            "UserLiteratureIngestionConfig": UserLiteratureIngestionConfig,
            "UserLiteratureIngestionService": UserLiteratureIngestionService,
            "UserLiteratureIngestionSummary": UserLiteratureIngestionSummary,
        }
        return exports[name]
    raise AttributeError(name)

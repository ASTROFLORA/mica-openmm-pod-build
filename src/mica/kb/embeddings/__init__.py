"""KB embedding providers."""

from .biolinkbert_modal_client import (
    BiolinkBertUnavailable,
    embed_texts_modal,
)

__all__ = ["BiolinkBertUnavailable", "embed_texts_modal"]
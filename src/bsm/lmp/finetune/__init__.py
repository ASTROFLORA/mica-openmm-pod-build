"""Finetuning exports for LMP.

This package contains reusable export logic for building datasets for:
- LLM finetuning (JSONL task records derived from LMP)
- pLM finetuning (per-residue labels derived from LMP)

CLI entrypoints remain in `scripts/` as thin wrappers.
"""

from .export_llm_jsonl import main as export_llm_jsonl_main
from .export_plm_labels import main as export_plm_labels_main

__all__ = [
    "export_llm_jsonl_main",
    "export_plm_labels_main",
]

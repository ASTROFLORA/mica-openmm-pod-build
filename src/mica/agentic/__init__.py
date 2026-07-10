"""MICA Agentic Core – unified iterative agent runtime.

Public API::

    from mica.agentic import AgenticLoop, ProviderRegistry, LoopConfig

    registry = ProviderRegistry.from_env()
    loop = AgenticLoop(registry, LoopConfig(max_iterations=10))

    async for event in loop.run(messages, tools, executor, provider_id="openai"):
        handle(event)

See :mod:`mica.agentic.core` for the full class reference.
"""

from .core import AgenticLoop, LoopConfig, ModelHandle, ProviderConfig, ProviderRegistry
from .events import (
    AnyLoopEvent,
    ContextCompacted,
    ContextOverflow,
    Error,
    LoopEvent,
    LoopFinish,
    ResourceInjected,
    RetryWait,
    StepFinish,
    StreamStart,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)
from .retry import RetryPolicy
from .safety import ContextTracker, DoomLoopDetector, ToolTruncator, compact_messages, llm_compact_messages

__all__ = [
    # Core
    "AgenticLoop",
    "LoopConfig",
    "ModelHandle",
    "ProviderConfig",
    "ProviderRegistry",
    # Events
    "AnyLoopEvent",
    "ContextCompacted",
    "ContextOverflow",
    "Error",
    "LoopEvent",
    "LoopFinish",
    "ResourceInjected",
    "RetryWait",
    "StepFinish",
    "StreamStart",
    "TextDelta",
    "ToolCallEnd",
    "ToolCallStart",
    # Retry
    "RetryPolicy",
    # Safety
    "ContextTracker",
    "DoomLoopDetector",
    "ToolTruncator",
    "compact_messages",
    "llm_compact_messages",
]

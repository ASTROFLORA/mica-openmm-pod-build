"""Resource cleanup helpers extracted from AgenticDriver loop executor."""

from typing import Any, Optional


async def cleanup_execution_resources(
    *,
    literature_service: Optional[Any],
    sandbox_manager: Optional[Any],
) -> None:
    """Best-effort cleanup for shared loop resources."""
    if literature_service is not None:
        try:
            await literature_service.close()
        except Exception:
            pass

    if sandbox_manager is not None:
        try:
            await sandbox_manager.cleanup_all()
        except Exception:
            pass
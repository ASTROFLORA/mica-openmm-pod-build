"""
job_transaction.py — GPU job lifecycle transaction with auto-rollback.

Anti-rigidity: A GPU instance provisioned by FASE 0 MUST be destroyed
on failure, even if the exception happens in FASE 1-7. JobTransaction
is the context manager that guarantees this.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Awaitable, List, Optional

logger = logging.getLogger(__name__)


class TransactionState(str, Enum):
    IDLE = "IDLE"
    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class NullDestroyFn:
    """
    Fallback destroy callable used when no real destroyer is provided.
    Logs a warning and does nothing else.
    """

    async def __call__(self, instance_id: str) -> None:
        logger.warning(
            "NullDestroyFn: no destroy implementation — instance %s NOT destroyed",
            instance_id,
        )


class JobTransaction:
    """
    Async context manager that wraps a GPU job lifecycle.

    Usage::

        async with JobTransaction(job_id="j-001", provider=provider, destroy_fn=destroy) as tx:
            tx.set_instance(await provider.provision(...))
            # ... do work ...
            # clean exit → commit()
            # exception → rollback(destroy instance)
    """

    def __init__(
        self,
        job_id: str,
        provider: Any,
        destroy_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.job_id: str = job_id
        self.provider: Any = provider
        self.instance_id: Optional[str] = None
        self.state: TransactionState = TransactionState.IDLE
        self.opened_at: Optional[datetime] = None
        self.closed_at: Optional[datetime] = None
        self._destroy_fn: Optional[Callable[[str], Awaitable[None]]] = destroy_fn
        self._rollback_log: List[str] = []

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "JobTransaction":
        self.state = TransactionState.OPEN
        self.opened_at = datetime.now(timezone.utc)
        logger.info("JobTransaction[%s]: OPEN", self.job_id)
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
    ) -> bool:
        if exc_type is None:
            await self.commit()
        else:
            if self.instance_id is not None:
                await self.rollback(reason=str(exc_val))
            else:
                logger.warning(
                    "JobTransaction[%s]: exception with no instance_id — skipping rollback: %s",
                    self.job_id,
                    exc_val,
                )
                self.state = TransactionState.ROLLED_BACK
        # Re-raise the exception
        return False

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def set_instance(self, instance_id: str) -> None:
        """Record the provisioned GPU instance ID."""
        if self.state != TransactionState.OPEN:
            raise RuntimeError(
                f"JobTransaction[{self.job_id}]: set_instance called while state={self.state.value}"
            )
        self.instance_id = instance_id
        logger.info(
            "JobTransaction[%s]: instance registered — %s", self.job_id, instance_id
        )

    async def commit(self) -> None:
        """Mark the transaction as successfully committed."""
        self.state = TransactionState.COMMITTED
        self.closed_at = datetime.now(timezone.utc)
        logger.info(
            "JobTransaction[%s]: COMMITTED (instance=%s)", self.job_id, self.instance_id
        )

    async def rollback(self, reason: str = "") -> None:
        """
        Destroy the provisioned instance on failure.

        If no instance_id is set, log WARNING only.
        If destroy_fn raises, log ERROR and set FAILED (best-effort, do NOT re-raise).
        """
        if self.instance_id is None:
            msg = f"JobTransaction[{self.job_id}]: rollback — no instance to destroy"
            logger.warning(msg)
            self._rollback_log.append(msg)
            self.state = TransactionState.ROLLED_BACK
            self.closed_at = datetime.now(timezone.utc)
            return

        msg = (
            f"JobTransaction[{self.job_id}]: rolling back instance {self.instance_id} "
            f"— reason: {reason}"
        )
        logger.warning(msg)
        self._rollback_log.append(msg)

        destroy = self._destroy_fn
        if destroy is None:
            logger.warning(
                "JobTransaction[%s]: no destroy_fn provided — instance %s may be leaked",
                self.job_id,
                self.instance_id,
            )
            self.state = TransactionState.ROLLED_BACK
            self.closed_at = datetime.now(timezone.utc)
            return

        try:
            await destroy(self.instance_id)
            self.state = TransactionState.ROLLED_BACK
            self.closed_at = datetime.now(timezone.utc)
            logger.info(
                "JobTransaction[%s]: rollback complete — instance %s destroyed",
                self.job_id,
                self.instance_id,
            )
        except Exception as exc:  # noqa: BLE001
            err_msg = (
                f"JobTransaction[{self.job_id}]: destroy_fn FAILED for instance "
                f"{self.instance_id} — {exc}"
            )
            logger.error(err_msg)
            self._rollback_log.append(err_msg)
            self.state = TransactionState.FAILED
            self.closed_at = datetime.now(timezone.utc)
            # Best-effort: do NOT re-raise

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "instance_id": self.instance_id,
            "state": self.state.value,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "rollback_log": list(self._rollback_log),
        }

"""Agentic driver-local tools."""

__all__ = ["LMPStateReceiptsPlugin"]


def __getattr__(name: str):
	if name == "LMPStateReceiptsPlugin":
		from .lmp_state_receipts import LMPStateReceiptsPlugin

		return LMPStateReceiptsPlugin
	raise AttributeError(name)

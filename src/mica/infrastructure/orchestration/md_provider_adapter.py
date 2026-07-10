from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .md_execution_protocol import RESULT_SCHEMA_VERSION


@dataclass
class AdapterExecution:
    adapter: "MDProviderAdapter"
    provider_name: str
    request: Dict[str, Any]
    orchestrator: Any
    canonical_result: Optional[Dict[str, Any]] = None

    async def run(self) -> Dict[str, Any]:
        try:
            raw_result = await self.orchestrator.run()
            self.canonical_result = self.adapter.normalize_result(
                raw_result=raw_result,
                request=self.request,
                orchestrator=self.orchestrator,
                provider_name=self.provider_name,
            )
            return self.canonical_result
        except Exception as exc:
            self.canonical_result = self.adapter.normalize_exception(
                exc=exc,
                request=self.request,
                orchestrator=self.orchestrator,
                provider_name=self.provider_name,
            )
            return self.canonical_result

    @property
    def state(self) -> Any:
        return getattr(self.orchestrator, "state", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.orchestrator, name)


class MDProviderAdapter(ABC):
    provider_aliases: tuple[str, ...] = ()
    adapter_id: str = "md_provider_adapter"

    @abstractmethod
    def build_orchestrator(self, cfg: Any, provider: Any, on_event: Any = None) -> Any:
        raise NotImplementedError

    @abstractmethod
    def build_request(self, cfg: Any, provider_name: str) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def normalize_result(
        self,
        raw_result: Any,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def supports_config(self, cfg: Any) -> bool:
        return False

    def normalize_exception(
        self,
        exc: Exception,
        request: Dict[str, Any],
        orchestrator: Any,
        provider_name: str,
    ) -> Dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "job": {
                "job_id": request.get("job", {}).get("job_id", ""),
                "workflow": request.get("job", {}).get("workflow", "protein_ligand_md"),
                "execution_target": request.get("job", {}).get("execution_target", "remote"),
                "execution_class": request.get("job", {}).get("execution_class", "research"),
            },
            "status": {
                "state": "failed",
                "phase": "failed",
                "terminal": True,
                "success": False,
                "reason_code": "adapter_exception",
                "reason_message": str(exc),
            },
            "provider": {
                "name": provider_name,
                "adapter_id": self.adapter_id,
            },
            "effective_config": request,
            "backend_native": {
                "error": str(exc),
            },
        }

    def create_execution(self, cfg: Any, provider: Any, on_event: Any = None) -> AdapterExecution:
        provider_name = str(getattr(provider, "PROVIDER_NAME", "") or "unknown").lower()
        request = self.build_request(cfg, provider_name)
        orchestrator = self.build_orchestrator(cfg, provider, on_event=on_event)
        return AdapterExecution(
            adapter=self,
            provider_name=provider_name,
            request=request,
            orchestrator=orchestrator,
        )
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from .md_execution_contract import build_execution_request_v1

if TYPE_CHECKING:
    from .biodynamo_driver import BioDynamoDriver


class DynamoExecutionFacade:
    """Private driver-layer router for BioDynamo protein-ligand MD execution."""

    def __init__(self, driver: "BioDynamoDriver") -> None:
        self._driver = driver

    async def execute_protein_ligand_md(
        self,
        *,
        context: Dict[str, Any],
        protein: str,
        smiles: str,
        docked: str,
        simulation_mode: str,
    ) -> Dict[str, Any]:
        query_text = str(context.get("_driver_query", "") or "").lower()

        execution_backend = self._resolve_execution_backend(context, query_text)
        if execution_backend and execution_backend not in ("vast", "", "local"):
            unified = self._driver._get_unified_client()
            if unified is not None:
                execution_request = self._build_execution_request(
                    context=context,
                    protein=protein,
                    smiles=smiles,
                    docked=docked,
                    simulation_mode=simulation_mode,
                    execution_target="remote",
                )
                md_result = await unified.run_md_inline(
                    pdb_path=protein,
                    user_id=str(context.get("user_id") or "anonymous"),
                    context=context,
                    execution_request=execution_request,
                    on_event=context.get("_remote_md_registry_event_sink"),
                )
                if not md_result.get("_delegate_to_caller"):
                    return md_result

        use_remote = self._should_use_remote(context, query_text)
        execution_request = self._build_execution_request(
            context=context,
            protein=protein,
            smiles=smiles,
            docked=docked,
            simulation_mode=simulation_mode,
            execution_target="remote" if use_remote else "local",
        )

        if use_remote:
            return await self._driver._execute_protein_ligand_md_remote(
                context,
                protein,
                smiles,
                docked,
                execution_request,
            )

        return await self._driver._execute_protein_ligand_md_local(
            context,
            protein,
            smiles,
            docked,
            execution_request,
        )

    @staticmethod
    def _resolve_execution_backend(context: Dict[str, Any], query_text: str) -> str:
        execution_backend = str(context.get("execution_backend") or "").lower().strip()
        if not execution_backend and any(kw in query_text for kw in ("salad", "srcg", "gcs-md")):
            execution_backend = "salad"
            context["execution_backend"] = execution_backend
        return execution_backend

    @staticmethod
    def _should_use_remote(context: Dict[str, Any], query_text: str) -> bool:
        execution_backend = str(context.get("execution_backend") or "").lower().strip()
        if execution_backend == "local":
            return False
        if execution_backend == "vast":
            return True
        use_remote = context.get("use_remote_vast", None)
        if use_remote is None:
            remote_intent = any(
                token in query_text
                for token in ("vast", "remote", "runpod", "pod", "ssh")
            )
            use_remote = bool(remote_intent or context.get("resume_spec_path"))
        return bool(use_remote)

    @staticmethod
    def _build_execution_request(
        *,
        context: Dict[str, Any],
        protein: str,
        smiles: str,
        docked: str,
        simulation_mode: str,
        execution_target: str,
    ) -> Dict[str, Any]:
        return build_execution_request_v1(
            context,
            protein_pdb=protein,
            ligand_smiles=smiles,
            docked_ligand_pdb=docked,
            execution_target=execution_target,
            simulation_mode=simulation_mode,
        )

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mica.api_v1.routers.lmp_annotations import (
    PairDynamicQueryItem,
    PairDynamicQueryRequest,
    ResidueDynamicQueryRequest,
    resolve_state_dynamics_from_state_id,
    resolve_state_pair_dynamics_query_from_state_id,
    resolve_state_residue_dynamics_query_from_state_id,
    resolve_state_receipt_from_state_id,
    resolve_state_structure_comparison_ledger_from_state_id,
)

try:
    from semantic_kernel.functions import kernel_function
except Exception:
    try:
        from semantic_kernel.functions.kernel_function_decorator import kernel_function
    except Exception:
        def kernel_function(*_args, **_kwargs):
            def _decorator(func):
                return func

            return _decorator


class LMPStateReceiptsPlugin:
    """Semantic Kernel-facing retrieval plugin for compact LMP state receipts."""

    def __init__(self, *, allow_afdb_fallback: bool = True) -> None:
        self.allow_afdb_fallback = bool(allow_afdb_fallback)

    @kernel_function(
        name="get_state_receipt",
        description=(
            "Resolve an LMP state_id into a compact structural receipt and optional dynamic statistics. "
            "Input: state_id from the LMP annotations manifest. Returns JSON with keys: state_id, meta, "
            "structural_receipt {source_kind, structure_origin, alphafold, visuals, pocket_sites, structure_path}, "
            "and dynamics_statistics. Use this instead of sending raw XML, PDB, CIF, or MD artifacts to the model."
        ),
    )
    async def get_state_receipt(self, state_id: str) -> Dict[str, Any]:
        response = await resolve_state_receipt_from_state_id(
            state_id,
            allow_afdb_fallback=self.allow_afdb_fallback,
        )
        return response.model_dump()

    @kernel_function(
        name="get_dynamic_statistics",
        description=(
            "Resolve only the DynamicsStatistics block for an LMP state_id. Returns JSON with keys: "
            "state_id, meta, dynamics_statistics {source_kind, run_metadata, dataset_refs, residue_stats, pair_stats}. "
            "Use this when you need RMSF, SASA, pairwise interaction, or normal-mode style summaries without the full receipt."
        ),
    )
    async def get_dynamic_statistics(self, state_id: str) -> Dict[str, Any]:
        response = await resolve_state_dynamics_from_state_id(state_id)
        return response.model_dump()

    @kernel_function(
        name="get_residue_dynamic_statistics",
        description=(
            "Resolve a bounded residue-level DynamicsStatistics query for an LMP state_id. "
            "Accepts explicit positions and/or a chain filter plus max_results. Returns only matched residue stats "
            "with query metadata so planners avoid reading the full dynamic block."
        ),
    )
    async def get_residue_dynamic_statistics(
        self,
        state_id: str,
        positions: Optional[List[int]] = None,
        chain: Optional[str] = None,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        response = await resolve_state_residue_dynamics_query_from_state_id(
            state_id,
            ResidueDynamicQueryRequest(
                positions=list(positions or []),
                chain=chain,
                max_results=max_results,
            ),
        )
        return response.model_dump()

    @kernel_function(
        name="get_pair_dynamic_statistics",
        description=(
            "Resolve a bounded pair-level DynamicsStatistics query for an LMP state_id. "
            "Accepts explicit residue pairs and/or chain filters plus max_results. Returns only matched pair stats "
            "with query metadata so planners avoid reading the full pairwise block."
        ),
    )
    async def get_pair_dynamic_statistics(
        self,
        state_id: str,
        pairs: Optional[List[Dict[str, Any]]] = None,
        chain_i: Optional[str] = None,
        chain_j: Optional[str] = None,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        query_pairs: List[PairDynamicQueryItem] = []
        for raw_pair in pairs or []:
            if isinstance(raw_pair, dict):
                query_pairs.append(PairDynamicQueryItem(**raw_pair))
            elif isinstance(raw_pair, (list, tuple)) and len(raw_pair) >= 2:
                query_pairs.append(
                    PairDynamicQueryItem(
                        position_i=int(raw_pair[0]),
                        position_j=int(raw_pair[1]),
                    )
                )
        response = await resolve_state_pair_dynamics_query_from_state_id(
            state_id,
            PairDynamicQueryRequest(
                pairs=query_pairs,
                chain_i=chain_i,
                chain_j=chain_j,
                max_results=max_results,
            ),
        )
        return response.model_dump()

    @kernel_function(
        name="get_structure_comparison_ledger",
        description=(
            "Resolve a deterministic AFDB-vs-PDB comparison ledger for an LMP state_id. "
            "Returns a stable ledger_id, catalog digest, AFDB summaries, experimental PDB summaries, and overlap entries "
            "derived from the StructureCatalog plane."
        ),
    )
    async def get_structure_comparison_ledger(
        self,
        state_id: str,
        allow_afdb_fallback: bool = True,
    ) -> Dict[str, Any]:
        response = await resolve_state_structure_comparison_ledger_from_state_id(
            state_id,
            allow_afdb_fallback=allow_afdb_fallback,
        )
        return response.model_dump()
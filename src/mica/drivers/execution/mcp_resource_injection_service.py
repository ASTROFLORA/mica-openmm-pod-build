"""
mcp_resource_injection_service.py — I09-C extraction.

Async MCP resource injection pipeline extracted from AgenticDriver.
All `self.*` references replaced with explicit parameters.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


async def inject_mcp_resources_into_query(
    user_query: str,
    *,
    config_obj: Any,
    mcp_sessions: Any,
    mcp_config: Any,
    bridge_obj: Any,
    format_lmp_context_fn: Callable[..., str],
    should_inject_institutional_fn: Callable[[str], bool],
    consult_institutional_fn: Callable[..., Any],
    format_institutional_fn: Callable[..., str],
    workspace_id: Optional[str] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Materialize MCP resources and prepend them as controlled context.

    MVP goals:
    - Deterministic policy triggers from MCP config
    - Redaction + truncation applied in the fabric layer
    - Best-effort behavior (never blocks core workflow)
    """

    if not getattr(config_obj, "mcp_enabled", False):
        return user_query, None
    if not mcp_sessions or not mcp_config:
        return user_query, None

    try:
        from ..mcp import resources_fabric as mrf  # type: ignore
    except Exception:
        return user_query, None

    def _extract_explicit_ids_from_query(query: str) -> Dict[str, List[str]]:
        # Keep deterministic and dependency-free: explicit IDs only.
        uniprot_pattern = r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5})\b"
        pdb_pattern = r"\b([0-9][A-Za-z][A-Za-z0-9]{2})\b"
        uniprot_ids = sorted({m.strip() for m in re.findall(uniprot_pattern, query or "") if m.strip()})
        pdb_ids = sorted({m.strip() for m in re.findall(pdb_pattern, query or "") if m.strip()})
        return {"uniprot_ids": uniprot_ids, "pdb_ids": pdb_ids}

    def _augment_ids_from_bridge(
        *,
        query: str,
        ids: Dict[str, List[str]],
    ) -> Tuple[Dict[str, List[str]], Dict[str, Any]]:
        """Best-effort augmentation using DLM/LMP bridge extraction + linking.

        Determinism contract:
        - This method must not perform network I/O itself.
        - If downstream bridge components do I/O in some environments, this is still
          guarded by `enable_bridge` (opt-in) and is best-effort.
        """
        meta: Dict[str, Any] = {
            "attempted": False,
            "used": False,
            "allow_api": bool(getattr(config_obj, "mcp_resources_nlp_bridge_allow_api", False)),
            "api_disabled": False,
            "threshold": float(getattr(config_obj, "bridge_confidence_threshold", 0.8) or 0.8),
            "error": None,
            "derived_uniprot_ids": [],
            "derived_pdb_ids": [],
            "derived_from": None,
        }

        if not getattr(config_obj, "enable_bridge", False):
            return ids, meta
        if not getattr(config_obj, "mcp_resources_nlp_use_bridge", False):
            return ids, meta
        if bridge_obj is None:
            return ids, meta

        meta["attempted"] = True

        try:
            # Use the bridge's own extraction + linking (can be mocked in tests).
            extracted = bridge_obj._extract_entities(query)  # type: ignore[attr-defined]

            # Linking can hit external APIs depending on EntityMapper configuration.
            # Default is deterministic/offline: temporarily disable API lookups unless allowed.
            mapper = getattr(bridge_obj, "entity_mapper", None)
            old_enable_api = None
            try:
                if mapper is not None and hasattr(mapper, "enable_api"):
                    old_enable_api = bool(getattr(mapper, "enable_api"))
                    if old_enable_api and not meta["allow_api"]:
                        setattr(mapper, "enable_api", False)
                        meta["api_disabled"] = True

                linked = bridge_obj._link_entities(extracted)  # type: ignore[attr-defined]
            finally:
                if mapper is not None and old_enable_api is not None:
                    try:
                        setattr(mapper, "enable_api", old_enable_api)
                    except Exception:
                        pass

            derived_uniprot: List[str] = []
            derived_pdb: List[str] = []

            # Prefer explicit IDs found by bridge extraction as well.
            for uid in getattr(extracted, "uniprot_ids", []) or []:
                if uid:
                    derived_uniprot.append(str(uid))
            for pid in getattr(extracted, "pdb_ids", []) or []:
                if pid:
                    derived_pdb.append(str(pid))

            # Pull KB-linked IDs from the mapper (e.g., TP53 -> P04637),
            # but only if confident enough.
            threshold = float(meta.get("threshold") or 0.8)
            for mapping in getattr(linked, "uniprot_mappings", []) or []:
                kb_id = getattr(mapping, "kb_id", None)
                conf = getattr(mapping, "confidence", 0.0)
                if kb_id and isinstance(conf, (int, float)) and float(conf) >= threshold:
                    derived_uniprot.append(str(kb_id))
            for mapping in getattr(linked, "pdb_mappings", []) or []:
                kb_id = getattr(mapping, "kb_id", None)
                conf = getattr(mapping, "confidence", 0.0)
                if kb_id and isinstance(conf, (int, float)) and float(conf) >= threshold:
                    derived_pdb.append(str(kb_id))

            merged = {
                "uniprot_ids": sorted(set((ids.get("uniprot_ids") or []) + derived_uniprot)),
                "pdb_ids": sorted(set((ids.get("pdb_ids") or []) + derived_pdb)),
            }

            meta["used"] = bool(derived_uniprot or derived_pdb)
            # Normalize derived IDs through the same regex filter.
            norm_uniprot = _extract_explicit_ids_from_query(" ".join(derived_uniprot)).get("uniprot_ids") or []
            norm_pdb = _extract_explicit_ids_from_query(" ".join(derived_pdb)).get("pdb_ids") or []
            meta["derived_uniprot_ids"] = sorted(set(norm_uniprot))
            meta["derived_pdb_ids"] = sorted(set(norm_pdb))
            meta["derived_from"] = "dlm_lmp_bridge"

            return merged, meta

        except Exception as exc:
            meta["error"] = str(exc)
            return ids, meta

    def _expand_nlp_templates_into_plan(
        *,
        explicit_ids: Dict[str, List[str]],
        max_total: int = 6,
    ) -> List[Any]:
        if not getattr(config_obj, "mcp_resources_nlp_enabled", False):
            return []
        if not isinstance(mcp_config, dict):
            return []

        out: List[Any] = []
        seen: Set[Tuple[str, str]] = set()

        for server_name, cfg in (mcp_config or {}).items():
            if not isinstance(cfg, dict):
                continue
            if not isinstance(cfg.get("resources"), dict):
                continue
            nlp_cfg = (cfg.get("resources") or {}).get("nlp_templates")
            if not isinstance(nlp_cfg, dict) or not nlp_cfg.get("enabled", False):
                continue

            max_resources = int(nlp_cfg.get("max_resources", 3) or 3)
            if max_resources <= 0:
                max_resources = 3

            uniprot_t = nlp_cfg.get("uniprot")
            pdb_t = nlp_cfg.get("pdb")

            def _format_template(t: str, *, id_value: str) -> str:
                mapping = {
                    "id": id_value,
                    "uniprot_id": id_value,
                    "accession": id_value,
                    "pdb_id": id_value,
                }
                try:
                    return str(t).format_map(mapping)
                except Exception:
                    return str(t).replace("{id}", id_value)

            if isinstance(uniprot_t, str) and uniprot_t.strip():
                for uid in explicit_ids.get("uniprot_ids", [])[:max_resources]:
                    uri = _format_template(uniprot_t, id_value=uid)
                    key = (server_name, uri)
                    if uri and key not in seen:
                        seen.add(key)
                        out.append(
                            mrf.ResourcePlanItem(
                                server=server_name,
                                uri=uri,
                                reason="nlp_template=uniprot",
                            )
                        )

            if isinstance(pdb_t, str) and pdb_t.strip():
                for pid in explicit_ids.get("pdb_ids", [])[:max_resources]:
                    uri = _format_template(pdb_t, id_value=pid)
                    key = (server_name, uri)
                    if uri and key not in seen:
                        seen.add(key)
                        out.append(
                            mrf.ResourcePlanItem(
                                server=server_name,
                                uri=uri,
                                reason="nlp_template=pdb",
                            )
                        )

            if len(out) >= max_total:
                break

        return out[:max_total]

    try:
        gateway = mrf.MCPResourceGateway(mcp_sessions=mcp_sessions, mcp_config=mcp_config)

        base_plan = gateway.plan_for_query(user_query)
        nlp_plan: List[Any] = []
        bridge_meta: Optional[Dict[str, Any]] = None
        if getattr(config_obj, "mcp_resources_nlp_enabled", False):
            explicit_ids = _extract_explicit_ids_from_query(user_query)
            explicit_ids, bridge_meta = _augment_ids_from_bridge(query=user_query, ids=explicit_ids)
            nlp_plan = _expand_nlp_templates_into_plan(explicit_ids=explicit_ids)

        # Keep order deterministic: config-triggered plan first, then NLP-derived.
        plan = list(base_plan) + [p for p in nlp_plan if p not in base_plan]

        # ── LMP pre-reasoning injection: load biological context for detected proteins ──
        lmp_context_blocks: List[str] = []
        lmp_cited_pmids: List[str] = []
        institutional_memory_block: str = ""
        institutional_memory_meta: Optional[Dict[str, Any]] = None
        _lmp_inject_ids = (
            (explicit_ids.get("uniprot_ids") or [])
            if getattr(config_obj, "mcp_resources_nlp_enabled", False)
            else _extract_explicit_ids_from_query(user_query).get("uniprot_ids", [])
        )
        for _uid in _lmp_inject_ids[:3]:  # cap at 3 proteins to bound context size
            try:
                from mica.drivers.dlm_lmp_bridge import get_bridge as _get_lmp_bridge
                _lmp_bridge = _get_lmp_bridge()
                _bio_ctx = _lmp_bridge.get_biological_context(_uid)
                if _bio_ctx:
                    _block = format_lmp_context_fn(_bio_ctx, max_chars=2000)
                    if _block:
                        lmp_context_blocks.append(_block)
                    # Extract PubMed IDs from comments for deferred deep-scan
                    _pmid_re = re.compile(r"PubMed:(\d{6,9})")
                    for _cmt_list in getattr(_bio_ctx, "comments", {}).values():
                        for _cmt_text in (_cmt_list or []):
                            lmp_cited_pmids.extend(_pmid_re.findall(str(_cmt_text)))
            except Exception:
                pass  # Never block main workflow for LMP injection

        if should_inject_institutional_fn(user_query):
            try:
                institutional_memory_meta = await consult_institutional_fn(user_query, session_id=workspace_id)
                institutional_memory_block = format_institutional_fn(
                    institutional_memory_meta,
                    query=user_query,
                )
            except Exception as exc:
                institutional_memory_meta = {"status": "error", "reason": str(exc), "query": user_query}
                institutional_memory_block = format_institutional_fn(
                    institutional_memory_meta,
                    query=user_query,
                )

        if not plan and not lmp_context_blocks and not institutional_memory_block:
            return user_query, {
                "enabled": True,
                "plan_count": 0,
                "materialized_count": 0,
            }

        # ── G5: Two-stage progressive disclosure ──────────────────
        # When enabled, inject a compact manifest (URIs + reasons) instead
        # of materializing all content.  The LLM can then drill into
        # specific resources via search_literature, load_knowledge_graph,
        # query_atom_facts, or download_pdf_to_workspace.
        _progressive = bool(getattr(config_obj, "progressive_disclosure", False))

        mats: List[Any] = []
        context: str = ""
        if _progressive and plan:
            # Stage 1: build a compact manifest listing available resources
            _manifest_lines = ["Available resources (use tools to retrieve specific content):"]
            for idx, item in enumerate(plan, 1):
                _manifest_lines.append(
                    f"  {idx}. [{item.reason}] {item.uri} (server: {item.server})"
                )
            _manifest_lines.append(
                "\nUse search_literature, load_knowledge_graph, query_atom_facts, "
                "or download_pdf_to_workspace to retrieve details you need."
            )
            context = "\n".join(_manifest_lines)
        elif plan:
            # Legacy: full materialization (pre-G5 behavior)
            mats = await gateway.materialize(
                plan,
                max_chars_per_resource=int(getattr(config_obj, "mcp_resources_max_chars_per_resource", 4000) or 4000),
                max_total_chars=int(getattr(config_obj, "mcp_resources_max_total_chars", 12000) or 12000),
            )
            context = mrf.format_resource_context(mats)

        # Build the injected query: LMP biological context FIRST, then MCP resources
        _injected_parts: List[str] = []
        if lmp_context_blocks:
            _injected_parts.append(
                "[AUTO-INJECTED BIOLOGICAL CONTEXT (LMP)]\n"
                + "\n\n".join(lmp_context_blocks)
            )
        if institutional_memory_block:
            _injected_parts.append("[AUTO-INJECTED INSTITUTIONAL MEMORY]\n" + institutional_memory_block)
        if context:
            _label = "[AVAILABLE RESOURCES MANIFEST]" if _progressive else "[AUTO-INJECTED MCP RESOURCES]"
            _injected_parts.append(_label + "\n" + context)
        if _injected_parts:
            injected_query = "\n\n".join(_injected_parts) + "\n\n[USER QUERY]\n" + user_query
        else:
            injected_query = user_query

        meta_out: Dict[str, Any] = {
            "enabled": True,
            "plan_count": len(plan),
            "materialized_count": len(mats),
            "progressive_disclosure": _progressive,
            "nlp_enabled": bool(getattr(config_obj, "mcp_resources_nlp_enabled", False)),
            "lmp_injected": len(lmp_context_blocks),
            "lmp_cited_pmids": sorted(set(lmp_cited_pmids))[:20],
            "institutional_memory": institutional_memory_meta,
            "bridge": bridge_meta,
            "resources": [
                {
                    "server": r.server,
                    "uri": r.uri,
                    "sha256": r.sha256,
                    "size_bytes": r.size_bytes,
                    "mime_type": r.mime_type,
                    "reason": r.reason,
                    "error": r.error,
                    "fetched_at": r.fetched_at,
                }
                for r in mats
            ],
        }

        return injected_query, meta_out

    except Exception as exc:
        # Never fail the main workflow for resources.
        return user_query, {
            "enabled": True,
            "error": str(exc),
        }

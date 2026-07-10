"""
Tool Selection Service — extraction from AgenticDriver

Encapsulates effective-tool routing and runtime capability snapshot composition.
"""

import os

from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from mica.agentic.tool_capability_registry import infer_lmp_state_query_tool_names
from mica_q.adapters.sandbox_adapter import query_targets_mica_q_sandbox


class ToolSelectionService:
    """Owns tool selection and capability snapshot composition logic."""

    @staticmethod
    def _resolve_public_tool_allowlist(config: Any) -> List[str]:
        configured = getattr(config, "public_tool_allowlist", None)
        values: List[str] = []
        if isinstance(configured, (list, tuple, set)):
            values.extend(str(item or "").strip() for item in configured)
        elif isinstance(configured, str):
            values.extend(str(item or "").strip() for item in configured.split(","))

        env_value = str(os.getenv("MICA_PUBLIC_TOOL_ALLOWLIST", "") or "").strip()
        if env_value:
            values.extend(str(item or "").strip() for item in env_value.split(","))

        normalized = []
        seen = set()
        for item in values:
            if not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized

    def select_effective_mica_tools_for_query(
        self,
        *,
        query: str,
        mica_tools: Sequence[Dict[str, Any]],
        config: Any,
        mcp_tools: Sequence[Dict[str, Any]],
        toolkg_registry: Any,
        route_card_service: Any,
        filter_tools_fn: Callable[[Sequence[Dict[str, Any]], Any], List[Dict[str, Any]]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Any]:
        effective_mica_tools = filter_tools_fn(mica_tools, config)
        public_tool_allowlist = self._resolve_public_tool_allowlist(config)
        allowlisted_tool_names: Set[str] = set(public_tool_allowlist)
        if allowlisted_tool_names:
            effective_mica_tools = [
                tool
                for tool in list(effective_mica_tools)
                if str(tool.get("function", tool).get("name") or "").strip() in allowlisted_tool_names
            ]
        visible_tool_map: Dict[str, Dict[str, Any]] = {}
        for tool in list(effective_mica_tools):
            tool_name = str(tool.get("function", tool).get("name") or "").strip()
            if tool_name and tool_name not in visible_tool_map:
                visible_tool_map[tool_name] = tool

        visible_tool_names = sorted(
            {
                str(tool.get("function", tool).get("name") or "").strip()
                for tool in effective_mica_tools
                if str(tool.get("function", tool).get("name") or "").strip()
            }
        )
        effective_mica_tools = list(effective_mica_tools)
        routing_meta: Dict[str, Any] = {
            "routing_strategy": "full_surface_fallback",
            "routed": False,
            "degraded": False,
            "visible_tool_names": visible_tool_names,
            "selected_tool_names": visible_tool_names,
            "planned_tool_names": [],
            "intent_tags": [],
            "routing_hint": "",
        }
        if public_tool_allowlist:
            routing_meta["public_tool_allowlist"] = list(public_tool_allowlist)
            routing_meta["routing_strategy"] = "explicit_public_tool_allowlist"

        # APV-11: product intent precedes ToolKG / scientific routing.
        from mica.drivers.routing.product_intent_classifier import classify_product_intent

        product_decision = classify_product_intent(query)
        routing_meta["product_intent_decision"] = product_decision
        routing_meta["product_intent"] = product_decision.product_intent
        routing_meta["driver_constraints"] = product_decision.constraints.to_dict()
        if product_decision.constraints.no_tool or product_decision.product_intent == "product_interview":
            routing_meta["routing_strategy"] = "product_constitution_no_tool"
            routing_meta["planned_tool_names"] = []
            routing_meta["selected_tool_names"] = []
            routing_meta["routing_hint"] = (
                "Product constitution: no_tool / product_interview — empty tool surface."
            )
            route_card = route_card_service.build_route_card(query=query, routing_meta=routing_meta)
            routing_meta["route_card"] = route_card
            routing_meta["planned_tool_names"] = list(route_card.get("planned_tools") or [])
            return [], routing_meta, toolkg_registry

        explicit_tool_names = route_card_service.extract_explicit_tool_mentions(
            query,
            visible_tool_names=visible_tool_names,
        )
        if explicit_tool_names:
            routing_meta["explicit_tool_names"] = explicit_tool_names
            routing_meta["planned_tool_names"] = list(dict.fromkeys(explicit_tool_names))
            routing_meta["routing_hint"] = (
                "Respect explicit tool mentions in the user query first: "
                + ", ".join(explicit_tool_names)
            )

        prefer_mica_q_sandbox = bool(
            query_targets_mica_q_sandbox(query)
            and "run_mica_q_sandbox" in visible_tool_names
        )
        if prefer_mica_q_sandbox:
            routing_meta["planned_tool_names"] = list(
                dict.fromkeys(list(routing_meta.get("planned_tool_names") or []) + ["run_mica_q_sandbox"])
            )
            routing_meta["routing_hint"] = (
                (str(routing_meta.get("routing_hint") or "") + "\n")
                if str(routing_meta.get("routing_hint") or "").strip()
                else ""
            ) + "Prefer the canonical MICA-Q sandbox surface for sandbox/code/dataset work."

        if not str(query or "").strip():
            return effective_mica_tools, routing_meta, toolkg_registry

        if public_tool_allowlist:
            routing_meta["routed"] = True
            routing_meta["degraded"] = False
            routing_meta["visible_tool_names"] = visible_tool_names
            routing_meta["selected_tool_names"] = list(visible_tool_names)
            routing_meta["planned_tool_names"] = list(visible_tool_names)
            routing_meta["routing_hint"] = (
                "Explicit public tool allowlist active. "
                "Only these public tools are visible/invocable for this run: "
                + ", ".join(visible_tool_names)
            )
            routing_meta["route_card"] = route_card_service.build_route_card(query=query, routing_meta=routing_meta)
            return effective_mica_tools, routing_meta, toolkg_registry

        current_registry = toolkg_registry
        try:
            from mica.toolkg.router import QueryIntentRouter

            if current_registry is None:
                from mica.toolkg.schema import ToolRegistry

                toolset_by_server: Dict[str, List[Dict[str, Any]]] = {}
                for mcp_tool in mcp_tools:
                    server = str(mcp_tool.get("server", "unknown") or "unknown").strip()
                    server_key = f"mcp_{server}"
                    raw_name = str(mcp_tool.get("name") or "").strip()
                    prefix = f"{server}_"
                    if raw_name.startswith(prefix):
                        raw_name = raw_name[len(prefix):]
                    toolset_by_server.setdefault(server_key, []).append(
                        {
                            "name": raw_name,
                            "description": mcp_tool.get("description", ""),
                            "inputSchema": mcp_tool.get("input_schema", {}),
                        }
                    )
                current_registry = ToolRegistry.from_mcp_list_tools(toolset_by_server)

            router = QueryIntentRouter(current_registry)
            plan = router.route_with_fallback(query, available_artifacts=["query_string"])

            routing_meta["intent_tags"] = list(plan.intent_tags or [])
            planned_tool_names = [
                str(getattr(ptc, "tool_name", "") or "").strip()
                for ptc in (plan.planned_tools or [])
                if str(getattr(ptc, "tool_name", "") or "").strip()
            ]
            routing_meta["planned_tool_names"] = list(dict.fromkeys(explicit_tool_names + planned_tool_names))
            hinted_tool_names = list(
                infer_lmp_state_query_tool_names(
                    query,
                    available_tool_names=visible_tool_names,
                )
            )
            if hinted_tool_names:
                routing_meta["planned_tool_names"] = list(
                    dict.fromkeys(
                        list(routing_meta.get("planned_tool_names") or [])
                        + hinted_tool_names
                    )
                )
            if plan.planned_tools:
                explained = router.explain(plan)
                routing_meta["routing_hint"] = (
                    f"Respect explicit tool mentions in the user query first: {', '.join(explicit_tool_names)}\n"
                    f"{explained}"
                    if explicit_tool_names else explained
                )

            cap_to_mica_tools: Dict[str, Set[str]] = {
                "literature_search": {"search_literature", "run_dlm_scan", "run_deep_research", "run_bibliotecario_scan", "generate_report"},
                "protein_structure_download": {"search_protein", "resolve_pdb", "analyze_structure", "load_knowledge_graph", "visualize_molecule"},
                "protein_metadata": {"search_protein", "search_protein_metadata", "resolve_entity"},
                "protein_interaction_network": {"load_knowledge_graph", "get_citations_and_references", "run_bibliotecario_scan"},
                "trajectory_analysis": {"analyze_structure", "visualize_molecule", "execute_in_sandbox"},
                "md_simulation": {"execute_in_sandbox", "sandbox_session_status", "terminate_sandbox_session", "analyze_structure"},
                "custom_computation": {"run_mica_q_sandbox", "sandbox_session_status", "terminate_sandbox_session", "run_driver_delegated_checkpoint"},
                "code_execution": {"run_mica_q_sandbox", "sandbox_session_status", "terminate_sandbox_session"},
                "dataset_processing": {"run_mica_q_sandbox", "sandbox_session_status", "terminate_sandbox_session"},
                "data_processing": {"run_mica_q_sandbox", "sandbox_session_status", "terminate_sandbox_session"},
                "ml_prediction": {"execute_in_sandbox", "sandbox_session_status", "terminate_sandbox_session"},
            }
            selected_names: Set[str] = set()
            for ptc in (plan.planned_tools or []):
                tool_name = str(getattr(ptc, "tool_name", "") or "").strip()
                capability_id = str(getattr(ptc, "capability_id", "") or "").strip()
                if tool_name:
                    selected_names.add(tool_name)
                if capability_id:
                    selected_names.update(cap_to_mica_tools.get(capability_id, set()))
            for cap in (plan.intent_tags or []):
                cap_id = str(cap or "").strip()
                if cap_id:
                    selected_names.update(cap_to_mica_tools.get(cap_id, set()))
            selected_names.update(hinted_tool_names)
            selected_names.update(explicit_tool_names)
            if prefer_mica_q_sandbox:
                selected_names.update({"run_mica_q_sandbox", "sandbox_session_status", "terminate_sandbox_session"})
            selected_names |= {"search_protein", "search_literature", "add_to_workspace", "visualize_molecule"}

            routed_tools = [
                tool for tool in effective_mica_tools
                if tool.get("function", tool).get("name") in selected_names
            ]
            if routed_tools:
                effective_mica_tools = routed_tools
                routing_meta["routed"] = len(routed_tools) < len(mica_tools)
                routing_meta["routing_strategy"] = "toolkg_filtered" if routing_meta["routed"] else "toolkg_full_surface"
                routing_meta["selected_tool_names"] = sorted(
                    {
                        str(tool.get("function", tool).get("name") or "").strip()
                        for tool in routed_tools
                        if str(tool.get("function", tool).get("name") or "").strip()
                    }
                )
        except Exception as toolkg_err:
            routing_meta["degraded"] = True
            routing_meta["routing_error"] = str(toolkg_err)

        route_card = route_card_service.build_route_card(query=query, routing_meta=routing_meta)
        routing_meta["route_card"] = route_card
        routing_meta["planned_tool_names"] = list(route_card.get("planned_tools") or routing_meta.get("planned_tool_names") or [])
        selected_tool_names = [
            str(item).strip()
            for item in list(routing_meta.get("selected_tool_names") or [])
            if str(item).strip()
        ]
        route_visible_planned_tool_names = [
            name
            for name in list(routing_meta.get("planned_tool_names") or [])
            if str(name).strip() in visible_tool_map
        ]
        missing_visible_planned_tool_names = [
            name
            for name in route_visible_planned_tool_names
            if str(name).strip() not in selected_tool_names
        ]
        if missing_visible_planned_tool_names:
            effective_mica_tools = list(effective_mica_tools) + [
                visible_tool_map[str(name).strip()]
                for name in missing_visible_planned_tool_names
            ]
            routing_meta["selected_tool_names"] = list(
                dict.fromkeys(selected_tool_names + missing_visible_planned_tool_names)
            )
        route_invisible_planned_tool_names = [
            str(name).strip()
            for name in list(routing_meta.get("planned_tool_names") or [])
            if str(name).strip() and str(name).strip() not in visible_tool_map
        ]
        if route_invisible_planned_tool_names:
            routing_meta["planned_but_invisible_tool_names"] = route_invisible_planned_tool_names
        if route_card.get("required") and not route_card.get("planned_tools"):
            routing_meta["degraded"] = True
            routing_meta.setdefault(
                "routing_error",
                route_card.get("no_tool_justification") or "route_card_missing_planned_tools",
            )
        if route_card.get("required") and route_invisible_planned_tool_names:
            routing_meta["degraded"] = True
            routing_meta.setdefault(
                "routing_error",
                "route_card_planned_tools_not_visible",
            )

        return effective_mica_tools, routing_meta, current_registry

    def compose_runtime_capability_snapshot(
        self,
        *,
        transport_path: str,
        routing_meta: Dict[str, Any],
        spawn_tools: Sequence[Dict[str, Any]],
        configured_providers: Sequence[str],
        provider_error: str,
        storage_snapshot: Dict[str, Any],
        langgraph_available: bool,
        graph_compiled: bool,
        mcp_enabled: bool,
        mcp_available: bool,
        bridge_available: bool,
        bridge_present: bool,
        security_stack_available: bool,
        use_checkpointing: bool,
        sqlite_checkpointer_available: bool,
    ) -> Dict[str, Any]:
        from mica.agentic.runtime_authority import resolve_runtime_authority
        from mica.agentic.tool_capability_registry import build_tool_capability_matrix

        capabilities_unavailable: List[str] = []
        degradation_flags: List[str] = []
        fallbacks_used: List[str] = []

        routing_runtime_authority = (
            routing_meta.get("runtime_authority")
            if isinstance(routing_meta.get("runtime_authority"), dict)
            else {}
        )
        visible_tool_names = list(
            routing_runtime_authority.get("visible_tools")
            or routing_meta.get("visible_tool_names")
            or []
        )
        invocable_tool_names = list(
            routing_runtime_authority.get("invocable_public_tools")
            or routing_meta.get("selected_tool_names")
            or visible_tool_names
        )
        internal_spawn_tools = sorted(
            {
                str(tool.get("function", tool).get("name") or "").strip()
                for tool in spawn_tools
                if str(tool.get("function", tool).get("name") or "").strip()
            }
        )

        degraded_providers: List[str] = []
        storage = storage_snapshot

        if transport_path != "langgraph":
            fallbacks_used.append("legacy_or_fallback_transport")
            degradation_flags.append("noncanonical_transport_path")
            if not langgraph_available or not graph_compiled:
                capabilities_unavailable.append("langgraph_state_graph")

        if not mcp_enabled or not mcp_available:
            capabilities_unavailable.append("mcp_tooling")

        if not bridge_available or not bridge_present:
            capabilities_unavailable.append("dlm_lmp_bridge")

        if not security_stack_available:
            capabilities_unavailable.append("security_governance_stack")

        if use_checkpointing and not sqlite_checkpointer_available:
            capabilities_unavailable.append("sqlite_checkpointer")

        if not configured_providers:
            capabilities_unavailable.append("llm_provider")
            degraded_providers.append("llm_provider_unconfigured")
            degradation_flags.append("provider_unconfigured")
        elif provider_error:
            degradation_flags.append("provider_registry_probe_failed")

        storage_status = str(storage.get("status") or "").lower()
        if storage_status in {"degraded", "failed"}:
            degradation_flags.append("artifact_storage_degraded")
            fallbacks_used.extend(list(storage.get("fallback_flags") or []))
        if storage_status == "failed":
            capabilities_unavailable.append("cloud_artifact_backend")
            degradation_flags.append("artifact_storage_startup_failed")

        if routing_meta.get("degraded"):
            degradation_flags.append("tool_routing_degraded")

        if capabilities_unavailable:
            degradation_flags.append("capability_loss_detected")

        route_card = routing_meta.get("route_card") if isinstance(routing_meta.get("route_card"), dict) else {}
        runtime_authority = resolve_runtime_authority(
            route_card=route_card,
            visible_tool_names=visible_tool_names,
            selected_tool_names=invocable_tool_names,
            internal_spawn_tools=internal_spawn_tools,
            configured_providers=list(configured_providers),
        ).to_dict()
        if routing_runtime_authority:
            runtime_authority.update(
                {
                    key: value
                    for key, value in routing_runtime_authority.items()
                    if value not in (None, "", [], {})
                }
            )
        visible_tool_names = list(runtime_authority.get("visible_tools") or visible_tool_names)
        invocable_tool_names = list(runtime_authority.get("invocable_public_tools") or invocable_tool_names)
        if not route_card and str(runtime_authority.get("route_card_id") or "").strip():
            route_card = {"route_card_id": str(runtime_authority.get("route_card_id") or "").strip()}

        capability_envelope = {
            "tools_visible": visible_tool_names,
            "tools_invocable": invocable_tool_names,
            "internal_spawn_tools": internal_spawn_tools,
            "tool_routing": {
                "strategy": routing_meta.get("routing_strategy") or "full_surface_fallback",
                "planned_tools": list(routing_meta.get("planned_tool_names") or []),
                "intent_tags": list(routing_meta.get("intent_tags") or []),
                "degraded": bool(routing_meta.get("degraded")),
                "route_card": route_card,
                "runtime_authority": runtime_authority,
                "no_tool_justification": str((route_card.get("no_tool_justification") or "")).strip(),
            },
            "tool_capability_matrix": build_tool_capability_matrix(visible_tool_names + internal_spawn_tools),
            "providers": {
                "configured": list(configured_providers),
                "degraded": degraded_providers,
                "probe_error": provider_error,
            },
            "storage": storage,
            "fallback_flags": sorted(set(str(flag) for flag in fallbacks_used if flag)),
        }

        return {
            "transport_path": transport_path,
            "capabilities_unavailable": sorted(set(capabilities_unavailable)),
            "degradation_flags": sorted(set(degradation_flags)),
            "fallbacks_used": sorted(set(fallbacks_used)),
            "providers": capability_envelope["providers"],
            "storage": storage,
            "capability_envelope": capability_envelope,
        }

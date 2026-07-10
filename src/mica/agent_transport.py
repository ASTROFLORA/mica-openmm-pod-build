"""Execution transport layer for agentic workers."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Tuple

from .config.agent_registry import AgentEndpointConfig, AgentRegistry, BackendType
from .model_runtime import ModelAdapterFactory, ModelDescriptor
from .model_runtime.backends import DEFAULT_VERTEX_DRIVER_MODEL, RouterRuntimeBackend
from .vertex_bridge import VertexMemoryBridge, VertexRemoteAgent

# 🔥 FIX: Import circular prevention
if TYPE_CHECKING:
    from .drivers.agentic_driver import AgenticDriver

logger = logging.getLogger(__name__)

FallbackExecutor = Callable[[str, str], Awaitable[Dict[str, object]]]


class TransportLayer:
    """Routes worker invocations to the appropriate backend."""

    def __init__(
        self,
        registry: AgentRegistry,
        fallback_executor: FallbackExecutor,
        memory_bridge: Optional[VertexMemoryBridge] = None,
        driver: Optional['AgenticDriver'] = None,  # 🔥 FIX: Referencia al driver
    ) -> None:
        self.registry = registry
        self.fallback_executor = fallback_executor
        self.memory_bridge = memory_bridge or VertexMemoryBridge()
        self._vertex_cache: Dict[str, VertexRemoteAgent] = {}
        self.driver = driver  # 🔥 FIX: Guardar referencia para acceder a MCP tools


    async def execute_worker(self, worker: str, prompt: str, session_id: str = "default") -> Dict[str, object]:
        """Execute a worker by name with a prompt. Convenience method for DAGExecutor compatibility."""
        return await self.execute(worker, prompt, session_id)

    async def execute(self, worker: str, prompt: str, session_id: str) -> Dict[str, object]:
        config = self.registry.get(worker)
        if config is None:
            logger.warning("Worker '%s' not registered; using fallback", worker)
            return await self.fallback_executor(worker, prompt)

        if config.backend == BackendType.SIMULATED:
            return await self.fallback_executor(worker, prompt)

        if config.backend == BackendType.LOCAL_HTTP:
            return await self._call_local_http(config, prompt)

        if config.backend == BackendType.LOCAL_MCP:
            return await self._call_local_mcp(config, prompt)

        if config.backend == BackendType.VERTEX_AGENT:
            return await self._call_vertex_agent(config, prompt, session_id)
        
        # Native LLM backends
        if config.backend == BackendType.OPENAI_NATIVE:
            return await self._call_openai_native(config, prompt, session_id=session_id)
        
        if config.backend == BackendType.CLAUDE_NATIVE:
            return await self._call_claude_native(config, prompt, session_id=session_id)
        
        if config.backend == BackendType.GEMINI_NATIVE:
            return await self._call_gemini_native(config, prompt)
        
        # 🎯 FASE 1: Router backend with intelligent cost optimization
        if config.backend == BackendType.ROUTER_NATIVE:
            return await self._call_router_native(config, prompt)

        logger.warning("Worker '%s' has unknown backend '%s'", worker, config.backend)
        return await self.fallback_executor(worker, prompt)

    def _default_model_for_family(self, provider_family: str) -> str:
        family = str(provider_family or "").strip().lower()
        if family == "anthropic_native":
            return "claude-sonnet-4-20250514"
        if family == "vertex_claude":
            return os.getenv("MICA_VERTEX_CLAUDE_MODEL") or os.getenv("VERTEX_CLAUDE_MODEL") or "claude-sonnet-4-20250514"
        if family in {"google_gemini_api", "gemini_native"}:
            return os.getenv("MICA_GOOGLE_MODEL") or os.getenv("GOOGLE_MODEL") or "gemini-2.5-flash"
        if family == "vertex_gemini":
            return os.getenv("MICA_VERTEX_MODEL") or os.getenv("VERTEX_MODEL") or DEFAULT_VERTEX_DRIVER_MODEL
        if family == "openrouter_chat":
            return os.getenv("MICA_OPENROUTER_MODEL") or os.getenv("OPENROUTER_MODEL") or "openai/gpt-4o"
        if family == "nvidia_openai_compatible":
            return os.getenv("MICA_NVIDIA_MODEL") or os.getenv("NVIDIA_MODEL") or "meta/llama-3.1-70b-instruct"
        return os.getenv("MICA_OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o"

    def _backend_default_family(self, backend: BackendType) -> str:
        if backend == BackendType.CLAUDE_NATIVE:
            return "anthropic_native"
        if backend == BackendType.GEMINI_NATIVE:
            return "google_gemini_api"
        if backend == BackendType.OPENAI_NATIVE:
            return "openai_chat"
        if backend == BackendType.ROUTER_NATIVE:
            return "anthropic_native"
        return "openai_chat"

    def _descriptor_from_worker_config(self, config: AgentEndpointConfig) -> ModelDescriptor:
        extras = dict(config.extras or {})
        provider_family = str(extras.get("provider_family") or self._backend_default_family(config.backend)).strip().lower()
        model_id = str(config.llm_model or self._default_model_for_family(provider_family)).strip()
        location = extras.get("location")
        project_id = extras.get("project_id") or os.getenv("MICA_VERTEX_PROJECT_ID") or os.getenv("VERTEX_PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        return ModelDescriptor(
            provider_family=provider_family,
            model_id=model_id,
            location=str(location).strip() if location else None,
            project_id=str(project_id).strip() if project_id else None,
            extras=extras,
        )

    def _resolve_mcp_tool_target(self, tool_name: str) -> Tuple[Optional[str], Optional[str]]:
        """Resolve MCP server/base tool name from a unified tool name."""
        if not tool_name:
            return None, None

        # Prefer authoritative mapping from driver's MCP tool catalog.
        if self.driver and hasattr(self.driver, "mcp_tools"):
            for tool in getattr(self.driver, "mcp_tools", []) or []:
                if not isinstance(tool, dict):
                    continue
                if tool.get("name") != tool_name:
                    continue
                server = tool.get("server")
                if not isinstance(server, str) or not server:
                    continue
                base = tool_name
                prefix = f"{server}_"
                if tool_name.startswith(prefix):
                    base = tool_name[len(prefix):]
                return server, base

        # Fallback heuristic: server_tool
        if "_" in tool_name:
            server, base = tool_name.split("_", 1)
            if server and base:
                return server, base

        return None, None

    async def _execute_native_tool_calls(
        self,
        *,
        tool_calls: List[Dict[str, Any]],
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """Execute model-requested tool calls through the driver MCP boundary."""
        results: List[Dict[str, Any]] = []

        for call in tool_calls:
            call_id = call.get("id")
            name = str(call.get("name") or "")
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}

            if not self.driver:
                results.append(
                    {
                        "id": call_id,
                        "name": name,
                        "ok": False,
                        "error": "driver_unavailable",
                        "result": None,
                    }
                )
                continue

            server_name, tool_base = self._resolve_mcp_tool_target(name)
            if not server_name or not tool_base:
                results.append(
                    {
                        "id": call_id,
                        "name": name,
                        "ok": False,
                        "error": "tool_resolution_failed",
                        "result": None,
                    }
                )
                continue

            try:
                tool_result = await self.driver.call_mcp_tool(
                    server_name=server_name,
                    tool_name=tool_base,
                    arguments=arguments,
                    session_id=session_id,
                )
                results.append(
                    {
                        "id": call_id,
                        "name": name,
                        "server": server_name,
                        "tool": tool_base,
                        "ok": bool(tool_result.get("success")),
                        "result": tool_result,
                    }
                )
            except Exception as exc:  # pragma: no cover - network/runtime dependent
                results.append(
                    {
                        "id": call_id,
                        "name": name,
                        "server": server_name,
                        "tool": tool_base,
                        "ok": False,
                        "error": str(exc),
                        "result": None,
                    }
                )

        return results

    def _build_tool_loop_prompt(
        self,
        *,
        original_prompt: str,
        last_answer: Optional[str],
        tool_results: List[Dict[str, Any]],
    ) -> str:
        payload = json.dumps(tool_results, ensure_ascii=False, default=str)
        last_answer_block = f"\nPrevious assistant draft:\n{last_answer}\n" if last_answer else ""
        return (
            f"{original_prompt}\n"
            f"{last_answer_block}\n"
            "Tool execution results (JSON):\n"
            f"{payload}\n\n"
            "Use these tool results to continue reasoning. "
            "If additional tools are required, call them. "
            "If enough evidence is available, return the final answer."
        )

    async def _run_openai_tool_loop(
        self,
        *,
        prompt: str,
        system_prompt: str,
        tools: List[Dict[str, Any]],
        worker_name: str,
        session_id: str,
        backend: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Run the agentic tool-call loop for OpenAI.

        Delegates to the shared model-runtime backend, which in turn uses the
        canonical AgenticLoop provider contract for tool dispatch and message
        history.

        Falls back to the legacy prompt-rebuild loop if the new backend is
        unavailable (e.g. missing openai package).
        """
        # ── New path: proper agentic loop via unified model runtime ─────
        try:
            if backend is None:
                backend = ModelAdapterFactory.build(
                    ModelDescriptor(
                        provider_id="openai",
                        provider_family="openai_chat",
                        model_id=self._default_model_for_family("openai_chat"),
                    )
                )
            if backend.available and tools:
                messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]

                async def _tool_executor(tool_name: str, call_id: str, arguments: dict) -> Any:
                    """Bridge invoke_with_tools → driver.call_mcp_tool."""
                    results = await self._execute_native_tool_calls(
                        tool_calls=[{"id": call_id, "name": tool_name, "arguments": arguments}],
                        session_id=session_id,
                    )
                    r = results[0] if results else {"ok": False, "error": "no result"}
                    return r.get("result") or {"error": r.get("error", "unknown")}

                result = await backend.invoke_with_tools(
                    messages=messages,
                    tools=tools,
                    tool_executor=_tool_executor,
                    system_prompt=system_prompt,
                    max_iterations=10,
                    metadata={"worker": worker_name, "session_id": session_id},
                )

                return {
                    "ok": result.get("status") in ("SUCCESS", "MAX_ITERATIONS"),
                    "answer": result.get("response"),
                    "tool_runs": result.get("tool_calls_made", []),
                    "usage": result.get("usage"),
                    "iterations": result.get("iterations"),
                    "backend_used": "governed",  # F-2 (MAD Critic): AP-003
                }
        except Exception as exc:
            logger.warning("invoke_with_tools fallback for '%s': %s", worker_name, exc)

        # ── Legacy path: prompt-rebuild loop (original behavior) ────────
        # AP-003: mark result with backend_used so callers can distinguish
        from .communication.openai_client import call_openai

        logger.warning(
            "⚠️ Entering legacy fallback path for '%s' — result will be marked backend_used=legacy_fallback",
            worker_name,
        )

        rounds = 0
        max_rounds = 3
        final_answer: Optional[str] = None
        final_latency: Optional[float] = None
        executed_tools: List[Dict[str, Any]] = []
        current_prompt = prompt

        while rounds < max_rounds:
            rounds += 1
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: call_openai(
                    current_prompt,
                    system=system_prompt,
                    max_tokens=2000,
                    tools=tools,
                    tool_choice="auto",
                    model=getattr(backend, "model", None),
                ),
            )

            if not result.get("ok"):
                logger.error("OpenAI call failed for '%s': %s", worker_name, result.get("error"))
                return {"ok": False, "error": result.get("error"), "backend_used": "legacy_fallback"}

            final_answer = result.get("answer")
            final_latency = result.get("latency_s")
            tool_calls = result.get("tool_calls") or []

            if not tool_calls:
                break

            tool_results = await self._execute_native_tool_calls(
                tool_calls=tool_calls,
                session_id=session_id,
            )
            executed_tools.extend(tool_results)
            current_prompt = self._build_tool_loop_prompt(
                original_prompt=prompt,
                last_answer=final_answer,
                tool_results=tool_results,
            )

        return {
            "ok": True,
            "answer": final_answer,
            "latency_s": final_latency,
            "tool_runs": executed_tools,
            "backend_used": "legacy_fallback",
        }

    async def _run_claude_tool_loop(
        self,
        *,
        prompt: str,
        system_prompt: str,
        tools: List[Dict[str, Any]],
        worker_name: str,
        session_id: str,
        backend: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Run the agentic tool-call loop for Claude.

        Delegates to the shared model-runtime backend, which preserves the same
        provider contract used by the main AgenticLoop. Falls back to the
        prompt-rebuild loop only if the unified backend is unavailable.
        """
        # ── New path: proper agentic loop ───────────────────────────────
        try:
            if backend is None:
                backend = ModelAdapterFactory.build(
                    ModelDescriptor(
                        provider_id="anthropic",
                        provider_family="anthropic_native",
                        model_id=self._default_model_for_family("anthropic_native"),
                    )
                )
            if backend.available and tools:
                messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]

                async def _tool_executor(tool_name: str, call_id: str, arguments: dict) -> Any:
                    results = await self._execute_native_tool_calls(
                        tool_calls=[{"id": call_id, "name": tool_name, "arguments": arguments}],
                        session_id=session_id,
                    )
                    r = results[0] if results else {"ok": False, "error": "no result"}
                    return r.get("result") or {"error": r.get("error", "unknown")}

                result = await backend.invoke_with_tools(
                    messages=messages,
                    tools=tools,
                    tool_executor=_tool_executor,
                    system_prompt=system_prompt,
                    max_iterations=10,
                    metadata={"worker": worker_name, "session_id": session_id},
                )

                return {
                    "ok": result.get("status") in ("SUCCESS", "MAX_ITERATIONS"),
                    "answer": result.get("response"),
                    "tool_runs": result.get("tool_calls_made", []),
                    "usage": result.get("usage"),
                    "iterations": result.get("iterations"),
                    "backend_used": "governed",
                }
        except Exception as exc:
            logger.warning("invoke_with_tools fallback for '%s': %s", worker_name, exc)

        # ── Legacy path ─────────────────────────────────────────────────
        from .communication.anthropic_client import call_claude

        rounds = 0
        max_rounds = 3
        final_answer: Optional[str] = None
        final_latency: Optional[float] = None
        executed_tools: List[Dict[str, Any]] = []
        current_prompt = prompt

        while rounds < max_rounds:
            rounds += 1
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: call_claude(
                    current_prompt,
                    system=system_prompt,
                    max_tokens=4000,
                    tools=tools if tools else None,
                    model=getattr(backend, "model", None),
                ),
            )

            if not result.get("ok"):
                logger.error("Claude call failed for '%s': %s", worker_name, result.get("error"))
                return {"ok": False, "error": result.get("error")}

            final_answer = result.get("answer")
            final_latency = result.get("latency_s")
            tool_calls = result.get("tool_calls") or []

            if not tool_calls:
                break

            tool_results = await self._execute_native_tool_calls(
                tool_calls=tool_calls,
                session_id=session_id,
            )
            executed_tools.extend(tool_results)
            current_prompt = self._build_tool_loop_prompt(
                original_prompt=prompt,
                last_answer=final_answer,
                tool_results=tool_results,
            )

        return {
            "ok": True,
            "answer": final_answer,
            "latency_s": final_latency,
            "tool_runs": executed_tools,
        }

    async def _call_local_http(
        self,
        config: AgentEndpointConfig,
        prompt: str,
    ) -> Dict[str, object]:
        if not config.is_local_http:
            logger.warning("HTTP backend for '%s' misconfigured", config.name)
            return await self.fallback_executor(config.name, prompt)

        import httpx

        endpoint = config.local_endpoint.rstrip("/") + "/execute"
        payload = {"prompt": prompt}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
        except Exception as exc:  # pragma: no cover - depends on runtime
            logger.error("Local HTTP worker '%s' failed: %s", config.name, exc)
            return await self.fallback_executor(config.name, prompt)

        data = response.json()
        return {
            "status": "SUCCESS",
            "worker": config.name,
            "backend": "local_http",
            "response": data,
        }

    async def _call_local_mcp(
        self,
        config: AgentEndpointConfig,
        prompt: str,
    ) -> Dict[str, object]:
        if not config.is_local_mcp:
            logger.warning("MCP backend for '%s' misconfigured", config.name)
            return await self.fallback_executor(config.name, prompt)

        # Launch MCP tool via subprocess using FastMCP command line.
        process = await asyncio.create_subprocess_shell(
            config.mcp_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                "MCP command failed for '%s': %s", config.name, stderr.decode("utf-8", "ignore")
            )
            return await self.fallback_executor(config.name, prompt)

        return {
            "status": "SUCCESS",
            "worker": config.name,
            "backend": "local_mcp",
            "stdout": stdout.decode("utf-8", "ignore"),
        }

    async def _call_vertex_agent(
        self,
        config: AgentEndpointConfig,
        prompt: str,
        session_id: str,
    ) -> Dict[str, object]:
        agent = self._vertex_cache.get(config.name)
        if agent is None:
            agent = VertexRemoteAgent(
                name=config.name,
                agent_card_url=config.agent_card_url or "",
                description=config.description,
            )
            self._vertex_cache[config.name] = agent

        if not agent.available:
            logger.warning(
                "Vertex agent '%s' unavailable (%s); falling back",
                config.name,
                agent.import_error,
            )
            return await self.fallback_executor(config.name, prompt)

        metadata = {"session_id": session_id}
        if self.memory_bridge.available and config.memory_scope:
            memory_snapshot = self.memory_bridge.get(session_id, config.memory_scope)
            metadata["memory_snapshot"] = memory_snapshot

        result = await agent.invoke(prompt, metadata=metadata)

        if self.memory_bridge.available and config.memory_scope:
            snapshot = {
                "prompt": prompt,
                "result": result.get("raw_result"),
            }
            self.memory_bridge.set(session_id, config.memory_scope, snapshot)

        return {
            "status": "SUCCESS",
            "worker": config.name,
            "backend": "vertex_agent",
            "result": result,
        }
    
    async def _call_openai_native(
        self,
        config: AgentEndpointConfig,
        prompt: str,
        session_id: str = "default",
    ) -> Dict[str, object]:
        """Call OpenAI using existing openai_client."""
        descriptor = self._descriptor_from_worker_config(config)
        adapter = ModelAdapterFactory.build(descriptor)
        system_prompt = config.system_prompt or "You are a helpful scientific assistant specializing in biomolecular analysis."
        
        # 🔥 CRITICAL: Get MCP tools from driver in OpenAI format
        mcp_tools = []
        if self.driver and hasattr(self.driver, 'get_mcp_tools_for_openai'):
            mcp_tools = self.driver.get_mcp_tools_for_openai()
            if mcp_tools:
                logger.info(f"🔥 Pasando {len(mcp_tools)} herramientas MCP a OpenAI")

        result = await self._run_openai_tool_loop(
            prompt=prompt,
            system_prompt=system_prompt,
            tools=mcp_tools,
            worker_name=config.name,
            session_id=session_id,
            backend=adapter,
        )
        
        if not result.get("ok"):
            logger.error(f"OpenAI call failed for '{config.name}': {result.get('error')}")
            return await self.fallback_executor(config.name, prompt)
        
        response_data = {
            "status": "SUCCESS",
            "worker": config.name,
            "backend": "openai_native",
            "response": result.get("answer"),
            "latency_s": result.get("latency_s"),
            "provider": descriptor.provider_family,
            "model": descriptor.model_id,
        }

        tool_runs = result.get("tool_runs") or []
        if tool_runs:
            response_data["tool_runs"] = tool_runs
            response_data["tool_calls"] = [r.get("name") for r in tool_runs if isinstance(r, dict)]
            logger.info("🔥 OpenAI ejecutó %d herramientas MCP", len(tool_runs))
        
        return response_data
    
    async def _call_claude_native(
        self,
        config: AgentEndpointConfig,
        prompt: str,
        session_id: str = "default",
    ) -> Dict[str, object]:
        """
        🔥 ENHANCED: Call Claude usando anthropic_client CON herramientas MCP.
        
        Esta es la función CRÍTICA que permite a Claude usar MCP tools.
        """
        descriptor = self._descriptor_from_worker_config(config)
        adapter = ModelAdapterFactory.build(descriptor)
        system_prompt = config.system_prompt or "You are a helpful scientific assistant specializing in biomolecular analysis."
        
        # 🔥 FIX CRÍTICO: Obtener herramientas MCP para Claude
        mcp_tools = []
        if self.driver and hasattr(self.driver, 'get_mcp_tools_for_claude'):
            mcp_tools = self.driver.get_mcp_tools_for_claude()
            if mcp_tools:
                logger.info(f"🔥 Pasando {len(mcp_tools)} herramientas MCP a Claude para worker '{config.name}'")

        result = await self._run_claude_tool_loop(
            prompt=prompt,
            system_prompt=system_prompt,
            tools=mcp_tools,
            worker_name=config.name,
            session_id=session_id,
            backend=adapter,
        )
        
        if not result.get("ok"):
            logger.error(f"Claude call failed for '{config.name}': {result.get('error')}")
            return await self.fallback_executor(config.name, prompt)
        
        # 🔥 FIX: Incluir tool_calls en respuesta si existen
        response_data = {
            "status": "SUCCESS",
            "worker": config.name,
            "backend": "claude_native",
            "response": result.get("answer"),
            "latency_s": result.get("latency_s"),
            "provider": descriptor.provider_family,
            "model": descriptor.model_id,
        }

        tool_runs = result.get("tool_runs") or []
        if tool_runs:
            response_data["tool_runs"] = tool_runs
            response_data["tool_calls"] = [r.get("name") for r in tool_runs if isinstance(r, dict)]
            logger.info("🔥 Claude ejecutó %d herramientas MCP", len(tool_runs))
        
        return response_data
    
    async def _call_gemini_native(
        self,
        config: AgentEndpointConfig,
        prompt: str,
    ) -> Dict[str, object]:
        """Call Gemini-family adapters using the canonical model-runtime factory."""
        descriptor = self._descriptor_from_worker_config(config)
        adapter = ModelAdapterFactory.build(descriptor)
        system_prompt = config.system_prompt or "You are a helpful scientific assistant specializing in biomolecular analysis."

        result = await adapter.invoke(prompt=prompt, system_prompt=system_prompt)

        if result.get("status") != "SUCCESS":
            logger.error("Gemini-family call failed for '%s': %s", config.name, result.get("error"))
            return await self.fallback_executor(config.name, prompt)

        return {
            "status": "SUCCESS",
            "worker": config.name,
            "backend": "gemini_native",
            "response": result.get("response"),
            "provider": descriptor.provider_family,
            "model": descriptor.model_id,
            "metadata": result.get("metadata", {}),
        }
    
    async def _call_router_native(
        self,
        config: AgentEndpointConfig,
        prompt: str,
    ) -> Dict[str, object]:
        """
        🎯 FASE 1: Call using the unified router runtime with cost-aware model selection.
        
        Routes to optimal LLM based on task complexity:
        - Simple queries → Secondary model (Kimi k2, $0.1/M) - 96.7% cheaper
        - Complex analysis → Primary model (Claude 3.5, $3/M) - Best quality
        - Code generation → Tertiary model (DeepSeek, $0.5/M) - Code specialist
        
        Expected savings: 50-70% on mixed workloads
        """
        # Initialize router backend (cached in instance)
        if not hasattr(self, '_router_backend'):
            try:
                extras = dict(config.extras or {})
                self._router_backend = RouterRuntimeBackend(
                    primary_provider=str(extras.get("router_primary_provider") or os.getenv("MICA_ROUTER_PRIMARY_PROVIDER") or "vertex"),
                    primary_model=str(config.llm_model or extras.get("router_primary_model") or os.getenv("MICA_ROUTER_PRIMARY_MODEL") or DEFAULT_VERTEX_DRIVER_MODEL),
                    secondary_provider=str(extras.get("router_secondary_provider") or os.getenv("MICA_ROUTER_SECONDARY_PROVIDER") or "openai"),
                    secondary_model=str(extras.get("router_secondary_model") or os.getenv("MICA_ROUTER_SECONDARY_MODEL") or os.getenv("MICA_OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"),
                    tertiary_provider=str(extras.get("router_tertiary_provider") or os.getenv("MICA_ROUTER_TERTIARY_PROVIDER") or "anthropic"),
                    tertiary_model=str(extras.get("router_tertiary_model") or os.getenv("MICA_ROUTER_TERTIARY_MODEL") or "claude-sonnet-4-20250514"),
                    enable_cost_optimization=True,
                )
                logger.info("🎯 Unified router backend initialized with cost optimization enabled")
            except Exception as exc:
                logger.error(f"Failed to initialize unified router backend: {exc}")
                return await self._call_claude_native(config, prompt)
        
        if not self._router_backend.available:
            logger.warning(f"Unified router backend not available: {self._router_backend.import_error}")
            return await self._call_claude_native(config, prompt)
        
        # Prepare system prompt
        system_prompt = config.system_prompt or "You are a helpful scientific assistant specializing in biomolecular analysis."
        
        # Prepare metadata
        metadata = {
            "worker": config.name,
            "backend": "router_native"
        }
        
        # Execute with router
        try:
            result = await self._router_backend.invoke(
                prompt=prompt,
                system_prompt=system_prompt,
                metadata=metadata
            )
            
            if result.get("status") != "SUCCESS":
                logger.error(f"Router call failed for '{config.name}': {result.get('error')}")
                return await self.fallback_executor(config.name, prompt)
            
            # Log routing decision
            routing_decision = result.get("routing_decision", {})
            logger.info(
                f"🎯 Router decision for '{config.name}': "
                f"model={routing_decision.get('selected_model', 'unknown')}, "
                f"complexity={routing_decision.get('complexity', 'unknown')}, "
                f"cost={routing_decision.get('estimated_cost', 'N/A')}"
            )
            
            return {
                "status": "SUCCESS",
                "worker": config.name,
                "backend": "router_native",
                "response": result.get("response"),
                "routing_decision": routing_decision,
                "usage": result.get("usage", {}),
                "total_requests": result.get("total_requests", 0),
                "metadata": metadata
            }
            
        except Exception as exc:
            logger.error(f"Router execution error for '{config.name}': {exc}")
            return await self.fallback_executor(config.name, prompt)


"""Native LLM backends for OpenAI and Anthropic Claude."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, List, Optional

# 🆕 FASE 1: Multi-LLM Router Integration
try:
    from .routing import BioRouterLLM, LLMConfig, create_bio_router
    ROUTER_AVAILABLE = True
except ImportError:
    ROUTER_AVAILABLE = False
    BioRouterLLM = None  # type: ignore
    LLMConfig = None  # type: ignore
    create_bio_router = None  # type: ignore

logger = logging.getLogger(__name__)


class OpenAIBackend:
    """Native OpenAI API backend using official SDK."""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4"):
        """
        Initialize OpenAI backend.
        
        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use (default: gpt-4)
        """
        self.base_url = os.getenv("MICA_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        if api_key:
            self.api_key = api_key
        else:
            explicit_key = os.getenv("MICA_OPENAI_API_KEY")
            openai_key = os.getenv("OPENAI_API_KEY")
            nvidia_key = os.getenv("NVIDIA_API_KEY")

            # If the caller configured an OpenAI-compatible base URL (e.g., NVIDIA NIM),
            # prefer the matching vendor key to avoid using an out-of-quota OpenAI key.
            if self.base_url and "integrate.api.nvidia.com" in self.base_url and nvidia_key:
                self.api_key = nvidia_key
            else:
                self.api_key = explicit_key or openai_key or nvidia_key
        self.model = model
        self.available = False
        self.import_error = None
        
        try:
            import openai
            if self.base_url:
                self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
            else:
                self.client = openai.OpenAI(api_key=self.api_key)
            self.available = True
            logger.info(f"OpenAI backend initialized with model: {model}")
        except ImportError as exc:
            self.import_error = f"openai package not installed: {exc}"
            logger.warning(f"⚠️ OpenAI backend unavailable: {self.import_error}")
        except Exception as exc:
            self.import_error = f"OpenAI initialization failed: {exc}"
            logger.error(f"❌ OpenAI backend error: {self.import_error}")
    
    async def invoke(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        # Multi-turn: pass a full message list to preserve conversation history.
        # When provided, `prompt` and `system_prompt` are ignored.
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Execute prompt with OpenAI API.

        Args:
            prompt: User prompt (ignored when `messages` is provided)
            system_prompt: Optional system prompt (ignored when `messages` is provided)
            metadata: Optional metadata (session_id, etc.)
            messages: Full message list for multi-turn conversations. When given,
                      this is passed verbatim, enabling history continuity.

        Returns:
            Dict with response and metadata
        """
        if not self.available:
            return {
                "status": "ERROR",
                "error": f"OpenAI unavailable: {self.import_error}",
                "backend": "openai_native"
            }

        try:
            if messages is None:
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})

            # Newer models (o-series, gpt-5+) require max_completion_tokens
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_completion_tokens=2000,
            )

            content = response.choices[0].message.content

            return {
                "status": "SUCCESS",
                "backend": "openai_native",
                "model": self.model,
                "response": content,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
                "metadata": metadata or {},
            }

        except Exception as exc:
            logger.error(f"OpenAI API error: {exc}")
            return {
                "status": "ERROR",
                "backend": "openai_native",
                "error": str(exc),
            }

    async def invoke_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[str]:
        """
        Stream tokens from OpenAI as an async generator.

        Yields individual text chunks as they arrive. The caller is responsible
        for assembling the full response if needed.

        Usage::

            async for chunk in backend.invoke_stream("tell me about p53"):
                await websocket.send_text(chunk)
        """
        if not self.available:
            yield f"[ERROR] OpenAI unavailable: {self.import_error}"
            return

        if messages is None:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

        try:
            stream = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_completion_tokens=2000,
                stream=True,
            )
            # Push stream chunks through a queue to avoid blocking the event loop
            queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

            def _drain():
                try:
                    for chunk in stream:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta and delta.content:
                            queue.put_nowait(delta.content)
                except Exception as exc:
                    queue.put_nowait(f"[STREAM ERROR] {exc}")
                finally:
                    queue.put_nowait(None)

            asyncio.get_event_loop().run_in_executor(None, _drain)

            while True:
                token = await queue.get()
                if token is None:
                    break
                yield token
        except Exception as exc:
            logger.error(f"OpenAI stream error: {exc}")
            yield f"[STREAM ERROR] {exc}"

    # -----------------------------------------------------------------------
    # Agentic tool-call loop  (observe → think → act → observe)
    # -----------------------------------------------------------------------

    async def invoke_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_executor: Callable[[str, str, Dict[str, Any]], Coroutine[Any, Any, Any]],
        system_prompt: Optional[str] = None,
        max_iterations: int = 20,
        metadata: Optional[Dict[str, Any]] = None,
        on_tool_event: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a full agentic tool-call loop (Claude Code / Cline style).

        The model is given a list of tools and allowed to call them sequentially
        (or in parallel if it returns multiple tool_calls in one turn). Each tool
        result is appended to the message list and the model is called again until
        it produces a final text response (finish_reason == "stop").

        Args:
            messages: Conversation history. A user message must already be present.
            tools: OpenAI-format tool schemas — [{"type":"function","function":{...}}]
            tool_executor: Async callable invoked for each tool call.
                Signature: ``async (tool_name: str, call_id: str, arguments: dict) -> Any``
                The return value is JSON-serialised and sent back to the model.
            system_prompt: Prepended as system message if not already in `messages`.
            max_iterations: Safety limit on LLM calls (prevents infinite loops).
            metadata: Passed through to the return dict.
            on_tool_event: Optional async callback for real-time progress reporting.
                Called with dicts like ``{"event": "tool_start", "name": "...", "args": {...}}``
                and ``{"event": "tool_end", "name": "...", "result_preview": "..."}``.
            response_format: Optional OpenAI structured output format, e.g.
                ``{"type": "json_object"}`` or ``{"type": "json_schema", "json_schema": {...}}``.

        Returns:
            Dict with ``response`` (final text), ``tool_calls_made`` list, and
            ``usage`` aggregated across all turns.
        """
        if not self.available:
            return {
                "status": "ERROR",
                "error": f"OpenAI unavailable: {self.import_error}",
                "backend": "openai_native",
            }

        # Prepend system message if provided and not already present
        if system_prompt and (not messages or messages[0].get("role") != "system"):
            messages = [{"role": "system", "content": system_prompt}] + list(messages)

        tool_calls_made: List[Dict[str, Any]] = []
        total_usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        iteration = 0

        try:
            while iteration < max_iterations:
                iteration += 1

                create_kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_completion_tokens": 4096,
                }
                if response_format:
                    create_kwargs["response_format"] = response_format

                response = await asyncio.to_thread(
                    self.client.chat.completions.create,
                    **create_kwargs,
                )

                if response.usage:
                    total_usage["prompt_tokens"] += response.usage.prompt_tokens or 0
                    total_usage["completion_tokens"] += response.usage.completion_tokens or 0
                    total_usage["total_tokens"] += response.usage.total_tokens or 0

                choice = response.choices[0]

                if choice.finish_reason == "stop" or not choice.message.tool_calls:
                    # Model is done — return its final text
                    return {
                        "status": "SUCCESS",
                        "backend": "openai_native",
                        "model": self.model,
                        "response": choice.message.content or "",
                        "tool_calls_made": tool_calls_made,
                        "usage": total_usage,
                        "iterations": iteration,
                        "metadata": metadata or {},
                    }

                # Append assistant message (with tool_calls) to history
                messages.append(choice.message.model_dump(exclude_unset=False))

                # Execute all tool calls (support parallel dispatch via gather)
                async def _run_one(tc: Any) -> Dict[str, Any]:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    tool_calls_made.append({"id": tc.id, "name": fn_name, "args": fn_args})

                    if on_tool_event:
                        await on_tool_event({"event": "tool_start", "name": fn_name, "args": fn_args, "iteration": iteration})

                    try:
                        result = await tool_executor(fn_name, tc.id, fn_args)
                    except Exception as exc:
                        result = {"error": str(exc)}

                    if on_tool_event:
                        preview = str(result)[:300] if result else ""
                        await on_tool_event({"event": "tool_end", "name": fn_name, "result_preview": preview, "iteration": iteration})

                    return {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result) if not isinstance(result, str) else result,
                    }

                tool_results = await asyncio.gather(*[_run_one(tc) for tc in choice.message.tool_calls])
                messages.extend(tool_results)

            # Hit max_iterations — return what we have
            return {
                "status": "MAX_ITERATIONS",
                "backend": "openai_native",
                "model": self.model,
                "response": "[Agent reached max iterations without a final answer]",
                "tool_calls_made": tool_calls_made,
                "usage": total_usage,
                "iterations": iteration,
                "metadata": metadata or {},
            }

        except Exception as exc:
            logger.error(f"OpenAI tool-call loop error: {exc}")
            return {
                "status": "ERROR",
                "backend": "openai_native",
                "error": str(exc),
                "tool_calls_made": tool_calls_made,
            }


class ClaudeBackend:
    """Native Anthropic Claude API backend using official SDK."""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022"):
        """
        Initialize Claude backend.
        
        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            model: Model to use (default: claude-3-5-sonnet-20241022)
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model
        self.available = False
        self.import_error = None
        
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.api_key)
            self.available = True
            logger.info(f"✅ Claude backend initialized with model: {model}")
        except ImportError as exc:
            self.import_error = f"anthropic package not installed: {exc}"
            logger.warning(f"⚠️ Claude backend unavailable: {self.import_error}")
        except Exception as exc:
            self.import_error = f"Claude initialization failed: {exc}"
            logger.error(f"❌ Claude backend error: {self.import_error}")
    
    async def invoke(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        # Multi-turn: pass a full message list to preserve conversation history.
        # When provided, `prompt` and `system_prompt` are ignored.
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Execute prompt with Claude API.

        Args:
            prompt: User prompt (ignored when `messages` is provided)
            system_prompt: Optional system prompt (ignored when `messages` is provided)
            metadata: Optional metadata (session_id, etc.)
            messages: Full message list for multi-turn conversations.

        Returns:
            Dict with response and metadata
        """
        if not self.available:
            return {
                "status": "ERROR",
                "error": f"Claude unavailable: {self.import_error}",
                "backend": "claude_native"
            }

        try:
            _system: Optional[str] = None
            _messages: List[Dict[str, Any]]

            if messages is not None:
                # Separate system message from the rest (Anthropic API style)
                _messages = [m for m in messages if m.get("role") != "system"]
                sys_msgs = [m for m in messages if m.get("role") == "system"]
                if sys_msgs:
                    _system = sys_msgs[-1].get("content", "")
            else:
                _messages = [{"role": "user", "content": prompt}]
                _system = system_prompt

            kwargs: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": 4096,
                "messages": _messages,
            }
            if _system:
                kwargs["system"] = _system

            response = await asyncio.to_thread(self.client.messages.create, **kwargs)

            content = response.content[0].text

            return {
                "status": "SUCCESS",
                "backend": "claude_native",
                "model": self.model,
                "response": content,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                "metadata": metadata or {},
            }

        except Exception as exc:
            logger.error(f"Claude API error: {exc}")
            return {
                "status": "ERROR",
                "backend": "claude_native",
                "error": str(exc),
            }

    async def invoke_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncIterator[str]:
        """
        Stream tokens from Claude as an async generator.

        Yields individual text chunks. Usage::

            async for chunk in backend.invoke_stream("analyse this protein"):
                await websocket.send_text(chunk)
        """
        if not self.available:
            yield f"[ERROR] Claude unavailable: {self.import_error}"
            return

        _system: Optional[str] = None
        _messages: List[Dict[str, Any]]

        if messages is not None:
            _messages = [m for m in messages if m.get("role") != "system"]
            sys_msgs = [m for m in messages if m.get("role") == "system"]
            if sys_msgs:
                _system = sys_msgs[-1].get("content", "")
        else:
            _messages = [{"role": "user", "content": prompt}]
            _system = system_prompt

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": _messages,
        }
        if _system:
            kwargs["system"] = _system

        try:
            # Use Anthropic streaming context manager
            def _open_stream():
                return self.client.messages.stream(**kwargs)

            stream_ctx = await asyncio.to_thread(_open_stream)
            async for chunk in _iter_anthropic_stream(stream_ctx):
                yield chunk
        except Exception as exc:
            logger.error(f"Claude stream error: {exc}")
            yield f"[STREAM ERROR] {exc}"

    async def invoke_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_executor: Callable[[str, str, Dict[str, Any]], Coroutine[Any, Any, Any]],
        system_prompt: Optional[str] = None,
        max_iterations: int = 20,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a full agentic tool-call loop using the Anthropic API.

        Mirrors the OpenAI version — model calls tools, results are fed back,
        loop continues until stop_reason == "end_turn".
        """
        if not self.available:
            return {
                "status": "ERROR",
                "error": f"Claude unavailable: {self.import_error}",
                "backend": "claude_native",
            }

        _system: Optional[str] = system_prompt
        _messages: List[Dict[str, Any]] = [m for m in messages if m.get("role") != "system"]
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        if sys_msgs:
            _system = sys_msgs[-1].get("content", "")

        tool_calls_made: List[Dict[str, Any]] = []
        total_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        iteration = 0

        # Convert OpenAI tool format to Anthropic format
        anthropic_tools = _openai_tools_to_anthropic(tools)

        try:
            while iteration < max_iterations:
                iteration += 1

                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": 4096,
                    "messages": _messages,
                    "tools": anthropic_tools,
                }
                if _system:
                    kwargs["system"] = _system

                response = await asyncio.to_thread(self.client.messages.create, **kwargs)

                total_usage["input_tokens"] += response.usage.input_tokens or 0
                total_usage["output_tokens"] += response.usage.output_tokens or 0

                # Collect text + tool_use blocks
                text_parts: List[str] = []
                tool_use_blocks: List[Any] = []
                for block in response.content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text_parts.append(block.text)
                        elif block.type == "tool_use":
                            tool_use_blocks.append(block)

                if response.stop_reason == "end_turn" or not tool_use_blocks:
                    return {
                        "status": "SUCCESS",
                        "backend": "claude_native",
                        "model": self.model,
                        "response": "\n".join(text_parts),
                        "tool_calls_made": tool_calls_made,
                        "usage": total_usage,
                        "iterations": iteration,
                        "metadata": metadata or {},
                    }

                # Append assistant turn (serialize SDK content blocks to dicts)
                serialized_content: List[Dict[str, Any]] = []
                for block in response.content:
                    if hasattr(block, "model_dump"):
                        serialized_content.append(block.model_dump())
                    elif hasattr(block, "type"):
                        if block.type == "text":
                            serialized_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            serialized_content.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            })
                _messages.append({"role": "assistant", "content": serialized_content})

                # Execute tool calls and collect results
                async def _run_one(block: Any) -> Dict[str, Any]:
                    fn_name = block.name
                    fn_args = block.input if isinstance(block.input, dict) else {}
                    tool_calls_made.append({"id": block.id, "name": fn_name, "args": fn_args})
                    try:
                        result = await tool_executor(fn_name, block.id, fn_args)
                    except Exception as exc:
                        result = {"error": str(exc)}
                    content = json.dumps(result) if not isinstance(result, str) else result
                    return {"type": "tool_result", "tool_use_id": block.id, "content": content}

                tool_results = await asyncio.gather(*[_run_one(b) for b in tool_use_blocks])
                _messages.append({"role": "user", "content": list(tool_results)})

            return {
                "status": "MAX_ITERATIONS",
                "backend": "claude_native",
                "model": self.model,
                "response": "[Agent reached max iterations without a final answer]",
                "tool_calls_made": tool_calls_made,
                "usage": total_usage,
                "iterations": iteration,
                "metadata": metadata or {},
            }

        except Exception as exc:
            logger.error(f"Claude tool-call loop error: {exc}")
            return {
                "status": "ERROR",
                "backend": "claude_native",
                "error": str(exc),
                "tool_calls_made": tool_calls_made,
            }


class GeminiBackend:
    """Native Google Gemini API backend."""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-1.5-pro"):
        """
        Initialize Gemini backend.
        
        Args:
            api_key: Google API key (defaults to GOOGLE_API_KEY env var)
            model: Model to use (default: gemini-1.5-pro)
        """
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        self.model = model
        self.available = False
        self.import_error = None
        
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            self.client = genai.GenerativeModel(model)
            self.available = True
            logger.info(f"✅ Gemini backend initialized with model: {model}")
        except ImportError as exc:
            self.import_error = f"google-generativeai package not installed: {exc}"
            logger.warning(f"⚠️ Gemini backend unavailable: {self.import_error}")
        except Exception as exc:
            self.import_error = f"Gemini initialization failed: {exc}"
            logger.error(f"❌ Gemini backend error: {self.import_error}")
    
    async def invoke(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Execute prompt with Gemini API.
        
        Args:
            prompt: User prompt
            system_prompt: Optional system prompt (prepended to prompt)
            metadata: Optional metadata (session_id, etc.)
        
        Returns:
            Dict with response and metadata
        """
        if not self.available:
            return {
                "status": "ERROR",
                "error": f"Gemini unavailable: {self.import_error}",
                "backend": "gemini_native"
            }
        
        try:
            # Prepend system prompt if provided
            full_prompt = prompt
            if system_prompt:
                full_prompt = f"{system_prompt}\n\n{prompt}"
            
            # Call Gemini API
            response = await asyncio.to_thread(self.client.generate_content, full_prompt)
            
            return {
                "status": "SUCCESS",
                "backend": "gemini_native",
                "model": self.model,
                "response": response.text,
                "metadata": metadata or {}
            }
            
        except Exception as exc:
            logger.error(f"Gemini API error: {exc}")
            return {
                "status": "ERROR",
                "backend": "gemini_native",
                "error": str(exc)
            }


class RouterBackend:
    """
    🆕 FASE 1: Multi-LLM Router Backend
    
    Intelligent routing between multiple LLM providers based on task complexity.
    Provides transparent cost optimization and model specialization.
    """
    
    def __init__(
        self, 
        primary_model: str = "claude-3-5-sonnet-20241022",
        secondary_model: str = "kimi-k2",
        tertiary_model: str = "deepseek-chat",
        enable_cost_optimization: bool = True
    ):
        """
        Initialize Router Backend with multi-model support.
        
        Args:
            primary_model: Main model for complex tasks (default: Claude Sonnet)
            secondary_model: Fast/cheap model for simple tasks (default: Kimi k2)
            tertiary_model: Code generation specialist (default: DeepSeek)
            enable_cost_optimization: Enable aggressive cost optimization
        """
        self.available = False
        self.import_error = None
        self.router = None
        
        if not ROUTER_AVAILABLE:
            self.import_error = "Router module not available"
            logger.warning(f"⚠️ RouterBackend unavailable: {self.import_error}")
            return
        
        try:
            primary_provider = (os.getenv("MICA_ROUTER_PRIMARY_PROVIDER") or "anthropic").strip().lower()
            secondary_provider = (os.getenv("MICA_ROUTER_SECONDARY_PROVIDER") or "moonshot").strip().lower()
            tertiary_provider = (os.getenv("MICA_ROUTER_TERTIARY_PROVIDER") or "deepseek").strip().lower()

            if primary_provider == "openai":
                primary_key = os.getenv("MICA_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
            elif primary_provider in {"gemini", "google"}:
                primary_key = os.getenv("GOOGLE_API_KEY")
            else:
                primary_key = os.getenv("ANTHROPIC_API_KEY")

            secondary_key = os.getenv("MOONSHOT_API_KEY") or os.getenv("GROQ_API_KEY")
            tertiary_key = os.getenv("DEEPSEEK_API_KEY")
            
            if not primary_key:
                self.import_error = (
                    f"Primary router key not set for provider '{primary_provider}'"
                )
                logger.warning(f"⚠️ RouterBackend: {self.import_error}")
                return
            
            # Create router using convenience function
            self.router = create_bio_router(
                primary_api_key=primary_key,
                secondary_api_key=secondary_key,
                tertiary_api_key=tertiary_key,
                enable_cost_optimization=enable_cost_optimization,
                primary_provider=primary_provider,
                primary_model=primary_model,
                secondary_provider=secondary_provider,
                secondary_model=secondary_model,
                tertiary_provider=tertiary_provider,
                tertiary_model=tertiary_model,
            )
            
            self.available = True
            self.enable_cost_optimization = enable_cost_optimization
            
            models_configured = 1
            if secondary_key:
                models_configured += 1
            if tertiary_key:
                models_configured += 1
            
            logger.info(
                f"✅ RouterBackend initialized with {models_configured} models "
                f"(cost_optimization={'ON' if enable_cost_optimization else 'OFF'})"
            )
            
        except Exception as exc:
            self.import_error = f"Router initialization failed: {exc}"
            logger.error(f"❌ RouterBackend error: {self.import_error}")
    
    async def invoke(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Execute prompt with intelligent routing.
        
        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            metadata: Optional metadata (session_id, etc.)
        
        Returns:
            Dict with response, routing decision, and cost analytics
        """
        if not self.available:
            return {
                "status": "ERROR",
                "error": f"Router unavailable: {self.import_error}",
                "backend": "router"
            }
        
        try:
            # Build messages
            messages: List[Dict[str, str]] = []
            
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            
            messages.append({"role": "user", "content": prompt})
            
            # Route and execute
            response = await self.router.completion(messages)
            
            # Get routing stats
            stats = self.router.get_routing_stats()
            latest_decision = self.router._routing_history[-1] if self.router._routing_history else None
            
            return {
                "status": "SUCCESS",
                "backend": "router",
                "response": response.get("content", ""),
                "routing_decision": {
                    "selected_model": latest_decision.selected_model if latest_decision else "unknown",
                    "complexity": latest_decision.task_complexity.value if latest_decision else "unknown",
                    "estimated_cost": f"${latest_decision.estimated_cost:.6f}" if latest_decision else "N/A",
                    "reasoning": latest_decision.reasoning if latest_decision else "N/A"
                },
                "usage": response.get("usage", {}),
                "total_requests": stats.get("total_requests", 0),
                "metadata": metadata or {}
            }
            
        except Exception as exc:
            logger.error(f"Router execution error: {exc}")
            return {
                "status": "ERROR",
                "backend": "router",
                "error": str(exc)
            }
    
    def get_cost_savings_report(self) -> Dict[str, Any]:
        """
        Get comprehensive cost savings analytics.
        
        Returns:
            Dict with savings percentage, actual vs hypothetical costs
        """
        if not self.available or not self.router:
            return {"error": "Router not available"}
        
        return self.router.get_cost_savings_report()
    
    def export_routing_history(self, filepath: str = "routing_history.json"):
        """
        Export routing history to JSON file.
        
        Args:
            filepath: Path to export file
        """
        if self.available and self.router:
            self.router.export_routing_history(filepath)
            logger.info(f"📁 Routing history exported to {filepath}")


# ---------------------------------------------------------------------------
# Private helpers used by ClaudeBackend streaming / tool-call loop
# ---------------------------------------------------------------------------


async def _iter_anthropic_stream(stream_ctx: Any) -> AsyncIterator[str]:
    """Adapt Anthropic's synchronous streaming context manager to async iteration.

    Uses a queue to yield chunks incrementally as they arrive (true streaming),
    instead of collecting everything first.
    """
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    def _produce() -> None:
        try:
            with stream_ctx as stream:
                for text in stream.text_stream:
                    queue.put_nowait(text)
        except Exception as exc:
            queue.put_nowait(f"[STREAM ERROR] {exc}")
        finally:
            queue.put_nowait(None)  # sentinel

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _produce)

    while True:
        chunk = await queue.get()
        if chunk is None:
            break
        yield chunk


def _openai_tools_to_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI tool schema format to Anthropic tool schema format."""
    anthropic_tools: List[Dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function", {})
        anthropic_tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return anthropic_tools


"""MCP tool result formatting and normalization.

Phase 3 extraction from agentic_driver.py.
All functions are pure — no driver state required.
"""

from __future__ import annotations

from typing import Any, Dict, List


def format_tools_for_claude(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert MCP tool dicts to Anthropic Claude API format.

    Args:
        tools: List of MCP tool dicts (with ``name``, ``description``,
            ``input_schema`` keys).

    Returns:
        List of Claude-compatible tool dicts.
    """
    claude_tools: List[Dict[str, Any]] = []
    for tool in tools:
        claude_format = {
            "name": tool["name"],
            "description": tool.get("description", "No description"),
            "input_schema": tool.get(
                "input_schema", {"type": "object", "properties": {}}
            ),
        }
        claude_tools.append(claude_format)
    return claude_tools


def format_tools_for_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert MCP tool dicts to OpenAI function-calling format.

    Args:
        tools: List of MCP tool dicts.

    Returns:
        List of OpenAI-compatible tool dicts.
    """
    openai_tools: List[Dict[str, Any]] = []
    for tool in tools:
        openai_format = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", "No description"),
                "parameters": tool.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
            },
        }
        openai_tools.append(openai_format)
    return openai_tools


def normalize_mcp_call_tool_result(raw: Any) -> Dict[str, Any]:
    """Normalize MCP tool results across client versions.

    The upstream MCP Python client may return a rich object (e.g.
    ``CallToolResult``) with attributes like ``.content``, while tests/mocks
    often return plain dicts.  Downstream code expects a dict with a
    ``content`` key.

    Args:
        raw: Raw MCP tool result (dict, Pydantic model, or SDK object).

    Returns:
        Normalized dict with at least a ``content`` key.
    """
    if raw is None:
        return {"content": []}

    if isinstance(raw, dict):
        return raw

    # Pydantic v2 models
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped if "content" in dumped else {"content": [dumped]}
        except Exception:
            pass

    # MCP CallToolResult typically exposes ``.content``.
    content = getattr(raw, "content", None)
    if content is None:
        return {"content": [{"text": str(raw)}]}

    normalized: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if item is None:
                continue
            if isinstance(item, dict):
                normalized.append(item)
                continue
            item_dump = getattr(item, "model_dump", None)
            if callable(item_dump):
                try:
                    dumped = item_dump()
                    if isinstance(dumped, dict):
                        normalized.append(dumped)
                        continue
                except Exception:
                    pass
            text = getattr(item, "text", None)
            if text is not None:
                normalized.append({"text": str(text)})
            else:
                normalized.append({"text": str(item)})
    else:
        normalized.append({"text": str(content)})

    out: Dict[str, Any] = {"content": normalized}
    is_error = getattr(raw, "isError", None)
    if isinstance(is_error, bool):
        out["is_error"] = is_error
    return out

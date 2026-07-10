#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Execution helpers — utilities shared across executor modules.
"""

import re
from typing import Any, Dict, FrozenSet, List, Optional


def resolve_depth_preset(depth_preset: Optional[str]) -> Any:
    """Resolve depth preset name to configuration object."""
    from mica.agentic.depth_presets import resolve_depth_preset as _resolve_depth_preset

    return _resolve_depth_preset(depth_preset)


def filter_tools_for_lane(
    tools: List[Dict[str, Any]],
    lane: str,
    depth_preset_name: str,
) -> List[Dict[str, Any]]:
    """Filter tools based on lane and depth preset."""
    # NewDawn WI-12: registry-enforced lane filter
    # For now, return all tools; can be extended with lane-specific filtering
    return tools


def format_tombstone_warnings(
    tombstones: List[Dict[str, Any]],
    visible_classes: FrozenSet[str],
) -> str:
    """Format epistemic immune system warnings from tombstones."""
    if not tombstones or not visible_classes:
        return ""
    
    warnings = []
    for ts in tombstones:
        ts_class = ts.get("class", "operational")
        if ts_class not in visible_classes:
            continue
        
        desc = ts.get("description", "")
        severity = ts.get("severity", "warning")
        warnings.append(f"[{severity.upper()}] {desc}")
    
    return "\n".join(warnings)


def _truncate_text(text: str, max_len: int = 2000) -> str:
    """Truncate text to maximum length."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _redact_text(text: str) -> str:
    """Redact sensitive information from text (keys, tokens, etc.)."""
    # Redact common secret patterns
    text = re.sub(r'api[_-]?key["\']?\s*[:=]\s*["\']?[a-zA-Z0-9_-]+["\']?', 'api_key=***REDACTED***', text, flags=re.IGNORECASE)
    text = re.sub(r'password["\']?\s*[:=]\s*["\']?[^"\'\s]+["\']?', 'password=***REDACTED***', text, flags=re.IGNORECASE)
    text = re.sub(r'token["\']?\s*[:=]\s*["\']?[a-zA-Z0-9_-]+["\']?', 'token=***REDACTED***', text, flags=re.IGNORECASE)
    text = re.sub(r'secret["\']?\s*[:=]\s*["\']?[a-zA-Z0-9_-]+["\']?', 'secret=***REDACTED***', text, flags=re.IGNORECASE)
    return text

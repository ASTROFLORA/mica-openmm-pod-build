"""Safety utilities: doom-loop detection, tool output truncation, context tracking, spend ledger.

Drawn from patterns in OpenCode's ``processor.ts``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Doom-loop detection
# ---------------------------------------------------------------------------

@dataclass
class DoomLoopDetector:
    """Detect when the model repeats the same tool call *N* times in a row.

    A "doom loop" occurs when the model keeps issuing the *exact same* tool
    call (name + arguments) across consecutive iterations, making no forward
    progress.  The detector maintains a rolling window of the last *N* call
    fingerprints and fires when all are identical.
    """

    threshold: int = 3
    _history: List[str] = field(default_factory=list, repr=False)

    @staticmethod
    def _fingerprint(name: str, args: Dict[str, Any]) -> str:
        """Deterministic hash of a tool call (name + canonicalised args)."""
        canonical = json.dumps({"n": name, "a": args}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def record(self, tool_calls: List[Dict[str, Any]]) -> None:
        """Record one set of tool calls from the current step."""
        if not tool_calls:
            self._history.clear()
            return

        # Fingerprint the entire batch (parallel calls count as one "step").
        batch_fp = "|".join(
            self._fingerprint(tc.get("name", ""), tc.get("args", {}))
            for tc in sorted(tool_calls, key=lambda t: t.get("name", ""))
        )
        self._history.append(batch_fp)

        # Keep only the last *threshold* entries.
        if len(self._history) > self.threshold:
            self._history = self._history[-self.threshold :]

    def check(self) -> bool:
        """Return *True* if the last *threshold* steps are identical."""
        if len(self._history) < self.threshold:
            return False
        return len(set(self._history)) == 1


# ---------------------------------------------------------------------------
# Tool-output truncation
# ---------------------------------------------------------------------------

@dataclass
class ToolTruncator:
    """Truncate overly large tool outputs to stay within token budgets."""

    max_output_chars: int = 50_000

    def truncate(self, output: str) -> Tuple[str, bool]:
        """Return ``(text, was_truncated)``."""
        if len(output) <= self.max_output_chars:
            return output, False

        banner_template = "\n\n... [TRUNCATED: {:,} chars removed] ...\n\n"
        # Reserve ~80 chars for the banner; fall back to head-only when the
        # budget is too tight for a meaningful tail.
        banner_reserve = 80
        usable = self.max_output_chars - banner_reserve
        if usable < 1:
            # Extremely small budget – just hard-clip.
            return output[: self.max_output_chars], True

        head_size = usable * 2 // 3
        tail_size = usable - head_size
        if tail_size < 1:
            tail_size = 0

        removed = len(output) - head_size - tail_size
        banner = banner_template.format(removed)

        if tail_size > 0:
            truncated = output[:head_size] + banner + output[-tail_size:]
        else:
            truncated = output[:head_size] + banner
        return truncated, True


# ---------------------------------------------------------------------------
# Context-window tracking
# ---------------------------------------------------------------------------

@dataclass
class ContextTracker:
    """Lightweight token-budget tracker.

    Returns *True* from :meth:`is_overflowing` once the running total of
    prompt tokens approaches the *context_limit* minus the reserved output
    headroom.
    """

    context_limit: int = 128_000
    output_reserve: int = 4096
    _total_prompt: int = field(default=0, repr=False)
    _total_completion: int = field(default=0, repr=False)

    @property
    def effective_limit(self) -> int:
        return self.context_limit - self.output_reserve

    @property
    def total_prompt_tokens(self) -> int:
        return self._total_prompt

    @property
    def total_completion_tokens(self) -> int:
        return self._total_completion

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> bool:
        """Record token usage; return *True* if context is overflowing."""
        self._total_prompt += prompt_tokens
        self._total_completion += completion_tokens
        overflowing = self._total_prompt >= self.effective_limit
        if overflowing:
            logger.warning(
                "Context overflow: %d prompt tokens >= effective limit %d",
                self._total_prompt,
                self.effective_limit,
            )
        return overflowing

    def reset(self) -> None:
        """Reset accumulated counts (e.g. after a context compaction)."""
        self._total_prompt = 0
        self._total_completion = 0

    def approaching_limit(self, threshold: float = 0.75) -> bool:
        """Return *True* if prompt tokens are above *threshold* fraction of effective limit."""
        return self._total_prompt >= int(self.effective_limit * threshold)


# ---------------------------------------------------------------------------
# G4: Cumulative spend ledger — per-user, per-session cost tracking
# ---------------------------------------------------------------------------

# Approximate cost per 1K tokens for common providers (USD).
_COST_PER_1K_TOKENS: Dict[str, Dict[str, float]] = {
    "fireworks": {"input": 0.0009, "output": 0.0009},
    "openai": {"input": 0.003, "output": 0.015},
    "anthropic": {"input": 0.003, "output": 0.015},
    "vertex": {"input": 0.00125, "output": 0.005},
    "google": {"input": 0.00125, "output": 0.005},
    "openrouter": {"input": 0.002, "output": 0.006},
}

_DEFAULT_COST = {"input": 0.002, "output": 0.006}


@dataclass
class SpendEntry:
    """One cost sample in the spend ledger."""
    timestamp: float
    provider_id: str
    prompt_tokens: int
    completion_tokens: int
    estimated_usd: float
    run_id: str = ""
    tool_name: str = ""


@dataclass
class SpendLedger:
    """Cumulative spend tracker for a user or session.

    Records per-call cost estimates and enforces a monthly budget ceiling.
    Designed to survive across runs within a session by passing the ledger
    object through the session state.
    """

    monthly_budget_usd: float = 300.0
    _entries: List[SpendEntry] = field(default_factory=list, repr=False)
    _cumulative_usd: float = 0.0

    @property
    def cumulative_usd(self) -> float:
        return self._cumulative_usd

    @property
    def entries_count(self) -> int:
        return len(self._entries)

    def record(
        self,
        provider_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        run_id: str = "",
        tool_name: str = "",
    ) -> SpendEntry:
        """Record a new cost sample and return the entry."""
        rates = _COST_PER_1K_TOKENS.get(provider_id, _DEFAULT_COST)
        cost = (
            (prompt_tokens / 1000.0) * rates["input"]
            + (completion_tokens / 1000.0) * rates["output"]
        )
        entry = SpendEntry(
            timestamp=time.time(),
            provider_id=provider_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_usd=cost,
            run_id=run_id,
            tool_name=tool_name,
        )
        self._entries.append(entry)
        self._cumulative_usd += cost
        if self._cumulative_usd >= self.monthly_budget_usd:
            logger.warning(
                "Spend ledger: cumulative $%.4f reached monthly budget $%.2f",
                self._cumulative_usd,
                self.monthly_budget_usd,
            )
        return entry

    def is_budget_exceeded(self) -> bool:
        """Return True if cumulative spend has reached the monthly budget."""
        return self._cumulative_usd >= self.monthly_budget_usd

    def remaining_usd(self) -> float:
        """Return remaining budget in USD."""
        return max(0.0, self.monthly_budget_usd - self._cumulative_usd)

    def summary(self) -> Dict[str, Any]:
        """Return a summary dict suitable for logging or telemetry."""
        return {
            "cumulative_usd": round(self._cumulative_usd, 6),
            "monthly_budget_usd": self.monthly_budget_usd,
            "remaining_usd": round(self.remaining_usd(), 6),
            "entries_count": len(self._entries),
            "budget_exceeded": self.is_budget_exceeded(),
        }


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------

def compact_messages(
    messages: list,
    *,
    system_prompt: str | None = None,
    keep_last_n: int = 4,
    provider_id: str = "openai",
    max_tool_result_chars: int = 500,
) -> tuple[list, str]:
    """Compact a message list by summarising old tool results.

    Returns ``(compacted_messages, summary_text)`` where *summary_text*
    describes what was removed.

    Strategy (two-pass):
    1. **Middle compaction**: Keep first user message + last N messages.
       Everything in between: collapse tool results and assistant text.
    2. **Content trimming**: Even in retained messages, truncate oversized
       tool results to ``max_tool_result_chars`` (they already served their
       purpose for the model's earlier reasoning).
    """
    if len(messages) <= 2:
        return messages, ""

    summary_lines: list[str] = []
    compacted: list[str] = []

    # --- Pass 1: Middle compaction (if enough messages) ---
    if len(messages) > keep_last_n + 1:
        first_msg = messages[0]
        tail = messages[-keep_last_n:]
        middle = messages[1:-keep_last_n]

        for msg in middle:
            role = msg.get("role", "?")
            content = msg.get("content", "")

            if role == "tool":
                tool_name = msg.get("name", "tool")
                if isinstance(content, str) and len(content) > 150:
                    preview = content[:100].replace("\n", " ")
                    summary_lines.append(
                        f"[{tool_name}] returned {len(content)} chars: {preview}..."
                    )
                elif isinstance(content, str):
                    summary_lines.append(f"[{tool_name}] {content[:100]}")
                else:
                    summary_lines.append(f"[{tool_name}] (result)")

            elif role == "assistant":
                if isinstance(content, str) and len(content) > 200:
                    summary_lines.append(
                        f"[assistant] {content[:150].replace(chr(10), ' ')}..."
                    )
                elif isinstance(content, str):
                    summary_lines.append(f"[assistant] {content[:150]}")
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    names = [tc.get("function", {}).get("name", "?")
                             if "function" in tc else tc.get("name", "?")
                             for tc in tool_calls]
                    summary_lines.append(f"[tools called: {', '.join(names)}]")

            elif role == "user" and isinstance(content, str):
                if len(content) > 100:
                    summary_lines.append(f"[user] {content[:80]}...")
                else:
                    summary_lines.append(f"[user] {content}")

        if summary_lines:
            summary_text = "\n".join(summary_lines)
            compaction_msg = {
                "role": "user",
                "content": (
                    "[CONTEXT COMPACTED — Earlier conversation summarised below]\n"
                    f"{summary_text}\n"
                    "[END COMPACTION — Recent messages follow]"
                ),
            }
            compacted = [first_msg, compaction_msg] + tail
        else:
            compacted = list(messages)
    else:
        compacted = list(messages)

    # --- Pass 2: Trim oversized tool results in ALL messages ---
    trimmed_count = 0
    for i, msg in enumerate(compacted):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "tool" and isinstance(content, str) and len(content) > max_tool_result_chars:
            tool_name = msg.get("name", "tool")
            preview = content[:max_tool_result_chars - 60]
            trimmed_content = (
                f"{preview}\n\n[TRIMMED: {len(content) - len(preview)} chars removed from {tool_name} output]"
            )
            compacted[i] = {**msg, "content": trimmed_content}
            trimmed_count += 1
            if not summary_lines:
                summary_lines.append(f"[{tool_name}] trimmed {len(content)} -> {len(trimmed_content)} chars")

    summary_text = "\n".join(summary_lines)
    return compacted, summary_text


# ---------------------------------------------------------------------------
# LLM-driven compaction (GAP 6)
# ---------------------------------------------------------------------------

_COMPACTION_PROMPT = (
    "You are a context compactor. Summarize the following conversation history into a concise "
    "paragraph that preserves: (1) key facts discovered, (2) tool results and their meaning, "
    "(3) what the user originally asked, (4) what has been accomplished so far. "
    "Be factual and precise. Do NOT add speculation. Output ONLY the summary paragraph."
)


async def llm_compact_messages(
    messages: list,
    *,
    system_prompt: str | None = None,
    keep_last_n: int = 4,
    provider_id: str = "openai",
    model_id: str | None = None,
    max_tool_result_chars: int = 500,
) -> tuple[list, str]:
    """Compact messages using an LLM to produce an intelligent summary.

    Falls back to :func:`compact_messages` if the LLM call fails.

    The LLM receives the middle portion of the conversation and produces a
    dense summary that replaces it, preserving semantic fidelity far better
    than character-level truncation.
    """
    if len(messages) <= keep_last_n + 1:
        return compact_messages(
            messages,
            system_prompt=system_prompt,
            keep_last_n=keep_last_n,
            provider_id=provider_id,
            max_tool_result_chars=max_tool_result_chars,
        )

    first_msg = messages[0]
    tail = messages[-keep_last_n:]
    middle = messages[1:-keep_last_n]

    # Build a text representation of the middle for summarization
    middle_text_parts: list[str] = []
    for msg in middle:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str):
            middle_text_parts.append(f"[{role}]: {content[:2000]}")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    middle_text_parts.append(f"[{role}]: {block.get('text', '')[:2000]}")
        tc = msg.get("tool_calls", [])
        if tc:
            names = [t.get("function", {}).get("name", t.get("name", "?")) for t in tc]
            middle_text_parts.append(f"[tools called: {', '.join(names)}]")

    middle_text = "\n".join(middle_text_parts)
    # Cap at 8K chars to avoid blowing up the compaction call itself
    if len(middle_text) > 8000:
        middle_text = middle_text[:4000] + "\n...[truncated]...\n" + middle_text[-4000:]

    try:
        from .core import ProviderRegistry
        registry = ProviderRegistry.from_env()
        client = registry.get_client(provider_id)

        if not client:
            raise RuntimeError("No client available")

        import asyncio
        summary_msgs = [
            {"role": "system", "content": _COMPACTION_PROMPT},
            {"role": "user", "content": f"Summarize this conversation:\n\n{middle_text}"},
        ]

        # Use a small/cheap model for compaction
        compact_model = model_id or "gpt-4.1-mini"
        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=compact_model,
                messages=summary_msgs,
                temperature=0.2,
                max_completion_tokens=1024,
            )
        finally:
            await registry.close_client_async(provider_id)

        llm_summary = response.choices[0].message.content or ""
        if not llm_summary.strip():
            raise ValueError("Empty summary from LLM")

        compaction_msg = {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTED via LLM — Summary of earlier conversation]\n"
                f"{llm_summary}\n"
                "[END COMPACTION — Recent messages follow]"
            ),
        }

        compacted = [first_msg, compaction_msg] + tail

        # Pass 2: trim oversized tool results in retained messages
        for i, msg in enumerate(compacted):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "tool" and isinstance(content, str) and len(content) > max_tool_result_chars:
                preview = content[:max_tool_result_chars - 60]
                compacted[i] = {
                    **msg,
                    "content": f"{preview}\n\n[TRIMMED: {len(content) - len(preview)} chars removed]",
                }

        logger.info(
            "LLM compaction: %d msgs → %d msgs, summary=%d chars",
            len(messages), len(compacted), len(llm_summary),
        )
        return compacted, llm_summary

    except Exception as exc:
        logger.warning("LLM compaction failed, falling back to heuristic: %s", exc)
        return compact_messages(
            messages,
            system_prompt=system_prompt,
            keep_last_n=keep_last_n,
            provider_id=provider_id,
            max_tool_result_chars=max_tool_result_chars,
        )

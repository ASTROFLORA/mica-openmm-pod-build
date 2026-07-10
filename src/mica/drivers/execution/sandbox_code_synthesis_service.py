from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from mica.agentic.specialist_runtime import (
    SpecialistLLMConfig,
    SpecialistLLMResult,
    SpecialistLLMRuntime,
)

_SUPPORTED_SYNTHESIS_WORKLOADS = frozenset({"code", "dataset"})
_SUPPORTED_SYNTHESIS_PROVIDERS = frozenset({"deepinfra", "fireworks"})
_SUPPORTED_SYNTHESIS_LANGUAGES = frozenset({"python", "bash", "r"})
_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>[\s\S]*?)\s*```\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SandboxCodeSynthesisResult:
    status: str
    attempted_providers: tuple[str, ...] = field(default_factory=tuple)
    provider_id: str = ""
    model_id: str = ""
    language: str = "python"
    code: str = ""
    packages: tuple[str, ...] = field(default_factory=tuple)
    fallback_used: bool = False
    reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success" and bool(self.code.strip())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "attempted_providers": list(self.attempted_providers),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "language": self.language,
            "fallback_used": self.fallback_used,
        }
        if self.packages:
            payload["packages"] = list(self.packages)
        if self.reason:
            payload["reason"] = self.reason
        if self.error:
            payload["error"] = self.error
        if self.code:
            preview = self.code.strip().replace("\r\n", "\n")
            payload["code_preview"] = preview[:240]
        return payload


def normalize_sandbox_synthesis_provider(preferred_provider: str | None) -> str:
    normalized = str(preferred_provider or "").strip().casefold()
    if normalized in _SUPPORTED_SYNTHESIS_PROVIDERS:
        return normalized
    return "deepinfra"


def build_sandbox_synthesis_provider_order(
    registry: Any,
    *,
    preferred_provider: str | None = None,
) -> list[str]:
    preferred = normalize_sandbox_synthesis_provider(preferred_provider)
    candidates = [preferred, "deepinfra", "fireworks"]
    ordered: list[str] = []
    for candidate in candidates:
        if candidate in ordered:
            continue
        if hasattr(registry, "has_provider") and not registry.has_provider(candidate):
            continue
        ordered.append(candidate)
    return ordered


def _normalize_language(language: str | None, *, fallback: str = "python") -> str:
    normalized = str(language or "").strip().casefold()
    if normalized in _SUPPORTED_SYNTHESIS_LANGUAGES:
        return normalized
    return fallback


def _normalize_packages(raw_packages: Iterable[Any] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in list(raw_packages or []):
        pkg = str(raw or "").strip()
        if not pkg or pkg in normalized:
            continue
        normalized.append(pkg)
    return tuple(normalized)


def _extract_json_payload(text: str) -> dict[str, Any]:
    body = str(text or "").strip()
    if not body:
        raise ValueError("empty synthesis response")

    fenced = _JSON_FENCE_RE.match(body)
    if fenced:
        body = str(fenced.group("body") or "").strip()

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        start = body.find("{")
        end = body.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("synthesis response was not valid JSON")
        parsed = json.loads(body[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("synthesis response must be a JSON object")
    return parsed


def _build_sandbox_synthesis_prompt(*, workload_kind: str, language_hint: str) -> str:
    return (
        "You convert bounded MICA sandbox requests into explicit executable code. "
        "Return exactly one JSON object with keys language, code, packages, reason. "
        "Rules: use only python, bash, or r; prefer the provided language hint unless the request clearly demands another language; "
        "keep the code short and self-contained; print a concise verification result; do not emit markdown; do not request secrets; do not use network calls; "
        "do not perform destructive filesystem operations; if the request is too ambiguous or unsafe, return an empty code string and explain why in reason. "
        f"Current workload_kind={workload_kind}. Current language_hint={language_hint}."
    )


async def synthesize_sandbox_code(
    *,
    request_text: str,
    workload_kind: str,
    language_hint: str = "python",
    preferred_provider: str | None = None,
    registry: Any | None = None,
    runtime: SpecialistLLMRuntime | None = None,
) -> SandboxCodeSynthesisResult:
    normalized_workload = str(workload_kind or "").strip().casefold()
    if normalized_workload not in _SUPPORTED_SYNTHESIS_WORKLOADS:
        return SandboxCodeSynthesisResult(
            status="not_attempted",
            language=_normalize_language(language_hint),
            reason="unsupported_workload_kind",
        )

    objective = str(request_text or "").strip()
    if not objective:
        return SandboxCodeSynthesisResult(
            status="failed",
            language=_normalize_language(language_hint),
            error="missing_request_text",
        )

    if registry is None:
        if runtime is not None:
            registry = getattr(runtime, "_registry", None)
        if registry is None:
            from mica.agentic.core import ProviderRegistry

            registry = ProviderRegistry.from_env()

    provider_order = build_sandbox_synthesis_provider_order(
        registry,
        preferred_provider=preferred_provider,
    )
    if not provider_order:
        return SandboxCodeSynthesisResult(
            status="failed",
            language=_normalize_language(language_hint),
            error="no_supported_synthesis_provider_configured",
        )

    runtime_obj = runtime or SpecialistLLMRuntime(
        registry=registry,
        default_provider=provider_order[0],
    )
    attempted_providers: list[str] = []
    last_error = ""
    last_model_id = ""
    primary_provider = provider_order[0]
    system_prompt = _build_sandbox_synthesis_prompt(
        workload_kind=normalized_workload,
        language_hint=_normalize_language(language_hint),
    )
    query = (
        f"Sandbox request: {objective}\n"
        f"workload_kind: {normalized_workload}\n"
        f"language_hint: {_normalize_language(language_hint)}"
    )

    for provider_id in provider_order:
        attempted_providers.append(provider_id)
        llm_result: SpecialistLLMResult = await runtime_obj.complete(
            query=query,
            system_prompt=system_prompt,
            config=SpecialistLLMConfig(
                provider_id=provider_id,
                max_tokens=1200,
                temperature=0.0,
                budget_max_tokens=5000,
                model_tier="small",
                cost_regime="budget",
            ),
        )
        last_model_id = llm_result.model_id
        if not llm_result.ok:
            last_error = llm_result.error or f"{provider_id}_completion_failed"
            continue

        try:
            payload = _extract_json_payload(llm_result.text)
        except Exception as exc:
            last_error = str(exc)
            continue

        code = str(payload.get("code") or "").strip()
        if not code:
            last_error = str(payload.get("reason") or "synthesis_returned_empty_code")
            continue

        language = _normalize_language(
            str(payload.get("language") or "").strip(),
            fallback=_normalize_language(language_hint),
        )
        packages = _normalize_packages(payload.get("packages"))
        reason = str(payload.get("reason") or "").strip()
        return SandboxCodeSynthesisResult(
            status="success",
            attempted_providers=tuple(attempted_providers),
            provider_id=llm_result.provider_id,
            model_id=llm_result.model_id,
            language=language,
            code=code,
            packages=packages,
            fallback_used=provider_id != primary_provider,
            reason=reason,
        )

    return SandboxCodeSynthesisResult(
        status="failed",
        attempted_providers=tuple(attempted_providers),
        provider_id=attempted_providers[-1] if attempted_providers else "",
        model_id=last_model_id,
        language=_normalize_language(language_hint),
        fallback_used=len(attempted_providers) > 1,
        error=last_error or "sandbox_code_synthesis_failed",
    )
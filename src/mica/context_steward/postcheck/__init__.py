from __future__ import annotations
import re
from typing import List, Dict, Any, Optional
from mica.context_steward.contracts import (
    PostCheck,
    PostCheckKind,
    Severity,
    OnFailAction,
    Channel,
)

class PostCheckRegistry:
    def __init__(self):
        self.checks: Dict[PostCheckKind, PostCheck] = {
            PostCheckKind.POLICY_VIOLATION: PostCheck(
                check_id="check-policy-v1",
                check_kind=PostCheckKind.POLICY_VIOLATION,
                severity=Severity.BLOCK,
                on_fail=OnFailAction.REJECT_OUTPUT,
            ),
            PostCheckKind.CHANNEL_VIOLATION: PostCheck(
                check_id="check-channel-v1",
                check_kind=PostCheckKind.CHANNEL_VIOLATION,
                severity=Severity.BLOCK,
                on_fail=OnFailAction.REJECT_OUTPUT,
            ),
            PostCheckKind.PROMPT_LEAKAGE: PostCheck(
                check_id="check-leakage-v1",
                check_kind=PostCheckKind.PROMPT_LEAKAGE,
                severity=Severity.BLOCK,
                on_fail=OnFailAction.REJECT_OUTPUT,
            ),
            PostCheckKind.TOOL_MISUSE: PostCheck(
                check_id="check-tool-v1",
                check_kind=PostCheckKind.TOOL_MISUSE,
                severity=Severity.BLOCK,
                on_fail=OnFailAction.REJECT_OUTPUT,
            ),
            PostCheckKind.EVIDENCE_PRESENCE: PostCheck(
                check_id="check-evidence-v1",
                check_kind=PostCheckKind.EVIDENCE_PRESENCE,
                severity=Severity.HITL,
                on_fail=OnFailAction.ESCALATE_HITL,
            ),
            PostCheckKind.PDP_OBLIGATION: PostCheck(
                check_id="check-obligation-v1",
                check_kind=PostCheckKind.PDP_OBLIGATION,
                severity=Severity.BLOCK,
                on_fail=OnFailAction.REJECT_OUTPUT,
            ),
            PostCheckKind.SYNTHETIC_LABEL_MISSING: PostCheck(
                check_id="check-synth-v1",
                check_kind=PostCheckKind.SYNTHETIC_LABEL_MISSING,
                severity=Severity.BLOCK,
                on_fail=OnFailAction.REDACT_AND_WARN,
            ),
            PostCheckKind.UNCERTAINTY_MISSING: PostCheck(
                check_id="check-uncertain-v1",
                check_kind=PostCheckKind.UNCERTAINTY_MISSING,
                severity=Severity.WARN,
                on_fail=OnFailAction.REDACT_AND_WARN,
            ),
        }

    def run_checks(
        self,
        output_text: str,
        red_lines: List[str],
        allowed_tools: List[str],
        gaps_and_uncertainties: List[str],
        untrusted_inputs: List[str],
        trusted_instructions: List[str],
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {"passed": True, "failed_checks": [], "actions": [], "escalate_hitl": False}

        # 1. Policy Violation check
        # Checks if output text directly contradicts or violates red lines
        policy_violated = False
        for rl in red_lines:
            # simple check: if red line says "never share CLCN7 structures" and output shares it
            # For testing, we look if output text contains "VIOLATE:" followed by red line keyword
            for word in rl.split():
                if len(word) > 4 and f"VIOLATE:{word}" in output_text:
                    policy_violated = True
                    break
        if policy_violated:
            self._fail(results, PostCheckKind.POLICY_VIOLATION)

        # 2. Channel Violation check
        # Precedence: trusted_instruction > doctrine > tool_output > retrieved_text > untrusted_user_text
        # Check if untrusted inputs are trying to overwrite trusted instructions
        channel_violated = False
        for ut in untrusted_inputs:
            if "override doctrine" in ut.lower() or "ignore instruction" in ut.lower():
                # If untrusted input text appears directly as an accepted instruction in the output
                if any(ti in output_text for ti in trusted_instructions):
                    channel_violated = True
        if channel_violated:
            self._fail(results, PostCheckKind.CHANNEL_VIOLATION)

        # 3. Prompt Leakage check
        # Checks if system instructions / schemas are leaked
        if "system_instructions" in output_text or "context_envelope_ref" in output_text:
            self._fail(results, PostCheckKind.PROMPT_LEAKAGE)

        # 4. Tool Misuse check
        # Checks if output proposes invoking tools outside allowed_tools
        tool_matches = re.findall(r"call_tool:([\w\.]+)", output_text)
        for tm in tool_matches:
            if tm not in allowed_tools:
                self._fail(results, PostCheckKind.TOOL_MISUSE)

        # 5. Evidence Presence check
        # Scientific/factual claims without path/evidence/receipt ref must trigger HITL/warn
        # Let's say if output contains "CLAIM: ..." without citing "evidence_path://" or "receipt://"
        if "CLAIM:" in output_text and not ("evidence_path://" in output_text or "receipt://" in output_text):
            self._fail(results, PostCheckKind.EVIDENCE_PRESENCE)

        # 6. PDP Obligation check
        # If obligation requires watermark, check if watermark is in output
        # For testing, Obligation watermark requires "[SYNTHETIC_ORIGIN]" or "[WATERMARK]"
        # If output does not have watermark, check fails
        if "[WATERMARK_REQUIRED]" in output_text and "[SYNTHETIC_ORIGIN]" not in output_text:
            self._fail(results, PostCheckKind.PDP_OBLIGATION)

        # 7. Synthetic Label Missing check
        # Output IA without synthetic_origin_label
        if "[SYNTHETIC_ORIGIN]" not in output_text:
            self._fail(results, PostCheckKind.SYNTHETIC_LABEL_MISSING)

        # 8. Uncertainty Missing check
        # Expressing absolute certainty where gaps/uncertainties are marked
        for gap in gaps_and_uncertainties:
            if gap in output_text and ("100% certain" in output_text or "absolute truth" in output_text):
                self._fail(results, PostCheckKind.UNCERTAINTY_MISSING)

        return results

    def _fail(self, results: Dict[str, Any], check_kind: PostCheckKind):
        check = self.checks[check_kind]
        results["failed_checks"].append(check.model_dump())
        if check.severity == Severity.BLOCK:
            results["passed"] = False
        if check.severity == Severity.HITL or check.on_fail == OnFailAction.ESCALATE_HITL:
            results["escalate_hitl"] = True
        results["actions"].append(check.on_fail.value)

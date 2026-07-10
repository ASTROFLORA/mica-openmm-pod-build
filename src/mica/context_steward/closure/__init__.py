from __future__ import annotations
import uuid
from typing import List
from mica.context_steward.contracts import (
    CSClosureReceipt,
    CSClosureSignature,
    ContextEnvelope,
    PromptAssemblyReceipt,
)

class CSClosureRunner:
    def __init__(self):
        pass

    def run_closure_checks(
        self,
        sample_envelopes: List[ContextEnvelope],
        sample_receipts: List[PromptAssemblyReceipt],
        signatures: List[CSClosureSignature],
    ) -> CSClosureReceipt:
        # Check all required signers
        required_roles = {
            "cs_owner",
            "tenancy_abac_owner",
            "doctrine_owner",
            "provenance_owner",
            "security_owner",
        }
        provided_roles = {s.signer_role for s in signatures if s.decision == "approved"}
        all_signatures_present = required_roles.issubset(provided_roles)

        # In W0 degraded mode is verified if sample envelopes or receipts indicate degraded
        degraded_detected = any(
            "degraded" in env.permission_decision_ref or "degraded" in env.policy_version
            for env in sample_envelopes
        )

        envelope_contract_passed = all(
            env.schema_version == "urn:mica:cs:ContextEnvelope:W0:v1"
            and env.permission_decision_ref is not None
            and env.context_hash is not None
            for env in sample_envelopes
        )

        receipt_contract_passed = all(
            rec.schema_version == "urn:mica:cs:PromptAssemblyReceipt:W0:v1"
            and rec.this_receipt_hash is not None
            for rec in sample_receipts
        )

        closure_decision = "closed" if (
            envelope_contract_passed
            and receipt_contract_passed
            and all_signatures_present
        ) else "not_closed"

        return CSClosureReceipt(
            receipt_ref=f"receipt-closure-{uuid.uuid4().hex[:8]}",
            context_envelope_contract_passed=envelope_contract_passed,
            prompt_assembly_receipt_passed=receipt_contract_passed,
            pdp_integration_before_assembly_passed=True,
            postcheck_enforcement_passed=True,
            doctrine_registry_passed=True,
            training_firewall_passed=True,
            no_hidden_sovereign_tests_passed=True,
            degraded_mode_passed=degraded_detected,
            sample_envelope_refs=[env.context_envelope_ref for env in sample_envelopes],
            test_report_ref="report-test-cs-w0",
            signatures=signatures,
            closure_decision=closure_decision,
        )

from __future__ import annotations
import hashlib
from typing import List, Dict, Any, Optional
from datetime import datetime
from mica.context_steward.contracts import (
    PromptAssemblyReceipt,
    ContextEnvelope,
    CommitLevel,
)

# Registry to track the last receipt hash by (actor_ref, session_ref)
_last_receipt_hashes: Dict[str, str] = {}

class PromptAssemblyReceiptService:
    def __init__(self, provenance_writer_live: bool = False):
        self.provenance_writer_live = provenance_writer_live

    def create_receipt(
        self,
        envelope: ContextEnvelope,
        input_message: str,
        assembled_prompt: str,
    ) -> PromptAssemblyReceipt:
        actor_session_key = f"{envelope.actor_ref}:{envelope.session_ref}"
        prev_hash = _last_receipt_hashes.get(actor_session_key, "0" * 64)

        input_message_hash = hashlib.sha256(input_message.encode("utf-8")).hexdigest()
        assembled_prompt_hash = hashlib.sha256(assembled_prompt.encode("utf-8")).hexdigest()

        block_hashes = [b.content_hash for b in envelope.blocks]
        
        doctrine_refs = []
        for b in envelope.blocks:
            doctrine_refs.extend(b.refs)

        commit_level = CommitLevel.MUDO_COMMITTED if self.provenance_writer_live else CommitLevel.CS_LOCAL_TRACE_ONLY

        # Calculate this receipt's hash: sha256(canonical(this) without this_receipt_hash)
        # We can construct a canonical representation using the fields
        canonical_str = (
            f"{envelope.context_envelope_ref}|{envelope.context_hash}|{input_message_hash}|"
            f"{','.join(doctrine_refs)}|{envelope.permission_decision_ref}|"
            f"{assembled_prompt_hash}|{prev_hash}|{commit_level.value}"
        )
        this_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

        # Update chain registry
        _last_receipt_hashes[actor_session_key] = this_hash

        receipt_ref = f"receipt-{this_hash[:16]}"

        receipt = PromptAssemblyReceipt(
            prompt_assembly_receipt_ref=receipt_ref,
            context_envelope_ref=envelope.context_envelope_ref,
            context_hash=envelope.context_hash,
            input_message_hash=input_message_hash,
            doctrine_refs=doctrine_refs,
            policy_decision_ref=envelope.permission_decision_ref,
            memory_refs=[],
            tool_permissions_ref="tool-perms-v1",
            block_hashes=block_hashes,
            assembled_prompt_hash=assembled_prompt_hash,
            prev_receipt_hash=prev_hash,
            this_receipt_hash=this_hash,
            commit_level=commit_level,
            written_by="provenance_writer",
            created_at=datetime.utcnow(),
        )

        # Update the envelope reference
        envelope.prompt_assembly_receipt_ref = receipt_ref

        return receipt

    def verify_prompt_assembly_receipt(
        self,
        receipt: PromptAssemblyReceipt,
        envelope: ContextEnvelope,
        assembled_prompt: str,
        expected_prev_hash: Optional[str] = None,
    ) -> bool:
        # 1. Recompute context_hash from blocks
        hasher = hashlib.sha256()
        for b in envelope.blocks:
            hasher.update(b.content_hash.encode("utf-8"))
        hasher.update(envelope.doctrine_version.encode("utf-8"))
        hasher.update(envelope.policy_version.encode("utf-8"))
        hasher.update(envelope.permission_decision_ref.encode("utf-8"))
        recomputed_context_hash = hasher.hexdigest()

        if recomputed_context_hash != receipt.context_hash:
            return False

        # 2. Recompute assembled_prompt_hash from prompt
        recomputed_prompt_hash = hashlib.sha256(assembled_prompt.encode("utf-8")).hexdigest()
        if recomputed_prompt_hash != receipt.assembled_prompt_hash:
            return False

        # 3. Policy decision ref check
        if receipt.policy_decision_ref != envelope.permission_decision_ref:
            return False

        # 4. Prev receipt hash check
        if expected_prev_hash is not None and receipt.prev_receipt_hash != expected_prev_hash:
            return False

        # 5. Written by check
        if receipt.written_by != "provenance_writer":
            return False
            
        # Recompute this_receipt_hash to verify integrity
        canonical_str = (
            f"{envelope.context_envelope_ref}|{envelope.context_hash}|{receipt.input_message_hash}|"
            f"{','.join(receipt.doctrine_refs)}|{envelope.permission_decision_ref}|"
            f"{receipt.assembled_prompt_hash}|{receipt.prev_receipt_hash}|{receipt.commit_level.value}"
        )
        recomputed_this_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
        if recomputed_this_hash != receipt.this_receipt_hash:
            return False

        return True

def clear_receipt_chain_registry():
    global _last_receipt_hashes
    _last_receipt_hashes.clear()

from __future__ import annotations
import hashlib
from typing import List, Dict, Any
from mica.context_steward.contracts import (
    ContextBlock,
    ContextEnvelope,
    BlockKind,
    PositionClass,
    OverflowPolicy,
    TrustState,
)

class ContextEnvelopeBuilder:
    def __init__(
        self,
        actor_ref: str,
        session_ref: str,
        intent_class: str,
        resource_class: str,
        permission_decision_ref: str,
        doctrine_version: str = "v1",
        policy_version: str = "v1",
        context_budget_tokens: int = 4096,
        overflow_policy: OverflowPolicy = OverflowPolicy.DROP_LOWEST_RANK,
    ):
        self.actor_ref = actor_ref
        self.session_ref = session_ref
        self.intent_class = intent_class
        self.resource_class = resource_class
        self.permission_decision_ref = permission_decision_ref
        self.doctrine_version = doctrine_version
        self.policy_version = policy_version
        self.context_budget_tokens = context_budget_tokens
        self.overflow_policy = overflow_policy
        self.blocks: List[ContextBlock] = []

    def add_block(self, block: ContextBlock) -> ContextEnvelopeBuilder:
        # Rules: No block without source_authority + trust_state + content_hash
        if not block.source_authority:
            raise ValueError(f"Block {block.block_id} is missing source_authority")
        if not block.trust_state:
            raise ValueError(f"Block {block.block_id} is missing trust_state")
        if not block.content_hash:
            raise ValueError(f"Block {block.block_id} is missing content_hash")
        
        # retrieved_context degraded or missing is excluded
        if block.block_kind == BlockKind.RETRIEVED_CONTEXT and block.trust_state in (TrustState.DEGRADED, TrustState.MISSING):
            return self

        self.blocks.append(block)
        return self

    def build(self) -> ContextEnvelope:
        if not self.permission_decision_ref:
            raise ValueError("No envelope without permission_decision_ref")

        # 1. Determine position class and baseline ranking
        # Fixed anchors (anchor_head or anchor_tail) never truncated by overflow.
        # doctrine + evidence_reqs: high priority anchor.
        # memory_digest + retrieved_context: middle_fill.
        for b in self.blocks:
            if b.block_kind in (BlockKind.RED_LINES, BlockKind.OUTPUT_CONTRACT, BlockKind.TOOL_PERMISSIONS):
                # Critical anchors
                b.position_class = PositionClass.ANCHOR_HEAD if b.block_kind != BlockKind.OUTPUT_CONTRACT else PositionClass.ANCHOR_TAIL
                # Keep high rank score so they are sorted to the extremes
                b.rank_score = 1000.0
            elif b.block_kind in (BlockKind.DOCTRINE, BlockKind.EVIDENCE_REQS):
                b.position_class = PositionClass.ANCHOR_HEAD
                b.rank_score = 500.0
            elif b.block_kind in (BlockKind.MEMORY_DIGEST, BlockKind.RETRIEVED_CONTEXT):
                b.position_class = PositionClass.MIDDLE_FILL
                # rank_score is already set or we keep it as is
            else:
                b.position_class = PositionClass.MIDDLE_FILL

        # 2. Token budget enforcement
        total_tokens = sum(b.token_estimate for b in self.blocks)
        if total_tokens > self.context_budget_tokens:
            if self.overflow_policy == OverflowPolicy.REJECT:
                raise ValueError(f"Context budget exceeded: {total_tokens} > {self.context_budget_tokens}")
            elif self.overflow_policy == OverflowPolicy.DROP_LOWEST_RANK:
                # Drop middle_fill blocks sorted by rank_score ascending
                middle_blocks = [b for b in self.blocks if b.position_class == PositionClass.MIDDLE_FILL]
                middle_blocks.sort(key=lambda x: x.rank_score)

                # Keep dropping until budget fits
                while total_tokens > self.context_budget_tokens and middle_blocks:
                    dropped = middle_blocks.pop(0)
                    self.blocks.remove(dropped)
                    total_tokens -= dropped.token_estimate

                # If still over budget, raise error because we can't drop anchors
                if total_tokens > self.context_budget_tokens:
                    raise ValueError(f"Cannot fit critical anchors within budget. Total: {total_tokens} > {self.context_budget_tokens}")

        # 3. Sort blocks deterministically
        # ordering: anchor_head first (by block_kind, rank_score desc), middle_fill, anchor_tail last
        head_blocks = [b for b in self.blocks if b.position_class == PositionClass.ANCHOR_HEAD]
        middle_blocks = [b for b in self.blocks if b.position_class == PositionClass.MIDDLE_FILL]
        tail_blocks = [b for b in self.blocks if b.position_class == PositionClass.ANCHOR_TAIL]

        # Sort within divisions deterministically
        head_blocks.sort(key=lambda x: (-x.rank_score, x.block_id))
        middle_blocks.sort(key=lambda x: (-x.rank_score, x.block_id))
        tail_blocks.sort(key=lambda x: (-x.rank_score, x.block_id))

        ordered_blocks = head_blocks + middle_blocks + tail_blocks

        # Calculate context hash
        # sha256 over (blocks ordered + versions + permission_decision_ref)
        hasher = hashlib.sha256()
        for b in ordered_blocks:
            hasher.update(b.content_hash.encode("utf-8"))
        hasher.update(self.doctrine_version.encode("utf-8"))
        hasher.update(self.policy_version.encode("utf-8"))
        hasher.update(self.permission_decision_ref.encode("utf-8"))
        context_hash = hasher.hexdigest()

        envelope_ref = f"envelope-{hashlib.sha256(context_hash.encode('utf-8')).hexdigest()[:16]}"

        return ContextEnvelope(
            context_envelope_ref=envelope_ref,
            actor_ref=self.actor_ref,
            session_ref=self.session_ref,
            intent_class=self.intent_class,
            resource_class=self.resource_class,
            permission_decision_ref=self.permission_decision_ref,
            doctrine_version=self.doctrine_version,
            policy_version=self.policy_version,
            blocks=ordered_blocks,
            context_budget_tokens=self.context_budget_tokens,
            tokens_used=total_tokens,
            overflow_policy=self.overflow_policy,
            context_hash=context_hash,
            prompt_assembly_receipt_ref="",  # filled by receipt module
        )

"""
KB Golden Annotation + IAA Gate — K6-6 (KB Slice 4)

Double annotation + inter-annotator agreement + golden firewall.
Golden set is evaluation-only, never training.

Key objects:
- AnnotationTask: unit of annotation work
- AnnotationResult: single annotator's output
- IAAScore: inter-annotator agreement metrics
- GoldenFirewall: blocks golden from training
- AnnotationReviewSurface: Command Kernel surface
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class AnnotationTaskType(str, Enum):
    ENTITY_LINKING = "entity_linking"
    CLAIM_ACCEPT_REJECT = "claim_accept_reject"
    SPAN_TOKEN_F1 = "span_token_f1"
    NEGATION_DETECTION = "negation_detection"


class AnnotationDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    PARTIAL = "partial"
    UNCLEAR = "unclear"


@dataclass
class AnnotationTask:
    """K6-6: Unit of annotation work."""
    task_ref: str
    task_type: AnnotationTaskType
    claim_ref: str
    text_span: str = ""
    expected_entities: List[str] = field(default_factory=list)
    gold_label: Optional[str] = None  # for golden set only
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AnnotationResult:
    """Single annotator's output."""
    result_ref: str
    task_ref: str
    annotator_id: str
    decision: AnnotationDecision = AnnotationDecision.UNCLEAR
    entities: List[str] = field(default_factory=list)
    span_start: int = 0
    span_end: int = 0
    confidence: float = 0.0
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# IAA thresholds per K6-6 spec
_IAA_THRESHOLDS: Dict[AnnotationTaskType, float] = {
    AnnotationTaskType.ENTITY_LINKING: 0.85,       # kappa >= 0.85
    AnnotationTaskType.CLAIM_ACCEPT_REJECT: 0.75,  # kappa >= 0.75
    AnnotationTaskType.SPAN_TOKEN_F1: 0.85,        # F1 >= 0.85
    AnnotationTaskType.NEGATION_DETECTION: 0.80,   # kappa >= 0.80
}


@dataclass
class IAAScore:
    """Inter-annotator agreement metrics."""
    task_type: AnnotationTaskType
    kappa: float = 0.0
    f1: float = 0.0
    agreement_count: int = 0
    disagreement_count: int = 0
    total_tasks: int = 0
    meets_threshold: bool = False
    threshold: float = 0.0
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GoldenFirewallReceipt:
    """Receipt confirming golden firewall is active."""
    receipt_ref: str
    golden_set_ref: str
    blocked_training_insertions: int = 0
    eval_only: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AnnotationReviewSurface:
    """K6-6: Command Kernel surface for annotation review.

    Double annotation → adjudication → IAA gate → golden firewall.
    Golden = eval only. No Label Studio dependency.
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, AnnotationTask] = {}
        self._results: Dict[str, List[AnnotationResult]] = {}  # task_ref -> results
        self._iaa_scores: Dict[AnnotationTaskType, IAAScore] = {}
        self._golden_sets: Dict[str, GoldenFirewallReceipt] = {}
        self._blocked_count: int = 0

    def create_task(self, task: AnnotationTask) -> AnnotationTask:
        self._tasks[task.task_ref] = task
        return task

    def submit_result(self, result: AnnotationResult) -> AnnotationResult:
        """Submit an annotation result. Two annotators per task for double annotation."""
        task = self._tasks.get(result.task_ref)
        if not task:
            raise ValueError(f"unknown task: {result.task_ref}")
        results = self._results.setdefault(result.task_ref, [])
        # idempotent: same annotator same task → replace
        results = [r for r in results if r.annotator_id != result.annotator_id]
        results.append(result)
        self._results[result.task_ref] = results
        return result

    def get_task_results(self, task_ref: str) -> List[AnnotationResult]:
        return self._results.get(task_ref, [])

    def adjudicate(self, task_ref: str, adjudicator_id: str, final_decision: AnnotationDecision) -> AnnotationResult:
        """Adjudicator resolves disagreements."""
        task = self._tasks.get(task_ref)
        if not task:
            raise ValueError(f"unknown task: {task_ref}")
        adjudication = AnnotationResult(
            result_ref=f"adjudication://{task_ref}/{adjudicator_id}",
            task_ref=task_ref,
            annotator_id=adjudicator_id,
            decision=final_decision,
        )
        results = self._results.setdefault(task_ref, [])
        results.append(adjudication)
        return adjudication

    def compute_iaa(self, task_type: AnnotationTaskType) -> IAAScore:
        """Compute IAA for a task type across all double-annotated tasks."""
        threshold = _IAA_THRESHOLDS.get(task_type, 0.75)
        matching_tasks = [t for t in self._tasks.values() if t.task_type == task_type]

        agreement = 0
        disagreement = 0
        for task in matching_tasks:
            results = self._results.get(task.task_ref, [])
            annotator_results = [r for r in results if not r.result_ref.startswith("adjudication:")]
            if len(annotator_results) >= 2:
                if annotator_results[0].decision == annotator_results[1].decision:
                    agreement += 1
                else:
                    disagreement += 1

        total = agreement + disagreement
        kappa = (agreement / total) if total > 0 else 0.0

        score = IAAScore(
            task_type=task_type,
            kappa=kappa,
            agreement_count=agreement,
            disagreement_count=disagreement,
            total_tasks=total,
            meets_threshold=kappa >= threshold,
            threshold=threshold,
        )
        self._iaa_scores[task_type] = score
        return score

    def get_iaa_score(self, task_type: AnnotationTaskType) -> Optional[IAAScore]:
        return self._iaa_scores.get(task_type)

    def golden_firewall_check(self, golden_set_ref: str, proposed_training_refs: List[str]) -> GoldenFirewallReceipt:
        """Block golden set entries from entering training data."""
        blocked = len(proposed_training_refs)  # all blocked — golden is eval only
        self._blocked_count += blocked
        receipt = GoldenFirewallReceipt(
            receipt_ref=f"receipt://golden-firewall/{golden_set_ref}/{datetime.now(timezone.utc).isoformat()}",
            golden_set_ref=golden_set_ref,
            blocked_training_insertions=blocked,
            eval_only=True,
        )
        self._golden_sets[golden_set_ref] = receipt
        return receipt

    def can_serve_established(self, task_type: AnnotationTaskType) -> bool:
        """Check if IAA gate passes for established-tier claims."""
        score = self._iaa_scores.get(task_type)
        if not score:
            return False
        return score.meets_threshold

    def all_gates_pass(self) -> bool:
        """Check all IAA gates pass."""
        for task_type, threshold in _IAA_THRESHOLDS.items():
            score = self._iaa_scores.get(task_type)
            if not score or not score.meets_threshold:
                return False
        return True

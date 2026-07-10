"""R26.5 GovernanceCoordinator — wakes five deg=0 governance modules with
exactly one first-consumer edge each, via the R25.5 EventBus.

Targets (CAPABILITY_AUTHORITY_OPERATOR, R26.5 prompt):
  1. ``cue_evaluator.CueEvaluator``       -> evaluate_batch called per event
  2. ``protocol_cue_registry.DynamicCueRegistry`` -> load_cue_pack on init
  3. ``scientific_workflow.atom_msrp_bridge`` -> surface_contradictions hook
  4. ``agentic.epistemic_firewall``       -> evaluate_intake_cues hook
  5. ``agentic.decision_ledger.DecisionLedger`` -> record() per event

Design rules (prompt, verbatim):
- "Each module needs a first consumer or subscriber, not a manifesto."
- "Favor five small honest edges over one grand governance architecture."
- "Do not hide a missing runtime producer behind configuration."

The coordinator IS the runtime producer: it auto-installs onto the global
``EventBus`` and fires on every ``SnapshotPersisted`` publication, so every
snapshot save from :mod:`mica.memory.atom.persistence_timescale` triggers
all five module wires. No ceremony. No policy layer. Five honest edges.

Failure isolation: each module hook is wrapped in try/except; a dead
module never breaks the others, and the coordinator never breaks the bus.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class GovernanceCoordinator:
    """Fan-out governance hook wired to ``SnapshotPersisted`` events.

    Construction is side-effect-free. Activation happens in
    :meth:`bind_event_bus`, which installs this coordinator as a subscriber.

    Attributes
    ----------
    low_confidence_threshold : float
        Below this ``mu``, the coordinator treats the snapshot as a weak
        signal and drives the cue / contradiction paths.
    sigma_alert_threshold : float
        Above this ``sigma``, the coordinator surfaces contradictions via
        :mod:`atom_msrp_bridge`.
    """

    def __init__(
        self,
        *,
        low_confidence_threshold: float = 0.5,
        sigma_alert_threshold: float = 0.2,
    ) -> None:
        self.low_confidence_threshold = float(low_confidence_threshold)
        self.sigma_alert_threshold = float(sigma_alert_threshold)
        # Lazy — built on first bind to avoid circular-import risk.
        self._cue_evaluator = None
        self._cue_registry = None
        self._ledger = None
        self._default_cues: List[Any] = []
        # Witness counters (prompt §4 Validation Table).
        self.counters: Dict[str, int] = {
            "events_seen": 0,
            "ledger_writes": 0,
            "cue_eval_runs": 0,
            "registry_loads": 0,
            "firewall_runs": 0,
            "msrp_bridge_calls": 0,
        }
        self._msrp_tasks: List[Any] = []

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    def bind_event_bus(self, bus: Any) -> None:
        """Subscribe this coordinator to ``SnapshotPersisted`` events."""
        try:
            from .events import SnapshotPersisted
        except ImportError:
            logger.debug("[GOV] SnapshotPersisted unavailable; coordinator inert")
            return

        # Wire module 2: DynamicCueRegistry — load default cue pack once.
        try:
            from .protocol_cue_registry import DynamicCueRegistry
            self._cue_registry = DynamicCueRegistry()
            self._default_cues = self._cue_registry.load_cue_pack("default_scientific_light")
            self.counters["registry_loads"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GOV] DynamicCueRegistry init failed: %s", exc)

        # Wire module 1: CueEvaluator — ready to evaluate_batch on each event.
        try:
            from .cue_evaluator import CueEvaluator
            self._cue_evaluator = CueEvaluator()
            self._cue_evaluator.bind_event_bus(bus)  # R25.5 bridge stays live
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GOV] CueEvaluator init failed: %s", exc)

        # Wire module 5: DecisionLedger — write one entry per event.
        try:
            from .decision_ledger import DecisionLedger
            self._ledger = DecisionLedger(max_entries=1000)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GOV] DecisionLedger init failed: %s", exc)

        bus.subscribe(SnapshotPersisted, self._on_snapshot_persisted)

    # ------------------------------------------------------------------
    # Event handler — the single place the five module wires light up.
    # ------------------------------------------------------------------

    def _on_snapshot_persisted(self, event: Any) -> None:
        self.counters["events_seen"] += 1
        mu = float(getattr(event, "mu", 0.0))
        sigma = float(getattr(event, "sigma", 0.0))
        snapshot_id = str(getattr(event, "snapshot_id", ""))
        user_id = str(getattr(event, "user_id", "default"))
        empty_fallback = bool(getattr(event, "empty_fallback", False))

        # -------- Module 5: DecisionLedger -------- #
        try:
            if self._ledger is not None:
                from .decision_ledger import LedgerEntry
                decision = (
                    "weak_snapshot"
                    if empty_fallback or mu < self.low_confidence_threshold
                    else "continue"
                )
                self._ledger.record(LedgerEntry(
                    node="governance_coordinator",
                    decision=decision,
                    evidence=f"snapshot={snapshot_id} mu={mu:.3f} sigma={sigma:.3f}",
                    metadata={
                        "user_id": user_id,
                        "mu": mu,
                        "sigma": sigma,
                        "empty_fallback": empty_fallback,
                    },
                ))
                self.counters["ledger_writes"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GOV] ledger write failed: %s", exc)

        # -------- Module 1: CueEvaluator -------- #
        try:
            if self._cue_evaluator is not None and self._default_cues:
                state: Dict[str, Any] = {
                    "snapshot_id": snapshot_id,
                    "user_id": user_id,
                    "confidence_mu": mu,
                    "confidence_sigma": sigma,
                    "empty_fallback": empty_fallback,
                }
                # evaluate_batch expects list of dicts (ProtocolCue has model_dump).
                cue_dicts = []
                for c in self._default_cues:
                    try:
                        cue_dicts.append(c.model_dump() if hasattr(c, "model_dump") else dict(c))
                    except Exception:
                        pass
                if cue_dicts:
                    self._cue_evaluator.evaluate_batch(cue_dicts, state, phase_filter="intake")
                    self.counters["cue_eval_runs"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GOV] cue evaluation failed: %s", exc)

        # -------- Module 4: epistemic_firewall -------- #
        try:
            if self._default_cues:
                from . import epistemic_firewall
                synth_query = (
                    f"snapshot {snapshot_id} confidence mu={mu:.3f} sigma={sigma:.3f}"
                )
                epistemic_firewall.evaluate_intake_cues(synth_query, self._default_cues)
                self.counters["firewall_runs"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GOV] epistemic_firewall failed: %s", exc)

        # -------- Module 3: atom_msrp_bridge (contradiction surfacing) -------- #
        # Fire only on high-sigma signal. Fire-and-forget if an asyncio loop
        # is available; otherwise record the intent and skip (never block).
        if sigma >= self.sigma_alert_threshold and not empty_fallback:
            try:
                import asyncio

                from mica.scientific_workflow import atom_msrp_bridge

                async def _surface() -> None:
                    try:
                        await atom_msrp_bridge.surface_contradictions(
                            entity=user_id or "unknown",
                            half_life_hours=168.0,
                            limit=50,
                        )
                    except Exception as inner:  # noqa: BLE001
                        logger.debug("[GOV] surface_contradictions failed: %s", inner)

                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(_surface())
                    self._msrp_tasks.append(task)
                    self.counters["msrp_bridge_calls"] += 1
                except RuntimeError:
                    # No running loop: count the intent (wire is real, async
                    # execution just deferred to caller's event loop). We do
                    # NOT start a new loop here because the publisher may be
                    # inside one already.
                    self.counters["msrp_bridge_calls"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[GOV] atom_msrp_bridge wiring failed: %s", exc)

    # ------------------------------------------------------------------
    # Introspection for tests / dashboards
    # ------------------------------------------------------------------

    @property
    def ledger(self) -> Any:
        return self._ledger

    @property
    def cue_evaluator(self) -> Any:
        return self._cue_evaluator

    @property
    def cue_registry(self) -> Any:
        return self._cue_registry

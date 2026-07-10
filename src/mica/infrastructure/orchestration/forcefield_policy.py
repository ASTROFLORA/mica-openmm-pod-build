"""forcefield_policy.py — Policy-driven force-field selection for MD simulations.

This module is the single source of truth for selecting a *forcefield mode*.

Important: in this repo, runtime simulation scripts (e.g.
`workers/dynamo/biodynamo/processors/run_binding_simulation_spontaneous.py`) currently
support a small, explicit set of modes:

    - auto
    - charmm36_2024
    - charmm36
    - amber14

So the policy intentionally outputs one of those modes. More granular XML-bundle
selection (openmmforcefields ffxml bundles, water-model swaps, etc.) can be added
later, but must be wired end-to-end (policy → staging → scripts) to be useful.

Anti-rigidity rules enforced:
    R-02: Selection never hardcoded at call sites; always goes through ForceFieldSelector.
    R-08: Any fallback/degradation path includes a non-empty degradation_notice.

Author: MICA Team Gamma (R-05 Architect)
Date: 2026-03-01
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

ForceFieldMode = Literal["auto", "charmm36_2024", "charmm36", "amber14"]
SimulationQuality = Literal["quick_screen", "default", "publication"]
ForceFieldBias = Literal["neutral", "prefer_charmm", "prefer_amber"]


def _normalize_mode(value: Optional[str]) -> Optional[ForceFieldMode]:
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    # Accept a few aliases for convenience.
    if v in {"charmm36m", "charmm36_2024", "charmm36-2024"}:
        return "charmm36_2024"
    if v in {"charmm36", "charmm"}:
        return "charmm36"
    if v in {"amber14", "amber"}:
        return "amber14"
    if v == "auto":
        return "auto"
    return None


def _normalize_bias(value: Optional[str]) -> Optional[ForceFieldBias]:
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v in {"neutral", "none", "auto"}:
        return "neutral"
    if v in {"prefer_charmm", "charmm", "charmm36", "charmm36m"}:
        return "prefer_charmm"
    if v in {"prefer_amber", "amber", "amber14"}:
        return "prefer_amber"
    return None


# ---------------------------------------------------------------------------
# ForceFieldPolicy — immutable configuration bundle
# ---------------------------------------------------------------------------

@dataclass
class ForceFieldPolicy:
    """
    Complete forcefield configuration for a single MD simulation job.

    This replaces all hardcoded forcefield logic in MDJobConfig.__post_init__
    and the STAGE_FF / VERIFY_FF phases of VastMDOrchestrator.

    Anti-rigidity rule R-02: forcefield is NEVER mandatory user input.
    It is always resolved by ForceFieldSelector from intent + system composition.
    """

    # Forcefield mode that downstream scripts understand.
    name: ForceFieldMode = "auto"

    # Simulation knobs (metadata unless a caller explicitly wires them).
    timestep_fs: float = 2.0
    hmr: bool = False

    # Ligand parameterisation preference (metadata today; used in some pipelines).
    ligand_ff: str = "none"  # e.g. "gaff2" | "cgenff" | "openff" | "none"

    # Staging intent: if True, Vast orchestrator should try to stage CHARMM36_2024.
    stage_charmm36_2024: bool = False

    # Non-empty when policy fell back / degraded (R-08).
    degradation_notice: str = ""

    # Optional rationale for logging / debugging.
    rationale: str = ""

    @property
    def is_degraded(self) -> bool:
        """True if this policy was produced by a degradation path (R-08)."""
        return bool(self.degradation_notice)

    @property
    def is_builtin(self) -> bool:
        """True if this policy uses OpenMM built-in assets only."""
        return self.name == "amber14"

    @property
    def requires_staging(self) -> bool:
        """Compatibility alias: True when Vast should stage forcefield assets."""
        return bool(self.stage_charmm36_2024)

    def to_dict(self) -> dict:
        """Serializable representation for events.jsonl logging."""
        return {
            "name": self.name,
            "ligand_ff": self.ligand_ff,
            "timestep_fs": self.timestep_fs,
            "hmr": self.hmr,
            "stage_charmm36_2024": self.stage_charmm36_2024,
            "requires_staging": self.requires_staging,
            "degradation_notice": self.degradation_notice,
            "rationale": self.rationale,
            "is_degraded": self.is_degraded,
            "is_builtin": self.is_builtin,
        }


# ---------------------------------------------------------------------------
# ForceFieldSelector — the single source of truth for FF selection
# ---------------------------------------------------------------------------

class ForceFieldSelector:
    """
    Selects ForceFieldPolicy from intent + system constraints + user overrides.

    Output is always a *mode* understood by downstream scripts:
    auto | charmm36_2024 | charmm36 | amber14

    Priority order:
    1. explicit_ff — user override (validated; otherwise falls back to safe "auto")
    2. CPU-only → safe, no-staging auto (annotated degradation)
    3. ff_bias (optional) → prefer amber/charmm without forcing call sites
    4. publication / long runs / IDP hints → prefer CHARMM36_2024 when stageable
    5. otherwise → auto (scripts pick best available at runtime)
    """

    # Publication keywords that trigger production-grade settings
    PUBLICATION_KEYWORDS = frozenset([
        "publication", "paper", "manuscript", "journal", "submit",
        "production", "publish",
    ])

    # Quick-screen keywords that allow lower-quality settings
    SCREEN_KEYWORDS = frozenset([
        "screen", "fast", "quick", "explore", "preliminary", "draft",
        "test", "trial", "probe",
    ])

    def select(
        self,
        intent: str = "",
        has_ligand: bool = False,
        gpu_available: bool = True,
        duration_ns: float = 50.0,
        explicit_ff: Optional[str] = None,
        ff_bias: Optional[str] = None,
        # New-style inputs (preferred): whether we can stage CHARMM36_2024 from local machine.
        charmm36_2024_local_xml: str = "",
        charmm36_2024_local_dir: str = "",
        # Backward-compatible legacy arg (ignored except for truthiness).
        charmm36m_xml_path: str = "",
        # Optional user / scenario knobs.
        quality: Optional[SimulationQuality] = None,
        is_idp: bool = False,
    ) -> ForceFieldPolicy:
        """
        Select the optimal ForceFieldPolicy for the given simulation context.

        Args:
            intent:              natural language description of the simulation goal
            has_ligand:          True if a ligand SMILES/SDF is present in the context
            gpu_available:       True if a GPU (local or remote) is available
            duration_ns:         planned simulation length in nanoseconds
            explicit_ff:         if user explicitly named a forcefield, use it (override)
            charmm36m_xml_path:  path to a CHARMM ffxml asset (file or directory); "" = not found

        Returns:
            ForceFieldPolicy with possibly non-empty degradation_notice
        """

        stageable_2024 = self._can_stage_charmm36_2024(
            charmm36_2024_local_xml=charmm36_2024_local_xml,
            charmm36_2024_local_dir=charmm36_2024_local_dir,
            charmm36m_xml_path=charmm36m_xml_path,
        )

        # ── Priority 1: explicit user override ─────────────────────────────
        explicit_mode = _normalize_mode(explicit_ff)
        if explicit_ff is not None:
            if explicit_mode is None:
                return ForceFieldPolicy(
                    name="auto",
                    timestep_fs=2.0,
                    hmr=False,
                    ligand_ff=("gaff2" if has_ligand else "none"),
                    stage_charmm36_2024=stageable_2024,
                    degradation_notice=(
                        f"Explicit forcefield '{explicit_ff}' not recognized; falling back to 'auto' (R-08)"
                    ),
                    rationale="explicit_override_unrecognized",
                )

            if explicit_mode == "amber14":
                return ForceFieldPolicy(
                    name="amber14",
                    timestep_fs=2.0,
                    hmr=False,
                    ligand_ff=("gaff2" if has_ligand else "none"),
                    stage_charmm36_2024=False,
                    degradation_notice="",
                    rationale="explicit_override",
                )

            if explicit_mode == "charmm36_2024":
                if stageable_2024:
                    return ForceFieldPolicy(
                        name="charmm36_2024",
                        timestep_fs=2.0,
                        hmr=False,
                        ligand_ff=("cgenff" if has_ligand else "none"),
                        stage_charmm36_2024=True,
                        degradation_notice="",
                        rationale="explicit_override",
                    )
                return ForceFieldPolicy(
                    name="auto",
                    timestep_fs=2.0,
                    hmr=False,
                    ligand_ff=("cgenff" if has_ligand else "none"),
                    stage_charmm36_2024=False,
                    degradation_notice=(
                        "Explicit 'charmm36_2024' requested but local staging assets not available; "
                        "falling back to 'auto' (R-08)"
                    ),
                    rationale="explicit_override_missing_assets",
                )

            # explicit_mode == "charmm36" or "auto"
            return ForceFieldPolicy(
                name=explicit_mode,
                timestep_fs=2.0,
                hmr=False,
                ligand_ff=("cgenff" if has_ligand else "none"),
                stage_charmm36_2024=(stageable_2024 and explicit_mode == "auto"),
                degradation_notice="",
                rationale="explicit_override",
            )

        # ── Priority 2: CPU-only fallback ────────────────────────────────
        # Anti-rigidity: don't force a specific FF family in CPU-only mode.
        # Instead, disable staging and let downstream scripts pick a safe built-in.
        if not gpu_available:
            return ForceFieldPolicy(
                name="auto",
                timestep_fs=2.0,
                hmr=False,
                ligand_ff=("gaff2" if has_ligand else "none"),
                stage_charmm36_2024=False,
                degradation_notice="CPU-only mode — staging disabled; using 'auto' (R-08)",
                rationale="cpu_only_fallback_auto",
            )

        # ── Priority 3+: intent-based selection (bias + quality) ─────────
        intent_lower = intent.lower() if intent else ""
        inferred_quality = quality or self._infer_quality(intent_lower=intent_lower)
        wants_production = inferred_quality == "publication" or duration_ns >= 100.0 or is_idp

        # Decouple integrator knobs from FF family: quality sets timestep/HMR.
        timestep = 4.0 if wants_production else 2.0
        hmr = bool(wants_production)

        bias = _normalize_bias(ff_bias) or self._infer_bias(intent_lower=intent_lower)
        bias = bias or "neutral"

        # Select forcefield mode.
        if bias == "prefer_amber":
            selected_mode: ForceFieldMode = "amber14"
            stage_2024 = False
            rationale = "bias_prefer_amber"
            degradation_notice = ""
        elif bias == "prefer_charmm":
            if stageable_2024:
                selected_mode = "charmm36_2024"
                stage_2024 = True
                rationale = "bias_prefer_charmm_stageable"
                degradation_notice = ""
            else:
                selected_mode = "auto"
                stage_2024 = False
                rationale = "bias_prefer_charmm_missing_assets"
                degradation_notice = (
                    "Forcefield bias prefers CHARMM but local staging assets not available; "
                    "falling back to 'auto' (R-08)"
                )
        else:
            # Neutral: only prefer staged CHARMM36_2024 when production intent exists.
            if wants_production and stageable_2024:
                selected_mode = "charmm36_2024"
                stage_2024 = True
                rationale = "production_prefer_charmm36_2024"
                degradation_notice = ""
            elif wants_production and not stageable_2024:
                selected_mode = "auto"
                stage_2024 = False
                rationale = "production_missing_assets_fallback_auto"
                degradation_notice = (
                    "Production/long-run/IDP intent detected but CHARMM36_2024 local staging assets not available; "
                    "falling back to 'auto' (R-08)"
                )
            else:
                selected_mode = "auto"
                stage_2024 = False
                rationale = "default_auto"
                degradation_notice = ""

        if not has_ligand:
            ligand_ff = "none"
        else:
            ligand_ff = "cgenff" if selected_mode in {"charmm36_2024", "charmm36"} else "gaff2"

        return ForceFieldPolicy(
            name=selected_mode,
            timestep_fs=timestep,
            hmr=hmr,
            ligand_ff=ligand_ff,
            stage_charmm36_2024=stage_2024,
            degradation_notice=degradation_notice,
            rationale=rationale,
        )

    # ── Intent helpers ──────────────────────────────────────────────────────

    def _is_publication_intent(self, intent_lower: str) -> bool:
        """Return True if any publication keyword is found in the intent string."""
        words = re.split(r"[\s\-_/,;:]+", intent_lower)
        return bool(self.PUBLICATION_KEYWORDS.intersection(words))

    def _is_screen_intent(self, intent_lower: str) -> bool:
        """Return True if any quick-screen keyword is found in the intent string."""
        words = re.split(r"[\s\-_/,;:]+", intent_lower)
        return bool(self.SCREEN_KEYWORDS.intersection(words))

    def _infer_quality(self, *, intent_lower: str) -> SimulationQuality:
        if self._is_publication_intent(intent_lower):
            return "publication"
        if self._is_screen_intent(intent_lower):
            return "quick_screen"
        return "default"

    def _infer_bias(self, *, intent_lower: str) -> Optional[ForceFieldBias]:
        """Best-effort bias inference from natural language.

        This is intentionally a *bias* (soft preference), not a hard override.
        Callers who need explicit control should use `explicit_ff`.
        """
        if not intent_lower:
            return None
        # Look for coarse, common tokens.
        if re.search(r"\bamber\b", intent_lower):
            return "prefer_amber"
        if re.search(r"\bcharmm\b", intent_lower):
            return "prefer_charmm"
        return None

    def _can_stage_charmm36_2024(
        self,
        *,
        charmm36_2024_local_xml: str,
        charmm36_2024_local_dir: str,
        charmm36m_xml_path: str,
    ) -> bool:
        """Return True if local machine appears to have CHARMM36_2024 staging assets.

        We intentionally keep this check cheap and permissive; the Vast orchestrator
        will validate paths and degrade gracefully if staging isn't possible.
        """
        if charmm36_2024_local_xml and charmm36_2024_local_dir:
            return True
        return bool(charmm36m_xml_path)


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def select_forcefield(
    intent: str = "",
    has_ligand: bool = False,
    gpu_available: bool = True,
    duration_ns: float = 50.0,
    explicit_ff: Optional[str] = None,
    ff_bias: Optional[str] = None,
    charmm36_2024_local_xml: str = "",
    charmm36_2024_local_dir: str = "",
    charmm36m_xml_path: str = "",
    quality: Optional[SimulationQuality] = None,
    is_idp: bool = False,
) -> ForceFieldPolicy:
    """
    Convenience wrapper around ForceFieldSelector().select(...).

    Suitable for one-line call sites that don't need to hold a selector instance.
    """
    return ForceFieldSelector().select(
        intent=intent,
        has_ligand=has_ligand,
        gpu_available=gpu_available,
        duration_ns=duration_ns,
        explicit_ff=explicit_ff,
        ff_bias=ff_bias,
        charmm36_2024_local_xml=charmm36_2024_local_xml,
        charmm36_2024_local_dir=charmm36_2024_local_dir,
        charmm36m_xml_path=charmm36m_xml_path,
        quality=quality,
        is_idp=is_idp,
    )

"""
mica.drivers.driver_role_manifest
==================================
Declarative boundary enforcement per specialist driver (P2-07).

Each driver role carries:
  - A CapabilityBoundary  (owned / shared / forbidden capability IDs)
  - An ArtifactBoundary   (produces / consumes / forbidden artifact types)

The module-level MANIFEST_TABLE is the single source of truth.
RoleEnforcer provides non-raising check helpers used by routers and gates.

Standard-library only — no third-party deps.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# DriverRole
# ---------------------------------------------------------------------------

class DriverRole(str, enum.Enum):
    """Canonical names for all MICA specialist drivers."""

    ALCHEMIST = "AlchemistDriver"
    BIODYNAMO = "BioDynamoDriver"
    SMIC = "SMICDriver"
    ORCHESTRATOR = "AgenticDriver"


# ---------------------------------------------------------------------------
# RoleCheckResult
# ---------------------------------------------------------------------------

@dataclass
class RoleCheckResult:
    """Outcome of a single boundary check.

    Attributes
    ----------
    allowed:       True if the driver may use the capability or artifact.
    driver_role:   The DriverRole.value string that was checked.
    resource_id:   The capability_id or artifact_type that was inspected.
    reason:        Human-readable explanation of the decision.
    degraded:      True when allowed was granted via degrade-allow (unknown resource).
    """

    allowed: bool
    driver_role: str
    resource_id: str
    reason: str
    degraded: bool

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Serialise to a plain dict (JSON-safe)."""
        return {
            "allowed": self.allowed,
            "driver_role": self.driver_role,
            "resource_id": self.resource_id,
            "reason": self.reason,
            "degraded": self.degraded,
        }

    def is_violation(self) -> bool:
        """Return True only when the check is an outright violation.

        A violation means the driver is *not* allowed AND the result was not
        granted via degradation.
        """
        return not self.allowed and not self.degraded


# ---------------------------------------------------------------------------
# CapabilityBoundary
# ---------------------------------------------------------------------------

@dataclass
class CapabilityBoundary:
    """Declares capability ownership / sharing / prohibition for one driver.

    Attributes
    ----------
    owned:     Capability IDs this driver has primary responsibility for.
    shared:    Capability IDs this driver may use but does not own.
    forbidden: Capability IDs this driver MUST NOT invoke.
    """

    owned: List[str] = field(default_factory=list)
    shared: List[str] = field(default_factory=list)
    forbidden: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ArtifactBoundary
# ---------------------------------------------------------------------------

@dataclass
class ArtifactBoundary:
    """Declares artifact type permissions for one driver.

    Attributes
    ----------
    produces: Artifact types this driver may emit.
    consumes: Artifact types this driver may read.
    forbidden: Artifact types this driver must never touch (produce or consume).
    """

    produces: List[str] = field(default_factory=list)
    consumes: List[str] = field(default_factory=list)
    forbidden: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DriverRoleManifest
# ---------------------------------------------------------------------------

@dataclass
class DriverRoleManifest:
    """Complete boundary specification for a single driver role.

    Attributes
    ----------
    driver_role:          The DriverRole this manifest describes.
    capability_boundary:  CapabilityBoundary for tool capabilities.
    artifact_boundary:    ArtifactBoundary for data artifacts.
    description:          Human-readable summary of the driver's purpose.
    version:              Manifest schema version.
    """

    driver_role: DriverRole
    capability_boundary: CapabilityBoundary
    artifact_boundary: ArtifactBoundary
    description: str
    version: str = "1.0"

    # ------------------------------------------------------------------
    def owns_capability(self, cap_id: str) -> bool:
        """Return True if this driver is the primary owner of *cap_id*."""
        return cap_id in self.capability_boundary.owned

    def can_use_capability(self, cap_id: str) -> bool:
        """Return True if this driver may use *cap_id* (owned **or** shared, not forbidden)."""
        if cap_id in self.capability_boundary.forbidden:
            return False
        return cap_id in self.capability_boundary.owned or cap_id in self.capability_boundary.shared

    def can_produce_artifact(self, artifact_type: str) -> bool:
        """Return True if this driver is allowed to produce the given *artifact_type*."""
        return (
            artifact_type in self.artifact_boundary.produces
            and artifact_type not in self.artifact_boundary.forbidden
        )

    def can_consume_artifact(self, artifact_type: str) -> bool:
        """Return True if this driver is allowed to consume the given *artifact_type*."""
        return (
            artifact_type in self.artifact_boundary.consumes
            and artifact_type not in self.artifact_boundary.forbidden
        )

    def to_dict(self) -> dict:
        """Serialise manifest to a plain nested dict."""
        return {
            "driver_role": self.driver_role.value,
            "version": self.version,
            "description": self.description,
            "capability_boundary": {
                "owned": list(self.capability_boundary.owned),
                "shared": list(self.capability_boundary.shared),
                "forbidden": list(self.capability_boundary.forbidden),
            },
            "artifact_boundary": {
                "produces": list(self.artifact_boundary.produces),
                "consumes": list(self.artifact_boundary.consumes),
                "forbidden": list(self.artifact_boundary.forbidden),
            },
        }


# ---------------------------------------------------------------------------
# MANIFEST_TABLE
# ---------------------------------------------------------------------------

MANIFEST_TABLE: Dict[DriverRole, DriverRoleManifest] = {
    DriverRole.ALCHEMIST: DriverRoleManifest(
        driver_role=DriverRole.ALCHEMIST,
        description=(
            "Handles literature search, SMILES resolution, and molecular docking. "
            "Primary interface to chemical databases and structure sources."
        ),
        capability_boundary=CapabilityBoundary(
            owned=["literature_search", "smiles_resolution", "molecular_docking"],
            shared=["protein_structure_download", "web_search"],
            forbidden=["md_simulation_local", "trajectory_analysis", "qsar_prediction"],
        ),
        artifact_boundary=ArtifactBoundary(
            produces=["smiles", "docked_ligand_pdb", "binding_affinity", "pdb_file", "literature_reference"],
            consumes=["smiles", "pdb_file", "query_string"],
            forbidden=["trajectory_file", "qsar_score"],
        ),
    ),
    DriverRole.BIODYNAMO: DriverRoleManifest(
        driver_role=DriverRole.BIODYNAMO,
        description=(
            "Executes molecular-dynamics simulations and trajectory analysis. "
            "Operates GPUs for local OpenMM runs."
        ),
        capability_boundary=CapabilityBoundary(
            owned=["md_simulation_local", "trajectory_analysis"],
            shared=["protein_structure_download"],
            forbidden=["literature_search", "smiles_resolution", "qsar_prediction"],
        ),
        artifact_boundary=ArtifactBoundary(
            produces=["trajectory_file", "rmsd_plot", "md_log", "energy_csv"],
            consumes=["pdb_file", "docked_ligand_pdb", "smiles", "forcefield_config"],
            forbidden=["binding_affinity", "qsar_score"],
        ),
    ),
    DriverRole.SMIC: DriverRoleManifest(
        driver_role=DriverRole.SMIC,
        description=(
            "Performs QSAR prediction, pathway analysis, and sequence alignment. "
            "Bridges cheminformatics and bioinformatics capabilities."
        ),
        capability_boundary=CapabilityBoundary(
            owned=["qsar_prediction", "pathway_analysis", "sequence_alignment"],
            shared=["literature_search"],
            forbidden=["md_simulation_local", "molecular_docking"],
        ),
        artifact_boundary=ArtifactBoundary(
            produces=["qsar_score", "pathway_map", "alignment_result"],
            consumes=["smiles", "trajectory_file", "pdb_file", "query_string"],
            forbidden=["docked_ligand_pdb", "trajectory_file"],
        ),
    ),
    DriverRole.ORCHESTRATOR: DriverRoleManifest(
        driver_role=DriverRole.ORCHESTRATOR,
        description=(
            "Top-level orchestration only. Does not own or invoke tool capabilities "
            "directly; routes tasks to specialist drivers."
        ),
        capability_boundary=CapabilityBoundary(
            owned=[],
            shared=[],
            forbidden=[
                "md_simulation_local",
                "molecular_docking",
                "qsar_prediction",
                "smiles_resolution",
                "trajectory_analysis",
            ],
        ),
        artifact_boundary=ArtifactBoundary(
            produces=["routing_plan", "workflow_summary"],
            consumes=["routing_plan", "phase_event"],
            forbidden=[],
        ),
    ),
}


# ---------------------------------------------------------------------------
# RoleEnforcer
# ---------------------------------------------------------------------------

class RoleEnforcer:
    """Non-raising boundary checker for MICA driver roles.

    Parameters
    ----------
    manifest_table:
        Optional custom table mapping DriverRole → DriverRoleManifest.
        Defaults to the module-level MANIFEST_TABLE when None.
    """

    def __init__(self, manifest_table: Optional[Dict[DriverRole, DriverRoleManifest]] = None) -> None:
        """Initialise the enforcer with a manifest table."""
        self._table: Dict[DriverRole, DriverRoleManifest] = (
            manifest_table if manifest_table is not None else MANIFEST_TABLE
        )

    # ------------------------------------------------------------------
    def get_manifest(self, driver_role: DriverRole) -> Optional[DriverRoleManifest]:
        """Return the DriverRoleManifest for *driver_role*, or None if unknown."""
        return self._table.get(driver_role)

    # ------------------------------------------------------------------
    def check_capability(self, driver_role: DriverRole, capability_id: str) -> RoleCheckResult:
        """Check whether *driver_role* may use *capability_id*.

        Decision logic (never raises):
          - forbidden  → allowed=False, degraded=False
          - owned or shared → allowed=True, degraded=False
          - completely unknown → degrade-allow: allowed=True, degraded=True
          - any error while checking → degrade-allow
        """
        try:
            manifest = self._table.get(driver_role)
            if manifest is None:
                return RoleCheckResult(
                    allowed=True,
                    driver_role=driver_role.value if isinstance(driver_role, DriverRole) else str(driver_role),
                    resource_id=capability_id,
                    reason=f"Unknown driver role '{driver_role}'; degrade-allow.",
                    degraded=True,
                )

            cb = manifest.capability_boundary
            role_val = driver_role.value

            if capability_id in cb.forbidden:
                return RoleCheckResult(
                    allowed=False,
                    driver_role=role_val,
                    resource_id=capability_id,
                    reason=f"Capability '{capability_id}' is explicitly forbidden for {role_val}.",
                    degraded=False,
                )

            if capability_id in cb.owned:
                return RoleCheckResult(
                    allowed=True,
                    driver_role=role_val,
                    resource_id=capability_id,
                    reason=f"Capability '{capability_id}' is owned by {role_val}.",
                    degraded=False,
                )

            if capability_id in cb.shared:
                return RoleCheckResult(
                    allowed=True,
                    driver_role=role_val,
                    resource_id=capability_id,
                    reason=f"Capability '{capability_id}' is shared and accessible to {role_val}.",
                    degraded=False,
                )

            # Unknown capability → degrade-allow
            return RoleCheckResult(
                allowed=True,
                driver_role=role_val,
                resource_id=capability_id,
                reason=(
                    f"Capability '{capability_id}' is not declared for {role_val}; "
                    "degrade-allow applied."
                ),
                degraded=True,
            )

        except Exception as exc:  # noqa: BLE001
            role_str = driver_role.value if isinstance(driver_role, DriverRole) else str(driver_role)
            return RoleCheckResult(
                allowed=True,
                driver_role=role_str,
                resource_id=capability_id,
                reason=f"Error during capability check ({exc!r}); degrade-allow applied.",
                degraded=True,
            )

    # ------------------------------------------------------------------
    def check_artifact(
        self,
        driver_role: DriverRole,
        artifact_type: str,
        mode: str = "produce",
    ) -> RoleCheckResult:
        """Check whether *driver_role* may produce or consume *artifact_type*.

        Parameters
        ----------
        mode: ``"produce"`` or ``"consume"``

        Decision logic (never raises):
          - artifact in forbidden → allowed=False, degraded=False
          - artifact in produces/consumes (matching mode) → allowed=True, degraded=False
          - completely unlisted → degrade-allow
          - any error → degrade-allow
        """
        try:
            manifest = self._table.get(driver_role)
            if manifest is None:
                return RoleCheckResult(
                    allowed=True,
                    driver_role=driver_role.value if isinstance(driver_role, DriverRole) else str(driver_role),
                    resource_id=artifact_type,
                    reason=f"Unknown driver role '{driver_role}'; degrade-allow.",
                    degraded=True,
                )

            ab = manifest.artifact_boundary
            role_val = driver_role.value

            # Forbidden wins over everything
            if artifact_type in ab.forbidden:
                return RoleCheckResult(
                    allowed=False,
                    driver_role=role_val,
                    resource_id=artifact_type,
                    reason=(
                        f"Artifact '{artifact_type}' is forbidden for {role_val} "
                        f"(mode='{mode}')."
                    ),
                    degraded=False,
                )

            if mode == "produce":
                target_list = ab.produces
                label = "produced"
            else:
                target_list = ab.consumes
                label = "consumed"

            if artifact_type in target_list:
                return RoleCheckResult(
                    allowed=True,
                    driver_role=role_val,
                    resource_id=artifact_type,
                    reason=f"Artifact '{artifact_type}' may be {label} by {role_val}.",
                    degraded=False,
                )

            # Not listed for this mode → degrade-allow
            return RoleCheckResult(
                allowed=True,
                driver_role=role_val,
                resource_id=artifact_type,
                reason=(
                    f"Artifact '{artifact_type}' is not declared for {role_val} "
                    f"(mode='{mode}'); degrade-allow applied."
                ),
                degraded=True,
            )

        except Exception as exc:  # noqa: BLE001
            role_str = driver_role.value if isinstance(driver_role, DriverRole) else str(driver_role)
            return RoleCheckResult(
                allowed=True,
                driver_role=role_str,
                resource_id=artifact_type,
                reason=f"Error during artifact check ({exc!r}); degrade-allow applied.",
                degraded=True,
            )

    # ------------------------------------------------------------------
    def enforce_routing_plan(
        self,
        driver_role: DriverRole,
        capability_ids: List[str],
    ) -> List[RoleCheckResult]:
        """Batch-check all *capability_ids* for *driver_role*.

        Returns a list of RoleCheckResult in the same order as *capability_ids*.
        Useful for up-front validation when a routing plan is assembled.
        """
        return [self.check_capability(driver_role, cap_id) for cap_id in capability_ids]

    # ------------------------------------------------------------------
    @staticmethod
    def violations(results: List[RoleCheckResult]) -> List[RoleCheckResult]:
        """Filter *results* to only those where ``is_violation()`` is True."""
        return [r for r in results if r.is_violation()]

    # ------------------------------------------------------------------
    @staticmethod
    def summary(results: List[RoleCheckResult]) -> str:
        """Return a human-readable one-liner: ``"N allowed, M degraded, K violations"``."""
        allowed_count = sum(1 for r in results if r.allowed and not r.degraded)
        degraded_count = sum(1 for r in results if r.degraded)
        violation_count = sum(1 for r in results if r.is_violation())
        return f"{allowed_count} allowed, {degraded_count} degraded, {violation_count} violations"

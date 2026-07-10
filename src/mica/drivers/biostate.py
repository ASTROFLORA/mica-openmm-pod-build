from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Literal, Mapping, Tuple


def _mapping_dict(raw: Any) -> Dict[str, Any]:
    return dict(raw or {}) if isinstance(raw, Mapping) else {}


def _float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_or_default(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _ion_species_target_from_dict(raw: Any) -> "IonSpeciesTarget":
    payload = _mapping_dict(raw)
    return IonSpeciesTarget(
        species=str(payload.get("species", "") or ""),
        concentration_mM=_float_or_default(payload.get("concentration_mM"), 0.0),
        valence=int(payload.get("valence", 0) or 0),
        role=str(payload.get("role", "major_cation") or "major_cation"),
        insertion_phase=str(payload.get("insertion_phase", "bulk_addsolvent") or "bulk_addsolvent"),
    )


def _donnan_background_from_dict(raw: Any) -> "DonnanBackgroundModel":
    payload = _mapping_dict(raw)
    return DonnanBackgroundModel(
        mode=str(payload.get("mode", "fixed_anion_gap") or "fixed_anion_gap"),
        fixed_anion_gap_mM=_float_or_default(payload.get("fixed_anion_gap_mM"), 0.0),
        source=str(payload.get("source", "") or ""),
    )


def _physiological_compartment_profile_from_dict(
    raw: Any,
) -> "PhysiologicalCompartmentProfile | None":
    payload = _mapping_dict(raw)
    if not payload:
        return None
    return PhysiologicalCompartmentProfile(
        profile_id=str(payload.get("profile_id", "") or ""),
        compartment=str(payload.get("compartment", "") or ""),
        organism=str(payload.get("organism", "homo_sapiens") or "homo_sapiens"),
        target_pH=_float_or_default(payload.get("target_pH"), 7.0),
        osmolarity_Osm=_float_or_default(payload.get("osmolarity_Osm"), 0.290),
        temperature_k=_float_or_default(payload.get("temperature_k"), 310.0),
        major_ions=tuple(
            _ion_species_target_from_dict(item) for item in (payload.get("major_ions") or ())
        ),
        minority_ions=tuple(
            _ion_species_target_from_dict(item) for item in (payload.get("minority_ions") or ())
        ),
        donnan_background=_donnan_background_from_dict(payload.get("donnan_background")),
        validated_against=str(payload.get("validated_against", "") or ""),
    )


def _protonation_policy_from_dict(raw: Any) -> "ProtonationPolicy":
    payload = _mapping_dict(raw)
    return ProtonationPolicy(
        engine=str(payload.get("engine", "propka3") or "propka3"),
        repair_backend=str(payload.get("repair_backend", "pdb2pqr") or "pdb2pqr"),
        ph_source=str(payload.get("ph_source", "profile") or "profile"),
        histidine_policy=str(
            payload.get("histidine_policy", "optimize_hbond_network") or "optimize_hbond_network"
        ),
        fallback_mode=str(payload.get("fallback_mode", "fail_loud") or "fail_loud"),
        allow_extreme_ph=_bool_or_default(payload.get("allow_extreme_ph"), True),
    )


def _ligand_parameterization_policy_from_dict(raw: Any) -> "LigandParameterizationPolicy":
    payload = _mapping_dict(raw)
    return LigandParameterizationPolicy(
        ligand_id=str(payload.get("ligand_id", "") or ""),
        forcefield=str(payload.get("forcefield", "openff-2.2.1-sage") or "openff-2.2.1-sage"),
        charge_backend=str(payload.get("charge_backend", "espaloma") or "espaloma"),
        fallback_charge_backend=str(
            payload.get("fallback_charge_backend", "am1bcc") or "am1bcc"
        ),
        stereochemistry_source=str(
            payload.get("stereochemistry_source", "input_smiles") or "input_smiles"
        ),
        tautomer_policy=str(payload.get("tautomer_policy", "keep_input") or "keep_input"),
        metal_coordination_mode=str(
            payload.get("metal_coordination_mode", "template_lookup") or "template_lookup"
        ),
    )


def _membrane_leaflet_composition_from_dict(raw: Any, default_leaflet: str) -> "MembraneLeafletComposition":
    payload = _mapping_dict(raw)
    lipid_entries = payload.get("lipids") or ()
    normalized_lipids: list[tuple[str, float]] = []
    for entry in lipid_entries:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            normalized_lipids.append((str(entry[0] or ""), _float_or_default(entry[1], 0.0)))
    return MembraneLeafletComposition(
        leaflet=str(payload.get("leaflet", default_leaflet) or default_leaflet),
        lipids=tuple(normalized_lipids),
    )


def _membrane_assembly_intent_from_dict(raw: Any) -> "MembraneAssemblyIntent":
    payload = _mapping_dict(raw)
    return MembraneAssemblyIntent(
        enabled=_bool_or_default(payload.get("enabled"), False),
        packing_backend=str(payload.get("packing_backend", "packmol") or "packmol"),
        orientation_backend=str(payload.get("orientation_backend", "memembed") or "memembed"),
        upper_leaflet=_membrane_leaflet_composition_from_dict(payload.get("upper_leaflet"), "upper"),
        lower_leaflet=_membrane_leaflet_composition_from_dict(payload.get("lower_leaflet"), "lower"),
        padding_nm=_float_or_default(payload.get("padding_nm"), 1.2),
        prune_cutoff_angstrom=_float_or_default(payload.get("prune_cutoff_angstrom"), 1.6),
        box_shape=str(payload.get("box_shape", "truncated_octahedron") or "truncated_octahedron"),
    )


def _solvent_assembly_policy_from_dict(raw: Any) -> "SolventAssemblyPolicy":
    payload = _mapping_dict(raw)
    return SolventAssemblyPolicy(
        water_model=str(payload.get("water_model", "tip3p") or "tip3p"),
        neutralization_mode=str(
            payload.get("neutralization_mode", "constant_ionic_strength")
            or "constant_ionic_strength"
        ),
        major_ion_backend=str(payload.get("major_ion_backend", "openmm_addsolvent") or "openmm_addsolvent"),
        minority_ion_backend=str(
            payload.get("minority_ion_backend", "water_replacement") or "water_replacement"
        ),
        ion_placement_backend=str(payload.get("ion_placement_backend", "mdanalysis") or "mdanalysis"),
        require_profile_match=_bool_or_default(payload.get("require_profile_match"), True),
    )


def _topology_preparation_context_from_dict(raw: Any) -> "TopologyPreparationContext":
    payload = _mapping_dict(raw)
    return TopologyPreparationContext(
        ligand_policies=tuple(
            _ligand_parameterization_policy_from_dict(item)
            for item in (payload.get("ligand_policies") or ())
        ),
        membrane=_membrane_assembly_intent_from_dict(payload.get("membrane")),
        solvation=_solvent_assembly_policy_from_dict(payload.get("solvation")),
    )


@dataclass(frozen=True)
class IonConditions:
    ionic_strength_molar: float = 0.15
    positive_ion: str = "Na+"
    negative_ion: str = "Cl-"


@dataclass(frozen=True)
class PhasePlan:
    duration_ns: float
    steps: int
    temperature_k: float | None = None
    pressure_atm: float | None = None


@dataclass(frozen=True)
class BVSSettings:
    enabled: bool = False
    collective_variables: Tuple[str, ...] = ()
    stride: int = 0


@dataclass(frozen=True)
class MLPotentialSettings:
    requested: bool = False
    engine: str = ""
    supported: bool = False
    augmenter: str = ""
    model: str = ""
    region_selector: str = ""
    selector_contract: str = "potential_region_selector_v2"


@dataclass(frozen=True)
class ArtifactManifestExpectations:
    required_artifact_types: Tuple[str, ...] = (
        "trajectory_dcd",
        "energy_csv",
    )
    durability_artifact_types: Tuple[str, ...] = (
        "final_checkpoint",
    )
    require_segment_evidence: bool = False


@dataclass(frozen=True)
class ScriptIntegrityReceipt:
    script_name: str
    sha256: str
    base_dir: str


@dataclass(frozen=True)
class ProvenanceIDs:
    request_id: str = ""
    session_id: str = ""
    job_id: str = ""


@dataclass(frozen=True)
class PhysiologicalBufferSnapshot:
    name: str = ""
    cellular_compartment: str = ""
    organism: str = "homo_sapiens"
    sodium_mM: float = 0.0
    potassium_mM: float = 0.0
    chloride_mM: float = 0.0
    magnesium_mM: float = 0.0
    calcium_uM: float = 0.0
    phosphate_mM: float = 0.0
    bicarbonate_mM: float = 0.0
    pH: float | None = None
    osmolarity_Osm: float | None = None
    ionic_strength_molar: float | None = None
    validated_against: str = ""


@dataclass(frozen=True)
class IonSpeciesTarget:
    species: str
    concentration_mM: float
    valence: int
    role: Literal[
        "major_cation",
        "major_anion",
        "minority_cation",
        "minority_anion",
        "counter_ion",
    ] = "major_cation"
    insertion_phase: Literal["bulk_addsolvent", "minority_replace"] = "bulk_addsolvent"


@dataclass(frozen=True)
class DonnanBackgroundModel:
    mode: Literal["none", "fixed_anion_gap", "crowding_proxy"] = "fixed_anion_gap"
    fixed_anion_gap_mM: float = 0.0
    source: str = ""


@dataclass(frozen=True)
class PhysiologicalCompartmentProfile:
    profile_id: str = ""
    compartment: str = ""
    organism: str = "homo_sapiens"
    target_pH: float = 7.0
    osmolarity_Osm: float = 0.290
    temperature_k: float = 310.0
    major_ions: Tuple[IonSpeciesTarget, ...] = ()
    minority_ions: Tuple[IonSpeciesTarget, ...] = ()
    donnan_background: DonnanBackgroundModel = field(default_factory=DonnanBackgroundModel)
    validated_against: str = ""


@dataclass(frozen=True)
class ProtonationPolicy:
    engine: Literal["propka3", "pdbfixer_only"] = "propka3"
    repair_backend: Literal["pdb2pqr", "pdbfixer"] = "pdb2pqr"
    ph_source: Literal["profile", "explicit_override"] = "profile"
    histidine_policy: Literal["optimize_hbond_network", "keep_input"] = "optimize_hbond_network"
    fallback_mode: Literal["fail_loud", "fallback_to_pdbfixer"] = "fail_loud"
    allow_extreme_ph: bool = True


@dataclass(frozen=True)
class PhysiologyContext:
    cellular_compartment: str = ""
    organism: str = "homo_sapiens"
    cell_type: str = ""
    temperature_k: float | None = None
    crowding_g_l: float | None = None
    crowding_model: str = ""
    protonation_strategy: str = ""
    buffer: PhysiologicalBufferSnapshot = field(default_factory=PhysiologicalBufferSnapshot)
    compartment_profile_id: str = ""
    compartment_profile_version: str = ""
    compartment_profile_override_reason: str = ""
    target_pH: float | None = None
    protonation_policy: ProtonationPolicy = field(default_factory=ProtonationPolicy)
    solvent_profile: PhysiologicalCompartmentProfile | None = None
    enable_donnan_correction: bool = True


@dataclass(frozen=True)
class LigandParameterizationPolicy:
    ligand_id: str = ""
    forcefield: str = "openff-2.2.1-sage"
    charge_backend: Literal["espaloma", "nagl", "am1bcc"] = "espaloma"
    fallback_charge_backend: Literal["am1bcc", "none"] = "am1bcc"
    stereochemistry_source: Literal["input_smiles", "rdkit_assign", "boltz_pose"] = "input_smiles"
    tautomer_policy: Literal["keep_input", "enumerate_and_select"] = "keep_input"
    metal_coordination_mode: Literal["template_lookup", "explicit_constraints", "none"] = "template_lookup"


@dataclass(frozen=True)
class MembraneLeafletComposition:
    leaflet: Literal["upper", "lower"] = "upper"
    lipids: Tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class MembraneAssemblyIntent:
    enabled: bool = False
    packing_backend: Literal["packmol", "mdanalysis", "ts2cg"] = "packmol"
    orientation_backend: Literal["memembed", "principal_axis", "manual"] = "memembed"
    upper_leaflet: MembraneLeafletComposition = field(
        default_factory=lambda: MembraneLeafletComposition("upper")
    )
    lower_leaflet: MembraneLeafletComposition = field(
        default_factory=lambda: MembraneLeafletComposition("lower")
    )
    padding_nm: float = 1.2
    prune_cutoff_angstrom: float = 1.6
    box_shape: Literal["truncated_octahedron", "dodecahedron", "orthorhombic"] = "truncated_octahedron"


@dataclass(frozen=True)
class SolventAssemblyPolicy:
    water_model: str = "tip3p"
    neutralization_mode: Literal["constant_ionic_strength", "charge_only"] = "constant_ionic_strength"
    major_ion_backend: Literal["openmm_addsolvent"] = "openmm_addsolvent"
    minority_ion_backend: Literal["water_replacement", "none"] = "water_replacement"
    ion_placement_backend: Literal["packmol", "mdanalysis"] = "mdanalysis"
    require_profile_match: bool = True


@dataclass(frozen=True)
class TopologyPreparationContext:
    ligand_policies: Tuple[LigandParameterizationPolicy, ...] = ()
    membrane: MembraneAssemblyIntent = field(default_factory=MembraneAssemblyIntent)
    solvation: SolventAssemblyPolicy = field(default_factory=SolventAssemblyPolicy)


@dataclass(frozen=True)
class ProtocolContext:
    execution_target: str = ""
    execution_class: str = "research"
    md_engine: str = "openmm"
    integrator: str = ""
    sampling_strategy: str = ""
    scientific_phases: Tuple[str, ...] = ("minimization", "equilibration", "production")
    checkpoint_policy: str = "strict"
    require_segment_evidence: bool = False
    storage_backend: str = "none"
    template_id: str = ""
    template_version: str = ""


@dataclass(frozen=True)
class EconomicsContext:
    max_price_per_hour: float | None = None
    max_total_cost_usd: float | None = None
    max_runtime_hours: float | None = None
    require_quote: bool = False
    require_approval: bool = False
    budget_bucket: str = ""
    cost_center: str = ""
    preserve_instance_on_failure: bool = True


@dataclass(frozen=True)
class ProteinComponent:
    id: str = ""
    source: str = ""
    chain_selection: str = ""
    mutations: Tuple[str, ...] = ()
    ptms: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LigandComponent:
    id: str = ""
    smiles: str = ""
    pose_uri: str = ""
    sdf_uri: str = ""
    provenance: str = ""


@dataclass(frozen=True)
class PeptideComponent:
    id: str = ""
    sequence: str = ""
    source: str = ""


@dataclass(frozen=True)
class BioStateComponents:
    proteins: Tuple[ProteinComponent, ...] = ()
    ligands: Tuple[LigandComponent, ...] = ()
    peptides: Tuple[PeptideComponent, ...] = ()


@dataclass(frozen=True)
class SiteReference:
    binding_site_id: str = ""
    residues: Tuple[str, ...] = ()
    atlas_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LineageMetadata:
    source_run_id: str = ""
    source_session_id: str = ""
    parent_biostate_id: str = ""
    handoff_from: str = ""
    handoff_step: str = ""
    trace_refs: Tuple[str, ...] = ()
    source_identities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BioStateSeedPacket:
    seed_type: str = "biostate_seed_v1"
    source_plane: str = ""
    provider_model_id: str = ""
    preferred_structure_ref: str = ""
    canonical_accession: str = ""
    isoform_accession: str = ""
    structure_name: str = ""
    structure_format: str = "pdb"
    structure_artifact_uri: str = ""
    inline_structure_text: str = ""
    chain_map: Dict[str, Any] = field(default_factory=dict)
    component_id: str = ""
    topology_region_id: str = ""
    membrane_required: bool = False
    forcefield_family: str = "amber14sb"
    water_model: str = "tip3p"
    task: str = "stability"
    requested_assay: str = "stability"
    scientific_question: str = ""
    lmp_refs: Tuple[str, ...] = ()
    upstream_artifacts: Tuple[str, ...] = ()
    lineage_metadata: LineageMetadata = field(default_factory=LineageMetadata)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BioStateSeedPacket":
        lineage_payload = _mapping_dict(payload.get("lineage_metadata"))
        return cls(
            seed_type=str(payload.get("seed_type", "biostate_seed_v1") or "biostate_seed_v1"),
            source_plane=str(payload.get("source_plane", "") or ""),
            provider_model_id=str(payload.get("provider_model_id", "") or ""),
            preferred_structure_ref=str(payload.get("preferred_structure_ref", "") or ""),
            canonical_accession=str(payload.get("canonical_accession", "") or ""),
            isoform_accession=str(payload.get("isoform_accession", "") or ""),
            structure_name=str(payload.get("structure_name", "") or ""),
            structure_format=str(payload.get("structure_format", "pdb") or "pdb"),
            structure_artifact_uri=str(payload.get("structure_artifact_uri", "") or ""),
            inline_structure_text=str(payload.get("inline_structure_text", "") or ""),
            chain_map=dict(payload.get("chain_map") or {}) if isinstance(payload.get("chain_map"), Mapping) else {},
            component_id=str(payload.get("component_id", "") or ""),
            topology_region_id=str(payload.get("topology_region_id", "") or ""),
            membrane_required=_bool_or_default(payload.get("membrane_required"), False),
            forcefield_family=str(payload.get("forcefield_family", "amber14sb") or "amber14sb"),
            water_model=str(payload.get("water_model", "tip3p") or "tip3p"),
            task=str(payload.get("task", "stability") or "stability"),
            requested_assay=str(payload.get("requested_assay", "stability") or "stability"),
            scientific_question=str(payload.get("scientific_question", "") or ""),
            lmp_refs=tuple(payload.get("lmp_refs") or ()),
            upstream_artifacts=tuple(payload.get("upstream_artifacts") or ()),
            lineage_metadata=LineageMetadata(
                source_run_id=str(lineage_payload.get("source_run_id", "") or ""),
                source_session_id=str(lineage_payload.get("source_session_id", "") or ""),
                parent_biostate_id=str(lineage_payload.get("parent_biostate_id", "") or ""),
                handoff_from=str(lineage_payload.get("handoff_from", "") or ""),
                handoff_step=str(lineage_payload.get("handoff_step", "") or ""),
                trace_refs=tuple(lineage_payload.get("trace_refs") or ()),
                source_identities=tuple(lineage_payload.get("source_identities") or ()),
            ),
        )


@dataclass(frozen=True)
class BioState:
    structure_input_uri: str
    ligand_input_uri: str
    forcefield_family: str
    water_model: str
    ion_conditions: IonConditions
    minimization_plan: PhasePlan
    equilibration_plan: PhasePlan
    production_plan: PhasePlan
    bvs_settings: BVSSettings = field(default_factory=BVSSettings)
    ml_potential_settings: MLPotentialSettings = field(default_factory=MLPotentialSettings)
    artifact_manifest_expectations: ArtifactManifestExpectations = field(
        default_factory=ArtifactManifestExpectations
    )
    script_integrity_receipt: ScriptIntegrityReceipt = field(
        default_factory=lambda: ScriptIntegrityReceipt(script_name="", sha256="", base_dir="")
    )
    provenance_ids: ProvenanceIDs = field(default_factory=ProvenanceIDs)
    schema_version: str = "biostate_envelope_v1"
    task: str = "protein_ligand_md"
    requested_assay: str = "stability"
    scientific_question: str = ""
    mode_key: str = "standard_prod"
    physiology: PhysiologyContext = field(default_factory=PhysiologyContext)
    topology_preparation: TopologyPreparationContext = field(default_factory=TopologyPreparationContext)
    protocol: ProtocolContext = field(default_factory=ProtocolContext)
    economics: EconomicsContext = field(default_factory=EconomicsContext)
    components: BioStateComponents = field(default_factory=BioStateComponents)
    sites: Tuple[SiteReference, ...] = ()
    atlas_refs: Tuple[str, ...] = ()
    output_prefix: str = ""
    produced_by: str = "user"
    upstream_artifacts: Tuple[str, ...] = ()
    lmp_refs: Tuple[str, ...] = ()
    lineage_metadata: LineageMetadata = field(default_factory=LineageMetadata)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BioState":
        physiology_payload = _mapping_dict(payload.get("physiology"))
        buffer_payload = _mapping_dict(
            physiology_payload.get("buffer") or physiology_payload.get("physiological_buffer")
        )
        return cls(
            structure_input_uri=str(payload.get("structure_input_uri", "") or ""),
            ligand_input_uri=str(payload.get("ligand_input_uri", "") or ""),
            forcefield_family=str(payload.get("forcefield_family", "") or ""),
            water_model=str(payload.get("water_model", "") or ""),
            ion_conditions=IonConditions(**dict(payload.get("ion_conditions") or {})),
            minimization_plan=PhasePlan(**dict(payload.get("minimization_plan") or {})),
            equilibration_plan=PhasePlan(**dict(payload.get("equilibration_plan") or {})),
            production_plan=PhasePlan(**dict(payload.get("production_plan") or {})),
            bvs_settings=BVSSettings(**dict(payload.get("bvs_settings") or {})),
            ml_potential_settings=MLPotentialSettings(
                **dict(payload.get("ml_potential_settings") or {})
            ),
            artifact_manifest_expectations=ArtifactManifestExpectations(
                **dict(payload.get("artifact_manifest_expectations") or {})
            ),
            script_integrity_receipt=ScriptIntegrityReceipt(
                **dict(payload.get("script_integrity_receipt") or {})
            ),
            provenance_ids=ProvenanceIDs(**dict(payload.get("provenance_ids") or {})),
            schema_version=str(payload.get("schema_version", "biostate_envelope_v1") or "biostate_envelope_v1"),
            task=str(payload.get("task", "protein_ligand_md") or "protein_ligand_md"),
            requested_assay=str(payload.get("requested_assay", "stability") or "stability"),
            scientific_question=str(payload.get("scientific_question", "") or ""),
            mode_key=str(payload.get("mode_key", "standard_prod") or "standard_prod"),
            physiology=PhysiologyContext(
                cellular_compartment=str(physiology_payload.get("cellular_compartment", "") or ""),
                organism=str(physiology_payload.get("organism", "homo_sapiens") or "homo_sapiens"),
                cell_type=str(physiology_payload.get("cell_type", "") or ""),
                temperature_k=physiology_payload.get("temperature_k"),
                crowding_g_l=physiology_payload.get("crowding_g_l"),
                crowding_model=str(physiology_payload.get("crowding_model", "") or ""),
                protonation_strategy=str(physiology_payload.get("protonation_strategy", "") or ""),
                buffer=PhysiologicalBufferSnapshot(
                    name=str(buffer_payload.get("name", "") or ""),
                    cellular_compartment=str(buffer_payload.get("cellular_compartment", "") or ""),
                    organism=str(buffer_payload.get("organism", physiology_payload.get("organism", "homo_sapiens")) or "homo_sapiens"),
                    sodium_mM=_float_or_default(buffer_payload.get("sodium_mM"), 0.0),
                    potassium_mM=_float_or_default(buffer_payload.get("potassium_mM"), 0.0),
                    chloride_mM=_float_or_default(buffer_payload.get("chloride_mM"), 0.0),
                    magnesium_mM=_float_or_default(buffer_payload.get("magnesium_mM"), 0.0),
                    calcium_uM=_float_or_default(buffer_payload.get("calcium_uM"), 0.0),
                    phosphate_mM=_float_or_default(buffer_payload.get("phosphate_mM"), 0.0),
                    bicarbonate_mM=_float_or_default(buffer_payload.get("bicarbonate_mM"), 0.0),
                    pH=buffer_payload.get("pH"),
                    osmolarity_Osm=buffer_payload.get("osmolarity_Osm"),
                    ionic_strength_molar=buffer_payload.get("ionic_strength_molar"),
                    validated_against=str(buffer_payload.get("validated_against", "") or ""),
                ),
                compartment_profile_id=str(physiology_payload.get("compartment_profile_id", "") or ""),
                compartment_profile_version=str(
                    physiology_payload.get("compartment_profile_version", "") or ""
                ),
                compartment_profile_override_reason=str(
                    physiology_payload.get("compartment_profile_override_reason", "") or ""
                ),
                target_pH=physiology_payload.get("target_pH"),
                protonation_policy=_protonation_policy_from_dict(
                    physiology_payload.get("protonation_policy")
                ),
                solvent_profile=_physiological_compartment_profile_from_dict(
                    physiology_payload.get("solvent_profile")
                ),
                enable_donnan_correction=_bool_or_default(
                    physiology_payload.get("enable_donnan_correction"),
                    True,
                ),
            ),
            topology_preparation=_topology_preparation_context_from_dict(
                payload.get("topology_preparation")
            ),
            protocol=ProtocolContext(
                execution_target=str(dict(payload.get("protocol") or {}).get("execution_target", "") or ""),
                execution_class=str(dict(payload.get("protocol") or {}).get("execution_class", "research") or "research"),
                md_engine=str(dict(payload.get("protocol") or {}).get("md_engine", "openmm") or "openmm"),
                integrator=str(dict(payload.get("protocol") or {}).get("integrator", "") or ""),
                sampling_strategy=str(dict(payload.get("protocol") or {}).get("sampling_strategy", "") or ""),
                scientific_phases=tuple(dict(payload.get("protocol") or {}).get("scientific_phases") or dict(payload.get("protocol") or {}).get("phases") or ("minimization", "equilibration", "production")),
                checkpoint_policy=str(dict(payload.get("protocol") or {}).get("checkpoint_policy", "strict") or "strict"),
                require_segment_evidence=bool(dict(payload.get("protocol") or {}).get("require_segment_evidence", False)),
                storage_backend=str(dict(payload.get("protocol") or {}).get("storage_backend", "none") or "none"),
                template_id=str(dict(payload.get("protocol") or {}).get("template_id", "") or ""),
                template_version=str(dict(payload.get("protocol") or {}).get("template_version", "") or ""),
            ),
            economics=EconomicsContext(
                max_price_per_hour=dict(payload.get("economics") or {}).get("max_price_per_hour"),
                max_total_cost_usd=dict(payload.get("economics") or {}).get("max_total_cost_usd"),
                max_runtime_hours=dict(payload.get("economics") or {}).get("max_runtime_hours"),
                require_quote=bool(dict(payload.get("economics") or {}).get("require_quote", False)),
                require_approval=bool(dict(payload.get("economics") or {}).get("require_approval", False)),
                budget_bucket=str(dict(payload.get("economics") or {}).get("budget_bucket", "") or ""),
                cost_center=str(dict(payload.get("economics") or {}).get("cost_center", "") or ""),
                preserve_instance_on_failure=bool(dict(payload.get("economics") or {}).get("preserve_instance_on_failure", True)),
            ),
            components=BioStateComponents(
                proteins=tuple(
                    ProteinComponent(
                        id=str(item.get("id", "") or ""),
                        source=str(item.get("source", "") or ""),
                        chain_selection=str(item.get("chain_selection", "") or ""),
                        mutations=tuple(item.get("mutations") or ()),
                        ptms=tuple(item.get("ptms") or ()),
                    )
                    for item in (dict(payload.get("components") or {}).get("proteins") or [])
                    if isinstance(item, dict)
                ),
                ligands=tuple(
                    LigandComponent(
                        id=str(item.get("id", "") or ""),
                        smiles=str(item.get("smiles", "") or ""),
                        pose_uri=str(item.get("pose_uri", "") or ""),
                        sdf_uri=str(item.get("sdf_uri", "") or ""),
                        provenance=str(item.get("provenance", "") or ""),
                    )
                    for item in (dict(payload.get("components") or {}).get("ligands") or [])
                    if isinstance(item, dict)
                ),
                peptides=tuple(
                    PeptideComponent(
                        id=str(item.get("id", "") or ""),
                        sequence=str(item.get("sequence", "") or ""),
                        source=str(item.get("source", "") or ""),
                    )
                    for item in (dict(payload.get("components") or {}).get("peptides") or [])
                    if isinstance(item, dict)
                ),
            ),
            sites=tuple(
                SiteReference(
                    binding_site_id=str(item.get("binding_site_id", "") or ""),
                    residues=tuple(item.get("residues") or ()),
                    atlas_refs=tuple(item.get("atlas_refs") or ()),
                )
                for item in (payload.get("sites") or [])
                if isinstance(item, dict)
            ),
            atlas_refs=tuple(payload.get("atlas_refs") or ()),
            output_prefix=str(payload.get("output_prefix", "") or ""),
            produced_by=str(payload.get("produced_by", "user") or "user"),
            upstream_artifacts=tuple(payload.get("upstream_artifacts") or ()),
            lmp_refs=tuple(payload.get("lmp_refs") or ()),
            lineage_metadata=LineageMetadata(
                source_run_id=str(dict(payload.get("lineage_metadata") or {}).get("source_run_id", "") or ""),
                source_session_id=str(dict(payload.get("lineage_metadata") or {}).get("source_session_id", "") or ""),
                parent_biostate_id=str(dict(payload.get("lineage_metadata") or {}).get("parent_biostate_id", "") or ""),
                handoff_from=str(dict(payload.get("lineage_metadata") or {}).get("handoff_from", "") or ""),
                handoff_step=str(dict(payload.get("lineage_metadata") or {}).get("handoff_step", "") or ""),
                trace_refs=tuple(dict(payload.get("lineage_metadata") or {}).get("trace_refs") or ()),
                source_identities=tuple(dict(payload.get("lineage_metadata") or {}).get("source_identities") or ()),
            ),
        )

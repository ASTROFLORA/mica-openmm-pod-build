from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bsm.lmp.context_extractor import BiologicalContext, get_context_extractor
from mica.storage.workspace_artifact_contract import ClaimBoundary, WorkspaceArtifactContract


ScientificClaimBoundary = Literal[
    "no_biological_claim",
    "unvalidated_structure_reference",
    "validated_structure_reference",
    "fixture_only",
]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    ref: str = Field(..., min_length=1)
    media_type: str = ""
    workspace_claim_boundary: ClaimBoundary | None = None
    workspace_artifact: WorkspaceArtifactContract | None = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_workspace_binding(self) -> "ArtifactReference":
        if self.workspace_artifact and self.workspace_claim_boundary is None:
            self.workspace_claim_boundary = self.workspace_artifact.claim_boundary
        if (
            self.workspace_artifact
            and self.workspace_claim_boundary
            and self.workspace_artifact.claim_boundary != self.workspace_claim_boundary
        ):
            raise ValueError("workspace_artifact claim boundary must match workspace_claim_boundary")
        return self


class ProteinAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotation_id: str = Field(..., min_length=1)
    kind: Literal["domain", "motif", "functional_note", "dataset_tag", "other"]
    label: str = Field(..., min_length=1)
    source_ref: str = ""
    residue_range: str | None = None
    evidence_refs: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GenesisProteinOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    input_ref: str = Field(..., min_length=1)
    sequence_ref: str | None = None
    structure_ref: str | None = None
    confidence_ref: str | None = None
    embedding_ref: str | None = None
    annotations: List[ProteinAnnotation] = Field(default_factory=list)
    protein_context_ref: str = Field(..., min_length=1)
    mol_lsp_portrait_ref: str | None = None
    artifact_refs: List[ArtifactReference] = Field(default_factory=list)
    claim_boundary: ScientificClaimBoundary
    generated_by: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow_iso)


class SequenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_ref: str | None = None
    sequence_length: int | None = Field(default=None, ge=1)
    sequence_preview: str | None = None
    alphabet: Literal["protein", "unknown"] = "unknown"
    source_context: str = ""
    warnings: List[str] = Field(default_factory=list)


class StructureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structure_ref: str | None = None
    confidence_ref: str | None = None
    has_structure: bool = False
    has_confidence: bool = False
    validation_state: Literal[
        "not_provided",
        "unvalidated_structure_reference",
        "validated_structure_reference",
    ]
    preview_receipt_ref: str | None = None
    warnings: List[str] = Field(default_factory=list)


class ModelContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(..., min_length=1)
    input_ref: str = Field(..., min_length=1)
    generated_by: Dict[str, Any] = Field(default_factory=dict)
    artifact_count: int = Field(default=0, ge=0)


class ProteinPortrait(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portrait_id: str = Field(..., min_length=1)
    output_id: str = Field(..., min_length=1)
    sequence_summary: SequenceSummary
    structure_summary: StructureSummary
    domain_annotations: List[ProteinAnnotation] = Field(default_factory=list)
    motif_annotations: List[ProteinAnnotation] = Field(default_factory=list)
    model_context: ModelContext
    literature_context_refs: List[str] = Field(default_factory=list)
    dataset_context_refs: List[str] = Field(default_factory=list)
    BioDynamo_validation_refs: List[str] = Field(default_factory=list)
    SMIC_analysis_refs: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    claim_boundary: ScientificClaimBoundary
    created_at: str = Field(default_factory=utcnow_iso)


class GenesisMolLspContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: str = Field(..., min_length=1)
    portrait_id: str = Field(..., min_length=1)
    output_id: str = Field(..., min_length=1)
    protein_context_ref: str = Field(..., min_length=1)
    canonical_lmp_xml_ref: str | None = None
    canonical_lmp_preset: str | None = None
    canonical_lmp_source: str | None = None
    lmp_context_ref: str | None = None
    structure_context_ref: str | None = None
    preview_receipt_ref: str | None = None
    semantic_kernel_refs: List[str] = Field(default_factory=list)
    knowledge_refs: List[str] = Field(default_factory=list)
    artifact_refs: List[ArtifactReference] = Field(default_factory=list)
    validation_refs: Dict[str, List[str]] = Field(default_factory=dict)
    workspace_policy: Dict[str, Any] = Field(default_factory=dict)
    supported_formats: Dict[str, bool] = Field(default_factory=dict)
    extracted_context_available: bool = False
    json_projection_role: Literal["auxiliary_projection", "canonical", "not_materialized"] = "auxiliary_projection"
    warnings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    claim_boundary: ScientificClaimBoundary
    status: Literal["completed", "partial"] = "completed"


class GenesisProteinPortraitReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(..., min_length=1)
    output_id: str = Field(..., min_length=1)
    portrait_id: str = Field(..., min_length=1)
    mol_lsp_context_id: str = Field(..., min_length=1)
    status: Literal["completed", "partial", "blocked"]
    claim_boundary: ScientificClaimBoundary
    unknowns: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    artifact_ref_count: int = Field(default=0, ge=0)
    handoff_contract_ref: str = Field(..., min_length=1)
    raw_secret_logged: bool = False
    created_at: str = Field(default_factory=utcnow_iso)


SEMANTIC_KERNEL_CODE_PATHS = [
    "src/mica/sdk/orchestration/protocol_kernel.py",
    "src/bsm/lmp/generator_v4.py",
    "src/bsm/lmp/structure_asset_context.py",
]


def build_genesis_semantic_kernel_audit() -> Dict[str, Any]:
    return {
        "audit_id": "genesis_mol_lsp_semantic_kernel_audit_v1",
        "status": "partial_fixture_only",
        "semantic_kernel": {
            "code_backed": [
                {
                    "path": "src/mica/sdk/orchestration/protocol_kernel.py",
                    "purpose": "Protocol JSON-LD lowering and validation kernel.",
                },
                {
                    "path": "src/bsm/lmp/generator_v4.py",
                    "purpose": "LMP authority that already emits structure visuals, knowledge graph references, and SMIC trajectory XML.",
                },
            ],
            "doc_backed": [
                {
                    "path": ".mica/programs/DLM_LMP_CONVERGENCE_BLUEPRINT/runtime_audits/hybrid_phase7_semantic_kernel_payload_20260521/README.md",
                    "purpose": "Semantic-kernel payload audit evidence, not a runtime authority.",
                }
            ],
            "future": [
                "No canonical Genesis JSON protein portrait authority existed before this slice.",
                "No repo-local Quetzal runtime contract was found under src/ or docs/; only prior packets/logs exist.",
            ],
        },
        "mol_lsp_contracts": {
            "current_code_backed": [
                "src/bsm/lmp/generator_v4.py",
                "src/bsm/lmp/structure_asset_context.py",
                "src/bsm/lmp/presets.py",
                "src/bsm/lmp/context_extractor.py",
            ],
            "xml_authority_ready": True,
            "genesis_adapter_ready": "partial",
            "notes": [
                "Current system already emits canonical preset-driven LMP XML.",
                "Genesis did not expose a first-class JSON portrait before this slice; that JSON is now a secondary projection, not the primary MOL/LSP authority.",
            ],
        },
        "representable_concepts": {
            "sequence": True,
            "structure": True,
            "domains": True,
            "motifs": True,
            "annotations": True,
            "protein_context_linking": True,
            "biodynamo_linking": True,
            "smic_linking": True,
            "quetzal_linking": "doc_backed_only",
        },
        "missing": [
            "Canonical Genesis JSON protein portrait format.",
            "Genesis adapter that materializes canonical preset-driven LMP XML for arbitrary serverless model outputs.",
            "Direct workspace-materialized artifact binding for remote ESM3 output fixture.",
            "Repo-local Quetzal runtime contract in src/ for typed handoff consumption.",
        ],
        "acceptance_boundary": {
            "code_backed": [
                "LMP XML authority",
                "Structure asset context",
                "Preview receipt contract",
                "Workspace artifact contract",
                "BioDynamo handoff receipt path",
                "SMIC worker capability/runtime receipt path",
            ],
            "doc_backed": [
                "Quetzal packet/log evidence",
                "Semantic-kernel audit packet under DLM/LMP convergence program",
            ],
            "future": [
                "Genesis-to-LMP preset materialization for new model outputs that do not yet have populated biological context",
                "Workspace-bound live Quetzal adapter",
            ],
        },
    }


def build_genesis_protein_output_schema() -> Dict[str, Any]:
    return {
        "schema_version": "genesis_protein_output_schema_v1",
        "model_schema": GenesisProteinOutput.model_json_schema(),
        "artifact_reference_schema": ArtifactReference.model_json_schema(),
        "scientific_claim_boundaries": list(ScientificClaimBoundary.__args__),  # type: ignore[attr-defined]
        "workspace_contract_ref": "mica.storage.workspace_artifact_contract.WorkspaceArtifactContract",
    }


def build_genesis_protein_portrait_schema() -> Dict[str, Any]:
    return {
        "schema_version": "genesis_protein_portrait_schema_v1",
        "portrait_schema": ProteinPortrait.model_json_schema(),
        "mol_lsp_context_schema": GenesisMolLspContext.model_json_schema(),
        "receipt_schema": GenesisProteinPortraitReceipt.model_json_schema(),
    }


def build_genesis_mol_lsp_output_contract() -> Dict[str, Any]:
    return {
        "contract_id": "genesis_mol_lsp_output_contract_v1",
        "required_outputs": ["protein_portrait.json", "mol_lsp_context.json", "protein_portrait_receipt.json"],
        "canonical_mol_lsp_authority": {
            "format": "lmp_xml",
            "artifact_kind": "lmp_preset.xml",
            "authority_ref": "src/bsm/lmp/generator_v4.py",
            "required_when_available": True,
        },
        "optional_outputs": ["lmp_preset.xml"],
        "supported_formats": {
            "json_portrait": True,
            "auxiliary_context_json": True,
            "canonical_lmp_xml": True,
        },
        "authority_bindings": {
            "semantic_kernel": SEMANTIC_KERNEL_CODE_PATHS,
            "lmp_presets": "src/bsm/lmp/presets.py",
            "lmp_context_extractor": "src/bsm/lmp/context_extractor.py",
            "workspace_artifact_contract": "src/mica/storage/workspace_artifact_contract.py",
            "preview_contract": "src/mica/md_preview/unified_preview_contract.py",
            "biodynamo_handoff": "src/mica/scientific/topology_kernel/handoff/biodynamo_handoff.py",
            "smic_runtime": "src/mica/worker/smic_runtime.py",
        },
        "backlog_items": [
            {
                "item_id": "GENESIS-PORTRAIT-LMP-01",
                "status": "backlog",
                "detail": "Genesis does not yet materialize new preset-driven LMP XML for arbitrary model outputs that start from raw sequence/structure refs.",
            },
            {
                "item_id": "GENESIS-QUETZAL-01",
                "status": "doc_backed_only",
                "detail": "Quetzal handoff remains packet/log backed until a repo-local runtime contract exists.",
            },
        ],
    }


def build_genesis_protein_output_handoff_contract() -> Dict[str, Any]:
    return {
        "contract_id": "genesis_protein_output_handoff_contract_v1",
        "biodynamo": {
            "status": "code_backed",
            "consumer_ref": "src/mica/scientific/topology_kernel/handoff/biodynamo_handoff.py",
            "required_fields": ["structure_ref", "artifact_refs", "protein_context_ref", "mol_lsp_portrait_ref", "claim_boundary"],
        },
        "smic": {
            "status": "code_backed",
            "consumer_ref": "src/mica/worker/smic_runtime.py",
            "required_fields": ["structure_ref", "artifact_refs", "mol_lsp_portrait_ref", "SMIC_analysis_refs"],
        },
        "quetzal": {
            "status": "doc_backed_only",
            "consumer_refs": [
                ".mica/logs/quetzal_cg_runtime_foundation_20260528T174054Z",
                ".mica/programs/QUETZAL_SUPERNOVA",
            ],
            "required_fields": ["structure_ref", "artifact_refs", "mol_lsp_portrait_ref", "claim_boundary"],
        },
        "gcs_workspace": {
            "status": "code_backed",
            "workspace_contract_ref": "mica.storage.workspace_artifact_contract.WorkspaceArtifactContract",
            "production_requires_gcs_uri": True,
            "blocked_state": "blocked_missing_gcs_uri",
        },
        "frontend_explorer": {
            "status": "code_backed",
            "preview_contract_ref": "src/mica/md_preview/unified_preview_contract.py",
            "required_fields": ["structure_ref", "preview_receipt_ref", "mol_lsp_portrait_ref"],
        },
        "shell": {
            "status": "code_backed",
            "required_fields": ["output_id", "portrait_id", "mol_lsp_portrait_ref", "artifact_refs", "warnings", "blockers"],
        },
    }


class GenesisProteinPortraitGenerator:
    def generate(
        self,
        output: GenesisProteinOutput,
        *,
        sequence_value: str | None = None,
        lmp_xml_ref: str | None = None,
        lmp_context_ref: str | None = None,
        structure_context_ref: str | None = None,
        preview_receipt_ref: str | None = None,
        lmp_preset_name: str | None = None,
        literature_context_refs: Optional[List[str]] = None,
        dataset_context_refs: Optional[List[str]] = None,
        biodynamo_validation_refs: Optional[List[str]] = None,
        smic_analysis_refs: Optional[List[str]] = None,
    ) -> tuple[GenesisProteinOutput, ProteinPortrait, GenesisMolLspContext, GenesisProteinPortraitReceipt]:
        output_copy = output.model_copy(deep=True)
        literature_context_refs = list(literature_context_refs or [])
        dataset_context_refs = list(dataset_context_refs or [])
        biodynamo_validation_refs = list(biodynamo_validation_refs or [])
        smic_analysis_refs = list(smic_analysis_refs or [])

        warnings: List[str] = []
        blockers: List[str] = []
        unknowns: List[str] = []
        lmp_context, resolved_lmp_xml_ref, resolved_lmp_preset = self._load_lmp_context(
            output_copy=output_copy,
            lmp_xml_ref=lmp_xml_ref,
            lmp_preset_name=lmp_preset_name,
            warnings=warnings,
        )
        if lmp_context and not sequence_value and lmp_context.sequence:
            sequence_value = lmp_context.sequence

        sequence_summary = self._build_sequence_summary(
            output_copy=output_copy,
            sequence_value=sequence_value,
            unknowns=unknowns,
        )
        structure_summary = self._build_structure_summary(
            output_copy=output_copy,
            preview_receipt_ref=preview_receipt_ref,
            warnings=warnings,
            unknowns=unknowns,
            lmp_context=lmp_context,
        )

        domain_annotations = [annotation for annotation in output_copy.annotations if annotation.kind == "domain"]
        motif_annotations = [annotation for annotation in output_copy.annotations if annotation.kind == "motif"]
        if not domain_annotations and lmp_context:
            domain_annotations = self._domain_annotations_from_lmp(lmp_context)
        if not motif_annotations and lmp_context:
            motif_annotations = self._motif_annotations_from_lmp(lmp_context)
        if not domain_annotations:
            unknowns.append("domain_annotations_not_provided")
        if not motif_annotations:
            unknowns.append("motif_annotations_not_provided")
        if not resolved_lmp_xml_ref:
            unknowns.append("canonical_lmp_xml_not_materialized")
        if not literature_context_refs:
            unknowns.append("literature_context_not_provided")
        if not dataset_context_refs:
            unknowns.append("dataset_context_not_provided")
        if not biodynamo_validation_refs:
            unknowns.append("biodynamo_validation_not_provided")
        if not smic_analysis_refs:
            unknowns.append("smic_analysis_not_provided")
        if not output_copy.protein_context_ref:
            blockers.append("missing_protein_context_ref")

        portrait_id = f"portrait:{output_copy.output_id}"
        context_id = f"mol_lsp:{output_copy.output_id}"
        output_copy.mol_lsp_portrait_ref = resolved_lmp_xml_ref

        portrait = ProteinPortrait(
            portrait_id=portrait_id,
            output_id=output_copy.output_id,
            sequence_summary=sequence_summary,
            structure_summary=structure_summary,
            domain_annotations=domain_annotations,
            motif_annotations=motif_annotations,
            model_context=ModelContext(
                model_id=output_copy.model_id,
                input_ref=output_copy.input_ref,
                generated_by=dict(output_copy.generated_by),
                artifact_count=len(output_copy.artifact_refs),
            ),
            literature_context_refs=literature_context_refs,
            dataset_context_refs=dataset_context_refs,
            BioDynamo_validation_refs=biodynamo_validation_refs,
            SMIC_analysis_refs=smic_analysis_refs,
            warnings=sorted(dict.fromkeys(warnings)),
            blockers=sorted(dict.fromkeys(blockers)),
            claim_boundary=output_copy.claim_boundary,
        )
        context = GenesisMolLspContext(
            context_id=context_id,
            portrait_id=portrait.portrait_id,
            output_id=output_copy.output_id,
            protein_context_ref=output_copy.protein_context_ref,
            canonical_lmp_xml_ref=resolved_lmp_xml_ref,
            canonical_lmp_preset=resolved_lmp_preset,
            canonical_lmp_source="preset_xml" if resolved_lmp_xml_ref else None,
            lmp_context_ref=lmp_context_ref,
            structure_context_ref=structure_context_ref,
            preview_receipt_ref=preview_receipt_ref,
            semantic_kernel_refs=list(SEMANTIC_KERNEL_CODE_PATHS),
            knowledge_refs=self._knowledge_refs(
                output_copy=output_copy,
                literature_context_refs=literature_context_refs,
                dataset_context_refs=dataset_context_refs,
                lmp_context=lmp_context,
            ),
            artifact_refs=list(output_copy.artifact_refs),
            validation_refs={
                "biodynamo": biodynamo_validation_refs,
                "smic": smic_analysis_refs,
                "quetzal": [],
            },
            workspace_policy=build_genesis_protein_output_handoff_contract()["gcs_workspace"],
            supported_formats=build_genesis_mol_lsp_output_contract()["supported_formats"],
            extracted_context_available=bool(lmp_context),
            json_projection_role="auxiliary_projection" if resolved_lmp_xml_ref else "not_materialized",
            warnings=sorted(dict.fromkeys(warnings)),
            blockers=sorted(dict.fromkeys(blockers)),
            claim_boundary=output_copy.claim_boundary,
            status="partial" if blockers or not resolved_lmp_xml_ref else "completed",
        )
        receipt = GenesisProteinPortraitReceipt(
            receipt_id=f"receipt:{output_copy.output_id}",
            output_id=output_copy.output_id,
            portrait_id=portrait.portrait_id,
            mol_lsp_context_id=context.context_id,
            status="blocked" if blockers else ("partial" if unknowns else "completed"),
            claim_boundary=output_copy.claim_boundary,
            unknowns=sorted(dict.fromkeys(unknowns)),
            warnings=sorted(dict.fromkeys(warnings)),
            blockers=sorted(dict.fromkeys(blockers)),
            artifact_ref_count=len(output_copy.artifact_refs),
            handoff_contract_ref=build_genesis_protein_output_handoff_contract()["contract_id"],
            raw_secret_logged=False,
        )
        return output_copy, portrait, context, receipt

    def _load_lmp_context(
        self,
        *,
        output_copy: GenesisProteinOutput,
        lmp_xml_ref: str | None,
        lmp_preset_name: str | None,
        warnings: List[str],
    ) -> tuple[BiologicalContext | None, str | None, str | None]:
        xml_ref = str(lmp_xml_ref or output_copy.mol_lsp_portrait_ref or "").strip() or None
        if not xml_ref:
            return None, None, lmp_preset_name
        candidate = Path(xml_ref)
        if not candidate.exists():
            warnings.append("canonical_lmp_xml_ref_not_found_locally")
            return None, xml_ref, lmp_preset_name
        try:
            context = get_context_extractor().extract_from_file(candidate)
        except Exception:
            warnings.append("canonical_lmp_xml_parse_failed")
            return None, xml_ref, lmp_preset_name
        return context, xml_ref, lmp_preset_name or context.preset_type or None

    def _build_sequence_summary(
        self,
        *,
        output_copy: GenesisProteinOutput,
        sequence_value: str | None,
        unknowns: List[str],
    ) -> SequenceSummary:
        trimmed = "".join((sequence_value or "").split()).upper()
        warnings: List[str] = []
        if not trimmed and not output_copy.sequence_ref:
            unknowns.append("sequence_not_provided")
        if trimmed:
            invalid = sorted({char for char in trimmed if char not in set("ACDEFGHIKLMNPQRSTVWY")})
            if invalid:
                warnings.append("sequence_contains_non_canonical_residues")
                alphabet = "unknown"
            else:
                alphabet = "protein"
            preview = trimmed if len(trimmed) <= 24 else f"{trimmed[:24]}..."
            return SequenceSummary(
                sequence_ref=output_copy.sequence_ref,
                sequence_length=len(trimmed),
                sequence_preview=preview,
                alphabet=alphabet,
                source_context="direct_sequence_value",
                warnings=warnings,
            )
        return SequenceSummary(
            sequence_ref=output_copy.sequence_ref,
            source_context="reference_only",
            warnings=warnings,
        )

    def _build_structure_summary(
        self,
        *,
        output_copy: GenesisProteinOutput,
        preview_receipt_ref: str | None,
        warnings: List[str],
        unknowns: List[str],
        lmp_context: BiologicalContext | None,
    ) -> StructureSummary:
        if not output_copy.structure_ref and lmp_context and lmp_context.geometry and lmp_context.geometry[0].pdb_id:
            output_copy.structure_ref = f"pdb:{lmp_context.geometry[0].pdb_id}"
        if not output_copy.structure_ref:
            unknowns.append("structure_not_provided")
            return StructureSummary(
                structure_ref=None,
                confidence_ref=output_copy.confidence_ref,
                has_structure=False,
                has_confidence=bool(output_copy.confidence_ref),
                validation_state="not_provided",
                preview_receipt_ref=preview_receipt_ref,
            )
        validation_state: Literal[
            "not_provided",
            "unvalidated_structure_reference",
            "validated_structure_reference",
        ] = "validated_structure_reference"
        if output_copy.claim_boundary != "validated_structure_reference":
            validation_state = "unvalidated_structure_reference"
            warnings.append("structure_reference_not_validated")
        if not output_copy.confidence_ref:
            unknowns.append("confidence_not_provided")
        return StructureSummary(
            structure_ref=output_copy.structure_ref,
            confidence_ref=output_copy.confidence_ref,
            has_structure=True,
            has_confidence=bool(output_copy.confidence_ref),
            validation_state=validation_state,
            preview_receipt_ref=preview_receipt_ref,
            warnings=["confidence_ref_missing"] if not output_copy.confidence_ref else [],
        )

    def _knowledge_refs(
        self,
        *,
        output_copy: GenesisProteinOutput,
        literature_context_refs: List[str],
        dataset_context_refs: List[str],
        lmp_context: BiologicalContext | None,
    ) -> List[str]:
        refs = [output_copy.protein_context_ref]
        if output_copy.mol_lsp_portrait_ref:
            refs.append(output_copy.mol_lsp_portrait_ref)
        refs.extend(literature_context_refs)
        refs.extend(dataset_context_refs)
        if lmp_context and lmp_context.budo_id:
            refs.append(lmp_context.budo_id)
        for annotation in output_copy.annotations:
            refs.extend(annotation.evidence_refs)
            if annotation.source_ref:
                refs.append(annotation.source_ref)
        return [ref for ref in dict.fromkeys(refs) if ref]

    def _domain_annotations_from_lmp(self, lmp_context: BiologicalContext) -> List[ProteinAnnotation]:
        annotations: List[ProteinAnnotation] = []
        for index, domain in enumerate(lmp_context.domains, start=1):
            annotations.append(
                ProteinAnnotation(
                    annotation_id=f"lmp-domain-{index}",
                    kind="domain",
                    label=domain.name or domain.domain_type,
                    source_ref=lmp_context.source_file,
                    residue_range=f"{domain.start}-{domain.end}",
                    metadata={"domain_type": domain.domain_type, "preset": lmp_context.preset_type},
                )
            )
        return annotations

    def _motif_annotations_from_lmp(self, lmp_context: BiologicalContext) -> List[ProteinAnnotation]:
        markers = get_context_extractor().extract_nesy_markers(lmp_context.nesy_grammar or "")
        annotations: List[ProteinAnnotation] = []
        for index, motif in enumerate(markers.get("motifs", []), start=1):
            annotations.append(
                ProteinAnnotation(
                    annotation_id=f"lmp-motif-{index}",
                    kind="motif",
                    label=motif,
                    source_ref=lmp_context.source_file,
                    metadata={"preset": lmp_context.preset_type},
                )
            )
        return annotations

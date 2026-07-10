"""
Lab Report Schema for BSM-BUDO-CEA
===================================

Scientific report schemas aligned with Nature standards and AI University workflow.

Implements structured reporting for:
- Experiment documentation
- Methods reproducibility
- Results presentation
- Discussion and conclusions
- Provenance tracking

References:
- AI University BITACORA format
- Nature publishing standards
- BSM traceability requirements
- Perplexity research on scientific validation
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .message_schema import AgentPersona, Attachment, Citation


def _utcnow() -> datetime:
    """Return a timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def _ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce datetimes to timezone-aware UTC values."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ExperimentMetadata(BaseModel):
    """Core metadata for experiment traceability."""

    experiment_id: UUID = Field(
        default_factory=uuid4,
        description="Unique experiment identifier",
    )
    title: str = Field(..., min_length=10, description="Experiment title")
    principal_investigator: AgentPersona = Field(
        ..., description="Lead researcher persona"
    )
    collaborators: List[AgentPersona] = Field(
        default_factory=list, description="Contributing personas"
    )
    roadmap_phase: str = Field(..., description="BSM-BUDO-CEA phase identifier")
    start_time: datetime = Field(
        default_factory=_utcnow,
        description="Experiment start timestamp",
    )
    end_time: Optional[datetime] = Field(
        None, description="Experiment completion timestamp"
    )
    lab_directory: str = Field(..., description="Lab directory path")
    related_message_ids: List[UUID] = Field(
        default_factory=list, description="Related agent messages"
    )

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("lab_directory")
    @classmethod
    def validate_lab_path(cls, value: str) -> str:
        """Ensure the lab directory follows the unified labs/ hierarchy."""

        if "labs/" not in value and "RESEARCH_LABS/" not in value:
            msg = f"Lab directory must reside within labs/ hierarchy: {value}"
            raise ValueError(msg)
        return value

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def ensure_utc_datetimes(
        cls, value: Optional[datetime]
    ) -> Optional[datetime]:
        """Guarantee telemetry timestamps remain UTC aware."""

        return _ensure_utc(value)


class MethodsSection(BaseModel):
    """Detailed methods for reproducibility following Nature standards."""

    summary: str = Field(..., min_length=100, description="Methods overview")
    materials: List[str] = Field(default_factory=list, description="Materials used")
    procedure: List[str] = Field(
        ..., min_length=1, description="Step-by-step experimental procedure"
    )
    software: Dict[str, str] = Field(
        default_factory=dict, description="Software name → version"
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Key configuration parameters"
    )
    computational_resources: Optional[Dict[str, Any]] = Field(
        None, description="Hardware or cloud resources"
    )

    @field_validator("procedure")
    @classmethod
    def validate_procedure_detail(cls, steps: List[str]) -> List[str]:
        """Ensure each procedure step contains meaningful detail."""

        for step in steps:
            if len(step.split()) < 5:
                raise ValueError(
                    f"Procedure steps must contain at least 5 words: {step}"
                )
        return steps

    def to_markdown(self) -> str:
        """Generate the Methods section in markdown format."""

        lines: List[str] = ["## Methods", "", self.summary, "", "### Materials", ""]
        for material in self.materials:
            lines.append(f"- {material}")

        lines.extend(["", "### Procedure", ""])
        for index, step in enumerate(self.procedure, start=1):
            lines.append(f"{index}. {step}")

        if self.software:
            lines.extend(["", "### Software", ""])
            for name, version in self.software.items():
                lines.append(f"- **{name}**: {version}")

        if self.parameters:
            lines.extend(["", "### Key Parameters", ""])
            for parameter, value in self.parameters.items():
                lines.append(f"- `{parameter}`: {value}")

        if self.computational_resources:
            lines.extend(["", "### Computational Resources", ""])
            for resource, value in self.computational_resources.items():
                lines.append(f"- **{resource}**: {value}")

        return "\n".join(lines)


class ResultsSection(BaseModel):
    """Experimental results with quantitative and qualitative observations."""

    summary: str = Field(..., min_length=50, description="Results summary")
    primary_findings: List[str] = Field(
        ..., min_length=1, description="Primary experimental findings"
    )
    quantitative_metrics: Dict[str, float] = Field(
        default_factory=dict, description="Numerical metrics"
    )
    qualitative_observations: List[str] = Field(
        default_factory=list, description="Qualitative observations"
    )
    figures: List[Attachment] = Field(
        default_factory=list, description="Associated figures or plots"
    )
    tables: List[Attachment] = Field(
        default_factory=list, description="Associated tables"
    )
    raw_data: List[Attachment] = Field(
        default_factory=list, description="Raw data files"
    )
    statistical_tests: Optional[Dict[str, Any]] = Field(
        None, description="Statistical test outputs"
    )

    def to_markdown(self) -> str:
        """Generate the Results section in markdown format."""

        lines: List[str] = ["## Results", "", self.summary, "", "### Primary Findings", ""]
        for finding in self.primary_findings:
            lines.append(f"- {finding}")

        if self.quantitative_metrics:
            lines.extend(
                [
                    "",
                    "### Quantitative Metrics",
                    "",
                    "| Metric | Value |",
                    "|--------|-------|",
                ]
            )
            for metric, value in self.quantitative_metrics.items():
                lines.append(f"| {metric} | {value:.4f} |")

        if self.qualitative_observations:
            lines.extend(["", "### Qualitative Observations", ""])
            for observation in self.qualitative_observations:
                lines.append(f"- {observation}")

        if self.statistical_tests:
            lines.extend(["", "### Statistical Tests", ""])
            for test, result in self.statistical_tests.items():
                lines.append(f"- **{test}**: {result}")

        if self.figures:
            lines.extend(["", "### Figures", ""])
            for figure in self.figures:
                lines.append(f"![{figure.description}]({figure.file_path})")

        if self.tables:
            lines.extend(["", "### Tables", ""])
            for table in self.tables:
                lines.append(f"- [{table.description}]({table.file_path})")

        if self.raw_data:
            lines.extend(["", "### Raw Data", ""])
            for artifact in self.raw_data:
                lines.append(f"- [{artifact.description}]({artifact.file_path})")

        return "\n".join(lines)


class DiscussionSection(BaseModel):
    """Discussion and interpretation of experimental outcomes."""

    summary: str = Field(..., min_length=100, description="Discussion summary")
    interpretation: List[str] = Field(
        ..., min_length=1, description="Interpretation of results"
    )
    limitations: List[str] = Field(
        default_factory=list, description="Known limitations"
    )
    future_work: List[str] = Field(default_factory=list, description="Proposed next steps")
    hypothesis_validation: Optional[str] = Field(
        None, description="Status of initial hypothesis"
    )
    citations: List[Citation] = Field(default_factory=list, description="Supporting literature")

    def to_markdown(self) -> str:
        """Generate the Discussion section in markdown format."""

        lines: List[str] = ["## Discussion", "", self.summary, "", "### Interpretation", ""]
        for insight in self.interpretation:
            lines.append(f"- {insight}")

        if self.hypothesis_validation:
            lines.extend([
                "",
                "### Hypothesis Validation",
                "",
                self.hypothesis_validation,
            ])

        if self.limitations:
            lines.extend(["", "### Limitations", ""])
            for item in self.limitations:
                lines.append(f"- {item}")

        if self.future_work:
            lines.extend(["", "### Future Work", ""])
            for plan in self.future_work:
                lines.append(f"- {plan}")

        if self.citations:
            lines.extend(["", "### Supporting Citations", ""])
            for citation in self.citations:
                lines.append(f"- {citation.title} ({citation.year})")

        return "\n".join(lines)


class ReproducibilityData(BaseModel):
    """Artifacts required for full experiment reproducibility."""

    random_seeds: Optional[Dict[str, int]] = Field(
        None, description="Random seeds per library"
    )
    environment: Dict[str, str] = Field(
        default_factory=dict, description="Software environment"
    )
    configuration_files: List[Attachment] = Field(
        default_factory=list, description="Configuration files"
    )
    scripts: List[Attachment] = Field(
        default_factory=list, description="Execution scripts"
    )
    docker_images: Optional[List[str]] = Field(
        None, description="Docker image references"
    )
    git_commit: Optional[str] = Field(None, description="Git commit hash")
    data_provenance: Dict[str, str] = Field(
        default_factory=dict, description="Input data provenance"
    )
    checksums: Dict[str, str] = Field(
        default_factory=dict, description="File checksums (SHA256)"
    )


class LabReport(BaseModel):
    """Complete laboratory report following scientific standards."""

    metadata: ExperimentMetadata = Field(..., description="Experiment metadata")
    abstract: str = Field(..., min_length=200, description="Experiment abstract")
    methods: MethodsSection = Field(..., description="Methods section")
    results: ResultsSection = Field(..., description="Results section")
    discussion: DiscussionSection = Field(..., description="Discussion section")
    references: List[Citation] = Field(default_factory=list, description="Reference list")
    reproducibility: ReproducibilityData = Field(
        default_factory=ReproducibilityData, description="Reproducibility artifacts"
    )
    acknowledgments: Optional[str] = Field(
        None, description="Acknowledgments or funding sources"
    )
    supplementary_materials: List[Attachment] = Field(
        default_factory=list, description="Supplementary materials"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "metadata": {
                    "title": "ESE Feature Extraction from mdCATH Dataset",
                    "principal_investigator": "dr_yuan_chen",
                    "collaborators": ["dr_sofia_petrov"],
                    "roadmap_phase": "3.002",
                    "lab_directory": "labs/yuan_chen"
                },
                "abstract": "We extracted Evolutionary Structure Embeddings (ESE) from 100 mdCATH trajectories. ESE features captured conformational dynamics with 512-dimensional signatures. Validation showed 94% coverage with no missing values.",
                "methods": {
                    "summary": "Applied Chronosfold ESE extractor to mdCATH H5 trajectory files",
                    "materials": ["mdCATH dataset v2.0", "Chronosfold v4.0"],
                    "procedure": [
                        "Load mdCATH H5 files from /data/mdcath",
                        "Apply ESE extractor with output_dim=512",
                        "Validate dimensions and check for NaN values",
                        "Store results in Zilliz Cloud collection"
                    ],
                    "software": {"Chronosfold": "4.0", "MDTraj": "1.9.7"},
                    "parameters": {"output_dim": 512, "batch_size": 100}
                },
                "results": {
                    "summary": "Successfully extracted ESE features from all 100 trajectories",
                    "primary_findings": [
                        "94% of trajectories yielded complete ESE signatures",
                        "Mean dimensionality: 512 (no variance)",
                        "No NaN values detected"
                    ],
                    "quantitative_metrics": {
                        "coverage": 0.94,
                        "mean_dim": 512.0,
                        "nan_fraction": 0.0
                    }
                },
                "discussion": {
                    "summary": "ESE extraction was successful and meets Phase 3 requirements",
                    "interpretation": [
                        "High coverage indicates robust feature extraction",
                        "Consistent dimensionality validates pipeline stability"
                    ],
                    "limitations": ["6% of trajectories had insufficient frames"],
                    "future_work": [
                        "Extend to full mdCATH dataset",
                        "Compare with PubMedBERT embeddings"
                    ]
                },
                "reproducibility": {
                    "random_seeds": {"numpy": 42, "torch": 123},
                    "git_commit": "abc123def456",
                    "environment": {"python": "3.11", "cuda": "12.1"}
                }
            }
        }
    )

    @field_validator("abstract")
    @classmethod
    def validate_abstract_structure(cls, abstract: str) -> str:
        """Require the abstract to include at least three sentences."""

        sentence_count = len([segment for segment in abstract.split(".") if segment.strip()])
        if sentence_count < 3:
            raise ValueError("Abstract must contain at least three sentences")
        return abstract

    def to_markdown(self, include_metadata: bool = True) -> str:
        """Generate the complete lab report in markdown format."""

        lines: List[str] = [f"# {self.metadata.title}", ""]

        if include_metadata:
            lines.extend(
                [
                    f"**Principal Investigator**: {self.metadata.principal_investigator.value}",
                    f"**Phase**: {self.metadata.roadmap_phase}",
                    f"**Lab**: {self.metadata.lab_directory}",
                    f"**Date**: {self.metadata.start_time.strftime('%Y-%m-%d')}",
                    "",
                ]
            )

            if self.metadata.collaborators:
                collaborator_names = ", ".join(
                    collaborator.value for collaborator in self.metadata.collaborators
                )
                lines.extend([f"**Collaborators**: {collaborator_names}", ""])

        lines.extend(
            [
                "## Abstract",
                "",
                self.abstract,
                "",
                self.methods.to_markdown(),
                "",
                self.results.to_markdown(),
                "",
                self.discussion.to_markdown(),
                "",
            ]
        )

        if self.reproducibility.git_commit or self.reproducibility.random_seeds:
            lines.extend(["## Reproducibility", ""])
            if self.reproducibility.git_commit:
                lines.append(f"**Git Commit**: `{self.reproducibility.git_commit}`")
            if self.reproducibility.random_seeds:
                lines.append("**Random Seeds**:")
                for name, seed in self.reproducibility.random_seeds.items():
                    lines.append(f"- {name}: {seed}")
            lines.append("")

        if self.references:
            lines.extend(["## References", ""])
            for reference in self.references:
                lines.append(
                    f"- {reference.title} — {reference.authors} ({reference.year})"
                )
            lines.append("")

        if self.supplementary_materials:
            lines.extend(["## Supplementary Materials", ""])
            for attachment in self.supplementary_materials:
                lines.append(f"- [{attachment.description}]({attachment.file_path})")
            lines.append("")

        if self.acknowledgments:
            lines.extend(["## Acknowledgments", "", self.acknowledgments, ""])

        return "\n".join(lines)

    def save_to_lab(self, report_dir: str, format: str = "markdown") -> str:
        """Persist the report to the specified lab directory."""

        import os
        from pathlib import Path

        os.makedirs(report_dir, exist_ok=True)

        timestamp = self.metadata.start_time.strftime("%Y%m%d_%H%M%S")
        base_name = f"{timestamp}_{self.metadata.experiment_id}"

        if format == "markdown":
            file_path = Path(report_dir) / f"{base_name}.md"
            file_path.write_text(self.to_markdown(), encoding="utf-8")
        elif format == "json":
            file_path = Path(report_dir) / f"{base_name}.json"
            file_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        else:
            raise ValueError(f"Unsupported format: {format}")

        return str(file_path)


# ===================================================================
# DYNAMIC SCIENTIFIC DAG - PEER REVIEW AND CONSOLIDATION SCHEMAS
# ===================================================================


class QualityScore(BaseModel):
    """Quantitative quality assessment following Nature standards."""

    score_id: UUID = Field(
        default_factory=uuid4,
        description="Unique quality score identifier",
    )
    timestamp: datetime = Field(
        default_factory=_utcnow,
        description="When quality was assessed",
    )

    # Dimension scores (0.0 - 1.0)
    methods_reproducibility: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Can another researcher reproduce this?",
    )
    results_rigor: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Statistical power and validation quality",
    )
    discussion_depth: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Critical analysis and interpretation quality",
    )
    data_availability: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Completeness of data and code sharing",
    )

    # Overall score (weighted average)
    overall_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Weighted overall quality score",
    )

    # Nature standards compliance
    nature_compliance_checks: Dict[str, bool] = Field(
        default_factory=dict,
        description="Pass/fail for specific Nature requirements",
    )

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("timestamp", mode="after")
    @classmethod
    def ensure_utc_timestamp(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Guarantee telemetry timestamps remain UTC aware."""
        return _ensure_utc(value)

    def calculate_overall(self) -> float:
        """
        Calculate weighted overall score.

        Weights: methods 30%, results 40%, discussion 20%, data 10%
        """
        return (
            0.30 * self.methods_reproducibility
            + 0.40 * self.results_rigor
            + 0.20 * self.discussion_depth
            + 0.10 * self.data_availability
        )


class PeerFeedback(BaseModel):
    """Peer review feedback following MSRP pressure protocol."""

    feedback_id: UUID = Field(
        default_factory=uuid4,
        description="Unique feedback identifier",
    )
    reviewer_persona: AgentPersona = Field(
        ..., description="Reviewer's scientific persona"
    )
    target_node_id: str = Field(..., description="Node being reviewed")
    target_report_version: int = Field(
        ..., description="LabReport version being reviewed"
    )
    timestamp: datetime = Field(
        default_factory=_utcnow,
        description="When review was completed",
    )

    # MSRP Phase 2: Skepticism
    methodological_concerns: List[str] = Field(
        default_factory=list,
        description="Skeptical challenges to methods",
    )
    reproducibility_gaps: List[str] = Field(
        default_factory=list,
        description="Missing reproducibility elements",
    )

    # MSRP Phase 3: Evidence Demands
    missing_evidence: List[str] = Field(
        default_factory=list,
        description="Required evidence not provided",
    )
    insufficient_rigor: List[str] = Field(
        default_factory=list,
        description="Statistical or experimental rigor gaps",
    )

    # MSRP Phase 5: Organizational Pressure
    nature_standard_violations: List[str] = Field(
        default_factory=list,
        description="Violations of Nature publication standards",
    )
    publication_readiness_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="0.0 = reject, 1.0 = accept for publication",
    )

    # Constructive guidance (not just criticism)
    specific_improvements: List[str] = Field(
        default_factory=list,
        description="Actionable improvement suggestions",
    )
    recommended_next_steps: List[str] = Field(
        default_factory=list,
        description="Next experiments or analyses to run",
    )

    # Overall assessment
    overall_assessment: str = Field(
        ...,
        description="REVISE_MAJOR, REVISE_MINOR, or ACCEPT",
    )
    quality_score: QualityScore = Field(
        ..., description="Quantitative quality assessment"
    )

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("overall_assessment")
    @classmethod
    def validate_assessment(cls, value: str) -> str:
        """Ensure assessment is one of the allowed values."""
        allowed = {"REVISE_MAJOR", "REVISE_MINOR", "ACCEPT"}
        if value not in allowed:
            msg = f"Assessment must be one of {allowed}, got: {value}"
            raise ValueError(msg)
        return value

    @field_validator("timestamp", mode="after")
    @classmethod
    def ensure_utc_timestamp(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Guarantee telemetry timestamps remain UTC aware."""
        return _ensure_utc(value)


class ConsolidatedPaper(BaseModel):
    """Final M-UDO as comprehensive scientific paper with complete lineage."""

    # Unique identifier
    paper_id: UUID = Field(
        default_factory=uuid4,
        description="Unique paper identifier",
    )

    # Standard M-UDO envelope (backward compatible)
    mudo_envelope: Dict[str, Any] = Field(
        default_factory=dict,
        description="Original M-UDO metadata for compatibility",
    )

    # Scientific paper metadata
    title: str = Field(..., min_length=20, description="Paper title")
    authors: List[AgentPersona] = Field(
        ..., min_length=1, description="All workers who contributed"
    )
    abstract: str = Field(
        ..., min_length=200, max_length=500, description="Executive summary"
    )
    keywords: List[str] = Field(
        default_factory=list, description="Research keywords"
    )

    # Consolidated sections
    introduction: str = Field(
        ..., min_length=200, description="Synthesized introduction"
    )
    methods: MethodsSection = Field(..., description="Merged methods from all workers")
    results: ResultsSection = Field(
        ..., description="Consolidated findings with cross-validation"
    )
    discussion: DiscussionSection = Field(
        ..., description="Integrated interpretation from all perspectives"
    )

    # Complete provenance (AI University BITACORA standard)
    worker_contributions: Dict[str, LabReport] = Field(
        default_factory=dict,
        description="Each worker's final LabReport (node_id → report)",
    )
    peer_review_history: List[PeerFeedback] = Field(
        default_factory=list,
        description="All feedback exchanged during workflow",
    )
    iteration_lineage: Dict[str, List[LabReport]] = Field(
        default_factory=dict,
        description="Version history per worker (node_id → [reports])",
    )
    quality_evolution: Dict[str, List[QualityScore]] = Field(
        default_factory=dict,
        description="Quality improvement tracking per worker",
    )

    # Cross-worker validation
    inter_worker_agreements: List[str] = Field(
        default_factory=list,
        description="Where workers agree on findings",
    )
    inter_worker_contradictions: List[str] = Field(
        default_factory=list,
        description="Where workers disagree (IMPORTANT for future research)",
    )
    resolution_strategy: str = Field(
        default="documented_as_open_question",
        description="How contradictions were handled",
    )

    # Meta-analysis
    confidence_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Confidence per finding (0.0 - 1.0)",
    )
    uncertainty_quantification: Dict[str, Any] = Field(
        default_factory=dict,
        description="Known unknowns and epistemic uncertainty",
    )

    # Reproducibility (Nature standard)
    reproducibility_package: ReproducibilityData = Field(
        default_factory=ReproducibilityData,
        description="Complete reproducibility artifacts",
    )

    # Timestamps
    workflow_start_time: datetime = Field(
        default_factory=_utcnow,
        description="When workflow began",
    )
    workflow_end_time: Optional[datetime] = Field(
        None, description="When workflow completed"
    )
    total_iterations: int = Field(
        default=0, description="Total peer review cycles executed"
    )

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("workflow_start_time", "workflow_end_time", mode="after")
    @classmethod
    def ensure_utc_datetimes(
        cls, value: Optional[datetime]
    ) -> Optional[datetime]:
        """Guarantee telemetry timestamps remain UTC aware."""
        return _ensure_utc(value)

    def to_nature_manuscript(self) -> str:
        """Export as Nature-format markdown manuscript."""

        lines = [
            f"# {self.title}",
            "",
            "## Authors",
            ", ".join([a.full_name for a in self.authors]),
            "",
            "## Abstract",
            self.abstract,
            "",
            "## Keywords",
            ", ".join(self.keywords) if self.keywords else "N/A",
            "",
            "## Introduction",
            self.introduction,
            "",
            self.methods.to_markdown(),
            "",
            self.results.to_markdown(),
            "",
            self.discussion.to_markdown(),
            "",
            "## Supplementary Information",
            "",
            "### Worker Contributions",
            "",
        ]

        for worker_id, report in self.worker_contributions.items():
            lines.append(f"#### {worker_id}: {report.metadata.title}")
            lines.append(
                f"*Principal Investigator*: {report.metadata.principal_investigator.value}"
            )
            lines.append(f"*Iterations*: {len(self.iteration_lineage.get(worker_id, []))}")
            lines.append("")

        if self.inter_worker_contradictions:
            lines.extend(
                [
                    "### Inter-Worker Contradictions",
                    "",
                    "*The following contradictions were identified and documented for future research:*",
                    "",
                ]
            )
            for contradiction in self.inter_worker_contradictions:
                lines.append(f"- {contradiction}")
            lines.append("")

        if self.inter_worker_agreements:
            lines.extend(
                [
                    "### Cross-Worker Validation (Agreements)",
                    "",
                    "*Multiple independent workers converged on these findings:*",
                    "",
                ]
            )
            for agreement in self.inter_worker_agreements:
                lines.append(f"- {agreement}")
            lines.append("")

        # Reproducibility section
        lines.extend(
            [
                "### Reproducibility Package",
                "",
            ]
        )
        if self.reproducibility_package.git_commit:
            lines.append(
                f"**Git Commit**: `{self.reproducibility_package.git_commit}`"
            )
        if self.reproducibility_package.docker_images:
            lines.append("**Docker Images**:")
            for image in self.reproducibility_package.docker_images:
                lines.append(f"- `{image}`")
        if self.reproducibility_package.random_seeds:
            lines.append("**Random Seeds**:")
            for lib, seed in self.reproducibility_package.random_seeds.items():
                lines.append(f"- {lib}: {seed}")
        lines.append("")

        # Workflow metadata
        lines.extend(
            [
                "### Workflow Metadata",
                "",
                f"**Total Iterations**: {self.total_iterations}",
                f"**Workflow Duration**: {self.workflow_start_time.strftime('%Y-%m-%d')}",
            ]
        )
        if self.workflow_end_time:
            duration = self.workflow_end_time - self.workflow_start_time
            lines.append(f"to {self.workflow_end_time.strftime('%Y-%m-%d')} ({duration.days} days)")
        lines.append("")

        return "\n".join(lines)

    def to_mudo_binary(self) -> bytes:
        """Serialize as M-UDO for database storage."""
        # Placeholder: Actual M-UDO serialization would use protobuf or similar
        return self.model_dump_json(indent=2).encode("utf-8")

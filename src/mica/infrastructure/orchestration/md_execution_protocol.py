from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict

REQUEST_SCHEMA_VERSION = "md_execution_request_v1"
RESULT_SCHEMA_VERSION = "md_execution_result_v1"


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactState(str, Enum):
    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True)
class TerminalAutopsyV1:
    schema_version: str = "terminal_autopsy_v1"
    terminal_state: str = "unknown"
    reason_code: str = ""
    reason_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeardownProofV1:
    schema_version: str = "teardown_proof_v1"
    destroy_attempted: bool = False
    destroy_succeeded: bool = False
    preserved_for_recovery: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MDExecutionRequestV1:
    schema_version: str = REQUEST_SCHEMA_VERSION
    job_id: str = ""
    workflow: str = "protein_ligand_md"
    provider_preference: str | None = None
    input_uri: str = ""
    output_uri: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MDExecutionResultV1:
    schema_version: str = RESULT_SCHEMA_VERSION
    job_id: str = ""
    provider_name: str = ""
    status: str = "unknown"
    success: bool = False
    terminal_autopsy: Dict[str, Any] = field(default_factory=dict)
    teardown_proof: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
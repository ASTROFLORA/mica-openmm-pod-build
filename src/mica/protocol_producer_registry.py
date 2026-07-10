"""Protocol producer registry for PAU compiler-ingress governance.

This module is deliberately declarative: it records which live surfaces are
allowed to produce canonical protocol payloads, without importing or executing
those producers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProducerStatus = Literal[
    "implemented",
    "partial",
    "registered_but_blocked",
    "design_only",
    "stale_missing",
]

OutputContract = Literal[
    "validated_protocol_jsonld_document",
    "validated_protocol_jsonld_payload_in_envelope",
    "raw_protocol_jsonld_payload_validated_downstream",
]

ValidationBoundary = Literal[
    "producer_self_validates",
    "execution_boundary_validates",
    "not_applicable",
]


@dataclass(frozen=True)
class ProtocolProducerDescriptor:
    producer_id: str
    source_path: str
    module_path: str
    callable_name: str
    status: ProducerStatus
    output_contract: OutputContract
    validation_boundary: ValidationBoundary
    authority_class: str = "protocol_producer"
    executes_protocol: bool = False
    notes: str = ""


PROTOCOL_PRODUCERS: tuple[ProtocolProducerDescriptor, ...] = (
    ProtocolProducerDescriptor(
        producer_id="biostate_mixed_protocol",
        source_path="src/mica/sdk/orchestration/protocol_kernel.py",
        module_path="mica.sdk.orchestration.protocol_kernel",
        callable_name="compile_biostate_mixed_protocol",
        status="implemented",
        output_contract="validated_protocol_jsonld_document",
        validation_boundary="producer_self_validates",
        notes="BioState/STG compiler ingress returns ProtocolJSONLDDocument via validate_protocol_jsonld.",
    ),
    ProtocolProducerDescriptor(
        producer_id="prometeus_structure_intake",
        source_path="src/mica/sdk/orchestration/prometeus_protocol_draft.py",
        module_path="mica.sdk.orchestration.prometeus_protocol_draft",
        callable_name="build_prometeus_protocol_draft",
        status="implemented",
        output_contract="validated_protocol_jsonld_payload_in_envelope",
        validation_boundary="producer_self_validates",
        notes="Returns a draft envelope whose protocol_jsonld payload is produced from a validated document.",
    ),
    ProtocolProducerDescriptor(
        producer_id="gog_driver_protocol_v2",
        source_path="src/mica/drivers/execution/gog_protocol_generator.py",
        module_path="mica.drivers.execution.gog_protocol_generator",
        callable_name="GogProtocolGeneratorDriver.generate",
        status="implemented",
        output_contract="validated_protocol_jsonld_document",
        validation_boundary="producer_self_validates",
        notes="Deterministic GoG generator returns validate_protocol_jsonld(document).",
    ),
    ProtocolProducerDescriptor(
        producer_id="genesis_gog_pipeline",
        source_path="src/mica/genesis/gog_pipeline.py",
        module_path="mica.genesis.gog_pipeline",
        callable_name="build_genesis_gog_protocol_document",
        status="partial",
        output_contract="raw_protocol_jsonld_payload_validated_downstream",
        validation_boundary="execution_boundary_validates",
        notes="Builder returns a JSON-LD payload; the pipeline validates it before executor request creation.",
    ),
    ProtocolProducerDescriptor(
        producer_id="proposal_compiler",
        source_path="src/mica/protocol_proposal_compiler.py",
        module_path="mica.protocol_proposal_compiler",
        callable_name="promote_proposal_to_protocol",
        status="implemented",
        output_contract="validated_protocol_jsonld_document",
        validation_boundary="producer_self_validates",
        notes="Governed compiler that lowers MSRP and Chronoracle proposals into validated ProtocolJSONLDDocument.",
    ),
)


def list_protocol_producers() -> tuple[ProtocolProducerDescriptor, ...]:
    return PROTOCOL_PRODUCERS


def get_protocol_producer(producer_id: str) -> ProtocolProducerDescriptor:
    for descriptor in PROTOCOL_PRODUCERS:
        if descriptor.producer_id == producer_id:
            return descriptor
    raise KeyError(f"Unknown protocol producer: {producer_id}")


__all__ = [
    "PROTOCOL_PRODUCERS",
    "ProtocolProducerDescriptor",
    "get_protocol_producer",
    "list_protocol_producers",
]

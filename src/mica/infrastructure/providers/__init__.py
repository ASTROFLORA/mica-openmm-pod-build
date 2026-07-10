# MICA Cloud Infrastructure Providers
# Multi-provider abstraction for GPU cloud orchestration
#
# Supported Providers:
# - Vast.ai (vast_provider.py) - Best $/GPU for spot instances
# - RunPod (runpod_provider.py) - Serverless + pods
# - GCP/Vertex AI (gcp_provider.py) - Enterprise, GCS integration
# - Lambda Labs (lambda_provider.py) - Reserved capacity
#
# Usage:
#   from infrastructure.providers import VastProvider, RunPodProvider, GCPProvider
#   
#   orchestrator = CloudOrchestrator()
#   orchestrator.register_provider(VastProvider(api_key="..."))
#   orchestrator.register_provider(RunPodProvider(api_key="..."))
#   orchestrator.register_provider(GCPProvider(project_id="..."))
#   result = await orchestrator.provision(request)

from .base_provider import (
    CloudProvider,
    GPUOffer,
    GPUType,
    Instance,
    InstanceStatus,
    ProvisionRequest,
    ProvisionResult,
)

from .vast_provider import VastProvider

from .runpod_provider import (
    RunPodProvider,
    RunPodOffer,
    RunPodEndpointInfo,
    create_runpod_provider,
)

from .gcp_provider import (
    GCPProvider,
    GCPOffer,
    GCPRegion,
    VertexAIJobSpec,
    create_gcp_provider,
)

from .mock_provider import MockProvider, MockConfig

__all__ = [
    # Base classes
    "CloudProvider",
    "GPUOffer",
    "GPUType",
    "Instance",
    "InstanceStatus",
    "ProvisionRequest",
    "ProvisionResult",
    # Providers
    "VastProvider",
    "RunPodProvider",
    "RunPodOffer",
    "RunPodEndpointInfo",
    "create_runpod_provider",
    "GCPProvider",
    "GCPOffer",
    "GCPRegion",
    "VertexAIJobSpec",
    "create_gcp_provider",
    # Mock (testing)
    "MockProvider",
    "MockConfig",
]

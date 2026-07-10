"""
cloud_orchestrator.py - Multi-Provider Cloud GPU Orchestrator

This is the main entry point for cloud infrastructure management in MICA.
It provides a unified API across multiple cloud providers (Vast.ai, RunPod, GCP).

Key Features:
- Provider abstraction: Same API regardless of backend
- Cross-provider search: Find best offers across all providers
- Cost optimization: Automatic selection based on DCEM or $/hr
- Fault tolerance: Retry with fallback providers
- Cost tracking: Unified billing across providers

Design Philosophy:
- Infrastructure is GENERIC (here, in infrastructure/)
- Worker-specific scoring (DCEM, $/epoch) stays in workers/
- MUDOEnvelope integration for job handoff

Example:
    orchestrator = CloudOrchestrator()
    orchestrator.register_provider(VastProvider(api_key="..."))
    orchestrator.register_provider(RunPodProvider(api_key="..."))
    
    # Search across all providers
    offers = await orchestrator.search_all_offers(GPUType.L40S, max_price=0.60)
    
    # Provision with automatic fallback
    result = await orchestrator.provision(ProvisionRequest(
        gpu_type=GPUType.L40S,
        docker_image="pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
        max_price_per_hour=0.60,
    ))

Author: MICA Team
Date: December 2024
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
from enum import Enum
import uuid

if TYPE_CHECKING:
    from mica.infrastructure.gpu_scorer import GPUScorer

from .providers.base_provider import (
    CloudProvider,
    GPUOffer,
    GPUType,
    Instance,
    InstanceStatus,
    ProvisionRequest,
    ProvisionResult,
)


class SelectionStrategy(Enum):
    """Strategy for selecting offers across providers."""
    CHEAPEST = "cheapest"           # Lowest $/hr
    BEST_DCEM = "best_dcem"         # Best $/ns (requires scorer)
    FASTEST = "fastest"             # Highest GPU memory bandwidth
    MOST_RELIABLE = "most_reliable"  # Lowest spot interruption rate
    BALANCED = "balanced"           # Weighted combination


@dataclass
class CostRecord:
    """Record of cost incurred by an instance."""
    instance_id: str
    provider: str
    gpu_type: GPUType
    started_at: datetime
    ended_at: Optional[datetime] = None
    price_per_hour: float = 0.0
    total_cost_usd: float = 0.0
    job_id: Optional[str] = None
    worker_type: Optional[str] = None


@dataclass
class OrchestratorStats:
    """Statistics for the orchestrator."""
    total_provisions: int = 0
    successful_provisions: int = 0
    failed_provisions: int = 0
    total_cost_usd: float = 0.0
    total_gpu_hours: float = 0.0
    instances_by_provider: Dict[str, int] = field(default_factory=dict)
    costs_by_provider: Dict[str, float] = field(default_factory=dict)


class CloudOrchestrator:
    """
    Multi-provider cloud GPU orchestrator.
    
    This class manages multiple cloud providers and provides a unified
    interface for provisioning GPU instances.
    """
    
    def __init__(
        self,
        default_strategy: SelectionStrategy = SelectionStrategy.CHEAPEST,
        max_retries: int = 3,
        retry_delay_seconds: float = 5.0,
    ):
        """
        Initialize the orchestrator.
        
        Args:
            default_strategy: Default offer selection strategy
            max_retries: Maximum provision attempts before failing
            retry_delay_seconds: Delay between retries
        """
        self.providers: Dict[str, CloudProvider] = {}
        self.default_strategy = default_strategy
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        
        # Instance tracking
        self._instances: Dict[str, Instance] = {}
        self._cost_records: List[CostRecord] = []
        
        # Statistics
        self.stats = OrchestratorStats()
        
        # Custom scorer (set by workers for DCEM, $/epoch, etc.)
        self._scorer: Optional[Callable[[GPUOffer], float]] = None
    
    def register_provider(self, provider: CloudProvider) -> None:
        """
        Register a cloud provider.
        
        Args:
            provider: CloudProvider instance (VastProvider, RunPodProvider, etc.)
        """
        self.providers[provider.PROVIDER_NAME] = provider
        self.stats.instances_by_provider[provider.PROVIDER_NAME] = 0
        self.stats.costs_by_provider[provider.PROVIDER_NAME] = 0.0
    
    def set_scorer(self, scorer: Callable[[GPUOffer], float]) -> None:
        """
        Set custom scoring function for offer selection.
        
        Workers use this to inject their domain-specific scoring:
        - Dynamo: DCEM ($/ns)
        - Chronos: $/epoch
        - Spectra: $/inference
        
        Args:
            scorer: Function that takes GPUOffer and returns score (lower is better)
        """
        self._scorer = scorer
    
    async def health_check_all(self) -> Dict[str, bool]:
        """
        Check health of all registered providers.
        
        Returns:
            Dict mapping provider name to health status
        """
        results = {}
        
        async def check_one(name: str, provider: CloudProvider):
            try:
                results[name] = await provider.health_check()
            except Exception:
                results[name] = False
        
        await asyncio.gather(*[
            check_one(name, provider)
            for name, provider in self.providers.items()
        ])
        
        return results
    
    async def search_all_offers(
        self,
        gpu_type: GPUType,
        max_price: Optional[float] = None,
        min_gpu_count: int = 1,
        prefer_spot: bool = True,
        providers: Optional[List[str]] = None,
    ) -> List[GPUOffer]:
        """
        Search for offers across all registered providers.
        
        Args:
            gpu_type: Desired GPU type
            max_price: Maximum $/hr
            min_gpu_count: Minimum GPUs per instance
            prefer_spot: Prefer spot instances
            providers: Specific providers to search (None = all)
            
        Returns:
            List of GPUOffer from all providers, sorted by price
        """
        target_providers = providers or list(self.providers.keys())
        all_offers: List[GPUOffer] = []
        
        async def search_one(name: str):
            provider = self.providers.get(name)
            if provider is None:
                return []
            
            try:
                offers = await provider.search_offers(
                    gpu_type=gpu_type,
                    max_price=max_price,
                    min_gpu_count=min_gpu_count,
                    prefer_spot=prefer_spot,
                )
                return offers
            except Exception as e:
                print(f"Error searching {name}: {e}")
                return []
        
        # Search all providers in parallel
        results = await asyncio.gather(*[
            search_one(name) for name in target_providers
        ])
        
        for offers in results:
            all_offers.extend(offers)
        
        # Apply custom scorer if set
        if self._scorer is not None:
            for offer in all_offers:
                try:
                    offer.dcem_score = self._scorer(offer)
                except Exception:
                    offer.dcem_score = float('inf')
        
        # Sort based on strategy
        if self.default_strategy == SelectionStrategy.CHEAPEST:
            all_offers.sort(key=lambda x: x.price_per_hour)
        elif self.default_strategy == SelectionStrategy.BEST_DCEM and self._scorer:
            all_offers.sort(key=lambda x: x.dcem_score or float('inf'))
        elif self.default_strategy == SelectionStrategy.MOST_RELIABLE:
            all_offers.sort(key=lambda x: x.spot_interruption_rate or 0.5)
        else:
            # Default to price
            all_offers.sort(key=lambda x: x.price_per_hour)
        
        return all_offers
    
    async def provision(
        self,
        request: ProvisionRequest,
        strategy: Optional[SelectionStrategy] = None,
        preferred_provider: Optional[str] = None,
    ) -> ProvisionResult:
        """
        Provision a GPU instance based on request.
        
        This method searches for offers, selects the best one based on
        strategy, and attempts to create an instance with automatic
        fallback to other offers on failure.
        
        Args:
            request: ProvisionRequest with requirements
            strategy: Selection strategy (None = use default)
            preferred_provider: Try this provider first
            
        Returns:
            ProvisionResult with instance or error
        """
        strategy = strategy or self.default_strategy
        self.stats.total_provisions += 1
        
        # Search for offers
        offers = await self.search_all_offers(
            gpu_type=request.gpu_type,
            max_price=request.max_price_per_hour,
            min_gpu_count=request.gpu_count,
            prefer_spot=request.prefer_spot,
        )
        
        if not offers:
            self.stats.failed_provisions += 1
            return ProvisionResult(
                success=False,
                error_message=f"No offers found for {request.gpu_type}"
            )
        
        # Prioritize preferred provider if specified
        if preferred_provider:
            preferred_offers = [o for o in offers if o.provider == preferred_provider]
            other_offers = [o for o in offers if o.provider != preferred_provider]
            offers = preferred_offers + other_offers
        
        # Try offers in order until one succeeds
        last_error = None
        attempts = 0
        
        for offer in offers[:self.max_retries]:
            attempts += 1
            
            try:
                provider = self.providers.get(offer.provider)
                if provider is None:
                    continue
                
                result = await provider.create_instance(
                    offer=offer,
                    docker_image=request.docker_image,
                    docker_command=request.docker_command,
                    env_vars=request.env_vars,
                    job_id=request.job_id,
                )
                
                if result.success and result.instance:
                    # Track instance
                    self._instances[result.instance.instance_id] = result.instance
                    
                    # Update stats
                    self.stats.successful_provisions += 1
                    self.stats.instances_by_provider[offer.provider] = \
                        self.stats.instances_by_provider.get(offer.provider, 0) + 1
                    
                    # Start cost tracking
                    self._start_cost_tracking(result.instance, request)
                    
                    return result
                else:
                    last_error = result.error_message
                    
            except Exception as e:
                last_error = str(e)
            
            # Delay before retry
            if attempts < len(offers):
                await asyncio.sleep(self.retry_delay_seconds)
        
        self.stats.failed_provisions += 1
        return ProvisionResult(
            success=False,
            error_message=f"All {attempts} provision attempts failed. Last error: {last_error}"
        )
    
    async def destroy(self, instance_id: str) -> bool:
        """
        Destroy an instance.
        
        Args:
            instance_id: Instance to destroy
            
        Returns:
            True if destroyed successfully
        """
        instance = self._instances.get(instance_id)
        if instance is None:
            return False
        
        provider = self.providers.get(instance.provider)
        if provider is None:
            return False
        
        # Stop cost tracking
        self._stop_cost_tracking(instance_id)
        
        # Destroy instance
        success = await provider.destroy_instance(instance_id)
        
        if success:
            instance.status = InstanceStatus.TERMINATED
            del self._instances[instance_id]
        
        return success
    
    async def destroy_all(self) -> Dict[str, bool]:
        """
        Destroy all tracked instances.
        
        Returns:
            Dict mapping instance_id to destruction success
        """
        results = {}
        
        for instance_id in list(self._instances.keys()):
            results[instance_id] = await self.destroy(instance_id)
        
        return results
    
    async def get_status(self, instance_id: str) -> Optional[Instance]:
        """
        Get current status of an instance.
        
        Args:
            instance_id: Instance to check
            
        Returns:
            Instance with current status, or None if not found
        """
        instance = self._instances.get(instance_id)
        if instance is None:
            return None
        
        provider = self.providers.get(instance.provider)
        if provider is None:
            return instance
        
        try:
            updated = await provider.get_instance_status(instance_id)
            self._instances[instance_id] = updated
            return updated
        except Exception:
            return instance
    
    async def wait_for_ready(
        self,
        instance_id: str,
        timeout_seconds: float = 300,
    ) -> Instance:
        """
        Wait for instance to be ready.
        
        Args:
            instance_id: Instance to wait for
            timeout_seconds: Maximum wait time
            
        Returns:
            Instance in RUNNING status
        """
        instance = self._instances.get(instance_id)
        if instance is None:
            raise ValueError(f"Unknown instance: {instance_id}")
        
        provider = self.providers.get(instance.provider)
        if provider is None:
            raise RuntimeError(f"Provider not found: {instance.provider}")
        
        return await provider.wait_for_ready(instance_id, timeout_seconds)
    
    def _start_cost_tracking(
        self,
        instance: Instance,
        request: ProvisionRequest,
    ) -> None:
        """Start tracking costs for an instance."""
        record = CostRecord(
            instance_id=instance.instance_id,
            provider=instance.provider,
            gpu_type=instance.gpu_type,
            started_at=datetime.utcnow(),
            price_per_hour=instance.price_per_hour,
            job_id=request.job_id,
            worker_type=request.worker_type,
        )
        self._cost_records.append(record)
    
    def _stop_cost_tracking(self, instance_id: str) -> None:
        """Stop tracking costs and compute total."""
        for record in self._cost_records:
            if record.instance_id == instance_id and record.ended_at is None:
                record.ended_at = datetime.utcnow()
                duration = (record.ended_at - record.started_at).total_seconds()
                hours = duration / 3600
                record.total_cost_usd = hours * record.price_per_hour
                
                # Update stats
                self.stats.total_cost_usd += record.total_cost_usd
                self.stats.total_gpu_hours += hours
                self.stats.costs_by_provider[record.provider] = \
                    self.stats.costs_by_provider.get(record.provider, 0) + record.total_cost_usd
                
                break
    
    def get_cost_summary(self) -> Dict[str, Any]:
        """
        Get cost summary across all instances.
        
        Returns:
            Dict with cost breakdown by provider, worker type, etc.
        """
        # Compute running costs for active instances
        running_cost = 0.0
        for instance in self._instances.values():
            running_cost += instance.compute_current_cost()
        
        # Aggregate by worker type
        costs_by_worker: Dict[str, float] = {}
        for record in self._cost_records:
            worker = record.worker_type or "unknown"
            costs_by_worker[worker] = costs_by_worker.get(worker, 0) + record.total_cost_usd
        
        return {
            "total_cost_usd": self.stats.total_cost_usd + running_cost,
            "completed_cost_usd": self.stats.total_cost_usd,
            "running_cost_usd": running_cost,
            "total_gpu_hours": self.stats.total_gpu_hours,
            "costs_by_provider": dict(self.stats.costs_by_provider),
            "costs_by_worker_type": costs_by_worker,
            "active_instances": len(self._instances),
            "total_provisions": self.stats.total_provisions,
            "success_rate": (
                self.stats.successful_provisions / max(self.stats.total_provisions, 1)
            ),
        }
    
    def list_instances(self) -> List[Instance]:
        """List all active instances."""
        return list(self._instances.values())

    # ── GPU cascade provisioning (W1-4 + W4 VRAM gate) ───────────

    _cascade_logger = logging.getLogger("mica.infrastructure.cloud_orchestrator.cascade")

    async def provision_with_cascade(
        self,
        request: ProvisionRequest,
        gpu_cascade: List[GPUType],
        strategy: Optional[SelectionStrategy] = None,
        preferred_provider: Optional[str] = None,
        scorer: Optional[GPUScorer] = None,
        atom_count: int = 0,
    ) -> ProvisionResult:
        """
        Provision iterating through a GPU fallback cascade.

        For each GPU type in *gpu_cascade*, calls :meth:`provision` with
        that type.  Returns the first successful result, or a failure
        aggregating all attempted types.

        If *scorer* and *atom_count* > 0 are provided, GPUs that fail the
        VRAM check are skipped before attempting provisioning.

        Args:
            request: Base provision request (gpu_type will be overridden
                     per cascade step).
            gpu_cascade: Ordered list of GPU types to try.
            strategy: Selection strategy (None = use default).
            preferred_provider: Try this provider first at every step.
            scorer: Optional GPUScorer for VRAM gating.
            atom_count: System atom count for VRAM check (0 = skip check).

        Returns:
            ProvisionResult with instance or aggregated error.
        """
        if not gpu_cascade:
            return ProvisionResult(
                success=False,
                error_message="Empty GPU cascade — nothing to provision",
            )

        errors: List[str] = []
        skipped_vram: List[str] = []

        for gpu_type in gpu_cascade:
            # ── W4 VRAM gate ─────────────────────────────────────
            if scorer and atom_count > 0 and not scorer.check_vram(gpu_type, atom_count):
                self._cascade_logger.info(
                    "Skipping %s — insufficient VRAM for %d atoms",
                    gpu_type.value, atom_count,
                )
                skipped_vram.append(gpu_type.value)
                continue

            # Patch the request with the current cascade step
            step_request = ProvisionRequest(
                gpu_type=gpu_type,
                gpu_count=request.gpu_count,
                docker_image=request.docker_image,
                docker_command=request.docker_command,
                max_price_per_hour=request.max_price_per_hour,
                prefer_spot=request.prefer_spot,
                env_vars=request.env_vars,
                job_id=request.job_id,
                worker_type=request.worker_type,
            )

            result = await self.provision(
                step_request,
                strategy=strategy,
                preferred_provider=preferred_provider,
            )

            if result.success:
                return result

            errors.append(f"{gpu_type.value}: {result.error_message}")

        vram_note = ""
        if skipped_vram:
            vram_note = f" Skipped (VRAM): {', '.join(skipped_vram)}."

        return ProvisionResult(
            success=False,
            error_message=(
                f"GPU cascade exhausted ({len(gpu_cascade)} types tried).{vram_note} "
                + "; ".join(errors)
            ),
        )

    def __repr__(self) -> str:
        providers = ", ".join(self.providers.keys())
        return f"<CloudOrchestrator providers=[{providers}] instances={len(self._instances)}>"


# Convenience factory function
async def create_orchestrator(
    vast_api_key: Optional[str] = None,
    runpod_api_key: Optional[str] = None,
    gcp_project: Optional[str] = None,
) -> CloudOrchestrator:
    """
    Factory function to create an orchestrator with common providers.
    
    Args:
        vast_api_key: Vast.ai API key (or uses VAST_API_KEY env var)
        runpod_api_key: RunPod API key (or uses RUNPOD_API_KEY env var)
        gcp_project: GCP project ID for Vertex AI
        
    Returns:
        Configured CloudOrchestrator
    """
    orchestrator = CloudOrchestrator()
    
    # Register Vast.ai
    if vast_api_key or os.environ.get("VAST_API_KEY"):
        try:
            from .providers.vast_provider import VastProvider
            orchestrator.register_provider(VastProvider(api_key=vast_api_key))
        except Exception as e:
            print(f"Failed to register Vast.ai provider: {e}")
    
    # Register RunPod (using existing client)
    if runpod_api_key or os.environ.get("RUNPOD_API_KEY"):
        try:
            from .providers.runpod_provider import RunPodProvider
            orchestrator.register_provider(RunPodProvider(api_key=runpod_api_key))
        except ImportError:
            print("RunPod provider not yet implemented")
    
    # Register GCP/Vertex AI
    if gcp_project or os.environ.get("GOOGLE_CLOUD_PROJECT"):
        try:
            from .providers.gcp_provider import GCPProvider
            orchestrator.register_provider(GCPProvider(project_id=gcp_project))
        except ImportError:
            print("GCP provider not yet implemented")
    
    return orchestrator


# Need os for env vars
import os

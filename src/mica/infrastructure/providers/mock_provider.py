"""
mock_provider.py - Mock GPU Provider for Testing

This module provides a mock implementation of CloudProvider for testing
Team 1's /jobs/* endpoints without incurring real GPU costs.

Features:
- Simulates offer search with configurable latency
- Simulates instance provisioning lifecycle
- Emits events to event store (if configured)
- Configurable failure modes for resilience testing

Author: MICA Team - Team 2
Date: January 2025
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base_provider import (
    CloudProvider,
    GPUOffer,
    GPUType,
    Instance,
    InstanceStatus,
    ProvisionResult,
)


@dataclass
class MockConfig:
    """Configuration for mock behavior."""
    
    # Latency simulation (seconds)
    search_latency: tuple[float, float] = (0.1, 0.5)
    provision_latency: tuple[float, float] = (1.0, 3.0)
    destroy_latency: tuple[float, float] = (0.5, 1.0)
    status_latency: tuple[float, float] = (0.05, 0.2)
    
    # Failure injection
    search_failure_rate: float = 0.0
    provision_failure_rate: float = 0.0
    destroy_failure_rate: float = 0.0
    
    # Pricing (for realistic cost tracking)
    base_prices: Dict[GPUType, float] = field(default_factory=lambda: {
        GPUType.RTX_4090: 0.35,
        GPUType.A100_40GB: 1.20,
        GPUType.A100_80GB: 1.80,
        GPUType.H100_80GB: 3.50,
        GPUType.L40S: 0.95,
        GPUType.A40: 0.65,
        GPUType.T4: 0.25,
    })
    
    # Provisioning behavior
    time_to_running_seconds: float = 5.0
    spot_interruption_probability: float = 0.0


class MockProvider(CloudProvider):
    """
    Mock cloud GPU provider for testing.
    
    This provider simulates the behavior of real GPU providers
    without making actual API calls or provisioning real resources.
    
    Example:
        provider = MockProvider()
        offers = await provider.search_offers(GPUType.A100_40GB)
        result = await provider.create_instance(offers[0], docker_image="test")
        # ... run tests ...
        await provider.destroy_instance(result.instance.instance_id)
    """
    
    PROVIDER_NAME = "mock"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        config: Optional[MockConfig] = None,
        **kwargs,
    ):
        """
        Initialize mock provider.
        
        Args:
            api_key: Ignored (for interface compatibility)
            config: MockConfig for behavior customization
        """
        super().__init__(api_key, **kwargs)
        self.config = config or MockConfig()
        self._instances: Dict[str, Instance] = {}
        self._offers: Dict[str, GPUOffer] = {}
        self._provision_times: Dict[str, datetime] = {}
    
    async def _simulate_latency(self, range_tuple: tuple[float, float]) -> None:
        """Simulate network latency."""
        delay = random.uniform(*range_tuple)
        await asyncio.sleep(delay)
    
    def _should_fail(self, rate: float) -> bool:
        """Check if operation should fail based on failure rate."""
        return random.random() < rate
    
    async def search_offers(
        self,
        gpu_type: GPUType,
        max_price: Optional[float] = None,
        min_gpu_count: int = 1,
        prefer_spot: bool = True,
        region: Optional[str] = None,
    ) -> List[GPUOffer]:
        """
        Search for mock GPU offers.
        
        Returns synthetic offers matching the criteria.
        """
        await self._simulate_latency(self.config.search_latency)
        
        if self._should_fail(self.config.search_failure_rate):
            raise RuntimeError("Mock search failure (injected)")
        
        # Get base price for GPU type
        base_price = self.config.base_prices.get(gpu_type, 1.0)
        
        # Generate 3-5 synthetic offers
        num_offers = random.randint(3, 5)
        offers = []
        
        for i in range(num_offers):
            # Add some price variance
            price_multiplier = random.uniform(0.9, 1.3)
            is_spot = prefer_spot and random.random() > 0.3
            spot_discount = 0.5 if is_spot else 1.0
            
            price = base_price * price_multiplier * spot_discount
            
            # Apply max_price filter
            if max_price and price > max_price:
                continue
            
            offer_id = f"mock-offer-{uuid.uuid4().hex[:8]}"
            
            offer = GPUOffer(
                provider=self.PROVIDER_NAME,
                offer_id=offer_id,
                gpu_type=gpu_type,
                gpu_count=min_gpu_count,
                gpu_memory_gb=40.0 if "40" in gpu_type.value else 80.0,
                cpu_cores=random.choice([16, 32, 64]),
                ram_gb=random.choice([64, 128, 256]),
                disk_gb=random.choice([200, 500, 1000]),
                disk_type="nvme",
                price_per_hour=round(price, 3),
                is_spot=is_spot,
                spot_interruption_rate=0.05 if is_spot else 0.0,
                region=region or random.choice(["us-east", "us-west", "eu-west"]),
                datacenter=f"mock-dc-{i}",
                raw_data={"mock": True},
            )
            
            self._offers[offer_id] = offer
            offers.append(offer)
        
        # Sort by price
        offers.sort(key=lambda o: o.price_per_hour)
        return offers
    
    async def create_instance(
        self,
        offer: GPUOffer,
        docker_image: str,
        docker_command: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        job_id: Optional[str] = None,
    ) -> ProvisionResult:
        """
        Create a mock instance.
        
        The instance starts in PROVISIONING and transitions to RUNNING
        after config.time_to_running_seconds.
        """
        start_time = datetime.utcnow()
        
        await self._simulate_latency(self.config.provision_latency)
        
        if self._should_fail(self.config.provision_failure_rate):
            return ProvisionResult(
                success=False,
                error_message="Mock provision failure (injected)",
                provision_time_seconds=(datetime.utcnow() - start_time).total_seconds(),
            )
        
        instance_id = f"mock-{uuid.uuid4().hex[:12]}"
        
        instance = Instance(
            provider=self.PROVIDER_NAME,
            instance_id=instance_id,
            status=InstanceStatus.PROVISIONING,
            ssh_host=f"mock-{instance_id}.example.com",
            ssh_port=22,
            ssh_user="root",
            gpu_type=offer.gpu_type,
            gpu_count=offer.gpu_count,
            price_per_hour=offer.price_per_hour,
            started_at=datetime.utcnow(),
            job_id=job_id,
            raw_data={
                "docker_image": docker_image,
                "docker_command": docker_command,
                "env_vars": env_vars or {},
                "mock": True,
            },
        )
        
        self._instances[instance_id] = instance
        self._provision_times[instance_id] = datetime.utcnow()
        
        # Schedule transition to RUNNING
        asyncio.create_task(self._transition_to_running(instance_id))
        
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        
        return ProvisionResult(
            success=True,
            instance=instance,
            offer_used=offer,
            provision_time_seconds=elapsed,
        )
    
    async def _transition_to_running(self, instance_id: str) -> None:
        """Background task to transition instance to RUNNING."""
        await asyncio.sleep(self.config.time_to_running_seconds)
        
        if instance_id in self._instances:
            instance = self._instances[instance_id]
            if instance.status == InstanceStatus.PROVISIONING:
                instance.status = InstanceStatus.RUNNING
    
    async def destroy_instance(self, instance_id: str) -> bool:
        """
        Destroy a mock instance.
        """
        await self._simulate_latency(self.config.destroy_latency)
        
        if self._should_fail(self.config.destroy_failure_rate):
            return False
        
        if instance_id not in self._instances:
            return False
        
        instance = self._instances[instance_id]
        instance.status = InstanceStatus.TERMINATED
        
        # Calculate final cost
        if instance.started_at:
            elapsed = (datetime.utcnow() - instance.started_at).total_seconds()
            instance.total_cost_usd = (elapsed / 3600) * instance.price_per_hour
        
        return True
    
    async def get_instance_status(self, instance_id: str) -> Instance:
        """
        Get status of a mock instance.
        """
        await self._simulate_latency(self.config.status_latency)
        
        if instance_id not in self._instances:
            return Instance(
                provider=self.PROVIDER_NAME,
                instance_id=instance_id,
                status=InstanceStatus.ERROR,
                error_message="Instance not found",
            )
        
        instance = self._instances[instance_id]
        
        # Update computed cost
        instance.total_cost_usd = instance.compute_current_cost()
        
        # Simulate spot interruption
        if (
            instance.status == InstanceStatus.RUNNING
            and self.config.spot_interruption_probability > 0
            and random.random() < self.config.spot_interruption_probability
        ):
            instance.status = InstanceStatus.PREEMPTED
            instance.error_message = "Spot instance preempted (simulated)"
        
        return instance
    
    # ===========================================
    # Testing utilities
    # ===========================================
    
    def get_all_instances(self) -> List[Instance]:
        """Return all instances (for testing)."""
        return list(self._instances.values())
    
    def reset(self) -> None:
        """Reset all state (for testing)."""
        self._instances.clear()
        self._offers.clear()
        self._provision_times.clear()
    
    def inject_instance_failure(self, instance_id: str, error: str) -> None:
        """Force an instance into ERROR state (for testing)."""
        if instance_id in self._instances:
            self._instances[instance_id].status = InstanceStatus.ERROR
            self._instances[instance_id].error_message = error

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 MICA Compute Module - Team 2: Infrastructure & Optimized Execution

Advanced computational resource management following Spanish doc strategy:
- ML-based resource prediction for enhanced sampling
- Production workload stress testing validation
- Adaptive resource allocation optimization
- Real-time system monitoring and dynamic adjustment

Memory requirements compliance:
- Advanced sampling methods (umbrella sampling, metadynamics, replica exchange)
- Production workload stress testing with concurrent validation
"""

from .resource_optimizer import (
    ComputationalResourceOptimizer,
    SamplingMethod,
    SamplingParameters,
    ResourceRequirements,
    PerformanceRecord,
    OptimizationReport,
    SystemMonitor,
    MLResourcePredictor,
    create_resource_optimizer
)

__all__ = [
    "ComputationalResourceOptimizer",
    "SamplingMethod",
    "SamplingParameters", 
    "ResourceRequirements",
    "PerformanceRecord",
    "OptimizationReport",
    "SystemMonitor",
    "MLResourcePredictor",
    "create_resource_optimizer"
]

__version__ = "1.0.0"
__author__ = "Team 2: Infrastructure & Optimized Execution"
__description__ = "Advanced computational resource management for enhanced sampling"
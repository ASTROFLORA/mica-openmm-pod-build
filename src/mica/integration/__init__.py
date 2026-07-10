#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔬 MICA Integration Module - Comprehensive System Validation

Phase 6 & 7 integration testing and validation:
- End-to-end workflow validation across all three expert teams
- Cross-team communication and data exchange testing
- Comprehensive system integration scoring
- Production readiness assessment

Validates complete integration of:
- Team 1: Scientific Reasoning & Biological Validation  
- Team 2: Infrastructure & Optimized Execution
- Team 3: Responsible AI & Continuous Improvement
"""

from .phase67_integration_validator import (
    Phase67IntegrationValidator,
    IntegrationTestResult,
    SystemIntegrationReport,
    run_phase67_integration_validation
)

__all__ = [
    "Phase67IntegrationValidator",
    "IntegrationTestResult", 
    "SystemIntegrationReport",
    "run_phase67_integration_validation"
]

__version__ = "1.0.0"
__author__ = "Team 3: Responsible AI & Continuous Improvement" 
__description__ = "Comprehensive Phase 6 & 7 system integration validation"
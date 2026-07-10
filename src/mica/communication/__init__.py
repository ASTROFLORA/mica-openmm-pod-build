#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📝 MICA Communication Module - Team 3: Responsible AI & Continuous Improvement

Advanced scientific communication capabilities following Spanish doc strategy:
- Adaptive Narrative Generator for audience-specific explanations
- Multi-modal explanation strategies (technical, conceptual, analogical, narrative)
- Scientific accuracy preservation across all complexity levels
- Integration with ethical framework for responsible communication
- Context-aware terminology and complexity adjustment

Moving beyond static documentation to intelligent, adaptive scientific communication.
"""

from .adaptive_narrative_generator import (
    AdaptiveNarrativeGenerator,
    AudienceType,
    ExplanationMode,
    AudienceProfile,
    ScientificContent,
    NarrativeStrategy,
    AdaptiveExplanation,
    TerminologyManager,
    AnalogyGenerator,
    ComplexityAnalyzer,
    create_adaptive_narrative_generator,
    create_expert_audience,
    create_general_audience
)
from .swarm_client import RealSwarmClient, SwarmInvocationResult

__all__ = [
    "AdaptiveNarrativeGenerator",
    "AudienceType",
    "ExplanationMode", 
    "AudienceProfile",
    "ScientificContent",
    "NarrativeStrategy",
    "AdaptiveExplanation",
    "TerminologyManager",
    "AnalogyGenerator",
    "ComplexityAnalyzer",
    "create_adaptive_narrative_generator",
    "create_expert_audience",
    "create_general_audience",
    "RealSwarmClient",
    "SwarmInvocationResult",
]

__version__ = "1.0.0"
__author__ = "Team 3: Responsible AI & Continuous Improvement"
__description__ = "Adaptive narrative generator for audience-specific scientific communication"
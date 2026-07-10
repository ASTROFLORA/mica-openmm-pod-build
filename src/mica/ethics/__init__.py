#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 MICA Ethics Module - Team 3: Responsible AI & Continuous Improvement

Ethical framework for AI usage in scientific publications:
- UNESCO AI Ethics Principles compliance system
- Transparent AI usage documentation and disclosure
- Human oversight and accountability validation
- Bias detection and mitigation recommendations
- Ethical review and approval workflows
- Complete audit trail for responsible AI usage

Following Spanish document strategy for responsible AI integration.
"""

from .ethical_framework import (
    EthicalFramework,
    AIUsageDeclaration,
    EthicalReview,
    PublicationEthicsReport,
    BiasDetector,
    AIUsageType,
    UNESCOPrinciple,
    EthicalRiskLevel,
    create_ethical_framework
)

__all__ = [
    "EthicalFramework",
    "AIUsageDeclaration",
    "EthicalReview", 
    "PublicationEthicsReport",
    "BiasDetector",
    "AIUsageType",
    "UNESCOPrinciple",
    "EthicalRiskLevel",
    "create_ethical_framework"
]

__version__ = "1.0.0"
__author__ = "Team 3: Responsible AI & Continuous Improvement"
__description__ = "Ethical framework for AI usage in scientific publications with UNESCO compliance"
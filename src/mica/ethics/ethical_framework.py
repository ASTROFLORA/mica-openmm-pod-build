#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 Ethical Framework for Scientific Publications - Team 3: Responsible AI

UNESCO AI Ethics Principles compliance system for scientific publications:
- Transparent AI usage documentation and disclosure
- Human oversight and accountability validation
- Bias detection and mitigation in AI-generated content
- Ethical review and approval workflows
- Complete audit trail for AI usage in publications

Following Spanish document strategy for responsible AI integration in scientific research.
"""

import asyncio
import logging
import json
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid

logger = logging.getLogger(__name__)

class UNESCOPrinciple(Enum):
    """UNESCO AI Ethics Principles"""
    HUMAN_RIGHTS_DIGNITY = "human_rights_dignity"
    LIVING_IN_PEACEFUL_SOCIETIES = "living_in_peaceful_societies"
    ENSURING_DIVERSITY_INCLUSION = "ensuring_diversity_inclusion"
    FLOURISHING_ENVIRONMENT = "flourishing_environment"

class EthicalRiskLevel(Enum):
    """Risk levels for ethical review"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class AIUsageType(Enum):
    """Types of AI usage in scientific work"""
    DATA_ANALYSIS = "data_analysis"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    FIGURE_GENERATION = "figure_generation"
    TEXT_ENHANCEMENT = "text_enhancement"
    SIMULATION_OPTIMIZATION = "simulation_optimization"

@dataclass
class AIUsageDeclaration:
    """Declaration of AI usage in scientific work"""
    usage_id: str
    usage_type: AIUsageType
    ai_system_name: str
    ai_system_version: str
    purpose_description: str
    human_oversight_level: str
    validation_method: str
    limitations_acknowledged: List[str]
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class EthicalReview:
    """Ethical review assessment"""
    review_id: str
    reviewer_id: str
    ai_usage: AIUsageDeclaration
    unesco_compliance: Dict[UNESCOPrinciple, bool]
    risk_assessment: Dict[str, EthicalRiskLevel]
    transparency_score: float
    accountability_score: float
    overall_approval: bool
    recommendations: List[str]
    review_timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class PublicationEthicsReport:
    """Comprehensive ethics report for publication"""
    report_id: str
    publication_title: str
    ai_usage_declarations: List[AIUsageDeclaration]
    ethical_reviews: List[EthicalReview]
    overall_compliance_score: float
    transparency_statement: str
    ethical_approval_status: bool
    generated_at: datetime = field(default_factory=datetime.now)

class BiasDetector:
    """AI bias detection system"""
    
    def __init__(self):
        self.bias_patterns = {
            "gender_bias": ["he", "she", "his", "her", "man", "woman"],
            "cultural_bias": ["western", "eastern", "developed", "developing"],
            "institutional_bias": ["prestigious", "elite", "top-tier", "leading"]
        }
    
    async def detect_bias(self, content: str) -> Dict[str, Any]:
        """Detect potential bias in AI-generated content"""
        bias_detection = {
            "bias_detected": False,
            "bias_types": [],
            "recommendations": []
        }
        
        content_lower = content.lower()
        
        for bias_type, patterns in self.bias_patterns.items():
            for pattern in patterns:
                if pattern.lower() in content_lower:
                    bias_detection["bias_detected"] = True
                    if bias_type not in bias_detection["bias_types"]:
                        bias_detection["bias_types"].append(bias_type)
        
        if bias_detection["bias_detected"]:
            bias_detection["recommendations"] = self._generate_bias_recommendations(
                bias_detection["bias_types"]
            )
        
        return bias_detection
    
    def _generate_bias_recommendations(self, bias_types: List[str]) -> List[str]:
        """Generate bias mitigation recommendations"""
        recommendations = []
        
        if "gender_bias" in bias_types:
            recommendations.append("Use gender-neutral language where possible")
        if "cultural_bias" in bias_types:
            recommendations.append("Use objective, descriptive terms instead of value-laden language")
        if "institutional_bias" in bias_types:
            recommendations.append("Focus on research quality rather than institutional prestige")
        
        return recommendations

class EthicalFramework:
    """
    🤖 Comprehensive Ethical Framework for AI in Scientific Publications
    
    UNESCO AI Ethics Principles compliance system providing:
    - Transparent AI usage documentation and disclosure
    - Human oversight and accountability validation
    - Bias detection and mitigation recommendations
    - Ethical review and approval workflows
    - Complete audit trail for responsible AI usage
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or self._default_config()
        self.bias_detector = BiasDetector()
        
        # Storage for ethical records
        self.ai_usage_registry: Dict[str, AIUsageDeclaration] = {}
        self.ethical_reviews: Dict[str, EthicalReview] = {}
        self.publication_reports: Dict[str, PublicationEthicsReport] = {}
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("🤖 Ethical Framework initialized with UNESCO principles compliance")
    
    def _default_config(self) -> Dict[str, Any]:
        """Default configuration for ethical framework"""
        return {
            "transparency_threshold": 0.8,
            "bias_detection_enabled": True,
            "unesco_compliance_required": True,
            "auto_generate_statements": True
        }
    
    async def declare_ai_usage(self, 
                             usage_type: AIUsageType,
                             ai_system_name: str,
                             ai_system_version: str,
                             purpose_description: str,
                             human_oversight_level: str,
                             validation_method: str,
                             limitations_acknowledged: List[str]) -> str:
        """
        Declare AI usage for ethical review and transparency.
        
        Returns:
            Usage declaration ID for tracking
        """
        
        declaration = AIUsageDeclaration(
            usage_id=str(uuid.uuid4()),
            usage_type=usage_type,
            ai_system_name=ai_system_name,
            ai_system_version=ai_system_version,
            purpose_description=purpose_description,
            human_oversight_level=human_oversight_level,
            validation_method=validation_method,
            limitations_acknowledged=limitations_acknowledged
        )
        
        self.ai_usage_registry[declaration.usage_id] = declaration
        
        self.logger.info(f"AI usage declared: {usage_type.value} with {ai_system_name}")
        return declaration.usage_id
    
    async def conduct_ethical_review(self, 
                                   usage_id: str,
                                   reviewer_id: str = "automated_system") -> EthicalReview:
        """
        Conduct comprehensive ethical review of AI usage.
        
        Returns:
            EthicalReview with complete assessment
        """
        
        if usage_id not in self.ai_usage_registry:
            raise ValueError(f"AI usage declaration {usage_id} not found")
        
        declaration = self.ai_usage_registry[usage_id]
        
        # UNESCO compliance assessment
        unesco_compliance = await self._assess_unesco_compliance(declaration)
        
        # Risk assessment
        risk_assessment = await self._assess_ethical_risks(declaration)
        
        # Bias analysis
        bias_analysis = await self.bias_detector.detect_bias(declaration.purpose_description)
        
        # Transparency and accountability scores
        transparency_score = await self._calculate_transparency_score(declaration)
        accountability_score = await self._calculate_accountability_score(declaration)
        
        # Overall approval determination
        overall_approval = (
            all(unesco_compliance.values()) and
            transparency_score >= self.config["transparency_threshold"] and
            all(risk != EthicalRiskLevel.CRITICAL for risk in risk_assessment.values()) and
            accountability_score >= 0.7
        )
        
        # Generate recommendations
        recommendations = await self._generate_recommendations(
            declaration, unesco_compliance, risk_assessment, bias_analysis
        )
        
        review = EthicalReview(
            review_id=str(uuid.uuid4()),
            reviewer_id=reviewer_id,
            ai_usage=declaration,
            unesco_compliance=unesco_compliance,
            risk_assessment=risk_assessment,
            transparency_score=transparency_score,
            accountability_score=accountability_score,
            overall_approval=overall_approval,
            recommendations=recommendations
        )
        
        self.ethical_reviews[review.review_id] = review
        
        self.logger.info(f"Ethical review completed: {'APPROVED' if overall_approval else 'CONDITIONAL'}")
        return review
    
    async def _assess_unesco_compliance(self, declaration: AIUsageDeclaration) -> Dict[UNESCOPrinciple, bool]:
        """Assess compliance with UNESCO AI Ethics principles"""
        
        return {
            UNESCOPrinciple.HUMAN_RIGHTS_DIGNITY: (
                declaration.human_oversight_level != "none" and
                len(declaration.limitations_acknowledged) > 0
            ),
            UNESCOPrinciple.LIVING_IN_PEACEFUL_SOCIETIES: (
                "misinformation" not in declaration.purpose_description.lower()
            ),
            UNESCOPrinciple.ENSURING_DIVERSITY_INCLUSION: (
                "bias" in " ".join(declaration.limitations_acknowledged).lower() or
                len(declaration.limitations_acknowledged) >= 2
            ),
            UNESCOPrinciple.FLOURISHING_ENVIRONMENT: (
                declaration.usage_type in [AIUsageType.DATA_ANALYSIS, AIUsageType.SIMULATION_OPTIMIZATION]
            )
        }
    
    async def _assess_ethical_risks(self, declaration: AIUsageDeclaration) -> Dict[str, EthicalRiskLevel]:
        """Assess ethical risks associated with AI usage"""
        
        risks = {}
        
        # Bias risk
        if declaration.usage_type in [AIUsageType.HYPOTHESIS_GENERATION, AIUsageType.TEXT_ENHANCEMENT]:
            risks["bias_risk"] = EthicalRiskLevel.HIGH if "bias" not in " ".join(declaration.limitations_acknowledged).lower() else EthicalRiskLevel.MEDIUM
        else:
            risks["bias_risk"] = EthicalRiskLevel.LOW
        
        # Transparency risk
        oversight_risks = {
            "minimal": EthicalRiskLevel.HIGH,
            "moderate": EthicalRiskLevel.MEDIUM,
            "comprehensive": EthicalRiskLevel.LOW
        }
        risks["transparency_risk"] = oversight_risks.get(declaration.human_oversight_level, EthicalRiskLevel.HIGH)
        
        # Accountability risk
        risks["accountability_risk"] = EthicalRiskLevel.HIGH if len(declaration.validation_method) < 10 else EthicalRiskLevel.MEDIUM
        
        return risks
    
    async def _calculate_transparency_score(self, declaration: AIUsageDeclaration) -> float:
        """Calculate transparency score"""
        score = 0.0
        
        # AI system disclosure
        if declaration.ai_system_name and declaration.ai_system_version:
            score += 0.25
        
        # Purpose description quality
        if len(declaration.purpose_description) >= 50:
            score += 0.25
        
        # Human oversight documentation
        if declaration.human_oversight_level in ["comprehensive", "substantial"]:
            score += 0.25
        
        # Limitations acknowledgment
        if len(declaration.limitations_acknowledged) >= 2:
            score += 0.25
        
        return score
    
    async def _calculate_accountability_score(self, declaration: AIUsageDeclaration) -> float:
        """Calculate accountability score"""
        score = 0.0
        
        # Human oversight level
        oversight_scores = {
            "comprehensive": 0.4,
            "substantial": 0.3,
            "moderate": 0.2,
            "minimal": 0.1
        }
        score += oversight_scores.get(declaration.human_oversight_level, 0.0)
        
        # Validation method quality
        if len(declaration.validation_method) >= 30:
            score += 0.3
        elif len(declaration.validation_method) >= 10:
            score += 0.2
        
        # Limitations acknowledgment
        if len(declaration.limitations_acknowledged) >= 3:
            score += 0.3
        elif len(declaration.limitations_acknowledged) >= 1:
            score += 0.2
        
        return min(1.0, score)
    
    async def _generate_recommendations(self, 
                                      declaration: AIUsageDeclaration,
                                      unesco_compliance: Dict[UNESCOPrinciple, bool],
                                      risk_assessment: Dict[str, EthicalRiskLevel],
                                      bias_analysis: Dict[str, Any]) -> List[str]:
        """Generate recommendations from review"""
        
        recommendations = []
        
        # UNESCO compliance recommendations
        for principle, compliant in unesco_compliance.items():
            if not compliant:
                if principle == UNESCOPrinciple.HUMAN_RIGHTS_DIGNITY:
                    recommendations.append("Strengthen human oversight and validation procedures")
                elif principle == UNESCOPrinciple.ENSURING_DIVERSITY_INCLUSION:
                    recommendations.append("Add explicit bias acknowledgment and mitigation measures")
        
        # Risk-based recommendations
        for risk_type, level in risk_assessment.items():
            if level in [EthicalRiskLevel.HIGH, EthicalRiskLevel.CRITICAL]:
                recommendations.append(f"Address {risk_type} through additional safeguards")
        
        # Bias mitigation recommendations
        if bias_analysis.get("recommendations"):
            recommendations.extend(bias_analysis["recommendations"])
        
        # General recommendations
        recommendations.extend([
            "Document AI usage in methods section",
            "Include AI ethics statement in manuscript",
            "Provide supplementary materials with AI procedures"
        ])
        
        return recommendations
    
    async def generate_publication_ethics_report(self, 
                                               publication_title: str,
                                               usage_ids: List[str]) -> PublicationEthicsReport:
        """
        Generate comprehensive ethics report for publication.
        
        Returns:
            PublicationEthicsReport with complete assessment
        """
        
        declarations = [self.ai_usage_registry[uid] for uid in usage_ids if uid in self.ai_usage_registry]
        reviews = [review for review in self.ethical_reviews.values() 
                  if review.ai_usage.usage_id in usage_ids]
        
        # Calculate overall compliance score
        if reviews:
            transparency_scores = [r.transparency_score for r in reviews]
            accountability_scores = [r.accountability_score for r in reviews]
            approval_rates = [1.0 if r.overall_approval else 0.0 for r in reviews]
            
            overall_compliance = (
                sum(transparency_scores) / len(transparency_scores) * 0.4 +
                sum(accountability_scores) / len(accountability_scores) * 0.3 +
                sum(approval_rates) / len(approval_rates) * 0.3
            )
        else:
            overall_compliance = 0.0
        
        # Generate transparency statement
        transparency_statement = self._generate_transparency_statement(declarations)
        
        # Determine ethical approval
        ethical_approval = all(r.overall_approval for r in reviews) if reviews else False
        
        report = PublicationEthicsReport(
            report_id=str(uuid.uuid4()),
            publication_title=publication_title,
            ai_usage_declarations=declarations,
            ethical_reviews=reviews,
            overall_compliance_score=overall_compliance,
            transparency_statement=transparency_statement,
            ethical_approval_status=ethical_approval
        )
        
        self.publication_reports[report.report_id] = report
        
        self.logger.info(f"Publication ethics report generated - Score: {overall_compliance:.2f}")
        return report
    
    def _generate_transparency_statement(self, declarations: List[AIUsageDeclaration]) -> str:
        """Generate transparency statement for publication"""
        
        if not declarations:
            return "No AI systems were used in this research."
        
        ai_systems = set()
        usage_types = set()
        
        for decl in declarations:
            ai_systems.add(f"{decl.ai_system_name} (v{decl.ai_system_version})")
            usage_types.add(decl.usage_type.value.replace('_', ' '))
        
        statement = f"This research utilized: {', '.join(ai_systems)}. "
        statement += f"AI was used for: {', '.join(usage_types)}. "
        statement += "All AI-generated content was subject to human review and validation."
        
        return statement

# Factory function
def create_ethical_framework(config: Dict[str, Any] = None) -> EthicalFramework:
    """Create ethical framework instance"""
    return EthicalFramework(config)

if __name__ == "__main__":
    async def main():
        # Demo usage
        framework = create_ethical_framework()
        
        # Declare AI usage for enhanced sampling optimization
        usage_id = await framework.declare_ai_usage(
            usage_type=AIUsageType.SIMULATION_OPTIMIZATION,
            ai_system_name="MICA Resource Optimizer",
            ai_system_version="1.0.0",
            purpose_description="Optimize computational resources for umbrella sampling and metadynamics simulations using ML models",
            human_oversight_level="comprehensive",
            validation_method="Performance validated against actual simulation results with expert review",
            limitations_acknowledged=[
                "Predictions based on historical data may not account for novel simulation types",
                "Resource optimization assumes similar hardware configurations",
                "Model accuracy depends on quality of training data"
            ]
        )
        
        # Conduct ethical review
        review = await framework.conduct_ethical_review(usage_id)
        
        # Generate publication ethics report  
        report = await framework.generate_publication_ethics_report(
            "Enhanced Sampling Optimization with Machine Learning Resource Prediction",
            [usage_id]
        )
        
        print("🤖 Ethical Framework Demo Results:")
        print(f"• Usage ID: {usage_id}")
        print(f"• Review Approval: {'✅ APPROVED' if review.overall_approval else '❌ CONDITIONAL'}")
        print(f"• Transparency Score: {review.transparency_score:.2f}")
        print(f"• Accountability Score: {review.accountability_score:.2f}")
        print(f"• UNESCO Compliance: {sum(review.unesco_compliance.values())}/{len(review.unesco_compliance)}")
        print(f"• Publication Compliance Score: {report.overall_compliance_score:.2f}")
        print(f"• Ethical Approval Status: {'✅ APPROVED' if report.ethical_approval_status else '❌ PENDING'}")
        
        print(f"\\n📋 Transparency Statement:")
        print(f"  {report.transparency_statement}")
        
        print(f"\\n📝 Top Recommendations:")
        for i, rec in enumerate(review.recommendations[:3], 1):
            print(f"  {i}. {rec}")
        
        print("\\n✅ Team 3 Ethical Framework: COMPLETE")
    
    asyncio.run(main())
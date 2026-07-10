#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔄 Automated Feedback System (AFT) - Team 3: Continuous Improvement

Advanced feedback collection and learning system for experimental results:
- Automated collection from enhanced sampling simulations  
- Performance analysis and trend detection
- ML model retraining based on feedback
- Scientific protocol optimization recommendations
- Integration with resource optimizer and ethical framework

Following Spanish document strategy for automated improvement and learning.
"""

import asyncio
import logging
import numpy as np
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import uuid

# ML imports for continuous learning
try:
    from sklearn.ensemble import RandomForestRegressor
    import pandas as pd
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

logger = logging.getLogger(__name__)

class FeedbackType(Enum):
    """Types of experimental feedback"""
    SIMULATION_PERFORMANCE = "simulation_performance"
    CONVERGENCE_ANALYSIS = "convergence_analysis"
    RESOURCE_EFFICIENCY = "resource_efficiency"
    SCIENTIFIC_QUALITY = "scientific_quality"

class FeedbackSeverity(Enum):
    """Severity levels for feedback"""
    EXCELLENT = "excellent"
    GOOD = "good" 
    ACCEPTABLE = "acceptable"
    POOR = "poor"
    CRITICAL = "critical"

@dataclass
class ExperimentalFeedback:
    """Feedback from experimental results"""
    feedback_id: str
    experiment_id: str
    feedback_type: FeedbackType
    severity: FeedbackSeverity
    metrics: Dict[str, float]
    improvement_suggestions: List[str]
    source_component: str
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class LearningInsight:
    """Insights learned from feedback analysis"""
    insight_id: str
    insight_type: str
    description: str
    confidence_score: float
    recommended_actions: List[str]
    impact_assessment: Dict[str, Any]
    generated_at: datetime = field(default_factory=datetime.now)

@dataclass
class OptimizationRecommendation:
    """Recommendations for protocol optimization"""
    recommendation_id: str
    target_component: str
    optimization_type: str
    recommended_parameters: Dict[str, Any]
    expected_improvement: Dict[str, float]
    confidence_level: float
    implementation_priority: str

@dataclass
class AFTReport:
    """Comprehensive AFT analysis report"""
    report_id: str
    analysis_period: Tuple[datetime, datetime]
    total_feedback_collected: int
    feedback_by_type: Dict[FeedbackType, int]
    key_insights: List[LearningInsight]
    optimization_recommendations: List[OptimizationRecommendation]
    learning_effectiveness: float
    generated_at: datetime = field(default_factory=datetime.now)

class FeedbackCollector:
    """Automated feedback collection system"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    async def collect_simulation_feedback(self, 
                                        experiment_id: str,
                                        simulation_results: Dict[str, Any],
                                        resource_usage: Dict[str, Any],
                                        convergence_data: Dict[str, Any]) -> ExperimentalFeedback:
        """Collect feedback from enhanced sampling simulation"""
        
        # Analyze simulation performance
        performance_metrics = {
            "convergence_score": convergence_data.get("convergence_score", 0.0),
            "statistical_significance": convergence_data.get("statistical_significance", 0.0),
            "resource_efficiency": self._calculate_resource_efficiency(resource_usage),
            "scientific_quality": simulation_results.get("scientific_quality", 0.0),
            "execution_time_ratio": resource_usage.get("actual_time", 0) / resource_usage.get("predicted_time", 1)
        }
        
        # Determine severity
        severity = self._assess_performance_severity(performance_metrics)
        
        # Generate suggestions
        suggestions = self._generate_improvement_suggestions(performance_metrics)
        
        feedback = ExperimentalFeedback(
            feedback_id=str(uuid.uuid4()),
            experiment_id=experiment_id,
            feedback_type=FeedbackType.SIMULATION_PERFORMANCE,
            severity=severity,
            metrics=performance_metrics,
            improvement_suggestions=suggestions,
            source_component="enhanced_sampling"
        )
        
        self.logger.info(f"Simulation feedback collected: {experiment_id} - {severity.value}")
        return feedback
    
    async def collect_resource_optimization_feedback(self,
                                                   optimization_id: str,
                                                   predicted_resources: Dict[str, Any],
                                                   actual_resources: Dict[str, Any],
                                                   performance_outcome: Dict[str, Any]) -> ExperimentalFeedback:
        """Collect feedback from resource optimization"""
        
        # Calculate prediction accuracy metrics
        accuracy_metrics = {}
        for resource_type in ["cpu_cores", "memory_gb", "duration_hours"]:
            if resource_type in predicted_resources and resource_type in actual_resources:
                predicted = predicted_resources[resource_type]
                actual = actual_resources[resource_type]
                if predicted > 0:
                    accuracy_metrics[f"{resource_type}_accuracy"] = 1.0 - abs(predicted - actual) / predicted
        
        metrics = {
            **accuracy_metrics,
            "prediction_confidence": predicted_resources.get("confidence_score", 0.0),
            "cost_savings": performance_outcome.get("cost_savings", 0.0),
            "performance_improvement": performance_outcome.get("performance_improvement", 0.0)
        }
        
        severity = self._assess_optimization_severity(metrics)
        suggestions = self._generate_optimization_suggestions(metrics)
        
        feedback = ExperimentalFeedback(
            feedback_id=str(uuid.uuid4()),
            experiment_id=optimization_id,
            feedback_type=FeedbackType.RESOURCE_EFFICIENCY,
            severity=severity,
            metrics=metrics,
            improvement_suggestions=suggestions,
            source_component="resource_optimizer"
        )
        
        self.logger.info(f"Resource optimization feedback collected: {optimization_id} - {severity.value}")
        return feedback
    
    def _calculate_resource_efficiency(self, resource_usage: Dict[str, Any]) -> float:
        """Calculate resource utilization efficiency"""
        allocated = resource_usage.get("allocated_resources", {})
        utilized = resource_usage.get("actual_usage", {})
        
        if not allocated or not utilized:
            return 0.5
        
        efficiency_scores = []
        for resource in ["cpu", "memory", "gpu"]:
            if resource in allocated and resource in utilized:
                alloc = allocated[resource]
                used = utilized[resource]
                if alloc > 0:
                    eff = min(1.0, used / alloc)
                    efficiency_scores.append(eff)
        
        return np.mean(efficiency_scores) if efficiency_scores else 0.5
    
    def _assess_performance_severity(self, metrics: Dict[str, float]) -> FeedbackSeverity:
        """Assess severity based on performance metrics"""
        
        weights = {
            "convergence_score": 0.3,
            "statistical_significance": 0.25,
            "resource_efficiency": 0.25,
            "scientific_quality": 0.2
        }
        
        score = sum(metrics.get(metric, 0) * weight for metric, weight in weights.items())
        
        if score >= 0.9:
            return FeedbackSeverity.EXCELLENT
        elif score >= 0.8:
            return FeedbackSeverity.GOOD
        elif score >= 0.7:
            return FeedbackSeverity.ACCEPTABLE
        elif score >= 0.5:
            return FeedbackSeverity.POOR
        else:
            return FeedbackSeverity.CRITICAL
    
    def _assess_optimization_severity(self, metrics: Dict[str, float]) -> FeedbackSeverity:
        """Assess severity for optimization feedback"""
        
        accuracy_metrics = [v for k, v in metrics.items() if "accuracy" in k]
        avg_accuracy = np.mean(accuracy_metrics) if accuracy_metrics else 0.0
        
        if avg_accuracy >= 0.95:
            return FeedbackSeverity.EXCELLENT
        elif avg_accuracy >= 0.85:
            return FeedbackSeverity.GOOD
        elif avg_accuracy >= 0.75:
            return FeedbackSeverity.ACCEPTABLE
        elif avg_accuracy >= 0.6:
            return FeedbackSeverity.POOR
        else:
            return FeedbackSeverity.CRITICAL
    
    def _generate_improvement_suggestions(self, metrics: Dict[str, float]) -> List[str]:
        """Generate improvement suggestions based on metrics"""
        
        suggestions = []
        
        if metrics.get("convergence_score", 0) < 0.8:
            suggestions.append("Increase simulation time or improve sampling parameters for better convergence")
        
        if metrics.get("resource_efficiency", 0) < 0.7:
            suggestions.append("Optimize resource allocation to reduce waste and improve efficiency")
        
        if metrics.get("statistical_significance", 0) < 0.9:
            suggestions.append("Increase bootstrap sampling or extend equilibration time for better statistics")
        
        return suggestions
    
    def _generate_optimization_suggestions(self, metrics: Dict[str, float]) -> List[str]:
        """Generate optimization improvement suggestions"""
        
        suggestions = []
        
        for resource in ["cpu_cores", "memory_gb", "duration_hours"]:
            accuracy_key = f"{resource}_accuracy"
            if accuracy_key in metrics and metrics[accuracy_key] < 0.8:
                suggestions.append(f"Improve {resource} prediction model with additional training data")
        
        if metrics.get("prediction_confidence", 0) < 0.8:
            suggestions.append("Increase training data diversity to improve prediction confidence")
        
        return suggestions

class LearningEngine:
    """ML-powered learning engine for continuous improvement"""
    
    def __init__(self):
        self.insights_database = []
        self.learning_active = ML_AVAILABLE
        self.logger = logging.getLogger(__name__)
    
    async def analyze_feedback_patterns(self, feedback_history: List[ExperimentalFeedback]) -> List[LearningInsight]:
        """Analyze feedback patterns to generate learning insights"""
        
        if len(feedback_history) < 5:
            return []
        
        insights = []
        
        # Performance trend analysis
        performance_insights = await self._analyze_performance_trends(feedback_history)
        insights.extend(performance_insights)
        
        # Resource optimization patterns
        resource_insights = await self._analyze_resource_patterns(feedback_history)
        insights.extend(resource_insights)
        
        # Failure pattern analysis
        failure_insights = await self._analyze_failure_patterns(feedback_history)
        insights.extend(failure_insights)
        
        self.insights_database.extend(insights)
        self.logger.info(f"Generated {len(insights)} learning insights from {len(feedback_history)} feedback records")
        
        return insights
    
    async def _analyze_performance_trends(self, feedback_history: List[ExperimentalFeedback]) -> List[LearningInsight]:
        """Analyze performance trends over time"""
        
        insights = []
        
        # Get convergence scores over time
        convergence_scores = []
        for feedback in feedback_history:
            if feedback.feedback_type == FeedbackType.SIMULATION_PERFORMANCE:
                score = feedback.metrics.get("convergence_score", 0)
                convergence_scores.append(score)
        
        if len(convergence_scores) >= 5:
            # Simple trend analysis
            recent_avg = np.mean(convergence_scores[-5:])
            earlier_avg = np.mean(convergence_scores[:-5]) if len(convergence_scores) > 5 else np.mean(convergence_scores[:3])
            
            if recent_avg > earlier_avg + 0.05:  # Significant improvement
                insights.append(LearningInsight(
                    insight_id=str(uuid.uuid4()),
                    insight_type="performance_improvement",
                    description=f"Convergence scores improving: {recent_avg:.3f} vs {earlier_avg:.3f}",
                    confidence_score=0.8,
                    recommended_actions=["Continue current optimization strategy", "Document successful approaches"],
                    impact_assessment={"performance_trend": "positive", "improvement": recent_avg - earlier_avg}
                ))
            
            elif recent_avg < earlier_avg - 0.05:  # Significant decline
                insights.append(LearningInsight(
                    insight_id=str(uuid.uuid4()),
                    insight_type="performance_decline",
                    description=f"Convergence scores declining: {recent_avg:.3f} vs {earlier_avg:.3f}",
                    confidence_score=0.85,
                    recommended_actions=["Investigate recent changes", "Review parameter settings"],
                    impact_assessment={"performance_trend": "negative", "decline": earlier_avg - recent_avg}
                ))
        
        return insights
    
    async def _analyze_resource_patterns(self, feedback_history: List[ExperimentalFeedback]) -> List[LearningInsight]:
        """Analyze resource optimization patterns"""
        
        insights = []
        
        # Resource efficiency analysis
        efficiency_scores = []
        for feedback in feedback_history:
            if feedback.feedback_type == FeedbackType.RESOURCE_EFFICIENCY:
                eff = feedback.metrics.get("resource_efficiency", 0)
                efficiency_scores.append(eff)
        
        if len(efficiency_scores) >= 10:
            high_efficiency_count = sum(1 for score in efficiency_scores if score > 0.8)
            low_efficiency_count = sum(1 for score in efficiency_scores if score < 0.6)
            
            high_rate = high_efficiency_count / len(efficiency_scores)
            low_rate = low_efficiency_count / len(efficiency_scores)
            
            if high_rate > 0.7:
                insights.append(LearningInsight(
                    insight_id=str(uuid.uuid4()),
                    insight_type="resource_optimization_success",
                    description=f"High resource efficiency rate: {high_rate:.1%}",
                    confidence_score=0.9,
                    recommended_actions=["Maintain current resource allocation strategy"],
                    impact_assessment={"efficiency_rate": high_rate}
                ))
            
            if low_rate > 0.3:
                insights.append(LearningInsight(
                    insight_id=str(uuid.uuid4()),
                    insight_type="resource_inefficiency_pattern",
                    description=f"Resource inefficiency detected in {low_rate:.1%} of experiments",
                    confidence_score=0.85,
                    recommended_actions=["Review resource allocation models", "Investigate inefficiency causes"],
                    impact_assessment={"inefficiency_rate": low_rate}
                ))
        
        return insights
    
    async def _analyze_failure_patterns(self, feedback_history: List[ExperimentalFeedback]) -> List[LearningInsight]:
        """Analyze failure and critical issue patterns"""
        
        insights = []
        
        # Count critical/poor feedback by component
        component_failures = {}
        for feedback in feedback_history:
            if feedback.severity in [FeedbackSeverity.CRITICAL, FeedbackSeverity.POOR]:
                component = feedback.source_component
                component_failures[component] = component_failures.get(component, 0) + 1
        
        for component, failure_count in component_failures.items():
            if failure_count >= 3:  # Significant failure pattern
                insights.append(LearningInsight(
                    insight_id=str(uuid.uuid4()),
                    insight_type="systematic_failure_pattern",
                    description=f"Recurring issues in {component}: {failure_count} critical/poor instances",
                    confidence_score=0.9,
                    recommended_actions=[
                        f"Investigate {component} implementation",
                        "Review validation procedures"
                    ],
                    impact_assessment={"failure_count": failure_count, "affected_component": component}
                ))
        
        return insights
    
    async def generate_optimization_recommendations(self, 
                                                 insights: List[LearningInsight],
                                                 current_config: Dict[str, Any]) -> List[OptimizationRecommendation]:
        """Generate optimization recommendations based on insights"""
        
        recommendations = []
        
        for insight in insights:
            if insight.insight_type == "performance_decline":
                recommendations.append(OptimizationRecommendation(
                    recommendation_id=str(uuid.uuid4()),
                    target_component="enhanced_sampling",
                    optimization_type="parameter_tuning",
                    recommended_parameters={
                        "simulation_time_multiplier": 1.2,
                        "convergence_threshold": 0.95,
                        "bootstrap_samples": 1500
                    },
                    expected_improvement={"convergence_score": 0.1},
                    confidence_level=insight.confidence_score,
                    implementation_priority="high"
                ))
            
            elif insight.insight_type == "resource_inefficiency_pattern":
                recommendations.append(OptimizationRecommendation(
                    recommendation_id=str(uuid.uuid4()),
                    target_component="resource_optimizer",
                    optimization_type="model_retraining",
                    recommended_parameters={
                        "training_data_expansion": True,
                        "validation_threshold": 0.9
                    },
                    expected_improvement={"resource_efficiency": 0.15},
                    confidence_level=insight.confidence_score,
                    implementation_priority="medium"
                ))
        
        return recommendations

class AutomatedFeedbackSystem:
    """
    🔄 Comprehensive Automated Feedback System (AFT)
    
    Advanced feedback collection and learning system providing:
    - Automated collection from enhanced sampling simulations
    - Performance analysis and trend detection
    - ML-powered pattern recognition and learning
    - Scientific protocol optimization recommendations
    - Integration with resource optimizer and ethical framework
    - Continuous improvement through adaptive learning
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or self._default_config()
        
        # Initialize components
        self.feedback_collector = FeedbackCollector()
        self.learning_engine = LearningEngine()
        
        # Storage
        self.feedback_history: List[ExperimentalFeedback] = []
        self.insights_history: List[LearningInsight] = []
        self.recommendations_history: List[OptimizationRecommendation] = []
        
        # State
        self.last_analysis_time = datetime.now()
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("🔄 Automated Feedback System (AFT) initialized")
    
    def _default_config(self) -> Dict[str, Any]:
        """Default AFT configuration"""
        return {
            "feedback_collection_enabled": True,
            "learning_enabled": True,
            "auto_analysis_interval_hours": 24,
            "min_feedback_for_analysis": 10
        }
    
    async def collect_experimental_feedback(self, 
                                          experiment_id: str,
                                          experiment_data: Dict[str, Any]) -> str:
        """
        Collect feedback from experimental results.
        
        Returns:
            Feedback ID for tracking
        """
        
        if not self.config["feedback_collection_enabled"]:
            return "feedback_disabled"
        
        feedback = None
        
        # Determine feedback type and collect
        if "simulation_results" in experiment_data:
            feedback = await self.feedback_collector.collect_simulation_feedback(
                experiment_id,
                experiment_data["simulation_results"],
                experiment_data.get("resource_usage", {}),
                experiment_data.get("convergence_data", {})
            )
        
        elif "optimization_results" in experiment_data:
            feedback = await self.feedback_collector.collect_resource_optimization_feedback(
                experiment_id,
                experiment_data.get("predicted_resources", {}),
                experiment_data.get("actual_resources", {}),
                experiment_data["optimization_results"]
            )
        
        if feedback:
            self.feedback_history.append(feedback)
            
            # Trigger analysis if conditions met
            if (len(self.feedback_history) >= self.config["min_feedback_for_analysis"] and
                datetime.now() - self.last_analysis_time > timedelta(hours=self.config["auto_analysis_interval_hours"])):
                await self._trigger_automated_analysis()
            
            return feedback.feedback_id
        
        return "no_feedback_collected"
    
    async def _trigger_automated_analysis(self) -> None:
        """Trigger automated analysis and learning"""
        
        if not self.config["learning_enabled"]:
            return
        
        self.logger.info("Triggering automated analysis and learning...")
        
        # Generate insights
        insights = await self.learning_engine.analyze_feedback_patterns(self.feedback_history)
        self.insights_history.extend(insights)
        
        # Generate recommendations
        current_config = {
            "sampling_parameters": {"simulation_time": 10.0, "convergence_threshold": 0.9},
            "resource_parameters": {"confidence_threshold": 0.8}
        }
        recommendations = await self.learning_engine.generate_optimization_recommendations(
            insights, current_config
        )
        self.recommendations_history.extend(recommendations)
        
        self.last_analysis_time = datetime.now()
        self.logger.info(f"Analysis complete: {len(insights)} insights, {len(recommendations)} recommendations")
    
    async def generate_aft_report(self, 
                                analysis_start: datetime = None,
                                analysis_end: datetime = None) -> AFTReport:
        """Generate comprehensive AFT analysis report"""
        
        # Define analysis period
        if not analysis_start:
            analysis_start = datetime.now() - timedelta(days=30)
        if not analysis_end:
            analysis_end = datetime.now()
        
        # Filter feedback by period
        period_feedback = [
            f for f in self.feedback_history 
            if analysis_start <= f.timestamp <= analysis_end
        ]
        
        # Analyze feedback distribution
        feedback_by_type = {}
        for feedback_type in FeedbackType:
            count = len([f for f in period_feedback if f.feedback_type == feedback_type])
            feedback_by_type[feedback_type] = count
        
        # Get recent insights and recommendations
        recent_insights = [
            i for i in self.insights_history
            if analysis_start <= i.generated_at <= analysis_end
        ]
        
        recent_recommendations = self.recommendations_history[-10:]  # Last 10
        
        # Calculate learning effectiveness
        learning_effectiveness = self._calculate_learning_effectiveness(period_feedback, recent_insights)
        
        report = AFTReport(
            report_id=str(uuid.uuid4()),
            analysis_period=(analysis_start, analysis_end),
            total_feedback_collected=len(period_feedback),
            feedback_by_type=feedback_by_type,
            key_insights=recent_insights,
            optimization_recommendations=recent_recommendations,
            learning_effectiveness=learning_effectiveness
        )
        
        self.logger.info(f"AFT report generated: {len(period_feedback)} feedback, {len(recent_insights)} insights")
        return report
    
    def _calculate_learning_effectiveness(self, 
                                        feedback: List[ExperimentalFeedback],
                                        insights: List[LearningInsight]) -> float:
        """Calculate learning effectiveness score"""
        
        if not feedback:
            return 0.0
        
        # Calculate improvement rate
        excellent_feedback = len([f for f in feedback if f.severity == FeedbackSeverity.EXCELLENT])
        improvement_rate = excellent_feedback / len(feedback)
        
        # Insight generation rate
        insight_rate = len(insights) / max(1, len(feedback) // 10)  # Insights per 10 feedback
        
        # Combine metrics
        effectiveness = (improvement_rate * 0.7 + min(1.0, insight_rate) * 0.3)
        
        return effectiveness

# Factory function
def create_automated_feedback_system(config: Dict[str, Any] = None) -> AutomatedFeedbackSystem:
    """Create automated feedback system instance"""
    return AutomatedFeedbackSystem(config)

if __name__ == "__main__":
    async def main():
        # Demo usage
        aft_system = create_automated_feedback_system()
        
        # Simulate experimental feedback collection
        experiment_data = {
            "simulation_results": {"scientific_quality": 0.9},
            "resource_usage": {
                "allocated_resources": {"cpu": 8, "memory": 16, "gpu": 1},
                "actual_usage": {"cpu": 6, "memory": 12, "gpu": 0.8},
                "predicted_time": 120,
                "actual_time": 115
            },
            "convergence_data": {
                "convergence_score": 0.92,
                "statistical_significance": 0.95
            }
        }
        
        # Collect feedback
        feedback_id = await aft_system.collect_experimental_feedback("test_experiment_001", experiment_data)
        
        # Simulate resource optimization feedback
        optimization_data = {
            "predicted_resources": {"cpu_cores": 8, "memory_gb": 16, "duration_hours": 2.0, "confidence_score": 0.85},
            "actual_resources": {"cpu_cores": 7, "memory_gb": 14, "duration_hours": 1.8},
            "optimization_results": {"cost_savings": 0.15, "performance_improvement": 0.1}
        }
        
        opt_feedback_id = await aft_system.collect_experimental_feedback("test_optimization_001", optimization_data)
        
        # Generate AFT report
        report = await aft_system.generate_aft_report()
        
        print("🔄 Automated Feedback System (AFT) Demo Results:")
        print(f"• Simulation Feedback ID: {feedback_id}")
        print(f"• Optimization Feedback ID: {opt_feedback_id}")
        print(f"• Total Feedback Collected: {report.total_feedback_collected}")
        print(f"• Feedback by Type: {[(ft.value, count) for ft, count in report.feedback_by_type.items() if count > 0]}")
        print(f"• Key Insights Generated: {len(report.key_insights)}")
        print(f"• Optimization Recommendations: {len(report.optimization_recommendations)}")
        print(f"• Learning Effectiveness: {report.learning_effectiveness:.2f}")
        
        if report.key_insights:
            print(f"\\n📊 Sample Insight:")
            insight = report.key_insights[0]
            print(f"  • Type: {insight.insight_type}")
            print(f"  • Description: {insight.description}")
            print(f"  • Confidence: {insight.confidence_score:.2f}")
        
        print("\\n✅ Team 3 Automated Feedback System: COMPLETE")
    
    asyncio.run(main())
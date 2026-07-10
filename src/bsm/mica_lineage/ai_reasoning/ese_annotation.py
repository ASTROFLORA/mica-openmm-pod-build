"""
ESE Annotation Sentient System - Dr. Priya Sharma  
=================================================

Intelligent annotation system for Evolutionary Situational Encodings (ESE).
Provides sentient analysis capabilities for protein evolution and adaptation.

Phase 3 Implementation: ESE Pipeline (6 weeks)  
Lead: Yuan Chen + Sofia Petrov + Dr. Priya Sharma
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import DBSCAN
import torch
import torch.nn as nn
from transformers import AutoTokenizer, AutoModel

from bsm.config import get_bsm_config
from .chronoracle_client import ChronoracleClient, ChronoracleQuery

logger = logging.getLogger(__name__)


@dataclass  
class ESESituation:
    """Represents an evolutionary situation for ESE analysis"""
    situation_id: str
    context_type: str  # 'environmental', 'temporal', 'functional', 'structural'
    protein_sequence: str
    environmental_factors: Dict[str, float]
    temporal_context: Dict[str, Any]
    stress_indicators: List[str]
    adaptation_signals: List[Dict]
    confidence_score: float = 0.0


@dataclass
class ESEAnnotation:
    """Complete ESE annotation with sentient analysis"""
    annotation_id: str
    situation: ESESituation
    ese_encoding: np.ndarray  # 416D ESE vector
    sentient_analysis: Dict[str, Any]
    evolutionary_pressure: float
    adaptation_likelihood: float
    conservation_score: float
    novelty_index: float
    annotation_timestamp: datetime
    annotator: str = "ESE_Sentient_System_v1.0"


class ESESentientAnnotator:
    """
    Sentient annotation system for Evolutionary Situational Encodings.
    
    Capabilities:
    - Intelligent situation recognition from protein contexts
    - Multi-modal ESE encoding generation (416D vectors)
    - Sentient analysis of evolutionary pressures
    - Adaptive learning from annotation patterns
    - Integration with Chronoracle reasoning system
    """
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or get_bsm_config()
        
        # Initialize models
        self.situation_classifier = None
        self.ese_encoder = None  
        self.adaptation_predictor = None
        self.chronoracle_client = None
        
        # Annotation storage
        self.annotations_cache = []
        self.situation_patterns = {}
        self.learning_history = []
        
        # Initialize system
        self._initialize_models()
        self._load_situation_patterns()
        
        logger.info("ESE Sentient Annotator initialized - Dr. Priya Sharma implementation")
    
    def _initialize_models(self):
        """Initialize all ESE annotation models"""
        
        try:
            # Situation classifier - recognizes evolutionary contexts
            self.situation_classifier = self._build_situation_classifier()
            
            # ESE encoder - generates 416D situational encodings
            self.ese_encoder = ESEEncoder(
                input_dim=1024,  # Input from protein embeddings
                output_dim=416,  # ESE encoding dimension
                hidden_dims=[768, 512, 416]
            )
            
            # Adaptation predictor - predicts evolutionary outcomes
            self.adaptation_predictor = AdaptationPredictor(
                input_dim=416,  # ESE encoding
                prediction_classes=['stable', 'adapting', 'diverging', 'novel']
            )
            
            # Chronoracle integration
            self.chronoracle_client = ChronoracleClient(self.config)
            
            logger.info("All ESE models initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize ESE models: {e}")
            self._initialize_fallback_models()
    
    def _build_situation_classifier(self):
        """Build random forest classifier for situation recognition"""
        
        # Pre-trained on evolutionary situation patterns
        classifier = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            random_state=42,
            class_weight='balanced'
        )
        
        # Mock training data - in practice would load from curated dataset
        mock_features = np.random.random((1000, 50))  # 50 situational features
        mock_labels = np.random.choice(['environmental', 'temporal', 'functional', 'structural'], 1000)
        
        classifier.fit(mock_features, mock_labels)
        
        return classifier
    
    def _initialize_fallback_models(self):
        """Initialize fallback models if main models fail"""
        logger.warning("Using fallback models for ESE annotation")
        
        # Simple rule-based fallbacks
        self.situation_classifier = None
        self.ese_encoder = None
        self.adaptation_predictor = None
    
    def _load_situation_patterns(self):
        """Load known evolutionary situation patterns"""
        
        # Archetypal evolutionary situations
        self.situation_patterns = {
            'heat_shock_response': {
                'temperature_range': [35, 65],  # Celsius
                'stress_proteins': ['HSP60', 'HSP70', 'HSP90'],
                'adaptation_indicators': ['chaperone_upregulation', 'protein_folding_enhancement'],
                'typical_duration': '2-24_hours'
            },
            
            'oxidative_stress': {
                'oxygen_levels': [0.1, 0.3],  # Atmospheric fraction
                'stress_markers': ['ROS', 'lipid_peroxidation', 'DNA_damage'],
                'adaptation_indicators': ['antioxidant_enzyme_induction', 'membrane_stabilization'],
                'typical_duration': '30_minutes_to_days'
            },
            
            'nutrient_limitation': {
                'nutrient_availability': [0.01, 0.1],  # Fraction of optimal
                'stress_indicators': ['growth_arrest', 'autophagy_induction'],
                'adaptation_indicators': ['metabolic_rewiring', 'storage_compound_mobilization'],
                'typical_duration': 'hours_to_weeks'
            },
            
            'pathogen_exposure': {
                'pathogen_load': [1e3, 1e8],  # CFU/ml equivalent
                'immune_markers': ['cytokine_release', 'complement_activation'],
                'adaptation_indicators': ['immune_memory', 'resistance_evolution'],
                'typical_duration': 'days_to_months'
            },
            
            'evolutionary_arms_race': {
                'competitive_pressure': [0.7, 1.0],  # Normalized intensity
                'interaction_type': ['predator-prey', 'host-parasite', 'resource_competition'],
                'adaptation_indicators': ['rapid_evolution', 'coevolution_signals'],
                'typical_duration': 'generations_to_millions_of_years'
            }
        }
        
        logger.info(f"Loaded {len(self.situation_patterns)} situation patterns")
    
    async def annotate_protein_situation(
        self, 
        protein_sequence: str,
        context_data: Dict[str, Any],
        situation_hint: Optional[str] = None
    ) -> ESEAnnotation:
        """
        Main annotation interface for protein evolutionary situations.
        
        Args:
            protein_sequence: Amino acid sequence
            context_data: Environmental/temporal context information
            situation_hint: Optional hint about situation type
        
        Returns:
            Complete ESE annotation with sentient analysis
        """
        
        logger.info(f"Starting ESE annotation for {len(protein_sequence)}aa protein")
        
        # Stage 1: Situation recognition
        situation = await self._recognize_situation(
            protein_sequence, context_data, situation_hint
        )
        
        # Stage 2: ESE encoding generation
        ese_encoding = await self._generate_ese_encoding(situation)
        
        # Stage 3: Sentient analysis
        sentient_analysis = await self._perform_sentient_analysis(
            situation, ese_encoding
        )
        
        # Stage 4: Evolutionary assessment
        evolutionary_metrics = await self._assess_evolutionary_metrics(
            situation, ese_encoding, sentient_analysis
        )
        
        # Stage 5: Chronoracle integration
        chronoracle_insights = await self._integrate_chronoracle_reasoning(
            situation, evolutionary_metrics
        )
        
        # Construct final annotation
        annotation = ESEAnnotation(
            annotation_id=f"ESE_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            situation=situation,
            ese_encoding=ese_encoding,
            sentient_analysis={
                **sentient_analysis,
                'chronoracle_insights': chronoracle_insights
            },
            evolutionary_pressure=evolutionary_metrics['pressure'],
            adaptation_likelihood=evolutionary_metrics['adaptation_likelihood'],
            conservation_score=evolutionary_metrics['conservation'],
            novelty_index=evolutionary_metrics['novelty'],
            annotation_timestamp=datetime.now()
        )
        
        # Cache annotation and update learning
        self.annotations_cache.append(annotation)
        await self._update_learning_patterns(annotation)
        
        logger.info(f"ESE annotation completed with {annotation.confidence_score:.3f} confidence")
        return annotation
    
    async def _recognize_situation(
        self, 
        sequence: str, 
        context_data: Dict[str, Any],
        hint: Optional[str] = None
    ) -> ESESituation:
        """Recognize the evolutionary situation from protein and context"""
        
        # Extract situational features
        situational_features = self._extract_situational_features(sequence, context_data)
        
        # Classify situation type
        if self.situation_classifier and hint is None:
            situation_type = self._classify_situation_ml(situational_features)
        else:
            situation_type = hint or self._classify_situation_rules(context_data)
        
        # Extract environmental factors
        environmental_factors = self._extract_environmental_factors(context_data)
        
        # Detect stress indicators
        stress_indicators = self._detect_stress_indicators(sequence, context_data)
        
        # Identify adaptation signals
        adaptation_signals = self._identify_adaptation_signals(sequence, situational_features)
        
        # Build temporal context
        temporal_context = self._build_temporal_context(context_data)
        
        situation = ESESituation(
            situation_id=f"SIT_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            context_type=situation_type,
            protein_sequence=sequence,
            environmental_factors=environmental_factors,
            temporal_context=temporal_context,
            stress_indicators=stress_indicators,
            adaptation_signals=adaptation_signals,
            confidence_score=self._calculate_situation_confidence(situational_features)
        )
        
        return situation
    
    def _extract_situational_features(self, sequence: str, context_data: Dict) -> np.ndarray:
        """Extract numerical features representing the evolutionary situation"""
        
        features = []
        
        # Sequence-based features
        features.extend([
            len(sequence),
            sequence.count('C') / len(sequence),  # Cysteine content (disulfide bonds)
            sequence.count('P') / len(sequence),  # Proline content (structure rigidity)
            sequence.count('G') / len(sequence),  # Glycine content (flexibility)
            self._calculate_hydrophobicity(sequence),
            self._calculate_charge_distribution(sequence)
        ])
        
        # Context-based features
        temperature = context_data.get('temperature', 25.0)
        ph = context_data.get('ph', 7.0)
        salinity = context_data.get('salinity', 0.0)
        oxygen = context_data.get('oxygen_level', 0.21)
        
        features.extend([
            (temperature - 25) / 50,  # Normalized temperature deviation
            (ph - 7) / 7,  # Normalized pH deviation  
            salinity / 100,  # Normalized salinity
            oxygen / 0.21,  # Normalized oxygen level
        ])
        
        # Temporal features
        time_scale = context_data.get('time_scale', 'minutes')
        time_mapping = {'seconds': 0, 'minutes': 1, 'hours': 2, 'days': 3, 
                       'weeks': 4, 'months': 5, 'years': 6, 'evolutionary': 7}
        features.append(time_mapping.get(time_scale, 2))
        
        # Stress features
        stress_level = context_data.get('stress_level', 0.0)
        pathogen_load = context_data.get('pathogen_load', 0.0)
        competitive_pressure = context_data.get('competitive_pressure', 0.0)
        
        features.extend([stress_level, pathogen_load, competitive_pressure])
        
        # Pad to standard feature size (50 features)
        while len(features) < 50:
            features.append(0.0)
        
        return np.array(features[:50])  # Trim if too long
    
    def _calculate_hydrophobicity(self, sequence: str) -> float:
        """Calculate sequence hydrophobicity using Kyte-Doolittle scale"""
        kd_scale = {
            'A': 1.8, 'R': -4.5, 'N': -3.5, 'D': -3.5, 'C': 2.5,
            'Q': -3.5, 'E': -3.5, 'G': -0.4, 'H': -3.2, 'I': 4.5,
            'L': 3.8, 'K': -3.9, 'M': 1.9, 'F': 2.8, 'P': -1.6,
            'S': -0.8, 'T': -0.7, 'W': -0.9, 'Y': -1.3, 'V': 4.2
        }
        return sum(kd_scale.get(aa, 0) for aa in sequence) / len(sequence)
    
    def _calculate_charge_distribution(self, sequence: str) -> float:
        """Calculate charge distribution across sequence"""
        charges = []
        window_size = min(20, len(sequence) // 5)
        
        for i in range(0, len(sequence) - window_size + 1, window_size):
            window = sequence[i:i + window_size]
            positive = window.count('K') + window.count('R') + window.count('H')
            negative = window.count('D') + window.count('E')
            charges.append(positive - negative)
        
        return np.std(charges) if charges else 0.0
    
    def _classify_situation_ml(self, features: np.ndarray) -> str:
        """Classify situation using machine learning"""
        try:
            prediction = self.situation_classifier.predict(features.reshape(1, -1))[0]
            return prediction
        except Exception as e:
            logger.warning(f"ML classification failed: {e}, using fallback")
            return self._classify_situation_rules({})
    
    def _classify_situation_rules(self, context_data: Dict) -> str:
        """Fallback rule-based situation classification"""
        
        temperature = context_data.get('temperature', 25.0)
        stress_level = context_data.get('stress_level', 0.0)
        pathogen_load = context_data.get('pathogen_load', 0.0)
        
        if temperature > 40 or temperature < 10:
            return 'environmental'
        elif pathogen_load > 0.1:
            return 'functional'  # Immune response
        elif stress_level > 0.5:
            return 'temporal'    # Stress response
        else:
            return 'structural'  # Default
    
    def _extract_environmental_factors(self, context_data: Dict) -> Dict[str, float]:
        """Extract quantified environmental factors"""
        
        factors = {
            'temperature': context_data.get('temperature', 25.0),
            'ph': context_data.get('ph', 7.0),
            'salinity': context_data.get('salinity', 0.0),
            'oxygen_level': context_data.get('oxygen_level', 0.21),
            'pressure': context_data.get('pressure', 1.0),  # Atmospheric
            'radiation': context_data.get('radiation', 0.0),
            'toxin_concentration': context_data.get('toxins', 0.0),
            'nutrient_availability': context_data.get('nutrients', 1.0)
        }
        
        return factors
    
    def _detect_stress_indicators(self, sequence: str, context_data: Dict) -> List[str]:
        """Detect molecular stress indicators"""
        
        indicators = []
        
        # Sequence-based stress indicators
        if sequence.count('C') / len(sequence) > 0.05:
            indicators.append('high_cysteine_content')
        
        if sequence.count('P') / len(sequence) > 0.08:
            indicators.append('high_proline_content')
        
        # Heat shock proteins indicators
        hsp_motifs = ['DKA', 'EEVD', 'GGM', 'VGG']
        for motif in hsp_motifs:
            if motif in sequence:
                indicators.append(f'heat_shock_motif_{motif}')
        
        # Context-based stress indicators
        if context_data.get('temperature', 25) > 35:
            indicators.append('thermal_stress')
        
        if context_data.get('ph', 7) < 5 or context_data.get('ph', 7) > 9:
            indicators.append('ph_stress')
        
        if context_data.get('oxygen_level', 0.21) < 0.1:
            indicators.append('hypoxic_stress')
        
        return indicators
    
    def _identify_adaptation_signals(self, sequence: str, features: np.ndarray) -> List[Dict]:
        """Identify molecular adaptation signals"""
        
        signals = []
        
        # Protein stability adaptations
        if features[1] > 0.05:  # High cysteine content
            signals.append({
                'type': 'stability_adaptation',
                'mechanism': 'disulfide_bond_formation',
                'strength': features[1] * 20,  # Scale to 0-1
                'evidence': 'elevated_cysteine_content'
            })
        
        # Thermal adaptations
        if features[2] > 0.08:  # High proline content
            signals.append({
                'type': 'thermal_adaptation',
                'mechanism': 'structural_rigidity',
                'strength': features[2] * 12.5,
                'evidence': 'elevated_proline_content'
            })
        
        # Charge adaptations
        if features[5] > 1.0:  # High charge variation
            signals.append({
                'type': 'electrostatic_adaptation',
                'mechanism': 'surface_charge_optimization',
                'strength': min(features[5] / 3.0, 1.0),
                'evidence': 'charge_distribution_pattern'
            })
        
        return signals
    
    def _build_temporal_context(self, context_data: Dict) -> Dict[str, Any]:
        """Build temporal context for the evolutionary situation"""
        
        temporal_context = {
            'time_scale': context_data.get('time_scale', 'minutes'),
            'duration': context_data.get('duration', '1_hour'),
            'frequency': context_data.get('frequency', 'single_exposure'),
            'periodicity': context_data.get('periodicity', None),
            'evolutionary_time': context_data.get('evolutionary_time', 'recent'),
            'generation_time': context_data.get('generation_time', '20_minutes'),
            'selection_intensity': context_data.get('selection_intensity', 'moderate')
        }
        
        return temporal_context
    
    def _calculate_situation_confidence(self, features: np.ndarray) -> float:
        """Calculate confidence in situation recognition"""
        
        # Features completeness
        non_zero_features = np.sum(features != 0) / len(features)
        
        # Feature variance (more variance = more information)
        feature_variance = np.var(features)
        
        # Combined confidence score
        confidence = (non_zero_features * 0.7) + (min(feature_variance, 1.0) * 0.3)
        
        return min(max(confidence, 0.1), 0.95)  # Clamp to reasonable range
    
    async def _generate_ese_encoding(self, situation: ESESituation) -> np.ndarray:
        """Generate 416D ESE encoding for the evolutionary situation"""
        
        if self.ese_encoder:
            # Use neural network encoder
            encoding = await self._neural_ese_encoding(situation)
        else:
            # Use feature-based encoding
            encoding = self._feature_based_ese_encoding(situation)
        
        # Ensure exactly 416 dimensions
        if len(encoding) != 416:
            encoding = self._normalize_to_416d(encoding)
        
        return encoding
    
    async def _neural_ese_encoding(self, situation: ESESituation) -> np.ndarray:
        """Generate ESE encoding using neural network"""
        
        # Combine all situational information into input vector
        input_features = np.concatenate([
            self._sequence_to_features(situation.protein_sequence),
            self._environmental_to_features(situation.environmental_factors),
            self._temporal_to_features(situation.temporal_context),
            self._stress_to_features(situation.stress_indicators),
            self._adaptation_to_features(situation.adaptation_signals)
        ])
        
        # Pad or truncate to expected input size (1024D)
        input_features = self._normalize_to_1024d(input_features)
        
        # Pass through ESE encoder
        with torch.no_grad():
            input_tensor = torch.FloatTensor(input_features).unsqueeze(0)
            encoding = self.ese_encoder(input_tensor).squeeze(0).numpy()
        
        return encoding
    
    def _feature_based_ese_encoding(self, situation: ESESituation) -> np.ndarray:
        """Generate ESE encoding using feature engineering approach"""
        
        encoding = np.zeros(416)
        
        # Sequence features (0-99)
        seq_features = self._sequence_to_features(situation.protein_sequence)[:100]
        encoding[:len(seq_features)] = seq_features
        
        # Environmental features (100-199)
        env_values = list(situation.environmental_factors.values())[:100]
        encoding[100:100+len(env_values)] = env_values
        
        # Temporal features (200-249)
        temporal_features = self._temporal_to_features(situation.temporal_context)[:50]
        encoding[200:200+len(temporal_features)] = temporal_features
        
        # Stress features (250-299)
        stress_features = self._stress_to_features(situation.stress_indicators)[:50]
        encoding[250:250+len(stress_features)] = stress_features
        
        # Adaptation features (300-349)
        adaptation_features = self._adaptation_to_features(situation.adaptation_signals)[:50]
        encoding[300:300+len(adaptation_features)] = adaptation_features
        
        # Meta features (350-415)
        meta_features = [
            situation.confidence_score,
            len(situation.protein_sequence) / 1000.0,  # Normalized length
            len(situation.stress_indicators) / 10.0,   # Normalized stress count
            len(situation.adaptation_signals) / 10.0,  # Normalized adaptation count
        ]
        encoding[350:350+len(meta_features)] = meta_features
        
        return encoding
    
    def _sequence_to_features(self, sequence: str) -> np.ndarray:
        """Convert protein sequence to numerical features"""
        
        features = []
        
        # Amino acid composition (20 features)
        for aa in 'ACDEFGHIKLMNPQRSTVWY':
            features.append(sequence.count(aa) / len(sequence))
        
        # Physicochemical properties
        features.extend([
            self._calculate_hydrophobicity(sequence),
            self._calculate_charge_distribution(sequence),
            len(sequence) / 1000.0,  # Normalized length
            sequence.count('C') / len(sequence),  # Cysteine fraction
            sequence.count('P') / len(sequence),  # Proline fraction
        ])
        
        # Secondary structure prediction (simplified)
        helix_prop = sum(1 for aa in sequence if aa in 'AEHK') / len(sequence)
        sheet_prop = sum(1 for aa in sequence if aa in 'VILF') / len(sequence)
        features.extend([helix_prop, sheet_prop, 1 - helix_prop - sheet_prop])
        
        # Repeat until we have 100 features
        while len(features) < 100:
            features.extend(features[:min(20, 100 - len(features))])
        
        return np.array(features[:100])
    
    def _environmental_to_features(self, env_factors: Dict[str, float]) -> np.ndarray:
        """Convert environmental factors to numerical features"""
        
        features = []
        
        # Direct environmental values
        features.extend(list(env_factors.values()))
        
        # Derived environmental features
        temp = env_factors.get('temperature', 25.0)
        features.extend([
            (temp - 25) ** 2,  # Temperature deviation squared
            1.0 if temp > 40 else 0.0,  # High temperature indicator
            1.0 if temp < 10 else 0.0,  # Low temperature indicator
        ])
        
        ph = env_factors.get('ph', 7.0)
        features.extend([
            abs(ph - 7),  # pH deviation from neutral
            1.0 if ph < 5 else 0.0,  # Acidic indicator
            1.0 if ph > 9 else 0.0,  # Basic indicator
        ])
        
        # Environmental stress composite score
        stress_score = 0
        if temp < 10 or temp > 40: stress_score += 1
        if ph < 5 or ph > 9: stress_score += 1
        if env_factors.get('salinity', 0) > 5: stress_score += 1
        if env_factors.get('oxygen_level', 0.21) < 0.1: stress_score += 1
        features.append(stress_score / 4.0)
        
        # Pad to 100 features
        while len(features) < 100:
            features.append(0.0)
        
        return np.array(features[:100])
    
    def _temporal_to_features(self, temporal_context: Dict) -> np.ndarray:
        """Convert temporal context to numerical features"""
        
        features = []
        
        # Time scale encoding
        time_scales = ['seconds', 'minutes', 'hours', 'days', 'weeks', 'months', 'years', 'evolutionary']
        time_scale = temporal_context.get('time_scale', 'minutes')
        time_encoding = [1.0 if ts == time_scale else 0.0 for ts in time_scales]
        features.extend(time_encoding)
        
        # Selection intensity
        selection_mapping = {'weak': 0.2, 'moderate': 0.5, 'strong': 0.8, 'extreme': 1.0}
        selection_intensity = temporal_context.get('selection_intensity', 'moderate')
        features.append(selection_mapping.get(selection_intensity, 0.5))
        
        # Frequency encoding
        freq_mapping = {'single_exposure': 1, 'occasional': 2, 'frequent': 3, 'continuous': 4}
        frequency = temporal_context.get('frequency', 'single_exposure')
        features.append(freq_mapping.get(frequency, 1) / 4.0)
        
        # Pad to 50 features
        while len(features) < 50:
            features.append(0.0)
        
        return np.array(features[:50])
    
    def _stress_to_features(self, stress_indicators: List[str]) -> np.ndarray:
        """Convert stress indicators to numerical features"""
        
        features = []
        
        # Stress type indicators
        stress_types = [
            'thermal_stress', 'ph_stress', 'osmotic_stress', 'oxidative_stress',
            'hypoxic_stress', 'nutrient_stress', 'pathogen_stress', 'mechanical_stress'
        ]
        
        for stress_type in stress_types:
            has_stress = any(stress_type in indicator for indicator in stress_indicators)
            features.append(1.0 if has_stress else 0.0)
        
        # Stress intensity (total number of indicators)
        features.append(len(stress_indicators) / 10.0)  # Normalized
        
        # Stress diversity (number of different stress types)
        unique_stresses = len(set(ind.split('_')[0] for ind in stress_indicators))
        features.append(unique_stresses / len(stress_types))
        
        # Pad to 50 features
        while len(features) < 50:
            features.append(0.0)
        
        return np.array(features[:50])
    
    def _adaptation_to_features(self, adaptation_signals: List[Dict]) -> np.ndarray:
        """Convert adaptation signals to numerical features"""
        
        features = []
        
        # Adaptation type indicators
        adaptation_types = [
            'stability_adaptation', 'thermal_adaptation', 'electrostatic_adaptation',
            'metabolic_adaptation', 'structural_adaptation', 'functional_adaptation'
        ]
        
        for adapt_type in adaptation_types:
            has_adaptation = any(signal['type'] == adapt_type for signal in adaptation_signals)
            features.append(1.0 if has_adaptation else 0.0)
        
        # Adaptation strengths
        if adaptation_signals:
            avg_strength = np.mean([signal.get('strength', 0.5) for signal in adaptation_signals])
            max_strength = max(signal.get('strength', 0.0) for signal in adaptation_signals)
            features.extend([avg_strength, max_strength])
        else:
            features.extend([0.0, 0.0])
        
        # Pad to 50 features
        while len(features) < 50:
            features.append(0.0)
        
        return np.array(features[:50])
    
    def _normalize_to_416d(self, encoding: np.ndarray) -> np.ndarray:
        """Normalize encoding to exactly 416 dimensions"""
        
        if len(encoding) == 416:
            return encoding
        elif len(encoding) < 416:
            # Pad with zeros
            padded = np.zeros(416)
            padded[:len(encoding)] = encoding
            return padded
        else:
            # Truncate or compress
            return encoding[:416]
    
    def _normalize_to_1024d(self, features: np.ndarray) -> np.ndarray:
        """Normalize features to 1024 dimensions for neural network input"""
        
        if len(features) == 1024:
            return features
        elif len(features) < 1024:
            padded = np.zeros(1024)
            padded[:len(features)] = features
            return padded
        else:
            return features[:1024]
    
    async def _perform_sentient_analysis(
        self, 
        situation: ESESituation, 
        ese_encoding: np.ndarray
    ) -> Dict[str, Any]:
        """Perform sentient analysis of the evolutionary situation"""
        
        analysis = {}
        
        # Pattern recognition
        analysis['pattern_recognition'] = self._recognize_evolutionary_patterns(situation, ese_encoding)
        
        # Adaptive potential assessment
        analysis['adaptive_potential'] = self._assess_adaptive_potential(situation, ese_encoding)
        
        # Evolutionary trajectory prediction
        analysis['trajectory_prediction'] = self._predict_evolutionary_trajectory(situation, ese_encoding)
        
        # Context integration
        analysis['context_integration'] = self._integrate_contextual_knowledge(situation)
        
        # Uncertainty quantification
        analysis['uncertainty'] = self._quantify_uncertainty(situation, ese_encoding)
        
        return analysis
    
    def _recognize_evolutionary_patterns(self, situation: ESESituation, encoding: np.ndarray) -> Dict:
        """Recognize known evolutionary patterns in the situation"""
        
        patterns = {}
        
        # Check against known situation patterns
        for pattern_name, pattern_data in self.situation_patterns.items():
            similarity = self._calculate_pattern_similarity(situation, pattern_data)
            if similarity > 0.6:
                patterns[pattern_name] = {
                    'similarity': similarity,
                    'confidence': similarity * situation.confidence_score,
                    'expected_adaptations': pattern_data.get('adaptation_indicators', [])
                }
        
        # Encoding-based pattern detection
        encoding_patterns = self._detect_encoding_patterns(encoding)
        patterns.update(encoding_patterns)
        
        return patterns
    
    def _calculate_pattern_similarity(self, situation: ESESituation, pattern_data: Dict) -> float:
        """Calculate similarity between situation and known pattern"""
        
        similarity_score = 0.0
        total_factors = 0
        
        # Temperature similarity
        if 'temperature_range' in pattern_data:
            temp_range = pattern_data['temperature_range']
            current_temp = situation.environmental_factors.get('temperature', 25)
            if temp_range[0] <= current_temp <= temp_range[1]:
                similarity_score += 0.3
            total_factors += 0.3
        
        # Stress indicator overlap
        pattern_stresses = set(pattern_data.get('stress_proteins', []) + 
                              pattern_data.get('stress_markers', []))
        situation_stresses = set(situation.stress_indicators)
        
        if pattern_stresses and situation_stresses:
            overlap = len(pattern_stresses & situation_stresses) / len(pattern_stresses | situation_stresses)
            similarity_score += overlap * 0.4
        total_factors += 0.4
        
        # Adaptation signal alignment
        pattern_adaptations = set(pattern_data.get('adaptation_indicators', []))
        situation_adaptations = set(signal['type'] for signal in situation.adaptation_signals)
        
        if pattern_adaptations and situation_adaptations:
            overlap = len(pattern_adaptations & situation_adaptations) / len(pattern_adaptations | situation_adaptations)
            similarity_score += overlap * 0.3
        total_factors += 0.3
        
        return similarity_score / total_factors if total_factors > 0 else 0.0
    
    def _detect_encoding_patterns(self, encoding: np.ndarray) -> Dict:
        """Detect patterns directly in ESE encoding"""
        
        patterns = {}
        
        # High-activation regions
        high_activation_indices = np.where(encoding > np.percentile(encoding, 95))[0]
        if len(high_activation_indices) > 5:
            patterns['high_activation_cluster'] = {
                'indices': high_activation_indices.tolist(),
                'mean_activation': float(np.mean(encoding[high_activation_indices])),
                'interpretation': 'strong_evolutionary_signal'
            }
        
        # Low-variance regions (conserved features)
        window_size = 20
        variances = []
        for i in range(0, len(encoding) - window_size, window_size):
            window_var = np.var(encoding[i:i+window_size])
            variances.append(window_var)
        
        low_var_windows = [i for i, var in enumerate(variances) if var < np.percentile(variances, 10)]
        if low_var_windows:
            patterns['conserved_regions'] = {
                'windows': low_var_windows,
                'interpretation': 'evolutionary_constraint'
            }
        
        return patterns
    
    def _assess_adaptive_potential(self, situation: ESESituation, encoding: np.ndarray) -> Dict:
        """Assess the adaptive potential of the protein in this situation"""
        
        # Sequence-based adaptability
        sequence_adaptability = self._calculate_sequence_adaptability(situation.protein_sequence)
        
        # Environmental pressure intensity
        pressure_intensity = self._calculate_pressure_intensity(situation.environmental_factors)
        
        # Existing adaptations strength
        adaptation_strength = np.mean([signal.get('strength', 0) for signal in situation.adaptation_signals]) if situation.adaptation_signals else 0
        
        # Encoding diversity (higher diversity = higher adaptive potential)
        encoding_diversity = np.std(encoding) / np.mean(np.abs(encoding)) if np.mean(np.abs(encoding)) > 0 else 0
        
        # Combined adaptive potential
        adaptive_potential = (
            sequence_adaptability * 0.3 +
            (1 - pressure_intensity) * 0.2 +  # Lower pressure = higher potential
            adaptation_strength * 0.3 +
            encoding_diversity * 0.2
        )
        
        return {
            'overall_potential': adaptive_potential,
            'sequence_adaptability': sequence_adaptability,
            'pressure_intensity': pressure_intensity,
            'current_adaptations': adaptation_strength,
            'encoding_diversity': encoding_diversity,
            'limiting_factors': self._identify_limiting_factors(situation)
        }
    
    def _calculate_sequence_adaptability(self, sequence: str) -> float:
        """Calculate intrinsic sequence adaptability"""
        
        # Factors that increase adaptability
        flexibility_residues = sum(1 for aa in sequence if aa in 'GST')  # Flexible residues
        surface_residues = sum(1 for aa in sequence if aa in 'RKDEQN')  # Likely surface residues
        
        # Factors that decrease adaptability
        rigid_residues = sum(1 for aa in sequence if aa in 'PC')  # Rigid residues
        buried_residues = sum(1 for aa in sequence if aa in 'VILFW')  # Likely buried residues
        
        adaptability = (
            (flexibility_residues + surface_residues) / len(sequence) -
            (rigid_residues + buried_residues) / len(sequence) * 0.5
        )
        
        return max(0, min(1, adaptability + 0.5))  # Normalize to [0,1]
    
    def _calculate_pressure_intensity(self, env_factors: Dict[str, float]) -> float:
        """Calculate environmental pressure intensity"""
        
        pressures = []
        
        # Temperature pressure
        temp = env_factors.get('temperature', 25)
        temp_pressure = max(abs(temp - 25) / 25, 0)
        pressures.append(min(temp_pressure, 1))
        
        # pH pressure
        ph = env_factors.get('ph', 7)
        ph_pressure = abs(ph - 7) / 7
        pressures.append(min(ph_pressure, 1))
        
        # Salinity pressure
        salinity = env_factors.get('salinity', 0)
        pressures.append(min(salinity / 10, 1))
        
        # Oxygen pressure
        oxygen = env_factors.get('oxygen_level', 0.21)
        oxygen_pressure = abs(oxygen - 0.21) / 0.21
        pressures.append(min(oxygen_pressure, 1))
        
        return np.mean(pressures)
    
    def _identify_limiting_factors(self, situation: ESESituation) -> List[str]:
        """Identify factors that limit adaptive potential"""
        
        limiting_factors = []
        
        # Structural constraints
        proline_content = situation.protein_sequence.count('P') / len(situation.protein_sequence)
        if proline_content > 0.1:
            limiting_factors.append('high_proline_structural_constraint')
        
        # Environmental extremes
        env_factors = situation.environmental_factors
        if env_factors.get('temperature', 25) > 80:
            limiting_factors.append('extreme_temperature')
        
        if env_factors.get('ph', 7) < 3 or env_factors.get('ph', 7) > 11:
            limiting_factors.append('extreme_ph')
        
        # Multiple simultaneous stresses
        if len(situation.stress_indicators) > 5:
            limiting_factors.append('multiple_stress_overload')
        
        return limiting_factors
    
    def _predict_evolutionary_trajectory(self, situation: ESESituation, encoding: np.ndarray) -> Dict:
        """Predict likely evolutionary trajectory"""
        
        if self.adaptation_predictor:
            # Use ML predictor
            trajectory = self._ml_trajectory_prediction(encoding)
        else:
            # Use rule-based prediction
            trajectory = self._rule_based_trajectory_prediction(situation)
        
        return trajectory
    
    def _ml_trajectory_prediction(self, encoding: np.ndarray) -> Dict:
        """Predict trajectory using machine learning"""
        
        # Mock prediction - in practice would use trained model
        prediction_scores = np.random.random(4)  # ['stable', 'adapting', 'diverging', 'novel']
        prediction_scores = prediction_scores / np.sum(prediction_scores)
        
        classes = ['stable', 'adapting', 'diverging', 'novel']
        
        return {
            'predicted_class': classes[np.argmax(prediction_scores)],
            'class_probabilities': dict(zip(classes, prediction_scores)),
            'prediction_confidence': float(np.max(prediction_scores)),
            'time_horizon': 'short_term'  # generations to decades
        }
    
    def _rule_based_trajectory_prediction(self, situation: ESESituation) -> Dict:
        """Predict trajectory using evolutionary rules"""
        
        # Assess stability vs change factors
        stability_factors = []
        change_factors = []
        
        # Conservation indicators
        if len(situation.adaptation_signals) < 2:
            stability_factors.append('low_adaptation_signal')
        
        # Change indicators
        if len(situation.stress_indicators) > 3:
            change_factors.append('high_stress_load')
        
        pressure_level = self._calculate_pressure_intensity(situation.environmental_factors)
        if pressure_level > 0.7:
            change_factors.append('high_environmental_pressure')
        
        # Predict based on balance
        if len(change_factors) > len(stability_factors):
            predicted_class = 'adapting' if pressure_level < 0.9 else 'diverging'
        else:
            predicted_class = 'stable'
        
        return {
            'predicted_class': predicted_class,
            'stability_factors': stability_factors,
            'change_factors': change_factors,
            'confidence': situation.confidence_score * 0.8  # Slightly lower for rule-based
        }
    
    def _integrate_contextual_knowledge(self, situation: ESESituation) -> Dict:
        """Integrate broader contextual knowledge"""
        
        context = {}
        
        # Taxonomic context
        context['taxonomic_implications'] = self._infer_taxonomic_context(situation)
        
        # Functional context
        context['functional_implications'] = self._infer_functional_context(situation)
        
        # Evolutionary context
        context['evolutionary_context'] = self._infer_evolutionary_context(situation)
        
        return context
    
    def _infer_taxonomic_context(self, situation: ESESituation) -> Dict:
        """Infer taxonomic implications"""
        
        # Simplified taxonomic inference based on environmental preferences
        env_factors = situation.environmental_factors
        
        taxonomic_hints = {}
        
        if env_factors.get('temperature', 25) > 60:
            taxonomic_hints['thermophile_affinity'] = 0.8
            taxonomic_hints['likely_domain'] = 'Archaea'
        
        elif env_factors.get('ph', 7) < 4:
            taxonomic_hints['acidophile_affinity'] = 0.7
            taxonomic_hints['likely_phylum'] = 'Proteobacteria or Firmicutes'
        
        elif env_factors.get('salinity', 0) > 5:
            taxonomic_hints['halophile_affinity'] = 0.9
            taxonomic_hints['likely_environment'] = 'marine or hypersaline'
        
        return taxonomic_hints
    
    def _infer_functional_context(self, situation: ESESituation) -> Dict:
        """Infer functional implications"""
        
        functional_hints = {}
        
        # Based on stress indicators
        if 'thermal_stress' in situation.stress_indicators:
            functional_hints['heat_shock_response'] = 0.8
            functional_hints['chaperone_activity'] = 0.7
        
        if 'oxidative_stress' in ' '.join(situation.stress_indicators):
            functional_hints['antioxidant_activity'] = 0.8
            functional_hints['redox_regulation'] = 0.6
        
        # Based on adaptation signals
        for signal in situation.adaptation_signals:
            if signal['type'] == 'stability_adaptation':
                functional_hints['structural_stability'] = 0.9
            elif signal['type'] == 'thermal_adaptation':
                functional_hints['thermal_tolerance'] = 0.8
        
        return functional_hints
    
    def _infer_evolutionary_context(self, situation: ESESituation) -> Dict:
        """Infer evolutionary context"""
        
        context = {}
        
        # Selection pressure assessment
        total_stress = len(situation.stress_indicators)
        if total_stress > 4:
            context['selection_pressure'] = 'high'
            context['evolutionary_rate'] = 'accelerated'
        else:
            context['selection_pressure'] = 'moderate'
            context['evolutionary_rate'] = 'normal'
        
        # Adaptation mode
        adaptation_count = len(situation.adaptation_signals)
        if adaptation_count > 3:
            context['adaptation_mode'] = 'rapid_adaptation'
        elif adaptation_count > 1:
            context['adaptation_mode'] = 'gradual_optimization'
        else:
            context['adaptation_mode'] = 'stabilizing_selection'
        
        return context
    
    def _quantify_uncertainty(self, situation: ESESituation, encoding: np.ndarray) -> Dict:
        """Quantify uncertainty in the analysis"""
        
        uncertainties = {}
        
        # Data completeness uncertainty
        missing_data = sum(1 for v in situation.environmental_factors.values() if v == 0)
        data_uncertainty = missing_data / len(situation.environmental_factors)
        uncertainties['data_completeness'] = 1 - data_uncertainty
        
        # Model uncertainty
        if self.ese_encoder is None:
            uncertainties['model_availability'] = 0.5  # Fallback methods
        else:
            uncertainties['model_availability'] = 0.9
        
        # Situation recognition uncertainty
        uncertainties['situation_recognition'] = situation.confidence_score
        
        # Encoding quality uncertainty
        encoding_quality = 1 - (np.sum(encoding == 0) / len(encoding))  # Non-zero fraction
        uncertainties['encoding_quality'] = encoding_quality
        
        # Overall uncertainty
        uncertainties['overall'] = np.mean(list(uncertainties.values()))
        
        return uncertainties
    
    async def _assess_evolutionary_metrics(
        self, 
        situation: ESESituation, 
        encoding: np.ndarray, 
        sentient_analysis: Dict
    ) -> Dict:
        """Assess key evolutionary metrics"""
        
        metrics = {}
        
        # Evolutionary pressure
        pressure_factors = [
            len(situation.stress_indicators) / 10.0,
            self._calculate_pressure_intensity(situation.environmental_factors),
            1 - situation.confidence_score  # Uncertainty adds pressure
        ]
        metrics['pressure'] = np.mean(pressure_factors)
        
        # Adaptation likelihood
        adaptive_potential = sentient_analysis.get('adaptive_potential', {})
        adaptation_likelihood = adaptive_potential.get('overall_potential', 0.5)
        metrics['adaptation_likelihood'] = adaptation_likelihood
        
        # Conservation score
        conservation_indicators = [
            len(situation.adaptation_signals) == 0,  # No current adaptations
            situation.environmental_factors.get('temperature', 25) == 25,  # Standard conditions
            'conserved_regions' in sentient_analysis.get('pattern_recognition', {})
        ]
        metrics['conservation'] = sum(conservation_indicators) / len(conservation_indicators)
        
        # Novelty index
        pattern_similarities = sentient_analysis.get('pattern_recognition', {})
        if pattern_similarities:
            max_similarity = max(p.get('similarity', 0) for p in pattern_similarities.values() if isinstance(p, dict))
            metrics['novelty'] = 1 - max_similarity
        else:
            metrics['novelty'] = 0.8  # High novelty if no pattern matches
        
        return metrics
    
    async def _integrate_chronoracle_reasoning(
        self, 
        situation: ESESituation, 
        metrics: Dict
    ) -> Dict:
        """Integrate Chronoracle reasoning for enhanced analysis"""
        
        # Create Chronoracle query
        query = ChronoracleQuery(
            protein_sequence=situation.protein_sequence,
            query_type='evolutionary',
            context_embeddings=None,  # Would use ESE encoding in practice
            confidence_threshold=0.6
        )
        
        # Get Chronoracle reasoning
        try:
            chronoracle_response = await self.chronoracle_client.reason_about_protein(query)
            
            insights = {
                'hypothesis': chronoracle_response.hypothesis,
                'confidence': chronoracle_response.confidence_score,
                'reasoning_chain': chronoracle_response.reasoning_chain,
                'next_experiments': chronoracle_response.next_experiments,
                'temporal_analysis': chronoracle_response.temporal_analysis
            }
        except Exception as e:
            logger.warning(f"Chronoracle integration failed: {e}")
            insights = {
                'status': 'unavailable',
                'fallback_hypothesis': 'Evolutionary analysis requires additional context'
            }
        
        return insights
    
    async def _update_learning_patterns(self, annotation: ESEAnnotation):
        """Update learning patterns based on new annotation"""
        
        # Store annotation pattern
        pattern_key = f"{annotation.situation.context_type}_{annotation.evolutionary_pressure:.1f}"
        
        if pattern_key not in self.situation_patterns:
            self.situation_patterns[pattern_key] = {
                'examples': [],
                'avg_metrics': {},
                'learning_iterations': 0
            }
        
        # Add example
        self.situation_patterns[pattern_key]['examples'].append(annotation.annotation_id)
        self.situation_patterns[pattern_key]['learning_iterations'] += 1
        
        # Update average metrics
        current_metrics = {
            'pressure': annotation.evolutionary_pressure,
            'adaptation': annotation.adaptation_likelihood,
            'conservation': annotation.conservation_score,
            'novelty': annotation.novelty_index
        }
        
        if not self.situation_patterns[pattern_key]['avg_metrics']:
            self.situation_patterns[pattern_key]['avg_metrics'] = current_metrics
        else:
            # Running average update
            n = self.situation_patterns[pattern_key]['learning_iterations']
            for metric, value in current_metrics.items():
                old_avg = self.situation_patterns[pattern_key]['avg_metrics'][metric]
                self.situation_patterns[pattern_key]['avg_metrics'][metric] = \
                    (old_avg * (n-1) + value) / n
        
        # Store in learning history
        self.learning_history.append({
            'timestamp': datetime.now(),
            'annotation_id': annotation.annotation_id,
            'pattern_key': pattern_key,
            'learning_update': 'pattern_reinforced'
        })
        
        logger.info(f"Updated learning pattern: {pattern_key}")
    
    def get_annotation_statistics(self) -> Dict:
        """Get comprehensive statistics about annotations"""
        
        if not self.annotations_cache:
            return {'status': 'no_annotations'}
        
        stats = {}
        
        # Basic counts
        stats['total_annotations'] = len(self.annotations_cache)
        stats['unique_situations'] = len(set(ann.situation.context_type for ann in self.annotations_cache))
        
        # Metric distributions
        pressures = [ann.evolutionary_pressure for ann in self.annotations_cache]
        adaptations = [ann.adaptation_likelihood for ann in self.annotations_cache]
        novelties = [ann.novelty_index for ann in self.annotations_cache]
        
        stats['metrics'] = {
            'pressure': {'mean': np.mean(pressures), 'std': np.std(pressures)},
            'adaptation': {'mean': np.mean(adaptations), 'std': np.std(adaptations)},
            'novelty': {'mean': np.mean(novelties), 'std': np.std(novelties)}
        }
        
        # Context type distribution
        context_types = [ann.situation.context_type for ann in self.annotations_cache]
        stats['context_distribution'] = {ct: context_types.count(ct) for ct in set(context_types)}
        
        return stats
    
    def export_annotations(self, filepath: str, format: str = 'json'):
        """Export all annotations to file"""
        
        export_data = {
            'metadata': {
                'export_timestamp': datetime.now().isoformat(),
                'annotator_version': 'ESE_Sentient_System_v1.0',
                'total_annotations': len(self.annotations_cache)
            },
            'annotations': [asdict(ann) for ann in self.annotations_cache],
            'learning_patterns': self.situation_patterns,
            'statistics': self.get_annotation_statistics()
        }
        
        if format == 'json':
            import json
            with open(filepath, 'w') as f:
                json.dump(export_data, f, indent=2, default=str)
        
        elif format == 'csv':
            # Export flattened data for analysis
            rows = []
            for ann in self.annotations_cache:
                row = {
                    'annotation_id': ann.annotation_id,
                    'context_type': ann.situation.context_type,
                    'sequence_length': len(ann.situation.protein_sequence),
                    'evolutionary_pressure': ann.evolutionary_pressure,
                    'adaptation_likelihood': ann.adaptation_likelihood,
                    'conservation_score': ann.conservation_score,
                    'novelty_index': ann.novelty_index,
                    'timestamp': ann.annotation_timestamp
                }
                rows.append(row)
            
            import csv
            with open(filepath, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
        
        logger.info(f"Annotations exported to {filepath} ({format} format)")


# Neural network classes for ESE encoding

class ESEEncoder(nn.Module):
    """Neural network for generating 416D ESE encodings"""
    
    def __init__(self, input_dim: int, output_dim: int, hidden_dims: List[int]):
        super(ESEEncoder, self).__init__()
        
        layers = []
        prev_dim = input_dim
        
        for hidden_dim in hidden_dims[:-1]:
            layers.extend([
                nn.Linear(prev_dim, hidden_dim),
                nn.ReLU(),
                nn.Dropout(0.2)
            ])
            prev_dim = hidden_dim
        
        # Final layer to ESE dimension
        layers.append(nn.Linear(prev_dim, output_dim))
        layers.append(nn.Tanh())  # Bounded output
        
        self.encoder = nn.Sequential(*layers)
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.encoder(x)


class AdaptationPredictor(nn.Module):
    """Neural network for predicting adaptation outcomes"""
    
    def __init__(self, input_dim: int, prediction_classes: List[str]):
        super(AdaptationPredictor, self).__init__()
        
        self.classes = prediction_classes
        num_classes = len(prediction_classes)
        
        self.predictor = nn.Sequential(
            nn.Linear(input_dim, 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, num_classes),
            nn.Softmax(dim=1)
        )
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.predictor(x)
    
    def predict_class(self, x: torch.Tensor) -> str:
        """Predict most likely class"""
        probs = self.forward(x)
        class_idx = torch.argmax(probs, dim=1).item()
        return self.classes[class_idx]
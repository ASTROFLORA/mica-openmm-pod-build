"""
Sistema de Clasificación de Intenciones Inteligente para MICA
Implementa análisis NLU avanzado para determinar la intención real de los mensajes.
"""

import re
import string
from typing import Dict, Tuple, List, Optional
from dataclasses import dataclass
import json
import time

@dataclass
class IntentScore:
    """Representa la puntuación de confianza para una intención específica."""
    intent: str
    score: float
    confidence: str  # "high", "medium", "low"
    reasoning: str

class IntelligentIntentClassifier:
    """
    Clasificador inteligente de intenciones que utiliza análisis lingüístico avanzado
    y heurísticas específicas del dominio bioinformático.
    """
    
    def __init__(self):
        # Palabras clave para cada tipo de intención
        self.intent_keywords = {
            "chat": {
                "greetings": ["hola", "hello", "hi", "buenos días", "buenas tardes", "buenas noches"],
                "farewells": ["adiós", "goodbye", "bye", "hasta luego", "nos vemos"],
                "courtesy": ["gracias", "thanks", "por favor", "please", "disculpa", "sorry"],
                "status": ["cómo estás", "how are you", "qué tal", "how's it going"],
                "capabilities": ["qué puedes hacer", "what can you do", "cuáles son tus funciones", "help"]
            },
            "question": {
                "what": ["qué es", "what is", "qué significa", "what does", "cómo funciona", "how does"],
                "how": ["cómo", "how", "de qué manera", "in what way"],
                "why": ["por qué", "why", "cuál es la razón", "what's the reason"],
                "when": ["cuándo", "when", "en qué momento", "at what time"],
                "where": ["dónde", "where", "en qué lugar", "in what place"]
            },
            "tool_request": {
                "bioinformatics": ["blast", "align", "sequence", "protein", "dna", "rna", "genome"],
                "analysis": ["analyze", "analizar", "process", "procesar", "calculate", "calcular"],
                "creation": ["create", "crear", "generate", "generar", "build", "construir"],
                "database": ["database", "base de datos", "query", "consultar", "search", "buscar"],
                "visualization": ["visualize", "visualizar", "plot", "graph", "chart", "diagram"]
            },
            "approve": {
                "approval": ["aprobar", "approve", "sí", "yes", "correcto", "correct", "adelante", "go ahead"],
                "execution": ["ejecuta", "execute", "run", "corre", "inicia", "start", "continúa", "continue"]
            },
            "abort": {
                "cancellation": ["cancelar", "cancel", "detener", "stop", "abortar", "abort"],
                "interruption": ["para", "halt", "pausa", "pause", "termina", "end"]
            },
            "instruction": {
                "direct": ["haz", "do", "realiza", "perform", "crea", "create", "genera", "generate"],
                "specific": ["muestra", "show", "obtén", "get", "encuentra", "find", "calcula", "calculate"]
            }
        }
        
        # Patrones de complejidad para tareas bioinformáticas
        self.complexity_patterns = {
            "high": [
                r"cura.*cáncer", r"cure.*cancer", r"tratamiento.*enfermedad", r"treatment.*disease",
                r"análisis.*complejo", r"complex.*analysis", r"pipeline.*bioinformático", r"bioinformatics.*pipeline",
                r"investigación.*genómica", r"genomic.*research", r"desarrollo.*fármaco", r"drug.*development"
            ],
            "medium": [
                r"blast.*proteína", r"protein.*blast", r"alineamiento.*secuencia", r"sequence.*alignment",
                r"análisis.*estructura", r"structure.*analysis", r"base.*datos.*proteínas", r"protein.*database",
                r"visualización.*molecular", r"molecular.*visualization"
            ],
            "low": [
                r"buscar.*proteína", r"search.*protein", r"información.*gen", r"gene.*information",
                r"estadísticas.*secuencia", r"sequence.*statistics", r"resumen.*datos", r"data.*summary"
            ]
        }
    
    def extract_linguistic_features(self, message: str) -> Dict[str, float]:
        """
        Extrae características lingüísticas del mensaje para análisis.
        """
        message_lower = message.lower().strip()
        
        features = {
            "length": len(message),
            "word_count": len(message.split()),
            "question_marks": message.count("?"),
            "exclamation_marks": message.count("!"),
            "has_greeting": any(g in message_lower for g in self.intent_keywords["chat"]["greetings"]),
            "has_farewell": any(f in message_lower for f in self.intent_keywords["chat"]["farewells"]),
            "has_courtesy": any(c in message_lower for c in self.intent_keywords["chat"]["courtesy"]),
            "has_question_words": any(q in message_lower for q in self.intent_keywords["question"]["what"] + 
                                    self.intent_keywords["question"]["how"] + 
                                    self.intent_keywords["question"]["why"]),
            "has_bioinformatics_keywords": any(b in message_lower for b in self.intent_keywords["tool_request"]["bioinformatics"]),
            "has_analysis_keywords": any(a in message_lower for a in self.intent_keywords["tool_request"]["analysis"]),
            "has_creation_keywords": any(c in message_lower for c in self.intent_keywords["tool_request"]["creation"]),
            "has_approval_keywords": any(ap in message_lower for ap in self.intent_keywords["approve"]["approval"]),
            "has_cancellation_keywords": any(ca in message_lower for ca in self.intent_keywords["abort"]["cancellation"]),
            "has_instruction_keywords": any(i in message_lower for i in self.intent_keywords["instruction"]["direct"])
        }
        
        return features
    
    def apply_domain_heuristics(self, base_scores: Dict[str, float], message: str) -> Dict[str, float]:
        """
        Aplica heurísticas específicas del dominio bioinformático para ajustar las puntuaciones.
        """
        message_lower = message.lower()
        
        # Heurística 1: Si contiene términos bioinformáticos específicos, aumentar tool_request
        bioinfo_terms = ["uniprot", "pdb", "ncbi", "genbank", "ensembl", "blast", "fasta", "sam", "bam"]
        if any(term in message_lower for term in bioinfo_terms):
            base_scores["tool_request"] = min(1.0, base_scores["tool_request"] + 0.3)
        
        # Heurística 2: Si es una pregunta específica sobre datos, aumentar question
        if "?" in message and any(q in message_lower for q in ["qué", "what", "cómo", "how", "por qué", "why"]):
            base_scores["question"] = min(1.0, base_scores["question"] + 0.2)
        
        # Heurística 3: Si contiene comandos directos, aumentar instruction
        command_patterns = [r"^haz\s+", r"^crea\s+", r"^genera\s+", r"^analiza\s+", r"^busca\s+"]
        if any(re.search(pattern, message_lower) for pattern in command_patterns):
            base_scores["instruction"] = min(1.0, base_scores["instruction"] + 0.25)
        
        # Heurística 4: Si es un saludo simple, maximizar chat
        simple_greetings = ["hola", "hello", "hi", "buenos días", "buenas tardes"]
        if any(greeting in message_lower for greeting in simple_greetings) and len(message.split()) <= 3:
            base_scores["chat"] = 0.95
            base_scores["tool_request"] = 0.05
        
        return base_scores
    
    def enhanced_intent_detection(self, message: str) -> Dict[str, float]:
        """
        Análisis avanzado de intenciones con puntuaciones de confianza.
        
        Returns:
            {
                "chat": 0.85,        # Conversación general
                "tool_request": 0.15,  # Solicitud de herramienta específica
                "approve": 0.02,     # Aprobación de plan
                "abort": 0.01,       # Cancelación de operación
                "question": 0.75,    # Pregunta informativa
                "instruction": 0.23  # Instrucción directa
            }
        """
        if not message or not message.strip():
            return {"chat": 1.0, "tool_request": 0.0, "approve": 0.0, "abort": 0.0, "question": 0.0, "instruction": 0.0}
        
        # Extraer características lingüísticas
        features = self.extract_linguistic_features(message)
        
        # Calcular puntuaciones base
        base_scores = {
            "chat": 0.0,
            "tool_request": 0.0,
            "approve": 0.0,
            "abort": 0.0,
            "question": 0.0,
            "instruction": 0.0
        }
        
        # Puntuación para chat
        if features["has_greeting"] or features["has_farewell"] or features["has_courtesy"]:
            base_scores["chat"] += 0.4
        if features["length"] < 50 and features["word_count"] <= 5:
            base_scores["chat"] += 0.2
        
        # Puntuación para question
        if features["has_question_words"] or features["question_marks"] > 0:
            base_scores["question"] += 0.5
        if features["question_marks"] > 0:
            base_scores["question"] += 0.2
        
        # Puntuación para tool_request
        if features["has_bioinformatics_keywords"]:
            base_scores["tool_request"] += 0.4
        if features["has_analysis_keywords"]:
            base_scores["tool_request"] += 0.3
        if features["has_creation_keywords"]:
            base_scores["tool_request"] += 0.3
        
        # Puntuación para approve
        if features["has_approval_keywords"]:
            base_scores["approve"] += 0.6
        
        # Puntuación para abort
        if features["has_cancellation_keywords"]:
            base_scores["abort"] += 0.6
        
        # Puntuación para instruction
        if features["has_instruction_keywords"]:
            base_scores["instruction"] += 0.4
        
        # Normalizar puntuaciones
        total_score = sum(base_scores.values())
        if total_score > 0:
            base_scores = {k: v / total_score for k, v in base_scores.items()}
        
        # Aplicar heurísticas del dominio
        final_scores = self.apply_domain_heuristics(base_scores, message)
        
        # Asegurar que las puntuaciones sumen aproximadamente 1.0
        total_final = sum(final_scores.values())
        if total_final > 0:
            final_scores = {k: v / total_final for k, v in final_scores.items()}
        
        return final_scores
    
    def get_primary_intent(self, intent_scores: Dict[str, float]) -> Tuple[str, float]:
        """
        Obtiene la intención primaria y su puntuación de confianza.
        """
        if not intent_scores:
            return "chat", 0.0
        
        primary_intent = max(intent_scores.items(), key=lambda x: x[1])
        return primary_intent
    
    def is_bioinformatic_task(self, message: str) -> Tuple[bool, float]:
        """
        Determina si un mensaje requiere genuinamente procesamiento bioinformático.
        
        Returns:
            (es_tarea_bioinformatica, nivel_de_confianza)
        """
        message_lower = message.lower()
        
        # Características clave para identificar tareas bioinformáticas
        features = [
            self._contains_sequence_data(message_lower),
            self._contains_protein_identifiers(message_lower),
            self._requests_structural_analysis(message_lower),
            self._contains_phylogenetic_request(message_lower),
            self._mentions_specific_bioinformatics_tool(message_lower),
            self._contains_genomic_coordinates(message_lower),
            self._requests_data_processing(message_lower)
        ]
        
        # Pesos asignados a cada característica
        weights = [0.8, 0.9, 0.85, 0.7, 0.9, 0.85, 0.75]
        
        # Calcular score ponderado
        score = sum(f * w for f, w in zip(features, weights)) / sum(weights)
        
        # Umbral de decisión
        return score > 0.4, score
    
    def _contains_sequence_data(self, message: str) -> float:
        """Detecta si el mensaje contiene datos de secuencia."""
        sequence_patterns = [
            r"[atgc]{10,}", r"[atgc]{10,}",  # DNA/RNA
            r"[acdefghiklmnpqrstvwy]{10,}",  # Proteínas
            r"fasta", r"genbank", r"embl"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in sequence_patterns) else 0.0
    
    def _contains_protein_identifiers(self, message: str) -> float:
        """Detecta identificadores de proteínas."""
        protein_patterns = [
            r"p\d{5}", r"q\d{5}", r"o\d{5}",  # UniProt IDs
            r"[a-z0-9]{6,8}",  # IDs cortos
            r"protein", r"proteína", r"peptide", r"péptido"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in protein_patterns) else 0.0
    
    def _requests_structural_analysis(self, message: str) -> float:
        """Detecta solicitudes de análisis estructural."""
        structural_patterns = [
            r"estructura", r"structure", r"pdb", r"molécula", r"molecule",
            r"conformación", r"conformation", r"binding", r"unido", r"ligando", r"ligand",
            r"dinámica molecular", r"molecular dynamics", r"simulación", r"simulation",
            r"cambios conformacionales", r"conformational changes", r"bolsillos crípticos", r"cryptic pockets",
            r"trayectoria", r"trajectory", r"estados conformacionales", r"conformational states",
            r"sitios de unión", r"binding sites", r"drogabilidad", r"druggability", r"scoring energético", r"energetic scoring",
            r"inhibidores", r"inhibitors", r"fragmentos químicos", r"chemical fragments", r"kinasa", r"kinase"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in structural_patterns) else 0.0
    
    def _contains_phylogenetic_request(self, message: str) -> float:
        """Detecta solicitudes de análisis filogenético."""
        phylogenetic_patterns = [
            r"filogenia", r"phylogeny", r"evolución", r"evolution", r"árbol", r"tree",
            r"ancestro", r"ancestor", r"clado", r"clade"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in phylogenetic_patterns) else 0.0
    
    def _mentions_specific_bioinformatics_tool(self, message: str) -> float:
        """Detecta menciones de herramientas bioinformáticas específicas."""
        tool_patterns = [
            r"blast", r"clustal", r"muscle", r"mafft", r"tcoffee",
            r"bowtie", r"bwa", r"samtools", r"bedtools", r"ucsc",
            r"smic", r"chronosfold", r"dynamo", r"openmm", r"gromacs", r"amber"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in tool_patterns) else 0.0
    
    def _contains_genomic_coordinates(self, message: str) -> float:
        """Detecta coordenadas genómicas."""
        coordinate_patterns = [
            r"chr\d+:\d+-\d+", r"chr[a-z]+:\d+-\d+",  # chr1:1000-2000
            r"\d+:\d+-\d+",  # 1000-2000
            r"position", r"posición", r"locus", r"loci"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in coordinate_patterns) else 0.0
    
    def _requests_data_processing(self, message: str) -> float:
        """Detecta solicitudes de procesamiento de datos."""
        processing_patterns = [
            r"procesar", r"process", r"filtrar", r"filter", r"normalizar", r"normalize",
            r"transformar", r"transform", r"convertir", r"convert", r"análisis", r"analysis"
        ]
        return 1.0 if any(re.search(pattern, message) for pattern in processing_patterns) else 0.0

# Instancia global del clasificador
intent_classifier = IntelligentIntentClassifier()

def enhanced_intent_detection(message: str) -> Dict[str, float]:
    """
    Función de conveniencia para usar el clasificador de intenciones.
    """
    return intent_classifier.enhanced_intent_detection(message)

def is_bioinformatic_task(message: str) -> Tuple[bool, float]:
    """
    Función de conveniencia para detectar tareas bioinformáticas.
    """
    return intent_classifier.is_bioinformatic_task(message)

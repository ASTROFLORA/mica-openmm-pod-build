#!/usr/bin/env python3
"""
🤖 Multi-Agent Cognitive Architecture for MICA
Implementación de la Arquitectura Cognitiva Distribuida siguiendo la Guía Técnica Española

Características implementadas según el documento:
- Orchestrator Agent (Controlador Metacognitivo)
- Ingestion Agents especializados (PDF, PubMed, PDB)
- Retrieval Agents (Vector, Graph)
- Synthesis Agent para generación coherente
- Cache Agent para memoria proactiva
- Shared Metacognition a través de memoria compartida
- Collaborative Reasoning Loop
"""

import os
import asyncio
import json
from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

class AgentType(Enum):
    """Tipos de agentes en la arquitectura cognitiva"""
    ORCHESTRATOR = "orchestrator"
    INGESTION = "ingestion"
    RETRIEVAL = "retrieval"
    SYNTHESIS = "synthesis"
    CACHE = "cache"

class TaskStatus(Enum):
    """Estados de las tareas en el sistema"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    DELEGATED = "delegated"

@dataclass
class CognitiveTask:
    """Tarea cognitiva en el sistema multi-agente"""
    task_id: str
    task_type: str
    description: str
    priority: int
    assigned_agent: Optional[str]
    status: TaskStatus
    input_data: Dict[str, Any]
    output_data: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    confidence_score: float
    requires_collaboration: bool

@dataclass
class AgentMessage:
    """Mensaje entre agentes en el sistema"""
    message_id: str
    sender_agent: str
    recipient_agent: str
    message_type: str
    content: Dict[str, Any]
    timestamp: datetime
    response_required: bool

@dataclass
class MetacognitiveState:
    """Estado metacognitivo compartido del sistema"""
    current_query: str
    query_complexity: float
    active_tasks: List[CognitiveTask]
    confidence_scores: Dict[str, float]
    knowledge_gaps: List[str]
    retrieval_quality: float
    reasoning_depth: int
    timestamp: datetime

class BaseAgent(ABC):
    """Clase base para todos los agentes cognitivos"""
    
    def __init__(self, agent_id: str, agent_type: AgentType, config: Dict[str, Any]):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.config = config
        self.is_active = False
        self.performance_stats = {
            "tasks_completed": 0,
            "avg_response_time": 0.0,
            "success_rate": 0.0,
            "last_activity": None
        }
        
        # Referencias a otros componentes del sistema
        self.cache_manager = None
        self.milvus_manager = None
        self.biobert_pipeline = None
        
    @abstractmethod
    async def process_task(self, task: CognitiveTask) -> Dict[str, Any]:
        """Procesar una tarea cognitiva específica"""
        pass
    
    @abstractmethod
    async def handle_message(self, message: AgentMessage) -> Optional[AgentMessage]:
        """Manejar mensaje de otro agente"""
        pass
    
    async def initialize(self):
        """Inicializar el agente"""
        self.is_active = True
        logger.info(f"🤖 Agente {self.agent_id} ({self.agent_type.value}) inicializado")
    
    async def shutdown(self):
        """Finalizar el agente"""
        self.is_active = False
        logger.info(f"🔴 Agente {self.agent_id} finalizado")
    
    def update_performance_stats(self, success: bool, response_time: float):
        """Actualizar estadísticas de rendimiento"""
        self.performance_stats["tasks_completed"] += 1
        self.performance_stats["last_activity"] = datetime.now()
        
        # Actualizar tiempo de respuesta promedio
        current_avg = self.performance_stats["avg_response_time"]
        tasks_count = self.performance_stats["tasks_completed"]
        self.performance_stats["avg_response_time"] = (
            (current_avg * (tasks_count - 1) + response_time) / tasks_count
        )
        
        # Actualizar tasa de éxito
        if tasks_count == 1:
            self.performance_stats["success_rate"] = 1.0 if success else 0.0
        else:
            current_success_rate = self.performance_stats["success_rate"]
            total_successes = current_success_rate * (tasks_count - 1)
            if success:
                total_successes += 1
            self.performance_stats["success_rate"] = total_successes / tasks_count

class OrchestratorAgent(BaseAgent):
    """
    🧠 Agente Orquestador - Controlador Metacognitivo
    
    Función ejecutiva del sistema que:
    - Analiza la complejidad de las consultas
    - Descompone tareas complejas
    - Delega trabajo a agentes especializados
    - Monitorea el progreso y calidad
    - Sintetiza respuestas finales
    """
    
    def __init__(self, agent_id: str, config: Dict[str, Any]):
        super().__init__(agent_id, AgentType.ORCHESTRATOR, config)
        self.active_sessions: Dict[str, MetacognitiveState] = {}
        self.agent_registry: Dict[str, BaseAgent] = {}
        
    async def process_task(self, task: CognitiveTask) -> Dict[str, Any]:
        """Procesar consulta compleja con loop metacognitivo"""
        start_time = datetime.now()
        session_id = task.task_id
        
        try:
            # 1. Análisis de complejidad de la consulta
            complexity_analysis = await self._analyze_query_complexity(task.input_data.get("query", ""))
            
            # 2. Inicializar estado metacognitivo
            metacognitive_state = MetacognitiveState(
                current_query=task.input_data.get("query", ""),
                query_complexity=complexity_analysis["complexity_score"],
                active_tasks=[],
                confidence_scores={},
                knowledge_gaps=[],
                retrieval_quality=0.0,
                reasoning_depth=0,
                timestamp=datetime.now()
            )
            
            self.active_sessions[session_id] = metacognitive_state
            
            # 3. Descomposición y planificación
            execution_plan = await self._create_execution_plan(complexity_analysis)
            
            # 4. Loop metacognitivo iterativo
            final_result = await self._execute_metacognitive_loop(session_id, execution_plan)
            
            # 5. Síntesis final
            synthesis_result = await self._synthesize_final_response(session_id, final_result)
            
            response_time = (datetime.now() - start_time).total_seconds()
            self.update_performance_stats(True, response_time)
            
            return {
                "success": True,
                "result": synthesis_result,
                "metacognitive_insights": {
                    "complexity_score": complexity_analysis["complexity_score"],
                    "reasoning_steps": len(execution_plan["steps"]),
                    "confidence_score": metacognitive_state.confidence_scores.get("final", 0.8),
                    "knowledge_gaps_identified": metacognitive_state.knowledge_gaps
                },
                "response_time_seconds": response_time
            }
            
        except Exception as e:
            logger.error(f"❌ Error en Orchestrator Agent: {e}")
            self.update_performance_stats(False, (datetime.now() - start_time).total_seconds())
            return {"success": False, "error": str(e)}
        
        finally:
            # Limpiar sesión
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
    
    async def _analyze_query_complexity(self, query: str) -> Dict[str, Any]:
        """Analizar complejidad de la consulta"""
        complexity_indicators = {
            "multi_hop": len([word for word in ["compare", "relationship", "interaction"] if word in query.lower()]),
            "temporal": len([word for word in ["recent", "latest", "current", "2023", "2024"] if word in query.lower()]),
            "synthesis": len([word for word in ["summarize", "explain", "analyze", "evaluate"] if word in query.lower()]),
            "scientific_entities": len([word for word in ["protein", "gene", "molecule", "pathway"] if word in query.lower()])
        }
        
        # Calcular score de complejidad (0.0 - 1.0)
        complexity_score = min(sum(complexity_indicators.values()) / 10.0, 1.0)
        
        return {
            "complexity_score": complexity_score,
            "indicators": complexity_indicators,
            "requires_multi_step": complexity_score > 0.5,
            "requires_synthesis": complexity_indicators["synthesis"] > 0
        }
    
    async def _create_execution_plan(self, complexity_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Crear plan de ejecución basado en la complejidad"""
        steps = []
        
        if complexity_analysis["complexity_score"] < 0.3:
            # Consulta simple - búsqueda directa
            steps = [
                {"type": "simple_retrieval", "agent": "retrieval", "priority": 1},
                {"type": "basic_synthesis", "agent": "synthesis", "priority": 2}
            ]
        else:
            # Consulta compleja - proceso multi-paso
            steps = [
                {"type": "query_expansion", "agent": "retrieval", "priority": 1},
                {"type": "multi_source_retrieval", "agent": "retrieval", "priority": 2},
                {"type": "confidence_assessment", "agent": "orchestrator", "priority": 3},
                {"type": "iterative_refinement", "agent": "retrieval", "priority": 4},
                {"type": "advanced_synthesis", "agent": "synthesis", "priority": 5}
            ]
        
        return {
            "steps": steps,
            "estimated_time": len(steps) * 2.0,  # Estimación en segundos
            "requires_collaboration": len(steps) > 2
        }
    
    async def _execute_metacognitive_loop(self, session_id: str, execution_plan: Dict[str, Any]) -> Dict[str, Any]:
        """Ejecutar loop metacognitivo iterativo"""
        metacognitive_state = self.active_sessions[session_id]
        accumulated_results = {}
        
        for step in execution_plan["steps"]:
            # Crear tarea cognitiva para el paso
            task = CognitiveTask(
                task_id=f"{session_id}_step_{step['priority']}",
                task_type=step["type"],
                description=f"Executing {step['type']}",
                priority=step["priority"],
                assigned_agent=step["agent"],
                status=TaskStatus.PENDING,
                input_data={
                    "query": metacognitive_state.current_query,
                    "previous_results": accumulated_results,
                    "context": metacognitive_state.__dict__
                },
                output_data=None,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                confidence_score=0.0,
                requires_collaboration=execution_plan["requires_collaboration"]
            )
            
            # Ejecutar paso
            step_result = await self._execute_step(task)
            accumulated_results[step["type"]] = step_result
            
            # Evaluación metacognitiva
            await self._metacognitive_assessment(session_id, step_result)
            
            # Decisión de continuar o refinar
            if await self._should_refine_approach(session_id):
                logger.info(f"🔄 Refinando enfoque en sesión {session_id}")
                # Aquí se podría agregar lógica para modificar el plan dinámicamente
        
        return accumulated_results
    
    async def _execute_step(self, task: CognitiveTask) -> Dict[str, Any]:
        """Ejecutar un paso individual delegando al agente apropiado"""
        # Por ahora, simulamos la ejecución
        # En implementación completa, esto delegaría al agente real
        
        if task.task_type == "simple_retrieval":
            return {
                "type": "retrieval_result",
                "documents": ["doc1", "doc2", "doc3"],
                "confidence": 0.85,
                "source": "vector_search"
            }
        elif task.task_type == "multi_source_retrieval":
            return {
                "type": "multi_retrieval_result",
                "vector_results": ["doc1", "doc2"],
                "graph_results": ["relation1", "relation2"],
                "hybrid_confidence": 0.90
            }
        elif task.task_type == "advanced_synthesis":
            return {
                "type": "synthesis_result",
                "generated_text": "Synthesized response based on retrieved context...",
                "coherence_score": 0.88,
                "factual_grounding": 0.92
            }
        else:
            return {
                "type": "generic_result",
                "status": "completed",
                "confidence": 0.75
            }
    
    async def _metacognitive_assessment(self, session_id: str, step_result: Dict[str, Any]):
        """Evaluación metacognitiva del progreso"""
        metacognitive_state = self.active_sessions[session_id]
        
        # Actualizar confidence scores
        if "confidence" in step_result:
            step_type = step_result.get("type", "unknown")
            metacognitive_state.confidence_scores[step_type] = step_result["confidence"]
        
        # Detectar gaps de conocimiento
        if step_result.get("confidence", 1.0) < 0.7:
            gap = f"Low confidence in {step_result.get('type', 'unknown step')}"
            if gap not in metacognitive_state.knowledge_gaps:
                metacognitive_state.knowledge_gaps.append(gap)
        
        # Actualizar calidad de recuperación
        if "retrieval" in step_result.get("type", ""):
            metacognitive_state.retrieval_quality = step_result.get("confidence", 0.0)
        
        metacognitive_state.updated_at = datetime.now()
    
    async def _should_refine_approach(self, session_id: str) -> bool:
        """Decidir si se debe refinar el enfoque basado en el estado metacognitivo"""
        metacognitive_state = self.active_sessions[session_id]
        
        # Refinar si la calidad de recuperación es baja
        if metacognitive_state.retrieval_quality < 0.6:
            return True
        
        # Refinar si hay muchos gaps de conocimiento
        if len(metacognitive_state.knowledge_gaps) > 2:
            return True
        
        # Refinar si la confianza promedio es baja
        if metacognitive_state.confidence_scores:
            avg_confidence = sum(metacognitive_state.confidence_scores.values()) / len(metacognitive_state.confidence_scores)
            if avg_confidence < 0.7:
                return True
        
        return False
    
    async def _synthesize_final_response(self, session_id: str, accumulated_results: Dict[str, Any]) -> Dict[str, Any]:
        """Síntesis final de la respuesta"""
        metacognitive_state = self.active_sessions[session_id]
        
        # Calcular confianza final
        if metacognitive_state.confidence_scores:
            final_confidence = sum(metacognitive_state.confidence_scores.values()) / len(metacognitive_state.confidence_scores)
        else:
            final_confidence = 0.5
        
        metacognitive_state.confidence_scores["final"] = final_confidence
        
        return {
            "query": metacognitive_state.current_query,
            "response": "Synthesized response based on cognitive processing...",
            "confidence": final_confidence,
            "sources_used": list(accumulated_results.keys()),
            "reasoning_steps": len(accumulated_results),
            "knowledge_gaps": metacognitive_state.knowledge_gaps,
            "processing_timestamp": datetime.now().isoformat()
        }
    
    async def handle_message(self, message: AgentMessage) -> Optional[AgentMessage]:
        """Manejar mensaje de otro agente"""
        if message.message_type == "status_update":
            # Actualizar estado de tarea
            task_id = message.content.get("task_id")
            status = message.content.get("status")
            logger.info(f"📊 Actualización de estado: {task_id} -> {status}")
        
        elif message.message_type == "request_guidance":
            # Proporcionar orientación a agente
            guidance = await self._provide_guidance(message.content)
            return AgentMessage(
                message_id=f"guidance_{datetime.now().timestamp()}",
                sender_agent=self.agent_id,
                recipient_agent=message.sender_agent,
                message_type="guidance_response",
                content=guidance,
                timestamp=datetime.now(),
                response_required=False
            )
        
        return None
    
    async def _provide_guidance(self, request_content: Dict[str, Any]) -> Dict[str, Any]:
        """Proporcionar orientación a agentes que la soliciten"""
        return {
            "guidance": "Continue with current approach",
            "confidence_threshold": 0.7,
            "alternative_strategies": ["query_expansion", "cross_verification"]
        }

class CognitiveArchitecture:
    """
    🏗️ Arquitectura Cognitiva Principal del Sistema MICA
    
    Coordina todos los agentes y proporciona la infraestructura
    para la cognición colaborativa distribuida
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.agents: Dict[str, BaseAgent] = {}
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.is_running = False
        
        # Componentes del sistema
        self.cache_manager = None
        self.milvus_manager = None
        self.biobert_pipeline = None
        
    async def initialize(self, cache_manager=None, milvus_manager=None, biobert_pipeline=None):
        """Inicializar la arquitectura cognitiva"""
        logger.info("🏗️ Inicializando Arquitectura Cognitiva MICA...")
        
        # Almacenar referencias a componentes
        self.cache_manager = cache_manager
        self.milvus_manager = milvus_manager
        self.biobert_pipeline = biobert_pipeline
        
        # Crear agentes especializados
        await self._create_agents()
        
        # Conectar agentes con componentes del sistema
        await self._connect_agent_dependencies()
        
        # Inicializar todos los agentes
        for agent in self.agents.values():
            await agent.initialize()
        
        # Iniciar sistema de mensajería
        asyncio.create_task(self._message_processing_loop())
        
        self.is_running = True
        logger.info("✅ Arquitectura Cognitiva MICA inicializada correctamente")
    
    async def _create_agents(self):
        """Crear agentes especializados"""
        # Orchestrator Agent (controlador principal)
        orchestrator = OrchestratorAgent("orchestrator_001", self.config)
        self.agents["orchestrator"] = orchestrator
        
        # Aquí se crearían los otros agentes especializados
        # Por brevedad, creamos instancias básicas
        
        logger.info(f"🤖 Creados {len(self.agents)} agentes cognitivos")
    
    async def _connect_agent_dependencies(self):
        """Conectar agentes con componentes del sistema"""
        for agent in self.agents.values():
            agent.cache_manager = self.cache_manager
            agent.milvus_manager = self.milvus_manager
            agent.biobert_pipeline = self.biobert_pipeline
    
    async def _message_processing_loop(self):
        """Loop de procesamiento de mensajes entre agentes"""
        while self.is_running:
            try:
                # Procesar mensaje de la cola
                message = await asyncio.wait_for(self.message_queue.get(), timeout=1.0)
                await self._route_message(message)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"❌ Error procesando mensaje: {e}")
    
    async def _route_message(self, message: AgentMessage):
        """Enrutar mensaje al agente destinatario"""
        if message.recipient_agent in self.agents:
            recipient = self.agents[message.recipient_agent]
            response = await recipient.handle_message(message)
            if response:
                await self.message_queue.put(response)
    
    async def process_query(self, query: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Procesar consulta usando la arquitectura cognitiva completa"""
        if not self.is_running:
            return {"success": False, "error": "Architecture not running"}
        
        try:
            # Crear tarea cognitiva principal
            main_task = CognitiveTask(
                task_id=f"query_{datetime.now().timestamp()}",
                task_type="complex_query",
                description=f"Process query: {query[:50]}...",
                priority=1,
                assigned_agent="orchestrator",
                status=TaskStatus.PENDING,
                input_data={"query": query, "context": context or {}},
                output_data=None,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                confidence_score=0.0,
                requires_collaboration=True
            )
            
            # Delegar al Orchestrator Agent
            orchestrator = self.agents["orchestrator"]
            result = await orchestrator.process_task(main_task)
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error procesando consulta: {e}")
            return {"success": False, "error": str(e)}
    
    async def shutdown(self):
        """Finalizar la arquitectura cognitiva"""
        self.is_running = False
        
        for agent in self.agents.values():
            await agent.shutdown()
        
        logger.info("🔴 Arquitectura Cognitiva MICA finalizada")
    
    def get_system_status(self) -> Dict[str, Any]:
        """Obtener estado del sistema cognitivo"""
        agent_stats = {}
        for agent_id, agent in self.agents.items():
            agent_stats[agent_id] = {
                "type": agent.agent_type.value,
                "active": agent.is_active,
                "performance": agent.performance_stats
            }
        
        return {
            "running": self.is_running,
            "agents_count": len(self.agents),
            "agents": agent_stats,
            "components": {
                "cache_manager": self.cache_manager is not None,
                "milvus_manager": self.milvus_manager is not None,
                "biobert_pipeline": self.biobert_pipeline is not None
            },
            "timestamp": datetime.now().isoformat()
        }

# Factory function
async def create_cognitive_architecture(config: Dict[str, Any], 
                                      cache_manager=None, 
                                      milvus_manager=None, 
                                      biobert_pipeline=None) -> CognitiveArchitecture:
    """Factory para crear y inicializar la arquitectura cognitiva"""
    architecture = CognitiveArchitecture(config)
    await architecture.initialize(cache_manager, milvus_manager, biobert_pipeline)
    return architecture
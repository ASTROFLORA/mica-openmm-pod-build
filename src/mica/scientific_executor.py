# -*- coding: utf-8 -*-
"""
🧬 MICA SCIENTIFIC EXECUTOR
Ejecutor topológico para DAGs científicos con manejo avanzado de dependencias.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass
from enum import Enum

from src.models.analysis import ScientificProtocol

logger = logging.getLogger(__name__)


class StepStatus(Enum):
    """Estados posibles de un paso en la ejecución."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ExecutionStep:
    """Representa un paso de ejecución con su estado y metadatos."""
    id: str
    type: str
    description: str
    tool: str
    parameters: Dict[str, Any]
    dependencies: List[str]
    estimated_time: int
    status: StepStatus = StepStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class ExecutionContext:
    """Contexto de ejecución que mantiene el estado global."""
    protocol_id: str
    steps: Dict[str, ExecutionStep]
    completed_steps: Set[str]
    failed_steps: Set[str]
    step_results: Dict[str, Any]
    max_concurrent: int = 3


class ScientificExecutor:
    """Ejecutor científico que procesa DAGs de pasos con ejecución topológica."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.max_concurrent = self.config.get("max_concurrent", 3)
        logger.info(f"ScientificExecutor inicializado con max_concurrent={self.max_concurrent}")
    
    async def execute_plan_topologically(self, protocol: ScientificProtocol) -> ScientificProtocol:
        """Ejecuta un plan científico siguiendo el orden topológico de dependencias."""
        logger.info(f"Iniciando ejecución topológica para protocolo {protocol.id}")
        
        try:
            # 1. Preparar contexto de ejecución
            context = self._prepare_execution_context(protocol)
            
            # 2. Validar DAG (detectar ciclos)
            if not self._validate_dag(context):
                raise ValueError("El plan contiene dependencias circulares")
            
            # 3. Ejecutar pasos en orden topológico
            await self._execute_dag(context)
            
            # 4. Actualizar protocolo con resultados
            protocol = self._update_protocol_with_results(protocol, context)
            
            logger.info(f"Ejecución topológica completada para protocolo {protocol.id}")
            return protocol
            
        except Exception as e:
            logger.error(f"Error en ejecución topológica: {e}")
            protocol.execution.update({
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.utcnow().isoformat()
            })
            return protocol
    
    def _prepare_execution_context(self, protocol: ScientificProtocol) -> ExecutionContext:
        """Prepara el contexto de ejecución a partir del protocolo científico."""
        steps_data = protocol.plan.get("steps", [])
        
        # Convertir pasos a ExecutionStep
        steps = {}
        for step_data in steps_data:
            step = ExecutionStep(
                id=step_data.get("id", f"step_{len(steps)}"),
                type=step_data.get("type", "unknown"),
                description=step_data.get("description", ""),
                tool=step_data.get("tool", "unknown"),
                parameters=step_data.get("parameters", {}),
                dependencies=step_data.get("dependencies", []),
                estimated_time=step_data.get("estimated_time", 300)
            )
            steps[step.id] = step
        
        context = ExecutionContext(
            protocol_id=protocol.id,
            steps=steps,
            completed_steps=set(),
            failed_steps=set(),
            step_results={},
            max_concurrent=self.max_concurrent
        )
        
        logger.debug(f"Contexto preparado con {len(steps)} pasos")
        return context
    
    def _validate_dag(self, context: ExecutionContext) -> bool:
        """Valida que el DAG no contenga ciclos usando DFS."""
        # Estados para detección de ciclos: 0=no visitado, 1=visitando, 2=visitado
        state = {step_id: 0 for step_id in context.steps.keys()}
        
        def has_cycle(step_id: str) -> bool:
            if state[step_id] == 1:  # Ciclo detectado
                return True
            if state[step_id] == 2:  # Ya procesado
                return False
            
            state[step_id] = 1  # Marcar como visitando
            
            # Verificar dependencias
            step = context.steps[step_id]
            for dep_id in step.dependencies:
                if dep_id in context.steps and has_cycle(dep_id):
                    return True
            
            state[step_id] = 2  # Marcar como visitado
            return False
        
        # Verificar todos los nodos
        for step_id in context.steps.keys():
            if state[step_id] == 0 and has_cycle(step_id):
                logger.error(f"Ciclo detectado comenzando en el paso {step_id}")
                return False
        
        logger.debug("DAG validado correctamente - sin ciclos")
        return True
    
    async def _execute_dag(self, context: ExecutionContext) -> None:
        """Ejecuta el DAG con paralelismo controlado."""
        logger.info(f"Iniciando ejecución DAG con max_concurrent={context.max_concurrent}")
        
        while len(context.completed_steps) + len(context.failed_steps) < len(context.steps):
            # Obtener pasos listos para ejecutar
            ready_steps = self._get_ready_steps(context)
            
            if not ready_steps:
                break  # No hay más pasos ejecutables
            
            # Ejecutar pasos de forma secuencial por simplicidad (versión básica)
            for step_id in ready_steps[:context.max_concurrent]:
                try:
                    await self._execute_step(context, step_id)
                    context.completed_steps.add(step_id)
                    logger.info(f"Paso {step_id} completado exitosamente")
                except Exception as e:
                    context.failed_steps.add(step_id)
                    logger.error(f"Paso {step_id} falló: {e}")
        
        logger.info(f"Ejecución DAG completada. Completados: {len(context.completed_steps)}, "
                   f"Fallidos: {len(context.failed_steps)}")
    
    def _get_ready_steps(self, context: ExecutionContext) -> List[str]:
        """Obtiene los pasos que están listos para ejecutar (dependencias satisfechas)."""
        ready_steps = []
        
        for step_id, step in context.steps.items():
            if step.status != StepStatus.PENDING:
                continue
            
            # Verificar que todas las dependencias estén completadas
            dependencies_met = all(
                dep_id in context.completed_steps 
                for dep_id in step.dependencies 
                if dep_id in context.steps
            )
            
            if dependencies_met:
                ready_steps.append(step_id)
        
        return ready_steps
    
    async def _execute_step(self, context: ExecutionContext, step_id: str) -> None:
        """Ejecuta un paso individual del plan."""
        step = context.steps[step_id]
        step.status = StepStatus.RUNNING
        step.started_at = datetime.utcnow()
        
        logger.debug(f"Ejecutando paso {step_id}: {step.description}")
        
        try:
            # Simular ejecución
            await asyncio.sleep(0.1)
            
            result = {
                "step_id": step_id,
                "status": "completed",
                "output": f"Resultado simulado para {step_id}",
                "execution_time": 0.1,
                "metadata": {
                    "tool_used": step.tool,
                    "parameters": step.parameters
                }
            }
            
            step.status = StepStatus.COMPLETED
            step.result = result
            step.completed_at = datetime.utcnow()
            context.step_results[step_id] = result
            
        except Exception as e:
            step.status = StepStatus.FAILED
            step.error = str(e)
            step.completed_at = datetime.utcnow()
            raise
    
    def _update_protocol_with_results(self, protocol: ScientificProtocol, 
                                    context: ExecutionContext) -> ScientificProtocol:
        """Actualiza el protocolo científico con los resultados de la ejecución."""
        # Determinar estado final
        if len(context.failed_steps) == 0:
            final_status = "completed"
        elif len(context.completed_steps) > 0:
            final_status = "partially_completed"
        else:
            final_status = "failed"
        
        # Crear logs de ejecución
        execution_logs = []
        for step_id, step in context.steps.items():
            log_entry = {
                "step_id": step_id,
                "status": step.status.value,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "completed_at": step.completed_at.isoformat() if step.completed_at else None,
                "error": step.error
            }
            
            if step.result:
                log_entry["result"] = step.result
            
            execution_logs.append(log_entry)
        
        # Crear artefactos de resultados
        artifacts = []
        for step_id, result in context.step_results.items():
            artifacts.append({
                "step_id": step_id,
                "type": "execution_result",
                "payload": result,
                "timestamp": datetime.utcnow().isoformat()
            })
        
        # Calcular métricas de ejecución
        total_steps = len(context.steps)
        metrics = {
            "total_steps": total_steps,
            "completed_steps": len(context.completed_steps),
            "failed_steps": len(context.failed_steps),
            "success_rate": len(context.completed_steps) / total_steps if total_steps > 0 else 0,
            "execution_mode": "topological_dag"
        }
        
        # Actualizar protocolo
        protocol.execution.update({
            "status": final_status,
            "completed_at": datetime.utcnow().isoformat(),
            "logs": execution_logs,
            "artifacts": artifacts,
            "metrics": metrics
        })
        
        logger.info(f"Protocolo actualizado: {len(context.completed_steps)}/{total_steps} pasos completados")
        return protocol
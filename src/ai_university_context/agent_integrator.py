"""
🤖 AI University Agent Integration Module
Integrador principal para activación automática de contexto de investigadores

Este módulo permite al agente principal activar automáticamente el contexto
y encarnación de investigadores de AI University basado en menciones en mensajes.

Dr. Yuan Chen - AI University Research Labs
Fecha: 21/09/2025
"""

import sys
from pathlib import Path
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AIUniversityAgentIntegrator:
    """
    Integrador principal para el sistema de contexto de AI University
    Permite activación automática de investigadores en conversaciones
    """
    
    def __init__(self):
        self.context_manager = None
        self.current_researcher = None
        self.initialize_system()
    
    def initialize_system(self):
        """Inicializa el sistema de contexto"""
        try:
            # Importar dinámicamente el sistema de contexto
            project_root = Path(__file__).parent.parent.parent
            sys.path.append(str(project_root / "src" / "ai_university_context"))
            
            from persistent_context_system import (
                AIUniversityContextManager,
                activate_researcher_context
            )
            
            self.context_manager = AIUniversityContextManager()
            self.activate_researcher_context = activate_researcher_context
            
            logger.info(f"✅ AI University Context System initialized")
            logger.info(f"✅ {len(self.context_manager.researchers)} researchers available")
            
        except Exception as e:
            logger.error(f"❌ Error initializing AI University context: {e}")
            self.context_manager = None
    
    def process_user_message(self, message: str) -> dict:
        """
        Procesa mensaje del usuario y activa contexto si detecta investigador
        
        Args:
            message: Mensaje del usuario
            
        Returns:
            dict: Resultado del procesamiento con información de activación
        """
        if not self.context_manager:
            return {"status": "error", "message": "Context system not initialized"}
        
        try:
            # Detectar y activar contexto automáticamente
            result = self.activate_researcher_context(message, self.context_manager)
            
            if result["researcher_detected"]:
                self.current_researcher = result["researcher_id"]
                profile = result["researcher_profile"]
                
                activation_info = {
                    "status": "researcher_activated",
                    "researcher_name": profile.name,
                    "researcher_id": result["researcher_id"],
                    "system_prompt": result["system_prompt"],
                    "publications_to_read": result["publications_to_read"],
                    "expertise": profile.expertise,
                    "research_focus": profile.research_focus,
                    "current_projects": profile.current_projects,
                    "personality_traits": profile.personality_traits,
                    "activation_message": result["activation_message"]
                }
                
                logger.info(f"🧬 Researcher activated: {profile.name}")
                return activation_info
            
            else:
                # No se detectó investigador específico
                return {
                    "status": "no_researcher_detected",
                    "message": "Proceeding with general AI University context"
                }
                
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return {"status": "error", "message": str(e)}
    
    def get_current_researcher_info(self) -> dict:
        """Obtiene información del investigador actualmente activo"""
        if not self.current_researcher or not self.context_manager:
            return {"status": "no_active_researcher"}
        
        profile = self.context_manager.get_researcher_context(self.current_researcher)
        if profile:
            return {
                "status": "active_researcher",
                "name": profile.name,
                "research_focus": profile.research_focus,
                "expertise": profile.expertise,
                "publications": profile.key_publications
            }
        
        return {"status": "researcher_not_found"}
    
    def get_researcher_system_prompt(self, researcher_name: str = None) -> str:
        """
        Obtiene system prompt para investigador específico o activo
        
        Args:
            researcher_name: Nombre del investigador (opcional, usa activo si no se especifica)
            
        Returns:
            str: System prompt del investigador
        """
        if not self.context_manager:
            return ""
        
        if researcher_name:
            # Buscar investigador por nombre
            for researcher_id, profile in self.context_manager.researchers.items():
                if researcher_name.lower() in profile.name.lower():
                    return self.context_manager.get_system_prompt(researcher_id)
        
        elif self.current_researcher:
            # Usar investigador activo
            return self.context_manager.get_system_prompt(self.current_researcher)
        
        return ""
    
    def list_available_researchers(self) -> list:
        """Lista todos los investigadores disponibles"""
        if not self.context_manager:
            return []
        
        researchers = []
        for researcher_id, profile in self.context_manager.researchers.items():
            researchers.append({
                "id": researcher_id,
                "name": profile.name,
                "aliases": profile.aliases,
                "research_focus": profile.research_focus,
                "expertise_count": len(profile.expertise),
                "publications_count": len(profile.key_publications)
            })
        
        return researchers
    
    def clear_active_researcher(self):
        """Limpia el investigador activo"""
        self.current_researcher = None
        logger.info("🔄 Active researcher cleared")

# Instancia global para uso del agente
ai_university_integrator = AIUniversityAgentIntegrator()

def check_for_researcher_activation(user_message: str) -> dict:
    """
    Función utilitaria principal para el agente
    Verifica si necesita activar contexto de investigador
    
    Args:
        user_message: Mensaje del usuario
        
    Returns:
        dict: Información de activación o None si no se requiere
    """
    return ai_university_integrator.process_user_message(user_message)

def get_active_researcher_prompt() -> str:
    """
    Función utilitaria para obtener system prompt del investigador activo
    
    Returns:
        str: System prompt del investigador activo o string vacío
    """
    return ai_university_integrator.get_researcher_system_prompt()

def get_researcher_publications(researcher_name: str = None) -> list:
    """
    Obtiene lista de publicaciones del investigador activo o especificado
    
    Args:
        researcher_name: Nombre del investigador (opcional)
        
    Returns:
        list: Lista de publicaciones
    """
    if not ai_university_integrator.context_manager:
        return []
    
    if researcher_name:
        for researcher_id, profile in ai_university_integrator.context_manager.researchers.items():
            if researcher_name.lower() in profile.name.lower():
                return profile.key_publications
    
    elif ai_university_integrator.current_researcher:
        profile = ai_university_integrator.context_manager.get_researcher_context(
            ai_university_integrator.current_researcher
        )
        if profile:
            return profile.key_publications
    
    return []

# Ejemplo de uso
if __name__ == "__main__":
    # Test del integrador
    test_messages = [
        "Yuang, necesito revisar la integración SPACE",
        "Petrov, analiza la viabilidad comercial",
        "Priya Sharma debe revisar los KAN networks"
    ]
    
    for message in test_messages:
        print(f"\n🔍 Testing: {message}")
        result = check_for_researcher_activation(message)
        
        if result["status"] == "researcher_activated":
            print(f"✅ Activated: {result['researcher_name']}")
            print(f"📚 Publications: {len(result['publications_to_read'])}")
            
            # Test getting system prompt
            prompt = get_active_researcher_prompt()
            print(f"🧠 System prompt: {len(prompt)} characters")
            
            # Clear for next test
            ai_university_integrator.clear_active_researcher()
        
        else:
            print(f"❌ No researcher detected")
    
    print(f"\n👥 Available researchers: {len(ai_university_integrator.list_available_researchers())}")
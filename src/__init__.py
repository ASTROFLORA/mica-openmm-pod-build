# -*- coding: utf-8 -*-
"""
🧬 ASTROFLORA CORE - BACKEND MCP SERVER
Metrópolis de Antares - Sistema Cognitivo para Investigación Científica Autónoma

Este módulo inicializa el backend de Astroflora Core con soporte para:
- Modo Protocolo Estático (flujos de trabajo predefinidos)
- Modo ReAct Framework (razonamiento adaptativo)
- MCP (Model Context Protocol) para comunicación estandarizada
"""

__version__ = "1.0.0"
__author__ = "Astroflora Team"
__description__ = "Backend MCP para investigación científica autónoma"

# Exportar componentes principales migrados
try:
    from .services.agentic.agentic_gateway import AgenticToolGateway
    from .services.agentic.atomic_tools import tool_registry
    from .config.settings import settings
    from .config.mcp_config_new import mcp_config_new
    
    __all__ = [
        "AgenticToolGateway",
        "tool_registry",
        "settings",
        "mcp_config_new"
    ]
except Exception as e:
    # Manejo de errores de importación para entornos de desarrollo
    import warnings
    warnings.warn(f"Some components not available: {e}")
    __all__ = []

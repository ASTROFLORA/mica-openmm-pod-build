#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⏰ BSM CHRONOSFOLD API
Servicio para integración con ChronosFold infrastructure

Author: Alex Rodriguez (AI Systems Architecture Lab)
Date: October 10, 2025
Phase: 3.800 - BSM-Mol★ Integration
"""

import asyncio
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# === PYDANTIC MODELS ===

class ChronosFoldBootstrapStatus(BaseModel):
    """Estado del bootstrap de ChronosFold infrastructure"""
    status: str = Field(..., description="ready, uninitialized, error")
    handles: Dict[str, bool] = Field(
        default_factory=lambda: {
            "neo4j": False,
            "milvus": False,
            "object_storage": False
        }
    )
    bootstrap_time: Optional[str] = None
    last_check: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    errors: List[str] = Field(default_factory=list)


class ChronosFoldConfig(BaseModel):
    """Configuración de ChronosFold"""
    enabled: bool = True
    bootstrap_on_startup: bool = False
    neo4j_uri: Optional[str] = None
    milvus_host: Optional[str] = None
    object_storage_endpoint: Optional[str] = None


# === CHRONOSFOLD API ===

class ChronosFoldAPI:
    """
    API para integración con ChronosFold infrastructure
    
    Proporciona:
    - Estado del bootstrap
    - Trigger de bootstrap lazy
    - Verificación de handles (Neo4j, Milvus, Object Storage)
    - Configuración de ChronosFold
    """
    
    def __init__(self, config: Optional[ChronosFoldConfig] = None):
        """
        Inicializa ChronosFold API
        
        Args:
            config: Configuración de ChronosFold (opcional)
        """
        self.config = config or ChronosFoldConfig()
        self._bootstrap_status: Optional[ChronosFoldBootstrapStatus] = None
        self._handles: Dict[str, Any] = {}
        logger.info("⏰ ChronosFoldAPI initialized")
    
    async def get_bootstrap_status(self) -> ChronosFoldBootstrapStatus:
        """
        Obtiene estado del bootstrap de ChronosFold
        
        Verifica:
        - Estado general (ready/uninitialized/error)
        - Handles disponibles (Neo4j, Milvus, Object Storage)
        - Errores si existen
        
        Returns:
            ChronosFoldBootstrapStatus
        """
        try:
            # Intentar importar ChronosFold infrastructure
            status = await self._check_infrastructure()
            
            self._bootstrap_status = status
            logger.info(f"📊 ChronosFold bootstrap status: {status.status}")
            
            return status
        
        except Exception as e:
            logger.error(f"❌ Error checking ChronosFold bootstrap status: {e}")
            return ChronosFoldBootstrapStatus(
                status="error",
                errors=[str(e)]
            )
    
    async def _check_infrastructure(self) -> ChronosFoldBootstrapStatus:
        """Verifica estado de la infraestructura ChronosFold"""
        try:
            # Intentar importar módulo ChronosFold
            try:
                # Importación relativa desde chronosfold_scaffold
                import sys
                from pathlib import Path
                
                # Agregar chronosfold_scaffold al path si no está
                scaffold_path = Path(__file__).parent.parent.parent / "chronosfold_scaffold" / "src"
                if str(scaffold_path) not in sys.path:
                    sys.path.insert(0, str(scaffold_path))
                
                from chronosfold.infrastructure.bridge import get_infrastructure_handles
                
                # Obtener handles
                handles = get_infrastructure_handles()
                
                if handles is None:
                    return ChronosFoldBootstrapStatus(
                        status="uninitialized",
                        handles={
                            "neo4j": False,
                            "milvus": False,
                            "object_storage": False
                        }
                    )
                
                # Verificar cada handle
                neo4j_ok = handles.get("neo4j") is not None
                milvus_ok = handles.get("milvus") is not None
                storage_ok = handles.get("object_storage") is not None
                
                all_ready = neo4j_ok and milvus_ok and storage_ok
                
                status_str = "ready" if all_ready else "partial"
                
                return ChronosFoldBootstrapStatus(
                    status=status_str,
                    handles={
                        "neo4j": neo4j_ok,
                        "milvus": milvus_ok,
                        "object_storage": storage_ok
                    },
                    bootstrap_time=datetime.utcnow().isoformat() if all_ready else None
                )
            
            except ImportError as ie:
                logger.warning(f"⚠️ ChronosFold infrastructure not available: {ie}")
                return ChronosFoldBootstrapStatus(
                    status="uninitialized",
                    handles={
                        "neo4j": False,
                        "milvus": False,
                        "object_storage": False
                    },
                    errors=[f"ChronosFold module not found: {str(ie)}"]
                )
        
        except Exception as e:
            logger.error(f"❌ Error checking infrastructure: {e}")
            return ChronosFoldBootstrapStatus(
                status="error",
                errors=[str(e)]
            )
    
    async def trigger_bootstrap(self) -> ChronosFoldBootstrapStatus:
        """
        Trigger lazy bootstrap de ChronosFold infrastructure
        
        Ejecuta:
        1. Importa módulo de bootstrap
        2. Llama a bootstrap_infrastructure()
        3. Verifica handles creados
        
        Returns:
            ChronosFoldBootstrapStatus después del bootstrap
        """
        try:
            logger.info("🚀 Triggering ChronosFold bootstrap...")
            
            # Importar módulo de bootstrap
            import sys
            from pathlib import Path
            
            scaffold_path = Path(__file__).parent.parent.parent / "chronosfold_scaffold" / "src"
            if str(scaffold_path) not in sys.path:
                sys.path.insert(0, str(scaffold_path))
            
            from chronosfold.infrastructure.bridge import bootstrap_infrastructure
            
            # Ejecutar bootstrap
            handles = bootstrap_infrastructure()
            
            if handles:
                self._handles = handles
                logger.info("✅ ChronosFold bootstrap completed")
            else:
                logger.warning("⚠️ Bootstrap returned no handles")
            
            # Verificar estado después del bootstrap
            status = await self.get_bootstrap_status()
            
            return status
        
        except Exception as e:
            logger.error(f"❌ Error triggering ChronosFold bootstrap: {e}")
            return ChronosFoldBootstrapStatus(
                status="error",
                errors=[f"Bootstrap failed: {str(e)}"]
            )
    
    async def get_config(self) -> ChronosFoldConfig:
        """
        Obtiene configuración actual de ChronosFold
        
        Returns:
            ChronosFoldConfig
        """
        return self.config
    
    async def update_config(self, config: ChronosFoldConfig) -> ChronosFoldConfig:
        """
        Actualiza configuración de ChronosFold
        
        Args:
            config: Nueva configuración
        
        Returns:
            ChronosFoldConfig actualizada
        """
        self.config = config
        logger.info("✅ ChronosFold config updated")
        return self.config
    
    def is_ready(self) -> bool:
        """
        Verifica si ChronosFold está listo
        
        Returns:
            True si status == "ready"
        """
        if self._bootstrap_status is None:
            return False
        return self._bootstrap_status.status == "ready"
    
    def get_handles(self) -> Dict[str, Any]:
        """
        Obtiene handles de infraestructura
        
        Returns:
            Dict con handles (neo4j, milvus, object_storage)
        """
        return self._handles


# === FACTORY FUNCTION ===

async def create_chronosfold_api(config: Optional[ChronosFoldConfig] = None) -> ChronosFoldAPI:
    """
    Factory function para crear ChronosFoldAPI
    
    Args:
        config: Configuración de ChronosFold (opcional)
    
    Returns:
        ChronosFoldAPI inicializado
    """
    api = ChronosFoldAPI(config)
    
    # Si bootstrap_on_startup está habilitado, ejecutar bootstrap
    if api.config.bootstrap_on_startup:
        logger.info("🚀 Bootstrap on startup enabled, triggering bootstrap...")
        await api.trigger_bootstrap()
    
    logger.info("✅ ChronosFoldAPI created")
    return api

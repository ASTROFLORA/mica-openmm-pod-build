"""
Cache de Redis simple y funcional para la memoria semántica de MICA
"""

import asyncio
import json
import time
from typing import Optional, Dict, Any
import os

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class SimpleRedisCache:
    """Cache de Redis simple para memoria semántica."""
    
    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "")
        if not self.redis_url:
            import logging
            logging.getLogger(__name__).warning("REDIS_URL not set — cache disabled")
        self.client = None
        self.connected = False
        
    async def initialize(self) -> bool:
        """Inicializa la conexión a Redis."""
        if not REDIS_AVAILABLE:
            print("❌ Redis no disponible - usando cache en memoria")
            return False
            
        try:
            self.client = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
            await self.client.ping()
            self.connected = True
            print(f"✅ Redis conectado: {self.redis_url}")
            return True
        except Exception as e:
            print(f"❌ Error conectando a Redis: {e}")
            self.connected = False
            return False
    
    async def cache_driver_results(self, key: str, data: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        """Cachea resultados del driver."""
        if not self.connected:
            return False
            
        try:
            # Serializar datos
            serialized_data = json.dumps(data, default=str)
            
            # Guardar en Redis
            if ttl:
                await self.client.setex(key, ttl, serialized_data)
            else:
                await self.client.set(key, serialized_data)
                
            print(f"💾 Datos guardados en Redis: {key}")
            return True
            
        except Exception as e:
            print(f"❌ Error guardando en Redis: {e}")
            return False
    
    async def get_cached_analysis(self, key: str, analysis_type: str = "driver") -> Optional[Dict[str, Any]]:
        """Recupera datos cacheados."""
        if not self.connected:
            return None
            
        try:
            # Buscar por key
            data = await self.client.get(key)
            if data:
                return json.loads(data)
            return None
            
        except Exception as e:
            print(f"❌ Error recuperando de Redis: {e}")
            return None
    
    async def close(self) -> bool:
        """Cierra la conexión a Redis."""
        if self.client:
            try:
                await self.client.close()
                self.connected = False
                print("🔌 Conexión a Redis cerrada")
                return True
            except Exception as e:
                print(f"❌ Error cerrando Redis: {e}")
                return False
        return True

# Función de conveniencia para crear cache
async def create_simple_redis_cache(redis_url: str = None) -> SimpleRedisCache:
    """Crea una instancia del cache de Redis simple."""
    cache = SimpleRedisCache(redis_url)
    await cache.initialize()
    return cache

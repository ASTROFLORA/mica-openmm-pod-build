"""
RunPod Endpoint Deployment Script

Crea y configura endpoints de BioDynamo en RunPod Serverless.

Usage:
    python deploy_runpod_endpoint.py --create
    python deploy_runpod_endpoint.py --status
    python deploy_runpod_endpoint.py --test

Autor: MICA Infrastructure Team
Fecha: 12 de Noviembre, 2025
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Load .env BEFORE importing other modules
from dotenv import load_dotenv
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.runpod_client import (
    RunPodClient,
    check_endpoint_status,
    submit_biodynamo_job,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def create_endpoint_via_console():
    """
    Instrucciones para crear endpoint en RunPod Console
    
    No podemos crear endpoints via API (requiere console web),
    pero podemos validar configuración.
    """
    print("=" * 70)
    print(" RUNPOD ENDPOINT CREATION GUIDE")
    print("=" * 70)
    print()
    print("⚠️  IMPORTANTE: Los endpoints se crean via RunPod Console web.")
    print("   API solo permite enviar jobs a endpoints existentes.")
    print()
    print("📋 PASOS PARA CREAR ENDPOINT:")
    print()
    print("1. Ir a https://www.runpod.io/console/serverless")
    print()
    print("2. Click 'New Endpoint' → Template: Custom")
    print()
    print("3. Configuración recomendada:")
    print("   - Name: biodynamo-production")
    print("   - Container Image: <TU_REGISTRY>/biodynamo-worker:latest")
    print("     (ejemplo: ghcr.io/your-org/biodynamo-worker:latest)")
    print()
    print("4. GPU Configuration:")
    print("   - GPU Type: NVIDIA A40 (48GB) o RTX 4090 (24GB)")
    print("   - Min Workers: 0 (scale-to-zero)")
    print("   - Max Workers: 10 (ajustar según presupuesto)")
    print("   - Idle Timeout: 5 seconds")
    print()
    print("5. Advanced Settings:")
    print("   - Execution Timeout: 600s (10 minutos)")
    print("   - Container Disk: 20GB")
    print("   - Volume: /workspace (persistent, 50GB)")
    print()
    print("6. Environment Variables:")
    print("   - PYTHONUNBUFFERED=1")
    print("   - CHECKPOINT_DIR=/workspace/checkpoints")
    print()
    print("7. Click 'Deploy' y copiar ENDPOINT_ID")
    print()
    print("8. Setear en tu shell:")
    print("   export RUNPOD_ENDPOINT_ID=<endpoint-id-from-console>")
    print()
    print("=" * 70)
    print()
    
    # Validar API key
    api_key = os.getenv("RUNPOD_API_KEY")
    if not api_key:
        print("❌ ERROR: RUNPOD_API_KEY no está configurada")
        print()
        print("Para obtener tu API key:")
        print("1. Ir a https://www.runpod.io/console/user/settings")
        print("2. API Keys → Create API Key")
        print("3. Copiar y ejecutar:")
        print("   export RUNPOD_API_KEY=<tu-api-key>")
        print()
        return False
    else:
        print(f"✅ RUNPOD_API_KEY configurada (length: {len(api_key)})")
    
    # Validar endpoint ID
    endpoint_id = os.getenv("RUNPOD_ENDPOINT_ID")
    if not endpoint_id:
        print("⚠️  RUNPOD_ENDPOINT_ID no está configurada")
        print("   Configúrala después de crear el endpoint en console.")
        print()
        return False
    else:
        print(f"✅ RUNPOD_ENDPOINT_ID configurada: {endpoint_id}")
        print()
        print("🔍 Verificando endpoint...")
        
        try:
            async with RunPodClient() as client:
                health = await client.get_endpoint_health()
                print(f"✅ Endpoint accesible!")
                print(f"   Workers running: {health.workers_running}")
                print(f"   Workers idle: {health.workers_idle}")
                print(f"   Jobs in queue: {health.jobs_in_queue}")
                print()
                return True
        except Exception as e:
            print(f"❌ Error accediendo endpoint: {e}")
            print("   Verifica que el ENDPOINT_ID sea correcto.")
            print()
            return False


async def show_endpoint_status():
    """Mostrar estado del endpoint"""
    print("=" * 70)
    print(" RUNPOD ENDPOINT STATUS")
    print("=" * 70)
    print()
    
    try:
        status = await check_endpoint_status()
        
        print(f"Endpoint ID: {status['endpoint_id']}")
        print(f"Health Status: {status['health_status']}")
        print()
        
        print("📊 JOBS:")
        for key, value in status['jobs'].items():
            print(f"   {key}: {value}")
        print()
        
        print("🖥️  WORKERS:")
        for key, value in status['workers'].items():
            print(f"   {key}: {value}")
        print()
        
        print("💡 RECOMMENDATIONS:")
        for rec in status['recommendations']:
            print(f"   • {rec}")
        print()
        
        print("=" * 70)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        print()
        print("Verifica que RUNPOD_API_KEY y RUNPOD_ENDPOINT_ID estén configuradas.")
        print()


async def test_endpoint():
    """Test endpoint con job simple"""
    print("=" * 70)
    print(" RUNPOD ENDPOINT TEST")
    print("=" * 70)
    print()
    
    # Primero verificar health
    try:
        status = await check_endpoint_status()
        print(f"✅ Endpoint {status['endpoint_id']} accessible")
        print()
    except Exception as e:
        print(f"❌ Endpoint not accessible: {e}")
        return
    
    # Submit test job (sync para resultado inmediato)
    print("📤 Submitting test job (sync)...")
    print()
    
    test_input = {
        "protein_pdb": "1ABC",
        "simulation_time_ns": 1,  # 1 ns mínimo para test rápido
        "temperature_k": 310.0,
        "test_mode": True,  # Handler puede detectar esto
    }
    
    print(f"Input: {json.dumps(test_input, indent=2)}")
    print()
    
    try:
        async with RunPodClient() as client:
            # Usar runsync para test rápido
            print("⏳ Waiting for result (max 60s)...")
            job = await client.submit_sync_job(
                input_data=test_input,
                wait=60000,  # 60 segundos
            )
            
            print()
            print(f"✅ Job completed!")
            print(f"   Job ID: {job.id}")
            print(f"   Status: {job.status}")
            print(f"   Queue delay: {job.delay_time}ms")
            print(f"   Execution time: {job.execution_time}ms")
            print()
            print("📊 Output:")
            print(json.dumps(job.output, indent=2))
            print()
            print("=" * 70)
            
    except Exception as e:
        print(f"❌ Test failed: {e}")
        print()
        print("Posibles causas:")
        print("   • No hay workers disponibles (check /health)")
        print("   • Handler tiene errores (check logs en RunPod console)")
        print("   • Timeout muy corto para cold start")
        print()


async def test_async_job():
    """Test job asíncrono con polling"""
    print("=" * 70)
    print(" RUNPOD ASYNC JOB TEST")
    print("=" * 70)
    print()
    
    test_input = {
        "protein_pdb": "1ABC",
        "simulation_time_ns": 10,  # 10 ns
        "temperature_k": 310.0,
    }
    
    print(f"Input: {json.dumps(test_input, indent=2)}")
    print()
    
    try:
        async with RunPodClient() as client:
            # Submit async job
            print("📤 Submitting async job...")
            job = await client.submit_job(
                input_data=test_input,
                execution_timeout=300000,  # 5 min timeout
            )
            
            print(f"✅ Job submitted: {job.id}")
            print(f"   Status: {job.status}")
            print()
            
            # Poll until complete
            print("⏳ Polling for completion (max 5 min)...")
            result = await client.poll_until_complete(
                job.id,
                poll_interval=5.0,  # Check every 5s
                max_wait=300.0,  # 5 min max
            )
            
            print()
            print(f"✅ Job {result.id} completed!")
            print(f"   Final status: {result.status}")
            print(f"   Queue delay: {result.delay_time}ms")
            print(f"   Execution time: {result.execution_time}ms")
            print()
            print("📊 Output:")
            print(json.dumps(result.output, indent=2))
            print()
            print("=" * 70)
            
    except TimeoutError as e:
        print(f"⏱️  Timeout: {e}")
        print()
    except Exception as e:
        print(f"❌ Error: {e}")
        print()


async def build_and_push_docker():
    """Instrucciones para construir y publicar imagen Docker"""
    print("=" * 70)
    print(" DOCKER BUILD & PUSH GUIDE")
    print("=" * 70)
    print()
    print("📦 CONSTRUCCIÓN DE IMAGEN:")
    print()
    print("1. Ubicarse en directorio del proyecto:")
    print("   cd astroflora-core-feature-spectra-worker-integration-1")
    print()
    print("2. Construir imagen:")
    print("   docker build -f docker/Dockerfile.runpod-dynamo \\")
    print("     -t biodynamo-worker:latest \\")
    print("     .")
    print()
    print("3. Testear localmente:")
    print("   docker run --rm \\")
    print("     -e RUNPOD_API_KEY=$RUNPOD_API_KEY \\")
    print("     biodynamo-worker:latest \\")
    print("     python -c \"from workers.dynamo.worker import DynamoWorker; print('OK')\"")
    print()
    print("4. Etiquetar para registry:")
    print("   # GitHub Container Registry")
    print("   docker tag biodynamo-worker:latest \\")
    print("     ghcr.io/YOUR_USERNAME/biodynamo-worker:latest")
    print()
    print("   # Docker Hub")
    print("   docker tag biodynamo-worker:latest \\")
    print("     YOUR_USERNAME/biodynamo-worker:latest")
    print()
    print("5. Login a registry:")
    print("   # GitHub")
    print("   echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin")
    print()
    print("   # Docker Hub")
    print("   docker login -u YOUR_USERNAME")
    print()
    print("6. Push imagen:")
    print("   docker push ghcr.io/YOUR_USERNAME/biodynamo-worker:latest")
    print()
    print("7. Verificar en:")
    print("   https://github.com/YOUR_USERNAME?tab=packages")
    print("   o https://hub.docker.com/u/YOUR_USERNAME")
    print()
    print("=" * 70)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="RunPod Endpoint Deployment & Testing"
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Show endpoint creation guide"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show endpoint status"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test endpoint with sync job"
    )
    parser.add_argument(
        "--test-async",
        action="store_true",
        help="Test endpoint with async job"
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Show Docker build & push guide"
    )
    
    args = parser.parse_args()
    
    if args.create:
        asyncio.run(create_endpoint_via_console())
    elif args.status:
        asyncio.run(show_endpoint_status())
    elif args.test:
        asyncio.run(test_endpoint())
    elif args.test_async:
        asyncio.run(test_async_job())
    elif args.docker:
        asyncio.run(build_and_push_docker())
    else:
        parser.print_help()
        print()
        print("Example workflow:")
        print("  1. python deploy_runpod_endpoint.py --docker")
        print("  2. python deploy_runpod_endpoint.py --create")
        print("  3. python deploy_runpod_endpoint.py --status")
        print("  4. python deploy_runpod_endpoint.py --test")
        print()


if __name__ == "__main__":
    main()

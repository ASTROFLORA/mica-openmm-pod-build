"""
demo_vast_gcs.py - Demo of Vast.ai + GCS Integration

This script demonstrates the complete workflow:
1. Create user storage bucket
2. Upload PDB file
3. Launch MD job on Vast.ai with GCS mount
4. Monitor progress
5. Download results

Usage:
    python demo_vast_gcs.py --user-id myuser --pdb-file protein.pdb
    
Environment Variables:
    VAST_API_KEY: Vast.ai API key
    GCS_CREDENTIALS_PATH: Path to GCS service account JSON
    GCP_PROJECT: GCP project ID

Author: MICA Team
Date: December 2024
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from mica.infrastructure.storage import UserStorageManager, UserQuota
from mica.infrastructure.orchestration import VastGCSOrchestrator, MDJobConfig
from mica.infrastructure.providers.base_provider import GPUType


async def demo_storage_only(
    user_id: str,
    credentials_path: str,
    project_id: str,
):
    """Demo: Create user bucket and generate mount script."""
    print("\n" + "="*60)
    print("🗄️  MICA Storage Demo")
    print("="*60 + "\n")
    
    # Initialize storage manager
    print(f"📦 Initializing storage manager...")
    print(f"   Project: {project_id}")
    print(f"   Credentials: {credentials_path}")
    
    storage = UserStorageManager(
        project_id=project_id,
        credentials_path=credentials_path,
        region="us-central1",
    )
    
    # Create user bucket
    print(f"\n👤 Creating bucket for user: {user_id}")
    bucket = await storage.provision_user_bucket(user_id)
    print(f"   ✓ Bucket: {bucket.bucket_url}")
    print(f"   ✓ Region: {bucket.region}")
    
    # Show quota
    print(f"\n📊 Quota Configuration:")
    print(f"   Max storage: {bucket.quota.max_storage_gb} GB")
    print(f"   Max input: {bucket.quota.max_input_gb} GB")
    print(f"   Max output: {bucket.quota.max_output_gb} GB")
    print(f"   Scratch TTL: {bucket.quota.scratch_ttl_hours} hours")
    
    # Generate mount script
    print(f"\n📝 Generated GCS Mount Script:")
    script = storage.generate_mount_script(bucket)
    print("-"*40)
    # Show first 30 lines
    lines = script.split("\n")[:30]
    for line in lines:
        print(f"   {line}")
    print("   ...")
    print("-"*40)
    
    # Save script to file
    script_path = Path(f"vast_mount_script_{user_id}.sh")
    script_path.write_text(script, encoding='utf-8')
    print(f"\n💾 Script saved to: {script_path}")
    
    # Generate signed upload URL
    print(f"\n🔗 Generating signed upload URL...")
    try:
        upload_url = await storage.generate_signed_url(
            bucket, "input", "test_upload.pdb",
            expiration_hours=1,
            for_upload=True
        )
        print(f"   Upload URL (1h expiry):")
        print(f"   {upload_url[:80]}...")
    except Exception as e:
        print(f"   (Signed URL generation requires additional permissions: {e})")
    
    print("\n✅ Storage demo complete!")
    return bucket


async def demo_full_pipeline(
    user_id: str,
    pdb_file: str,
    credentials_path: str,
    project_id: str,
    vast_api_key: str,
):
    """Demo: Full pipeline from PDB upload to MD results."""
    print("\n" + "="*60)
    print("🧬 MICA Full Pipeline Demo")
    print("="*60 + "\n")
    
    # Initialize orchestrator
    print(f"🚀 Initializing orchestrator...")
    orchestrator = VastGCSOrchestrator(
        vast_api_key=vast_api_key,
        gcs_credentials_path=credentials_path,
        gcs_project=project_id,
    )
    
    # Check user storage
    print(f"\n📦 Checking user storage...")
    usage = await orchestrator.get_user_usage(user_id)
    print(f"   Bucket: gs://{usage['bucket']}")
    print(f"   Used: {usage['usage_gb'].get('total', 0):.2f} GB ({usage['percent_used']:.1f}%)")
    
    # Check for GPU offers
    print(f"\n🔍 Searching for GPU offers...")
    offers = await orchestrator.vast.search_offers(
        gpu_type=GPUType.RTX_4090,
        max_price=0.50,
    )
    print(f"   Found {len(offers)} offers")
    if offers:
        best = offers[0]
        print(f"   Best offer: {best.gpu_type.value} @ ${best.price_per_hour:.2f}/hr")
        print(f"   GPU Memory: {best.gpu_memory_gb:.0f} GB")
        print(f"   Region: {best.region or 'Unknown'}")
    
    # Configure job
    config = MDJobConfig(
        pdb_file=pdb_file,
        steps=10000,  # Short test: ~20ps
        temperature_k=300,
        gpu_type=GPUType.RTX_4090,
        max_price_per_hour=0.50,
    )
    
    print(f"\n⚙️  Job Configuration:")
    print(f"   PDB file: {config.pdb_file}")
    print(f"   Steps: {config.steps}")
    print(f"   Temperature: {config.temperature_k} K")
    print(f"   GPU: {config.gpu_type.value}")
    print(f"   Max price: ${config.max_price_per_hour}/hr")
    
    # Run job (dry run mode - just show what would happen)
    print(f"\n⏸️  DRY RUN MODE - Not actually launching job")
    print(f"   To run for real, remove the dry_run check in the code")
    
    # Show what the startup script would look like
    bucket = await orchestrator.storage.provision_user_bucket(user_id)
    startup = orchestrator.storage.generate_openmm_startup_script(
        bucket=bucket,
        pdb_filename=config.pdb_file,
        simulation_params={"steps": config.steps, "temperature": config.temperature_k}
    )
    
    print(f"\n📝 Startup script preview (first 50 lines):")
    print("-"*40)
    for line in startup.split("\n")[:50]:
        print(f"   {line}")
    print("   ...")
    print("-"*40)
    
    print("\n✅ Pipeline demo complete!")


async def list_gpu_prices():
    """Show current GPU prices on Vast.ai."""
    print("\n" + "="*60)
    print("💰 Vast.ai GPU Pricing")
    print("="*60 + "\n")
    
    from mica.infrastructure.providers.vast_provider import VastProvider
    
    vast = VastProvider()
    
    gpu_types = [
        GPUType.RTX_4090,
        GPUType.L40S,
        GPUType.A100_40GB,
        GPUType.H100_80GB,
    ]
    
    print(f"{'GPU Type':<20} {'Min $/hr':<12} {'Max $/hr':<12} {'# Offers':<10}")
    print("-"*54)
    
    for gpu_type in gpu_types:
        try:
            offers = await vast.search_offers(gpu_type, max_price=5.0)
            if offers:
                min_price = min(o.price_per_hour for o in offers)
                max_price = max(o.price_per_hour for o in offers)
                print(f"{gpu_type.value:<20} ${min_price:<11.2f} ${max_price:<11.2f} {len(offers):<10}")
            else:
                print(f"{gpu_type.value:<20} {'No offers':<24}")
        except Exception as e:
            print(f"{gpu_type.value:<20} {'Error: ' + str(e)[:30]:<24}")


def main():
    parser = argparse.ArgumentParser(
        description="MICA Vast.ai + GCS Integration Demo"
    )
    parser.add_argument(
        "--mode",
        choices=["storage", "pipeline", "prices"],
        default="storage",
        help="Demo mode: storage (bucket only), pipeline (full MD), prices (GPU listing)"
    )
    parser.add_argument(
        "--user-id",
        default="demo_user",
        help="User identifier for bucket creation"
    )
    parser.add_argument(
        "--pdb-file",
        default="protein.pdb",
        help="PDB filename in bucket/input/ for MD simulation"
    )
    parser.add_argument(
        "--gcs-credentials",
        default=os.environ.get("GCS_CREDENTIALS_PATH", "C:\\Users\\busta\\Downloads\\googlejson.json"),
        help="Path to GCS service account JSON"
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT", "dark-yen-476115-j4"),
        help="GCP project ID"
    )
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("🧬 MICA Cloud Infrastructure Demo")
    print("   Vast.ai GPUs + Google Cloud Storage")
    print("="*60)
    
    if args.mode == "storage":
        asyncio.run(demo_storage_only(
            user_id=args.user_id,
            credentials_path=args.gcs_credentials,
            project_id=args.project,
        ))
    elif args.mode == "pipeline":
        vast_key = os.environ.get("VAST_API_KEY")
        if not vast_key:
            print("\n❌ VAST_API_KEY environment variable required for pipeline demo")
            sys.exit(1)
        asyncio.run(demo_full_pipeline(
            user_id=args.user_id,
            pdb_file=args.pdb_file,
            credentials_path=args.gcs_credentials,
            project_id=args.project,
            vast_api_key=vast_key,
        ))
    elif args.mode == "prices":
        asyncio.run(list_gpu_prices())


if __name__ == "__main__":
    main()

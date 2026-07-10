"""
vast_audit.py - Vast.ai Instance Auditor & Manager

Script de línea de comandos para auditar y gestionar instancias de Vast.ai.

Funcionalidades:
- Listar todas las instancias activas
- Ver balance de cuenta
- Buscar ofertas de GPU
- Crear/destruir instancias
- Calcular costos acumulados
- Detectar instancias huérfanas (running sin job)

Uso:
    python vast_audit.py --status          # Ver estado de instancias
    python vast_audit.py --balance         # Ver balance de cuenta
    python vast_audit.py --search L40S     # Buscar ofertas
    python vast_audit.py --destroy ID      # Destruir instancia
    python vast_audit.py --destroy-all     # Destruir todas las instancias
    python vast_audit.py --health          # Health check completo

Autor: MICA Infrastructure Team
Fecha: Diciembre 2024
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path


# ============================================================================
# Colors for terminal output
# ============================================================================

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def color(text: str, c: str) -> str:
    """Apply color to text."""
    return f"{c}{text}{Colors.ENDC}"


# ============================================================================
# Vast.ai CLI Wrapper
# ============================================================================

class VastCLI:
    """Direct Vast.ai CLI wrapper for audit operations."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("VAST_API_KEY")
        
        # Set API key if provided
        if self.api_key:
            self._run(["set", "api-key", self.api_key])
    
    def _run(
        self, 
        args: List[str], 
        timeout: float = 60.0
    ) -> Tuple[bool, Any]:
        """Run vastai CLI command."""
        cmd = ["vastai"] + args
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            
            if result.returncode != 0:
                return False, result.stderr.strip()
            
            # Try to parse as JSON
            try:
                return True, json.loads(result.stdout)
            except json.JSONDecodeError:
                return True, result.stdout.strip()
                
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except FileNotFoundError:
            return False, "vastai CLI not installed. Run: pip install vastai"
    
    def get_instances(self) -> List[Dict[str, Any]]:
        """Get all instances."""
        success, data = self._run(["show", "instances", "--raw"])
        if success and isinstance(data, list):
            return data
        return []
    
    def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get single instance details."""
        success, data = self._run(["show", "instance", instance_id, "--raw"])
        if success and isinstance(data, dict):
            return data
        return None
    
    def get_user(self) -> Dict[str, Any]:
        """Get user/account info."""
        success, data = self._run(["show", "user", "--raw"])
        if success and isinstance(data, dict):
            return data
        return {}
    
    def search_offers(
        self, 
        gpu_type: str, 
        max_price: Optional[float] = None,
        min_reliability: float = 0.95,
    ) -> List[Dict[str, Any]]:
        """Search GPU offers."""
        query_parts = [f"gpu_name~={gpu_type}"]
        
        if max_price:
            query_parts.append(f"dph_total<={max_price}")
        
        query_parts.append(f"reliability>={min_reliability}")
        query_parts.append("cuda_vers>=12.0")
        query_parts.append("verified=True")
        
        query = " ".join(query_parts)
        success, data = self._run(["search", "offers", query, "--raw"])
        
        if success and isinstance(data, list):
            # Sort by price
            data.sort(key=lambda x: x.get("dph_total", float("inf")))
            return data
        return []
    
    def destroy_instance(self, instance_id: str) -> bool:
        """Destroy an instance."""
        success, _ = self._run(["destroy", "instance", instance_id])
        return success
    
    def create_instance(
        self,
        offer_id: str,
        docker_image: str,
        disk_gb: int = 100,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str]:
        """Create an instance."""
        args = [
            "create", "instance",
            str(offer_id),
            "--image", docker_image,
            "--disk", str(disk_gb),
        ]
        
        if env_vars:
            for key, value in env_vars.items():
                args.extend(["--env", f"{key}={value}"])
        
        success, data = self._run(args, timeout=120)
        
        if success:
            if isinstance(data, dict) and data.get("success"):
                return True, str(data.get("new_contract", ""))
            elif isinstance(data, str):
                # Try to extract ID
                import re
                match = re.search(r"(\d+)", data)
                if match:
                    return True, match.group(1)
        
        return False, str(data)


# ============================================================================
# Audit Functions
# ============================================================================

def print_banner():
    """Print audit banner."""
    print(color("""
╔═══════════════════════════════════════════════════════════════════════╗
║             VAST.AI INFRASTRUCTURE AUDIT - MICA                       ║
║                    Instance Management Console                         ║
╚═══════════════════════════════════════════════════════════════════════╝
""", Colors.CYAN))


def print_account_info(cli: VastCLI):
    """Print account information."""
    print(color("\n📊 ACCOUNT STATUS", Colors.BOLD))
    print("=" * 60)
    
    user = cli.get_user()
    
    if user:
        credit = user.get("credit", 0)
        email = user.get("email", "N/A")
        verified = "✓" if user.get("verified", False) else "✗"
        
        print(f"  Email:      {email}")
        print(f"  Verified:   {verified}")
        print(f"  Balance:    {color(f'${credit:.2f}', Colors.GREEN if credit > 5 else Colors.RED)}")
        
        # Warn if low balance
        if credit < 5:
            print(color("  ⚠️  LOW BALANCE - Add funds to avoid interruption!", Colors.YELLOW))
    else:
        print(color("  ❌ Failed to get account info", Colors.RED))


def print_instances(cli: VastCLI, verbose: bool = False):
    """Print all instances with status."""
    print(color("\n🖥️  ACTIVE INSTANCES", Colors.BOLD))
    print("=" * 80)
    
    instances = cli.get_instances()
    
    if not instances:
        print(color("  No active instances found.", Colors.YELLOW))
        return
    
    total_cost_per_hour = 0.0
    
    for inst in instances:
        instance_id = inst.get("id", "?")
        status = inst.get("actual_status", "unknown")
        gpu_name = inst.get("gpu_name", "?")
        num_gpus = inst.get("num_gpus", 1)
        price = inst.get("dph_total", 0)
        ssh_host = inst.get("ssh_host", "")
        ssh_port = inst.get("ssh_port", 22)
        
        # Status color
        status_colors = {
            "running": Colors.GREEN,
            "loading": Colors.YELLOW,
            "created": Colors.YELLOW,
            "exited": Colors.RED,
            "error": Colors.RED,
        }
        status_color = status_colors.get(status, Colors.YELLOW)
        
        # Calculate runtime
        start_time = inst.get("start_date")
        runtime_str = "N/A"
        cost_so_far = 0.0
        if start_time:
            try:
                start_dt = datetime.fromtimestamp(start_time)
                runtime = datetime.now() - start_dt
                hours = runtime.total_seconds() / 3600
                cost_so_far = hours * price
                runtime_str = f"{int(hours)}h {int(runtime.seconds % 3600 / 60)}m"
            except:
                pass
        
        total_cost_per_hour += price
        
        # Print instance info
        print(f"\n  {color(f'Instance #{instance_id}', Colors.CYAN)}")
        print(f"    Status:    {color(status.upper(), status_color)}")
        print(f"    GPU:       {gpu_name} x{num_gpus}")
        print(f"    Price:     ${price:.4f}/hr")
        print(f"    Runtime:   {runtime_str}")
        print(f"    Cost:      ${cost_so_far:.2f}")
        
        if ssh_host:
            print(f"    SSH:       ssh root@{ssh_host} -p {ssh_port}")
        
        if verbose:
            print(f"    CUDA:      {inst.get('cuda_vers', 'N/A')}")
            print(f"    Disk:      {inst.get('disk_space', 0):.0f} GB")
            print(f"    Location:  {inst.get('geolocation', 'N/A')}")
    
    # Summary
    print("\n" + "=" * 80)
    print(f"  {color('SUMMARY', Colors.BOLD)}")
    print(f"    Total Instances:   {len(instances)}")
    print(f"    Burn Rate:         ${total_cost_per_hour:.4f}/hr = ${total_cost_per_hour * 24:.2f}/day")
    
    running = sum(1 for i in instances if i.get("actual_status") == "running")
    print(f"    Running:           {running}/{len(instances)}")


def search_offers(cli: VastCLI, gpu_type: str, max_price: Optional[float] = None, limit: int = 10):
    """Search and display GPU offers."""
    print(color(f"\n🔍 SEARCHING OFFERS: {gpu_type}", Colors.BOLD))
    print("=" * 80)
    
    offers = cli.search_offers(gpu_type, max_price)
    
    if not offers:
        print(color(f"  No offers found for {gpu_type}", Colors.YELLOW))
        return
    
    print(f"  Found {len(offers)} offers. Showing top {min(limit, len(offers))}:\n")
    
    for i, offer in enumerate(offers[:limit]):
        offer_id = offer.get("id", "?")
        gpu_name = offer.get("gpu_name", "?")
        num_gpus = offer.get("num_gpus", 1)
        gpu_ram = offer.get("gpu_ram", 0) / 1024  # MB to GB
        price = offer.get("dph_total", 0)
        reliability = offer.get("reliability", 0) * 100
        location = offer.get("geolocation", "?")
        
        price_color = Colors.GREEN if price < 1.0 else (Colors.YELLOW if price < 2.0 else Colors.RED)
        
        print(f"  {i+1}. {color(f'Offer #{offer_id}', Colors.CYAN)}")
        print(f"     GPU:         {gpu_name} x{num_gpus} ({gpu_ram:.0f} GB VRAM)")
        print(f"     Price:       {color(f'${price:.4f}/hr', price_color)}")
        print(f"     Reliability: {reliability:.1f}%")
        print(f"     Location:    {location}")
        print()


def destroy_all_instances(cli: VastCLI, force: bool = False):
    """Destroy all instances."""
    instances = cli.get_instances()
    
    if not instances:
        print(color("  No instances to destroy.", Colors.YELLOW))
        return
    
    print(color(f"\n⚠️  DESTROY ALL {len(instances)} INSTANCES?", Colors.RED))
    
    if not force:
        confirm = input("  Type 'YES' to confirm: ")
        if confirm != "YES":
            print("  Cancelled.")
            return
    
    for inst in instances:
        instance_id = str(inst.get("id", ""))
        print(f"  Destroying instance #{instance_id}...", end=" ")
        
        if cli.destroy_instance(instance_id):
            print(color("✓", Colors.GREEN))
        else:
            print(color("✗", Colors.RED))
    
    print(color("\n  All instances destroyed.", Colors.GREEN))


def health_check(cli: VastCLI):
    """Run comprehensive health check."""
    print(color("\n🏥 HEALTH CHECK", Colors.BOLD))
    print("=" * 60)
    
    checks = []
    
    # Check 1: API connectivity
    user = cli.get_user()
    if user:
        checks.append(("API Connection", True, "Connected"))
    else:
        checks.append(("API Connection", False, "Failed to connect"))
    
    # Check 2: Account balance
    credit = user.get("credit", 0) if user else 0
    if credit >= 10:
        checks.append(("Balance", True, f"${credit:.2f}"))
    elif credit >= 1:
        checks.append(("Balance", True, f"${credit:.2f} (LOW)"))
    else:
        checks.append(("Balance", False, f"${credit:.2f} (CRITICAL)"))
    
    # Check 3: Active instances
    instances = cli.get_instances()
    running = sum(1 for i in instances if i.get("actual_status") == "running")
    error = sum(1 for i in instances if i.get("actual_status") == "error")
    
    if error > 0:
        checks.append(("Instances", False, f"{running} running, {error} errors"))
    else:
        checks.append(("Instances", True, f"{len(instances)} total, {running} running"))
    
    # Check 4: GPU availability (L40S)
    offers = cli.search_offers("L40S", max_price=2.0)
    if len(offers) >= 5:
        checks.append(("L40S Availability", True, f"{len(offers)} offers under $2/hr"))
    elif len(offers) > 0:
        checks.append(("L40S Availability", True, f"Limited: {len(offers)} offers"))
    else:
        checks.append(("L40S Availability", False, "No offers available"))
    
    # Print results
    all_passed = True
    for check_name, passed, message in checks:
        status = color("✓ PASS", Colors.GREEN) if passed else color("✗ FAIL", Colors.RED)
        print(f"  {check_name:20s} {status}  {message}")
        if not passed:
            all_passed = False
    
    print()
    if all_passed:
        print(color("  ✅ All health checks passed!", Colors.GREEN))
    else:
        print(color("  ⚠️  Some checks failed. Review above.", Colors.YELLOW))


def create_instance_interactive(cli: VastCLI, gpu_type: str = "L40S"):
    """Interactive instance creation."""
    print(color(f"\n🚀 CREATE INSTANCE ({gpu_type})", Colors.BOLD))
    print("=" * 60)
    
    # Search offers
    offers = cli.search_offers(gpu_type, max_price=3.0)
    
    if not offers:
        print(color("  No offers available.", Colors.RED))
        return
    
    # Show top 5
    print("  Available offers:\n")
    for i, offer in enumerate(offers[:5]):
        print(f"    {i+1}. #{offer['id']} - {offer['gpu_name']} @ ${offer['dph_total']:.4f}/hr")
    
    # Select offer
    try:
        choice = int(input("\n  Select offer (1-5): ")) - 1
        if choice < 0 or choice >= min(5, len(offers)):
            print("  Invalid selection.")
            return
    except ValueError:
        print("  Invalid input.")
        return
    
    selected = offers[choice]
    
    # Get Docker image
    default_image = "nvcr.io/nvidia/pytorch:24.01-py3"
    image = input(f"  Docker image [{default_image}]: ").strip() or default_image
    
    # Confirm
    print(f"\n  Creating instance with:")
    print(f"    Offer: #{selected['id']} ({selected['gpu_name']})")
    print(f"    Price: ${selected['dph_total']:.4f}/hr")
    print(f"    Image: {image}")
    
    confirm = input("\n  Proceed? (y/N): ")
    if confirm.lower() != 'y':
        print("  Cancelled.")
        return
    
    # Create
    print("  Creating instance...", end=" ")
    success, result = cli.create_instance(
        offer_id=str(selected['id']),
        docker_image=image,
        disk_gb=100,
    )
    
    if success:
        print(color(f"✓ Created instance #{result}", Colors.GREEN))
    else:
        print(color(f"✗ Failed: {result}", Colors.RED))


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Vast.ai Instance Auditor & Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python vast_audit.py --status          # Ver estado de instancias
    python vast_audit.py --balance         # Ver balance de cuenta
    python vast_audit.py --search L40S     # Buscar ofertas L40S
    python vast_audit.py --search A100 --max-price 1.5   # A100 bajo $1.5/hr
    python vast_audit.py --destroy 12345   # Destruir instancia específica
    python vast_audit.py --destroy-all     # Destruir todas las instancias
    python vast_audit.py --create          # Crear instancia interactivo
    python vast_audit.py --health          # Health check completo
    python vast_audit.py --full            # Auditoría completa
        """
    )
    
    parser.add_argument("--api-key", help="Vast.ai API key")
    parser.add_argument("--status", "-s", action="store_true", help="Show instance status")
    parser.add_argument("--balance", "-b", action="store_true", help="Show account balance")
    parser.add_argument("--search", metavar="GPU", help="Search GPU offers (e.g., L40S, A100)")
    parser.add_argument("--max-price", type=float, help="Max price per hour for search")
    parser.add_argument("--destroy", metavar="ID", help="Destroy specific instance")
    parser.add_argument("--destroy-all", action="store_true", help="Destroy ALL instances")
    parser.add_argument("--create", action="store_true", help="Create instance interactively")
    parser.add_argument("--health", action="store_true", help="Run health check")
    parser.add_argument("--full", "-f", action="store_true", help="Full audit (all checks)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--force", action="store_true", help="Force dangerous operations")
    
    args = parser.parse_args()
    
    # Default to full audit if no args
    if not any([args.status, args.balance, args.search, args.destroy, 
                args.destroy_all, args.create, args.health, args.full]):
        args.full = True
    
    print_banner()
    
    # Initialize CLI
    try:
        cli = VastCLI(api_key=args.api_key)
    except Exception as e:
        print(color(f"❌ Error initializing Vast.ai CLI: {e}", Colors.RED))
        sys.exit(1)
    
    # Execute commands
    if args.full:
        print_account_info(cli)
        print_instances(cli, verbose=args.verbose)
        health_check(cli)
    
    if args.balance:
        print_account_info(cli)
    
    if args.status:
        print_instances(cli, verbose=args.verbose)
    
    if args.search:
        search_offers(cli, args.search, args.max_price)
    
    if args.destroy:
        print(f"  Destroying instance #{args.destroy}...", end=" ")
        if cli.destroy_instance(args.destroy):
            print(color("✓", Colors.GREEN))
        else:
            print(color("✗", Colors.RED))
    
    if args.destroy_all:
        destroy_all_instances(cli, force=args.force)
    
    if args.create:
        create_instance_interactive(cli)
    
    if args.health:
        health_check(cli)
    
    print()


if __name__ == "__main__":
    main()

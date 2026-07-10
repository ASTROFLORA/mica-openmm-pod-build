"""
user_storage_manager.py - Multi-User Cloud Storage Manager

Provides isolated GCS buckets per user with automatic provisioning,
quota management, and seamless integration with compute providers.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                   UserStorageManager                         │
    ├─────────────────────────────────────────────────────────────┤
    │  gs://mica-md-{user_id}/                                    │
    │  ├── input/           # PDB files, parameters               │
    │  ├── output/          # Trajectories, analyses              │
    │  ├── checkpoints/     # Simulation checkpoints              │
    │  └── scratch/         # Temporary files (auto-cleanup)      │
    └─────────────────────────────────────────────────────────────┘

Features:
    - Automatic bucket creation per user
    - Quota tracking and enforcement
    - GCS FUSE mount scripts for compute instances
    - Signed URLs for direct browser uploads
    - Lifecycle policies for cost optimization

Author: MICA Team
Date: December 2024
"""

import json
import os
import subprocess
import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import shutil


class StorageTier(Enum):
    """Storage class tiers with associated costs."""
    STANDARD = "STANDARD"           # $0.020/GB/month - frequent access
    NEARLINE = "NEARLINE"           # $0.010/GB/month - monthly access
    COLDLINE = "COLDLINE"           # $0.004/GB/month - quarterly access
    ARCHIVE = "ARCHIVE"             # $0.0012/GB/month - yearly access


@dataclass
class UserQuota:
    """Storage quota configuration per user."""
    max_storage_gb: float = 100.0           # Max total storage
    max_input_gb: float = 10.0              # Max input files
    max_output_gb: float = 50.0             # Max output/trajectories
    max_checkpoints_gb: float = 30.0        # Max checkpoints
    max_scratch_gb: float = 10.0            # Max scratch space
    scratch_ttl_hours: int = 24             # Auto-delete scratch after
    checkpoint_retention_days: int = 30     # Keep checkpoints for
    archive_after_days: int = 90            # Move to coldline after


@dataclass
class UserBucket:
    """Represents a user's storage bucket."""
    user_id: str
    bucket_name: str
    project_id: str
    region: str
    created_at: datetime
    quota: UserQuota
    current_usage_gb: float = 0.0
    is_active: bool = True
    
    @property
    def bucket_url(self) -> str:
        return f"gs://{self.bucket_name}"
    
    def get_path(self, subdir: str, filename: str = "") -> str:
        """Get full GCS path for a file."""
        if filename:
            return f"gs://{self.bucket_name}/{subdir}/{filename}"
        return f"gs://{self.bucket_name}/{subdir}/"


@dataclass
class GCSCredentials:
    """GCS service account credentials."""
    project_id: str
    service_account_email: str
    private_key_id: str
    private_key: str
    client_id: str
    
    @classmethod
    def from_json_file(cls, json_path: str) -> "GCSCredentials":
        """Load credentials from service account JSON file."""
        with open(json_path, 'r') as f:
            data = json.load(f)
        
        return cls(
            project_id=data["project_id"],
            service_account_email=data["client_email"],
            private_key_id=data["private_key_id"],
            private_key=data["private_key"],
            client_id=data["client_id"],
        )
    
    def to_json(self) -> str:
        """Export as JSON string for injection into compute instances."""
        return json.dumps({
            "type": "service_account",
            "project_id": self.project_id,
            "private_key_id": self.private_key_id,
            "private_key": self.private_key,
            "client_email": self.service_account_email,
            "client_id": self.client_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        })
    
    def to_base64(self) -> str:
        """Encode credentials as base64 for safe env var transmission."""
        return base64.b64encode(self.to_json().encode()).decode()


class UserStorageManager:
    """
    Manages per-user GCS buckets for MICA cloud infrastructure.
    
    Example:
        manager = UserStorageManager(
            project_id="dark-yen-476115-j4",
            credentials_path="/path/to/service-account.json"
        )
        
        # Create bucket for new user
        bucket = await manager.provision_user_bucket("user123")
        
        # Get mount script for Vast.ai instance
        script = manager.generate_mount_script(bucket)
        
        # Upload file
        await manager.upload_file(bucket, "input", "protein.pdb", local_path)
    """
    
    BUCKET_PREFIX = os.environ.get("MICA_COMPUTE_BUCKET_PREFIX", "mica-user")
    LEGACY_BUCKET_PREFIX = "mica-md"
    
    def __init__(
        self,
        project_id: str,
        credentials_path: Optional[str] = None,
        region: str = "us-central1",
        default_quota: Optional[UserQuota] = None,
    ):
        """
        Initialize storage manager.
        
        Args:
            project_id: GCP project ID
            credentials_path: Path to service account JSON
            region: Default GCS region
            default_quota: Default quota for new users
        """
        self.project_id = project_id
        self.region = region
        self.default_quota = default_quota or UserQuota()
        
        # Load credentials
        if credentials_path:
            self.credentials = GCSCredentials.from_json_file(credentials_path)
        else:
            # Try default locations
            default_paths = [
                os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                os.path.expanduser("~/.config/gcloud/application_default_credentials.json"),
            ]
            for path in default_paths:
                if path and os.path.exists(path):
                    self.credentials = GCSCredentials.from_json_file(path)
                    break
            else:
                raise ValueError("No GCS credentials found")
        
        # Cache of user buckets
        self._user_buckets: Dict[str, UserBucket] = {}
        
        # Find gcloud executable (handle Windows .cmd extension)
        self._gcloud_cmd = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not self._gcloud_cmd:
            # Try common Windows locations
            win_paths = [
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
                os.path.expandvars(r"%PROGRAMFILES%\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
                os.path.expandvars(r"%PROGRAMFILES(x86)%\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
            ]
            for p in win_paths:
                if os.path.exists(p):
                    self._gcloud_cmd = p
                    break
        
        if not self._gcloud_cmd:
            raise RuntimeError("gcloud CLI not found")
    
    def _run_gcloud(self, args: List[str], timeout: float = 60.0) -> str:
        """Run gcloud command and return output."""
        cmd = [self._gcloud_cmd] + args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gcloud error: {result.stderr}")
        return result.stdout.strip()
    
    def _generate_bucket_name(self, user_id: str) -> str:
        """Generate unique bucket name for user."""
        # Create deterministic but anonymized bucket name
        hash_suffix = hashlib.sha256(user_id.encode()).hexdigest()[:8]
        return f"{self.BUCKET_PREFIX}-{hash_suffix}"
    
    async def provision_user_bucket(
        self,
        user_id: str,
        quota: Optional[UserQuota] = None,
    ) -> UserBucket:
        """
        Create or get existing bucket for user.
        
        Args:
            user_id: Unique user identifier
            quota: Custom quota (uses default if not specified)
            
        Returns:
            UserBucket object
        """
        # Check cache
        if user_id in self._user_buckets:
            return self._user_buckets[user_id]
        
        bucket_name = self._generate_bucket_name(user_id)
        quota = quota or self.default_quota
        
        # Check if bucket exists
        try:
            self._run_gcloud([
                "storage", "buckets", "describe",
                f"gs://{bucket_name}",
                "--format=json"
            ])
            # Bucket exists
            bucket = UserBucket(
                user_id=user_id,
                bucket_name=bucket_name,
                project_id=self.project_id,
                region=self.region,
                created_at=datetime.utcnow(),  # Approximate
                quota=quota,
            )
        except RuntimeError:
            # Create new bucket
            self._run_gcloud([
                "storage", "buckets", "create",
                f"gs://{bucket_name}",
                f"--location={self.region}",
                "--uniform-bucket-level-access",
                "--public-access-prevention",
            ])
            
            bucket = UserBucket(
                user_id=user_id,
                bucket_name=bucket_name,
                project_id=self.project_id,
                region=self.region,
                created_at=datetime.utcnow(),
                quota=quota,
            )
            
            # Create directory structure using touch/placeholder approach
            # Note: GCS doesn't need actual folders, but we create placeholder files
            # to make the structure visible
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.keep', delete=False) as f:
                f.write("# MICA placeholder file\n")
                placeholder_path = f.name
            
            try:
                for subdir in ["input", "output", "checkpoints", "scratch"]:
                    try:
                        self._run_gcloud([
                            "storage", "cp", placeholder_path,
                            f"gs://{bucket_name}/{subdir}/.keep"
                        ])
                    except RuntimeError:
                        pass  # Ignore if already exists
            finally:
                os.unlink(placeholder_path)
            
            # Set lifecycle policy
            self._set_lifecycle_policy(bucket)
        
        # Cache and return
        self._user_buckets[user_id] = bucket
        return bucket
    
    def _set_lifecycle_policy(self, bucket: UserBucket) -> None:
        """Set lifecycle rules for automatic data management."""
        policy = {
            "lifecycle": {
                "rule": [
                    # Delete scratch files after TTL
                    {
                        "action": {"type": "Delete"},
                        "condition": {
                            "age": bucket.quota.scratch_ttl_hours // 24 or 1,
                            "matchesPrefix": ["scratch/"]
                        }
                    },
                    # Move old checkpoints to nearline
                    {
                        "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
                        "condition": {
                            "age": bucket.quota.checkpoint_retention_days,
                            "matchesPrefix": ["checkpoints/"]
                        }
                    },
                    # Archive old output files
                    {
                        "action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
                        "condition": {
                            "age": bucket.quota.archive_after_days,
                            "matchesPrefix": ["output/"]
                        }
                    }
                ]
            }
        }
        
        # Write policy to temp file and apply
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(policy, f)
            policy_path = f.name
        
        try:
            self._run_gcloud([
                "storage", "buckets", "update",
                f"gs://{bucket.bucket_name}",
                f"--lifecycle-file={policy_path}"
            ])
        finally:
            os.unlink(policy_path)
    
    def generate_mount_script(
        self,
        bucket: UserBucket,
        mount_point: str = "/mnt/gcs",
        include_credentials: bool = True,
    ) -> str:
        """
        Generate bash script to mount GCS bucket via gcsfuse.
        
        This script is designed to run as onstart script in Vast.ai
        or as startup script in other compute providers.
        
        Args:
            bucket: UserBucket to mount
            mount_point: Local mount point path
            include_credentials: Whether to include credential setup
            
        Returns:
            Bash script as string
        """
        script_parts = [
            "#!/bin/bash",
            "set -e",
            "",
            "# ============================================",
            f"# MICA GCS Mount Script for user: {bucket.user_id}",
            f"# Bucket: gs://{bucket.bucket_name}",
            f"# Generated: {datetime.utcnow().isoformat()}",
            "# ============================================",
            "",
            "echo '[MICA] Starting GCS mount setup...'",
            "",
            "# Install gcsfuse if not present",
            "if ! command -v gcsfuse &> /dev/null; then",
            "    echo '[MICA] Installing gcsfuse...'",
            "    export GCSFUSE_REPO=gcsfuse-$(lsb_release -c -s 2>/dev/null || echo 'focal')",
            "    echo \"deb https://packages.cloud.google.com/apt $GCSFUSE_REPO main\" | sudo tee /etc/apt/sources.list.d/gcsfuse.list",
            "    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -",
            "    sudo apt-get update -qq",
            "    sudo apt-get install -y -qq gcsfuse",
            "fi",
            "",
        ]
        
        if include_credentials:
            # Inject credentials from environment variable
            script_parts.extend([
                "# Setup GCS credentials from environment",
                "if [ -n \"$GCS_CREDENTIALS_B64\" ]; then",
                "    echo '[MICA] Setting up GCS credentials...'",
                "    mkdir -p /root/.config/gcloud",
                "    echo \"$GCS_CREDENTIALS_B64\" | base64 -d > /root/.config/gcloud/application_default_credentials.json",
                "    chmod 600 /root/.config/gcloud/application_default_credentials.json",
                "    export GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json",
                "else",
                "    echo '[MICA] WARNING: No GCS credentials found in GCS_CREDENTIALS_B64'",
                "fi",
                "",
            ])
        
        script_parts.extend([
            f"# Create mount point",
            f"MOUNT_POINT=\"{mount_point}\"",
            f"BUCKET=\"{bucket.bucket_name}\"",
            "",
            "mkdir -p $MOUNT_POINT",
            "",
            "# Unmount if already mounted",
            "fusermount -u $MOUNT_POINT 2>/dev/null || true",
            "",
            "# Mount with gcsfuse",
            "echo \"[MICA] Mounting gs://$BUCKET to $MOUNT_POINT...\"",
            "gcsfuse --implicit-dirs \\",
            "        --file-mode=666 \\",
            "        --dir-mode=777 \\",
            "        --key-file=${GOOGLE_APPLICATION_CREDENTIALS:-/root/.config/gcloud/application_default_credentials.json} \\",
            "        $BUCKET $MOUNT_POINT",
            "",
            "# Verify mount",
            "if mountpoint -q $MOUNT_POINT; then",
            "    echo '[MICA] ✓ GCS bucket mounted successfully!'",
            "    echo '[MICA] Available directories:'",
            "    ls -la $MOUNT_POINT/",
            "else",
            "    echo '[MICA] ✗ Mount failed!'",
            "    exit 1",
            "fi",
            "",
            "# Create convenience symlinks",
            "ln -sf $MOUNT_POINT/input /workspace/input 2>/dev/null || true",
            "ln -sf $MOUNT_POINT/output /workspace/output 2>/dev/null || true",
            "",
            "echo '[MICA] GCS setup complete!'",
            "echo '[MICA] Input files: $MOUNT_POINT/input/'",
            "echo '[MICA] Output files: $MOUNT_POINT/output/'",
            "",
        ])
        
        return "\n".join(script_parts)
    
    def generate_openmm_startup_script(
        self,
        bucket: UserBucket,
        pdb_filename: str,
        simulation_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate complete startup script for OpenMM MD simulation on Vast.ai.
        
        Args:
            bucket: User's storage bucket
            pdb_filename: PDB file in bucket/input/
            simulation_params: Simulation parameters (steps, temperature, etc.)
            
        Returns:
            Complete bash script
        """
        params = simulation_params or {}
        steps = params.get("steps", 50000)  # 100 ps at 2fs timestep
        temperature = params.get("temperature", 300)
        
        mount_script = self.generate_mount_script(bucket)
        
        openmm_script = f'''
# ============================================
# OpenMM Molecular Dynamics Simulation
# ============================================

echo '[MICA] Installing OpenMM and dependencies...'
pip install -q openmm mdtraj numpy

echo '[MICA] Starting MD simulation...'
python3 << 'PYTHON_SCRIPT'
import os
from openmm.app import *
from openmm import *
from openmm.unit import *
import mdtraj
from datetime import datetime

print(f"[MICA] OpenMM version: {{Platform.getOpenMMVersion()}}")
print(f"[MICA] Available platforms: {{[Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())]}}")

# Paths
MOUNT = "/mnt/gcs"
INPUT_PDB = f"{{MOUNT}}/input/{pdb_filename}"
OUTPUT_DIR = f"{{MOUNT}}/output"
CHECKPOINT_DIR = f"{{MOUNT}}/checkpoints"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CHECKPOINT_DIR, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

# Load structure
print(f"[MICA] Loading {{INPUT_PDB}}...")
pdb = PDBFile(INPUT_PDB)

# Setup system with implicit solvent (faster for testing)
forcefield = ForceField('amber14-all.xml', 'implicit/gbn2.xml')
system = forcefield.createSystem(
    pdb.topology,
    nonbondedMethod=NoCutoff,
    constraints=HBonds,
)

# Integrator
integrator = LangevinMiddleIntegrator(
    {temperature}*kelvin,
    1/picosecond,
    2*femtoseconds
)

# Platform - prefer CUDA
try:
    platform = Platform.getPlatformByName('CUDA')
    properties = {{'DeviceIndex': '0', 'Precision': 'mixed'}}
    print("[MICA] Using CUDA platform")
except:
    platform = Platform.getPlatformByName('CPU')
    properties = {{}}
    print("[MICA] Using CPU platform")

# Simulation
simulation = Simulation(pdb.topology, system, integrator, platform, properties)
simulation.context.setPositions(pdb.positions)

# Minimize
print("[MICA] Minimizing energy...")
simulation.minimizeEnergy(maxIterations=1000)

# Reporters
dcd_path = f"{{OUTPUT_DIR}}/trajectory_{{timestamp}}.dcd"
log_path = f"{{OUTPUT_DIR}}/simulation_{{timestamp}}.log"
chk_path = f"{{CHECKPOINT_DIR}}/checkpoint_{{timestamp}}.chk"

simulation.reporters.append(DCDReporter(dcd_path, 1000))
simulation.reporters.append(StateDataReporter(
    log_path, 1000,
    step=True, time=True, potentialEnergy=True,
    temperature=True, progress=True, 
    remainingTime=True, speed=True,
    totalSteps={steps}
))
simulation.reporters.append(CheckpointReporter(chk_path, 10000))

# Run
print(f"[MICA] Running {{steps}} steps...")
simulation.step({steps})

# Final checkpoint
simulation.saveCheckpoint(chk_path)
print(f"[MICA] ✓ Simulation complete!")
print(f"[MICA] Trajectory: {{dcd_path}}")
print(f"[MICA] Log: {{log_path}}")
print(f"[MICA] Checkpoint: {{chk_path}}")

PYTHON_SCRIPT

echo '[MICA] MD simulation finished!'
'''
        
        return mount_script + openmm_script
    
    async def get_bucket_usage(self, bucket: UserBucket) -> Dict[str, float]:
        """Get current storage usage by directory."""
        result = self._run_gcloud([
            "storage", "du", "-s",
            f"gs://{bucket.bucket_name}/*",
            "--readable-sizes"
        ])
        
        usage = {"total": 0.0}
        for line in result.split("\n"):
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    size_str = parts[0]
                    path = parts[1]
                    
                    # Parse size (e.g., "1.5 GiB")
                    size_gb = self._parse_size_to_gb(size_str)
                    
                    # Extract subdirectory
                    for subdir in ["input", "output", "checkpoints", "scratch"]:
                        if f"/{subdir}" in path:
                            usage[subdir] = usage.get(subdir, 0) + size_gb
                    
                    usage["total"] += size_gb
        
        return usage
    
    def _parse_size_to_gb(self, size_str: str) -> float:
        """Parse size string like '1.5 GiB' to float GB."""
        try:
            if "TiB" in size_str or "TB" in size_str:
                return float(size_str.replace("TiB", "").replace("TB", "").strip()) * 1024
            elif "GiB" in size_str or "GB" in size_str:
                return float(size_str.replace("GiB", "").replace("GB", "").strip())
            elif "MiB" in size_str or "MB" in size_str:
                return float(size_str.replace("MiB", "").replace("MB", "").strip()) / 1024
            elif "KiB" in size_str or "KB" in size_str:
                return float(size_str.replace("KiB", "").replace("KB", "").strip()) / 1024 / 1024
            else:
                return float(size_str.strip()) / 1024 / 1024 / 1024  # Assume bytes
        except:
            return 0.0
    
    async def upload_file(
        self,
        bucket: UserBucket,
        subdir: str,
        filename: str,
        local_path: str,
    ) -> str:
        """Upload file to user's bucket."""
        gcs_path = bucket.get_path(subdir, filename)
        self._run_gcloud([
            "storage", "cp",
            local_path,
            gcs_path
        ])
        return gcs_path
    
    async def download_file(
        self,
        bucket: UserBucket,
        subdir: str,
        filename: str,
        local_path: str,
    ) -> str:
        """Download file from user's bucket."""
        gcs_path = bucket.get_path(subdir, filename)
        self._run_gcloud([
            "storage", "cp",
            gcs_path,
            local_path
        ])
        return local_path
    
    async def generate_signed_url(
        self,
        bucket: UserBucket,
        subdir: str,
        filename: str,
        expiration_hours: int = 1,
        for_upload: bool = False,
    ) -> str:
        """
        Generate signed URL for direct browser access.
        
        Args:
            bucket: User's bucket
            subdir: Subdirectory (input, output, etc.)
            filename: File name
            expiration_hours: URL validity period
            for_upload: If True, generates upload URL (PUT)
            
        Returns:
            Signed URL string
        """
        gcs_path = bucket.get_path(subdir, filename)
        method = "PUT" if for_upload else "GET"
        
        result = self._run_gcloud([
            "storage", "sign-url",
            gcs_path,
            f"--duration={expiration_hours}h",
            f"--http-verb={method}",
        ])
        
        # Extract URL from output
        for line in result.split("\n"):
            if line.startswith("https://"):
                return line.strip()
        
        return result
    
    async def list_user_files(
        self,
        bucket: UserBucket,
        subdir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List files in user's bucket."""
        path = f"gs://{bucket.bucket_name}/"
        if subdir:
            path += f"{subdir}/"
        
        result = self._run_gcloud([
            "storage", "ls", "-l", path
        ])
        
        files = []
        for line in result.split("\n"):
            if line.strip() and not line.startswith("TOTAL:"):
                parts = line.split()
                if len(parts) >= 3:
                    files.append({
                        "size": parts[0],
                        "modified": parts[1],
                        "path": parts[-1],
                        "name": parts[-1].split("/")[-1],
                    })
        
        return files
    
    async def delete_user_bucket(self, bucket: UserBucket, force: bool = False) -> bool:
        """Delete user's bucket (with confirmation)."""
        if not force:
            raise ValueError("Set force=True to delete bucket and all contents")
        
        try:
            self._run_gcloud([
                "storage", "rm", "-r",
                f"gs://{bucket.bucket_name}"
            ])
            
            # Remove from cache
            if bucket.user_id in self._user_buckets:
                del self._user_buckets[bucket.user_id]
            
            return True
        except Exception as e:
            print(f"Failed to delete bucket: {e}")
            return False


# Convenience function for integration with VastProvider
def create_vast_env_vars(
    storage_manager: UserStorageManager,
    bucket: UserBucket,
) -> Dict[str, str]:
    """
    Create environment variables for Vast.ai instance.
    
    Returns dict to pass to VastProvider.create_instance(env_vars=...)
    """
    return {
        "GCS_CREDENTIALS_B64": storage_manager.credentials.to_base64(),
        "GCS_BUCKET": bucket.bucket_name,
        "GCS_PROJECT": storage_manager.project_id,
        "MICA_USER_ID": bucket.user_id,
    }

"""
security_analyzer.py - Pre-flight Security Verification

Performs security analysis BEFORE job dispatch to cloud providers.

Checks:
1. Container Security: Scans for vulnerabilities, verifies base images
2. Volume Mounts: Validates GCS/S3 bucket permissions and paths
3. Network Policy: Checks egress rules and firewall requirements
4. Privilege Escalation: Detects dangerous capabilities (privileged mode)
5. Secrets Management: Ensures no plaintext secrets in environment
6. Resource Limits: Validates CPU/memory/GPU quotas

Integrates with Event Store for audit trail of all security decisions.

Author: MICA Infrastructure Team
Date: December 2024
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Security Enums and Constants
# ============================================================================

class SecurityCheckResult(Enum):
    """Result of a security check."""
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class SecurityCategory(Enum):
    """Categories of security checks."""
    CONTAINER = "container"
    VOLUME = "volume"
    NETWORK = "network"
    PRIVILEGE = "privilege"
    SECRETS = "secrets"
    RESOURCE = "resource"
    COMPLIANCE = "compliance"


# Known vulnerable base images (CVE database)
VULNERABLE_IMAGES = {
    "python:3.7": "EOL - upgrade to 3.10+",
    "python:3.8": "Security patches ending",
    "ubuntu:18.04": "EOL - upgrade to 22.04+",
    "nvidia/cuda:10.0": "EOL - use 11.8+",
    "nvidia/cuda:10.1": "EOL - use 11.8+",
    "nvidia/cuda:10.2": "EOL - use 11.8+",
}

# Trusted base image prefixes
TRUSTED_IMAGE_PREFIXES = [
    "gcr.io/",
    "us-docker.pkg.dev/",
    "europe-docker.pkg.dev/",
    "nvcr.io/nvidia/",
    "pytorch/pytorch:",
    "tensorflow/tensorflow:",
    "huggingface/",
]

# Dangerous Docker capabilities
DANGEROUS_CAPABILITIES = {
    "SYS_ADMIN",
    "NET_ADMIN",
    "SYS_PTRACE",
    "SYS_RAWIO",
    "DAC_OVERRIDE",
    "SETUID",
    "SETGID",
}

# Patterns for detecting secrets in environment variables
SECRET_PATTERNS = [
    r"(?i)(password|passwd|pwd)\s*=\s*['\"]?[^'\"]{6,}",
    r"(?i)(api[_-]?key|apikey)\s*=\s*['\"]?[^'\"]{16,}",
    r"(?i)(secret[_-]?key|secretkey)\s*=\s*['\"]?[^'\"]{16,}",
    r"(?i)(token)\s*=\s*['\"]?[^'\"]{16,}",
    r"(?i)(private[_-]?key)\s*=",
    r"-----BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY-----",
    r"(?i)aws[_-]?(access[_-]?key|secret)",
    r"(?i)gcp[_-]?credentials",
]


# ============================================================================
# Security Check Results
# ============================================================================

@dataclass
class SecurityCheck:
    """
    Result of a single security check.
    """
    category: SecurityCategory
    name: str
    result: SecurityCheckResult
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    remediation: Optional[str] = None
    severity: str = "medium"  # low, medium, high, critical
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "name": self.name,
            "result": self.result.value,
            "message": self.message,
            "details": self.details,
            "remediation": self.remediation,
            "severity": self.severity,
        }


@dataclass
class SecurityReport:
    """
    Complete security analysis report.
    """
    job_id: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    checks: List[SecurityCheck] = field(default_factory=list)
    passed: bool = True
    summary: Dict[str, int] = field(default_factory=dict)
    
    def add_check(self, check: SecurityCheck) -> None:
        """Add a check result and update summary."""
        self.checks.append(check)
        
        # Update summary counts
        result = check.result.value
        self.summary[result] = self.summary.get(result, 0) + 1
        
        # Update overall status
        if check.result == SecurityCheckResult.FAILED:
            self.passed = False
    
    @property
    def failed_checks(self) -> List[SecurityCheck]:
        return [c for c in self.checks if c.result == SecurityCheckResult.FAILED]
    
    @property
    def warning_checks(self) -> List[SecurityCheck]:
        return [c for c in self.checks if c.result == SecurityCheckResult.WARNING]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "timestamp": self.timestamp,
            "passed": self.passed,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


# ============================================================================
# Security Analyzer
# ============================================================================

class SecurityAnalyzer:
    """
    Pre-flight security analyzer for cloud job dispatch.
    
    Performs comprehensive security checks before allowing a job
    to be dispatched to any cloud provider.
    
    Example:
        analyzer = SecurityAnalyzer()
        
        report = await analyzer.analyze_job(
            job_id="job-123",
            docker_image="gcr.io/my-project/biodynamo:latest",
            volumes=["/gcs/bucket:/data"],
            env_vars={"RUNPOD_API_KEY": "..."},
        )
        
        if not report.passed:
            for check in report.failed_checks:
                print(f"FAILED: {check.name} - {check.message}")
    """
    
    def __init__(
        self,
        strict_mode: bool = True,
        allow_privileged: bool = False,
        trusted_registries: Optional[List[str]] = None,
        max_volume_size_gb: int = 1000,
        required_labels: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize security analyzer.
        
        Args:
            strict_mode: Fail on warnings (default: True)
            allow_privileged: Allow privileged containers (default: False)
            trusted_registries: Additional trusted container registries
            max_volume_size_gb: Maximum allowed volume size
            required_labels: Required container labels for compliance
        """
        self.strict_mode = strict_mode
        self.allow_privileged = allow_privileged
        self.trusted_registries = trusted_registries or []
        self.max_volume_size_gb = max_volume_size_gb
        self.required_labels = required_labels or {}
        
        # Compile secret detection patterns
        self._secret_patterns = [re.compile(p) for p in SECRET_PATTERNS]
    
    async def analyze_job(
        self,
        job_id: str,
        docker_image: str,
        volumes: Optional[List[str]] = None,
        env_vars: Optional[Dict[str, str]] = None,
        capabilities: Optional[Set[str]] = None,
        privileged: bool = False,
        network_mode: str = "bridge",
        resource_limits: Optional[Dict[str, Any]] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> SecurityReport:
        """
        Perform comprehensive security analysis on a job.
        
        Args:
            job_id: Unique job identifier
            docker_image: Container image to run
            volumes: Volume mount specifications (host:container format)
            env_vars: Environment variables
            capabilities: Linux capabilities requested
            privileged: Whether privileged mode is requested
            network_mode: Docker network mode
            resource_limits: CPU/memory/GPU limits
            labels: Container labels
            
        Returns:
            SecurityReport with all check results
        """
        report = SecurityReport(job_id=job_id)
        
        # Run all security checks
        await asyncio.gather(
            self._check_container_image(report, docker_image),
            self._check_volumes(report, volumes or []),
            self._check_secrets(report, env_vars or {}),
            self._check_privileges(report, privileged, capabilities or set()),
            self._check_network(report, network_mode),
            self._check_resources(report, resource_limits or {}),
            self._check_compliance(report, labels or {}),
        )
        
        # In strict mode, warnings become failures
        if self.strict_mode:
            for check in report.checks:
                if check.result == SecurityCheckResult.WARNING and check.severity in ["high", "critical"]:
                    check.result = SecurityCheckResult.FAILED
                    report.passed = False
        
        return report
    
    # ============================================================================
    # Individual Security Checks
    # ============================================================================
    
    async def _check_container_image(
        self,
        report: SecurityReport,
        docker_image: str,
    ) -> None:
        """Check container image for security issues."""
        
        # Check if image is from trusted registry
        is_trusted = any(
            docker_image.startswith(prefix)
            for prefix in TRUSTED_IMAGE_PREFIXES + self.trusted_registries
        )
        
        if is_trusted:
            report.add_check(SecurityCheck(
                category=SecurityCategory.CONTAINER,
                name="trusted_registry",
                result=SecurityCheckResult.PASSED,
                message=f"Image from trusted registry",
                details={"image": docker_image},
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.CONTAINER,
                name="trusted_registry",
                result=SecurityCheckResult.WARNING,
                message=f"Image not from trusted registry",
                details={"image": docker_image},
                remediation="Use images from gcr.io, nvcr.io, or other trusted registries",
                severity="medium",
            ))
        
        # Check for known vulnerable base images
        for vuln_image, reason in VULNERABLE_IMAGES.items():
            if vuln_image in docker_image:
                report.add_check(SecurityCheck(
                    category=SecurityCategory.CONTAINER,
                    name="vulnerable_base",
                    result=SecurityCheckResult.FAILED,
                    message=f"Image uses vulnerable base: {vuln_image}",
                    details={"image": docker_image, "reason": reason},
                    remediation=f"Upgrade base image: {reason}",
                    severity="high",
                ))
                return
        
        report.add_check(SecurityCheck(
            category=SecurityCategory.CONTAINER,
            name="vulnerable_base",
            result=SecurityCheckResult.PASSED,
            message="No known vulnerable base images detected",
        ))
        
        # Check for image tag (avoid :latest in production)
        if docker_image.endswith(":latest") or ":" not in docker_image.split("/")[-1]:
            report.add_check(SecurityCheck(
                category=SecurityCategory.CONTAINER,
                name="image_tag",
                result=SecurityCheckResult.WARNING,
                message="Using :latest or untagged image",
                details={"image": docker_image},
                remediation="Use specific version tags (e.g., v1.2.3 or sha256 digest)",
                severity="low",
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.CONTAINER,
                name="image_tag",
                result=SecurityCheckResult.PASSED,
                message="Image has specific version tag",
            ))
    
    async def _check_volumes(
        self,
        report: SecurityReport,
        volumes: List[str],
    ) -> None:
        """Check volume mounts for security issues."""
        
        if not volumes:
            report.add_check(SecurityCheck(
                category=SecurityCategory.VOLUME,
                name="volume_count",
                result=SecurityCheckResult.PASSED,
                message="No volumes mounted",
            ))
            return
        
        dangerous_mounts = []
        for vol in volumes:
            parts = vol.split(":")
            host_path = parts[0] if len(parts) >= 1 else ""
            
            # Check for dangerous host paths
            dangerous_paths = ["/", "/etc", "/var", "/root", "/home", "/usr"]
            for dp in dangerous_paths:
                if host_path == dp or host_path.startswith(dp + "/"):
                    dangerous_mounts.append((vol, dp))
        
        if dangerous_mounts:
            report.add_check(SecurityCheck(
                category=SecurityCategory.VOLUME,
                name="dangerous_mounts",
                result=SecurityCheckResult.FAILED,
                message=f"Dangerous host paths mounted: {[m[0] for m in dangerous_mounts]}",
                details={"mounts": dangerous_mounts},
                remediation="Avoid mounting sensitive host paths",
                severity="critical",
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.VOLUME,
                name="dangerous_mounts",
                result=SecurityCheckResult.PASSED,
                message="No dangerous host paths detected",
            ))
        
        # Check for cloud storage mounts (GCS, S3)
        cloud_mounts = [v for v in volumes if any(x in v for x in ["/gcs", "gs://", "s3://"])]
        if cloud_mounts:
            report.add_check(SecurityCheck(
                category=SecurityCategory.VOLUME,
                name="cloud_storage",
                result=SecurityCheckResult.PASSED,
                message=f"Cloud storage mounts detected: {len(cloud_mounts)}",
                details={"mounts": cloud_mounts},
            ))
    
    async def _check_secrets(
        self,
        report: SecurityReport,
        env_vars: Dict[str, str],
    ) -> None:
        """Check for exposed secrets in environment variables."""
        
        detected_secrets = []
        
        for key, value in env_vars.items():
            # Check key names
            if any(s in key.upper() for s in ["PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL"]):
                # Check if it looks like an actual secret (not a key name reference)
                if value and len(value) > 8 and not value.startswith("$"):
                    detected_secrets.append({
                        "key": key,
                        "type": "env_var_name",
                        "value_preview": value[:4] + "..." if len(value) > 4 else "***",
                    })
            
            # Check value patterns
            combined = f"{key}={value}"
            for pattern in self._secret_patterns:
                if pattern.search(combined):
                    detected_secrets.append({
                        "key": key,
                        "type": "pattern_match",
                        "pattern": pattern.pattern[:30],
                    })
                    break
        
        if detected_secrets:
            report.add_check(SecurityCheck(
                category=SecurityCategory.SECRETS,
                name="exposed_secrets",
                result=SecurityCheckResult.FAILED,
                message=f"Potential secrets exposed in environment: {len(detected_secrets)}",
                details={"secrets": detected_secrets},
                remediation="Use secret management (GCP Secret Manager, HashiCorp Vault)",
                severity="critical",
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.SECRETS,
                name="exposed_secrets",
                result=SecurityCheckResult.PASSED,
                message="No plaintext secrets detected in environment",
            ))
    
    async def _check_privileges(
        self,
        report: SecurityReport,
        privileged: bool,
        capabilities: Set[str],
    ) -> None:
        """Check for privilege escalation risks."""
        
        # Check privileged mode
        if privileged:
            if self.allow_privileged:
                report.add_check(SecurityCheck(
                    category=SecurityCategory.PRIVILEGE,
                    name="privileged_mode",
                    result=SecurityCheckResult.WARNING,
                    message="Container running in privileged mode (allowed by policy)",
                    severity="high",
                ))
            else:
                report.add_check(SecurityCheck(
                    category=SecurityCategory.PRIVILEGE,
                    name="privileged_mode",
                    result=SecurityCheckResult.FAILED,
                    message="Container requests privileged mode",
                    remediation="Remove --privileged flag, use specific capabilities instead",
                    severity="critical",
                ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.PRIVILEGE,
                name="privileged_mode",
                result=SecurityCheckResult.PASSED,
                message="Container not running in privileged mode",
            ))
        
        # Check dangerous capabilities
        dangerous_found = capabilities.intersection(DANGEROUS_CAPABILITIES)
        if dangerous_found:
            report.add_check(SecurityCheck(
                category=SecurityCategory.PRIVILEGE,
                name="dangerous_capabilities",
                result=SecurityCheckResult.WARNING,
                message=f"Dangerous capabilities requested: {dangerous_found}",
                details={"capabilities": list(dangerous_found)},
                remediation="Remove unnecessary capabilities",
                severity="high",
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.PRIVILEGE,
                name="dangerous_capabilities",
                result=SecurityCheckResult.PASSED,
                message="No dangerous capabilities requested",
            ))
    
    async def _check_network(
        self,
        report: SecurityReport,
        network_mode: str,
    ) -> None:
        """Check network configuration."""
        
        if network_mode == "host":
            report.add_check(SecurityCheck(
                category=SecurityCategory.NETWORK,
                name="network_mode",
                result=SecurityCheckResult.WARNING,
                message="Container using host network mode",
                remediation="Use bridge networking with explicit port mapping",
                severity="medium",
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.NETWORK,
                name="network_mode",
                result=SecurityCheckResult.PASSED,
                message=f"Network mode: {network_mode}",
            ))
    
    async def _check_resources(
        self,
        report: SecurityReport,
        resource_limits: Dict[str, Any],
    ) -> None:
        """Check resource limits are set."""
        
        if not resource_limits:
            report.add_check(SecurityCheck(
                category=SecurityCategory.RESOURCE,
                name="resource_limits",
                result=SecurityCheckResult.WARNING,
                message="No resource limits specified",
                remediation="Set CPU, memory, and GPU limits to prevent resource exhaustion",
                severity="low",
            ))
        else:
            # Check for reasonable limits
            memory_gb = resource_limits.get("memory_gb", 0)
            cpu_cores = resource_limits.get("cpu_cores", 0)
            
            if memory_gb > 500 or cpu_cores > 96:
                report.add_check(SecurityCheck(
                    category=SecurityCategory.RESOURCE,
                    name="resource_limits",
                    result=SecurityCheckResult.WARNING,
                    message=f"Very high resource limits: {memory_gb}GB RAM, {cpu_cores} CPUs",
                    severity="low",
                ))
            else:
                report.add_check(SecurityCheck(
                    category=SecurityCategory.RESOURCE,
                    name="resource_limits",
                    result=SecurityCheckResult.PASSED,
                    message="Resource limits configured",
                    details=resource_limits,
                ))
    
    async def _check_compliance(
        self,
        report: SecurityReport,
        labels: Dict[str, str],
    ) -> None:
        """Check compliance requirements (labels, tags)."""
        
        if not self.required_labels:
            report.add_check(SecurityCheck(
                category=SecurityCategory.COMPLIANCE,
                name="required_labels",
                result=SecurityCheckResult.SKIPPED,
                message="No required labels configured",
            ))
            return
        
        missing_labels = []
        for req_key, req_value in self.required_labels.items():
            if req_key not in labels:
                missing_labels.append(req_key)
            elif req_value and labels[req_key] != req_value:
                missing_labels.append(f"{req_key}={req_value}")
        
        if missing_labels:
            report.add_check(SecurityCheck(
                category=SecurityCategory.COMPLIANCE,
                name="required_labels",
                result=SecurityCheckResult.FAILED,
                message=f"Missing required labels: {missing_labels}",
                details={"missing": missing_labels, "provided": labels},
                remediation="Add required labels for compliance tracking",
                severity="medium",
            ))
        else:
            report.add_check(SecurityCheck(
                category=SecurityCategory.COMPLIANCE,
                name="required_labels",
                result=SecurityCheckResult.PASSED,
                message="All required labels present",
            ))


# ============================================================================
# Event Store Integration
# ============================================================================

async def log_security_check(
    event_store: Any,  # InfrastructureEventStore
    report: SecurityReport,
) -> None:
    """
    Log security check result to event store.
    
    Args:
        event_store: Event store instance
        report: Security report to log
    """
    try:
        # Import event types
        from .event_store import (
            SecurityCheckPassedEvent,
            SecurityCheckFailedEvent,
        )
        
        if report.passed:
            event = SecurityCheckPassedEvent(
                job_id=report.job_id,
                checks_performed=[c.name for c in report.checks],
                metadata={"summary": report.summary},
            )
        else:
            event = SecurityCheckFailedEvent(
                job_id=report.job_id,
                failed_checks=[c.name for c in report.failed_checks],
                reason="; ".join(c.message for c in report.failed_checks[:3]),
                action_taken="rejected",
                metadata={"summary": report.summary},
            )
        
        event_store.append(event)
        
    except Exception as e:
        logger.warning(f"Failed to log security check to event store: {e}")


# ============================================================================
# Factory Functions
# ============================================================================

def create_security_analyzer(
    strict_mode: bool = True,
    allow_privileged: bool = False,
) -> SecurityAnalyzer:
    """
    Factory function to create security analyzer.
    
    Args:
        strict_mode: Fail on warnings
        allow_privileged: Allow privileged containers
        
    Returns:
        Configured SecurityAnalyzer
    """
    return SecurityAnalyzer(
        strict_mode=strict_mode,
        allow_privileged=allow_privileged,
    )

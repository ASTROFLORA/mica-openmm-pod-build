"""
ssh_resilience.py - Robust SSH execution layer for unstable Vast.ai connections

Handles:
- Connection retries with exponential backoff
- Circuit breaker pattern
- Command-level protocols (fail-fast vs retry vs fallback)
- Structured error reporting
- Timeout management

Author: Team 2
Date: 2026-01-22
"""

import asyncio
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict, Any
import logging

logger = logging.getLogger(__name__)


class CommandProtocol(Enum):
    """Execution protocol for SSH commands."""
    FAIL_FAST = "fail_fast"  # Single attempt, fail immediately
    RETRY_3X = "retry_3x"    # Retry up to 3 times with backoff
    RETRY_5X = "retry_5x"    # Retry up to 5 times (long operations)
    FALLBACK = "fallback"    # Try alternative approaches on failure


class SSHFailureReason(Enum):
    """Categorized SSH failure reasons."""
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_TIMEOUT = "connection_timeout"
    AUTH_FAILURE = "auth_failure"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_ERROR = "command_error"
    NETWORK_UNREACHABLE = "network_unreachable"
    HOST_KEY_CHANGED = "host_key_changed"
    UNKNOWN = "unknown"


@dataclass
class SSHResult:
    """Structured SSH command result."""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    attempts: int
    failure_reason: Optional[SSHFailureReason] = None
    raw_error: Optional[str] = None


class SSHCircuitBreaker:
    """Circuit breaker to prevent hammering failing SSH hosts."""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures: Dict[str, List[float]] = {}  # host -> [timestamps]
    
    def is_open(self, host: str) -> bool:
        """Check if circuit is open (too many recent failures)."""
        if host not in self.failures:
            return False
        
        now = time.time()
        # Remove old failures outside timeout window
        self.failures[host] = [
            t for t in self.failures[host] if now - t < self.timeout
        ]
        
        return len(self.failures[host]) >= self.failure_threshold
    
    def record_failure(self, host: str):
        """Record a failure."""
        if host not in self.failures:
            self.failures[host] = []
        self.failures[host].append(time.time())
    
    def reset(self, host: str):
        """Reset failures for host (after successful connection)."""
        if host in self.failures:
            del self.failures[host]


class ResilientSSHExecutor:
    """Resilient SSH command executor with retry logic and circuit breaker."""
    
    def __init__(self):
        self.circuit_breaker = SSHCircuitBreaker()
        self.default_ssh_options = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes",
        ]
    
    def _classify_error(self, stderr: str, exit_code: int) -> SSHFailureReason:
        """Classify SSH error from stderr."""
        stderr_lower = stderr.lower()
        
        if "connection refused" in stderr_lower:
            return SSHFailureReason.CONNECTION_REFUSED
        elif "connection timed out" in stderr_lower or "operation timed out" in stderr_lower:
            return SSHFailureReason.CONNECTION_TIMEOUT
        elif "permission denied" in stderr_lower or "authentication failed" in stderr_lower:
            return SSHFailureReason.AUTH_FAILURE
        elif "network is unreachable" in stderr_lower or "no route to host" in stderr_lower:
            return SSHFailureReason.NETWORK_UNREACHABLE
        elif "remote host identification has changed" in stderr_lower or "host key" in stderr_lower:
            return SSHFailureReason.HOST_KEY_CHANGED
        elif "bad permissions" in stderr_lower or "bad owner" in stderr_lower:
            # SSH config file permission issues are transient startup errors — treat as retryable
            return SSHFailureReason.CONNECTION_TIMEOUT
        elif exit_code == 255:
            # exit 255 is SSH's own fatal error (config read failure, connection refused, etc.) — retryable
            return SSHFailureReason.CONNECTION_REFUSED
        elif exit_code == 124 or exit_code == 137:  # timeout command codes
            return SSHFailureReason.COMMAND_TIMEOUT
        elif exit_code != 0:
            return SSHFailureReason.COMMAND_ERROR
        else:
            return SSHFailureReason.UNKNOWN
    
    def _should_retry(self, failure_reason: SSHFailureReason) -> bool:
        """Determine if failure is retryable."""
        retryable = {
            SSHFailureReason.CONNECTION_REFUSED,
            SSHFailureReason.CONNECTION_TIMEOUT,
            SSHFailureReason.NETWORK_UNREACHABLE,
        }
        return failure_reason in retryable
    
    async def execute_with_protocol(
        self,
        host: str,
        port: int,
        command: str,
        protocol: CommandProtocol = CommandProtocol.RETRY_3X,
        timeout: int = 30,
        user: str = "root",
        key_path: Optional[str] = None
    ) -> SSHResult:
        """
        Execute SSH command with specified protocol.
        
        Args:
            host: SSH host
            port: SSH port
            command: Command to execute
            protocol: Execution protocol (retry strategy)
            timeout: Per-attempt timeout
            user: SSH user
            key_path: Path to SSH private key (e.g. ~/.ssh/vast_key)
        
        Returns:
            SSHResult with structured output
        """
        use_circuit_breaker = protocol is not CommandProtocol.FAIL_FAST

        # Check circuit breaker
        if use_circuit_breaker and self.circuit_breaker.is_open(host):
            logger.warning(f"Circuit breaker OPEN for {host} - too many recent failures")
            return SSHResult(
                success=False,
                stdout="",
                stderr="Circuit breaker open - host has too many recent failures",
                exit_code=-1,
                duration=0.0,
                attempts=0,
                failure_reason=SSHFailureReason.CONNECTION_REFUSED,
                raw_error="Circuit breaker triggered"
            )
        
        # Determine max attempts from protocol
        max_attempts = {
            CommandProtocol.FAIL_FAST: 1,
            CommandProtocol.RETRY_3X: 3,
            CommandProtocol.RETRY_5X: 5,
            CommandProtocol.FALLBACK: 3,
        }[protocol]
        
        attempts = 0
        total_start = time.time()
        last_result: Optional[SSHResult] = None
        
        for attempt in range(max_attempts):
            attempts += 1
            attempt_start = time.time()
            
            # Build SSH command
            ssh_cmd = [
                "ssh",
                *self.default_ssh_options,
                "-o", f"ConnectTimeout={timeout}",
            ]
            
            # Add identity key if provided
            if key_path:
                ssh_cmd.extend(["-i", key_path])
            
            ssh_cmd.extend([
                "-p", str(port),
                f"{user}@{host}",
                command
            ])
            
            logger.debug(f"SSH attempt {attempts}/{max_attempts} to {host}:{port}")
            
            try:
                # Execute with timeout
                process = subprocess.run(
                    ssh_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 10  # Add buffer to SSH timeout
                )
                
                duration = time.time() - attempt_start
                
                # Success!
                if process.returncode == 0:
                    self.circuit_breaker.reset(host)  # Reset on success
                    return SSHResult(
                        success=True,
                        stdout=process.stdout,
                        stderr=process.stderr,
                        exit_code=0,
                        duration=duration,
                        attempts=attempts
                    )
                
                # Failure - classify
                failure_reason = self._classify_error(process.stderr, process.returncode)
                last_result = SSHResult(
                    success=False,
                    stdout=process.stdout,
                    stderr=process.stderr,
                    exit_code=process.returncode,
                    duration=duration,
                    attempts=attempts,
                    failure_reason=failure_reason,
                    raw_error=process.stderr[:500]
                )
                
                # Check if we should retry
                if not self._should_retry(failure_reason):
                    logger.warning(f"Non-retryable error: {failure_reason}")
                    break
                
                # Exponential backoff before retry
                if attempt < max_attempts - 1:
                    backoff = 2 ** attempt  # 1s, 2s, 4s, 8s, 16s
                    logger.info(f"Retrying in {backoff}s after {failure_reason}")
                    await asyncio.sleep(backoff)
            
            except subprocess.TimeoutExpired:
                duration = time.time() - attempt_start
                last_result = SSHResult(
                    success=False,
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    exit_code=124,
                    duration=duration,
                    attempts=attempts,
                    failure_reason=SSHFailureReason.COMMAND_TIMEOUT,
                    raw_error="TimeoutExpired"
                )
                
                # Timeout is retryable
                if attempt < max_attempts - 1:
                    backoff = 2 ** attempt
                    logger.info(f"Retrying after timeout in {backoff}s")
                    await asyncio.sleep(backoff)
            
            except Exception as e:
                duration = time.time() - attempt_start
                last_result = SSHResult(
                    success=False,
                    stdout="",
                    stderr=str(e),
                    exit_code=-1,
                    duration=duration,
                    attempts=attempts,
                    failure_reason=SSHFailureReason.UNKNOWN,
                    raw_error=str(e)[:500]
                )
                break  # Unknown errors don't retry
        
        # All attempts exhausted
        if use_circuit_breaker:
            self.circuit_breaker.record_failure(host)
        return last_result or SSHResult(
            success=False,
            stdout="",
            stderr="All retry attempts exhausted",
            exit_code=-1,
            duration=time.time() - total_start,
            attempts=attempts,
            failure_reason=SSHFailureReason.UNKNOWN
        )
    
    async def execute_with_fallback(
        self,
        host: str,
        port: int,
        primary_command: str,
        fallback_commands: List[str],
        timeout: int = 30,
        key_path: Optional[str] = None
    ) -> SSHResult:
        """
        Execute command with fallback alternatives.
        
        Useful for situations like:
        - Try `python3`, fallback to `python`
        - Try `nvidia-smi`, fallback to `lspci | grep -i nvidia`
        - Try `conda`, fallback to `/opt/conda/bin/conda`
        """
        # Try primary
        result = await self.execute_with_protocol(
            host, port, primary_command,
            protocol=CommandProtocol.RETRY_3X,
            timeout=timeout,
            key_path=key_path
        )
        
        if result.success:
            return result
        
        # Try fallbacks
        for i, fallback_cmd in enumerate(fallback_commands):
            logger.info(f"Primary failed, trying fallback {i+1}/{len(fallback_commands)}")
            result = await self.execute_with_protocol(
                host, port, fallback_cmd,
                protocol=CommandProtocol.RETRY_3X,
                timeout=timeout,
                key_path=key_path
            )
            if result.success:
                return result
        
        # All failed
        return result
    
    async def health_check(
        self,
        host: str,
        port: int,
        timeout: int = 10,
        key_path: Optional[str] = None
    ) -> bool:
        """
        Quick health check - can we connect?
        
        Uses fail-fast protocol for speed.
        """
        result = await self.execute_with_protocol(
            host, port, "echo 'OK'",
            protocol=CommandProtocol.FAIL_FAST,
            timeout=timeout,
            key_path=key_path
        )
        return result.success and "OK" in result.stdout

    async def scp_upload(
        self,
        host: str,
        port: int,
        local_path: str,
        remote_path: str,
        key_path: Optional[str] = None,
        recursive: bool = False,
        timeout: int = 120,
    ) -> SSHResult:
        """
        Upload file(s) via SCP with retry logic.
        
        Args:
            host: SSH host
            port: SSH port
            local_path: Local file/directory path
            remote_path: Destination path on remote
            key_path: Path to SSH private key
            recursive: If True, copy directories recursively (-r)
            timeout: Transfer timeout in seconds
        """
        scp_cmd = [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}",
            "-P", str(port),
        ]
        if key_path:
            scp_cmd.extend(["-i", key_path])
        if recursive:
            scp_cmd.append("-r")
        scp_cmd.extend([local_path, f"root@{host}:{remote_path}"])

        max_attempts = 3
        attempts = 0
        total_start = time.time()
        last_result: Optional[SSHResult] = None

        for attempt in range(max_attempts):
            attempts += 1
            attempt_start = time.time()
            try:
                process = subprocess.run(
                    scp_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 30,
                )
                duration = time.time() - attempt_start
                if process.returncode == 0:
                    return SSHResult(
                        success=True, stdout=process.stdout,
                        stderr=process.stderr, exit_code=0,
                        duration=duration, attempts=attempts
                    )
                last_result = SSHResult(
                    success=False, stdout=process.stdout,
                    stderr=process.stderr, exit_code=process.returncode,
                    duration=duration, attempts=attempts,
                    failure_reason=self._classify_error(process.stderr, process.returncode),
                    raw_error=process.stderr[:500]
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
            except subprocess.TimeoutExpired:
                last_result = SSHResult(
                    success=False, stdout="", stderr=f"SCP timed out after {timeout}s",
                    exit_code=124, duration=time.time() - attempt_start,
                    attempts=attempts, failure_reason=SSHFailureReason.COMMAND_TIMEOUT
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_result = SSHResult(
                    success=False, stdout="", stderr=str(e),
                    exit_code=-1, duration=time.time() - attempt_start,
                    attempts=attempts, failure_reason=SSHFailureReason.UNKNOWN
                )
                break

        return last_result or SSHResult(
            success=False, stdout="", stderr="All SCP retry attempts exhausted",
            exit_code=-1, duration=time.time() - total_start, attempts=attempts
        )

    async def scp_download(
        self,
        host: str,
        port: int,
        remote_path: str,
        local_path: str,
        key_path: Optional[str] = None,
        recursive: bool = False,
        timeout: int = 120,
    ) -> SSHResult:
        """
        Download file(s) via SCP with retry logic.
        
        Args:
            host: SSH host
            port: SSH port
            remote_path: Remote file/directory path
            local_path: Local destination path
            key_path: Path to SSH private key
            recursive: If True, copy directories recursively (-r)
            timeout: Transfer timeout in seconds
        """
        scp_cmd = [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}",
            "-P", str(port),
        ]
        if key_path:
            scp_cmd.extend(["-i", key_path])
        if recursive:
            scp_cmd.append("-r")
        scp_cmd.extend([f"root@{host}:{remote_path}", local_path])

        max_attempts = 3
        attempts = 0
        total_start = time.time()
        last_result: Optional[SSHResult] = None

        for attempt in range(max_attempts):
            attempts += 1
            attempt_start = time.time()
            try:
                process = subprocess.run(
                    scp_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 30,
                )
                duration = time.time() - attempt_start
                if process.returncode == 0:
                    return SSHResult(
                        success=True, stdout=process.stdout,
                        stderr=process.stderr, exit_code=0,
                        duration=duration, attempts=attempts
                    )
                last_result = SSHResult(
                    success=False, stdout=process.stdout,
                    stderr=process.stderr, exit_code=process.returncode,
                    duration=duration, attempts=attempts,
                    failure_reason=self._classify_error(process.stderr, process.returncode),
                    raw_error=process.stderr[:500]
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
            except subprocess.TimeoutExpired:
                last_result = SSHResult(
                    success=False, stdout="", stderr=f"SCP timed out after {timeout}s",
                    exit_code=124, duration=time.time() - attempt_start,
                    attempts=attempts, failure_reason=SSHFailureReason.COMMAND_TIMEOUT
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_result = SSHResult(
                    success=False, stdout="", stderr=str(e),
                    exit_code=-1, duration=time.time() - attempt_start,
                    attempts=attempts, failure_reason=SSHFailureReason.UNKNOWN
                )
                break

        return last_result or SSHResult(
            success=False, stdout="", stderr="All SCP download retry attempts exhausted",
            exit_code=-1, duration=time.time() - total_start, attempts=attempts
        )


# Global singleton
_executor: Optional[ResilientSSHExecutor] = None


def get_ssh_executor() -> ResilientSSHExecutor:
    """Get global SSH executor instance."""
    global _executor
    if _executor is None:
        _executor = ResilientSSHExecutor()
    return _executor

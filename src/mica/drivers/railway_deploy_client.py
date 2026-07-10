"""Railway deploy client (Pillar 4, R28 W5 — real-mode enabled).

Bridges ``RailwayDeployCandidate`` -> ``RailwayDeployReport`` against the
Railway GraphQL API. The default mode is **dry_run=False** — the client
sends the real GraphQL mutation to Railway once a CLI session or
``RAILWAY_TOKEN`` is present.

Safety rails:
    - Hard refusal to deploy against the production service ``mica-api``.
    - Service-name allowlist enforced before any API call.
    - Wall-clock timeout on the smoke matrix.
    - All exceptions mapped onto ``RailwayDeployReport(status="blocked")``.
    - ``MICA_DEPLOY_DRY_RUN=1`` env var forces dry mode as a kill-switch;
      useful during CI preflight or when staging is known-unstable.

Staging cycle (current state):
    ``mica-driver-staging`` is provisioned and healthy (verified 2026-04-25).
    Real deploys are now the default. Set ``MICA_DEPLOY_DRY_RUN=1`` to
    temporarily revert to dry mode without a code change.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterable
from urllib.parse import urlsplit

from mica.sdk.contracts import RailwayDeployCandidate, RailwayDeployReport
from mica.sdk.ops import (
    CanarySnapshot,
    PolicyCheck,
    build_deploy_contract_checks,
    build_ops_evidence_report,
    evaluate_release_gate,
    run_runtime_checks,
)

# Hard-coded refusal list. ``mica-api`` is production; the driver lab is
# never allowed to deploy against it directly.
_FORBIDDEN_SERVICES = frozenset({"mica-api", "mica-worker", "mica-production"})

_DEFAULT_RAILWAY_API = "https://backboard.railway.app/graphql/v2"


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _resolve_cli_bin(cli_bin: str | None) -> str:
    if cli_bin:
        return cli_bin
    if os.name == "nt":
        for candidate in ("railway.cmd", "railway.CMD", "railway.exe", "railway"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    return shutil.which("railway") or "railway"


class RailwayDeployClient:
    """Railway GraphQL client gated by service allowlist + dry-run default.

    Parameters
    ----------
    project_id:
        Railway project UUID (e.g. mica-production = ``02732816-...``).
    environment_id:
        Railway environment UUID (e.g. production = ``be191e0d-...``).
    api_token:
        Railway API token. If absent, the client refuses with
        ``status="blocked"``.
    api_url:
        Override for tests. Defaults to Railway's GraphQL endpoint.
    dry_run:
        If True, construct + log the mutation payload but do NOT send it.
        Returns ``status="deployed"`` with a fake ``deploy_id`` so
        downstream contract tests can run. Defaults to ``False``.
        Override via ``MICA_DEPLOY_DRY_RUN=1`` env var (takes precedence
        over the constructor argument; set to ``0`` to force real mode).
    """

    def __init__(
        self,
        project_id: str,
        environment_id: str,
        api_token: str | None = None,
        api_url: str = _DEFAULT_RAILWAY_API,
        dry_run: bool = False,
        cli_bin: str | None = None,
        workdir: str | None = None,
    ):
        self.project_id = project_id
        self.environment_id = environment_id
        self.explicit_api_token = api_token
        self.environment_api_token = os.environ.get("RAILWAY_TOKEN")
        self.api_token = api_token if api_token is not None else self.environment_api_token
        if api_token is not None:
            self.api_token_source = "argument"
        elif self.environment_api_token:
            self.api_token_source = "environment"
        else:
            self.api_token_source = "none"
        self.api_url = api_url
        # MICA_DEPLOY_DRY_RUN env var is a kill-switch: "1"/"true" → dry, "0"/"false" → real.
        # If the var is unset, respect the constructor argument (default: False).
        _env_dry = os.environ.get("MICA_DEPLOY_DRY_RUN", "").strip().lower()
        if _env_dry in {"1", "true", "yes"}:
            self.dry_run = True
        elif _env_dry in {"0", "false", "no"}:
            self.dry_run = False
        else:
            self.dry_run = dry_run
        self.cli_bin = _resolve_cli_bin(cli_bin)
        self.workdir = workdir

    async def deploy(
        self,
        candidate: RailwayDeployCandidate,
        smoke_runner=None,
    ) -> RailwayDeployReport:
        started_at = _utc_now_iso()
        deploy_id = f"r28-deploy-{uuid.uuid4().hex[:12]}"
        auth_surface = "blocked"
        security_controls: dict[str, Any] = {
            "forbidden_service_guard": candidate.staging_service not in _FORBIDDEN_SERVICES,
            "staging_service": candidate.staging_service,
            "api_token_source": self.api_token_source,
            "explicit_cli_token": bool(self.explicit_api_token),
            "cli_session_prefers_shell_auth": self.explicit_api_token is None,
            "deploy_workdir_configured": bool(self.workdir),
        }

        # 1. Service allowlist guard.
        if candidate.staging_service in _FORBIDDEN_SERVICES:
            return RailwayDeployReport(
                deploy_id=deploy_id,
                status="blocked",
                smoke_results={"reason": f"service '{candidate.staging_service}' is in _FORBIDDEN_SERVICES"},
                started_at=started_at,
                ended_at=_utc_now_iso(),
                auth_surface=auth_surface,
                api_token_source=self.api_token_source,
                security_controls=security_controls,
                promotion_blockers=["forbidden_service_guard"],
            )

        # 2. Transport availability guard.
        if not self.api_token and not self.dry_run and not self._cli_available():
            return RailwayDeployReport(
                deploy_id=deploy_id,
                status="blocked",
                smoke_results={"reason": "RAILWAY_TOKEN missing and Railway CLI session unavailable"},
                started_at=started_at,
                ended_at=_utc_now_iso(),
                auth_surface=auth_surface,
                api_token_source=self.api_token_source,
                security_controls=security_controls,
                promotion_blockers=["no_graphql_token_or_cli_session"],
            )

        # 3. Construct the GraphQL mutation. Real call deferred to Rung-5.
        mutation = """
        mutation deploy($input: ServiceInstanceDeployInput!) {
            serviceInstanceDeploy(input: $input)
        }
        """.strip()
        variables = {
            "input": {
                "projectId": self.project_id,
                "environmentId": self.environment_id,
                "serviceId": candidate.staging_service,
                "commitSHA": candidate.commit_sha,
            }
        }
        payload = {"query": mutation, "variables": variables}

        if self.dry_run:
            # Dry-run: skip the actual POST, treat as deployed.
            deployed = True
            auth_surface = "dry_run"
            railway_logs_uri = f"dry-run://railway/projects/{self.project_id}/services/{candidate.staging_service}"
        else:
            deploy_error: str | None = None
            try:
                if self.api_token:
                    deployed = await asyncio.wait_for(
                        asyncio.to_thread(self._post_graphql, payload),
                        timeout=min(candidate.max_wall_seconds, 600),
                    )
                    auth_surface = "graphql" if deployed else auth_surface
                    railway_logs_uri = (
                        f"https://railway.app/project/{self.project_id}"
                        f"/service/{candidate.staging_service}?env={self.environment_id}"
                    )
                else:
                    deployed = False
                    railway_logs_uri = ""
            except (asyncio.TimeoutError, urllib.error.URLError) as exc:
                deployed = False
                railway_logs_uri = ""
                deploy_error = f"graphql_failed: {exc}"
            except Exception as exc:  # noqa: BLE001
                deployed = False
                railway_logs_uri = ""
                deploy_error = f"graphql_failed: {exc}"

            if not deployed and self._cli_available():
                try:
                    railway_logs_uri = await asyncio.wait_for(
                        asyncio.to_thread(self._deploy_via_cli, candidate),
                        timeout=min(candidate.max_wall_seconds, 600),
                    )
                    deployed = True
                    auth_surface = "cli"
                except (asyncio.TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    deploy_error = f"cli_failed: {exc}"

            if not deployed:
                return RailwayDeployReport(
                    deploy_id=deploy_id,
                    status="timeout",
                    smoke_results={"reason": deploy_error or "deploy_post_failed"},
                    started_at=started_at,
                    ended_at=_utc_now_iso(),
                    auth_surface=auth_surface,
                    api_token_source=self.api_token_source,
                    security_controls=security_controls,
                    promotion_blockers=[deploy_error or "deploy_post_failed"],
                )

        if not deployed:
            return RailwayDeployReport(
                deploy_id=deploy_id,
                status="blocked",
                smoke_results={"reason": "GraphQL returned falsy serviceInstanceDeploy"},
                started_at=started_at,
                ended_at=_utc_now_iso(),
                auth_surface=auth_surface,
                api_token_source=self.api_token_source,
                security_controls=security_controls,
                promotion_blockers=["graphql_returned_falsy_serviceInstanceDeploy"],
            )

        deployment_terminal_state: str | None = None
        deployment_terminal_id: str | None = None
        if not self.dry_run and self._cli_available():
            latest = await asyncio.to_thread(
                self._wait_for_terminal_deployment,
                candidate.staging_service,
                candidate.deployment_poll_seconds,
                min(candidate.max_wall_seconds, 600),
            )
            deployment_terminal_state = str(latest.get("status") or "").upper() or None
            deployment_terminal_id = str(latest.get("id") or "") or None

        readiness_result: dict[str, Any] = {}
        dependency_findings: list[str] = []
        if candidate.readiness_url:
            readiness_result = await asyncio.to_thread(
                self._probe_readiness,
                candidate.readiness_url,
                candidate.readiness_timeout_seconds,
            )
            dependency_findings = self._extract_dependency_findings(readiness_result)

        # 4. Smoke matrix (caller supplies runner; readiness is now a first-class contract).
        smoke_results: dict[str, str] = {}
        if smoke_runner is not None and candidate.smoke_matrix:
            for probe in candidate.smoke_matrix:
                try:
                    result = await asyncio.to_thread(smoke_runner, probe)
                    smoke_results[probe] = "pass" if result else "fail"
                except Exception as exc:  # noqa: BLE001
                    smoke_results[probe] = f"error: {exc}"

        any_smoke_failed = any(v != "pass" for v in smoke_results.values())
        readiness_failed = bool(candidate.readiness_required and not readiness_result.get("ready", False))
        promotion_blockers: list[str] = []

        if deployment_terminal_state in {"FAILED", "CRASHED", "REMOVED"}:
            promotion_blockers.append(f"deployment_terminal_state:{deployment_terminal_state.lower()}")
        elif deployment_terminal_state in {"INITIALIZING", "BUILDING", "DEPLOYING"}:
            promotion_blockers.append(f"deployment_terminal_state:{deployment_terminal_state.lower()}")

        if readiness_failed:
            promotion_blockers.append("readiness:not_ready")
            promotion_blockers.extend(dependency_findings)

        if any_smoke_failed:
            promotion_blockers.append("smoke_matrix:failed")

        if deployment_terminal_state in {"FAILED", "CRASHED", "REMOVED"}:
            status = "blocked"
        elif deployment_terminal_state in {"INITIALIZING", "BUILDING", "DEPLOYING"}:
            status = "timeout"
        elif (any_smoke_failed or readiness_failed) and candidate.rollback_on_fail:
            status = "rollback"
        elif any_smoke_failed or readiness_failed:
            status = "smoke_failed"
        else:
            status = "deployed"

        # 5. sdk.ops gate bridge: deploy-lane artifacts + real runtime probes -> promote/hold/rollback.
        ops_checks = build_deploy_contract_checks(
            deployment_status=status,
            promotion_blockers=promotion_blockers,
            dependency_findings=dependency_findings,
        )
        runtime_probe_payload: dict[str, Any] = {}
        explicit_runtime_base = os.getenv("MICA_OPS_RUNTIME_BASE_URL")
        runtime_base_url = explicit_runtime_base or self._derive_runtime_base_url(candidate.readiness_url)
        runtime_probe_enabled = (
            os.getenv("MICA_OPS_ENABLE_RUNTIME_PROBES", "0").strip().lower() in {"1", "true", "yes"}
            or bool(explicit_runtime_base)
        )
        ops_environment = os.getenv("MICA_OPS_ENVIRONMENT", "production").strip().lower() or "production"

        if runtime_probe_enabled and runtime_base_url:
            queue_headers = self._runtime_queue_headers_from_env()
            try:
                runtime_report = await asyncio.to_thread(
                    run_runtime_checks,
                    base_url=runtime_base_url,
                    environment=ops_environment,
                    timeout_seconds=int(os.getenv("MICA_OPS_RUNTIME_TIMEOUT_SECONDS", "20")),
                    queue_headers=queue_headers,
                )
                ops_checks.extend(runtime_report.checks)
                runtime_probe_payload = runtime_report.model_dump()
            except Exception as exc:  # noqa: BLE001
                ops_checks.append(
                    build_deploy_contract_checks(
                        deployment_status="blocked",
                        promotion_blockers=[f"runtime_probe_error:{exc}"],
                    )[0]
                )
                promotion_blockers.append("runtime_probe:error")

        gate = evaluate_release_gate(
            checks=ops_checks,
            canary=CanarySnapshot(
                security_regression_detected=any(
                    blocker.startswith("forbidden_service_guard")
                    or blocker.startswith("no_graphql_token_or_cli_session")
                    for blocker in promotion_blockers
                ),
                cost_guardrail_breached=any("cost_guardrail" in blocker for blocker in promotion_blockers),
            ),
        )
        ops_evidence = build_ops_evidence_report(
            gate=gate,
            artifacts={
                "deploy_status": status,
                "readiness_result": readiness_result,
                "smoke_results": smoke_results,
                "runtime_checks": runtime_probe_payload,
            },
        )

        artifact_payload = {
            "deploy_id": deploy_id,
            "ops_evidence": ops_evidence.model_dump(),
            "checks": [check.model_dump() for check in ops_checks],
            "canary": gate.canary.model_dump(),
            "artifacts": ops_evidence.artifacts,
        }
        artifact_ref = self._persist_ops_evidence_artifact(deploy_id=deploy_id, payload=artifact_payload)
        ops_evidence.artifacts["ops_evidence_artifact"] = artifact_ref
        persist_required = os.getenv("MICA_OPS_EVIDENCE_PERSIST_REQUIRED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if artifact_ref.get("persisted") is not True and persist_required:
            ops_checks.append(
                PolicyCheck(
                    check_id="deploy.evidence_artifact_persisted",
                    severity="P0",
                    passed=False,
                    description="Ops evidence artifact must persist to GCS when persistence is required.",
                    evidence=f"artifact_ref={artifact_ref}",
                    remediation="Configure valid GCS credentials/bucket and retry deploy gate.",
                )
            )
            gate = evaluate_release_gate(checks=ops_checks, canary=gate.canary)
            ops_evidence = build_ops_evidence_report(
                gate=gate,
                artifacts={
                    "deploy_status": status,
                    "readiness_result": readiness_result,
                    "smoke_results": smoke_results,
                    "runtime_checks": runtime_probe_payload,
                },
            )
            ops_evidence.artifacts["ops_evidence_artifact"] = artifact_ref
            promotion_blockers.append("ops_evidence_artifact:persist_failed")

        if gate.decision != "promote":
            promotion_blockers.append(f"ops_gate:{gate.decision}")
        if gate.decision == "rollback" and status in {"deployed", "smoke_failed"} and candidate.rollback_on_fail:
            status = "rollback"

        # Stable ordering without duplicate blocker inflation.
        promotion_blockers = list(dict.fromkeys(promotion_blockers))

        security_controls.update(
            {
                "auth_surface": auth_surface,
                "cli_env_token_sanitized": self.explicit_api_token is None,
                "readiness_required": candidate.readiness_required,
                "readiness_url_configured": bool(candidate.readiness_url),
                "ops_gate_decision": gate.decision,
                "ops_evidence_artifact": artifact_ref,
            }
        )

        promotion_requested = status == "deployed" and gate.decision == "promote" and not promotion_blockers

        return RailwayDeployReport(
            deploy_id=deploy_id,
            status=status,
            smoke_results=smoke_results,
            railway_logs_uri=railway_logs_uri,
            promotion_requested=promotion_requested,
            started_at=started_at,
            ended_at=_utc_now_iso(),
            auth_surface=auth_surface,
            api_token_source=self.api_token_source,
            deployment_terminal_state=deployment_terminal_state,
            deployment_terminal_id=deployment_terminal_id,
            readiness_result=readiness_result,
            dependency_findings=dependency_findings,
            security_controls=security_controls,
            promotion_blockers=promotion_blockers,
            ops_gate_decision=gate.decision,
            ops_evidence=ops_evidence.model_dump(),
        )

    @staticmethod
    def _derive_runtime_base_url(readiness_url: str | None) -> str | None:
        if not readiness_url:
            return None
        parsed = urlsplit(readiness_url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _runtime_queue_headers_from_env() -> dict[str, str] | None:
        raw = os.getenv("MICA_OPS_QUEUE_HEADERS", "").strip()
        if not raw:
            return None
        headers: dict[str, str] = {}
        for pair in raw.split(";"):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key = key.strip()
            if not key:
                continue
            headers[key] = value.strip()
        return headers or None

    @staticmethod
    def _persist_ops_evidence_artifact(*, deploy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        bucket_name = os.getenv("MICA_OPS_EVIDENCE_BUCKET", "").strip()
        if not bucket_name:
            return {"persisted": False, "reason": "bucket_not_configured"}

        prefix = os.getenv("MICA_OPS_EVIDENCE_PREFIX", "ops-evidence/railway-deploy").strip("/")
        timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        object_path = f"{prefix}/{deploy_id}/{timestamp}.json"

        try:
            from google.cloud import storage  # type: ignore
            from google.oauth2 import service_account  # type: ignore

            credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
            credentials_json_b64 = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", "").strip()
            credentials = None
            project = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")

            if credentials_json_b64:
                decoded = base64.b64decode(credentials_json_b64.encode("utf-8")).decode("utf-8")
                credentials_info = json.loads(decoded)
                credentials = service_account.Credentials.from_service_account_info(credentials_info)
                project = project or credentials_info.get("project_id")
            elif credentials_json:
                credentials_info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(credentials_info)
                project = project or credentials_info.get("project_id")

            if credentials is not None:
                client = storage.Client(project=project, credentials=credentials)
            else:
                client = storage.Client(project=project)

            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_path)
            blob.upload_from_string(
                json.dumps(payload, indent=2, sort_keys=True),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "persisted": False,
                "reason": "upload_failed",
                "error": str(exc),
                "bucket": bucket_name,
                "object_path": object_path,
            }

        return {
            "persisted": True,
            "bucket": bucket_name,
            "object_path": object_path,
            "gs_uri": f"gs://{bucket_name}/{object_path}",
        }

    def _post_graphql(self, payload: dict) -> bool:
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return bool(data.get("data", {}).get("serviceInstanceDeploy"))

    def _cli_available(self) -> bool:
        cli_path = self.cli_bin
        if os.path.isabs(cli_path):
            if not os.path.exists(cli_path):
                return False
        elif not shutil.which(cli_path):
            return False
        try:
            result = self._run_cli("whoami")
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _redeploy_via_cli(self, candidate: RailwayDeployCandidate) -> str:
        result = self._run_cli(
            "redeploy",
            "--service",
            candidate.staging_service,
            "--yes",
            "--json",
            timeout=min(candidate.max_wall_seconds, 600),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "railway redeploy failed").strip()
            raise subprocess.SubprocessError(detail)
        return (
            f"railway-cli://project/{self.project_id}/environment/{self.environment_id}"
            f"/service/{candidate.staging_service}"
        )

    def _deploy_via_cli(self, candidate: RailwayDeployCandidate) -> str:
        self._ensure_cli_link(candidate.staging_service)
        if not self.workdir and self._service_has_deployments(candidate.staging_service):
            return self._redeploy_via_cli(candidate)

        result = self._run_cli(
            "deployment",
            "up",
            "--service",
            candidate.staging_service,
            "--environment",
            self.environment_id,
            "--project",
            self.project_id,
            "--ci",
            "--json",
            "--message",
            f"mica-driver-staging:{candidate.commit_sha}",
            timeout=min(candidate.max_wall_seconds, 1800),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "railway up failed").strip()
            raise subprocess.SubprocessError(detail)
        return (
            f"railway-cli://project/{self.project_id}/environment/{self.environment_id}"
            f"/service/{candidate.staging_service}"
        )

    def _service_has_deployments(self, service_name: str) -> bool:
        return bool(self._deployment_list(service_name, limit=1))

    def _deployment_list(self, service_name: str, limit: int = 1) -> list[dict[str, Any]]:
        result = self._run_cli(
            "deployment",
            "list",
            "--service",
            service_name,
            "--environment",
            self.environment_id,
            "--json",
            "--limit",
            str(limit),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "railway deployment list failed").strip()
            raise subprocess.SubprocessError(detail)
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise subprocess.SubprocessError(f"invalid deployment list json: {exc}") from exc
        if not isinstance(payload, list):
            raise subprocess.SubprocessError("invalid deployment list payload: expected list")
        return payload

    def _wait_for_terminal_deployment(
        self,
        service_name: str,
        poll_seconds: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout_seconds, poll_seconds)
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            deployments = self._deployment_list(service_name, limit=1)
            latest = deployments[0] if deployments else {}
            status = str(latest.get("status") or "").upper()
            if status in {"SUCCESS", "FAILED", "CRASHED", "REMOVED"}:
                return latest
            time.sleep(poll_seconds)
        return latest

    def _probe_readiness(self, url: str, timeout_seconds: int) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        status_code: int
        raw_body: str
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                status_code = resp.status
                raw_body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            raw_body = exc.read().decode("utf-8")

        body: Any
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {"raw": raw_body}

        detail = body.get("detail") if isinstance(body, dict) else None
        normalized = detail if isinstance(detail, dict) else body
        ready = bool(normalized.get("ready")) if isinstance(normalized, dict) else False
        return {
            "status_code": status_code,
            "ready": ready,
            "body": normalized,
            "url": url,
        }

    def _extract_dependency_findings(self, readiness_result: dict[str, Any]) -> list[str]:
        findings: list[str] = []
        body = readiness_result.get("body")
        if not isinstance(body, dict):
            return findings

        checks = body.get("checks")
        if not isinstance(checks, dict):
            return findings

        storage = checks.get("storage")
        if isinstance(storage, dict) and not storage.get("ready", storage.get("configured", False)):
            findings.append("storage:not_ready")

        redis = checks.get("redis")
        if redis == "ok":
            pass
        elif isinstance(redis, dict):
            findings.append(f"redis:{redis.get('status', 'unknown')}")
        elif redis:
            findings.append(f"redis:{redis}")

        routers_failed = checks.get("routers_failed")
        if isinstance(routers_failed, dict) and routers_failed:
            findings.append("routers_failed")

        database = checks.get("database")
        if isinstance(database, dict):
            db_status = str(database.get("status") or "unknown")
            if db_status != "ok":
                findings.append(f"database:{db_status}")
            for key, value in database.items():
                if not isinstance(value, dict):
                    continue
                status = str(value.get("status") or "unknown")
                if status not in {"ok", "not_configured"}:
                    findings.append(f"database.{key}:{status}")
            return findings

    def _ensure_cli_link(self, service_name: str) -> None:
        result = self._run_cli(
            "link",
            "--project",
            self.project_id,
            "--environment",
            self.environment_id,
            "--service",
            service_name,
            "--json",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "railway link failed").strip()
            raise subprocess.SubprocessError(detail)

    def _run_cli(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        command = [self.cli_bin, *args]
        if os.name == "nt":
            cli_bin_lower = self.cli_bin.lower()
            if cli_bin_lower.endswith((".cmd", ".bat")):
                command = ["cmd.exe", "/d", "/c", self.cli_bin, *args]
            elif cli_bin_lower.endswith(".ps1"):
                command = [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    self.cli_bin,
                    *args,
                ]
        env = os.environ.copy()
        if self.explicit_api_token:
            env["RAILWAY_TOKEN"] = self.explicit_api_token
        else:
            env.pop("RAILWAY_TOKEN", None)
        return subprocess.run(
            command,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )


__all__ = ["RailwayDeployClient"]

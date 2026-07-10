from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from mica.infrastructure.orchestration.vast_md_orchestrator import (
    MDJobConfig,
    OrchestratorPhase,
    ReplicaStatus,
    ResumeSpec,
    SimStatus,
    SimulationMode,
    VastMDOrchestrator,
)
from mica.infrastructure.orchestration.sp04_durability_truth import write_sp04_packet
from mica.infrastructure.persistence.remote_md_result_synthesis import (
    build_remote_md_result,
    materialize_remote_md_output_json,
)
from mica.infrastructure.persistence.remote_md_session_registry import (
    RemoteMDSessionRegistry,
    build_sp04_runtime_packet,
    create_default_remote_md_session_registry,
)
from mica.infrastructure.providers.base_provider import InstanceStatus
from mica.infrastructure.providers.vast_provider import VastProvider
from mica.infrastructure.ssh_resilience import ResilientSSHExecutor


def _utcnow_dt() -> datetime:
    return datetime.now(UTC)


def _utcnow_iso() -> str:
    return _utcnow_dt().isoformat().replace("+00:00", "Z")


@dataclass
class RemoteMDReconcileOutcome:
    session_id: str
    status: str
    action: str
    output_json: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "action": self.action,
            "output_json": self.output_json,
            "reason": self.reason,
        }


class RemoteMDReconciler:
    def __init__(
        self,
        *,
        registry: RemoteMDSessionRegistry,
        provider: Optional[VastProvider] = None,
        ssh: Optional[ResilientSSHExecutor] = None,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.ssh = ssh or ResilientSSHExecutor()

    def _get_provider(self) -> VastProvider:
        if self.provider is None:
            self.provider = VastProvider()
        return self.provider

    @staticmethod
    def _session_teardown_proof(session: Dict[str, Any]) -> Dict[str, Any]:
        proof = session.get("teardown_proof")
        if isinstance(proof, dict):
            return dict(proof)
        result = session.get("result")
        if isinstance(result, dict):
            nested = result.get("execution_result_v1")
            if isinstance(nested, dict):
                nested_proof = nested.get("teardown_proof")
                if isinstance(nested_proof, dict):
                    return dict(nested_proof)
            result_proof = result.get("teardown_proof")
            if isinstance(result_proof, dict):
                return dict(result_proof)
        return {}

    def _needs_terminal_teardown_reconcile(self, session: Dict[str, Any]) -> bool:
        status = str(session.get("status") or "").strip().lower()
        if status != "failed_recoverable":
            return False
        proof = self._session_teardown_proof(session)
        if bool(proof.get("orphan_sweep_triggered")):
            return False
        destroy_attempted = bool(proof.get("destroy_attempted"))
        destroy_succeeded = bool(proof.get("destroy_succeeded"))
        orphan_scan_result = str(proof.get("orphan_scan_result") or "").strip().lower()
        if orphan_scan_result in {"clean", "none"}:
            return False
        return destroy_attempted and not destroy_succeeded

    async def _reconcile_terminal_teardown_uncertainty(
        self,
        session_id: str,
        session: Dict[str, Any],
        *,
        output_json: str,
    ) -> RemoteMDReconcileOutcome:
        proof = self._session_teardown_proof(session)
        instance_id = str(session.get("instance_id") or proof.get("instance_id") or "").strip()
        result_payload = dict(session.get("result") or {})
        execution_result = dict(result_payload.get("execution_result_v1") or {})

        def _build_patch(
            *,
            orphan_scan_result: str,
            sweep_action: str,
            destroy_attempted: bool,
            destroy_succeeded: bool,
            destroy_error: str = "",
            provider_instance_status: str = "",
        ) -> Dict[str, Any]:
            merged_proof = dict(proof)
            merged_proof.update(
                {
                    "instance_id": instance_id or str(merged_proof.get("instance_id") or "").strip(),
                    "destroy_attempted": destroy_attempted,
                    "destroy_succeeded": destroy_succeeded,
                    "preserved_for_recovery": not destroy_succeeded,
                    "orphan_scan_result": orphan_scan_result,
                    "orphan_sweep_triggered": True,
                    "orphan_sweep_action": sweep_action,
                    "teardown_state": "completed" if destroy_succeeded else "unknown",
                    "destroy_skipped_reason": destroy_error or str(merged_proof.get("destroy_skipped_reason") or ""),
                }
            )
            patched_result = dict(result_payload)
            if execution_result:
                patched_execution = dict(execution_result)
                patched_execution["teardown_proof"] = dict(merged_proof)
                backend_native = dict(patched_execution.get("backend_native") or {})
                backend_native["orphan_sweep_action"] = sweep_action
                patched_execution["backend_native"] = backend_native
                patched_result["execution_result_v1"] = patched_execution
            patched_result["teardown_proof"] = dict(merged_proof)
            return {
                "status": "failed_recoverable",
                "recoverable": True,
                "teardown_proof": merged_proof,
                "orphan_scan_result": orphan_scan_result,
                "reconcile_forced_destroy": destroy_attempted,
                "reconcile_destroy_result": destroy_succeeded if destroy_attempted else False,
                "reconcile_destroy_error": destroy_error,
                "reconcile_destroy_timestamp_iso": _utcnow_iso(),
                "provider_instance_status": provider_instance_status,
                "result": patched_result,
            }

        if not instance_id:
            patch = _build_patch(
                orphan_scan_result="detected",
                sweep_action="missing_instance_id",
                destroy_attempted=False,
                destroy_succeeded=False,
                destroy_error="terminal teardown uncertainty without instance_id",
            )
            await self.registry.update_session(session_id, patch)
            updated = await self.registry.get_session(session_id) or {**session, **patch}
            written = materialize_remote_md_output_json(updated, output_json=output_json, result_override=patch["result"])
            await self.registry.update_session(session_id, {"output_json": written})
            updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
            await self._materialize_sp04_runtime_evidence(session_id, updated)
            return RemoteMDReconcileOutcome(
                session_id=session_id,
                status="failed_recoverable",
                action="terminal_teardown_missing_instance_id",
                output_json=written,
            )

        try:
            instance = await self._get_provider().get_instance_status(instance_id)
        except Exception as exc:
            patch = _build_patch(
                orphan_scan_result="detected",
                sweep_action="provider_lookup_failed",
                destroy_attempted=False,
                destroy_succeeded=False,
                destroy_error=f"provider lookup failed during terminal teardown reconcile: {exc}",
            )
            await self.registry.update_session(session_id, patch)
            updated = await self.registry.get_session(session_id) or {**session, **patch}
            written = materialize_remote_md_output_json(updated, output_json=output_json, result_override=patch["result"])
            await self.registry.update_session(session_id, {"output_json": written})
            updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
            await self._materialize_sp04_runtime_evidence(session_id, updated)
            return RemoteMDReconcileOutcome(
                session_id=session_id,
                status="failed_recoverable",
                action="terminal_teardown_provider_lookup_failed",
                output_json=written,
                reason=str(exc),
            )

        provider_status = instance.status.value
        if instance.status in {InstanceStatus.RUNNING, InstanceStatus.PROVISIONING, InstanceStatus.PENDING}:
            destroy_error = ""
            try:
                destroy_result = await self._get_provider().destroy_instance(instance_id)
            except Exception as destroy_exc:
                destroy_result = False
                destroy_error = str(destroy_exc)
            patch = _build_patch(
                orphan_scan_result="clean" if destroy_result else "detected",
                sweep_action="forced_destroy",
                destroy_attempted=True,
                destroy_succeeded=bool(destroy_result),
                destroy_error=destroy_error,
                provider_instance_status=provider_status,
            )
            await self.registry.update_session(session_id, patch)
            updated = await self.registry.get_session(session_id) or {**session, **patch}
            written = materialize_remote_md_output_json(updated, output_json=output_json, result_override=patch["result"])
            await self.registry.update_session(session_id, {"output_json": written})
            updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
            await self._materialize_sp04_runtime_evidence(session_id, updated)
            return RemoteMDReconcileOutcome(
                session_id=session_id,
                status="failed_recoverable",
                action="terminal_teardown_forced_destroy" if destroy_result else "terminal_teardown_orphan_detected",
                output_json=written,
                reason=destroy_error,
            )

        patch = _build_patch(
            orphan_scan_result="clean",
            sweep_action="provider_already_ended",
            destroy_attempted=False,
            destroy_succeeded=False,
            provider_instance_status=provider_status,
        )
        await self.registry.update_session(session_id, patch)
        updated = await self.registry.get_session(session_id) or {**session, **patch}
        written = materialize_remote_md_output_json(updated, output_json=output_json, result_override=patch["result"])
        await self.registry.update_session(session_id, {"output_json": written})
        updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
        await self._materialize_sp04_runtime_evidence(session_id, updated)
        return RemoteMDReconcileOutcome(
            session_id=session_id,
            status="failed_recoverable",
            action="terminal_teardown_provider_already_ended",
            output_json=written,
        )

    async def _materialize_sp04_runtime_evidence(
        self,
        session_id: str,
        session: Dict[str, Any],
    ) -> Dict[str, Any]:
        packet = dict(session.get("sp04_packet") or build_sp04_runtime_packet(session))
        output_dir = Path.cwd() / ".mica" / "logs" / "compute_lab_live" / f"stone_md_infra_sp04_{session_id}"
        written = write_sp04_packet(packet, output_dir)
        patch = {
            "sp04_packet": packet,
            "sp04_gate_verdict": dict(packet.get("gate_verdict") or {}),
            "sp04_packet_dir": str(output_dir),
            "sp04_evidence_paths": {key: str(path) for key, path in written.items()},
        }
        await self.registry.update_session(session_id, patch)
        return patch

    async def reconcile_session(
        self,
        session_id: str,
        *,
        heartbeat_timeout_sec: int = 120,
        force: bool = False,
    ) -> RemoteMDReconcileOutcome:
        session = await self.registry.get_session(session_id)
        if session is None:
            raise KeyError(f"unknown session_id={session_id}")

        session_status = str(session.get("status") or "unknown").lower()
        output_json = str(session.get("output_json") or "").strip()
        if session_status in {"completed", "failed", "failed_recoverable", "error", "interrupted", "lost", "cancelled"}:
            if self._needs_terminal_teardown_reconcile(session):
                return await self._reconcile_terminal_teardown_uncertainty(
                    session_id,
                    session,
                    output_json=output_json,
                )
            if not self._output_exists(output_json):
                written = materialize_remote_md_output_json(session, output_json=output_json)
                await self.registry.update_session(session_id, {"output_json": written})
                session = await self.registry.get_session(session_id) or {**session, "output_json": written}
                await self._materialize_sp04_runtime_evidence(session_id, session)
                return RemoteMDReconcileOutcome(session_id=session_id, status=session_status, action="synthesized_terminal_json", output_json=written)
            await self._materialize_sp04_runtime_evidence(session_id, session)
            return RemoteMDReconcileOutcome(session_id=session_id, status=session_status, action="no_op_terminal")

        if not force and not self._heartbeat_expired(session, heartbeat_timeout_sec=heartbeat_timeout_sec):
            return RemoteMDReconcileOutcome(session_id=session_id, status=session_status or "running", action="no_op_fresh")

        await self.registry.mark_orphaned(session_id)
        session = await self.registry.get_session(session_id) or session

        instance_id = str(session.get("instance_id") or "").strip()
        if not instance_id:
            if self._has_recoverable_contracts(session):
                updated = await self.registry.mark_failed_recoverable(session_id, "launcher lost and no instance_id; resume contracts still present")
                written = materialize_remote_md_output_json(updated, output_json=output_json)
                await self.registry.update_session(session_id, {"output_json": written})
                updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
                await self._materialize_sp04_runtime_evidence(session_id, updated)
                return RemoteMDReconcileOutcome(session_id=session_id, status="failed_recoverable", action="marked_failed_recoverable", output_json=written)
            updated = await self.registry.mark_lost(session_id, "launcher lost and no instance_id available")
            written = materialize_remote_md_output_json(updated, output_json=output_json)
            await self.registry.update_session(session_id, {"output_json": written})
            updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
            await self._materialize_sp04_runtime_evidence(session_id, updated)
            return RemoteMDReconcileOutcome(session_id=session_id, status="lost", action="marked_lost", output_json=written)

        try:
            instance = await self._get_provider().get_instance_status(instance_id)
        except Exception as exc:
            if self._has_recoverable_contracts(session):
                updated = await self.registry.mark_failed_recoverable(session_id, f"provider lookup failed but contracts remain: {exc}")
                written = materialize_remote_md_output_json(updated, output_json=output_json)
                await self.registry.update_session(session_id, {"output_json": written})
                updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
                await self._materialize_sp04_runtime_evidence(session_id, updated)
                return RemoteMDReconcileOutcome(session_id=session_id, status="failed_recoverable", action="provider_lookup_failed_recoverable", output_json=written, reason=str(exc))
            updated = await self.registry.mark_lost(session_id, f"provider lookup failed: {exc}")
            written = materialize_remote_md_output_json(updated, output_json=output_json)
            await self.registry.update_session(session_id, {"output_json": written})
            updated = await self.registry.get_session(session_id) or {**updated, "output_json": written}
            await self._materialize_sp04_runtime_evidence(session_id, updated)
            return RemoteMDReconcileOutcome(session_id=session_id, status="lost", action="provider_lookup_failed_lost", output_json=written, reason=str(exc))

        if instance.status in {InstanceStatus.RUNNING, InstanceStatus.PROVISIONING, InstanceStatus.PENDING}:
            patch = {
                "instance_id": instance.instance_id,
                "ssh_host": instance.ssh_host,
                "ssh_port": instance.ssh_port,
                "provider_instance_status": instance.status.value,
            }
            await self.registry.update_session(session_id, patch)
            if instance.status != InstanceStatus.RUNNING:
                await self.registry.update_session(session_id, {"status": "running", "recoverable": True})
                return RemoteMDReconcileOutcome(session_id=session_id, status="running", action="instance_not_ready")

            inspection = await self._inspect_live_session({**session, **patch})
            inspection_status = str(inspection.get("status") or "running")
            await self.registry.update_session(session_id, inspection)
            if inspection_status == "completed":
                refreshed = await self.registry.get_session(session_id) or {**session, **inspection}
                written = materialize_remote_md_output_json(refreshed, output_json=output_json, result_override=inspection.get("result"))
                await self.registry.mark_completed(session_id, {**inspection, "output_json": written})
                refreshed = await self.registry.get_session(session_id) or {**refreshed, **inspection, "output_json": written}
                await self._materialize_sp04_runtime_evidence(session_id, refreshed)
                return RemoteMDReconcileOutcome(session_id=session_id, status="completed", action="downloaded_and_completed", output_json=written)
            return RemoteMDReconcileOutcome(session_id=session_id, status=inspection_status, action=str(inspection.get("action") or "still_running"))

        # ── Corrective convergence: instance ENDED, force destroy if not already terminal ────
        session_status = str(session.get("status") or "").lower()
        if session_status and session_status not in {"completed", "failed", "cancelled", "lost"}:
            # Session is not terminal but provider reports instance ENDED → force destroy attempt
            try:
                destroy_result = await self._get_provider().destroy_instance(instance_id)
                patch = {
                    "provider_instance_status": instance.status.value,
                    "reconcile_forced_destroy": True,
                    "reconcile_destroy_result": destroy_result,
                    "reconcile_destroy_timestamp_iso": _utcnow_iso(),
                }
                await self.registry.update_session(session_id, patch)
            except Exception as destroy_exc:
                patch = {
                    "provider_instance_status": instance.status.value,
                    "reconcile_forced_destroy": True,
                    "reconcile_destroy_error": str(destroy_exc),
                    "reconcile_destroy_timestamp_iso": _utcnow_iso(),
                }
                await self.registry.update_session(session_id, patch)

        if self._has_recoverable_contracts(session):
            updated = await self.registry.mark_failed_recoverable(session_id, f"instance ended in state {instance.status.value}; contracts remain; reconcile forced destroy attempt executed")
            written = materialize_remote_md_output_json(updated, output_json=output_json)
            await self.registry.update_session(session_id, {"output_json": written, "provider_instance_status": instance.status.value})
            updated = await self.registry.get_session(session_id) or {**updated, "output_json": written, "provider_instance_status": instance.status.value}
            await self._materialize_sp04_runtime_evidence(session_id, updated)
            return RemoteMDReconcileOutcome(session_id=session_id, status="failed_recoverable", action="instance_ended_recoverable_with_corrective_destroy", output_json=written)

        updated = await self.registry.mark_lost(session_id, f"instance ended in state {instance.status.value} and no recoverable contracts remain; reconcile forced destroy attempt executed")
        written = materialize_remote_md_output_json(updated, output_json=output_json)
        await self.registry.update_session(session_id, {"output_json": written, "provider_instance_status": instance.status.value})
        updated = await self.registry.get_session(session_id) or {**updated, "output_json": written, "provider_instance_status": instance.status.value}
        await self._materialize_sp04_runtime_evidence(session_id, updated)
        return RemoteMDReconcileOutcome(session_id=session_id, status="lost", action="instance_ended_lost_with_corrective_destroy", output_json=written)

    async def reconcile_stale_sessions(
        self,
        *,
        heartbeat_timeout_sec: int = 120,
        force: bool = False,
    ) -> List[RemoteMDReconcileOutcome]:
        sessions = await self.registry.list_active_sessions()
        outcomes: List[RemoteMDReconcileOutcome] = []
        for session in sessions:
            if force or self._heartbeat_expired(session, heartbeat_timeout_sec=heartbeat_timeout_sec):
                outcomes.append(await self.reconcile_session(str(session.get("session_id") or ""), heartbeat_timeout_sec=heartbeat_timeout_sec, force=True))
        return outcomes

    async def _inspect_live_session(self, session: Dict[str, Any]) -> Dict[str, Any]:
        orchestrator = self._build_orchestrator(session)
        if not orchestrator.state.ssh_host or not orchestrator.state.run_dir:
            return {
                "status": "running",
                "recoverable": True,
                "action": "missing_ssh_or_run_dir",
            }

        replica_ids = sorted(orchestrator.state.replicas) or list(range(1, orchestrator.cfg.n_replicas + 1))
        statuses = []
        for replica_id in replica_ids:
            statuses.append(await orchestrator._check_replica(replica_id))

        if statuses and all(status.status == SimStatus.COMPLETE for status in statuses):
            orchestrator.state.completed_at = _utcnow_dt()
            orchestrator.state.phase = OrchestratorPhase.COMPLETE
            await orchestrator._fase_8_download()
            state_dict = orchestrator.to_dict()
            result = build_remote_md_result(
                session,
                result_override={
                    "workflow": "protein_ligand_md",
                    "execution_mode": "remote_vast",
                    "status": "completed",
                    "success": True,
                    "vast_phase_final": OrchestratorPhase.COMPLETE.value,
                    "instance_id": orchestrator.state.instance_id,
                    "ssh_host": orchestrator.state.ssh_host,
                    "ssh_port": orchestrator.state.ssh_port,
                    "output_dir": orchestrator.state.local_output_dir,
                    "results_json": state_dict,
                    "resume_spec_path": orchestrator.state.latest_resume_spec_path,
                    "artifact_manifest_path": orchestrator.state.latest_job_manifest_path,
                },
            )
            return {
                "status": "completed",
                "result": result,
                "results_json": state_dict,
                "artifact_manifest_path": orchestrator.state.latest_job_manifest_path,
                "resume_spec_path": orchestrator.state.latest_resume_spec_path,
                "output_dir": orchestrator.state.local_output_dir,
                "finished_at": _utcnow_iso(),
                "action": "remote_complete_detected",
            }

        current_ns = max((float(status.current_ns) for status in statuses), default=0.0)
        max_speed = max((float(status.speed_ns_day) for status in statuses), default=0.0)
        return {
            "status": "running",
            "recoverable": True,
            "current_ns": current_ns,
            "speed_ns_day": max_speed,
            "results_json": orchestrator.state.to_dict(),
            "action": "still_running",
        }

    def _build_orchestrator(self, session: Dict[str, Any]) -> VastMDOrchestrator:
        context = dict(session.get("context") or {})
        resume_spec_path = str(session.get("resume_spec_path") or context.get("resume_spec_path") or "").strip()
        config_data: Dict[str, Any]
        if resume_spec_path and Path(resume_spec_path).is_file():
            spec = ResumeSpec.from_dict(json.loads(Path(resume_spec_path).read_text(encoding="utf-8")))
            config_data = dict(spec.config)
            config_data.setdefault("pdb_path", spec.pdb_path)
            config_data.setdefault("simulation_script", spec.simulation_script)
            config_data.setdefault("extractor_script", spec.extractor_script)
            config_data.setdefault("simulation_mode", spec.simulation_mode)
            config_data.setdefault("steps", spec.target_steps)
            config_data.setdefault("production_ns", spec.target_production_ns)
            config_data.setdefault("storage_backend", spec.storage_backend)
            config_data.setdefault("resume_spec", spec)
            config_data.setdefault("resume_spec_path", resume_spec_path)
        else:
            config_data = {
                "pdb_path": context.get("protein_pdb") or context.get("pdb_path") or "unknown.pdb",
                "steps": int(context.get("steps", 75_000_000)),
                "n_replicas": int(context.get("n_replicas", 1)),
                "production_ns": float(context.get("production_ns", 100.0)),
                "simulation_mode": context.get("simulation_mode", SimulationMode.BINDING.value),
                "storage_backend": context.get("storage_backend", "none"),
            }
        config_data.setdefault("job_id", str(session.get("job_id") or context.get("job_id") or ""))
        config_data.setdefault("ssh_key_path", str(context.get("ssh_key_path") or os.path.expanduser("~/.ssh/vast_key")))
        config_data.setdefault("local_output_dir", str(session.get("output_dir") or context.get("output_dir") or session.get("local_output_dir") or ""))
        config = MDJobConfig(**config_data)
        orchestrator = VastMDOrchestrator(config, provider=self._get_provider(), ssh=self.ssh)
        orchestrator.state.job_id = str(session.get("job_id") or config.job_id)
        orchestrator.state.instance_id = str(session.get("instance_id") or "")
        orchestrator.state.ssh_host = str(session.get("ssh_host") or "")
        orchestrator.state.ssh_port = int(session.get("ssh_port") or 22)
        orchestrator.state.run_dir = str(session.get("run_dir") or session.get("results_json", {}).get("run_dir") or (getattr(config.resume_spec, "run_dir", "") if getattr(config, "resume_spec", None) else "") or f"/workspace/{config.run_dir_name}")
        orchestrator.state.local_output_dir = str(session.get("output_dir") or config.local_output_dir)
        orchestrator.state.latest_job_manifest_path = str(session.get("artifact_manifest_path") or orchestrator.state.latest_job_manifest_path)
        orchestrator.state.latest_resume_spec_path = str(session.get("resume_spec_path") or orchestrator.state.latest_resume_spec_path)
        if getattr(config, "resume_spec", None) is not None and config.resume_spec.replicas:
            for replica in config.resume_spec.replicas:
                seeded = ReplicaStatus(replica_id=replica.replica_id, gpu_id=replica.replica_id - 1)
                if replica.checkpoint_step > 0:
                    seeded.current_step = int(replica.checkpoint_step)
                orchestrator.state.replicas[replica.replica_id] = seeded
        if not orchestrator.state.replicas:
            for replica_id in range(1, config.n_replicas + 1):
                orchestrator.state.replicas[replica_id] = ReplicaStatus(replica_id=replica_id, gpu_id=replica_id - 1)
        return orchestrator

    @staticmethod
    def _output_exists(output_json: str) -> bool:
        candidate = str(output_json or "").strip()
        return bool(candidate) and Path(candidate).is_file()

    @staticmethod
    def _heartbeat_expired(session: Dict[str, Any], *, heartbeat_timeout_sec: int) -> bool:
        last = str(session.get("last_heartbeat_at") or session.get("updated_at") or "").strip()
        if not last:
            return True
        try:
            ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return True
        return ts <= datetime.now(ts.tzinfo) - timedelta(seconds=max(1, heartbeat_timeout_sec))

    @staticmethod
    def _has_recoverable_contracts(session: Dict[str, Any]) -> bool:
        manifest_path = str(session.get("artifact_manifest_path") or "").strip()
        resume_spec_path = str(session.get("resume_spec_path") or "").strip()
        if manifest_path and Path(manifest_path).is_file():
            return True
        if resume_spec_path and Path(resume_spec_path).is_file():
            return True
        return bool(session.get("output_dir") or session.get("storage_backend") == "rclone")


async def create_default_remote_md_reconciler(registry_path: str = "") -> RemoteMDReconciler:
    registry = await create_default_remote_md_session_registry(registry_path)
    return RemoteMDReconciler(registry=registry)

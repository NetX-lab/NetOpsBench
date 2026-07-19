"""Warm runtime leasing and the real Containerlab simulator backend."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.runtime.health import check_worker_health
from netopsbench.platform.runtime.lifecycle import ensure_worker_observability, ensure_worker_pingmesh
from netopsbench.platform.runtime.manager import RuntimeManager, RuntimePool
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.incident_backend import ExecutorIncidentBackend
from netopsbench.platform.simulator.contracts import SimulatorConfig, ToolAction
from netopsbench.platform.topology.topology_utils import load_topology_manifest

logger = get_logger(__name__)

_PROVISION_LOCK = threading.Lock()
_LEASE_WAIT_TIMEOUT_SECONDS = 1_800


class _BaselineNotReadyError(RuntimeError):
    """The runtime is healthy, but its initial observation is not yet clean."""


@dataclass
class WarmRuntime:
    runtime: RuntimePool
    runner: ScenarioExecutor
    in_use: bool = False
    acquired_at: float = 0.0
    baseline_signature: str | None = None
    baseline: dict[str, Any] | None = None
    quarantined: bool = False


class RuntimeLeasePool:
    """Own warm runtimes and grant one exclusive episode lease at a time."""

    def __init__(
        self,
        manager: RuntimeManager,
        registry: ScaleRegistry,
        config: SimulatorConfig,
        fault_registry: Any = None,
    ):
        self.manager = manager
        self.registry = registry
        self.config = config
        self.fault_registry = fault_registry
        self._runtimes: dict[str, list[WarmRuntime]] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)

    def acquire(self, scale: str) -> WarmRuntime:
        deadline = time.monotonic() + _LEASE_WAIT_TIMEOUT_SECONDS
        with self._condition:
            while True:
                self.reap_orphans()
                records = self._runtimes.setdefault(scale, [])
                available = next(
                    (record for record in records if not record.in_use and not record.quarantined),
                    None,
                )
                if available is None:
                    self._make_capacity_for(scale)
                    records = self._runtimes.setdefault(scale, [])
                if available is None and self._runtime_count() < self.config.max_active_runtimes:
                    available = self._provision(scale)
                    records.append(available)
                if available is not None:
                    available.in_use = True
                    available.acquired_at = time.monotonic()
                    return available

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Timed out waiting for runtime lease for scale {scale}; "
                        f"global capacity is {self.config.max_active_runtimes}"
                    )
                self._condition.wait(timeout=remaining)

    def release(self, record: WarmRuntime) -> None:
        with self._condition:
            record.in_use = False
            record.acquired_at = 0.0
            self._condition.notify_all()

    def quarantine(self, record: WarmRuntime) -> None:
        with self._condition:
            record.quarantined = True
            record.in_use = False
            try:
                record.runner._stop_traffic()
            except Exception:
                logger.warning("Failed to stop traffic while quarantining runtime", exc_info=True)
            try:
                record.runtime.teardown()
            except Exception:
                logger.warning("Failed to tear down quarantined runtime", exc_info=True)
            records = self._runtimes.get(record.runtime.scale, [])
            if record in records:
                records.remove(record)
            self._condition.notify_all()

    def drain(self, scales: set[str] | None = None) -> None:
        with self._lock:
            selected = [
                record
                for scale, records in self._runtimes.items()
                if scales is None or scale in scales
                for record in records
            ]
            for record in selected:
                self.quarantine(record)
            if scales is None:
                self._runtimes.clear()
            else:
                for scale in scales:
                    self._runtimes.pop(scale, None)

    def reap_orphans(self) -> None:
        now = time.monotonic()
        ttl = self.config.orphan_lease_ttl_seconds
        stale = [
            record
            for records in self._runtimes.values()
            for record in records
            if record.in_use and record.acquired_at and now - record.acquired_at > ttl
        ]
        for record in stale:
            self.quarantine(record)

    def _runtime_count(self) -> int:
        return sum(len(records) for records in self._runtimes.values())

    def _make_capacity_for(self, scale: str) -> None:
        if self._runtime_count() < self.config.max_active_runtimes:
            return
        candidates = [
            record
            for candidate_scale, records in self._runtimes.items()
            if candidate_scale != scale
            for record in records
            if not record.in_use
        ]
        for record in candidates:
            self.quarantine(record)
            if self._runtime_count() < self.config.max_active_runtimes:
                return

    def _provision(self, scale: str) -> WarmRuntime:
        with _PROVISION_LOCK:
            runtime = self.manager.provision(
                scale=scale,
                workers=1,
                name=f"sim-{scale}-{uuid.uuid4().hex[:8]}",
            )
        worker = runtime.workers[0]
        runner = ScenarioExecutor(
            topology_dir=str(worker.topology_dir),
            topology_id=worker.topology_id,
            influxdb_bucket=worker.bucket,
            persist_results=False,
            fault_registry=self.fault_registry,
            scale_registry=self.registry,
        )
        return WarmRuntime(runtime=runtime, runner=runner)


class RuntimeEpisodeBackend:
    """Bridge one interactive environment to a leased real runtime."""

    def __init__(self, leases: RuntimeLeasePool, registry: ScaleRegistry):
        self.leases = leases
        self.registry = registry
        self.record: WarmRuntime | None = None
        self.scenario: ScenarioSpec | None = None
        self.episode_result: dict[str, Any] | None = None
        self.delegate: ExecutorIncidentBackend | None = None
        self.topology_dir: str | None = None

    def prepare(self, scenario: ScenarioSpec) -> dict[str, Any]:
        self.record = self.leases.acquire(scenario.scale)
        self.scenario = scenario
        runner = self.record.runner
        worker = self.record.runtime.workers[0]
        self.topology_dir = str(worker.topology_dir)
        try:
            recovery = runner._recover_fault()
            if any(not item.get("recovered", False) for item in recovery):
                raise RuntimeError(f"Runtime recovery failed: {recovery}")
            if recovery:
                runner.sleep(runner.post_recovery_wait_seconds)
            errors = self._health_errors(self.record, refresh=True)
            if errors:
                raise RuntimeError("Runtime health check failed: " + "; ".join(errors))
            if runner.traffic_controller is None or not runner.traffic_controller.active_flows:
                runner._stop_traffic()
                runner._setup_traffic(scenario.scale, "standard")
                self.record.baseline_signature = None
            self._ensure_baseline(scenario)
            self.delegate = ExecutorIncidentBackend(
                runner,
                setup_traffic=False,
                wait_for_baseline=False,
                baseline_window=self.record.baseline,
                influxdb_bucket=worker.bucket,
                topology_id=worker.topology_id,
            )
            observation = self.delegate.prepare(scenario)
            self.episode_result = self.delegate.episode_result
            return observation
        except _BaselineNotReadyError:
            # A newly started Pingmesh/traffic window can contain transient
            # anomalies while the otherwise healthy runtime settles. Reuse it
            # for the caller's reset retry instead of paying for a full
            # teardown/provision cycle.
            self.finish(broken=False)
            raise
        except Exception:
            self.finish(broken=True)
            raise

    def call_tool(self, action: ToolAction) -> dict[str, Any]:
        if self.delegate is None:
            raise RuntimeError("Incident backend is not prepared")
        return self.delegate.call_tool(action)

    def finish(self, *, broken: bool = False) -> None:
        record = self.record
        if record is None:
            return
        requested_broken = broken
        failure = ""
        try:
            if self.delegate is not None:
                try:
                    self.delegate.finish(broken=broken)
                except Exception as exc:
                    broken = True
                    failure = f"{type(exc).__name__}: {exc}"
                health_errors = self._health_errors(record, refresh=True)
                if health_errors:
                    broken = True
                    failure = "Runtime health check failed after recovery: " + "; ".join(health_errors)
        except Exception as exc:
            broken = True
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            self.scenario = None
            self.episode_result = None
            self.delegate = None
            self.record = None
        if broken:
            self.leases.quarantine(record)
        else:
            self.leases.release(record)
        if broken and not requested_broken:
            raise RuntimeError(failure or "Runtime cleanup failed")

    def close(self) -> None:
        self.finish()

    def _ensure_baseline(self, scenario: ScenarioSpec) -> None:
        if self.record is None:
            raise RuntimeError("Runtime lease is missing")
        runner = self.record.runner
        manifest = load_topology_manifest(Path(runner.topology_dir))
        profile = self.registry.get(scenario.scale)
        signature = f"{profile.digest}:{manifest.model_dump_json()}:{id(runner.traffic_controller)}"
        if self.record.baseline_signature == signature and self.record.baseline is not None:
            return
        duration = max(60, manifest.pingmesh.coverage_epoch_seconds(manifest.facts.total_clients))
        baseline = runner._wait_and_observe(duration)
        if baseline.get("data_source_status") != "ok" or baseline.get("coverage_status") != "complete":
            raise _BaselineNotReadyError(
                "Healthy baseline is unavailable: "
                f"data={baseline.get('data_source_status')} coverage={baseline.get('coverage_status')}"
            )
        hard_types = {"packet_loss", "path_unreachable", "mtu_or_fragmentation_suspect"}
        hard_anomalies = [
            anomaly
            for anomaly in (baseline.get("pingmesh_metrics", {}).get("anomalies") or [])
            if anomaly.get("type") in hard_types or anomaly.get("severity") in {"medium", "high"}
        ]
        if hard_anomalies:
            raise _BaselineNotReadyError(
                f"Healthy baseline contains {len(hard_anomalies)} hard Pingmesh anomalies"
            )
        self.record.baseline_signature = signature
        self.record.baseline = baseline

    def _health_errors(self, record: WarmRuntime, *, refresh: bool) -> list[str]:
        worker = record.runtime.workers[0]
        errors = check_worker_health(worker, scale_registry=self.registry)
        if not errors or not refresh:
            return errors
        ensure_worker_observability(worker)
        ensure_worker_pingmesh(worker)
        record.baseline_signature = None
        record.baseline = None
        return check_worker_health(worker, scale_registry=self.registry)


__all__ = [
    "RuntimeEpisodeBackend",
    "RuntimeLeasePool",
    "WarmRuntime",
]

"""Warm runtime leasing and the real Containerlab simulator backend."""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.incident.contracts import SimulatorConfig, ToolAction
from netopsbench.platform.runtime.manager import RuntimeManager, RuntimePool, RuntimeProvisionError
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.incident_backend import ExecutorIncidentBackend
from netopsbench.platform.scenario.validator import require_scenario_topology

logger = get_logger(__name__)

_PROVISION_LOCK = threading.Lock()
_LEASE_WAIT_TIMEOUT_SECONDS = 1_800


@dataclass
class WarmRuntime:
    runtime: RuntimePool
    runner: ScenarioExecutor | None
    in_use: bool = False
    lease_deadline: float = 0.0
    cleaning: bool = False
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
        self._pending = 0
        self._draining = False
        self.lease_acquire_seconds = 0.0
        self.lock_wait_seconds = 0.0
        self.condition_wait_seconds = 0.0

    @contextmanager
    def _locked(self):
        started = time.monotonic()
        self._condition.acquire()
        self.lock_wait_seconds += time.monotonic() - started
        try:
            yield
        finally:
            self._condition.release()

    def _wait(self, timeout=None):
        started = time.monotonic()
        try:
            self._condition.wait(timeout=timeout)
        finally:
            self.condition_wait_seconds += time.monotonic() - started

    def acquire(self, scale: str) -> WarmRuntime:
        started = time.monotonic()
        deadline = started + _LEASE_WAIT_TIMEOUT_SECONDS
        while True:
            self.reap_orphans()
            victim = None
            with self._locked():
                if self._draining:
                    raise RuntimeError("Runtime pool is draining")
                available = next((r for r in self._runtimes.get(scale, []) if not r.in_use and not r.quarantined), None)
                if available is not None:
                    available.in_use = True
                    available.lease_deadline = time.monotonic() + self.config.orphan_lease_ttl_seconds
                    self.lease_acquire_seconds += time.monotonic() - started
                    return available
                if self._runtime_count() + self._pending < self.config.max_active_runtimes:
                    self._pending += 1
                    break
                victim = next(
                    (
                        r
                        for key, records in self._runtimes.items()
                        if key != scale
                        for r in records
                        if not r.in_use and not r.quarantined
                    ),
                    None,
                )
                if victim is not None:
                    victim.quarantined = True
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(f"Timed out waiting for runtime lease for scale {scale}")
                    self._wait(timeout=self._next_wait_timeout(remaining))
            if victim is not None:
                self.quarantine(victim)
        try:
            record = self._provision(scale)
        except BaseException:
            with self._locked():
                self._pending -= 1
                self._condition.notify_all()
            raise
        with self._locked():
            self._pending -= 1
            self._runtimes.setdefault(scale, []).append(record)
            draining = self._draining
            if not draining:
                record.in_use = True
                record.lease_deadline = time.monotonic() + self.config.orphan_lease_ttl_seconds
                self.lease_acquire_seconds += time.monotonic() - started
            self._condition.notify_all()
        if draining:
            # drain owns this newly published record and its teardown.
            raise RuntimeError("Runtime pool was drained during provisioning")
        return record

    def release(self, record: WarmRuntime) -> None:
        with self._locked():
            record.in_use = False
            record.lease_deadline = 0.0
            self._condition.notify_all()

    def refresh(self, record: WarmRuntime, *, min_seconds: float = 0.0) -> None:
        with self._locked():
            if record.in_use and not record.quarantined:
                ttl = max(float(self.config.orphan_lease_ttl_seconds), float(min_seconds) + 300.0)
                record.lease_deadline = time.monotonic() + ttl

    def quarantine(self, record: WarmRuntime) -> None:
        with self._locked():
            while record.cleaning:
                self._wait()
            if record not in self._runtimes.get(record.runtime.scale, []):
                return
            record.quarantined = True
            record.in_use = False
            record.lease_deadline = 0.0
            record.cleaning = True
        try:
            try:
                if record.runner is not None:
                    record.runner._stop_traffic()
            except Exception:
                logger.warning("Failed to stop traffic while quarantining runtime", exc_info=True)
            record.runtime.teardown()
        except BaseException:
            # Failed teardown continues to occupy capacity and is never leased.
            raise
        else:
            with self._locked():
                self._remove(record)
        finally:
            with self._locked():
                record.cleaning = False
                self._condition.notify_all()

    def drain(self, scales: set[str] | None = None) -> None:
        failures = []
        with self._locked():
            while self._draining:
                self._wait()
            self._draining = True
            while self._pending:
                self._wait()
            selected = [
                r for scale, records in self._runtimes.items() if scales is None or scale in scales for r in records
            ]
            for record in selected:
                record.quarantined = True
        try:
            for record in selected:
                try:
                    self.quarantine(record)
                except Exception as exc:
                    failures.append(f"{record.runtime.scale}: {type(exc).__name__}: {exc}")
        finally:
            with self._locked():
                self._draining = False
                self._condition.notify_all()
        if failures:
            raise RuntimeError("Simulator runtime drain failed: " + "; ".join(failures))

    def reap_orphans(self) -> None:
        with self._locked():
            now = time.monotonic()
            stale = [
                r
                for records in self._runtimes.values()
                for r in records
                if r.in_use and r.lease_deadline and now > r.lease_deadline
            ]
            for record in stale:
                record.quarantined = True
        for record in stale:
            try:
                self.quarantine(record)
            except Exception:
                logger.warning("Failed to reap quarantined runtime", exc_info=True)

    def _runtime_count(self) -> int:
        return sum(len(records) for records in self._runtimes.values())

    def _next_wait_timeout(self, remaining: float) -> float:
        now = time.monotonic()
        active_deadlines = [
            record.lease_deadline
            for records in self._runtimes.values()
            for record in records
            if record.in_use and record.lease_deadline > now
        ]
        if not active_deadlines:
            return remaining
        return min(remaining, max(0.01, min(active_deadlines) - now))

    def _remove(self, record: WarmRuntime) -> None:
        records = self._runtimes.get(record.runtime.scale, [])
        if record in records:
            records.remove(record)

    def _retain_failed_provision(self, runtime: RuntimePool) -> None:
        with self._locked():
            self._runtimes.setdefault(runtime.scale, []).append(
                WarmRuntime(runtime=runtime, runner=None, quarantined=True)
            )

    def _provision(self, scale: str) -> WarmRuntime:
        try:
            with _PROVISION_LOCK:
                runtime = self.manager.provision(
                    scale=scale,
                    workers=1,
                    name=f"sim-{scale}-{uuid.uuid4().hex[:8]}",
                )
        except RuntimeProvisionError as exc:
            self._retain_failed_provision(exc.runtime)
            raise
        try:
            worker = runtime.workers[0]
            runner = ScenarioExecutor(
                topology_dir=str(worker.topology_dir),
                topology_id=worker.topology_id,
                influxdb_bucket=worker.bucket,
                fault_registry=self.fault_registry,
                scale_registry=self.registry,
                runtime_worker=worker,
            )
        except BaseException as exc:
            try:
                runtime.teardown()
            except Exception as cleanup_error:
                self._retain_failed_provision(runtime)
                raise RuntimeProvisionError(
                    runtime, f"Executor setup and cleanup failed: {exc}; {cleanup_error}"
                ) from cleanup_error
            raise
        return WarmRuntime(runtime=runtime, runner=runner)


class RuntimeEpisodeBackend:
    """Bridge one interactive environment to a leased real runtime."""

    def __init__(self, leases: RuntimeLeasePool, registry: ScaleRegistry):
        self.leases = leases
        self.registry = registry
        self.record: WarmRuntime | None = None
        self.delegate: ExecutorIncidentBackend | None = None
        self.topology_dir: str | None = None

    def prepare(self, scenario: ScenarioSpec) -> dict[str, Any]:
        self.record = self.leases.acquire(scenario.scale)
        runner = self.record.runner
        if runner is None:
            raise RuntimeError("Cannot use an incomplete runtime")
        worker = self.record.runtime.workers[0]
        self.topology_dir = str(worker.topology_dir)
        try:
            require_scenario_topology(scenario, self.topology_dir)
        except Exception:
            record = self.record
            self.record = None
            self.leases.release(record)
            raise
        # Preparation, traffic, baseline identity and recovery are shared with
        # benchmark execution. An unhealthy lease is handled by incident cleanup.
        runner.begin_case()
        # Every prepare acquires a new lease; no cross-user baseline handoff.
        from netopsbench.models.recovery import RecoveryPolicy

        if RecoveryPolicy.from_metadata(scenario.metadata).mode == "state_checked":
            runner._baseline_handoff.clear("lease_transfer")
            runner._calibration_current = False
        self.delegate = ExecutorIncidentBackend(
            runner,
            influxdb_bucket=worker.bucket,
            topology_id=worker.topology_id,
        )
        return self.delegate.prepare(scenario)

    def call_tool(self, action: ToolAction) -> dict[str, Any]:
        if self.delegate is None:
            raise RuntimeError("Incident backend is not prepared")
        return self.delegate.call_tool(action)

    def refresh(self, *, min_seconds: float = 0.0) -> None:
        if self.record is not None:
            self.leases.refresh(self.record, min_seconds=min_seconds)

    def finish(self, *, broken: bool = False) -> None:
        record = self.record
        if record is None:
            return
        requested_broken = broken
        failure = ""
        delegate = self.delegate
        try:
            if delegate is not None:
                delegate.finish(broken=broken)
        except Exception as exc:
            broken = True
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            self.delegate = None
            self.record = None
        if broken:
            self.leases.quarantine(record)
        else:
            self.leases.release(record)
        if broken and not requested_broken:
            raise RuntimeError(failure or "Runtime cleanup failed")


__all__ = [
    "RuntimeEpisodeBackend",
    "RuntimeLeasePool",
    "WarmRuntime",
]

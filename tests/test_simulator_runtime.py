"""Generic incident engine and warm-runtime contracts."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from netopsbench.models.profiles import default_scale_registry
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.incident.engine import (
    CleanupStatus,
    FailureDomain,
    IncidentEngine,
    IncidentState,
    SessionState,
)
from netopsbench.platform.scenario.observation import (
    baseline_gate_errors,
    observation_integrity_errors,
)
from netopsbench.platform.simulator import runtime as simulator_runtime_module
from netopsbench.platform.simulator.environment import (
    DiagnosisSubmission,
    DiagnosticEnvironment,
    SimulatorConfig,
    SubmitDiagnosisAction,
    ToolAction,
)
from netopsbench.platform.simulator.runtime import RuntimeEpisodeBackend, RuntimeLeasePool, WarmRuntime
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.sdk.simulators import SimulatorManager


def _scenario(*, healthy: bool = False) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id="scenario-1",
        name="scenario-1",
        topology_scale="xs",
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="none" if healthy else "link_down",
            target_device=None if healthy else "leaf1",
            target_interface=None if healthy else "Ethernet1",
        ),
    )


class FakeBackend:
    topology_dir = None

    def __init__(self, *, prepare_error: Exception | None = None, cleanup_error: Exception | None = None):
        self.prepare_error = prepare_error
        self.cleanup_error = cleanup_error
        self.prepares = 0
        self.finishes: list[bool] = []
        self.tool_calls = 0

    def prepare(self, scenario):
        self.prepares += 1
        if self.prepare_error:
            raise self.prepare_error
        return {
            "case_id": f"case-{scenario.digest[:12]}",
            "topology_summary": {"family": "clos"},
            "symptoms": {},
        }

    def call_tool(self, action):
        self.tool_calls += 1
        return {"success": True, "data": {"name": action.name}}

    def refresh(self, *, min_seconds=0.0):
        del min_seconds

    def finish(self, *, broken=False):
        self.finishes.append(broken)
        if self.cleanup_error:
            raise self.cleanup_error


def test_one_incident_hosts_independent_sessions_and_recovers_once():
    backend = FakeBackend()
    incident = IncidentEngine(lambda: backend).prepare(_scenario())
    config = SimulatorConfig(max_tool_calls=2)
    sessions = [incident.open_session(config) for _ in range(4)]

    tool = sessions[0].call_tool(ToolAction(name="get_topology"))
    assert tool.state is SessionState.ACTIVE
    assert sessions[0].tool_calls == 1
    assert sessions[1].tool_calls == 0

    for session in sessions:
        accounting = (
            {
                "tool_calls": [{"tool": "get_topology", "args": {}}],
                "time_taken_seconds": 12.5,
                "metadata": {"input_tokens": 1200, "output_tokens": 80},
            }
            if session is sessions[0]
            else {}
        )
        result = session.submit(
            DiagnosisSubmission(
                verdict="fault_detected",
                fault_type="link_down",
                location={"device": "leaf1", "interface": "Ethernet1"},
            ),
            **accounting,
        )
        assert result.reward == 1.0
        assert result.state is SessionState.TERMINAL
        if session is sessions[0]:
            assert result.metrics["tool_calls"] == 1
            assert session.evaluation_result["details"]["tool_calls_count"] == 1
            assert session.evaluation_result["details"]["time_taken"] == 12.5
            assert session.evaluation_result["details"]["agent_output"]["metadata"] == {
                "input_tokens": 1200,
                "output_tokens": 80,
            }
            assert session.evaluation_result["details"]["agent_output"]["tool_calls"] == [
                {"tool": "get_topology", "args": {}}
            ]

    assert incident.close() is CleanupStatus.SUCCEEDED
    assert incident.close() is CleanupStatus.SUCCEEDED
    assert backend.prepares == 1
    assert backend.finishes == [False]


def test_incident_default_session_config_is_used_when_opening_sessions():
    incident = IncidentEngine(
        FakeBackend,
        default_session_config=SimulatorConfig(max_tool_calls=7),
    ).prepare(_scenario())

    session = incident.open_session()

    assert session.config.max_tool_calls == 7
    incident.close()


def test_protocol_error_is_recoverable_and_does_not_break_incident():
    backend = FakeBackend()
    incident = IncidentEngine(lambda: backend).prepare(_scenario())
    session = incident.open_session(SimulatorConfig())

    result = session.call_tool(ToolAction(name="not_a_tool"))

    assert result.failure is not None
    assert result.failure.domain is FailureDomain.PROTOCOL
    assert result.state is SessionState.ACTIVE
    assert incident.state is IncidentState.ACTIVE
    assert backend.tool_calls == 0


def test_diagnostic_session_applies_tool_result_cap_for_every_facade():
    backend = FakeBackend()
    backend.call_tool = lambda _action: {"success": True, "data": "x" * 20_000}
    incident = IncidentEngine(lambda: backend).prepare(_scenario())
    session = incident.open_session(SimulatorConfig(max_tool_result_bytes=1_024))

    result = session.call_tool(ToolAction(name="get_topology"))

    serialized = json.dumps(result.observation, separators=(",", ":"), sort_keys=True)
    assert len(serialized.encode("utf-8")) <= 1_024
    assert "truncation" in serialized


def test_prepare_failure_is_invalid_and_cleanup_failure_is_separate():
    prepare_backend = FakeBackend(prepare_error=RuntimeError("inject failed"))
    invalid = IncidentEngine(lambda: prepare_backend).prepare(_scenario())
    assert invalid.state is IncidentState.BROKEN
    assert invalid.failure is not None
    assert invalid.failure.domain is FailureDomain.INFRASTRUCTURE
    assert invalid.cleanup_failure is None

    cleanup_backend = FakeBackend(cleanup_error=RuntimeError("recovery failed"))
    incident = IncidentEngine(lambda: cleanup_backend).prepare(_scenario(healthy=True))
    session = incident.open_session(SimulatorConfig())
    result = session.submit(DiagnosisSubmission(verdict="network_healthy"))
    assert result.reward == 1.0
    assert incident.close() is CleanupStatus.FAILED
    assert result.reward == 1.0
    assert incident.failure is None
    assert incident.cleanup_failure is not None
    assert incident.cleanup_failure.domain is FailureDomain.CLEANUP


def test_environment_preserves_outcome_when_cleanup_fails():
    backend = FakeBackend(cleanup_error=RuntimeError("recovery failed"))
    environment = DiagnosticEnvironment(
        IncidentEngine(lambda: backend),
        _scenario(healthy=True),
        SimulatorConfig(),
    )
    assert environment.reset().valid is True

    result = environment.step(SubmitDiagnosisAction(diagnosis=DiagnosisSubmission(verdict="network_healthy")))

    assert result.reward == 1.0
    assert result.cleanup_status is CleanupStatus.FAILED
    assert result.failure is None
    assert result.cleanup_failure is not None
    assert result.cleanup_failure.domain is FailureDomain.CLEANUP


def test_environment_preserves_execution_and_cleanup_failures_independently():
    backend = FakeBackend(cleanup_error=RuntimeError("recovery failed"))
    environment = DiagnosticEnvironment(
        IncidentEngine(lambda: backend),
        _scenario(healthy=True),
        SimulatorConfig(),
    )
    assert environment.reset().valid is True

    result = environment.terminate_protocol("bad envelope")

    assert result.failure is not None
    assert result.failure.domain is FailureDomain.PROTOCOL
    assert result.error == "bad envelope"
    assert result.cleanup_failure is not None
    assert result.cleanup_failure.domain is FailureDomain.CLEANUP


def test_environment_illegal_diagnosis_terminates_as_protocol_outcome_zero():
    environment = DiagnosticEnvironment(
        IncidentEngine(FakeBackend),
        _scenario(healthy=True),
        SimulatorConfig(),
    )
    assert environment.reset().valid is True

    result = environment.terminate_protocol("invalid diagnosis schema")

    assert result.case_valid is True
    assert result.reward == 0.0
    assert result.failure is not None
    assert result.failure.domain is FailureDomain.PROTOCOL
    assert result.termination_reason == "protocol_error"


def test_environment_cannot_reset_into_an_untracked_second_incident():
    environment = DiagnosticEnvironment(
        IncidentEngine(FakeBackend),
        _scenario(healthy=True),
        SimulatorConfig(),
    )
    environment.reset()
    environment.close()

    with pytest.raises(RuntimeError, match="single-use"):
        environment.reset()


def test_session_cannot_use_or_score_a_closed_incident():
    incident = IncidentEngine(FakeBackend).prepare(_scenario(healthy=True))
    session = incident.open_session(SimulatorConfig())
    incident.close()

    with pytest.raises(RuntimeError, match="Incident is not active"):
        session.call_tool(ToolAction(name="get_topology"))
    with pytest.raises(RuntimeError, match="Incident is not active"):
        session.submit(DiagnosisSubmission(verdict="network_healthy"))


class FakeRunner:
    def __init__(self):
        self.stops = 0

    def _stop_traffic(self):
        self.stops += 1


class FakeRuntime:
    def __init__(self, scale: str, *, teardown_failures: int = 0):
        self.scale = scale
        self.teardowns = 0
        self.teardown_failures = teardown_failures

    def teardown(self):
        self.teardowns += 1
        if self.teardowns <= self.teardown_failures:
            raise RuntimeError("teardown failed")


def _pool(monkeypatch) -> tuple[RuntimeLeasePool, list[WarmRuntime]]:
    pool = RuntimeLeasePool(
        SimpleNamespace(),
        default_scale_registry(),
        SimulatorConfig(max_active_runtimes=1),
    )
    provisioned: list[WarmRuntime] = []

    def provision(scale: str) -> WarmRuntime:
        record = WarmRuntime(runtime=FakeRuntime(scale), runner=FakeRunner())
        provisioned.append(record)
        return record

    monkeypatch.setattr(pool, "_provision", provision)
    return pool, provisioned


def test_runtime_lease_is_exclusive_and_reusable(monkeypatch):
    pool, provisioned = _pool(monkeypatch)
    first = pool.acquire("xs")
    acquired: list[WarmRuntime] = []
    waiter = threading.Thread(target=lambda: acquired.append(pool.acquire("xs")))
    waiter.start()
    time.sleep(0.01)
    assert waiter.is_alive()
    pool.release(first)
    waiter.join(timeout=1)
    assert acquired == [first]
    assert len(provisioned) == 1


def test_runtime_capacity_switch_has_no_training_phase_state(monkeypatch):
    pool, _ = _pool(monkeypatch)
    xs = pool.acquire("xs")
    pool.release(xs)
    large = pool.acquire("large")
    pool.release(large)
    returned = pool.acquire("xs")

    assert xs.runtime.teardowns == 1
    assert large.runtime.teardowns == 1
    assert returned.runtime.scale == "xs"


def test_failed_quarantine_remains_at_capacity_until_drain_retry(monkeypatch):
    pool, _ = _pool(monkeypatch)
    record = WarmRuntime(
        runtime=FakeRuntime("xs", teardown_failures=1),
        runner=FakeRunner(),
        in_use=True,
    )
    pool._runtimes["xs"] = [record]

    with pytest.raises(RuntimeError, match="teardown failed"):
        pool.quarantine(record)

    assert record.quarantined is True
    assert record.in_use is False
    assert pool._runtime_count() == 1

    pool.drain()

    assert record.runtime.teardowns == 2
    assert pool._runtime_count() == 0


def test_runtime_lease_wait_is_bounded(monkeypatch):
    pool, _ = _pool(monkeypatch)
    pool.acquire("xs")
    monkeypatch.setattr("netopsbench.platform.simulator.runtime._LEASE_WAIT_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(RuntimeError, match="Timed out waiting for runtime lease"):
        pool.acquire("xs")


def test_active_runtime_deadline_refresh_prevents_orphan_reaping(monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr("netopsbench.platform.simulator.runtime.time.monotonic", lambda: clock[0])
    pool = RuntimeLeasePool(
        SimpleNamespace(),
        default_scale_registry(),
        SimulatorConfig(max_active_runtimes=1, orphan_lease_ttl_seconds=600),
    )
    record = WarmRuntime(runtime=FakeRuntime("xs"), runner=FakeRunner())
    monkeypatch.setattr(pool, "_provision", lambda _scale: record)

    acquired = pool.acquire("xs")
    clock[0] = 1_500.0
    pool.refresh(acquired, min_seconds=1_000)
    clock[0] = 1_700.0
    pool.reap_orphans()

    assert acquired.in_use is True
    assert acquired.runtime.teardowns == 0


def test_runtime_waiter_wakes_at_nearest_active_lease_deadline(monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr("netopsbench.platform.simulator.runtime.time.monotonic", lambda: clock[0])
    pool, _ = _pool(monkeypatch)
    record = pool.acquire("xs")
    record.lease_deadline = 1_025.0

    assert pool._next_wait_timeout(1_800.0) == 25.0


def test_simulator_manager_uses_one_physical_pool_for_different_session_limits():
    manager = SimulatorManager(
        scale_registry=default_scale_registry(),
        runtime_manager=SimpleNamespace(),
    )
    first = SimulatorConfig(max_active_runtimes=1, max_tool_calls=4)
    second = SimulatorConfig(max_active_runtimes=1, max_tool_calls=9)

    manager._engine(first)
    lease_pool = manager._lease_pool
    manager._engine(second)

    assert manager._lease_pool is lease_pool
    with pytest.raises(ValueError, match="capacity is fixed"):
        manager._engine(SimulatorConfig(max_active_runtimes=2))
    manager.close()


def test_simulator_manager_close_retains_failed_pool_for_retry():
    manager = SimulatorManager(
        scale_registry=default_scale_registry(),
        runtime_manager=SimpleNamespace(),
    )

    class Pool:
        attempts = 0

        def drain(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("teardown failed")

    pool = Pool()
    manager._lease_pool = pool
    manager._pool_config = (1, 600)

    with pytest.raises(RuntimeError, match="teardown failed"):
        manager.close()
    assert manager._lease_pool is pool

    manager.close()
    assert pool.attempts == 2
    assert manager._lease_pool is None


def test_manager_registries_drop_closed_environments_and_incidents(monkeypatch):
    manager = SimulatorManager(
        scale_registry=default_scale_registry(),
        runtime_manager=SimpleNamespace(),
    )

    def engine(_config):
        return IncidentEngine(FakeBackend, on_close=manager._discard_incident)

    monkeypatch.setattr(manager, "_engine", engine)
    environment = manager.create(scenario=_scenario(healthy=True))
    environment.reset()
    assert len(manager._environments) == 1
    environment.close()
    assert len(manager._environments) == 0

    incident = manager.prepare(scenario=_scenario(healthy=True))
    assert len(manager._incidents) == 1
    incident.close()
    assert len(manager._incidents) == 0


def _baseline_observation(
    *,
    paths=10_000,
    loss=0,
    unreachable=0,
    mtu=0,
    latency=0,
    local_df=0,
    local_probe_errors=0,
):
    return {
        "start_time": "2026-01-01T00:01:00Z",
        "end_time": "2026-01-01T00:02:00Z",
        "duration_seconds": 60,
        "data_source_status": "ok",
        "coverage_status": "complete",
        "_baseline_coverage": {"status": "ok", "coverage_status": "complete"},
        "pingmesh_metrics": {
            "summary": {
                "packet_loss_events": loss,
                "path_unreachable_events": unreachable,
                "mtu_or_fragmentation_events": mtu,
                "latency_spikes": latency,
            },
            "quality": {
                "current_paths_observed": paths,
                "absolute_packet_loss_paths": loss,
                "absolute_unreachable_paths": unreachable,
                "absolute_network_mtu_paths": mtu,
                "local_df_mtu_drops": local_df,
                "local_probe_errors": local_probe_errors,
            },
        },
    }


def test_simulator_baseline_gate_allows_only_bounded_loss_noise():
    assert baseline_gate_errors(_baseline_observation(paths=10_000, loss=10)) == []

    errors = baseline_gate_errors(
        _baseline_observation(paths=1_000, loss=2, unreachable=1, mtu=1, latency=1, local_df=1)
    )

    assert any("absolute_unreachable_paths=1" in error for error in errors)
    assert any("packet_loss_path_rate=0.002000" in error for error in errors)
    assert any("absolute_network_mtu_paths=1" in error for error in errors)
    assert any("latency_spikes=1" in error for error in errors)
    assert any("local_df_mtu_drops=1" in error for error in errors)


def test_baseline_gate_rejects_incomplete_reference_coverage():
    observation = _baseline_observation()
    observation["_baseline_coverage"] = {"status": "ok", "coverage_status": "incomplete"}

    assert baseline_gate_errors(observation) == ["baseline_coverage=incomplete"]
    assert observation_integrity_errors(observation) == ["baseline_coverage=incomplete"]


def test_observation_integrity_rejects_incomplete_or_local_probe_failures():
    observation = _baseline_observation(paths=0, local_df=2, local_probe_errors=3)
    observation["data_source_status"] = "error: timeout"
    observation["coverage_status"] = "incomplete"

    assert observation_integrity_errors(observation) == [
        "data=error: timeout",
        "coverage=incomplete",
        "current_paths_observed=0",
        "local_df_mtu_drops=2",
        "local_probe_errors=3",
    ]


def test_observation_integrity_does_not_reject_real_network_anomalies():
    observation = _baseline_observation(
        paths=10_000,
        loss=500,
        unreachable=200,
        mtu=100,
        latency=50,
    )

    assert observation_integrity_errors(observation) == []


def test_simulator_builds_reference_and_validation_once_then_reuses_latest_window(tmp_path):
    topology_dir = tmp_path / "xs-topology"
    generate_topology("xs", str(topology_dir), name="sim-baseline")
    reference = {
        "name": "baseline",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:01:00Z",
        "duration_seconds": 60,
    }
    calls = []

    class BaselineRunner:
        def __init__(self):
            self.topology_dir = topology_dir
            self.traffic_controller = object()

        def _capture_baseline_window(self):
            calls.append("reference")
            return reference

        def _wait_and_observe(self, duration, *, baseline_window):
            calls.append(("validation", duration, baseline_window))
            return _baseline_observation()

    runner = BaselineRunner()
    record = WarmRuntime(runtime=FakeRuntime("xs"), runner=runner)
    backend = RuntimeEpisodeBackend(SimpleNamespace(), default_scale_registry())
    backend.record = record

    backend._ensure_baseline(_scenario(healthy=True))
    backend._ensure_baseline(_scenario(healthy=True))

    assert calls == ["reference", ("validation", 60, reference)]
    assert record.baseline == {
        "name": "baseline",
        "start_time": "2026-01-01T00:01:00Z",
        "end_time": "2026-01-01T00:02:00Z",
        "duration_seconds": 60,
    }

    runner.traffic_controller = object()
    backend._ensure_baseline(_scenario(healthy=True))
    assert calls.count("reference") == 2


def test_simulator_rebuilds_missing_traffic_once_and_invalidates_baseline(monkeypatch):
    events = []

    class StaleTraffic:
        active_flows = {"flow": object()}

        @staticmethod
        def verify_active_flows():
            events.append("verify")
            return False

    class FreshTraffic:
        active_flows = {"fresh": object()}

    class Runner:
        topology_dir = "unused"
        post_recovery_wait_seconds = 0

        def __init__(self):
            self.traffic_controller = StaleTraffic()

        @staticmethod
        def _recover_fault():
            return []

        def _stop_traffic(self):
            events.append("stop")
            self.traffic_controller = None

        def _setup_traffic(self, scale, profile):
            events.append(("setup", scale, profile))
            self.traffic_controller = FreshTraffic()

    worker = SimpleNamespace(topology_dir="unused", bucket="bucket", topology_id="topology")
    record = WarmRuntime(
        runtime=SimpleNamespace(workers=[worker]),
        runner=Runner(),
        baseline_signature="old-signature",
        baseline={"name": "old"},
    )

    class Leases:
        @staticmethod
        def acquire(_scale):
            return record

        @staticmethod
        def release(_record):
            events.append("release")

        @staticmethod
        def quarantine(_record):
            events.append("quarantine")

    class Delegate:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def prepare(_scenario):
            return {"case_id": "case-test"}

        @staticmethod
        def finish(*, broken=False):
            events.append(("finish", broken))

    backend = RuntimeEpisodeBackend(Leases(), default_scale_registry())
    monkeypatch.setattr(backend, "_health_errors", lambda *args, **kwargs: [])

    def ensure_baseline(_scenario):
        assert record.baseline_signature is None
        assert record.baseline is None
        events.append("baseline")

    monkeypatch.setattr(backend, "_ensure_baseline", ensure_baseline)
    monkeypatch.setattr(simulator_runtime_module, "ExecutorIncidentBackend", Delegate)

    result = backend.prepare(_scenario(healthy=True))

    assert result == {"case_id": "case-test"}
    assert events[:4] == ["verify", "stop", ("setup", "xs", "standard"), "baseline"]


def test_simulator_rebuilds_baseline_after_recovering_lingering_fault(monkeypatch):
    class Traffic:
        active_flows = {"flow": object()}

        @staticmethod
        def verify_active_flows():
            return True

    runner = SimpleNamespace(
        traffic_controller=Traffic(),
        topology_dir="unused",
        post_recovery_wait_seconds=0,
        sleep=lambda _seconds: None,
        _recover_fault=lambda: [{"type": "link_down", "recovered": True}],
    )
    worker = SimpleNamespace(topology_dir="unused", bucket="bucket", topology_id="topology")
    record = WarmRuntime(
        runtime=SimpleNamespace(workers=[worker]),
        runner=runner,
        baseline_signature="stale",
        baseline={"name": "stale"},
    )

    class Leases:
        @staticmethod
        def acquire(_scale):
            return record

    class Delegate:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def prepare(_scenario):
            return {"case_id": "case-test"}

    backend = RuntimeEpisodeBackend(Leases(), default_scale_registry())
    monkeypatch.setattr(backend, "_health_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        backend,
        "_ensure_baseline",
        lambda _scenario: (record.baseline_signature is None and record.baseline is None)
        or pytest.fail("lingering-fault recovery must invalidate the cached baseline"),
    )
    monkeypatch.setattr(simulator_runtime_module, "ExecutorIncidentBackend", Delegate)

    assert backend.prepare(_scenario(healthy=True)) == {"case_id": "case-test"}


def test_simulator_quarantines_when_traffic_rebuild_is_incomplete(monkeypatch):
    events = []

    class StaleTraffic:
        active_flows = {"flow": object()}

        @staticmethod
        def verify_active_flows():
            return False

    runner = SimpleNamespace(
        traffic_controller=StaleTraffic(),
        topology_dir="unused",
        post_recovery_wait_seconds=0,
        _recover_fault=lambda: [],
        _stop_traffic=lambda: events.append("stop"),
        _setup_traffic=lambda _scale, _profile: (_ for _ in ()).throw(
            RuntimeError("Background traffic matrix incomplete")
        ),
    )
    worker = SimpleNamespace(topology_dir="unused", bucket="bucket", topology_id="topology")
    record = WarmRuntime(runtime=SimpleNamespace(workers=[worker]), runner=runner)

    class Leases:
        @staticmethod
        def acquire(_scale):
            return record

        @staticmethod
        def quarantine(_record):
            events.append("quarantine")

    from netopsbench.platform.incident.engine import IncidentEngine, IncidentState

    backend = RuntimeEpisodeBackend(Leases(), default_scale_registry())
    monkeypatch.setattr(backend, "_health_errors", lambda *args, **kwargs: [])

    incident = IncidentEngine(lambda: backend).prepare(_scenario(healthy=True))

    assert events == ["stop", "quarantine"]
    assert incident.state is IncidentState.BROKEN
    assert incident.failure is not None
    assert incident.failure.message == "Background traffic matrix incomplete"

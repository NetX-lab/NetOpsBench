"""Generic incident engine and warm-runtime contracts."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from netopsbench.models.profiles import default_scale_registry
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.simulator.engine import (
    CleanupStatus,
    FailureDomain,
    IncidentEngine,
    IncidentState,
    SessionState,
)
from netopsbench.platform.simulator.environment import (
    DiagnosisSubmission,
    DiagnosticEnvironment,
    SimulatorConfig,
    SubmitDiagnosisAction,
    ToolAction,
)
from netopsbench.platform.simulator.runtime import RuntimeLeasePool, WarmRuntime


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

    def finish(self, *, broken=False):
        self.finishes.append(broken)
        if self.cleanup_error:
            raise self.cleanup_error

    def close(self):
        self.finish()


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
        result = session.submit(
            DiagnosisSubmission(
                verdict="fault_detected",
                fault_type="link_down",
                location={"device": "leaf1", "interface": "Ethernet1"},
            )
        )
        assert result.reward == 1.0
        assert result.state is SessionState.TERMINAL

    assert incident.close() is CleanupStatus.SUCCEEDED
    assert incident.close() is CleanupStatus.SUCCEEDED
    assert backend.prepares == 1
    assert backend.finishes == [False]


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


def test_prepare_failure_is_invalid_and_cleanup_failure_is_separate():
    prepare_backend = FakeBackend(prepare_error=RuntimeError("inject failed"))
    invalid = IncidentEngine(lambda: prepare_backend).prepare(_scenario())
    assert invalid.state is IncidentState.BROKEN
    assert invalid.failure is not None
    assert invalid.failure.domain is FailureDomain.INFRASTRUCTURE

    cleanup_backend = FakeBackend(cleanup_error=RuntimeError("recovery failed"))
    incident = IncidentEngine(lambda: cleanup_backend).prepare(_scenario(healthy=True))
    session = incident.open_session(SimulatorConfig())
    result = session.submit(DiagnosisSubmission(verdict="network_healthy"))
    assert result.reward == 1.0
    assert incident.close() is CleanupStatus.FAILED
    assert result.reward == 1.0
    assert incident.failure is not None
    assert incident.failure.domain is FailureDomain.CLEANUP


def test_environment_preserves_outcome_when_cleanup_fails():
    backend = FakeBackend(cleanup_error=RuntimeError("recovery failed"))
    environment = DiagnosticEnvironment(
        IncidentEngine(lambda: backend),
        _scenario(healthy=True),
        SimulatorConfig(),
    )
    assert environment.reset().valid is True

    result = environment.step(
        SubmitDiagnosisAction(diagnosis=DiagnosisSubmission(verdict="network_healthy"))
    )

    assert result.reward == 1.0
    assert result.cleanup_status is CleanupStatus.FAILED
    assert result.failure is not None
    assert result.failure.domain is FailureDomain.CLEANUP


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


class FakeRunner:
    def __init__(self):
        self.stops = 0

    def _stop_traffic(self):
        self.stops += 1


class FakeRuntime:
    def __init__(self, scale: str):
        self.scale = scale
        self.teardowns = 0

    def teardown(self):
        self.teardowns += 1


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


def test_runtime_lease_wait_is_bounded(monkeypatch):
    pool, _ = _pool(monkeypatch)
    pool.acquire("xs")
    monkeypatch.setattr("netopsbench.platform.simulator.runtime._LEASE_WAIT_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(RuntimeError, match="Timed out waiting for runtime lease"):
        pool.acquire("xs")

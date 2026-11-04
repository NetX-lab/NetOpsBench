"""Behavioral regression for frozen policies, short windows and shared budgets."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from netopsbench.models.profiles import ScaleProfileDocument, get_scale_profile
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.observability.routes import verify_route_recovery
from netopsbench.platform.scenario.effects import scoped_integrity_errors
from netopsbench.platform.scenario.episode_runner import observe_episode
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.timing import PhaseTimings, agent_cleanup_timing
from netopsbench.platform.session.diagnosis import run_agent_diagnose


def test_experiment_defaults_are_resolved_once_and_fingerprinted():
    original = get_scale_profile("medium")
    defaults = original.experiment.model_dump()
    raw = original.model_dump(exclude={"experiment"})
    document = ScaleProfileDocument.model_validate(
        {
            "experiment_defaults": {**defaults, "observation_seconds": 40},
            "profiles": [raw, {**raw, "name": "custom", "experiment": {"diagnosis_timeout_seconds": 90}}],
        }
    )
    first, second = document.profiles
    assert first.experiment.observation_seconds == second.experiment.observation_seconds == 40
    assert second.experiment.diagnosis_timeout_seconds == 90
    assert first.digest != original.digest
    with pytest.raises(ValueError):
        first.experiment.observation_seconds = 50


def test_observation_is_one_capture_with_analysis_slices():
    captures = []
    analyzed = []
    runner = SimpleNamespace(
        timings=PhaseTimings(),
        observation_timeout_seconds=60,
        _inject_fault=lambda _: {"success": True},
    )
    runner._capture_observation_window = lambda duration, name: (
        captures.append((duration, name))
        or {"start_time": "2026-10-24T00:00:00Z", "end_time": "2026-10-24T00:00:30Z", "duration_seconds": duration}
    )
    runner._merge_observation_windows = lambda windows, **kw: (
        analyzed.extend(windows) or {"duration_seconds": kw["total_duration_seconds"]}
    )
    observe_episode(
        runner,
        EpisodeSpec(episode_id="a", fault_type="link_flapping", duration_seconds=30, stabilization_time=0),
        baseline_window={},
    )
    assert captures == [(30, "full")]
    assert [(w["name"], w["duration_seconds"]) for w in analyzed] == [("early", 10), ("steady", 20)]
    assert analyzed[0]["end_time"] == analyzed[1]["start_time"]


@pytest.mark.parametrize(
    "broken_batch,missing_peer,valid", [(False, False, True), (True, False, False), (False, True, False)]
)
def test_short_outage_requires_actual_correctly_scheduled_failure(broken_batch, missing_peer, valid):
    obs = {"data_source_status": "ok", "_baseline_coverage": {"coverage_status": "complete"}}
    task = ["client1", "client3" if missing_peer else "client2", 0]
    audit = {
        "missing_tasks": [task],
        "invalid_tasks": [["client1", "client2", 0]],
        "invalid_socket_rows": 1,
        "invalid_rows": [
            {
                "src": "client1",
                "dst": "client2",
                "port_batch_index": 0,
                "time": "2026-10-24T00:00:05Z",
                "local_probe_errors": 1,
                "socket_batch_valid": not broken_batch,
            }
        ],
        "observable_pairs": [["client2", "client3"]],
        "coverage_status": "incomplete",
    }
    effect = {
        "probe_cycle_seconds": 1,
        "bounded_client_outages": [
            {"client": "client1", "start": "2026-10-24T00:00:00+00:00", "end": "2026-10-24T00:00:10+00:00"}
        ],
    }
    assert (not scoped_integrity_errors(obs, audit, effect)) is valid
    assert audit["coverage_status"] == "incomplete"


def test_down_destination_does_not_excuse_incorrect_source_port_pool():
    obs = {"data_source_status": "ok", "_baseline_coverage": {"coverage_status": "complete"}}
    audit = {
        "missing_tasks": [],
        "invalid_tasks": [["client1", "client7", 0]],
        "invalid_socket_rows": 1,
        "invalid_rows": [{"src": "client1", "dst": "client7", "socket_batch_valid": False}],
        "observable_pairs": [["client1", "client2"]],
    }
    assert scoped_integrity_errors(obs, audit, {"unobservable_clients": ["client7"]})


def test_restored_bgp_route_does_not_hide_residual_more_specific_static():
    before = {
        "nodes": {
            "leaf1": {"fib": [{"dst": "192.168.2.0/30", "protocol": "bgp", "gateway": "10.0.0.1", "dev": "Ethernet0"}]}
        }
    }
    after = deepcopy(before)
    after["nodes"]["leaf1"]["fib"].append(
        {"dst": "192.168.2.2", "protocol": "static", "gateway": "10.0.0.9", "dev": "Ethernet4"}
    )
    assert "residual" in verify_route_recovery(before, after)[0]
    assert not verify_route_recovery(after, deepcopy(after))


def test_late_recovery_cannot_be_published_as_clean(monkeypatch):
    import netopsbench.platform.scenario.executor as module
    from netopsbench.platform.utils import control

    clock = [0.0]
    runner = object.__new__(ScenarioExecutor)
    from netopsbench.models.recovery import RecoveryPolicy

    runner.recovery_policy = RecoveryPolicy()
    runner.begin_case()
    runner.traffic_controller = None
    runner.runtime_worker = None
    runner.injector = SimpleNamespace(active_faults=[{"type": "link_down"}])
    runner.scale_registry = SimpleNamespace(
        get=lambda _: SimpleNamespace(experiment=SimpleNamespace(recovery_timeout_seconds=1))
    )
    runner.sleep = lambda _: None

    def recover():
        runner.injector.active_faults.clear()
        clock[0] += 5
        return [{"recovered": True}]

    runner._recover_fault = recover
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    result = runner._cleanup_after_scenario(
        ScenarioSpec(
            metadata={"recovery_policy": {"version": 2}},
            scenario_id="late",
            name="late",
            episode=EpisodeSpec(episode_id="a", fault_type="link_down"),
        ),
        None,
    )
    assert result["success"] is False
    assert result["status"] == "recovery_timeout"


def test_recovery_steps_accumulate_retries_without_resetting_budget(monkeypatch):
    import netopsbench.platform.scenario.timing as module
    from netopsbench.platform.utils import control
    from netopsbench.platform.utils.control import current_operation_control

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    timings = PhaseTimings()
    with timings.measure("recovery", timeout_seconds=120):
        deadline = current_operation_control().deadline
        with pytest.raises(RuntimeError), timings.measure_step("recovery", "health_check"):
            clock[0] += 3
            raise RuntimeError("not yet healthy")
        with timings.measure_step("recovery", "health_check"):
            assert current_operation_control().deadline == deadline
            clock[0] += 5
        with timings.measure_step("recovery", "window_wait"):
            clock[0] += 42
        with timings.measure_step("recovery", "window_query") as query:
            query["ingestion"] = {"baseline": {"query_seconds": 9}, "current": {"query_seconds": 8}}
            clock[0] += 10
    recovery = timings.phases["recovery"]
    assert recovery["actual_seconds"] == 60
    assert recovery["steps"]["health_check"] == {
        "status": "completed",
        "actual_seconds": 8,
        "calls": 2,
        "failed_calls": 1,
        "operations": {},
    }
    assert recovery["steps"]["window_query"]["actual_seconds"] == 10
    assert sum(step["actual_seconds"] for step in recovery["steps"].values()) == recovery["actual_seconds"]


def test_recovery_window_and_ingestion_timings_are_separate(monkeypatch):
    from netopsbench.platform.scenario import observation, timing

    clock = [0.0]
    monkeypatch.setattr(timing, "monotonic", lambda: clock[0])
    timings = PhaseTimings()
    reference = {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:00:42Z"}

    def capture(*args, **kwargs):
        clock[0] += 42
        return reference

    def analyze(*args, **kwargs):
        clock[0] += 10
        return {
            "_baseline_health": {
                "absolute_unreachable_paths": 0,
                "absolute_packet_loss_paths": 0,
                "absolute_network_mtu_paths": 0,
            },
            "_baseline_coverage": {"ingestion": {"query_requests": 21}},
            "_coverage_audit": {"ingestion": {"query_requests": 21}},
        }

    from netopsbench.models.profiles import default_scale_registry
    from netopsbench.platform.scenario.baseline import BaselineHandoff
    from netopsbench.platform.scenario.executor import ScenarioExecutor

    runner = object.__new__(ScenarioExecutor)
    runner.timings = timings
    runner.injector = SimpleNamespace(active_faults=[])
    runner._recovering = True
    runner.runtime_worker = None
    runner.manifest = SimpleNamespace(scale="xs")
    runner.scale_registry = default_scale_registry()
    runner.experiment = runner.scale_registry.get("xs").experiment
    runner.minimum_baseline_seconds = 42
    runner._baseline_handoff = BaselineHandoff()
    runner._baseline_scope = lambda _: None
    runner._capture_baseline_window = capture
    runner._merge_observation_windows = analyze
    reference["duration_seconds"] = 42
    monkeypatch.setattr(observation, "baseline_gate_errors", lambda _: [])
    runner.calibrate(SimpleNamespace(id="case"), "test")
    steps = timings.phases["calibration"]["steps"]
    assert steps["window_wait"]["actual_seconds"] == 42
    assert steps["window_query"]["actual_seconds"] == 10
    assert steps["window_query"]["ingestion"] == {"baseline": {"query_requests": 21}, "current": {"query_requests": 21}}


def test_total_diagnosis_timeout_cancels_and_measures_cleanup():
    events = []

    class Agent:
        async def diagnose(self, context):
            try:
                events.append("started")
                await asyncio.sleep(10)
            finally:
                with agent_cleanup_timing():
                    await asyncio.sleep(0.01)
                    events.append("cleaned")

    timings = PhaseTimings()
    with pytest.raises(TimeoutError):
        run_agent_diagnose(Agent(), None, timeout_seconds=0.02, timings=timings)
    assert events == ["started", "cleaned"]
    assert timings.phases["agent_cleanup"]["actual_seconds"] >= 0.01


def test_completed_diagnosis_cleanup_does_not_consume_the_work_budget():
    from netopsbench.platform.toolkit._core.control import diagnosis_budget_scope

    expected = object()

    class Agent:
        async def diagnose(self, context):
            # A bundled adapter and the SDK share the same earlier deadline.
            async with diagnosis_budget_scope(10):
                try:
                    await asyncio.sleep(0)
                    return expected
                finally:
                    with agent_cleanup_timing():
                        await asyncio.sleep(0.05)

    timings = PhaseTimings()
    assert run_agent_diagnose(Agent(), None, timeout_seconds=0.02, timings=timings) is expected
    assert timings.phases["agent_cleanup"]["actual_seconds"] >= 0.05


def test_session_budget_excludes_recorded_cleanup_but_preserves_wall_time_metrics():
    from netopsbench.platform.incident.engine import DiagnosticSession

    session = object.__new__(DiagnosticSession)
    session.config = SimpleNamespace(max_agent_seconds=0.02, max_tool_calls=10)
    session.tool_calls = 0
    session.phase_timings = PhaseTimings()
    session.phase_timings.phases["agent_cleanup"] = {"status": "completed", "actual_seconds": 0.05}
    assert session._limit_transition(0.06) is None
    assert session._metrics(0.06)["elapsed_seconds"] == 0.06
    assert session._limit_transition(0.08).error == "max_agent_seconds exceeded"


def test_user_cancellation_still_interrupts_post_diagnosis_cleanup():
    from netopsbench.platform.toolkit._core.control import diagnosis_budget_scope
    from netopsbench.platform.utils.control import finish_cleanup

    async def run():
        entered = asyncio.Event()
        cleaned = []

        async def agent():
            async with diagnosis_budget_scope(10):
                with agent_cleanup_timing():
                    entered.set()

                    async def cleanup():
                        await asyncio.sleep(0.01)
                        cleaned.append(True)

                    _, cancelled = await finish_cleanup(cleanup())
                    if cancelled:
                        raise asyncio.CancelledError

        task = asyncio.create_task(agent())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]

    asyncio.run(run())


def test_recovery_stops_an_inflight_periodic_command():
    from threading import Event

    from netopsbench.platform.faults.services.periodic import PeriodicController
    from netopsbench.platform.utils.control import current_operation_control

    entered = Event()

    def healthy():
        control = current_operation_control()
        assert control is not None
        entered.set()
        control.sleep(30)

    controller = PeriodicController(lambda: None, healthy, down_time=0.001, up_time=0.001)
    controller.start()
    try:
        assert entered.wait(1)
    finally:
        controller.stop_event.set()
        controller.thread.join(timeout=1)
    assert not controller.thread.is_alive()
    assert controller.error is None
    assert [row["phase"] for row in controller.history] == ["fault"]


@pytest.mark.parametrize("unhealthy", [False, True])
def test_fresh_baseline_is_one_capture_and_checks_absolute_health(tmp_path, monkeypatch, unhealthy):
    from netopsbench.platform.topology.generator import generate_topology

    metadata = generate_topology("xs", str(tmp_path), name="single-baseline")["metadata"]
    sleeps = []
    clock = [0.0]

    def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    from datetime import UTC, datetime, timedelta

    epoch = datetime.now(UTC)

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return epoch + timedelta(seconds=clock[0])

    monkeypatch.setattr("netopsbench.platform.scenario.baseline.datetime", ClockDatetime)
    monkeypatch.setattr("netopsbench.platform.scenario.observation.datetime", ClockDatetime)
    monkeypatch.setattr("netopsbench.platform.scenario.observation.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("netopsbench.platform.scenario.timing.monotonic", lambda: clock[0])
    runner = ScenarioExecutor(topology_dir=str(tmp_path), topology_metadata=metadata, sleep_fn=advance)
    monkeypatch.setattr(runner, "_setup_traffic", lambda *_: {})
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    reads = []

    def analyze(windows, total_duration_seconds, **kwargs):
        reads.append(windows)
        return {
            "start_time": "2026-10-24T00:00:00Z",
            "end_time": "2026-10-24T00:00:30Z",
            "duration_seconds": total_duration_seconds,
            "data_source_status": "ok",
            "coverage_status": "complete",
            "_baseline_coverage": {"coverage_status": "complete"},
            "_baseline_health": {
                "absolute_unreachable_paths": int(unhealthy),
                "absolute_packet_loss_paths": 0,
                "absolute_network_mtu_paths": 0,
            },
            "pingmesh_metrics": {"summary": {"total_anomalies": 0}, "quality": {"current_paths_observed": 2}},
        }

    from netopsbench.platform.scenario.baseline import RuntimeScope

    monkeypatch.setattr(runner, "_baseline_scope", lambda _: RuntimeScope({}, {}, {}))
    monkeypatch.setattr("netopsbench.platform.observability.routes.collect_routes", lambda *a, **k: {"nodes": {}})
    monkeypatch.setattr(runner, "_merge_observation_windows", analyze)
    case = ScenarioSpec(
        metadata={"recovery_policy": {"version": 2}},
        scenario_id="healthy",
        name="healthy",
        topology_scale="xs",
        episode=EpisodeSpec(episode_id="a", fault_type="none", duration_seconds=30, stabilization_time=0),
    )
    result = runner.run_scenario(case)
    assert result["success"] is not unhealthy, result.get("error", result.get("episode"))
    assert len(sleeps) == (30 if unhealthy else 60)
    assert sum(w[0]["name"] == "baseline" for w in reads) == 1
    if unhealthy:
        assert "absolute_unreachable_paths=1" in result["error"]
    else:
        assert result["phase_timings"]["calibration"]["steps"]["window_wait"]["actual_seconds"] == 30
        assert result["phase_timings"]["diagnosis"]["status"] == "skipped"


def test_periodic_effect_cannot_borrow_transitions_outside_window():
    from netopsbench.platform.scenario.effects import _observed_periodic_phases

    history = [
        {"phase": phase, "time": f"2026-10-24T00:00:{i * 10:02d}Z"}
        for i, phase in enumerate(("fault", "healthy", "fault", "healthy", "fault"))
    ]
    assert [p["phase"] for p in _observed_periodic_phases(history, "2026-10-24T00:00:25Z", "2026-10-24T00:00:29Z")] == [
        "fault"
    ]
    assert [p["phase"] for p in _observed_periodic_phases(history, "2026-10-24T00:00:00Z", "2026-10-24T00:00:30Z")] == [
        "fault",
        "healthy",
        "fault",
        "healthy",
    ]


def test_fresh_health_snapshot_is_read_once_without_losing_absolute_health(monkeypatch):
    from netopsbench.platform.pingmesh._detector_query import SnapshotQueryResult
    from netopsbench.platform.pingmesh.detector import AnomalyDetector

    detector = object.__new__(AnomalyDetector)
    detector.plan_id = "fixed"
    calls = []

    def query(start, end, **kwargs):
        calls.append((start, end))
        return SnapshotQueryResult(status="ok", rows=[]), {"coverage_status": "complete"}

    monkeypatch.setattr(detector, "_query_covered_snapshot", query)
    monkeypatch.setattr(
        detector,
        "analyze_snapshot_rows",
        lambda *_: SimpleNamespace(
            anomalies=[],
            quality={
                "current_paths_observed": 1,
                "absolute_unreachable_paths": 1,
                "absolute_packet_loss_paths": 0,
                "absolute_network_mtu_paths": 0,
            },
        ),
    )
    report = detector.generate_windowed_anomaly_report(
        baseline_start="start",
        baseline_end="end",
        current_start="start",
        current_end="end",
        windows=[],
        include_internal_health=True,
    )
    assert calls == [("start", "end")]
    assert report["_baseline_health"]["absolute_unreachable_paths"] == 1


def test_healthy_and_fault_cases_share_the_same_public_window_shape():
    shapes = []
    for fault in ("none", "link_down"):
        runner = SimpleNamespace(
            timings=PhaseTimings(),
            observation_timeout_seconds=60,
            _inject_fault=lambda _: {"success": True},
        )
        runner._capture_observation_window = lambda duration, name: {
            "start_time": "2026-10-24T00:00:00Z",
            "end_time": "2026-10-24T00:00:30Z",
            "duration_seconds": duration,
        }
        runner._merge_observation_windows = lambda windows, **kwargs: {"observation_windows": windows}
        result = observe_episode(
            runner,
            EpisodeSpec(episode_id="diagnosis", fault_type=fault, duration_seconds=30, stabilization_time=0),
            baseline_window={},
        )
        shapes.append(result["observations"])
    assert shapes[0] == shapes[1]

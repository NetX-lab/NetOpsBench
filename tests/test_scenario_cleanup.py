from types import SimpleNamespace

import pytest

import netopsbench.platform.scenario.executor as executor_module
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.scenario.executor import ScenarioExecutor


@pytest.fixture(autouse=True)
def shared_recovery_clock(monkeypatch):
    from netopsbench.platform.utils import control

    monkeypatch.setattr(control.time, "monotonic", lambda: executor_module.monotonic())


def _scenario() -> ScenarioSpec:
    return ScenarioSpec(
        metadata={"recovery_policy": {"version": 3}},
        scenario_id="cleanup-case",
        name="cleanup-case",
        description="cleanup boundary test",
        topology_scale="xs",
        episode=EpisodeSpec(
            episode_id="cleanup-episode",
            fault_type="link_down",
            target_device="leaf1",
            target_interface="Ethernet1",
        ),
    )


def _runner(*, active_faults, timeout_seconds=10):
    runner = object.__new__(ScenarioExecutor)
    from netopsbench.models.recovery import RecoveryPolicy

    runner.recovery_policy = RecoveryPolicy()
    runner.begin_case()
    runner.traffic_controller = None
    runner.runtime_worker = None
    runner._baseline_handoff = executor_module.BaselineHandoff()
    runner._recovering = True
    runner.injector = SimpleNamespace(active_faults=list(active_faults))
    runner.scale_registry = SimpleNamespace(
        get=lambda _scale: SimpleNamespace(
            health_timeout_seconds=timeout_seconds, experiment=SimpleNamespace(recovery_timeout_seconds=timeout_seconds)
        )
    )
    runner._cancel_event = None
    runner._baseline_scope = lambda _: None
    runner.minimum_baseline_seconds = 4
    runner._capture_baseline_window = lambda: executor_module._capture_observation_window_impl(
        runner, runner.minimum_baseline_seconds, name="baseline"
    )
    runner._merge_observation_windows = lambda windows, *a, **k: {
        "data_source_status": "ok",
        "coverage_status": "complete",
        "_baseline_coverage": {"coverage_status": "complete"},
        "_baseline_health": {
            "absolute_unreachable_paths": 0,
            "absolute_packet_loss_paths": 0,
            "absolute_network_mtu_paths": 0,
        },
        "pingmesh_metrics": {"quality": {"current_paths_observed": 1}},
    }
    return runner


def test_clean_scenario_boundary_does_not_retry_or_sleep(monkeypatch):
    runner = _runner(active_faults=[])
    calls = []
    runner._recover_fault = lambda: calls.append("recover")
    runner.sleep = lambda seconds: calls.append(f"sleep:{seconds}")
    monkeypatch.setattr(executor_module, "monotonic", lambda: 0.0)

    cleanup = runner._cleanup_after_scenario(
        _scenario(),
        {"recovery": [{"type": "link_down", "recovered": True}]},
    )

    assert cleanup == {
        "success": True,
        "status": "clean",
        "attempts": 1,
        "duration_seconds": 0.0,
        "errors": [],
        "validation_level": "full_window",
        "full_validation_reason": "custom_callback",
        "state_validation": {},
        "recovery_policy": runner.recovery_policy.model_dump(mode="json"),
        "recovery": [{"type": "link_down", "recovered": True}],
    }
    assert calls == []


def test_explicit_sdk_baseline_can_recover_without_a_handoff_identity(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}])
    runner.runtime_worker = SimpleNamespace()
    runner.sleep = lambda _: None
    runner._baseline_scope = lambda _: None
    monkeypatch.setattr(executor_module, "monotonic", lambda: 0.0)
    monkeypatch.setattr(executor_module, "check_worker_health", lambda *a, **k: [])

    def recover():
        runner.injector.active_faults.clear()
        return [{"type": "link_down", "recovered": True, "readback_verified": True}]

    runner._recover_fault = recover
    result = runner._cleanup_after_scenario(_scenario(), None, baseline_window={"duration_seconds": 30})
    assert result["success"] is True
    assert result["validation_level"] == "state_checked"
    assert "validated_baseline" not in result
    assert "validated_baseline_scope" not in result


def test_failed_episode_recovery_is_retried_once_and_can_continue(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}])
    calls = []

    def recover():
        calls.append("recover")
        runner.injector.active_faults.clear()
        return [{"type": "link_down", "recovered": True}]

    runner._recover_fault = recover
    runner.sleep = lambda seconds: calls.append(f"sleep:{seconds}")
    monkeypatch.setattr(executor_module, "monotonic", lambda: 0.0)

    cleanup = runner._cleanup_after_scenario(
        _scenario(),
        {"recovery": [{"type": "link_down", "recovered": False}]},
    )

    assert cleanup["success"] is True
    assert cleanup["status"] == "clean"
    assert cleanup["attempts"] == 1
    assert cleanup["recovery"] == [{"type": "link_down", "recovered": True}]
    assert calls == ["recover"]


def test_cleanup_retry_stops_at_scale_timeout(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}], timeout_seconds=1)
    clock = [0.0]
    calls = []
    runner._recover_fault = lambda: [{"type": "link_down", "recovered": False}]

    def sleep(seconds):
        calls.append(f"sleep:{seconds}")
        clock[0] += seconds

    runner.sleep = sleep
    monkeypatch.setattr(executor_module, "monotonic", lambda: clock[0])

    cleanup = runner._cleanup_after_scenario(
        _scenario(),
        {"recovery": [{"type": "link_down", "recovered": False}]},
    )

    assert cleanup["success"] is False
    assert cleanup["status"] == "recovery_timeout"
    assert cleanup["attempts"] == 1
    assert cleanup["duration_seconds"] == 1.0
    assert cleanup["remaining_faults"] == 1
    assert calls == ["sleep:1.0"]


def test_terminal_recovery_failure_is_not_retried(monkeypatch):
    runner = _runner(active_faults=[{"type": "device_down"}], timeout_seconds=60)
    calls = []

    def recover():
        calls.append("recover")
        return [
            {
                "type": "device_down",
                "recovered": False,
                "retryable": False,
                "error": "parking namespace is missing",
            }
        ]

    runner._recover_fault = recover
    runner.sleep = lambda seconds: calls.append(f"sleep:{seconds}")
    monkeypatch.setattr(executor_module, "monotonic", lambda: 0.0)

    cleanup = runner._cleanup_after_scenario(_scenario(), None)

    assert cleanup["success"] is False
    assert cleanup["status"] == "terminal_recovery_failure"
    assert cleanup["attempts"] == 1
    assert calls == ["recover"]


def test_cleanup_requires_shared_post_recovery_health(monkeypatch):
    runner = _runner(active_faults=[])
    clock = [0.0]
    health_checks = 0
    runner.runtime_worker = object()
    runner.traffic_controller = SimpleNamespace(
        active_flows={"flow": object()},
        verify_active_flows=lambda **kw: True,
    )
    runner.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

    def check_health(worker, **_kwargs):
        nonlocal health_checks
        assert worker is runner.runtime_worker
        health_checks += 1
        return ["network not converged"] if health_checks == 1 else []

    monkeypatch.setattr(
        executor_module,
        "check_worker_health",
        check_health,
    )
    monkeypatch.setattr(executor_module, "monotonic", lambda: clock[0])

    cleanup = runner._cleanup_after_scenario(_scenario(), None)

    assert cleanup["success"] is True
    assert cleanup["status"] == "recovered_after_retry"
    assert cleanup["attempts"] == 2
    assert cleanup["errors"] == ["post_recovery: network not converged"]


def test_cleanup_retries_background_traffic_until_scale_deadline(monkeypatch):
    runner = _runner(active_faults=[])
    clock = [0.0]
    checks = 0

    def verify(**kwargs):
        nonlocal checks
        checks += 1
        return checks >= 2

    runner.traffic_controller = SimpleNamespace(
        active_flows={"flow": object()},
        verify_active_flows=verify,
    )
    runner.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    monkeypatch.setattr(executor_module, "monotonic", lambda: clock[0])

    cleanup = runner._cleanup_after_scenario(_scenario(), None)

    assert cleanup["success"] is True
    assert cleanup["attempts"] == 2
    assert checks == 2


def test_executor_close_stops_suite_owned_traffic_once():
    runner = _runner(active_faults=[])
    calls = []
    runner._stop_traffic = lambda: calls.append("stop")

    runner.close()

    assert calls == ["stop"]


def test_recovery_health_cannot_start_window_larger_than_remaining_budget(monkeypatch):
    from netopsbench.platform.utils import control

    runner = _runner(active_faults=[{"type": "link_down"}], timeout_seconds=10)
    runner.runtime_worker = object()
    clock = [100.0]
    sleeps = []
    runner.sleep = lambda seconds: sleeps.append(seconds)

    def recover():
        runner.injector.active_faults.clear()
        return [{"type": "link_down", "recovered": True}]

    def check_health(_worker, *, deadline, **_kwargs):
        assert deadline == 110
        clock[0] = 108
        return []

    runner._recover_fault = recover
    monkeypatch.setattr(executor_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(executor_module, "check_worker_health", check_health)
    cleanup = runner._cleanup_after_scenario(_scenario(), None, baseline_window={"duration_seconds": 4})

    assert cleanup["success"] is False
    assert cleanup["status"] == "recovery_timeout"
    assert cleanup["duration_seconds"] == 8
    assert "complete observation window" in cleanup["errors"][-1]
    assert not sleeps
    assert not runner.injector.active_faults


def test_observation_that_reaches_deadline_is_not_published_as_healthy(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}], timeout_seconds=10)
    clock = [100.0]

    def recover():
        runner.injector.active_faults.clear()
        return [{"type": "link_down", "recovered": True}]

    def observe(_duration, *, baseline_window, deadline):
        assert deadline == 110
        clock[0] = deadline
        return {"data_source_status": "ok", "coverage_status": "complete"}

    runner._recover_fault = recover
    runner.calibrate = lambda scenario, reason, deadline: (
        observe(runner.minimum_baseline_seconds, baseline_window={}, deadline=deadline),
        setattr(runner._baseline_handoff, "window", {"duration_seconds": runner.minimum_baseline_seconds}),
    )
    monkeypatch.setattr(executor_module, "monotonic", lambda: clock[0])
    cleanup = runner._cleanup_after_scenario(_scenario(), None, baseline_window={"duration_seconds": 1})
    assert cleanup["success"] is False
    assert cleanup["status"] == "recovery_timeout"
    assert "validated_baseline" not in cleanup


def test_observation_stops_after_a_sleep_exhausts_its_budget(monkeypatch):
    from netopsbench.platform.scenario.observation import capture_observation_window
    from netopsbench.platform.utils import control

    clock = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds + 2  # Scheduling delay after a requested one-second sleep.

    runner = SimpleNamespace(sleep=sleep)
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        capture_observation_window(runner, 2, deadline=102.5)
    assert sleeps == [1]


def test_recovery_budget_is_explicit_and_not_derived_from_baseline(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}], timeout_seconds=120)
    runner.minimum_baseline_seconds = 60
    clock = [100.0]

    def recover():
        runner.injector.active_faults.clear()
        clock[0] += 8
        return [{"type": "link_down", "recovered": True}]

    def observe(duration, *, baseline_window, deadline):
        assert duration == 60
        assert deadline == 220
        clock[0] += duration
        return {
            "data_source_status": "ok",
            "coverage_status": "complete",
            "_baseline_coverage": {"coverage_status": "complete"},
            "pingmesh_metrics": {"quality": {"current_paths_observed": 1}},
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "duration_seconds": duration,
        }

    runner._recover_fault = recover
    runner.calibrate = lambda scenario, reason, deadline: (
        observe(runner.minimum_baseline_seconds, baseline_window={}, deadline=deadline),
        setattr(runner._baseline_handoff, "window", {"duration_seconds": runner.minimum_baseline_seconds}),
    )
    monkeypatch.setattr(executor_module, "monotonic", lambda: clock[0])
    cleanup = runner._cleanup_after_scenario(_scenario(), None, baseline_window={"duration_seconds": 60})
    assert cleanup["success"] is True
    assert cleanup["duration_seconds"] == 68
    assert cleanup["validated_baseline"]["duration_seconds"] == 60


@pytest.mark.parametrize("verified", [True, False])
def test_state_checked_readback_avoids_window_custom_callback_requires_calibration(verified):
    from netopsbench.models.recovery import RecoveryPolicy

    runner = _runner(active_faults=[{"type": "link_down"}], timeout_seconds=60)
    runner.recovery_policy = RecoveryPolicy()
    runner._baseline_scope = lambda scenario: None
    calls = []
    window = {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:00:30Z", "duration_seconds": 30}

    def recover():
        runner.injector.active_faults.clear()
        return [{"recovered": True, "readback_verified": verified}]

    def calibrate(scenario, reason, **kwargs):
        calls.append(reason)
        runner._baseline_handoff.window = dict(window)

    runner._recover_fault = recover
    runner.calibrate = calibrate
    result = runner._cleanup_after_scenario(_scenario(), {}, baseline_window=window)
    assert result["success"]
    assert result["validation_level"] == ("state_checked" if verified else "full_window")
    assert calls == ([] if verified else ["custom_callback"])

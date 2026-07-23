from types import SimpleNamespace

import netopsbench.platform.scenario.executor as executor_module
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.scenario.executor import ScenarioExecutor


def _scenario() -> ScenarioSpec:
    return ScenarioSpec(
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
    runner.injector = SimpleNamespace(active_faults=list(active_faults))
    runner.scale_registry = SimpleNamespace(
        get=lambda _scale: SimpleNamespace(health_timeout_seconds=timeout_seconds)
    )
    return runner


def test_clean_scenario_boundary_does_not_retry_or_sleep(monkeypatch):
    runner = _runner(active_faults=[])
    calls = []
    runner._stop_traffic = lambda: calls.append("stop")
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
        "recovery": [{"type": "link_down", "recovered": True}],
    }
    assert calls == ["stop"]


def test_failed_episode_recovery_is_retried_once_and_can_continue(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}])
    calls = []
    runner._stop_traffic = lambda: calls.append("stop")

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
    assert calls == ["stop", "recover"]


def test_cleanup_retry_stops_at_scale_timeout(monkeypatch):
    runner = _runner(active_faults=[{"type": "link_down"}], timeout_seconds=1)
    clock = [0.0]
    calls = []
    runner._stop_traffic = lambda: calls.append("stop")
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
    assert calls == ["stop", "sleep:1.0"]

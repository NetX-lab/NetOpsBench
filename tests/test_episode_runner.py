"""Tests for :class:`EpisodeRunner` extracted from ScenarioExecutor."""

from __future__ import annotations

from typing import Any

from netopsbench.platform.scenario.episode_runner import (
    EpisodeExecutionPort,
    EpisodeRunner,
)
from netopsbench.platform.scenario.models import Episode


class StubExecutor:
    """Minimal stand-in for ScenarioExecutor used by EpisodeRunner."""

    def __init__(
        self,
        *,
        skip_none_episodes: bool = True,
        post_recovery_wait_seconds: float = 0,
        injection_success: bool = True,
        observe_payload: dict[str, Any] | None = None,
    ):
        self.skip_none_episodes = skip_none_episodes
        self.post_recovery_wait_seconds = post_recovery_wait_seconds
        self._injection_success = injection_success
        self._observe_payload = observe_payload or {"observed": True}
        self.calls: list[str] = []

    def _build_skipped_episode_result(self, episode, *, start_time, end_time, waited_seconds):
        self.calls.append("skipped")
        return {
            "episode_id": episode.episode_id,
            "skipped": True,
            "waited_seconds": waited_seconds,
            "start_time": start_time,
            "end_time": end_time,
            "success": True,
        }

    def _inject_fault(self, episode):
        self.calls.append("inject")
        return {"success": self._injection_success, "fault_type": episode.fault_type}

    def _wait_and_observe(self, seconds, *, baseline_end_time=None):
        self.calls.append(f"observe:{seconds}")
        return {"duration": seconds, **self._observe_payload}

    def _merge_observation_windows(self, windows, *, total_duration_seconds):
        self.calls.append("merge")
        return {"windows": list(windows), "total": total_duration_seconds}

    def _recover_fault(self):
        self.calls.append("recover")
        return {"success": True}


def _make_episode(**overrides) -> Episode:
    base = dict(
        episode_id="ep-1",
        description="test episode",
        fault_type="link_down",
        target_device="leaf-1",
        target_interface="Ethernet1",
        target_prefix=None,
        mtu=None,
        duration_seconds=12,
        stabilization_time=0,
        metadata={"early_observation_seconds": 1},
        parameters={},
    )
    base.update(overrides)
    return Episode(**base)


def test_runner_skips_baseline_when_fault_type_none():
    executor = StubExecutor(skip_none_episodes=True)
    runner = EpisodeRunner(executor)
    episode = _make_episode(fault_type="none", duration_seconds=0, metadata={})
    result = runner.run(episode)
    assert result["skipped"] is True
    assert "inject" not in executor.calls
    assert executor.calls == ["skipped"]


def test_runner_returns_error_when_injection_fails():
    executor = StubExecutor(injection_success=False)
    runner = EpisodeRunner(executor)
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})
    result = runner.run(episode)
    assert result["success"] is False
    assert result["error"] == "Fault injection failed"
    assert "recover" not in executor.calls


def test_runner_full_happy_path_invokes_inject_observe_recover():
    executor = StubExecutor()
    runner = EpisodeRunner(executor)
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})
    result = runner.run(episode)
    assert result["success"] is True
    assert "injection" in result
    assert "observations" in result
    assert "recovery" in result
    assert "inject" in executor.calls
    assert "recover" in executor.calls
    assert "merge" in executor.calls


def test_runner_uses_executor_sleep_hook():
    executor = StubExecutor(post_recovery_wait_seconds=2)
    slept = []
    executor.sleep = slept.append
    runner = EpisodeRunner(executor)
    episode = _make_episode(duration_seconds=3, stabilization_time=1, metadata={"early_observation_seconds": 0})

    result = runner.run(episode)

    assert result["success"] is True
    assert slept == [1, 2]


def test_runner_invokes_diagnosis_callback_for_fault_episodes():
    executor = StubExecutor()
    runner = EpisodeRunner(executor)
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})

    captured: list[dict[str, Any]] = []

    def diagnose(payload):
        captured.append(payload)
        return {"verdict": "ok", "success": True}

    result = runner.run(episode, diagnosis_callback=diagnose)
    assert result["diagnosis"] == {"verdict": "ok", "success": True}
    assert captured, "diagnosis callback should have been invoked"


def test_runner_does_not_invoke_diagnosis_for_baseline_episode():
    executor = StubExecutor(skip_none_episodes=False)
    runner = EpisodeRunner(executor)
    episode = _make_episode(
        fault_type="none",
        duration_seconds=2,
        metadata={"early_observation_seconds": 0},
    )

    called = []
    runner.run(episode, diagnosis_callback=lambda payload: called.append(payload) or {})
    assert called == []


def test_runner_captures_diagnosis_exception_into_result():
    executor = StubExecutor()
    runner = EpisodeRunner(executor)
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})

    def boom(_payload):
        raise RuntimeError("diagnose failed")

    result = runner.run(episode, diagnosis_callback=boom)
    assert result["diagnosis"] == {"error": "diagnose failed", "success": False}
    assert result["success"] is True


def test_runner_attempts_recovery_on_unexpected_exception():
    class ExplodingExecutor(StubExecutor):
        def _wait_and_observe(self, seconds, *, baseline_end_time=None):
            raise RuntimeError("observe blew up")

    executor = ExplodingExecutor()
    runner = EpisodeRunner(executor)
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})
    result = runner.run(episode)
    assert result["success"] is False
    assert "observe blew up" in result["error"]
    assert "recover" in executor.calls


def test_stub_executor_satisfies_episode_execution_port_protocol():
    """The test stub must satisfy the public Protocol so production
    executors and stubs share the same typed contract."""
    assert isinstance(StubExecutor(), EpisodeExecutionPort)


def test_real_scenario_executor_satisfies_protocol():
    """ScenarioExecutor (the production host) must also satisfy the Protocol."""
    from netopsbench.platform.scenario.executor import ScenarioExecutor

    # We only need a class that *defines* the methods; we don't construct
    # a real one because it would require a topology + influxdb setup.
    # ``runtime_checkable`` Protocols verify method presence, so checking
    # against a synthetic instance is sufficient.
    for attr in (
        "_inject_fault",
        "_wait_and_observe",
        "_recover_fault",
        "_merge_observation_windows",
        "_build_skipped_episode_result",
    ):
        assert hasattr(ScenarioExecutor, attr), f"executor missing required attr: {attr}"

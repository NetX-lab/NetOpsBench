from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from netopsbench.platform.scenario import observation
from netopsbench.platform.utils.control import OperationControl, operation_scope


def test_scheduling_overhead_does_not_accumulate_per_second(monkeypatch):
    clock = [100.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds + 0.2

    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])
    runner = SimpleNamespace(sleep=sleep, topology_metadata={"pingmesh_plan": {"settings": {"plan_id": "plan"}}})
    window = observation.capture_observation_window(runner, 42)
    start = datetime.fromisoformat(window["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["end_time"].replace("Z", "+00:00"))
    assert end - start == timedelta(seconds=42)
    assert 42 <= window["actual_wait_seconds"] < 42.21
    assert len(waits) < 42


def test_outer_deadline_prevents_starting_an_incomplete_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])
    runner = SimpleNamespace(sleep=lambda _: pytest.fail("insufficient budget started waiting"))
    with operation_scope(OperationControl(deadline=110)):
        with pytest.raises(TimeoutError, match="Insufficient"):
            observation.capture_observation_window(runner, 30)


def test_precise_utc_boundary_is_anchored_once(monkeypatch):
    from datetime import UTC

    first = datetime(2026, 10, 24, 0, 0, 0, 123456, tzinfo=UTC)

    class WallClock:
        @staticmethod
        def now(_zone):
            return first  # The end boundary must be derived, not read again.

    clock = [0.0]
    monkeypatch.setattr(observation, "datetime", WallClock)
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])
    runner = SimpleNamespace(
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        topology_metadata={"pingmesh_plan": {"settings": {"plan_id": "plan"}}},
    )
    result = observation.capture_observation_window(runner, 30)
    assert result["start_time"] == "2026-10-24T00:00:00.123456Z"
    assert result["end_time"] == "2026-10-24T00:00:30.123456Z"

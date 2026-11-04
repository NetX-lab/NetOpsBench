"""Monotonic phase measurements, separate from public observations."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from time import monotonic

from netopsbench.platform.toolkit._core.control import finish_diagnosis_work
from netopsbench.platform.utils.control import OperationControl, operation_scope
from netopsbench.platform.utils.measurements import measurement_scope

_current_timings = ContextVar("case_phase_timings", default=None)


@contextmanager
def timing_scope(timings):
    token = _current_timings.set(timings)
    try:
        yield
    finally:
        _current_timings.reset(token)


@contextmanager
def agent_cleanup_timing():
    finish_diagnosis_work()
    timings = _current_timings.get()
    if timings is None:
        yield
    else:
        with timings.measure("agent_cleanup"):
            yield


class PhaseTimings:
    def __init__(self):
        self.phases = {}

    @contextmanager
    def measure(self, name, *, planned_seconds=None, timeout_seconds=None):
        started = monotonic()
        record = {"status": "running", "planned_seconds": planned_seconds, "timeout_seconds": timeout_seconds}
        self.phases[name] = record
        try:
            with (
                operation_scope(OperationControl(deadline=started + timeout_seconds))
                if timeout_seconds is not None and name != "diagnosis"
                else nullcontext()
            ):
                with measurement_scope(record.setdefault("operations", {})):
                    yield record
        except BaseException:
            record["status"] = "failed"
            raise
        else:
            if record["status"] == "running":
                record["status"] = "completed"
        finally:
            record["actual_seconds"] = max(0, monotonic() - started)

    def skip(self, name, reason):
        self.phases[name] = {"status": "skipped", "actual_seconds": 0.0, "reason": reason}

    @contextmanager
    def measure_step(self, parent, name):
        """Accumulate repeated work inside a phase, without creating a new budget."""
        steps = self.phases.setdefault(parent, {}).setdefault("steps", {})
        record = steps.setdefault(name, {"actual_seconds": 0.0, "calls": 0, "failed_calls": 0})
        started = monotonic()
        record["calls"] += 1
        record["status"] = "running"
        try:
            with measurement_scope(record.setdefault("operations", {})):
                yield record
        except BaseException:
            record["failed_calls"] += 1
            record["status"] = "failed"
            raise
        else:
            record["status"] = "completed"
        finally:
            record["actual_seconds"] += max(0.0, monotonic() - started)

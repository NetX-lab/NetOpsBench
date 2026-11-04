"""Audit instrumentation must not leak counters between fresh runtime batches."""

import sys
from types import ModuleType

from scripts.validate_runtime_refactor import CommandCounts, influxdb


def test_lazy_query_alias_is_restored_after_batch(monkeypatch):
    original = influxdb.query_flux
    with CommandCounts():
        late = ModuleType("netopsbench.audit_test_alias")
        late.query = influxdb.query_flux
        monkeypatch.setitem(sys.modules, late.__name__, late)
        assert late.query is not original
    assert late.query is original
    assert influxdb.query_flux is original


def test_shadow_preserves_production_baseline_on_success_and_failure():
    from types import SimpleNamespace

    import pytest

    from netopsbench.platform.scenario.baseline import BaselineHandoff
    from scripts.validate_runtime_refactor import verify_shadow

    for failed in (False, True):
        baseline = BaselineHandoff(window={"end_time": "original"}, uses=9, ready=True)
        runner = SimpleNamespace(
            _baseline_handoff=baseline,
            _last_calibration={"success": True, "reason": "original"},
            _calibration_current=False,
        )

        def calibrate(case, reason, runner=runner, failed=failed):
            runner._baseline_handoff.window = {"end_time": "new"}
            runner._baseline_handoff.uses = 0
            runner._calibration_current = True
            runner._last_calibration = {"success": not failed, "reason": reason}
            if failed:
                raise RuntimeError("shadow failed")

        runner.calibrate = calibrate
        if failed:
            with pytest.raises(RuntimeError, match="shadow failed"):
                verify_shadow(runner, None)
        else:
            assert verify_shadow(runner, None)["reason"] == "shadow_validation"
        assert runner._baseline_handoff.window == {"end_time": "original"}
        assert runner._baseline_handoff.uses == 9
        assert runner._baseline_handoff.ready
        assert runner._last_calibration == {"success": True, "reason": "original"}
        assert not runner._calibration_current

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

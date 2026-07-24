"""Regression tests for explicit topology propagation across runtime helpers."""

from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.topology.generator import generate_topology


def test_scenario_executor_passes_explicit_topology_to_fault_injector(monkeypatch, tmp_path):
    topology_dir = tmp_path / "generated_topology_xs"
    metadata = generate_topology("xs", str(topology_dir), name="dcn-test")["metadata"]

    captured = {}

    class FakeFaultInjector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("netopsbench.platform.scenario.executor.FaultInjector", FakeFaultInjector)

    runner = ScenarioExecutor(topology_dir=str(topology_dir))

    assert runner.topology_dir == str(topology_dir)
    assert captured["clab_dir"] == str(topology_dir)
    assert captured["topology_metadata"] == metadata


def test_scenario_executor_is_not_exported_from_platform_scenario_namespace():
    import netopsbench.platform.scenario as scenario_mod

    assert not hasattr(scenario_mod, "ScenarioExecutor")

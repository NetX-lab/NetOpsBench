"""Regression tests for explicit topology propagation across runtime helpers."""

import json

from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.session import context as runtime_agent_context


def test_scenario_executor_passes_explicit_topology_to_fault_injector(monkeypatch, tmp_path):
    topology_dir = tmp_path / "generated_topology_xs"
    topology_dir.mkdir()
    metadata = {"name": "dcn-test", "devices": {"spines": [], "leafs": [], "clients": []}}
    (topology_dir / "topology.json").write_text(json.dumps(metadata), encoding="utf-8")

    captured = {}

    class FakeFaultInjector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("netopsbench.platform.scenario.executor.FaultInjector", FakeFaultInjector)

    runner = ScenarioExecutor(topology_dir=str(topology_dir))

    assert runner.topology_dir == str(topology_dir)
    assert captured["clab_dir"] == str(topology_dir)
    assert captured["topology_metadata"] == metadata


def test_build_toolkit_for_topology_uses_explicit_metadata(monkeypatch, tmp_path):
    topology_dir = tmp_path / "generated_topology_xs"
    topology_dir.mkdir()
    metadata = {"name": "dcn-test", "devices": {"spines": [], "leafs": [], "clients": []}}
    (topology_dir / "topology.json").write_text(json.dumps(metadata), encoding="utf-8")
    (topology_dir / "custom.clab.yaml").write_text("name: dcn-test\n", encoding="utf-8")

    captured = {}

    class FakeToolkit:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(runtime_agent_context, "AgentToolkit", FakeToolkit)

    runtime_agent_context._build_toolkit_for_topology(str(topology_dir))

    assert captured["topology_metadata"] == metadata
    assert captured["topology_file"].endswith("custom.clab.yaml")


def test_scenario_executor_is_not_exported_from_platform_scenario_namespace():
    import netopsbench.platform.scenario as scenario_mod

    assert not hasattr(scenario_mod, "ScenarioExecutor")

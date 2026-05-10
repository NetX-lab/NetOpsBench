"""Tests for fail-fast topology loading behavior."""

import pytest

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.toolkit.toolkit import AgentToolkit


def test_agent_toolkit_fails_fast_without_topology_metadata(monkeypatch, tmp_path):
    monkeypatch.delenv("NETOPSBENCH_TOPOLOGY_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(AgentToolkit, "_discover_topology_dir", lambda self, base_dir: str(tmp_path))

    with pytest.raises(FileNotFoundError, match="topology.json"):
        AgentToolkit()


def test_fault_injector_fails_fast_without_topology_metadata(monkeypatch, tmp_path):
    monkeypatch.delenv("NETOPSBENCH_TOPOLOGY_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(FaultInjector, "_discover_topology_dir", lambda self, base_dir: str(tmp_path))

    with pytest.raises(FileNotFoundError, match="topology.json"):
        FaultInjector()


def test_agent_toolkit_ignores_deprecated_topology_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("NETOPSBENCH_TOPOLOGY_DIR", raising=False)
    monkeypatch.setenv("NETOPSBENCH_ALLOW_TOPOLOGY_FALLBACK", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(AgentToolkit, "_discover_topology_dir", lambda self, base_dir: str(tmp_path))

    with pytest.raises(FileNotFoundError, match="topology.json"):
        AgentToolkit()


def test_fault_injector_ignores_deprecated_topology_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("NETOPSBENCH_TOPOLOGY_DIR", raising=False)
    monkeypatch.setenv("NETOPSBENCH_ALLOW_TOPOLOGY_FALLBACK", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(FaultInjector, "_discover_topology_dir", lambda self, base_dir: str(tmp_path))

    with pytest.raises(FileNotFoundError, match="topology.json"):
        FaultInjector()


def test_agent_toolkit_discovery_considers_benchmark_generated_topologies(tmp_path):
    base_dir = tmp_path
    benchmark_dir = base_dir / "lab-topology" / "benchmarks" / "generated_topology_small"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "topology.json").write_text("{}", encoding="utf-8")

    toolkit = AgentToolkit.__new__(AgentToolkit)

    discovered = AgentToolkit._discover_topology_dir(toolkit, str(base_dir))

    assert discovered == str(benchmark_dir)

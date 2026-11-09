"""Tests for explicit, fail-fast topology loading behavior."""

import pytest

from netopsbench.config import config
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.generator import generate_topology


def test_agent_toolkit_fails_fast_without_topology_metadata(tmp_path):
    with pytest.raises(FileNotFoundError, match="topology.json"):
        AgentToolkit(topology_dir=tmp_path)


def test_fault_injector_fails_fast_without_topology_metadata(tmp_path):
    with pytest.raises(FileNotFoundError, match="topology.json"):
        FaultInjector(clab_dir=str(tmp_path))


def test_toolkit_uses_only_the_explicit_topology_directory(tmp_path):
    selected_dir = tmp_path / "selected"
    unrelated_dir = tmp_path / "newer"
    generate_topology("xs", str(selected_dir), name="selected-lab")
    generate_topology("medium", str(unrelated_dir), name="unrelated-lab")

    toolkit = AgentToolkit(topology_dir=selected_dir)

    assert toolkit.manifest.name == "selected-lab"
    assert toolkit.manifest.scale == "xs"


def test_toolkit_does_not_discover_topology_from_current_directory(tmp_path, monkeypatch):
    generate_topology("xs", str(tmp_path), name="cwd-lab")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "topology_dir", None)

    with pytest.raises(ValueError, match="explicit topology_dir"):
        AgentToolkit()

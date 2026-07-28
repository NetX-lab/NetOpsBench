"""Tests for centralized fault registry dispatch."""

import importlib
import tempfile

import pytest

from netopsbench.models.scenario import EpisodeSpec
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.topology.generator import generate_topology


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


def test_scenario_runner_uses_registered_fault_spec(monkeypatch):
    from netopsbench.platform.faults.specs import FaultSpec, create_fault_registry

    captured = {}

    def inject_episode(injector, episode):
        captured["injector"] = injector
        captured["episode_id"] = episode.episode_id
        captured["fault_type"] = episode.fault_type
        return {"success": True, "type": episode.fault_type, "source": "registry"}

    registry = create_fault_registry()
    registry.register(FaultSpec(name="synthetic_fault", inject_episode=inject_episode))
    runner = ScenarioExecutor(
        topology_dir="lab-topology",
        topology_metadata=_metadata(),
        fault_registry=registry,
    )
    episode = EpisodeSpec(
        episode_id="ep_synth",
        description="Synthetic fault via registry",
        fault_type="synthetic_fault",
        target_device="leaf1",
    )

    result = runner._inject_fault(episode)

    assert result["success"] is True
    assert result["source"] == "registry"
    assert captured["episode_id"] == "ep_synth"


def test_fault_registries_do_not_share_custom_specs():
    from netopsbench.platform.faults.specs import FaultSpec, create_fault_registry

    first = create_fault_registry()
    second = create_fault_registry()
    first.register(FaultSpec(name="isolated_fault"))

    assert first.get("isolated_fault") is not None
    assert second.get("isolated_fault") is None


def test_fault_spec_validate_episode_supports_prefix_and_required_parameters():
    from netopsbench.platform.faults.specs import FaultSpec

    spec = FaultSpec(
        name="validated_fault",
        requires_prefix=True,
        required_parameters=("target_ip", "wrong_nexthop"),
    )

    episode = type(
        "Episode",
        (),
        {
            "target_prefix": None,
            "parameters": {"target_ip": "192.168.0.1/32"},
            "metadata": {"wrong_nexthop": "192.168.0.254"},
        },
    )()

    errors = spec.validate_episode(episode, episode_index=2)

    assert "Episode 2: validated_fault requires target_prefix" in errors
    assert "Episode 2: validated_fault requires parameter 'wrong_nexthop'" not in errors
    assert "Episode 2: validated_fault requires parameter 'target_ip'" not in errors


def test_fault_registry_shim_module_is_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("netopsbench.platform.faults_registry")


def test_fault_execution_helpers_are_available_under_faults_subsystem():
    from netopsbench.platform.faults.scenario_execution import inject_fault, recover_fault

    assert callable(inject_fault)
    assert callable(recover_fault)

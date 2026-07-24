"""Tests for the public canonical scenario API."""

from __future__ import annotations

import pytest

from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.faults.specs import FaultSpec
from netopsbench.platform.scenario.parser import scenario_from_dict
from netopsbench.platform.scenario.validator import validate_scenario
from netopsbench.sdk.exceptions import ScenarioValidationError
from netopsbench.sdk.scenarios import ScenarioManager


def test_scenario_manager_can_create_and_roundtrip_yaml(tmp_path):
    manager = ScenarioManager(workspace=tmp_path)
    scenario = manager.create(
        id="scenario_x",
        name="Scenario X",
        description="desc",
        scale="small",
        episode={
            "episode_id": "diagnosis",
            "fault_type": "static_route_misconfig",
            "target_device": "leaf1",
            "parameters": {"target_ip": "auto", "wrong_nexthop": "auto"},
        },
        metadata={"difficulty": "medium"},
    )

    out = tmp_path / "scenario_x.yaml"
    loaded = manager.load(manager.save(scenario, out))

    assert loaded == scenario
    assert loaded.id == "scenario_x"
    assert loaded.scale == "small"
    assert loaded.episode.fault_type == "static_route_misconfig"
    assert scenario_from_dict(scenario.to_dict()) == scenario


def test_scenario_save_atomically_preserves_existing_file_on_replace_failure(tmp_path, monkeypatch):
    from netopsbench.platform.utils import files

    manager = ScenarioManager(workspace=tmp_path)
    scenario = manager.create(
        id="atomic",
        name="Atomic",
        episode={"episode_id": "diagnosis", "fault_type": "none"},
    )
    target = tmp_path / "atomic.yaml"
    target.write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(files.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        manager.save(scenario, target)

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".atomic.yaml.*.tmp")) == []


def test_legacy_multi_episode_input_has_explicit_migration_error():
    with pytest.raises(ValueError, match="single 'episode'"):
        scenario_from_dict(
            {
                "scenario_id": "legacy",
                "name": "Legacy",
                "topology_scale": "xs",
                "episodes": [{"episode_id": "diagnosis", "fault_type": "none"}],
            }
        )


@pytest.mark.parametrize("profile", ["light", "stress"])
def test_scenario_manager_rejects_nonstandard_traffic_profile(tmp_path, profile):
    manager = ScenarioManager(workspace=tmp_path)

    with pytest.raises(ScenarioValidationError, match="Only the standard traffic profile is supported"):
        manager.create(
            id="legacy_profile",
            name="Legacy Profile",
            traffic_profile=profile,
            episode={"episode_id": "diagnosis", "fault_type": "none"},
        )


def test_supported_scales_are_available_from_public_sdk():
    from netopsbench.sdk import supported_scales

    assert supported_scales() == ("xs", "small", "medium", "large", "xlarge", "fat-tree-k8", "fat-tree-k12")


def test_scenario_validation_uses_fault_registry(tmp_path):
    from netopsbench.sdk import NetOpsBench

    bench = NetOpsBench(workspace=str(tmp_path))
    bench.faults.register(
        spec=FaultSpec(name="public_registry_fault", required_parameters=("probe",)),
        executor=type(
            "Executor",
            (),
            {"inject": lambda self, context: {}, "recover": lambda self, context: {}},
        )(),
    )
    scenario = bench.scenarios.create(
        id="registry_case",
        name="Registry Case",
        scale="xs",
        episode={
            "episode_id": "diagnosis",
            "fault_type": "public_registry_fault",
            "target_device": "leaf1",
            "parameters": {"probe": "icmp"},
        },
        metadata={"difficulty": "easy"},
    )

    assert bench.scenarios.validate(scenario) == []


def test_validate_scenario_does_not_mutate_fault_type_alias():
    scenario = ScenarioSpec(
        scenario_id="alias_case",
        name="Alias Case",
        topology_scale="xs",
        metadata={"difficulty": "easy"},
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="static_route_misconfiguration",
            target_device="leaf1",
        ),
    )

    errors = validate_scenario(scenario)

    assert errors == []
    assert scenario.episode.fault_type == "static_route_misconfiguration"


def test_scenario_models_are_frozen_canonical_values():
    scenario = ScenarioManager().create(
        id="immutable_case",
        name="Immutable Case",
        episode={"episode_id": "diagnosis", "fault_type": "none"},
    )

    with pytest.raises(Exception, match="frozen"):
        scenario.name = "mutated"

"""Schema regression tests for normalized scenario parameters."""

import yaml

from netopsbench.platform.faults.specs import FaultSpec, register_fault_spec, unregister_fault_spec
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.models import Episode, Scenario
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario


def test_parse_scenario_allows_none_episode_without_target_device(tmp_path):
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "scenario_id": "baseline_only",
                "name": "Baseline Only",
                "description": "No fault episode",
                "topology_scale": "xs",
                "traffic_profile": "standard",
                "episodes": [
                    {
                        "episode_id": "ep001",
                        "description": "baseline",
                        "fault_type": "none",
                        "duration_seconds": 10,
                        "stabilization_time": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))

    assert scenario.episodes[0].target_device is None
    assert validate_scenario(scenario) == []


def test_parse_scenario_preserves_episode_parameters(tmp_path):
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "scenario_id": "static_route_case",
                "name": "Static route case",
                "description": "parameter parsing",
                "topology_scale": "xs",
                "traffic_profile": "standard",
                "metadata": {"difficulty": "medium", "expected_diagnosis": "static_route_misconfiguration"},
                "episodes": [
                    {
                        "episode_id": "ep001",
                        "description": "fault",
                        "fault_type": "static_route_misconfiguration",
                        "target_device": "leaf1",
                        "duration_seconds": 10,
                        "stabilization_time": 1,
                        "parameters": {
                            "target_ip": "192.168.102.2/32",
                            "wrong_nexthop": "auto",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))

    assert scenario.episodes[0].fault_type == "static_route_misconfig"
    assert scenario.episodes[0].parameters == {
        "target_ip": "192.168.102.2/32",
        "wrong_nexthop": "auto",
    }
    assert validate_scenario(scenario) == []


def test_validate_scenario_rejects_blackhole_route_without_target_prefix(tmp_path):
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "scenario_id": "blackhole_without_prefix",
                "name": "Blackhole without prefix",
                "description": "missing required prefix",
                "topology_scale": "xs",
                "traffic_profile": "standard",
                "metadata": {"difficulty": "medium", "expected_diagnosis": "blackhole_route"},
                "episodes": [
                    {
                        "episode_id": "ep001",
                        "description": "fault",
                        "fault_type": "blackhole_route",
                        "target_device": "leaf1",
                        "duration_seconds": 10,
                        "stabilization_time": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))
    errors = validate_scenario(scenario)

    assert any("blackhole_route requires target_prefix" in error for error in errors)


def test_validate_scenario_accepts_runtime_registered_fault():
    register_fault_spec(FaultSpec(name="synthetic_schema_fault"))
    try:
        scenario = type(
            "Scenario",
            (),
            {
                "scenario_id": "synthetic",
                "name": "Synthetic",
                "episodes": [
                    type(
                        "Episode",
                        (),
                        {
                            "episode_id": "ep1",
                            "fault_type": "synthetic_schema_fault",
                            "target_device": "leaf1",
                            "target_interface": None,
                            "target_prefix": None,
                        },
                    )()
                ],
                "topology_scale": "xs",
                "traffic_profile": "standard",
                "metadata": {"difficulty": "easy", "expected_diagnosis": "synthetic_schema_fault"},
            },
        )()
        assert validate_scenario(scenario) == []
    finally:
        unregister_fault_spec("synthetic_schema_fault")


def test_validate_scenario_enforces_blackhole_route_target_prefix():
    scenario = type(
        "Scenario",
        (),
        {
            "scenario_id": "blackhole_case",
            "name": "Blackhole case",
            "episodes": [
                type(
                    "Episode",
                    (),
                    {
                        "episode_id": "ep1",
                        "fault_type": "blackhole_route",
                        "target_device": "leaf1",
                        "target_interface": None,
                        "target_prefix": None,
                        "parameters": {},
                        "metadata": {},
                    },
                )()
            ],
            "topology_scale": "xs",
            "traffic_profile": "standard",
            "metadata": {"difficulty": "easy", "expected_diagnosis": "blackhole_route"},
        },
    )()

    errors = validate_scenario(scenario)

    assert "Episode 0: blackhole_route requires target_prefix" in errors


def test_validate_scenario_runs_custom_fault_spec_validator():
    def validate_episode(episode):
        if getattr(episode, "target_device", None) != "leaf1":
            return ["custom validator requires target_device to be leaf1"]
        return []

    register_fault_spec(FaultSpec(name="synthetic_validated_fault", episode_validator=validate_episode))
    try:
        scenario = type(
            "Scenario",
            (),
            {
                "scenario_id": "synthetic_validator",
                "name": "Synthetic Validator",
                "episodes": [
                    type(
                        "Episode",
                        (),
                        {
                            "episode_id": "ep1",
                            "fault_type": "synthetic_validated_fault",
                            "target_device": "leaf2",
                            "target_interface": None,
                            "target_prefix": None,
                            "parameters": {},
                            "metadata": {},
                        },
                    )()
                ],
                "topology_scale": "xs",
                "traffic_profile": "standard",
                "metadata": {"difficulty": "easy", "expected_diagnosis": "synthetic_validated_fault"},
            },
        )()

        errors = validate_scenario(scenario)

        assert "custom validator requires target_device to be leaf1" in errors
    finally:
        unregister_fault_spec("synthetic_validated_fault")


def test_scenario_runner_prefers_parameters_over_metadata(monkeypatch):
    runner = ScenarioExecutor(
        topology_dir="lab-topology",
        topology_metadata={"name": "dcn", "devices": {"spines": [], "leafs": [], "clients": []}},
    )
    captured = {}

    def fake_inject(device, target_ip, wrong_nexthop):
        captured["device"] = device
        captured["target_ip"] = target_ip
        captured["wrong_nexthop"] = wrong_nexthop
        return {"success": True}

    monkeypatch.setattr(runner.injector, "inject_static_route_misconfig", fake_inject)

    episode = Episode(
        episode_id="ep001",
        description="fault",
        fault_type="static_route_misconfig",
        target_device="leaf1",
        metadata={"target_ip": "old", "wrong_nexthop": "old-hop"},
        parameters={"target_ip": "new", "wrong_nexthop": "new-hop"},
    )

    result = runner._inject_fault(episode)

    assert result["success"] is True
    assert captured == {
        "device": "leaf1",
        "target_ip": "new",
        "wrong_nexthop": "new-hop",
    }


def test_scenario_executor_can_return_result_without_persisting_raw_file(tmp_path, monkeypatch):
    runner = ScenarioExecutor(
        topology_dir="lab-topology",
        topology_metadata={"name": "dcn", "devices": {"spines": [], "leafs": [], "clients": []}},
        baseline_wait_seconds=3,
        sleep_fn=lambda _seconds: None,
        persist_results=False,
    )
    runner.results_dir = tmp_path
    monkeypatch.setattr(runner, "_setup_traffic", lambda scale, profile: {"scale": scale, "profile": profile})
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    monkeypatch.setattr(runner, "_recover_fault", lambda: {"success": True})

    scenario = Scenario(
        scenario_id="no-persist",
        name="No Persist",
        description="test",
        topology_scale="xs",
        traffic_profile="standard",
        episodes=[],
    )

    result = runner.run_scenario(scenario)

    assert result["success"] is True
    assert "result_file" not in result
    assert list(tmp_path.iterdir()) == []

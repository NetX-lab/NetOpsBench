"""Schema regression tests for normalized scenario parameters."""

import ipaddress
import json
import random

import pytest
import yaml

from netopsbench.models import topology as topology_models
from netopsbench.models.profiles import default_scale_registry
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.models.topology import Collector, Device, DeviceRole, Management, TopologyManifest
from netopsbench.platform.faults.specs import FaultSpec, create_fault_registry
from netopsbench.platform.pingmesh.generator import PinglistGenerator
from netopsbench.platform.scenario import generator as scenario_generator
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario, validate_scenario_topology
from netopsbench.platform.topology.generator import generate_topology


def _minimal_canonical_topology() -> dict:
    manifest = TopologyManifest(
        topology_id="dcn",
        name="dcn",
        scale="xs",
        family="clos",
        management=Management(network="clab-dcn", ipv4_subnet="172.20.20.0/24"),
        collector=Collector(ipv4="172.20.20.200"),
        defaults=topology_models.TopologyDefaults(),
        facts=topology_models.TopologyFacts(
            num_spines=1,
            num_leafs=1,
            clients_per_attached_switch=1,
            total_clients=0,
            total_switches=2,
        ),
        routing=topology_models.RoutingMetadata(ecmp_hash_policy_by_role={DeviceRole.SPINE: 1, DeviceRole.LEAF: 1}),
        devices=[
            Device(name="spine1", role=DeviceRole.SPINE),
            Device(name="leaf1", role=DeviceRole.LEAF),
        ],
        links=[],
    )
    return manifest.model_dump(mode="json")


def test_all_diagnostic_observation_durations_use_complete_pingmesh_window():
    k12 = topology_models.PingmeshPolicy(
        destination_batch_size=16,
        rtt_port_pool_size=16,
        rtt_ports_per_cycle=4,
        cycle_interval_seconds=2,
    )
    manifest = TopologyManifest.model_validate(
        {
            **_minimal_canonical_topology(),
            "facts": {
                "clients_per_attached_switch": 2,
                "total_clients": 144,
                "total_switches": 180,
            },
            "pingmesh": k12.model_dump(),
        }
    )

    assert scenario_generator.diagnostic_observation_duration(20, manifest) == 77
    assert scenario_generator.diagnostic_observation_duration(30, manifest) == 77
    assert scenario_generator.diagnostic_observation_duration(90, manifest) == 90

    xlarge = manifest.model_copy(
        update={
            "facts": manifest.facts.model_copy(update={"total_clients": 128}),
            "pingmesh": topology_models.PingmeshPolicy(
                destination_batch_size=16,
                rtt_port_pool_size=16,
                rtt_ports_per_cycle=4,
                cycle_interval_seconds=2,
            ),
        }
    )
    assert scenario_generator.diagnostic_observation_duration(30, xlarge) == 69


@pytest.mark.parametrize(
    ("scale", "expected_window"),
    [
        ("xs", 7),
        ("small", 7),
        ("medium", 8),
        ("large", 9),
        ("xlarge", 69),
        ("fat-tree-k8", 69),
        ("fat-tree-k12", 77),
    ],
)
def test_builtin_scale_complete_windows_are_topology_derived(scale, expected_window):
    profile = default_scale_registry().get(scale)
    policy = topology_models.PingmeshPolicy(
        destination_batch_size=profile.pingmesh_destination_batch_size,
        rtt_port_pool_size=profile.pingmesh_rtt_port_pool_size,
        rtt_ports_per_cycle=profile.pingmesh_rtt_ports_per_cycle,
        cycle_interval_seconds=profile.pingmesh_cycle_interval_seconds,
    )

    assert policy.complete_window_seconds(profile.total_clients) == expected_window
    assert max(30, policy.complete_window_seconds(profile.total_clients)) == (
        expected_window if scale in {"xlarge", "fat-tree-k8", "fat-tree-k12"} else 30
    )


def test_custom_pingmesh_policy_derives_complete_window_without_scale_name():
    policy = topology_models.PingmeshPolicy(
        destination_batch_size=10,
        rtt_port_pool_size=12,
        rtt_ports_per_cycle=3,
        cycle_interval_seconds=3,
    )

    assert policy.coverage_epoch_seconds(41) == 48
    assert policy.coverage_grace_seconds() == 6
    assert policy.complete_window_seconds(41) == 54


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
                "episode": {
                    "episode_id": "diagnosis",
                    "description": "baseline",
                    "fault_type": "none",
                    "duration_seconds": 10,
                    "stabilization_time": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))

    assert scenario.episode.target_device is None
    assert validate_scenario(scenario) == []


def test_validate_scenario_accepts_xlarge_scale(tmp_path):
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "scenario_id": "generated_healthy_network_xlarge_001",
                "name": "XLarge healthy case",
                "description": "No fault episode",
                "topology_scale": "xlarge",
                "traffic_profile": "standard",
                "episode": {
                    "episode_id": "diagnosis",
                    "description": "baseline",
                    "fault_type": "none",
                    "duration_seconds": 10,
                    "stabilization_time": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))

    assert validate_scenario(scenario) == []


@pytest.mark.parametrize("profile", ["light", "stress"])
def test_validate_scenario_rejects_nonstandard_traffic_profile(profile):
    with pytest.raises(ValueError, match="traffic_profile"):
        ScenarioSpec.model_validate(
            {
                "scenario_id": "nonstandard_traffic",
                "name": "Nonstandard traffic",
                "topology_scale": "xs",
                "traffic_profile": profile,
                "episode": {"episode_id": "diagnosis", "fault_type": "none"},
            }
        )


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
                "metadata": {"difficulty": "medium"},
                "episode": {
                    "episode_id": "diagnosis",
                    "description": "fault",
                    "fault_type": "static_route_misconfiguration",
                    "target_device": "leaf1",
                    "duration_seconds": 10,
                    "stabilization_time": 1,
                    "parameters": {
                        "target_ip": "192.168.102.2/32",
                        "wrong_nexthop": "auto",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))

    assert scenario.episode.fault_type == "static_route_misconfig"
    assert scenario.episode.parameters == {
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
                "metadata": {"difficulty": "medium"},
                "episode": {
                    "episode_id": "diagnosis",
                    "description": "fault",
                    "fault_type": "blackhole_route",
                    "target_device": "leaf1",
                    "duration_seconds": 10,
                    "stabilization_time": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    scenario = parse_scenario_file(str(scenario_path))
    errors = validate_scenario(scenario)

    assert any("blackhole_route requires target_prefix" in error for error in errors)


def test_validate_scenario_accepts_runtime_registered_fault():
    registry = create_fault_registry()
    registry.register(FaultSpec(name="synthetic_schema_fault"))
    scenario = ScenarioSpec(
        scenario_id="synthetic",
        name="Synthetic",
        topology_scale="xs",
        metadata={"difficulty": "easy"},
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="synthetic_schema_fault",
            target_device="leaf1",
        ),
    )
    assert validate_scenario(scenario, fault_registry=registry) == []


def test_validate_scenario_enforces_blackhole_route_target_prefix():
    scenario = ScenarioSpec(
        scenario_id="blackhole_case",
        name="Blackhole case",
        topology_scale="xs",
        metadata={"difficulty": "easy"},
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="blackhole_route",
            target_device="leaf1",
        ),
    )

    errors = validate_scenario(scenario)

    assert "Episode 0: blackhole_route requires target_prefix" in errors


def test_validate_scenario_runs_custom_fault_spec_validator():
    def validate_episode(episode):
        if getattr(episode, "target_device", None) != "leaf1":
            return ["custom validator requires target_device to be leaf1"]
        return []

    registry = create_fault_registry()
    registry.register(FaultSpec(name="synthetic_validated_fault", episode_validator=validate_episode))
    scenario = ScenarioSpec(
        scenario_id="synthetic_validator",
        name="Synthetic Validator",
        topology_scale="xs",
        metadata={"difficulty": "easy"},
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="synthetic_validated_fault",
            target_device="leaf2",
        ),
    )

    errors = validate_scenario(scenario, fault_registry=registry)

    assert "custom validator requires target_device to be leaf1" in errors


def test_validate_scenario_topology_accepts_fat_tree_agg_target(tmp_path):
    topology_dir = tmp_path / "generated_topology_fat-tree-k8"
    generate_topology("fat-tree-k8", str(topology_dir), name="ft")
    scenario = ScenarioSpec(
        scenario_id="fat_tree_agg_fault",
        name="Fat-tree aggregation fault",
        topology_scale="fat-tree-k8",
        metadata={"difficulty": "easy"},
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="link_down",
            target_device="agg1",
            target_interface="Ethernet0",
            duration_seconds=69,
        ),
    )

    result = validate_scenario_topology(scenario, str(topology_dir))

    assert result["status"] == "pass"
    assert result["actual_scale"] == "fat-tree-k8"
    assert "agg1" in result["topology_devices"]


def test_validate_scenario_topology_rejects_window_shorter_than_manifest_requires(tmp_path):
    topology_dir = tmp_path / "generated_topology_xs"
    generate_topology("xs", str(topology_dir), name="xs-window")
    scenario = ScenarioSpec(
        scenario_id="short-window",
        name="Short observation window",
        topology_scale="xs",
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="none",
            duration_seconds=6,
        ),
    )

    result = validate_scenario_topology(scenario, str(topology_dir))

    assert result["status"] == "fail"
    assert "topology-derived Pingmesh complete window (7s)" in result["errors"][0]


def test_validate_scenario_topology_rejects_legacy_grouped_topology(tmp_path):
    topology_dir = tmp_path / "legacy_topology"
    topology_dir.mkdir()
    (topology_dir / "topology.json").write_text(
        json.dumps(
            {
                "name": "legacy",
                "topology_scale": "xs",
                "devices": {"spines": [{"name": "spine1"}], "leafs": [{"name": "leaf1"}], "clients": []},
            }
        ),
        encoding="utf-8",
    )
    scenario = ScenarioSpec(
        scenario_id="legacy_schema",
        name="Legacy schema",
        topology_scale="xs",
        episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
    )

    with pytest.raises(ValueError, match="schema_version.*3.*Regenerate"):
        validate_scenario_topology(scenario, str(topology_dir))


def test_scenario_generator_supports_fat_tree_roles_with_structured_artifacts(tmp_path):
    topology_dir = tmp_path / "topology"
    generate_topology("fat-tree-k8", str(topology_dir))
    topo = scenario_generator.load_topology("fat-tree-k8", str(topology_dir))

    assert topo.cores[0] == "core1"
    assert topo.aggs[0] == "agg1"
    assert topo.edges[0] == "edge1"
    assert "Ethernet0" in topo.device_interfaces["agg1"]

    spec = {
        "defaults": {"count_per_fault": 1, "seed": 1},
        "fault_templates": [
            {
                "name": "link_flapping_agg",
                "fault_type": "link_flapping",
                "device_role": "agg",
                "interface_role": "uplink",
            }
        ],
    }
    generated = scenario_generator.generate(spec, topo, tmp_path / "scenarios", seed=1)

    scenario = parse_scenario_file(str(generated[0]))
    assert scenario.topology_scale == "fat-tree-k8"
    assert scenario.episode.target_device.startswith("agg")
    assert validate_scenario(scenario) == []


@pytest.mark.parametrize("location", ["defaults", "template"])
def test_scenario_generator_rejects_nonstandard_traffic_profile(tmp_path, location):
    topology_dir = tmp_path / "topology"
    generate_topology("xs", str(topology_dir))
    topo = scenario_generator.load_topology("xs", str(topology_dir))
    spec = {
        "defaults": {"count_per_fault": 1},
        "fault_templates": [{"name": "healthy", "fault_type": "none"}],
    }
    if location == "defaults":
        spec["defaults"]["traffic_profile"] = "stress"
    else:
        spec["fault_templates"][0]["traffic_profile"] = "light"

    with pytest.raises(ValueError, match="Only the standard traffic profile is supported"):
        scenario_generator.generate(spec, topo, tmp_path / "scenarios", seed=1)


def _xlarge_topology_context(tmp_path):
    topology_dir = tmp_path / "topology"
    generate_topology("xlarge", str(topology_dir))
    return scenario_generator.load_topology("xlarge", str(topology_dir))


def _pingmesh_destination_names(topo, src_leaf: str) -> set[str]:
    tasks = PinglistGenerator().generate(topo.manifest.model_dump(mode="json"))
    return {task.dst_name for task in tasks if task.src_leaf == src_leaf}


def _client_for_prefix(topo, prefix: str) -> dict:
    network = ipaddress.ip_network(prefix, strict=False)
    return next(client for client in topo.clients if ipaddress.ip_address(client["data_ip"]) in network)


def _client_for_host_route(topo, host_route: str) -> dict:
    host_ip = host_route.split("/", 1)[0]
    return next(client for client in topo.clients if client["data_ip"] == host_ip)


def test_xlarge_blackhole_route_targets_pingmesh_observable_destination(tmp_path):
    topo = _xlarge_topology_context(tmp_path)

    scenario = scenario_generator.build_fault_instance(
        "blackhole_route",
        "medium",
        topo,
        random.Random(1),
        {"seed": 1},
        {"name": "blackhole_route_leaf", "fault_type": "blackhole_route", "device_role": "leaf"},
        1,
    )

    fault = scenario["episode"]
    target_client = _client_for_prefix(topo, fault["target_prefix"])

    assert target_client["name"] in _pingmesh_destination_names(topo, fault["target_device"])


def test_xlarge_static_route_targets_pingmesh_observable_destination(tmp_path):
    topo = _xlarge_topology_context(tmp_path)

    scenario = scenario_generator.build_fault_instance(
        "static_route_misconfig",
        "medium",
        topo,
        random.Random(1),
        {"seed": 1},
        {"name": "static_route_misconfig", "fault_type": "static_route_misconfig", "device_role": "leaf"},
        1,
    )

    fault = scenario["episode"]
    target_client = _client_for_host_route(topo, fault["metadata"]["target_ip"])

    assert target_client["name"] in _pingmesh_destination_names(topo, fault["target_device"])


def test_scenario_runner_prefers_parameters_over_metadata(monkeypatch):
    runner = ScenarioExecutor(
        topology_dir="lab-topology",
        topology_metadata=_minimal_canonical_topology(),
    )
    captured = {}

    def fake_inject(device, target_ip, wrong_nexthop):
        captured["device"] = device
        captured["target_ip"] = target_ip
        captured["wrong_nexthop"] = wrong_nexthop
        return {"success": True}

    monkeypatch.setattr(runner.injector, "inject_static_route_misconfig", fake_inject)

    episode = EpisodeSpec(
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
    events = []
    runner = ScenarioExecutor(
        topology_dir="lab-topology",
        topology_metadata=_minimal_canonical_topology(),
        minimum_baseline_seconds=3,
        sleep_fn=lambda _seconds: None,
        persist_results=False,
    )
    runner.results_dir = tmp_path
    def setup_traffic(scale, profile):
        events.append("traffic_ready")
        return {"scale": scale, "profile": profile}

    baseline = {"start_time": "baseline-start", "end_time": "baseline-end", "duration_seconds": 7}

    def capture_baseline():
        events.append("baseline_captured")
        return baseline

    def observe(duration, *, baseline_window):
        events.append("current_observed")
        assert baseline_window == baseline
        return {
            "start_time": "current-start",
            "end_time": "current-end",
            "duration_seconds": duration,
            "pingmesh_metrics": {"summary": {"total_anomalies": 0}, "anomalies": []},
            "anomalies_detected": False,
            "coverage_status": "complete",
            "data_source_status": "ok",
            "_coverage_audit": {"coverage_status": "complete"},
        }

    monkeypatch.setattr(runner, "_setup_traffic", setup_traffic)
    monkeypatch.setattr(runner, "_capture_baseline_window", capture_baseline)
    monkeypatch.setattr(runner, "_wait_and_observe", observe)
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    monkeypatch.setattr(runner, "_recover_fault", lambda: {"success": True})

    scenario = ScenarioSpec(
        scenario_id="no-persist",
        name="No Persist",
        description="test",
        topology_scale="xs",
        episode=EpisodeSpec(episode_id="diagnosis", fault_type="none", duration_seconds=1),
    )

    result = runner.run_scenario(scenario)

    assert result["success"] is True
    assert events == ["traffic_ready", "baseline_captured", "current_observed"]
    assert result["episode"]["coverage_audit"] == {"coverage_status": "complete"}
    assert "_coverage_audit" not in result["episode"]["observations"]
    assert "result_file" not in result
    assert list(tmp_path.iterdir()) == []

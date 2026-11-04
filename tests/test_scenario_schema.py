"""Schema regression tests for normalized scenario parameters."""

import json

import pytest
import yaml

from netopsbench.models import topology as topology_models
from netopsbench.models.profiles import get_scale_profile
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.faults.specs import FaultSpec, create_fault_registry
from netopsbench.platform.scenario import generator as scenario_generator
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario, validate_scenario_topology
from netopsbench.platform.topology.generator import generate_topology


def _minimal_canonical_topology() -> dict:
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import TopologyConfig

    return build_clos_plan(TopologyConfig(scale_name="xs")).manifest.model_dump(mode="json")


def test_all_diagnostic_observation_durations_use_complete_pingmesh_window():
    from netopsbench.platform.topology.config import config_for_scale
    from netopsbench.platform.topology.fat_tree_builder import build_fat_tree_plan

    manifest = build_fat_tree_plan(config_for_scale("fat-tree-k12")).manifest
    profile = get_scale_profile("fat-tree-k12").experiment
    complete = manifest.require_pingmesh_plan().settings.complete_window_seconds
    for requested, expected in ((20, 42), (30, 42), (90, 90)):
        assert profile.model_copy(update={"observation_seconds": requested}).window_seconds(complete) == expected


@pytest.mark.parametrize(
    "scale,expected_window",
    [
        ("xs", 7),
        ("medium", 8),
        ("large", 30),
        ("xlarge", 66),
        ("fat-tree-k8", 42),
        ("fat-tree-k12", 42),
        ("fat-tree-k18", 42),
    ],
)
def test_builtin_scale_complete_windows_are_topology_derived(scale, expected_window):
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import TopologyConfig, config_for_scale
    from netopsbench.platform.topology.fat_tree_builder import build_fat_tree_plan

    config = config_for_scale(scale)
    manifest = (build_clos_plan if isinstance(config, TopologyConfig) else build_fat_tree_plan)(config).manifest
    assert manifest.require_pingmesh_plan().settings.complete_window_seconds == expected_window
    assert get_scale_profile(scale).experiment.window_seconds(expected_window) == max(30, expected_window)


def test_custom_pingmesh_policy_derives_complete_window_without_scale_name():
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import TopologyConfig

    policy = topology_models.PingmeshPolicy(
        destination_batch_size=10, rtt_port_pool_size=12, rtt_ports_per_cycle=3, cycle_interval_seconds=3
    )
    manifest = build_clos_plan(TopologyConfig(num_leafs=42, scale_name="xs", pingmesh_policy=policy)).manifest
    assert manifest.require_pingmesh_plan().settings.coverage_epoch_seconds == 60
    assert manifest.require_pingmesh_plan().settings.complete_window_seconds == 66


def test_executor_uses_v2_without_fixed_recovery_wait(tmp_path):
    metadata = _minimal_canonical_topology()
    metadata["pingmesh"]["cycle_interval_seconds"] = 3
    from netopsbench.platform.pingmesh.planner import compile_pingmesh_plan

    manifest = TopologyManifest.model_validate(metadata)
    manifest.pingmesh_plan = compile_pingmesh_plan(manifest)
    metadata = manifest.model_dump(mode="json")

    runner = ScenarioExecutor(
        topology_dir=str(tmp_path),
        topology_metadata=metadata,
    )

    assert runner.recovery_policy.version == 2
    assert not hasattr(runner, "post_recovery_wait_seconds")


def test_parse_scenario_allows_none_episode_without_target_device(tmp_path):
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "metadata": {"recovery_policy": {"version": 2}},
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
                "metadata": {"recovery_policy": {"version": 2}},
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
                "metadata": {"recovery_policy": {"version": 2}, "difficulty": "medium"},
                "episode": {
                    "episode_id": "diagnosis",
                    "description": "fault",
                    "fault_type": "static_route_misconfiguration",
                    "target_device": "leaf1",
                    "duration_seconds": 10,
                    "stabilization_time": 1,
                    "parameters": {
                        "target_ip": "192.168.2.2/32",
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
        "target_ip": "192.168.2.2/32",
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
                "metadata": {"recovery_policy": {"version": 2}, "difficulty": "medium"},
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
        metadata={"recovery_policy": {"version": 2}, "difficulty": "easy"},
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
        metadata={"recovery_policy": {"version": 2}, "difficulty": "easy"},
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
        metadata={"recovery_policy": {"version": 2}, "difficulty": "easy"},
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
        metadata={"recovery_policy": {"version": 2}, "difficulty": "easy"},
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type="link_down",
            target_device="agg1",
            target_interface="Ethernet0",
            duration_seconds=102,
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
        metadata={"recovery_policy": {"version": 2}},
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
        metadata={"recovery_policy": {"version": 2}},
        scenario_id="legacy_schema",
        name="Legacy schema",
        topology_scale="xs",
        episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
    )

    with pytest.raises(ValueError, match="schema_version.*4.*Regenerate"):
        validate_scenario_topology(scenario, str(topology_dir))


def test_scenario_generator_rejects_nonstandard_traffic_profile(tmp_path):
    topology_dir = tmp_path / "topology"
    generate_topology("medium", str(topology_dir))
    topo = scenario_generator.load_topology("medium", str(topology_dir))
    with pytest.raises(ValueError, match="Only standard traffic"):
        scenario_generator.generate(
            {"fault_catalog_version": 2, "defaults": {"traffic_profile": "stress"}},
            topo,
            tmp_path / "scenarios",
            seed=1,
        )


@pytest.mark.parametrize("parameters", [{"target_ip": "new", "wrong_nexthop": "new-hop"}, {}])
def test_builtin_faults_read_only_parameters_not_descriptive_metadata(monkeypatch, parameters):
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
        parameters=parameters,
    )

    result = runner._inject_fault(episode)

    assert result["success"] is True
    assert captured == {
        "device": "leaf1",
        "target_ip": parameters.get("target_ip"),
        "wrong_nexthop": parameters.get("wrong_nexthop"),
    }


def test_mtu_injection_reads_the_canonical_episode_parameters(monkeypatch):
    runner = ScenarioExecutor(topology_dir="lab-topology", topology_metadata=_minimal_canonical_topology())
    captured = []
    monkeypatch.setattr(
        runner.injector, "inject_mtu_mismatch", lambda device, interface, mtu: captured.append(mtu) or {"success": True}
    )
    result = runner._inject_fault(
        EpisodeSpec(
            episode_id="mtu",
            fault_type="mtu_mismatch",
            target_device="leaf1",
            target_interface="Ethernet0",
            parameters={"mtu": 1450},
        )
    )
    assert result["success"]
    assert captured == [1450]


def test_scenario_executor_returns_result_for_session_persistence(monkeypatch):
    events = []
    runner = ScenarioExecutor(
        topology_dir="lab-topology",
        topology_metadata=_minimal_canonical_topology(),
        minimum_baseline_seconds=3,
        sleep_fn=lambda _seconds: None,
    )

    def setup_traffic(scale, profile):
        events.append("traffic_ready")
        return {"scale": scale, "profile": profile}

    from datetime import UTC, datetime, timedelta

    from netopsbench.platform.scenario.baseline import RuntimeScope

    baseline = {
        "start_time": (datetime.now(UTC) - timedelta(seconds=7)).isoformat(),
        "end_time": datetime.now(UTC).isoformat(),
        "duration_seconds": 7,
    }
    monkeypatch.setattr(runner, "_baseline_scope", lambda _: RuntimeScope({}, {}, {}))
    monkeypatch.setattr("netopsbench.platform.observability.routes.collect_routes", lambda *a, **k: {"nodes": {}})

    def capture_baseline():
        events.append("baseline_captured")
        return baseline

    def observe(windows, total_duration_seconds, *, baseline_window):
        is_validation = len([event for event in events if event.endswith("_observed")]) == 0
        events.append("validation_observed" if is_validation else "current_observed")
        assert baseline_window == baseline
        return {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:00:07Z",
            "duration_seconds": total_duration_seconds,
            "pingmesh_metrics": {
                "summary": {
                    "total_anomalies": 0,
                    "packet_loss_events": 0,
                    "path_unreachable_events": 0,
                    "latency_spikes": 0,
                    "mtu_or_fragmentation_events": 0,
                },
                "quality": {"current_paths_observed": 2, "local_df_mtu_drops": 0},
                "anomalies": [],
            },
            "_baseline_health": {
                "absolute_unreachable_paths": 0,
                "absolute_packet_loss_paths": 0,
                "absolute_network_mtu_paths": 0,
            },
            "anomalies_detected": False,
            "coverage_status": "complete",
            "data_source_status": "ok",
            "_coverage_audit": {"coverage_status": "complete"},
            "_baseline_coverage": {"status": "ok", "coverage_status": "complete"},
        }

    monkeypatch.setattr(runner, "_setup_traffic", setup_traffic)
    monkeypatch.setattr(runner, "_capture_baseline_window", capture_baseline)
    monkeypatch.setattr(
        runner,
        "_capture_observation_window",
        lambda duration, name: {
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:00:15Z",
            "duration_seconds": duration,
        },
    )
    monkeypatch.setattr(runner, "_merge_observation_windows", observe)
    monkeypatch.setattr(runner, "_stop_traffic", lambda: None)
    monkeypatch.setattr(runner, "_recover_fault", lambda: {"success": True})

    scenario = ScenarioSpec(
        metadata={"recovery_policy": {"version": 2}},
        scenario_id="no-persist",
        name="No Persist",
        description="test",
        topology_scale="xs",
        episode=EpisodeSpec(episode_id="diagnosis", fault_type="none", duration_seconds=7),
    )

    result = runner.run_scenario(scenario)

    assert result["success"] is True
    assert events == ["traffic_ready", "baseline_captured", "validation_observed", "current_observed"]
    assert result["episode"]["coverage_audit"] == {"coverage_status": "complete"}
    assert "_coverage_audit" not in result["episode"]["observations"]
    assert "result_file" not in result


def test_fault_observation_stabilization_is_inside_total_duration_budget():
    from netopsbench.platform.scenario.episode_runner import observe_episode

    calls = []

    class Executor:
        from netopsbench.platform.scenario.timing import PhaseTimings

        timings = PhaseTimings()
        observation_timeout_seconds = 60

        def _inject_fault(self, _episode):
            return {"success": True}

        def _capture_observation_window(self, duration, name):
            calls.append((name, duration))
            return {
                "name": name,
                "start_time": "2026-10-24T00:00:00Z",
                "end_time": "2026-10-24T00:01:54Z",
                "duration_seconds": duration,
            }

        def sleep(self, duration):
            calls.append(("stabilization", duration))

        def _merge_observation_windows(
            self,
            windows,
            total_duration_seconds,
            *,
            baseline_window,
        ):
            calls.append(("merge", total_duration_seconds, len(windows)))
            assert baseline_window["name"] == "baseline"
            return {
                "data_source_status": "ok",
                "coverage_status": "complete",
                "pingmesh_metrics": {"quality": {"current_paths_observed": 1}},
            }

    episode = EpisodeSpec(
        episode_id="duration-budget",
        fault_type="link_down",
        target_device="leaf1",
        target_interface="Ethernet0",
        duration_seconds=114,
        stabilization_time=5,
        metadata={"early_observation_seconds": 20},
    )

    observe_episode(
        Executor(),
        episode,
        baseline_window={
            "name": "baseline",
            "start_time": "baseline-start",
            "end_time": "baseline-end",
        },
    )

    assert calls == [
        ("full", 114),
        ("merge", 114, 2),
    ]

import pytest

from netopsbench.platform.incident.context import (
    build_canonical_observation,
    build_public_case_id,
    build_public_symptoms,
)
from netopsbench.sdk import (
    build_canonical_observation as sdk_build_canonical_observation,
)
from netopsbench.sdk import (
    build_public_case_id as sdk_build_public_case_id,
)
from netopsbench.sdk import (
    build_public_symptoms as sdk_build_public_symptoms,
)


def test_canonical_observation_builders_are_public_sdk_contracts():
    assert sdk_build_canonical_observation is build_canonical_observation
    assert sdk_build_public_case_id is build_public_case_id
    assert sdk_build_public_symptoms is build_public_symptoms


def test_canonical_observation_is_compact_non_semantic_and_stable():
    symptoms = {
        "episode": {"episode_id": "diagnosis"},
        "observations": {"pingmesh_metrics": {"summary": {"total_anomalies": 2}}},
        "pingmesh_query_window": {"start_time": "start", "end_time": "end"},
        "observation_type": "scenario_episode",
        "ground_truth": {"fault_type": "link_down"},
    }
    observation = build_canonical_observation(
        case_id="case-deadbeef1234",
        topology={
            "topology_type": "clos",
            "devices": {
                "spines": [{}, {}],
                "leafs": [{}, {}, {}, {}],
                "clients": [{}] * 8,
            },
            "links": [{}] * 14,
        },
        symptoms=symptoms,
    )

    assert observation == {
        "case_id": "case-deadbeef1234",
        "topology_summary": {
            "family": "clos",
            "spines": 2,
            "leafs": 4,
            "cores": 0,
            "aggs": 0,
            "edges": 0,
            "clients": 8,
            "links": 14,
        },
        "symptoms": {
            "episode": symptoms["episode"],
            "observations": symptoms["observations"],
            "pingmesh_query_window": symptoms["pingmesh_query_window"],
        },
    }
    serialized = str(observation)
    assert "task_id" not in serialized
    assert "split" not in serialized
    assert "ground_truth" not in serialized


def test_canonical_fat_tree_summary_preserves_real_tiers():
    observation = build_canonical_observation(
        case_id="case-fat-tree",
        topology={
            "topology_type": "fat-tree",
            "devices": {
                "spines": [{}] * 16,
                "leafs": [{}] * 32,
                "cores": [{}] * 16,
                "aggs": [{}] * 32,
                "edges": [{}] * 32,
                "clients": [{}] * 64,
            },
            "links": [{}] * 128,
        },
        symptoms={},
    )

    assert observation["topology_summary"] == {
        "family": "fat-tree",
        "spines": 0,
        "leafs": 0,
        "cores": 16,
        "aggs": 32,
        "edges": 32,
        "clients": 64,
        "links": 128,
    }


def test_build_public_symptoms_strips_fault_injection_labels():
    episode_result = {
        "episode": {
            "episode_id": "ep002_fault",
            "fault_type": "link_down",
            "target_device": "leaf1",
            "target_interface": "Ethernet8",
            "duration_seconds": 30,
            "stabilization_time": 5,
        },
        "observations": {"pingmesh_metrics": {"summary": {"total_anomalies": 3}}},
    }

    payload = build_public_symptoms(
        episode_result=episode_result,
        pingmesh_query_window={"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z"},
    )

    episode = payload["episode"]
    assert "episode_id" not in episode
    assert episode["duration_seconds"] == 30
    assert episode["stabilization_time"] == 5
    assert "fault_type" not in episode
    assert "target_device" not in episode
    assert "target_interface" not in episode


@pytest.mark.parametrize("total_anomalies", [0, 3])
def test_initial_context_only_exposes_pingmesh_and_window_metadata(total_anomalies):
    from copy import deepcopy

    public = {
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:00:30Z",
        "duration_seconds": 30,
        "pingmesh_metrics": {"summary": {"total_anomalies": total_anomalies}},
        "anomalies_detected": bool(total_anomalies),
        "coverage_status": "complete",
        "data_source_status": "ok",
        "observation_windows": [{"name": "steady", "duration_seconds": 20}],
    }
    observations = {
        **public,
        "route_observations": {"changes": [{"device": "core4", "route_type": "blackhole"}]},
        "bgp_observations": {"device": "agg16", "state": "Idle"},
        "device_status": {"spine4": {"running": False}},
        "interface_observations": {"leaf2": {"Ethernet0": "down"}},
        "acl_observations": {"device": "edge1", "hits": 10},
        "future_device_summary": {"root": "answer"},
        "_probe_evidence": [{"root": "answer"}],
    }
    original = deepcopy(observations)
    window = {"start_time": public["start_time"], "end_time": public["end_time"]}
    symptoms = build_public_symptoms(episode_result={"observations": observations}, pingmesh_query_window=window)
    assert symptoms["observations"] == public
    assert symptoms["pingmesh_query_window"] == window

    # The canonical helper is also public: callers can bypass build_public_symptoms.
    canonical = build_canonical_observation(
        case_id="case-anonymous",
        topology={},
        symptoms={"observations": observations, "pingmesh_query_window": window},
    )
    assert canonical["symptoms"]["observations"] == public
    assert observations == original
    symptoms["observations"]["pingmesh_metrics"]["summary"]["total_anomalies"] = 99
    assert observations == original


@pytest.mark.parametrize("fault_type", ["none", "blackhole_route", "ecmp_misconfig"])
def test_incident_keeps_route_validation_private_and_supports_explicit_route_queries(monkeypatch, fault_type):
    from types import SimpleNamespace

    from netopsbench.models.scenario import EpisodeSpec
    from netopsbench.platform.incident.contracts import ToolAction
    from netopsbench.platform.scenario import effects, incident_backend
    from netopsbench.platform.scenario.timing import PhaseTimings
    from netopsbench.platform.toolkit._core.common import ToolResult

    baseline = {"nodes": {"core4": {"fib": [{"dst": "192.168.49.0/30", "gateway": "10.0.0.1"}]}}}
    current = {"nodes": {"core4": {"fib": [{"dst": "192.168.49.2/32", "type": "blackhole"}]}}}
    snapshots = iter([baseline, current])
    monkeypatch.setattr("netopsbench.platform.observability.routes.collect_routes", lambda *a, **kw: next(snapshots))
    manifest = SimpleNamespace(
        require_pingmesh_plan=lambda: SimpleNamespace(settings=SimpleNamespace(complete_window_seconds=30))
    )
    episode = EpisodeSpec(episode_id="diagnosis", fault_type=fault_type, target_device="core4")
    observations = {"pingmesh_metrics": {"summary": {"total_anomalies": 0}}}
    result = {"episode": episode.model_dump(), "observations": observations}
    monkeypatch.setattr(incident_backend, "observe_episode", lambda *a, **kw: result)
    audit = {"valid": True, "errors": []}

    def validate(executor, ep, episode_result, before, after):
        assert before is baseline and after is current
        assert episode_result["route_evidence"] == {"baseline": baseline, "fault": current}
        return audit

    monkeypatch.setattr(effects, "validate_effect", validate)
    monkeypatch.setattr(effects, "scoped_integrity_errors", lambda *a: [])

    class Toolkit:
        def __init__(self, **kwargs):
            pass

        def get_topology(self):
            return ToolResult(True, {"devices": {"cores": [{"name": "core4"}]}})

        def set_pingmesh_time_window(self, start, end):
            pass

        def get_route_table(self, device, *, view):
            assert view == "kernel"
            return ToolResult(True, current["nodes"][device]["fib"])

    monkeypatch.setattr(incident_backend, "AgentToolkit", Toolkit)
    from netopsbench.models.recovery import RecoveryPolicy
    from netopsbench.platform.scenario.baseline import BaselineHandoff

    executor = SimpleNamespace(
        recovery_policy=RecoveryPolicy(mode="full_window"),
        topology_dir="unused",
        _traffic_config=None,
        _cancel_event=None,
        _baseline_handoff=BaselineHandoff(),
        _baseline_scope=lambda scenario: None,
        topology_metadata={},
        injector=SimpleNamespace(active_faults=[]),
        traffic_controller=SimpleNamespace(active_flows={"flow": 1}, verify_active_flows=lambda: True),
        runtime_worker=None,
        minimum_baseline_seconds=30,
        observation_timeout_seconds=60,
        _capture_baseline_window=lambda: {"duration_seconds": 30},
        _merge_observation_windows=lambda *a, **kw: {
            "start_time": "2026-07-11T00:00:00Z",
            "end_time": "2026-07-11T00:00:30Z",
            "duration_seconds": 30,
        },
        current_manifest=lambda: manifest,
        timings=PhaseTimings(),
        scale_registry=SimpleNamespace(get=lambda scale: SimpleNamespace(health_timeout_seconds=60)),
        influxdb_bucket="bucket",
        topology_id="topology",
    )
    monkeypatch.setattr(incident_backend, "baseline_gate_errors", lambda value: [])
    backend = incident_backend.ExecutorIncidentBackend(executor)
    canonical = backend.prepare(
        SimpleNamespace(id="private-case", scale="medium", episode=episode, metadata={"fault_catalog_version": 2})
    )
    assert backend.symptoms["observations"] == observations
    assert canonical["symptoms"]["observations"] == observations
    assert result["route_evidence"] == {"baseline": baseline, "fault": current}
    assert result["effect_audit"] is audit
    assert "route_observations" not in result["observations"]
    queried = backend.call_tool(ToolAction(name="get_route_table", arguments={"device": "core4", "view": "kernel"}))
    assert queried["success"]
    assert queried["data"] == current["nodes"]["core4"]["fib"]


def test_build_public_case_id_is_non_semantic_and_stable():
    episode_result = {"episode": {"episode_id": "ep002_fault"}}

    case_a = build_public_case_id(scenario_id="generated_link_down_xs_001", episode_result=episode_result)
    case_b = build_public_case_id(scenario_id="generated_link_down_xs_001", episode_result=episode_result)

    assert case_a == case_b
    assert case_a.startswith("case-")
    assert "link_down" not in case_a


def test_build_public_symptoms_preserves_complete_anomalies_and_aggregates():
    anomalies = [
        {
            "type": "packet_loss",
            "src_ip": f"192.0.2.{index}",
            "dst_ip": "198.51.100.1",
            "src_leaf": f"leaf{index % 4}",
            "dst_leaf": "leaf9",
            "severity": "high",
            "value": float(index),
        }
        for index in range(150)
    ]
    episode_result = {
        "episode": {"episode_id": "ep002", "duration_seconds": 72, "stabilization_time": 5},
        "observations": {
            "pingmesh_metrics": {
                "summary": {"total_anomalies": 150},
                "anomalies": anomalies,
                "aggregated_anomalies": {"by_src_leaf": {}},
            }
        },
    }

    payload = build_public_symptoms(episode_result=episode_result, pingmesh_query_window={})
    metrics = payload["observations"]["pingmesh_metrics"]

    assert len(metrics["anomalies"]) == 150
    assert "returned_anomalies" not in metrics
    assert "truncated" not in metrics
    assert metrics["summary"]["total_anomalies"] == 150
    assert "aggregated_anomalies" in metrics
    assert len(episode_result["observations"]["pingmesh_metrics"]["anomalies"]) == 150
    assert "aggregated_anomalies" in episode_result["observations"]["pingmesh_metrics"]


def test_canonical_observation_compacts_without_changing_public_symptoms():
    episode_result = {
        "episode": {"episode_id": "ep", "duration_seconds": 30},
        "observations": {
            "pingmesh_metrics": {
                "anomalies": [{"type": "packet_loss", "severity": "high", "value": index} for index in range(20)],
                "aggregated_anomalies": {"by_src_leaf": {}},
            }
        },
    }
    symptoms = build_public_symptoms(episode_result=episode_result, pingmesh_query_window={})
    canonical = build_canonical_observation(
        case_id="case-deadbeef1234",
        topology={"devices": {}, "links": []},
        symptoms=symptoms,
    )

    compacted = canonical["symptoms"]["observations"]["pingmesh_metrics"]
    assert len(compacted["anomalies"]) == 12
    assert compacted["truncated"] is True
    assert "aggregated_anomalies" not in compacted
    assert len(symptoms["observations"]["pingmesh_metrics"]["anomalies"]) == 20


def test_agent_success_false_survives_runtime_callback(tmp_path):
    from types import SimpleNamespace

    from netopsbench.platform.session.diagnosis import build_runtime_diagnosis_callback
    from netopsbench.sdk.agents import DiagnosisResult

    async def diagnose(context):
        return DiagnosisResult("provider", "inconclusive", success=False, reasoning="provider unavailable")

    callback = build_runtime_diagnosis_callback(
        SimpleNamespace(diagnose=diagnose, name="provider"), str(tmp_path), "case"
    )
    result = callback(
        {},
        diagnostic_session=SimpleNamespace(config=SimpleNamespace(max_agent_seconds=600)),
        diagnostic_payload={"case_id": "anonymous", "topology": {}, "symptoms": {}, "canonical_observation": {}},
    )
    assert result["success"] is False
    assert result["error"] == "provider unavailable"


def test_agent_cancellation_waits_for_resource_cleanup():
    import asyncio
    import threading
    from types import SimpleNamespace

    import pytest

    from netopsbench.platform.session.diagnosis import run_agent_diagnose

    cleaned = []
    cancelled = threading.Event()

    async def diagnose(context):
        try:
            cancelled.set()
            await asyncio.sleep(100)
        finally:
            cleaned.append(True)

    with pytest.raises(RuntimeError, match="cancelled"):
        run_agent_diagnose(SimpleNamespace(diagnose=diagnose), None, cancelled)
    assert cleaned == [True]


def test_scenario_cancellation_prevents_injection_and_allows_recovery_sleep(monkeypatch):
    import threading

    import pytest

    from netopsbench.platform.scenario.executor import ScenarioExecutor
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import config_for_scale

    slept = []
    runner = ScenarioExecutor(
        topology_metadata=build_clos_plan(config_for_scale("xs")).manifest,
        sleep_fn=lambda seconds: slept.append(seconds),
    )
    runner._cancel_event = threading.Event()
    runner._cancel_event.set()
    monkeypatch.setattr(
        "netopsbench.platform.scenario.executor._inject_fault_impl",
        lambda *a: pytest.fail("cancelled scenario must not inject"),
    )
    with pytest.raises(RuntimeError, match="cancelled"):
        runner._inject_fault(None)
    with pytest.raises(RuntimeError, match="cancelled"):
        runner.sleep(1)
    runner._recovering = True
    runner.sleep(1)
    assert slept == [1]

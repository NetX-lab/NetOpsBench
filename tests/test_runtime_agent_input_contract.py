from netopsbench.platform.session.context import (
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
    assert episode["episode_id"] == "ep002_fault"
    assert episode["duration_seconds"] == 30
    assert episode["stabilization_time"] == 5
    assert "fault_type" not in episode
    assert "target_device" not in episode
    assert "target_interface" not in episode


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
                "anomalies": [
                    {"type": "packet_loss", "severity": "high", "value": index}
                    for index in range(20)
                ],
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

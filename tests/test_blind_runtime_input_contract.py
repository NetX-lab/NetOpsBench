"""Regression tests for evaluator/scenario data isolation."""

from __future__ import annotations

import pytest

from netopsbench.platform.incident.context import (
    assert_model_visible_payload,
    build_canonical_observation,
    build_public_symptoms,
)
from netopsbench.platform.incident.engine import SessionToolGateway
from netopsbench.platform.session.atif import build_atif_payload
from netopsbench.sdk.reports import BenchmarkReport


def test_public_symptoms_strip_episode_identity_and_fault_fields():
    symptoms = build_public_symptoms(
        episode_result={
            "episode": {
                "episode_id": "diagnosis",
                "fault_type": "link_down",
                "target_device": "leaf1",
                "target_interface": "Ethernet0",
                "duration_seconds": 30,
                "stabilization_time": 5,
            },
            "observations": {"pingmesh_metrics": {"summary": {"total_anomalies": 1}}},
        },
        pingmesh_query_window={"start_time": "start", "end_time": "end"},
    )
    assert symptoms["episode"] == {"duration_seconds": 30, "stabilization_time": 5}
    assert_model_visible_payload(symptoms)


def test_canonical_observation_has_no_case_id_by_default():
    observation = build_canonical_observation(
        case_id="case-secret",
        topology={"topology_type": "clos", "devices": {}, "links": []},
        symptoms={"episode": {}, "observations": {}, "pingmesh_query_window": {}},
    )
    assert "case_id" not in observation
    assert "scenario_id" not in str(observation)
    assert_model_visible_payload(observation)


def test_model_visible_payload_rejects_evaluator_fields():
    with pytest.raises(ValueError, match="forbidden field"):
        assert_model_visible_payload({"observations": {"ground_truth": {"fault_type": "packet_loss"}}})


def test_legacy_case_id_is_explicit_and_never_used_by_blind_builder():
    legacy = build_canonical_observation(
        case_id="case-legacy",
        include_case_id=True,
        topology={"topology_type": "clos", "devices": {}, "links": []},
        symptoms={"episode": {}, "observations": {}, "pingmesh_query_window": {}},
    )
    assert legacy["case_id"] == "case-legacy"
    blind = build_canonical_observation(
        case_id="case-legacy",
        include_case_id=False,
        topology={"topology_type": "clos", "devices": {}, "links": []},
        symptoms={"episode": {}, "observations": {}, "pingmesh_query_window": {}},
    )
    assert "case_id" not in blind


def test_console_report_redaction_hides_ground_truth_and_match(capsys):
    report = BenchmarkReport(
        id="run-heldout",
        summary={"total_cases": 1, "overall_accuracy": 1.0, "avg_time_seconds": 1.0},
        detailed_results=[
            {
                "scenario_id": "heldout-secret",
                "score": 1.0,
                "correct_verdict": True,
                "details": {
                    "ground_truth": {"fault_type": "packet_loss"},
                    "agent_output": {"fault_type": "packet_loss", "location": {}},
                },
            }
        ],
    )
    report.pretty_print(include_evaluator_details=False)
    output = capsys.readouterr().out
    assert "ground truth" not in output.lower()
    assert "match" not in output.lower()
    assert "heldout-secret" not in output


def test_agent_atif_extra_has_no_case_or_scenario_identity():
    atif = build_atif_payload(
        {
            "trace_id": "corr-random",
            "run_id": "run-random",
            "case_id": "case-secret",
            "scenario_id": "scenario-secret.yaml",
            "episode_id": "diagnosis",
            "runtime_id": "runtime",
            "worker": "worker-1",
            "steps": [],
            "final_diagnosis": {"verdict": "network_healthy"},
            "metrics": {},
        }
    )
    serialized = str(atif)
    assert "case-secret" not in serialized
    assert "scenario-secret.yaml" not in serialized
    assert "episode_id" not in atif["extra"]


def test_session_tool_gateway_redacts_injection_markers_from_tool_data():
    class FakeSession:
        def call_tool(self, _action):
            return type(
                "Transition",
                (),
                {
                    "failure": None,
                    "observation": {
                        "result": {
                            "success": True,
                            "data": {"message": "netopsbench injected deny; injected at 12:00Z"},
                        }
                    },
                },
            )()

    result = SessionToolGateway(FakeSession()).get_device_logs(device="leaf1")
    text = str(result.data).lower()
    assert "netopsbench injected" not in text
    assert "injected at" not in text
    assert "deny" in text

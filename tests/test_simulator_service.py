"""Generic HTTP simulator boundary tests."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.simulator.service import SimulatorService, create_app, scenario_case_id


class Result:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, mode="python"):
        return dict(self.payload)


class FakeEnvironment:
    def __init__(self, case_id):
        self.case_id = case_id
        self.closed = False

    def reset(self):
        return Result(
            {
                "valid": True,
                "case_valid": True,
                "state": "active",
                "case_id": self.case_id,
                "observation": {},
                "tools": [],
                "cleanup_status": "not_started",
            }
        )

    def step(self, action):
        terminal = action.type == "submit_diagnosis"
        return Result(
            {
                "valid": True,
                "case_valid": True,
                "state": "terminal" if terminal else "active",
                "reward": 1.0 if terminal else None,
                "observation": {} if terminal else {"tool": action.name},
                "reward_components": {"outcome": 1.0} if terminal else {},
                "metrics": {},
                "termination_reason": "submitted" if terminal else None,
                "cleanup_status": "succeeded" if terminal else "not_started",
            }
        )

    def terminate_protocol(self, message):
        return Result(
            {
                "valid": True,
                "case_valid": True,
                "state": "terminal",
                "reward": 0.0,
                "reward_components": {"outcome": 0.0},
                "metrics": {},
                "termination_reason": "protocol_error",
                "failure": {
                    "domain": "protocol",
                    "phase": "diagnosis",
                    "message": message,
                    "error_type": None,
                },
                "cleanup_status": "succeeded",
                "error": message,
            }
        )

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self):
        self.closed = False

    def create(self, *, scenario, config):
        return FakeEnvironment(scenario_case_id(scenario))

    def close(self):
        self.closed = True


def _scenario():
    return ScenarioSpec(
        scenario_id="scenario-1",
        name="scenario-1",
        episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
    )


def test_http_service_uses_opaque_case_ids_and_generic_actions(tmp_path):
    manager = FakeManager()
    scenario = _scenario()
    case_id = scenario_case_id(scenario)
    event_log = tmp_path / "events.jsonl"
    service = SimulatorService(manager, [scenario], event_log=event_log)

    with TestClient(create_app(service)) as client:
        assert client.get("/v1/cases").json() == {"case_ids": [case_id]}
        created = client.post("/v1/environments", json={"case_id": case_id}).json()
        environment_id = created["environment_id"]
        tool = client.post(
            f"/v1/environments/{environment_id}/actions",
            json={"action": {"type": "tool", "name": "get_topology", "arguments": {}}},
        )
        terminal = client.post(
            f"/v1/environments/{environment_id}/actions",
            json={
                "action": {
                    "type": "submit_diagnosis",
                    "diagnosis": {"verdict": "network_healthy"},
                }
            },
        )
        deleted = client.delete(f"/v1/environments/{environment_id}")

    assert created["reset"]["case_id"] == case_id
    assert tool.status_code == 200 and tool.json()["state"] == "active"
    assert terminal.status_code == 200 and terminal.json()["reward"] == 1.0
    assert deleted.json() == {"deleted": True}
    assert manager.closed is True
    events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["create", "step", "step", "delete"]
    assert all("task_id" not in event and "split" not in event for event in events)


def test_http_service_rejects_unknown_case_and_group_fields():
    service = SimulatorService(FakeManager(), [_scenario()])
    with TestClient(create_app(service)) as client:
        missing = client.post("/v1/environments", json={"case_id": "case-missing"})
        grouped = client.post(
            "/v1/environments",
            json={"case_id": scenario_case_id(_scenario()), "group_id": "rl-group"},
        )
    assert missing.status_code == 404
    assert grouped.status_code == 422


def test_http_service_maps_illegal_diagnosis_to_protocol_outcome_zero():
    service = SimulatorService(FakeManager(), [_scenario()])
    with TestClient(create_app(service)) as client:
        created = client.post(
            "/v1/environments",
            json={"case_id": scenario_case_id(_scenario())},
        ).json()
        result = client.post(
            f"/v1/environments/{created['environment_id']}/actions",
            json={
                "action": {
                    "type": "submit_diagnosis",
                    "diagnosis": {"verdict": "not-a-verdict"},
                }
            },
        )

    assert result.status_code == 200
    assert result.json()["case_valid"] is True
    assert result.json()["reward"] == 0.0
    assert result.json()["termination_reason"] == "protocol_error"
    assert result.json()["failure"]["domain"] == "protocol"

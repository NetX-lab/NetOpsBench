"""The deterministic audit records real execution rather than predicted diagnoses."""

import subprocess
from types import SimpleNamespace

import pytest

from scripts import validate_runtime_refactor as audit


def test_scripted_diagnosis_uses_same_tools_without_reading_truth():
    calls = []

    class Session:
        def call_tool(self, action):
            calls.append(action)
            return SimpleNamespace(model_dump=lambda **_: {"observation": {"success": True}})

    result = audit.deterministic_diagnosis(
        None,
        diagnostic_session=Session(),
        diagnostic_payload={"topology": {"devices": {"leafs": [{"name": "leaf1"}], "clients": [{"name": "client1"}]}}},
    )
    assert result["verdict"] == "inconclusive"
    assert [call.name for call in calls] == [
        "get_topology",
        "get_pingmesh_summary",
        "get_device_interfaces",
        "get_bgp_neighbors",
        "get_route_table",
        "get_device_acl",
    ]
    assert len(result["metadata"]["scripted_observations"]) == 6


def test_counter_restores_process_api_even_after_failure():
    original = subprocess.Popen
    with pytest.raises(RuntimeError), audit.CommandCounts() as counts:
        subprocess.run(["true"], check=True)
        assert counts.snapshot() == {"subprocess": 1, "docker": 0, "influx_queries": 0}
        raise RuntimeError("failed audit")
    assert subprocess.Popen is original


def test_selection_rejects_unsupported_fault_instead_of_silent_skip(monkeypatch):
    healthy = SimpleNamespace(id="healthy", episode=SimpleNamespace(is_healthy=True, fault_type="none"))
    monkeypatch.setattr(audit, "parse_scenario_file", lambda _: healthy)
    with pytest.raises(ValueError, match="No generated case"):
        audit.select_cases(["case"], ["packet_loss"])

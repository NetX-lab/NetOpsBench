"""Focused tests for per-case diagnostic context handling."""

from __future__ import annotations

import json

import pytest

from netopsbench.agents.base import DiagnosisResult
from netopsbench.platform.session.diagnosis import build_runtime_diagnosis_callback


class _HealthyAgent:
    name = "healthy-agent"

    def diagnose(self, context):
        return DiagnosisResult(
            agent_name=self.name,
            verdict="network_healthy",
            findings={},
            confidence=1.0,
        )


def _payload() -> dict:
    return {
        "case_id": "case-1",
        "topology": {},
        "symptoms": {},
        "canonical_observation": {"case_id": "case-1"},
    }


def test_pingmesh_context_is_atomically_reset_when_case_has_no_window(tmp_path):
    callback = build_runtime_diagnosis_callback(_HealthyAgent(), str(tmp_path), "scenario-1")
    context_file = tmp_path / ".netopsbench" / "pingmesh_context.json"

    callback(
        {"observations": {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z"}},
        diagnostic_session=object(),
        diagnostic_payload=_payload(),
    )
    assert json.loads(context_file.read_text())["start_time"] == "2026-01-01T00:00:00Z"

    callback({}, diagnostic_session=object(), diagnostic_payload=_payload())
    assert json.loads(context_file.read_text()) == {}


def test_pingmesh_context_write_failure_stops_diagnosis(tmp_path, monkeypatch):
    callback = build_runtime_diagnosis_callback(_HealthyAgent(), str(tmp_path), "scenario-1")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("netopsbench.platform.session.diagnosis.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        callback({}, diagnostic_session=object(), diagnostic_payload=_payload())

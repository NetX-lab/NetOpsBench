"""
Real-runtime XS smoke regression.

Purpose:
- Run one minimal XS scenario through SDK runtime path.
- Assert traffic starts, fault injects, and anomaly is observable.

This test is opt-in and expensive.
Enable with:
  NETOPSBENCH_RUN_XS_REAL_SMOKE=1 pytest -m real tests/test_runtime_xs_smoke_real.py -v
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from netopsbench.sdk import NetOpsBench
from netopsbench.sdk.agents import DiagnosisResult


def _require_opt_in() -> None:
    if os.environ.get("NETOPSBENCH_RUN_XS_REAL_SMOKE", "").strip() != "1":
        pytest.skip("set NETOPSBENCH_RUN_XS_REAL_SMOKE=1 to run real XS smoke regression")


def _scenario_path(repo_root: Path) -> Path:
    path = repo_root / "scenarios" / "generated" / "xs" / "generated_link_down_xs_001.yaml"
    if not path.exists():
        pytest.skip(f"scenario not found: {path}")
    return path


class _RuntimeSmokeStubAgent:
    def diagnose(self, context):
        ep = (getattr(context, "symptoms", {}) or {}).get("episode", {}) or {}
        fault_type = ep.get("fault_type")
        return DiagnosisResult(
            agent_name="runtime-smoke-stub-agent",
            verdict="fault_detected" if fault_type and fault_type != "none" else "network_healthy",
            findings={
                "fault_type": fault_type,
                "location": {
                    "device": ep.get("target_device"),
                    "interface": ep.get("target_interface"),
                },
            },
            confidence=0.8,
            reasoning="xs-real-smoke",
            metadata={"kind": "xs-real-smoke"},
        )


@pytest.mark.real
def test_runtime_xs_link_down_smoke_observable():
    _require_opt_in()
    repo = Path(__file__).resolve().parents[1]
    scenario = _scenario_path(repo)
    bench = NetOpsBench(workspace=str(repo))
    runtime_name = f"pytest-xs-smoke-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    runtime = None
    try:
        runtime = bench.runtimes.provision(scale="xs", workers=1, name=runtime_name)
        run = bench.sessions.run_on_runtime_scenario(
            scenario=scenario,
            runtime=runtime,
            agent=_RuntimeSmokeStubAgent(),
            artifacts_dir=repo / "scenario_results" / "pytest_xs_smoke",
        )
        report = run.wait()
        report_path = Path(report.artifact_paths["report"])
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        raw_path = Path(payload["scenario_summaries"][0]["raw_result_path"])
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        fault_ep = next((e for e in raw.get("episodes", []) if e.get("episode_id") == "ep002_fault"), {})

        traffic_flows = ((raw.get("traffic_config") or {}).get("stats") or {}).get("total_flows", 0)
        injection = fault_ep.get("injection", {}) or {}
        observations = fault_ep.get("observations", {}) or {}
        summary = (observations.get("pingmesh_metrics") or {}).get("summary") or {}

        assert traffic_flows > 0, "traffic did not start"
        assert injection.get("success") is True, f"fault injection failed: {injection}"
        assert observations.get("anomalies_detected") is True, f"no observable anomaly: {summary}"
        assert int(summary.get("total_anomalies", 0) or 0) > 0, f"empty anomaly summary: {summary}"
    finally:
        if runtime is not None:
            runtime.teardown()

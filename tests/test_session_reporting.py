from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from netopsbench.platform.session.reporting import create_run_report


def test_create_run_report_preserves_topology_scale_and_agent_name(tmp_path: Path):
    runtime = SimpleNamespace(id="run-0001-runtime", scale="small")
    agent = SimpleNamespace(name="agent-x")
    scenario = SimpleNamespace(id="generated_link_down_small_001", scale="small")

    report = create_run_report(
        run_id="run-0001",
        mode="suite",
        started_at=datetime(2026, 4, 29, 1, 0, tzinfo=UTC),
        completed_at=datetime(2026, 4, 29, 2, 0, tzinfo=UTC),
        runtime=runtime,
        runtime_owner="sdk",
        teardown="always",
        scenarios=[scenario],
        agent=agent,
        worker_summaries=[{"success": True}],
        scenario_summaries=[],
        aggregate_report={
            "agent_name": "agent-x",
            "topology_scale": "small",
            "summary": {"total_cases": 1, "overall_accuracy": 1.0},
            "detailed_results": [],
        },
        artifact_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        report_path=tmp_path / "report.json",
        metadata_path=tmp_path / "metadata.json",
    )

    assert report["agent_name"] == "agent-x"
    assert report["topology_scale"] == "small"
    assert report["summary"]["agent_name"] == "agent-x"
    assert report["summary"]["topology_scale"] == "small"
    assert report["raw"]["topology_scale"] == "small"

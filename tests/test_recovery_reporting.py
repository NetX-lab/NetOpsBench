import csv
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from netopsbench.models.profiles import default_scale_registry
from netopsbench.platform.session.reporting import create_run_report


def test_nested_recovery_timings_preserve_phase_csv_and_export_steps(tmp_path):
    step = {
        "status": "completed",
        "actual_seconds": 10,
        "calls": 1,
        "failed_calls": 0,
        "ingestion": {"baseline": {"query_requests": 21}, "current": {"query_requests": 21}},
    }
    phase = {
        "status": "completed",
        "actual_seconds": 80,
        "timeout_seconds": 600,
        "steps": {"window_query": step},
    }
    runtime = SimpleNamespace(id="runtime", scale="medium", scale_registry=default_scale_registry())
    now = datetime.now(UTC)
    report = create_run_report(
        run_id="test",
        mode="effects",
        started_at=now,
        completed_at=now,
        runtime=runtime,
        runtime_owner="external",
        teardown="never",
        scenarios=[],
        agent=None,
        worker_summaries=[],
        scenario_summaries=[{"scenario_id": "case", "scale": "medium", "phase_timings": {"recovery": phase}}],
        aggregate_report={},
        artifact_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        report_path=tmp_path / "report.json",
        metadata_path=tmp_path / "metadata.json",
    )
    with Path(report["artifact_paths"]["phase_timings"]).open() as handle:
        phases = list(csv.DictReader(handle))
    assert len(phases) == 1
    assert phases[0]["phase"] == "recovery"
    assert phases[0]["actual_seconds"] == "80"
    assert "steps" not in phases[0]
    with Path(report["artifact_paths"]["recovery_steps"]).open() as handle:
        steps = list(csv.DictReader(handle))
    assert len(steps) == 1
    assert steps[0]["actual_seconds"] == "10"
    assert steps[0]["baseline_query_requests"] == steps[0]["current_query_requests"] == "21"

"""Tests for :meth:`RunHandle.wait` ``raise_on_failure`` semantics."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from netopsbench.sdk.exceptions import RunFailedError
from netopsbench.sdk.reports import BenchmarkReport, RunHandle, _report_succeeded


def _write_report(tmp_path: Path, payload: dict) -> Path:
    report_path = tmp_path / "report.json"
    report = BenchmarkReport(
        id="run-1",
        summary=dict(payload.get("summary") or {}),
        scenario_summaries=list(payload.get("scenario_summaries") or []),
        raw=dict(payload.get("raw") or {}),
    )
    report.save(report_path)
    # Also assert it round-trips before tests use it.
    assert json.loads(report_path.read_text())
    return report_path


def _make_handle(report_path: Path) -> RunHandle:
    return RunHandle(
        id="run-1",
        mode="single",
        status="running",
        started_at=datetime(2026, 4, 22, 0, 0, 0),
        completed_at=None,
        artifact_dir=str(report_path.parent),
        scenario_ids=["s1"],
        runtime_id="rt-1",
        report_path=report_path,
    )


def test_wait_returns_report_when_no_raise(tmp_path):
    report_path = _write_report(tmp_path, {"summary": {"status": "completed"}})
    handle = _make_handle(report_path)
    report = handle.wait()
    assert isinstance(report, BenchmarkReport)


def test_wait_raises_run_failed_when_status_failed(tmp_path):
    report_path = _write_report(
        tmp_path,
        {"summary": {"status": "completed"}, "raw": {"status": "failed"}},
    )
    handle = _make_handle(report_path)
    with pytest.raises(RunFailedError) as excinfo:
        handle.wait(raise_on_failure=True)
    assert excinfo.value.report is not None


def test_wait_raises_when_scenario_summary_failed(tmp_path):
    report_path = _write_report(
        tmp_path,
        {
            "summary": {"status": "completed"},
            "scenario_summaries": [
                {"scenario_id": "s1", "status": "completed"},
                {"scenario_id": "s2", "status": "failed"},
            ],
        },
    )
    handle = _make_handle(report_path)
    with pytest.raises(RunFailedError):
        handle.wait(raise_on_failure=True)


def test_wait_does_not_raise_when_all_completed(tmp_path):
    report_path = _write_report(
        tmp_path,
        {
            "summary": {"status": "completed"},
            "scenario_summaries": [{"scenario_id": "s1", "status": "completed"}],
        },
    )
    handle = _make_handle(report_path)
    report = handle.wait(raise_on_failure=True)
    assert _report_succeeded(report)


def test_wait_raises_when_summary_success_false(tmp_path):
    report_path = _write_report(
        tmp_path,
        {"summary": {"status": "completed", "success": False}},
    )
    handle = _make_handle(report_path)
    with pytest.raises(RunFailedError):
        handle.wait(raise_on_failure=True)


def test_wait_missing_report_raises_file_not_found(tmp_path):
    handle = _make_handle(tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        handle.wait(raise_on_failure=True)

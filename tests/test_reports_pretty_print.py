"""Tests for BenchmarkReport.pretty_print human-readable rendering."""

from __future__ import annotations

import json

from netopsbench.sdk.reports import BenchmarkReport


def _build_suite_report() -> BenchmarkReport:
    return BenchmarkReport(
        id="run-test-0001",
        summary={
            "mode": "suite",
            "status": "completed",
            "runtime_id": "run-test-runtime",
            "started_at": "2026-04-23T10:00:00+00:00",
            "completed_at": "2026-04-23T10:13:00+00:00",
            "total_cases": 2,
            "correct_verdict": 1,
            "correct_device": 1,
            "correct_fault_type": 0,
            "correct_interface": 1,
            "interface_applicable_cases": 2,
            "detection_accuracy": 0.5,
            "device_accuracy": 0.5,
            "fault_type_accuracy": 0.0,
            "interface_localization_rate": 0.5,
            "average_score": 0.5,
            "avg_time_seconds": 87.0,
            "avg_tool_calls": 14.0,
            "total_input_tokens": 456,
            "avg_input_tokens_per_case": 228.0,
            "total_output_tokens": 123,
            "avg_output_tokens_per_case": 61.5,
        },
        detailed_results=[
            {
                "scenario_id": "generated_link_down_xs_001",
                "score": 1.0,
                "correct_verdict": True,
                "correct_device": True,
                "correct_fault_type": True,
                "correct_interface": True,
                "details": {
                    "scenario_id": "generated_link_down_xs_001",
                    "interface_applicable": True,
                    "inconclusive": False,
                    "time_taken": 60.5,
                    "tool_calls_count": 12,
                    "ground_truth": {
                        "fault_type": "link_down",
                        "location": {"device": "leaf1", "interface": "Ethernet8"},
                    },
                    "agent_output": {
                        "fault_type": "link_down",
                        "location": {"device": "leaf1", "interface": "Ethernet8"},
                    },
                },
            },
            {
                "scenario_id": "generated_packet_loss_xs_001",
                "score": 0.0,
                "correct_verdict": False,
                "correct_device": False,
                "correct_fault_type": False,
                "correct_interface": False,
                "details": {
                    "scenario_id": "generated_packet_loss_xs_001",
                    "interface_applicable": True,
                    "inconclusive": True,
                    "time_taken": 113.5,
                    "tool_calls_count": 18,
                    "ground_truth": {
                        "fault_type": "packet_loss",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                    },
                    "agent_output": {
                        "fault_type": None,
                        "location": {"device": None, "interface": None},
                    },
                },
            },
        ],
        artifact_paths={
            "report": "/tmp/run-test-0001/report.json",
            "raw_dir": "/tmp/run-test-0001/raw",
        },
    )


def test_pretty_print_renders_sections(capsys):
    report = _build_suite_report()

    assert report.pretty_print() is None
    out = capsys.readouterr().out

    # Header contains the run id and suite metadata.
    assert "run-test-0001" in out
    assert "Benchmark Report" in out
    assert "suite" in out
    assert "completed" in out

    # Per-case table header + at least one short scenario id rendered.
    assert "Per-case Breakdown" in out
    assert "Scenario" in out
    assert "GT type" in out
    assert "Pred dev:if" in out
    assert "link_down" in out
    assert "packet_loss" in out
    # Inconclusive cases are surfaced.
    assert "inconclusive" in out
    # Verdict markers (Y / N) appear.
    assert " Y " in out or out.endswith(" Y\n") or "\nY" in out or " Y" in out
    assert " N " in out or " N" in out
    # Legend present.
    assert "Legend" in out

    # Summary block.
    assert "Summary" in out
    assert "Total cases" in out
    assert "Average score" in out
    assert "Total input tokens" in out
    assert "Avg output tokens" in out

    # Footer with artifact paths.
    assert "Artifacts" in out
    assert "/tmp/run-test-0001/report.json" in out


def test_pretty_print_json_mode_emits_valid_json(capsys):
    report = _build_suite_report()
    assert report.pretty_print(json=True) is None
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload["id"] == "run-test-0001"
    assert "detailed_results" in payload


def test_pretty_print_handles_empty_report(capsys):
    report = BenchmarkReport(id="empty-1", summary={}, detailed_results=[])
    assert report.pretty_print() is None
    out = capsys.readouterr().out
    # With no header fields and no cases, falls back to JSON dump.
    assert "empty-1" in out


def test_pretty_print_handles_single_case_scenario_run(capsys):
    report = BenchmarkReport(
        id="run-single-1",
        summary={
            "mode": "scenario",
            "status": "completed",
            "total_cases": 1,
            "average_score": 1.0,
        },
        detailed_results=[
            {
                "scenario_id": "generated_high_latency_xs_001",
                "score": 1.0,
                "correct_verdict": True,
                "correct_device": True,
                "correct_fault_type": True,
                "correct_interface": True,
                "details": {
                    "interface_applicable": True,
                    "inconclusive": False,
                    "ground_truth": {
                        "fault_type": "high_latency",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                    },
                    "agent_output": {
                        "fault_type": "high_latency",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                        "tool_calls": [{"tool": "a"}, {"tool": "b"}, {"tool": "c"}],
                        "time_taken_seconds": 42.0,
                    },
                },
            }
        ],
    )
    report.pretty_print()
    out = capsys.readouterr().out
    assert "high_latency" in out
    # Tool count fallback from tool_calls list length.
    assert " 3" in out
    # Time formatted.
    assert "42.0" in out

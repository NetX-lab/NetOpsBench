from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plot_results.py"
SPEC = importlib.util.spec_from_file_location("plot_results", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
plot_results = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plot_results)


def test_k12_case_snapshot_matches_release_summary() -> None:
    release = plot_results._load_release_data(plot_results.DEFAULT_RELEASE_DATA)
    snapshot = plot_results._load_k12_case_data(plot_results.DEFAULT_K12_CASE_DATA, release)

    cases = snapshot["cases"]
    assert len(cases) == 70
    assert len({case["scenario_id"] for case in cases}) == 70
    assert sum(case["fault_type"] == "healthy_network" for case in cases) == 4
    assert sum(case["fault_type"] != "healthy_network" for case in cases) == 66
    assert sum(float(case["score"]) for case in cases) / len(cases) == 0.55


def test_k12_case_outcomes_are_exhaustive() -> None:
    release = plot_results._load_release_data(plot_results.DEFAULT_RELEASE_DATA)
    snapshot = plot_results._load_k12_case_data(plot_results.DEFAULT_K12_CASE_DATA, release)
    outcomes = Counter(plot_results._k12_case_outcome(case) for case in snapshot["cases"])

    assert outcomes == {
        "complete": 36,
        "device_only": 5,
        "location_missed": 1,
        "incorrect_verdict": 28,
    }

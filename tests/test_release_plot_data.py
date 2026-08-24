from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt

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


def test_all_scale_analysis_has_complete_bounded_denominators() -> None:
    snapshot = plot_results._load_analysis_data(plot_results.DEFAULT_ANALYSIS_DATA)
    families = snapshot["fault_families"]

    assert snapshot["agent"] == "minimal-deepagent"
    assert snapshot["model"] == "deepseek-v4-pro"
    assert sum(values["operational_cases"] for values in families.values()) == 319
    assert sum(values["agent_cases"] for values in families.values()) == 319
    healthy = families["healthy_network"]
    assert healthy["operational_cases"] == 25
    assert healthy["signal_cases"] == 0
    assert healthy["correct_verdict"] == 25
    public_text = plot_results.DEFAULT_ANALYSIS_DATA.read_text(encoding="utf-8")
    for forbidden in ("/local/", "/home/", "prompt", "reasoning", "trace_id"):
        assert forbidden not in public_text


def test_release_figure_header_has_visible_line_spacing() -> None:
    fig = plt.figure(figsize=(9.2, 4.85), dpi=100)
    title, subtitle = plot_results._release_figure_header(fig, "Release title", "Release subtitle")
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    title_box = title.get_window_extent(renderer=renderer)
    subtitle_box = subtitle.get_window_extent(renderer=renderer)
    plt.close(fig)

    assert title_box.y0 - subtitle_box.y1 >= 10


def test_results_table_matches_release_snapshot() -> None:
    release = plot_results._load_release_data(plot_results.DEFAULT_RELEASE_DATA)
    results_page = (ROOT / "docs/content/docs/run-benchmarks/results.mdx").read_text(encoding="utf-8")
    for row in plot_results._release_scale_rows(release):
        source = release["scales"].get(row["key"]) or release["large_topologies"][row["key"]]
        expected = (
            f"| {row['label'].replace('K=', 'Fat-tree K=') if row['architecture'] == 'Fat-tree' else row['label']} "
            f"| {row['cases']} | {row['diagnosis_score']:.1f} | {row['detection_f1']:.1f} "
            f"| {100 * float(source['device_localization_rate']):.1f} "
            f"| {100 * float(source['interface_localization_rate']):.1f} "
            f"| {float(source['avg_input_tokens']) / 1000:.1f} | {float(source['avg_agent_seconds']):.1f} |"
        )
        assert expected in results_page

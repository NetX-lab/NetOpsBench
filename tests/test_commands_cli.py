"""Regression tests for the new query-focused SDK CLI."""

import pytest

from netopsbench.cli import build_parser, main
from netopsbench.sdk import NetOpsBench


def test_cli_help_shows_query_focused_commands(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    output = capsys.readouterr().out
    assert "status" in output
    assert "runtime" in output
    assert "scenario" in output
    assert "result" in output


def test_cli_runtime_list_and_show(tmp_path, monkeypatch, capsys):
    bench = NetOpsBench(workspace=str(tmp_path))
    bench.runtimes.create(scale="xs", workers=1, name="r1")

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "runtime", "list"])
    assert main() == 0
    list_out = capsys.readouterr().out
    assert "r1" in list_out

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "runtime", "show", "r1"])
    assert main() == 0
    show_out = capsys.readouterr().out
    assert '"name": "r1"' in show_out


def test_cli_scenario_validate_and_list(tmp_path, monkeypatch, capsys):
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    scenario_file = scenario_dir / "s1.yaml"
    scenario_file.write_text(
        "scenario_id: s1\nname: Scenario 1\ntopology_scale: xs\nepisodes:\n  - episode_id: ep1\n    fault_type: link_down\n    target:\n      device: leaf1\n      interface: Ethernet1\n",
        encoding="utf-8",
    )

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    monkeypatch.chdir(outside_dir)

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "scenario", "list", "scenarios"])
    assert main() == 0
    list_out = capsys.readouterr().out
    assert str(scenario_file) in list_out

    monkeypatch.setattr(
        "sys.argv", ["netopsbench", "--workspace", str(tmp_path), "scenario", "validate", "scenarios/s1.yaml"]
    )
    assert main() == 0
    validate_out = capsys.readouterr().out
    assert f"valid: {scenario_file}" in validate_out


def test_cli_status_reports_runtime_count(tmp_path, monkeypatch, capsys):
    bench = NetOpsBench(workspace=str(tmp_path))
    bench.runtimes.create(scale="xs", workers=1, name="r1")
    bench.runtimes.create(scale="xs", workers=1, name="r2")

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "status"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "runtimes: 2" in output


def test_cli_scenario_validate_missing_file_returns_friendly_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["netopsbench", "--workspace", str(tmp_path), "scenario", "validate", "scenarios/not_exists.yaml"],
    )
    assert main() == 1
    out = capsys.readouterr().out
    assert "invalid:" in out
    assert "not_exists.yaml" in out


def test_cli_help_shows_generation_commands(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    output = capsys.readouterr().out
    assert "benchmark" in output
    assert "topology" in output


def test_scenario_cli_help_includes_generate(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scenario", "--help"])
    output = capsys.readouterr().out
    assert "generate" in output


def test_topology_cli_help_includes_generate(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["topology", "--help"])
    output = capsys.readouterr().out
    assert "generate" in output


def test_cli_scenario_generate_uses_existing_default_spec(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_generate(workspace, *, scale, spec, topology_dir=None, out=None, seed=42):
        calls.update(
            workspace=workspace,
            scale=scale,
            spec=spec,
            topology_dir=topology_dir,
            out=out,
            seed=seed,
        )
        print("generated scenarios")
        return 0

    monkeypatch.setattr("netopsbench.cli.main._generate_scenarios", fake_generate)
    monkeypatch.setattr(
        "sys.argv", ["netopsbench", "--workspace", str(tmp_path), "scenario", "generate", "--scale", "xs"]
    )

    assert main() == 0
    out = capsys.readouterr().out
    assert "generated scenarios" in out
    assert calls["scale"] == "xs"
    assert calls["spec"] == tmp_path / "scenarios/specs/fault_campaign.yaml"
    assert calls["topology_dir"] is None
    assert calls["out"] is None
    assert calls["seed"] == 42


def test_cli_topology_generate_uses_default_output_dir(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_generate(workspace, *, scale, output_dir=None):
        calls.update(workspace=workspace, scale=scale, output_dir=output_dir)
        print("generated topology")
        return 0

    monkeypatch.setattr("netopsbench.cli.main._generate_topology", fake_generate)
    monkeypatch.setattr(
        "sys.argv", ["netopsbench", "--workspace", str(tmp_path), "topology", "generate", "--scale", "small"]
    )

    assert main() == 0
    out = capsys.readouterr().out
    assert "generated topology" in out
    assert calls == {"workspace": tmp_path, "scale": "small", "output_dir": None}


def test_scenario_generator_module_importable():
    from netopsbench.platform.scenario import generator

    assert hasattr(generator, "TopologyContext")
    assert hasattr(generator, "generate")


def test_cli_runtime_teardown_by_name(tmp_path, monkeypatch, capsys):
    bench = NetOpsBench(workspace=str(tmp_path))
    bench.runtimes.create(scale="xs", workers=1, name="r1")
    assert bench.runtimes.get("r1") is not None

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "runtime", "teardown", "r1"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "torn down: r1" in out
    assert bench.runtimes.get("r1") is None


def test_cli_runtime_teardown_all(tmp_path, monkeypatch, capsys):
    bench = NetOpsBench(workspace=str(tmp_path))
    bench.runtimes.create(scale="xs", workers=1, name="r1")
    bench.runtimes.create(scale="xs", workers=1, name="r2")

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "runtime", "teardown", "--all"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "torn down 2 runtime(s)" in out
    assert bench.runtimes.list() == []


def test_cli_runtime_teardown_not_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "runtime", "teardown", "ghost"])
    assert main() == 1
    out = capsys.readouterr().out
    assert "runtime not found: ghost" in out


def test_cli_result_list(tmp_path, monkeypatch, capsys):
    results_dir = tmp_path / "scenario_results" / "suite1" / "run-0001"
    results_dir.mkdir(parents=True)
    (results_dir / "report.json").write_text(
        '{"id": "run:run-0001", "summary": {"status": "completed", "total_cases": 3, "average_score": 0.75, "completed_at": "2026-04-12T00:00:00Z"}, "scenario_summaries": [], "detailed_results": [], "artifact_paths": {}, "raw": {}}',
        encoding="utf-8",
    )

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "result", "list"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "run:run-0001" in out
    assert "completed" in out
    assert "0.75" in out
    assert "3" in out


def test_cli_result_list_empty(tmp_path, monkeypatch, capsys):
    (tmp_path / "scenario_results").mkdir()
    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "result", "list"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "no results found" in out


def test_cli_result_show(tmp_path, monkeypatch, capsys):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        '{"id": "run:r1", "summary": {"status": "completed", "total_cases": 1}, "scenario_summaries": [], "detailed_results": [], "artifact_paths": {}, "raw": {}}',
        encoding="utf-8",
    )

    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "result", "show", str(report_path)])
    assert main() == 0
    out = capsys.readouterr().out
    assert "run:r1" in out


def test_cli_result_show_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["netopsbench", "--workspace", str(tmp_path), "result", "show", "no_such.json"])
    assert main() == 1
    out = capsys.readouterr().out
    assert "report not found" in out


def test_cli_benchmark_prepare_runs_topology_then_scenario_generation(tmp_path, monkeypatch, capsys):
    topology_calls = []
    scenario_calls = []

    def fake_generate_topology(workspace, *, scale, output_dir=None):
        topology_calls.append((workspace, scale, output_dir))
        print(f"topology:{scale}")
        return 0

    def fake_generate_scenarios(workspace, *, scale, spec, topology_dir=None, out=None, seed=42):
        scenario_calls.append((workspace, scale, spec, topology_dir, out, seed))
        print(f"scenarios:{scale}")
        return 0

    monkeypatch.setattr("netopsbench.cli.main._generate_topology", fake_generate_topology)
    monkeypatch.setattr("netopsbench.cli.main._generate_scenarios", fake_generate_scenarios)
    monkeypatch.setattr(
        "sys.argv",
        [
            "netopsbench",
            "--workspace",
            str(tmp_path),
            "benchmark",
            "prepare",
            "--scales",
            "xs,medium",
            "--seed",
            "7",
        ],
    )

    assert main() == 0
    out = capsys.readouterr().out
    assert "topology:xs" in out
    assert "scenarios:medium" in out
    assert [call[1] for call in topology_calls] == ["xs", "medium"]
    assert [call[1] for call in scenario_calls] == ["xs", "medium"]
    assert scenario_calls[0][2] == tmp_path / "scenarios/specs/fault_campaign.yaml"
    assert scenario_calls[0][5] == 7

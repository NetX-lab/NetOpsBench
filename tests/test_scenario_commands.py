"""Scenario command coverage for query-focused CLI."""

import pytest

from netopsbench.cli import build_parser


def test_scenario_cli_exposes_only_list_and_validate(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scenario", "--help"])
    output = capsys.readouterr().out
    assert "list" in output
    assert "validate" in output
    assert "generate" in output
    assert "run" not in output


def test_scenario_cli_rejects_legacy_run_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scenario", "run", "scenarios/xs"])

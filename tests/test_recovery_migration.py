"""Recovery v3 is explicit and cannot rewrite frozen source campaigns."""

import json
from types import SimpleNamespace

import pytest

from netopsbench.models.recovery import RecoveryPolicy
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.scenario.migration import migrate_recovery
from netopsbench.platform.scenario.parser import parse_scenario_file, save_scenario_file
from netopsbench.platform.session.orchestrator import SessionOrchestrator


def legacy():
    return ScenarioSpec(
        scenario_id="original",
        name="original",
        topology_scale="xs",
        episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
        metadata={"negative_sample": True, "experiment": {"observation_seconds": 30}},
    )


@pytest.mark.parametrize("source_version", [None, 2])
def test_migration_preserves_case_and_source_and_changes_identity(tmp_path, source_version):
    source = tmp_path / "frozen"
    case = legacy()
    if source_version is not None:
        case.metadata["recovery_policy"] = {
            "version": source_version,
            "baseline_max_cases": 10,
            "baseline_max_age_seconds": 600,
        }
    path = save_scenario_file(case, source / "healthy.yaml")
    original = path.read_bytes()
    output = migrate_recovery(source, tmp_path / "new")
    migrated = parse_scenario_file(output / "healthy.yaml")
    assert path.read_bytes() == original
    assert migrated.id == case.id and migrated.episode == case.episode
    assert migrated.metadata["experiment"] == case.metadata["experiment"]
    assert migrated.digest != case.digest
    assert RecoveryPolicy.from_metadata(migrated.metadata) == RecoveryPolicy()
    receipt = json.loads((output / "migration.json").read_text())
    assert receipt["sources"][0]["scenario_digest"] == case.digest
    assert migrated.metadata["recovery_migration"]["campaign_id"] == receipt["campaign_id"]
    with pytest.raises(ValueError, match="new output"):
        migrate_recovery(source, output)
    with pytest.raises(ValueError, match="outside"):
        migrate_recovery(source, source / "nested")


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {"version": 1},
        {"version": 2},
        {"version": 2, "baseline_max_age_seconds": 600},
        {"mode": "full_window"},
        {"version": 3, "baseline_max_age_seconds": 600},
    ],
)
def test_old_policy_rejected_before_deployment(tmp_path, monkeypatch, policy):
    session = SessionOrchestrator(workspace=str(tmp_path))
    case = legacy()
    if policy is not None:
        case.metadata["recovery_policy"] = policy
    monkeypatch.setattr(session, "_provision_runtime", lambda **kw: pytest.fail("must validate before provisioning"))
    with pytest.raises(ValueError):
        session.run_scenario(scenario=case, agent=SimpleNamespace())


def test_failed_migration_does_not_create_destination(tmp_path):
    source = tmp_path / "source"
    save_scenario_file(legacy(), source / "good.yaml")
    (source / "bad.yaml").write_text("invalid: true")
    with pytest.raises(ValueError):
        migrate_recovery(source, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_cli_migration_writes_a_new_campaign(tmp_path, monkeypatch):
    from netopsbench.cli.main import main

    save_scenario_file(legacy(), tmp_path / "old.yaml")
    monkeypatch.setattr(
        "sys.argv", ["netopsbench", "--workspace", str(tmp_path), "scenario", "migrate", "old.yaml", "--out", "new"]
    )
    assert main() == 0
    assert RecoveryPolicy.from_metadata(parse_scenario_file(tmp_path / "new" / "old.yaml").metadata).version == 3

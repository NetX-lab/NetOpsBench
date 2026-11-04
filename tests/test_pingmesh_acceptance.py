"""Acceptance selection and capacity checks must fail before costly deployments."""

import hashlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.runtime import resources
from netopsbench.platform.scenario.parser import save_scenario_file
from netopsbench.platform.session import acceptance
from netopsbench.platform.topology.config import config_for_scale
from scripts import validate_pingmesh_scales as cli

BACKEND = {"provider": "upstream", "containerlab_version": "test"}


@pytest.mark.parametrize("change", [None, "imported_image", "content", "source", "agent", "backend", "health", "case", "fingerprint"])
def test_codex_qualification_requires_matching_frozen_evidence(tmp_path, change):
    identity = {"model": "gpt-6.1-sol", "service_tier": "default", "image_id": "sha256:test", "image_content_sha256": "same-content"}
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "runtime_disposition": "torn_down",
        "final_health": {"status": "passed"},
        "runtime_backend": BACKEND,
        "agent_identity": identity,
        "source_tree_sha256": "frozen-source",
        "cases": [
            {"id": f"xs-{fault}", "fault_type": fault, "status": "completed", "valid": True,
             "diagnosis_complete": True, "cleanup_failed": False, "score": 0}
            for fault in ("none", "link_down", "device_down", "bgp_auth_mismatch")
        ],
    }
    if change == "source":
        receipt["source_tree_sha256"] = "other-source"
    elif change == "agent":
        receipt["agent_identity"] = {**identity, "service_tier": "fast"}
    elif change == "content":
        receipt["agent_identity"] = {**identity, "image_content_sha256": "other-content"}
    elif change == "backend":
        receipt["runtime_backend"] = {"provider": "other"}
    elif change == "health":
        receipt["final_health"] = {"status": "failed"}
    elif change == "case":
        receipt["cases"][0]["diagnosis_complete"] = False
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(receipt))
    args = SimpleNamespace(
        codex_smoke_receipt=path,
        codex_smoke_receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        source_tree_sha256="frozen-source",
    )
    if change == "fingerprint":
        args.codex_smoke_receipt_sha256 = "changed"
    current_identity = {**identity, "image_id": "imported-id"} if change == "imported_image" else identity
    if change not in {None, "imported_image"}:
        with pytest.raises(ValueError, match="Codex qualification"):
            acceptance.qualified_codex_smoke(args, {"codex": current_identity}, BACKEND)
    else:
        # Qualification checks execution and health, never model accuracy.
        assert acceptance.qualified_codex_smoke(args, {"codex": current_identity}, BACKEND)["status"] == "passed"


@pytest.fixture(autouse=True)
def mock_runtime_backend(monkeypatch):
    monkeypatch.setattr(acceptance, "runtime_backend_identity", lambda: dict(BACKEND))
    monkeypatch.setattr(resources, "runtime_backend_identity", lambda: dict(BACKEND))


@pytest.mark.parametrize("agents", [[], ["codex"], ["deepagent", "codex"]])
def test_acceptance_defaults_to_deepseek_and_requires_explicit_codex(monkeypatch, agents):
    arguments = ["acceptance", "--scales", "xs"]
    if agents:
        arguments.extend(["--agents", *agents])
    monkeypatch.setattr(sys, "argv", arguments)
    received = []

    def run(args, **kwargs):
        received.append((args.vendor, args.agents))
        return 0

    monkeypatch.setattr(cli, "run_acceptance", run)
    assert cli.main() == 0
    assert received == [("deepseek", agents or ["deepagent"])]


def test_selection_preserves_every_coverage_unit(tmp_path):
    from netopsbench.platform.scenario.generator import generate, load_topology
    from netopsbench.platform.topology.generator import generate_topology

    root = tmp_path / "topology"
    generate_topology("medium", str(root))
    topo = load_topology("medium", str(root))
    paths = generate({"fault_catalog_version": 2}, topo, tmp_path / "cases", 42)
    selected = cli.select_cases(paths, True, topo.manifest)
    assert len(selected) == 64
    assert all(cli.parse_scenario_file(p).episode.is_healthy for p in selected[:4])
    assert len(cli.select_cases(paths, False, topo.manifest)) == 4


def test_capacity_requires_twenty_percent_margin_and_disk_reserve(monkeypatch):
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *a, **kw: (
            "MemAvailable: 100000000\n" if str(self) == "/proc/meminfo" else original_read(self, *a, **kw)
        ),
    )
    free = [1000 * resources.GIB]
    monkeypatch.setattr(acceptance.shutil, "disk_usage", lambda _: SimpleNamespace(free=free[0]))
    sample = {
        "runtime_backend": BACKEND,
        "nodes": [
            {"name": "lab-edge1", "data_ports": 18, "memory_bytes": 100 * 1024**2, "writable_bytes": 10 * 1024**2},
            {"name": "lab-client1", "memory_bytes": 2 * 1024**2, "writable_bytes": 1024},
            {
                "name": "influxdb",
                "role": "observability",
                "memory_bytes": 1000 * resources.GIB,
                "writable_bytes": 1000 * resources.GIB,
            },
        ],
    }
    result = resources.capacity_check(sample, "fat-tree-k18", Path("/unused"))
    assert result["memory_margin"] == 1.2
    assert result["status"] == "passed"
    free[0] = 20 * resources.GIB
    assert resources.capacity_check(sample, "fat-tree-k18", Path("/unused"))["status"] == "blocked"


def test_capacity_uses_peak_of_matching_port_capacity(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "MemAvailable: 400000000\n")
    monkeypatch.setattr(acceptance.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000 * resources.GIB))
    sample = resources.peak_sample(
        [
            {
                "runtime_backend": BACKEND,
                "nodes": [
                    {
                        "name": "lab-leaf1",
                        "role": "switch",
                        "data_ports": 18,
                        "memory_bytes": 100 * 1024**2,
                        "writable_bytes": 1024,
                    },
                    {
                        "name": "lab-spine1",
                        "role": "switch",
                        "data_ports": 128,
                        "memory_bytes": 1000 * 1024**2,
                        "writable_bytes": 1024,
                    },
                    {
                        "name": "lab-client1",
                        "role": "client",
                        "data_ports": 1,
                        "memory_bytes": 2 * 1024**2,
                        "writable_bytes": 1024,
                    },
                ],
            },
            {
                "runtime_backend": BACKEND,
                "nodes": [
                    {
                        "name": "lab-leaf1",
                        "role": "switch",
                        "data_ports": 18,
                        "memory_bytes": 200 * 1024**2,
                        "writable_bytes": 2048,
                    }
                ],
            },
        ]
    )
    result = resources.capacity_check(sample, "fat-tree-k18", Path("/unused"))
    assert all(cost["sample_ports"] == 18 and not cost["extrapolated"] for cost in result["switch_costs"])
    assert all(cost["sample_memory_bytes"] == 200 * 1024**2 for cost in result["switch_costs"])
    for row in sample["nodes"]:
        if row["role"] == "switch":
            row["data_ports"] = 8
    assert resources.capacity_check(sample, "fat-tree-k18", Path("/unused"))["status"] == "blocked"


@pytest.mark.parametrize("retained", [False, True])
def test_effects_capacity_excludes_absent_diagnostic_resources_only(monkeypatch, retained):
    monkeypatch.setattr(Path, "read_text", lambda self: "MemAvailable: 400000000\n")
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000 * resources.GIB))
    sample = {
        "runtime_backend": BACKEND,
        "nodes": [
            {"name": "lab-core1", "data_ports": 18, "memory_bytes": 500 * 1024**2, "writable_bytes": 1024},
            {"name": "lab-client1", "memory_bytes": 20 * 1024**2, "writable_bytes": 1024},
        ],
    }
    diagnose = resources.capacity_check(sample, "fat-tree-k18", Path("/unused"), retained)
    effects = resources.capacity_check(sample, "fat-tree-k18", Path("/unused"), retained, False)
    assert diagnose["diagnostic_memory_reserve_bytes"] == int(2.25 * resources.GIB)
    assert effects["diagnostic_memory_reserve_bytes"] == 0
    assert diagnose["estimated_memory_bytes"] - effects["estimated_memory_bytes"] == pytest.approx(
        2.7 * resources.GIB, abs=1
    )
    assert diagnose["estimated_disk_bytes"] == effects["estimated_disk_bytes"]
    assert diagnose["switch_costs"] == effects["switch_costs"]
    assert effects["memory_margin"] == 1.2
    assert resources.EMERGENCY_MEMORY_BYTES == 12 * resources.GIB


def test_docker_memory_units_are_converted():
    assert resources.memory_bytes("195.5MiB") == int(195.5 * 1024**2)
    with pytest.raises(ValueError, match="Invalid Docker memory"):
        resources.memory_bytes("invalid")


def test_resume_retains_valid_low_scores_and_retries_incomplete_cases():
    def report(case, valid, cleanup_failed=False, score=0):
        return {
            "diagnosis_complete": not cleanup_failed,
            "summary": {"average_score": score},
            "scenario_summaries": [{"scenario_id": case, "case_valid": valid, "cleanup_failed": cleanup_failed}],
        }

    entry = {
        "cases": [
            report("low-score", True),
            report("healthy", True, score=1),
            report("injection-failed", False),
            report("recovery-failed", True, True),
        ]
    }
    assert acceptance.finished_case_ids(entry) == {"low-score", "healthy"}


def test_completed_campaign_returns_without_constructing_agent_or_executor(tmp_path):
    path = tmp_path / "low-score.yaml"
    save_scenario_file(
        ScenarioSpec(
            scenario_id="low-score",
            name="low-score",
            topology_scale="xs",
            episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
        ),
        path,
    )
    entry = {
        "agents": {
            "codex": {
                "identity": {},
                "cases": [
                    {
                        "scenario_summaries": [{"scenario_id": path.stem, "case_valid": True, "status": "completed"}],
                        "summary": {"average_score": 0},
                        "diagnosis_complete": True,
                    }
                ],
            }
        }
    }
    assert (
        acceptance.run_cases(
            None,
            None,
            [path],
            backend="codex",
            vendor="deepseek",
            entry=entry,
            identity={},
            output=tmp_path,
            result={},
            cancel=None,
        )
        is False
    )
    assert entry["agents"]["codex"]["selected_cases"] == [path.stem]


def test_failed_execution_is_not_completed_model_acceptance():
    failed = {
        "scenario_summaries": [
            {"scenario_id": "failed", "case_valid": True, "status": "failed", "cleanup_failed": False}
        ]
    }
    assert acceptance.finished_case_ids({"cases": [failed]}) == set()


def test_recovered_agent_failure_is_finished_but_not_a_completed_diagnosis():
    summary = {
        "scenario_id": "budget-exhausted",
        "case_valid": True,
        "status": "failed",
        "failure_stage": "agent",
        "cleanup_failed": False,
    }
    report = {"diagnosis_complete": False, "scenario_summaries": [summary]}
    assert acceptance.finished_case_ids({"cases": [report]}) == {"budget-exhausted"}
    assert report["diagnosis_complete"] is False
    summary["cleanup_failed"] = True
    assert acceptance.finished_case_ids({"cases": [report]}) == set()
    summary.update(cleanup_failed=False, failure_stage="execution")
    assert acceptance.finished_case_ids({"cases": [report]}) == set()


@pytest.mark.parametrize("failure_stage", ["agent", "effect_validation"])
def test_run_cases_records_case_failure_and_continues_without_retry(monkeypatch, tmp_path, failure_stage):
    import threading

    paths = []
    for name in ["agent-failed", "valid-zero"]:
        path = tmp_path / f"{name}.yaml"
        save_scenario_file(
            ScenarioSpec(
                scenario_id=name,
                name=name,
                topology_scale="xs",
                episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
            ),
            path,
        )
        paths.append(path)
    selected = []

    def execute(**kwargs):
        selected.extend(case.id for case in kwargs["scenarios"])
        assert kwargs["control"].stop_on_failure is True
        summaries = []
        for case in kwargs["scenarios"]:
            failed = case.id == "agent-failed"
            summary = {
                "scenario_id": case.id,
                "case_valid": not (failed and failure_stage == "effect_validation"),
                "status": ("invalid" if failure_stage == "effect_validation" else "failed") if failed else "completed",
                "cleanup_failed": False,
                **({"failure_stage": failure_stage} if failed else {}),
            }
            raw = {
                "episode": {
                    "diagnosis": {"success": not failed},
                    "execution": {"case_valid": True, "termination_reason": "agent_error" if failed else "submitted"},
                },
                "cleanup": {"success": True},
            }
            kwargs["control"].on_case(summary, raw, [])
            summaries.append(summary)
        return SimpleNamespace(
            wait=lambda: SimpleNamespace(artifact_paths={"report": "report.json"}, scenario_summaries=summaries)
        )

    monkeypatch.setattr(acceptance.Evaluator, "generate_report", lambda *a, **kw: {"summary": {"average_score": 0}})
    bench = SimpleNamespace(
        agents=SimpleNamespace(wrap=lambda raw: SimpleNamespace(name="agent")),
        sessions=SimpleNamespace(_executor=SimpleNamespace(_run_with_existing_runtime=execute)),
    )
    entry, result = {}, {}
    kwargs = dict(
        backend="deepagent",
        vendor="deepseek",
        entry=entry,
        identity={},
        output=tmp_path,
        result=result,
        cancel=threading.Event(),
        agent_factory=lambda backend: lambda **kw: object(),
    )
    runtime = SimpleNamespace(scale="xs")
    assert acceptance.run_cases(bench, runtime, paths, **kwargs) is True
    records = entry["agents"]["deepagent"]
    assert [r["diagnosis_complete"] for r in records["cases"]] == [False, True]
    assert records["cases"][0]["scenario_summaries"][0]["status"] == (
        "invalid" if failure_stage == "effect_validation" else "failed"
    )
    assert acceptance.finished_case_ids(records) == {"agent-failed", "valid-zero"}
    assert acceptance.run_cases(bench, runtime, paths, **kwargs) is False
    assert selected == ["agent-failed", "valid-zero"]


def test_resume_skips_verified_invalid_effect_without_scoring_it():
    summary = {
        "scenario_id": "acl-no-hit",
        "status": "invalid",
        "case_valid": False,
        "failure_stage": "effect_validation",
        "cleanup_failed": False,
    }
    record = {"diagnosis_complete": False, "scenario_summaries": [summary]}
    assert acceptance.finished_case_ids({"cases": [record]}) == {"acl-no-hit"}
    record["mode"] = "effects"
    assert acceptance.finished_case_ids({"cases": [record]}) == set()
    record.pop("mode")
    summary["cleanup_failed"] = True
    assert acceptance.finished_case_ids({"cases": [record]}) == set()


def test_model_inconclusive_without_execution_error_remains_valid():
    diagnosis = {"verdict": "inconclusive", "metadata": {"llm_call_count": 4}}
    report = {
        "scenario_summaries": [{"scenario_id": "low-score", "case_valid": True, "cleanup_failed": False}],
        "diagnosis_complete": True,
        "detailed_results": [{"details": {"agent_output": diagnosis}}],
    }
    assert acceptance.finished_case_ids({"cases": [report]}) == {"low-score"}


@pytest.mark.parametrize("mismatch", ["scale", "plan"])
def test_resume_identity_mismatch_is_rejected_before_health_or_mutation(monkeypatch, tmp_path, mismatch):
    entry = {"runtime_backend": BACKEND, "plan": {"plan_id": "original"}}
    runtime = SimpleNamespace(
        scale="xs" if mismatch == "scale" else "fat-tree-k18",
        workers=[SimpleNamespace(topology_dir=tmp_path)],
        metadata={"runtime_backend": BACKEND},
    )
    monkeypatch.setattr(
        acceptance,
        "load_topology_manifest",
        lambda _: SimpleNamespace(
            require_pingmesh_plan=lambda: SimpleNamespace(settings=SimpleNamespace(plan_id="changed"))
        ),
    )
    with pytest.raises(ValueError, match="runtime|plan"):
        acceptance.validate_resume(entry, runtime, "fat-tree-k18")


@pytest.mark.parametrize(
    ("allow", "memory_gib", "disk_gib", "extrapolated", "expected"),
    [
        (False, 194, 100, False, "Capacity check failed"),
        (True, 194, 100, False, "deployment reached"),
        (True, 191, 100, False, "Capacity check failed"),
        (True, 180, 100, False, "Capacity check failed"),
        (True, 194, 20, False, "Capacity check failed"),
        (True, 194, 100, True, "Capacity check failed"),
    ],
)
def test_explicit_margin_waiver_keeps_memory_disk_and_sample_guards(
    monkeypatch, tmp_path, allow, memory_gib, disk_gib, extrapolated, expected
):
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"runtime_backend": BACKEND, "nodes": []}))
    argv = ["acceptance", "--scales", "fat-tree-k18", "--capacity-sample", str(sample), "--output", str(tmp_path)]
    if allow:
        argv.append("--allow-tight-capacity")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(cli, "preflight_agents", lambda *a, **kw: {"deepagent": {}})
    monkeypatch.setattr(acceptance.subprocess, "check_output", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(
        acceptance,
        "capacity_check",
        lambda *a: {
            "status": "blocked",
            "available_memory_bytes": memory_gib * resources.GIB,
            "estimated_memory_bytes": 218 * resources.GIB,
            "docker_disk_free_bytes": disk_gib * resources.GIB,
            "estimated_disk_bytes": 28 * resources.GIB,
            "switch_costs": [{"extrapolated": extrapolated}],
        },
    )

    def create(**kwargs):
        raise RuntimeError("deployment reached")

    monkeypatch.setattr(
        cli, "NetOpsBench", lambda **kw: nullcontext(SimpleNamespace(runtimes=SimpleNamespace(create=create)))
    )
    with pytest.raises(RuntimeError, match=expected):
        cli.main()


def test_bgp_acceptance_requires_every_neighbor_to_be_established(monkeypatch, tmp_path):
    from netopsbench.platform.topology.clos_builder import build_clos_plan

    manifest = build_clos_plan(config_for_scale("xs")).manifest
    switches = {device.name for device in manifest.switches()}
    rows = [",result,table,_time,_value,source,neighbor_address"]
    for index, link in enumerate(manifest.links):
        if all(endpoint.device in switches for endpoint in link.endpoints):
            a, b = link.endpoints
            rows.extend(f",,0,2026-10-11T00:00:00Z,ESTABLISHED,{src},peer{index}" for src in (a.device, b.device))
    response = SimpleNamespace(status="ok", text="\n".join(rows))
    queries = []
    monkeypatch.setattr(acceptance, "query_flux", lambda *a, **kw: queries.append(a[-1]) or response)
    worker = SimpleNamespace(bucket="test", topology_id="test")
    assert acceptance.bgp_observation(worker, manifest, tmp_path / "bgp.csv")["established"] == 8
    assert "range(start: -130s)" in queries[-1]
    assert 'group(columns: ["source", "neighbor_address"]) |> sort(columns: ["_time"]) |> last()' in queries[-1]
    response.text = response.text.replace("ESTABLISHED", "IDLE", 1)
    with pytest.raises(RuntimeError, match="Incomplete BGP observations"):
        acceptance.bgp_observation(worker, manifest, tmp_path / "bgp.csv")


@pytest.mark.parametrize(
    "owned,completed,emergency,expected",
    [
        (True, False, False, False),
        (True, True, False, True),
        (False, True, False, False),
        (False, False, True, False),
        (True, False, True, True),
    ],
)
def test_retention_obeys_runtime_ownership(owned, completed, emergency, expected):
    calls = []
    runtime = SimpleNamespace(metadata={}, teardown=lambda: calls.append("teardown"))
    disposition = acceptance.release_runtime(runtime, owned=owned, completed=completed, emergency=emergency)
    assert bool(calls) is expected
    assert disposition == ("torn_down" if expected else "retained")


def test_quarantined_runtime_is_retained():
    runtime = SimpleNamespace(metadata={"quarantined": True}, teardown=lambda: pytest.fail("must retain"))
    assert acceptance.release_runtime(runtime, owned=True, completed=False) == "quarantined"


def test_failed_full_health_persists_quarantine_before_raising(monkeypatch):
    writes = []
    runtime = SimpleNamespace(
        workers=[SimpleNamespace()],
        scale_registry=SimpleNamespace(),
        metadata={},
        state="warm",
        _write_metadata=lambda: writes.append(dict(runtime.metadata)),
    )
    runtime.mark_quarantined = lambda reason: (
        runtime.metadata.update(quarantined=True, quarantine_reason=reason),
        setattr(runtime, "state", "quarantined"),
        runtime._write_metadata(),
    )
    monkeypatch.setattr(acceptance, "check_worker_health", lambda *a, **kw: ["BGP not converged"])
    with pytest.raises(RuntimeError, match="BGP not converged"):
        acceptance.require_full_health(runtime)
    assert runtime.state == "quarantined"
    assert writes == [{"quarantined": True, "quarantine_reason": "acceptance_health_failure"}]


def test_checkpoint_is_atomic_and_preserves_failed_attempts(tmp_path):
    payload = {"status": "failed_retained", "scales": {"xs": {"error": "interrupted", "cases": []}}}
    acceptance.checkpoint(tmp_path, payload)
    assert json.loads((tmp_path / "acceptance.json").read_text()) == payload
    assert len(list(tmp_path.iterdir())) == 1


def test_xs_smoke_has_four_current_validation_cases(tmp_path):
    from netopsbench.platform.scenario.generator import generate, load_topology
    from netopsbench.platform.topology.generator import generate_topology

    root = tmp_path / "topology"
    generate_topology("xs", str(root))
    topo = load_topology("xs", str(root))
    paths = generate({"fault_catalog_version": 2}, topo, tmp_path / "cases", 42)
    selected = cli.smoke_cases(paths, topo.manifest)
    cases = {cli.parse_scenario_file(path).episode.fault_type: cli.parse_scenario_file(path) for path in selected}
    assert set(cases) == {"none", "link_down", "device_down", "bgp_auth_mismatch"}
    assert topo.manifest.device(cases["device_down"].episode.target_device).role.value == "spine"
    assert cases["link_down"].metadata["coverage_unit"].endswith("leaf-spine")


def test_owned_failed_partial_deployment_is_retained_and_quarantined(tmp_path):
    calls = []
    runtime = SimpleNamespace(
        stage_results={"deploy": SimpleNamespace(status="failed")},
        root_dir=tmp_path,
        name="partial",
        metadata={},
        state="created",
        _write_metadata=lambda: calls.append("write"),
        describe=lambda: {"state": "created"},
        teardown=lambda: calls.append("teardown"),
    )
    runtime.mark_quarantined = lambda reason: (
        runtime.metadata.update(quarantined=True, quarantine_reason=reason),
        setattr(runtime, "state", "quarantined"),
        runtime._write_metadata(),
    )
    assert not acceptance.quarantine_partial_deployment(runtime, {"runtime_owned": False}, tmp_path)
    entry = {"runtime_owned": True}
    assert acceptance.quarantine_partial_deployment(runtime, entry, tmp_path)
    assert calls == ["write"]
    assert runtime.state == "quarantined"
    assert runtime.metadata["quarantined"]
    assert entry["partial_deployment"] == {"state": "created"}
    runtime.stage_results["deploy"].status = "completed"
    runtime.mark_quarantined = lambda reason: (
        runtime.metadata.update(quarantined=True, quarantine_reason=reason),
        setattr(runtime, "state", "quarantined"),
        runtime._write_metadata(),
    )
    assert not acceptance.quarantine_partial_deployment(runtime, entry, tmp_path)


def test_explicit_rebuild_can_validate_an_incomplete_owned_runtime_without_a_plan(tmp_path):
    runtime = SimpleNamespace(
        scale="xs", workers=[SimpleNamespace(topology_dir=tmp_path)], metadata={"runtime_backend": BACKEND}
    )
    assert acceptance.validate_resume({"runtime_backend": BACKEND}, runtime, "xs", allow_unplanned=True) is None
    with pytest.raises((FileNotFoundError, ValueError)):
        acceptance.validate_resume({"runtime_backend": BACKEND}, runtime, "xs")


def test_retained_lab_capacity_does_not_charge_for_another_deployment(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a: "MemAvailable: 110000000\n")
    monkeypatch.setattr(acceptance.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000 * resources.GIB))
    sample = {
        "runtime_backend": BACKEND,
        "nodes": [
            {
                "name": "lab-core1",
                "role": "switch",
                "data_ports": 18,
                "memory_bytes": 500 * 1024**2,
                "writable_bytes": 1024,
            },
            {"name": "lab-client1", "role": "client", "memory_bytes": 20 * 1024**2, "writable_bytes": 1024},
        ],
    }
    fresh = resources.capacity_check(sample, "fat-tree-k18", Path("/unused"))
    retained = resources.capacity_check(sample, "fat-tree-k18", Path("/unused"), True)
    assert fresh["status"] == "blocked"
    assert retained["status"] == "passed"
    assert retained["mode"] == "retained_runtime"
    assert retained["estimated_memory_bytes"] < retained["full_runtime_estimated_memory_bytes"]


@pytest.mark.parametrize("change", ["add", "remove", "replace", "content"])
def test_case_set_and_contents_are_immutable_on_resume(tmp_path, change):
    def case(name, filename=None):
        return save_scenario_file(
            ScenarioSpec(
                scenario_id=name,
                name=name,
                topology_scale="xs",
                episode=EpisodeSpec(episode_id="diagnosis", fault_type="none"),
            ),
            tmp_path / f"{filename or name}.yaml",
        )

    original = [case("one", "filename-is-not-id"), case("two")]
    records = {}
    acceptance.freeze_cases(records, original)
    assert records["selected_cases"] == ["one", "two"]
    before = json.loads(json.dumps(records))
    changed = list(original)
    if change == "add":
        changed.append(case("three"))
    elif change == "remove":
        changed.pop()
    elif change == "replace":
        changed[-1] = case("three")
    else:
        original[0].write_text(original[0].read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="scenario"):
        acceptance.freeze_cases(records, changed)
    assert records == before


def test_preflight_interrupt_prevents_runtime_creation(monkeypatch, tmp_path):
    import signal

    monkeypatch.setattr(sys, "argv", ["acceptance", "--scales", "xs", "--agents", "codex", "--output", str(tmp_path)])
    monkeypatch.setattr(acceptance.subprocess, "check_output", lambda *a, **kw: str(tmp_path))

    def preflight(*args, **kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return {"codex": {}}

    monkeypatch.setattr(cli, "preflight_agents", preflight)
    created = []
    monkeypatch.setattr(
        cli,
        "NetOpsBench",
        lambda **kw: nullcontext(
            SimpleNamespace(
                runtimes=SimpleNamespace(create=lambda **kw: created.append(kw)),
            )
        ),
    )
    with pytest.raises(RuntimeError, match="cancelled"):
        cli.main()
    assert created == []
    checkpoint = json.loads((tmp_path / "acceptance.json").read_text())
    assert checkpoint["status"] == "failed_retained"
    assert checkpoint["memory_samples"]


def test_resumed_campaign_exposes_current_resource_state_before_deployment(monkeypatch, tmp_path):
    from netopsbench.platform.runtime.manager import RuntimePool
    from netopsbench.sdk import NetOpsBench

    workspace = tmp_path / "workspace"
    output = tmp_path / "campaign"
    output.mkdir()
    runtime_dir = workspace / ".netopsbench" / "runtimes" / "historical"
    recorded = {
        "runtime_backend": BACKEND,
        "vendor": "deepseek",
        "mode": "effects",
        "fault_catalog_version": 2,
        "status": "failed_retained",
        "finished": "2026-10-12T00:00:00Z",
        "scales": {
            "xs": {
                "runtime": str(runtime_dir),
                "runtime_owned": True,
                "runtime_disposition": "quarantined",
                "resource_samples": [],
            }
        },
    }
    acceptance.checkpoint(output, recorded)
    sample = tmp_path / "capacity.json"
    sample.write_text(json.dumps({"runtime_backend": BACKEND, "nodes": []}))
    args = SimpleNamespace(
        mode="effects",
        agents=[],
        scales="xs",
        vendor="deepseek",
        output=output,
        resume_runtime=runtime_dir,
        rebuild_runtime=False,
        capacity_sample=sample,
        allow_tight_capacity=False,
    )
    monkeypatch.setattr(acceptance.subprocess, "check_output", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(acceptance, "capacity_check", lambda *a: {"status": "passed"})
    monkeypatch.setattr(acceptance, "validate_campaign_cases", lambda *a: None)
    monkeypatch.setattr(resources.ResourceMonitor, "watch", lambda *a: None)

    def deploy(runtime):
        current = json.loads((output / "acceptance.json").read_text())
        assert current["status"] == "running"
        assert "finished" not in current
        assert current["scales"]["xs"]["runtime_disposition"] == "active"
        assert current["resumptions"][-1]["previous_finished"] == recorded["finished"]
        raise RuntimeError("deployment reached")

    monkeypatch.setattr(RuntimePool, "deploy", deploy)
    with pytest.raises(RuntimeError, match="deployment reached"):
        acceptance.run_acceptance(
            args,
            bench_factory=lambda **kw: NetOpsBench(workspace=str(workspace)),
            agent_factory=None,
            preflight=lambda *a, **kw: {},
            select_cases=cli.select_cases,
            smoke_cases=cli.smoke_cases,
        )


def test_prepared_identity_is_durable_before_the_first_case(monkeypatch, tmp_path):
    from netopsbench.models.profiles import default_scale_registry
    from netopsbench.platform.topology.generator import generate_topology

    output, topology = tmp_path / "campaign", tmp_path / "runtime" / "worker-1"
    generated = generate_topology("xs", str(topology), name="checkpoint-xs")
    worker = SimpleNamespace(
        lab_name="checkpoint-xs", topology_dir=topology, mgmt_subnet="172.31.100.0/24", mgmt_network="checkpoint-mgmt"
    )
    runtime = SimpleNamespace(
        state="warm",
        root_dir=topology.parent,
        workers=[worker],
        scale_registry=default_scale_registry(),
        metadata={},
        stage_results={},
        describe=lambda: {},
    )
    bench = SimpleNamespace(runtimes=SimpleNamespace(create=lambda **kw: SimpleNamespace(_runtime=runtime)))
    sample = tmp_path / "capacity.json"
    sample.write_text(json.dumps({"runtime_backend": BACKEND, "nodes": []}))
    args = SimpleNamespace(
        mode="effects",
        agents=[],
        scales="xs",
        vendor="deepseek",
        output=output,
        resume_runtime=None,
        rebuild_runtime=False,
        capacity_sample=sample,
        allow_tight_capacity=False,
    )
    monkeypatch.setattr(
        acceptance.subprocess, "check_output", lambda cmd, **kw: "sha256:test" if "inspect" in cmd else str(tmp_path)
    )
    monkeypatch.setattr(resources.ResourceMonitor, "start", lambda *a: None)
    monkeypatch.setattr(resources.ResourceMonitor, "watch", lambda *a: None)
    monkeypatch.setattr(acceptance, "require_full_health", lambda *a: {"status": "passed"})
    monkeypatch.setattr(acceptance, "capacity_check", lambda *a: {"status": "passed"})
    monkeypatch.setattr(acceptance, "ingestion_latency", lambda *a: {"query_visible_seconds": 0.1})
    monkeypatch.setattr(acceptance, "bgp_observation", lambda *a, **kw: {"established": 8})
    monkeypatch.setattr(acceptance, "storage_snapshot", lambda: {})

    def first_case(*a, **kw):
        saved = json.loads((output / "acceptance.json").read_text())["scales"]["xs"]
        assert saved["plan"]["plan_id"] == generated["metadata"]["pingmesh_plan"]["settings"]["plan_id"]
        assert saved["client_images"] == ["sha256:test"]
        assert saved["start_health"]["status"] == "passed"
        assert saved["ingestion_latency"]["query_visible_seconds"] == 0.1
        raise RuntimeError("first case interrupted")

    monkeypatch.setattr(acceptance, "run_cases", first_case)
    with pytest.raises(RuntimeError, match="first case interrupted"):
        acceptance.run_acceptance(
            args,
            bench_factory=lambda **kw: nullcontext(bench),
            agent_factory=None,
            preflight=lambda *a, **kw: {},
            select_cases=cli.select_cases,
            smoke_cases=cli.smoke_cases,
        )
    assert json.loads((output / "acceptance.json").read_text())["status"] == "failed_retained"


def test_checkpoint_remains_parseable_during_resource_sampling(tmp_path):
    import threading

    from netopsbench.platform.runtime.resources import ResourceMonitor

    result = {"scales": {}}
    cancel, emergency = threading.Event(), threading.Event()
    monitor = ResourceMonitor(result, cancel, emergency, acceptance._CHECKPOINT_LOCK)
    monitor.start()
    try:
        for _ in range(10):
            acceptance.checkpoint(tmp_path, result)
            assert json.loads((tmp_path / "acceptance.json").read_text())["memory_samples"]
    finally:
        monitor.close()
    assert not cancel.is_set()


@pytest.mark.parametrize("mismatch", ["model", "image_id", "service_tier"])
def test_codex_resume_identity_checked_before_credentials_or_container_preflight(monkeypatch, tmp_path, mismatch):
    agent = SimpleNamespace(
        cli_version="0.159.2",
        model="gpt-6.1-sol",
        reasoning_effort="xhigh",
        service_tier="default",
        timeout_seconds=600,
        max_tool_calls=40,
        image="codex-image",
        preflight=lambda: pytest.fail("identity mismatch must not create credentials or containers"),
    )
    expected = {
        "cli_version": agent.cli_version,
        "model": agent.model,
        "reasoning_effort": agent.reasoning_effort,
        "service_tier": agent.service_tier,
        "timeout_seconds": agent.timeout_seconds,
        "max_mcp_calls": agent.max_tool_calls,
        "image_id": "image-digest",
        "image_content_sha256": "image-content",
    }
    expected[mismatch] = "changed"
    monkeypatch.setattr(cli, "runtime_tools", lambda: tmp_path)
    monkeypatch.setattr(cli, "agent_class", lambda backend: lambda **kwargs: agent)
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *args, **kwargs: "image-digest\n")
    monkeypatch.setattr("examples.agents.codex_agent.runtime.image_content_identity", lambda image: "image-content")
    with pytest.raises(ValueError, match="configuration"):
        cli.preflight_agents(["codex"], "deepseek", tmp_path, expected={"codex": expected})


def test_resume_keeps_completed_zero_score_diagnosis_when_recovery_requires_rebuild():
    report = {
        "diagnosis_complete": True,
        "summary": {"average_score": 0},
        "scenario_summaries": [
            {"scenario_id": "valid-zero", "case_valid": True, "status": "failed", "cleanup_failed": True}
        ],
    }
    assert acceptance.finished_case_ids({"cases": [report]}) == {"valid-zero"}
    report["diagnosis_complete"] = False
    assert acceptance.finished_case_ids({"cases": [report]}) == set()
    report["mode"] = "effects"
    report["diagnosis_complete"] = True
    assert acceptance.finished_case_ids({"cases": [report]}) == set()


def test_resume_does_not_infer_missing_diagnosis_state_from_archived_raw_results(tmp_path):
    raw = tmp_path / "case.json"
    raw.write_text(json.dumps({"episode": {"diagnosis": {"success": True}}}))
    report = {
        "scenario_summaries": [
            {"scenario_id": "old", "case_valid": True, "status": "completed", "raw_result_path": str(raw)}
        ],
    }
    original = raw.read_bytes()
    assert acceptance.finished_case_ids({"cases": [report]}) == set()
    assert raw.read_bytes() == original


def test_effects_resume_requires_valid_completed_and_recovered_case():
    reports = [
        {"mode": "effects", "scenario_summaries": [{"scenario_id": name, **summary}]}
        for name, summary in (
            ("completed", {"case_valid": True, "status": "completed", "cleanup_failed": False}),
            ("invalid", {"case_valid": False, "status": "completed"}),
            ("incomplete", {"case_valid": True, "status": "failed"}),
            ("unrecovered", {"case_valid": True, "status": "completed", "cleanup_failed": True}),
        )
    ]
    assert acceptance.finished_case_ids({"cases": reports}) == {"completed"}


def test_legacy_campaign_rejected_before_provider_or_docker(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from netopsbench.platform.session import acceptance

    output = tmp_path / "campaign"
    output.mkdir()
    (output / "acceptance.json").write_text(
        json.dumps({"mode": "effects", "fault_catalog_version": 2, "runtime_backend": {"provider": "custom"}})
    )
    monkeypatch.setattr(acceptance, "runtime_backend_identity", lambda: {"provider": "upstream"})
    monkeypatch.setattr(acceptance.subprocess, "check_output", lambda *a, **kw: pytest.fail("Docker called"))
    args = SimpleNamespace(
        mode="effects",
        agents=[],
        scales="xs",
        vendor="deepseek",
        output=output,
        resume_runtime=tmp_path / "runtime",
        rebuild_runtime=False,
    )
    with pytest.raises(ValueError, match="Campaign runtime backend changed"):
        acceptance.run_acceptance(
            args,
            bench_factory=None,
            agent_factory=None,
            preflight=lambda *a: pytest.fail("provider called"),
            select_cases=None,
            smoke_cases=None,
        )

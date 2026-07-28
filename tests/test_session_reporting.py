from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.models.profiles import default_scale_registry
from netopsbench.platform.session.orchestrator import SessionOrchestrator
from netopsbench.platform.session.reporting import (
    create_run_report,
    load_topology_metadata,
    reserve_run_id,
)
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.utils.files import atomic_write_json


def test_reserve_run_id_atomically_claims_distinct_directories(tmp_path: Path):
    artifact_root = tmp_path / "runs"
    started_at = datetime(2026, 6, 5, 12, 40, 40, tzinfo=UTC)

    first = reserve_run_id(artifact_root, started_at=started_at)
    second = reserve_run_id(artifact_root, started_at=started_at)

    assert (first, second) == ("run-20260605T124040Z", "run-20260605T124040Z-02")
    assert (artifact_root / first).is_dir()
    assert (artifact_root / second).is_dir()


def test_session_runtime_loader_preserves_canonical_topology_schema(tmp_path: Path):
    topology_dir = tmp_path / "topology"
    generate_topology("xs", str(topology_dir))

    metadata = load_topology_metadata(topology_dir)

    assert metadata["schema_version"] == "3"
    assert isinstance(metadata["devices"], list)
    assert {device["role"] for device in metadata["devices"]} == {"spine", "leaf", "client"}


def test_session_runtime_loader_requires_canonical_topology(tmp_path: Path):
    missing = tmp_path / "missing-topology"
    missing.mkdir()

    with pytest.raises(FileNotFoundError):
        load_topology_metadata(missing)


def test_create_run_report_preserves_topology_scale_and_agent_name(tmp_path: Path):
    runtime = SimpleNamespace(
        id="run-0001-runtime",
        scale="small",
        scale_registry=default_scale_registry(),
    )
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
        traces_dir=tmp_path / "traces",
        trace_index_path=tmp_path / "traces" / "index.jsonl",
        report_path=tmp_path / "report.json",
        metadata_path=tmp_path / "metadata.json",
    )

    assert report["agent_name"] == "agent-x"
    assert report["topology_scale"] == "small"
    assert report["summary"]["agent_name"] == "agent-x"
    assert report["summary"]["topology_scale"] == "small"
    assert report["raw"]["topology_scale"] == "small"
    assert report["scale_registry_sha256"] == runtime.scale_registry.digest
    assert report["scale_profile_sha256"] == runtime.scale_registry.get("small").digest
    assert report["resolved_scale_profile"]["name"] == "small"
    assert report["artifact_paths"]["traces_dir"] == str(tmp_path / "traces")
    assert report["artifact_paths"]["trace_index"] == str(tmp_path / "traces" / "index.jsonl")


def test_session_report_keeps_execution_and_cleanup_failures_separate(tmp_path: Path, monkeypatch):
    class FailingRuntime:
        id = "runtime-1"
        scale = "xs"
        scale_registry = default_scale_registry()

        def teardown(self):
            raise OSError("cleanup exploded")

    orchestrator = SessionOrchestrator(workspace=str(tmp_path))
    monkeypatch.setattr(orchestrator, "_provision_runtime", lambda **_kwargs: FailingRuntime())

    def fail_execution(**_kwargs):
        raise ValueError("execution exploded")

    monkeypatch.setattr(orchestrator, "_execute_on_runtime_pool", fail_execution)
    scenario = SimpleNamespace(id="scenario-1", scale="xs")

    with pytest.raises(ValueError, match="execution exploded") as raised:
        orchestrator._run_with_platform_runtime(
            mode="scenario",
            scenarios=[scenario],
            agent=SimpleNamespace(name="agent"),
            scale="xs",
            workers=1,
            root_dir=None,
            keep_runtime=False,
            artifacts_dir=None,
            trace=False,
        )

    assert any("cleanup exploded" in note for note in getattr(raised.value, "__notes__", []))
    reports = list((tmp_path / ".netopsbench" / "artifacts" / "runs").glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["status"] == "failed"
    assert report["raw"]["teardown"] == "failed"
    assert report["raw"]["execution_failure"] == {"type": "ValueError", "message": "execution exploded"}
    assert report["raw"]["cleanup_failure"] == {"type": "OSError", "message": "cleanup exploded"}


def test_session_writes_performed_only_after_successful_teardown(tmp_path: Path, monkeypatch):
    class Runtime:
        id = "runtime-1"
        scale = "xs"
        scale_registry = default_scale_registry()
        torn_down = False

        def teardown(self):
            self.torn_down = True

    runtime = Runtime()
    orchestrator = SessionOrchestrator(workspace=str(tmp_path))
    monkeypatch.setattr(orchestrator, "_provision_runtime", lambda **_kwargs: runtime)

    def execute(**kwargs):
        report_path = orchestrator._artifacts_root(None) / kwargs["run_id"] / "report.json"
        atomic_write_json(
            report_path,
            {
                "id": f"run:{kwargs['run_id']}",
                "status": "completed",
                "summary": {"status": "completed"},
                "raw": {"status": "completed", "teardown": kwargs["teardown"]},
            },
        )
        return SimpleNamespace(refresh=lambda: None)

    monkeypatch.setattr(orchestrator, "_execute_on_runtime_pool", execute)
    scenario = SimpleNamespace(id="scenario-1", scale="xs")
    orchestrator._run_with_platform_runtime(
        mode="scenario",
        scenarios=[scenario],
        agent=SimpleNamespace(name="agent"),
        scale="xs",
        workers=1,
        root_dir=None,
        keep_runtime=False,
        artifacts_dir=None,
        trace=False,
    )

    report_path = next((tmp_path / ".netopsbench" / "artifacts" / "runs").glob("*/report.json"))
    report = json.loads(report_path.read_text())
    assert runtime.torn_down is True
    assert report["raw"]["teardown"] == "performed"

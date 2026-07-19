from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.models.profiles import default_scale_registry
from netopsbench.platform.session.reporting import create_run_report, load_topology_metadata, next_run_id
from netopsbench.platform.topology.generator import generate_topology


def test_next_run_id_uses_utc_timestamp_and_collision_suffix(tmp_path: Path):
    artifact_root = tmp_path / "runs"
    started_at = datetime(2026, 6, 5, 12, 40, 40, tzinfo=UTC)

    assert next_run_id(artifact_root, started_at=started_at) == "run-20260605T124040Z"

    (artifact_root / "run-20260605T124040Z").mkdir(parents=True)
    assert next_run_id(artifact_root, started_at=started_at) == "run-20260605T124040Z-02"

    (artifact_root / "run-20260605T124040Z-02").mkdir()
    assert next_run_id(artifact_root, started_at=started_at) == "run-20260605T124040Z-03"


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

"""Tests for the public artifact and report persistence API."""


def test_artifact_manager_resolves_run_and_runtime_dirs(tmp_path):
    from netopsbench.sdk.artifacts import ArtifactManager

    manager = ArtifactManager(workspace=tmp_path)

    assert manager.get_run_dir("run-123") == tmp_path / ".netopsbench" / "runs" / "run-123"
    assert manager.get_runtime_dir("rt-123") == tmp_path / ".netopsbench" / "runtimes" / "rt-123"
    assert manager.get_run_metadata_path("run-123") == tmp_path / ".netopsbench" / "runs" / "run-123" / "metadata.json"
    assert (
        manager.get_runtime_metadata_path("rt-123")
        == tmp_path / ".netopsbench" / "runtimes" / "rt-123" / "metadata.json"
    )


def test_artifact_manager_persists_metadata(tmp_path):
    from netopsbench.sdk.artifacts import ArtifactManager

    manager = ArtifactManager(workspace=tmp_path)
    run_dir = manager.get_run_dir("run-123")
    payload = {"status": "ok", "count": 2}

    metadata_path = manager.save_metadata(run_dir, payload)
    loaded = manager.load_metadata(run_dir)

    assert metadata_path == run_dir / "metadata.json"
    assert loaded == payload


def test_artifact_manager_load_metadata_raises_clear_errors(tmp_path):
    from netopsbench.sdk.artifacts import ArtifactManager

    manager = ArtifactManager(workspace=tmp_path)
    run_dir = manager.get_run_dir("run-404")

    try:
        manager.load_metadata(run_dir)
    except FileNotFoundError as exc:
        assert "metadata file not found" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError for missing metadata")

    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text("{not-json", encoding="utf-8")

    try:
        manager.load_metadata(run_dir)
    except ValueError as exc:
        assert "Invalid metadata JSON" in str(exc)
    else:
        raise AssertionError("expected ValueError for malformed metadata JSON")


def test_artifact_manager_rejects_non_object_metadata_payload(tmp_path):
    from netopsbench.sdk.artifacts import ArtifactManager

    manager = ArtifactManager(workspace=tmp_path)
    run_dir = manager.get_run_dir("run-bad-shape")
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text('["bad-shape"]', encoding="utf-8")

    try:
        manager.load_metadata(run_dir)
    except ValueError as exc:
        assert "Metadata payload must be a JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid metadata shape")


def test_artifact_manager_lists_and_exports_run_traces(tmp_path):
    from netopsbench.sdk.artifacts import ArtifactManager

    manager = ArtifactManager(workspace=tmp_path)
    run_dir = manager.get_run_dir("run-123")
    trace_dir = run_dir / "traces" / "worker-1" / "case-1"
    trace_dir.mkdir(parents=True)
    (run_dir / "traces" / "index.jsonl").write_text(
        '{"trace_id":"t1","run_id":"run-123","case_id":"case-1","scenario_id":"scenario-1","worker":"worker-1","agent":"agent","model":"model","provider":"provider","atif_path":"'
        + str(trace_dir / "trajectory-t1.atif.json")
        + '"}\n',
        encoding="utf-8",
    )
    (run_dir / "traces" / "results.jsonl").write_text(
        '{"trace_id":"t1","score":1.0}\n',
        encoding="utf-8",
    )
    (trace_dir / "trajectory-t1.atif.json").write_text(
        '{"schema_version":"ATIF-v1.7","session_id":"run-123","trajectory_id":"t1","agent":{},"steps":[],"final_metrics":{},"extra":{"case_id":"case-1","scenario_id":"scenario-1"}}',
        encoding="utf-8",
    )

    rows = manager.get_run_traces("run-123")
    result_rows = manager.get_run_trace_results("run-123")
    output = manager.export_traces("run-123", output=tmp_path / "harbor-jobs")

    assert rows[0]["trace_id"] == "t1"
    assert rows[0]["case_id"] == "case-1"
    assert result_rows == [{"trace_id": "t1", "score": 1.0}]
    assert (output / "netopsbench-run-123" / "scenario-1__case-1__t1" / "agent" / "trajectory.json").exists()


def test_benchmark_report_save_and_load_roundtrip(tmp_path):
    from netopsbench.sdk.reports import BenchmarkReport

    report = BenchmarkReport(
        id="report-123",
        summary={"score": 1.0, "cases": 4},
        scenario_summaries=[],
        detailed_results=[],
        artifact_paths={},
        raw={"status": "completed"},
    )
    report_path = tmp_path / "report.json"

    saved_path = report.save(report_path)
    loaded = BenchmarkReport.load(report_path)

    assert saved_path is None
    assert loaded.id == "report-123"
    assert loaded.summary == {"score": 1.0, "cases": 4}
    assert loaded.raw == {"status": "completed"}


def test_benchmark_report_load_raises_clear_errors(tmp_path):
    from netopsbench.sdk.reports import BenchmarkReport

    report_path = tmp_path / "missing.json"

    try:
        BenchmarkReport.load(report_path)
    except FileNotFoundError as exc:
        assert "report file not found" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError for missing report")

    report_path.write_text("{not-json", encoding="utf-8")

    try:
        BenchmarkReport.load(report_path)
    except ValueError as exc:
        assert "Invalid report JSON" in str(exc)
    else:
        raise AssertionError("expected ValueError for malformed report JSON")


def test_benchmark_report_load_validates_basic_shape(tmp_path):
    from netopsbench.sdk.reports import BenchmarkReport

    report_path = tmp_path / "bad-shape.json"
    report_path.write_text('{"summary": []}', encoding="utf-8")

    try:
        BenchmarkReport.load(report_path)
    except ValueError as exc:
        assert "Report payload must contain string id and object summary" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid report shape")

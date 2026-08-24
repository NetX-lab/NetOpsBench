from types import SimpleNamespace

from examples.agents.diagnostic_harness.telemetry.logging import CaseTraceWriter


def test_trace_writer_derives_unique_runtime_from_worker_topology_directory(tmp_path):
    context = SimpleNamespace(
        scenario_id="opaque-case",
        metadata={
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": "/work/.netopsbench/runtimes/run-unique-runtime/worker-1"}
        },
    )

    path = CaseTraceWriter(tmp_path).write(context=context, payload={"ok": True})

    assert path.parent == tmp_path / "run-unique-runtime"
    assert path.name.startswith("trace-")
    assert path.suffix == ".json"
    assert path.exists()

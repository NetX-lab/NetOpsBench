from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from types import SimpleNamespace

import pytest

from netopsbench.platform.pingmesh import _detector_query
from netopsbench.platform.pingmesh._detector_query import DetectorQueryMixin, SnapshotQueryResult


class _Detector(DetectorQueryMixin):
    def __init__(self, clients: list[str]):
        self.bucket = "runtime-bucket"
        self.influxdb_url = "http://influxdb"
        self.token = "token"
        self.org = "org"
        self.topology_id = "runtime-topology"
        self.plan_id = "current-plan"
        self._pingmesh_clients = clients
        self.client_to_leaf: dict[str, str] = {}


@pytest.mark.parametrize("scale", ["xs", "fat-tree-k8"])
def test_topology_mapping_uses_canonical_attached_switch(tmp_path, scale):
    from netopsbench.platform.pingmesh.detector import AnomalyDetector
    from netopsbench.platform.topology.generator import generate_topology

    metadata = generate_topology(scale, str(tmp_path))["metadata"]
    detector = AnomalyDetector("http://influxdb", "token", "org", "bucket", topology_metadata=metadata)
    clients = [device for device in metadata["devices"] if device["role"] == "client"]
    assert detector.client_to_leaf == {client["name"]: client["attached_switch"] for client in clients}
    assert detector.client_to_leaf
    if scale == "fat-tree-k8":
        roles = {device["name"]: device["role"] for device in metadata["devices"]}
        assert {roles[switch] for switch in detector.client_to_leaf.values()} == {"edge"}
    from netopsbench.platform.toolkit.toolkit import AgentToolkit

    toolkit = AgentToolkit(topology_metadata=metadata)
    assert toolkit._bgp_device_roles() == {
        device["name"]: device["role"] for device in metadata["devices"] if device["role"] != "client"
    }


def test_snapshot_query_uses_bounded_pushdown_source_shards(monkeypatch):
    queries: list[str] = []
    worker_counts: list[int] = []

    def query_flux(_url, _token, _org, query, *, timeout):
        queries.append(query)
        assert timeout == 60
        return SimpleNamespace(status="ok", text="", error=None)

    def executor_factory(*, max_workers):
        worker_counts.append(max_workers)
        return RealThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(_detector_query, "query_flux", query_flux)
    monkeypatch.setattr(_detector_query, "ThreadPoolExecutor", executor_factory)

    result = _Detector([f"client{index}" for index in range(1, 66)])._query_snapshot(
        "2026-07-20T00:00:00Z",
        "2026-07-20T00:01:00Z",
    )

    assert result.status == "ok"
    assert len(queries) == 5
    assert worker_counts == [4]
    assert all('r.plan_id == "current-plan"' in query for query in queries)
    assert all("contains(" not in query for query in queries)
    assert all('r.topology_id == "runtime-topology"' in query for query in queries)
    assert sorted(len(re.findall(r'r\.src_name == "client\d+"', query)) for query in queries) == [1, 16, 16, 16, 16]
    assert result.request_count == 5


def test_k18_two_closed_windows_require_42_first_pass_requests(monkeypatch):
    from netopsbench.platform.pingmesh._detector_ingestion import DetectorIngestionMixin

    class ClosedDetector(DetectorIngestionMixin, _Detector):
        _pingmesh_plan = SimpleNamespace(settings=SimpleNamespace(coverage_epoch_seconds=36))

        def _boundary_padding_seconds(self):
            return 6

        def _slice_rows(self, rows, start, end):
            return rows

        def summarize_coverage(self, rows):
            return {"coverage_status": "complete"}

        def audit_ingestion(self, *args):
            return {"status": "ready"}

    requests = []

    def query(*args, **kwargs):
        requests.append(args[3])
        return SimpleNamespace(status="ok", text="", error=None)

    monkeypatch.setattr(_detector_query, "query_flux", query)
    detector = ClosedDetector([f"client{i}" for i in range(1, 325)])
    for start, end in [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:42Z"),
        ("2026-01-01T00:01:00Z", "2026-01-01T00:01:42Z"),
    ]:
        snapshot, coverage = detector._query_covered_snapshot(start, end)
        assert snapshot.status == "ok"
        assert coverage["ingestion"]["query_requests"] == 21
    assert len(requests) == 42


def test_snapshot_query_merges_every_shard_in_stable_order(monkeypatch):
    detector = _Detector([f"client{index}" for index in range(1, 18)])

    def query_shard(_start, _end, sources):
        return SnapshotQueryResult(
            status="ok",
            rows=[
                {
                    "_time": "2026-07-20T00:00:02Z" if sources[0] == "client1" else "2026-07-20T00:00:01Z",
                    "src_name": sources[0],
                    "dst_name": "client99",
                    "src_ip": "10.0.0.1",
                    "dst_ip": "10.0.0.99",
                }
            ],
        )

    monkeypatch.setattr(detector, "_query_snapshot_shard", query_shard)

    result = detector._query_snapshot("start", "end")

    assert result.status == "ok"
    assert [row["src_name"] for row in result.rows] == ["client17", "client1"]


def test_snapshot_query_discards_partial_rows_when_any_shard_fails(monkeypatch):
    detector = _Detector([f"client{index}" for index in range(1, 18)])

    def query_shard(_start, _end, sources):
        if sources[0] == "client17":
            return SnapshotQueryResult(status="error", rows=[], error="influx timeout")
        return SnapshotQueryResult(status="ok", rows=[{"src_name": "client1"}])

    monkeypatch.setattr(detector, "_query_snapshot_shard", query_shard)

    result = detector._query_snapshot("start", "end")

    assert result.status == "error"
    assert result.rows == []
    assert result.error == "snapshot shard 2/2 failed: influx timeout"


def test_baseline_and_current_reads_overlap_and_remain_fresh(monkeypatch, tmp_path):
    import threading

    from netopsbench.platform.pingmesh.detector import AnomalyDetector
    from netopsbench.platform.topology.generator import generate_topology

    metadata = generate_topology("xs", str(tmp_path))["metadata"]
    detector = AnomalyDetector("http://test", "token", "org", "bucket", topology_metadata=metadata)
    barrier = threading.Barrier(2)
    reads = []

    def query(start, end):
        reads.append((start, end))
        barrier.wait(timeout=5)
        return SnapshotQueryResult(status="error", rows=[], error="test incomplete"), {}

    monkeypatch.setattr(detector, "_query_covered_snapshot", query)
    for _ in range(2):
        report = detector.generate_windowed_anomaly_report(
            baseline_start="b1", baseline_end="b2", current_start="c1", current_end="c2", windows=[]
        )
        assert report["query_status"]["ok"] is False
    assert reads.count(("b1", "b2")) == 2
    assert reads.count(("c1", "c2")) == 2


@pytest.mark.parametrize("client_count", [1, 128])
def test_expired_snapshot_budget_never_starts_http(monkeypatch, client_count):
    def unexpected(*_args, **_kwargs):
        pytest.fail("expired snapshot started an HTTP request")

    monkeypatch.setattr(_detector_query, "query_flux", unexpected)
    result = _Detector([f"client{i}" for i in range(client_count)])._query_snapshot(
        "start", "end", deadline=time.monotonic() - 1
    )
    assert result.status == "error"
    assert result.rows == []
    assert "deadline exceeded" in result.error


def test_queued_snapshot_shards_share_deadline_and_stop_http(monkeypatch):
    calls = []
    ready = threading.Barrier(4)
    deadline = time.monotonic() + 0.08

    def query_flux(*_args, timeout, deadline):
        calls.append(deadline)
        ready.wait(timeout=1)
        time.sleep(max(0, deadline - time.monotonic()))
        return SimpleNamespace(status="error", text="", error="query deadline exceeded")

    monkeypatch.setattr(_detector_query, "query_flux", query_flux)
    started = time.monotonic()
    result = _Detector([f"client{i}" for i in range(128)])._query_snapshot("start", "end", deadline=deadline)
    assert result.status == "error"
    assert result.rows == []
    assert calls == [deadline] * 4
    assert time.monotonic() - started < 0.5


def test_parallel_baseline_and_current_queries_receive_same_deadline(monkeypatch):
    from netopsbench.platform.pingmesh.detector import AnomalyDetector

    detector = object.__new__(AnomalyDetector)
    budgets = []

    def query(_start, _end, *, deadline):
        budgets.append(deadline)
        return SnapshotQueryResult(status="error", rows=[], error="deadline exceeded"), {}

    monkeypatch.setattr(detector, "_query_covered_snapshot", query)
    monkeypatch.setattr(detector, "_build_report", lambda **kwargs: kwargs)
    deadline = time.monotonic() + 1
    report = detector.generate_windowed_anomaly_report(
        baseline_start="b1", baseline_end="b2", current_start="c1", current_end="c2", windows=[], deadline=deadline
    )
    assert report["query_status"]["ok"] is False
    assert budgets == [deadline, deadline]


def test_snapshot_csv_honors_reordered_table_headers():
    text = (
        ",result,table,_time,src_name,dst_name,probe_cycle,port_batch_index\n"
        ",_result,0,2026-10-24T00:00:00Z,client1,client2,6,0\n"
        ",result,table,_time,port_batch_index,probe_cycle,dst_name,src_name\n"
        ",_result,1,2026-10-24T00:00:01Z,1,7,client3,client1\n"
    )
    rows = _Detector([])._parse_snapshot_csv(text)
    assert [(r["src_name"], r["dst_name"], r["probe_cycle"], r["port_batch_index"]) for r in rows] == [
        ("client1", "client2", 6, 0),
        ("client1", "client3", 7, 1),
    ]

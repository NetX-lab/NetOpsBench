from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from types import SimpleNamespace

from netopsbench.platform.pingmesh import _detector_query
from netopsbench.platform.pingmesh._detector_query import DetectorQueryMixin, SnapshotQueryResult


class _Detector(DetectorQueryMixin):
    def __init__(self, clients: list[str]):
        self.bucket = "runtime-bucket"
        self.influxdb_url = "http://influxdb"
        self.token = "token"
        self.org = "org"
        self.topology_id = "runtime-topology"
        self._pingmesh_clients = clients


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
    assert all("contains(" not in query for query in queries)
    assert all('r.topology_id == "runtime-topology"' in query for query in queries)
    assert sorted(len(re.findall(r'r\.src_name == "client\d+"', query)) for query in queries) == [1, 16, 16, 16, 16]


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

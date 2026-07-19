from __future__ import annotations

import urllib.parse

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.observability import influxdb
from netopsbench.platform.observability import lifecycle as observability_lifecycle


def test_pingmesh_task_flux_uses_fixed_30_second_topology_aggregate():
    flux = influxdb.build_pingmesh_aggregate_task_flux(
        name="netopsbench-demo-w01-pingmesh-leaf-pair-v1",
        bucket="worker-bucket",
        org="netopsbench",
        topology_id="demo-topology",
        measurement="pingmesh_leaf_pair_30s",
        dimensions=("src_leaf", "dst_leaf"),
    )

    assert 'import "date"' in flux
    assert 'option task = {name: "netopsbench-demo-w01-pingmesh-leaf-pair-v1", every: 10s}' in flux
    assert "windowStop = date.truncate(t: now(), unit: 30s)" in flux
    assert "windowStart = date.sub(d: 2m, from: windowStop)" in flux
    assert "|> range(start: windowStart, stop: windowStop)" in flux
    assert 'r._measurement == "pingmesh"' in flux
    assert 'r.topology_id == "demo-topology"' in flux
    assert '|> group(columns: ["topology_id", "src_leaf", "dst_leaf", "_field"])' in flux
    assert 'aggregateWindow(every: 30s, fn: mean, createEmpty: false, timeSrc: "_stop")' in flux
    assert '|> set(key: "_measurement", value: "pingmesh_leaf_pair_30s")' in flux
    assert 'tagColumns: ["topology_id", "src_leaf", "dst_leaf"]' in flux
    assert "hotspot_score" in flux
    assert "float(v: r.packet_loss) * 1000000000000.0" in flux


def test_ensure_influx_task_creates_then_updates_named_task(monkeypatch):
    create_calls = []

    def create_request(url, token, method="GET", payload=None):
        create_calls.append((url, method, payload))
        if "/api/v2/tasks?name=" in url:
            return {"tasks": []}
        if "/api/v2/orgs?org=" in url:
            return {"orgs": [{"id": "org-id"}]}
        if method == "POST":
            return {"id": "task-id"}
        raise AssertionError((url, method, payload))

    monkeypatch.setattr(influxdb, "_request", create_request)

    assert influxdb.ensure_influx_task("http://influx:8086", "token", "org", "task-name", "flux-v1") == "task-id"
    assert create_calls[-1][1:] == ("POST", {"flux": "flux-v1", "status": "active", "orgID": "org-id"})

    update_calls = []

    def update_request(url, token, method="GET", payload=None):
        update_calls.append((url, method, payload))
        if "/api/v2/tasks?name=" in url:
            return {"tasks": [{"id": "task-id", "name": "task-name"}]}
        if method == "PATCH":
            return {"id": "task-id"}
        raise AssertionError((url, method, payload))

    monkeypatch.setattr(influxdb, "_request", update_request)

    assert influxdb.ensure_influx_task("http://influx:8086", "token", "org", "task-name", "flux-v2") == "task-id"
    assert update_calls[-1][1:] == ("PATCH", {"flux": "flux-v2", "status": "active"})


def test_delete_pingmesh_tasks_removes_both_worker_tasks(monkeypatch):
    names = influxdb.pingmesh_aggregate_task_names("Demo Runtime", 2)
    calls = []

    def fake_request(url, token, method="GET", payload=None):
        calls.append((url, method))
        if method == "GET":
            query_name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["name"][0]
            return {"tasks": [{"id": f"id-{len(calls)}", "name": query_name}]}
        return {}

    monkeypatch.setattr(influxdb, "_request", fake_request)

    deleted = influxdb.delete_pingmesh_aggregate_tasks(
        "http://influx:8086",
        "token",
        runtime_id="Demo Runtime",
        worker_index=2,
    )

    assert len(deleted) == 2
    assert all(
        any(urllib.parse.quote(name, safe="") in url for url, method in calls if method == "GET") for name in names
    )
    assert sum(method == "DELETE" for _, method in calls) == 2


def test_worker_observability_continues_when_tasks_api_is_unavailable(monkeypatch, tmp_path):
    worker = RuntimeIdentity.create(
        runtime_id="demo-runtime",
        worker_id="worker-1",
        worker_index=1,
        lab_name="demo-lab",
        topology_dir=tmp_path,
        mgmt_subnet="172.31.100.0/24",
        mgmt_network="demo-network",
    )
    reconciled = []
    monkeypatch.setattr(observability_lifecycle, "ensure_observability_core", lambda: None)
    monkeypatch.setattr(observability_lifecycle, "ensure_bucket", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        observability_lifecycle,
        "ensure_pingmesh_aggregate_tasks",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tasks disabled")),
    )
    monkeypatch.setattr(observability_lifecycle, "safe_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        observability_lifecycle,
        "ensure_worker_bgp_collector",
        lambda value: reconciled.append(("bgp", value.lab_name)),
    )
    monkeypatch.setattr(
        observability_lifecycle,
        "ensure_worker_telegraf",
        lambda value: reconciled.append(("telegraf", value.lab_name)),
    )

    observability_lifecycle.ensure_worker_observability(worker)

    assert reconciled == [("bgp", "demo-lab"), ("telegraf", "demo-lab")]

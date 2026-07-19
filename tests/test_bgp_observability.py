from __future__ import annotations

import subprocess

from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.generator import generate_topology


def _toolkit(tmp_path) -> AgentToolkit:
    metadata = generate_topology("xs", str(tmp_path))["metadata"]
    return AgentToolkit(topology_metadata=metadata)


def _complete_index_rows(devices):
    rows = []
    for device in devices:
        rows.extend(
            [
                {"result": "index_first", "source": device, "_time": "2026-07-11T00:00:01Z", "_value": 1},
                {"result": "index_last", "source": device, "_time": "2026-07-11T00:00:29Z", "_value": 1},
                {"result": "index_count", "source": device, "_value": 3},
            ]
        )
    return rows


def test_query_bgp_events_classifies_session_transitions(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    rows = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:00Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "ESTABLISHED",
            "prefixes_received": 4,
        },
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "IDLE",
        },
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:20Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "ESTABLISHED",
            "prefixes_received": 3,
        },
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:20Z", "source": "leaf1", "collection_ok": True},
    ]
    monkeypatch.setattr(
        toolkit,
        "_query_influx_rows",
        lambda query, **kwargs: rows[:1] if "range(start: -30d" in query else rows[1:],
    )

    result = toolkit.query_bgp_events(start_time="2026-07-11T00:00:05Z", end_time="2026-07-11T00:00:30Z")

    assert result.success is True
    event = result.data["events"][0]
    assert event["event_type"] == "session_flap"
    assert event["previous_state"] == "ESTABLISHED"
    assert event["latest_state"] == "ESTABLISHED"
    assert event["states_observed"] == ["IDLE", "ESTABLISHED"]


def test_query_bgp_events_reports_non_established_and_collection_gap(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    rows = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "IDLE",
        },
        {
            "_measurement": "bgp_collection",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf2",
            "collection_ok": False,
            "error_type": "timeout",
        },
    ]
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda *a, **k: rows)

    result = toolkit.query_bgp_events(start_time="2026-07-11T00:00:00Z", end_time="2026-07-11T00:00:30Z")

    kinds = {(event["device"], event["event_type"]) for event in result.data["events"]}
    assert ("leaf1", "non_established_observed") in kinds
    assert ("leaf2", "collection_gap") in kinds


def test_query_bgp_events_classifies_down_and_recovery(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    prior = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-10T23:59:50Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "ESTABLISHED",
        }
    ]
    window = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "session_state": "IDLE",
        },
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "leaf1", "collection_ok": True},
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "leaf2", "collection_ok": True},
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "spine1", "collection_ok": True},
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": "spine2", "collection_ok": True},
    ]
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kwargs: prior if "-30d" in query else window)

    down = toolkit.query_bgp_events(start_time="2026-07-11T00:00:00Z", end_time="2026-07-11T00:00:30Z")
    assert down.data["events"][0]["event_type"] == "session_down"

    prior[0]["session_state"] = "IDLE"
    window[0]["session_state"] = "ESTABLISHED"
    recovered = toolkit.query_bgp_events(
        start_time="2026-07-11T00:00:00Z", end_time="2026-07-11T00:00:30Z", state="all"
    )
    assert recovered.data["events"][0]["event_type"] == "session_recovered"


def test_query_bgp_events_filters_role_and_limit(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    rows = [
        {
            "_measurement": "bgp_neighbors",
            "_time": "2026-07-11T00:00:10Z",
            "source": leaf,
            "neighbor_address": f"10.0.0.{index}",
            "session_state": "IDLE",
        }
        for index, leaf in enumerate(("leaf1", "leaf2"), 1)
    ]
    rows += [
        {"_measurement": "bgp_collection", "_time": "2026-07-11T00:00:10Z", "source": device, "collection_ok": True}
        for device in ("spine1", "spine2", "leaf1", "leaf2")
    ]
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda *args, **kwargs: rows)

    result = toolkit.query_bgp_events(time_range_minutes=10, role="leaf", limit=1)

    assert result.success is True
    assert result.data["returned_events"] == 1
    assert result.data["truncated"] is True
    assert result.data["events"][0]["role"] == "leaf"


def test_query_bgp_events_query_is_centralized_and_topology_scoped(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    queries = []
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kwargs: queries.append(query) or [])
    monkeypatch.setattr(
        toolkit, "_docker_exec", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live query"))
    )

    result = toolkit.query_bgp_events(time_range_minutes=10, device="leaf1", peer="10.0.0.1")

    assert result.success is True
    assert len(queries) == 3
    assert 'r._measurement == "bgp_event_index"' in queries[0]
    assert f'r.topology_id == "{toolkit.topology_id}"' in queries[1]
    assert 'r._measurement == "bgp_neighbors"' in queries[1]
    assert 'r._measurement == "bgp_collection"' in queries[2]
    assert 'r.source == "leaf1"' in queries[1]
    assert 'r.neighbor_address == "10.0.0.1"' in queries[1]


def test_query_bgp_events_uses_transition_fast_path_when_index_covers_window(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    queries = []
    fast_rows = [
        {
            "result": "first_state",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_time": "2026-07-11T00:00:10Z",
            "_value": "IDLE",
        },
        {
            "result": "last_state",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_time": "2026-07-11T00:00:20Z",
            "_value": "ESTABLISHED",
        },
        {"result": "state_count", "source": "leaf1", "neighbor_address": "10.0.0.1", "_value": 5},
        {
            "result": "latest_field",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_field": "asn",
            "_time": "2026-07-11T00:00:20Z",
            "_value": 65100,
        },
        {
            "result": "latest_field",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_field": "prefixes_received",
            "_time": "2026-07-11T00:00:20Z",
            "_value": 3,
        },
        {
            "result": "prior_field",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_field": "session_state",
            "_time": "2026-07-10T23:59:50Z",
            "_value": "ESTABLISHED",
        },
        {
            "result": "prior_field",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_field": "prefixes_received",
            "_time": "2026-07-10T23:59:50Z",
            "_value": 4,
        },
        {
            "result": "session_events",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "event_type": "session_down",
            "_time": "2026-07-11T00:00:10Z",
            "previous_state": "ESTABLISHED",
            "latest_state": "IDLE",
        },
        {
            "result": "session_events",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "event_type": "session_recovered",
            "_time": "2026-07-11T00:00:20Z",
            "previous_state": "IDLE",
            "latest_state": "ESTABLISHED",
        },
        {"result": "collection_first", "source": "leaf1", "_time": "2026-07-11T00:00:01Z", "_value": True},
        {
            "result": "collection_last",
            "source": "leaf1",
            "_field": "collection_ok",
            "_time": "2026-07-11T00:00:29Z",
            "_value": True,
        },
        {"result": "collection_count", "source": "leaf1", "_value": 3},
    ]

    def fake_query(query, **kwargs):
        queries.append(query)
        if 'r._measurement == "bgp_event_index"' in query:
            return _complete_index_rows(["leaf1"])
        return fast_rows

    monkeypatch.setattr(toolkit, "_query_influx_rows", fake_query)

    result = toolkit.query_bgp_events(
        start_time="2026-07-11T00:00:00Z",
        end_time="2026-07-11T00:00:30Z",
        device="leaf1",
        peer="10.0.0.1",
    )

    assert result.success is True
    assert len(queries) == 3
    event = result.data["events"][0]
    assert event["event_type"] == "session_flap"
    assert event["sample_count"] == 5
    assert event["prefixes_before"] == 4
    assert event["prefixes_after"] == 3
    assert event["states_observed"] == ["IDLE", "ESTABLISHED"]
    assert all('r.source == "leaf1"' in query for query in queries[1:])
    assert all('r.neighbor_address == "10.0.0.1"' in query for query in queries[1:])
    prior_section = queries[2].split("prior =", 1)[1]
    assert '|> group(columns: ["source", "neighbor_address", "_field"])' in prior_section
    assert "|> pivot" not in prior_section


def test_query_bgp_events_fast_path_pushes_role_filter_and_limits(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    queries = []
    rows = []
    for index, leaf in enumerate(("leaf1", "leaf2"), 1):
        rows.extend(
            [
                {
                    "result": "first_state",
                    "source": leaf,
                    "neighbor_address": f"10.0.0.{index}",
                    "_time": "2026-07-11T00:00:10Z",
                    "_value": "IDLE",
                },
                {
                    "result": "last_state",
                    "source": leaf,
                    "neighbor_address": f"10.0.0.{index}",
                    "_time": "2026-07-11T00:00:20Z",
                    "_value": "IDLE",
                },
                {"result": "state_count", "source": leaf, "neighbor_address": f"10.0.0.{index}", "_value": 2},
                {"result": "collection_first", "source": leaf, "_time": "2026-07-11T00:00:01Z", "_value": True},
                {
                    "result": "collection_last",
                    "source": leaf,
                    "_field": "collection_ok",
                    "_time": "2026-07-11T00:00:29Z",
                    "_value": True,
                },
                {"result": "collection_count", "source": leaf, "_value": 3},
            ]
        )

    def fake_query(query, **kwargs):
        queries.append(query)
        return _complete_index_rows(["leaf1", "leaf2"]) if "bgp_event_index" in query else rows

    monkeypatch.setattr(toolkit, "_query_influx_rows", fake_query)

    result = toolkit.query_bgp_events(
        start_time="2026-07-11T00:00:00Z",
        end_time="2026-07-11T00:00:30Z",
        role="leaf",
        limit=1,
    )

    assert result.success is True
    assert result.data["returned_events"] == 1
    assert result.data["truncated"] is True
    assert all('contains(value: r.source, set: ["leaf1", "leaf2"])' in query for query in queries)


def test_query_bgp_events_healthy_fast_path_skips_global_session_details(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    queries = []
    collection_rows = [
        {"result": "collection_first", "source": "leaf1", "_time": "2026-07-11T00:00:01Z", "_value": True},
        {
            "result": "collection_last",
            "source": "leaf1",
            "_field": "collection_ok",
            "_time": "2026-07-11T00:00:29Z",
            "_value": True,
        },
        {"result": "collection_count", "source": "leaf1", "_value": 3},
    ]

    def fake_query(query, **kwargs):
        queries.append(query)
        return _complete_index_rows(["leaf1"]) if "bgp_event_index" in query else collection_rows

    monkeypatch.setattr(toolkit, "_query_influx_rows", fake_query)

    result = toolkit.query_bgp_events(
        start_time="2026-07-11T00:00:00Z",
        end_time="2026-07-11T00:00:30Z",
        device="leaf1",
    )

    assert result.success is True
    assert result.data["events"] == []
    assert len(queries) == 2
    assert "neighbors =" not in queries[1]


def test_bgp_fast_event_uses_later_missing_transition_as_latest_state(tmp_path):
    toolkit = _toolkit(tmp_path)
    scope = toolkit._resolve_pingmesh_time_scope(
        10,
        "2026-07-11T00:00:00Z",
        "2026-07-11T00:00:30Z",
    )
    rows = [
        {
            "result": "first_state",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_time": "2026-07-11T00:00:10Z",
            "_value": "ESTABLISHED",
        },
        {
            "result": "last_state",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_time": "2026-07-11T00:00:10Z",
            "_value": "ESTABLISHED",
        },
        {"result": "state_count", "source": "leaf1", "neighbor_address": "10.0.0.1", "_value": 1},
        {
            "result": "session_events",
            "source": "leaf1",
            "neighbor_address": "10.0.0.1",
            "_time": "2026-07-11T00:00:20Z",
            "previous_state": "ESTABLISHED",
            "latest_state": "MISSING",
        },
        {"result": "collection_first", "source": "leaf1", "_time": "2026-07-11T00:00:01Z", "_value": True},
        {
            "result": "collection_last",
            "source": "leaf1",
            "_field": "collection_ok",
            "_time": "2026-07-11T00:00:29Z",
            "_value": True,
        },
        {"result": "collection_count", "source": "leaf1", "_value": 3},
    ]

    events = toolkit._build_bgp_events_fast(
        rows,
        scope,
        toolkit._bgp_device_roles(),
        "leaf1",
        "10.0.0.1",
        None,
        "non_established",
    )

    assert events[0]["event_type"] == "session_down"
    assert events[0]["latest_state"] == "MISSING"
    assert events[0]["last_seen"] == "2026-07-11T00:00:20Z"


def test_query_bgp_events_propagates_influx_failure(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    result = toolkit.query_bgp_events(time_range_minutes=10)

    assert result.success is False
    assert "down" in result.error


def test_get_bgp_neighbor_only_queries_requested_device_and_peer(monkeypatch, tmp_path):
    toolkit = _toolkit(tmp_path)
    calls = []

    def fake_exec(container, args, timeout):
        calls.append((container, args))
        if "neighbors" in args[-1]:
            stdout = "BGP neighbor is 10.0.0.1, remote AS 65100, local AS 65001\n  BGP state = Idle\n  Last reset due to Bad Peer AS\n"
        else:
            stdout = "Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd\n10.0.0.1 4 65100 3 4 0 0 0 never Idle\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(toolkit, "_docker_exec", fake_exec)
    result = toolkit.get_bgp_neighbor("leaf1", "10.0.0.1")

    assert result.success is True
    assert result.data["state"] == "Idle"
    assert result.data["peer_as"] == 65100
    assert "Bad Peer AS" in result.data["last_reset"]
    assert all(call[0].endswith("leaf1") for call in calls)
    assert all("10.0.0.1" in call[1][-1] or "summary" in call[1][-1] for call in calls)

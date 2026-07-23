"""Loopback unit tests for the UDP cycle event loop and responder."""

from __future__ import annotations

import json
import socket
import struct
import time

import pytest

from netopsbench.platform.pingmesh._agent_probe import _HEADER_FMT, UdpProbeMixin
from netopsbench.platform.pingmesh._agent_responder import UdpEchoResponder
from netopsbench.platform.pingmesh._agent_runtime import _next_cycle_deadline
from netopsbench.platform.pingmesh._detector_analysis import _loss_stats
from netopsbench.platform.pingmesh.agent import (
    PingmeshAgent,
    _deterministic_startup_jitter_seconds,
    _probe_batch_indices,
    _rotating_destination_batch,
)


class _Harness(UdpProbeMixin):
    """Bare host class wiring the attrs UdpProbeMixin reads."""

    def __init__(
        self,
        dst_port: int,
        *,
        n_rtt_ports: int = 8,
        rtt_src_port_base: int = 34000,
        enable_df_probe: bool = True,
        rtt_ports_per_cycle: int | None = None,
        interval: float = 0.2,
    ):
        self.udp_dst_port = dst_port
        self.n_rtt_ports = n_rtt_ports
        self.rtt_src_port_base = rtt_src_port_base
        self.df_payload_size = 1400
        self.interval = interval
        self.enable_df_probe = enable_df_probe
        self.rtt_ports_per_cycle = rtt_ports_per_cycle if rtt_ports_per_cycle is not None else n_rtt_ports


class _SpyHarness(_Harness):
    def __init__(self, dst_port: int, **kwargs):
        super().__init__(dst_port, **kwargs)
        self.open_calls = []

    def _open_probe_sockets(self, src_ip: str, src_port_base: int, n_ports: int, set_df: bool) -> list:
        self.open_calls.append((src_ip, src_port_base, n_ports, set_df))
        return super()._open_probe_sockets(src_ip, src_port_base, n_ports, set_df)


class _CycleSpyHarness(_Harness):
    def __init__(self, dst_port: int, **kwargs):
        super().__init__(dst_port, **kwargs)
        self.cycle_ports = []

    def _run_udp_cycle(self, probes: list[dict], rtt_sockets: list, enable_df: bool):
        self.cycle_ports.append(([src_port for src_port, _sock in rtt_sockets], enable_df))
        rtt = [
            {"sent": len(rtt_sockets), "received": len(rtt_sockets), "rtts": [0.1], "mtu_drops": 0} for _probe in probes
        ]
        df = [{"sent": int(enable_df), "received": int(enable_df), "rtts": [0.1], "mtu_drops": 0} for _probe in probes]
        return rtt, df


@pytest.fixture()
def loopback_responder():
    # Pick a high port unlikely to clash with system services.
    port = 38434
    responder = UdpEchoResponder(bind_ip="127.0.0.1", port=port)
    responder.start()
    # Give the responder a beat to enter recvfrom().
    time.sleep(0.05)
    try:
        yield port
    finally:
        responder.stop()


@pytest.fixture()
def loopback_responder_set():
    port = 38436
    bind_ips = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]
    responders = []
    for bind_ip in bind_ips:
        responder = UdpEchoResponder(bind_ip=bind_ip, port=port)
        responder.start()
        responders.append(responder)
    time.sleep(0.05)
    try:
        yield port, bind_ips
    finally:
        for responder in responders:
            responder.stop()


def _probe(dst_ip: str, index: int, src_ip: str = "127.0.0.1") -> dict:
    return {
        "src_ip": src_ip,
        "src_name": "client-src",
        "src_rack": "rack-a",
        "src_leaf": "leaf-a",
        "dst_ip": dst_ip,
        "dst_name": f"client-dst-{index}",
        "dst_rack": "rack-b",
        "dst_leaf": "leaf-b",
        "path_type": "cross_rack",
    }


def test_udp_responder_treats_socket_timeout_as_idle(caplog):
    responder = UdpEchoResponder()

    class _Socket:
        @staticmethod
        def recvfrom(_size):
            responder._shutdown_event.set()
            raise socket.timeout("timed out")  # noqa: UP041

    responder._socket = _Socket()
    responder._run()

    assert "UDP responder recv error" not in caplog.text


def test_pingmesh_agent_rejects_incomplete_canonical_policy(tmp_path):
    pinglist = tmp_path / "pinglist.json"
    pinglist.write_text(json.dumps({"probes": [], "pingmesh_policy": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="regenerate the topology"):
        PingmeshAgent(str(pinglist))


def test_udp_cycle_loopback_has_no_loss(loopback_responder):
    port = loopback_responder
    harness = _Harness(dst_port=port)
    try:
        result = harness.udp_probe_cycle([_probe("127.0.0.1", 1)], 0)[0]["result"]
    finally:
        harness._close_udp_probe_sockets()

    assert result["packets_sent"] == harness.n_rtt_ports
    # All packets should round-trip on loopback.
    assert result["packets_lost"] == 0
    assert result["success"] is True
    assert result["rtt_min"] >= 0.0
    assert result["rtt_max"] < 1000.0  # loopback RTT is sub-millisecond
    assert result["loss_pct"] == 0.0


def test_udp_cycle_rtt_and_df_share_one_deadline_without_responder():
    harness = _Harness(dst_port=38435, interval=0.15)
    started = time.monotonic()
    try:
        result = harness.udp_probe_cycle([_probe("127.0.0.1", 1)], 0)[0]["result"]
    finally:
        harness._close_udp_probe_sockets()
    elapsed = time.monotonic() - started

    assert result["packets_sent"] == harness.n_rtt_ports
    assert result["packets_lost"] == harness.n_rtt_ports
    assert result["success"] is False
    assert result["loss_pct"] == pytest.approx(100.0)
    assert result["df_packets_lost"] == 1
    assert elapsed < 0.3


def test_udp_cycle_partial_timeout_only_affects_missing_destination(loopback_responder):
    harness = _Harness(dst_port=loopback_responder, n_rtt_ports=2, enable_df_probe=False)
    probes = [_probe("127.0.0.1", 1), _probe("192.0.2.1", 2)]
    try:
        results = harness.udp_probe_cycle(probes, 0)
    finally:
        harness._close_udp_probe_sockets()

    assert results[0]["result"]["packets_lost"] == 0
    assert results[1]["result"]["packets_lost"] == 2


def test_udp_probe_requires_the_complete_source_port_pool(monkeypatch):
    harness = _Harness(dst_port=38435, n_rtt_ports=4)

    class SocketStub:
        closed = False

        def close(self):
            self.closed = True

    socket_stub = SocketStub()
    monkeypatch.setattr(harness, "_open_probe_sockets", lambda **_kwargs: [(34000, socket_stub)])

    with pytest.raises(RuntimeError, match="complete Pingmesh source-port pool"):
        harness._ensure_udp_probe_sockets("127.0.0.1")

    assert socket_stub.closed is True
    assert harness._udp_rtt_sockets == []


def test_udp_cycle_discards_stale_reply_before_reusing_socket(loopback_responder):
    harness = _Harness(dst_port=loopback_responder, n_rtt_ports=1, enable_df_probe=False)
    rtt_sockets = harness._ensure_udp_probe_sockets("127.0.0.1")
    stale = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        stale_payload = struct.pack(_HEADER_FMT, time.time_ns(), 999_999) + b"stale"
        stale.sendto(stale_payload, ("127.0.0.1", rtt_sockets[0][0]))
        time.sleep(0.01)
        result = harness.udp_probe_cycle([_probe("127.0.0.1", 1)], 0)[0]["result"]
    finally:
        stale.close()
        harness._close_udp_probe_sockets()

    assert result["packets_sent"] == 1
    assert result["packets_lost"] == 0


def test_udp_probe_cycle_fanout_loopback(loopback_responder_set):
    port, bind_ips = loopback_responder_set
    harness = _Harness(
        dst_port=port,
        n_rtt_ports=3,
        rtt_src_port_base=34200,
    )
    probes = [_probe(dst_ip, index) for index, dst_ip in enumerate(bind_ips)]

    try:
        cycle_results = harness.udp_probe_cycle(probes, 0)
    finally:
        harness._close_udp_probe_sockets()

    assert len(cycle_results) == len(probes)
    for item in cycle_results:
        assert item["success"] is True
        result = item["result"]
        assert result["packets_sent"] == harness.n_rtt_ports
        assert result["packets_lost"] == 0
        assert result["loss_pct"] == 0.0
        assert result["df_success"] == 1
        assert result["df_loss_pct"] == 0.0
        assert result["df_mtu_drops"] == 0


def test_udp_probe_cycle_reuses_bound_source_ports(loopback_responder):
    port = loopback_responder
    harness = _SpyHarness(
        dst_port=port,
        n_rtt_ports=2,
        rtt_src_port_base=34400,
    )
    probes = [_probe("127.0.0.1", 1)]

    try:
        first = harness.udp_probe_cycle(probes, 0)
        second = harness.udp_probe_cycle(probes, 0)
    finally:
        harness._close_udp_probe_sockets()

    assert first[0]["result"]["packets_lost"] == 0
    assert second[0]["result"]["packets_lost"] == 0
    assert harness.open_calls == [
        ("127.0.0.1", harness.rtt_src_port_base, harness.n_rtt_ports, True),
    ]


def test_udp_probe_cycle_sequence_maps_shared_dst_probes(loopback_responder):
    port = loopback_responder
    harness = _Harness(
        dst_port=port,
        n_rtt_ports=3,
        rtt_src_port_base=34600,
        enable_df_probe=False,
    )
    probes = [_probe("127.0.0.1", index) for index in range(3)]

    try:
        cycle_results = harness.udp_probe_cycle(probes, 0)
    finally:
        harness._close_udp_probe_sockets()

    assert len(cycle_results) == len(probes)
    for item in cycle_results:
        result = item["result"]
        assert result["packets_sent"] == harness.n_rtt_ports
        assert result["packets_lost"] == 0
        assert result["df_success"] == 0
        assert result["df_loss_pct"] == 0.0


def test_udp_probe_cycle_rotates_active_source_ports():
    harness = _CycleSpyHarness(
        dst_port=38437,
        n_rtt_ports=4,
        rtt_ports_per_cycle=2,
        rtt_src_port_base=34800,
    )
    probes = [_probe("127.0.0.1", 1)]

    try:
        first = harness.udp_probe_cycle(probes, 0)
        second = harness.udp_probe_cycle(probes, 1)
    finally:
        harness._close_udp_probe_sockets()

    assert harness.cycle_ports == [
        ([34800, 34801], True),
        ([34802, 34803], True),
    ]
    assert first[0]["result"]["packets_sent"] == 2
    assert first[0]["result"]["rtt_ports_active"] == 2
    assert first[0]["result"]["rtt_ports_total"] == 4
    assert first[0]["result"]["df_packets_sent"] == 1
    assert first[0]["result"]["df_ports_active"] == 1
    assert first[0]["result"]["df_ports_total"] == 4
    assert second[0]["result"]["packets_sent"] == 2
    assert second[0]["result"]["df_packets_sent"] == 1


def test_df_probe_reuses_one_active_rtt_tuple_per_destination():
    sockets = [(33000 + index, object()) for index in range(4)]
    probes = [_probe("192.0.2.1", index) for index in range(6)]

    queue = UdpProbeMixin._build_send_queue(probes, sockets, enable_df=True)

    for probe_index in range(len(probes)):
        rtt_entries = [item for item in queue if item[0] == "rtt" and item[1] == probe_index]
        df_entries = [item for item in queue if item[0] == "df" and item[1] == probe_index]
        assert len(rtt_entries) == 4
        assert len(df_entries) == 1
        assert df_entries[0][2] is sockets[probe_index % len(sockets)][1]


def test_pingmesh_startup_jitter_is_deterministic():
    first = _deterministic_startup_jitter_seconds("client-17", 1.0)
    second = _deterministic_startup_jitter_seconds("client-17", 1.0)

    assert first == second
    assert 0.0 <= first < 1.0
    assert _deterministic_startup_jitter_seconds("client-17", 0.0) == 0.0


def test_pingmesh_startup_jitter_spreads_sequential_client_names():
    bins = {int(_deterministic_startup_jitter_seconds(f"client{index}", 2.0) * 10) for index in range(1, 145)}

    assert len(bins) >= 16


def test_fixed_rate_cycle_deadline_recovers_one_overrun():
    deadline = _next_cycle_deadline(0.0, 2.0, 2.1)

    assert deadline == 2.0
    assert max(0.0, deadline - 2.1) == 0.0

    deadline = _next_cycle_deadline(deadline, 2.0, 3.1)

    assert deadline == 4.0
    assert max(0.0, deadline - 3.1) == pytest.approx(0.9)


def test_fixed_rate_cycle_deadline_drops_excessive_backlog():
    assert _next_cycle_deadline(0.0, 2.0, 5.0) == 5.0


def test_k12_destination_and_port_batches_cover_joint_epoch():
    tasks = [{"dst_name": f"client{i}"} for i in range(1, 144)]
    phase = 16
    combinations = set()
    destinations = set()
    for cycle in range(36):
        destination_batch, port_batch = _probe_batch_indices(cycle, 9, 4)
        active = _rotating_destination_batch(tasks, 16, destination_batch, phase)
        assert 15 <= len(active) <= 16
        destinations.update(item["dst_name"] for item in active)
        combinations.add((destination_batch, port_batch))

    assert len(destinations) == 143
    assert len(combinations) == 36


def test_k8_and_xlarge_cover_all_destination_port_batch_pairs():
    combinations = {_probe_batch_indices(cycle, 8, 4) for cycle in range(32)}

    assert combinations == {(destination, port) for destination in range(8) for port in range(4)}


def test_destination_rotation_is_deterministic_and_bounded():
    tasks = [{"dst_name": f"client{i}"} for i in range(1, 40)]
    first = [_rotating_destination_batch(tasks, 16, cycle) for cycle in range(3)]
    second = [_rotating_destination_batch(tasks, 16, cycle) for cycle in range(3)]

    assert first == second
    assert [len(batch) for batch in first] == [13, 13, 13]
    assert {item["dst_name"] for batch in first for item in batch} == {item["dst_name"] for item in tasks}


def test_destination_slot_phase_balances_incoming_probe_load():
    client_count = 144
    tasks_by_source = {
        source: [
            {"src_name": f"client{source + 1}", "dst_name": f"client{destination + 1}"}
            for destination in range(client_count)
            if destination != source
        ]
        for source in range(client_count)
    }

    for cycle in range(9):
        incoming = {destination: 0 for destination in range(client_count)}
        for source, tasks in tasks_by_source.items():
            batch = _rotating_destination_batch(tasks, 16, cycle, phase_offset=source)
            for probe in batch:
                incoming[int(probe["dst_name"].removeprefix("client")) - 1] += 1

        assert min(incoming.values()) >= 14
        assert max(incoming.values()) <= 18


def test_loss_aggregation_prefers_ratio_of_sums():
    points = [
        {"_time": "t1", "packets_sent": 10, "packets_lost": 0, "packet_loss": 0.0},
        {"_time": "t2", "packets_sent": 2, "packets_lost": 1, "packet_loss": 50.0},
    ]

    stats = _loss_stats(points)

    assert stats is not None
    assert stats["loss_pct"] == pytest.approx((1 / 12) * 100.0)


def test_loss_aggregation_falls_back_to_pct_average():
    points = [
        {"_time": "t1", "df_loss_pct": 10.0},
        {"_time": "t2", "df_loss_pct": 20.0},
        {"_time": "t3", "df_loss_pct": 100.0},
    ]

    stats = _loss_stats(points, prefix="df_")
    converged = _loss_stats(points, prefix="df_", drop_unreachable=True)

    assert stats is not None
    assert converged is not None
    assert stats["loss_pct"] == pytest.approx(130.0 / 3)
    assert converged["loss_pct"] == pytest.approx(15.0)

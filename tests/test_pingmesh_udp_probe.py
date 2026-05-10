"""Loopback unit tests for the UDP burst probe + responder."""

from __future__ import annotations

import time

import pytest

from netopsbench.platform.pingmesh._agent_probe import UdpProbeMixin
from netopsbench.platform.pingmesh._agent_responder import UdpEchoResponder


class _Harness(UdpProbeMixin):
    """Bare host class wiring the attrs UdpProbeMixin reads."""

    def __init__(self, dst_port: int):
        self.udp_dst_port = dst_port
        self.n_rtt_ports = 8
        self.n_df_ports = 4
        self.rtt_src_port_base = 34000
        self.df_src_port_base = 34100
        self.df_payload_size = 1400
        self.burst_timeout_s = 1.0


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


def test_udp_rtt_burst_loopback(loopback_responder):
    port = loopback_responder
    harness = _Harness(dst_port=port)
    result = harness.udp_rtt_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")

    assert result["packets_sent"] == harness.n_rtt_ports
    # All packets should round-trip on loopback.
    assert result["packets_lost"] == 0
    assert result["success"] is True
    assert result["rtt_min"] >= 0.0
    assert result["rtt_max"] < 1000.0  # loopback RTT is sub-millisecond
    assert result["loss_pct"] == 0.0


def test_udp_df_burst_loopback(loopback_responder):
    port = loopback_responder
    harness = _Harness(dst_port=port)
    # Loopback MTU is 65536 so a 1400-byte DF payload always fits.
    result = harness.udp_df_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")

    assert result["packets_sent"] == harness.n_df_ports
    assert result["packets_lost"] == 0
    assert result["success"] is True
    assert result["mtu_drops"] == 0


def test_udp_rtt_burst_no_responder():
    """Probes to an unbound port should report 100% loss without crashing."""
    harness = _Harness(dst_port=38435)
    result = harness.udp_rtt_burst(dst_ip="127.0.0.1", src_ip="127.0.0.1")
    assert result["packets_sent"] == harness.n_rtt_ports
    # On loopback the kernel may deliver an ICMP "port unreachable" that
    # causes recvfrom() to return ECONNREFUSED — those count as loss too.
    assert result["packets_lost"] == harness.n_rtt_ports
    assert result["success"] is False
    assert result["loss_pct"] == pytest.approx(100.0)

"""UDP burst probe helpers for the Pingmesh agent.

Replaces the previous ICMP-based ``ping``/``ping -M do`` subprocess calls
with native UDP socket bursts that vary src_port across a configurable
range. Varying src_port spreads probes across all ECMP paths in the
fabric so a fault on any single spine uplink becomes statistically
visible instead of being hidden by a fixed flow hash.

Industrial parallel: Microsoft Pingmesh / Meta NetNORAD do exactly this
— TCP/UDP probes with multiple 5-tuples — to cover every parallel link.

The receiving side runs :class:`UdpEchoResponder` from
``_agent_responder``; it echoes payloads unchanged so the sender can
recover its own send timestamp from the reply and compute RTT without
clock synchronisation.
"""

from __future__ import annotations

import select
import socket
import struct
import time

try:
    from ._agent_support import logger
except ImportError:  # standalone in-container deployment
    from _agent_support import logger  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Linux IP_MTU_DISCOVER constants (define locally so the module is portable
# to systems whose Python build omits these socket attributes).
# ---------------------------------------------------------------------------
_IP_MTU_DISCOVER = getattr(socket, "IP_MTU_DISCOVER", 10)
_IP_PMTUDISC_DO = getattr(socket, "IP_PMTUDISC_DO", 2)

# Header layout: 8B timestamp_ns | 8B seq number | padding
_HEADER_FMT = "!QQ"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

_DEFAULT_RTT_PAYLOAD_BYTES = 64
_MIN_PAYLOAD_BYTES = _HEADER_SIZE


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


def _empty_rtt_result(sent: int) -> dict:
    return {
        "success": False,
        "rtt_min": 0.0,
        "rtt_avg": 0.0,
        "rtt_max": 0.0,
        "rtt_p90": 0.0,
        "rtt_p99": 0.0,
        "packets_sent": sent,
        "packets_lost": sent,
        "loss_pct": 100.0,
    }


def _empty_df_result(sent: int) -> dict:
    return {
        "success": False,
        "packets_sent": sent,
        "packets_lost": sent,
        "loss_pct": 100.0,
        "rtt_avg": 0.0,
        "mtu_drops": 0,
    }


class UdpProbeMixin:
    """Mixin providing UDP RTT and DF probe bursts.

    Required attributes on the host class:
        udp_dst_port (int)
        n_rtt_ports (int)
        n_df_ports (int)
        rtt_src_port_base (int)
        df_src_port_base (int)
        df_payload_size (int)
        burst_timeout_s (float)
    """

    def _open_burst_sockets(
        self,
        src_ip: str,
        src_port_base: int,
        n_ports: int,
        set_df: bool,
    ) -> list:
        """Open up to ``n_ports`` non-blocking UDP sockets bound to consecutive src_ports.

        Sockets that fail to bind (e.g. port already in use) are skipped.
        Returns a list of (src_port, socket) tuples.
        """
        opened: list = []
        bind_addr = src_ip or ""
        for offset in range(n_ports):
            src_port = src_port_base + offset
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            if set_df:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, _IP_MTU_DISCOVER, _IP_PMTUDISC_DO)
                except OSError:
                    logger.debug("setting IP_MTU_DISCOVER failed", exc_info=True)
            try:
                sock.bind((bind_addr, src_port))
            except OSError as exc:
                logger.debug("bind %s:%d failed: %s", bind_addr or "*", src_port, exc)
                sock.close()
                continue
            opened.append((src_port, sock))
        return opened

    def _udp_burst(
        self,
        dst_ip: str,
        src_ip: str,
        src_port_base: int,
        n_ports: int,
        payload_size: int,
        timeout_s: float,
    ) -> tuple:
        """Send a burst of UDP probes and collect echoes.

        Returns ``(sent, received, rtts_ms, mtu_drops)`` where
        ``mtu_drops`` counts ``EMSGSIZE`` send failures (signal of an
        upstream MTU mismatch when DF is set).
        """
        payload_size = max(payload_size, _MIN_PAYLOAD_BYTES)
        sockets = self._open_burst_sockets(src_ip, src_port_base, n_ports, set_df=True)
        if not sockets:
            return 0, 0, [], 0

        send_ts: dict = {}  # fileno -> send_time_ns
        mtu_drops = 0
        for seq, (_src_port, sock) in enumerate(sockets):
            send_time_ns = time.time_ns()
            payload = struct.pack(_HEADER_FMT, send_time_ns, seq) + b"\x00" * (payload_size - _HEADER_SIZE)
            try:
                sock.sendto(payload, (dst_ip, self.udp_dst_port))
                send_ts[sock.fileno()] = send_time_ns
            except OSError as exc:
                # EMSGSIZE (errno 90) means the kernel's cached PMTU
                # cannot accommodate this packet with DF set — that IS the
                # MTU-mismatch signal we want to surface.
                if getattr(exc, "errno", None) == 90:
                    mtu_drops += 1
                else:
                    logger.debug("sendto %s failed: %s", dst_ip, exc)

        sent = len(send_ts) + mtu_drops
        rtts_ms: list = []
        deadline = time.monotonic() + timeout_s
        pending_socks = [sock for _, sock in sockets if sock.fileno() in send_ts]
        while pending_socks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select(pending_socks, [], [], remaining)
            except (OSError, ValueError):
                break
            if not readable:
                break
            for sock in readable:
                try:
                    data, _addr = sock.recvfrom(65535)
                except OSError:
                    pending_socks = [s for s in pending_socks if s is not sock]
                    continue
                if len(data) >= _HEADER_SIZE:
                    sent_ns, _seq = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
                    rtt_ms = (time.time_ns() - sent_ns) / 1e6
                    if rtt_ms >= 0:
                        rtts_ms.append(rtt_ms)
                pending_socks = [s for s in pending_socks if s is not sock]

        for _, sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

        return sent, len(rtts_ms), rtts_ms, mtu_drops

    def udp_rtt_burst(self, dst_ip: str, src_ip: str = None) -> dict:
        """Run an RTT-oriented UDP burst: many small DF packets across src_ports."""
        try:
            sent, received, rtts, mtu_drops = self._udp_burst(
                dst_ip=dst_ip,
                src_ip=src_ip or "",
                src_port_base=self.rtt_src_port_base,
                n_ports=self.n_rtt_ports,
                payload_size=_DEFAULT_RTT_PAYLOAD_BYTES,
                timeout_s=self.burst_timeout_s,
            )
        except Exception as exc:
            logger.warning("UDP RTT burst to %s failed: %s", dst_ip, exc)
            return _empty_rtt_result(self.n_rtt_ports)

        if sent == 0:
            return _empty_rtt_result(self.n_rtt_ports)

        lost = sent - received
        loss_pct = (lost / sent) * 100.0 if sent > 0 else 100.0
        if rtts:
            return {
                "success": received == sent and mtu_drops == 0,
                "rtt_min": min(rtts),
                "rtt_avg": sum(rtts) / len(rtts),
                "rtt_max": max(rtts),
                "rtt_p90": _percentile(rtts, 0.9),
                "rtt_p99": _percentile(rtts, 0.99),
                "packets_sent": sent,
                "packets_lost": lost,
                "loss_pct": loss_pct,
            }
        return {
            "success": False,
            "rtt_min": 0.0,
            "rtt_avg": 0.0,
            "rtt_max": 0.0,
            "rtt_p90": 0.0,
            "rtt_p99": 0.0,
            "packets_sent": sent,
            "packets_lost": lost,
            "loss_pct": loss_pct,
        }

    def udp_df_burst(self, dst_ip: str, src_ip: str = None) -> dict:
        """Run a DF-oriented UDP burst: a few large DF packets across src_ports.

        A failure rate >0 here indicates either path packet loss OR an
        MTU mismatch on at least one ECMP path between src and dst.
        """
        try:
            sent, received, rtts, mtu_drops = self._udp_burst(
                dst_ip=dst_ip,
                src_ip=src_ip or "",
                src_port_base=self.df_src_port_base,
                n_ports=self.n_df_ports,
                payload_size=self.df_payload_size,
                timeout_s=self.burst_timeout_s,
            )
        except Exception as exc:
            logger.warning("UDP DF burst to %s failed: %s", dst_ip, exc)
            return _empty_df_result(self.n_df_ports)

        if sent == 0:
            return _empty_df_result(self.n_df_ports)

        lost = sent - received
        loss_pct = (lost / sent) * 100.0 if sent > 0 else 100.0
        return {
            "success": received == sent and mtu_drops == 0,
            "packets_sent": sent,
            "packets_lost": lost,
            "loss_pct": loss_pct,
            "rtt_avg": (sum(rtts) / len(rtts)) if rtts else 0.0,
            "mtu_drops": mtu_drops,
        }

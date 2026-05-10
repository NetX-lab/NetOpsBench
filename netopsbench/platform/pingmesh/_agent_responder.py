"""UDP echo responder for the Pingmesh probe agent.

The pinger sends UDP probes from many src_port values (to spread across
ECMP paths). Each probe carries an 8-byte send timestamp in the payload;
this responder echoes the payload back unchanged so the pinger can
compute RTT from the embedded timestamp.

Run as a daemon thread inside each client container alongside the pinger.
"""

from __future__ import annotations

import socket
import threading

try:
    from ._agent_support import logger
except ImportError:  # standalone in-container deployment
    from _agent_support import logger  # type: ignore[no-redef]


_RECV_BUF_SIZE = 65535
_SOCK_RCVBUF_BYTES = 4 * 1024 * 1024  # 4 MB; absorbs bursty traffic
_SHUTDOWN_POLL_TIMEOUT_S = 1.0


class UdpEchoResponder:
    """Threaded UDP echo server bound to a single (ip, port) pair.

    Echoes every received datagram back to its sender unchanged. The
    pinger encodes its send timestamp in the first bytes of the payload
    so RTT measurement is single-sided (no clock sync needed).
    """

    def __init__(self, bind_ip: str = "", port: int = 33434):
        self.bind_ip = bind_ip or ""
        self.port = int(port)
        self._shutdown_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        """Bind the listening socket and launch the responder thread."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCK_RCVBUF_BYTES)
        except OSError:
            logger.debug("UDP responder: SO_RCVBUF tuning rejected", exc_info=True)
        try:
            sock.bind((self.bind_ip, self.port))
            bound_to = self.bind_ip or "0.0.0.0"
        except OSError as exc:
            logger.warning(
                "UDP responder: bind to %s:%d failed (%s); falling back to 0.0.0.0",
                self.bind_ip or "*",
                self.port,
                exc,
            )
            try:
                sock.bind(("", self.port))
                bound_to = "0.0.0.0"
            except OSError as exc2:
                sock.close()
                raise RuntimeError(f"UDP responder: cannot bind any address on port {self.port}: {exc2}") from exc2
        sock.settimeout(_SHUTDOWN_POLL_TIMEOUT_S)
        self._socket = sock
        self._thread = threading.Thread(
            target=self._run,
            name="pingmesh-udp-responder",
            daemon=True,
        )
        self._thread.start()
        logger.info("UDP echo responder listening on %s:%d", bound_to, self.port)

    def _run(self) -> None:
        sock = self._socket
        if sock is None:
            return
        while not self._shutdown_event.is_set():
            try:
                data, addr = sock.recvfrom(_RECV_BUF_SIZE)
            except TimeoutError:
                continue
            except OSError as exc:
                if self._shutdown_event.is_set():
                    return
                logger.warning("UDP responder recv error: %s", exc)
                continue
            try:
                sock.sendto(data, addr)
            except OSError as exc:
                logger.debug("UDP responder echo to %s failed: %s", addr, exc)

    def stop(self) -> None:
        """Signal shutdown and join the responder thread."""
        self._shutdown_event.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

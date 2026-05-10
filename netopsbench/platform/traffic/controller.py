"""
Traffic Controller - Manages background iperf3 traffic flows
"""

import subprocess
import uuid
from dataclasses import dataclass, field

from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.events import emit as _emit
from netopsbench.platform.utils.proc import safe_run, sudo_prefix

logger = get_logger(__name__)


@dataclass
class TrafficFlow:
    """Single iperf3 traffic flow specification"""

    src: str  # client name (e.g., "client1")
    dst: str  # destination client name
    dst_ip: str  # destination IP address
    dst_port: int = 5201
    protocol: str = "tcp"  # "tcp" or "udp"
    bandwidth: str = "100M"  # "100M", "1G", etc.
    duration: int = 0  # seconds, 0 = continuous
    parallel: int = 1  # parallel streams
    udp_payload_len: int = 1400
    tcp_mss: int = 1360
    flow_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class TrafficController:
    """
    Controls iperf3 traffic flows between client containers.

    Manages starting/stopping background traffic for benchmark realism.
    """

    def __init__(self, container_names: dict[str, str]):
        """
        Initialize traffic controller.

        Args:
            container_names: Dict mapping client names to container names
                            e.g., {"client1": "clab-dcn-client1"}
        """
        self.container_names = container_names
        self.active_flows: dict[str, TrafficFlow] = {}
        self.started_server_ports: dict[str, set] = {}
        # Bound external command latency to avoid benchmark hangs.
        self.command_timeout_seconds = 15
        self.start_retries = 1

    def _ensure_iperf_server(self, dst_client: str, dst_port: int):
        """Ensure destination has an iperf3 server listening on dst_port."""
        dst_container = self.container_names.get(dst_client)
        if not dst_container:
            raise ValueError(f"Unknown destination client: {dst_client}")

        started_ports = self.started_server_ports.setdefault(dst_container, set())
        if dst_port in started_ports:
            return

        check_cmd = f"ss -lnt 2>/dev/null | grep -q ':{dst_port} ' " f"|| iperf3 -s -D -p {dst_port}"
        safe_run(
            [*sudo_prefix(), "docker", "exec", dst_container, "sh", "-lc", check_cmd],
            check=True,
            capture_output=True,
            timeout=self.command_timeout_seconds,
        )
        started_ports.add(dst_port)

    def start_flow(self, flow: TrafficFlow) -> str:
        """
        Start a single iperf3 traffic flow.

        Args:
            flow: TrafficFlow specification

        Returns:
            flow_id of the started flow
        """
        src_container = self.container_names.get(flow.src)
        if not src_container:
            raise ValueError(f"Unknown client: {flow.src}")

        # Build iperf3 command
        cmd_parts = [
            *sudo_prefix(),
            "docker",
            "exec",
            "-d",
            src_container,
            "iperf3",
            "-c",
            flow.dst_ip,
            "-p",
            str(flow.dst_port),
            "-P",
            str(flow.parallel),
        ]

        if flow.protocol == "udp":
            cmd_parts.extend(["-u", "-b", flow.bandwidth, "-l", str(flow.udp_payload_len)])
        elif flow.bandwidth:
            cmd_parts.extend(["-b", flow.bandwidth])
            if flow.tcp_mss > 0:
                cmd_parts.extend(["-M", str(flow.tcp_mss)])

        # Keep continuous semantics explicit
        cmd_parts.extend(["-t", str(flow.duration)])

        # Start flow in background
        last_error: Exception | None = None
        for attempt in range(self.start_retries + 1):
            try:
                self._ensure_iperf_server(flow.dst, flow.dst_port)
                safe_run(
                    cmd_parts,
                    check=True,
                    capture_output=True,
                    timeout=self.command_timeout_seconds,
                )
                self.active_flows[flow.flow_id] = flow
                _emit(f"  Started flow: {flow.src} -> {flow.dst} ({flow.bandwidth})")
                return flow.flow_id
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                last_error = e
                if attempt < self.start_retries:
                    _emit(
                        f"  Warning: flow start retry {attempt + 1}/{self.start_retries} "
                        f"for {flow.src} -> {flow.dst}"
                    )
                    continue
                break

        _emit(f"  Failed to start flow {flow.src} -> {flow.dst}: {last_error}")
        raise RuntimeError(f"Failed to start flow {flow.src}->{flow.dst}: {last_error}")

    def stop_flow(self, flow_id: str):
        """
        Stop a specific traffic flow.

        Args:
            flow_id: ID of flow to stop
        """
        if flow_id not in self.active_flows:
            _emit(f"  Warning: Flow {flow_id} not found")
            return

        flow = self.active_flows[flow_id]
        src_container = self.container_names.get(flow.src)

        if src_container:
            try:
                # Kill iperf3 client processes
                safe_run(
                    [*sudo_prefix(), "docker", "exec", src_container, "pkill", "-f", f"iperf3 -c {flow.dst_ip}"],
                    check=False,  # Don't fail if process already stopped
                    capture_output=True,
                    timeout=15,
                )
                _emit(f"  Stopped flow: {flow.src} -> {flow.dst}")

            except Exception as e:
                _emit(f"  Warning: Failed to stop flow {flow_id}: {e}", level="warning")

        del self.active_flows[flow_id]

    def start_matrix(self, flows: list[TrafficFlow]) -> list[str]:
        """
        Start multiple traffic flows.

        Args:
            flows: List of TrafficFlow specifications

        Returns:
            List of flow_ids
        """
        flow_ids = []

        for flow in flows:
            try:
                flow_id = self.start_flow(flow)
                flow_ids.append(flow_id)
            except Exception as e:
                _emit(f"  Warning: Skipping flow due to error: {e}", level="warning")

        _emit(f"\n  Started {len(flow_ids)}/{len(flows)} flows")
        return flow_ids

    def stop_all(self):
        """Stop all active traffic flows."""
        flow_ids = list(self.active_flows.keys())

        for flow_id in flow_ids:
            self.stop_flow(flow_id)

        _emit(f"  Stopped all {len(flow_ids)} flows")

    def get_active_flows(self) -> list[TrafficFlow]:
        """Get list of all active flows."""
        return list(self.active_flows.values())

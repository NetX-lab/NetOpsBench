"""
Traffic Controller - Manages background iperf3 traffic flows
"""

import subprocess
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.proc import docker_prefix, safe_run

from .commands import IperfCommandBuilder
from .settings import TrafficSettings

logger = get_logger(__name__)


def _traffic_parallelism() -> int:
    return TrafficSettings.from_env().parallelism


def _flow_summary(flow: "TrafficFlow") -> str:
    return (
        f"src={flow.src} dst={flow.dst} dst_ip={flow.dst_ip} "
        f"protocol={flow.protocol} port={flow.dst_port} bandwidth={flow.bandwidth}"
    )


def _error_detail(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    stdout = getattr(exc, "stdout", None)
    if stderr:
        return str(stderr).strip()
    if stdout:
        return str(stdout).strip()
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timeout after {exc.timeout}s"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit status {exc.returncode}"
    return f"{type(exc).__name__}: {exc}"


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


@dataclass(frozen=True)
class TrafficStartStats:
    """Timings and retry counters for the latest matrix start."""

    server_duration_seconds: float = 0.0
    source_duration_seconds: float = 0.0
    server_first_attempt_successes: int = 0
    server_first_attempt_failures: int = 0
    retry_count: int = 0
    timeout_count: int = 0
    started_flow_count: int = 0
    failed_flow_count: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class _BatchPhaseResult:
    failures: dict[str, Exception]
    duration_seconds: float
    first_attempt_successes: int
    first_attempt_failures: int
    retry_count: int
    timeout_count: int


class TrafficController:
    """
    Controls iperf3 traffic flows between client containers.

    Manages starting/stopping background traffic for benchmark realism.
    """

    def __init__(
        self,
        container_names: dict[str, str],
        command_builder: IperfCommandBuilder | None = None,
        parallelism: int | None = None,
    ):
        """
        Initialize traffic controller.

        Args:
            container_names: Dict mapping client names to container names
                            e.g., {"client1": "clab-dcn-client1"}
        """
        self.container_names = container_names
        self.command_builder = command_builder or IperfCommandBuilder()
        self.parallelism = max(1, parallelism or _traffic_parallelism())
        self.server_parallelism = min(16, max(1, (self.parallelism + 1) // 2))
        self.retry_parallelism = min(4, self.server_parallelism)
        self.active_flows: dict[str, TrafficFlow] = {}
        self.last_start_stats = TrafficStartStats()
        # Bound external command latency to avoid benchmark hangs.
        self.command_timeout_seconds = 15
        self.start_retries = 1

    def _ensure_iperf_servers_batch(self, dst_container: str, dst_ports: set[int]) -> None:
        """Ensure a destination container has all required iperf3 server ports."""
        script = self.command_builder.server_batch_script(dst_ports)
        safe_run(
            [*docker_prefix(), "docker", "exec", dst_container, "sh", "-lc", script],
            check=True,
            capture_output=True,
            timeout=max(self.command_timeout_seconds, 15),
        )

    def _start_source_flows_batch(self, src_container: str, flows: list[TrafficFlow]) -> None:
        script = self.command_builder.source_batch_script(flows)
        safe_run(
            [*docker_prefix(), "docker", "exec", src_container, "sh", "-lc", script],
            check=True,
            capture_output=True,
            timeout=max(self.command_timeout_seconds, 15),
        )

    def _run_batch_phase(
        self,
        groups: dict[str, Any],
        operation: Callable[[str, Any], None],
        initial_parallelism: int,
    ) -> _BatchPhaseResult:
        """Run one grouped Docker phase, retrying transient failures at lower pressure."""
        started_at = monotonic()
        pending = dict(groups)
        failures: dict[str, Exception] = {}
        first_attempt_successes = 0
        first_attempt_failures = 0
        retry_count = 0
        timeout_count = 0
        for attempt in range(self.start_retries + 1):
            if not pending:
                break
            max_workers = initial_parallelism if attempt == 0 else self.retry_parallelism
            current = pending
            pending = {}
            failures = {}
            if attempt > 0:
                retry_count += len(current)
            with ThreadPoolExecutor(max_workers=min(max_workers, len(current))) as executor:
                future_map = {
                    executor.submit(operation, container, value): container for container, value in current.items()
                }
                for future in as_completed(future_map):
                    container = future_map[future]
                    try:
                        future.result()
                    except Exception as exc:
                        pending[container] = current[container]
                        failures[container] = exc
                        if isinstance(exc, subprocess.TimeoutExpired):
                            timeout_count += 1
            if attempt == 0:
                first_attempt_failures = len(pending)
                first_attempt_successes = len(current) - first_attempt_failures
        return _BatchPhaseResult(
            failures=failures,
            duration_seconds=monotonic() - started_at,
            first_attempt_successes=first_attempt_successes,
            first_attempt_failures=first_attempt_failures,
            retry_count=retry_count,
            timeout_count=timeout_count,
        )

    def start_matrix(self, flows: list[TrafficFlow]) -> list[str]:
        """
        Start multiple traffic flows.

        Args:
            flows: List of TrafficFlow specifications

        Returns:
            List of flow_ids
        """
        if not flows:
            self.last_start_stats = TrafficStartStats()
            logger.info("Started 0/0 flows")
            return []

        valid_flows: list[TrafficFlow] = []
        dst_ports_by_container: dict[str, set[int]] = defaultdict(set)
        flows_by_dst_container: dict[str, list[TrafficFlow]] = defaultdict(list)
        for flow in flows:
            src_container = self.container_names.get(flow.src)
            dst_container = self.container_names.get(flow.dst)
            if not src_container:
                logger.warning("Skipping flow with unknown source: %s", _flow_summary(flow))
                continue
            if not dst_container:
                logger.warning("Skipping flow with unknown destination: %s", _flow_summary(flow))
                continue
            valid_flows.append(flow)
            dst_ports_by_container[dst_container].add(flow.dst_port)
            flows_by_dst_container[dst_container].append(flow)

        failed_flows: set[str] = set()
        server_result = self._run_batch_phase(
            dst_ports_by_container,
            self._ensure_iperf_servers_batch,
            self.server_parallelism,
        )
        for dst_container, exc in server_result.failures.items():
            dst_flows = flows_by_dst_container.get(dst_container, [])
            for flow in dst_flows:
                failed_flows.add(flow.flow_id)
            logger.warning(
                "Failed to prepare traffic destination %s (%d flows, ports=%s): %s",
                dst_container,
                len(dst_flows),
                sorted(dst_ports_by_container[dst_container]),
                _error_detail(exc),
            )

        source_groups: dict[str, list[TrafficFlow]] = defaultdict(list)
        for flow in valid_flows:
            if flow.flow_id in failed_flows:
                continue
            source_groups[self.container_names[flow.src]].append(flow)

        source_result = self._run_batch_phase(
            source_groups,
            self._start_source_flows_batch,
            self.parallelism,
        )
        for src_container, src_flows in source_groups.items():
            source_error = source_result.failures.get(src_container)
            if source_error is not None:
                for flow in src_flows:
                    failed_flows.add(flow.flow_id)
                    self.active_flows.pop(flow.flow_id, None)
                logger.warning(
                    "Failed to start traffic source %s (%d flows): %s",
                    src_container,
                    len(src_flows),
                    _error_detail(source_error),
                )
                continue
            for flow in src_flows:
                self.active_flows[flow.flow_id] = flow
            logger.debug("Started %d flow(s) from %s", len(src_flows), src_flows[0].src)

        flow_ids = [flow.flow_id for flow in valid_flows if flow.flow_id in self.active_flows]
        self.last_start_stats = TrafficStartStats(
            server_duration_seconds=server_result.duration_seconds,
            source_duration_seconds=source_result.duration_seconds,
            server_first_attempt_successes=server_result.first_attempt_successes,
            server_first_attempt_failures=server_result.first_attempt_failures,
            retry_count=server_result.retry_count + source_result.retry_count,
            timeout_count=server_result.timeout_count + source_result.timeout_count,
            started_flow_count=len(flow_ids),
            failed_flow_count=len(flows) - len(flow_ids),
        )
        logger.info(
            "Traffic phases: servers=%.1fs sources=%.1fs retries=%d timeouts=%d",
            server_result.duration_seconds,
            source_result.duration_seconds,
            self.last_start_stats.retry_count,
            self.last_start_stats.timeout_count,
        )
        logger.info("Started %d/%d flows", len(flow_ids), len(flows))
        return flow_ids

    def verify_active_flows(self) -> bool:
        """Return whether all recorded clients and destination listeners are alive."""
        if not self.active_flows:
            return False

        flows_by_container: dict[str, list[TrafficFlow]] = defaultdict(list)
        ports_by_container: dict[str, set[int]] = defaultdict(set)
        for flow in self.active_flows.values():
            src_container = self.container_names.get(flow.src)
            dst_container = self.container_names.get(flow.dst)
            if not src_container or not dst_container:
                return False
            flows_by_container[src_container].append(flow)
            ports_by_container[dst_container].add(flow.dst_port)

        containers = set(flows_by_container) | set(ports_by_container)

        def verify_container(container: str) -> None:
            script = self.command_builder.verify_runtime_script(
                flows_by_container.get(container, []),
                ports_by_container.get(container, set()),
            )
            safe_run(
                [*docker_prefix(), "docker", "exec", container, "sh", "-lc", script],
                check=True,
                capture_output=True,
                timeout=self.command_timeout_seconds,
            )

        with ThreadPoolExecutor(max_workers=min(self.parallelism, len(containers))) as executor:
            futures = [executor.submit(verify_container, container) for container in containers]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("Traffic liveness verification failed: %s", _error_detail(exc))
                    return False
        return True

    def stop_all(self):
        """Stop all iperf clients and servers in every client container."""
        flow_ids = list(self.active_flows.keys())

        def stop_container(container: str) -> None:
            safe_run(
                [
                    *docker_prefix(),
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-lc",
                    self.command_builder.stop_all_script(),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )

        containers = set(self.container_names.values())
        parallelism = min(self.parallelism, max(len(containers), 1))
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {executor.submit(stop_container, container): container for container in containers}
            for future in as_completed(future_map):
                container = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("Failed to stop traffic on %s: %s", container, _error_detail(exc))
                    failures.append(f"{container}: {_error_detail(exc)}")

        if failures:
            raise RuntimeError("Traffic cleanup failed: " + "; ".join(sorted(failures)))
        self.active_flows.clear()
        logger.info("Stopped all %d flows", len(flow_ids))

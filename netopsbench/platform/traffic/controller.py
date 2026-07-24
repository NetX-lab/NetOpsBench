"""Management-network controller for the native background-traffic process."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from netopsbench.logging_utils import get_logger
from netopsbench.platform.client_agent.contract import HEARTBEAT_MAX_AGE_SECONDS, TRAFFIC_CONTROL_PORT
from netopsbench.platform.client_agent.control import request_agent

logger = get_logger(__name__)

CONTROL_TIMEOUT_SECONDS = 3.0
READY_TIMEOUT_SECONDS = 15.0
CONTROL_PARALLELISM = 32


@dataclass
class TrafficFlow:
    """Single background flow specification shared with the traffic planner."""

    src: str
    dst: str
    dst_ip: str
    dst_port: int = 5201
    protocol: str = "tcp"
    bandwidth: str = "100M"
    udp_payload_len: int = 1400
    tcp_mss: int = 1360
    flow_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class TrafficStartStats:
    """Native traffic startup metrics retained for reports."""

    plan_load_seconds: float = 0.0
    enable_to_ready_seconds: float = 0.0
    client_count: int = 0
    started_flow_count: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


class TrafficController:
    """Load, enable, and inspect one native traffic process per client."""

    def __init__(self, management_ips: dict[str, str]):
        self.management_ips = dict(management_ips)
        self.active_flows: dict[str, TrafficFlow] = {}
        self.last_start_stats = TrafficStartStats()
        self.generation = 0
        self.plan_digests: dict[str, str] = {}

    def _request(self, client: str, operation: str, payload: dict) -> dict:
        host = self.management_ips.get(client)
        if not host:
            raise RuntimeError(f"Missing management IP for traffic client {client}")
        try:
            return request_agent(
                host,
                TRAFFIC_CONTROL_PORT,
                operation,
                payload,
                timeout=CONTROL_TIMEOUT_SECONDS,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"Traffic process on {client}: {exc}") from exc

    def _request_with_retry(self, client: str, operation: str, payload: dict) -> dict:
        for attempt in range(2):
            try:
                return self._request(client, operation, payload)
            except (OSError, json.JSONDecodeError):
                if attempt == 1:
                    raise
                time.sleep(0.1)
        raise AssertionError("unreachable")

    def _parallel_requests(self, requests: dict[str, tuple[str, dict]]) -> dict[str, dict]:
        results: dict[str, dict] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=min(CONTROL_PARALLELISM, max(1, len(requests)))) as executor:
            futures = {
                executor.submit(self._request_with_retry, client, operation, payload): client
                for client, (operation, payload) in requests.items()
            }
            for future in as_completed(futures):
                client = futures[future]
                try:
                    results[client] = future.result()
                except Exception as exc:  # noqa: BLE001 - aggregate every client failure
                    failures.append(f"{client}: {type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("Native traffic control failed: " + "; ".join(sorted(failures)))
        return results

    def start_matrix(self, flows: list[TrafficFlow]) -> list[str]:
        if not flows:
            self.last_start_stats = TrafficStartStats()
            return []
        unknown = sorted({name for flow in flows for name in (flow.src, flow.dst) if name not in self.management_ips})
        if unknown:
            raise RuntimeError(f"Traffic plan references unknown clients: {', '.join(unknown)}")

        started = time.monotonic()
        self.generation = time.time_ns() & 0xFFFF_FFFF_FFFF_FFFF or 1
        plans = _client_plans(self.management_ips, flows)
        self.plan_digests = {client: _plan_digest(plan) for client, plan in plans.items()}
        load_requests = {
            client: (
                "load_plan",
                {
                    "generation": self.generation,
                    "plan_digest": self.plan_digests[client],
                    "plan": plan,
                },
            )
            for client, plan in plans.items()
        }
        try:
            self._parallel_requests(load_requests)
            listeners_ready_at = time.monotonic()
            enable_requests = {
                client: (
                    "enable",
                    {
                        "generation": self.generation,
                        "plan_digest": self.plan_digests[client],
                    },
                )
                for client in plans
            }
            self._parallel_requests(enable_requests)

            deadline = time.monotonic() + READY_TIMEOUT_SECONDS
            while True:
                statuses = self._status_all()
                if all(self._status_ready(client, response) for client, response in statuses.items()):
                    break
                if time.monotonic() >= deadline:
                    details = {
                        client: response.get("status")
                        for client, response in statuses.items()
                        if not self._status_ready(client, response)
                    }
                    raise RuntimeError(f"Native traffic did not become ready within 15s: {details}")
                time.sleep(0.2)
        except Exception:
            self._disable_best_effort()
            self.generation = 0
            self.plan_digests.clear()
            raise

        self.active_flows = {flow.flow_id: flow for flow in flows}
        self.last_start_stats = TrafficStartStats(
            plan_load_seconds=listeners_ready_at - started,
            enable_to_ready_seconds=time.monotonic() - listeners_ready_at,
            client_count=len(plans),
            started_flow_count=len(flows),
        )
        logger.info(
            "Native traffic ready: clients=%d flows=%d elapsed=%.1fs",
            len(plans),
            len(flows),
            time.monotonic() - started,
        )
        return [flow.flow_id for flow in flows]

    def _status_all(self) -> dict[str, dict]:
        return self._parallel_requests({client: ("status", {}) for client in self.management_ips})

    def verify_active_flows(self) -> bool:
        if not self.active_flows or not self.generation:
            return False
        try:
            statuses = self._status_all()
        except RuntimeError as exc:
            logger.warning("Native traffic health check failed: %s", exc)
            return False
        for client, response in statuses.items():
            if not self._status_ready(client, response):
                return False
        return True

    def _status_ready(self, client: str, response: dict) -> bool:
        status = response.get("status") or {}
        heartbeat_ns = int(status.get("heartbeat_unix_ns", 0) or 0)
        heartbeat_age = time.time() - heartbeat_ns / 1_000_000_000
        return (
            status.get("ready") is True
            and status.get("enabled") is True
            and int(status.get("generation", 0)) == self.generation
            and status.get("plan_digest") == self.plan_digests.get(client)
            and int(status.get("active_flows", -1)) == int(status.get("expected_flows", -2))
            and int(status.get("active_listeners", -1)) == int(status.get("expected_listeners", -2))
            and 0 <= heartbeat_age <= HEARTBEAT_MAX_AGE_SECONDS
        )

    def _disable_best_effort(self) -> list[str]:
        if not self.generation:
            return []
        requests = {
            client: (
                "disable",
                {"generation": self.generation, "plan_digest": digest},
            )
            for client, digest in self.plan_digests.items()
        }
        try:
            self._parallel_requests(requests)
        except RuntimeError as exc:
            return [str(exc)]
        return []

    def stop_all(self) -> None:
        failures = self._disable_best_effort()
        if failures:
            raise RuntimeError("Native traffic disable failed: " + "; ".join(sorted(failures)))
        stopped = len(self.active_flows)
        self.active_flows.clear()
        self.generation = 0
        self.plan_digests.clear()
        logger.info("Disabled all %d native traffic flows", stopped)


def _client_plans(clients: dict[str, str], flows: list[TrafficFlow]) -> dict[str, dict]:
    outgoing: dict[str, list[dict]] = defaultdict(list)
    listeners: dict[str, list[dict]] = defaultdict(list)
    seen_listeners: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for flow in flows:
        protocol = flow.protocol.lower()
        if protocol not in {"tcp", "udp"}:
            raise ValueError(f"Unsupported traffic protocol: {flow.protocol}")
        payload_bytes = flow.udp_payload_len if protocol == "udp" else flow.tcp_mss
        outgoing[flow.src].append(
            {
                "flow_id": flow.flow_id,
                "protocol": protocol,
                "dst_ip": flow.dst_ip,
                "dst_port": int(flow.dst_port),
                "bandwidth_bps": _parse_bandwidth_bps(flow.bandwidth),
                "payload_bytes": max(1, int(payload_bytes)),
                "tcp_mss": max(0, int(flow.tcp_mss if protocol == "tcp" else 0)),
            }
        )
        listener_key = (protocol, int(flow.dst_port))
        if listener_key not in seen_listeners[flow.dst]:
            listeners[flow.dst].append({"protocol": protocol, "port": int(flow.dst_port)})
            seen_listeners[flow.dst].add(listener_key)
    return {
        client: {
            "listeners": sorted(
                listeners.get(client, []),
                key=lambda item: (item["port"], item["protocol"]),
            ),
            "flows": sorted(
                outgoing.get(client, []),
                key=lambda item: item["flow_id"],
            ),
        }
        for client in clients
    }


def _plan_digest(plan: dict) -> str:
    encoded = json.dumps(plan, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_bandwidth_bps(value: str) -> int:
    text = str(value).strip().upper()
    factors = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
    if text and text[-1:] in factors:
        return int(float(text[:-1]) * factors[text[-1]])
    return int(float(text))


__all__ = [
    "TrafficController",
    "TrafficFlow",
    "TrafficStartStats",
]

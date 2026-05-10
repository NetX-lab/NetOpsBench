"""Shared helper mixin for AgentToolkit wiring."""

from __future__ import annotations

import subprocess
from typing import Any

from netopsbench.platform.topology.topology_utils import (
    build_topology_state_from_metadata,
    discover_topology_dir,
    enrich_topology_metadata,
    resolve_interface_metric_identities,
)

from ._core.device.parsers import (
    get_active_interface_names,
    get_device_logs_fallback,
    get_live_interface_snapshot,
    get_recent_influx_interface_identities,
    merge_interface_tables,
    parse_bgp_summary,
    parse_influx_metric_rows,
    parse_influx_syslog_rows,
    parse_influx_timestamp,
    parse_ip_link_stats,
    parse_route_table,
    parse_table,
    preview_items,
    query_influx_rows,
    summarize_counter_points,
)
from ._core.device.validators import (
    docker_exec,
    require_client_source,
    resolve_container,
    validate_device_name,
    validate_interface_name,
    validate_ip_address,
    validate_prefix,
)
from ._core.observability.pingmesh_scope import resolve_pingmesh_time_scope


class AgentToolkitHelperMixin:
    _SEVERITY_OPTIONS = {
        "emergency",
        "alert",
        "critical",
        "error",
        "warning",
        "notice",
        "info",
        "debug",
    }
    _PINGMESH_RANGE_ENV_START = "NETOPSBENCH_PINGMESH_START_TIME"
    _PINGMESH_RANGE_ENV_END = "NETOPSBENCH_PINGMESH_END_TIME"
    _DEFAULT_SONIC_PORT_MTU = 9100

    def _validate_device_name(self, device: str, field_name: str = "device") -> str:
        return validate_device_name(device, field_name)

    def _validate_interface_name(self, interface: str) -> str:
        return validate_interface_name(interface)

    def _validate_ip_address(self, ip_value: str, field_name: str = "ip") -> str:
        return validate_ip_address(ip_value, field_name)

    def _validate_prefix(self, prefix: str) -> str:
        return validate_prefix(prefix)

    def _resolve_container(self, device: str, field_name: str = "device") -> str:
        return resolve_container(self, device, field_name)

    def _require_client_source(self, src: str, tool_name: str) -> str:
        return require_client_source(self, src, tool_name)

    def _docker_exec(self, container: str, cmd_args: list[str], timeout: int) -> subprocess.CompletedProcess:
        return docker_exec(container, cmd_args, timeout)

    def _discover_topology_dir(self, base_dir: str) -> str:
        return discover_topology_dir(base_dir)

    def _load_topology_metadata(self, metadata: dict):
        build_topology_state_from_metadata(metadata).apply_to(self)

    def _enrich_topology_metadata(self, topology: dict[str, Any]) -> dict[str, Any]:
        return enrich_topology_metadata(topology, self._DEFAULT_SONIC_PORT_MTU)

    def _resolve_interface_metric_identities(self, interface: str) -> dict[str, list[str]]:
        return resolve_interface_metric_identities(interface)

    @staticmethod
    def _parse_influx_timestamp(value: str | None):
        return parse_influx_timestamp(value)

    def _parse_influx_metric_rows(self, csv_text: str) -> list[dict[str, Any]]:
        return parse_influx_metric_rows(csv_text)

    def _get_recent_influx_interface_identities(
        self, device: str, time_range_minutes: int, headers: dict[str, str] | None = None
    ) -> list[dict[str, str | None]]:
        return get_recent_influx_interface_identities(self, device, time_range_minutes, headers)

    @staticmethod
    def _preview_items(items: list[str], limit: int = 8) -> str:
        return preview_items(items, limit)

    def _summarize_counter_points(self, field: str, points: list[dict[str, Any]]) -> dict[str, Any]:
        return summarize_counter_points(field, points)

    def _parse_table(self, text: str) -> list[dict[str, str]]:
        return parse_table(text)

    def _merge_interface_tables(
        self, status_rows: list[dict[str, str]], counter_rows: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        return merge_interface_tables(status_rows, counter_rows)

    def _parse_ip_link_stats(self, text: str) -> list[dict[str, Any]]:
        return parse_ip_link_stats(text)

    def _parse_bgp_summary(self, text: str) -> list[dict[str, Any]]:
        return parse_bgp_summary(text)

    def _parse_influx_syslog_rows(self, csv_text: str) -> list[dict[str, Any]]:
        return parse_influx_syslog_rows(csv_text)

    def _get_device_logs_fallback(
        self, device: str, time_range_minutes: int, severity: str | None = None
    ) -> list[dict[str, Any]]:
        return get_device_logs_fallback(self, device, time_range_minutes, severity=severity)

    def _get_live_interface_snapshot(self, device: str, interface: str) -> dict[str, Any] | None:
        return get_live_interface_snapshot(self, device, interface)

    def _get_active_interface_names(self, device: str) -> list[str]:
        return get_active_interface_names(self, device)

    def _parse_route_table(self, text: str) -> list[dict[str, Any]]:
        return parse_route_table(text)

    def _resolve_pingmesh_time_scope(
        self, time_range_minutes: int, start_time: str | None = None, end_time: str | None = None
    ) -> dict[str, Any]:
        return resolve_pingmesh_time_scope(self, time_range_minutes, start_time=start_time, end_time=end_time)

    def _query_influx_rows(self, query: str, require_value: bool = True) -> list[dict[str, Any]]:
        return query_influx_rows(self, query, require_value=require_value)

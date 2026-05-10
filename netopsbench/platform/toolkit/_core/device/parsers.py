"""Compatibility re-export layer for device parser helpers."""

from __future__ import annotations

from .bgp_parsers import parse_bgp_summary
from .interface_parsers import (
    get_active_interface_names,
    get_live_interface_snapshot,
    merge_interface_tables,
    parse_ip_link_stats,
)
from .log_parsers import (
    get_device_logs_fallback,
    parse_influx_syslog_rows,
    parse_local_syslog_lines,
)
from .route_parsers import parse_route_table
from .telemetry_parsers import (
    get_recent_influx_interface_identities,
    parse_influx_interface_identity_rows,
    parse_influx_metric_rows,
    parse_influx_timestamp,
    query_influx_rows,
    summarize_counter_points,
)
from .text_parsers import coerce_value, extract_interface_name, normalize_key, parse_table, preview_items

__all__ = [
    "coerce_value",
    "extract_interface_name",
    "get_active_interface_names",
    "get_device_logs_fallback",
    "get_live_interface_snapshot",
    "get_recent_influx_interface_identities",
    "merge_interface_tables",
    "normalize_key",
    "parse_bgp_summary",
    "parse_influx_interface_identity_rows",
    "parse_influx_metric_rows",
    "parse_influx_syslog_rows",
    "parse_influx_timestamp",
    "parse_ip_link_stats",
    "parse_local_syslog_lines",
    "parse_route_table",
    "parse_table",
    "preview_items",
    "query_influx_rows",
    "summarize_counter_points",
]

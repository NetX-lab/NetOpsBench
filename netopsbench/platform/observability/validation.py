"""Validate the worker observability path through InfluxDB."""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Sequence

QueryRunner = Callable[[str], str]
_INTERFACE_COUNTER_FIELDS = {
    "in_octets",
    "out_octets",
    "in_unicast_pkts",
    "out_unicast_pkts",
    "in_discarded_packets",
    "out_discarded_packets",
    "in_errors",
    "out_errors",
}


def _interface_counter_field_filter() -> str:
    predicates = " or ".join(f'r._field == "{field}"' for field in sorted(_INTERFACE_COUNTER_FIELDS))
    return f"  |> filter(fn: (r) => {predicates})\n"


def count_data_rows(csv_text: str) -> int:
    """Count non-comment CSV rows that contain actual measurement data."""
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return 0
    reader = csv.DictReader(lines)
    count = 0
    for row in reader:
        if row.get("result") == "result":
            continue
        if row.get("_time") in (None, "", "_time"):
            continue
        count += 1
    return count


def extract_interface_names(csv_text: str) -> list[str]:
    """Extract SONiC interface names from interface measurement CSV output."""
    names = set()
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(lines)
    for row in reader:
        if row.get("result") == "result":
            continue
        if (row.get("_value") or "").strip() == "":
            continue
        field = (row.get("_field") or "").strip()
        if field and field not in _INTERFACE_COUNTER_FIELDS:
            continue
        name = (row.get("name") or "").strip()
        path = (row.get("path") or "").strip()
        if name and name != "name":
            names.add(name)
            continue
        if path and path != "path":
            match = re.search(r"(Ethernet\d+)", path)
            if match:
                names.add(match.group(1))
    return sorted(names)


def preview(items: Sequence[str], limit: int = 8) -> str:
    """Compact list preview for human-readable error messages."""
    if not items:
        return "none"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... (+{len(items) - limit} more)"


def _topology_filter(topology_id: str) -> str:
    return f'  |> filter(fn: (r) => r.topology_id == "{topology_id}")\n' if topology_id else ""


def check_observability(
    query_runner: QueryRunner,
    bucket: str,
    obs_device: str,
    bgp_device: str = "",
    topology_id: str = "",
    syslog_marker: str = "",
    active_interfaces: Sequence[str] | None = None,
    min_active_coverage_ratio: float = 0.5,
) -> list[str]:
    """Run Pingmesh, interface, and syslog path checks."""
    errors: list[str] = []
    active = [item for item in active_interfaces or [] if item]
    topology_filter = _topology_filter(topology_id)

    pingmesh_query = (
        f'from(bucket: "{bucket}")\n'
        f"  |> range(start: -10m)\n"
        f'  |> filter(fn: (r) => r._measurement == "pingmesh")\n'
        f"{topology_filter}"
        f'  |> filter(fn: (r) => r._field == "rtt_p99")\n'
        f"  |> last()\n"
        f"  |> group()\n"
        f"  |> limit(n: 1)\n"
    )
    if count_data_rows(query_runner(pingmesh_query)) <= 0:
        errors.append("no recent pingmesh samples found in InfluxDB")

    if bgp_device:
        bgp_query = (
            f'from(bucket: "{bucket}")\n'
            f"  |> range(start: -10m)\n"
            f'  |> filter(fn: (r) => r._measurement == "bgp_event_index")\n'
            f"{topology_filter}"
            f'  |> filter(fn: (r) => r.source == "{bgp_device}")\n'
            f'  |> filter(fn: (r) => r._field == "schema_version")\n'
            f"  |> last()\n"
            f"  |> group()\n"
            f"  |> limit(n: 1)\n"
        )
        if count_data_rows(query_runner(bgp_query)) <= 0:
            errors.append(f"no recent bgp neighbor samples found for {bgp_device} in InfluxDB")

    interface_identity_query = (
        f'from(bucket: "{bucket}")\n'
        f"  |> range(start: -10m)\n"
        f'  |> filter(fn: (r) => r._measurement == "interfaces")\n'
        f"{topology_filter}"
        f'  |> filter(fn: (r) => r.source == "{obs_device}")\n'
        f"{_interface_counter_field_filter()}"
        f'  |> group(columns: ["name", "path", "_field"])\n'
        f"  |> last()\n"
        f'  |> keep(columns: ["_time", "_field", "_value", "name", "path"])\n'
    )
    interface_rows = query_runner(interface_identity_query)
    if count_data_rows(interface_rows) <= 0:
        errors.append(f"no recent interface samples found for {obs_device} in InfluxDB")
    else:
        observed_interfaces = extract_interface_names(interface_rows)
        if active:
            observed_active = [name for name in active if name in observed_interfaces]
            coverage = len(observed_active) / len(active)
            if not observed_active:
                errors.append(
                    f"no recent interface samples found for active interfaces on {obs_device}; "
                    f"active={preview(active)} observed={preview(observed_interfaces)}"
                )
            elif coverage < min_active_coverage_ratio:
                missing_active = [name for name in active if name not in observed_interfaces]
                errors.append(
                    f"active interface observability coverage too low on {obs_device}: "
                    f"{len(observed_active)}/{len(active)} observed; "
                    f"missing={preview(missing_active)} observed={preview(observed_interfaces)}"
                )

    if syslog_marker:
        syslog_query = (
            'import "strings"\n'
            f'from(bucket: "{bucket}")\n'
            f"  |> range(start: -5m)\n"
            f'  |> filter(fn: (r) => r._measurement == "syslog")\n'
            f"{topology_filter}"
            f'  |> filter(fn: (r) => r._field == "message")\n'
            f'  |> filter(fn: (r) => strings.containsStr(v: r._value, substr: "{syslog_marker}"))\n'
            f"  |> group()\n"
            f"  |> limit(n: 1)\n"
        )
        if count_data_rows(query_runner(syslog_query)) <= 0:
            errors.append("syslog collector path check failed: marker not found in InfluxDB")

    return errors

#!/usr/bin/env python3
"""Validate the worker observability path through InfluxDB."""

from __future__ import annotations

import argparse
import csv
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)


QueryRunner = Callable[[str], str]


def _make_url_opener(base_url: str):
    hostname = (urllib.parse.urlparse(base_url).hostname or "").strip().lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def run_query(base_url: str, token: str, org: str, query: str) -> str:
    """Execute one Flux query against InfluxDB and return CSV output."""
    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/csv",
        "Content-Type": "application/vnd.flux",
    }
    org_q = urllib.parse.quote(org, safe="")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v2/query?org={org_q}",
        data=query.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with _make_url_opener(base_url).open(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


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
        f"  |> limit(n: 5)\n"
    )
    if count_data_rows(query_runner(pingmesh_query)) <= 0:
        errors.append("no recent pingmesh samples found in InfluxDB")

    if bgp_device:
        bgp_query = (
            f'from(bucket: "{bucket}")\n'
            f"  |> range(start: -10m)\n"
            f'  |> filter(fn: (r) => r._measurement == "bgp_neighbors")\n'
            f"{topology_filter}"
            f'  |> filter(fn: (r) => r.source == "{bgp_device}")\n'
            f'  |> filter(fn: (r) => r._field == "session_state")\n'
            f"  |> last()\n"
        )
        if count_data_rows(query_runner(bgp_query)) <= 0:
            errors.append(f"no recent bgp neighbor samples found for {bgp_device} in InfluxDB")

    interface_query = (
        f'from(bucket: "{bucket}")\n'
        f"  |> range(start: -10m)\n"
        f'  |> filter(fn: (r) => r._measurement == "interfaces")\n'
        f'  |> filter(fn: (r) => r.source == "{obs_device}")\n'
        f"  |> limit(n: 5)\n"
    )
    if count_data_rows(query_runner(interface_query)) <= 0:
        errors.append(f"no recent interface samples found for {obs_device} in InfluxDB")
    else:
        interface_identity_query = (
            f'from(bucket: "{bucket}")\n'
            f"  |> range(start: -10m)\n"
            f'  |> filter(fn: (r) => r._measurement == "interfaces")\n'
            f'  |> filter(fn: (r) => r.source == "{obs_device}")\n'
            f'  |> keep(columns: ["name", "path"])\n'
            f"  |> limit(n: 2000)\n"
        )
        observed_interfaces = extract_interface_names(query_runner(interface_identity_query))
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
            f'  |> filter(fn: (r) => r._field == "message")\n'
            f'  |> filter(fn: (r) => strings.containsStr(v: r._value, substr: "{syslog_marker}"))\n'
            f"  |> limit(n: 5)\n"
        )
        found = False
        for attempt in range(6):
            if count_data_rows(query_runner(syslog_query)) > 0:
                found = True
                break
            if attempt < 5:
                time.sleep(2)
        if not found:
            errors.append("syslog collector path check failed: marker not found in InfluxDB")

    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate worker observability data in InfluxDB")
    parser.add_argument("--url", required=True, help="InfluxDB base URL")
    parser.add_argument("--token", required=True, help="InfluxDB token")
    parser.add_argument("--org", required=True, help="InfluxDB organization")
    parser.add_argument("--bucket", required=True, help="InfluxDB bucket")
    parser.add_argument("--obs-device", required=True, help="Device expected to emit interface metrics")
    parser.add_argument("--bgp-device", default="", help="Device expected to emit BGP neighbor metrics")
    parser.add_argument("--topology-id", default="", help="Optional topology identifier for Pingmesh filtering")
    parser.add_argument("--syslog-marker", default="", help="Optional syslog marker to verify")
    parser.add_argument(
        "--active-interfaces",
        default="",
        help="Comma-separated active interfaces expected in the interface metrics",
    )
    parser.add_argument(
        "--min-active-coverage-ratio",
        type=float,
        default=0.5,
        help="Minimum acceptable coverage ratio for active interface observability",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    active_interfaces = [item for item in args.active_interfaces.split(",") if item]
    try:
        errors = check_observability(
            lambda query: run_query(args.url, args.token, args.org, query),
            bucket=args.bucket,
            obs_device=args.obs_device,
            bgp_device=args.bgp_device,
            topology_id=args.topology_id,
            syslog_marker=args.syslog_marker,
            active_interfaces=active_interfaces,
            min_active_coverage_ratio=args.min_active_coverage_ratio,
        )
    except urllib.error.HTTPError as exc:
        logger.error("InfluxDB query failed: HTTP %s", exc.code)
        return 1
    except urllib.error.URLError as exc:
        logger.error("InfluxDB query failed: %s", exc)
        return 1
    except Exception as exc:
        logger.error("observability query failed: %s", exc)
        return 1

    if errors:
        logger.error("%s", "; ".join(errors))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

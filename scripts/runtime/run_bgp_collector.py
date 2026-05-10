#!/usr/bin/env python3
"""Poll BGP summary from SONiC nodes and emit Influx line protocol snapshots."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from netopsbench.platform.toolkit._core.device.bgp_parsers import parse_bgp_summary


def _docker_prefix() -> List[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo", "-n"]


def _escape_tag(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def _escape_string_field(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def normalize_bgp_state(value: str | None) -> str:
    if not value:
        return "UNKNOWN"
    return str(value).strip().upper()


def _int_field(name: str, value: object) -> str | None:
    if value is None or value == "":
        return None
    return f"{name}={int(value)}i"


def build_bgp_lines(device: str, rows: Iterable[dict], timestamp_ns: int) -> list[str]:
    lines: list[str] = []
    source = _escape_tag(device)
    for row in rows:
        neighbor = row.get("neighbor")
        if not neighbor:
            continue
        tags = [f"source={source}", f"neighbor_address={_escape_tag(str(neighbor))}"]
        fields = [f'session_state="{_escape_string_field(normalize_bgp_state(row.get("state")))}"']
        for key in ("asn", "prefixes_received", "msg_rcvd", "msg_sent", "in_q", "out_q"):
            field = _int_field(key, row.get(key))
            if field:
                fields.append(field)
        up_down = row.get("up_down")
        if up_down:
            fields.append(f'up_down="{_escape_string_field(str(up_down))}"')
        lines.append(f"bgp_neighbors,{','.join(tags)} {','.join(fields)} {timestamp_ns}")
    return lines


def _read_topology(metadata_file: Path) -> tuple[str, list[str]]:
    payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    lab_name = str(payload.get("name") or "dcn").strip()
    devices = payload.get("devices", {}) or {}
    names = [item.get("name") for item in devices.get("spines", []) + devices.get("leafs", []) if item.get("name")]
    return lab_name, names


def collect_bgp_lines(metadata_file: Path, timestamp_ns: int | None = None) -> list[str]:
    lab_name, devices = _read_topology(metadata_file)
    docker_prefix = _docker_prefix()
    resolved_timestamp = time.time_ns() if timestamp_ns is None else int(timestamp_ns)
    lines: list[str] = []
    for device in devices:
        container = f"clab-{lab_name}-{device}"  # matches clab_container_name() convention
        result = subprocess.run(
            [*docker_prefix, "docker", "exec", container, "vtysh", "-c", "show ip bgp summary"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            continue
        rows = parse_bgp_summary(result.stdout)
        lines.extend(build_bgp_lines(device, rows, resolved_timestamp))
    return lines


def run_loop(metadata_file: Path, output_file: Path, interval_seconds: float) -> int:
    running = True

    def _stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.touch(exist_ok=True)

    while running:
        try:
            lines = collect_bgp_lines(metadata_file)
            if lines:
                with output_file.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
        except Exception as exc:
            print(f"WARN: bgp collector iteration failed: {exc}", file=sys.stderr)
        if not running:
            break
        time.sleep(max(1.0, interval_seconds))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit BGP neighbor snapshots as Influx line protocol")
    parser.add_argument("metadata_file", help="Path to topology.json")
    parser.add_argument("--output", required=True, help="Output line protocol file")
    parser.add_argument("--interval", type=float, default=10.0, help="Polling interval in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_loop(Path(args.metadata_file), Path(args.output), args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
#!/usr/bin/env python3
"""Poll BGP summary from SONiC nodes and emit Influx line protocol snapshots."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from netopsbench.platform.observability.bgp_parser import parse_bgp_summary
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.proc import docker_prefix

DEFAULT_BGP_COLLECTOR_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_BGP_COLLECTOR_PARALLELISM = 16
DEFAULT_BGP_POLL_INTERVAL_SECONDS = 10.0
_BGP_EVENT_SCHEMA_VERSION = 1


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
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    return f"{name}={int(value)}i"


def build_bgp_lines(device: str, rows: Iterable[dict], timestamp_ns: int, topology_id: str = "") -> list[str]:
    lines: list[str] = []
    source = _escape_tag(device)
    topology_tag = _escape_tag(topology_id)
    for row in rows:
        neighbor = row.get("neighbor")
        if not neighbor:
            continue
        tags = [f"source={source}", f"neighbor_address={_escape_tag(str(neighbor))}"]
        if topology_tag:
            tags.append(f"topology_id={topology_tag}")
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


def build_bgp_collection_line(
    device: str,
    timestamp_ns: int,
    topology_id: str,
    collection_ok: bool,
    neighbor_count: int,
    duration_ms: int,
    error_type: str,
) -> str:
    tags = [f"source={_escape_tag(device)}"]
    if topology_id:
        tags.append(f"topology_id={_escape_tag(topology_id)}")
    fields = [
        f"collection_ok={'true' if collection_ok else 'false'}",
        f"neighbor_count={max(0, int(neighbor_count))}i",
        f"duration_ms={max(0, int(duration_ms))}i",
        f'error_type="{_escape_string_field(error_type)}"',
    ]
    return f"bgp_collection,{','.join(tags)} {','.join(fields)} {timestamp_ns}"


def build_bgp_event_index_line(
    device: str,
    timestamp_ns: int,
    topology_id: str,
    collection_ok: bool,
    neighbor_count: int,
) -> str:
    """Emit a heartbeat proving that transition indexing covered one poll."""
    tags = [f"source={_escape_tag(device)}"]
    if topology_id:
        tags.append(f"topology_id={_escape_tag(topology_id)}")
    fields = [
        f"schema_version={_BGP_EVENT_SCHEMA_VERSION}i",
        f"collection_ok={'true' if collection_ok else 'false'}",
        f"neighbor_count={max(0, int(neighbor_count))}i",
    ]
    return f"bgp_event_index,{','.join(tags)} {','.join(fields)} {timestamp_ns}"


def build_bgp_transition_line(
    device: str,
    neighbor: str,
    previous: dict,
    current: dict,
    timestamp_ns: int,
    topology_id: str,
) -> str:
    previous_state = normalize_bgp_state(previous.get("state"))
    latest_state = normalize_bgp_state(current.get("state"))
    if previous_state == "ESTABLISHED" and latest_state != "ESTABLISHED":
        event_type = "session_down"
    elif previous_state != "ESTABLISHED" and latest_state == "ESTABLISHED":
        event_type = "session_recovered"
    else:
        event_type = "session_state_changed"
    tags = [
        f"source={_escape_tag(device)}",
        f"neighbor_address={_escape_tag(neighbor)}",
        f"event_type={event_type}",
    ]
    if topology_id:
        tags.append(f"topology_id={_escape_tag(topology_id)}")
    fields = [
        f'previous_state="{_escape_string_field(previous_state)}"',
        f'latest_state="{_escape_string_field(latest_state)}"',
    ]
    for name, value in (
        ("asn", current.get("asn") if current.get("asn") is not None else previous.get("asn")),
        ("prefixes_before", previous.get("prefixes_received")),
        ("prefixes_after", current.get("prefixes_received")),
    ):
        field = _int_field(name, value)
        if field:
            fields.append(field)
    return f"bgp_session_events,{','.join(tags)} {','.join(fields)} {timestamp_ns}"


class BgpTransitionTracker:
    """Maintain one collector process' latest BGP states and emit transitions."""

    def __init__(self) -> None:
        self._previous: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def process(
        self,
        device: str,
        rows: Iterable[dict],
        timestamp_ns: int,
        topology_id: str,
        *,
        collection_ok: bool,
    ) -> list[str]:
        current_rows = {str(row["neighbor"]): dict(row) for row in rows if row.get("neighbor")}
        lines = [
            build_bgp_event_index_line(
                device,
                timestamp_ns,
                topology_id,
                collection_ok,
                len(current_rows),
            )
        ]
        if not collection_ok:
            return lines

        with self._lock:
            for neighbor, current in current_rows.items():
                key = (device, neighbor)
                previous = self._previous.get(key)
                if previous and normalize_bgp_state(previous.get("state")) != normalize_bgp_state(current.get("state")):
                    lines.append(
                        build_bgp_transition_line(
                            device,
                            neighbor,
                            previous,
                            current,
                            timestamp_ns,
                            topology_id,
                        )
                    )
                self._previous[key] = current

            missing_keys = [key for key in self._previous if key[0] == device and key[1] not in current_rows]
            for key in missing_keys:
                previous = self._previous[key]
                if normalize_bgp_state(previous.get("state")) != "MISSING":
                    missing = {**previous, "state": "MISSING", "prefixes_received": None}
                    lines.append(
                        build_bgp_transition_line(
                            device,
                            key[1],
                            previous,
                            missing,
                            timestamp_ns,
                            topology_id,
                        )
                    )
                    self._previous[key] = missing
        return lines


def _read_topology(metadata_file: Path) -> tuple[str, list[str]]:
    manifest = load_topology_manifest(metadata_file)
    lab_name = manifest.name.strip()
    names = [device.name for device in manifest.routing_devices()]
    return lab_name, names


def _collect_device_bgp(
    lab_name: str,
    device: str,
    docker_prefix: list[str],
    timestamp_ns: int,
    topology_id: str,
    transition_tracker: BgpTransitionTracker | None = None,
) -> list[str]:
    container = f"clab-{lab_name}-{device}"  # matches clab_container_name() convention
    started = time.monotonic()
    error_type = ""
    rows: list[dict] = []
    try:
        result = subprocess.run(
            [*docker_prefix, "docker", "exec", container, "vtysh", "-c", "show ip bgp summary"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            error_type = "command_failed"
        else:
            rows = parse_bgp_summary(result.stdout)
            if result.stdout.strip() and "Neighbor" not in result.stdout and not rows:
                error_type = "parser_failed"
    except subprocess.TimeoutExpired:
        error_type = "timeout"
    except Exception:
        error_type = "collector_error"
    duration_ms = round((time.monotonic() - started) * 1000)
    lines = build_bgp_lines(device, rows, timestamp_ns, topology_id=topology_id)
    if transition_tracker is not None:
        lines.extend(
            transition_tracker.process(
                device,
                rows,
                timestamp_ns,
                topology_id,
                collection_ok=not error_type,
            )
        )
    lines.append(
        build_bgp_collection_line(
            device,
            timestamp_ns,
            topology_id,
            not error_type,
            len(rows),
            duration_ms,
            error_type,
        )
    )
    return lines


def _collect_device_bgp_with_tracker(
    lab_name: str,
    device: str,
    docker_prefix: list[str],
    timestamp_ns: int,
    topology_id: str,
    transition_tracker: BgpTransitionTracker | None,
) -> list[str]:
    """Keep the legacy five-argument collector hook compatible with tests/extensions."""
    if transition_tracker is None:
        return _collect_device_bgp(lab_name, device, docker_prefix, timestamp_ns, topology_id)
    return _collect_device_bgp(
        lab_name,
        device,
        docker_prefix,
        timestamp_ns,
        topology_id,
        transition_tracker,
    )


def collect_bgp_lines(
    metadata_file: Path,
    timestamp_ns: int | None = None,
    parallelism: int = 1,
    topology_id: str | None = None,
    transition_tracker: BgpTransitionTracker | None = None,
) -> list[str]:
    lab_name, devices = _read_topology(metadata_file)
    resolved_topology_id = topology_id or lab_name
    command_prefix = docker_prefix()
    resolved_timestamp = time.time_ns() if timestamp_ns is None else int(timestamp_ns)
    workers = max(1, min(int(parallelism), len(devices) or 1))

    if workers == 1:
        device_lines = [
            _collect_device_bgp_with_tracker(
                lab_name,
                device,
                command_prefix,
                resolved_timestamp,
                resolved_topology_id,
                transition_tracker,
            )
            for device in devices
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            device_lines = list(
                executor.map(
                    lambda device: _collect_device_bgp_with_tracker(
                        lab_name,
                        device,
                        command_prefix,
                        resolved_timestamp,
                        resolved_topology_id,
                        transition_tracker,
                    ),
                    devices,
                )
            )

    lines: list[str] = []
    for entries in device_lines:
        lines.extend(entries)
    return lines


def _collect_bgp_lines_paced(
    metadata_file: Path,
    interval_seconds: float,
    parallelism: int,
    stop_event: threading.Event,
    topology_id: str | None = None,
    transition_tracker: BgpTransitionTracker | None = None,
) -> list[str]:
    """Collect one fleet snapshot while spreading docker exec starts over the interval."""
    lab_name, devices = _read_topology(metadata_file)
    if not devices:
        return []

    resolved_topology_id = topology_id or lab_name
    command_prefix = docker_prefix()
    workers = max(1, min(int(parallelism), len(devices)))
    launch_spacing = max(0.0, float(interval_seconds)) / len(devices)
    round_started = time.monotonic()
    futures = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, device in enumerate(devices):
            launch_at = round_started + index * launch_spacing
            wait_seconds = max(0.0, launch_at - time.monotonic())
            if wait_seconds and stop_event.wait(wait_seconds):
                break
            futures.append(
                executor.submit(
                    _collect_device_bgp_with_tracker,
                    lab_name,
                    device,
                    command_prefix,
                    time.time_ns(),
                    resolved_topology_id,
                    transition_tracker,
                )
            )

    lines: list[str] = []
    for future in futures:
        lines.extend(future.result())
    return lines


def _write_lines(output_file: Path, lines: list[str], max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(lines)
    if rendered:
        rendered += "\n"
    mode = "a"
    if max_bytes > 0 and output_file.exists():
        current_size = output_file.stat().st_size
        if current_size + len(rendered.encode("utf-8")) > max_bytes:
            mode = "w"
    with output_file.open(mode, encoding="utf-8") as handle:
        if rendered:
            handle.write(rendered)


def run_once(
    metadata_file: Path,
    output_file: Path,
    parallelism: int = 1,
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
    topology_id: str | None = None,
) -> int:
    _write_lines(
        output_file,
        collect_bgp_lines(metadata_file, parallelism=parallelism, topology_id=topology_id),
        max_bytes=max_bytes,
    )
    return 0


def run_loop(
    metadata_file: Path,
    output_file: Path,
    interval_seconds: float,
    parallelism: int = 1,
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
    topology_id: str | None = None,
) -> int:
    stop_event = threading.Event()
    transition_tracker = BgpTransitionTracker()

    def _stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.touch(exist_ok=True)

    while not stop_event.is_set():
        iteration_started = time.monotonic()
        try:
            _write_lines(
                output_file,
                _collect_bgp_lines_paced(
                    metadata_file,
                    interval_seconds,
                    parallelism,
                    stop_event,
                    topology_id=topology_id,
                    transition_tracker=transition_tracker,
                ),
                max_bytes=max_bytes,
            )
        except Exception as exc:
            print(f"WARN: bgp collector iteration failed: {exc}", file=sys.stderr)
        elapsed = time.monotonic() - iteration_started
        stop_event.wait(max(0.0, interval_seconds - elapsed))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit BGP neighbor snapshots as Influx line protocol")
    parser.add_argument("metadata_file", help="Path to topology.json")
    parser.add_argument("--output", required=True, help="Output line protocol file")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_BGP_POLL_INTERVAL_SECONDS,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        default=DEFAULT_BGP_COLLECTOR_PARALLELISM,
        help="Maximum concurrent docker exec workers; starts are spread over each polling interval",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_BGP_COLLECTOR_MAX_BYTES,
        help="Maximum BGP line protocol file size before truncating to the latest snapshot; <=0 disables.",
    )
    parser.add_argument("--once", action="store_true", help="Collect one snapshot and exit")
    parser.add_argument("--topology-id", help="Explicit topology identity for emitted line protocol")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.once:
        return run_once(
            Path(args.metadata_file),
            Path(args.output),
            parallelism=args.parallelism,
            max_bytes=args.max_bytes,
            topology_id=args.topology_id,
        )
    return run_loop(
        Path(args.metadata_file),
        Path(args.output),
        args.interval,
        parallelism=args.parallelism,
        max_bytes=args.max_bytes,
        topology_id=args.topology_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())

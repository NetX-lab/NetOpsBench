#!/usr/bin/env python3
"""Poll BGP summary from SONiC nodes and emit Influx line protocol snapshots."""

from __future__ import annotations

import argparse
import csv
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.config import config
from netopsbench.platform.observability.bgp_parser import parse_bgp_summary
from netopsbench.platform.observability.influxdb import query_flux
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.proc import docker_prefix

DEFAULT_BGP_COLLECTOR_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_BGP_SEGMENT_BYTES = 16 * 1024 * 1024
DEFAULT_BGP_COLLECTOR_PARALLELISM = 16
DEFAULT_BGP_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS = 60.0
DEFAULT_BGP_SPARSE_DEVICE_THRESHOLD = 128
DEFAULT_BGP_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BGP_LOG_BACKUP_COUNT = 2
_BGP_EVENT_SCHEMA_VERSION = 1


class _LogWriter:
    """Line-buffered file-like adapter for child-process stdout/stderr."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += str(text)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self.logger.log(self.level, line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self.logger.log(self.level, self._buffer)
            self._buffer = ""


def configure_rotating_log(
    path: Path,
    *,
    max_bytes: int = DEFAULT_BGP_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_BGP_LOG_BACKUP_COUNT,
) -> None:
    """Route collector process output through a bounded rotating log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("netopsbench.bgp_collector.process")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    sys.stdout = _LogWriter(logger, logging.INFO)
    sys.stderr = _LogWriter(logger, logging.WARNING)


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
        for key in ("asn", "prefixes_received"):
            field = _int_field(key, row.get(key))
            if field:
                fields.append(field)
        lines.append(f"bgp_neighbors,{','.join(tags)} {','.join(fields)} {timestamp_ns}")
    return lines


def build_bgp_collection_line(
    device: str,
    timestamp_ns: int,
    topology_id: str,
    collection_ok: bool,
    error_type: str,
) -> str:
    tags = [f"source={_escape_tag(device)}"]
    if topology_id:
        tags.append(f"topology_id={_escape_tag(topology_id)}")
    fields = [
        f"collection_ok={'true' if collection_ok else 'false'}",
        f'error_type="{_escape_string_field(error_type)}"',
    ]
    return f"bgp_collection,{','.join(tags)} {','.join(fields)} {timestamp_ns}"


def build_bgp_event_index_line(
    device: str,
    timestamp_ns: int,
    topology_id: str,
    collection_ok: bool,
) -> str:
    """Emit a heartbeat proving that transition indexing covered one poll."""
    tags = [f"source={_escape_tag(device)}"]
    if topology_id:
        tags.append(f"topology_id={_escape_tag(topology_id)}")
    fields = [
        f"schema_version={_BGP_EVENT_SCHEMA_VERSION}i",
        f"collection_ok={'true' if collection_ok else 'false'}",
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
    include_full_snapshot: bool = True,
) -> list[str]:
    container = f"clab-{lab_name}-{device}"  # matches clab_container_name() convention
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
    snapshot_rows = (
        rows
        if include_full_snapshot
        else [row for row in rows if normalize_bgp_state(row.get("state")) != "ESTABLISHED"]
    )
    lines = build_bgp_lines(device, snapshot_rows, timestamp_ns, topology_id=topology_id)
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
            error_type,
        )
    )
    return lines


def collect_bgp_lines(
    metadata_file: Path,
    timestamp_ns: int | None = None,
    parallelism: int = 1,
    topology_id: str | None = None,
    transition_tracker: BgpTransitionTracker | None = None,
    include_full_snapshot: bool = True,
) -> list[str]:
    lab_name, devices = _read_topology(metadata_file)
    resolved_topology_id = topology_id or lab_name
    command_prefix = docker_prefix()
    resolved_timestamp = time.time_ns() if timestamp_ns is None else int(timestamp_ns)
    workers = max(1, min(int(parallelism), len(devices) or 1))

    if workers == 1:
        device_lines = [
            _collect_device_bgp(
                lab_name,
                device,
                command_prefix,
                resolved_timestamp,
                resolved_topology_id,
                transition_tracker,
                include_full_snapshot,
            )
            for device in devices
        ]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            device_lines = list(
                executor.map(
                    lambda device: _collect_device_bgp(
                        lab_name,
                        device,
                        command_prefix,
                        resolved_timestamp,
                        resolved_topology_id,
                        transition_tracker,
                        include_full_snapshot,
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
    include_full_snapshot: bool = True,
    on_lines: Callable[[list[str]], None] | None = None,
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
    pending = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, device in enumerate(devices):
            launch_at = round_started + index * launch_spacing
            wait_seconds = max(0.0, launch_at - time.monotonic())
            if wait_seconds and stop_event.wait(wait_seconds):
                break
            future = executor.submit(
                _collect_device_bgp,
                lab_name,
                device,
                command_prefix,
                time.time_ns(),
                resolved_topology_id,
                transition_tracker,
                include_full_snapshot,
            )
            futures.append(future)
            pending.append(future)
            if on_lines is not None:
                completed = [item for item in pending if item.done()]
                for item in completed:
                    on_lines(item.result())
                    pending.remove(item)

    lines: list[str] = []
    if on_lines is not None:
        for future in pending:
            on_lines(future.result())
        return lines
    for future in futures:
        lines.extend(future.result())
    return lines


def _write_lines(
    output_file: Path,
    lines: list[str],
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
    topology_id: str = "",
) -> None:
    _write_segmented_lines(output_file, lines, max_bytes=max_bytes, topology_id=topology_id)


def _line_timestamp(line: str) -> int | None:
    match = re.search(r"\s(\d+)\s*$", line)
    return int(match.group(1)) if match else None


def _file_max_timestamp(path: Path) -> int | None:
    latest: int | None = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            timestamp = _line_timestamp(line)
            if timestamp is not None and (latest is None or timestamp > latest):
                latest = timestamp
    return latest


def _sealed_segments(output_file: Path) -> list[Path]:
    return sorted(output_file.parent.glob(f"{output_file.stem}.sealed.*{output_file.suffix}"))


def _sealed_segment_identity(path: Path) -> tuple[str, int] | None:
    match = re.fullmatch(r".+\.sealed\.(\d+)-(\d+)\.lp", path.name)
    return (f"{match.group(1)}-{match.group(2)}", int(match.group(1))) if match else None


def _rotate_active_segment(output_file: Path, topology_id: str, max_bytes: int) -> None:
    if not output_file.exists() or output_file.stat().st_size == 0:
        return
    timestamp = _file_max_timestamp(output_file)
    if timestamp is None:
        raise RuntimeError(f"BGP spool contains a line without a timestamp: {output_file}")
    sequence = 0
    while True:
        segment_id = f"{timestamp}-{sequence}"
        sealed = output_file.with_name(f"{output_file.stem}.sealed.{segment_id}{output_file.suffix}")
        if not sealed.exists():
            break
        sequence += 1
    marker_tags = ["source=__spool__", f"spool_segment={segment_id}"]
    if topology_id:
        marker_tags.append(f"topology_id={_escape_tag(topology_id)}")
    marker = f"bgp_event_index,{','.join(marker_tags)} schema_version=1i {timestamp}\n"
    if max_bytes > 0 and _spool_size(output_file) + len(marker.encode("utf-8")) > max_bytes:
        raise BufferError(
            f"BGP spool limit reached for {output_file} while sealing a segment; "
            "refusing to overwrite unconsumed telemetry"
        )
    with output_file.open("a", encoding="utf-8") as handle:
        handle.write(marker)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(output_file, sealed)
    output_file.touch()


def _spool_size(output_file: Path) -> int:
    paths = [output_file, *_sealed_segments(output_file)]
    return sum(path.stat().st_size for path in paths if path.exists())


def _write_segmented_lines(
    output_file: Path,
    lines: list[str],
    *,
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
    segment_bytes: int = DEFAULT_BGP_SEGMENT_BYTES,
    topology_id: str = "",
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(lines)
    if rendered:
        rendered += "\n"
    rendered_size = len(rendered.encode("utf-8"))
    if max_bytes > 0 and _spool_size(output_file) + rendered_size > max_bytes:
        raise BufferError(
            f"BGP spool limit reached for {output_file}: "
            f"current={_spool_size(output_file)} limit={max_bytes}; "
            "refusing to overwrite unconsumed telemetry"
        )
    if (
        segment_bytes > 0
        and output_file.exists()
        and output_file.stat().st_size > 0
        and output_file.stat().st_size + rendered_size > segment_bytes
    ):
        _rotate_active_segment(output_file, topology_id, max_bytes)
    with output_file.open("a", encoding="utf-8") as handle:
        if rendered:
            handle.write(rendered)


def _delete_ingested_segments(output_file: Path, ingested_segments: set[str]) -> list[Path]:
    if not ingested_segments:
        return []
    removed: list[Path] = []
    for segment in _sealed_segments(output_file):
        identity = _sealed_segment_identity(segment)
        if identity is not None and identity[0] in ingested_segments:
            segment.unlink()
            removed.append(segment)
    return removed


def _query_ingested_segments(bucket: str, topology_id: str) -> set[str]:
    safe_bucket = bucket.replace("\\", "\\\\").replace('"', '\\"')
    safe_topology = topology_id.replace("\\", "\\\\").replace('"', '\\"')
    query = f"""
from(bucket: "{safe_bucket}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "bgp_event_index")
  |> filter(fn: (r) => r.topology_id == "{safe_topology}")
  |> filter(fn: (r) => r._field == "schema_version")
  |> filter(fn: (r) => exists r.spool_segment)
  |> group(columns: ["spool_segment"])
  |> last()
  |> keep(columns: ["_time", "spool_segment"])
"""
    result = query_flux(config.influxdb_url, config.influxdb_token, config.influxdb_org, query)
    if result.status != "ok":
        return set()
    lines = [line for line in result.text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return set()
    ingested: set[str] = set()
    for row in csv.DictReader(lines):
        segment_id = str(row.get("spool_segment") or "")
        timestamp_text = str(row.get("_time") or "")
        match = re.fullmatch(r"(\d+)-\d+", segment_id)
        if not match or not timestamp_text:
            continue
        time_match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z", timestamp_text)
        if not time_match:
            continue
        whole_seconds = int(datetime.fromisoformat(time_match.group(1)).replace(tzinfo=UTC).timestamp())
        fractional_ns = int((time_match.group(2) or "").ljust(9, "0"))
        ingested_at = whole_seconds * 1_000_000_000 + fractional_ns
        if ingested_at >= int(match.group(1)):
            ingested.add(segment_id)
    return ingested


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
        topology_id=topology_id or "",
    )
    return 0


def run_loop(
    metadata_file: Path,
    output_file: Path,
    interval_seconds: float,
    parallelism: int = 1,
    max_bytes: int = DEFAULT_BGP_COLLECTOR_MAX_BYTES,
    topology_id: str | None = None,
    influxdb_bucket: str | None = None,
) -> int:
    stop_event = threading.Event()
    transition_tracker = BgpTransitionTracker()

    def _stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.touch(exist_ok=True)
    _, routing_devices = _read_topology(metadata_file)
    use_sparse_snapshots = len(routing_devices) > DEFAULT_BGP_SPARSE_DEVICE_THRESHOLD
    last_full_snapshot_at = float("-inf")

    while not stop_event.is_set():
        iteration_started = time.monotonic()
        include_full_snapshot = not use_sparse_snapshots or (
            iteration_started - last_full_snapshot_at >= DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS
        )
        try:
            _collect_bgp_lines_paced(
                metadata_file,
                interval_seconds,
                parallelism,
                stop_event,
                topology_id=topology_id,
                transition_tracker=transition_tracker,
                include_full_snapshot=include_full_snapshot,
                on_lines=lambda lines: _write_lines(
                    output_file,
                    lines,
                    max_bytes=max_bytes,
                    topology_id=topology_id or "",
                ),
            )
            if include_full_snapshot:
                last_full_snapshot_at = iteration_started
            if influxdb_bucket and topology_id and _sealed_segments(output_file):
                ingested_segments = _query_ingested_segments(influxdb_bucket, topology_id)
                _delete_ingested_segments(output_file, ingested_segments)
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
        help="Maximum total BGP spool size before failing closed; <=0 disables.",
    )
    parser.add_argument("--once", action="store_true", help="Collect one snapshot and exit")
    parser.add_argument("--topology-id", help="Explicit topology identity for emitted line protocol")
    parser.add_argument("--influxdb-bucket", help="Worker bucket used to confirm sealed spool ingestion")
    parser.add_argument("--log-file", type=Path, help="Bounded rotating collector process log")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.log_file is not None:
        configure_rotating_log(args.log_file)
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
        influxdb_bucket=args.influxdb_bucket,
    )


if __name__ == "__main__":
    raise SystemExit(main())

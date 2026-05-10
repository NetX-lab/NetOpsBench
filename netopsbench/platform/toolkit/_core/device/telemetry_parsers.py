"""Influx/telemetry parsing helpers for device toolkit internals."""

from __future__ import annotations

import csv
import re
from datetime import datetime
from io import StringIO
from typing import Any

import requests


def parse_influx_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"^(.*?\.\d{6})\d+(Z|[+-]\d{2}:\d{2})$", text)
    if match:
        text = f"{match.group(1)}{match.group(2)}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_influx_metric_rows(csv_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return rows
    reader = csv.DictReader(lines)
    for row in reader:
        if row.get("result", "") == "result":
            continue
        field = row.get("_field")
        timestamp = row.get("_time")
        raw_value = row.get("_value")
        if not field or field == "_field" or not timestamp or timestamp == "_time":
            continue
        if raw_value in (None, "", "_value"):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        rows.append({"_field": field, "_time": timestamp, "_value": value})
    return rows


def parse_influx_interface_identity_rows(csv_text: str) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return rows
    reader = csv.DictReader(lines)
    seen = set()
    for row in reader:
        if row.get("result", "") == "result":
            continue
        raw_name = str(row.get("name") or "").strip()
        raw_path = str(row.get("path") or "").strip()
        if raw_name == "name":
            raw_name = ""
        if raw_path == "path":
            raw_path = ""
        name = raw_name or None
        if not name and raw_path:
            match = re.search(r"(Ethernet\d+)", raw_path)
            if match:
                name = match.group(1)
        identity_key = (name, raw_path or None)
        if identity_key == (None, None) or identity_key in seen:
            continue
        seen.add(identity_key)
        rows.append({"name": name, "path": raw_path or None, "time": row.get("_time")})
    return rows


def get_recent_influx_interface_identities(
    toolkit, device: str, time_range_minutes: int, headers: dict[str, str] | None = None
) -> list[dict[str, str | None]]:
    safe_device = toolkit._validate_device_name(device)
    safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
    request_headers = headers or {
        "Authorization": f"Token {toolkit.influxdb_token}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    query = f"""\nfrom(bucket: "{toolkit.influxdb_bucket}")\n  |> range(start: -{safe_minutes}m)\n  |> filter(fn: (r) => r._measurement == "interfaces")\n  |> filter(fn: (r) => r.source == "{safe_device}")\n  |> keep(columns: ["_time", "name", "path", "source"])\n  |> limit(n: 2000)\n"""
    response = requests.post(
        f"{toolkit.influxdb_url}/api/v2/query?org={toolkit.influxdb_org}",
        headers=request_headers,
        data=query,
        timeout=30,
        proxies={"http": "", "https": ""},
    )
    if response.status_code != 200:
        return []
    return parse_influx_interface_identity_rows(response.text)


def summarize_counter_points(field: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    numeric_points = [point for point in points if point.get("value") is not None]
    if not numeric_points:
        return {}
    start_point = numeric_points[0]
    end_point = numeric_points[-1]
    values = [point["value"] for point in numeric_points]
    delta = 0.0
    resets = 0
    previous_value = values[0]
    for current_value in values[1:]:
        step_delta = current_value - previous_value
        if step_delta >= 0:
            delta += step_delta
        else:
            resets += 1
        previous_value = current_value
    start_time = parse_influx_timestamp(start_point.get("time"))
    end_time = parse_influx_timestamp(end_point.get("time"))
    elapsed_seconds: float | None = None
    if start_time and end_time:
        elapsed_seconds = max(0.0, (end_time - start_time).total_seconds())
    summary = {
        "window_start_time": start_point.get("time"),
        "window_end_time": end_point.get("time"),
        "counter_start": start_point.get("value"),
        "counter_end": end_point.get("value"),
        "counter_min": min(values),
        "counter_max": max(values),
        "window_delta": delta,
        "points": len(values),
        "elapsed_seconds": elapsed_seconds,
        "avg_per_second": (delta / elapsed_seconds) if elapsed_seconds and elapsed_seconds > 0 else None,
    }
    if field.endswith("_octets") and summary["avg_per_second"] is not None:
        summary["avg_bps"] = summary["avg_per_second"] * 8.0
    if resets:
        summary["counter_resets_detected"] = resets
    return summary


def query_influx_rows(toolkit, query: str, require_value: bool = True) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Token {toolkit.influxdb_token}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    response = requests.post(
        f"{toolkit.influxdb_url}/api/v2/query?org={toolkit.influxdb_org}",
        headers=headers,
        data=query,
        timeout=30,
        proxies={"http": "", "https": ""},
    )
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(StringIO(response.text))
    for row in reader:
        if row.get("result", "") == "result":
            continue
        if require_value and not row.get("_value"):
            continue
        parsed = dict(row)
        for key, value in list(parsed.items()):
            if value in (None, ""):
                continue
            try:
                parsed[key] = float(value)
            except (TypeError, ValueError):
                continue
        has_payload = any(
            value not in (None, "", []) for key, value in parsed.items() if key not in {"result", "table"}
        )
        if not has_payload:
            continue
        rows.append(parsed)
    return rows

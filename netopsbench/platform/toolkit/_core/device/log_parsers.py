"""Syslog/log helpers for device toolkit internals."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any


def parse_influx_syslog_rows(csv_text: str) -> list[dict[str, Any]]:
    import csv

    rows: list[dict[str, Any]] = []
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return rows
    reader = csv.DictReader(lines)
    for row in reader:
        if row.get("result", "") == "result":
            continue
        timestamp = row.get("_time")
        message = row.get("_value")
        if not timestamp or timestamp == "_time" or message in (None, "", "_value"):
            continue
        rows.append(
            {
                "time": timestamp,
                "message": message,
                "severity": row.get("severity"),
                "appname": row.get("appname"),
                "hostname": row.get("hostname") or row.get("host"),
                "source": row.get("source"),
            }
        )
    return rows


def parse_local_syslog_lines(
    text: str, cutoff: datetime | None = None, severity: str | None = None
) -> list[dict[str, Any]]:
    if not text:
        return []
    now_utc = datetime.now(UTC)
    current_year = now_utc.year
    current_month = now_utc.month
    entries: list[dict[str, Any]] = []
    severity_filter = str(severity or "").lower().strip() or None
    pattern = re.compile(
        r"^(?P<month>[A-Z][a-z]{2})\s+"
        r"(?P<day>\d{1,2})\s+"
        r"(?P<clock>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
        r"(?P<host>\S+)\s+"
        r"(?P<severity>[A-Z]+)\s+"
        r"#(?P<app>[^:]+):\s*"
        r"(?P<message>.*)$"
    )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = pattern.match(stripped)
        if not match:
            continue
        try:
            month = datetime.strptime(match.group("month"), "%b").month
            naive_dt = datetime.strptime(
                f"{current_year} {match.group('month')} {int(match.group('day')):02d} {match.group('clock')}",
                "%Y %b %d %H:%M:%S.%f" if "." in match.group("clock") else "%Y %b %d %H:%M:%S",
            )
        except ValueError:
            continue
        if month - current_month > 6:
            naive_dt = naive_dt.replace(year=current_year - 1)
        elif current_month - month > 6:
            naive_dt = naive_dt.replace(year=current_year + 1)
        log_dt = naive_dt.replace(tzinfo=UTC)
        if cutoff and log_dt < cutoff:
            continue
        log_severity = match.group("severity").lower()
        if severity_filter and log_severity != severity_filter:
            continue
        entries.append(
            {
                "time": log_dt.isoformat().replace("+00:00", "Z"),
                "message": match.group("message"),
                "severity": log_severity,
                "appname": match.group("app"),
                "hostname": match.group("host"),
                "source": match.group("host"),
            }
        )
    entries.sort(key=lambda item: item.get("time") or "", reverse=True)
    return entries[:100]


def get_device_logs_fallback(
    toolkit, device: str, time_range_minutes: int, severity: str | None = None
) -> list[dict[str, Any]]:
    container = toolkit._resolve_container(device)
    safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
    cutoff = datetime.now(UTC) - timedelta(minutes=safe_minutes)
    candidate_files = ["/var/log/syslog"]
    if str(device).startswith(("leaf", "spine")):
        candidate_files.append("/var/log/frr/frr.log")
    quoted_files = " ".join(candidate_files)
    command = (
        "for f in "
        + quoted_files
        + "; do "
        + '[ -f "$f" ] || continue; '
        + 'tail -n 400 "$f"; '
        + 'printf "\\n"; '
        + "done"
    )
    result = toolkit._docker_exec(container, ["bash", "-lc", command], timeout=30)
    if result.returncode != 0:
        return []
    return parse_local_syslog_lines(result.stdout, cutoff=cutoff, severity=severity)

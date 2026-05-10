"""Pingmesh time-window helpers extracted from AgentToolkit."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any


def parse_iso8601_timestamp(value: str | None, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 timestamp")
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def resolve_pingmesh_time_scope(
    toolkit,
    time_range_minutes: int,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    """Resolve Pingmesh queries to either an absolute window or a rolling lookback."""
    explicit_start = str(start_time or "").strip()
    explicit_end = str(end_time or "").strip()
    env_start = str(os.environ.get(toolkit._PINGMESH_RANGE_ENV_START, "") or "").strip()
    env_end = str(os.environ.get(toolkit._PINGMESH_RANGE_ENV_END, "") or "").strip()

    if explicit_start or explicit_end:
        if not (explicit_start and explicit_end):
            raise ValueError("start_time and end_time must be provided together")
        start_dt = parse_iso8601_timestamp(explicit_start, "start_time")
        end_dt = parse_iso8601_timestamp(explicit_end, "end_time")
        if start_dt >= end_dt:
            raise ValueError("start_time must be earlier than end_time")
        normalized_start = start_dt.isoformat().replace("+00:00", "Z")
        normalized_end = end_dt.isoformat().replace("+00:00", "Z")
        return {
            "mode": "absolute",
            "source": "explicit",
            "start_time": normalized_start,
            "end_time": normalized_end,
            "range_clause": (
                f'  |> range(start: time(v: "{normalized_start}"), ' f'stop: time(v: "{normalized_end}"))\n'
            ),
        }

    if env_start or env_end:
        if not (env_start and env_end):
            raise ValueError(
                f"{toolkit._PINGMESH_RANGE_ENV_START} and {toolkit._PINGMESH_RANGE_ENV_END} must be set together"
            )
        start_dt = parse_iso8601_timestamp(env_start, toolkit._PINGMESH_RANGE_ENV_START)
        end_dt = parse_iso8601_timestamp(env_end, toolkit._PINGMESH_RANGE_ENV_END)
        if start_dt >= end_dt:
            raise ValueError(
                f"{toolkit._PINGMESH_RANGE_ENV_START} must be earlier than {toolkit._PINGMESH_RANGE_ENV_END}"
            )
        normalized_start = start_dt.isoformat().replace("+00:00", "Z")
        normalized_end = end_dt.isoformat().replace("+00:00", "Z")
        return {
            "mode": "absolute",
            "source": "env",
            "start_time": normalized_start,
            "end_time": normalized_end,
            "range_clause": (
                f'  |> range(start: time(v: "{normalized_start}"), ' f'stop: time(v: "{normalized_end}"))\n'
            ),
        }

    safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
    return {
        "mode": "rolling",
        "source": "time_range_minutes",
        "time_range_minutes": safe_minutes,
        "range_clause": f"  |> range(start: -{safe_minutes}m)\n",
    }

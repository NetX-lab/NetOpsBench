"""Pingmesh observability helpers for AgentToolkit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import requests

from ..common import ToolResult
from .pingmesh_scope import parse_iso8601_timestamp

_PINGMESH_AGGREGATE_MAX_AGE_SECONDS = 45


class PingmeshOpsMixin:
    def get_pingmesh_summary(
        self,
        time_range_minutes: int = 10,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> ToolResult:
        try:
            time_scope = self._resolve_pingmesh_time_scope(
                time_range_minutes=time_range_minutes,
                start_time=start_time,
                end_time=end_time,
            )
            topology_filter = ""
            if self.topology_id:
                safe = str(self.topology_id).replace("\\", "\\\\").replace('"', '\\"')
                topology_filter = f'  |> filter(fn: (r) => r.topology_id == "{safe}")\n'
            aggregate_query = f"""
from(bucket: "{self.influxdb_bucket}")
{time_scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "pingmesh_path_type_30s")
{topology_filter}  |> filter(fn: (r) => r._field == "rtt_p99" or r._field == "packet_loss")
  |> last()
"""
            try:
                rows = self._query_influx_rows(aggregate_query)
            except Exception:
                rows = []
            if self._pingmesh_aggregate_rows_fresh(rows, time_scope):
                rows = self._normalize_pingmesh_aggregate_rows(rows)
            else:
                query = f"""
from(bucket: "{self.influxdb_bucket}")
{time_scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "pingmesh")
{topology_filter}  |> filter(fn: (r) => r._field == "rtt_p99" or r._field == "packet_loss")
  |> group(columns: ["path_type", "_field"])
  |> aggregateWindow(every: 30s, fn: mean, createEmpty: false)
  |> last()
"""
                rows = self._query_influx_rows(query)
            summary: dict[str, dict[str, float | None]] = {}
            for row in rows:
                path_type = row.get("path_type") or "unknown"
                field = row.get("_field")
                if path_type not in summary:
                    summary[path_type] = {"rtt_p99": None, "packet_loss": None}
                if field in {"rtt_p99", "packet_loss"}:
                    value = row.get("_value")
                    if isinstance(value, (float, int)):
                        summary[path_type][field] = float(value)

            return ToolResult(
                success=True,
                data={
                    "time_scope": {key: value for key, value in time_scope.items() if key != "range_clause"},
                    "path_type_summary": summary,
                    "rows": rows,
                },
            )
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_pingmesh_hotspots(
        self,
        time_range_minutes: int = 10,
        limit: int = 10,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> ToolResult:
        try:
            time_scope = self._resolve_pingmesh_time_scope(
                time_range_minutes=time_range_minutes,
                start_time=start_time,
                end_time=end_time,
            )
            safe_limit = max(1, min(int(limit), 50))
            topology_filter = ""
            if self.topology_id:
                safe = str(self.topology_id).replace("\\", "\\\\").replace('"', '\\"')
                topology_filter = f'  |> filter(fn: (r) => r.topology_id == "{safe}")\n'
            score_ranges = self._pingmesh_recent_closed_aggregate_ranges(time_scope)
            rows = []
            try:
                for score_range in score_ranges:
                    score_query = f"""
from(bucket: "{self.influxdb_bucket}")
{score_range}  |> filter(fn: (r) => r._measurement == "pingmesh_leaf_pair_30s")
{topology_filter}  |> filter(fn: (r) => r._field == "hotspot_score")
  |> group()
  |> top(n: {safe_limit}, columns: ["_value"])
"""
                    score_rows = self._query_influx_rows(score_query, require_value=False)
                    if self._pingmesh_aggregate_rows_fresh(score_rows, time_scope) and all(
                        row.get("_field") == "hotspot_score" for row in score_rows
                    ):
                        rows = self._query_pingmesh_hotspot_candidates(
                            score_rows,
                            time_scope,
                            topology_filter,
                            safe_limit,
                        )
                        break
            except Exception:
                rows = []
            aggregate_query = f"""
from(bucket: "{self.influxdb_bucket}")
{time_scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "pingmesh_leaf_pair_30s")
{topology_filter}  |> filter(fn: (r) => r._field == "rtt_p99" or r._field == "packet_loss")
  |> last()
  |> group(columns: ["src_leaf", "dst_leaf"])
  |> pivot(rowKey: ["_time", "src_leaf", "dst_leaf"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> sort(columns: ["packet_loss", "rtt_p99"], desc: true)
  |> limit(n: {safe_limit})
"""
            if rows:
                rows = self._normalize_pingmesh_aggregate_rows(rows)
            else:
                try:
                    rows = self._query_influx_rows(aggregate_query, require_value=False)
                except Exception:
                    rows = []
                if self._pingmesh_aggregate_rows_fresh(rows, time_scope):
                    rows = self._normalize_pingmesh_aggregate_rows(rows)
                else:
                    query = f"""
from(bucket: "{self.influxdb_bucket}")
{time_scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "pingmesh")
{topology_filter}  |> filter(fn: (r) => r._field == "rtt_p99" or r._field == "packet_loss")
  |> group(columns: ["src_leaf", "dst_leaf", "_field"])
  |> aggregateWindow(every: 30s, fn: mean, createEmpty: false)
  |> last()
  |> pivot(rowKey: ["src_leaf", "dst_leaf"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> sort(columns: ["packet_loss", "rtt_p99"], desc: true)
  |> limit(n: {safe_limit})
"""
                    rows = self._query_influx_rows(query, require_value=False)
            hotspots = []
            for row in rows:
                hotspots.append(
                    {
                        "src_leaf": row.get("src_leaf"),
                        "dst_leaf": row.get("dst_leaf"),
                        "rtt_p99": row.get("rtt_p99"),
                        "packet_loss": row.get("packet_loss"),
                    }
                )

            return ToolResult(
                success=True,
                data={
                    "time_scope": {key: value for key, value in time_scope.items() if key != "range_clause"},
                    "limit": safe_limit,
                    "hotspots": hotspots,
                },
            )
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    @staticmethod
    def _normalize_pingmesh_aggregate_rows(rows: list[dict]) -> list[dict]:
        return [dict(row, _measurement="pingmesh") for row in rows]

    def _query_pingmesh_hotspot_candidates(
        self,
        score_rows: list[dict],
        time_scope: dict,
        topology_filter: str,
        limit: int,
    ) -> list[dict]:
        candidates = {
            (str(row.get("src_leaf") or ""), str(row.get("dst_leaf") or ""))
            for row in score_rows
            if row.get("src_leaf") and row.get("dst_leaf")
        }
        if not candidates:
            return []
        predicates = [
            f'(r.src_leaf == "{self._pingmesh_flux_string(src_leaf)}" and '
            f'r.dst_leaf == "{self._pingmesh_flux_string(dst_leaf)}")'
            for src_leaf, dst_leaf in sorted(candidates)
        ]
        candidate_filter = f"  |> filter(fn: (r) => {' or '.join(predicates)})\n"
        query = f"""
from(bucket: "{self.influxdb_bucket}")
{time_scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "pingmesh_leaf_pair_30s")
{topology_filter}{candidate_filter}  |> filter(fn: (r) => r._field == "rtt_p99" or r._field == "packet_loss")
  |> last()
  |> group(columns: ["src_leaf", "dst_leaf"])
  |> pivot(rowKey: ["_time", "src_leaf", "dst_leaf"], columnKey: ["_field"], valueColumn: "_value")
"""
        rows = self._query_influx_rows(query, require_value=False)
        rows.sort(
            key=lambda row: (
                float(row.get("packet_loss") or 0),
                float(row.get("rtt_p99") or 0),
            ),
            reverse=True,
        )
        return rows[:limit]

    @staticmethod
    def _pingmesh_flux_string(value: object) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _pingmesh_recent_closed_aggregate_ranges(time_scope: dict) -> list[str]:
        reference = (
            parse_iso8601_timestamp(time_scope["end_time"], "end_time")
            if time_scope["mode"] == "absolute"
            else datetime.now(UTC)
        )
        epoch_seconds = int(reference.timestamp())
        boundary_seconds = (epoch_seconds // 30) * 30
        if reference.microsecond == 0 and epoch_seconds % 30 == 0:
            boundary_seconds -= 30
        boundary = datetime.fromtimestamp(boundary_seconds, tz=UTC)
        scope_start = None
        if time_scope["mode"] == "absolute":
            scope_start = parse_iso8601_timestamp(time_scope["start_time"], "start_time")
        ranges = []
        for candidate in (boundary, boundary - timedelta(seconds=30)):
            if scope_start is not None and candidate < scope_start:
                continue
            if (reference - candidate).total_seconds() > _PINGMESH_AGGREGATE_MAX_AGE_SECONDS:
                continue
            stop = candidate + timedelta(microseconds=1)
            start_text = candidate.isoformat().replace("+00:00", "Z")
            stop_text = stop.isoformat().replace("+00:00", "Z")
            ranges.append(f'  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))\n')
        return ranges

    @staticmethod
    def _pingmesh_aggregate_rows_fresh(rows: list[dict], time_scope: dict) -> bool:
        if not rows:
            return False
        reference = (
            parse_iso8601_timestamp(time_scope["end_time"], "end_time")
            if time_scope["mode"] == "absolute"
            else datetime.now(UTC)
        )
        timestamps = []
        for row in rows:
            try:
                timestamps.append(parse_iso8601_timestamp(str(row.get("_time") or ""), "_time"))
            except ValueError:
                return False
        return all(
            (reference - timestamp).total_seconds() <= _PINGMESH_AGGREGATE_MAX_AGE_SECONDS for timestamp in timestamps
        )

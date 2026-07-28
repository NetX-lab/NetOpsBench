"""Log-oriented device toolkit operations."""

from __future__ import annotations

from netopsbench.platform.observability.influxdb import query_flux

from ..common import ToolResult
from ..observability.pingmesh_scope import parse_iso8601_timestamp
from .log_parsers import get_device_logs_fallback, parse_influx_syslog_rows


class LogOpsMixin:
    def get_device_logs(
        self,
        device: str,
        time_range_minutes: int = 30,
        severity: str | None = None,
        include_raw: bool = False,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> ToolResult:
        try:
            safe_device = self._validate_device_name(device)
            safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
            explicit_start = str(start_time or "").strip()
            explicit_end = str(end_time or "").strip()
            if bool(explicit_start) != bool(explicit_end):
                raise ValueError("start_time and end_time must be provided together")
            start_dt = end_dt = None
            if explicit_start:
                start_dt = parse_iso8601_timestamp(explicit_start, "start_time")
                end_dt = parse_iso8601_timestamp(explicit_end, "end_time")
                if start_dt >= end_dt:
                    raise ValueError("start_time must be earlier than end_time")
                normalized_start = start_dt.isoformat().replace("+00:00", "Z")
                normalized_end = end_dt.isoformat().replace("+00:00", "Z")
                range_clause = (
                    f'  |> range(start: time(v: "{normalized_start}"), ' f'stop: time(v: "{normalized_end}"))'
                )
                time_scope = {
                    "mode": "absolute",
                    "source": "explicit",
                    "start_time": normalized_start,
                    "end_time": normalized_end,
                }
            else:
                range_clause = f"  |> range(start: -{safe_minutes}m)"
                time_scope = {
                    "mode": "rolling",
                    "source": "time_range_minutes",
                    "time_range_minutes": safe_minutes,
                }
            severity_filter = ""
            if severity:
                safe_severity = severity.lower()
                if safe_severity not in self._SEVERITY_OPTIONS:
                    return ToolResult(success=False, data=None, error=f"Invalid severity: {severity}")
                severity_filter = f'|> filter(fn: (r) => r.severity == "{safe_severity}")'
            else:
                safe_severity = None
            query = f"""\nfrom(bucket: "{self.influxdb_bucket}")
{range_clause}
  |> filter(fn: (r) => r._measurement == "syslog")
  |> filter(fn: (r) => r.source == "{safe_device}")
  |> filter(fn: (r) => r._field == "message")
  {severity_filter}
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 100)
"""
            result = query_flux(self.influxdb_url, self.influxdb_token, self.influxdb_org, query)
            if result.status != "ok":
                return ToolResult(success=False, data=None, error=f"InfluxDB query failed: {result.error}")
            structured_logs = parse_influx_syslog_rows(result.text)
            if structured_logs:
                data = {
                    "device": safe_device,
                    "time_range_minutes": safe_minutes,
                    "severity": safe_severity,
                    "source": "influxdb",
                    "logs": structured_logs,
                    "time_scope": time_scope,
                }
                if include_raw:
                    data["raw_csv"] = result.text
                return ToolResult(success=True, data=data)
            fallback_logs = get_device_logs_fallback(
                self,
                safe_device,
                time_range_minutes=safe_minutes,
                severity=safe_severity,
                start_time=start_dt,
                end_time=end_dt,
            )
            warning = None
            source = "influxdb"
            if fallback_logs:
                warning = "No syslog entries were available in InfluxDB for this device/time window; returning a live container log fallback."
                source = "container_logs_fallback"
            data = {
                "device": safe_device,
                "time_range_minutes": safe_minutes,
                "severity": safe_severity,
                "source": source,
                "logs": fallback_logs,
                "warning": warning,
                "time_scope": time_scope,
            }
            if include_raw:
                data["raw_csv"] = result.text
            return ToolResult(success=True, data=data)
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

"""Metrics query helpers for AgentToolkit."""

from __future__ import annotations

from typing import Any

import requests

from ..common import ToolResult


class MetricsOpsMixin:
    def get_interface_metrics(
        self,
        device: str,
        interface: str,
        time_range_minutes: int = 30,
        metric_type: str = "all",
        view: str = "summary",
        max_points: int = 120,
    ) -> ToolResult:
        try:
            safe_device = self._validate_device_name(device)
            safe_interface = self._validate_interface_name(interface)
            safe_minutes = max(1, min(int(time_range_minutes), 24 * 60))
            safe_metric_type = (metric_type or "all").lower()
            safe_view = (view or "summary").lower()
            safe_max_points = max(1, min(int(max_points), 2000))

            allowed_types = {"throughput", "errors", "discards", "phy", "all"}
            if safe_metric_type not in allowed_types:
                return ToolResult(success=False, data=None, error=f"Invalid metric_type: {metric_type}")

            allowed_views = {"summary", "series", "raw"}
            if safe_view not in allowed_views:
                return ToolResult(success=False, data=None, error=f"Invalid view: {view}")

            identities = self._resolve_interface_metric_identities(safe_interface)

            type_fields = {
                "throughput": ["in_octets", "out_octets"],
                "errors": ["in_errors", "out_errors", "in_error_packets", "out_error_packets"],
                "discards": ["in_discarded_packets", "out_discarded_packets"],
                "phy": ["in_fcs_error_packets", "in_error_packets", "out_error_packets"],
            }
            if safe_metric_type == "all":
                fields = sorted({f for vals in type_fields.values() for f in vals})
            else:
                fields = type_fields[safe_metric_type]

            field_filter = " or ".join([f'r._field == "{f}"' for f in fields])
            identity_clauses = [f'r.name == "{n}"' for n in identities["names"]]
            identity_clauses.extend(f'r.path == "{p}"' for p in identities["paths"])
            name_filter = " or ".join(identity_clauses)
            query = f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{safe_minutes}m)
  |> filter(fn: (r) => r._measurement == "interfaces")
  |> filter(fn: (r) => r.source == "{safe_device}")
  |> filter(fn: (r) => {name_filter})
  |> filter(fn: (r) => {field_filter})
  |> aggregateWindow(every: 1m, fn: last, createEmpty: false)
  |> yield(name: "metrics")
"""

            headers = {
                "Authorization": f"Token {self.influxdb_token}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            }

            response = requests.post(
                f"{self.influxdb_url}/api/v2/query?org={self.influxdb_org}",
                headers=headers,
                data=query,
                timeout=30,
                proxies={"http": "", "https": ""},
            )

            if response.status_code != 200:
                return ToolResult(success=False, data=None, error=f"InfluxDB query failed: {response.text}")

            if safe_view == "raw":
                return ToolResult(
                    success=True,
                    data={
                        "device": safe_device,
                        "interface": safe_interface,
                        "time_range_minutes": safe_minutes,
                        "metric_type": safe_metric_type,
                        "view": safe_view,
                        "metrics": response.text,
                    },
                )

            metric_rows = self._parse_influx_metric_rows(response.text)
            if not metric_rows:
                observed_identities = self._get_recent_influx_interface_identities(
                    safe_device,
                    safe_minutes,
                    headers=headers,
                )
                observed_interfaces = sorted(
                    {str(item.get("name")).strip() for item in observed_identities if item.get("name")}
                )
                active_interfaces = self._get_active_interface_names(safe_device)
                missing_active_interfaces = [name for name in active_interfaces if name not in observed_interfaces]
                live_snapshot = self._get_live_interface_snapshot(safe_device, safe_interface)
                warning_parts: list[str] = []
                if observed_interfaces:
                    warning_parts.append(
                        "Interface metrics exist for the device, but per-interface samples are missing for "
                        f"{safe_interface}. Recent Influx interfaces for {safe_device}: "
                        f"{self._preview_items(observed_interfaces)}."
                    )
                else:
                    warning_parts.append(
                        "No interface time-series samples were available in InfluxDB for this device/interface "
                        "window."
                    )
                if missing_active_interfaces:
                    warning_parts.append(
                        "Active interfaces missing from recent Influx data: "
                        f"{self._preview_items(missing_active_interfaces)}."
                    )
                if live_snapshot:
                    warning_parts.append("Returning a live CLI snapshot as fallback context.")
                warning = " ".join(warning_parts) if warning_parts else None
                return ToolResult(
                    success=True,
                    data={
                        "device": safe_device,
                        "interface": safe_interface,
                        "time_range_minutes": safe_minutes,
                        "metric_type": safe_metric_type,
                        "view": safe_view,
                        "summary": {},
                        "series": {},
                        "warning": warning,
                        "current_snapshot": live_snapshot,
                        "fallback_source": "live_cli_snapshot" if live_snapshot else None,
                        "observed_interfaces": observed_interfaces,
                        "active_interfaces": active_interfaces,
                        "missing_active_interfaces": missing_active_interfaces,
                    },
                )

            series: dict[str, list[dict[str, Any]]] = {}
            for row in metric_rows:
                field = row["_field"]
                series.setdefault(field, []).append({"time": row["_time"], "value": row["_value"]})

            for _field, points in series.items():
                points.sort(key=lambda p: p.get("time") or "")

            summary: dict[str, dict[str, Any]] = {}
            for field, points in series.items():
                field_summary = self._summarize_counter_points(field, points)
                if not field_summary:
                    continue
                summary[field] = field_summary

            if safe_view == "summary":
                return ToolResult(
                    success=True,
                    data={
                        "device": safe_device,
                        "interface": safe_interface,
                        "time_range_minutes": safe_minutes,
                        "metric_type": safe_metric_type,
                        "view": safe_view,
                        "summary": summary,
                    },
                )

            if safe_max_points:
                for field, points in series.items():
                    if len(points) > safe_max_points:
                        series[field] = points[-safe_max_points:]
            return ToolResult(
                success=True,
                data={
                    "device": safe_device,
                    "interface": safe_interface,
                    "time_range_minutes": safe_minutes,
                    "metric_type": safe_metric_type,
                    "view": safe_view,
                    "summary": summary,
                    "series": series,
                },
            )
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

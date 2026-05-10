"""Grafana screenshot and panel helpers for AgentToolkit."""

from __future__ import annotations

import base64
import os
from datetime import datetime

import requests

from ..common import ToolResult


class GrafanaOpsMixin:
    def get_grafana_screenshot(
        self,
        panel_name: str,
        time_range: str = "1h",
        width: int = 1000,
        height: int = 500,
        save_to_file: bool = True,
        include_base64: bool = False,
    ) -> ToolResult:
        panel_id = self.panel_mapping.get(panel_name.lower().replace(" ", "_").replace("-", "_"))

        if panel_id is None:
            return ToolResult(
                success=False,
                data=None,
                error=f"Unknown panel: {panel_name}. Available panels: {list(self.panel_mapping.keys())}",
            )

        dashboard_uid = "dcn-overview"

        try:
            render_url = (
                f"{self.grafana_url}/render/d-solo/{dashboard_uid}/"
                f"?orgId=1&panelId={panel_id}"
                f"&from=now-{time_range}&to=now"
                f"&width={width}&height={height}"
                f"&tz=UTC"
            )

            response = requests.get(
                render_url,
                auth=self.grafana_auth,
                timeout=30,
                proxies={"http": "", "https": ""},
            )

            if response.status_code == 200:
                image_data = response.content

                result_data = {
                    "panel_name": panel_name,
                    "panel_id": panel_id,
                    "time_range": time_range,
                    "width": width,
                    "height": height,
                    "content_type": response.headers.get("Content-Type", "image/png"),
                }

                if include_base64:
                    result_data["image_base64"] = base64.b64encode(image_data).decode("utf-8")

                if save_to_file:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{panel_name}_{timestamp}.png"
                    filepath = os.path.join(self.screenshot_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(image_data)
                    result_data["file_path"] = filepath

                return ToolResult(success=True, data=result_data)

            return ToolResult(
                success=False,
                data=None,
                error=(
                    f"Grafana render failed (status {response.status_code}). "
                    "Note: Grafana Image Renderer plugin may need to be installed. "
                    "Use get_grafana_panel_data() for text-based data instead."
                ),
            )

        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_dashboard_screenshot(
        self,
        time_range: str = "1h",
        width: int = 1920,
        height: int = 1080,
        save_to_file: bool = True,
        include_base64: bool = False,
    ) -> ToolResult:
        dashboard_uid = "dcn-overview"

        try:
            render_url = (
                f"{self.grafana_url}/render/d/{dashboard_uid}/dcn-overview"
                f"?orgId=1"
                f"&from=now-{time_range}&to=now"
                f"&width={width}&height={height}"
                f"&tz=UTC"
            )

            response = requests.get(
                render_url,
                auth=self.grafana_auth,
                timeout=60,
                proxies={"http": "", "https": ""},
            )

            if response.status_code == 200:
                image_data = response.content

                result_data = {
                    "dashboard": "DCN Overview",
                    "time_range": time_range,
                    "width": width,
                    "height": height,
                    "content_type": response.headers.get("Content-Type", "image/png"),
                }

                if include_base64:
                    result_data["image_base64"] = base64.b64encode(image_data).decode("utf-8")

                if save_to_file:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"dashboard_{timestamp}.png"
                    filepath = os.path.join(self.screenshot_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(image_data)
                    result_data["file_path"] = filepath

                return ToolResult(success=True, data=result_data)

            return ToolResult(
                success=False,
                data=None,
                error=(
                    f"Dashboard render failed (status {response.status_code}). "
                    "Note: Grafana Image Renderer plugin may need to be installed."
                ),
            )

        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_multiple_panel_screenshots(
        self,
        panel_names: list[str],
        time_range: str = "1h",
        include_base64: bool = False,
    ) -> ToolResult:
        results = []
        errors = []

        for panel_name in panel_names:
            result = self.get_grafana_screenshot(panel_name, time_range, include_base64=include_base64)
            if result.success:
                panel_result = {
                    "panel_name": panel_name,
                    "file_path": result.data.get("file_path"),
                }
                if include_base64 and "image_base64" in result.data:
                    panel_result["image_base64"] = result.data["image_base64"]
                results.append(panel_result)
            else:
                errors.append({"panel_name": panel_name, "error": result.error})

        return ToolResult(
            success=len(results) > 0,
            data={
                "captured": results,
                "failed": errors,
                "total_requested": len(panel_names),
                "total_captured": len(results),
            },
            error=f"Failed to capture {len(errors)} panels" if errors else None,
        )

    def get_troubleshooting_screenshots(self, time_range: str = "30m", include_base64: bool = False) -> ToolResult:
        troubleshooting_panels = [
            "bgp_timeline",
            "interface_in_throughput",
            "interface_out_throughput",
            "physical_errors",
            "logical_discards",
            "queue_drops",
            "syslog_events",
        ]

        return self.get_multiple_panel_screenshots(
            troubleshooting_panels,
            time_range=time_range,
            include_base64=include_base64,
        )

    def get_grafana_panel_data(self, panel_name: str, time_range_minutes: int = 60) -> ToolResult:
        panel_queries = {
            "bgp_session": f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{time_range_minutes}m)
  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r._field == "session_state")
  |> last()
""",
            "interface_throughput": f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{time_range_minutes}m)
  |> filter(fn: (r) => r._measurement == "interfaces")
  |> filter(fn: (r) => r._field == "in_octets" or r._field == "out_octets")
  |> derivative(unit: 1s, nonNegative: true)
  |> map(fn: (r) => ({{ r with _value: r._value * 8.0 }}))
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
""",
            "interface_errors": f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{time_range_minutes}m)
  |> filter(fn: (r) => r._measurement == "interfaces")
  |> filter(fn: (r) => r._field == "in_error_packets" or r._field == "out_error_packets" or r._field == "in_errors" or r._field == "out_errors" or r._field == "in_discarded_packets" or r._field == "out_discarded_packets")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
""",
            "queue_drops": f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{time_range_minutes}m)
  |> filter(fn: (r) => r._measurement == "interfaces")
  |> filter(fn: (r) => r._field == "out_discarded_packets" or r._field == "in_discarded_packets")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
""",
            "cpu_usage": f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{time_range_minutes}m)
  |> filter(fn: (r) => r._measurement == "cpu_usage")
  |> filter(fn: (r) => r._field == "instant")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
""",
            "syslog_events": f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -{time_range_minutes}m)
  |> filter(fn: (r) => r._measurement == "syslog")
  |> filter(fn: (r) => r._field == "message")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
""",
        }

        panel_key = panel_name.lower().replace(" ", "_").replace("-", "_")
        query = None
        for key, q in panel_queries.items():
            if key in panel_key or panel_key in key:
                query = q
                break

        if not query:
            return ToolResult(
                success=False,
                data=None,
                error=f"Unknown panel: {panel_name}. Available panels: {list(panel_queries.keys())}",
            )

        try:
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

            return ToolResult(
                success=True,
                data={
                    "panel_name": panel_name,
                    "time_range_minutes": time_range_minutes,
                    "data": response.text,
                },
            )
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

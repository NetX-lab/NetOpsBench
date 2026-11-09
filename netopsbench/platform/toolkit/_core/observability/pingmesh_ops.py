"""Pingmesh observability helpers for AgentToolkit."""

from __future__ import annotations

import requests

from ..common import ToolResult


class PingmeshOpsMixin:
    def _pingmesh_plan_filter(self) -> str:
        plan_id = self.manifest.require_pingmesh_plan().settings.plan_id
        filters = {"topology_id": self.topology_id, "plan_id": plan_id}
        clauses = []
        for tag, value in filters.items():
            if value:
                safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
                clauses.append(f'  |> filter(fn: (r) => r.{tag} == "{safe}")\n')
        return "".join(clauses)

    def get_pingmesh_summary(
        self,
        time_range_minutes: int = 10,
        start_time: str | None = None,
        end_time: str | None = None,
        src_client: str | None = None,
        dst_client: str | None = None,
    ) -> ToolResult:
        try:
            time_scope = self._resolve_pingmesh_time_scope(
                time_range_minutes=time_range_minutes,
                start_time=start_time,
                end_time=end_time,
            )
            topology_filter = self._pingmesh_plan_filter()
            for tag, client in (("src_name", src_client), ("dst_name", dst_client)):
                if client:
                    self._validate_device_name(client)
                    node = self.manifest.device(client)
                    if node is None or node.role.value != "client":
                        raise ValueError(f"Unknown client: {client}")
                    topology_filter += f'  |> filter(fn: (r) => r.{tag} == "{client}")\n'

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
                    "plan_id": self.manifest.require_pingmesh_plan().settings.plan_id,
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
            topology_filter = self._pingmesh_plan_filter()
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
                    "plan_id": self.manifest.require_pingmesh_plan().settings.plan_id,
                    "time_scope": {key: value for key, value in time_scope.items() if key != "range_clause"},
                    "limit": safe_limit,
                    "hotspots": hotspots,
                },
            )
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, data=None, error=f"Request failed: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

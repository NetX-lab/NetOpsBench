"""Influx/topology helpers for Pingmesh anomaly detector."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO

from netopsbench.platform.observability.influxdb import query_flux

_SNAPSHOT_FIELDS = (
    "rtt_min",
    "rtt_avg",
    "rtt_max",
    "packets_sent",
    "packets_lost",
    "df_packets_sent",
    "df_packets_lost",
    "df_mtu_drops",
    "probe_cycle",
    "destination_batch_index",
    "port_batch_index",
    "rtt_ports_active",
    "rtt_ports_total",
)
_SNAPSHOT_QUERY_TIMEOUT_SECONDS = 60
_SNAPSHOT_SOURCE_SHARD_SIZE = 16
_SNAPSHOT_QUERY_WORKERS = 4


@dataclass(frozen=True)
class SnapshotQueryResult:
    status: str
    rows: list[dict]
    error: str | None = None


class DetectorQueryMixin:
    @staticmethod
    def _safe_flux_string(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _parse_snapshot_csv(text: str) -> list[dict]:
        data_lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        if not data_lines:
            return []
        rows = []
        for row in csv.DictReader(StringIO("\n".join(data_lines))):
            if row.get("_time") in (None, "", "_time"):
                continue
            parsed = {key: value for key, value in row.items() if key and value not in (None, "")}
            for field in _SNAPSHOT_FIELDS:
                if field not in parsed:
                    continue
                try:
                    parsed[field] = float(parsed[field])
                except (TypeError, ValueError):
                    parsed.pop(field, None)
            rows.append(parsed)
        return rows

    def _query_snapshot_shard(
        self,
        start_time: str,
        end_time: str,
        source_names: list[str],
    ) -> SnapshotQueryResult:
        bucket = self._safe_flux_string(self.bucket)
        start = self._safe_flux_string(start_time)
        end = self._safe_flux_string(end_time)
        fields = " or ".join(f'r._field == "{field}"' for field in _SNAPSHOT_FIELDS)
        source_filter = ""
        if source_names:
            predicates = " or ".join(
                f'r.src_name == "{self._safe_flux_string(source)}"' for source in source_names
            )
            source_filter = f"  |> filter(fn: (r) => {predicates})\n"
        query = (
            f'from(bucket: "{bucket}")\n'
            f'  |> range(start: time(v: "{start}"), stop: time(v: "{end}"))\n'
            '  |> filter(fn: (r) => r._measurement == "pingmesh")\n'
            + self._topology_filter()
            + source_filter
            + f"  |> filter(fn: (r) => {fields})\n"
            '  |> keep(columns: ["_time", "_field", "_value", "src_ip", "dst_ip", '
            '"src_name", "dst_name", "src_leaf", "dst_leaf", "path_type"])\n'
            '  |> pivot(rowKey: ["_time", "src_ip", "dst_ip", "src_name", "dst_name", '
            '"src_leaf", "dst_leaf", "path_type"], columnKey: ["_field"], valueColumn: "_value")\n'
        )
        result = query_flux(
            self.influxdb_url,
            self.token,
            self.org,
            query,
            timeout=_SNAPSHOT_QUERY_TIMEOUT_SECONDS,
        )
        if result.status != "ok":
            return SnapshotQueryResult(status="error", rows=[], error=result.error)
        return SnapshotQueryResult(status="ok", rows=self._parse_snapshot_csv(result.text))

    def _query_snapshot(self, start_time: str, end_time: str) -> SnapshotQueryResult:
        sources = list(dict.fromkeys(self._pingmesh_clients))
        shards = [
            sources[index : index + _SNAPSHOT_SOURCE_SHARD_SIZE]
            for index in range(0, len(sources), _SNAPSHOT_SOURCE_SHARD_SIZE)
        ] or [[]]

        if len(shards) == 1:
            results = [self._query_snapshot_shard(start_time, end_time, shards[0])]
        else:
            workers = min(_SNAPSHOT_QUERY_WORKERS, len(shards))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(
                    executor.map(
                        lambda shard: self._query_snapshot_shard(start_time, end_time, shard),
                        shards,
                    )
                )

        failures = [
            (index, result)
            for index, result in enumerate(results, start=1)
            if result.status != "ok"
        ]
        if failures:
            errors = [
                f"snapshot shard {index}/{len(shards)} failed: {result.error or 'query_failed'}"
                for index, result in failures
            ]
            return SnapshotQueryResult(status="error", rows=[], error="; ".join(errors))

        rows = [row for result in results for row in result.rows]
        rows.sort(
            key=lambda row: (
                str(row.get("_time") or ""),
                str(row.get("src_name") or ""),
                str(row.get("dst_name") or ""),
                str(row.get("src_ip") or ""),
                str(row.get("dst_ip") or ""),
            )
        )
        return SnapshotQueryResult(status="ok", rows=rows)

    def _topology_filter(self) -> str:
        if not self.topology_id:
            return ""
        safe = str(self.topology_id).replace("\\", "\\\\").replace('"', '\\"')
        return f'  |> filter(fn: (r) => r.topology_id == "{safe}")\n'

    def _load_topology_metadata(self, metadata: dict) -> None:
        devices = metadata.get("devices", {}) if isinstance(metadata, dict) else {}
        clients = devices.get("clients", []) if isinstance(devices, dict) else []
        for client in clients:
            name = client.get("name")
            leaf = client.get("leaf")
            if isinstance(name, str) and isinstance(leaf, str) and name and leaf:
                self.client_to_leaf[name] = leaf

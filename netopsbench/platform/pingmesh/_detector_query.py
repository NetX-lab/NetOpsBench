"""Influx/topology helpers for Pingmesh anomaly detector."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass

from netopsbench.platform.observability.influxdb import iter_annotated_csv_rows, query_flux

_SNAPSHOT_FIELDS = (
    "rtt_min",
    "rtt_avg",
    "rtt_max",
    "packets_sent",
    "packets_lost",
    "df_packets_sent",
    "df_packets_lost",
    "df_mtu_drops",
    "local_probe_errors",
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
    request_count: int = 0


class DetectorQueryMixin:
    bucket: str
    influxdb_url: str
    token: str
    org: str
    topology_id: str | None
    _pingmesh_clients: list[str]
    client_to_leaf: dict[str, str]

    @staticmethod
    def _safe_flux_string(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _parse_snapshot_csv(text: str) -> list[dict]:
        rows = []
        for row in iter_annotated_csv_rows(text):
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
        *,
        deadline: float | None = None,
        fields: tuple[str, ...] | None = None,
    ) -> SnapshotQueryResult:
        if deadline is not None and time.monotonic() >= deadline:
            return SnapshotQueryResult(status="error", rows=[], error="snapshot query deadline exceeded")
        bucket = self._safe_flux_string(self.bucket)
        start = self._safe_flux_string(start_time)
        end = self._safe_flux_string(end_time)
        field_filter = " or ".join(f'r._field == "{field}"' for field in (fields or _SNAPSHOT_FIELDS))
        source_filter = ""
        if source_names:
            predicates = " or ".join(f'r.src_name == "{self._safe_flux_string(source)}"' for source in source_names)
            source_filter = f"  |> filter(fn: (r) => {predicates})\n"
        query = (
            f'from(bucket: "{bucket}")\n'
            f'  |> range(start: time(v: "{start}"), stop: time(v: "{end}"))\n'
            '  |> filter(fn: (r) => r._measurement == "pingmesh")\n'
            + self._topology_filter()
            + source_filter
            + f"  |> filter(fn: (r) => {field_filter})\n"
            '  |> keep(columns: ["_time", "_field", "_value", "src_ip", "dst_ip", '
            '"src_name", "dst_name", "src_leaf", "dst_leaf", "path_type", "plan_id"])\n'
            '  |> pivot(rowKey: ["_time", "src_ip", "dst_ip", "src_name", "dst_name", '
            '"src_leaf", "dst_leaf", "path_type", "plan_id"], columnKey: ["_field"], valueColumn: "_value")\n'
        )
        result = query_flux(
            self.influxdb_url,
            self.token,
            self.org,
            query,
            timeout=_SNAPSHOT_QUERY_TIMEOUT_SECONDS,
            **({"deadline": deadline} if deadline is not None else {}),
        )
        if result.status != "ok":
            return SnapshotQueryResult(status="error", rows=[], error=result.error, request_count=1)
        return SnapshotQueryResult(status="ok", rows=self._parse_snapshot_csv(result.text), request_count=1)

    def _query_snapshot(
        self, start_time: str, end_time: str, *, deadline: float | None = None, fields: tuple[str, ...] | None = None
    ) -> SnapshotQueryResult:
        sources = list(dict.fromkeys(self._pingmesh_clients))
        shards = [
            sources[index : index + _SNAPSHOT_SOURCE_SHARD_SIZE]
            for index in range(0, len(sources), _SNAPSHOT_SOURCE_SHARD_SIZE)
        ] or [[]]

        def query(shard):
            return self._query_snapshot_shard(
                start_time,
                end_time,
                shard,
                **({"deadline": deadline} if deadline is not None else {}),
                **({"fields": fields} if fields is not None else {}),
            )

        if len(shards) == 1:
            results = [query(shards[0])]
        else:
            workers = min(_SNAPSHOT_QUERY_WORKERS, len(shards))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(copy_context().run, query, shard) for shard in shards]
                results = [future.result() for future in futures]

        request_count = sum(result.request_count for result in results)
        failures = [(index, result) for index, result in enumerate(results, start=1) if result.status != "ok"]
        if failures:
            errors = [
                f"snapshot shard {index}/{len(shards)} failed: {result.error or 'query_failed'}"
                for index, result in failures
            ]
            return SnapshotQueryResult(status="error", rows=[], error="; ".join(errors), request_count=request_count)

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
        return SnapshotQueryResult(status="ok", rows=rows, request_count=request_count)

    def _topology_filter(self) -> str:
        filters = {"topology_id": self.topology_id, "plan_id": self.plan_id}
        return "".join(
            f'  |> filter(fn: (r) => r.{tag} == "{self._safe_flux_string(value)}")\n'
            for tag, value in filters.items()
            if value
        )

"""Shared InfluxDB lifecycle and Flux query client."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Literal

import requests

from netopsbench.logging_utils import get_logger
from netopsbench.models.runtime import safe_runtime_label

logger = get_logger(__name__)


@dataclass(frozen=True)
class FluxQueryResult:
    status: Literal["ok", "error"]
    text: str = ""
    error: str | None = None


def query_flux(
    base_url: str,
    token: str,
    org: str,
    query: str,
    *,
    timeout: int = 30,
) -> FluxQueryResult:
    """Execute one Flux query without interpreting its domain-specific CSV."""
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/v2/query",
            params={"org": org},
            headers=headers,
            data=query,
            timeout=timeout,
            proxies={"http": "", "https": ""},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return FluxQueryResult(status="error", error=f"{type(exc).__name__}: {exc}")
    return FluxQueryResult(status="ok", text=response.text)


def _make_url_opener(url: str):
    hostname = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _request(url: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = None
    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with _make_url_opener(url).open(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def ensure_bucket(base_url: str, token: str, org: str, bucket: str, retries: int = 20, delay: float = 2.0) -> None:
    base = base_url.rstrip("/")
    bucket_q = urllib.parse.quote(bucket, safe="")
    org_q = urllib.parse.quote(org, safe="")

    last_error = None
    for _ in range(retries):
        try:
            existing = _request(f"{base}/api/v2/buckets?name={bucket_q}", token)
            for item in existing.get("buckets", []) or []:
                if item.get("name") == bucket:
                    logger.info("InfluxDB bucket already exists: %s", bucket)
                    return

            orgs = _request(f"{base}/api/v2/orgs?org={org_q}", token)
            matches = orgs.get("orgs", []) or []
            if not matches:
                raise RuntimeError(f"InfluxDB organization not found: {org}")
            org_id = matches[0].get("id")
            if not org_id:
                raise RuntimeError(f"InfluxDB organization has no id: {org}")

            _request(
                f"{base}/api/v2/buckets",
                token,
                method="POST",
                payload={"orgID": org_id, "name": bucket},
            )
            logger.info("Created InfluxDB bucket: %s", bucket)
            return
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as exc:
            last_error = exc
            time.sleep(delay)

    raise RuntimeError(f"Failed to ensure InfluxDB bucket '{bucket}': {last_error}")


def pingmesh_aggregate_task_names(runtime_id: str, worker_index: int) -> tuple[str, str]:
    prefix = f"netopsbench-{safe_runtime_label(runtime_id)}-w{int(worker_index):02d}"
    return f"{prefix}-pingmesh-path-v1", f"{prefix}-pingmesh-leaf-pair-v1"


def build_pingmesh_aggregate_task_flux(
    *,
    name: str,
    bucket: str,
    org: str,
    topology_id: str,
    measurement: str,
    dimensions: tuple[str, ...],
) -> str:
    def flux_string(value: object) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    group_columns = ", ".join(f'"{item}"' for item in ("topology_id", *dimensions, "_field"))
    tag_columns = ", ".join(f'"{item}"' for item in ("topology_id", *dimensions))
    aggregate = f'''import "date"

option task = {{name: "{flux_string(name)}", every: 10s}}

windowStop = date.truncate(t: now(), unit: 30s)
windowStart = date.sub(d: 2m, from: windowStop)

aggregate = from(bucket: "{flux_string(bucket)}")
  |> range(start: windowStart, stop: windowStop)
  |> filter(fn: (r) => r._measurement == "pingmesh")
  |> filter(fn: (r) => r.topology_id == "{flux_string(topology_id)}")
  |> filter(fn: (r) => r._field == "rtt_p99" or r._field == "packet_loss")
  |> group(columns: [{group_columns}])
  |> aggregateWindow(every: 30s, fn: mean, createEmpty: false, timeSrc: "_stop")
  |> set(key: "_measurement", value: "{flux_string(measurement)}")
'''
    if measurement != "pingmesh_leaf_pair_30s":
        return (
            aggregate
            + f'''
aggregate
  |> to(bucket: "{flux_string(bucket)}", org: "{flux_string(org)}", tagColumns: [{tag_columns}])
'''
        )
    return (
        aggregate
        + f'''
aggregate
  |> group(columns: ["topology_id", "src_leaf", "dst_leaf"])
  |> pivot(rowKey: ["_time", "topology_id", "src_leaf", "dst_leaf"], columnKey: ["_field"], valueColumn: "_value")
  |> set(key: "_measurement", value: "{flux_string(measurement)}")
  |> map(fn: (r) => ({{r with hotspot_score: (float(v: r.packet_loss) * 1000000000000.0) + float(v: r.rtt_p99)}}))
  |> to(
      bucket: "{flux_string(bucket)}",
      org: "{flux_string(org)}",
      tagColumns: [{tag_columns}],
      fieldFn: (r) => ({{
          "rtt_p99": float(v: r.rtt_p99),
          "packet_loss": float(v: r.packet_loss),
          "hotspot_score": r.hotspot_score,
      }}),
  )
'''
    )


def _influx_org_id(base: str, token: str, org: str) -> str:
    org_q = urllib.parse.quote(org, safe="")
    payload = _request(f"{base}/api/v2/orgs?org={org_q}", token)
    matches = payload.get("orgs", []) or []
    if not matches or not matches[0].get("id"):
        raise RuntimeError(f"InfluxDB organization not found: {org}")
    return str(matches[0]["id"])


def ensure_influx_task(base_url: str, token: str, org: str, name: str, flux: str) -> str:
    """Create or update one named Influx Task and return its id."""
    base = base_url.rstrip("/")
    name_q = urllib.parse.quote(name, safe="")
    existing = _request(f"{base}/api/v2/tasks?name={name_q}", token)
    matches = [item for item in existing.get("tasks", []) or [] if item.get("name") == name]
    payload = {"flux": flux, "status": "active"}
    if matches:
        task_id = str(matches[0].get("id") or "")
        if not task_id:
            raise RuntimeError(f"InfluxDB task has no id: {name}")
        _request(f"{base}/api/v2/tasks/{urllib.parse.quote(task_id, safe='')}", token, method="PATCH", payload=payload)
        return task_id
    payload["orgID"] = _influx_org_id(base, token, org)
    created = _request(f"{base}/api/v2/tasks", token, method="POST", payload=payload)
    task_id = str(created.get("id") or "")
    if not task_id:
        raise RuntimeError(f"InfluxDB task creation returned no id: {name}")
    return task_id


def ensure_pingmesh_aggregate_tasks(
    base_url: str,
    token: str,
    org: str,
    *,
    bucket: str,
    topology_id: str,
    runtime_id: str,
    worker_index: int,
) -> list[str]:
    path_name, leaf_name = pingmesh_aggregate_task_names(runtime_id, worker_index)
    specs = (
        (path_name, "pingmesh_path_type_30s", ("path_type",)),
        (leaf_name, "pingmesh_leaf_pair_30s", ("src_leaf", "dst_leaf")),
    )
    return [
        ensure_influx_task(
            base_url,
            token,
            org,
            name,
            build_pingmesh_aggregate_task_flux(
                name=name,
                bucket=bucket,
                org=org,
                topology_id=topology_id,
                measurement=measurement,
                dimensions=dimensions,
            ),
        )
        for name, measurement, dimensions in specs
    ]


def delete_pingmesh_aggregate_tasks(
    base_url: str,
    token: str,
    *,
    runtime_id: str,
    worker_index: int,
) -> list[str]:
    base = base_url.rstrip("/")
    deleted: list[str] = []
    for name in pingmesh_aggregate_task_names(runtime_id, worker_index):
        name_q = urllib.parse.quote(name, safe="")
        existing = _request(f"{base}/api/v2/tasks?name={name_q}", token)
        for item in existing.get("tasks", []) or []:
            if item.get("name") != name or not item.get("id"):
                continue
            task_id = str(item["id"])
            _request(
                f"{base}/api/v2/tasks/{urllib.parse.quote(task_id, safe='')}",
                token,
                method="DELETE",
            )
            deleted.append(task_id)
    return deleted


__all__ = [
    "FluxQueryResult",
    "build_pingmesh_aggregate_task_flux",
    "delete_pingmesh_aggregate_tasks",
    "ensure_bucket",
    "ensure_influx_task",
    "ensure_pingmesh_aggregate_tasks",
    "pingmesh_aggregate_task_names",
    "query_flux",
]

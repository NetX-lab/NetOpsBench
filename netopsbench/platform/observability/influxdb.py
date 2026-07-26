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

logger = get_logger(__name__)

DEFAULT_MANAGED_BUCKET_RETENTION_SECONDS = 7 * 24 * 60 * 60


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


def wait_for_influxdb_ready(
    base_url: str,
    *,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 0.5,
) -> None:
    """Wait for the shared InfluxDB API to report a passing health status."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    health_url = f"{base_url.rstrip('/')}/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with _make_url_opener(health_url).open(health_url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "pass":
                return
            last_error = RuntimeError(f"unexpected health status: {payload.get('status')!r}")
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(max(0.0, poll_interval_seconds))
    raise RuntimeError(f"InfluxDB did not become ready at {health_url}: {last_error}")


def ensure_bucket(
    base_url: str,
    token: str,
    org: str,
    bucket: str,
    retries: int = 20,
    delay: float = 2.0,
    *,
    retention_seconds: int | None = None,
) -> bool:
    """Create a bucket if absent.

    ``retention_seconds`` is intentionally opt-in so callers attaching to a
    user-owned bucket never mutate its retention policy.
    """
    if retention_seconds is not None and retention_seconds <= 0:
        raise ValueError("retention_seconds must be positive")

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
                    return False

            orgs = _request(f"{base}/api/v2/orgs?org={org_q}", token)
            matches = orgs.get("orgs", []) or []
            if not matches:
                raise RuntimeError(f"InfluxDB organization not found: {org}")
            org_id = matches[0].get("id")
            if not org_id:
                raise RuntimeError(f"InfluxDB organization has no id: {org}")

            payload: dict[str, object] = {"orgID": org_id, "name": bucket}
            if retention_seconds is not None:
                payload["retentionRules"] = [{"type": "expire", "everySeconds": int(retention_seconds)}]
            _request(
                f"{base}/api/v2/buckets",
                token,
                method="POST",
                payload=payload,
            )
            logger.info("Created InfluxDB bucket: %s", bucket)
            return True
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as exc:
            last_error = exc
            time.sleep(delay)

    raise RuntimeError(f"Failed to ensure InfluxDB bucket '{bucket}': {last_error}")


def delete_bucket(base_url: str, token: str, bucket: str) -> bool:
    """Delete one exact bucket name, returning false when it does not exist."""
    base = base_url.rstrip("/")
    bucket_q = urllib.parse.quote(bucket, safe="")
    existing = _request(f"{base}/api/v2/buckets?name={bucket_q}", token)
    match = next((item for item in existing.get("buckets", []) or [] if item.get("name") == bucket), None)
    if match is None:
        return False
    bucket_id = match.get("id")
    if not bucket_id:
        raise RuntimeError(f"InfluxDB bucket has no id: {bucket}")
    _request(f"{base}/api/v2/buckets/{urllib.parse.quote(str(bucket_id), safe='')}", token, method="DELETE")
    return True


__all__ = [
    "FluxQueryResult",
    "DEFAULT_MANAGED_BUCKET_RETENTION_SECONDS",
    "delete_bucket",
    "ensure_bucket",
    "query_flux",
    "wait_for_influxdb_ready",
]

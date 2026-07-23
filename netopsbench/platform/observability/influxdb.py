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


__all__ = [
    "FluxQueryResult",
    "ensure_bucket",
    "query_flux",
]

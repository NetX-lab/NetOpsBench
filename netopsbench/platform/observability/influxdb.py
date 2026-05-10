#!/usr/bin/env python3
"""Create an InfluxDB bucket if it does not already exist."""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from netopsbench.config import config
from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an InfluxDB bucket if needed")
    parser.add_argument("--url", default="http://localhost:8086", help="InfluxDB base URL")
    parser.add_argument("--token", default=config.influxdb_token, help="InfluxDB token")
    parser.add_argument("--org", default=config.influxdb_org, help="InfluxDB organization")
    parser.add_argument("--bucket", required=True, help="Bucket name")
    parser.add_argument("--retries", type=int, default=20, help="Retry count while waiting for InfluxDB")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between retries in seconds")
    args = parser.parse_args()

    ensure_bucket(args.url, args.token, args.org, args.bucket, retries=args.retries, delay=args.delay)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI error path
        logger.error("%s", exc)
        raise SystemExit(1) from None

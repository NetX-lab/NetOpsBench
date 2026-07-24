import pytest

from netopsbench.platform.observability import influxdb


def test_ensure_bucket_applies_retention_only_when_creating_managed_bucket(monkeypatch):
    calls = []

    def fake_request(url, token, method="GET", payload=None):
        calls.append((url, method, payload))
        if "/buckets?name=" in url:
            return {"buckets": []}
        if "/orgs?org=" in url:
            return {"orgs": [{"id": "org-id"}]}
        return {}

    monkeypatch.setattr(influxdb, "_request", fake_request)

    created = influxdb.ensure_bucket(
        "http://influxdb:8086",
        "token",
        "netopsbench",
        "runtime-xs",
        retries=1,
        delay=0,
        retention_seconds=influxdb.DEFAULT_MANAGED_BUCKET_RETENTION_SECONDS,
    )

    create = next(call for call in calls if call[1] == "POST")
    assert created is True
    assert create[2]["retentionRules"] == [
        {
            "type": "expire",
            "everySeconds": 7 * 24 * 60 * 60,
        }
    ]


def test_ensure_bucket_does_not_mutate_existing_bucket_retention(monkeypatch):
    calls = []

    def fake_request(url, token, method="GET", payload=None):
        calls.append((url, method, payload))
        return {"buckets": [{"id": "bucket-id", "name": "attached"}]}

    monkeypatch.setattr(influxdb, "_request", fake_request)

    created = influxdb.ensure_bucket(
        "http://influxdb:8086",
        "token",
        "netopsbench",
        "attached",
        retries=1,
        delay=0,
        retention_seconds=influxdb.DEFAULT_MANAGED_BUCKET_RETENTION_SECONDS,
    )

    assert created is False
    assert all(method != "POST" for _, method, _ in calls)


def test_invalid_retention_fails_before_network_retry(monkeypatch):
    monkeypatch.setattr(
        influxdb,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("invalid local input must not reach InfluxDB"),
    )

    with pytest.raises(ValueError, match="must be positive"):
        influxdb.ensure_bucket(
            "http://influxdb:8086",
            "token",
            "netopsbench",
            "managed",
            retention_seconds=0,
        )


def test_delete_bucket_uses_exact_lookup_and_server_bucket_id(monkeypatch):
    calls = []

    def fake_request(url, token, method="GET", payload=None):
        calls.append((url, method))
        if method == "GET":
            return {
                "buckets": [
                    {"id": "other-id", "name": "managed-suffix"},
                    {"id": "managed-id", "name": "managed"},
                ]
            }
        return {}

    monkeypatch.setattr(influxdb, "_request", fake_request)

    assert influxdb.delete_bucket("http://influxdb:8086", "token", "managed") is True
    assert calls[-1] == ("http://influxdb:8086/api/v2/buckets/managed-id", "DELETE")


def test_delete_bucket_does_not_delete_when_exact_name_is_absent(monkeypatch):
    calls = []

    def fake_request(url, token, method="GET", payload=None):
        calls.append((url, method))
        return {"buckets": [{"id": "other-id", "name": "managed-suffix"}]}

    monkeypatch.setattr(influxdb, "_request", fake_request)

    assert influxdb.delete_bucket("http://influxdb:8086", "token", "managed") is False
    assert all(method != "DELETE" for _, method in calls)

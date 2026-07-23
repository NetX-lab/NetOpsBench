"""Pingmesh ingest backpressure contract."""

from __future__ import annotations

from types import SimpleNamespace

from netopsbench.platform.pingmesh._agent_influx import PingInfluxMixin


class _Writer(PingInfluxMixin):
    def __init__(self, statuses: list[int]) -> None:
        self.use_influxdb = True
        self.influxdb_url = "http://telegraf:8186"
        self.influxdb_org = "org"
        self.influxdb_bucket = "bucket"
        self.max_retries = 3
        self.retry_backoff_base = 0
        self.responses = list(statuses)
        self.calls = 0
        self.session = self

    def post(self, *_args, **_kwargs):
        self.calls += 1
        status = self.responses.pop(0)
        return SimpleNamespace(status_code=status, text="")


def test_pingmesh_ingest_retries_telegraf_backpressure():
    writer = _Writer([429, 204])

    assert writer._write_to_influxdb(["pingmesh value=1"]) is True
    assert writer.calls == 2


def test_pingmesh_ingest_does_not_retry_permanent_client_error():
    writer = _Writer([400, 204])

    assert writer._write_to_influxdb(["pingmesh value=1"]) is False
    assert writer.calls == 1


def test_pingmesh_ingest_fails_after_temporary_error_retries_are_exhausted():
    writer = _Writer([503, 503, 503])

    assert writer._write_to_influxdb(["pingmesh value=1"]) is False
    assert writer.calls == 3

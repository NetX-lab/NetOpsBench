"""Tests for the shared native client-agent control transport."""

from __future__ import annotations

import json

import pytest

from netopsbench.platform.client_agent import control


class _Connection:
    def __init__(self, response: bytes):
        self.response = bytearray(response)
        self.request = b""
        self.timeout = 0.0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def sendall(self, request: bytes) -> None:
        self.request = request

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, size: int) -> bytes:
        chunk = bytes(self.response[:size])
        del self.response[:size]
        return chunk


def test_request_agent_sends_and_validates_one_json_line(monkeypatch):
    connection = _Connection(b'{"protocol_version":1,"ok":true,"status":{"ready":true}}\n')
    monkeypatch.setattr(control.socket, "create_connection", lambda address, timeout: connection)

    response = control.request_agent(
        "172.20.20.101",
        9910,
        "status",
        timeout=3.0,
    )

    assert response["status"]["ready"] is True
    assert json.loads(connection.request) == {
        "op": "status",
        "payload": {},
        "protocol_version": 1,
    }
    assert connection.timeout == 3.0


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (b"", "empty response"),
        (b"[]\n", "non-object"),
        (b'{"protocol_version":2,"ok":true}\n', "protocol mismatch"),
        (b'{"protocol_version":1,"ok":false,"error":"not ready"}\n', "not ready"),
    ],
)
def test_request_agent_rejects_invalid_contract(monkeypatch, response, message):
    monkeypatch.setattr(
        control.socket,
        "create_connection",
        lambda address, timeout: _Connection(response),
    )

    with pytest.raises(RuntimeError, match=message):
        control.request_agent("172.20.20.101", 9910, "status")

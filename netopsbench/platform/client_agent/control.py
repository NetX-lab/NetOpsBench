"""Internal JSON-line control client for native client-agent processes."""

from __future__ import annotations

import json
import socket
from typing import Any

from .contract import CONTROL_PROTOCOL_VERSION

MAX_CONTROL_RESPONSE_BYTES = 65_536


def request_agent(
    host: str,
    port: int,
    operation: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Send one native-agent request and return its validated response."""
    request = (
        json.dumps(
            {
                "protocol_version": CONTROL_PROTOCOL_VERSION,
                "op": operation,
                "payload": payload or {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(request)
        connection.settimeout(timeout)
        response = bytearray()
        while b"\n" not in response:
            chunk = connection.recv(min(4096, MAX_CONTROL_RESPONSE_BYTES + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_CONTROL_RESPONSE_BYTES:
                raise RuntimeError("native client-agent response exceeds 65536 bytes")
    if not response:
        raise RuntimeError("native client-agent returned an empty response")
    decoded = json.loads(bytes(response).splitlines()[0])
    if not isinstance(decoded, dict):
        raise RuntimeError("native client-agent returned a non-object response")
    if decoded.get("protocol_version") != CONTROL_PROTOCOL_VERSION:
        raise RuntimeError("native client-agent protocol mismatch")
    if decoded.get("ok") is not True:
        raise RuntimeError(str(decoded.get("error") or "native client-agent request failed"))
    return decoded


__all__ = ["request_agent"]

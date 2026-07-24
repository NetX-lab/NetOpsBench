"""Parse FRR BGP summary output for collectors and device tools."""

from __future__ import annotations

import ipaddress
import re
from typing import Any


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _coerce_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def parse_bgp_summary(text: str) -> list[dict[str, Any]]:
    """Return normalized neighbor rows from ``show ip bgp summary`` output."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("Neighbor") and "State" in line),
        None,
    )
    if header_index is None:
        return []

    headers = lines[header_index].split()
    rows: list[dict[str, Any]] = []
    for line in lines[header_index + 1 :]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ipaddress.ip_address(parts[0])
        except ValueError:
            continue
        if len(parts) > len(headers):
            parts = parts[: len(headers) - 1] + [" ".join(parts[len(headers) - 1 :])]
        row = {_normalize_key(key): value for key, value in zip(headers, parts, strict=False)}
        state_value = row.get("state_pfxrcd")
        state_text = str(state_value) if state_value is not None else ""
        established = state_text.isdigit()
        rows.append(
            {
                "neighbor": row.get("neighbor"),
                "asn": _coerce_value(row.get("as")),
                "state": "Established" if established else state_value,
                "prefixes_received": int(state_text) if established else None,
                "up_down": row.get("up_down"),
                "msg_rcvd": _coerce_value(row.get("msgrcvd")),
                "msg_sent": _coerce_value(row.get("msgsent")),
                "in_q": _coerce_value(row.get("inq")),
                "out_q": _coerce_value(row.get("outq")),
            }
        )
    return rows


__all__ = ["parse_bgp_summary"]

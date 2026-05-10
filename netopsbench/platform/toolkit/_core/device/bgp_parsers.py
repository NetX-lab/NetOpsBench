"""BGP parsing helpers for device toolkit internals."""

from __future__ import annotations

import ipaddress
from typing import Any

from .text_parsers import coerce_value, normalize_key


def parse_bgp_summary(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    header_idx = None
    for idx, line in enumerate(lines):
        if line.startswith("Neighbor") and "State" in line:
            header_idx = idx
            break
    if header_idx is None:
        return []
    headers = lines[header_idx].split()
    rows: list[dict[str, Any]] = []
    for line in lines[header_idx + 1 :]:
        parts = line.split()
        if len(parts) < 2:
            continue
        neighbor = parts[0].strip()
        try:
            ipaddress.ip_address(neighbor)
        except ValueError:
            continue
        if len(parts) > len(headers):
            parts = parts[: len(headers) - 1] + [" ".join(parts[len(headers) - 1 :])]
        row = {normalize_key(k): v for k, v in zip(headers, parts, strict=False)}
        state_value = row.get("state_pfxrcd") or row.get("state_pfxrcd".lower())
        prefixes = None
        state = None
        if state_value is not None and str(state_value).isdigit():
            prefixes = int(state_value)
            state = "Established"
        else:
            state = state_value
        rows.append(
            {
                "neighbor": row.get("neighbor"),
                "asn": coerce_value(row.get("as")),
                "state": state,
                "prefixes_received": prefixes,
                "up_down": row.get("up_down"),
                "msg_rcvd": coerce_value(row.get("msgrcvd")),
                "msg_sent": coerce_value(row.get("msgsent")),
                "in_q": coerce_value(row.get("inq")),
                "out_q": coerce_value(row.get("outq")),
            }
        )
    return rows
